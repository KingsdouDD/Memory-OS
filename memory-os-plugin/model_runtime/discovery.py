#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
model_runtime.discovery
=======================

获取"当前系统正在使用的模型"。

设计原则：
  - 默认从 OpenClaw session_status 读取（与运行环境保持一致）
  - 系统模型切换 → 这里返回值自动跟随
  - 不缓存"硬编码"模型名，每次调用实时探测
  - 不使用绝对路径，所有路径相对化

调用示例：
    from model_runtime.discovery import get_active_model
    info = get_active_model()
    print(info.model_id)   # e.g. "minimax/MiniMax-M3"
    print(info.backend)    # e.g. "api"
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional, List

# 把当前包所在目录加入 sys.path，便于直接 `python discovery.py` 测试
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)


# ============================================================
# 数据结构
# ============================================================

@dataclass
class ModelInfo:
    """一个可用模型的信息描述。"""
    model_id: str                                    # e.g. "minimax/MiniMax-M3"
    backend: str = "api"                             # api / ollama / mlx / openai / anthropic
    endpoint: Optional[str] = None                   # 实际调用 URL（自动推断）
    source: str = "unknown"                          # session / env / config / fallback
    raw: dict = field(default_factory=dict)           # 探测时的原始数据，便于排查

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# 后端识别
# ============================================================

# 支持的后端白名单（业务调用方只用这些 key）
KNOWN_BACKENDS = {"api", "ollama", "mlx", "openai", "anthropic", "minimax"}


def detect_backend(model_id: str) -> str:
    """
    根据 model_id 前缀推断后端类型。
    
    规则：
      - "ollama/xxx"    → ollama
      - "mlx/xxx"       → mlx
      - "openai/xxx"    → openai
      - "anthropic/xxx" → anthropic
      - "minimax/xxx"   → minimax
      - 无前缀或前缀未知 → api（通用 OpenAI 兼容）
    """
    if not model_id or "/" not in model_id:
        return "api"
    prefix = model_id.split("/", 1)[0].lower().strip()
    return prefix if prefix in KNOWN_BACKENDS else "api"


# ============================================================
# 探测路径（按优先级）
# ============================================================

def _probe_env() -> Optional[ModelInfo]:
    """优先级 1：环境变量（用户手动指定最高优先）。"""
    model_id = os.environ.get("MEMORY_OS_DECISION_MODEL") or os.environ.get("OPENCLAW_ACTIVE_MODEL")
    if not model_id:
        return None
    return ModelInfo(
        model_id=model_id,
        backend=detect_backend(model_id),
        source="env",
        raw={"env_key": "MEMORY_OS_DECISION_MODEL"},
    )


def _probe_session_status() -> Optional[ModelInfo]:
    """
    优先级 2：OpenClaw session_status。
    
    真实模型所在路径：
      openclaw status --json
      └─ sessions.recent[0].selectedModel     ← 当前会话实际在跑的模型
      └─ sessions.recent[0].configuredModel  ← 配置里的默认模型
      └─ sessions.defaults.model             ← 全局默认
    
    优先级：selectedModel > configuredModel > defaults.model
    """
    try:
        result = subprocess.run(
            ["openclaw", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "NO_PROXY": "127.0.0.1,localhost"},
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return None

    if result.returncode != 0 or not result.stdout:
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    sessions = data.get("sessions") or {}
    
    # 1. 当前活跃 session 的 selectedModel（最新一条）
    recent = sessions.get("recent") or []
    if recent:
        latest = recent[0]
        for field_name in ("selectedModel", "model", "configuredModel"):
            model_id = latest.get(field_name)
            if model_id:
                return ModelInfo(
                    model_id=str(model_id),
                    backend=detect_backend(str(model_id)),
                    source="session_selected" if field_name == "selectedModel" else "session",
                    raw={
                        "field": field_name,
                        "session_key": latest.get("key"),
                        "modelSelectionReason": latest.get("modelSelectionReason"),
                    },
                )

    # 2. 全局默认
    defaults = sessions.get("defaults") or {}
    model_id = defaults.get("model")
    if model_id:
        return ModelInfo(
            model_id=str(model_id),
            backend=detect_backend(str(model_id)),
            source="session_defaults",
            raw={"field": "sessions.defaults.model"},
        )

    return None


def _deep_get(obj, keys):
    """从嵌套 dict 里按路径取值。"""
    cur = obj
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return None
    return cur


def _probe_config_file() -> Optional[ModelInfo]:
    """优先级 3：读 OpenClaw 配置文件（相对路径，从 cwd 出发）。"""
    candidates = [
        ".openclaw/config.yaml",
        ".openclaw/config.json",
        "config/openclaw.yaml",
    ]
    for rel in candidates:
        if not os.path.exists(rel):
            continue
        try:
            with open(rel, "r", encoding="utf-8") as f:
                content = f.read()
            # 简化解析：找 `model:` / `active_model:` 这种行
            import re
            m = re.search(r'(?:^|\n)\s*(?:active_model|default_model|model)\s*[:=]\s*["\']?([\w\-./:]+)', content)
            if m:
                model_id = m.group(1).strip().strip("'\"")
                return ModelInfo(
                    model_id=model_id,
                    backend=detect_backend(model_id),
                    source="config",
                    raw={"path": rel},
                )
        except Exception:
            continue
    return None


# ============================================================
# 兜底
# ============================================================

DEFAULT_FALLBACK = "minimax/MiniMax-M3"


# ============================================================
# 主入口
# ============================================================

def get_active_model(force_refresh: bool = False) -> ModelInfo:
    """
    获取当前系统正在使用的主模型。
    
    优先级：
      1. 环境变量 MEMORY_OS_DECISION_MODEL / OPENCLAW_ACTIVE_MODEL
      2. OpenClaw session_status
      3. 配置文件
      4. 兜底：minimax/MiniMax-M3
    
    参数：
      force_refresh: 强制重新探测（每次调用本来就是实时探测，此参数仅为语义清晰）
    
    返回：
      ModelInfo 数据类
    """
    for probe in (_probe_env, _probe_session_status, _probe_config_file):
        try:
            info = probe()
            if info and info.model_id:
                info.endpoint = _infer_endpoint(info)
                return info
        except Exception:
            continue

    # 兜底
    fallback = ModelInfo(
        model_id=DEFAULT_FALLBACK,
        backend=detect_backend(DEFAULT_FALLBACK),
        source="fallback",
        raw={"reason": "all probes failed"},
    )
    fallback.endpoint = _infer_endpoint(fallback)
    return fallback


def _infer_endpoint(info: ModelInfo) -> Optional[str]:
    """根据 backend 推断默认 endpoint。"""
    if info.backend == "ollama":
        host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
        return f"http://{host}/api/generate"
    if info.backend == "mlx":
        host = os.environ.get("MLX_HOST", "127.0.0.1:8080")
        return f"http://{host}/v1/chat/completions"
    if info.backend in ("openai", "anthropic", "minimax", "api"):
        # 这些走各家云端 API，endpoint 由业务调用方注入
        return None
    return None


# ============================================================
# 列出所有可用模型（简化版：从环境变量 / 常见端点扫一遍）
# ============================================================

def list_available_models() -> List[ModelInfo]:
    """列出当前环境所有可探测到的模型后端。"""
    found = []

    # 1. Ollama
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as r:
            data = json.load(r)
            for m in data.get("models", []):
                found.append(ModelInfo(
                    model_id=f"ollama/{m['name']}",
                    backend="ollama",
                    endpoint="http://127.0.0.1:11434/api/generate",
                    source="ollama_local",
                ))
    except Exception:
        pass

    # 2. MLX（约定端口 8080）
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8080/v1/models", method="GET")
        with urllib.request.urlopen(req, timeout=2) as r:
            data = json.load(r)
            for m in data.get("data", []):
                found.append(ModelInfo(
                    model_id=f"mlx/{m['id']}",
                    backend="mlx",
                    endpoint="http://127.0.0.1:8080/v1/chat/completions",
                    source="mlx_local",
                ))
    except Exception:
        pass

    # 3. 当前活跃模型（如果不在列表里）
    active = get_active_model()
    if not any(f.model_id == active.model_id for f in found):
        found.insert(0, active)

    return found


# ============================================================
# CLI 测试入口
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Probe active model from system")
    parser.add_argument("--list", action="store_true", help="List all available models")
    args = parser.parse_args()

    if args.list:
        print(json.dumps([m.to_dict() for m in list_available_models()], ensure_ascii=False, indent=2))
    else:
        info = get_active_model()
        print(json.dumps(info.to_dict(), ensure_ascii=False, indent=2))
