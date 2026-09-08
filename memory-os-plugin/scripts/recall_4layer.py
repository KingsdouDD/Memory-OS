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

# ── numpy 兼容补丁（老库依赖 np.bool_ / np.int8 等 numpy 1.x 别名）──────────
# 必须放在所有 import 之前，让 qdrant_client / neo4j 能在 numpy 2.0+ 下正常加载
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import _numpy_compat  # noqa: F401
except Exception:
    pass

import json
import pickle
import threading
import time
from pathlib import Path

from process_dream import embed, _qdrant_client
from recall_fusion import fusion_post_fuse, fusion_boost_graph_hits, kg_verify_v2, association_expand
from recall_config import RecallConfig

# ── Reranker 服务（Qwen3-Reranker-0.6B）──────────────────────────
RERANKER_URL = "http://127.0.0.1:8877/rerank"


def _rerank_via_http(query, candidates, top_k=5, timeout=10):
    """调 reranker HTTP 服务做精排，返回 (index, score) 列表。

    关键设计：
      - 不再重复调 ensure_service_up：服务就绪由 recall_4layer 入口统一负责
      - timeout 从 30s 降到 10s：模型加载卡死时快速失败，recall 走降级路径
      - 失败返回 []：让主流程能继续出结果，绝不因 reranker 卡死整个 recall
    """
    if not candidates:
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
        # L3/L2 只用作召回辅助（提取 entities/scenario_ids 给 L1 filter），
        # 不进最终输出。L1 atom 才是用户要的“原子记忆”。
        # 为保持调试可见性，L3/L2 明细仍保留在响应里但加 _aux 前缀，提醒上层不要注入。
        "_aux_persona": [...],    # L3 召回明细（仅调试用，不注入提示词）
        "_aux_scenario": [...],   # L2 召回明细（仅调试用，不注入提示词）
        "atom": [...],       # L1 最终结果（已 entity-overlap 重排 + kg_verify）
        "memories": [...],   # 最终输出的 summary 列表（仅含 L1）
        "context": {         # 上下文信息（供调试用）
          "filter_entities": [...],
          "filter_scenario_ids": [...],
          "graph_prf_triggered": bool,
          "entity_overlap_avg": float,
        }
      }
    """
    # 进入召回前，主动拉起依赖的 embed/reranker 服务（idle 超时后进程可能已 dead）
    # max_wait 从 90 降到 30
    # 原因：端口起来一般 5-10s，模型加载是服务内部的事，等再久也是服务内部 race
    # 真等不了时直接返回失败让上层走降级路径，不要让用户干等
    try:
        from service_lifecycle import ensure_service_up
        ensure_service_up(8765, max_wait=30)  # embed
        ensure_service_up(8877, max_wait=30)  # reranker
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

    # ── Step 2.5: 从 L3/L2 hits 的 summary 里提取种子实体 ────────
    # 当 filter_entities 仍为空时，从已有 hits 的 summary 文本提取名词作为种子
    if not filter_entities:
        import jieba
        _STOP = {
            "的", "了", "在", "是", "有", "和", "就", "不", "都",
            "一", "上", "也", "很", "到", "去", "会", "着", "好",
            "这", "那", "吗", "吧", "啊", "呢", "哦", "嗯", "呀",
            "是", "一个", "自己", "没有", "什么", "怎么", "可以",
        }
        all_hit_summaries = (
            [h.get("summary", "") for h in persona if h.get("summary")]
            + [h.get("title", "") for h in scenario if h.get("title")]
            + [h.get("summary", "") for h in scenario if h.get("summary")]
        )
        for text in all_hit_summaries:
            for w in jieba.cut(text):
                word = w.strip()
                if not word or len(word) < 2 or word in _STOP:
                    continue
                # 接受常见实体词性：人名(nrt)、地名(ns)、机构名(nt)、普通名词(n)
                # jieba.cut 不返回词性，改用 posseg
                pass
        # 用 posseg 提取
        try:
            import jieba.posseg as pseg
            for text in all_hit_summaries:
                for w in pseg.cut(text):
                    word = w.word.strip()
                    if not word or len(word) < 2 or word in _STOP:
                        continue
                    # 接受：人名(nrt/nr)、地名(ns)、机构名(nt)、名词(n/m)
                    # 以及含语义词的复合词
                    if any(w.flag.startswith(p) for p in ("n", "m")) or w.flag in ("x", "g"):
                        if word not in filter_entities:
                            filter_entities.append(word)
        except Exception:
            pass

    # ── Step 3: graph 通道 ─ 知识图谱单跳直接召回 + 实体抽取 ─────────────
    # 不做 PRF 多跳扩散，只走 neo4j_expand depth=1
    graph_items = []
    graph_prf_triggered = False
    graph_entity_names = []
    if "L1" in layers:
        graph_items = _graph_channel_with_sim(query, limit=5)
        if graph_items:
            # 从 graph 结果中提取实体名，用于后续 fusion_boost
            for g in graph_items:
                for r in g.get("raw_triples", []) or []:
                    subj = (r.get("subj") or "").strip()
                    obj = (r.get("obj") or "").strip()
                    if len(subj) >= 2 and subj not in graph_entity_names:
                        graph_entity_names.append(subj)
                    if len(obj) >= 2 and obj not in graph_entity_names:
                        graph_entity_names.append(obj)
            graph_prf_triggered = True

    # ── Step 4: 向量召回（无条件，单一路径，top 20，水位 0.62）────────────
    # 设计意图：
    #   - 不再有"Path A / Path B"分支（这是我之前臆想的）
    #   - 统一走纯向量召回，entity filter 不再是硬过滤（删）
    #   - top_k = 20（在 0.62 水位基础上保证候选充足）
    #   - 召回范围 = RecallConfig.COLLECTIONS（保持原有 L1 collection 集合）
    atom = []
    vec = None
    if "L1" in layers:
        try:
            vecs = embed(query)
            if not vecs:
                raise ValueError("embed failed")
            vec = vecs[0] if isinstance(vecs[0], list) else vecs
        except Exception as e:
            print(f"[warn] L1 embed failed: {e}", file=sys.stderr)
            vecs = []

        if vecs and vec is not None:
            # 统一走纯向量召回，无 entity 硬过滤
            try:
                from process_dream import _qdrant_client
                client = _qdrant_client()
                hits_raw = []
                for coll in RecallConfig.COLLECTIONS:
                    try:
                        resp = client.query_points(
                            collection_name=coll,
                            query=vec,
                            limit=20,                        # top 20
                            score_threshold=0.62,            # 水位 0.62 不动
                        )
                        for hit in resp.points:
                            hits_raw.append({
                                "coll": coll,
                                "pid": hit.id,
                                "score": float(hit.score),
                                "payload": hit.payload or {},
                            })
                    except Exception as e:
                        print(f"[warn] vec recall {coll}: {e}", file=sys.stderr)
                items = _build_l1_items_from_hits(hits_raw)
            except Exception as e:
                print(f"[warn] vec recall failed: {e}", file=sys.stderr)
                items = []

            # entity overlap 重排保留（entity overlap 作为加权信号，不是硬过滤）
            if filter_entities and items:
                items = _rerank_by_entity_overlap(items, filter_entities)
                items.sort(key=lambda x: -x.get("combined_score", x.get("score", 0)))

            atom = items[:20]   # top 20

    # ── Step 5: 已删除（与 Step 4 entity overlap 重排重复）──────────
    # 保留 Step 4 里的 entity overlap 重排一次即可

    # ── Step 6: Association Expansion（联想记忆）────────────────────
    # 如果 L3/L2 有召回 entities → 用 filter_entities 启动联想
    # 如果没有 → 从 query 文本用 jieba 提取名词实体作为种子
    assoc_candidates = []
    assoc_triggered = False

    # Fallback：当 L3/L2 没有召回实体时，从 query 本身提取候选实体
    # 策略：jieba 分词（名词/动词/复合词）+ query 全文本都当种子
    q_filter_entities = list(filter_entities)
    if RecallConfig.ASSOC_ENABLED and not q_filter_entities:
        try:
            import jieba.posseg as pseg
            # 只过滤纯语法词/语气词，不过滤内容词（工作/想/找 都要留）
            _STOP = {
                "的", "了", "在", "是", "有", "和", "就", "不", "都",
                "一", "上", "也", "很", "到", "去", "会", "着", "好",
                "这", "那", "吗", "吧", "啊", "呢", "哦", "嗯", "呀",
            }
            for w in pseg.cut(query):
                word = w.word.strip()
                if not word or len(word) < 2 or word in _STOP:
                    continue
                # 接受：名词(n*/nr/ns/nt/nz)、动词(v*/vn)、人名(nr)、地名(ns)
                # 也接受 x(字母数字)、g(其他)：捕捉"职业规划"这类复合词
                if any(w.flag.startswith(p) for p in ("n", "v", "x", "g")):
                    if word not in q_filter_entities:
                        q_filter_entities.append(word)
        except Exception:
            pass

        # 如果提取出的实体太少（< 2个），直接用 query 全文本作为种子
        if len(q_filter_entities) < 2:
            q_filter_entities.append(query.strip())

    if RecallConfig.ASSOC_ENABLED and q_filter_entities:
        # 收集 seed summaries（persona + scenario）
        seed_summaries = (
            [p.get("summary", "") for p in persona if p.get("summary")]
            + [s.get("summary", "") for s in scenario if s.get("summary")]
            + [s.get("title", "") for s in scenario if s.get("title")]
        )
        assoc_candidates = association_expand(
            query=query,
            seed_entities=filter_entities,
            seed_summaries=seed_summaries,
            config=RecallConfig,
            embed_fn=embed,
        )
        assoc_triggered = len(assoc_candidates) > 0

        if assoc_triggered:
            # 联想候选先不做 Reranker，等合并后统一做一次
            # Pre-filter：按 assoc_score 粗排，保留 top_k×3
            assoc_candidates.sort(key=lambda x: -x.get("assoc_score", 0))
            assoc_candidates = assoc_candidates[:top_k * 3]
            # 标记这些是联想来的记忆（先不做 final_score，等统一 Reranker）
            for c in assoc_candidates:
                c["_is_assoc"] = True

    # ── 合并 seed atom + association candidates ────────────────────────
    # 去重：用完整 summary 精确匹配
    seed_summaries_full = {m.get("summary", "") for m in atom}

    # 统一格式：给 atom 的记忆补 recall_reason
    for m in atom:
        if "recall_reason" not in m:
            m["recall_reason"] = "直接匹配"

    merged_atom = list(atom)
    # Neo4j 联想扩散的候选不再进 merged_atom
    # 原因：联想扩散会从实体跳到与 query 语义无关的其他实体
    #       这些候选跟用户实际想问的东西不相关，进了最终输出会污染召回
    # 保留功能： Neo4j 还能给直接命中项加分（上面 Step 3 PRF + Step 4 entity overlap 重排）
    # 所以下面这段不再把 assoc_candidates 接入 merged_atom

    # ── Step 3.5: 知识图谱直接召回的候选项入池 ───────────────────
    # 1. 把 graph_items 转换为统一格式，接入 merged_atom
    # 2. 调用 fusion_boost_graph_hits 对接 graph_entity_names 的项加分
    # 3. 调用 fusion_post_fuse 做最终融合（去重+评分）
    graph_items_normalized = []
    for g in graph_items or []:
        # graph_item 的 summary / entities / graph_sim 都可复用
        g_norm = dict(g)
        g_norm.setdefault("source", "graph")
        g_norm.setdefault("recall_reason", "图谱直接召回")
        # fusion_boost_graph_hits 靠 _channels 判断 graph hit，这里必须补上
        g_norm["_channels"] = ["graph"]
        # graph 召回顾量赋值 sort_key 供 fusion_post_fuse / fusion_boost 用
        if "sort_key" not in g_norm:
            g_norm["sort_key"] = float(g_norm.get("graph_sim", 0.7))
        g_norm["graph_sim"] = g.get("graph_sim", 0)
        graph_items_normalized.append(g_norm)

    if graph_items_normalized:
        # 给合并后的池子加 graph 加分
        try:
            merged_atom = fusion_boost_graph_hits(
                merged_atom + graph_items_normalized,
                graph_entity_names=graph_entity_names,
                boost=1.3,
            )
        except Exception as e:
            print(f"[warn] fusion_boost_graph_hits failed: {e}", file=sys.stderr)

        # 最终融合（去重 + entity_overlap 加权 + 综合打分）
        try:
            merged_atom = fusion_post_fuse(merged_atom)
        except Exception as e:
            print(f"[warn] fusion_post_fuse failed: {e}", file=sys.stderr)

    # ── 统一 Reranker（一次调用，精排全部候选）──────────────────────
    # retrieval_top_k: 合并后进入 Reranker 的候选数量
    retrieval_top_k = len(merged_atom)
    reranker_input_k = retrieval_top_k  # 输入数量（input k = output k）
    reranker_output_k = retrieval_top_k

    reranker_call_count = 0
    if merged_atom:
        summaries_for_rerank = [m.get("summary", "") for m in merged_atom]
        reranked = _rerank_via_http(query, summaries_for_rerank, top_k=reranker_output_k)
        reranker_call_count = 1
        rerank_map = {idx: score for idx, score in reranked}

        for i, m in enumerate(merged_atom):
            rr = rerank_map.get(i, 0.0)
            m["rerank_score"] = rr
            # 联想记忆不再进 merged_atom
            # 所以只走 entity_overlap 分支
            m["final_score"] = round(
                rr * 0.6 + m.get("entity_overlap", 0) * 0.4,
                4,
            )

    merged_atom.sort(key=lambda x: -x.get("final_score", x.get("score", 0)))
    merged_atom = [m for m in merged_atom if (m.get("rerank_score") or 0) >= 0.55]
    merged_atom = merged_atom[:top_k]

    all_memories = [_format_memory_with_time(m) for m in merged_atom if m.get("summary")]
    overlap_avg = (
        sum(m.get("entity_overlap", 0) for m in merged_atom) / max(len(merged_atom), 1)
    )

    all_memories = [_format_memory_with_time(m) for m in merged_atom if m.get("summary")]
    overlap_avg = (
        sum(m.get("entity_overlap", 0) for m in merged_atom) / max(len(merged_atom), 1)
    )

    return {
        "query": query,
        "layers": layers,
        # L3/L2 只做 filter，不进最终输出
        "_aux_persona": persona,
        "_aux_scenario": scenario,
        "atom": merged_atom,
        "assoc_candidates": assoc_candidates if assoc_triggered else [],
        "raw": [],
        "memories": all_memories,
        "context": {
            "filter_entities": filter_entities,
            "filter_scenario_ids": filter_scenario_ids,
            "graph_prf_triggered": graph_prf_triggered,
            "assoc_triggered": assoc_triggered,
            "assoc_candidate_count": len(assoc_candidates),
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
    assoc_cands = result.get("assoc_candidates") or []
    # 统计联想来源的记忆条数
    assoc_mem_count = sum(1 for m in atom if m.get("_is_assoc"))
    channels = {
        "vec": 0,
        "bm25": 0,
        "bm25_kw_filtered": 0,
        "graph": 0,
        "prf_kg_summaries": 0,
        "rrf_k": rrf_k or 60,
        "aux_persona": len(result.get("_aux_persona", []) or result.get("persona", [])),
        "aux_scenario": len(result.get("_aux_scenario", []) or result.get("scenario", [])),
        "l1_atom_count": len(atom),
        "assoc_count": assoc_mem_count,
        "assoc_candidates": ctx.get("assoc_candidate_count", 0),
        "graph_prf_triggered": ctx.get("graph_prf_triggered", False),
        "assoc_triggered": ctx.get("assoc_triggered", False),
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
            # CLI 模式要保留 memories 字段
            # 原因：OpenClaw 工具读 payload.memories 判断是否空
            # 原代码过滤掉了，导致有数据时也返回 empty:true
            result = recall_4layer(args.query, top_k=args.top_k)
            print(json.dumps(result, ensure_ascii=False, indent=2))
