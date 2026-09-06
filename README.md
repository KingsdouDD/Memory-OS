# Memory OS

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![GitHub Stars](https://img.shields.io/github/stars/KingsdouDD/Memory-OS)](https://github.com/KingsdouDD/Memory-OS/stargazers)
[![Node.js](https://img.shields.io/badge/node-V26%2B-339933?logo=node.js&logoColor=white)](https://nodejs.org/)
[![Python](https://img.shields.io/badge/python-3.14-3776ab?logo=python&logoColor=white)](https://www.python.org/)

> **Neo4j + Qdrant 混合长期记忆系统**，4 层记忆架构（L0/L1/L2/L3），为 AI Agent 提供持久化上下文记忆能力。
>
> 主动召回 / 联想扩散 / 两阶段安全更新 / 按状态分流。

---

## 一句话定位

把 AI 的"记忆"从模糊的上下文窗口，升级成**可精确写入、语义召回、关系推理、主动联想**的长期记忆系统。

---

## 核心特色

### 🧠 4 层记忆分层架构

| 层 | 名称 | 内容 | 召回优先级 |
|----|------|------|------------|
| **L3** | 长期画像 | 跨场景稳定认知（性格、习惯、偏好、关系） | 🔴 最高 |
| **L2** | 场景记忆 | 完整历史场景（事件、项目、关系） | 🟠 高 |
| **L1** | 原子事实 | 最小独立知识单元（fact / event / preference / routine / goal …） | 🟡 中 |
| **L0** | 原始对话 | 原始对话原文（托底证据） | ⚪ 最低 |

层级越高，允许的推断越少。L1 必须独立可读，L2 必须可恢复历史场景，L3 必须跨场景稳定。

### 🔗 混合召回引擎

- **向量检索**（Qdrant ANN）× **全文检索**（BM25 + jieba）× **知识图谱**（Neo4j）三路并行
- **RRF 融合** + **重要性加权**（0.5×~1.5×）+ **时间衰减**（180 天半衰期）+ **图命中 boost**（×1.3）
- **联想扩散**（Association Expansion）：Neo4j 多跳扩散 → 拼接 expansion vector → Qdrant 联想候选 → 综合打分
- **一次 Reranker 调用**完成精排，从 2次/Query → 1次/Query

### 🏃 主动记忆注入

- **Hook 自动召回**：`before_prompt_build` 触发，无需 LLM 显式调用
- **门控过滤**（`recall_gate.py`）：长度 < 7 字 / > 300 字 / 纯情绪词 / 粗口 / 重复字符 → 跳过
- **同会话 Query 去重**：md5 缓存，避免重复召回
- **注入格式**：包裹成"【以下是你和用户之间的共同记忆】"，引导 LLM 自然回忆而非外部资料感

### 🔄 灵活的记忆管理

- **6 个 MCP 工具**：ingest / recall / update / delete / health / **extract_runtime**
- **两阶段更新/删除**（token 保护，30 分钟 TTL，存 HOME 目录）+ **快捷模式**（直接指定 PID）
- **按状态分流**：
  - `completed` → 写入 Memory-OS（永久）
  - `ongoing / stalled` → 写入运行时临时文件（按 agent 分文件 + 多任务增量衔接）

### 🛠 本地优先 / 性能优异

- Embedding / Reranker 跑在**本地 GGUF 模型**（Metal 加速），不依赖云 API
- Embed / Reranker daemon **按需自动 spawn**：`subprocess.Popen` 拉起（`start_new_session=False`，父进程退出时子进程跟着退出），避免常驻僵尸
- 11 项启动自检改为**后台执行**（1 秒延迟），不再阻塞插件加载（延迟从 60s+ → 0）

---

## 系统架构

```
用户输入
   │
   ▼
┌──────────────────────────────────────┐
│  Hook Gate（recall_gate.py）           │  长度 / 情绪 / 粗口过滤
│  文本 < 7 字 / > 300 字 → 跳过         │
│  纯情绪词（嗯/好的/ok/继续） → 跳过    │
└──────────────────────────────────────┘
   │ Pass
   ▼
┌──────────────────────────────────────┐
│  4-Layer Recall（recall_4layer.py）   │
│                                      │
│  Step 1: L3 召回（persona）          │  → 提取 entities
│  Step 2: L2 召回（scenario）          │  → 提取 entities + ids
│  Step 2.5: jieba 实体补充             │
│  Step 3: Graph PRF（Neo4j 1-hop）     │
│  Step 4: L1 主召回（vec+BM25+Graph）  │  三路并行 → RRF 融合
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

## 仓库结构

```
memory-os/                          # ← 本仓库（运行时 + 文档 + 配置）
├── memory-os-plugin/               #    OpenClaw 插件源代码
│   ├── src/index.js                 #      插件入口：Hook + 6 个 MCP 工具
│   ├── scripts/                    #      核心脚本（召回 / 写入 / 守护进程）
│   ├── prompts/                    #      LLM 抽取规范
│   ├── openclaw.plugin.json
│   ├── README.md                   #      插件专属文档（hooks / 实现细节）
│   └── README_recall.md            #      召回流程详解（参数表 / 6 Steps）
├── models/                         # 本地 GGUF 模型（BGE-M3 / Qwen3-Reranker）
├── neo4j/                          # Neo4j 数据目录
├── qdrant/                         # Qdrant 数据目录
├── venv/                           # Python 虚拟环境
├── tokens/                         # update/delete token（TTL 30 min）
├── config/                         # Neo4j / Qdrant 配置
├── logs/                           # 运行时日志
├── dream-cron.sh                   # dream 摄取定时任务
├── README.md                       # ← 你在这里（主页面）
├── .gitattributes
└── .gitignore
```

---

## 6 个 MCP 工具

| 工具 | 用途 | 调用示例 |
|------|------|----------|
| `memory_os_ingest` | 存入永久记忆（4 层 JSON） | `memory_os_ingest({ memory_json: '{"l0":...,"l1":{"kos":[...]},"l2":...,"l3":...}' })` |
| `memory_os_recall` | 查询记忆（4 层融合） | `memory_os_recall({ query: "...", top_k: 5, layers: "L3,L2,L1" })` |
| `memory_os_update` | 更新记忆（追加不是覆盖） | `memory_os_update({ target_pid: "...", target_layer: "L3", memory_json: "...", confirm: true })` |
| `memory_os_delete` | 删除记忆（物理删除） | `memory_os_delete({ target_pid: "...", target_layer: "L3", confirm: true })` |
| `memory_os_health` | 服务健康检查 | `memory_os_health({ deep: false })` |
| `memory_os_extract_runtime` | 运行时临时记忆（按状态分流） | `memory_os_extract_runtime({ memory_json: "...", status: "completed" })` |

> 📖 详细 API、参数、所有调用模式 → 见 [`memory-os-plugin/README.md`](memory-os-plugin/README.md)

---

## 安装

### 1. 依赖服务

| 服务 | 端口 | 启动方式 |
|------|------|----------|
| **Neo4j** | 7474 / 7687 | `brew services start neo4j` |
| **Qdrant** | 6333 / 6334 | `brew services start qdrant` |
| **Embed Daemon** | 8765 | **自动 spawn**（端口无人监听时） |
| **Reranker Daemon** | 8877 | **自动 spawn**（端口无人监听时） |

> **架构说明（2026-09-06 重构）**：Embed / Reranker daemon 改用 `subprocess.Popen` 直接 spawn（不再依赖 launchd）。`start_new_session=False` 让父进程退出时子进程跟着退出，避免 daemon 变成常驻僵尸。Neo4j / Qdrant 仍由 brew 管理（需要常驻）。

### 2. 环境变量

在 `memory-os-plugin/` 创建 `.env`：

```bash
MEMORY_OS_NEO4J_URI=bolt://127.0.0.1:7687
MEMORY_OS_NEO4J_USER=neo4j
MEMORY_OS_NEO4J_PASSWORD=***
MEMORY_OS_QDRANT_HOST=127.0.0.1
MEMORY_OS_QDRANT_PORT=6333
MEMORY_OS_EMBEDDING_MODEL=~/.openclaw/workspace/memory-os/models/bge-m3-Q8_0.gguf
MEMORY_OS_HOOK_TRACE_ENABLED=1
```

### 3. Embedding / Reranker 模型

| 服务 | 模型 | 路径 |
|------|------|------|
| Embed | BGE-M3（GGUF / MLX） | `~/.openclaw/workspace/memory-os/models/bge-m3-mlx-8bit` |
| Reranker | Qwen3-Reranker-0.6B | `~/.openclaw/workspace/memory-os/models/Qwen3-Reranker-0.6B-4bit` |

---

## 关键设计决策

### PID 格式：标准 UUID

L2/L3 的 PID 从整数 md5 改为标准 UUID（`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`），避免与 Neo4j Long 上限冲突。

### 写入是追加，不是覆盖

L0-L3 长文本（summary）在更新时用 ` | ` 拼接追加。Neo4j 写新关系（MERGE），旧关系保留。

### 删除是物理删除

Qdrant 点直接 delete，Neo4j 节点 DETACH DELETE，不做软删除。

### Association Expansion 联想扩散

从种子实体出发，Neo4j 多跳扩散（默认 2 跳）→ Qdrant 向量搜索联想候选 → 综合打分（`overlap × 0.4 + depth_decay × 0.25 + importance × 0.2 + temporal × 0.15`）→ 统一 Reranker 精排。

### Token 安全

Update / Delete 的两阶段 token 存放在 `~/.openclaw/workspace/memory-os/tokens/`，TTL 30 分钟，不受 `/tmp` 清理影响。

### Embed / Reranker 按需 spawn

`subprocess.Popen` + `start_new_session=False`，父进程退出时子进程跟着退出，避免 daemon 变成常驻僵尸。

### 启动自检后台化

插件 register 时不再同步跑 11 项自检（曾经最坏 60s+ 延迟），改 1 秒后后台 fire-and-forget。需要时调 `memory_os_health` 工具。

### 运行时临时记忆按 agent 分文件

不同通道（QQ / 微信 / Telegram / subagent）的临时记忆写到不同文件，互不污染。老豆有多个微信账号，按 `openid` 区分。

---

## 文档索引

| 文档 | 内容 |
|------|------|
| [`README.md`](README.md) | 本文档：主页面、核心特色、架构、6 工具概览、安装 |
| [`memory-os-plugin/README.md`](memory-os-plugin/README.md) | 插件专属：Hook 实现、注入格式、运维命令 |
| [`memory-os-plugin/README_recall.md`](memory-os-plugin/README_recall.md) | 召回流程详解：6 Steps + Association Expansion + 参数表 |
| [`memory-os-plugin/scripts/extract_prompt.md`](memory-os-plugin/scripts/extract_prompt.md) | LLM 抽取永久记忆的 4 层规范 |
| [`memory-os-plugin/prompts/runtime_memory_extract.md`](memory-os-plugin/prompts/runtime_memory_extract.md) | LLM 抽取运行时临时记忆的规范（含快照窗口 + 按 agent 分文件） |

---

## License

MIT · [KingsdouDD/Memory-OS](https://github.com/KingsdouDD/Memory-OS)