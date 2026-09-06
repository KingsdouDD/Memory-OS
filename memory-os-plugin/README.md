# Memory OS

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![GitHub Stars](https://img.shields.io/github/stars/KingsdouDD/Memory-OS)](https://github.com/KingsdouDD/Memory-OS/stargazers)
[![Node.js](https://img.shields.io/badge/node-V26%2B-339933?logo=node.js&logoColor=white)](https://nodejs.org/)
[![Python](https://img.shields.io/badge/python-3.14-3776ab?logo=python&logoColor=white)](https://www.python.org/)

> **Neo4j + Qdrant 混合长期记忆系统**，4 层记忆架构（L0/L1/L2/L3），为 AI Agent 提供持久化上下文记忆能力。

---

## 一句话定位

把 AI 的"记忆"从模糊的上下文窗口，升级成**可精确写入、语义召回、关系推理、主动联想**的长期记忆系统。

---

## 核心特色

### 🧠 4 层记忆分层架构

| 层 | 名称 | 内容 | 召回优先级 |
|----|------|------|------------|
| **L3** | 长期画像 | 跨场景稳定认知（性格、习惯、偏好） | 🔴 最高 |
| **L2** | 场景记忆 | 完整历史场景（事件、项目、关系） | 🟠 高 |
| **L1** | 原子事实 | 最小独立知识单元（fact/event/preference…） | 🟡 中 |
| **L0** | 原始对话 | 原始对话原文（托底证据） | ⚪ 最低 |

### 🔗 混合召回引擎

- **向量检索**（Qdrant ANN）× **全文检索**（BM25）× **知识图谱**（Neo4j）三路并行
- **RRF 融合** + **重要性加权** + **时间衰减** + **联想扩散**（Association Expansion）
- **一次 Reranker 调用**完成精排，从 2次/Query → 1次/Query

### 🏃 主动记忆注入

- **Hook 自动召回**：用户每次说话，自动在 `before_prompt_build` 触发召回，无需显式调用
- 智能**门控过滤**：长度 / 情绪 / 粗口自动跳过，不浪费召回算力
- 同会话 **Query 去重**，避免重复召回

### 🔄 灵活的记忆管理

- **Ingest / Recall / Update / Delete** 4 个 MCP 工具，完整 CRUD
- **两阶段更新/删除**（token 保护）+ **快捷模式**（直接指定 PID）
- **按状态分流**：`completed` → 永久写入；`ongoing/stalled` → 临时状态文件

### 🛠 本地优先，性能优异

- Embedding / Reranker 均运行在**本地 GGUF 模型**（Metal 加速），不依赖云 API
- 服务**自动拉起**：Neo4j / Qdrant / Embed Daemon / Reranker Daemon 挂了自动恢复
- 启动自检改为**后台执行**，不再阻塞插件加载（延迟从 60s+ → 0）

---

## 系统架构

```
用户输入
   │
   ▼
┌──────────────────────────────────────┐
│  Hook Gate（recall_gate.py）           │  长度/情绪/粗口过滤
│  文本 < 7 字 → 跳过                   │
└──────────────────────────────────────┘
   │ Pass
   ▼
┌──────────────────────────────────────┐
│  4-Layer Recall（recall_4layer.py）   │
│                                      │
│  Step 1: L3 召回（persona）           │  → 提取 entities
│  Step 2: L2 召回（scenario）          │  → 提取 entities + ids
│  Step 2.5: jieba 实体补充             │
│  Step 3: Graph PRF（Neo4j 1-hop）     │
│  Step 4: L1 主召回（vec+BM25+RRF）    │
│  Step 5: Pre-filter（entity overlap） │
│  Step 6: Association Expansion        │  ← Neo4j 多跳扩散 + Qdrant 联想
│  → 合并去重 + 统一 Reranker × 1       │
│  → Final Top-K                       │
└──────────────────────────────────────┘
   │
   ▼
┌──────────────────────────────────────┐
│  Memory Injection（src/index.js）      │  prepend 到 system prompt
│  "【以下是你和用户之间的共同记忆】"     │
└──────────────────────────────────────┘
   │
   ▼
  LLM 输出（含注入记忆）
```

---

## 目录结构

```
memory-os-plugin/                # OpenClaw 插件根目录
├── src/
│   └── index.js                 # 插件入口：Hook 注册 + 5 个 MCP 工具
├── scripts/
│   ├── recall_4layer.py         # 4 层融合召回主脚本
│   ├── recall_fusion.py         # RRF 融合 + kg_verify + Association Expansion
│   ├── recall_config.py         # 所有召回超参数（参数中心）
│   ├── recall_gate.py           # Hook 门控（长度/情绪/粗口过滤）
│   ├── write_4layer.py          # 4 层写入/更新/删除
│   ├── process_dream.py         # Embedding + Qdrant 底层读写
│   ├── extract_prompt.md        # LLM 4 层抽取规范
│   ├── embed_daemon.py          # Embedding HTTP 守护进程（本地 GGUF）
│   ├── reranker_daemon.py       # Reranker HTTP 守护进程
│   ├── bm25_index.py            # BM25 全文索引
│   ├── service_lifecycle.py      # 服务启动/停止/健康检查
│   ├── cron_runner.py           # 定时任务（dream 摄取）
│   ├── *dedup*.py / *clean*.py # 运维工具
│   └── __pycache__/             # 编译缓存
├── prompts/
│   └── runtime_memory_extract.md # 运行时记忆抽取提示词
├── logs/                        # 运行日志
├── openclaw.plugin.json         # 插件配置
├── package.json
├── README.md                    # 本文档
├── README_recall.md             # 召回流程详解
└── MEMORY-OS-4LAYER.md         # 4 层架构数据结构和设计决策
```

---

## 5 个 MCP 工具

### 1. `memory_os_ingest` — 存入记忆

```js
// 推荐：4层 JSON 字符串（避免 MCP 嵌套数组展平）
memory_os_ingest({
  memory_json: JSON.stringify({
    l0: { scene_summary: "...", source: "qq:2026-09-06" },
    l1: { kos: [{ type: "fact", summary: "...", state: "active", entities: [...] }] },
    l2: { scenario: { title: "...", summary: "...", type: "event", state: "active" } },
    l3: { persona: [{ type: "preference", summary: "...", importance: 0.85 }] }
  })
})

// 兼容老格式：纯 L1 KO 数组
memory_os_ingest({ kos: [...], source: "qq:2026-09-06" })
```

### 2. `memory_os_recall` — 查询记忆

```js
// 默认全开 4 层
memory_os_recall({ query: "用户的工作习惯", top_k: 5 })

// 手控召回顺序
memory_os_recall({ query: "用户爱好", layers: "L3,L2,L1" })
```

返回结构：`{ persona: [...L3], scenario: [...L2], atom: [...L1], raw: [...L0] }`

### 3. `memory_os_update` — 更新记忆

```js
// 快捷模式（直接指定 PID，跳过召回）
memory_os_update({
  target_pid: "uuid格式",
  target_collection: "memory_persona",
  target_layer: "L3",
  memory_json: '{"l3":{"persona":[...]}}',
  confirm: true
})

// 两阶段模式
memory_os_update({ query: "记忆关键词", memory_json: "...", confirm: false })
// → 返回 token
memory_os_update({ query: "记忆关键词", confirm: true, token: "***" })
```

### 4. `memory_os_delete` — 删除记忆

```js
// 快捷模式（直接删除）
memory_os_delete({
  target_pid: "uuid格式",
  target_collection: "memory_persona",
  target_layer: "L3",
  confirm: true
})

// 两阶段模式
memory_os_delete({ query: "记忆关键词", confirm: false })
// → 返回候选清单 + token
memory_os_delete({ query: "记忆关键词", confirm: true, token: "***", selected_pids: [...] })
```

### 5. `memory_os_health` — 服务健康检查

```js
// Fast 模式（< 2s）：查 4 个服务端口
memory_os_health({})

// Deep 模式（5-30s）：跑完整 11 项自检
memory_os_health({ deep: true })
```

检查内容：Neo4j / Qdrant / Embed Daemon / Reranker Daemon，自动拉起宕机服务。

---

## 安装配置

### 1. 依赖服务

| 服务 | 端口 | 启动命令 |
|------|------|----------|
| **Neo4j** | 7474 / 7687 | `brew services start neo4j` |
| **Qdrant** | 6333 / 6334 | `brew services start qdrant` |
| **Embed Daemon** | 8765 | `launchctl kickstart gui/501/com.memoryos.embed-daemon` |
| **Reranker Daemon** | 8877 | `launchctl kickstart gui/501/com.memoryos.reranker` |

> **重要（2026-09-03）**：插件不再在召回路径上自动检查/拉起服务（原来最坏延迟 60s+）。
> 用 `memory_os_health` 工具按需检查服务状态（fast < 2s，deep 5-30s）。

### 2. 环境变量

在 `memory-os-plugin/` 创建 `.env`：

```bash
MEMORY_OS_NEO4J_URI=bolt://127.0.0.1:7687
MEMORY_OS_NEO4J_USER=neo4j
MEMORY_OS_NEO4J_PASSWORD=你的密码
MEMORY_OS_QDRANT_HOST=127.0.0.1
MEMORY_OS_QDRANT_PORT=6333
MEMORY_OS_EMBEDDING_MODEL=~/.openclaw/workspace/memory-os/models/bge-m3-Q8_0.gguf
MEMORY_OS_HOOK_TRACE_ENABLED=1   # 写 hook 日志（开发调试）
```

### 3. Embedding 模型

默认本地 BGE-M3（GGUF，Metal 加速）：

```
~/.openclaw/workspace/memory-os/models/bge-m3-Q8_0.gguf
```

---

## 运维命令

```bash
# 启动所有服务
python3 scripts/service_lifecycle.py start-all

# 检查服务状态
python3 scripts/service_lifecycle.py status

# 停止所有服务
python3 scripts/service_lifecycle.py stop-all

# 召回统计
python3 scripts/recall_stats.py

# 开启召回调试日志
MEMORY_OS_RECALL_DEBUG=1 python3 scripts/recall_4layer.py recall --query "..."

# 查看 hook 追踪日志
cat ~/.openclaw/workspace/memory-os/logs/hook-trace.md
```

---

## 关键设计决策

### PID 格式：标准 UUID

L2/L3 的 PID 从整数 md5 改为标准 UUID（`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`），避免与 Neo4j Long 上限冲突。

### 更新是追加，不是覆盖

L0-L3 长文本（summary）在更新时用 ` | ` 拼接追加。Neo4j 写新关系，旧关系保留。

### Association Expansion 联想扩散

从种子实体出发，在 Neo4j 图里多跳扩散（默认 2 跳），找到相关记忆后通过 Qdrant 向量搜索联想候选，综合打分后统一 Reranker 精排。

### Token 安全

Update / Delete 的两阶段 token 存放在 `~/.openclaw/workspace/memory-os/tokens/`，TTL 30 分钟，不受 `/tmp` 清理影响。

---

## 文档索引

| 文档 | 内容 |
|------|------|
| `README.md` | 概览、安装、工具 API、运维 |
| `README_recall.md` | 召回流程详解（6 Steps + Association Expansion） |
| `MEMORY-OS-4LAYER.md` | 4 层架构、数据结构、设计决策 |

---

## License

MIT · [KingsdouDD/Memory-OS](https://github.com/KingsdouDD/Memory-OS)
