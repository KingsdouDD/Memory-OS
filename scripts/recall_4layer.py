#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
4 层记忆召回器 v2（2026-08-26 重写）

与 v1 的根本区别：
  v1: L3/L2 的 summary 拆成碎片词 → 塞进 expanded_query → 污染 L1 向量搜索
  v2: L3/L2 的 entities/scenario_id 当 context filter → 直接过滤 L1 召回范围

业界融合原理（结合 extract_prompt 的层级语义）：
  - KG（Neo4j L3/L2）当 Router/Filter，不当 Query 扩展词
  - L1 召回 = Qdrant entity-filter 召回 + entity-overlap 重排
  - PRF 只在 graph sim ≥ 0.62 时触发（0.62 是向量模型分水岭）
  - 最终排序以 entity-overlap + sim 为主，RRF 只当辅助信号

层级语义对应：
  L3 (memory_persona)  → 跨场景稳定实体/关系 → entity filter 最高优先级
  L2 (memory_scenario) → 场景 + 关联实体       → entity filter 次优先级
  L1 (memory_atom)     → 原子记忆             → 主召回，用上层 filter
  L0 (memory_l0)       → 原始对话             → BM25 索引保留，不进主流程
"""

import os
import sys
import json
import pickle
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from process_dream import embed, _qdrant_client
from recall_fusion import fusion_post_fuse, fusion_boost_graph_hits, kg_verify_v2
from recall_config import RecallConfig

# ── Reranker 服务（Qwen3-Reranker-0.6B）──────────────────────────
RERANKER_URL = "http://127.0.0.1:8877/rerank"


def _rerank_via_http(query, candidates, top_k=5, timeout=30):
    """调 reranker HTTP 服务做精排，返回 (index, score) 列表。

    进程 dead 时自动拉起服务（不常驻，只在使用时拉）。
    """
    from service_lifecycle import ensure_service_up
    try:
        ensure_service_up(8877, max_wait=90)
    except Exception as e:
        print(f"[warn] ensure reranker up failed: {e}", file=sys.stderr)
        return []
    import urllib.request
    payload = json.dumps({"query": query, "candidates": candidates, "top_k": top_k}).encode("utf-8")
    req = urllib.request.Request(
        RERANKER_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return [(r["index"], r["score"]) for r in result.get("results", [])]
    except Exception as e:
        print(f"[warn] reranker call failed: {e}", file=sys.stderr)
        return []


L2_COLLECTION = "memory_scenario"
L3_COLLECTION = "memory_persona"
L0_COLLECTION = "memory_l0"

# ── 向量模型分水岭（确认：0.62 是确值，以下都是噪声）─────────────
SIM_WATERMARK = 0.62

# ── L2/L3 召回质量门控 ───────────────────────────────────────────
L3_MIN_SCORE = 0.62   # L3 只有向量分 ≥ 0.62 才参与构造 filter
L2_MIN_SCORE = 0.62   # L2 只有向量分 ≥ 0.62 才参与构造 filter

# ── entity overlap 重排权重 ──────────────────────────────────────
ENTITY_OVERLAP_WEIGHT = 0.3   # entity overlap 对最终 score 的贡献比例

# ── PRF 扩展门控 ─────────────────────────────────────────────────
PRF_MIN_GRAPH_SIM = 0.62      # graph 结果 sim ≥ 0.62 才触发 PRF 扩展

L0_BM25_INDEX_PATH = Path("/tmp/memory_os_bm25_l0.pkl")


# ============================================================
# 工具函数
# ============================================================

def _safe_get(d, key, default=None):
    """安全取 dict 值（兼容 None 本身）。"""
    v = d.get(key, default)
    return v if v is not None else default


def _entities_from_payload(pl):
    """从 Qdrant payload 提取 entity 列表（字符串）。"""
    raw = pl.get("entities") or []
    return [e for e in raw if e and isinstance(e, str) and e.strip()]


def _scenario_ids_from_payload(pl):
    """从 Qdrant payload 提取 scenario_id 列表。"""
    sid = pl.get("scenario_id") or ""
    title = pl.get("title") or ""
    ids = []
    if sid:
        ids.append(str(sid))
    if title and title != sid:
        ids.append(str(title))
    return ids


def _entity_overlap(query_entities, item_entities):
    """计算 entity 重叠率（交集 / query 实体数），返回 0.0~1.0。"""
    if not query_entities or not item_entities:
        return 0.0
    qset = set(e.strip().lower() for e in query_entities if e and len(e.strip()) >= 2)
    iset = set(e.strip().lower() for e in item_entities if e and len(e.strip()) >= 2)
    if not qset:
        return 0.0
    return len(qset & iset) / len(qset)


def _format_memory_with_time(m: dict) -> str:
    """格式化 L1 记忆：带 event_time 和 valid_time 标签。"""
    summary = m.get("summary", "") or ""
    if not summary:
        return ""
    tags = []
    et = m.get("event_time") or {}
    if isinstance(et, dict):
        expr = et.get("expression") or et.get("start")
        if expr:
            tags.append(f"发生: {expr}")
    vt = m.get("valid_time") or {}
    if isinstance(vt, dict):
        vend = vt.get("end")
        vend_type = vt.get("end_type")
        if vend:
            tags.append(f"状态: 已失效 {vend}")
        elif vend_type == "until_revoked":
            tags.append("状态: 至今有效")
    if tags:
        return "[" + "] [".join(tags) + "] " + summary
    return summary


# ============================================================
# Q1. 基础向量搜索（带 entity filter）
# ============================================================

def _qdrant_search_filtered(query_vec, collections, top_k, filter_ents, filter_scenario_ids):
    """带 entity/scenario filter 的 Qdrant 搜索。

    策略：
      - 用 Qdrant Payload Match 过滤 entities（至少命中 filter_ents 里的 1 个）
      - 如果有 filter_scenario_ids，也按 scenario_id 过滤
      - 返回所有命中的结果，entity overlap 重排由上层做
    """
    try:
        from qdrant_client.models import (
            Filter, FieldCondition, MatchAny, MatchValue,
        )
    except ImportError:
        print("[warn] qdrant client models not available", file=sys.stderr)
        return []

    client = _qdrant_client()
    results = []

    for coll in collections:
        try:
            must_clauses = []

            # entity 过滤：L1 payload.entities 至少含 1 个 filter entity
            if filter_ents:
                non_empty = [e for e in filter_ents if e]
                if non_empty:
                    must_clauses.append(
                        FieldCondition(
                            key="entities",
                            match=MatchAny(any=non_empty),
                        )
                    )

            # scenario_id 过滤
            if filter_scenario_ids:
                # 支持 scenario_id 或 title 任一匹配
                sid_conditions = [
                    FieldCondition(key="scenario_id", match=MatchValue(value=sid))
                    for sid in filter_scenario_ids if sid
                ]
                title_conditions = [
                    FieldCondition(key="title", match=MatchValue(value=sid))
                    for sid in filter_scenario_ids if sid
                ]
                all_conditions = sid_conditions + title_conditions
                if all_conditions:
                    # 直接放 should 列表（新版 Qdrant 默认 OR 语义，至少命中 1 个）
                    must_clauses.extend(all_conditions)

            # 构造 filter
            query_filter = None
            if must_clauses:
                query_filter = Filter(must=must_clauses)

            # 搜索（放宽 limit，因为 entity filter 会大幅缩小范围）
            search_top_k = top_k * 3  # filter 后候选少，多拉一些
            resp = client.query_points(
                collection_name=coll,
                query=query_vec,
                limit=search_top_k,
                query_filter=query_filter,
                score_threshold=0.5,   # 先拉宽，sim ≥ 0.62 在 kg_verify 里过滤
            )
            for hit in resp.points:
                pl = hit.payload or {}
                results.append({
                    "coll": coll,
                    "pid": hit.id,
                    "score": float(hit.score),
                    "payload": pl,
                })
        except Exception as e:
            print(f"[warn] qdrant_filtered {coll}: {e}", file=sys.stderr)

    return results


def _build_l1_items_from_hits(hits):
    """把 Qdrant hits 构造成 recall 标准的 item 列表。"""
    items = []
    for hit in hits:
        pl = hit["payload"]
        text = pl.get("summary") or pl.get("text") or ""
        if not text:
            continue
        items.append({
            "summary": text,
            "relation": pl.get("memory_type", ""),
            "score": hit["score"],
            "source": "vec",
            "collection": hit["coll"],
            "_qdrant_pid": hit["pid"],
            "_point_type": pl.get("_point_type", ""),
            "parent_summary": pl.get("parent_summary") or "",
            "importance": pl.get("importance", 0.5),
            "ts": pl.get("ts", ""),
            "tags": pl.get("tags") or [],
            "entities": _entities_from_payload(pl),
            "scenario_id": pl.get("scenario_id") or pl.get("title") or "",
            "event_time": pl.get("event_time") or {},
            "valid_time": pl.get("valid_time") or {},
            "recorded_at": pl.get("recorded_at") or "",
            "source_time": pl.get("source_time") or "",
        })
    return items


# ============================================================
# Q2. entity overlap 重排
# ============================================================

def _rerank_by_entity_overlap(items, filter_entities):
    """用 entity overlap 重排 items。

    entity overlap 反映"这条 L1 记忆和 L3/L2 上层上下文的关联度"。
    关联度高的记忆优先展示，即使 sim 分稍低。

    综合分 = sim * (1 - w) + entity_overlap * w
    w = ENTITY_OVERLAP_WEIGHT（默认 0.3）
    """
    if not items:
        return items
    w = ENTITY_OVERLAP_WEIGHT
    fset = set(e.strip().lower() for e in filter_entities if e and len(e.strip()) >= 2)

    for it in items:
        sim = float(it.get("score", 0))
        overlap = _entity_overlap(fset, it.get("entities") or [])
        # 综合分：sim 为主，entity overlap 加持
        it["entity_overlap"] = round(overlap, 3)
        it["combined_score"] = round(sim * (1 - w) + overlap * w, 4)
    return items


# ============================================================
# Q3. L3/L2 辅助召回 → 提取 entities/scenario_ids
# ============================================================

def _collect_context_from_layer(query, collection, min_score, top_k):
    """从指定 collection 召回，返回 (entities, scenario_ids, hit_records)。"""
    try:
        client = _qdrant_client()
        vecs = embed(query)
        if not vecs:
            return [], [], []
        vec = vecs[0] if isinstance(vecs[0], list) else vecs
        resp = client.query_points(
            collection_name=collection,
            query=vec,
            limit=top_k,
            score_threshold=min_score,
        )
    except Exception as e:
        print(f"[warn] _collect_context {collection}: {e}", file=sys.stderr)
        return [], [], []

    entities = []
    scenario_ids = []
    hits = []

    for hit in resp.points:
        pl = hit.payload or {}
        score = float(hit.score)
        if score < min_score:
            continue

        # 提取 entities
        for e in _entities_from_payload(pl):
            if e and e not in entities:
                entities.append(e)

        # 提取 scenario_ids（仅 L2）
        if collection == L2_COLLECTION:
            for sid in _scenario_ids_from_payload(pl):
                if sid and sid not in scenario_ids:
                    scenario_ids.append(sid)

        hits.append({
            "summary": pl.get("summary") or "",
            "title": pl.get("title") or "",
            "score": score,
            "layer": "L3" if collection == L3_COLLECTION else "L2",
        })

    return entities, scenario_ids, hits


def _collect_graph_entities(query):
    """从 Neo4j 拉 query 相关实体，返回 entity 列表（用于 entity filter 增强）。"""
    try:
        from process_dream import neo4j_entity_search
        names = neo4j_entity_search(query, limit=5)
        return names or []
    except Exception:
        return []


def _graph_channel_with_sim(query, limit=5):
    """拉 Neo4j graph 通道，并附上 embedding sim（用于 PRF 门控）。"""
    try:
        from process_dream import neo4j_entity_search, neo4j_expand
        entity_names = neo4j_entity_search(query, limit=limit)
        if not entity_names:
            return []
        graph_items = neo4j_expand(entity_names)
        if not graph_items:
            return []

        # 批量算 embedding sim
        summaries = [g.get("summary", "") for g in graph_items]
        summaries = [s for s in summaries if s]
        if not summaries:
            return graph_items

        vectors = embed([query] + summaries)
        if not vectors or len(vectors) < 2:
            return graph_items

        qvec = vectors[0]
        for i, item in enumerate(graph_items):
            if i + 1 < len(vectors) and vectors[i + 1]:
                v = vectors[i + 1]
                dot = sum(a * b for a, b in zip(qvec, v))
                nq = sum(a * a for a in qvec) ** 0.5
                nm = sum(b * b for b in v) ** 0.5
                item["graph_sim"] = round(dot / (nq * nm + 1e-9), 4)
            else:
                item["graph_sim"] = 0.0
        return graph_items
    except Exception as e:
        print(f"[warn] graph_channel_with_sim: {e}", file=sys.stderr)
        return []


# ============================================================
# L0 独立 BM25（保持不变，不进主流程）
# ============================================================

_l0_index_lock = threading.Lock()


def _tokenize_l0(text: str):
    if not text:
        return []
    try:
        import jieba
        tokens = list(jieba.cut(text))
    except ImportError:
        tokens = text.split()
    stop = {"的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都",
            "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你",
            "会", "着", "没有", "看", "好", "自己", "这", "那", "他", "她",
            "它", "们", "吗", "吧", "啊", "呢", "哦", "嗯", "噢", "呀"}
    return [t for t in tokens if len(t) >= 2 and t not in stop]


def _load_or_build_l0_index():
    if L0_BM25_INDEX_PATH.exists():
        try:
            with open(L0_BM25_INDEX_PATH, "rb") as f:
                idx = pickle.load(f)
            if idx and idx.get("bm25"):
                return idx
        except Exception:
            pass
    with _l0_index_lock:
        if L0_BM25_INDEX_PATH.exists():
            try:
                with open(L0_BM25_INDEX_PATH, "rb") as f:
                    idx = pickle.load(f)
                if idx and idx.get("bm25"):
                    return idx
            except Exception:
                pass
        idx = _build_l0_index()
        try:
            with open(L0_BM25_INDEX_PATH, "wb") as f:
                pickle.dump(idx, f)
        except Exception as e:
            print(f"[warn] L0 index save failed: {e}", file=sys.stderr)
        return idx


def _build_l0_index():
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        return {"tokenized": [], "documents": [], "bm25": None}

    client = _qdrant_client()
    docs = []
    offset = None
    while True:
        try:
            res, offset = client.scroll(
                collection_name=L0_COLLECTION,
                limit=1000,
                with_payload=True,
                offset=offset,
            )
        except Exception as e:
            print(f"[warn] L0 scroll: {e}", file=sys.stderr)
            return {"tokenized": [], "documents": [], "bm25": None}
        for pt in res:
            pl = pt.payload or {}
            text = pl.get("summary") or ""
            if text:
                docs.append({"summary": text, "pid": str(pt.id), "payload": pl})
        if offset is None:
            break
    if not docs:
        return {"tokenized": [], "documents": [], "bm25": None}
    tokenized = [_tokenize_l0(d["summary"]) for d in docs]
    bm25 = BM25Okapi(tokenized) if any(tokenized) else None
    return {"tokenized": tokenized, "documents": docs, "bm25": bm25}


def _search_l0_bm25(query, top_k=5):
    try:
        idx = _load_or_build_l0_index()
    except Exception as e:
        return []
    if not idx or not idx.get("bm25"):
        return []
    q_tokens = _tokenize_l0(query)
    if not q_tokens:
        return []
    bm25 = idx["bm25"]
    docs = idx["documents"]
    try:
        top_docs = bm25.get_top_n(q_tokens, docs, n=top_k)
    except Exception:
        return []
    out = []
    for rank, d in enumerate(top_docs):
        pl = d.get("payload") or {}
        out.append({
            "summary": d["summary"],
            "title": "",
            "memory_type": "l0_conversation",
            "layer": "L0",
            "score": round(max(0.1, 1.0 - rank * 0.2), 3),
            "importance": 0.5,
            "tags": [],
            "event_time": {},
            "recorded_at": pl.get("ts", ""),
            "source": "bm25_l0",
        })
    return out


# ============================================================
# 主召回函数：recall_4layer_v2
# ============================================================

def recall_4layer(query, top_k=5, layers=None):
    """4 层召回 v2：

    架构：
      L3 (persona) → 提取高置信实体 → entity filter
      L2 (scenario) → 提取场景+实体 → entity/scenario filter
      L1 (atom) → 带 filter 的向量召回 → entity overlap 重排 → kg_verify
      L0 (bm25) → 独立索引（不进主输出）

    与 v1 的区别：
      - 不再把 L3/L2 的词塞进 query
      - 用 L3/L2 的 entities/scenario_id 当 Qdrant filter
      - 用 entity overlap 做重排
      - PRF 只在 graph sim ≥ 0.62 时触发

    Returns:
      {
        "query": str,
        "persona": [...],    # L3 召回明细
        "scenario": [...],   # L2 召回明细
        "atom": [...],       # L1 最终结果（已 entity-overlap 重排 + kg_verify）
        "memories": [...],   # 最终输出的 summary 列表
        "context": {         # 上下文信息（供调试用）
          "filter_entities": [...],
          "filter_scenario_ids": [...],
          "graph_prf_triggered": bool,
          "entity_overlap_avg": float,
        }
      }
    """
    # 进入召回前，主动拉起依赖的 embed/reranker 服务（idle 超时后进程可能已 dead）
    try:
        from service_lifecycle import ensure_service_up
        ensure_service_up(8765, max_wait=90)  # embed
        ensure_service_up(8877, max_wait=90)  # reranker
    except Exception as e:
        print(f"[warn] ensure service up failed at recall entry: {e}", file=sys.stderr)
    if layers is None:
        layers = ["L3", "L2", "L1"]

    persona, scenario = [], []
    filter_entities = []
    filter_scenario_ids = []

    # ── Step 1: L3 召回（高置信），提取 entities ─────────────────
    if "L3" in layers:
        entities, sids, hits = _collect_context_from_layer(
            query, L3_COLLECTION, min_score=L3_MIN_SCORE, top_k=top_k
        )
        for h in hits:
            h["layer"] = "L3"
            persona.append(h)
        for e in entities:
            if e and e not in filter_entities:
                filter_entities.append(e)

    # ── Step 2: L2 召回（中高置信），提取 entities + scenario_ids ──
    if "L2" in layers:
        entities, sids, hits = _collect_context_from_layer(
            query, L2_COLLECTION, min_score=L2_MIN_SCORE, top_k=top_k
        )
        for h in hits:
            h["layer"] = "L2"
            scenario.append(h)
        for e in entities:
            if e and e not in filter_entities:
                filter_entities.append(e)
        for sid in sids:
            if sid and sid not in filter_scenario_ids:
                filter_scenario_ids.append(sid)

    # ── Step 3: graph 通道 → 检查是否触发 PRF ───────────────────
    graph_prf_triggered = False
    if "L1" in layers and filter_entities:
        # 先用 graph 通道验证上下文实体是否真实关联
        graph_items = _graph_channel_with_sim(query, limit=5)
        if graph_items:
            # PRF 触发条件：至少 1 个 graph 结果 sim ≥ 0.62
            max_graph_sim = max((g.get("graph_sim", 0) for g in graph_items), default=0)
            if max_graph_sim >= PRF_MIN_GRAPH_SIM:
                # 补充 filter_entities（从 graph 结果里再拿一些高置信实体）
                for g in graph_items:
                    if g.get("graph_sim", 0) >= PRF_MIN_GRAPH_SIM:
                        for r in g.get("raw_triples", []) or []:
                            subj = (r.get("subj") or "").strip()
                            obj = (r.get("obj") or "").strip()
                            if len(subj) >= 2 and subj not in filter_entities:
                                filter_entities.append(subj)
                            if len(obj) >= 2 and obj not in filter_entities:
                                filter_entities.append(obj)
                graph_prf_triggered = True

    # ── Step 4: L1 主召回 ────────────────────────────────────────
    atom = []
    if "L1" in layers:
        try:
            vecs = embed(query)
            if not vecs:
                raise ValueError("embed failed")
            vec = vecs[0] if isinstance(vecs[0], list) else vecs
        except Exception as e:
            print(f"[warn] L1 embed failed: {e}", file=sys.stderr)
            vecs = []

        if vecs:
            # 统一走纯向量搜索（entity filter 改作 re-ranking 信号，不硬过滤）
            if filter_entities or filter_scenario_ids:
                # Path A: 拉更多候选（top_k * 4），再用 entity overlap 重排
                hits = _qdrant_search_filtered(
                    vec, RecallConfig.COLLECTIONS, top_k=top_k * 4,
                    filter_ents=filter_entities,
                    filter_scenario_ids=filter_scenario_ids,
                )
                items = _build_l1_items_from_hits(hits)
            else:
                # Path B: 无 filter → 直接用 process_dream.recall
                items = []

            if not items:
                # 兜底：用 process_dream.recall（它内部有完整的 vec+graph+bm25 融合）
                try:
                    from process_dream import recall as l1_recall
                    result = l1_recall(query, top_k=top_k * 2)
                    raw_memories = result.get("memories", [])
                    for m in raw_memories:
                        if isinstance(m, dict):
                            items.append(m)
                        elif isinstance(m, str):
                            items.append({"summary": m, "score": 0.0, "relation": ""})
                except Exception as e:
                    print(f"[warn] L1 fallback recall failed: {e}", file=sys.stderr)

            # entity overlap 重排（无论有没有 filter_entities 都做）
            if filter_entities and items:
                items = _rerank_by_entity_overlap(items, filter_entities)
                items.sort(key=lambda x: -x.get("combined_score", x.get("score", 0)))

            atom = items[:top_k * 2]

    # ── Step 5: Reranker 精排（Qwen3-Reranker-0.6B，P(yes) ≥ 0.62 才保留）───
    if atom:
        summaries = [m.get("summary", "") for m in atom]

        # 调 reranker HTTP 服务（官方 yes/no 打分）
        reranked = _rerank_via_http(query, summaries, top_k=len(summaries))
        rerank_map = {idx: score for idx, score in reranked}

        # 把 reranker 分数注入 atom
        for i, m in enumerate(atom):
            m["rerank_score"] = rerank_map.get(i, 0.0)

        # P(yes) < 0.62 的结果丢弃（噪声过滤，0.62 = embedding sim 水位等效）
        before = len(atom)
        atom = [m for m in atom if m.get("rerank_score", 0) >= SIM_WATERMARK]
        dropped = before - len(atom)

        # 综合 rerank_score + entity_overlap 排序
        if atom:
            w = ENTITY_OVERLAP_WEIGHT
            for m in atom:
                overlap = m.get("entity_overlap", 0)
                rr_score = m.get("rerank_score", 0)
                m["final_score"] = round(rr_score * (1 - w) + overlap * w, 4)
            atom.sort(key=lambda x: -x.get("final_score", 0))
            atom = atom[:top_k]

    all_memories = [_format_memory_with_time(m) for m in atom if m.get("summary")]
    overlap_avg = (
        sum(m.get("entity_overlap", 0) for m in atom) / max(len(atom), 1)
    )

    return {
        "query": query,
        "layers": layers,
        "persona": persona,
        "scenario": scenario,
        "atom": atom,
        "raw": [],
        "memories": all_memories,
        "context": {
            "filter_entities": filter_entities,
            "filter_scenario_ids": filter_scenario_ids,
            "graph_prf_triggered": graph_prf_triggered,
            "entity_overlap_avg": round(overlap_avg, 3),
        },
    }


# ============================================================
# Hook 入口（兼容 process_dream 的 hook 调用格式）
# ============================================================

def recall_for_hook(query, top_k=8, rrf_k=None):
    """Hook 调用的 recall：门控 → recall_4layer_v2 → 只输出 L1。"""
    from process_dream import should_skip_recall, log_hook_event

    skip, reason = should_skip_recall(query)
    dedup_key = query[:50]
    now = time.monotonic()
    if hasattr(recall_for_hook, "_last_call"):
        last_q, last_t = recall_for_hook._last_call
        if last_q == dedup_key and now - last_t < 10:
            return {"skipped": True, "reason": "dedup", "memories": [], "channels": {}, "query": query}
    recall_for_hook._last_call = (dedup_key, now)

    if skip:
        return {"skipped": True, "reason": reason, "memories": [], "channels": {}, "query": query}

    result = recall_4layer(query, top_k=top_k)
    atom = result.get("atom", [])

    memories = [_format_memory_with_time(m) for m in atom[:5]]
    memories = [m for m in memories if m]

    ctx = result.get("context", {})
    channels = {
        "vec": 0,
        "bm25": 0,
        "bm25_kw_filtered": 0,
        "graph": 0,
        "prf_kg_summaries": 0,
        "rrf_k": rrf_k or 60,
        "aux_persona": len(result.get("persona", [])),
        "aux_scenario": len(result.get("scenario", [])),
        "l1_atom_count": len(atom),
        "graph_prf_triggered": ctx.get("graph_prf_triggered", False),
        "entity_overlap_avg": ctx.get("entity_overlap_avg", 0),
        "filter_entities": ctx.get("filter_entities", [])[:5],
    }

    return {
        "skipped": False,
        "reason": "",
        "memories": memories,
        "channels": channels,
        "query": query,
    }


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    p_recall = sub.add_parser("recall")
    p_recall.add_argument("--query", required=True)
    p_recall.add_argument("--top-k", type=int, default=5)
    p_recall.add_argument("--hook", action="store_true")
    args = parser.parse_args()

    if args.cmd == "recall":
        if args.hook:
            result = recall_for_hook(args.query, top_k=args.top_k)
            print(json.dumps(result, ensure_ascii=False))
        else:
            result = recall_4layer(args.query, top_k=args.top_k)
            print(json.dumps(
                {k: v for k, v in result.items() if k != "memories"},
                ensure_ascii=False, indent=2,
            ))
