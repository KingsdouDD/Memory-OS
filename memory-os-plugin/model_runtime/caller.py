#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
model_runtime.caller
====================

通过 OpenClaw 的 `openclaw infer model run` 调用模型。

设计原则：
  - 工具不持有任何 API key / endpoint / auth 配置
  - OpenClaw 自己管 auth (models.json / OAuth / 环境变量)
  - 工具只问 OpenClaw："用这个模型跑这段 prompt"
  - 模型来源由 get_active_model() 提供（来自 OpenClaw session_status）

调用示例：
    from model_runtime.caller import call_with_active_model, call_with_model
    text = call_with_active_model("你好")
    text = call_with_model("minimax/MiniMax-M3", "你好")
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Optional, Dict, Any

from .discovery import get_active_model, ModelInfo


# ============================================================
# 核心：通过 openclaw infer model run 调模型
# ============================================================

def _build_openclaw_infer_cmd(
    model_id: str,
    prompt: str,
    *,
    temperature: float = 0.1,
    max_tokens: int = 500,
    timeout: int = 60,
) -> list:
    """构造 `openclaw infer model run` 命令。"""
    bare_model = model_id.split("/", 1)[-1] if "/" in model_id else model_id
    return [
        "openclaw", "infer", "model", "run",
        "--model", bare_model,
        "--prompt", prompt,
        "--json",  # 输出 JSON，方便解析
    ]


def _call_via_openclaw(model_id: str, prompt: str, **opts) -> str:
    """通过 OpenClaw CLI 调用模型，返回纯文本输出。"""
    cmd = _build_openclaw_infer_cmd(model_id, prompt, **opts)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=opts.get("timeout", 60) + 30,
            env={**os.environ, "NO_PROXY": "127.0.0.1,localhost"},
        )
    except FileNotFoundError:
        raise RuntimeError("openclaw CLI 未安装或不在 PATH 中")
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"openclaw infer 超时（>{opts.get('timeout', 60)}s）") from e

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"openclaw infer 失败 (rc={result.returncode}): {err}")

    raw = (result.stdout or "").strip()
    
    # 解析 JSON 输出（openclaw infer model run --json 的格式）
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            # OpenClaw 自己的格式: {"outputs": [{"text": "..."}]}
            outputs = data.get("outputs") or []
            if outputs and isinstance(outputs, list):
                first = outputs[0]
                if isinstance(first, dict):
                    text = first.get("text")
                    if isinstance(text, str):
                        return text
            # OpenAI 兼容: {"choices": [{"message": {"content": "..."}}]}
            choices = data.get("choices") or []
            if choices and isinstance(choices, list):
                msg = choices[0].get("message") or {}
                if isinstance(msg, dict) and msg.get("content"):
                    return msg["content"]
            # 平铺字段
            for key in ("content", "text", "output", "message", "completion"):
                v = data.get(key)
                if isinstance(v, str):
                    return v
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    
    return raw


# ============================================================
# 统一调用入口（保持原有 API 签名）
# ============================================================

def call_with_model(model_id: str, prompt: str, **opts) -> str:
    """
    用指定模型调用一次，返回纯文本。

    注意：本工具不直接调任何 LLM API。
    所有调用都委托给 OpenClaw 的 `openclaw infer model run`，
    OpenClaw 自己处理 auth / endpoint / 模型路由。

    参数：
      model_id: 完整模型 ID（带前缀），如 "minimax/MiniMax-M3"
      prompt:   用户 prompt
      **opts:   temperature / max_tokens / timeout
    """
    return _call_via_openclaw(model_id, prompt, **opts)


def call_with_active_model(prompt: str, **opts) -> str:
    """
    用当前系统主模型调用（推荐入口）。

    模型来源：get_active_model() → openclaw status --json 探测。
    调用方式：openclaw infer model run（OpenClaw 内部处理 auth）。
    """
    info = get_active_model()
    return call_with_model(info.model_id, prompt, **opts)


# ============================================================
# CLI 入口
# ============================================================

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Call model via OpenClaw")
    parser.add_argument("prompt", help="Prompt text")
    parser.add_argument("--model", default=None, help="Override model (default: current active)")
    args = parser.parse_args()

    if args.model:
        model_id = args.model
    else:
        info = get_active_model()
        model_id = info.model_id

    print(f"[via openclaw infer] model={model_id}", file=sys.stderr)
    result = call_with_model(model_id, args.prompt)
    print(result)
