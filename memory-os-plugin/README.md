# Memory OS

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![GitHub Stars](https://img.shields.io/github/stars/KingsdouDD/Memory-OS)](https://github.com/KingsdouDD/Memory-OS/stargazers)
[![Node.js](https://img.shields.io/badge/node-V26%2B-339933?logo=node.js&logoColor=white)](https://nodejs.org/)
[![Python](https://img.shields.io/badge/python-3.14-3776ab?logo=python&logoColor=white)](https://www.python.org/)

> **Neo4j + Qdrant 混合长期记忆系统**，4 层记忆架构（L0/L1/L2/L3），为 AI Agent 提供持久化上下文记忆能力。
>
> 主动召回 / 联想扩散 / 两阶段安全更新 / 按状态分流，是本系统的核心设计。

---

## 一句话定位

把 AI 的"记忆"从模糊的上下文窗口，升级成**可精确写入、语义召回、关系推理、主动联想、按场景分流**的长期记忆系统。

---

## 核心特色

### 🧠 4 层记忆分层架构

| 层 | 名称 | 内容 | 召回优先级 |
|----|------|------|------------|
| **L3** | 长期画像 | 跨场景稳定认知（性格、习惯、偏好、关系） | 🔴 最高 |
| **L2** | 场景记忆 | 完整历史场景（事件、项目、关系） | 🟠 高 |
| **L1** | 原子事实 | 最小独立知识单元（fact/event/preference/routine/goal…） | 🟡 中 |
| **L0** | 原始对话 | 原始对话原文（托底证据） | ⚪ 最低 |

层级越高，允许的推断越少。L1 必须独立可读，L2 必须可恢复历史场景，L3 必须跨场景稳定。

### 🔗 混合召回引擎

- **向量检索**（Qdrant ANN）× **全文检索**（BM25 + jieba）× **知识图谱**（Neo4j）三路并行
- **RRF 融合** + **重要性加权**（0.5×~1.5×）+ **时间衰减**（180 天半衰期）+ **图命中 boost**（×1.3）
- **联想扩散**（Association Expansion）：Neo4j 多跳扩散 → 拼接 expansion vector → Qdrant 联想候选 → 综合打分
- **一次 Reranker 调用**完成精排，从 2次/Query → 1次/Query

### 🏃 主动记忆注入

- **Hook 自动召回**：`before_prompt_build` 触发，无需 LLM 显式调用
- **门控过滤**（`recall_gate.py`）：长度 < 7 字 / > 300 字 / 纯情绪词 / 粗口 / 重复字符 → 跳过召回
- **同会话 Query 去重**：md5 缓存，避免重复召回
- **注入格式**：包裹成"【以下是你和用户之间的共同记忆】"，引导 LLM 自然回忆而非外部资料感

### 🔄 灵活的记忆管理

- **6 个 MCP 工具**：ingest / recall / update / delete / health / **extract_runtime**
- **两阶段更新/删除**（token 保护，30 分钟 TTL，存 HOME 目录）+ **快捷模式**（直接指定 PID）
- **按状态分流**：
  - `completed` → 写入 Memory-OS（永久）
  - `ongoing/stalled` → 写入运行时临时文件（覆盖式增量衔接）

### 🛠 本地优先 / 性能优异

- Embedding / Reranker 跑在**本地 GGUF 模型**（Metal 加速），不依赖云 API
- Embed / Reranker daemon **按需自动 spawn**：端口无人监听时 `subprocess.Popen` 拉起，父进程退出时子进程跟着退出（`start_new_session=False`），不会变成常驻僵尸
- 11 项启动自检改为**后台执行**（1 秒延迟），不再阻塞插件加载（延迟从 60s+ → 0）

---

## 系统架构

```
用户输入
   │
   ▼
┌──────────────────────────────────────┐
│  Hook Gate（recall_gate.py）           │  长度/情绪/粗口过滤
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

## 目录结构

```
memory-os-plugin/                # OpenClaw 插件根目录
├── src/
│   └── index.js                 # 插件入口：Hook 注册 + 6 个 MCP 工具
├── scripts/
│   ├── recall_4layer.py         # 4 层融合召回主脚本
│   ├── recall_fusion.py         # RRF 融合 + kg_verify + Association Expansion
│   ├── recall_config.py         # 所有召回超参数（参数中心）
│   ├── recall_gate.py           # Hook 门控（长度/情绪/粗口过滤）
│   ├── write_4layer.py          # 4 层写入/更新/删除
│   ├── process_dream.py         # Embedding + Qdrant 底层读写
│   ├── extract_prompt.md        # LLM 4 层抽取规范（永久记忆）
│   ├── embed_daemon.py          # Embedding HTTP 守护进程（本地 GGUF）
│   ├── reranker_daemon.py       # Reranker HTTP 守护进程
│   ├── bm25_index.py            # BM25 全文索引
│   ├── service_lifecycle.py     # 服务生命周期（spawn / health / http_post）
│   ├── cron_runner.py           # 定时任务（dream 摄取）
│   ├── *dedup*.py / *clean*.py # 运维工具
│   └── __pycache__/             # 编译缓存
├── prompts/
│   └── runtime_memory_extract.md # 运行时临时记忆抽取规范（含快照窗口 + 按 agent 分文件）
├── logs/                        # 运行日志（hook-trace.md 等）
├── openclaw.plugin.json         # 插件清单 + 工具/hook 注册
├── package.json
├── README.md                    # 本文档
├── README_recall.md             # 召回流程详解（6 Steps + Association Expansion）
└── MEMORY-OS-4LAYER.md         # 4 层架构数据结构和设计决策
```

---

## 6 个 MCP 工具

### 1. `memory_os_ingest` — 存入永久记忆

按 `extract_prompt.md` 规范抽取的 4 层 JSON，写入 Memory-OS（Neo4j 知识图谱 + Qdrant 向量库）。

```js
// 推荐：4 层 JSON 字符串（避免 MCP 嵌套数组被展平）
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

**硬约束**：L1/L2/L3 summary ≤ 150 字；importance ∈ [0.0, 1.0]；L1 KOs ≤ 8 条；summary 必须独立可读、不依赖对话上下文。

### 2. `memory_os_recall` — 查询记忆

```js
// 默认全开 4 层
memory_os_recall({ query: "用户的工作习惯", top_k: 5 })

// 手控召回顺序
memory_os_recall({ query: "用户爱好", layers: "L3,L2,L1" })
```

返回结构：`{ persona: [...L3], scenario: [...L2], atom: [...L1], raw: [...L0] }`

每条记忆字段：`summary / score / rerank_score / final_score / entity_overlap / assoc_score / hop_depth / association_path / recall_reason / _is_assoc / importance / event_time / source`

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

// 两阶段模式（带 token 保护）
memory_os_update({ query: "记忆关键词", memory_json: "...", confirm: false })
// → 返回 token
memory_os_update({ query: "记忆关键词", confirm: true, token: "***" })
```

**更新逻辑**：在旧 summary 后面**追加**新内容（用 ` | ` 拼接）。Neo4j 写新关系，旧关系保留。

### 4. `memory_os_delete` — 删除记忆

```js
// 快捷模式（直接删除）
memory_os_delete({
  target_pid: "uuid格式",
  target_collection: "memory_persona",
  target_layer: "L3",
  confirm: true
})

// 两阶段模式（带 token 保护）
memory_os_delete({ query: "记忆关键词", confirm: false })
// → 返回候选清单 + token
memory_os_delete({ query: "记忆关键词", confirm: true, token: "***", selected_pids: [...] })
```

**删除是物理删除**（Qdrant delete + Neo4j DETACH DELETE），不做软删除。Token TTL 30 分钟，存 `~/.openclaw/workspace/memory-os/tokens/`。

### 5. `memory_os_health` — 服务健康检查

```js
// Fast 模式（< 2s）：查 4 个服务端口
memory_os_health({})

// Deep 模式（5-30s）：跑完整 11 项自检
memory_os_health({ deep: true })
```

检查内容：Neo4j 7687 / Qdrant 6333 / Embed Daemon 8765 / Reranker Daemon 8877。挂了自动拉起。

### 6. `memory_os_extract_runtime` — 运行时临时记忆（按状态分流）

> 老豆手动触发的工作流工具："按提示词抽临时记忆" / "总结今天" / "存储今天的对话"。

**调用步骤**：

1. 读 `prompts/runtime_memory_extract.md` 提示词模板
2. 按规范抽取当前对话上下文 → 4 层 JSON（含 `snapshot_window` 时间窗）
3. 判断 status：`completed` / `ongoing` / `stalled`
4. 调本工具，传 `memory_json` + `status`
5. 工具按 status 自动分流：
   - `completed` → 调 `write_4layer.py` 写入永久 Memory-OS + 清空临时文件
   - `ongoing/stalled` → 原样覆盖临时记忆文件（按 agent 分文件 + 多任务增量衔接）

**按 agent 分文件**：临时记忆按通道/插件类型分文件，互不污染：

```
runtime_active_state/qq.json          # QQ 通道
runtime_active_state/telegram.json    # Telegram 通道
runtime_active_state/wechat_<openid>.json  # 微信（按用户 openid 区分，老豆有多个微信账号）
runtime_active_state/<agent>.json     # 其他 subagent
```

**多任务结构**：临时文件用 `l2.scenarios[]` 数组 + `_meta.tasks[]` 多任务追踪，同任务覆盖、新任务追加、persona 去重合并。

**时间戳铁律**（避免"刻舟求剑"）：`event_time` / `snapshot_window.from_iso` / `to_iso` 必须是具体 ISO 8601 时间戳，禁止"今天/昨天/上周"等模糊表达。

---

## 安装配置

### 1. 依赖服务

| 服务 | 端口 | 启动方式 | 说明 |
|------|------|----------|------|
| **Neo4j** | 7474 / 7687 | `brew services start neo4j` | 知识图谱，bolt 协议 |
| **Qdrant** | 6333 / 6334 | `brew services start qdrant` | 向量库，HTTP API |
| **Embed Daemon** | 8765 | **自动 spawn** | Embedding HTTP 服务，按需启动 |
| **Reranker Daemon** | 8877 | **自动 spawn** | Reranker HTTP 服务，按需启动 |

> **架构说明（2026-09-06 重构）**：Embed / Reranker daemon 改用 `subprocess.Popen` 直接 spawn（不再依赖 launchd）。
> - 端口无人监听时由 `service_lifecycle.py` 自动拉起（`start_new_session=False`，父进程退出时一起退出）
> - 这避免了 launchd 把 daemon 变成常驻进程的问题
> - Neo4j / Qdrant 仍由 brew 管理（需要常驻）

### 2. 环境变量

在 `memory-os-plugin/` 创建 `.env`：

```bash
MEMORY_OS_NEO4J_URI=bolt://127.0.0.1:7687
MEMORY_OS_NEO4J_USER=neo4j
MEMORY_OS_NEO4J_PASSWORD=***
MEMORY_OS_QDRANT_HOST=127.0.0.1
MEMORY_OS_QDRANT_PORT=6333
MEMORY_OS_EMBEDDING_MODEL=~/.openclaw/workspace/memory-os/models/bge-m3-Q8_0.gguf
MEMORY_OS_HOOK_TRACE_ENABLED=1   # 写 hook 日志（开发调试）
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

### Association Expansion 联想扩散

从种子实体出发，在 Neo4j 图里多跳扩散（默认 2 跳），找到相关记忆后通过 Qdrant 向量搜索联想候选，综合打分（`overlap × 0.4 + depth_decay × 0.25 + importance × 0.2 + temporal × 0.15`）后统一 Reranker 精排。

### Token 安全

Update / Delete 的两阶段 token 存放在 `~/.openclaw/workspace/memory-os/tokens/`，TTL 30 分钟，不受 `/tmp` 清理影响。

### Embed/Reranker 按需 spawn

改用 `subprocess.Popen` + `start_new_session=False`，父进程退出时子进程跟着退出，避免 daemon 变成常驻僵尸。

### 启动自检后台化

插件 register 时不再同步跑 11 项自检（曾经最坏 60s+ 延迟），改 1 秒后后台 fire-and-forget。需要时调 `memory_os_health` 工具。

### 运行时临时记忆按 agent 分文件

不同通道（QQ / 微信 / Telegram / subagent）的临时记忆写到不同文件，互不污染。老豆有多个微信账号，按 `openid` 区分。

---

## 文档索引

| 文档 | 内容 |
|------|------|
| `README.md` | 概览、安装、工具 API、运维、设计决策 |
| `README_recall.md` | 召回流程详解（6 Steps + Association Expansion） |
| `MEMORY-OS-4LAYER.md` | 4 层架构、数据结构、设计决策 |
| `scripts/extract_prompt.md` | LLM 抽取永久记忆的 4 层规范 |
| `prompts/runtime_memory_extract.md` | LLM 抽取运行时临时记忆的规范（含快照窗口 + 按 agent 分文件） |

---

## License

MIT · [KingsdouDD/Memory-OS](https://github.com/KingsdouDD/Memory-OS)