def write_kos(kos):
    """批量写 KO：Neo4j（MERGE 去重）+ Qdrant（PID 指纹去重 + ANN 兜底）。"""
    report = {"neo4j": {"entities": 0, "relations": 0},
              "qdrant": {"written": 0, "updated": 0, "errors": 0}}
    client = None
    try:
        client = _qdrant_client()
    except Exception as e:
        print(f"[warn] qdrant client init failed: {e}", file=sys.stderr)

    for ko in kos:
        # 实体清洗：丢脏实体名（如"外婆记忆召回"被误抽成实体）
        ko, dropped = clean_ko_for_write(ko)
        if dropped:
            continue
        # 强制补齐时间字段（recorded_at / source_time 必填，event_time / valid_time 规范化）
        ko = _normalize_time_fields(ko)
        kotype = ko.get("type") or "fact"
        collection = qdrant_collection_for(kotype)
        if client is not None:
            try:
                qdrant_ensure_collection(collection)
            except Exception as e:
                print(f"[warn] ensure collection {collection}: {e}", file=sys.stderr)
        # Neo4j 写
        try:
            w = neo4j_upsert_ko(ko)
            report["neo4j"]["entities"] += w["entities"]
            report["neo4j"]["relations"] += w["relations"]
        except Exception as e:
            print(f"[warn] neo4j upsert failed: {e}", file=sys.stderr)
        # Qdrant 写
        if client is None:
            continue
        try:
            # 用 recall_fusion.build_qdrant_text 拼进 relations 三元组
            text, imp = build_qdrant_text(ko)
            if not text.strip():
                continue
            vecs = embed(text)
            if not vecs:
                # 【修复 D】embedding 服务不可用 → 记 error + warn，不再静默跳过
                # 此时 Neo4j 已写、Qdrant 未写，两边不一致；必须让老豆在报告里看到
                report["qdrant"]["errors"] += 1
                print(f"[warn] embed failed, Qdrant skipped (Neo4j already written) ko.summary={ko.get('summary', '')[:60]}", file=sys.stderr)
                continue
            vec = vecs[0] if isinstance(vecs[0], list) else vecs
            # PID 指纹：同一 fact → 同 PID，upsert 自然覆盖（修复 #2 静默覆盖）
            fact_parts = sorted({
                (e.get("name") or "").strip() for e in (ko.get("entities") or [])
                if (e.get("name") or "").strip()
            })
            rel_parts = sorted({
                f"{(r.get('subject') or '').strip()}|{r.get('predicate') or ''}|{(r.get('object') or '').strip()}"
                for r in (ko.get("relations") or [])
                if (r.get("subject") or "").strip() and (r.get("object") or "").strip()
            })
            pid_src = "qdrant-v2" + "|" + collection + "|" + "::".join(fact_parts) + "||" + "::".join(rel_parts)
            pid = int(hashlib.md5(pid_src.encode("utf-8")).hexdigest()[:16], 16)

            payload = {
                "summary": ko.get("summary", ""),
                "memory_type": kotype,
                "entities": [e.get("name", "") for e in (ko.get("entities") or [])],
                "tags": ko.get("tags") or [],
                "importance": imp,
                "source": ko.get("source", ""),
                "evidence": ko.get("evidence", ""),
                "relations": ko.get("relations") or [],
                "ts": _now_cn(),
                "event_time": ko.get("event_time") or {},
                "valid_time": ko.get("valid_time") or {},
                "recorded_at": ko.get("recorded_at") or "",
                "source_time": ko.get("source_time") or "",
                "_fact_fingerprint": pid_src,   # 用于 PID 碰撞检测
            }

            # ===== Bug B 修复：用 PID 决定 update / insert =====
            # 逻辑：
            #   1. 先按 PID 检查 point 是否存在（确定性 ID，实体+关系相同→同 PID）
            #   2. PID 命中 → UPDATE（同事实不同表述，自然覆盖）
            #   3. PID 未命中 → ANN 查重判定（可能是撞了不同 fingerprint，也可能真的是新事实）
            #   4. ANN 也未命中 → INSERT
            # 这样修：同 fact 不同表述时，即使 ANN cosine < threshold 也能走 UPDATE，
            # 不会出现「PID 一样但 report 记成 written」的情况
            pid_exists = False
            try:
                existing = client.retrieve(collection_name=collection, ids=[pid])
                if existing:
                    pid_exists = True
                    # 顺手做碰撞检测
                    old_fp = (existing[0].payload or {}).get("_fact_fingerprint", "")
                    if old_fp and old_fp != pid_src:
                        print(f"[warn] PID collision! pid={pid}\n  old_fp={old_fp[:60]}...\n  new_fp={pid_src[:60]}...", file=sys.stderr)
            except Exception as e:
                print(f"[warn] qdrant retrieve failed (pid={pid}): {e}", file=sys.stderr)

            if pid_exists:
                # 路径 1：PID 命中 → UPDATE（同 fact 自然覆盖）
                try:
                    client.upsert(
                        collection_name=collection,
                        points=[PointStruct(id=pid, vector=vec, payload=payload)],
                    )
                    report["qdrant"]["updated"] = report["qdrant"].get("updated", 0) + 1
                except Exception as e:
                    report["qdrant"]["errors"] += 1
                    print(f"[warn] qdrant update failed (pid={pid}): {e}", file=sys.stderr)
            else:
                # 路径 2：PID 未命中 → ANN 查重（兜底，防止纯 ANN 撞库被插成不同 point）
                dup_pids = qdrant_dedup_check(client, collection, vec, DEDUP_THRESHOLD)
                if dup_pids:
                    # ANN 命中但 PID 不存在 → 用 vec 命中点的 PID 替换（罕见，多为轻微表述差异）
                    # 这种情况下保留原 PID（因为 PID 是事实指纹），只 upsert 当前 PID
                    # 不算 updated（事实真的不存在），但也不算纯新（ANN 已存相似）
                    # 折中：走 INSERT + warn
                    print(f"[info] ANN 命中但 PID 未命中（pid={pid}），可能撞了相似 fact: {dup_pids}", file=sys.stderr)
                try:
                    client.upsert(
                        collection_name=collection,
                        points=[PointStruct(id=pid, vector=vec, payload=payload)],
                    )
                    report["qdrant"]["written"] += 1
                except Exception as e:
                    report["qdrant"]["errors"] += 1
                    print(f"[warn] qdrant upsert failed (pid={pid}): {e}", file=sys.stderr)
        except Exception as e:
            report["qdrant"]["errors"] += 1
            print(f"[warn] qdrant upsert failed: {e}", file=sys.stderr)
    return report

