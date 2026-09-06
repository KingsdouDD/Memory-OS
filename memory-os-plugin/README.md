# Memory OS Plugin

> OpenClaw 插件代码层。架构说明、工具 API 概览、安装配置见仓库根目录的 [`README.md`](../README.md)。

---

## 插件结构

```
memory-os-plugin/
├── src/
│   └── index.js              # 插件入口：Hook 注册 + 6 个 MCP 工具
├── scripts/
│   ├── recall_4layer.py      # 4 层融合召回主脚本
│   ├── recall_fusion.py      # RRF + kg_verify + Association Expansion
│   ├── recall_config.py      # 所有召回超参数（参数中心）
│   ├── recall_gate.py        # Hook 门控（长度 / 情绪 / 粗口过滤）
│   ├── write_4layer.py       # 4 层写入 / 更新 / 删除
│   ├── process_dream.py      # Embedding + Qdrant 底层读写
│   ├── embed_daemon.py       # Embedding HTTP 守护进程
│   ├── reranker_daemon.py    # Reranker HTTP 守护进程
│   ├── bm25_index.py         # BM25 全文索引
│   ├── service_lifecycle.py  # 服务生命周期（spawn / health / http_post）
│   ├── cron_runner.py        # 定时任务
│   └── *dedup*.py / *clean*.py  # 运维工具
├── prompts/
│   └── runtime_memory_extract.md  # 运行时临时记忆抽取规范
├── openclaw.plugin.json
├── package.json
├── README.md                 # ← 本文档（插件专属）
└── README_recall.md          # 召回流程详解
```

---

## Hook 实现

插件注册两个 Hook：

| Hook | 作用 |
|------|------|
| `before_prompt_build` | 在每次 LLM 调用前，召回相关记忆并 prepend 到 system prompt |
| `message_received` | 消息接收时记录活动（已禁用 `message_received` 路径，跟 `before_prompt_build` 重复） |

### Hook Gate（recall_gate.py）

| 规则 | 行为 |
|------|------|
| 文字 < 7 字 | 跳过召回 |
| 文字 > 300 字 | 跳过召回 |
| 纯情绪词（嗯/好的/ok/继续） | 跳过 |
| 粗口 | 跳过 |
| 重复字符（"啊啊啊啊啊"） | 跳过 |
| 同会话同 query | md5 去重 |

### 注入格式

```text
【以下是你和用户之间的共同记忆】
这是你们之间真实发生过的往事，回答相关话题时自然想起来用。
请根据当前对话语境，结合记忆来回答
严禁编造、不用"根据记忆"等机械化表达
真实性永远高于"真人感"。

[召回的记忆列表，每条一行]
```

模型被引导把记忆当作"我们之间的共同记忆"，而不是外部资料。

---

## 6 个 MCP 工具（实现）

| 工具 | 入口 | Python 实现 |
|------|------|-------------|
| `memory_os_ingest` | `src/index.js` → `runPython(['ingest', ...], { script: write_4layer.py })` | `scripts/write_4layer.py ingest --file <json>` |
| `memory_os_recall` | `src/index.js` → `runPython(['recall', ...], { script: recall_4layer.py })` | `scripts/recall_4layer.py recall --query ...` |
| `memory_os_update` | 同 ingest / 调 `write_4layer.py update` / `confirm` | `scripts/write_4layer.py update / confirm` |
| `memory_os_delete` | 同 update / `delete` / `confirm` | `scripts/write_4layer.py delete / confirm` |
| `memory_os_health` | 4 端口 lsof 检查 + 自动 spawn + 可选 11 项 selfCheck | `src/index.js` 自实现 |
| `memory_os_extract_runtime` | 读 `prompts/runtime_memory_extract.md` + 写 `runtime_active_state/<agent>.json` | 写入时调 `write_4layer.py ingest` |

### 两阶段更新/删除

Update / Delete 提供两阶段 + 快捷模式：

- **两阶段**：第一阶段传 query 召回候选 + 生成 token；第二阶段带 token 真更新/删除。Token TTL 30 分钟，存 `~/.openclaw/workspace/memory-os/tokens/`
- **快捷模式**：直接传 `target_pid` + `target_collection` + `target_layer`，跳过召回一步到位

### `memory_os_extract_runtime` 工作流

按状态分流：

| status | 动作 |
|--------|------|
| `completed` | 调 `write_4layer.py ingest` 写入永久 Memory-OS + 清空临时文件 |
| `ongoing` | 增量覆盖 `runtime_active_state/<agent>.json`（同任务覆盖 / 新任务追加） |
| `stalled` | 同 ongoing，但额外记录 `blocked_reason` |

按 agent 分文件（不污染其他通道）：

```
runtime_active_state/qq.json                  # QQ 通道
runtime_active_state/telegram.json            # Telegram 通道
runtime_active_state/wechat_<openid>.json     # 微信（按用户 openid）
runtime_active_state/<agent>.json             # 其他 subagent
```

---

## 启动自检（11 项）

插件 register 后**后台执行**（1 秒延迟），不阻塞加载。需要时调 `memory_os_health` 工具按需检查。

| 检查项 | 说明 |
|--------|------|
| Python env + version | Python 可执行文件 |
| `neo4j` / `qdrant_client` / `jieba` 包 | 安装 + 版本 |
| 关键脚本文件 | `write_4layer.py` / `recall_4layer.py` / `process_dream.py` |
| Embedding 模型 | GGUF 文件存在 |
| Token 目录可写 | `~/.openclaw/workspace/memory-os/tokens/` |
| 4 服务端口 | Neo4j / Qdrant / Embed / Reranker |
| Neo4j bolt 认证 | 用户名密码连通性 |
| Qdrant REST API | `GET /readyz` HTTP 200 |

---

## 服务生命周期（`scripts/service_lifecycle.py`）

2026-09-06 重构：从 launchd 改为 `subprocess.Popen` 直接 spawn。

| 行为 | 实现 |
|------|------|
| 端口无人监听 | 自动 `subprocess.Popen` 拉起 daemon |
| 拉起后等待就绪 | 轮询 `/health` HTTP 200（最多 90s） |
| 父进程退出 | `start_new_session=False` 让子进程跟着退出 |
| stdout/stderr | 重定向到 `/tmp/memory-os-embed.log` / `/tmp/memory-os-reranker.log` |

Neo4j / Qdrant 仍由 brew 管理（需要常驻，不在 service_lifecycle 管理范围）。

---

## 运维命令

```bash
# 查看 hook 追踪日志
cat ~/.openclaw/workspace/memory-os/logs/hook-trace.md

# 召回统计
python3 scripts/recall_stats.py

# 开启召回调试日志
MEMORY_OS_RECALL_DEBUG=1 python3 scripts/recall_4layer.py recall --query "..."

# Neo4j 去重清理
python3 scripts/clean_neo4j_dupes.py
python3 scripts/dedup_cleanup.py
```

---

## 文档

| 文档 | 内容 |
|------|------|
| [`../README.md`](../README.md) | 仓库主页面（架构、特色、6 工具概览、安装） |
| [`README_recall.md`](README_recall.md) | 召回流程详解（6 Steps + Association Expansion + 参数表） |
| [`scripts/extract_prompt.md`](scripts/extract_prompt.md) | LLM 抽取永久记忆的 4 层规范 |
| [`prompts/runtime_memory_extract.md`](prompts/runtime_memory_extract.md) | LLM 抽取运行时临时记忆的规范 |