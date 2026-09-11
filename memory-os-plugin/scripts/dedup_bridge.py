"""
dedup_bridge.py
================
写入层去重决策桥接器（位于 scripts/ 目录，供 write_4layer.py 调用）。

当前实现：纯规则降级（与旧版 _rule_decide_layer_action 完全一致），
未来可替换为 LLM 语义判断（传 layer + new_text 进来做 prompt）。

接口签名
--------
    dedup_decide_layer_action(state, candidates, layer=None, new_text=None)
        -> (action: str, reason: str)

action 取值
-----------
    "CREATE"     无候选或低分 → 新建记录
    "SKIP"       最高分 ≥ DEDUP_THRESHOLD → 完全重复，跳过
    "UPDATE"     0.6 ≤ 最高分 < 0.95 → 相似，合并补充
    "DISCARD"    state=uncertain + 无候选 → 丢弃
    "INVALIDATE" state=uncertain + 有候选 → 标记旧记录失效
"""

import sys
import os

# recall_config 与本文件同在 scripts/ 目录
from recall_config import RecallConfig


def dedup_decide_layer_action(state, candidates, layer=None, new_text=None):
    """
    规则驱动的去重决策。

    Args:
        state:       写入状态，None/"active"/"historical"/"uncertain"
        candidates:  ANN 召回候选列表，每项含 score 字段
        layer:       层级标签（L0/L1/L2/L3），当前版本未使用，保留给未来 LLM 调用
        new_text:    待写入文本，当前版本未使用，保留给未来 LLM 调用

    Returns:
        (action, reason) 元组
    """
    state = state or "active"

    if not candidates:
        if state == "uncertain":
            return "DISCARD", "state=uncertain, no candidates, discarded"
        return "CREATE", "no candidates"

    best = max(candidates, key=lambda c: c.get("score", 0))
    score = float(best.get("score", 0))

    if state == "uncertain":
        return "INVALIDATE", f"state=uncertain, score={score:.3f}, invalidate old"
    if state == "historical":
        return "UPDATE", f"state=historical, score={score:.3f}, update old to historical"
    if score >= RecallConfig.DEDUP_THRESHOLD:
        return "SKIP", f"dup score={score:.3f} >= {RecallConfig.DEDUP_THRESHOLD}"
    if score >= RecallConfig.WRITE_ANN_RECALL_THRESHOLD:
        return "UPDATE", f"similar score={score:.3f}, merge supplement"
    return "CREATE", f"new score={score:.3f} < recall threshold"
