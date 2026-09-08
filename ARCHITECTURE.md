# Memory OS — 架构文档（2026-09-08 更新）

> 本文档记录 Memory OS 的核心架构设计、4 层记忆模型、融合召回链路，以及 2026-09-08 图谱融合重构的关键改动。

---

## 1. 整体架构

```
┌──────────────────────────────────────────────────────────────┐
│                       User Query                              │
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
        │   4-Layer 召回引擎 (recall_4layer.py)  │
        │                                          │
        │  Step 1: L3 recall (persona, top 20)   │  → Extract entities
        │  Step 2: L2 recall (scenario, top 20)   │  → Extract scenario_ids
        │  Step 2.5: jieba entity supplement     │
        │                                          │
        │  Step 3: Neo4j graph (1-hop direct)     │  → No PRF expansion
        │  Step 4: Vector recall (Qdrant ANN)     │  → Single path, score >= 0.62
        │  Step 4.5: Entity Overlap rerank        │
        │                                          │
        │  Step 3.5: Graph Fusion Integration      │  → fusion_boost_graph_hits
        │              + fusion_post_fuse          │    ×1.3 + importance + time decay
        │                                          │
        │  Unified Reranker (single call)          │
        │  final_score = rerank × 0.6 + overlap × 0.4
        │  rerank < 0.55 → hard discard
        │  → Top 5 → PID → L1 output
        └──────────────────────────────────────────┘
                              │
                              ▼
        ┌──────────────────────────────────────────┐
        │      Memory Injection (src/index.js)      │
        │   注入到 LLM system prompt 前缀           │
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

### 3.1 三路并行召回

```
                       query
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
   [向量召回]        [BM25 召回]     [Neo4j 知识图谱]
   Qdrant ANN         memory_l0         1-hop direct
   top 20             旁路索引         (no PRF expansion)
   水位 0.62
        │
        ▼
   [Step 3.5: Graph Fusion Integration]
   - graph_items normalized → _channels=["graph"], sort_key=graph_sim
   - fusion_boost_graph_hits: graph entries × 1.3
   - fusion_post_fuse: importance + time decay
        │
        ▼
   [Unified Reranker] 一次精排
   final_score = rerank × 0.6 + entity_overlap × 0.4
   rerank < 0.55 → hard discard
        │
        ▼
   Top 5 → PID 关联 L1 → 输出
```

### 3.2 关键设计原则

#### 原则 1: 无水位硬过滤（除了向量召回的 0.62）
- 向量召回: `score_threshold = 0.62` (查询时卡, 不是结果卡)
- Reranker: rerank < 0.55 → hard discard
- Neo4j: 无 sim >= 0.62 触发条件，直接走 1-hop

#### 原则 2: 各通道权重（融合语义，不是层级过滤）

| 通道 | 召回内容 | 权重 | 在融合中的角色 |
|------|---------|------|----------------|
| 向量召回 | 语义宽匹配 | 主权重 (rerank × 0.6) | 提供候选池 |
| BM25 | 关键词精确 | 不进候选池 (旁路) | 兜底索引 |
| Neo4j 知识图谱 | 1-hop 实体关联 | boost ×1.3 (fusion_boost_graph_hits) | 直接进入融合池 |

#### 原则 3: entity 是信号，不是门控
- Entity Overlap = 候选记忆 entities 与 query entities 的交集比例
- **加权信号** (`overlap × 0.4` 加到 final_score)
- **不是硬过滤** (overlap = 0 的记忆依然可以输出)

#### 原则 4: 最终输出是 L1
- 向量召回跨 L0~L3 命中
- L0~L3 通过 payload.entities 或 scenario_id 关联到 L1
- 输出时只显示 L1 原子记忆

---

## 4. 2026-09-08 的关键改动

### 4.1 删除 PRF 多跳扩张

**改动前**:
```python
# PRF 触发条件: max_graph_sim >= 0.62
if max_graph_sim >= PRF_MIN_GRAPH_SIM:
    # 从 raw_triples 补充 filter_entities
    # graph_prf_triggered = True
# 结果: 只补充了 filter_entities，graph hits 从未进入融合池
```

**改动后**:
```python
# Step 3: Neo4j graph (1-hop direct, no PRF)
graph_items = _graph_channel_with_sim(query, limit=5)
if graph_items:
    # 从 graph 结果提取实体名
    for g in graph_items:
        for r in g.get("raw_triples", []):
            subj, obj = r.get("subj"), r.get("obj")
            if subj not in graph_entity_names: graph_entity_names.append(subj)
            if obj not in graph_entity_names: graph_entity_names.append(obj)
    graph_prf_triggered = True
```

### 4.2 新增 Step 3.5: Graph Fusion Integration

**改动前**: graph_items 只用来补充 filter_entities，然后被丢弃

**改动后**:
```python
# Step 3.5: graph_items 标准化后接入融合
graph_items_normalized = []
for g in graph_items or []:
    g_norm = dict(g)
    g_norm["_channels"] = ["graph"]
    g_norm["sort_key"] = g_norm.get("graph_sim", 0.7)
    graph_items_normalized.append(g_norm)

# fusion_boost_graph_hits: graph entries × 1.3
merged_atom = fusion_boost_graph_hits(
    merged_atom + graph_items_normalized,
    graph_entity_names=graph_entity_names,
    boost=1.3,
)

# fusion_post_fuse: importance + time decay
merged_atom = fusion_post_fuse(merged_atom)
```

### 4.3 恢复 0.55 Reranker

**改动前**:
```python
merged_atom.sort(key=lambda x: -x.get("final_score", ...))
merged_atom = merged_atom[:top_k]  # 直接取前 5
```

**改动后**:
```python
merged_atom.sort(key=lambda x: -x.get("final_score", ...))
merged_atom = [m for m in merged_atom if (m.get("rerank_score") or 0) >= 0.55]
merged_atom = merged_atom[:top_k]
```

**原因**: 0.55 过滤恢复了。Reranker 对情感化 query + 事实性 memory 的打分有时偏高，需要 0.55 作为兜底安全阀。

### 4.4 top_k 调整

| 阶段 | 值 | 说明 |
|------|------|------|
| 向量召回每 collection | 20 (固定) | 保证候选充足 |
| Reranker 候选数 | len(merged_atom) | 全部候选进 Reranker |
| 最终输出 | top_k | 重排后取前 5 |

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
│ memory_event        │    │ :Concept :Object   │
│ memory_experience   │    │ :L0Conversation     │
│ memory_fact         │    │                     │
│ memory_observation  │    │ 边类型:             │
│ memory_relation     │    │ INVOLVES / VISITED  │
│ memory_l0 (原始)   │    │ LOVES / MENTIONED_IN│
└─────────────────────┘    └─────────────────────┘
```

---

## 6. 相关文件

| 文件 | 用途 |
|------|------|
| `src/index.js` | 插件入口，`before_prompt_build` hook 注册 |
| `scripts/recall_4layer.py` | 召回主脚本 (Step 1~4 + Step 3.5 fusion) |
| `scripts/process_dream.py` | Embedding + Qdrant 低层读写 |
| `scripts/recall_gate.py` | Hook 门控 |
| `scripts/recall_fusion.py` | 融合层 (fusion_boost_graph_hits / fusion_post_fuse / association_expand) |
| `scripts/recall_config.py` | 所有可调参数 |
| `scripts/extract_prompt.md` | KO 抽取规范 |

---

_文档版本: 2026-09-08_
