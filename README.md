# Memory OS

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![GitHub Stars](https://img.shields.io/github/stars/KingsdouDD/Memory-OS)](https://github.com/KingsdouDD/Memory-OS/stargazers)
[![Node.js](https://img.shields.io/badge/node-V26%2B-339933?logo=node.js&logoColor=white)](https://nodejs.org/)
[![Python](https://img.shields.io/badge/python-3.14-3776ab?logo=python&logoColor=white)](https://www.python.org/)

> **Neo4j + Qdrant hybrid long-term memory system** with a 4-layer architecture (L0/L1/L2/L3), giving AI Agents persistent contextual memory.
>
> Proactive recall · Associative expansion · Two-phase safe updates · Status-based routing.

---

## Elevator Pitch

Turn AI "memory" from a vague context window into a **precisely writable, semantically searchable, relationship-reasoning, proactively associative** long-term memory system.

---

## Core Features

### 🧠 4-Layer Memory Hierarchy

| Layer | Name | Content | Recall Priority |
|-------|------|---------|----------------|
| **L3** | Persona | Cross-scenario stable traits (personality, habits, preferences, relationships) | 🔴 Highest |
| **L2** | Scenario | Complete historical scenes (events, projects, relationships) | 🟠 High |
| **L1** | Atom | Smallest independent knowledge units (fact / event / preference / routine / goal …) | 🟡 Medium |
| **L0** | Raw Dialog | Original conversation transcript (fallback evidence) | ⚪ Lowest |

Higher layers allow less inference. L1 must be independently readable. L2 must recover historical scenes. L3 must be cross-scenario stable.

### 🔗 Hybrid Recall Engine

- **Vector search** (Qdrant ANN) × **Full-text search** (BM25 + jieba) × **Knowledge graph** (Neo4j) — all three run in parallel
- **RRF fusion** + **importance weighting** (0.5×~1.5×) + **temporal decay** (180-day half-life) + **graph hit boost** (×1.3)
- **Associative Expansion**: Neo4j multi-hop expansion → concat expansion vector → Qdrant associative candidates → comprehensive scoring
- **One Reranker call** per query (reduced from 2 calls/query → 1)

### 🏃 Proactive Memory Injection

- **Hook-triggered recall**: `before_prompt_build` fires before every LLM call
- **Gate filtering** (`recall_gate.py`): length < 7 chars / > 300 chars / pure emotional words / profanity / repetitive chars → skip
- **Same-session query deduplication**: md5 cache prevents redundant recalls
- **Injection format**: wrapped as "【以下是你和用户之间的共同记忆】", guiding the LLM to recall naturally rather than cite external data

### 🔄 Flexible Memory Management

- **6 MCP tools**: ingest / recall / update / delete / health / **extract_runtime**
- **Two-phase update/delete** (token protection, 30-min TTL, stored in HOME dir) + **shortcut mode** (direct PID)
- **Status-based routing**:
  - `completed` → write to Memory-OS (permanent)
  - `ongoing / stalled` → write to runtime temp files (per-agent files + incremental append)

### 🛠 Local-First / High Performance

- Embedding / Reranker run on **local GGUF models** (Metal-accelerated), no cloud API dependency
- Embed / Reranker daemon **spawns on demand**: `subprocess.Popen`拉起（`start_new_session=False` — child dies when parent exits), no zombie daemons
- 11-item startup self-check moved to **background execution** (1-second delay), no longer blocks plugin load (latency: 60s+ → 0)

---

## Architecture

```
User Input
   │
   ▼
┌──────────────────────────────────────┐
│  Hook Gate（recall_gate.py）           │  Length / emotion / profanity filter
│  Text < 7 chars / > 300 chars → skip  │
│  Pure filler（嗯/好的/ok/继续） → skip │
└──────────────────────────────────────┘
   │ Pass
   ▼
┌──────────────────────────────────────┐
│  4-Layer Recall（recall_4layer.py）   │
│                                      │
│  Step 0: query 向量化                │
│  Step 1: L3 recall（persona, 0.62, top 20）│  → 路由信号: entities
│  Step 2: L2 recall（scenario, 0.62, top 20）│  → 路由信号: scenario_ids
│  Step 2.5: jieba entity 补充          │
│  Step 3: Neo4j graph (多跳联想)       │  → entities 给候选池加分
│  Step 4: 向量召回 (Qdrant 跨层, 0.62)│  → 唯一主召回路径（无 Path A/B）
│         BM25 (memory_l0 only, 旁路)   │  → 不进主输出
│  Step 6: 融合重排 + Reranker × 1     │
│         final_score = rerank×0.6 + overlap×0.4 │
│         ❌ 删除了 0.55 rerank score 硬过滤
│  → 取 top 5 → PID 关联 L1 → 输出     │
└──────────────────────────────────────┘
   │
   ▼
┌──────────────────────────────────────┐
│  Memory Injection（src/index.js）     │  Prepend to system prompt
│  "【以下是你和用户之间的共同记忆】"     │
└──────────────────────────────────────┘
   │
   ▼
  LLM Output (with injected memories)
```

> **详细架构 / 改动说明 / 召回链路 trace / 性能基线** →
> 见 [`ARCHITECTURE.md`](ARCHITECTURE.md)
>
> 关键改动 (2026-09-07): Step 4 Path A/B 删除改为单一向量召回; Reranker 0.55 硬过滤删除;
> 新增 `scripts/_numpy_compat.py` (Python 3.14 + numpy 2.0+ 兼容).

---

## Repository Structure

```
memory-os/                          # ← This repo (runtime + docs + config)
├── memory-os-plugin/               #    OpenClaw plugin source
│   ├── src/index.js                #      Plugin entry: Hook + 6 MCP tools
│   ├── scripts/                    #      Core scripts (recall / write / daemons)
│   ├── prompts/                    #      LLM extraction specs
│   ├── openclaw.plugin.json
│   ├── README.md                   #      Plugin-specific docs (hooks / impl details)
│   └── README_recall.md            #      Recall flow deep-dive (6 Steps)
├── models/                         # Local GGUF models (BGE-M3 / Qwen3-Reranker)
├── neo4j/                          # Neo4j data directory
├── qdrant/                         # Qdrant data directory
├── venv/                           # Python virtual environment
├── tokens/                         # update/delete tokens (TTL 30 min)
├── config/                         # Neo4j / Qdrant config
├── logs/                           # Runtime logs
├── dream-cron.sh                   # dream 摄取定时任务
├── README.md                       # ← You are here (main page)
├── .gitattributes
└── .gitignore
```

---

## 6 MCP Tools

| Tool | Purpose | Call Example |
|------|---------|--------------|
| `memory_os_ingest` | Store permanent memory (4-layer JSON) | `memory_os_ingest({ memory_json: '{"l0":...,"l1":{"kos":[...]},"l2":...,"l3":...}' })` |
| `memory_os_recall` | Query memories (4-layer fusion) | `memory_os_recall({ query: "...", top_k: 5, layers: "L3,L2,L1" })` |
| `memory_os_update` | Update memory (append, not overwrite) | `memory_os_update({ target_pid: "...", target_layer: "L3", memory_json: "...", confirm: true })` |
| `memory_os_delete` | Delete memory (physical) | `memory_os_delete({ target_pid: "...", target_layer: "L3", confirm: true })` |
| `memory_os_health` | Service health check | `memory_os_health({ deep: false })` |
| `memory_os_extract_runtime` | Runtime temp memory (status-based routing) | `memory_os_extract_runtime({ memory_json: "...", status: "completed" })` |

> 📖 Detailed API, parameters, all call patterns → see [`memory-os-plugin/README.md`](memory-os-plugin/README.md)

---

## Installation

### 1. Dependencies

| Service | Ports | How to start |
|---------|-------|--------------|
| **Neo4j** | 7474 / 7687 | `brew services start neo4j` |
| **Qdrant** | 6333 / 6334 | `brew services start qdrant` |
| **Embed Daemon** | 8765 | **Auto-spawned** (when port is not listened) |
| **Reranker Daemon** | 8877 | **Auto-spawned** (when port is not listened) |

> **Architecture note (refactored 2026-09-06)**: Embed / Reranker daemons now use `subprocess.Popen` directly (no more launchd). `start_new_session=False` means child dies with parent — no zombie daemons. Neo4j / Qdrant are still managed by brew (they need to stay alive).

### 2. Environment Variables

Create `.env` in `memory-os-plugin/` (or `export` in shell):

```bash
# Required: connection config
MEMORY_OS_NEO4J_URI=bolt://127.0.0.1:7687
MEMORY_OS_NEO4J_USER=neo4j
MEMORY_OS_NEO4J_PASSWORD=***           # Neo4j password (required, no default in source)
MEMORY_OS_QDRANT_HOST=127.0.0.1
MEMORY_OS_QDRANT_PORT=6333
MEMORY_OS_EMBEDDING_MODEL=~/.openclaw/workspace/memory-os/models/bge-m3-Q8_0.gguf

# Optional: debug / notifications
MEMORY_OS_HOOK_TRACE_ENABLED=1
QQ_OWNER_OPENID=qqbot:c2c:<your_openid>   # dream-cron.sh notifications; replace with your QQ openid
```

> **Security note (2026-09-06)**: Source code no longer hardcodes Neo4j password or QQ openid — everything goes through env vars.
> - Neo4j password: `MEMORY_OS_NEO4J_PASSWORD` (required; source defaults to `'openclaw'` only as local fallback)
> - QQ openid: `QQ_OWNER_OPENID` (only needed by `dream-cron.sh`)

### 3. Embedding / Reranker Models

| Service | Model | Path |
|---------|-------|------|
| Embed | BGE-M3 (GGUF / MLX) | `~/.openclaw/workspace/memory-os/models/bge-m3-mlx-8bit` |
| Reranker | Qwen3-Reranker-0.6B | `~/.openclaw/workspace/memory-os/models/Qwen3-Reranker-0.6B-4bit` |

---

## Key Design Decisions

### PID Format: Standard UUID

L2/L3 PIDs changed from integer md5 to standard UUID (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`), avoiding Neo4j Long overflow.

### Writes Are Append, Not Overwrite

L0-L3 long text (summary) is appended with ` | ` on update. Neo4j writes new relationships (MERGE), old ones are preserved.

### Deletes Are Physical

Qdrant points are directly deleted. Neo4j nodes are DETACH DELETED. No soft delete.

### Associative Expansion

Starting from seed entities: Neo4j multi-hop expansion (default 2 hops) → concat expansion vector → Qdrant associative search → comprehensive scoring (`overlap × 0.4 + depth_decay × 0.25 + importance × 0.2 + temporal × 0.15`) → single Reranker ranking.

### Token Safety

Update / Delete two-phase tokens are stored in `~/.openclaw/workspace/memory-os/tokens/`, TTL 30 minutes, immune to `/tmp` cleanup.

### Embed / Reranker On-Demand Spawn

`subprocess.Popen` + `start_new_session=False` — child dies when parent exits, no zombie daemons.

### Startup Self-Check Backgrounded

Plugin registration no longer runs 11-item sync self-check (once took 60s+). Now fires 1 second later as fire-and-forget. Call `memory_os_health` tool when needed.

### Runtime Temp Memory Per-Agent

Different channels (QQ / WeChat / Telegram / subagent) write to separate temp files, no cross-pollution. Multiple WeChat accounts are distinguished by `openid`.

---

## Documentation Index

| Document | Content |
|----------|---------|
| [`README.md`](README.md) | This page: main landing, core features, architecture, 6-tool overview, install |
| [`memory-os-plugin/README.md`](memory-os-plugin/README.md) | Plugin-specific: Hook impl, injection format, ops commands |
| [`memory-os-plugin/README_recall.md`](memory-os-plugin/README_recall.md) | Recall flow deep-dive: 6 Steps + Association Expansion + parameter table |
| [`memory-os-plugin/scripts/extract_prompt.md`](memory-os-plugin/scripts/extract_prompt.md) | LLM extraction spec for permanent memory (4-layer) |
| [`memory-os-plugin/prompts/runtime_memory_extract.md`](memory-os-plugin/prompts/runtime_memory_extract.md) | LLM extraction spec for runtime temp memory (snapshot window + per-agent files) |

---

## License

MIT · [KingsdouDD/Memory-OS](https://github.com/KingsdouDD/Memory-OS)
