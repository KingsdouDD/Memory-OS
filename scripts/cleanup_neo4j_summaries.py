#!/usr/bin/env python3
"""清理 Neo4j 里存的长文本字段：
- KO.summary
- 关系边上的 ko_summary
- L0Conversation 边上的 ko_summary
- Persona.summary（用 pid_str 定位）
- Scenario.summary（用 scenario_id 定位）

运行一次即可，已有的 summary 字段值不会被用到召回链路上。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用 process_dream 里的配置
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_OS_PLUGIN = os.path.join(SCRIPT_DIR, "..", "memory-os-plugin")
sys.path.insert(0, os.path.join(MEMORY_OS_PLUGIN, "scripts"))

from process_dream import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
from neo4j import GraphDatabase


def cleanup():
    driver = GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USER, NEO4J_PASSWORD),
        notifications_min_severity="OFF",
    )

    stats = {"ko_summary_removed": 0, "persona_summary_removed": 0,
             "scenario_summary_removed": 0, "l0_ko_summary_removed": 0}

    with driver.session() as sess:
        # 1. KO 节点：REMOVE summary 属性
        r = sess.run("MATCH (k:KO) WHERE k.summary IS NOT NULL RETURN count(k) AS cnt")
        cnt = r.single()["cnt"]
        print(f"  KO 节点含 summary: {cnt} 个 → 清除")
        r = sess.run("MATCH (k:KO) WHERE k.summary IS NOT NULL REMOVE k.summary RETURN count(k) AS cnt")
        stats["ko_summary_removed"] = r.single()["cnt"]

        # 2. 所有关系边：REMOVE ko_summary
        r = sess.run("MATCH ()-[r]->() WHERE r.ko_summary IS NOT NULL RETURN count(r) AS cnt")
        cnt = r.single()["cnt"]
        print(f"  关系边含 ko_summary: {cnt} 条 → 清除")
        r = sess.run("MATCH ()-[r]->() WHERE r.ko_summary IS NOT NULL REMOVE r.ko_summary RETURN count(r) AS cnt")
        stats["l0_ko_summary_removed"] = r.single()["cnt"]

        # 3. Persona 节点：REMOVE summary（用 pid_str 定位，summary 本就不应该存）
        r = sess.run("MATCH (p:Persona) WHERE p.summary IS NOT NULL RETURN count(p) AS cnt")
        cnt = r.single()["cnt"]
        print(f"  Persona 节点含 summary: {cnt} 个 → 清除")
        r = sess.run("MATCH (p:Persona) WHERE p.summary IS NOT NULL REMOVE p.summary RETURN count(p) AS cnt")
        stats["persona_summary_removed"] = r.single()["cnt"]

        # 4. Scenario 节点：REMOVE summary（用 scenario_id 定位，summary 本就不应该存）
        r = sess.run("MATCH (s:Scenario) WHERE s.summary IS NOT NULL RETURN count(s) AS cnt")
        cnt = r.single()["cnt"]
        print(f"  Scenario 节点含 summary: {cnt} 个 → 清除")
        r = sess.run("MATCH (s:Scenario) WHERE s.summary IS NOT NULL REMOVE s.summary RETURN count(s) AS cnt")
        stats["scenario_summary_removed"] = r.single()["cnt"]

    driver.close()
    print("\n清理完成:", stats)
    return stats


if __name__ == "__main__":
    print("=== Neo4j 长文本字段清理 ===")
    cleanup()
