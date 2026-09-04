# Memory OS

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Contributors](https://img.shields.io/github/contributors/KingsdouDD/Memory-OS)](https://github.com/KingsdouDD/Memory-OS/graphs/contributors)
[![GitHub Stars](https://img.shields.io/github/stars/KingsdouDD/Memory-OS)](https://github.com/KingsdouDD/Memory-OS/stargazers)
[![GitHub Issues](https://img.shields.io/github/issues/KingsdouDD/Memory-OS)](https://github.com/KingsdouDD/Memory-OS/issues)
[![Node.js](https://img.shields.io/badge/node-V26%2B-339933?logo=node.js&logoColor=white)](https://nodejs.org/)
[![Python](https://img.shields.io/badge/python-3.14-3776ab?logo=python&logoColor=white)](https://www.python.org/)

# Neo4j + Qdrant Long-Term Memory System — OpenClaw Plugin

A 4-layer memory architecture (L0/L1/L2/L3) combining vector search and knowledge graphs, supporting proactive injection, passive recall, and LLM self-reflection writes.

### Memory OS Mechanism

* **Temporal Traceability** — Track the temporal evolution of memories, events, entities, and states, enabling historical reconstruction and time-aware reasoning.
* **Dynamic State Updates** — Events, people, entities, and profile attributes can be updated as their states change over time, rather than being stored as immutable facts.
* **State-Aware Memory** — Distinguishes historical states from current states, allowing the system to maintain evolving representations of the world and user context.
* **Hybrid Memory Retrieval** — Combines Qdrant vector retrieval with Neo4j knowledge graphs for semantic recall, relational reasoning, and structured state tracking.
* **Flexible Model Services** — Supports both local models and configurable API-based LLM providers, allowing the underlying model service to be changed without redesigning the memory architecture.
* **Multi-Dimensional State Compatibility** — Designed to work with multi-dimensional context and state mechanisms, including state-machine-based runtime architectures.
* **LLM Self-Reflection Writes** — Allows the LLM to evaluate, update, merge, or discard memories based on their relevance, temporal state, and contextual significance.


---

## Architecture Overview

```
User Input
   │
   ▼
┌───────────────────────────────────────┐
│         Hook Gate                      │  recall_gate.py
│   Length / emotion / profanity filter  │
└───────────────────────────────────────┘
   │ Pass
   ▼
┌───────────────────────────────────────┐
│         4-Layer Recall                │  recall_4layer.py
│                                       │
│  Step 1-2: L3/L2 High-Confidence     │
│  Step 2.5: Entity Augment (jieba)     │
│  Step 3: Graph PRF (Neo4j 1-hop)     │
│  Step 4: L1 Main Recall (vec+BM25+RRF)│
│  Step 5: Pre-filter (entity overlap)  │
│  Step 6: Association Expansion         │  ← Neo4j multi-hop + Qdrant
│         ↓ Deduplicate                 │
│    Single Reranker Pass               │
│    Final Top-K                        │
└───────────────────────────────────────┘
   │
   ▼
┌───────────────────────────────────────┐
│       Memory Injection                │  src/index.js
│   [Shared Memory] + Natural tone      │
└───────────────────────────────────────┘
   │
   ▼
  LLM Output (with injected memories)
```

---

## 4-Layer Memory Design

| Layer | Name | Qdrant Collection | PID Strategy | Recall Priority |
|-------|------|-------------------|--------------|----------------|
| **L0** | Raw Scene | `memory_l0` | UUID | Lowest (fallback) |
| **L1** | Atomic Memory | `memory_<type>` (8 collections) | md5 fingerprint | Medium |
| **L2** | Scenario | `memory_scenario` | UUID | High |
| **L3** | Long-Term Persona | `memory_persona` | UUID | Highest |

### L1 8 Collections

```
memory_atom / memory_fact / memory_event / memory_experience
memory_preference / memory_routine / memory_concept / memory_relation
```

---

## Directory Structure

```
memory-os-plugin/          # ← OpenClaw plugin root
├── src/
│   └── index.js         # Plugin entry, Hook registration + 4 MCP tools
├── scripts/
│   ├── recall_4layer.py    # 4-layer fusion recall
│   ├── recall_fusion.py     # RRF fusion + graph boost + time decay
│   ├── recall_config.py     # Recall hyperparameter center
│   ├── recall_gate.py       # Hook gate
│   ├── recall_stats.py      # Collection statistics
│   ├── write_4layer.py     # 4-layer write/update/delete (Python CLI)
│   ├── process_dream.py     # Embedding + Qdrant low-level
│   ├── extract_prompt.md    # LLM 4-layer extraction spec
│   ├── embed_daemon.py      # Embedding HTTP daemon (local GGUF)
│   ├── reranker_daemon.py  # Reranker HTTP daemon
│   ├── bm25_index.py        # BM25 full-text index
│   ├── service_lifecycle.py  # Service start/stop management
│   ├── cron_runner.py        # Cron jobs (dream ingestion)
│   └── *dedup*.py / *clean*.py  # DevOps tools
├── config/
├── logs/
├── openclaw.plugin.json
├── package.json
├── README.md                 # ← you are here
├── README_recall.md
└── MEMORY-OS-4LAYER.md

memory-os/                 # ← Local runtime data (separate directory)
├── venv/
├── models/
├── neo4j/
├── qdrant/
├── tokens/                # delete/update token (TTL 30 min)
└── logs/
```

---

## Environment Dependencies

### Required Services

| Service | Port | Start Command |
|---------|------|---------------|
| **Neo4j** | 7474 / 7687 | `brew services start neo4j` |
| **Qdrant** | 6333 / 6334 | `brew services start qdrant` |
| **Embed Daemon** | 8765 | `launchctl kickstart gui/501/com.memoryos.embed-daemon` |
| **Reranker Daemon** | 8877 | `launchctl kickstart gui/501/com.memoryos.reranker` |

> 如需检查服务状态，使用 `memory_os_health` 工具（fast 模式 < 2s，deep 模式 5-30s）。

### Embedding Model

Default: local BGE-M3 (GGUF/MLX, Metal accelerated):

```
~/.openclaw/workspace/memory-os/models/bge-m3-Q8_0.gguf
```

---

## Installation

### 1. Configure Environment Variables

Create `.env` in `memory-os-plugin/`:

```bash
MEMORY_OS_NEO4J_URI=bolt://127.0.0.1:7687
MEMORY_OS_NEO4J_USER=neo4j
MEMORY_OS_NEO4J_PASSWORD=***
MEMORY_OS_QDRANT_HOST=127.0.0.1
MEMORY_OS_QDRANT_PORT=6333
MEMORY_OS_EMBEDDING_MODEL=~/.openclaw/workspace/memory-os/models/bge-m3-Q8_0.gguf
MEMORY_OS_HOOK_TRACE_ENABLED=1
```

### 2. Start Services (optional, plugin auto-starts)

```bash
python3 scripts/service_lifecycle.py start-all
python3 scripts/service_lifecycle.py status
```

> Tip: Plugin auto-detects and starts services. Manual start only when needed.

### 3. Plugin Self-Check

**⚠️ 2026-09-03 重大变更**：自检不再在插件启动时同步阻塞（之前最坏 60s+ 延迟）。

- **启动时**：自检在 1 秒后后台执行，结果只打印到插件日志，不阻塞插件加载
- **按需检查**：用 `memory_os_health` 工具查看服务状态（fast < 2s，deep 5-30s）

```bash
# 在 OpenClaw 中调用（LLM/Agent 视角）
memory_os_health()        # fast：查 4 个端口是否在线
memory_os_health(deep=true)  # deep：跑完整 11 项自检
```

11 项自检内容：

| Check | Description |
|-------|-------------|
| Python env + version | Python executable availability |
| `neo4j` / `qdrant_client` / `jieba` packages | Installed and version |
| Key script files | `write_4layer.py` / `recall_4layer.py` / `process_dream.py` |
| Embedding model file | GGUF file existence |
| Token directory writability | `~/.openclaw/workspace/memory-os/tokens/` |
| 4 service ports | Neo4j / Qdrant / Embed Daemon / Reranker Daemon |
| Neo4j bolt auth | Username/password connectivity |
| Qdrant REST API | `GET /readyz` HTTP 200 |

FAIL item example output:

```
🍊 Memory OS Self-Check
  ✅ Python env              3.11.0
  ✅   pkg: neo4j         5.x.x
  ✅   pkg: qdrant_client   ok
  ✅   pkg: jieba           ok
  ✅   Script: write_4layer.py   exists
  ✅   Embedding model     bge-m3-Q8_0.gguf
  ✅   Token dir         writable
  ❌  Neo4j Service         port 7687 not listening
                              Run: brew services start neo4j
  ✅  Qdrant Service         port 6333 online
  ✅  Embed Daemon           port 8765 online
  ✅  Reranker Daemon        port 8877 online
══════════════════════════════════════════════
  ❌  1 item failed, please fix before use
```

---

## 5 MCP Tools

### `memory_os_ingest` — Store Memories

**Recommended** (4-layer JSON string, avoids MCP nested array flattening):

```json
{
  "memory_json": "{\"l0\":{\"scene_summary\":\"...\",\"source\":\"...\"},\"l1\":{\"kos\":[{\"type\":\"routine\",\"summary\":\"...\",\"state\":\"ongoing\",\"entities\":[{\"name\":\"...\",\"label\":\"Person\"}]}]},\"l3\":{\"persona\":[{\"type\":\"routine\",\"summary\":\"...\",\"state\":\"active\",\"importance\":0.85}]}}"
}
```

**Legacy format** (L1 only):

```json
{
  "kos": [
    {
      "type": "routine",
      "summary": "Someone's daily habit",
      "state": "ongoing",
      "entities": [{"name": "Someone", "label": "Person"}]
    }
  ],
  "source": "WeChat:2026-09-02"
}
```

---

### `memory_os_recall` — Query Memories

```json
{
  "query": "Someone's habits",
  "top_k": 5,
  "include_persona": true,
  "include_scenario": true,
  "layers": "L3,L2,L1,L0"
}
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `query` | string | **required** | Query text |
| `top_k` | integer | 5 | Results per layer |
| `include_persona` | boolean | true | Include L3 |
| `include_scenario` | boolean | true | Include L2 |
| `layers` | string | all open | Manual order, e.g. `"L3,L2"` |

---

### `memory_os_update` — Update Memories

> **Update logic**: Appends new content after old content (L0-L3 long text joined by ` | `). Neo4j writes new relations; old relations kept.

**Shortcut mode (recommended, skip recall)**:

```json
{
  "memory_json": "{\"l3\":{\"persona\":[{\"type\":\"preference\",\"summary\":\"New preference\",\"state\":\"active\",\"importance\":0.85}]}}",
  "target_pid": "<UUID-format PID>",
  "target_collection": "memory_persona",
  "target_layer": "L3",
  "confirm": true
}
```

**Two-phase mode**:

```json
// Phase 1: recall + generate token
{ "query": "...", "memory_json": "...", "confirm": false }
// → returns { phase:"confirm", token:"...", target:{pid,layer,summary}, candidates:[...] }

// Phase 2: update with token
{ "query": "...", "confirm": true, "token": "***" }
```

---

### `memory_os_delete` — Delete Memories

**Shortcut mode (recommended, direct delete)**:

```json
{
  "target_pid": "<UUID-format PID>",
  "target_collection": "memory_persona",
  "target_layer": "L3",
  "confirm": true
}
// → returns { deleted: {l0:0, l1:0, l2:0, l3:1} }
```

**Two-phase mode**:

```json
// Phase 1: recall candidates + generate token
{ "query": "...", "layer": "L3", "confirm": false }
// → returns { phase:"confirm", token:"...", candidates:[...] }

// Phase 2: delete with token
{ "query": "...", "confirm": true, "token": "***", "selected_pids": ["pid1"] }
```

**PID and Collection Quick Ref**:

| Layer | Collection | PID Source |
|-------|-----------|-----------|
| L0 | `memory_l0` | `_qdrant_pid` from recall |
| L1 | 8 collections | `_qdrant_pid` from recall |
| L2 | `memory_scenario` | `pid` from recall (UUID) |
| L3 | `memory_persona` | `pid` from recall (UUID) |

**Token TTL: 30 minutes**, stored in `~/.openclaw/workspace/memory-os/tokens/`

---

### `memory_os_health` — Service Health Check

**⚠️ 2026-09-03 新增**：按需检查服务状态，不在召回路径上自动检查（避免 60s+ 延迟）。

```json
{
  "deep": false
}
```

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `deep` | boolean | false | `true` 跑完整 11 项自检（耗时 5-30s），默认 fast 只查 4 端口（< 2s） |

**Fast 模式返回值示例**（< 2s）：

```json
{
  "mode": "fast",
  "timestamp": "2026-09-03T00:20:00.000Z",
  "ports": [
    { "name": "neo4j",    "port": 7687, "up": true,  "latency_ms": 12, "fix_command": "brew services start neo4j" },
    { "name": "qdrant",   "port": 6333, "up": true,  "latency_ms":  8, "fix_command": "brew services start qdrant" },
    { "name": "embed",    "port": 8765, "up": false, "latency_ms": 15, "fix_command": "launchctl kickstart gui/501/com.memoryos.embed-daemon" },
    { "name": "reranker", "port": 8877, "up": true,  "latency_ms": 10, "fix_command": "launchctl kickstart gui/501/com.memoryos.reranker" }
  ],
  "all_up": false,
  "down": ["embed"]
}
```

**何时调用**：
- 召回明显变慢 / 报错 / 结果为空
- 启动时看到服务异常提示
- 任何想确认服务状态的时候

---

## Auto Hook Injection

Plugin works automatically via OpenClaw Hook — no explicit Agent calls needed:

- `before_prompt_build`: Recall on every LLM call, inject if conditions met
- `message_received`: Triggers on message receipt

**Hook Gate Rules**:

| Rule | Behavior |
|------|----------|
| Text < 5 chars | Skip |
| Text > 300 chars | Split by sentence |
| Pure emotion words (嗯/啊/好的/OK) | Skip |
| Contains profanity | Skip |

---

## Extraction Spec

LLM extracts 4-layer memories per `scripts/extract_prompt.md`.

**Core principles**:

- **L0**: Raw text preserved, no reasoning
- **L1**: Minimum independent knowledge unit, understandable without original context
- **L2**: Complete scenario from multiple L1s, with title and summary
- **L3**: Cross-scenario stable cognition, **prefer to omit rather than fabricate**

---

## Tuning Guide

All parameters centralized in `scripts/recall_config.py`.

### Recall Quality

| Param | Default | Description |
|-------|---------|-------------|
| `MEMORY_OS_RECALL_DEFAULT_TOP_K` | 8 | Final retained count after fusion |
| `MEMORY_OS_VEC_MIN_SCORE` | 0.60 | Vector recall min similarity |
| `MEMORY_OS_GRAPH_DEPTH` | 1 | Graph recall hop count |
| `MEMORY_OS_RRF_K` | 60 | RRF fusion parameter |

### Association Expansion (2026-09-01)

| Param | Default | Description |
|-------|---------|-------------|
| `ASSOC_ENABLED` | 1 | Toggle, 0=off |
| `ASSOC_MAX_HOPS` | 2 | Neo4j max expansion hops |
| `ASSOC_MAX_NEIGHBORS` | 6 | Max neighbors per hop |
| `ASSOC_ACTIVATION_THRESHOLD` | 0.1 | Association activation threshold |
| `ASSOC_DEPTH_DECAY` | 0.5 | Hop depth decay factor |
| `ASSOC_MAX_CANDIDATES` | 20 | Max association candidates |

### Hook Gate

| Param | Default | Description |
|-------|---------|-------------|
| `HOOK_MIN_LEN` | 5 | Min text length to trigger recall |
| `HOOK_MAX_LEN` | 300 | Per-segment trigger cap |

---

## Operations Commands

```bash
# Start all services
python3 scripts/service_lifecycle.py start-all

# Check service status (plugin log)
python3 scripts/service_lifecycle.py status

# Stop all services
python3 scripts/service_lifecycle.py stop-all

# Recall statistics
python3 scripts/recall_stats.py

# Enable recall debug
MEMORY_OS_RECALL_DEBUG=1 python3 scripts/recall_4layer.py recall --query "..."

# View hook trace log
cat ~/.openclaw/workspace/memory-os/logs/hook-trace.md
```

### `memory_os_health` Tool（推荐）

```json
// Fast：< 2s 查 4 端口
{ "tool": "memory_os_health", "params": {} }

// Deep：5-30s 跑完整 11 项
{ "tool": "memory_os_health", "params": { "deep": true } }
```

---

## Documentation Index

| Doc | Content |
|-----|---------|
| `README.md` | Overview, install, tool API, ops |
| `README_recall.md` | Recall flow detail (6 Steps + Association) |
| `MEMORY-OS-4LAYER.md` | 4-layer architecture, data structures, design decisions |

---

## License

MIT
