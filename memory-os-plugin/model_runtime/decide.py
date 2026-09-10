#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
model_runtime.decide
====================

LLM 驱动的记忆写入决策。

解决的问题：
  原 write_4layer._rule_decide_layer_action 只看 cosine 分数（0.82 阈值），
  导致语义不重复但向量接近的记录被错误 SKIP。老豆拍板 2026-09-10：
  去重靠 LLM 语义判断，action 只剩 CREATE / SKIP / INVALIDATE 三个。

  本模块：
    1. ANN 召回候选（向量 + cosine 分数）
    2. 拼 prompt，把候选和元数据一起给 LLM
    3. 让 LLM 决定：CREATE / SKIP / INVALIDATE
    4. 解析 LLM 输出，按 action 执行
    5. 调用失败时降级回纯规则（不阻塞写入）

调用示例：
    from model_runtime.decide import llm_decide_action
    action, reason = llm_decide_action(
        new_text="外婆和姨婆去香港玩了",
        candidates=[{"score": 0.85, "summary": "...", "state": "historical", ...}, ...],
        new_state="historical",
        layer="L1",
    )
"""

from __future__ import annotations

import json
import re
from typing import List, Dict, Any, Optional, Tuple

from .caller import call_with_active_model, call_with_model


# ============================================================
# 决策常量
# ============================================================

ACTION_CREATE = "CREATE"        # 新建
ACTION_SKIP = "SKIP"            # 完全重复，跳过
ACTION_INVALIDATE = "INVALIDATE" # 旧记录作废


VALID_ACTIONS = {ACTION_CREATE, ACTION_SKIP, ACTION_INVALIDATE}


# ============================================================
# Prompt 模板
# ============================================================

DECIDE_SYSTEM_PROMPT = """你是一名严格的记忆去重决策器。

任务：判断"新记忆"是否与候选列表中的某条"旧记忆"重复、冲突或使旧记录过时。

设计原则：
  - 写入工具只创建新记录，不做任何 UPDATE / MERGE
  - 你的职责只有：决定新记忆 CREATE 还是 SKIP，旧记录要不要 INVALIDATE
  - 不要建议 UPDATE 或 MERGE，那些由独立工具负责

决策选项（只这三个）：
- CREATE        新建：新记忆与所有候选都不同主题，无冲突，直接插入
- SKIP          跳过：新记忆与某候选是同一事实，完全重复，不存
- INVALIDATE    作废：新记忆使某候选过时（旧的不再成立），标记旧记录

判断要点：
1. 时间、地点、人物、事件是否真正相同
2. 是否只是"提到同一类对象"但事件不同（两次都去香港但日期同行人都不同 → CREATE）
3. state 字段说明历史/当前；uncertain 表示存疑
4. 只看语义和事实，不要被向量相似度干扰

严格按 JSON 输出：
{"action": "<ACTION>", "reason": "<一句话理由>", "match_pid": "<候选中匹配的 pid，没有则空>"}

不要解释，不要 markdown，只输出一行 JSON。"""


def build_decide_prompt(new_text: str, candidates: List[Dict[str, Any]], new_state: str = "active", layer: str = "L1") -> str:
    """构造用户 prompt。"""
    if not candidates:
        return (
            f"新记忆（layer={layer}, state={new_state}）：\n"
            f"\"\"\"{new_text}\"\"\"\n\n"
            f"候选列表：空\n\n"
            f"决策："
        )
    
    cand_lines = []
    for i, c in enumerate(candidates, 1):
        score = c.get("score", 0.0)
        pid = c.get("pid", "?")
        text = (c.get("summary") or c.get("text") or "").strip()
        cstate = c.get("state", "active")
        ts = c.get("ts") or c.get("event_time") or ""
        cand_lines.append(
            f"  [{i}] pid={pid} | cosine={score:.3f} | state={cstate} | ts={ts}\n"
            f"      text: \"{text}\""
        )
    cand_block = "\n".join(cand_lines)
    
    return (
        f"新记忆（layer={layer}, state={new_state}）：\n"
        f"\"\"\"{new_text}\"\"\"\n\n"
        f"候选列表（按 cosine 相似度排序）：\n{cand_block}\n\n"
        f"请判断新记忆与哪条候选的关系最匹配（如果有），并输出 JSON 决策："
    )


# ============================================================
# LLM 解析
# ============================================================

def _parse_llm_decision(raw: str) -> Optional[Dict[str, str]]:
    """从 LLM 输出抠出 JSON。容错：剥 markdown 围栏、抓首个 JSON 对象。"""
    if not raw:
        return None
    
    raw = raw.strip()
    
    # 1. 直接尝试 parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    
    # 2. 剥 markdown 围栏
    if "```" in raw:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    
    # 3. 抓第一个 {...}
    m = re.search(r"\{[^{}]*\"action\"[^{}]*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    
    return None


# ============================================================
# Fallback（LLM 失败时降级 —— 极简，不靠阈值）
# ============================================================

def _fallback_rule_decide(new_text: str, candidates: List[Dict[str, Any]], new_state: str) -> Tuple[str, str]:
    """
    LLM 不可用时的极简 fallback：
      - state=uncertain 无候选 → SKIP
      - state=uncertain 有候选 → INVALIDATE
      - 其他情况 → 一律 CREATE

    设计原则：向量模型蠢，不靠相似度阈值做判断。
    宁可重复也不丢失：重复记录可以事后 review / 清理，丢失了找不回来。
    """
    state = new_state or "active"
    
    if not candidates:
        if state == "uncertain":
            return ACTION_SKIP, "fallback: state=uncertain, no candidates"
        return ACTION_CREATE, "fallback: no candidates"
    
    if state == "uncertain":
        return ACTION_INVALIDATE, "fallback: state=uncertain, has candidates"
    
    # 一律 CREATE
    return ACTION_CREATE, "fallback: default CREATE"


# ============================================================
# 主入口
# ============================================================

def llm_decide_action(
    new_text: str,
    candidates: List[Dict[str, Any]],
    new_state: str = "active",
    layer: str = "L1",
    *,
    use_llm: bool = True,
    temperature: float = 0.1,
    max_tokens: int = 200,
    model_id: Optional[str] = None,
) -> Tuple[str, str]:
    """
    LLM 驱动的记忆去重决策。
    
    参数：
      new_text:     新记忆文本
      candidates:   ANN 召回的候选列表 [{pid, score, summary/state/...}]
      new_state:    新记忆的状态 (active/historical/ongoing/uncertain)
      layer:        层名 (L0/L1/L2/L3)
      use_llm:      是否使用 LLM（False 时走极简 fallback）
      model_id:     指定模型（默认用当前系统主模型）
      temperature:  LLM 采样温度
      max_tokens:   LLM 输出上限
    
    返回：
      (action, reason)
        action ∈ {"CREATE", "SKIP", "INVALIDATE"}
    """
    if not use_llm:
        return _fallback_rule_decide(new_text, candidates, new_state)
    
    # 1. 拼 prompt
    user_prompt = build_decide_prompt(new_text, candidates, new_state, layer)
    full_prompt = f"{DECIDE_SYSTEM_PROMPT}\n\n{user_prompt}"
    
    # 2. 调 LLM
    try:
        if model_id:
            raw = call_with_model(model_id, full_prompt, temperature=temperature, max_tokens=max_tokens)
        else:
            raw = call_with_active_model(full_prompt, temperature=temperature, max_tokens=max_tokens)
    except Exception as e:
        # LLM 调用失败，降级到极简 fallback
        action, reason = _fallback_rule_decide(new_text, candidates, new_state)
        return action, f"llm failed ({type(e).__name__}: {str(e)[:80]}), {reason}"
    
    # 3. 解析
    parsed = _parse_llm_decision(raw)
    if not parsed:
        action, reason = _fallback_rule_decide(new_text, candidates, new_state)
        return action, f"llm parse failed (raw={raw[:60]}), {reason}"
    
    action = str(parsed.get("action", "")).upper().strip()
    if action not in VALID_ACTIONS:
        action = ACTION_CREATE  # 默认保底
    reason = str(parsed.get("reason", ""))[:200]
    
    # 附加最高分信息，便于排错
    best_score = max((c.get("score", 0) for c in candidates), default=0)
    full_reason = f"{reason} | best_cosine={best_score:.3f}"
    
    return action, full_reason


# ============================================================
# CLI / 调试入口
# ============================================================

if __name__ == "__main__":
    # 测试用例：和当前库里那条"老豆和外婆深夜按摩"做对比
    new = "外婆半夜帮我盘猪老二，还帮外婆按摩身体"
    cands = [
        {
            "pid": "15295351293916629378",
            "score": 0.891,
            "summary": "外婆与老豆关系亲密，工作相识后一直保持联系，即使后来异地也保持每周见面，平日一起进行看电视、骑马射箭、爬山等活动。",
            "state": "active",
        },
        {
            "pid": "9999999999",
            "score": 0.78,
            "summary": "外婆喜欢逛超市和买零食，旅行中给她买零食她会特别开心。",
            "state": "active",
        },
    ]
    
    print("=== LLM 决策 ===")
    action, reason = llm_decide_action(
        new_text=new,
        candidates=cands,
        new_state="historical",
        layer="L3",
    )
    print(f"action: {action}")
    print(f"reason: {reason}")
    
    print("\n=== 纯规则 fallback 对比 ===")
    action, reason = llm_decide_action(
        new_text=new,
        candidates=cands,
        new_state="historical",
        layer="L3",
        use_llm=False,
    )
    print(f"action: {action}")
    print(f"reason: {reason}")
