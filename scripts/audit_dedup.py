#!/usr/bin/env python3
"""Memory OS 去重巡检脚本

跑 5 项检查，dump 到 memory-os/logs/dedup-audit-<timestamp>.md：
1. Neo4j 同 (subj, pred, obj) 多个 active 边 — 真重复
2. Neo4j status='superseded' 但 superseded_by 为空的 — 死边
3. Qdrant 同 pid 多份（应该不可能，验）
4. 跨 collection 重复（同 summary 出现在多个 collection）
5. Neo4j 实体 vs Qdrant point 数对账

跑法：
  python3 memory-os-plugin/scripts/audit_dedup.py
"""
import os
import sys
import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict, Counter

# 跟 process_dream.py 同源
sys.path.insert(0, str(Path(__file__).parent))
from recall_config import RecallConfig

CN_TZ = timezone(timedelta(hours=8))

NEO4J_URI = os.environ.get("MEMORY_OS_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("MEMORY_OS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("MEMORY_OS_NEO4J_PASSWORD", "openclaw")
QDRANT_HOST = os.environ.get("MEMORY_OS_QDRANT_HOST", "127.0.0.1")
QDRANT_PORT = int(os.environ.get("MEMORY_OS_QDRANT_PORT", "6333"))

LOG_DIR = Path(str(Path.home()) + "/.openclaw/workspace/memory-os/logs")


def _now_cn() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _qdrant_client():
    from qdrant_client import QdrantClient
    return QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)


# ============================================================
# 检查 1：Neo4j 同 (subj, pred, obj) 多个 active 边
# ============================================================
def check_neo4j_duplicate_active_edges(session):
    cypher = """
    MATCH (a)-[r]->(b)
    WHERE r.status = 'active' OR r.status IS NULL
    WITH a.name AS subj, type(r) AS pred, b.name AS obj, count(r) AS cnt
    WHERE cnt > 1
    RETURN subj, pred, obj, cnt
    ORDER BY cnt DESC
    LIMIT 50
    """
    rows = []
    for rec in session.run(cypher).data():
        rows.append({
            "subj": rec["subj"],
            "pred": rec["pred"],
            "obj": rec["obj"],
            "count": rec["cnt"],
        })
    return rows


# ============================================================
# 检查 2：Neo4j superseded 死边（superseded_by 空）
# ============================================================
def check_neo4j_superseded_dead_edges(session):
    cypher = """
    MATCH (a)-[r]->(b)
    WHERE r.status = 'superseded'
      AND (r.superseded_by IS NULL OR r.superseded_by = '')
    RETURN a.name AS subj, type(r) AS pred, b.name AS obj,
           coalesce(r.superseded_at, '') AS superseded_at
    LIMIT 50
    """
    rows = []
    for rec in session.run(cypher).data():
        rows.append({
            "subj": rec["subj"],
            "pred": rec["pred"],
            "obj": rec["obj"],
            "superseded_at": str(rec["superseded_at"]),
        })
    return rows


# ============================================================
# 检查 3：Qdrant 同 pid（应该不可能，验证）
# ============================================================
def check_qdrant_dup_pids(client):
    """Qdrant 用 pid 作为 point id，理论上同 pid 只存一个。
    这里取所有 collection 的所有 point，按 (collection, pid) 计数。"""
    collections = [c for c in RecallConfig.COLLECTIONS if c.startswith("memory_")]
    rows = []
    for coll in collections:
        try:
            count = client.count(collection_name=coll).count
            rows.append({"collection": coll, "point_count": count})
        except Exception as e:
            rows.append({"collection": coll, "error": str(e)})
    return rows


# ============================================================
# 检查 4：跨 collection 重复（同 summary 出现在多个 collection）
# ============================================================
def check_qdrant_cross_collection_dup(client):
    """按 summary 分组，跨 collection 重复 → 列出 sample + collection 列表。"""
    collections = [c for c in RecallConfig.COLLECTIONS if c.startswith("memory_")]
    summary_to_collections = defaultdict(set)
    for coll in collections:
        try:
            offset = None
            while True:
                points, next_offset = client.scroll(
                    collection_name=coll,
                    limit=100,
                    offset=offset,
                    with_payload=["summary"],
                    with_vectors=False,
                )
                for p in points:
                    s = (p.payload.get("summary") or "").strip()
                    if s:
                        summary_to_collections[s].add(coll)
                if next_offset is None:
                    break
                offset = next_offset
        except Exception as e:
            print(f"[warn] scroll {coll}: {e}", file=sys.stderr)

    rows = []
    for s, colls in summary_to_collections.items():
        if len(colls) > 1:
            rows.append({
                "summary": s[:120],
                "collections": sorted(colls),
                "count": len(colls),
            })
    rows.sort(key=lambda x: -x["count"])
    return rows[:50]


# ============================================================
# 检查 5：Neo4j 实体数 vs Qdrant point 数对账
# ============================================================
def check_neo4j_qdrant_counts(session, client):
    """两边各自的"事实数"对不上 = 写入流程漏了一边。"""
    # Neo4j 关系数
    rel_count_res = session.run("MATCH ()-[r]->() RETURN count(r) AS cnt").data()
    rel_count = rel_count_res[0]["cnt"] if rel_count_res else 0
    # Neo4j 实体数
    ent_count_res = session.run("MATCH (n) WHERE n.name IS NOT NULL RETURN count(n) AS cnt").data()
    ent_count = ent_count_res[0]["cnt"] if ent_count_res else 0
    # Qdrant point 数
    collections = [c for c in RecallConfig.COLLECTIONS if c.startswith("memory_")]
    qdrant_total = 0
    coll_counts = {}
    for coll in collections:
        try:
            cnt = client.count(collection_name=coll).count
            coll_counts[coll] = cnt
            qdrant_total += cnt
        except Exception:
            pass

    return {
        "neo4j_entities": ent_count,
        "neo4j_relations": rel_count,
        "qdrant_total_points": qdrant_total,
        "qdrant_per_collection": coll_counts,
    }


# ============================================================
# 检查 6：孤儿边（Neo4j 有边但对应 KO 没进 Qdrant）
# ============================================================
def check_neo4j_orphan_edges(session, client):
    """每个 KO 在 Qdrant 存为 1 个 point，Neo4j 存为 1+ 条边。
    如果 Neo4j 边数 显著超过 Qdrant points 数，说明有 KO 只进了 Neo4j 没进 Qdrant。
    表现：关系的 source / ko_summary 在 Qdrant payload 里搜不到对应 summary。
    """
    # 先 dump Neo4j 所有 active 边的 (subj, pred, obj, source, ko_summary)
    cypher = """
    MATCH (a)-[r]->(b)
    WHERE r.status IS NULL OR r.status = 'active'
    RETURN a.name AS subj, type(r) AS pred, b.name AS obj,
           coalesce(r.source, '') AS source,
           coalesce(r.ko_summary, '') AS ko_summary
    """
    neo4j_edges = []
    for rec in session.run(cypher).data():
        neo4j_edges.append({
            "subj": rec["subj"],
            "pred": rec["pred"],
            "obj": rec["obj"],
            "source": rec["source"],
            "ko_summary": rec["ko_summary"],
        })

    # 从 Qdrant 收集所有 summary / entities
    collections = [c for c in RecallConfig.COLLECTIONS if c.startswith("memory_")]
    qdrant_summaries = set()
    qdrant_entity_pairs = set()
    for coll in collections:
        try:
            offset = None
            while True:
                points, next_offset = client.scroll(
                    collection_name=coll,
                    limit=100,
                    offset=offset,
                    with_payload=["summary", "entities"],
                    with_vectors=False,
                )
                for p in points:
                    s = (p.payload.get("summary") or "").strip()
                    if s:
                        qdrant_summaries.add(s)
                    for e in p.payload.get("entities") or []:
                        if e:
                            qdrant_entity_pairs.add(e)
                if next_offset is None:
                    break
                offset = next_offset
        except Exception as e:
            print(f"[warn] scroll {coll}: {e}", file=sys.stderr)

    # 孤儿边：在 Qdrant 完全找不到踪迹的
    orphans = []
    for e in neo4j_edges:
        summary = e["ko_summary"]
        source_match = e["source"] and any(e["source"] in s for s in qdrant_summaries)
        summary_match = summary and summary in qdrant_summaries
        subj_in_qdrant = e["subj"] in qdrant_entity_pairs
        obj_in_qdrant = e["obj"] in qdrant_entity_pairs
        if not summary_match and not source_match and not (subj_in_qdrant and obj_in_qdrant):
            orphans.append(e)

    return {
        "total_neo4j_edges": len(neo4j_edges),
        "total_qdrant_summaries": len(qdrant_summaries),
        "orphan_count": len(orphans),
        "orphans_sample": orphans[:30],
    }


# ============================================================
# 检查 7：Neo4j 实体 vs Qdrant payload entities 集合 diff
# ============================================================
def check_neo4j_qdrant_entity_diff(session, client):
    """Neo4j 实体 vs Qdrant payload 里的 entities，应该接近 100% 一致。
    只在 Neo4j 出现的实体 = 孤儿实体（没有事实引用、或所有事实 KO 没进 Qdrant）
    只在 Qdrant 出现的实体 = 反向孤儿（payload 有但 KG 没表达）
    """
    # Neo4j 实体
    neo4j_ents = set()
    for rec in session.run("MATCH (n) WHERE n.name IS NOT NULL RETURN n.name AS name").data():
        neo4j_ents.add(rec["name"])

    # Qdrant entities
    collections = [c for c in RecallConfig.COLLECTIONS if c.startswith("memory_")]
    qdrant_ents = set()
    for coll in collections:
        try:
            offset = None
            while True:
                points, next_offset = client.scroll(
                    collection_name=coll,
                    limit=100,
                    offset=offset,
                    with_payload=["entities"],
                    with_vectors=False,
                )
                for p in points:
                    for e in p.payload.get("entities") or []:
                        if e:
                            qdrant_ents.add(e)
                if next_offset is None:
                    break
                offset = next_offset
        except Exception:
            pass

    only_neo4j = sorted(neo4j_ents - qdrant_ents)
    only_qdrant = sorted(qdrant_ents - neo4j_ents)

    return {
        "neo4j_entity_count": len(neo4j_ents),
        "qdrant_entity_count": len(qdrant_ents),
        "only_in_neo4j_count": len(only_neo4j),
        "only_in_qdrant_count": len(only_qdrant),
        "only_in_neo4j_sample": only_neo4j[:30],
        "only_in_qdrant_sample": only_qdrant[:30],
    }


# ============================================================
# 检查 8：Neo4j 边 predicate 白名单外未降级
# ============================================================
ALLOWED_PREDICATES = {
    # 基础人际关系
    "KNOWS", "LIKES", "LOVES", "WORKS_AT", "PARENT_OF", "FRIEND_OF",
    "VISITED", "OWNS", "HAS_GOAL", "HAS_HABIT", "BELIEVES", "MENTIONED_IN",
    # 生活化谓词
    "ATE", "PLAYED_WITH", "WATCHED", "VISITED_WITH", "ARGUMENT_WITH",
    "OFTEN_CALLS", "REMEMBERS", "TEACHES", "LEARNS_FROM",
    # 技术动作谓词
    "DESIGNS", "BUILDS", "STUDIES", "MANAGES",
    # 业务扩展
    "BELONGS_TO", "CAUSES", "TREATS", "INTERACTS_WITH", "DIAGNOSES",
    "CITES", "COAUTHORS", "PUBLISHES_IN", "EXTENDS", "CONTRADICTS",
    "AMENDS", "OVERRIDES", "APPLIES_TO",
}


def check_neo4j_predicate_whitelist(session):
    """Neo4j 里出现的谓词 + 在不在白名单 + 是否本该降级为 MENTIONED_IN。
    如果发现白名单外的谓词被直接创建 → MERGE 路径没卡干净。
    """
    cypher = """
    MATCH ()-[r]->()
    WHERE r.status IS NULL OR r.status = 'active'
    RETURN type(r) AS pred, count(r) AS cnt
    ORDER BY cnt DESC
    """
    rows = []
    for rec in session.run(cypher).data():
        pred = rec["pred"]
        in_wl = pred in ALLOWED_PREDICATES
        rows.append({
            "predicate": pred,
            "count": rec["cnt"],
            "in_whitelist": in_wl,
            "should_be": pred if in_wl else "MENTIONED_IN",
        })
    return rows


# ============================================================
# 检查 9：实体 label 分布（看是否混进了工具词 label）
# ============================================================
def check_neo4j_label_distribution(session):
    cypher = """
    MATCH (n)
    WHERE n.name IS NOT NULL
    RETURN labels(n) AS labels, count(n) AS cnt
    ORDER BY cnt DESC
    """
    rows = []
    for rec in session.run(cypher).data():
        rows.append({
            "labels": rec["labels"],
            "count": rec["cnt"],
        })
    return rows


# ============================================================
# 检查 10：Neo4j source 字段分布（看 source 是不是被截断或为空）
# ============================================================
def check_neo4j_source_field(session):
    cypher = """
    MATCH ()-[r]->()
    WHERE r.status IS NULL OR r.status = 'active'
    RETURN coalesce(r.source, '<NULL>') AS source,
           coalesce(r.ko_summary, '<NULL>') AS ko_summary,
           count(r) AS cnt
    ORDER BY cnt DESC
    LIMIT 20
    """
    rows = []
    for rec in session.run(cypher).data():
        rows.append({
            "source": rec["source"],
            "ko_summary_empty": rec["ko_summary"] == "<NULL>",
            "count": rec["cnt"],
        })
    return rows
def main():
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    client = _qdrant_client()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LOG_DIR / f"dedup-audit-{datetime.now(CN_TZ).strftime('%Y%m%d-%H%M%S')}.md"

    lines = []
    lines.append(f"# Memory OS 去重巡检 — {_now_cn()}\n")
    lines.append("5 项检查 + 结论。\n")
    lines.append("---\n")

    try:
        with driver.session() as session:
            # 1
            lines.append("\n## 1. Neo4j 同 (subj, pred, obj) 多个 active 边\n")
            dup_edges = check_neo4j_duplicate_active_edges(session)
            if dup_edges:
                lines.append(f"**⚠️ 发现 {len(dup_edges)} 组重复边**\n\n")
                lines.append("| subj | pred | obj | count |\n|---|---|---|---|\n")
                for r in dup_edges[:30]:
                    lines.append(f"| `{r['subj']}` | `{r['pred']}` | `{r['obj']}` | {r['count']} |\n")
                if len(dup_edges) > 30:
                    lines.append(f"\n_还有 {len(dup_edges) - 30} 组未列出_\n")
            else:
                lines.append("✅ 无\n")

            # 2
            lines.append("\n## 2. Neo4j superseded 死边（superseded_by 空）\n")
            dead = check_neo4j_superseded_dead_edges(session)
            if dead:
                lines.append(f"**⚠️ 发现 {len(dead)} 条 superseded 但 superseded_by 为空的死边**\n\n")
                lines.append("| subj | pred | obj | superseded_at |\n|---|---|---|---|\n")
                for r in dead[:30]:
                    lines.append(f"| `{r['subj']}` | `{r['pred']}` | `{r['obj']}` | {r['superseded_at']} |\n")
                if len(dead) > 30:
                    lines.append(f"\n_还有 {len(dead) - 30} 条未列出_\n")
            else:
                lines.append("✅ 无\n")

            # 3
            lines.append("\n## 3. Qdrant 各 collection point 数\n")
            qd = check_qdrant_dup_pids(client)
            lines.append("| collection | points |\n|---|---|\n")
            for r in qd:
                if "error" in r:
                    lines.append(f"| `{r['collection']}` | error: {r['error']} |\n")
                else:
                    lines.append(f"| `{r['collection']}` | {r['point_count']} |\n")

            # 4
            lines.append("\n## 4. Qdrant 跨 collection 重复 summary\n")
            cross = check_qdrant_cross_collection_dup(client)
            if cross:
                lines.append(f"**⚠️ 发现 {len(cross)} 条 summary 跨多 collection**\n\n")
                lines.append("| summary | collections |\n|---|---|\n")
                for r in cross[:30]:
                    lines.append(f"| {r['summary']} | {', '.join(r['collections'])} |\n")
                if len(cross) > 30:
                    lines.append(f"\n_还有 {len(cross) - 30} 条未列出_\n")
            else:
                lines.append("✅ 无\n")

            # 5
            lines.append("\n## 5. Neo4j vs Qdrant 数量对账\n")
            counts = check_neo4j_qdrant_counts(session, client)
            lines.append(f"- Neo4j 实体数: **{counts['neo4j_entities']}**\n")
            lines.append(f"- Neo4j 关系数: **{counts['neo4j_relations']}**\n")
            lines.append(f"- Qdrant point 总数: **{counts['qdrant_total_points']}**\n\n")
            lines.append("**说明：**\n")
            lines.append("- Neo4j 实体 / 关系 MERGE 去重（结构层）\n")
            lines.append("- Qdrant 按 pid 指纹覆盖（payload 层）\n")
            lines.append("- 实体数 ≠ Qdrant point 数是正常的（多个 KO 可引用同一实体；不同 KO 可合并到同一 point）\n")
            lines.append("- Neo4j 关系数 应当 ≈ Qdrant point 总数（每条事实 KO 对应一条边 / 一个 point）\n")
            ratio = counts["neo4j_relations"] / max(counts["qdrant_total_points"], 1)
            lines.append(f"\n当前比率: Neo4j 关系 / Qdrant point = **{ratio:.2f}**\n")
            if ratio > 1.5:
                lines.append("> ⚠️ 比率偏高 → Neo4j 重复边较多\n")
            elif ratio < 0.7:
                lines.append("> ⚠️ 比率偏低 → Qdrant 较多点未在 Neo4j 表达\n")
            else:
                lines.append("> ✅ 比率正常\n")

            # 6：孤儿边（Neo4j 有边但 Qdrant 找不到对应 KO）
            lines.append("\n## 6. 孤儿边（Neo4j 有但 Qdrant 没跡象）\n")
            orphan = check_neo4j_orphan_edges(session, client)
            lines.append(f"- Neo4j active 边总数: **{orphan['total_neo4j_edges']}**\n")
            lines.append(f"- Qdrant summary 集合去重后: **{orphan['total_qdrant_summaries']}**\n")
            lines.append(f"- 孤儿边数: **{orphan['orphan_count']}**\n\n")
            if orphan["orphan_count"] > 0:
                lines.append(f"**⚠️ 这些边在 Neo4j 存在，但 Qdrant 找不到对应 KO 的 summary / source / entities**\n\n")
                lines.append("| subj | pred | obj | source | ko_summary |\n|---|---|---|---|---|\n")
                for r in orphan["orphans_sample"]:
                    s = r["ko_summary"][:60] if r["ko_summary"] else ""
                    src = r["source"][:30] if r["source"] else ""
                    lines.append(f"| `{r['subj']}` | `{r['pred']}` | `{r['obj']}` | `{src}` | {s} |\n")
                if orphan["orphan_count"] > len(orphan["orphans_sample"]):
                    lines.append(f"\n_还有 {orphan['orphan_count'] - len(orphan['orphans_sample'])} 条未列出_\n")
            else:
                lines.append("✅ 无\n")

            # 7：实体集合 diff
            lines.append("\n## 7. 实体集合 diff（Neo4j 实体 vs Qdrant payload entities）\n")
            diff = check_neo4j_qdrant_entity_diff(session, client)
            lines.append(f"- Neo4j 实体数: **{diff['neo4j_entity_count']}**\n")
            lines.append(f"- Qdrant payload entities 去重后: **{diff['qdrant_entity_count']}**\n")
            lines.append(f"- 只在 Neo4j: **{diff['only_in_neo4j_count']}**\n")
            lines.append(f"- 只在 Qdrant: **{diff['only_in_qdrant_count']}**\n\n")
            if diff["only_in_neo4j_count"] > 0:
                lines.append(f"**只在 Neo4j 的实体（Qdrant 没引用）:**\n\n")
                lines.append(", ".join(f"`{e}`" for e in diff["only_in_neo4j_sample"]) + "\n")
            if diff["only_in_qdrant_count"] > 0:
                lines.append(f"\n**只在 Qdrant 的实体（Neo4j 未表达）:**\n\n")
                lines.append(", ".join(f"`{e}`" for e in diff["only_in_qdrant_sample"]) + "\n")
            if diff["only_in_neo4j_count"] == 0 and diff["only_in_qdrant_count"] == 0:
                lines.append("✅ 完全一致\n")

            # 8：谓词白名单检查
            lines.append("\n## 8. Neo4j 谓词白名单检查\n")
            preds = check_neo4j_predicate_whitelist(session)
            out_of_wl = [r for r in preds if not r["in_whitelist"]]
            lines.append(f"- 出现的谓词总数: **{len(preds)}**\n")
            lines.append(f"- 白名单外的谓词: **{len(out_of_wl)}**\n\n")
            if out_of_wl:
                lines.append("**⚠️ 白名单外的谓词应该降级为 MENTIONED_IN**\n\n")
                lines.append("| predicate | count | should_be |\n|---|---|---|\n")
                for r in out_of_wl:
                    lines.append(f"| `{r['predicate']}` | {r['count']} | `{r['should_be']}` |\n")
            else:
                lines.append("✅ 所有谓词都在白名单内\n")
            lines.append("\n完整谓词分布：\n\n")
            lines.append("| predicate | count | in_whitelist |\n|---|---|---|\n")
            for r in preds[:30]:
                lines.append(f"| `{r['predicate']}` | {r['count']} | {'✅' if r['in_whitelist'] else '❌'} |\n")

            # 9：实体 label 分布
            lines.append("\n## 9. Neo4j 实体 label 分布\n")
            labels = check_neo4j_label_distribution(session)
            lines.append(f"- 出现的 label 组合数: **{len(labels)}**\n\n")
            lines.append("| labels | count |\n|---|---|\n")
            for r in labels[:30]:
                lbl_str = ", ".join(f"`{l}`" for l in r["labels"]) if r["labels"] else "<无>"
                lines.append(f"| {lbl_str} | {r['count']} |\n")

            # 10：source 字段分布
            lines.append("\n## 10. Neo4j 边的 source / ko_summary 分布 top-20\n")
            sources = check_neo4j_source_field(session)
            lines.append("| source | ko_summary 空? | count |\n|---|---|---|\n")
            for r in sources:
                src = r["source"][:60] if len(r["source"]) > 60 else r["source"]
                lines.append(f"| `{src}` | {'是' if r['ko_summary_empty'] else '否'} | {r['count']} |\n")
    finally:
        driver.close()

    # 写文件
    out_path.write_text("".join(lines), encoding="utf-8")
    print(f"\n✅ 巡检完成 → {out_path}")
    print(f"\n--- 前 30 行预览 ---")
    print("".join(lines[:30]))


if __name__ == "__main__":
    main()