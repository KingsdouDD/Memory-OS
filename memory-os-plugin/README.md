# Memory OS Plugin

> OpenClaw plugin code layer. For architecture overview, tool API summary, and installation guide, see the root [`README.md`](../README.md).

---

## Plugin Structure

```
memory-os-plugin/
├── src/
│   └── index.js              # Plugin entry: Hook registration + 6 MCP tools
├── scripts/
│   ├── recall_4layer.py      # 4-layer fusion recall main script
│   ├── recall_fusion.py      # RRF + kg_verify + Association Expansion
│   ├── recall_config.py      # All recall hyperparameters (parameter hub)
│   ├── recall_gate.py        # Hook gate (length / emotion / profanity filter)
│   ├── write_4layer.py       # 4-layer write / update / delete
│   ├── process_dream.py       # Embedding + Qdrant low-level read/write
│   ├── embed_daemon.py       # Embedding HTTP daemon
│   ├── reranker_daemon.py    # Reranker HTTP daemon
│   ├── bm25_index.py         # BM25 full-text index
│   ├── service_lifecycle.py  # Service lifecycle (spawn / health / http_post)
│   ├── cron_runner.py        # Cron jobs
│   └── *dedup*.py / *clean*.py  # Ops tools
├── prompts/
│   └── runtime_memory_extract.md  # Runtime temp memory extraction spec
├── openclaw.plugin.json
├── package.json
├── README.md                 # ← You are here (plugin-specific)
└── README_recall.md          # Recall flow deep-dive
```

---

## Hook Implementation

The plugin registers two Hooks:

| Hook | Purpose |
|------|---------|
| `before_prompt_build` | Before every LLM call: recall relevant memories and prepend to system prompt |
| `message_received` | Log activity on message receipt (disabled — `message_received` path was redundant with `before_prompt_build`) |

### Hook Gate (recall_gate.py)

| Rule | Behavior |
|------|---------|
| Text < 7 chars | Skip recall |
| Text > 300 chars | Skip recall |
| Pure filler words (嗯/好的/ok/继续) | Skip |
| Profanity | Skip |
| Repetitive chars ("啊啊啊啊啊") | Skip |
| Same-session same query | md5 deduplication |

### Injection Format

```text
【以下是你和用户之间的共同记忆】
这是你们之间真实发生过的往事，回答相关话题时自然想起来用。
请根据当前对话语境，结合记忆来回答
严禁编造、不用"根据记忆"等机械化表达
真实性永远高于"真人感"。

[Recalled memories, one per line]
```

The model is guided to treat memories as "shared memories between us", not external data.

---

## 6 MCP Tools (Implementation)

| Tool | Entry | Python Implementation |
|------|-------|----------------------|
| `memory_os_ingest` | `src/index.js` → `runPython(['ingest', ...], { script: write_4layer.py })` | `scripts/write_4layer.py ingest --file <json>` |
| `memory_os_recall` | `src/index.js` → `runPython(['recall', ...], { script: recall_4layer.py })` | `scripts/recall_4layer.py recall --query ...` |
| `memory_os_update` | Same as ingest / calls `write_4layer.py update` / `confirm` | `scripts/write_4layer.py update / confirm` |
| `memory_os_delete` | Same as update / `delete` / `confirm` | `scripts/write_4layer.py delete / confirm` |
| `memory_os_health` | 4-port lsof check + auto-spawn + optional 11-item selfCheck | Self-implemented in `src/index.js` |
| `memory_os_extract_runtime` | Reads `prompts/runtime_memory_extract.md` + writes `runtime_active_state/<agent>.json` | Calls `write_4layer.py ingest` on write |

### Two-Phase Update / Delete

Update / Delete offer two-phase + shortcut mode:

- **Two-phase**: Phase 1 passes query to retrieve candidates + generate token; Phase 2 carries token to actually update/delete. Token TTL 30 minutes, stored in `~/.openclaw/workspace/memory-os/tokens/`
- **Shortcut mode**: Pass `target_pid` + `target_collection` + `target_layer` directly, skip retrieval

### `memory_os_extract_runtime` Workflow

Status-based routing:

| status | Action |
|--------|--------|
| `completed` | Calls `write_4layer.py ingest` to write to permanent Memory-OS + clears temp file |
| `ongoing` | Incrementally overwrites `runtime_active_state/<agent>.json` (same task overwrites / new task appends) |
| `stalled` | Same as ongoing, but additionally records `blocked_reason` |

Per-agent file separation (no cross-channel pollution):

```
runtime_active_state/qq.json                  # QQ channel
runtime_active_state/telegram.json            # Telegram channel
runtime_active_state/wechat_<openid>.json     # WeChat (distinguished by user openid)
runtime_active_state/<agent>.json            # Other subagents
```

---

## Startup Self-Check (11 Items)

Runs in the **background** (1-second delay) after plugin registration — does not block load. Call `memory_os_health` tool to check on demand.

| Check | Description |
|-------|-------------|
| Python env + version | Python executable |
| `neo4j` / `qdrant_client` / `jieba` packages | Installed + version |
| Key script files | `write_4layer.py` / `recall_4layer.py` / `process_dream.py` |
| Embedding model | GGUF file exists |
| Token directory writable | `~/.openclaw/workspace/memory-os/tokens/` |
| 4 service ports | Neo4j / Qdrant / Embed / Reranker |
| Neo4j bolt auth | Username/password connectivity |
| Qdrant REST API | `GET /readyz` HTTP 200 |

---

## Service Lifecycle (`scripts/service_lifecycle.py`)

Refactored 2026-09-06: switched from launchd to `subprocess.Popen` direct spawn.

| Behavior | Implementation |
|----------|---------------|
| Port not listened | Auto `subprocess.Popen` spawns daemon |
| Wait for ready | Poll `/health` HTTP 200 (up to 90s) |
| Parent exits | `start_new_session=False` makes child die with parent |
| stdout/stderr | Redirected to `/tmp/memory-os-embed.log` / `/tmp/memory-os-reranker.log` |

Neo4j / Qdrant are still managed by brew (need to stay alive, not managed by service_lifecycle).

---

## Ops Commands

```bash
# View hook trace log
cat ~/.openclaw/workspace/memory-os/logs/hook-trace.md

# Recall statistics
python3 scripts/recall_stats.py

# Enable recall debug log
MEMORY_OS_RECALL_DEBUG=1 python3 scripts/recall_4layer.py recall --query "..."

# Neo4j deduplication cleanup
python3 scripts/clean_neo4j_dupes.py
python3 scripts/dedup_cleanup.py
```

---

## Documentation

| Document | Content |
|----------|---------|
| [`../README.md`](../README.md) | Repository main page (architecture, features, 6-tool overview, install) |
| [`README_recall.md`](README_recall.md) | Recall flow deep-dive (6 Steps + Association Expansion + parameter table) |
| [`scripts/extract_prompt.md`](scripts/extract_prompt.md) | LLM extraction spec for permanent memory (4-layer) |
| [`prompts/runtime_memory_extract.md`](prompts/runtime_memory_extract.md) | LLM extraction spec for runtime temp memory |
