#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Memory OS 存量重复清理脚本（方案 B）

扫全库，按 embedding 相似度聚类（>= THRESH 视为同一事实），
每组保留 1 条（importance 最高 → summary 最长），删除其余。

- dry-run 模式（默认）：只打印将删的，不执行
- --apply：真正删除

用法：
  python3 dedup_cleanup.py                 # dry-run
  python3 dedup_cleanup.py --apply         # 真删
  python3 dedup_cleanup.py --threshold 0.90
"""

import sys
import os
import argparse

sys.path.insert(0, str(Path.home()) + "/.openclaw/workspace/memory-os-plugin/scripts")
os.environ.setdefault("MEMORY_OS_QDRANT_HOST", "127.0.0.1")
os.environ.setdefault("MEMORY_OS_EMBED_URL", "http://127.0.0.1:8765/embed")

import process_dream as pd
from qdrant_client import QdrantClient


def cos(v1, v2):
    if v1 and isinstance(v1[0], list):
        v1 = v1[0]
    if v2 and isinstance(v2[0], list):
        v2 = v2[0]
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = sum(a * a for a in v1) ** 0.5
    n2 = sum(b * b for b in v2) ** 0.5
    return dot / (n1 * n2 + 1e-9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正删除（默认 dry-run）")
    ap.add_argument("--threshold", type=float, default=0.90, help="相似度阈值，默认 0.90")
    args = ap.parse_args()

    client = QdrantClient(host="127.0.0.1", port=6333)

    # 1. 拉全库
    all_pts = []  # (collection, id, payload)
    for coll in client.get_collections().collections:
        name = coll.name
        try:
            pts, _ = client.scroll(collection_name=name, limit=500, with_payload=True)
            for p in pts:
                all_pts.append((name, p.id, p.payload or {}))
        except Exception as e:
            print(f"  [warn] scroll {name}: {e}")

    print(f"全库 {len(all_pts)} 条")

    # 2. 批量 embed
    texts = [p.get("summary", "") for _, _, p in all_pts]
    vecs = pd.embed(texts)
    if len(vecs) != len(all_pts):
        print(f"[error] embed 数量不对: {len(vecs)} != {len(all_pts)}")
        sys.exit(1)

    # 3. 贪心聚类
    n = len(all_pts)
    assigned = [False] * n
    groups = []
    for i in range(n):
        if assigned[i]:
            continue
        group = [i]
        assigned[i] = True
        for j in range(i + 1, n):
            if assigned[j]:
                continue
            s1 = all_pts[i][2].get("summary", "")
            s2 = all_pts[j][2].get("summary", "")
            if not s1 or not s2:
                continue
            try:
                if cos(vecs[i], vecs[j]) >= args.threshold:
                    group.append(j)
                    assigned[j] = True
            except Exception:
                pass
        if len(group) > 1:
            groups.append(group)

    print(f"相似度 >= {args.threshold} 的重复组: {len(groups)} 组，涉及 {sum(len(g) for g in groups)} 条\n")

    # 4. 每组选保留者：importance 高优先，其次 summary 长
    to_delete = []  # (collection, pid)
    for gi, group in enumerate(groups, 1):
        scored = []
        for idx in group:
            coll, pid, payload = all_pts[idx]
            imp = payload.get("importance")
            imp = float(imp) if imp is not None else 0.0
            slen = len(payload.get("summary", ""))
            scored.append((idx, imp, slen))
        # 保留：importance 最高 → summary 最长
        scored.sort(key=lambda x: (-x[1], -x[2]))
        keep_idx = scored[0][0]
        print(f"组 {gi} ({len(group)} 条) → 保留:")
        kcoll, kpid, kpayload = all_pts[keep_idx]
        print(f"  ✅ [{kcoll}] imp={kpayload.get('importance')} {kpayload.get('summary','')[:60]}")
        for idx, imp, slen in scored[1:]:
            coll, pid, payload = all_pts[idx]
            print(f"  🗑️  [{coll}] imp={payload.get('importance')} {payload.get('summary','')[:60]}")
            to_delete.append((coll, pid))
        print()

    print(f"共删除 {len(to_delete)} 条")

    # 5. 执行
    if args.apply:
        for coll, pid in to_delete:
            try:
                client.delete(collection_name=coll, points_selector=[pid])
            except Exception as e:
                print(f"  [warn] delete {coll}/{pid}: {e}")
        print(f"✅ 已删除 {len(to_delete)} 条")
    else:
        print("（dry-run，未实际删除。加 --apply 执行）")


if __name__ == "__main__":
    main()
