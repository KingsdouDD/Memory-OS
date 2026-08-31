# Memory OS 主动召回说明

> 整理时间：2026-08-21
> 整理人：小橘子

---

## 触发链路

`before_prompt_build` 事件钩子（index.js 第 315 行附近）
→ 提取用户文本 → `recall_for_hook()` → `process_dream.py recall --hook`
→ 三路并行召回 → RRF 融合 → 去噪精排 → 注入 prompt

> ⚠️ 2026-08-20 起该钩子临时禁用，改为被动召回（`memory_os_recall` 工具显式调用）。

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

## 二、三路召回（process_dream.py）

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

## 三、融合去噪（recall_fusion.py）

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

## 四、意图过滤

- 动词意图（喝/看/吃/爬）：**2026-08-21 已软化，不再硬过滤**；默认作为软排序因素
- 属性名词（习惯/爱好/性格）：弱信号，未命中只沉底不删
- 参数 `INTENT_VERB_HARD_FILTER`（默认 False）和 `INTENT_VERB_SOFT_WEIGHT`（默认 0.05）控制

---

## 五、输出铁律

- 库里多少条返回多少条，**绝不凑数**
- 输出字段：`summary / relation / score(sim) / sort_key / sim / source / event_time / valid_time`
- graph 通道只当补位，须满足：summary ≥ 6 字、非拼接三元组

---

## 六、调试日志

设置环境变量 `MEMORY_OS_RECALL_DEBUG=1`，会在 `logs/recall-debug.log` 输出：

```
[RECALL] query="..."
  vec_raw=N bm25_raw=N bm25_filtered=N graph_raw=N prf_kg=N rrf=N kg_verified=N final=N
  [0] score=0.xxx sort_key=0.xxx sim=0.xxx src=vec summary="..."
```

便于定位「召回不到」的根因：门控被拦 / BM25 过滤 / Vector 阈值过滤 / KG 过滤 / 意图过滤 / 排序被压。

---

## 七、新增可调参数索引（recall_config.py）

| 参数 | 默认值 | 作用 |
|------|--------|------|
| `BM25_KEYWORD_FILTER_RATIO` | 0.0 | BM25 关键词过滤比例，0=关闭 |
| `BM25_KEYWORD_FILTER_MIN` | 0 | BM25 关键词过滤最低保留条数 |
| `PRF_TOKEN_OVERLAP_MIN` | 0 | PRF 字面触发 token 数，0=关闭字面触发 |
| `KG_SIM_RANKING_WEIGHT` | 0.5 | kg_verify 综合排序中 sim 权重 |
| `INTENT_VERB_HARD_FILTER` | False | 动词意图硬过滤开关 |
| `INTENT_VERB_SOFT_WEIGHT` | 0.05 | 动词意图软加权幅度 |
| `RECALL_DEBUG` | "0" | 调试日志开关，"1"=开启 |

---

## 八、相关文件索引

| 文件 | 作用 |
|------|------|
| `src/index.js` | 插件入口，before_prompt_build 钩子注册 |
| `scripts/process_dream.py` | recall / recall_for_hook 主逻辑 |
| `scripts/recall_gate.py` | Hook 门控（should_skip_recall / is_discardable） |
| `scripts/recall_fusion.py` | 融合层（RRF / boost / importance / time_decay / kg_verify_v2） |
| `scripts/recall_config.py` | 所有可调参数的"参数中心"，默认值 + 环境变量覆盖 |
| `scripts/bm25_index.py` | BM25 稀疏索引（rank_bm25 + jieba） |
| `scripts/extract_prompt.md` | KO 抽取规范（写入前的抽取标准） |
