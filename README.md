# Memory OS

Neo4j + Qdrant 长期记忆系统 — OpenClaw 插件

四层记忆架构（L0/L1/L2/L3），兼顾向量检索与知识图谱，支持主动注入、被动召回、LLM 自我反思写入。

---

## 架构总览

```
用户输入
   │
   ▼
┌───────────────────────────────────────┐
│         Hook Gate（门控）               │  recall_gate.py
│   长度 / 纯情绪 / 粗口过滤            │
└───────────────────────────────────────┘
   │ 通过
   ▼
┌───────────────────────────────────────┐
│         4-Layer Recall                │  recall_4layer.py
│                                       │
│  Step 1-2: L3/L2 高置信召回          │  提取 filter_entities
│  Step 2.5: 实体补充（jieba）         │
│  Step 3: Graph PRF（Neo4j 1-hop）     │
│  Step 4: L1 主召回（vec+BM25+RRF）   │
│  Step 5: Pre-filter（entity overlap） │
│  Step 6: Association Expansion        │  ← Neo4j 多跳扩散 + Qdrant
│         ↓ 合并去重                    │
│    统一一次 Reranker 精排             │
│    Final Top-K                        │
└───────────────────────────────────────┘
   │
   ▼
┌───────────────────────────────────────┐
│       Memory Injection                │  src/index.js
│   【共同记忆】+ 自然语气              │
└───────────────────────────────────────┘
   │
   ▼
  LLM 输出（已注入记忆）
```

---

## 四层记忆设计

| 层 | 名称 | Qdrant collection | PID 策略 | 召回优先级 |
|----|------|-------------------|----------|------------|
| **L0** | 原始场景 | `memory_l0` | UUID | 最低（托底）|
| **L1** | 原子记忆 | `memory_<type>`（8 个 collection）| md5 指纹 | 中 |
| **L2** | 场景记忆 | `memory_scenario` | UUID | 高 |
| **L3** | 长期画像 | `memory_persona` | UUID | 最高 |

### L1 的 8 个 Collection

```
memory_atom / memory_fact / memory_event / memory_experience
memory_preference / memory_routine / memory_concept / memory_relation
```

---

## 目录结构

```
memory-os-plugin/          # ← OpenClaw 插件根目录
├── src/
│   └── index.js         # 插件入口，Hook 注册 + 4 个 MCP 工具
├── scripts/
│   ├── recall_4layer.py    # 4 层融合召回
│   ├── recall_fusion.py     # RRF 融合 + graph boost + time decay
│   ├── recall_config.py     # 召回超参数中心
│   ├── recall_gate.py       # Hook 门控
│   ├── recall_stats.py      # 各 collection 统计
│   ├── write_4layer.py     # 4 层写入/更新/删除（Python CLI）
│   ├── process_dream.py     # Embedding + Qdrant 底层
│   ├── extract_prompt.md    # LLM 4 层抽取规范
│   ├── embed_daemon.py      # Embedding HTTP 守护进程（本地 GGUF）
│   ├── reranker_daemon.py  # Reranker HTTP 守护进程
│   ├── bm25_index.py        # BM25 全文索引
│   ├── service_lifecycle.py  # 服务启停管理
│   ├── cron_runner.py        # 定时任务（梦境入库）
│   └── *dedup*.py / *clean*.py  # 运维工具
├── config/                 # 配置文件
├── logs/                  # 运行日志（.gitignore 排除）
├── openclaw.plugin.json   # 插件 manifest
├── package.json
├── README.md              # 本文档
├── README_recall.md       # 召回流程详解
└── MEMORY-OS-4LAYER.md    # 4 层架构详解

memory-os/                 # ← 本地运行时数据（独立目录）
├── venv/                  # Python 虚拟环境
├── models/                # Embedding 模型（GGUF/MLX）
├── neo4j/                 # Neo4j 数据目录
├── qdrant/                # Qdrant 数据目录
├── tokens/                # delete/update token（TTL 30 分钟）
└── logs/                  # 运行日志
```

---

## 环境依赖

### 必须服务

| 服务 | 端口 | 启动方式 | 状态查询 |
|------|------|---------|---------|
| **Neo4j** | 7474 / 7687 | `brew services start neo4j` | `lsof -i :7687` |
| **Qdrant** | 6333 / 6334 | `brew services start qdrant` | `lsof -i :6333` |
| **Embed Daemon** | 8765 | `launchctl kickstart gui/501/com.memoryos.embed-daemon` | `lsof -i :8765` |
| **Reranker Daemon** | 8877 | `launchctl kickstart gui/501/com.memoryos.reranker` | `lsof -i :8877` |

> 插件启动时会自动检测服务状态，未启动时自动拉起。

### Embedding 模型

默认使用本地 BGE-M3（GGUF/MLX 格式，Metal 加速）：

```
~/.openclaw/workspace/memory-os/models/bge-m3-Q8_0.gguf
```

路径可通过 `MEMORY_OS_EMBEDDING_MODEL` 环境变量覆盖。

---

## 安装配置

### 1. 配置环境变量

在 `memory-os-plugin/` 目录创建 `.env`：

```bash
# Neo4j
MEMORY_OS_NEO4J_URI=bolt://127.0.0.1:7687
MEMORY_OS_NEO4J_USER=neo4j
MEMORY_OS_NEO4J_PASSWORD=openclaw

# Qdrant
MEMORY_OS_QDRANT_HOST=127.0.0.1
MEMORY_OS_QDRANT_PORT=6333

# Embedding 模型
MEMORY_OS_EMBEDDING_MODEL=~/.openclaw/workspace/memory-os/models/bge-m3-Q8_0.gguf

# Hook 日志（可选，默认开）
MEMORY_OS_HOOK_TRACE_ENABLED=1
```

### 2. 一键启动所有服务

```bash
python3 scripts/service_lifecycle.py start-all
```

### 3. 检查服务状态

```bash
python3 scripts/service_lifecycle.py status
```

---

## 4 个 MCP 工具

### `memory_os_ingest` — 存入记忆

**推荐方式**（传 4 层 JSON 字符串，避免 MCP 嵌套数组被展平）：

```json
{
  "memory_json": "{\"l0\":{\"scene_summary\":\"外婆今天去越秀山爬山\",\"source\":\"微信对话:2026-09-02\"},\"l1\":{\"kos\":[{\"type\":\"routine\",\"summary\":\"外婆早上7点半去越秀山爬山\",\"state\":\"ongoing\",\"entities\":[{\"name\":\"外婆\",\"label\":\"Person\"},{\"name\":\"越秀山\",\"label\":\"Place\"}]}]},\"l3\":{\"persona\":[{\"type\":\"routine\",\"summary\":\"外婆喜欢早上爬山呼吸新鲜空气\",\"state\":\"active\",\"importance\":0.85}]}}"
}
```

**兼容老格式**（仅 L1）：

```json
{
  "kos": [
    {
      "type": "routine",
      "summary": "外婆早上7点半去越秀山爬山",
      "state": "ongoing",
      "entities": [{"name": "外婆", "label": "Person"}, {"name": "越秀山", "label": "Place"}]
    }
  ],
  "source": "微信对话:2026-09-02"
}
```

---

### `memory_os_recall` — 查询记忆

```json
{
  "query": "外婆最近在干嘛",
  "top_k": 5,
  "include_persona": true,
  "include_scenario": true,
  "layers": "L3,L2,L1,L0"
}
```

**参数说明**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `query` | string | **必填** | 查询文本 |
| `top_k` | integer | 5 | 每层返回条数 |
| `include_persona` | boolean | true | 是否召回 L3 |
| `include_scenario` | boolean | true | 是否召回 L2 |
| `layers` | string | 全开 | 手控顺序，如 `"L3,L2"` |

---

### `memory_os_update` — 更新记忆

> **更新逻辑**：在旧内容后面**追加**新内容（L0-L3 长文本用 ` | ` 拼接），Neo4j 写新关系，旧关系保留。

**快捷模式（推荐，跳过召回，直接更新）**：

```json
{
  "memory_json": "{\"l3\":{\"persona\":[{\"type\":\"preference\",\"summary\":\"外婆喜欢早上爬山呼吸新鲜空气\",\"state\":\"active\",\"importance\":0.85}]}}",
  "target_pid": "6d4b5542-9775-9364-87b7-a19901d67eda",
  "target_collection": "memory_persona",
  "target_layer": "L3",
  "confirm": true
}
```

**两阶段模式**：

```json
// 第一阶段：召回 + 生成 token
{
  "query": "外婆的习惯",
  "memory_json": "{\"l3\":{\"persona\":[...]}}",
  "confirm": false
}
// → 返回 { phase:"confirm", token:"...", target:{pid,layer,summary}, candidates:[...] }

// 第二阶段：带 token 真更新
{
  "query": "外婆的习惯",
  "confirm": true,
  "token": "***"
}
```

---

### `memory_os_delete` — 删除记忆

**快捷模式（推荐，一次性直接删）**：

```json
{
  "target_pid": "6d4b5542-9775-9364-87b7-a19901d67eda",
  "target_collection": "memory_persona",
  "target_layer": "L3",
  "confirm": true
}
// → 返回 { deleted: {l0:0, l1:0, l2:0, l3:1} }
```

**两阶段模式**：

```json
// 第一阶段：召回候选 + 生成 token
{
  "query": "外婆菠萝包",
  "layer": "L3",
  "confirm": false
}
// → 返回 { phase:"confirm", token:"...", candidates:[...] }

// 第二阶段：带 token 真删
{
  "query": "外婆菠萝包",
  "confirm": true,
  "token": "***",
  "selected_pids": ["pid1", "pid2"]
}
// → 返回 { deleted: {l0:0, l1:0, l2:0, l3:1} }
```

**PID 和 Collection 速查**：

| 层 | Collection | PID 来源 |
|----|-----------|---------|
| L0 | `memory_l0` | 召回返回的 `_qdrant_pid` |
| L1 | 原有 8 个 collection | 召回返回的 `_qdrant_pid` |
| L2 | `memory_scenario` | 召回返回的 `pid`（UUID） |
| L3 | `memory_persona` | 召回返回的 `pid`（UUID） |

**Token TTL：30 分钟**，存在 `~/.openclaw/workspace/memory-os/tokens/`

---

## 自动 Hook 注入

插件通过 OpenClaw Hook **自动**工作，无需 Agent 显式调用：

- `before_prompt_build`：每次 LLM 调用前，对用户输入走召回流程，符合条件时自动注入记忆
- `message_received`：消息接收时触发

**Hook 门控规则（`recall_gate.py`）**：

| 规则 | 行为 |
|------|------|
| 字数 < 5 字 | 跳过 |
| 字数 > 300 字 | 截句拆送 |
| 纯情绪词（嗯/啊/好的/OK） | 跳过 |
| 含粗口 | 跳过 |

---

## 抽取规范

LLM 按 `scripts/extract_prompt.md` 的规范抽取 4 层记忆。

**核心原则**：

- **L0**：原文保存，不推理
- **L1**：最小独立知识单元，可脱离原 Context 独立理解
- **L2**：多个 L1 组成的完整场景，有标题和摘要
- **L3**：跨场景长期稳定的认知，**宁缺勿编**

---

## 调参指南

所有参数集中在 `scripts/recall_config.py`。

### 召回质量

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MEMORY_OS_RECALL_DEFAULT_TOP_K` | 8 | 融合后保留条数 |
| `MEMORY_OS_VEC_MIN_SCORE` | 0.60 | 向量召回最低相似度 |
| `MEMORY_OS_GRAPH_DEPTH` | 1 | 图召回跳数 |
| `MEMORY_OS_RRF_K` | 60 | RRF 融合参数 |

### Association Expansion 联想扩散（2026-09-01）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ASSOC_ENABLED` | 1 | 开关，0=关闭 |
| `ASSOC_MAX_HOPS` | 2 | Neo4j 最大扩散跳数 |
| `ASSOC_MAX_NEIGHBORS` | 6 | 每跳最多扩展邻居数 |
| `ASSOC_ACTIVATION_THRESHOLD` | 0.1 | 联想激活阈值 |
| `ASSOC_DEPTH_DECAY` | 0.5 | hop 深度衰减系数 |
| `ASSOC_MAX_CANDIDATES` | 20 | 最多联想候选数 |

### Hook 门控

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `HOOK_MIN_LEN` | 5 | 触发召回的最短字数 |
| `HOOK_MAX_LEN` | 300 | 单段触发上限 |

### 写入去重

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `MEMORY_OS_DEDUP_THRESHOLD` | 0.82 | 去重相似度阈值 |
| `MEMORY_OS_WRITE_ANN_RECALL_THRESHOLD` | 0.80 | 写入前 ANN 召回门槛 |

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

# 召回统计
python3 scripts/recall_stats.py

# 开启召回调试日志
MEMORY_OS_RECALL_DEBUG=1 python3 scripts/recall_4layer.py recall --query "你的旅行"

# 查看 Hook 追踪日志
cat ~/.openclaw/workspace/memory-os/logs/hook-trace.md
```

---

## 与 OpenClaw 的关系

```
OpenClaw（AI Host）
    │
    ├── src/index.js（插件 Hook + 工具）
    │       │
    │       ├── Hook：触发 recall_4layer.py（被动召回）
    │       └── Tool：调用 write_4layer.py（主动存取）
    │
    └── LLM
            │
            ├── 收到注入的【共同记忆】块
            └── 调用 memory_os_* 工具（主动存取）
```

---

## 文档索引

| 文档 | 内容 |
|------|------|
| `README.md` | 总览、安装、工具 API、运维 |
| `README_recall.md` | 召回流程详解（6 Step + Association） |
| `MEMORY-OS-4LAYER.md` | 4 层架构详解、数据结构、设计决策 |

---

## License

MIT
