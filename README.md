# Memory OS

Neo4j + Qdrant 长期记忆系统 — OpenClaw 插件

四层记忆架构（L0/L1/L2/L3），兼顾向量检索与知识图谱，支持主动注入、被动召回、LLM 自我反思写入。

---

## 架构总览

```
用户输入
   │
   ▼
┌─────────────────────────────┐
│      Hook Gate（门控）        │  recall_gate.py
│  长度 / 纯情绪 / 粗口过滤     │
└─────────────────────────────┘
   │ 通过
   ▼
┌─────────────────────────────┐
│      4-Layer Recall         │  recall_4layer.py
│  ┌─────────────────────────┐│
│  │ Step 1-2: L3/L2 召回     ││
│  │ 提取 filter_entities     ││
│  │ Step 2.5: 实体补充       ││
│  │ Step 3: Graph PRF        ││
│  │ Step 4: L1 主召回        ││
│  │ (向量+BM25+Graph RRF)   ││
│  │ Step 5: Pre-filter      ││
│  │ Step 6: Association     ││  ← 联想记忆扩散（2026-09-01）
│  │ (Neo4j多跳扩散+Qdrant) ││
│  └─────────────────────────┘│
│         ↓ 合并去重            │
│    统一一次 Reranker         │
│    Pre-filter + Final Top-K │
└─────────────────────────────┘
   │
   ▼
┌─────────────────────────────┐
│   Memory Injection          │  src/index.js
│   【共同记忆】+ 自然语气      │
└─────────────────────────────┘
   │
   ▼
  LLM 输出（已注入记忆）
```

---

## 四层记忆设计

| 层 | 名称 | 存储 | 用途 |
|----|------|------|------|
| **L0** | 原始场景 | `memory_l0` collection | Scene 原文，证据溯源 |
| **L1** | 原子记忆 | `memory_atom` 等 8 个 collection | 最小独立知识单元（KO） |
| **L2** | 场景记忆 | `memory_scenario` collection | 多个 L1 组成的完整事件/经历 |
| **L3** | 长期认知 | `memory_persona` collection | 跨场景稳定关系/偏好/习惯 |

### L1 的 8 个 Collection

```
memory_atom / memory_fact / memory_event / memory_experience
memory_preference / memory_routine / memory_concept / memory_relation
```

---

## 目录结构

```
memory-os-plugin/          # 
├── src/
│   └── index.js           # 插件入口，Hook 注册 + 4 个 MCP 工具
├── scripts/
│   ├── recall_4layer.py   # 召回主脚本（read）
│   ├── write_4layer.py    # 写入主脚本（write/update/delete）
│   ├── recall_fusion.py   # RRF 融合 + kg_verify + time_decay
│   ├── recall_config.py   # 所有可调参数的"参数中心"
│   ├── recall_gate.py     # Hook 门控（长度/情绪/粗口过滤）
│   ├── process_dream.py   # Embedding + Qdrant 底层读写
│   ├── extract_prompt.md  # LLM 4 层抽取规范（提示词模板）
│   ├── embed_daemon.py    # Embedding HTTP 守护进程（本地模型）
│   ├── reranker_daemon.py # Reranker HTTP 守护进程
│   ├── bm25_index.py      # BM25 全文索引
│   ├── service_lifecycle.py # 服务启停管理
│   ├── cron_runner.py     # 定时任务（梦境入库）
│   └── *dedup*.py / *clean*.py  # 运维工具（去重/清理/审计）
├── config/
│   └── (配置文件)
├── logs/
│   └── (本地运行日志，.gitignore 排除)
├── openclaw.plugin.json   # 插件 manifest
├── package.json
└── README.md

memory-os/                 # ⬇ 本地运行时数据
├── venv/                  # Python 虚拟环境
├── models/                # Embedding 模型（GGUF/MLX）
├── neo4j/                 # Neo4j 数据目录
├── qdrant/                # Qdrant 数据目录
└── logs/                  # 运行日志
```


## 环境依赖

### 必须服务

| 服务 | 端口 | 启动方式 |
|------|------|---------|
| **Neo4j** | 7474 / 7687 | `brew services start neo4j` |
| **Qdrant** | 6333 / 6334 | `brew services start qdrant` |
| **Embed Daemon** | 8765 | `launchctl kickstart gui/501/com.memoryos.embed-daemon` |
| **Reranker Daemon** | 8877 | `launchctl kickstart gui/501/com.memoryos.reranker` |

> 插件启动时会自动检测服务状态，未启动时自动拉起（通过 `brew services` 或 `launchctl kickstart`）。

### Embedding 模型

默认使用本地 BGE-M3（GGUF/MLX 格式，Metal 加速）：

```
~/.openclaw/workspace/memory-os/models/bge-m3-Q8_0.gguf
```

路径可通过 `MEMORY_OS_EMBEDDING_MODEL` 环境变量覆盖。

---

## 安装配置

### 1. 安装插件

将 `memory-os-plugin/` 目录放入 OpenClaw 插件目录，并在 `openclaw.plugin.json` 中注册。

### 2. 配置环境变量

创建 `memory-os-plugin/.env`：

```bash
# Neo4j
MEMORY_OS_NEO4J_URI=bolt://127.0.0.1:7687
MEMORY_OS_NEO4J_USER=neo4j
MEMORY_OS_NEO4J_PASSWORD=your_password

# Qdrant
MEMORY_OS_QDRANT_HOST=127.0.0.1
MEMORY_OS_QDRANT_PORT=6333

# Embedding 模型
MEMORY_OS_EMBEDDING_MODEL=/Users/xxx/.openclaw/workspace/memory-os/models/bge-m3-Q8_0.gguf

# Hook 日志（可选）
MEMORY_OS_HOOK_TRACE_ENABLED=1
```

### 3. 一键启动所有服务

```bash
python3 scripts/service_lifecycle.py start-all
```

### 4. 检查服务状态

```bash
python3 scripts/service_lifecycle.py status
```

---

## 工具 API

插件注册了 4 个 MCP 工具，供 Agent 主动调用：

### `memory_os_ingest` — 存入记忆

**推荐方式（传 4 层 JSON 字符串）：**

```json
{
  "memory_json": "{\"l0\":{\"scene_summary\":\"...\",\"source\":\"...\"},\"l1\":{\"kos\":[...]},\"l2\":{\"scenario\":{...}或null},\"l3\":{\"persona\":[...]}}"
}
```

**兼容老格式（仅 L1）：**

```json
{
  "kos": [
    {
      "type": "fact",
      "summary": "用户曾去过北京",
      "state": "historical",
      "entities": [{"name": "北京", "label": "Place"}],
      "relations": [{"subject": "用户", "predicate": "VISITED", "object": "北京"}],
      "tags": ["旅行"],
      "importance": 0.8
    }
  ],
  "source": "微信对话:2026-08-26"
}
```

---

### `memory_os_recall` — 查询记忆

```json
{
  "query": "之前去旅行的事",
  "top_k": 5,
  "include_persona": true,
  "include_scenario": true
}
```

返回 4 层融合结果，按 persona(L3) → scenario(L2) → atom(L1) → raw(L0) 优先级排序。

**手控召回顺序：**

```json
{
  "query": "用户的旅行偏好",
  "layers": "L3,L2"
}
```

---

### `memory_os_update` — 更新记忆

**两阶段（传统模式）：**

```json
// 第一阶段
{ "query": "去北京的事", "memory": {...} }

// 第二阶段（拿 token）
{ "query": "去北京的事", "memory": {...}, "confirm": true, "token": "xxx" }
```

**快捷模式（直接指定 PID）：**

```json
{
  "memory": {...},
  "target_pid": "17174871657559152634",
  "target_collection": "memory_atom",
  "target_layer": "L1",
  "confirm": true
}
```

---

### `memory_os_delete` — 删除记忆

**两阶段（传统模式）：**

```json
// 第一阶段
{ "query": "去北京的事", "top_k": 5 }

// 第二阶段
{ "query": "去北京的事", "confirm": true, "token": "xxx", "selected_pids": ["pid1", "pid2"] }
```

**快捷模式（直接指定 PID）：**

```json
{
  "target_pid": "17174871657559152634",
  "target_collection": "memory_atom",
  "target_layer": "L1",
  "confirm": true
}
```

---

## 自动 Hook 注入

插件同时通过 OpenClaw Hook 自动工作，**无需 Agent 显式调用**：

- `before_prompt_build`：每次 LLM 调用前，对用户输入走召回流程，符合条件时自动注入记忆
- `message_received`：消息接收时触发（可选通道）

**Hook 门控规则（`recall_gate.py`）：**

- 字数 < 5 字 → 跳过
- 字数 > 300 字 → 截句拆送
- 纯情绪词（嗯/啊/好的/OK...）→ 跳过
- 含粗口 → 跳过

---

## 抽取规范

LLM 抽取 4 层记忆时，按 `scripts/extract_prompt.md` 的规范执行。

核心原则：

- **L0**：原文保存，不推理
- **L1**：最小独立知识单元，可脱离原 Context 独立理解
- **L2**：多个 L1 组成的完整场景，有标题和摘要
- **L3**：跨场景长期稳定的认知（关系/偏好/习惯），**宁缺勿编**

---

## 调参指南

所有参数集中在 `scripts/recall_config.py`，通过环境变量覆盖默认值。

### 召回质量

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MEMORY_OS_RECALL_DEFAULT_TOP_K` | 8 | 融合后保留条数 |
| `MEMORY_OS_VEC_MIN_SCORE` | 0.60 | 向量召回最低相似度 |
| `MEMORY_OS_GRAPH_DEPTH` | 1 | 图召回跳数（默认 1 跳，避免带偏） |
| `MEMORY_OS_RRF_K` | 60 | RRF 融合参数 |

### Association Expansion 联想记忆（2026-09-01 新增）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ASSOC_ENABLED` | 1 | 联想扩散开关，0=关闭 |
| `ASSOC_MAX_HOPS` | 2 | Neo4j 最大扩散跳数 |
| `ASSOC_MAX_NEIGHBORS` | 6 | 每跳最多扩展邻居数 |
| `ASSOC_ACTIVATION_THRESHOLD` | 0.1 | 联想激活阈值（低于此且 hop>1 丢弃） |
| `ASSOC_DEPTH_DECAY` | 0.5 | hop 深度衰减系数（每多一跳 ×0.5） |
| `ASSOC_MAX_CANDIDATES` | 20 | 最多联想候选数 |

### Hook 门控

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MEMORY_OS_HOOK_MIN_LEN` | 5 | 触发召回的最短字数 |
| `MEMORY_OS_HOOK_MAX_LEN` | 300 | 单段触发上限，超长截句 |

### 写入去重

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MEMORY_OS_DEDUP_THRESHOLD` | 0.82 | 去重相似度阈值 |
| `MEMORY_OS_WRITE_ANN_RECALL_THRESHOLD` | 0.80 | LLM 决策前 ANN 召回门槛 |

### 调试

```bash
# 开启召回调试日志
MEMORY_OS_RECALL_DEBUG=1 python3 scripts/recall_4layer.py recall --query "你的旅行"

# 查看 Hook 追踪日志
cat ~/.openclaw/workspace/memory-os/logs/hook-trace.md

# 查看写入决策日志
cat ~/.openclaw/workspace/memory-os/logs/write-decision.md
```

---

## 运维命令

```bash
# 启动所有服务
python3 scripts/service_lifecycle.py start-all

# 查看服务状态
python3 scripts/service_lifecycle.py status

# 停止所有服务
python3 scripts/service_lifecycle.py stop-all

# 重启指定服务
python3 scripts/service_lifecycle.py restart neo4j

# 召回统计（各 collection 条数）
python3 scripts/recall_stats.py

# 清理 Neo4j 重复边
python3 scripts/dedupe_active_edges.py --dry-run

# 清理孤儿节点
python3 scripts/clean_neo4j_orphans.py --dry-run
```

---

## 与 OpenClaw 的关系

```
AI Host（OpenClaw）
    │
    ├── src/index.js（插件 Hook + 工具）
    │       │
    │       ├── 触发 recall_4layer.py（被动召回）
    │       └── 调用 write_4layer.py（显式存取）
    │
    └── LLM
            │
            ├── 收到注入的【共同记忆】块
            └── 调用 memory_os_* 工具（主动存取）
```

---

## License

MIT
