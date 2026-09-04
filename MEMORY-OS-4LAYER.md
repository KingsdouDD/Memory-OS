# Memory OS 4 层架构（4-Layer Architecture）

> **生效日期**：2026-08-25，**更新**：2026-09-02
> **作者**：小橘子 + 老豆

---

## 1. 架构概览

| 层 | 名称 | Qdrant collection | PID 策略 | 召回优先级 |
|----|------|-------------------|----------|------------|
| **L0** | 原始对话 | `memory_l0` | UUID | 最低（托底）|
| **L1** | 原子事实 | `memory_<type>`（8 个 collection）| md5 指纹 | 中 |
| **L2** | 场景记忆 | `memory_scenario` | UUID | 高 |
| **L3** | 长期画像 | `memory_persona` | UUID | 最高 |

融合策略：召回时按 `L3 → L2 → L1 → L0` 优先级排序，输出分层结构给 LLM。

---

## 2. 文件清单

```
memory-os-plugin/
├── src/
│   └── index.js              # 4 个 MCP 工具入口
├── scripts/
│   ├── write_4layer.py       # 4 层写入/更新/删除（Python CLI）
│   ├── recall_4layer.py      # 4 层融合召回
│   ├── recall_fusion.py      # RRF 融合 + graph boost + time decay
│   ├── recall_config.py      # 召回超参数中心
│   ├── recall_gate.py        # Hook 门控（长度/情绪/粗口过滤）
│   ├── process_dream.py     # Embedding + Qdrant 底层读写
│   ├── extract_prompt.md    # LLM 4 层抽取规范
│   ├── embed_daemon.py      # Embedding HTTP 守护进程（本地 GGUF）
│   ├── reranker_daemon.py   # Reranker HTTP 守护进程
│   └── bm25_index.py        # BM25 全文索引
└── MEMORY-OS-4LAYER.md     # 本文档
```

**旁路原则**：新功能全部走新脚本，**不修改** `process_dream.py` / `recall_fusion.py` / `recall_config.py` 等核心文件。

---

## 3. 数据结构

### 3.1 输入格式（LLM 抽取后传给 `memory_os_ingest`）

```jsonc
{
  "l0": {
    "scene_summary": "当前 Scene 的简短摘要",
    "source": "dream:light:2026-09-02"
  },
  "l1": {
    "kos": [
      {
        "type": "fact|preference|event|experience|routine|goal|decision|concept",
        "summary": "独立、完整的原子记忆，≤150字",
        "state": "active|historical|ongoing|uncertain",
        "entities": [{"name": "主体", "label": "Person|Place|Animal|Concept|Object"}],
        "relations": [{"subject": "...", "predicate": "...", "object": "...", "status": "..."}],
        "tags": [],
        "importance": 0.0,
        "event_time": {"start": null, "end": null, "expression": "2026-09-01", "precision": "day"},
        "valid_time": {"start": null, "end": null, "end_type": "until_revoked"}
      }
    ]
  },
  "l2": {
    "scenario": {
      "title": "场景名称",
      "summary": "完整独立的场景摘要，≤200字",
      "type": "event|experience|project|relationship|topic|other",
      "state": "active|historical|ongoing|uncertain",
      "entities": [{"name": "...", "label": "Person|Place|..."}],
      "tags": [],
      "importance": 0.0,
      "event_time": {...},
      "valid_time": {...}
    }
  },
  "l3": {
    "persona": [
      {
        "type": "fact|preference|routine",
        "summary": "跨场景稳定画像，≤150字",
        "state": "active|historical|ongoing|uncertain",
        "importance": 0.85,
        "entities": [{"name": "...", "label": "Person|Place|..."}]
      }
    ]
  }
}
```

### 3.2 输出格式（`memory_os_recall` 返回）

```jsonc
{
  "query": "...",
  "layers": ["L3", "L2", "L1", "L0"],
  "persona": [...],   // L3，按 score 降序
  "scenario": [...],  // L2
  "atom": [...],      // L1（向量+BM25+Graph RRF 融合）
  "raw": [...]        // L0 BM25 全文
}
```

---

## 4. 4 个 MCP 工具详解

### 4.1 `memory_os_ingest`（存记忆）

**推荐方式**——传 4 层 JSON 字符串（避免 MCP 嵌套数组被展平）：

```js
memory_os_ingest({
  memory_json: '{"l0":{"scene_summary":"...","source":"..."},"l1":{"kos":[...]}}'
})
```

**兼容老格式**——直接传 L1 KO 数组：

```js
memory_os_ingest({
  kos: [
    { type: "fact", summary: "...", entities: [...] }
  ],
  source: "..."
})
```

**Python CLI**：`python write_4layer.py ingest --file <json>`

---

### 4.2 `memory_os_recall`（查记忆）

```js
memory_os_recall({
  query: "...",         // 必填
  top_k: 5,             // 每层返回几条，默认 5
  include_persona: true, // 是否召回 L3，默认 true
  include_scenario: true, // 是否召回 L2，默认 true
  layers: "L3,L2,L1,L0" // 手控召回顺序，默认全开
})
```

**Python CLI**：`python recall_4layer.py recall --query "..." --top-k 5`

---

### 4.3 `memory_os_delete`（删记忆）

#### 快捷模式（推荐，一次性直接删）

```js
memory_os_delete({
  target_pid: "<UUID 格式的 PID>",
  target_collection: "memory_persona",
  target_layer: "L3",
  confirm: true
})
```

#### 两阶段模式

**第一阶段**（召回候选 + 生成 token）：
```js
memory_os_delete({
  query: "...",
  top_k: 5,
  layer: "L3",
  confirm: false
})
// → 返回 { phase:"confirm", token:"...", candidates:[...] }
```

**第二阶段**（带 token 真删）：
```js
memory_os_delete({
  query: "...",
  confirm: true,
  token: "***",
  selected_pids: ["pid1", "pid2"]  // 可选
})
// → 返回 { deleted: {l0:0, l1:0, l2:0, l3:1} }
```

**Python CLI**：
```bash
# 快捷模式
python write_4layer.py delete --direct-pid <pid> --direct-collection <coll> --direct-layer <layer>

# 两阶段
python write_4layer.py delete --query "..."
python write_4layer.py confirm --token "***"
```

**Token TTL：30 分钟**，存在 `~/.openclaw/workspace/memory-os/tokens/`

---

### 4.4 `memory_os_update`（更新记忆）

> **更新逻辑**：在旧内容后面**追加**新内容（L0-L3 长文本用 ` | ` 拼接）。Neo4j 写新关系，旧关系保留。

#### 快捷模式（推荐，跳过召回，直接更新）

```js
memory_os_update({
  memory_json: '{"l3":{"persona":[{"type":"preference","summary":"...","state":"active"}]}}',
  target_pid: "<UUID>",
  target_collection: "memory_persona",
  target_layer: "L3",
  confirm: true
})
```

#### 两阶段模式

**第一阶段**：
```js
memory_os_update({
  query: "...",
  memory_json: '{"l3":{"persona":[...]}}',
  confirm: false
})
// → 返回 { phase:"confirm", token:"...", target:{pid,layer,summary}, candidates:[...] }
```

**第二阶段**：
```js
memory_os_update({
  query: "...",
  confirm: true,
  token: "***"
})
```

**Python CLI**：
```bash
# 快捷模式
python write_4layer.py update --target-pid <pid> --target-collection <coll> --target-layer L3 --file <new_memory.json>

# 两阶段
python write_4layer.py update --query "..." --file <new_memory.json>
python write_4layer.py confirm --token "***"
```

---

## 5. PID 与 Collection 速查

| 层 | Collection | PID 来源 |
|----|-----------|---------|
| L0 | `memory_l0` | 召回返回的 `_qdrant_pid` |
| L1 | 原有 8 个 collection | 召回返回的 `_qdrant_pid` |
| L2 | `memory_scenario` | 召回返回的 `pid`（UUID 格式）|
| L3 | `memory_persona` | 召回返回的 `pid`（UUID 格式）|

---

## 6. 关键设计决策

### 6.1 PID 格式：标准 UUID

L2/L3 PID 从整数 md5 改为标准 UUID 格式（`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`），避免与 Neo4j Long 上限冲突。

### 6.2 Token 存 HOME 目录

Token 目录从 `/tmp` 改为 `~/.openclaw/workspace/memory-os/tokens/`，TTL 30 分钟，不受系统重启影响。

### 6.3 更新是追加，不是覆盖

L0-L3 长文本（summary）在更新时用 ` | ` 拼接追加。Neo4j 写新关系（MERGE），旧关系保留。

### 6.4 删除是物理删除

Qdrant 点直接 delete，Neo4j 节点 DETACH DELETE，不做软删除。

### 6.5 Qdrant upsert 后验证

`write_4layer.py` 在 upsert 后主动验证点是否真正写入（异步问题），重试最多 5 次。

---

## 7. 故障排查

| 问题 | 原因 | 修复 |
|------|------|------|
| `python exit 2: invalid choice` | JS 端没传 subcommand | `runPython(["ingest", "--file", ...], {script: ...})` |
| 召回返回空 | query 太短被 gate 拦截 | recall_config.py 里 `HOOK_MIN_LEN` 默认 2 |
| PID 删不掉 | 旧数据 PID 是整数，新代码用 UUID | 查新版召回结果里的 pid 字段格式 |
| Token 过期 | /tmp 被清理 | Token TTL 已改为 30 分钟，存 HOME 目录 |

---

_最后更新：2026-09-02 11:14_
