#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Recall 融合层（B 版 / 激进）：图谱 × 向量融合的全部中间件

设计目标：
  - 不动 process_dream.py 里任何原函数，recall / recall_for_hook 都自动受益
  - 提供三个 hook 点，在原流程的三个位置接入：
      1. fusion_transform_channel(channel_items, channel_name)
         → 在 rrf_fuse 之前调用，给 vec / graph 通道的 item 重新打分 / 加权
      2. fusion_boost_graph_hits(fused, graph_entity_names)
         → rrf_fuse 之后，给图谱命中（含 vec+graph）的条目加权
      3. fusion_post_fuse(fused)
         → 融合完成后再做一遍过滤 / 重排 / 归一化
  - 主动召回 (recall_for_hook) 和被动召回 (recall) 都调同一个融合层，行为一致

包含的优化（按优先级）：
  ★ 核心 5 条：
    1. graph 通道深度打分（一跳 > 二跳）
    2. graph 实体重叠 boost（从 *1.0 复活为 *1.3）
    3. kg_verify 不再覆盖 vec 排序（保留 RRF 分数）
    4. importance 加权（payload 里的 importance 字段）
    5. 写入时把 relations 拼进向量文本

  ☆ 进阶 3 条：
    6. 时间衰减（半年内的记忆优先）
    7. tag 预过滤（召回前按 query 命中 tag 做硬过滤）
    8. 实体别名归一化（写入时 LLM 抽出的名字归一化到已有标准名）

  · 预留接口：
    - fusion_graphrag_hooks：未来接 GraphRAG 时（实体子图、社区摘要、全局查询）
      只需要在 graph_channel 构造后插入一次 fusion_graphrag_hooks(graph_channel)

调参全在 recall_config.RecallConfig，环境变量即可。
"""

import re
import hashlib
import math
from datetime import datetime, timezone, timedelta

from recall_config import RecallConfig

try:
    from bm25_index import bm25_search
    BM25_AVAILABLE = True
except Exception:
    BM25_AVAILABLE = False


# ============================================================
# 工具：现在时间 / 解析时间
# ============================================================

CN_TZ = timezone(timedelta(hours=8))


def _now_cn() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(ts_str: str):
    """尽量宽松地解析 payload 里的 ts 字段。失败返回 None。"""
    if not ts_str or not isinstance(ts_str, str):
        return None
    s = ts_str.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=CN_TZ)
        except ValueError:
            continue
    return None


# ============================================================
# 进阶 6：时间衰减
# ============================================================

def time_decay(items, half_life_days=180):
    """对每个 item 的 score 乘上时间衰减因子。
       weight = 0.5 ** (Δdays / half_life_days)
       - 半衰期 180 天：180 天前的记忆权重降到 0.5
       - 1 天内的记忆权重 ~1.0
       - 缺 ts 的记忆按 1.0 处理（不衰减）
    """
    now = datetime.now(CN_TZ)
    for it in items:
        ts = _parse_ts(it.get("ts") or "")
        if ts is None:
            continue
        delta_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        weight = 0.5 ** (delta_days / max(half_life_days, 1.0))
        sk = float(it.get("sort_key", it.get("score", 0)))
        it["sort_key"] = round(sk * weight, 4)
    return items


# ============================================================
# 核心 4：importance 加权
# ============================================================

def importance_weight(items):
    """对每个 item 的排序键 sort_key 乘以 (0.5 + importance)。
       importance=0.5 → 1.0×  不变
       importance=1.0 → 1.5×  重要记忆浮顶
       importance=0.0 → 0.5×  低价值记忆下沉
       缺 importance 按 0.5 处理
       🔧 2026-08-10 修复：不再覆盖 score（score 保留 RRF 原始分，
       否则 score 会 >1 且 kg_verify 阈值失效）。加权只影响 sort_key。
    """
    for it in items:
        imp = float(it.get("importance", 0.5))
        imp = max(0.0, min(1.0, imp))
        weight = 0.5 + imp
        sk = float(it.get("sort_key", it.get("score", 0)))
        it["sort_key"] = round(sk * weight, 4)
    return items


# ============================================================
# 核心 1：graph 通道深度打分
# ============================================================

def graph_depth_score(items):
    """把 graph 通道 item 的恒 1.0 分数替换为深度衰减分。
       1 跳 → 1.0×；2 跳 → 0.5×；3 跳 → 0.33×
       已经带 score 的不动（以防手动指定）
    """
    for it in items:
        depth = int(it.get("depth", 1))
        # 只对 source=graph 的 item 起作用，且 score 是 1.0（默认）时替换
        if it.get("source") == "graph" and float(it.get("score", 0)) >= 0.99:
            it["score"] = round(1.0 / (1 + max(0, depth - 1)), 4)
    return items


# ============================================================
# 进阶 7：tag 预过滤
# ============================================================

def tag_prefilter(items, query_tags=None):
    """按 query 命中的 tag 做硬过滤：只保留至少有一个共同 tag 的 item。
       如果 query_tags 为空或 item 没 tag，则不过滤（兼容旧数据）。
    """
    if not query_tags:
        return items
    qset = set(t.lower() for t in query_tags if t)
    if not qset:
        return items
    out = []
    for it in items:
        tags = it.get("tags") or []
        tset = set(t.lower() for t in tags if t)
        if not tset or tset & qset:
            out.append(it)
    return out


# ============================================================
# Hook 1：通道级打分（rrf_fuse 之前调）
# ============================================================

def fusion_transform_channel(items, channel_name):
    """对单个通道的 item 做打分调整。在 rrf_fuse 之前调用。

    channel_name: "vec" | "graph" | 其他
    """
    if not items:
        return items
    if channel_name == "graph":
        items = graph_depth_score(items)
    elif channel_name == "vec":
        # vec 通道目前不需要转换，未来 sparse/BM25 通道可在此扩展
        pass
    elif channel_name == "bm25":
        # BM25 通道：归一化 score（norm_score 已经由 bm25_search 处理）
        # 这里只做兜底：确保每个 item 有 sort_key 供 RRF 使用
        for it in items:
            if "sort_key" not in it:
                it["sort_key"] = float(it.get("norm_score", it.get("score", 0)))
    return items


# ============================================================
# Hook 2：图命中 boost（rrf_fuse 之后调）
# ============================================================

def fusion_boost_graph_hits(fused, graph_entity_names, boost=1.3):
    """对图谱命中的 item 加权。
       原代码 bug：判定 "graph" in source，但 rrf_fuse 已把 source 改成首个通道名。
       修复：用 _channels（列表）判断是否含 graph，且用实体名做二次验证。
    """
    if not fused or not graph_entity_names:
        return fused
    boosted = []
    for item in fused:
        channels = item.get("_channels") or []
        summary = (item.get("summary") or "").lower()
        is_graph_hit = (
            "graph" in channels and
            any(ent and ent.lower() in summary for ent in graph_entity_names)
        )
        if is_graph_hit:
            sk = float(item.get("sort_key", item.get("score", 0)))
            item = {**item, "sort_key": round(sk * boost, 4)}
        boosted.append(item)
    boosted.sort(key=lambda x: -float(x.get("sort_key", x.get("score", 0))))
    return boosted


# ============================================================
# Hook 3：融合后处理（kg_verify 之前调）
# ============================================================

def fusion_post_fuse(fused):
    """融合后再做一遍 importance 加权 + 时间衰减。
       注意：必须在 kg_verify 之前跑，这样强档过滤用的是加权后的分数。
       🔧 2026-08-10 修复：sort_key 初始化为 score（RRF 原始分），
       加权/衰减只改 sort_key，score 保持 0~1 可解释。
       🔧 2026-08-21：relation point 碎片展示时用 parent_summary 还原可读记忆。
    """
    if not fused:
        return fused
    for it in fused:
        if "sort_key" not in it:
            it["sort_key"] = float(it.get("score", 0))
    # relation point（来自 memory_relation）的 summary 是碎片，
    # parent_summary 已在写库时写入 payload，直接用它替换碎片
    for it in fused:
        if it.get("_point_type") == "relation":
            parent_sum = it.get("parent_summary", "") or ""
            if parent_sum:
                it["summary"] = parent_sum
    fused = importance_weight(fused)
    fused = time_decay(fused)
    fused.sort(key=lambda x: -float(x.get("sort_key", x.get("score", 0))))
    return fused


# ============================================================
# 核心 3：kg_verify 重写（保留 RRF 分数，不覆盖）
# ============================================================

def kg_verify_v2(items, query, embed_fn=None, min_similarity=None):
    """kg_verify v3（2026-08-21 重构）：
       - 用 embedding 独立计算 query 与每条 summary 的 cosine sim
       - 弱档 (>= KG_WEAK_THRESHOLD)：保留，标 is_weak=True
       - 强档 (>= KG_STRONG_THRESHOLD)：标 is_weak=False
       - 不足 top_n 时不强补
       - ⚠️ 排序：综合 sort_key（RRF×boost×importance×衰减）和 sim，
         不再完全按 sim 重排，避免抹掉前面的多通道证据积累。
         final_score = sort_key * (1-w) + sim * w，w = KG_SIM_RANKING_WEIGHT（默认 0.5）
    """
    strong_threshold = RecallConfig.KG_STRONG_THRESHOLD
    weak_threshold = RecallConfig.KG_WEAK_THRESHOLD
    top_n = RecallConfig.KG_TOP_N
    w = RecallConfig.KG_SIM_RANKING_WEIGHT

    if min_similarity is not None:
        strong_threshold = min_similarity
        weak_threshold = min_similarity

    if not items:
        return []

    # 算 embedding（embed_fn 可注入便于测试）
    if embed_fn is None:
        from process_dream import embed
        embed_fn = embed

    summaries = [it.get("summary") or "" for it in items]
    vectors = embed_fn([query] + summaries)
    if not vectors or len(vectors) < 2:
        # embed 失败 = 召回失败，返 []，不要返回未验证的项
        return []
    query_vec = vectors[0]

    sims = []
    for i in range(len(items)):
        mem_vec = vectors[i + 1]
        if not mem_vec:
            sims.append(0.0)
            continue
        dot = sum(a * b for a, b in zip(query_vec, mem_vec))
        nq = sum(a * a for a in query_vec) ** 0.5
        nm = sum(b * b for b in mem_vec) ** 0.5
        sims.append(dot / (nq * nm + 1e-9))

    out = []
    for it, sim in zip(items, sims):
        if sim < weak_threshold:
            continue
        sk = float(it.get("sort_key", it.get("score", 0)))
        # ── 2026-08-21：综合排序，保留前面的多通道证据积累 ──
        # sort_key 已经是 RRF×boost×importance×time_decay 的融合结果
        # sim 是语义精排信号，两者加权组合
        final = round(sk * (1 - w) + sim * w, 4)
        new_it = {
            **it,
            "sim": round(sim, 4),
            "sort_key": final,      # 综合分覆写 sort_key，后续继续用 sort_key 排序
            "is_weak": sim < strong_threshold,
        }
        out.append(new_it)

    # 综合分降序，保留多通道证据
    out.sort(key=lambda x: -float(x.get("sort_key", 0)))
    return out[:top_n]


# ============================================================
# 进阶 9：实体名清洗（防止 LLM 抽到工具词当实体名）
# ============================================================

# 中文实体工具词黑名单（出现 = LLM 抽取错误的概率大）
_TOOL_WORDS = {
    "召回", "记忆", "实体", "关系", "抽取", "嵌入", "向量", "数据库",
    "知识", "图谱", "存储", "检索", "写入", "节点", "边缘",
    "KO", "ko", "summary", "relation", "entity", "embedding",
    "qdrant", "neo4j", "memoryos", "openclaw", "bge", "m3",
}

# 中文实体名最长不超过 MAX_ENTITY_LEN 个字
MAX_ENTITY_LEN = 12


def _is_dirty_entity(name: str) -> bool:
    if not name:
        return True
    s = name.strip()
    # 太长
    if len(s) > MAX_ENTITY_LEN:
        return True
    # 包含工具词（大小写不敏感）
    s_lower = s.lower()
    for tw in _TOOL_WORDS:
        if tw.lower() in s_lower:
            return True
    # 以 "小" 开头的名字且后续是工具词（如 "小召回"）
    if s.startswith(("小", "老", "阿")) and len(s) <= 4:
        for tw in _TOOL_WORDS:
            if tw.lower() in s_lower:
                return True
    return False


def clean_ko_for_write(ko):
    """清洗 LLM 抽出的 KO：丢脏实体、丢含脏实体的关系、必要时丢整个 KO。
    返回 (cleaned_ko, dropped: bool)
    """
    if not ko:
        return ko, True

    ents = ko.get("entities") or []
    rels = ko.get("relations") or []

    # 1. 过滤脏实体
    clean_ents = []
    dirty_ents = set()
    for e in ents:
        name = (e.get("name") or "").strip()
        if _is_dirty_entity(name):
            dirty_ents.add(name)
        else:
            clean_ents.append(e)
    ko["entities"] = clean_ents

    # 2. 过滤含脏实体的关系
    clean_rels = []
    for r in rels:
        subj = (r.get("subject") or "").strip()
        obj = (r.get("object") or "").strip()
        if subj in dirty_ents or obj in dirty_ents:
            continue
        clean_rels.append(r)
    ko["relations"] = clean_rels

    # 3. summary 里如果都是工具词，丢整个 KO
    summary = (ko.get("summary") or "").strip()
    if not summary:
        return ko, True

    # 4. 没有任何实体也没有任何关系 → 丢
    if not clean_ents and not clean_rels:
        return ko, True

    return ko, False


# ============================================================
# 核心 5：写入时把 relations 拼进向量文本
# ============================================================

def build_qdrant_text(ko):
    """构造 Qdrant 向量文本：summary + entities + relations 三元组。
       返回 (text, importance) 给 write_kos 用。
    """
    summary = (ko.get("summary") or "").strip()
    entities_text = " ".join(
        (e.get("name") or "").strip()
        for e in (ko.get("entities") or [])
        if (e.get("name") or "").strip()
    )
    relations_text = " ".join(
        f"{(r.get('subject') or '').strip()} {(r.get('predicate') or '').strip()} {(r.get('object') or '').strip()}"
        for r in (ko.get("relations") or [])
        if (r.get('subject') or '').strip() and (r.get('object') or '').strip()
    )
    parts = [p for p in (summary, entities_text, relations_text) if p]
    text = " ".join(parts)
    importance = float(ko.get("importance", 0.5))
    importance = max(0.0, min(1.0, importance))
    return text, importance


def build_relation_text(ko):
    """构造关系向量文本：只抽取 relations 三元组，脱离 summary 独立描述关系。
       返回关系文本字符串（空字串表示无有效关系）。
    """
    parts = []
    for r in (ko.get("relations") or []):
        subj = (r.get("subject") or "").strip()
        pred = (r.get("predicate") or "").strip()
        obj = (r.get("object") or "").strip()
        if subj and obj:
            parts.append(f"{subj} {pred} {obj}")
    return " ".join(parts)


# ============================================================
# 进阶 8：实体别名归一化
# ============================================================

_NEO4J_DRIVER = None


def _get_driver():
    global _NEO4J_DRIVER
    if _NEO4J_DRIVER is None:
        import os
        from neo4j import GraphDatabase
        _NEO4J_DRIVER = GraphDatabase.driver(
            os.environ.get("MEMORY_OS_NEO4J_URI", "bolt://127.0.0.1:7687"),
            auth=(
                os.environ.get("MEMORY_OS_NEO4J_USER", "neo4j"),
                os.environ.get("MEMORY_OS_NEO4J_PASSWORD", "openclaw"),
            ),
        )
    return _NEO4J_DRIVER


def normalize_entity_names(names):
    """把 LLM 抽出来的实体名字归一化到已有标准名。
       规则：查 Neo4j 里是否已有 name 相似（jaccard >= 0.6 或子串匹配）的节点。
       返回 dict: {原名: 标准名}，没找到就不在 dict 里。
    """
    if not names:
        return {}
    driver = _get_driver()
    mapping = {}
    try:
        with driver.session() as session:
            for name in names:
                if not name or len(name) < 2:
                    continue
                # 查已有节点：精确 / 子串 / 前缀匹配
                cypher = """
                MATCH (n)
                WHERE n.name IS NOT NULL
                WITH n, CASE
                    WHEN toLower(n.name) = toLower($name) THEN 1.0
                    WHEN toLower(n.name) CONTAINS toLower($name) OR toLower($name) CONTAINS toLower(n.name) THEN 0.7
                    ELSE 0.0
                END AS sim
                WHERE sim >= 0.7
                RETURN n.name AS std, sim
                ORDER BY sim DESC
                LIMIT 1
                """
                rec = session.run(cypher, name=name).data()
                if rec:
                    std = rec[0]["std"]
                    if std != name:
                        mapping[name] = std
    except Exception:
        return mapping
    return mapping


# ============================================================
# 预留接口：GraphRAG 钩子（未来接社区摘要 / 实体子图）
# ============================================================

def fusion_graphrag_hooks(graph_channel_items):
    """未来 GraphRAG 接入点：社区摘要、全局查询、实体子图扩展。
       当前是 stub，直接返回原 items。
    """
    return graph_channel_items


# ============================================================
# 自检
# ============================================================

if __name__ == "__main__":
    # 模拟一些数据
    graph_items = [
        {"summary": "老豆 KNOWS 小橘子", "source": "graph", "score": 1.0, "depth": 1},
        {"summary": "外婆 PARENT_OF 老豆", "source": "graph", "score": 1.0, "depth": 2},
    ]
    vec_items = [
        {"summary": "向量召回的记忆 A", "source": "vec", "score": 0.9},
        {"summary": "向量召回的记忆 B", "source": "vec", "score": 0.8},
    ]
    graph_items = graph_depth_score(graph_items)
    vec_items = importance_weight(vec_items)
    fused = graph_items + vec_items
    fused = fusion_post_fuse(fused)
    print("graph after depth_score:", graph_items)
    print("vec after importance_weight:", vec_items)
    print("fused after post_fuse:", fused)