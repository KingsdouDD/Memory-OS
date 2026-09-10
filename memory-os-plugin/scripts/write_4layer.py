#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
4 层记忆写入器（旁路，不动 process_dream.py）
- L1: 复用 write_kos_v5()（一行调用）
- L2: 新增 Scenario 节点 + memory_scenario collection
- L3: 新增 Persona 节点 + memory_persona collection

设计原则：
  - 不修改 process_dream.py / recall_config.py 任何代码
  - L1 走原路径，L2/L3 独立写入
  - 所有错误降级（写失败不抛异常，返回 ok=False）
  - 不动现有数据（PID 独立生成，不撞车）
"""

import os
import sys
import json
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from process_dream import (
    embed, _now_cn_iso, _qdrant_client,
    qdrant_ensure_collection, qdrant_upsert_point,
    write_kos_v5, write_kos_v5_return_pids, _normalize_time_fields,
)

# LLM 驱动的去重决策（优先走 LLM，失败时降级到原规则）
# 路径：scripts/write_4layer.py → ../model_runtime/dedup_bridge.py
_BRIDGE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "model_runtime"))
if _BRIDGE_PATH not in sys.path:
    sys.path.insert(0, _BRIDGE_PATH)
from dedup_bridge import dedup_decide_layer_action as _rule_decide_layer_action
from recall_config import RecallConfig

CN_TZ = timezone(timedelta(hours=8))
NEO4J_URI = os.environ.get("MEMORY_OS_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("MEMORY_OS_NEO4J_USER", "neo4j")
NEO4J_PASS = os.environ.get("MEMORY_OS_NEO4J_PASSWORD", "openclaw")

L2_COLLECTION = "memory_scenario"
L3_COLLECTION = "memory_persona"
L0_COLLECTION = "memory_l0"


def _ann_find_candidates_in_collection(collection, text, top_k=5):
    """在单个 collection 里查 ANN 候选（仿 L1 _ann_find_candidates，限定一个 collection）。

    返回 [{pid, score, payload}]，按 score 降序。
    """
    try:
        client = _qdrant_client()
    except Exception:
        return []
    if not text or not text.strip():
        return []
    try:
        vecs = embed(text)
        if not vecs:
            return []
        vec = vecs[0] if isinstance(vecs[0], list) else vecs
        res = client.query_points(
            collection_name=collection,
            query=vec,
            limit=top_k,
            score_threshold=RecallConfig.WRITE_ANN_RECALL_THRESHOLD,
        )
        out = []
        for hit in res.points:
            out.append({
                "pid": hit.id,
                "score": float(hit.score),
                "payload": hit.payload or {},
            })
        out.sort(key=lambda x: -x["score"])
        return out
    except Exception:
        return []


def _now_cn_str():
    """CN 时区当前时间（字符串，用于 Neo4j SET 属性）。"""
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _gen_pid_layer(text, layer):
    """L2/L3 独立 PID（前缀带层名，避免与 L1 撞车）。"""
    safe = (text or "").strip()
    return int(hashlib.md5(f"4layer|{layer}|{safe}".encode()).hexdigest()[:16], 16)


def _safe_label(label):
    """label 白名单检查（绕开 _sanitize_label，写自己的）。"""
    label = label or "Concept"
    if label not in RecallConfig.ALLOWED_LABELS:
        return "Concept"
    return label


def write_l0_conversation(l0_payload, l1_kos=None, linked_l1_pids=None):
    """写 L0 原始对话：全文 BM25 召回通道。

    Args:
        l0_payload: {"scene_summary": "...", "source": "..."}
        l1_kos: 对应的 L1 KO 列表（用于反向关联 Neo4j 边）
        linked_l1_pids: 可选，已写入的 L1 PID 列表（用于跨层级联删除追溯）

    L0 用 UUID 作 point id（与 L1/L2/L3 不共享 ID 空间）。
    Neo4j 建 :L0Conversation 节点，反向边 :GENERATED → L1 KO（如果 L1 KO 已写入）。
    去重逻辑：仿 L1 ANN 三态，scene_summary 高相似 → SKIP。
    """
    if not l0_payload:
        return {"layer": "L0", "skipped": True, "reason": "l0 is null"}

    scene_summary = (l0_payload.get("scene_summary") or "").strip()
    source = (l0_payload.get("source") or "").strip()
    if not scene_summary:
        return {"layer": "L0", "skipped": True, "reason": "缺少 scene_summary"}

    # ---- 0. ANN 去重决策（LLM 语义判断）----
    candidates = _ann_find_candidates_in_collection(L0_COLLECTION, scene_summary, top_k=5)
    action, reason = _rule_decide_layer_action(l0_payload.get("state"), candidates, layer="L0", new_text=scene_summary)
    if action == "SKIP":
        return {"layer": "L0", "skipped": True, "reason": f"dup: {reason}",
                "l0_id": str(candidates[0]["pid"]) if candidates else None,
                "action": "SKIP"}
    if action == "DISCARD":
        return {"layer": "L0", "skipped": True, "reason": reason}
    if action == "INVALIDATE":
        try:
            client = _qdrant_client()
            old_pts = client.retrieve(collection_name=L0_COLLECTION, ids=[candidates[0]["pid"]])
            if old_pts:
                old_pl = old_pts[0].payload or {}
                old_pl["state"] = "uncertain"
                old_pl["updated"] = _now_cn_iso()
                client.upsert(collection_name=L0_COLLECTION,
                              points=[{"id": candidates[0]["pid"], "vector": [0.0]*1024, "payload": old_pl}])
        except Exception as e:
            print(f"[warn] L0 invalidate old failed: {e}", file=sys.stderr)
        return {"layer": "L0", "skipped": True, "reason": f"invalidate old: {reason}", "action": "INVALIDATE"}

    l0_pid = None  # 先写 Qdrant 拿到 pid，再写 Neo4j 关联
    qdrant_ok = False
    try:
        client = _qdrant_client()
        qdrant_ensure_collection(L0_COLLECTION)

        # L0 向量用 scene_summary 编码（全文也存 payload 里供 BM25 用）
        text = scene_summary
        vecs = embed(text)
        if vecs:
            vec = vecs[0] if isinstance(vecs[0], list) else vecs
            import uuid as _uuid
            l0_pid = str(_uuid.uuid4())
            payload = {
                "summary": scene_summary,
                "memory_type": "l0_conversation",
                "layer": "L0",
                "source": source,
                "ts": _now_cn_iso(),
                # 🔧 2026-09-09 新增：跨层级联追溯字段（与 L1 反向关联）
                "linked_l1_pids": list(linked_l1_pids) if linked_l1_pids else [],
            }
            qdrant_upsert_point(client, L0_COLLECTION, l0_pid, vec, payload)
            qdrant_ok = True
    except Exception as e:
        print(f"[warn] L0 qdrant write failed: {e}", file=sys.stderr)

    # Neo4j: L0Conversation 节点 + 反向关联 L1（如果 L1 已写入）
    neo4j_ok = False
    if l0_pid:
        try:
            driver = _neo4j_driver()
            with driver.session() as session:
                # 1) 主节点
                session.run(
                    """
                    MERGE (l:L0Conversation {l0_id: $l0_id})
                    SET l.source = $source,
                        l.recorded_at = $ts
                    """,
                    l0_id=l0_pid,
                    source=source,
                    ts=_now_cn_str(),
                )

                # 2) 反向关联 L1 KO（按 ko_summary 定位关系，连到 L1 实体节点）
                if l1_kos:
                    for ko in l1_kos:
                        ko_summary = (ko.get("summary") or "").strip()
                        if not ko_summary:
                            continue
                        # 找这条 KO 涉及的主体实体作为锚点
                        entities = ko.get("entities") or []
                        anchor = None
                        for ent in entities:
                            n = (ent.get("name") or "").strip()
                            if n:
                                anchor = n
                                break
                        if not anchor:
                            continue
                        session.run(
                            """
                            MATCH (l:L0Conversation {l0_id: $l0_id})
                            MATCH (a {name: $anchor})
                            WHERE EXISTS {
                                MATCH (a)-[r]->()
                                WHERE r.ko_summary = $ko_summary AND r.status <> 'deleted'
                            }
                            WITH l, a LIMIT 1
                            MERGE (l)-[g:GENERATED]->(a)
                            SET g.ko_summary = $ko_summary,
                                g.updated = $ts
                            """,
                            l0_id=l0_pid,
                            anchor=anchor,
                            ko_summary=ko_summary,
                            ts=_now_cn_str(),
                        )

                # 🔧 2026-09-09 PID 级联追溯：L0 → KO 节点直接连边（不依赖实体）
                if linked_l1_pids:
                    for ko_pid in linked_l1_pids:
                        session.run(
                            """MATCH (l:L0Conversation {l0_id: $l0_id})
                               MATCH (k:KO {pid_str: $ko_pid})
                               MERGE (l)-[r:L0_GENERATED]->(k)
                               SET r.updated = $ts""",
                            l0_id=l0_pid, ko_pid=str(ko_pid), ts=_now_cn_str(),
                        )
            driver.close()
            neo4j_ok = True
        except Exception as e:
            print(f"[warn] L0 neo4j write failed: {e}", file=sys.stderr)

    return {
        "layer": "L0",
        "l0_id": l0_pid,
        "neo4j_ok": neo4j_ok,
        "qdrant_ok": qdrant_ok,
    }


def _neo4j_driver():
    """Neo4j 驱动（每次新建连接，避免长连接线程问题）。"""
    from neo4j import GraphDatabase
    return GraphDatabase.driver(
        NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS),
        notifications_min_severity="OFF",
    )


def write_l2_scenario(scenario, linked_l1_pids=None):
    """写 L2 scenario：Neo4j Scenario 节点 + Qdrant memory_scenario collection。

    失败兜底：每一步异常都不抛异常，记录 ok=False。
    去重逻辑：照搬 L1 的 ANN 三态决策（SKIP / UPDATE / CREATE）。

    Args:
        scenario: scenario dict
        linked_l1_pids: 可选，本场景关联的 L1 PID 列表（用于跨层级联删除追溯）
    """

    title = (scenario.get("title") or "").strip()
    summary = (scenario.get("summary") or "").strip()
    if not title and not summary:
        return {"layer": "L2", "skipped": True, "reason": "缺少 title/summary"}

    scenario = _normalize_time_fields(scenario, source_path=None)
    scenario["layer"] = "L2"
    scenario["type"] = "scenario"

    entities = scenario.get("entities") or []
    scenario_id = title or summary[:50]

    # ---- 0. ANN 去重决策（仿 L1 _rule_decide_action）----
    ann_text = f"{title} {summary}"
    candidates = _ann_find_candidates_in_collection(L2_COLLECTION, ann_text, top_k=5)
    action, reason = _rule_decide_layer_action(scenario.get("state"), candidates, layer="L2", new_text=ann_text)
    if action == "SKIP":
        return {"layer": "L2", "skipped": True, "reason": f"dup: {reason}",
                "scenario_id": scenario_id, "action": "SKIP"}
    if action == "DISCARD":
        return {"layer": "L2", "skipped": True, "reason": reason}
    if action == "INVALIDATE":
        # state=uncertain 有候选 → 标记旧 scenario 为 uncertain
        try:
            client = _qdrant_client()
            old_pts = client.retrieve(collection_name=L2_COLLECTION, ids=[candidates[0]["pid"]])
            if old_pts:
                old_pl = old_pts[0].payload or {}
                old_pl["state"] = "uncertain"
                old_pl["updated"] = _now_cn_iso()
                client.upsert(collection_name=L2_COLLECTION,
                              points=[{"id": candidates[0]["pid"], "vector": [0.0]*1024, "payload": old_pl}])
        except Exception as e:
            print(f"[warn] L2 invalidate old failed: {e}", file=sys.stderr)
        return {"layer": "L2", "skipped": True, "reason": f"invalidate old: {reason}",
                "action": "INVALIDATE"}

    # ---- 1. Neo4j: Scenario 节点 + 实体关联 ----
    neo4j_ok = False
    try:
        driver = _neo4j_driver()
        with driver.session() as session:
            # 主节点
            session.run(
                """
                MERGE (s:Scenario {scenario_id: $sid})
                SET s.title = $title,
                    s.scenario_type = $stype,
                    s.state = $state,
                    s.importance = $imp,
                    s.tags = $tags,
                    s.event_time = $et,
                    s.valid_time = $vt,
                    s.recorded_at = $rec_at,
                    s.source_time = $src_at,
                    s.updated = $ts
                """,
                sid=scenario_id,
                title=title,
                summary=summary,
                stype=scenario.get("type", "event"),
                state=scenario.get("state", "historical"),
                imp=float(scenario.get("importance", 0.7)),
                tags=scenario.get("tags") or [],
                et=str(scenario.get("event_time", {})),
                vt=str(scenario.get("valid_time", {})),
                rec_at=scenario.get("recorded_at", ""),
                src_at=scenario.get("source_time", ""),
                ts=_now_cn_str(),
            )

            # 实体关联（实体已存在则连边，不存在则 MERGE）
            for ent in entities:
                name = (ent.get("name") or "").strip()
                if not name:
                    continue
                label = _safe_label(ent.get("label"))
                session.run(
                    f"""
                    MATCH (s:Scenario {{scenario_id: $sid}})
                    MERGE (e:{label} {{name: $name}})
                    MERGE (s)-[r:INVOLVES]->(e)
                    SET r.updated = $ts
                    """,
                    sid=scenario_id, name=name, ts=_now_cn_str(),
                )

            # 🔧 2026-09-09 PID 级联追溯：从 L1 KO 节点连边到 Scenario
            if linked_l1_pids:
                for ko_pid in linked_l1_pids:
                    session.run(
                        """MATCH (k:KO {pid_str: $ko_pid})
                           MATCH (s:Scenario {scenario_id: $sid})
                           MERGE (k)-[r:BELONGS_TO]->(s)
                           SET r.updated = $ts""",
                        ko_pid=str(ko_pid), sid=scenario_id, ts=_now_cn_str(),
                    )
        driver.close()
        neo4j_ok = True
    except Exception as e:
        print(f"[warn] L2 neo4j write failed: {e}", file=sys.stderr)

    # ---- 2. Qdrant: memory_scenario collection ----
    qdrant_ok = False
    try:
        client = _qdrant_client()
        qdrant_ensure_collection(L2_COLLECTION)

        text = f"{title} {summary} {' '.join(e.get('name','') for e in entities)}".strip()
        vecs = embed(text)
        if vecs:
            vec = vecs[0] if isinstance(vecs[0], list) else vecs
            pid = _gen_pid_layer(scenario_id, "L2")
            payload = {
                "summary": summary or title,
                "title": title,
                "memory_type": "scenario",
                "layer": "L2",
                "scenario_id": scenario_id,
                "scenario_type": scenario.get("type", "event"),
                "state": scenario.get("state", "historical"),
                "entities": [e.get("name", "") for e in entities],
                "tags": scenario.get("tags") or [],
                "importance": float(scenario.get("importance", 0.7)),
                "event_time": scenario.get("event_time") or {},
                "valid_time": scenario.get("valid_time") or {},
                "recorded_at": scenario.get("recorded_at", ""),
                "source_time": scenario.get("source_time", ""),
                "ts": _now_cn_iso(),
                # 🔧 2026-09-09 新增：跨层级联追溯字段（与 L1 反向关联）
                "linked_l1_pids": list(linked_l1_pids) if linked_l1_pids else [],
            }
            qdrant_upsert_point(client, L2_COLLECTION, pid, vec, payload)
            qdrant_ok = True
    except Exception as e:
        print(f"[warn] L2 qdrant write failed: {e}", file=sys.stderr)

    return {
        "layer": "L2",
        "scenario_id": scenario_id,
        "neo4j_ok": neo4j_ok,
        "qdrant_ok": qdrant_ok,
    }


def write_l3_personas(personas, linked_l1_pids=None):
    """写 L3 persona 列表：Neo4j Persona 节点 + Qdrant memory_persona collection。

    Args:
        personas: persona dict 列表
        linked_l1_pids: 可选，所有 persona 共享的 L1 PID 列表（用于跨层级联删除追溯）
    """
    if not personas:
        return {"layer": "L3", "skipped": True, "reason": "personas is empty"}

    results = []
    for p in personas:
        summary = (p.get("summary") or "").strip()
        if not summary:
            results.append({"layer": "L3", "skipped": True, "reason": "缺少 summary"})
            continue

        p = _normalize_time_fields(p, source_path=None)
        p["layer"] = "L3"

        # ---- ANN 去重决策（LLM 语义判断）----
        candidates = _ann_find_candidates_in_collection(L3_COLLECTION, summary, top_k=5)
        action, reason = _rule_decide_layer_action(p.get("state"), candidates, layer="L3", new_text=summary)
        if action == "SKIP":
            results.append({"layer": "L3", "skipped": True, "reason": f"dup: {reason}", "summary": summary[:60], "action": "SKIP"})
            continue
        if action == "DISCARD":
            results.append({"layer": "L3", "skipped": True, "reason": reason, "summary": summary[:60]})
            continue
        if action == "INVALIDATE":
            try:
                client = _qdrant_client()
                old_pts = client.retrieve(collection_name=L3_COLLECTION, ids=[candidates[0]["pid"]])
                if old_pts:
                    old_pl = old_pts[0].payload or {}
                    old_pl["state"] = "uncertain"
                    old_pl["updated"] = _now_cn_iso()
                    client.upsert(collection_name=L3_COLLECTION,
                                  points=[{"id": candidates[0]["pid"], "vector": [0.0]*1024, "payload": old_pl}])
            except Exception as e:
                print(f"[warn] L3 invalidate old failed: {e}", file=sys.stderr)
            results.append({"layer": "L3", "skipped": True, "reason": f"invalidate old: {reason}", "summary": summary[:60]})
            continue

        # ---- 1. Neo4j Persona 节点 ----
        neo4j_ok = False
        try:
            driver = _neo4j_driver()
            with driver.session() as session:
                pid = _gen_pid_layer(summary, "L3")
                session.run(
                    """
                    MERGE (p:Persona {pid_str: $pid_str})
                    SET p.pid = $pid_int,
                        p.persona_type = $ptype,
                        p.state = $state,
                        p.importance = $imp,
                        p.recorded_at = $rec_at,
                        p.source_time = $src_at,
                        p.updated = $ts
                    """,
                    pid_str=str(pid),
                    pid_int=pid if pid < 9223372036854775807 else None,
                    ptype=p.get("type", "fact"),
                    state=p.get("state", "active"),
                    imp=float(p.get("importance", 0.8)),
                    rec_at=p.get("recorded_at", ""),
                    src_at=p.get("source_time", ""),
                    ts=_now_cn_str(),
                )

                # 🔧 2026-09-09 PID 级联追溯：从 L1 KO 节点连边到 Persona
                if linked_l1_pids:
                    for ko_pid in linked_l1_pids:
                        session.run(
                            """MATCH (k:KO {pid_str: $ko_pid})
                               MATCH (p:Persona {pid_str: $p_pid})
                               MERGE (k)-[r:DESCRIBES]->(p)
                               SET r.updated = $ts""",
                            ko_pid=str(ko_pid), p_pid=str(pid), ts=_now_cn_str(),
                        )
            driver.close()
            neo4j_ok = True
        except Exception as e:
            print(f"[warn] L3 neo4j write failed: {e}", file=sys.stderr)

        # ---- 2. Qdrant memory_persona collection ----
        qdrant_ok = False
        try:
            client = _qdrant_client()
            qdrant_ensure_collection(L3_COLLECTION)
            vecs = embed(summary)
            if vecs:
                vec = vecs[0] if isinstance(vecs[0], list) else vecs
                pid = _gen_pid_layer(summary, "L3")
                payload = {
                    "summary": summary,
                    "memory_type": "persona",
                    "layer": "L3",
                    "persona_type": p.get("type", "fact"),
                    "state": p.get("state", "active"),
                    "importance": float(p.get("importance", 0.8)),
                    "tags": p.get("tags") or [],
                    "event_time": p.get("event_time") or {},
                    "valid_time": p.get("valid_time") or {},
                    "recorded_at": p.get("recorded_at", ""),
                    "source_time": p.get("source_time", ""),
                    "ts": _now_cn_iso(),
                    # 🔧 2026-09-09 新增：跨层级联追溯字段（与 L1 反向关联）
                    "linked_l1_pids": list(linked_l1_pids) if linked_l1_pids else [],
                }
                qdrant_upsert_point(client, L3_COLLECTION, pid, vec, payload)
                qdrant_ok = True
        except Exception as e:
            print(f"[warn] L3 qdrant write failed: {e}", file=sys.stderr)

        results.append({
            "layer": "L3",
            "summary": summary[:60],
            "neo4j_ok": neo4j_ok,
            "qdrant_ok": qdrant_ok,
        })

    return {"layer": "L3", "results": results}


def write_4layer(payload):
    """4 层记忆写入入口。

    payload 格式：
    {
      "l0": {"scene_summary": "...", "source": "..."},
      "l1": {"kos": [...]},
      "l2": {"scenario": {...} | null},
      "l3": {"persona": [...] | []}
    }

    兼容老格式：
      - 直接传 [...] 数组 → 当 L1
      - {"kos": [...]} → 当 L1（自动从 scene_summary 派生 L0）
    """
    # 兼容老格式
    if isinstance(payload, list):
        payload = {"l1": {"kos": payload}}
    elif isinstance(payload, dict) and "kos" in payload and "l1" not in payload:
        # 有 kos 但无 l1：老格式，保留已有的 l0，只补充 l1
        existing_l0 = payload.get("l0") or {}
        payload = {
            "l0": {
                "scene_summary": existing_l0.get("scene_summary") or payload.get("scene_summary") or "",
                "source": existing_l0.get("source") or payload.get("source") or "",
            },
            "l1": {"kos": payload["kos"]},
        }

    # 拆 4 层（l0/l1/l2/l3 都允许为空）
    l0 = payload.get("l0") or {}
    l1_block = payload.get("l1") or {}
    l1_kos = l1_block.get("kos") or [] if isinstance(l1_block, dict) else []
    l2_block = payload.get("l2") or {}
    l2_scenario = l2_block.get("scenario") if isinstance(l2_block, dict) else None
    l3_block = payload.get("l3") or {}
    l3_personas = l3_block.get("persona") or [] if isinstance(l3_block, dict) else []

    report = {"l0": None, "l1": None, "l2": None, "l3": None}
    # 🔧 2026-09-09 PID 级联追溯改造：L1 先写拿到 PID，L0/L2/L3 拿这些 PID 写入
    l1_pids = []  # 收集本批 L1 的 PID，传给 L0/L2/L3
    l1_report = {}

    # 1) L1 先写（拿到 PID，供 L0/L2/L3 反向关联用）
    if l1_kos:
        try:
            l1_result = write_kos_v5_return_pids(l1_kos)
            l1_pids = l1_result.get("pids", [])
            l1_report = l1_result.get("report", {})
            report["l1"] = l1_result
        except Exception as e:
            report["l1"] = {"error": str(e)}

    # 2) L0 带 linked_l1_pids 写入（同时传 l1_kos 用于 Neo4j GENERATED 边）
    if l0 and l0.get("scene_summary"):
        report["l0"] = write_l0_conversation(l0, l1_kos=l1_kos, linked_l1_pids=l1_pids)

    # 3) L2 带 linked_l1_pids 写入
    if l2_scenario:
        report["l2"] = write_l2_scenario(l2_scenario, linked_l1_pids=l1_pids)

    # 4) L3 带 linked_l1_pids 写入
    if l3_personas:
        report["l3"] = write_l3_personas(l3_personas, linked_l1_pids=l1_pids)

    return report


# ============================================================

# ============================================================
# 两阶段 delete / update 辅助函数（4 层）
# ============================================================
from datetime import timedelta as _td

ACTION_TOKEN_DIR = Path("/tmp/memory-os-action-tokens")
ACTION_TOKEN_TTL_SEC = 300


def _action_token_path(token):
    return ACTION_TOKEN_DIR / f"{token}.json"


def _gen_action_token():
    import uuid as _uuid
    return str(_uuid.uuid4())


def _save_action_token(token, data):
    ACTION_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    data["expires_at"] = (datetime.now(CN_TZ) + _td(seconds=ACTION_TOKEN_TTL_SEC)).isoformat()
    _action_token_path(token).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _load_action_token(token):
    p = _action_token_path(token)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    exp = data.get("expires_at")
    if exp:
        try:
            if datetime.fromisoformat(exp) < datetime.now(CN_TZ):
                return None
        except Exception:
            return None
    return data


def _qdrant_delete_point(client, collection, pid):
    try:
        # 大整数 PID 字符串转 int，避免 Qdrant 精度溢出
        try:
            pid_int = int(pid)
        except (ValueError, TypeError):
            pid_int = pid
        existing = client.retrieve(collection_name=collection, ids=[pid_int])
        if existing:
            from qdrant_client.models import PointIdsList
            client.delete(collection_name=collection, points_selector=PointIdsList(points=[pid_int]))
            return True
    except Exception:
        pass
    return False


def _neo4j_soft_delete_scenario(scenario_id):
    driver = _neo4j_driver()
    deleted = 0
    try:
        with driver.session() as session:
            res = session.run(
                """MATCH (s:Scenario {scenario_id: $sid})
                   SET s.status = 'deleted', s.updated = $ts
                   WITH s
                   OPTIONAL MATCH (s)-[r]->()
                   SET r.status = 'deleted'
                   RETURN count(r) AS cnt""",
                sid=scenario_id, ts=_now_cn_str(),
            )
            rec = res.single()
            deleted = rec.get("cnt", 0) if rec else 0
    finally:
        driver.close()
    return deleted


def _neo4j_soft_delete_persona(summary):
    driver = _neo4j_driver()
    deleted = 0
    try:
        with driver.session() as session:
            session.run(
                """MATCH (p:Persona {summary: $summary})
                   SET p.status = 'deleted', p.updated = $ts""",
                summary=summary, ts=_now_cn_str(),
            )
            deleted = 1
    finally:
        driver.close()
    return deleted


def _neo4j_soft_delete_l0(l0_id):
    driver = _neo4j_driver()
    deleted = 0
    try:
        with driver.session() as session:
            res = session.run(
                """MATCH (l:L0Conversation {l0_id: $l0_id})
                   OPTIONAL MATCH (l)-[r:GENERATED]->()
                   SET r.status = 'deleted'
                   WITH l
                   DETACH DELETE l
                   RETURN 1 AS cnt""",
                l0_id=l0_id,
            )
            rec = res.single()
            deleted = rec.get("cnt", 0) if rec else 0
    finally:
        driver.close()
    return deleted


# ============================================================
# 🔧 2026-09-09 PID 级联删除：一条 L1 PID 带走所有 4 层关联
# ============================================================

# L1 按 type 分到多个 collection，列在这里反查
L1_COLLECTIONS = ["memory_fact", "memory_concept", "memory_event", "memory_preference",
                  "memory_routine", "memory_goal", "memory_decision", "memory_experience"]


def delete_by_pid_cascade(l1_pid):
    """通过 L1 PID 反查所有层关联，一次清干净。

    流程：
    1. 反查 L0/L2/L3 payload里 linked_l1_pids 是否包含这个 PID
       → 拿到 l0_ids / l2_pids / l3_pids
    2. Qdrant: 删 L0 + L1 + L2 + L3 point
    3. Neo4j: DETACH DELETE 关联 (:KO) / (:Scenario) / (:Persona) / (:L0Conversation)

    Returns:
        {"deleted": {"l0": n, "l1": n, "l2": n, "l3": n, "ko": n},
         "l0_ids": [...], "l2_pids": [...], "l3_pids": [...]}
    """
    deleted = {"l0": 0, "l1": 0, "l2": 0, "l3": 0, "ko": 0}
    result = {"deleted": deleted, "l0_ids": [], "l2_pids": [], "l3_pids": [], "l1_collection": None}

    # PID 转 int（Qdrant PID 是大整数）
    try:
        l1_pid_int = int(l1_pid)
    except (ValueError, TypeError):
        return {"error": f"L1 PID 不是合法整数: {l1_pid}", "deleted": deleted}

    # ---- 1. 反查 L0/L2/L3 payload ----
    l0_ids = set()
    l2_pids = set()
    l3_pids = set()
    l1_collection = None
    l1_payload = None

    try:
        client = _qdrant_client()
        # 找 L1（所有可能的 collection）
        for coll in L1_COLLECTIONS:
            try:
                pts = client.retrieve(collection_name=coll, ids=[l1_pid_int])
            except Exception:
                continue
            if pts:
                l1_payload = pts[0].payload or {}
                l1_collection = coll
                deleted["l1"] = 1
                break

        if l1_payload is None:
            return {"error": f"L1 PID 在 Qdrant 里查不到: {l1_pid}", "deleted": deleted}

        result["l1_collection"] = l1_collection

        # 反查 L2
        try:
            from qdrant_client.models import Filter as QF, FieldCondition, MatchValue
            f = QF(must=[FieldCondition(key="linked_l1_pids", match=MatchValue(value=str(l1_pid)))])
            l2_hits = client.scroll(collection_name=L2_COLLECTION, scroll_filter=f, limit=100, with_payload=True, with_vectors=False)[0]
            for p in l2_hits:
                l2_pids.add(p.id)
        except Exception as e:
            print(f"[warn] cascade L2 lookup failed: {e}", file=sys.stderr)

        # 反查 L3
        try:
            from qdrant_client.models import Filter as QF, FieldCondition, MatchValue
            f = QF(must=[FieldCondition(key="linked_l1_pids", match=MatchValue(value=str(l1_pid)))])
            l3_hits = client.scroll(collection_name=L3_COLLECTION, scroll_filter=f, limit=100, with_payload=True, with_vectors=False)[0]
            for p in l3_hits:
                l3_pids.add(p.id)
        except Exception as e:
            print(f"[warn] cascade L3 lookup failed: {e}", file=sys.stderr)

        # 反查 L0
        try:
            from qdrant_client.models import Filter as QF, FieldCondition, MatchValue
            f = QF(must=[FieldCondition(key="linked_l1_pids", match=MatchValue(value=str(l1_pid)))])
            l0_hits = client.scroll(collection_name=L0_COLLECTION, scroll_filter=f, limit=100, with_payload=True, with_vectors=False)[0]
            for p in l0_hits:
                l0_ids.add(str(p.id))
        except Exception as e:
            print(f"[warn] cascade L0 lookup failed: {e}", file=sys.stderr)

    except Exception as e:
        return {"error": f"反查失败: {e}", "deleted": deleted}

    # ---- 2. 删 Qdrant L2/L3/L0 point ----
    try:
        client = _qdrant_client()
        from qdrant_client.models import PointIdsList

        if l2_pids:
            try:
                client.delete(collection_name=L2_COLLECTION, points_selector=PointIdsList(points=list(l2_pids)))
                deleted["l2"] = len(l2_pids)
            except Exception as e:
                print(f"[warn] delete L2 qdrant failed: {e}", file=sys.stderr)

        if l3_pids:
            try:
                client.delete(collection_name=L3_COLLECTION, points_selector=PointIdsList(points=list(l3_pids)))
                deleted["l3"] = len(l3_pids)
            except Exception as e:
                print(f"[warn] delete L3 qdrant failed: {e}", file=sys.stderr)

        if l0_ids:
            # L0 PID 可能是 UUID 字符串或 int，统一传到 Qdrant 让它自己识别
            l0_delete_ids = [int(x) if str(x).isdigit() else str(x) for x in l0_ids]
            try:
                client.delete(collection_name=L0_COLLECTION, points_selector=PointIdsList(points=l0_delete_ids))
                deleted["l0"] = len(l0_delete_ids)
            except Exception as e:
                print(f"[warn] delete L0 qdrant failed: {e}", file=sys.stderr)

        # 删 L1 自己（最后一步，避免中间状态被查询到）
        try:
            client.delete(collection_name=l1_collection, points_selector=PointIdsList(points=[l1_pid_int]))
            deleted["l1"] = 1
        except Exception as e:
            print(f"[warn] delete L1 qdrant failed: {e}", file=sys.stderr)

    except Exception as e:
        print(f"[warn] qdrant cascade delete failed: {e}", file=sys.stderr)

    # ---- 3. 删 Neo4j 节点（DETACH DELETE 带走所有边）----
    try:
        driver = _neo4j_driver()
        with driver.session() as session:
            # 删 L2 Scenario 节点
            for l2_pid in l2_pids:
                session.run(
                    """MATCH (s:Scenario {scenario_id: $sid})
                       DETACH DELETE s""",
                    sid=str(l2_pid),
                )
            # 删 L3 Persona 节点
            for l3_pid in l3_pids:
                session.run(
                    """MATCH (p:Persona {pid: $pid})
                       DETACH DELETE p""",
                    pid=str(l3_pid),
                )
            # 删 L0 L0Conversation 节点
            for l0_id in l0_ids:
                session.run(
                    """MATCH (l:L0Conversation {l0_id: $l0_id})
                       DETACH DELETE l""",
                    l0_id=str(l0_id),
                )
            # 删 L1 KO 节点（DETACH DELETE 带走 BELONGS_TO / DESCRIBES / L0_GENERATED 边）
            res = session.run(
                """MATCH (k:KO {pid_str: $pid_str})
                   DETACH DELETE k
                   RETURN count(k) AS cnt""",
                pid_str=str(l1_pid),
            )
            rec = res.single()
            deleted["ko"] = rec.get("cnt", 0) if rec else 0
        driver.close()
    except Exception as e:
        print(f"[warn] neo4j cascade delete failed: {e}", file=sys.stderr)

    result["l0_ids"] = list(l0_ids)
    result["l2_pids"] = list(l2_pids)
    result["l3_pids"] = list(l3_pids)
    return result


def confirm_delete_4layer(token, selected_pids=None):
    data = _load_action_token(token)
    if not data or data.get("action") != "delete":
        return {"error": "token 无效或已过期", "deleted": {}}
    candidates = data.get("candidates", [])
    if selected_pids is not None:
        candidates = [c for c in candidates if c.get("pid") in selected_pids]
    deleted = {"l0": 0, "l1": 0, "l2": 0, "l3": 0}
    try:
        client = _qdrant_client()
        for cand in candidates:
            layer = cand.get("layer")
            pid = cand.get("pid")
            if layer == "L0":
                if _qdrant_delete_point(client, L0_COLLECTION, pid):
                    deleted["l0"] += 1
                _neo4j_soft_delete_l0(pid)
            elif layer == "L2":
                if _qdrant_delete_point(client, L2_COLLECTION, pid):
                    deleted["l2"] += 1
                scenario_id = cand.get("scenario_id") or pid
                _neo4j_soft_delete_scenario(scenario_id)
            elif layer == "L3":
                if _qdrant_delete_point(client, L3_COLLECTION, pid):
                    deleted["l3"] += 1
                summary = cand.get("summary") or ""
                if summary:
                    _neo4j_soft_delete_persona(summary)
            elif layer == "L1":
                pid = cand.get("pid") or cand.get("_qdrant_pid")
                coll = cand.get("collection", "memory_fact")
                if pid:
                    if _qdrant_delete_point(client, coll, pid):
                        deleted["l1"] += 1
    finally:
        try: _action_token_path(token).unlink()
        except Exception: pass
    return {"deleted": deleted, "n_candidates": len(candidates)}


def confirm_update_4layer(token, selected_pids=None, new_memory=None):
    data = _load_action_token(token)
    if not data or data.get("action") != "update":
        return {"error": "token 无效或已过期"}
    if new_memory is None:
        new_memory = data.get("new_memory") or {}
    target_pid = data.get("target_pid")
    target_layer = data.get("target_layer")
    target_collection = data.get("target_collection")
    if not target_pid or not target_layer:
        return {"error": "token 缺少 target_pid/target_layer"}
    updated = {}
    try:
        if target_layer == "L1":
            l1_kos = (new_memory.get("l1") or {}).get("kos") or []
            if l1_kos:
                from process_dream import _execute_update_v5
                client = _qdrant_client()
                report = {"update": 0, "qdrant_written": 0, "qdrant_updated": 0, "neo4j": {"entities": 0, "relations": 0}}
                _execute_update_v5(
                    l1_kos[0], l1_kos[0].get("memory_type", "fact"),
                    target_collection, client, target_pid, report
                )
                updated["l1"] = report
        elif target_layer == "L2":
            scenario = (new_memory.get("l2") or {}).get("scenario")
            if scenario:
                result = write_l2_scenario(scenario)
                if result.get("neo4j_ok") and result.get("qdrant_ok"):
                    client = _qdrant_client()
                    _qdrant_delete_point(client, L2_COLLECTION, target_pid)
                updated["l2"] = result
        elif target_layer == "L3":
            personas = (new_memory.get("l3") or {}).get("persona") or []
            if personas:
                result = write_l3_personas(personas)
                if any(p.get("qdrant_ok") for p in result.get("results", [])):
                    client = _qdrant_client()
                    _qdrant_delete_point(client, L3_COLLECTION, target_pid)
                updated["l3"] = result
        elif target_layer == "L0":
            l0 = new_memory.get("l0")
            if l0:
                result = write_l0_conversation(l0, l1_kos=(new_memory.get("l1") or {}).get("kos"))
                if result.get("neo4j_ok") and result.get("qdrant_ok"):
                    client = _qdrant_client()
                    _qdrant_delete_point(client, L0_COLLECTION, target_pid)
                updated["l0"] = result
    finally:
        try: _action_token_path(token).unlink()
        except Exception: pass
    return {"updated": updated, "target_layer": target_layer}


def confirm_action(token, selected_pids=None, new_memory=None):
    data = _load_action_token(token)
    if not data:
        return {"error": "token 无效或已过期"}
    action = data.get("action")
    if action == "delete":
        return confirm_delete_4layer(token, selected_pids=selected_pids)
    elif action == "update":
        return confirm_update_4layer(token, selected_pids=selected_pids, new_memory=new_memory)
    return {"error": f"未知 action: {action}"}

# CLI
# ============================================================
if __name__ == "__main__":
    import argparse as _ap
    parser = _ap.ArgumentParser()
    parser.add_argument("command", choices=["ingest", "delete", "update", "confirm"])
    parser.add_argument("--file", help="4 层 JSON 文件路径")
    parser.add_argument("--query", help="召回 query")
    parser.add_argument("--token", help="confirm 阶段的 token")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--layer", choices=["L0","L1","L2","L3"], default=None)
    parser.add_argument("--selected-pids", default=None, help="逗号分隔的 pid 列表")
    parser.add_argument("--direct-pid", default=None, help="直接删除的 PID（跳过召回）")
    parser.add_argument("--direct-collection", default=None, help="direct_pid 所在的 collection")
    parser.add_argument("--direct-layer", choices=["L0","L1","L2","L3"], default=None, help="direct_pid 的层")
    parser.add_argument("--target-pid", default=None, help="直接指定要更新的 PID")
    parser.add_argument("--target-collection", default=None, help="target_pid 所在的 collection")
    parser.add_argument("--target-layer", choices=["L0","L1","L2","L3"], default=None, help="target_pid 的层")
    # 🔧 2026-09-09 PID 级联删除快捷模式参数
    parser.add_argument("--pid", default=None, help="L1 PID（与 --cascade 搭配使用，一次级联删 4 层）")
    parser.add_argument("--cascade", action="store_true", help="PID 级联删除模式")
    args = parser.parse_args()

    if args.command == "ingest":
        if not args.file:
            print(json.dumps({"error": "ingest 需要 --file"}))
            sys.exit(1)
        with open(args.file, encoding="utf-8") as f:
            payload = json.load(f)
        print(json.dumps(write_4layer(payload), ensure_ascii=False, indent=2))

    elif args.command == "delete":
        # 🔧 2026-09-09 快捷模式：--pid + --cascade → 调用 delete_by_pid_cascade() 一次删 4 层
        if args.pid and args.cascade:
            result = delete_by_pid_cascade(args.pid)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(0)
        # 快捷模式：direct_pid + direct_collection + direct_layer → 直接删
        if args.direct_pid and args.direct_collection and args.direct_layer:
            client = _qdrant_client()
            layer = str(args.direct_layer)
            coll = str(args.direct_collection)
            pid = str(args.direct_pid)
            deleted = {"l0": 0, "l1": 0, "l2": 0, "l3": 0}
            try:
                if layer == "L0":
                    if _qdrant_delete_point(client, L0_COLLECTION, pid): deleted["l0"] = 1
                    _neo4j_soft_delete_l0(pid)
                elif layer == "L1":
                    if _qdrant_delete_point(client, coll, pid): deleted["l1"] = 1
                elif layer == "L2":
                    if _qdrant_delete_point(client, L2_COLLECTION, pid): deleted["l2"] = 1
                    _neo4j_soft_delete_scenario(pid)
                elif layer == "L3":
                    if _qdrant_delete_point(client, L3_COLLECTION, pid): deleted["l3"] = 1
                    _neo4j_soft_delete_persona(pid)
            except Exception as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)
            print(json.dumps({"deleted": deleted}, ensure_ascii=False))
            sys.exit(0)
        if not args.query:
            print(json.dumps({"error": "delete 需要 --query"}))
            sys.exit(1)
        from recall_4layer import recall_4layer
        # 🔧 2026-08-29 修复：两层召回策略，确保全链路删除
        # 策略1：直接用 query 查所有层
        layers = ["L3","L2","L1","L0"]
        if args.layer:
            layers = [args.layer]
        result = recall_4layer(args.query, top_k=args.top_k, layers=layers)
        candidates = []
        # 🔧 2026-09-09 修复：recall_4layer v2 把 L3/L2 改名为 _aux_persona/_aux_scenario，避免上层误注入
        for m in result.get("_aux_persona", []) or []:
            candidates.append({**m, "layer":"L3", "collection":L3_COLLECTION})
        for m in result.get("_aux_scenario", []) or []:
            candidates.append({**m, "layer":"L2", "collection":L2_COLLECTION,
                              "scenario_id": m.get("scenario_id") or m.get("title") or ""})
        for m in result.get("atom", []) or []:
            candidates.append({**m, "layer":"L1", "collection":m.get("collection","memory_fact"),
                              "pid": m.get("_qdrant_pid") or m.get("pid")})
        for m in result.get("raw", []) or []:
            candidates.append({**m, "layer":"L0", "collection":L0_COLLECTION,
                              "pid": m.get("_qdrant_pid") or m.get("pid")})
        if not candidates:
            print(json.dumps({"phase":"confirm","action":"delete","candidates":[],
                              "message":"未召回到候选记忆"}))
            sys.exit(0)
        token = _gen_action_token()
        _save_action_token(token, {"action":"delete","query":args.query,"candidates":candidates})
        print(json.dumps({"phase":"confirm","action":"delete","token":token,
                          "expires_in_sec":ACTION_TOKEN_TTL_SEC,"candidates":candidates},
                         ensure_ascii=False, indent=2))

    elif args.command == "update":
        # 快捷模式：target_pid + target_collection + target_layer + file → 跳过召回直接更新
        if args.target_pid and args.target_collection and args.target_layer and args.file:
            with open(args.file, encoding="utf-8") as f:
                new_memory = json.load(f)
            fake_token = _gen_action_token()
            _save_action_token(fake_token, {
                "action":"update","query":args.query or "",
                "candidates":[{"pid":str(args.target_pid),"layer":str(args.target_layer),
                               "collection":str(args.target_collection),"summary":""}],
                "target_pid":str(args.target_pid),
                "target_layer":str(args.target_layer),
                "target_collection":str(args.target_collection),
                "new_memory":new_memory,
            })
            print(json.dumps(confirm_update_4layer(fake_token, selected_pids=None, new_memory=new_memory),
                             ensure_ascii=False, indent=2))
            sys.exit(0)
        if not args.query or not args.file:
            print(json.dumps({"error":"update 需要 --query 和 --file"}))
            sys.exit(1)
        from recall_4layer import recall_4layer
        result = recall_4layer(args.query, top_k=args.top_k, layers=["L3","L2","L1","L0"])
        candidates = []
        # 🔧 2026-09-09 修复：recall_4layer v2 把 L3/L2 改名为 _aux_persona/_aux_scenario
        for m in result.get("_aux_persona", []) or []:
            candidates.append({**m, "layer":"L3", "collection":L3_COLLECTION})
        for m in result.get("_aux_scenario", []) or []:
            candidates.append({**m, "layer":"L2", "collection":L2_COLLECTION,
                              "scenario_id": m.get("title") or m.get("summary","")[:50]})
        for m in result.get("atom", []) or []:
            candidates.append({**m, "layer":"L1", "collection":"memory_fact"})
        for m in result.get("raw", []) or []:
            candidates.append({**m, "layer":"L0", "collection":L0_COLLECTION})
        if not candidates:
            print(json.dumps({"phase":"confirm","action":"update","candidates":[],
                              "message":"未召回到候选记忆"}))
            sys.exit(0)
        # 快捷模式：传了 target_pid + target_collection + target_layer + file，直接更新
        if args.target_pid and args.target_collection and args.target_layer and args.file:
            with open(args.file, encoding="utf-8") as f:
                new_memory = json.load(f)
            fake_token = _gen_action_token()
            _save_action_token(fake_token, {
                "action":"update","query":args.query,
                "candidates":[{"pid":args.target_pid,"layer":args.target_layer,
                               "collection":args.target_collection,"summary":""}],
                "target_pid":str(args.target_pid),
                "target_layer":str(args.target_layer),
                "target_collection":str(args.target_collection),
                "new_memory":new_memory,
            })
            print(json.dumps(confirm_update_4layer(fake_token, selected_pids=None, new_memory=new_memory),
                             ensure_ascii=False, indent=2))
            sys.exit(0)
        target = candidates[0]
        with open(args.file, encoding="utf-8") as f:
            new_memory = json.load(f)
        token = _gen_action_token()
        _save_action_token(token, {
            "action":"update","query":args.query,"candidates":candidates,
            "target_pid": str(target.get("pid") or target.get("l0_id") or ""),
            "target_layer": target["layer"],
            "target_collection": target.get("collection"),
            "new_memory": new_memory,
        })
        print(json.dumps({"phase":"confirm","action":"update","token":token,
                          "expires_in_sec":ACTION_TOKEN_TTL_SEC,
                          "target":{"pid":target.get("pid") or target.get("l0_id"),
                                    "layer":target["layer"],
                                    "summary":target.get("summary","")[:80]},
                          "candidates":candidates[:5]}, ensure_ascii=False, indent=2))

    elif args.command == "confirm":
        if not args.token:
            print(json.dumps({"error":"confirm 需要 --token"}))
            sys.exit(1)
        selected = None
        if args.selected_pids:
            selected = set(s.strip() for s in args.selected_pids.split(","))
        print(json.dumps(confirm_action(args.token, selected_pids=selected),
                         ensure_ascii=False, indent=2))
