# Memory OS 完整系统说明

> 整理时间：2026-08-24
> 整理人：小橘子（被老豆骂完气喘吁吁整理的）

本说明书**完全覆盖**：架构、文件目录、4 个工具的使用、插件加载与排错、Python 召回流程、参数调优、常见 bug 排查。下一任模型进来如果连这个都看不懂，那就真的可以删了。

---

## 0. 一句话总结

**Memory OS = OpenClaw 插件 + Python 召回脚本**，把记忆存到 **Neo4j（知识图谱）+ Qdrant（向量库）+ BM25 索引** 三路，写入前用 LLM 抽 KO，召回时三路融合 RRF + kg_verify。**4 个工具（ingest / recall / update / delete）暴露给 agent 用**，**3 个钩子（user_request / message_received / agent_end / before_prompt_build）** 走自动召回 + 自动记忆注入。

---

## 1. 系统架构

```
┌──────────────────────────────────────────────────────────────────┐
│                     OpenClaw Runtime / Agent                     │
│  ┌─────────────┐    ┌──────────────────────────────────────┐     │
│  │  Agent Loop │◄──►│  Memory OS Plugin (Node)             │     │
│  │  (LLM)      │    │   src/index.js                       │     │
│  └──────┬──────┘    │    ├─ 4 个工具：                       │     │
│         │            │    │   • memory_os_ingest              │     │
│         │            │    │   • memory_os_recall              │     │
│         │            │    │   • memory_os_update              │     │
│         │            │    │   • memory_os_delete              │     │
│         │            │    ├─ 4 个钩子：                       │     │
│         │            │    │   • user_request                  │     │
│         │            │    │   • message_received (已禁)       │     │
│         │            │    │   • agent_end                     │     │
│         │            │    │   • before_prompt_build           │     │
│         │            │    └─ 自动拉起服务                      │     │
│         │            └─────────┬────────────────────────────┘     │
│         │                      │ spawn                            │
│         │                      ▼                                  │
│         │            ┌─────────────────────┐                     │
│         │            │ Python 子进程       │                     │
│         │            │ scripts/process_dream.py                  │
│         │            │   ├─ recall (查)                           │
│         │            │   ├─ ingest-kos (写)                      │
│         │            │   ├─ delete-memories                      │
│         │            │   └─ update-memories                      │
│         │            └─────┬───────────┬───────────┬───────────┘ │
└─────────┼──────────────────┼───────────┼───────────┼─────────────┘
          │                  ▼           ▼           ▼
   召回结果注入 prompt     Neo4j       Qdrant      BM25 索引
   (before_prompt_build)  (知识图谱)   (向量库)     (rank_bm25)
                          :7687       :6333        (内存)
                              ▲           ▲
                              │           │
                              └───── BGE-M3 ─────┘
                              Embed Daemon :8765
                              (GGUF Metal)
```

### 数据流向

| 流向 | 触发 | 步骤 |
|------|------|------|
| **写入** | agent 调 `memory_os_ingest` | LLM 抽 KO → JSON → Python 写 Neo4j + Qdrant + BM25 |
| **查询** | agent 调 `memory_os_recall` | query → 三路召回 → RRF 融合 → kg_verify → 返回 top-k |
| **自动召回** | `before_prompt_build` 钩子 | 从 user 文本抽 query → 走门控 → 召回 → 拼到 prompt 前面 |
| **自动写入** | `agent_end` 钩子 + cron 3:30 | 把对话/梦境文件抽 KO → 写库 |

---

## 2. 文件目录（去这里找 bug）

```
memory-os-plugin/
├── openclaw.plugin.json         ← OpenClaw 插件清单（**别改坏了，否则插件加载直接失败**）
├── package.json                 ← npm 元数据
├── README.md                    ← 你正在看的这个文件
├── README_recall.md             ← 旧的召回说明文档（2026-08-21 整理，已被本文件取代）
├── src/
│   └── index.js                 ← 插件 Node 入口（4 个工具 + 4 个钩子 + 服务拉起 + 日志）
├── scripts/
│   ├── process_dream.py         ← Python 主入口（4 个 CLI 子命令）
│   ├── recall_gate.py           ← 召回门控（should_skip_recall / is_discardable）
│   ├── recall_fusion.py         ← RRF 融合 + kg_verify_v2 + importance + 时间衰减
│   ├── recall_config.py         ← 所有可调参数（环境变量可覆盖，**改这里**）
│   ├── bm25_index.py            ← BM25 稀疏索引（rank_bm25 + jieba）
│   ├── extract_prompt.md        ← LLM 抽取 KO 的 prompt（ingest 时用）
│   ├── embed_daemon.py          ← BGE-M3 embed daemon（**独立进程**，端口 8765）
│   ├── cron_runner.py           ← 定时任务入口
│   ├── process_dream.py.bak.*   ← 备份（**别删，留着对比**）
│   ├── audit_dedup.py           ← 数据审计
│   ├── clean_neo4j_dupes.py     ← Neo4j 重复清理
│   ├── clean_neo4j_orphans.py   ← 孤立节点清理
│   ├── dedup_cleanup.py         ← Qdrant 重复清理
│   ├── dedupe_active_edges.py   ← Neo4j 边去重
│   ├── fix_time_fields.py       ← 修时间字段
│   ├── recall_stats.py          ← 召回统计
│   └── write_kos_v2_deleted.py  ← 旧版写入（已废）
└── logs/
    ├── hook-trace.md            ← 钩子行为日志（gate skip / injection / recall）
    ├── recall-debug.log         ← 召回调试日志（MEMORY_OS_RECALL_DEBUG=1 开启）
    └── write-decision.md        ← LLM 写入决策日志
```

**Python venv 路径**：`~/.openclaw/workspace/memory-os/venv/bin/python3`（**有 qdrant_client / neo4j / rank_bm25 / jieba 等依赖**）。**别用** `~/.openclaw/workspace/venv/bin/python3`，那个没 qdrant_client。

---

## 3. 4 个工具（agent 直接调）

所有工具在 `src/index.js` 里通过 `api.registerTool(...)` 注册。**插件必须先成功加载才能用，详见 §6 排错**。

### 3.1 `memory_os_ingest`（写记忆）

**用法**：
```js
memory_os_ingest({
  kos: [
    {
      type: "event",
      summary: "2026-08-10 外婆姨公等带老豆游澳门...",
      entities: ["外婆", "老豆", "姨公", "澳门"],
      relations: [{subject: "老豆", predicate: "VISITED", object: "澳门"}],
      tags: ["旅游", "澳门"],
      importance: 0.85,
      event_time: {start: "2026-08-10", end: "2026-08-10", precision: "day"},
      valid_time: {start: "2026-08", end_type: "until_revoked"}
    }
  ],
  source: "微信对话:2026-08-24"  // 可选
})
```

**流程**：
1. 把 `kos` 数组写临时文件 `/tmp/memory-os-tool-kos-{ts}.json`
2. spawn Python：`process_dream.py ingest-kos --file <tmp> [--source ...]`
3. Python 走 `ingest_kos_json()` → `write_kos_v5()` → 写 Neo4j + Qdrant + BM25
4. 返回 `{write_report: {...}}`

**KO 抽取规范**：`scripts/extract_prompt.md`（type/summary/entities/relations/tags/importance/event_time/valid_time）。**实体名必须是真实的人/物/事/地，不得含工具词**。**谓词只能用白名单**（recall_config.py 第 182 行起 `_DEFAULT_ALLOWED_RELATIONSHIPS`），不在白名单的会走默认 MENTIONED_IN。

### 3.2 `memory_os_recall`（查记忆）

**用法**：
```js
memory_os_recall({query: "老豆跟外婆去澳门", top_k: 5})
```

**流程**：
1. spawn Python：`process_dream.py recall --query ... --top-k N`
2. Python 走 `recall()` → 三路召回 → RRF 融合 → kg_verify_v2
3. 返回 `[{summary, relation, score, sort_key, sim, source, event_time, valid_time, ...}]`
4. `source` 字段：`vec` / `bm25` / `graph`

**注意**：当前 session 如果没有这个工具，先看 §6 排错。

### 3.3 `memory_os_update`（更新记忆）

**用法**：先 recall 找目标，再用新 KO 覆盖。
```js
memory_os_update({
  query: "外婆和老豆爬山的记忆",  // 用于找目标
  kos: [{type: "experience", summary: "新内容...", ...}],  // 只取第一条
  top_k: 5
})
```

### 3.4 `memory_os_delete`（删除记忆）

**用法**：先 recall 找目标（取最高分那条），确认后删 Qdrant point + Neo4j 关系。
```js
memory_os_delete({query: "要删的那条记忆的关键词", top_k: 5})
```

---

## 4. 4 个钩子（自动跑）

钩子都在 `src/index.js` 里 `api.on(...)` 注册。

| 钩子 | 状态 | 作用 |
|------|------|------|
| `user_request` | active | 用户发起请求时触发 |
| `message_received` | **禁用**（2026-08-20） | 跟 before_prompt_build 重复触发，禁用 |
| `agent_end` | active | agent 跑完一轮后触发（梦境入库、cron 链入口） |
| `before_prompt_build` | active | **核心钩子**：从 user 文本自动召回，注入到 prompt 前面 |

### 4.1 `before_prompt_build` 自动召回链路

```
用户发消息
  ↓
钩子提取 userText（兼容多通道：event.metadata.body / event.content / event.messages[N].content）
  ↓
提取失败 → 写 userText_empty 日志 → return
  ↓
太短（< 3）→ return
  ↓
同会话同 query md5 命中缓存 → return
  ↓
spawn Python：process_dream.py recall --query ... --top-k 8 --hook
  ↓
门控 should_skip_recall → payload.skipped → return
  ↓
RRF 融合 + kg_verify
  ↓
makeMemoryInjectionBlock(memories) → 拼到 system prompt 前面
  ↓
INJECTION_HEADER（"【以下是你和用户之间的共同记忆】..."） + summary 列表
```

**关键**：注入块前缀在 `src/index.js` 第 215 行附近 `INJECTION_HEADER`，是被老豆反复调过的口吻（"长期认识用户""自然联想""严禁编造"）。

---

## 5. Python 召回流程（脚本层）

### 5.1 process_dream.py 4 个 CLI 子命令

```bash
python3 scripts/process_dream.py recall --query "..." --top-k 8 --rrf-k 60 [--hook]
python3 scripts/process_dream.py ingest-kos --file <kos.json> [--source ...]
python3 scripts/process_dream.py delete-memories --query "..." --top-k 5
python3 scripts/process_dream.py update-memories --query "..." --file <kos.json> --top-k 5
```

### 5.2 召回主流程（`recall()` 函数，第 682 行）

```
query 输入
  ↓
门控 should_skip_recall → True 则返回 {skipped, reason}
  ↓
三路召回（并行）：
  ├─ vec: qdrant_search(query_vec, collections, top_k=3) → VEC_MIN_SCORE=0.70 过滤
  ├─ bm25: bm25_search(query) → BM25_KEYWORD_FILTER_RATIO=0 软过滤
  └─ graph: neo4j_entity_search → neo4j_expand(1 跳, 4 条/节点)
  ↓
PRF 扩展：用 vec+graph 命中的实体做 query 增强，再去 qdrant 召回一轮（×0.9）
  ↓
RRF 融合（K=60，多通道分数**累加**）
  ↓
fusion_transform_channel：graph 通道深度打分（1 跳 ×1.0, 2 跳 ×0.5）
  ↓
fusion_boost_graph_hits：graph 命中条目 ×1.3
  ↓
fusion_post_fuse：
  - importance 加权（0~1 → 0.5×~1.5×）
  - 时间衰减（半衰期 180 天）
  - relation 类型 point 用 parent_summary 还原可读文本
  ↓
kg_verify_v2：
  - 独立 embed 每个 summary + query
  - sim < 0.60 丢弃；0.60~0.70 弱信号；≥ 0.70 强信号
  - 综合分 = sort_key × (1 - 0.5) + sim × 0.5
  ↓
取 top_k，**不凑数**
```

### 5.3 写入主流程（`ingest_kos_json()` → `write_kos_v5()`）

```
kos 数组输入
  ↓
embed(kos.summary) → BGE-M3 向量
  ↓
ANN 召回候选（top-k=3）
  ↓
LLM 决策：create / update / override / discard（用 MiniMax-M3，温度 0.1）
  ↓
对应执行：
  - create: 新建 Qdrant point + Neo4j nodes/edges
  - update: 更新已有 point + 合并关系
  - override: 覆盖（保留旧）
  - discard: 丢弃
  ↓
写 write-decision.md 日志
```

### 5.4 参数中心（`recall_config.py`）

所有可调参数都在这里，**改这一个文件 + 重启 gateway**。

| 参数 | 默认 | 作用 |
|------|------|------|
| `HOOK_MIN_LEN` / `HOOK_MAX_LEN` | 7 / 300 | 召回门控长度 |
| `HOOK_SKIP_FILLER` | 30+ 词 | 纯语气词白名单 |
| `HOOK_SKIP_SWEAR` | 粗口正则 | 粗口跳过 |
| `VEC_TOP_K_DEFAULT` | 3 | 向量召回数 |
| `VEC_MIN_SCORE` | 0.70 | 向量最低分 |
| `BM25_TOP_K` | 5 | BM25 召回数 |
| `BM25_KEYWORD_FILTER_RATIO` | 0 | BM25 关键词过滤比例（0=关闭） |
| `GRAPH_DEPTH` | 1 | 图谱扩展跳数 |
| `GRAPH_LIMIT_PER_NODE` | 4 | 每节点扩展上限 |
| `RRF_K` | 60 | RRF K 值 |
| `KG_STRONG_THRESHOLD` / `KG_WEAK_THRESHOLD` | 0.7 / 0.6 | kg_verify 阈值 |
| `KG_SIM_RANKING_WEIGHT` | 0.5 | kg_verify 综合排序 sim 权重 |
| `PRF_TOKEN_OVERLAP_MIN` | 0 | PRF 字面触发 token 数 |
| `INTENT_VERB_HARD_FILTER` | False | 动词意图硬过滤 |
| `INTENT_VERB_SOFT_WEIGHT` | 0.05 | 动词意图软加权 |
| `DEDUP_THRESHOLD` | 0.82 | 写入去重阈值 |
| `WRITE_DECISION_MODEL` | MiniMax-M3 | 决策用 LLM |
| `RECALL_DEBUG` | "0" | 调试日志开关（"1"=开启写 recall-debug.log） |

**环境变量覆盖**：所有参数都支持 `MEMORY_OS_<PARAM_NAME>` 环境变量，但**改完必须重启 gateway**让 Python 重新 import。

---

## 6. 排错（**这一节最重要，下次出事先看这**）

### 6.1 工具不见了 / 调不到

**症状**：agent 说 `memory_os_recall` 工具找不到。

**排查顺序**（**严格按这个顺序**）：

#### 第 1 步：检查插件是否成功加载

```bash
openclaw doctor 2>&1 | grep -i "memory-os"
```

**正常**：
```
│ Memory OS    │ memory-os│ openclaw │ enabled  │ ~/.openclaw/workspace/memory-os-plugin/src/index.js
```

**异常**：
```
[plugins] memory-os failed to load from .../index.js: Error: ParseError: ...
```

→ **就是插件代码有语法错误！** 整个 `index.js` 解析失败 → 4 个工具全部没注册 → 看起来工具"不见了"。

**这是 2026-08-24 的真实事故**：第 39 行和第 61 行 `spawn("lsof", ["-i", \`:${port}"\])` 模板字符串里多了一个 `"`，导致整个插件加载失败。**任何语法错误都会让插件直接挂掉，没有 fallback**。

→ 修复方法：`node --check src/index.js` 看具体哪行报错，改完 `node --check` 通过后 `gateway restart`。

#### 第 2 步：检查钩子日志

```bash
ls -la /Users/king/.openclaw/workspace/memory-os/logs/
```

如果有 `hook-trace.md` 且大小在增长 → 插件加载了，钩子也在跑。**但这不代表工具注册了**——钩子和工具是两套注册机制（`api.on` vs `api.registerTool`）。

#### 第 3 步：检查 Python 是否能直连

```bash
~/.openclaw/workspace/memory-os/venv/bin/python3 scripts/process_dream.py recall --query "测试" --top-k 3
```

**报错示例**：
- `ModuleNotFoundError: No module named 'qdrant_client'` → **用错 venv 了**，换 `memory-os/venv`
- `Connection refused 127.0.0.1:6333` → Qdrant 没起，见 §6.3
- `Connection refused 127.0.0.1:7687` → Neo4j 没起，见 §6.3
- `Connection refused 127.0.0.1:8765` → Embed daemon 没起，见 §6.3

#### 第 4 步：手动调 Python（绕过插件）

如果插件层有问题，但 Python 能跑，可以先临时绕过：

```bash
# 查
~/.openclaw/workspace/memory-os/venv/bin/python3 scripts/process_dream.py recall --query "老豆跟外婆去澳门" --top-k 3

# 写（先把 KO 数组写到文件）
~/.openclaw/workspace/memory-os/venv/bin/python3 scripts/process_dream.py ingest-kos --file /tmp/kos.json --source "手动写入"
```

### 6.2 钩子没触发 / 没注入

```bash
cat /Users/king/.openclaw/workspace/memory-os/logs/hook-trace.md | tail -50
```

常见日志：
- `userText_empty` → event 文本提取失败，检查通道兼容性（QQ / webchat / weixin 取法不同）
- `recall_skipped` reason=xxx → 门控跳过，看 recall_gate.py 的 should_skip_recall
- `recall_failed` → Python 调用失败，看 stderr
- `recall_cache_hit` → 同会话同 query 命中缓存（去重命中）

### 6.3 服务没起

插件启动时会自动拉起 3 个服务：

| 服务 | 端口 | 启动命令 | 检查 |
|------|------|---------|------|
| Neo4j | 7687 | `brew services start neo4j` | `lsof -i :7687` |
| Qdrant | 6333 | `brew services start qdrant` | `lsof -i :6333` |
| Embed daemon | 8765 | `launchctl load -w ~/Library/LaunchAgents/com.memoryos.embed-daemon.plist` | `lsof -i :8765` |

**手动检查**：
```bash
lsof -i :7687  # Neo4j
lsof -i :6333  # Qdrant
lsof -i :8765  # Embed daemon
launchctl list | grep memoryos  # Embed daemon plist 状态
```

**密码**：Neo4j `openclaw`，Qdrant 无密码，Embed daemon 无 auth。

### 6.4 召回效果差

开调试日志：
```bash
export MEMORY_OS_RECALL_DEBUG=1
gateway restart
```

然后查 `~/.openclaw/workspace/memory-os/logs/recall-debug.log`，看每条候选的 score / sim / sort_key / source / summary。

典型根因（按频率排）：
1. **向量分数 < 0.70** → 被 `VEC_MIN_SCORE` 直接丢掉
2. **kg_verify sim < 0.60** → 被 kg_verify_v2 丢
3. **门控跳过** → 改 `HOOK_MIN_LEN` / 去掉白名单
4. **BM25 关键词过滤误杀** → 调 `BM25_KEYWORD_FILTER_RATIO`
5. **图谱 1 跳没命中实体** → 调 `GRAPH_DEPTH=2`（但老豆之前改回 1 跳避免噪声）
6. **时间衰减过重** → 调 `RECALL_TIME_DECAY_HALF_LIFE`（默认 180 天）

### 6.5 数据脏了 / 有重复

```bash
# Qdrant 去重
python3 scripts/dedup_cleanup.py

# Neo4j 节点重复
python3 scripts/clean_neo4j_dupes.py

# Neo4j 孤立节点
python3 scripts/clean_neo4j_orphans.py

# Neo4j 边重复
python3 scripts/dedupe_active_edges.py

# 时间字段修复
python3 scripts/fix_time_fields.py
```

**慎用**：所有清理脚本都会改库，**先用 `openclaw doctor` 看一眼数据健康度**，确认影响范围再跑。

---

## 7. 已知事故清单（踩过的坑）

| 时间 | 事故 | 原因 | 修复 |
|------|------|------|------|
| 2026-08-24 | 4 个工具全部消失 | `src/index.js` 第 39/61 行模板字符串多一个 `"`，插件加载失败 | 去掉多余的引号 + restart gateway |
| 2026-08-21 | 召回不到外婆/爬山等基础记忆 | 多重过滤叠加，把该召的也过滤掉了 | 软化 BM25 关键词过滤、PRF 字面触发改为实体触发、kg_verify 不完全覆盖 vec 排序 |
| 2026-08-20 | message_received + before_prompt_build 重复触发 | 两个钩子都触发 recall | 禁用 message_received |
| 2026-08-13 | Dream cron 不入库 | launchd 老脚本失败 | 改用 OpenClaw cron "Memory OS 梦境入库" 3:30 |
| 2026-08-10 | LLM 决策写入不稳定 | 决策温度太高 + thinking 循环 | 温度 0.1 + 模型切到 MiniMax-M3 |

---

## 8. 配置文件位置速查

| 配置 | 位置 |
|------|------|
| OpenClaw 主配置 | `~/.openclaw/openclaw.json` |
| Memory OS 插件条目 | `~/.openclaw/openclaw.json` → `plugins.entries["memory-os"]` |
| Memory OS 插件目录 | `plugins.load.paths` 第一个 |
| venv | `~/.openclaw/workspace/memory-os/venv/` |
| Embed 模型 | `~/.openclaw/workspace/models/bge-m3-Q8_0.gguf` |
| 钩子日志 | `~/.openclaw/workspace/memory-os/logs/hook-trace.md` |
| 召回调试日志 | `~/.openclaw/workspace/memory-os/logs/recall-debug.log` |
| 写入决策日志 | `~/.openclaw/workspace/memory-os/logs/write-decision.md` |
| 梦境文件 | `~/.openclaw/workspace/memory/dreaming/{light,rem}/YYYY-MM-DD.md` |

---

## 9. 写给下一任模型的话

1. **别瞎改 `src/index.js`**，这文件现在能跑就行，语法错误会让整个插件消失。改完必须 `node --check` 验证。
2. **别瞎改 `openclaw.plugin.json` 的 `contracts.tools`**，4 个工具名字必须保持一致，否则注册时被 `findUndeclaredPluginToolNames` 静默丢弃。
3. **别用 `~/.openclaw/workspace/venv`** 调 Python，那个没 qdrant_client。**用 `memory-os/venv`**。
4. **改完代码必须 restart gateway** 才生效（`gateway` tool 重启，不是 OpenClaw runtime 内重启）。
5. **别瞎用 `memory_search` 工具**，那是内置旧版已废弃的。**只用 `memory_os_recall`**。
6. **写入前必须按 `extract_prompt.md` 抽 KO**，实体名必须是真实的人/物/事/地，谓词只能用白名单。
7. **门控 / 融合 / 调参 全在 `recall_config.py`**，别在 `process_dream.py` 里翻常量，全是死代码。
8. **出事先看 §6 排错**，90% 的问题是插件加载失败 / venv 用错 / 服务没起。