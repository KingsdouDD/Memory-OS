#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recall_stats.py - Memory OS 召回质量统计

读取 hook-trace.md，统计：
  - 总触发次数 / 跳过次数 / 注入次数
  - 跳过的原因分布（too_short / filler / swear / emotional_vent ...）
  - 召回率 / 融合率 / KG verify 通过率
  - 渠道分布：vec-only / graph-only / both
  - 最近的 config_dump（确认当前生效参数）

用法：
  python3 recall_stats.py                # 默认读 hook-trace.md，最近全部
  python3 recall_stats.py --hours 24     # 只看最近 24h
  python3 recall_stats.py --hours 168    # 最近 7d
  python3 recall_stats.py --json         # 输出纯 JSON

md 日志格式约定（由 log_hook_event 写入）：
  ### HH:MM:SS event_type
  - **key**: value
  - **key**:
    ```json
    {...}
    ```
  - **query**: 老豆说的话
事件块之间用空行分隔。每条事件第一行时间戳作为该事件的"时间"。
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_LOG = Path(
    str(Path.home()) + "/.openclaw/workspace/memory-os/logs/hook-trace.md"
)

CN_TZ = timezone(timedelta(hours=8))

# 事件头：### HH:MM:SS event_type
EVENT_RE = re.compile(r"^###\s+(\S+)\s+(\S+)\s*$")


def parse_event_ts(ts_str):
    """解析事件头里的 HH:MM:SS，结合今天的日期构造完整 datetime。
    注意：md 日志没有日期，只有当日 HH:MM:SS。跨午夜时不准，但日常足够。
    """
    if not ts_str:
        return None
    try:
        today = datetime.now(CN_TZ).date()
        h, m, s = ts_str.split(":")
        return datetime.combine(
            today,
            __import__("datetime").time(int(h), int(m), int(s)),
            tzinfo=CN_TZ,
        )
    except Exception:
        return None


def within_window(ts, cutoff):
    if ts is None or cutoff is None:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=CN_TZ)
    return ts >= cutoff


def parse_md_log(text):
    """把 md 日志切成事件列表，每条事件是：
      {"ts": datetime|None, "event": str, "fields": dict, "raw_blocks": list}
    字段提取逻辑：
      - 标量行：- **k**: v
      - 代码块行：- **k**:  + ```...```  → 把代码块里的 JSON 字符串还原成 dict/list
    """
    events = []
    cur = None
    cur_k = None
    in_code = False
    code_buf = []
    code_indent = 0

    def flush_field():
        nonlocal cur, cur_k, in_code, code_buf
        if cur is None:
            return
        if cur_k is not None:
            # 把积累的代码块作为该字段的值
            if code_buf:
                raw = "\n".join(code_buf)
                # 尝试按 JSON 解析
                try:
                    val = json.loads(raw)
                except Exception:
                    val = raw
                cur["fields"][cur_k] = val
                code_buf = []
                in_code = False
            cur_k = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        m = EVENT_RE.match(line)
        if m:
            # 新事件，先收尾上一个
            flush_field()
            if cur is not None:
                events.append(cur)
            cur = {
                "ts": parse_event_ts(m.group(1)),
                "event": m.group(2),
                "fields": {},
            }
            cur_k = None
            in_code = False
            code_buf = []
            continue

        if cur is None:
            continue

        # 进入/退出代码块
        if line.strip() == "```":
            if in_code:
                # 退出代码块，flush
                flush_field()
            else:
                # 进入代码块；此时 cur_k 必须不为 None（由上一个 - **k**: 行设置）
                in_code = True
                code_buf = []
            continue

        if in_code:
            code_buf.append(line)
            continue

        # 标量字段行：- **k**: value
        # 或开始字段：- **k**: （下一行是代码块）
        fm = re.match(r"^-\s+\*\*(?P<k>[^*]+)\*\*:\s*(?P<v>.*)$", line)
        if fm:
            # 先收尾上一个字段
            flush_field()
            k = fm.group("k").strip()
            v = fm.group("v").strip()
            cur_k = k
            if v == "":
                # 等代码块
                continue
            cur["fields"][k] = v
            cur_k = None  # 标量字段不期待代码块
            continue

        # 其他行：忽略（空行等）
        if line.strip() == "":
            continue

    # 收尾
    flush_field()
    if cur is not None:
        events.append(cur)
    return events


def main():
    ap = argparse.ArgumentParser(description="Memory OS 召回统计")
    ap.add_argument("--hours", type=float, default=0,
                    help="只看最近 N 小时（0 = 全部）")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG,
                    help=f"hook-trace.md 路径（默认 {DEFAULT_LOG}）")
    ap.add_argument("--json", action="store_true", help="输出纯 JSON")
    args = ap.parse_args()

    if not args.log.exists():
        print(f"[err] 日志不存在: {args.log}", file=sys.stderr)
        sys.exit(1)

    cutoff = None
    if args.hours and args.hours > 0:
        cutoff = datetime.now(CN_TZ) - timedelta(hours=args.hours)

    text = args.log.read_text(encoding="utf-8")
    events = parse_md_log(text)

    # ---- 累计指标 ----
    total = 0
    skipped = 0
    injected = 0
    skip_reasons = Counter()

    # 召回质量累计
    q_vec_hits = 0
    q_graph_hits = 0
    q_fused = 0
    q_kg_pass = 0
    q_vec_only = 0
    q_graph_only = 0
    q_both = 0
    inject_count = 0

    # KG 三元组累计
    kg_triple_total = 0
    kg_triple_anchors = Counter()

    last_config = None

    for ev in events:
        if cutoff and not within_window(ev["ts"], cutoff):
            continue
        et = ev["event"]
        f = ev["fields"]
        total += 1
        if et == "recall_skipped":
            skipped += 1
            r = f.get("reason", "unknown")
            skip_reasons[r] += 1
        elif et == "recall_injected":
            injected += 1
            q = f.get("quality") or {}
            if isinstance(q, str):
                try:
                    q = json.loads(q)
                except Exception:
                    q = {}
            q_vec_hits += q.get("vec_hits", 0) if isinstance(q, dict) else 0
            q_graph_hits += q.get("graph_hits", 0) if isinstance(q, dict) else 0
            q_fused += q.get("fused_n", 0) if isinstance(q, dict) else 0
            q_kg_pass += q.get("kg_pass_n", 0) if isinstance(q, dict) else 0
            q_vec_only += q.get("vec_only", 0) if isinstance(q, dict) else 0
            q_graph_only += q.get("graph_only", 0) if isinstance(q, dict) else 0
            q_both += q.get("both", 0) if isinstance(q, dict) else 0
            inject_count += f.get("n_memories", 0) or 0
            # KG triples
            triples = f.get("kg_triples") or []
            if isinstance(triples, str):
                try:
                    triples = json.loads(triples)
                except Exception:
                    triples = []
            if isinstance(triples, list):
                kg_triple_total += len(triples)
                for t in triples:
                    if isinstance(t, dict) and t.get("anchor"):
                        kg_triple_anchors[t["anchor"]] += 1
        elif et == "config_dump":
            cfg = f.get("config")
            if isinstance(cfg, str):
                last_config = cfg
            else:
                last_config = json.dumps(cfg, ensure_ascii=False)

    summary = {
        "log_path": str(args.log),
        "window_hours": args.hours if args.hours else "all",
        "total_events": total,
        "skipped": skipped,
        "injected": injected,
        "skip_ratio": round(skipped / max(total, 1), 3),
        "inject_ratio": round(injected / max(total, 1), 3),
        "skip_reasons": dict(skip_reasons.most_common()),
        "recall_quality": {
            "vec_hits_total": q_vec_hits,
            "graph_hits_total": q_graph_hits,
            "fused_total": q_fused,
            "kg_pass_total": q_kg_pass,
            "inject_total_memories": inject_count,
            "kg_pass_ratio_avg": round(q_kg_pass / max(q_fused, 1), 3),
            "vec_only_total": q_vec_only,
            "graph_only_total": q_graph_only,
            "both_total": q_both,
        },
        "kg_triples": {
            "total_in_log": kg_triple_total,
            "top_anchors": dict(kg_triple_anchors.most_common(10)),
        },
        "last_config": last_config,
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    # ---- 人类可读输出 ----
    print("=" * 60)
    print(f"Memory OS 召回统计  （窗口：{summary['window_hours']}）")
    print(f"日志：{summary['log_path']}")
    print("=" * 60)
    print(f"总事件:       {total}")
    print(f"  - skipped:  {skipped}  ({summary['skip_ratio']*100:.1f}%)")
    print(f"  - injected: {injected}  ({summary['inject_ratio']*100:.1f}%)")
    print()
    print("跳过原因分布:")
    if skip_reasons:
        for r, c in skip_reasons.most_common():
            print(f"  {r:<20s} {c:>4}")
    else:
        print("  （无）")
    print()
    rq = summary["recall_quality"]
    print("召回质量累计:")
    print(f"  vec 命中:   {rq['vec_hits_total']}")
    print(f"  graph 命中: {rq['graph_hits_total']}")
    print(f"  融合候选:   {rq['fused_total']}")
    print(f"  KG 通过:    {rq['kg_pass_total']}  (avg ratio {rq['kg_pass_ratio_avg']*100:.1f}%)")
    print(f"  注入条数:   {rq['inject_total_memories']}")
    print(f"  vec-only:   {rq['vec_only_total']}")
    print(f"  graph-only: {rq['graph_only_total']}")
    print(f"  both:       {rq['both_total']}")
    print()
    kg = summary["kg_triples"]
    print(f"KG 三元组（累计）: {kg['total_in_log']} 条")
    if kg["top_anchors"]:
        print("  高频 anchor:")
        for a, c in list(kg["top_anchors"].items())[:5]:
            print(f"    {a:<20s} {c:>4}")
    else:
        print("  （无 KG 召回）")
    print()

    if last_config:
        try:
            cfg = json.loads(last_config)
        except Exception:
            cfg = None
        if cfg:
            print("当前生效参数（最近一次 config_dump）:")
            for k in [
                "HOOK_MIN_LEN", "HOOK_MAX_LEN",
                "GRAPH_DEPTH", "GRAPH_LIMIT_PER_NODE",
                "RRF_K", "RRF_RELATIVE_KEEP_RATIO",
                "KG_STRONG_THRESHOLD", "KG_WEAK_THRESHOLD", "KG_TOP_N",
                "RECALL_DEFAULT_TOP_K", "RECALL_DEFAULT_RRF_K",
                "VEC_TOP_K_MULTIPLIER",
            ]:
                if k in cfg:
                    print(f"  {k:<30s} = {cfg[k]}")
            print()


if __name__ == "__main__":
    main()