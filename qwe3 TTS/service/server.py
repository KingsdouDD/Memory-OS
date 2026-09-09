"""
HTTP API server for qwe3 TTS.
Runs as a long-lived Python process, separate from OpenClaw.
Idle timeout → model unloaded → service stops after further idle.
"""

import json
import logging
import os
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from service.model_manager import ModelManager, SERVICE_STOP_FILE
from service.voice_manager import VoiceManager
from service.inference import InferenceEngine
from service.lifecycle import LifecycleManager

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "logs" / "qwe3-tts.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("qwe3-tts")

# Load config
CONFIG_PATH = ROOT / "config" / "config.json"
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

# Global engine
mm = ModelManager(CONFIG_PATH)
vm = VoiceManager()
engine = InferenceEngine(mm, vm, CONFIG)
lifecycle = LifecycleManager(CONFIG)

# Stop marker check interval (seconds)
STOP_CHECK_INTERVAL = 10


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.info(f"{self.address_string()} {fmt % args}")

    def send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self.send_json({
                "status": "ok",
                "model": mm.model_id,
                "model_loaded": mm.is_loaded,
                "pid": os.getpid(),
                "last_used_at": mm._last_used_at,
                "idle_seconds": int(time.time() - mm._last_used_at) if mm._last_used_at else 0,
            })

        elif path == "/voices":
            self.send_json({"voices": vm.list_voices()})

        elif path == "/model/status":
            self.send_json(mm.status())

        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        try:
            data = json.loads(body.decode()) if body else {}
        except Exception:
            self.send_json({"error": "Invalid JSON"}, 400)
            return

        # Check idle before each request
        mm.check_idle_unload()

        if path == "/load":
            mm.ensure_loaded()
            lifecycle.update_last_used()
            self.send_json({"status": "ok", "model_loaded": mm.is_loaded})

        elif path == "/unload":
            mm.check_idle_unload()
            self.send_json({"status": "ok", "model_loaded": mm.is_loaded})

        elif path == "/tts":
            text = data.get("text", "")
            voice = data.get("voice", CONFIG["voices"]["default"])
            language = data.get("language", "Chinese")

            if not text:
                self.send_json({"error": "text is required"}, 400)
                return

            lifecycle.update_last_used()
            result = engine.synthesize(text, voice, language)
            self.send_json(result, 200 if result["success"] else 500)

        elif path == "/voices/register":
            voice_id = data.get("voice_id")
            ref_audio = data.get("reference_audio")
            if not voice_id or not ref_audio:
                self.send_json({"error": "voice_id and reference_audio required"}, 400)
                return
            voice = vm.add_voice(voice_id, ref_audio,
                                 data.get("reference_text", ""),
                                 data.get("language", "Chinese"),
                                 data.get("description", ""))
            self.send_json({"success": True, "voice": voice})

        elif path == "/voices/remove":
            voice_id = data.get("voice_id")
            if not voice_id:
                self.send_json({"error": "voice_id required"}, 400)
                return
            ok = vm.remove_voice(voice_id)
            self.send_json({"success": ok})

        else:
            self.send_json({"error": "Not found"}, 404)


def _stop_check_loop(server: HTTPServer):
    """后台线程：定期检查 stop marker，符合条件则关闭服务。"""
    while True:
        time.sleep(STOP_CHECK_INTERVAL)
        if not SERVICE_STOP_FILE.exists():
            continue
        # marker 存在且是当前进程写的
        try:
            pid = int(SERVICE_STOP_FILE.read_text().strip())
        except (ValueError, OSError):
            SERVICE_STOP_FILE.unlink(missing_ok=True)
            continue
        if pid != os.getpid():
            continue
        # 确认模型已卸
        if mm.is_loaded:
            logger.info("Stop marker present but model still loaded, skipping")
            SERVICE_STOP_FILE.unlink(missing_ok=True)
            continue
        logger.info("Stop marker confirmed, shutting down service")
        SERVICE_STOP_FILE.unlink(missing_ok=True)
        server.shutdown()
        break


def run_server():
    host = CONFIG["server"]["host"]
    port = int(os.environ.get("QWE3_TTS_PORT", CONFIG["server"]["port"]))

    # Update service.json with current pid
    lifecycle._write_service_json(os.getpid())

    server = HTTPServer((host, port), Handler)
    logger.info(f"qwe3 TTS service listening on {host}:{port}")
    sys.stdout.flush()

    # 启动 stop marker 检查线程
    checker = threading.Thread(target=_stop_check_loop, args=(server,), daemon=True)
    checker.start()

    # Graceful shutdown on SIGTERM
    import signal as _signal
    def _sigterm_handler(signum, frame):
        logger.info("Received SIGTERM, shutting down gracefully")
        server.shutdown()
    _signal.signal(_signal.SIGTERM, _sigterm_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server shutting down")
        server.shutdown()


if __name__ == "__main__":
    run_server()
