# Memory OS 主动召回说明

## 召回流程总览（2026-09-01 更新）

```
用户输入
   │
   ▼
┌──────────────────────────────────────────┐
│  Step 1: L3 召回（高置信）               │  提取 persona entities
│  Step 2: L2 召回（中高置信）              │  提取 scenario + entities
│  Step 2.5: 从 L3/L2 hits 补充实体        │  jieba 从 summary 提取
└──────────────────────────────────────────┘
   │ filter_entities / filter_scenario_ids
   ▼
┌──────────────────────────────────────────┐
│  Step 3: Graph PRF 通道                  │  Neo4j 1-hop 验证
│  Step 4: L1 主召回（向量+BM25+Graph）     │  三路并行 → RRF 融合
│  Step 5: Pre-filter（entity overlap）    │  粗排保留 top_k×3，不过 Reranker
└──────────────────────────────────────────┘
   │ atom（Pre-filter 候选池）
   ▼
┌──────────────────────────────────────────┐
│  Step 6: Association Expansion           │  ← 新增（2026-09-01）
│  Neo4j 多跳扩散 → Expansion query → Qdrant
│  → 联想候选（assoc_score/hop_depth/path）
└──────────────────────────────────────────┘
   │ seed atom + assoc candidates
   ▼
┌──────────────────────────────────────────┐
│  合并去重（精确 summary 匹配）             │
│  统一一次 Reranker（精排全部候选）         │  ← 合并（2026-09-01）
│  Pre-filter（水位 0.55）                 │
│  Final Top-K 输出                        │
└──────────────────────────────────────────┘
   │
   ▼
  LLM 输出（含联想路径说明）
```

---

## 一、门控（recall_gate.py）

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `HOOK_MIN_LEN` | 7 | 文字少于 7 字 → 跳过 |
| `HOOK_MAX_LEN` | 300 | 文字超过 300 字 → 跳过 |
| `HOOK_SKIP_FILLER` | 30+ 词白名单 | 命中"嗯/好的/ok/继续"等 → 跳过 |
| `HOOK_SKIP_SWEAR` | 粗口正则 | 命中粗言秽语 → 跳过 |
| `FILLER_PATTERNS` | 纯情绪宣泄正则 | `^(今天好累\|好困\|饿了\|无聊\|嗯+\|啊+)` 整句匹配 → 跳过 |
| 纯命令式 | 动词开头 + 无中文名词 + 单词数<3 | 英文字符串命令 → 跳过 |
| 重复字符 | `len(set(s)) <= 2 && n > 6` | "啊啊啊啊啊" 类 → 跳过 |
| **名词闸门** | **软化（2026-08-21）** | **query 无名词时不硬跳过，只记录日志；改由后续召回链路判断是否有效** |

---

## 二、四层召回（recall_4layer.py）

### L3 召回（高置信）
- 从 `memory_persona` collection 召回
- `L3_MIN_SCORE = 0.70`
- 提取 `entities` → 加入 `filter_entities`

### L2 召回（中高置信）
- 从 `memory_scenario` collection 召回
- `L2_MIN_SCORE = 0.65`
- 提取 `entities` + `scenario_ids` → 加入 filter

### Step 2.5：实体补充
- 当 L3/L2 有 hits 但 `filter_entities` 仍为空时
- 用 jieba posseg 从 hits 的 summary 文本提取名词（`n*` / `m` 词性）
- 作为种子实体启动后续联想

### L1 主召回
见「三路召回」章节。

---

## 三、三路召回（process_dream.py）

### 向量通道（Dense Vector — Qdrant ANN）
- `VEC_TOP_K_DEFAULT = 3`（qdrant_search 内部 top_k）
- `VEC_TOP_K_MULTIPLIER = 1`（recall 里 top_k × multiplier）
- `VEC_MIN_SCORE = 0.70`（cosine 低于 0.70 的直接丢弃）

### BM25 通道（Sparse — rank_bm25 + jieba）
- `BM25_TOP_K = 5`（候选数）
- **关键词过滤已软化（2026-08-21）：不再硬阻断。BM25 本身负责词项相关性，额外字面硬过滤会误杀同义表达（如"爬山" vs "登山"）。改为软过滤：头部候选直接保留，其余按比例过关键词过滤，不足则全部保留。**
- 参数 `BM25_KEYWORD_FILTER_RATIO`（默认 0）和 `BM25_KEYWORD_FILTER_MIN`（默认 0）控制

### 图通道（Knowledge Graph — Neo4j 实体扩展）
- `GRAPH_DEPTH = 1`（一跳扩展）
- `GRAPH_LIMIT_PER_NODE = 4`（每节点最多扩展 4 条）

---

## 四、融合去噪（recall_fusion.py）

### RRF 融合
- `RRF_K = 60`（Reciprocal Rank Fusion）
- **多通道累加**：同一 memory 被多通道命中时，RRF 分数**累加**（不是取 max），形成累积排名优势
- `RRF_RELATIVE_KEEP_RATIO = 0.95`

### Hook 1：通道级打分（rrf_fuse 之前）
- `fusion_transform_channel`：
  - graph 通道：`graph_depth_score` → 1跳=1.0×，2跳=0.5×，3跳=0.33×
  - bm25 通道：确保每条有 `sort_key` 兜底

### PRF 扩展（KG 反哺向量，2026-08-21 重构）
- **触发条件改用实体/关系 evidence 优先，不再强制要求字面 token 重叠**
  - 方式A：raw_triples 里的 subj/obj 与 query 有重叠 → 触发
  - 方式B：jieba token 字面重叠 ≥ `PRF_TOKEN_OVERLAP_MIN`（默认 0，即关闭字面触发）
- 扩展轮 score 打 9 折（×0.9）
- 按 `_qdrant_pid` 去重

### Hook 2：图命中 boost（rrf_fuse 之后）
- `fusion_boost_graph_hits`：图谱命中条目 `sort_key × 1.3`
- 判断：`"graph" in _channels` 且 summary 含图谱实体名

### Hook 3：融合后处理（kg_verify 之前）
- `fusion_post_fuse`：
  1. **importance 加权**：`sort_key × (0.5 + importance)`，0~1 映射到 0.5×~1.5×
  2. **时间衰减**：`sort_key × 0.5^(Δdays/180)`，半衰期 180 天
  3. `relation` 类型 point 用 `parent_summary` 还原可读文本
  4. 按 sort_key 降序

### kg_verify_v2（2026-08-21 重构）
- query + 每个 summary **独立 embedding**，分别算 cosine sim
- `sim < 0.60` → 直接丢弃
- `0.60 ≤ sim < 0.70` → 保留，标 `is_weak=True`
- `sim ≥ 0.70` → 保留，标 `is_weak=False`
- **综合排序**：`sort_key = sort_key * (1 - w) + sim * w`（w=`KG_SIM_RANKING_WEIGHT`，默认 0.5）
  - 不再完全按 sim 重排，保留前面 RRF+GraphBoost+Importance+TimeDecay 的积累
- 最终按 sort_key（综合分）降序，取 top 5，**不凑数**

---

## 五、Association Expansion 联想记忆（2026-09-01 新增）

### 触发条件
- `ASSOC_ENABLED = 1`（开关，默认开启）
- 有 `filter_entities`（L3/L2 召回的实体）或有 jieba 从 query/hits 提取的实体

### Step 1：Neo4j 多跳扩散
- 从种子实体出发，在 Neo4j 图里扩散 `ASSOC_MAX_HOPS` 跳（默认 2 跳）
- 每跳最多扩展 `ASSOC_MAX_NEIGHBORS` 个邻居节点（默认 6 个）
- 追踪每个扩散实体的真实 `hop_depth` 和完整 `association_path`（seed → intermediate → ...）

### Step 2：Expansion Query 向量化
- 把 seed summaries + 所有扩散实体名拼接成一段文本
- 用 embed 函数生成向量

### Step 3：Qdrant 全库搜索
- 用 expansion vector 在所有 collection 搜索
- `ASSOC_MAX_CANDIDATES` 控制最多召回联想候选数（默认 20）

### Step 4：Association Scoring
每条联想候选的综合打分：

```
assoc_score = overlap × 0.4 + depth_decay × 0.25 + importance × 0.2 + temporal × 0.15
```

- **entity_overlap**：候选记忆和种子实体的重叠率
- **depth_decay**：`ASSOC_DEPTH_DECAY ^ (hop_depth - 1)`，跳得越远衰减越多（默认 0.5）
- **importance**：写入时的重要性评分
- **temporal**：越近的记忆分数越高（半年内满分，两年以上衰减到 0.5）

低于 `ASSOC_ACTIVATION_THRESHOLD`（默认 0.1）且 hop > 1 的候选直接丢弃。

### Step 5：合并 + 统一 Reranker（一次调用）
```
seed atom（Pre-filter 候选池）
    +
assoc candidates（联想候选）
    │
    ↓ 精确 summary 去重
统一候选池
    │
    ↓ 一次 Reranker HTTP 调用
精排分数（rerank_score）
    │
    ↓
final_score = rerank × 0.6 + signal × 0.4
    │（signal = entity_overlap for seed，assoc_score for assoc）
    ↓
Pre-filter（水位 0.55）
    │
    ↓
Final Top-K
```

**改造效果**：Reranker 调用从 2次/Query → 1次/Query

---

## 六、输出字段（2026-09-01 更新）

最终输出的每条记忆包含：

| 字段 | 说明 |
|------|------|
| `summary` | 记忆摘要文本 |
| `score` | 向量相似度原始分 |
| `rerank_score` | Reranker P(yes) 分数（0~1） |
| `final_score` | 综合分数（rerank × 0.6 + signal × 0.4） |
| `entity_overlap` | 和种子实体的重叠率（seed 记忆） |
| `assoc_score` | 联想综合分数（联想记忆） |
| `hop_depth` | 扩散跳数（联想记忆，1~N） |
| `association_path` | 完整扩散路径（联想记忆） |
| `recall_reason` | 触发原因（直接匹配 / 通过 X 联想到） |
| `_is_assoc` | 是否为联想记忆（True/False） |
| `importance` | 重要性评分 |
| `event_time` | 事件时间 |
| `source` | 来源（vec / bm25 / graph / assoc） |

---

## 七、意图过滤

- 动词意图（喝/看/吃/爬）：**2026-08-21 已软化，不再硬过滤**；默认作为软排序因素
- 属性名词（习惯/爱好/性格）：弱信号，未命中只沉底不删
- 参数 `INTENT_VERB_HARD_FILTER`（默认 False）和 `INTENT_VERB_SOFT_WEIGHT`（默认 0.05）控制

---

## 八、输出铁律

- 库里多少条返回多少条，**绝不凑数**
- graph 通道只当补位，须满足：Summary ≥ 6 字、非拼接三元组

---

## 九、调试日志

设置环境变量 `MEMORY_OS_RECALL_DEBUG=1`，会在 `logs/recall-debug.log` 输出：

```
[RECALL] query="..."
  vec_raw=N bm25_raw=N bm25_filtered=N graph_raw=N prf_kg=N rrf=N kg_verified=N final=N
  [0] score=0.xxx sort_key=0.xxx sim=0.xxx src=vec summary="..."
```

---

## 十、可调参数索引（recall_config.py）

### 基础召回
| 参数 | 默认值 | 作用 |
|------|--------|------|
| `BM25_KEYWORD_FILTER_RATIO` | 0.0 | BM25 关键词过滤比例，0=关闭 |
| `BM25_KEYWORD_FILTER_MIN` | 0 | BM25 关键词过滤最低保留条数 |
| `PRF_TOKEN_OVERLAP_MIN` | 0 | PRF 字面触发 token 数，0=关闭字面触发 |
| `KG_SIM_RANKING_WEIGHT` | 0.5 | kg_verify 综合排序中 sim 权重 |
| `INTENT_VERB_HARD_FILTER` | False | 动词意图硬过滤开关 |
| `INTENT_VERB_SOFT_WEIGHT` | 0.05 | 动词意图软加权幅度 |

### Association Expansion（2026-09-01 新增）
| 参数 | 默认值 | 作用 |
|------|--------|------|
| `ASSOC_ENABLED` | 1 | 联想扩散开关，0=关闭 |
| `ASSOC_MAX_HOPS` | 2 | Neo4j 最大扩散跳数 |
| `ASSOC_MAX_NEIGHBORS` | 6 | 每跳最多扩展邻居数 |
| `ASSOC_ACTIVATION_THRESHOLD` | 0.1 | 联想激活阈值（低于此且 hop>1 丢弃） |
| `ASSOC_DEPTH_DECAY` | 0.5 | hop 深度衰减系数（每多一跳 ×0.5） |
| `ASSOC_MAX_CANDIDATES` | 20 | 最多联想候选数 |

### 调试
| 参数 | 默认值 | 作用 |
|------|--------|------|
| `RECALL_DEBUG` | "0" | 调试日志开关，"1"=开启 |

---

## 十一、相关文件索引

| 文件 | 作用 |
|------|------|
| `src/index.js` | 插件入口，before_prompt_build 钩子注册 |
| `scripts/recall_4layer.py` | 召回主脚本（Step 1~6，含联想集成） |
| `scripts/process_dream.py` | Embedding + Qdrant 底层读写 |
| `scripts/recall_gate.py` | Hook 门控（should_skip_recall / is_discardable） |
| `scripts/recall_fusion.py` | 融合层 + Association Expansion（RRF / boost / importance / time_decay / kg_verify_v2 / association_expand） |
| `scripts/recall_config.py` | 所有可调参数的"参数中心" |
| `scripts/bm25_index.py` | BM25 稀疏索引（rank_bm25 + jieba） |
| `scripts/extract_prompt.md` | KO 抽取规范（写入前的抽取标准） |
