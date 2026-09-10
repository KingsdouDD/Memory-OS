#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
model_runtime.dedup_bridge
===========================

作为 write_4layer.py 与 model_runtime.decide 之间的桥。

设计原则（老豆拍板 2026-09-10）：
  - 写入工具不持有任何更新/合并机制
  - 不靠向量阈值做去重（向量模型蠢，阈值误判）
  - 去重由 LLM 语义判断决定 CREATE / SKIP / INVALIDATE / DISCARD
"""

from __future__ import annotations

import os
import sys
from typing import List, Dict, Any, Tuple

# 让 model_runtime 包可导入（不依赖绝对路径）
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from model_runtime.decide import (
    llm_decide_action,
    ACTION_CREATE,
    ACTION_SKIP,
    ACTION_INVALIDATE,
)


# ============================================================
# 开关
# ============================================================

# True: 用 LLM 决策（推荐）
# False: 走极简 fallback（无阈值，仅根据 state 和是否有候选决定）
USE_LLM_DEDUP = os.environ.get("MEMORY_OS_USE_LLM_DEDUP", "1") == "1"


# ============================================================
# 候选格式转换
# ============================================================

def _normalize_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 write_4layer.py 召回的 [{pid, score, payload}] 标准化给 LLM 看。"""
    out = []
    for c in candidates or []:
        payload = c.get("payload") or {}
        out.append({
            "pid": c.get("pid"),
            "score": float(c.get("score", 0)),
            "summary": (
                payload.get("summary")
                or payload.get("title")
                or payload.get("text")
                or ""
            ).strip(),
            "state": payload.get("state", "active"),
            "ts": payload.get("ts") or payload.get("recorded_at") or "",
            "event_time": payload.get("event_time") or "",
            "importance": payload.get("importance", 0.5),
        })
    return out


# ============================================================
# 写入工具期望的 4 种 action
# ============================================================

_VALID_OUTPUT_ACTIONS = {"CREATE", "SKIP", "INVALIDATE", "DISCARD"}


def _map_action(llm_action: str, state: str, candidates: List[Dict]) -> str:
    """
    把 LLM 决策映射回写入工具的合法值。

    写入工具只接受 4 种 action：
      - CREATE       新建
      - SKIP         跳过
      - INVALIDATE   标记旧记录作废
      - DISCARD      state=uncertain 且无候选时丢弃

    LLM 可能返回的 action 范围：
      - CREATE / SKIP / INVALIDATE / DISCARD → 直接透传
      - 其它 → 不可能出现（decide.py 已收敛）
    """
    if state == "uncertain" and not candidates:
        return "DISCARD"
    if llm_action in _VALID_OUTPUT_ACTIONS:
        return llm_action
    # 未知 action 兜底 CREATE（不阻塞写入）
    return "CREATE"


# ============================================================
# 桥接主函数（替换 _rule_decide_layer_action）
# ============================================================

def dedup_decide_layer_action(
    state: str,
    candidates: List[Dict[str, Any]],
    *,
    layer: str = "L1",
    new_text: str = "",
    use_llm: bool = USE_LLM_DEDUP,
) -> Tuple[str, str]:
    """
    替代原 write_4layer._rule_decide_layer_action 的入口。

    参数完全兼容：
      state:       "active"/"historical"/"ongoing"/"uncertain"
      candidates:  [{pid, score, payload}]（write_4layer 现有格式）

    额外参数（LLM 决策需要）：
      layer:       "L0"/"L1"/"L2"/"L3"
      new_text:    新记忆的文本

    返回：
      (action, reason)，action ∈ {"CREATE","SKIP","INVALIDATE","DISCARD"}
    """
    if not use_llm or not new_text:
        return _trivial_fallback(state, candidates)

    norm_cands = _normalize_candidates(candidates)

    try:
        llm_action, reason = llm_decide_action(
            new_text=new_text,
            candidates=norm_cands,
            new_state=state,
            layer=layer,
        )
    except Exception as e:
        return _trivial_fallback(
            state, candidates,
            prefix=f"bridge-llm-failed:{type(e).__name__}:{str(e)[:60]}",
        )

    mapped = _map_action(llm_action, state, candidates)
    return mapped, reason


# ============================================================
# 极简 fallback（无任何阈值）
# ============================================================

def _trivial_fallback(
    state: str,
    candidates: List[Dict[str, Any]],
    prefix: str = "trivial-fallback",
) -> Tuple[str, str]:
    """
    LLM 不可用时的兜底逻辑：
      - 不靠任何相似度阈值（向量蠢）
      - 只判 CREATE / SKIP / INVALIDATE
      - 只处理最极端的两种情况：
          ① state=uncertain 且无候选 → DISCARD
          ② state=uncertain 且有候选 → INVALIDATE
          ③ 其它情况 → 一律 CREATE（让 LLM 失败也不阻塞写入）

    这套 fallback 的逻辑是保守的"宁可重复也不丢失"：
    重复的记录可以在事后人工 review / 清理，但丢失了就找不回来。
    """
    state = state or "active"

    if state == "uncertain":
        if not candidates:
            return "DISCARD", f"{prefix}: state=uncertain, no candidates"
        return "INVALIDATE", f"{prefix}: state=uncertain, has candidates"

    # 任何 active / historical / ongoing 一律 CREATE
    return "CREATE", f"{prefix}: state={state}, no threshold check"


# ============================================================
# 调试入口
# ============================================================

if __name__ == "__main__":
    print("=== dedup_bridge 自检 ===\n")

    # 1. 复现之前被错误 SKIP 的场景
    new_text = "外婆半夜帮老豆盘猪老二，老豆也帮外婆按摩"
    fake_cands = [{
        "pid": "15295351293916629378",
        "score": 0.891,
        "payload": {
            "summary": "外婆与老豆关系亲密，工作相识后一直保持联系",
            "state": "active",
            "ts": "2026-09-10T00:25:24+08:00",
        },
    }]

    # LLM 路径
    action, reason = dedup_decide_layer_action(
        state="historical", candidates=fake_cands,
        layer="L3", new_text=new_text,
    )
    print(f"[LLM] action={action} | reason={reason}")

    # 极简 fallback
    action, reason = dedup_decide_layer_action(
        state="historical", candidates=fake_cands,
        layer="L3", new_text=new_text, use_llm=False,
    )
    print(f"[TRIVIAL FALLBACK] action={action} | reason={reason}")

    # 边界情况
    print()
    print("=== 边界情况 ===")
    cases = [
        ("active", []),
        ("active", fake_cands),
        ("historical", fake_cands),
        ("uncertain", []),
        ("uncertain", fake_cands),
        ("ongoing", fake_cands),
    ]
    for st, cs in cases:
        action, reason = _trivial_fallback(st, cs, prefix="test")
        assert action in _VALID_OUTPUT_ACTIONS, f"非法 action: {action}"
        assert action in {"CREATE","SKIP","INVALIDATE","DISCARD"}, f"非法 action: {action}"
        print(f"  state={st:10s} has_cands={bool(cs)} → {action}: {reason}")
