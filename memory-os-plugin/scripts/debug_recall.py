"""
Debug 召回脚本：分阶段打印每个通道召回的数据。
使用：python3 debug_recall.py "query"
"""

import sys
import os

# 加 scripts/ 到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from recall_4layer import (
    embed, _qdrant_client, _bm25_search, _entities_from_payload,
    _compute_entity_overlap, _build_l1_items_from_hits,
    _fuse_three_channels, neo4j_entity_search,
    VEC_TOP_K, BM25_TOP_K, SIM_WATERMARK, RecallConfig,
)
from recall_config import RecallConfig as RC


def debug_recall(query: str):
    print("=" * 70)
    print(f"Query: {query}")
    print("=" * 70)

    # === Step 1: 向量召回 ===
    print("\n" + "─" * 35)
    print("STEP 1: 向量召回 (ANN)")
    print("─" * 35)
    vec_items = []
    try:
        vecs = embed(query)
        if not vecs:
            raise ValueError("embed failed")
        vec = vecs[0] if isinstance(vecs[0], list) else vecs
        client = _qdrant_client()
        hits_raw = []
        for coll in RC.COLLECTIONS:
            try:
                resp = client.query_points(
                    collection_name=coll,
                    query=vec,
                    limit=VEC_TOP_K,
                    score_threshold=SIM_WATERMARK,
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
        vec_items = _build_l1_items_from_hits(hits_raw)
    except Exception as e:
        print(f"[err] vec recall failed: {e}", file=sys.stderr)

    print(f"\n共召回 {len(vec_items)} 条 (top {VEC_TOP_K}, threshold={SIM_WATERMARK}):")
    for i, it in enumerate(vec_items, 1):
        print(f"  {i}. [{it.get('collection','?')}] score={it.get('score',0):.3f}  {it.get('summary','')[:80]}")

    # === Step 2: BM25 召回 ===
    print("\n" + "─" * 35)
    print("STEP 2: BM25 召回")
    print("─" * 35)
    bm25_items = []
    try:
        bm25_raw = _bm25_search(query, top_k=BM25_TOP_K)
        for r in bm25_raw:
            if r.get("norm_score", 0) < SIM_WATERMARK:
                continue
            pl = r.get("payload") or {}
            bm25_items.append({
                "summary": r.get("summary", ""),
                "relation": pl.get("memory_type", ""),
                "score": float(r.get("norm_score", 0)),
                "norm_score": float(r.get("norm_score", 0)),
                "source": "bm25",
                "collection": r.get("collection", ""),
                "importance": pl.get("importance", 0.5),
                "entities": _entities_from_payload(pl),
            })
    except Exception as e:
        print(f"[err] bm25 recall failed: {e}", file=sys.stderr)

    print(f"\n共召回 {len(bm25_items)} 条 (top {BM25_TOP_K}, threshold={SIM_WATERMARK}):")
    for i, it in enumerate(bm25_items, 1):
        print(f"  {i}. [{it.get('collection','?')}] score={it.get('norm_score',0):.3f}  {it.get('summary','')[:80]}")

    # === Step 3: Neo4j 实体召回 ===
    print("\n" + "─" * 35)
    print("STEP 3: Neo4j 实体检索")
    print("─" * 35)
    graph_entity_names = []
    try:
        entity_names = neo4j_entity_search(query, limit=5)
        if entity_names:
            graph_entity_names = [n for n in entity_names if n]
    except Exception as e:
        print(f"[err] neo4j entity search failed: {e}", file=sys.stderr)
    print(f"提取到 {len(graph_entity_names)} 个实体: {graph_entity_names[:10]}")

    # === Step 4: 三通道融合 ===
    print("\n" + "─" * 35)
    print("STEP 4: 三通道融合 (vec ∩ bm25 + Neo4j 软加权)")
    print("─" * 35)
    fused = _fuse_three_channels(vec_items, bm25_items, graph_entity_names)
    print(f"\n融合后 {len(fused)} 条:")
    for i, it in enumerate(fused, 1):
        channels = it.get("_channels", [])
        print(f"  {i}. channels={channels} sort_key={it.get('sort_key',0):.3f}  {it.get('summary','')[:80]}")

    print("\n" + "=" * 70)
    print("Done.")
    print("=" * 70)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 debug_recall.py \"your query here\"")
        sys.exit(1)
    query = " ".join(sys.argv[1:])
    debug_recall(query)
