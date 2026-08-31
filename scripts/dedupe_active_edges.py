#!/usr/bin/env python3
"""合并 Neo4j 同 (subj, pred, obj) 多条 active 边。

策略：保留"最完整"的一条（updated 最新 + ko_summary 不空），
其余打 status='superseded'，保留追溯。
"""
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

from neo4j import GraphDatabase

NEO4J_URI = os.environ.get("MEMORY_OS_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("MEMORY_OS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("MEMORY_OS_NEO4J_PASSWORD", "openclaw")

CN_TZ = timezone(timedelta(hours=8))
LOG_DIR = Path(str(Path.home()) + "/.openclaw/workspace/memory-os/logs")


def main():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    report = {"groups": 0, "superseded": 0, "no_change": 0}

    with driver.session() as session:
        # 找所有重复 (subj, pred, obj) 组（active）
        groups = session.run("""
            MATCH (a)-[r]->(b)
            WHERE r.status IS NULL OR r.status = 'active'
            WITH a.name AS subj, type(r) AS pred, b.name AS obj, collect(r) AS rels
            WHERE size(rels) > 1
            RETURN subj, pred, obj, rels
        """).data()
        print(f"重复边组数: {len(groups)}")

        for g in groups:
            rels = g["rels"]

            def score(rel):
                d = rel._asdict() if hasattr(rel, "_asdict") else dict(rel)
                ko = d.get("ko_summary") or ""
                upd = d.get("updated") or ""
                return (1 if ko else 0, upd)

            rels_sorted = sorted(rels, key=score, reverse=True)
            keep = rels_sorted[0]
            kill = rels_sorted[1:]

            report["groups"] += 1
            keep_summary = keep.get("ko_summary") or ""
            for r in kill:
                try:
                    session.run(
                        """
                        MATCH ()-[r]->() WHERE id(r) = $rid
                        SET r.status = 'superseded',
                            r.superseded_at = datetime(),
                            r.superseded_by = $keep_summary
                        """,
                        rid=r.id,
                        keep_summary=keep_summary[:200],
                    )
                    report["superseded"] += 1
                except Exception as e:
                    report["no_change"] += 1
                    print(f"  fail {r.id}: {e}")

    driver.close()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(CN_TZ).strftime("%Y%m%d-%H%M%S")
    out_path = LOG_DIR / f"edge-dedupe-{ts}.md"
    out_path.write_text(
        f"""# Edge Dedupe Report — {datetime.now(CN_TZ).strftime('%Y-%m-%d %H:%M:%S')}

- 重复组数: **{report['groups']}**
- 被打 superseded: **{report['superseded']}**
- 失败: **{report['no_change']}**
""",
        encoding="utf-8",
    )
    print(f"报告: {report}")
    print(f"dump: {out_path}")


if __name__ == "__main__":
    main()