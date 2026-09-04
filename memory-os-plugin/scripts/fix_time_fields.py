#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Memory OS 存量时间字段修复脚本

背景：老数据 27/29 条缺 recorded_at，导致"最近/今天"类时间意图召回全废。
修复策略（按优先级）：
  1. summary 里的显式时间线索 → 推断 event_time
     - "于2026-08-05" / "2026-08-05" → 具体日期
     - "最近" / "前几天" → 3 天前
     - "今天" / "今天下午" → 今天
     - "小时候" / "童年" / "当年" → 假设 20 年前（有 event_time 但 recorded_at 是写入时间）
  2. recorded_at：无则用现在时间（写入时刻即记忆创建时刻，合理兜底）
  3. source_time：无则用 recorded_at

用法：
  python3 fix_time_fields.py           # dry-run
  python3 fix_time_fields.py --apply   # 真写
"""

import sys
import os
import re
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, str(Path.home()) + "/.openclaw/workspace/memory-os-plugin/scripts")
os.environ.setdefault("MEMORY_OS_QDRANT_HOST", "127.0.0.1")

from qdrant_client import QdrantClient

CN_TZ = __import__("zoneinfo").ZoneInfo("Asia/Shanghai")


def infer_time(summary):
    """从 summary 提取时间线索，返回 (recorded_at, event_time_dict) 或 None。"""
    now = datetime.now(CN_TZ)
    s = summary or ""
    et = None
    rec = None

    # 1. 显式日期
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            et = {"start": f"{y:04d}-{mo:02d}-{d:02d}T00:00:00+08:00", "end": None,
                  "expression": "explicit", "precision": "day"}
        except Exception:
            pass
    # 2. "最近" / "前几天" / "这两天"
    elif re.search(r"最近|这两天|前几天|刚", s):
        et = {"start": (now - timedelta(days=3)).isoformat(timespec="seconds"),
              "end": None, "expression": "recent", "precision": "day"}
    # 3. "今天"
    elif re.search(r"今天|今日", s):
        et = {"start": now.isoformat(timespec="seconds"), "end": None,
              "expression": "today", "precision": "day"}
    # 4. 童年/小时候/当年 → 无精确时间，只标 expression
    elif re.search(r"小时候|童年|当年|以前|曾经", s):
        et = {"start": None, "end": None, "expression": "childhood", "precision": "vague"}

    if et is None:
        return None
    rec = now.isoformat(timespec="seconds")
    return rec, et


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    client = QdrantClient(host="127.0.0.1", port=6333)
    fixed = 0
    skipped = 0

    for coll in client.get_collections().collections:
        name = coll.name
        pts, _ = client.scroll(collection_name=name, limit=500, with_payload=True)
        for p in pts:
            pl = p.payload or {}
            if pl.get("recorded_at"):
                skipped += 1  # 已有时间戳，跳过
                continue
            summary = pl.get("summary", "")
            inferred = infer_time(summary)
            if not inferred:
                skipped += 1
                continue
            rec, et = inferred
            new_pl = dict(pl)
            new_pl["recorded_at"] = rec
            new_pl["source_time"] = rec
            if et:
                new_pl["event_time"] = et
            if args.apply:
                client.set_payload(collection_name=name, payload=new_pl, points=[p.id])
            fixed += 1
            print(f"  [{name}] {summary[:40]}")
            print(f"    → recorded_at={rec[:19]} event_time={et.get('expression','?')}")

    print(f"\n修复 {fixed} 条（有线索），跳过 {skipped} 条（已有时间戳或无线索）")
    if not args.apply:
        print("（dry-run，加 --apply 写入）")


if __name__ == "__main__":
    main()
