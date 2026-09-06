#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Memory OS 服务生命周期管理

所有调用 embed/reranker 服务的地方都走这里：
- 端口没人监听 → 直接 subprocess.Popen 前台拉起 daemon
- 拉起后等待端口就绪再返回
- 给 hook / dream / recall / ingest 等所有路径统一用

需要满足：
1. 进程 dead 时被使用 → 自动拉起
2. idle 超时后进程自己退出 → 保持 dead 状态
3. 父进程退出时，spawn 出来的 daemon 一起退出（start_new_session=False）
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = PLUGIN_DIR.parent  # memory-os-plugin/
MEMORY_OS_ROOT = PLUGIN_ROOT.parent  # memory-os/
VENV_PYTHON = MEMORY_OS_ROOT / "venv" / "bin" / "python"

# 端口 → daemon 启动配置
SERVICE_MAP = {
    8765: {
        "script": PLUGIN_DIR / "embed_daemon.py",
        "model": Path.home() / ".openclaw/workspace/memory-os/models/bge-m3-mlx-8bit",
        "args": ["--host", "127.0.0.1", "--port", "8765"],
        "log": "/tmp/memory-os-embed.log",
    },
    8877: {
        "script": PLUGIN_DIR / "reranker_daemon.py",
        "model": Path.home() / ".openclaw/workspace/memory-os/models/Qwen3-Reranker-0.6B-4bit",
        "args": ["--host", "127.0.0.1", "--port", "8877"],
        "log": "/tmp/memory-os-reranker.log",
    },
}


def _port_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    """检查端口是否有人监听。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def _wait_port_ready(host: str, port: int, max_wait: float = 60.0) -> bool:
    """等待端口就绪（健康检查 OK）。"""
    import urllib.request
    health_paths = {8765: "/health", 8877: "/health"}
    path = health_paths.get(port, "/")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if _port_listening(host, port):
            try:
                with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=2) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
        time.sleep(0.5)
    return False


def _wait_port_free(host: str, port: int, max_wait: float = 30.0) -> bool:
    """等待端口从 TIME_WAIT 状态释放（真正可绑定）。"""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if not _port_listening(host, port):
            return True
        time.sleep(1.0)
    return False


def _spawn_daemon(port: int) -> bool:
    """直接 Popen 启动 daemon 进程，stdout/stderr 重定向到日志文件。"""
    cfg = SERVICE_MAP.get(port)
    if not cfg:
        print(f"[service_lifecycle] no mapping for port {port}", file=sys.stderr)
        return False

    script = cfg["script"]
    model_path = cfg["model"]
    if not script.exists():
        print(f"[service_lifecycle] script not found: {script}", file=sys.stderr)
        return False
    if not Path(model_path).exists():
        print(f"[service_lifecycle] model not found: {model_path}", file=sys.stderr)
        return False

    python_bin = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    cmd = [python_bin, str(script), "--model", str(model_path)] + cfg["args"]

    log_fp = open(cfg["log"], "a", buffering=1)
    log_fp.write(f"\n--- spawn {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)} ---\n")
    log_fp.flush()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=False,  # 父进程退出时一起退出
        )
        print(f"[service_lifecycle] spawned pid={proc.pid} port={port} log={cfg['log']}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[service_lifecycle] spawn failed: {e}", file=sys.stderr)
        return False


def ensure_service_up(port: int, host: str = "127.0.0.1", max_wait: float = 90.0) -> bool:
    """确保指定端口的服务在运行。

    逻辑：
    1. 端口有人监听 → 直接返回
    2. 端口没人 → 直接 spawn 守护进程（不走 launchctl）
    3. 等待端口就绪（最长 max_wait 秒）
    """
    if _port_listening(host, port):
        return True

    cfg = SERVICE_MAP.get(port)
    if not cfg:
        print(f"[service_lifecycle] no mapping for port {port}", file=sys.stderr)
        return False

    if not _spawn_daemon(port):
        return False

    if _wait_port_ready(host, port, max_wait=max_wait):
        print(f"[service_lifecycle] port {port} ready", file=sys.stderr)
        return True

    print(f"[service_lifecycle] port {port} failed to start within {max_wait}s (see {cfg['log']})", file=sys.stderr)
    return False


def http_post(url: str, payload: dict, timeout: float = 30.0, max_retries: int = 1):
    """带自动拉起服务的 HTTP POST。

    1. 解析端口
    2. 确保服务在运行
    3. 发请求
    4. 失败一次后重试（可能是服务刚好被 unload）
    """
    import urllib.request
    import urllib.error
    import json as _json

    # 提取 host:port
    try:
        host_port = url.split("//", 1)[1].split("/", 1)[0]
        host, port = host_port.split(":")
        port = int(port)
    except Exception:
        host, port = "127.0.0.1", 8765

    data = _json.dumps(payload).encode("utf-8")

    for attempt in range(max_retries + 1):
        if attempt > 0 or not _port_listening(host, port):
            if not ensure_service_up(port, host=host):
                if attempt >= max_retries:
                    raise ConnectionError(f"service on port {port} failed to start")
                continue

        try:
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            print(f"[service_lifecycle] POST {url} failed (attempt {attempt+1}): {e}", file=sys.stderr)
            if attempt >= max_retries:
                raise
            time.sleep(0.5)
