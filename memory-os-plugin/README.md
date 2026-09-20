# Memory OS Plugin

> Neo4j + Qdrant 长期记忆系统，4 层架构，L0/L1/L2/L3 分层管理记忆。

---

## 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                      OpenClaw Gateway                        │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              src/index.js (插件入口)                  │  │
│  │  · 注册 2 个 Hooks                                   │  │
│  │  · 注册 6 个 MCP Tools                               │  │
│  │  · 启动自检（后台，1s 延迟）                         │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                           │
              ┌────────────┴────────────┐
              │                         │
         Hook 触发                    Tool 调用
              │                         │
              ▼                         ▼
┌─────────────────────┐   ┌──────────────────────────────────┐
│  before_prompt_build │   │  6 个 MCP Tools                  │
│  (主动注入记忆块)     │   │  · memory_os_ingest             │
│                     │   │  · memory_os_recall             │
│  message_received   │   │  · memory_os_update              │
│  (已禁用)           │   │  · memory_os_delete              │
└─────────────────────┘   │  · memory_os_health             │
                          │  · memory_os_extract_runtime     │
                          └──────────────────────────────────┘
                                        │
                                        ▼
                          ┌─────────────────────────────┐
                          │   Python 脚本层               │
                          │  recall_4layer.py            │
                          │  write_4layer.py             │
                          │  recall_fusion.py             │
                          │  process_dream.py             │
                          └─────────────────────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              │                         │                         │
              ▼                         ▼                         ▼
     ┌────────────────┐      ┌─────────────────┐     ┌────────────────┐
     │  Neo4j (知识图谱)│      │ Qdrant (向量库)  │     │ BM25 (全文索引) │
     │  L3 persona     │      │ L1/L2 向量检索   │     │ 稀疏文本召回    │
     │  L2 scenario    │      │ ANN 查询         │     │ jieba 分词     │
     │  L1 atom        │      │                  │     │                │
     │  实体/关系/边    │      │                  │     │                │
     └────────────────┘      └─────────────────┘     └────────────────┘
              │                         │                         │
              └─────────────────────────┼─────────────────────────┘
                                        │
                                        ▼
                              ┌─────────────────┐
                              │  Fusion 融合层   │
                              │  RRF + GraphBoost │
                              │  + Importance    │
                              │  + Temporal Decay │
                              └─────────────────┘
                                        │
                                        ▼
                              ┌─────────────────┐
                              │  Reranker 排序  │
                              │  (Qwen3-Reranker│
                              │   0.6B 4bit)    │
                              └─────────────────┘
                                        │
                                        ▼
                              ┌─────────────────┐
                              │  最终 Top-K 输出 │
                              └─────────────────┘

外部服务：
  Neo4j         bolt://127.0.0.1:7687
  Qdrant        http://127.0.0.1:6333
  Embed Daemon  http://127.0.0.1:8765  (BGE-M3 推理服务)
  Reranker     http://127.0.0.1:8877  (Qwen3-Reranker 推理服务)
```

---

## 目录结构

```
memory-os-plugin/
├── src/
│   └── index.js              # 插件入口：Hook 注册 + 6 个 Tool 注册 + 自检
├── scripts/
│   ├── recall_4layer.py      # 召回主脚本（Step 1~4 + Step 3.5 融合）
│   ├── recall_fusion.py       # RRF 融合 + GraphBoost + 时间衰减 + 关联扩展
│   ├── recall_gate.py         # Hook 门控（长度/语气词/敏感词/重复字符）
│   ├── recall_config.py       # 所有召回超参数（参数调优中心）
│   ├── write_4layer.py       # 4 层写入 / 更新 / 删除
│   ├── process_dream.py       # Embedding + Qdrant 底层读写
│   ├── embed_daemon.py        # BGE-M3 Embedding HTTP 守护进程
│   ├── reranker_daemon.py     # Qwen3-Reranker HTTP 守护进程
│   ├── bm25_index.py          # BM25 全文索引
│   ├── service_lifecycle.py   # 服务启停 + 健康检测 + 自动拉起
│   ├── cron_runner.py         # 定时任务
│   └── *dedup*.py / *clean*.py  # 去重和清理脚本
├── prompts/
│   └── runtime_memory_extract.md  # Runtime 临时记忆提取规范
├── model_runtime/
│   ├── __init__.py
│   ├── discovery.py           # 探测当前活跃模型
│   ├── caller.py             # 统一调用入口（多后端路由）
│   └── decide.py             # LLM 驱动的记忆去重决策
├── openclaw.plugin.json       # 插件声明（Hooks + Tools + ConfigSchema）
├── package.json
└── README.md                 # ← 本文件
```

---

## 4 层记忆架构

| 层 | 名称 | 内容 | 存储 |
|----|------|------|------|
| **L0** | 原始场景 | scene_summary / source | Qdrant memory_raw |
| **L1** | 核心记忆 | 最重要的一条原子记忆（事实/偏好/事件/习惯） | Qdrant memory_atom + Neo4j Node |
| **L2** | 场景记忆 | 完整场景描述（title/summary/entities/relations） | Qdrant memory_scenario + Neo4j Node |
| **L3** | 长期认知 | 稳定 persona（长期偏好/关系/身份/观点） | Qdrant memory_persona |

> 每次写入调用 LLM 按 `scripts/extract_prompt.md` 规范抽取 4 层结构，写入 Qdrant 向量库 + Neo4j 知识图谱。

---

## Hook 机制

### before_prompt_build（主动注入）

每次 LLM 调用前触发：从用户文本召回相关记忆，注入到 `prependContext` 拼入 system prompt。

```
用户输入 → recall_gate 门控过滤 → recall_4layer.py 4层融合召回
         → 注入记忆块 → 拼入 system prompt → LLM 收到
```

### message_received（已禁用）

2026-08-20 起禁用，与 `before_prompt_build` 重复触发。

---

## Hook 门控规则（recall_gate.py）

满足任一条件则跳过召回，不写日志（避免噪音）：

| 条件 | 规则 |
|------|------|
| 文本长度 | < 7 字符 或 > 300 字符 |
| 语气词 | 匹配纯语气词列表（嗯/好的/ok/继续 等 30+ 词） |
| 敏感词 | 匹配脏话正则 |
| 重复字符 | `len(set(s)) <= 2 && len(s) > 6`（如"啊啊啊"） |
| 命令式英文 | 动词开头 + 无中文名词 + 词数 < 3 |
| 同会话去重 | 相同 sessionKey + 相同 query MD5 → 跳过 |

注入格式：
```
【以下是你和用户之间的共同记忆】
这是你们之间真实发生过的往事，回答相关话题时自然想起来用。
请根据当前对话语境，结合记忆来回答
严禁编造、不用"根据记忆"等机械化表达
真实性永远高于"真人感"。

[召回的记忆，每条一行]
```

---

## 6 个 MCP Tools

### memory_os_ingest
存入长期记忆。传入 4 层 JSON（推荐 `memory_json` 参数避免 MCP 数组展平），底层调 `write_4layer.py ingest`。

### memory_os_recall
4 层融合召回。返回分层结构：`persona(L3)` / `scenario(L2)` / `atom(L1)` / `raw(L0)`。支持 `layers` 手控召回顺序。

### memory_os_update
两阶段更新。Phase 1 召回候选 + 生成 token；Phase 2 传 token + 新内容确认写入。也支持快捷模式（`target_pid` 直接更新）。

### memory_os_delete
两阶段删除。Phase 1 召回候选 + 生成 token；Phase 2 传 token 确认删除。快捷模式传 `target_pid` + `confirm=true` 直接级联删除（L0/L1/L2/L3 + Neo4j 边一次清干净）。

### memory_os_health
按需服务体检。默认快速模式（< 2s）检查 4 个端口；`deep=true` 跑 11 项完整自检。embed/reranker 端口 up ≠ 模型就绪，会额外探测 `/health` 确认模型热加载完成。服务挂了自动拉起。

### memory_os_extract_runtime
运行时临时记忆抽取。按 `prompts/runtime_memory_extract.md` 规范从当前对话上下文抽取，status 路由：
- `completed` → 写入永久 Memory-OS
- `ongoing` / `stalled` → 覆盖 `runtime_active_state/<agent>.json`

---

## 召回流程（Step by Step）

详见 [`README_recall.md`](README_recall.md)，核心步骤：

```
Step 1  L3 Recall  →  persona 向量召回，过滤实体
Step 2  L2 Recall  →  scenario 向量召回，补充实体 + scenario_ids
Step 2.5 Entity supplement → jieba 从 L2/L3 命中摘要中提取名词
Step 3  Graph Channel  →  Neo4j 1-hop 直接召回（L2 补充的实体触发）
Step 4  Vector Channel  →  Qdrant ANN 召回，L1 atom
Step 4.5 BM25 Channel  →  jieba 分词 + BM25 稀疏召回
Step 3.5 Graph Fusion  →  graph_items 规范化 → 融合到统一候选池
         ↓
       RRF 融合（多通道分数累加）
       GraphBoost（graph 命中 × 1.3）
       时间衰减（180 天半衰期）
       Importance 加权
         ↓
       Reranker 排序（rerank×0.6 + entity_overlap×0.4）
       硬过滤：rerank < 0.55 丢弃
         ↓
       Top-K 输出
```

---

## 服务生命周期

### 4 个依赖服务

| 服务 | 端口 | 管理方式 | 拉起命令 |
|------|------|---------|---------|
| Neo4j | 7687 | brew services | `brew services start neo4j` |
| Qdrant | 6333 | brew services | `brew services start qdrant` |
| Embed Daemon | 8765 | launchd / fallback spawn | `launchctl kickstart gui/501/com.memoryos.embed-daemon` |
| Reranker | 8877 | launchd / fallback spawn | `launchctl kickstart gui/501/com.memoryos.reranker` |

### 自动拉起策略

`src/index.js` 在召回前不做端口检查（避免每次召回延迟 60s+）。服务挂了 → Python 脚本自己报错 → 错误进 hook-trace。

`memory_os_health` 工具按需调用：端口挂了自动走 launchctl kickstart → 等 30s 端口就绪 → 超时走 fallback 直接 spawn Python 跑 daemon。

embed/reranker 端口 up ≠ 模型就绪（模型可能还在加载）。health 工具额外发 `/health` HTTP 请求验证，模型未就绪时走 `relaunchService` 重拉，最多两轮（第二轮强杀旧进程后重拉）。

### 端口冲突检测

`detectPortConflict(port)` 用 `lsof -i :port -P -n` 检测占用进程，返回 `${procName} (PID ${pid})` 描述字符串。

---

## 启动自检（11 项，后台运行）

插件注册后 1s 延迟后台执行，不阻塞插件加载：

```
1. Python 环境 + 版本
2. neo4j / qdrant_client / jieba 包
3. write_4layer.py / recall_4layer.py / process_dream.py 存在性
4. Embedding 模型文件存在
5. Token 目录可写
6. Neo4j 端口在线
7. Qdrant 端口在线
8. Embed Daemon 端口在线
9. Reranker 端口在线
10. Neo4j bolt 认证连通
11. Qdrant REST API /readyz
```

---

## 融合算法

### 动态切换

```python
memory_os_fusion(action="switch", name="rrf")   # 切 RRF
memory_os_fusion(action="reset")                  # 重置回 arithmetic
```

- **arithmetic**（默认）：算术融合 + 减分 ≥ 50% 丢弃，适合精确召回
- **rrf**：RRF 排名累加，不丢弃，适合全量召回场景

---

## 配置项（openclaw.plugin.json）

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `neo4jUri` | bolt://127.0.0.1:7687 | Neo4j 地址 |
| `neo4jUser` | neo4j | Neo4j 用户名 |
| `neo4jPassword` | **必填** | Neo4j 密码 |
| `qdrantHost` | 127.0.0.1 | Qdrant 地址 |
| `qdrantPort` | 6334 | Qdrant 端口（注意：代码里用 6333） |
| `embeddingModel` | ${HOME}/.openclaw/workspace/models/bge-m3-Q8_0.gguf | Embedding 模型路径 |
| `cronSchedule` | `30 3 * * *` | 定时任务 |
| `dedupThreshold` | 0.95 | 去重阈值 |
| `hookTraceEnabled` | true | Hook 日志开关 |
| `fusionAlgorithm` | arithmetic | 融合算法 |
| `runtimeStateTimer` | (见下) | 运行时状态计时器 |

`runtimeStateTimer` 子配置：
- `enabled`：是否启用无活动超时自动探查
- `timeoutMs`：超时毫秒数（默认 3 分钟）
- `fireOnAgentEnd`：agent_end 时是否立刻触发

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MEMORY_OS_PYTHON` | ~/.openclaw/workspace/memory-os/venv/bin/python3 | Python 路径 |
| `MEMORY_OS_HOOK_TRACE_ENABLED` | 1 | 设为 0 关闭 hook-trace.md 写入 |
| `MEMORY_OS_HOOK_LOG` | ~/.openclaw/workspace/memory-os/logs/hook-trace.md | Hook 日志路径 |
| `MEMORY_OS_HOOK_LOG_KEEP_BYTES` | 262144 | Hook 日志滚动字节数 |
| `MEMORY_OS_NEO4J_PASSWORD` | openclaw | Neo4j 密码（环境变量优先） |
| `MEMORY_OS_SESSION_CACHE_ENABLED` | true | 同会话 query 去重缓存 |
| `MEMORY_OS_SESSION_CACHE_TTL` | 0 | 缓存 TTL（0=进程内永久） |
| `MEMORY_OS_RECALL_DEBUG` | 0 | 设为 1 输出 recall-debug.log |
| `MEMORY_OS_FUSION_ALGORITHM` | arithmetic | 融合算法 |

---

## 操作命令

```bash
# 查看 Hook 追踪日志
cat ~/.openclaw/workspace/memory-os/logs/hook-trace.md

# 召回统计
python3 scripts/recall_stats.py

# 开启召回调试日志
MEMORY_OS_RECALL_DEBUG=1 python3 scripts/recall_4layer.py recall --query "..."

# Neo4j 去重清理
python3 scripts/clean_neo4j_dupes.py
python3 scripts/dedup_cleanup.py

# 服务健康检查（快速模式）
python3 -c "
import sys; sys.path.insert(0,'scripts');
from service_lifecycle import ensure_service_up;
for port in [7687, 6333, 8765, 8877]:
    ok = ensure_service_up(port, max_wait=5);
    print(f'port {port}: {\"OK\" if ok else \"FAIL\"}');
"
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [`README_recall.md`](README_recall.md) | 召回流程深度解析（6 Steps + 融合 + 参数表） |
| [`scripts/extract_prompt.md`](scripts/extract_prompt.md) | 永久记忆 4 层抽取规范 |
| [`prompts/runtime_memory_extract.md`](prompts/runtime_memory_extract.md) | Runtime 临时记忆抽取规范 |
| [`model_runtime/README.md`](model_runtime/README.md) | 统一模型运行时（discovery/caller/decide） |
