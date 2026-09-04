#!/usr/bin/env python3
"""Memory OS Embedding HTTP Service (MLX 版本)

常驻 daemon，监听 127.0.0.1:8765，暴露 /embed 端点。
模型：mlx-community/bge-m3-mlx-8bit（mlx_embeddings 加载，Metal 加速）。
空闲一定时间后自动卸载模型释放内存。
"""

import os
import sys
import json
import argparse
import signal
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# 用 ctypes 改进程名（macOS Activity Monitor 显示用）
try:
    from ctypes import CDLL
    libc = CDLL(None)
    libc.setproctitle(b"embed-daemon")
except Exception:
    pass

LOG = logging.getLogger("embed-daemon")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

PROMPT_PREFIX = "Represent this sentence for searching: "

# 进程级共享状态：请求时间戳（供 idle monitor 线程读取）
_shared_last_active = [time.time()]


class MLXEmbeddingModel:
    """BGE-M3 MLX 版本（mlx_embeddings）"""

    def __init__(self, model_path: str):
        from mlx_embeddings.utils import load_model, load_tokenizer

        model_path = Path(model_path)
        LOG.info("loading model from %s ...", model_path)
        t0 = time.time()
        self.model = load_model(model_path)
        self.tokenizer = load_tokenizer(model_path)
        LOG.info("model loaded in %.1fs", time.time() - t0)

    def encode(self, text: str) -> list:
        import mlx.core as mx

        prompt = PROMPT_PREFIX + text
        tokens = self.tokenizer.encode(prompt)
        input_ids = mx.array([tokens])
        output = self.model(input_ids)
        embedding = output.last_hidden_state.mean(axis=1)
        result = embedding[0].tolist()
        mx.clear_cache()
        return result

    def encode_batch(self, texts: list) -> list:
        return [self.encode(text) for text in texts]

    def unload(self):
        import mlx.core as mx
        del self.model
        del self.tokenizer
        self.model = None
        self.tokenizer = None
        self.service = None
        mx.clear_cache()
        LOG.info("model unloaded, cache cleared")


class EmbedHandler(BaseHTTPRequestHandler):
    service: MLXEmbeddingModel = None
    model_path: str = None

    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _record_activity(self):
        _shared_last_active[0] = time.time()

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {
                "status": "ok",
                "service": "memory-os-embed-mlx",
                "model_loaded": self.service is not None,
                "model_path": self.model_path,
            })
        elif self.path == "/status":
            self._json(200, {
                "model_loaded": self.service is not None,
                "model_path": self.model_path,
            })
        else:
            self._json(404, {"error": "not found", "endpoints": ["/embed (POST)", "/health", "/status"]})

    def do_POST(self):
        if self.path == "/embed":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                texts = data.get("texts") or data.get("text") or []
                if isinstance(texts, str):
                    texts = [texts]
                if not texts:
                    self._json(400, {"error": "missing 'texts' or 'text' field"})
                    return
                self._record_activity()
                self._ensure_model()
                LOG.info("embedding %d texts", len(texts))
                vectors = self.service.encode_batch(texts)
                self._json(200, {"vectors": vectors, "count": len(vectors)})
            except Exception as e:
                LOG.exception("embed failed")
                self._json(500, {"error": str(e)})
        else:
            self._json(404, {"error": "not found"})

    def _ensure_model(self):
        if self.service is None or self.service.tokenizer is None:
            LOG.info("loading model on demand ...")
            self.service = MLXEmbeddingModel(self.model_path)
            _shared_last_active[0] = time.time()

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _idle_monitor(idle_timeout: int, shutdown_event: threading.Event):
    """空闲监控：idle_timeout 秒无请求则卸载模型，进程保持常驻。"""
    import signal
    pid = os.getpid()
    while not shutdown_event.is_set():
        shutdown_event.wait(timeout=15)
        if shutdown_event.is_set():
            break
        idle = time.time() - _shared_last_active[0]
        if idle >= idle_timeout and EmbedHandler.service is not None:
            LOG.info("idle %.0f s, unloading model ...", idle)
            try:
                EmbedHandler.service.unload()
            except Exception as e:
                LOG.warning("unload error: %s", e)


def main():
    ap = argparse.ArgumentParser(description="Memory OS Embedding MLX Service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--model", required=True, help="模型目录路径")
    ap.add_argument("--idle-timeout", type=int, default=15 * 60,
                    help="空闲多少秒后自动卸载模型（默认 900，即 15 分钟）")
    args = ap.parse_args()

    model_path = Path(args.model)
    if not model_path.exists():
        LOG.error("model not found: %s", model_path)
        sys.exit(1)

    # 启动时预加载模型
    EmbedHandler.model_path = str(model_path)
    EmbedHandler.service = MLXEmbeddingModel(str(model_path))
    _shared_last_active[0] = time.time()

    shutdown_event = threading.Event()
    threading.Thread(target=_idle_monitor, args=(args.idle_timeout, shutdown_event), daemon=True).start()

    server = HTTPServer((args.host, args.port), EmbedHandler)

    def _shutdown(signum, frame):
        LOG.info("signal %s received, shutting down", signum)
        shutdown_event.set()
        if EmbedHandler.service:
            try:
                EmbedHandler.service.unload()
            except Exception:
                pass
        try:
            server.shutdown()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    LOG.info("listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _shutdown(0, None)


if __name__ == "__main__":
    main()
