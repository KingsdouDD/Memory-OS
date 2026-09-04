#!/usr/bin/env python3
"""清理 Neo4j 里 (subj, pred, obj) 完全相同的重复关系。

逻辑：
  1. 找出所有 (subj, pred, obj) 重复的关系组
  2. 每组保留 r.updated 最大的那条（最新写的）
  3. 删除其他重复关系

约束：
  - 保留条不动数据，只删除
  - 跑前先 dry-run 一下，统计会删多少条
  - 默认 dry-run，传 --commit 才会真删
"""
import os
import sys
import argparse
from neo4j import GraphDatabase

NEO4J_URI = os.environ.get("MEMORY_OS_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("MEMORY_OS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("MEMORY_OS_NEO4J_PASSWORD", "openclaw")


def find_duplicates(driver):
    """找出所有 (subj, pred, obj) 重复的关系组。"""
    with driver.session() as s:
        result = s.run("""
            MATCH (a)-[r]->(b)
            WITH a.name AS subj, type(r) AS pred, b.name AS obj,
                 collect({
                     id: id(r),
                     updated: r.updated,
                     source: r.source,
                     ko_summary: r.ko_summary,
                     created: r.created
                 }) AS rels
            WHERE size(rels) > 1
            RETURN subj, pred, obj, rels
            ORDER BY size(rels) DESC
        """).data()
        return result


def delete_duplicates(driver, dupes):
    """每组保留 updated 最大的，其余删除。"""
    deleted = 0
    with driver.session() as s:
        for d in dupes:
            rels = sorted(d['rels'], key=lambda x: x.get('updated') or '', reverse=True)
            for to_delete in rels[1:]:
                s.run("MATCH ()-[r]->() WHERE id(r) = $rid DELETE r", rid=to_delete['id'])
                deleted += 1
    return deleted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="真删；不加则只 dry-run")
    parser.add_argument("--verbose", "-v", action="store_true", help="列出每组会被删的 id")
    args = parser.parse_args()

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        dupes = find_duplicates(driver)
        total_groups = len(dupes)
        total_to_delete = sum(len(d['rels']) - 1 for d in dupes)

        print(f"找到 {total_groups} 组重复关系，将要删除 {total_to_delete} 条")
        if args.verbose:
            for d in dupes:
                print(f"\n  ({d['subj']}) -[{d['pred']}]-> ({d['obj']})")
                rels = sorted(d['rels'], key=lambda x: x.get('updated') or '', reverse=True)
                for i, rel in enumerate(rels):
                    marker = "KEEP" if i == 0 else "DELETE"
                    print(f"    [{marker}] id={rel['id']} updated={rel.get('updated')} source={rel.get('source')}")

        total_before = single_count(driver)
        print(f"\n  当前关系总数: {total_before}")

        if not args.commit:
            print(f"\n[DRY-RUN] 不会真的删除。加 --commit 才会执行")
            return 0

        deleted = delete_duplicates(driver, dupes)
        total_after = single_count(driver)
        print(f"\n[COMMITTED] 删除 {deleted} 条，{total_before} → {total_after}")
        return 0
    finally:
        driver.close()


def single_count(driver):
    with driver.session() as s:
        return s.run("MATCH ()-[r]->() RETURN count(r) AS cnt").single()["cnt"]


if __name__ == "__main__":
    sys.exit(main())
