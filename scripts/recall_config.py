#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Recall 流水线的全部可调参数集中地（"参数中心"）

设计原则：
  - 所有可调参数都在这里，默认值等于 process_dream.py / recall_gate.py
    当前硬编码的值，确保不改 env 时行为零变化
  - 优先级：环境变量 > 类内默认值
  - 调参只改环境变量，不必动代码；也不必重启插件外的常驻进程
    （下次进程启动 / 重新 import 时生效）

环境变量 → 参数映射（部分）：
  Hook 门控:
    MEMORY_OS_HOOK_MIN_LEN          默认 7
    MEMORY_OS_HOOK_MAX_LEN          默认 300
    MEMORY_OS_HOOK_SKIP_FILLER      默认白名单（逗号或换行分隔）
    MEMORY_OS_HOOK_SKIP_SWEAR       默认粗言秽语正则（换行分隔）
    MEMORY_OS_FILLER_PATTERNS       默认纯情绪宣泄正则（换行分隔）
    MEMORY_OS_SWEAR_PATTERNS        默认 KO 兜底 SWEAR 正则（换行分隔）
  向量召回:
    MEMORY_OS_VEC_TOP_K_DEFAULT     默认 20（qdrant_search 内部 top_k）
    MEMORY_OS_VEC_TOP_K_MULTIPLIER  默认 2（recall 里 top_k * multiplier）
    MEMORY_OS_COLLECTIONS           默认 8 个 collection，逗号分隔
  图召回:
    MEMORY_OS_GRAPH_DEPTH           默认 1
    MEMORY_OS_GRAPH_LIMIT_PER_NODE  默认 4
  RRF 融合:
    MEMORY_OS_RRF_K                 默认 60
    MEMORY_OS_RRF_RELATIVE_KEEP_RATIO  默认 0.95（max_score * ratio）
  KG verify:
    MEMORY_OS_KG_STRONG_THRESHOLD   默认 0.7
    MEMORY_OS_KG_WEAK_THRESHOLD     默认 0.68（低于此值的记忆不进召回）
    MEMORY_OS_KG_TOP_N              默认 5
  Recall 入口:
    MEMORY_OS_RECALL_DEFAULT_TOP_K  默认 8
    MEMORY_OS_RECALL_DEFAULT_RRF_K  默认 60
  写入去重:
    MEMORY_OS_DEDUP_THRESHOLD       默认 0.82
"""

from pathlib import Path
import os
import json


# ============================================================
# 通用环境变量读取辅助
# ============================================================

def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    v = os.environ.get(key)
    return int(v) if v else default


def _env_float(key: str, default: float) -> float:
    v = os.environ.get(key)
    return float(v) if v else default


def _env_list(key: str, default):
    """环境变量按换行或逗号分隔的多值；空字符串返回默认。"""
    v = os.environ.get(key)
    if v is None or v == "":
        return list(default)
    if "\n" in v:
        out = [s.strip() for s in v.splitlines() if s.strip()]
    else:
        out = [s.strip() for s in v.split(",") if s.strip()]
    return out


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ============================================================
# 默认值（与原硬编码值一一对应，改这里就是改默认行为）
# ============================================================

# Hook 门控
_DEFAULT_HOOK_MIN_LEN = 5   # 开发者 2026-08-25：字数要求降到2字 → 5字
_DEFAULT_HOOK_MAX_LEN = 300  # 开发者 2026-08-20：太长段落按句拆，不整段送召回
_DEFAULT_HOOK_SKIP_FILLER = [
    "嗯", "啊", "哦", "嗯嗯", "啊啊", "哦哦", "噢", "喔", "嗳",
    "好", "好的", "好哒", "行", "可以", "ok", "OK", "Ok", "okay", "OKAY",
    "继续", "再来", "搞起", "嗯好", "好哦", "看看",
    "y", "n", "no", "yes", "yeah", "yep", "nope",
    "ok呀", "ok啊", "好呀", "好啦",
    "对", "对的", "是的", "是的呢", "没错", "确实",
    "跑", "跑一下", "继续跑", "再来一次", "继续吧",
    "hi", "hello", "hey", "嗨", "你好", "在吗", "在么",
    "test", "测试", "测试一下",
]
_DEFAULT_HOOK_SKIP_SWEAR = [
    r"\b(他妈的|操|fuck|fucking|shit|damn|傻逼|草|妈的|滚|煞笔|智障)\b",
]
_DEFAULT_FILLER_PATTERNS = [
    r"^(今天好累|好困|饿了|无聊|没什么|嗯+|啊+|哦+)\s*[。.!！]?$",
]
_DEFAULT_SWEAR_PATTERNS = [
    r"\b(他妈的|操|fuck|shit|damn|傻逼|草|妈的)\b",
]

# 向量召回
_DEFAULT_VEC_TOP_K = 3   # 开发者 2026-08-20：BM25 主召，Vec 辅助；改为 top3
_DEFAULT_VEC_TOP_K_MULTIPLIER = 1  # 开发者 2026-08-09 改：2 → 1（vec 召回直接要 top-20，不再乘 2）
_DEFAULT_BM25_TOP_K = 5   # BM25 通道候选数（后续会与 RecallConfig.BM25_TOP_K 取 max，确保拉够候选）
_DEFAULT_COLLECTIONS = [
    "memory_atom", "memory_fact", "memory_event", "memory_experience",
    "memory_preference", "memory_routine", "memory_concept",
    "memory_relation", "memory_observation",
]

# 图召回
_DEFAULT_GRAPH_DEPTH = 1          # 开发者 2026-08-08 改回 1 跳：避免 2 跳把"Memory OS"整张子图捞回来
_DEFAULT_GRAPH_LIMIT_PER_NODE = 4  # 每节点扩展上限

# 向量召回质量门槛（开发者 2026-08-08 加）
# 业界共识：cosine < 0.55 都是垃圾，参考
#   https://dev.to/yaruyng/retrieval-strategy-design-vector-keyword-and-hybrid-search-53j3
#   "> 0.85 强相关 / 0.75-0.85 可接受 / < 0.70 噪音"
_DEFAULT_VEC_MIN_SCORE = 0.60     # 开发者 2026-08-25 16:43：0.70 → 0.60（提高召回召回数）

# RRF 融合
_DEFAULT_RRF_K = 60
_DEFAULT_RRF_RELATIVE_KEEP_RATIO = 0.95

# KG verify
_DEFAULT_KG_STRONG_THRESHOLD = 0.7
_DEFAULT_KG_WEAK_THRESHOLD = 0.60
_DEFAULT_KG_TOP_N = 5
# ── 2026-08-21 新增 ──
# BM25 关键词过滤（开发者 2026-08-21：降为软过滤，不阻断）
_DEFAULT_BM25_KEYWORD_FILTER_RATIO = 0.0   # 0=关闭；>0=BM25结果中要求至少N%含关键词才保留
_DEFAULT_BM25_KEYWORD_FILTER_MIN = 0       # 至少保留 N 条，不足则全部保留
# PRF 触发：字面重叠阈值（开发者 2026-08-21：改为实体/关系触发，不再只看字面）
_DEFAULT_PRF_TOKEN_OVERLAP_MIN = 0  # 0=关闭字面触发，改用实体/关系触发
# kg_verify 综合排序：sim 对最终排序的权重（0=纯 sort_key，1=纯 sim）
_DEFAULT_KG_SIM_RANKING_WEIGHT = 0.61
# 意图过滤模式（开发者 2026-08-21：动词意图软化，不再硬过滤）
_DEFAULT_INTENT_VERB_HARD_FILTER = False
_DEFAULT_INTENT_VERB_SOFT_WEIGHT = 0.05
# 调试日志级别
_DEFAULT_RECALL_DEBUG = os.environ.get("MEMORY_OS_RECALL_DEBUG", "0")

# Recall 入口
# 开发者 2026-08-10 改：5 → 8 → 12（业界黄金区 8-15；vec 主力召够多，覆盖跨话题对话）
_DEFAULT_RECALL_TOP_K = 8  # 开发者 2026-08-20：融合后保留8条进kg_verify，不凑数
_DEFAULT_RECALL_RRF_K = 60

# 写入去重
_DEFAULT_DEDUP_THRESHOLD = 0.82

# 写入决策 v5（开发者 2026-08-10：Mem0 方案 5）
_DEFAULT_WRITE_ANN_RECALL_THRESHOLD = 0.80   # ANN 召回门槛（召回宽，让 LLM 决策严）
_DEFAULT_WRITE_DECISION_TOP_K = 3            # 召回候选数（top-K 给 LLM 看）
_DEFAULT_WRITE_DECISION_TEMPERATURE = 0.1    # LLM 决策温度（0.1 = 准确定性 + 防 thinking 循环）
_DEFAULT_WRITE_DECISION_MAX_TOKENS = 500     # LLM 决策输出上限
_DEFAULT_WRITE_DECISION_MODEL = "MiniMax-M3  # 决策专用模型（开发者 2026-08-10 拍：M3 输出更稳）
_DEFAULT_WRITE_DECISION_LOG_PATH = str(Path.home()) + "/.openclaw/workspace/memory-os/logs/write-decision.md"

# 会话级 query 去重缓存
_DEFAULT_SESSION_CACHE_ENABLED = True
_DEFAULT_SESSION_CACHE_TTL = 0  # 0 = 仅本次进程内有效

# ============================================================
# 写入 schema 白名单（开发者 2026-08-10 拍）
# 原则：
#   - 关系：49 个，按场景分组（人物基础 / 生活行为 / 情感表达 / 学习工作 / 拥有状态 / 时间锚点）
#   - label：11 个，按生活场景裁剪（砍业务扩展 Product/Feature/Disease/...）
#   - 不在白名单 → 降 MENTIONED_IN（关系） / 降 Concept（label）
# ============================================================
_DEFAULT_ALLOWED_RELATIONSHIPS = {
    # ---- 人物基础（10）----
    "KNOWS", "FRIEND_OF", "PARENT_OF", "CHILD_OF", "SIBLING_OF",
    "LOVES", "LIKES", "DISLIKES", "HATES", "RESPECTS",
    # ---- 生活行为（15）----
    "ATE", "DRANK", "COOKED",
    "PLAYED_WITH", "WATCHED", "LISTENED_TO", "READ",
    "VISITED", "VISITED_WITH", "WALKED_WITH", "RODE",
    "SHOPPED_WITH", "ATE_WITH", "CELEBRATED",
    # ---- 情感表达（5）----
    "COMFORTED", "ENCOURAGED", "ARGUED_WITH", "APOLOGIZED_TO", "MISSES",
    # ---- 学习工作（8）----
    "TEACHES", "LEARNS_FROM", "WORKS_AT", "MANAGES", "COLLABORATES_WITH",
    "STUDIED", "PRACTICED", "COACHED",
    # ---- 拥有状态（7）----
    "OWNS", "HAS_GOAL", "HAS_HABIT", "HAS_PREFERENCE", "HAS_EXPERIENCE",
    "BELIEVES", "REMEMBERS",
    # ---- 时间锚点（3）----
    "HAPPENED_ON", "HAPPENED_IN", "HAPPENED_DURING",
    # ---- 兜底（1，唯一）----
    "MENTIONED_IN",
}

_DEFAULT_ALLOWED_LABELS = {
    "Person", "Place", "Organization", "Animal",
    "Concept", "Object", "Event", "Goal", "Routine", "State",
    "Decision",
}

# 拒收谓词集合（中文 / 系词 / 形容词 / 动词原形）
_DENIED_PREDICATE_CHARS = set("的一是不了有在和人这中大为上个们来到时说看要也能")
# 明确拒收的系词 / 泛词 / 模糊词（LLM 抽到直接降级）
_DENIED_PREDICATE_WORDS = {
    "是", "的", "有", "像", "等于", "以前", "和外婆一样",  # 系词 / 时间泛词 / 模糊比喻
    "喜欢", "爱", "讨厌", "不喜欢", "喜欢看", "喜欢吃", "喜欢去", "喜欢做",  # 中文动词原形
    "骑",  # 动作原形
}


# ============================================================
# 主配置类
# ============================================================

class RecallConfig:
    """所有可调参数。访问方式：RecallConfig.HOOK_MIN_LEN 等"""

    # ---- Hook 门控 ----
    HOOK_MIN_LEN = _env_int("MEMORY_OS_HOOK_MIN_LEN", _DEFAULT_HOOK_MIN_LEN)
    HOOK_MAX_LEN = _env_int("MEMORY_OS_HOOK_MAX_LEN", _DEFAULT_HOOK_MAX_LEN)

    HOOK_SKIP_FILLER = set(_env_list(
        "MEMORY_OS_HOOK_SKIP_FILLER",
        _DEFAULT_HOOK_SKIP_FILLER,
    ))
    HOOK_SKIP_SWEAR = _env_list(
        "MEMORY_OS_HOOK_SKIP_SWEAR",
        _DEFAULT_HOOK_SKIP_SWEAR,
    )
    FILLER_PATTERNS = _env_list(
        "MEMORY_OS_FILLER_PATTERNS",
        _DEFAULT_FILLER_PATTERNS,
    )
    SWEAR_PATTERNS = _env_list(
        "MEMORY_OS_SWEAR_PATTERNS",
        _DEFAULT_SWEAR_PATTERNS,
    )

    # ---- 向量召回 ----
    VEC_TOP_K_DEFAULT = _env_int("MEMORY_OS_VEC_TOP_K_DEFAULT", _DEFAULT_VEC_TOP_K)
    BM25_TOP_K = _env_int("MEMORY_OS_BM25_TOP_K", _DEFAULT_BM25_TOP_K)
    VEC_TOP_K_MULTIPLIER = _env_int("MEMORY_OS_VEC_TOP_K_MULTIPLIER", _DEFAULT_VEC_TOP_K_MULTIPLIER)
    VEC_MIN_SCORE = _env_float("MEMORY_OS_VEC_MIN_SCORE", _DEFAULT_VEC_MIN_SCORE)
    COLLECTIONS = _env_list(
        "MEMORY_OS_COLLECTIONS",
        _DEFAULT_COLLECTIONS,
    )

    # ---- 图召回 ----
    GRAPH_DEPTH = _env_int("MEMORY_OS_GRAPH_DEPTH", _DEFAULT_GRAPH_DEPTH)
    GRAPH_LIMIT_PER_NODE = _env_int("MEMORY_OS_GRAPH_LIMIT_PER_NODE", _DEFAULT_GRAPH_LIMIT_PER_NODE)

    # ---- RRF 融合 ----
    RRF_K = _env_int("MEMORY_OS_RRF_K", _DEFAULT_RRF_K)
    RRF_RELATIVE_KEEP_RATIO = _env_float(
        "MEMORY_OS_RRF_RELATIVE_KEEP_RATIO", _DEFAULT_RRF_RELATIVE_KEEP_RATIO
    )

    # ---- KG verify ----
    KG_STRONG_THRESHOLD = _env_float(
        "MEMORY_OS_KG_STRONG_THRESHOLD", _DEFAULT_KG_STRONG_THRESHOLD
    )
    KG_WEAK_THRESHOLD = _env_float(
        "MEMORY_OS_KG_WEAK_THRESHOLD", _DEFAULT_KG_WEAK_THRESHOLD
    )
    KG_TOP_N = _env_int("MEMORY_OS_KG_TOP_N", _DEFAULT_KG_TOP_N)

    # ---- Recall 入口默认值 ----
    RECALL_DEFAULT_TOP_K = _env_int(
        "MEMORY_OS_RECALL_DEFAULT_TOP_K", _DEFAULT_RECALL_TOP_K
    )
    RECALL_DEFAULT_RRF_K = _env_int(
        "MEMORY_OS_RECALL_DEFAULT_RRF_K", _DEFAULT_RECALL_RRF_K
    )

    # ---- 写入去重 ----
    DEDUP_THRESHOLD = _env_float(
        "MEMORY_OS_DEDUP_THRESHOLD", _DEFAULT_DEDUP_THRESHOLD
    )

    # ---- 写入决策 v5（开发者 2026-08-10：Mem0 方案 5）----
    WRITE_ANN_RECALL_THRESHOLD = _env_float(
        "MEMORY_OS_WRITE_ANN_RECALL_THRESHOLD",
        _DEFAULT_WRITE_ANN_RECALL_THRESHOLD,
    )
    WRITE_DECISION_TOP_K = _env_int(
        "MEMORY_OS_WRITE_DECISION_TOP_K",
        _DEFAULT_WRITE_DECISION_TOP_K,
    )
    WRITE_DECISION_TEMPERATURE = _env_float(
        "MEMORY_OS_WRITE_DECISION_TEMPERATURE",
        _DEFAULT_WRITE_DECISION_TEMPERATURE,
    )
    WRITE_DECISION_MAX_TOKENS = _env_int(
        "MEMORY_OS_WRITE_DECISION_MAX_TOKENS",
        _DEFAULT_WRITE_DECISION_MAX_TOKENS,
    )
    WRITE_DECISION_MODEL = _env_str(
        "MEMORY_OS_WRITE_DECISION_MODEL",
        _DEFAULT_WRITE_DECISION_MODEL,
    )
    WRITE_DECISION_LOG_PATH = _env_str(
        "MEMORY_OS_WRITE_DECISION_LOG_PATH",
        _DEFAULT_WRITE_DECISION_LOG_PATH,
    )

    # ---- 会话级 query 去重 ----
    SESSION_CACHE_ENABLED = _env_bool(
        "MEMORY_OS_SESSION_CACHE_ENABLED", _DEFAULT_SESSION_CACHE_ENABLED
    )
    SESSION_CACHE_TTL = _env_int(
        "MEMORY_OS_SESSION_CACHE_TTL", _DEFAULT_SESSION_CACHE_TTL
    )

    # ── 2026-08-21 新增召回调参 ──
    # BM25 关键词过滤：降为软过滤，不再硬阻断
    BM25_KEYWORD_FILTER_RATIO = _env_float(
        "MEMORY_OS_BM25_KEYWORD_FILTER_RATIO", _DEFAULT_BM25_KEYWORD_FILTER_RATIO
    )
    BM25_KEYWORD_FILTER_MIN = _env_int(
        "MEMORY_OS_BM25_KEYWORD_FILTER_MIN", _DEFAULT_BM25_KEYWORD_FILTER_MIN
    )
    # PRF 字面触发阈值：0=关闭字面触发，改用实体/关系触发
    PRF_TOKEN_OVERLAP_MIN = _env_int(
        "MEMORY_OS_PRF_TOKEN_OVERLAP_MIN", _DEFAULT_PRF_TOKEN_OVERLAP_MIN
    )
    # kg_verify 综合排序：0=纯 sort_key，1=纯 sim
    KG_SIM_RANKING_WEIGHT = _env_float(
        "MEMORY_OS_KG_SIM_RANKING_WEIGHT", _DEFAULT_KG_SIM_RANKING_WEIGHT
    )
    # 意图过滤：动词硬过滤开关
    INTENT_VERB_HARD_FILTER = _env_bool(
        "MEMORY_OS_INTENT_VERB_HARD_FILTER", _DEFAULT_INTENT_VERB_HARD_FILTER
    )
    INTENT_VERB_SOFT_WEIGHT = _env_float(
        "MEMORY_OS_INTENT_VERB_SOFT_WEIGHT", _DEFAULT_INTENT_VERB_SOFT_WEIGHT
    )
    # 调试日志
    RECALL_DEBUG = _env_str(
        "MEMORY_OS_RECALL_DEBUG", _DEFAULT_RECALL_DEBUG
    )

    # ---- 自检 / dump ----
    @classmethod
    def dump(cls) -> str:
        """所有当前生效参数 dump 成 JSON（便于记入 hook-trace.md）"""
        out = {}
        for k in dir(cls):
            if k.startswith("_"):
                continue
            v = getattr(cls, k)
            if callable(v):
                continue
            out[k] = sorted(v) if isinstance(v, set) else (
                list(v) if isinstance(v, (list, tuple)) else v
            )
        return json.dumps(out, ensure_ascii=False, indent=2)

    # ---- 写入 schema 白名单（开发者 2026-08-10 拍）----
    ALLOWED_RELATIONSHIPS = frozenset(_env_list(
        "MEMORY_OS_ALLOWED_RELATIONSHIPS",
        _DEFAULT_ALLOWED_RELATIONSHIPS,
    ))
    ALLOWED_LABELS = frozenset(_env_list(
        "MEMORY_OS_ALLOWED_LABELS",
        _DEFAULT_ALLOWED_LABELS,
    ))
    DENIED_PREDICATE_WORDS = frozenset(_env_list(
        "MEMORY_OS_DENIED_PREDICATE_WORDS",
        _DENIED_PREDICATE_WORDS,
    ))


if __name__ == "__main__":
    print(RecallConfig.dump())
