# Memory OS — 架构文档（2026-09-07 更新）

> 本文档记录 Memory OS 的核心架构设计、4 层记忆模型、融合召回链路，以及今天 (2026-09-07) 召回引擎重构的关键改动。

---

## 1. 整体架构

```
┌──────────────────────────────────────────────────────────────┐
│                       User Query                              │
│            "真怀念跟你外婆去香港的时候的日子"                  │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
        ┌──────────────────────────────────────────┐
        │      Recall Gate (recall_gate.py)        │
        │  长度/情绪/敏感词过滤 + 同会话去重        │
        └──────────────────────────────────────────┘
                              │ Pass
                              ▼
        ┌──────────────────────────────────────────┐
        │   4-Layer 召回引擎 (recall_4layer.py)    │
        │                                          │
        │   [并行召回三路,各自取前20条,水位 0.62]   │
        │   ┌──────────────────────────────┐       │
        │   │ 通道 1: 向量召回 (Qdrant)    │       │
        │   │   - 跨 L0~L3 全 collection   │       │
        │   │   - score_threshold = 0.62    │       │
        │   │   - limit = 20               │       │
        │   └──────────────────────────────┘       │
        │   ┌──────────────────────────────┐       │
        │   │ 通道 2: BM25 召回 (memory_l0)│       │
        │   │   - 全文关键词匹配           │       │
        │   │   - 不进主候选池 (旁路索引)   │       │
        │   └──────────────────────────────┘       │
        │   ┌──────────────────────────────┐       │
        │   │ 通道 3: Neo4j 知识图谱       │       │
        │   │   - 多跳实体关联             │       │
        │   │   - 不进主候选池 (给候选池加分)│      │
        │   └──────────────────────────────┘       │
        │                                          │
        │   [融合重排]                              │
        │   - 向量命中进候选池                      │
        │   - Neo4j 关联 entity → 候选池同 entity 项加分│
        │   - Entity Overlap 加权 (信号,非硬过滤)  │
        │                                          │
        │   [Reranker 精排]                         │
        │   - 一次调用,统一候选池精排               │
        │   - final_score = rerank × 0.6 + overlap/assoc × 0.4│
        │   - ❌ 删除了之前的 0.55 硬过滤           │
        │   - 直接取 top 5 作为最终输出              │
        └──────────────────────────────────────────┘
                              │
                              ▼
        ┌──────────────────────────────────────────┐
        │         PID → L1 关联输出                │
        │   - top 5 命中通过 PID 关联到 L1          │
        │   - 最终只显示 L1 原子记忆                │
        └──────────────────────────────────────────┘
                              │
                              ▼
        ┌──────────────────────────────────────────┐
        │      Memory Injection (src/index.js)    │
        │   注入到 LLM system prompt 前缀          │
        │   「【以下是你和用户之间的共同记忆】」     │
        └──────────────────────────────────────────┘
                              │
                              ▼
                       LLM Output (with injected memories)
```

---

## 2. 4 层记忆模型 (L0 ~ L3)

| Layer | 名称 | 内容 | 召回角色 |
|-------|------|------|----------|
| **L3** | Persona | 跨场景稳定特征 (人格/习惯/偏好/关系) | 路由信号: 提供高频实体 |
| **L2** | Scenario | 完整历史场景 (事件/项目/关系) | 路由信号: 提供场景上下文 |
| **L1** | Atom | 最小独立知识单元 (fact/event/preference/routine) | **最终输出**: 召回终点 |
| **L0** | Raw Dialog | 原始对话记录 (兜底证据) | BM25 索引源 (不进主输出) |

**核心设计原则**: 不管哪一层先命中, 最终都汇到 **L1 原子记忆**输出.

---

## 3. 融合召回链路 (核心)

### 3.1 三路并行召回 (无门控)

```
                       query
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
   [向量召回]        [BM25 召回]     [Neo4j 知识图谱]
   Qdrant 跨层        memory_l0 only   多跳实体拓展
   top 20             旁路索引         给候选池加分
   水位 0.62
        │
        ▼
   [候选池] ← Neo4j 关联 entity 加分
        │
        ▼
   [Reranker] 一次精排
        │
        ▼
   Top 5 → PID 关联 L1 → 输出
```

### 3.2 关键设计原则 (老豆 2026-09-07 设计意图)

#### 原则 1: 无水位硬过滤 (除了向量召回的 0.62)
- 向量召回: `score_threshold = 0.62` (查询时卡, 不是结果卡)
- Reranker: **不卡阈值** (重排后直接取 top 5)
- Neo4j 联想: **不卡 sim** (无 sim ≥ 0.62 触发条件)

#### 原则 2: 各通道权重 (融合语义,不是层级过滤)

| 通道 | 召回内容 | 权重 | 在融合中的角色 |
|------|---------|------|----------------|
| 向量召回 | 语义宽匹配 | 主权重 (rerank × 0.6) | 提供候选池 |
| BM25 | 关键词精确 | 不进候选池 (旁路) | 兜底索引 |
| Neo4j 知识图谱 | 实体关联 | 信号加权 (overlap × 0.4) | 过滤噪声 + 关联聚合 |

#### 原则 3: entity 是信号,不是门控
- Entity Overlap = 候选记忆 entities 与 query entities 的交集比例
- **加权信号** (`overlap × 0.4` 加到 final_score)
- **不是硬过滤** (overlap = 0 的记忆依然可以输出)

#### 原则 4: 最终输出是 L1
- 向量召回跨 L0~L3 命中
- L0~L3 通过 payload.entities 或 scenario_id 关联到 L1
- 输出时只显示 L1 原子记忆

---

## 4. 今天 (2026-09-07) 的关键改动

### 4.1 Step 4 重构: 单一向量召回路径 (删 Path A/B)

**改动前**:
```python
if filter_entities or filter_scenario_ids:
    # Path A: 硬过滤 entity → 召回
    hits = _qdrant_search_filtered(...)
else:
    # Path B: fallback 到 process_dream.recall
    items = []
```
**问题**: Path A 在 L3/L2 sim<0.62 → filter 为空 → Path B 兜底,但 Path B 没做 Neo4j 加权

**改动后**:
```python
# 单一路径:无条件走向量召回
for coll in RecallConfig.COLLECTIONS:
    client.query_points(
        collection_name=coll,
        query=vec,
        limit=20,                # 老豆要求 top 20
        score_threshold=0.62,    # 水位不动
    )
# Entity Overlap 仅做加权,不是硬过滤
```

### 4.2 删 Reranker 0.55 硬过滤

**改动前**:
```python
merged_atom = [m for m in merged_atom
              if m.get("rerank_score", 0) >= 0.55]  # ❌ 硬过滤
```

**改动后**:
```python
merged_atom.sort(key=lambda x: -x.get("final_score", ...))
merged_atom = merged_atom[:top_k]  # ✅ 直接取前 5
```

**原因**: Reranker 对情感化 query ("怀念..."/"感觉...") + 事实性 memory 的打分天然偏低, 0.55 水位会把合理命中都砍掉,导致 `memory_os_recall` 返回 0 条.

### 4.3 新建 `_numpy_compat.py` (Python 3.14 兼容补丁)

**问题**: Python 3.14 + numpy 2.0+ 删除了 `np.bool_` / `np.int8` 等老别名,导致 `qdrant_client` / `neo4j` import 失败.

**解决**: 新建 `scripts/_numpy_compat.py`,在所有 import 之前恢复 numpy 1.x 别名.

```python
# 用法: 必须在所有 import 之前
try:
    import _numpy_compat  # noqa: F401
except Exception:
    pass
```

**接入点**:
- `recall_4layer.py` 顶部
- `process_dream.py` 顶部 (docstring 之后,import 之前)

### 4.4 top_k 调整

| 阶段 | 老值 | 新值 | 原因 |
|------|------|------|------|
| 向量召回每 collection | top_k × 4 (Path A) / top_k × 2 (Path B) | **20** (固定) | 老豆要求: 数据多了之后保证候选充足 |
| Reranker 候选数 | 不限 (删 Pre-filter) | **top_k × 3** (top_k=5 → 15 条进 Reranker) | 保留预过滤,减少 Reranker 调用 |
| 最终输出 | top_k | **top_k** (top_k=5) | 老豆要求: 重排后直接取前 5 |

---

## 5. 数据存储架构

```
┌─────────────────────┐    ┌─────────────────────┐
│      Qdrant         │    │       Neo4j          │
│   (向量 + payload)  │    │   (知识图谱)         │
├─────────────────────┤    ├─────────────────────┤
│ memory_persona      │    │ :Persona            │
│ memory_scenario     │    │ :Scenario           │
│ memory_atom         │◄───┤ :Person :Place      │
│ memory_event        │    │ :Concept :Object    │
│ memory_experience   │    │ :L0Conversation     │
│ memory_fact         │    │                     │
│ memory_observation  │    │ 边类型:              │
│ memory_relation     │    │ INVOLVES (63)       │
│ memory_l0 (原始)    │    │ VISITED / MENTIONED_IN│
│ ...                 │    │ LOVES / COMFORTED   │
└─────────────────────┘    └─────────────────────┘
       ▲                          ▲
       │                          │
       └──────────┬───────────────┘
                  │
            写入时建立关联
            (memory_os_update.py)
```

---

## 6. 召回性能基线 (修复后)

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 情感化 query 召回数 | 0 条 | 5+ 条 ✅ |
| 关键词 query 召回数 | 5 条 | 5+ 条 ✅ |
| Reranker 调用次数/Query | 1 次 | 1 次 |
| 平均召回耗时 | ~6s (含 Reranker) | ~6s |

**测试 query**: "真怀念跟你外婆去香港的时候的日子"

**修复前结果**:
```json
{ "ok": true, "empty": true, "memories": [] }  // ❌
```

**修复后结果**:
```json
{
  "memories": [
    "[发生: 以前] 用户曾与外婆一起去澳门旅行,去了三姐妹和威尼斯人拍照...用户曾与外婆一起去香港旅行...",
    "外婆今天从澳门坐船回香港,下船后心情不错,说想吃点东西",
    "外婆平时早上7点半钟左右去越秀山爬山...老豆喜欢吃牛肉丸",
    "老豆昨晚去朋友家吃晚饭...外婆和老豆去维多利亚港散步看海",
    "..."
  ]
}  // ✅
```

---

## 7. 不动的东西 (老豆明确禁止)

- ❌ `src/index.js` 工具调用层 (6 个 MCP 工具签名)
- ❌ 0.62 水位 (任何通道)
- ❌ BM25 通道 (旁路索引保留,不动)
- ❌ Neo4j 联想扩展的内部逻辑
- ❌ `recall_for_hook` 入口函数签名
- ❌ 6 个 MCP 工具的对外 API

---

## 8. 文件变更清单 (今天)

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `scripts/_numpy_compat.py` | **新建** | Python 3.14 + numpy 2.0+ 兼容补丁 |
| `scripts/recall_4layer.py` | 修改 | Step 4 重构 + 删 0.55 + 接 numpy_compat |
| `scripts/process_dream.py` | 修改 | 接 numpy_compat (仅顶部 import) |
| `README.md` | 修改 | 架构图更新 (去掉 Path A/B) |
| `ARCHITECTURE.md` | **新建** | 本文档 |

---

## 9. 未来扩展方向 (记录,未实现)

### Phase 3: 真正的 hop 分离召回
- 当前: Neo4j 联想扩展把所有 hop 实体合并成一个大 query 搜 Qdrant
- 改进: 每个 hop 单独搜 Qdrant, 单独打分, 让 `depth_decay` 对不同 hop 真实生效
- 效果: hop1 > hop2 > hop3 的衰减效果可观测

### L1 PID 追溯字段
- 当前: L0/L2/L3 通过 Neo4j 边间接关联到 L1
- 改进: 写入时把 L1 PID 显式存到 L0/L2/L3 的 payload (`source_l1_pid` 字段)
- 效果: 召回时直接按 PID 折叠, 避免多层级重复

---

_文档版本: 2026-09-07_
_作者: 小橘子 (基于老豆 2026-09-07 设计意图整理)_
