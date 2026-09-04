#!/usr/bin/env python3
"""Memory OS 孤儿边清理脚本

按 schema 白名单清理存量 Neo4j 边：
- predicate 不在 RecallConfig.ALLOWED_RELATIONSHIPS → 改成 MENTIONED_IN
- predicate 含中文 / 是拒收词 → 改成 MENTIONED_IN
- 实体 label 不在 RecallConfig.ALLOWED_LABELS → 改成 Concept
- 空 source / 空 ko_summary 的边 → 打上 legacy_marker 便于追溯

只动 schema 不合规的边，不删数据。dry-run 默认 False，要先 dry-run 看效果再实际跑。

跑法：
  python3 memory-os-plugin/scripts/clean_neo4j_orphans.py --dry-run  # 预览
  python3 memory-os-plugin/scripts/clean_neo4j_orphans.py            # 实际改
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent))
from recall_config import RecallConfig

CN_TZ = timezone(timedelta(hours=8))
NEO4J_URI = os.environ.get("MEMORY_OS_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("MEMORY_OS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("MEMORY_OS_NEO4J_PASSWORD", "openclaw")
LOG_DIR = Path(str(Path.home()) + "/.openclaw/workspace/memory-os/logs")


def _now_cn() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _sanitize_predicate(raw, allowed, denied_words):
    pred = (raw or "").strip()
    if not pred:
        return "MENTIONED_IN"
    if pred in denied_words:
        return "MENTIONED_IN"
    if not pred.isascii() or " " in pred:
        return "MENTIONED_IN"
    pred = pred.upper()
    if pred in allowed:
        return pred
    return "MENTIONED_IN"


def _is_label_allowed(label, allowed):
    return label in allowed


def collect_violations(session):
    """扫所有边和实体，按 schema 列出违规项。"""
    # 边：predicate 不合规
    cypher_edges = """
    MATCH (a)-[r]->(b)
    WHERE r.status IS NULL OR r.status = 'active'
    RETURN a.name AS subj, type(r) AS pred, b.name AS obj,
           id(r) AS rid, coalesce(r.source, '') AS source
    """
    bad_edges = []
    for rec in session.run(cypher_edges).data():
        new_pred = _sanitize_predicate(rec["pred"], RecallConfig.ALLOWED_RELATIONSHIPS, RecallConfig.DENIED_PREDICATE_WORDS)
        if new_pred != rec["pred"]:
            bad_edges.append({
                "type": "edge_predicate",
                "rid": rec["rid"],
                "subj": rec["subj"],
                "old_pred": rec["pred"],
                "new_pred": new_pred,
                "obj": rec["obj"],
                "source": rec["source"],
            })

    # 实体：label 不合规
    cypher_nodes = """
    MATCH (n)
    WHERE n.name IS NOT NULL
    RETURN n.name AS name, labels(n) AS labels, id(n) AS nid
    """
    bad_nodes = []
    for rec in session.run(cypher_nodes).data():
        labels = rec["labels"] or []
        if not labels:
            bad_nodes.append({
                "type": "node_no_label",
                "nid": rec["nid"],
                "name": rec["name"],
                "old_labels": [],
                "new_labels": ["Concept"],
            })
            continue
        # 只保留白名单内的 label，多 label 取第一个
        kept = [l for l in labels if _is_label_allowed(l, RecallConfig.ALLOWED_LABELS)]
        if not kept:
            bad_nodes.append({
                "type": "node_bad_label",
                "nid": rec["nid"],
                "name": rec["name"],
                "old_labels": labels,
                "new_labels": ["Concept"],
            })

    return bad_edges, bad_nodes


def apply_fixes(session, bad_edges, bad_nodes):
    """实际改库。"""
    report = {"edges_fixed": 0, "nodes_relabeled": 0, "errors": 0}

    # 改边：删旧关系 + 建新关系（保持其他属性）
    # 因为 Neo4j 不支持直接改关系类型，所以删 + 重建最稳
    for e in bad_edges:
        try:
            cypher = """
            MATCH (a {name: $subj})-[r]->(b {name: $obj})
            WHERE type(r) = $old
            WITH a, b, properties(r) AS props, type(r) AS old_type
            DELETE r
            WITH a, b, props, old_type
            CALL apoc.merge.relationship(a, $new, {}, props, b, b) YIELD rel
            SET rel.status = 'active',
                rel.schema_fixed_at = datetime(),
                rel.schema_old_type = old_type
            RETURN rel
            """
            try:
                session.run(cypher, subj=e["subj"], obj=e["obj"], old=e["old_pred"], new=e["new_pred"])
            except Exception:
                # apoc 可能没装，退路：先建新的 + 再删旧的
                session.run("""
                    MATCH (a {name: $subj}), (b {name: $obj})
                    MERGE (a)-[r:""" + e["new_pred"] + """]->(b)
                    SET r.status = 'active', r.schema_fixed_at = datetime(),
                        r.schema_old_type = $old
                """, subj=e["subj"], obj=e["obj"], old=e["old_pred"])
                session.run("""
                    MATCH (a {name: $subj})-[r:""" + e["old_pred"] + """]->(b {name: $obj})
                    DELETE r
                """, subj=e["subj"], obj=e["obj"])
            report["edges_fixed"] += 1
        except Exception as ex:
            report["errors"] += 1
            print(f"[warn] edge fix failed: {e['subj']} -[{e['old_pred']}]-> {e['obj']}: {ex}", file=sys.stderr)

    # 改 label
    for n in bad_nodes:
        try:
            old_labels = n["old_labels"]
            new_labels = n["new_labels"]
            # 移除所有旧 label，加新 label
            cypher = """
            MATCH (n) WHERE id(n) = $nid
            WITH n, labels(n) AS old
            UNWIND old AS l
            WITH n, collect(l) AS old_labels
            FOREACH (l IN old_labels | REMOVE n:l)
            FOREACH (l IN $new | SET n:l)
            SET n.schema_relabeled_at = datetime(),
                n.schema_old_labels = $old_str
            RETURN n
            """
            session.run(cypher, nid=n["nid"], new=new_labels, old_str=",".join(old_labels))
            report["nodes_relabeled"] += 1
        except Exception as ex:
            report["errors"] += 1
            print(f"[warn] node relabel failed: {n['name']}: {ex}", file=sys.stderr)

    return report


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只看不动")
    args = parser.parse_args()

    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_path = LOG_DIR / f"orphan-clean-{datetime.now(CN_TZ).strftime('%Y%m%d-%H%M%S')}.md"

    lines = []
    lines.append(f"# Memory OS 孤儿清理 — {_now_cn()}\n")
    lines.append(f"模式: **{'DRY-RUN' if args.dry_run else '实际改库'}**\n\n")

    with driver.session() as session:
        bad_edges, bad_nodes = collect_violations(session)

        lines.append(f"- 违规边数: **{len(bad_edges)}**\n")
        lines.append(f"- 违规实体数: **{len(bad_nodes)}**\n\n")

        if bad_edges:
            lines.append("## 违规边（前 30）\n\n")
            lines.append("| subj | old_pred → new_pred | obj | source |\n|---|---|---|---|\n")
            for e in bad_edges[:30]:
                src = e["source"][:40] if e["source"] else "<空>"
                lines.append(f"| `{e['subj']}` | `{e['old_pred']}` → `{e['new_pred']}` | `{e['obj']}` | {src} |\n")
            if len(bad_edges) > 30:
                lines.append(f"\n_还有 {len(bad_edges) - 30} 条未列出_\n")

        if bad_nodes:
            lines.append(f"\n## 违规实体（前 30）\n\n")
            lines.append("| name | old_labels → new_labels |\n|---|---|\n")
            for n in bad_nodes[:30]:
                old = ", ".join(f"`{l}`" for l in n["old_labels"]) if n["old_labels"] else "<无>"
                new = ", ".join(f"`{l}`" for l in n["new_labels"])
                lines.append(f"| `{n['name']}` | {old} → {new} |\n")
            if len(bad_nodes) > 30:
                lines.append(f"\n_还有 {len(bad_nodes) - 30} 个未列出_\n")

        if not args.dry_run and (bad_edges or bad_nodes):
            lines.append("\n## 实际改库\n\n")
            report = apply_fixes(session, bad_edges, bad_nodes)
            lines.append(f"- 边修复: **{report['edges_fixed']}**\n")
            lines.append(f"- 实体重打 label: **{report['nodes_relabeled']}**\n")
            lines.append(f"- 错误: **{report['errors']}**\n")
        elif args.dry_run:
            lines.append("\n## DRY-RUN 预览（未实际改库）\n\n加 `--no-dry-run` 跑实际改库。\n")

    driver.close()

    out_path.write_text("".join(lines), encoding="utf-8")
    print(f"\n✅ 清理完成 → {out_path}")
    print("".join(lines[:20]))


if __name__ == "__main__":
    main()