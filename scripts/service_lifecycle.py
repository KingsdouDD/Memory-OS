#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Memory OS 服务生命周期管理

所有调用 embed/reranker 服务的地方都走这里：
- 端口没人监听 → 自动拉起 launchd 服务
- 拉起后等待端口就绪再返回
- 给 hook / dream / recall / ingest 等所有路径统一用

需要满足：
1. 进程 dead 时被使用 → 自动拉起
2. idle 超时后进程自己退出 → 保持 dead 状态
3. launchd 不会自动拉（避免变成常驻）
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

PLIST_DIR = Path.home() / "Library" / "LaunchAgents"

# 端口 → launchd label 映射
SERVICE_MAP = {
    8765: "com.memoryos.embed-daemon",
    8877: "com.memoryos.reranker",
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


def ensure_service_up(port: int, host: str = "127.0.0.1", max_wait: float = 90.0) -> bool:
    """确保指定端口的服务在运行。

    逻辑：
    1. 端口有人监听 → 直接返回
    2. 端口没人 → launchctl load 拉起（不杀进程，避免 TIME_WAIT 端口冲突）
    3. 如果端口还被占着（TIME_WAIT），等最多 30 秒再试
    4. 等待端口就绪（最长 max_wait 秒）
    5. 返回 True 表示拉起成功，False 表示超时
    """
    if _port_listening(host, port):
        return True

    label = SERVICE_MAP.get(port)
    if not label:
        print(f"[service_lifecycle] no plist mapping for port {port}", file=sys.stderr)
        return False

    plist_path = PLIST_DIR / f"{label}.plist"
    uid = os.getuid()

    for attempt in range(3):
        # 先等端口释放（处理 TIME_WAIT 残留）
        if not _port_listening(host, port):
            print(f"[service_lifecycle] port {port} free, loading {label} ...", file=sys.stderr)
            try:
                subprocess.run(
                    ["launchctl", "kickstart", f"gui/{uid}/{label}"],
                    check=True, timeout=10,
                )
            except Exception as e:
                print(f"[service_lifecycle] load failed (attempt {attempt+1}): {e}", file=sys.stderr)
        else:
            print(f"[service_lifecycle] port {port} still in use, waiting ...", file=sys.stderr)

        # 等待端口就绪
        if _wait_port_ready(host, port, max_wait=max_wait):
            print(f"[service_lifecycle] port {port} ready", file=sys.stderr)
            return True

        # 端口还没好，等一下再试（给系统时间彻底释放端口）
        if attempt < 2:
            print(f"[service_lifecycle] port {port} not ready, retrying ...", file=sys.stderr)
            time.sleep(2.0)

    print(f"[service_lifecycle] port {port} failed to start within {max_wait}s", file=sys.stderr)
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
