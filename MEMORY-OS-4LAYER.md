# Memory OS 4 层架构（4-Layer Architecture）

> **生效日期**：2026-08-25
> **作者**：小橘子 + 老豆
> **召回率**：L0+L1+L2+L3 全开 vs L1-only，平均 +386%

## 1. 架构概览

Memory OS 把长期记忆分成 4 层存储，每层独立通道召回、独立数据结构：

| 层 | 名称 | Qdrant collection | Neo4j 节点 | PID 策略 | 召回通道 | 优先级 |
|----|------|-------------------|------------|----------|----------|--------|
| **L0** | 原始对话 | `memory_l0` | `:L0Conversation` + `:GENERATED` 边 | UUID | BM25 全文 | 最低（托底）|
| **L1** | 原子事实 | `memory_<type>` (原 6 个) | 实体 + 关系（原路径）| md5 指纹 | vec + graph PRF | 中 |
| **L2** | 场景记忆 | `memory_scenario` | `:Scenario` + `:INVOLVES` 边 | md5 指纹 | vec | 高 |
| **L3** | 长期画像 | `memory_persona` | `:Persona` | md5 指纹 | vec | 最高 |

**融合策略**：召回时按 `L3 → L2 → L1 → L0` 优先级排序，输出分层结构给 LLM。

## 2. 文件清单

```
memory-os-plugin/
├── scripts/
│   ├── write_4layer.py          # 4 层写入器（旁路，不动 process_dream.py）
│   ├── recall_4layer.py         # 4 层召回器（旁路，不动 process_dream.py）
│   └── test_4layer_batch.py     # 批量验证脚本
├── src/
│   └── index.js                 # 4 个 MCP 工具（升级到 4 层）
└── MEMORY-OS-4LAYER.md          # 本文档
```

## 3. 数据结构

### 3.1 输入格式（LLM 抽取后传给 `memory_os_ingest`）

```jsonc
{
  "l0": {
    "scene_summary": "当前 Scene 的简短摘要",
    "source": "dream:light:2026-08-25"
  },
  "l1": {
    "kos": [
      {
        "type": "fact|preference|event|experience|routine|goal|decision|concept",
        "summary": "独立、完整的原子记忆，≤80字",
        "state": "active|historical|ongoing|uncertain",
        "entities": [{"name": "...", "label": "Person|Place|..."}],
        "relations": [{"subject": "...", "predicate": "...", "object": "...", "status": "..."}],
        "tags": [],
        "importance": 0.0,
        "event_time": {"start": null, "end": null, "expression": null, "precision": "unknown"},
        "valid_time": {"start": null, "end": null, "end_type": "until_revoked"}
      }
    ]
  },
  "l2": {
    "scenario": {           // 可选，单个对象；不够格生成时为 null
      "title": "场景名称",
      "summary": "完整独立的场景摘要",
      "type": "event|experience|project|relationship|topic|other",
      "state": "active|historical|ongoing|uncertain",
      "entities": [],
      "relations": [],
      "tags": [],
      "importance": 0.0,
      "event_time": {...},
      "valid_time": {...}
    }
  },
  "l3": {
    "persona": []           // 可选，数组；可空
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
  "atom": [...],      // L1（复用现有 process_dream.recall）
  "raw": [...]        // L0 BM25 全文
}
```

## 4. 4 个 MCP 工具

### 4.1 `memory_os_ingest`

**新格式**：传 4 层完整结构

```js
{
  "memory": {                  // 必填（也可只传 kos 兼容老格式）
    "l0": {...},
    "l1": {...},
    "l2": {...},
    "l3": {...}
  }
}
```

**老格式兼容**：仍接受 `kos: [...]`，自动当 L1 处理。

**CLI**：`python write_4layer.py ingest --file <json>`

### 4.2 `memory_os_recall`

```js
{
  "query": "...",
  "top_k": 5,                     // 每层返回几条
  "include_persona": true,        // 默认 true
  "include_scenario": true,       // 默认 true
  "layers": "L3,L2,L1,L0"         // 可选手控顺序
}
```

**CLI**：`python recall_4layer.py <query> [top_k] [layers]`

### 4.3 `memory_os_delete`（两阶段）

**第一阶段（召回 + token）**：
```js
{
  "query": "...",
  "top_k": 5,
  "layer": "L2",                 // 可选，限定层
  "confirm": false               // 默认 false
}
// → 返回 {phase:"confirm", token:"...", candidates:[...]}
```

**第二阶段（真删）**：
```js
{
  "query": "...",
  "confirm": true,
  "token": "...",                 // 第一阶段返回
  "selected_pids": ["..."]        // 可选，限定只删这些
}
// → 返回 {deleted: {l0: 0, l1: 0, l2: 3, l3: 0}, n_candidates: 3}
```

**CLI**：
- 第一阶段：`python write_4layer.py delete --query "..." [--layer L2] [--top-k 5]`
- 第二阶段：`python write_4layer.py confirm --token "..." [--selected-pids p1,p2]`

### 4.4 `memory_os_update`（两阶段）

**第一阶段**：
```js
{
  "query": "...",
  "memory": {l1: {...}, l2: {...}, l3: {...}},  // 新内容（4 层结构）
  "top_k": 5,
  "confirm": false
}
// → 返回 {phase:"confirm", token:"...", target:{layer, pid, summary}, candidates:[...]}
```

**第二阶段**：
```js
{
  "query": "...",
  "confirm": true,
  "token": "..."
}
// → 返回 {updated: {l1: {...}, l2: {...}}, target_layer: "L2"}
```

**CLI**：
- 第一阶段：`python write_4layer.py update --query "..." --file <new_memory.json>`
- 第二阶段：`python write_4layer.py confirm --token "..."`

## 5. 关键设计决策

### 5.1 L0 PID 用 UUID，不与 L1 冲突

L1 PID 是 md5(实体+关系指纹)，L0 用 UUID v4，互不冲突。

### 5.2 L0 Neo4j 反向关联

L0 节点通过 `:GENERATED` 边连到 L1 主体实体（不是关系边，因为 Cypher 不允许边到边）。

### 5.3 L0 BM25 独立索引

不复用现有 `bm25_index.py` 的索引——L0 原文长且嘈杂，会冲淡 L1 精确摘要。
独立索引存 `/tmp/memory_os_bm25_l0.pkl`。

### 5.4 L0 BM25 分数用 `get_top_n` 排名，不用 `get_scores`

`rank_bm25` 的 `get_scores` 在单查询词场景会返回负数（IDF 异常），`get_top_n` 是官方推荐用法。

### 5.5 两阶段 delete/update 用 token 防误删

Token 存 `/tmp/memory-os-action-tokens/`，5 分钟过期。第二阶段必须带 token + 确认。

### 5.6 L3 抽取门槛严格

**一次经历 ≠ 长期偏好**——需要反复出现才能晋升 L3。详见 `extract_prompt.md`。

## 6. 召回率验证

`test_4layer_batch.py` 跑了 5 条不同主题场景 + 6 种 query 类型：

| Query 类型 | 全开 | L1-only | 提升 |
|-----------|------|---------|------|
| 精确匹配 | 17 | 4 | +325% |
| 主题匹配 | 17 | 4 | +325% |
| 偏好匹配 | 18 | 5 | +260% |
| 关系查询 | 17 | 4 | +325% |
| 模糊查询 | 15 | 2 | +650% |
| L0 字面匹配 | 16 | 3 | +433% |

**平均召回提升 +386%**。

## 7. 不动现有代码（旁路原则）

新功能全部走 `write_4layer.py` / `recall_4layer.py` 两个新脚本，**不修改**：
- `process_dream.py`（写入主流程 + 决策逻辑）
- `recall_fusion.py`（融合层）
- `recall_config.py`（核心配置）
- `recall_gate.py`（门控逻辑）
- `bm25_index.py`（现有 BM25 索引）

只改了：
- `recall_config.py`: `HOOK_MIN_LEN` 7 → 2（让短 query 也能召回）
- `src/index.js`: 4 个 MCP 工具的 schema + execute

## 8. 故障排查

| 问题 | 原因 | 修复 |
|------|------|------|
| `python exit 2: invalid choice` | JS 端没传 subcommand | `runPython(["ingest", "--file", tmpFile], {script: ...})` |
| `NameError: _gen_action_token` | helper 函数没插到 CLI 段前面 | 在 CLI 段（`if __name__`）前插入 |
| `Type mismatch: r defined with conflicting type` | Cypher 试图把 Relationship 当 Node MERGE 边 | 先锚定实体节点 |
| `L0 BM25 raw: []` | `get_scores` 单查询词返回负数被过滤 | 改用 `get_top_n` |
| `pid: None` | `_search_one_collection` 没把 `pid` 复制到返回 dict | 手动加 `"pid": hit.id` 字段 |

## 9. 备份

改动前已备份原文件（带 `.bak-4layer-*` / `.bak-pre2stage` 后缀）：
- `process_dream.py.bak-4layer-20260825`
- `recall_fusion.py.bak-4layer-20260825`
- `recall_config.py.bak-4layer-20260825`
- `recall_gate.py.bak-4layer-20260825`
- `write_4layer.py.bak-cli-123103`
- `write_4layer.py.bak-pre2stage`
- `src/index.js.bak-4layer-1241`

---

_最后更新：2026-08-25 12:53_
