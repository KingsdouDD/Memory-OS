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
    write_kos_v5, _normalize_time_fields,
)
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


def _rule_decide_layer_action(state, candidates):
    """通用 ANN 三态决策（仿 L1 _rule_decide_action，但适配 L2/L3/L0）。

    返回 (action, reason):
      - "CREATE"  无候选或低分 → 新建
      - "SKIP"    最高分 ≥ DEDUP_THRESHOLD → 完全重复
      - "UPDATE"  0.6 ≤ 最高分 < 0.95 → 相似合并
      - "DISCARD" state=uncertain 无候选 → 丢弃
      - "INVALIDATE" state=uncertain 有候选 → 标记旧记录

    state 默认 "active"；historical/uncertain 同 L1 逻辑。
    """
    state = state or "active"

    if not candidates:
        if state == "uncertain":
            return "DISCARD", "state=uncertain, no candidates, discarded"
        return "CREATE", "no candidates"

    best = max(candidates, key=lambda c: c.get("score", 0))
    score = float(best.get("score", 0))

    if state == "uncertain":
        return "INVALIDATE", f"state=uncertain, score={score:.3f}, invalidate old"
    if state == "historical":
        return "UPDATE", f"state=historical, score={score:.3f}, update old to historical"
    if score >= RecallConfig.DEDUP_THRESHOLD:
        return "SKIP", f"dup score={score:.3f} >= {RecallConfig.DEDUP_THRESHOLD}"
    if score >= RecallConfig.WRITE_ANN_RECALL_THRESHOLD:
        return "UPDATE", f"similar score={score:.3f}, merge supplement"
    return "CREATE", f"new score={score:.3f} < recall threshold"


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


def write_l0_conversation(l0_payload, l1_kos=None):
    """写 L0 原始对话：全文 BM25 召回通道。

    Args:
        l0_payload: {"scene_summary": "...", "source": "..."}
        l1_kos: 对应的 L1 KO 列表（用于反向关联 Neo4j 边）

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

    # ---- 0. ANN 去重决策（仿 L1 三态）----
    candidates = _ann_find_candidates_in_collection(L0_COLLECTION, scene_summary, top_k=5)
    action, reason = _rule_decide_layer_action(l0_payload.get("state"), candidates)
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


def write_l2_scenario(scenario):
    """写 L2 scenario：Neo4j Scenario 节点 + Qdrant memory_scenario collection。

    失败兜底：每一步异常都不抛异常，记录 ok=False。
    去重逻辑：照搬 L1 的 ANN 三态决策（SKIP / UPDATE / CREATE）。
    """
    if not scenario:
        return {"layer": "L2", "skipped": True, "reason": "scenario is null"}

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
    action, reason = _rule_decide_layer_action(scenario.get("state"), candidates)
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


def write_l3_personas(personas):
    """写 L3 persona 列表：Neo4j Persona 节点 + Qdrant memory_persona collection。"""
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

        # ---- ANN 去重决策（仿 L1 三态）----
        candidates = _ann_find_candidates_in_collection(L3_COLLECTION, summary, top_k=5)
        action, reason = _rule_decide_layer_action(p.get("state"), candidates)
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
                    MERGE (p:Persona {pid: $pid})
                    SET p.persona_type = $ptype,
                        p.state = $state,
                        p.importance = $imp,
                        p.recorded_at = $rec_at,
                        p.source_time = $src_at,
                        p.updated = $ts
                    """,
                    pid=pid,
                    ptype=p.get("type", "fact"),
                    state=p.get("state", "active"),
                    imp=float(p.get("importance", 0.8)),
                    rec_at=p.get("recorded_at", ""),
                    src_at=p.get("source_time", ""),
                    ts=_now_cn_str(),
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

    # L0 先写（拿到 l0_id 之后 L1 关联用）
    if l0 and l0.get("scene_summary"):
        report["l0"] = write_l0_conversation(l0, l1_kos=l1_kos)

    # L1 走原路径（最稳）
    if l1_kos:
        try:
            report["l1"] = write_kos_v5(l1_kos)
        except Exception as e:
            report["l1"] = {"error": str(e)}

    # L2
    if l2_scenario:
        report["l2"] = write_l2_scenario(l2_scenario)

    # L3
    if l3_personas:
        report["l3"] = write_l3_personas(l3_personas)

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
    args = parser.parse_args()

    if args.command == "ingest":
        if not args.file:
            print(json.dumps({"error": "ingest 需要 --file"}))
            sys.exit(1)
        with open(args.file, encoding="utf-8") as f:
            payload = json.load(f)
        print(json.dumps(write_4layer(payload), ensure_ascii=False, indent=2))

    elif args.command == "delete":
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
        for m in result["persona"]:
            candidates.append({**m, "layer":"L3", "collection":L3_COLLECTION})
        for m in result["scenario"]:
            candidates.append({**m, "layer":"L2", "collection":L2_COLLECTION,
                              "scenario_id": m.get("scenario_id") or m.get("title") or ""})
        for m in result["atom"]:
            candidates.append({**m, "layer":"L1", "collection":m.get("collection","memory_fact"),
                              "pid": m.get("_qdrant_pid") or m.get("pid")})
        for m in result["raw"]:
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
        for m in result["persona"]:
            candidates.append({**m, "layer":"L3", "collection":L3_COLLECTION})
        for m in result["scenario"]:
            candidates.append({**m, "layer":"L2", "collection":L2_COLLECTION,
                              "scenario_id": m.get("title") or m.get("summary","")[:50]})
        for m in result["atom"]:
            candidates.append({**m, "layer":"L1", "collection":"memory_fact"})
        for m in result["raw"]:
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
