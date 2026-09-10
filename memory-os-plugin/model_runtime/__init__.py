#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
model_runtime
=============

统一的模型运行时（基于 OpenClaw infer CLI）

  - discovery: 探测当前活跃模型（从 OpenClaw session_status）
  - caller:    调模型（通过 `openclaw infer model run`）
  - decide:    LLM 驱动的记忆去重决策
  - dedup_bridge: 与 write_4layer.py 集成的桥

设计原则：
  - 工具不持有任何 API key / endpoint
  - 所有 auth / 路由都委托给 OpenClaw
  - 工具只负责 "用模型 A 跑 prompt X"
"""

from .discovery import (
    ModelInfo,
    get_active_model,
    list_available_models,
    detect_backend,
    KNOWN_BACKENDS,
)

from .caller import (
    call_with_model,
    call_with_active_model,
)

from .decide import (
    llm_decide_action,
    build_decide_prompt,
    DECIDE_SYSTEM_PROMPT,
    ACTION_CREATE,
    ACTION_SKIP,
    ACTION_INVALIDATE,
)

__all__ = [
    # discovery
    "ModelInfo",
    "get_active_model",
    "list_available_models",
    "detect_backend",
    "KNOWN_BACKENDS",
    # caller
    "call_with_model",
    "call_with_active_model",
    # decide
    "llm_decide_action",
    "build_decide_prompt",
    "DECIDE_SYSTEM_PROMPT",
    "ACTION_CREATE",
    "ACTION_SKIP",
    "ACTION_INVALIDATE",
]
