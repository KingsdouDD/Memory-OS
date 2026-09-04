#!/usr/bin/env python3
"""
Memory OS Qwen3-Reranker HTTP Service
监听 127.0.0.1:8877，暴露 /rerank 端点。
模型：Qwen3-Reranker-0.6B-4bit（mlx-community 版）

官方用法（来自 README）：
  - 用 Qwen3ForCausalLM 结构，yes/no token 打分
  - P(yes) 作为相关度分数
  - 格式：<Instruct> + <Query> + <Document> + <Response>
"""

import os
import sys
import json
import argparse
import signal
import logging
import threading
import time
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# 用 ctypes 改进程名（macOS Activity Monitor 显示用）
try:
    from ctypes import CDLL
    libc = CDLL(None)
    libc.setproctitle(b"reranker-daemon")
except Exception:
    pass

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

LOG = logging.getLogger("reranker-daemon")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

# ── 官方 prompt 模板 ────────────────────────────────────────────
INSTRUCT = "Given a web search query, retrieve relevant passages that answer the query."
PREFIX = (
    "[REMOVED_SPECIAL_TOKEN]system\n"
    "Judge whether the Document meets the requirements based on the Query and the Instruct provided. "
    'Note that the answer can only be "yes" or "no".'
    "[REMOVED_SPECIAL_TOKEN]\n[REMOVED_SPECIAL_TOKEN]user\n"
)
SUFFIX = "[REMOVED_SPECIAL_TOKEN]\n[REMOVED_SPECIAL_TOKEN]assistant\n</think>\n\n"


class RerankerModel:
    """Qwen3-Reranker-0.6B（mlx_lm 加载，官方 yes/no 打分法）"""

    def __init__(self, model_path: str):
        self.model_path = model_path
        self.model = None
        self.tokenizer = None
        self.true_id = None
        self.false_id = None
        self._load_model()

    def _load_model(self):
        LOG.info("loading model from %s ...", self.model_path)
        t0 = time.time()
        from mlx_lm import load as mlx_load
        self.model, _ = mlx_load(self.model_path)
        LOG.info("model loaded in %.1fs", time.time() - t0)

        from transformers import AutoTokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_path,
            trust_remote_code=True,
        )
        # yes/no token id（官方用 true/false 而非 yes/no）
        self.true_id = self.tokenizer.convert_tokens_to_ids("true")
        self.false_id = self.tokenizer.convert_tokens_to_ids("false")
        LOG.info("tokenizer ready: true_id=%d false_id=%d", self.true_id, self.false_id)

    def _rerank_one(self, query: str, candidate: str) -> float:
        """官方 yes/no 打分：P(true) 作为相关度分数。"""
        import mlx.core as mx

        content = f"<Instruct>: {INSTRUCT}\n<Query>: {query}\n<Document>: {candidate}"
        pre_ids = self.tokenizer.encode(PREFIX, add_special_tokens=False)
        content_ids = self.tokenizer.encode(content, add_special_tokens=False)
        suf_ids = self.tokenizer.encode(SUFFIX, add_special_tokens=False)
        ids = pre_ids + content_ids + suf_ids

        # forward，取最后一层 logits；cache=None 禁用 KV 缓存，防止每次调用后 KV 累积导致内存暴涨
        logits = self.model(mx.array([ids]), cache=None)[0, -1, :]  # (vocab,)
        # 官方用法：每次 forward 后清 Metal GPU 缓存，防止激活值累积
        mx.clear_cache()

        # P(yes) = softmax(logits[true]) over [no, yes]
        pair = mx.stack([logits[self.false_id], logits[self.true_id]])
        score = float(mx.exp(pair - mx.logsumexp(pair))[1])  # P(true)
        return score

    def rerank(self, query: str, candidates: list, top_k: int = 5) -> list:
        if not candidates:
            return []
        if self.model is None:
            raise RuntimeError("model not loaded")

        scores = []
        for c in candidates:
            try:
                score = self._rerank_one(query, str(c))
            except Exception as e:
                LOG.warning("_rerank_one failed: %s", e)
                score = -100.0
            scores.append(score)

        # 按分数降序
        indexed = list(enumerate(scores))
        indexed.sort(key=lambda x: -x[1])
        return indexed[:top_k]


class RerankerService:
    """HTTP 服务：模型按需加载 + idle 自动卸载，进程保持常驻"""

    def __init__(self, model_path: str, auto_unload_after: int = 7200):
        self.model_path = model_path
        self.auto_unload_after = auto_unload_after
        self.model = None
        self._lock = threading.Lock()
        self._idle_timer = None
        self._load_model_unlocked()

    def _load_model_unlocked(self):
        self.model = RerankerModel(self.model_path)

    def _reset_idle_timer(self):
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        self._idle_timer = threading.Timer(self.auto_unload_after, self._unload_if_idle)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _unload_if_idle(self):
        with self._lock:
            if self.model is not None:
                LOG.info("idle for %ds, unloading model ...", self.auto_unload_after)
                del self.model
                self.model = None
                import mlx.core as mx
                mx.clear_cache()

    def rerank(self, query: str, candidates: list, top_k: int = 5) -> dict:
        with self._lock:
            if self.model is None:
                LOG.info("model not loaded, loading now ...")
                self._load_model_unlocked()
            self._reset_idle_timer()
            ranked = self.model.rerank(query, candidates, top_k=top_k)
            return {
                "query": query,
                "results": [
                    {"index": int(idx), "score": float(score), "text": candidates[idx]}
                    for idx, score in ranked
                ],
            }

    def get_status(self) -> dict:
        return {
            "model_loaded": self.model is not None,
            "model_path": self.model_path,
            "auto_unload_after_seconds": self.auto_unload_after,
        }


class RerankHandler(BaseHTTPRequestHandler):
    service: RerankerService = None

    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "service": "qwen3-reranker"})
        elif self.path == "/status":
            try:
                self._json(200, self.service.get_status())
            except Exception as e:
                LOG.exception("status failed")
                self._json(500, {"error": str(e)})
        else:
            self._json(404, {"error": "not found", "endpoints": ["/rerank (POST)", "/health", "/status"]})

    def do_POST(self):
        if self.path == "/rerank":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                query = data.get("query", "")
                candidates = data.get("candidates", [])
                top_k = int(data.get("top_k", 5))

                if not query:
                    self._json(400, {"error": "missing 'query' field"})
                    return
                if not isinstance(candidates, list):
                    self._json(400, {"error": "'candidates' must be a list"})
                    return

                LOG.info("rerank query=%r candidates=%d top_k=%d", query, len(candidates), top_k)
                result = self.service.rerank(query, candidates, top_k=top_k)
                self._json(200, result)
            except Exception as e:
                LOG.exception("rerank failed")
                self._json(500, {"error": str(e)})
        else:
            self._json(404, {"error": "not found"})

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser(description="Memory OS Qwen3-Reranker Service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8877)
    ap.add_argument("--model", required=True, help="模型目录路径，launchd 必须通过 --model 传入")
    ap.add_argument("--unload-after", type=int, default=15 * 60, help="空闲多少秒后自动卸载模型（默认 900，即 15 分钟）")
    args = ap.parse_args()

    if not Path(args.model).exists():
        LOG.error("model not found: %s", args.model)
        sys.exit(1)

    LOG.info("starting Qwen3-Reranker service ...")
    service = RerankerService(args.model, auto_unload_after=args.unload_after)
    RerankHandler.service = service

    server = HTTPServer((args.host, args.port), RerankHandler)
    LOG.info("listening on http://%s:%d", args.host, args.port)

    def _shutdown(signum, frame):
        LOG.info("signal %s received, shutting down", signum)
        if service.model is not None:
            try:
                del service.model
                service.model = None
                import mlx.core as mx
                mx.clear_cache()
            except Exception:
                pass
        try:
            server.shutdown()
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _shutdown(0, None)


if __name__ == "__main__":
    main()
