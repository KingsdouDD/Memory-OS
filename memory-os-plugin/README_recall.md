# Memory OS Proactive Recall — Deep Dive

## Recall Flow Overview (updated 2026-09-01)

```
User Input
   │
   ▼
┌──────────────────────────────────────────┐
│  Step 1: L3 Recall (high confidence)     │  Extract persona entities
│  Step 2: L2 Recall (medium-high confidence)│  Extract scenario + entities
│  Step 2.5: Entity supplement from L3/L2  │  jieba noun extraction from summaries
└──────────────────────────────────────────┘
   │ filter_entities / filter_scenario_ids
   ▼
┌──────────────────────────────────────────┐
│  Step 3: Graph PRF channel              │  Neo4j 1-hop verification
│  Step 4: L1 Main Recall (vec+BM25+Graph) │  Three paths → RRF fusion
│  Step 5: Pre-filter (entity overlap)     │  Top-k×3粗排, no Reranker yet
└──────────────────────────────────────────┘
   │ atom (Pre-filter candidate pool)
   ▼
┌──────────────────────────────────────────┐
│  Step 6: Association Expansion           │  ← New (2026-09-01)
│  Neo4j multi-hop → expansion query → Qdrant
│  → Associative candidates (assoc_score/hop_depth/path)
└──────────────────────────────────────────┘
   │ seed atom + assoc candidates
   ▼
┌──────────────────────────────────────────┐
│  Dedupe (exact summary match)             │
│  Single unified Reranker (all candidates) │  ← Merged (2026-09-01)
│  Pre-filter (threshold 0.55)             │
│  Final Top-K output                      │
└──────────────────────────────────────────┘
   │
   ▼
  LLM Output (with associative path notes)
```

---

## I. Gate (recall_gate.py)

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `HOOK_MIN_LEN` | 7 | Text < 7 chars → skip |
| `HOOK_MAX_LEN` | 300 | Text > 300 chars → skip |
| `HOOK_SKIP_FILLER` | 30+ word whitelist | Match "嗯/好的/ok/继续" etc → skip |
| `HOOK_SKIP_SWEAR` | Profanity regex | Match profanity → skip |
| `FILLER_PATTERNS` | Pure emotion regex | `^(今天好累\|好困\|饿了\|无聊\|嗯+\|啊+)` full sentence match → skip |
| Pure command | Verb start + no Chinese noun + word count < 3 | English command strings → skip |
| Repetitive chars | `len(set(s)) <= 2 && n > 6` | "啊啊啊啊啊" type → skip |
| **Noun gate** | **Softened (2026-08-21)** | **No hard skip when query has no noun; log only; defer judgment to recall chain** |

---

## II. Four-Layer Recall (recall_4layer.py)

### L3 Recall (high confidence)
- Recalls from `memory_persona` collection
- `L3_MIN_SCORE = 0.70`
- Extracts `entities` → adds to `filter_entities`

### L2 Recall (medium-high confidence)
- Recalls from `memory_scenario` collection
- `L2_MIN_SCORE = 0.65`
- Extracts `entities` + `scenario_ids` → adds to filter

### Step 2.5: Entity Supplement
- When L3/L2 have hits but `filter_entities` is still empty
- Uses jieba posseg to extract nouns (`n*` / `m` POS tags) from hit summaries
- Seeds subsequent association chain

### L1 Main Recall
See "Three-Path Recall" section.

---

## III. Three-Path Recall (process_dream.py)

### Vector Channel (Dense Vector — Qdrant ANN)
- `VEC_TOP_K_DEFAULT = 3` (internal qdrant_search top_k)
- `VEC_TOP_K_MULTIPLIER = 1` (recall top_k × multiplier)
- `VEC_MIN_SCORE = 0.70` (cosine below 0.70 directly discarded)

### BM25 Channel (Sparse — rank_bm25 + jieba)
- `BM25_TOP_K = 5` (candidate count)
- **Keyword filter softened (2026-08-21)**: no longer hard-blocks. BM25 handles term relevance; extra literal keyword filter kills synonyms (e.g. "爬山" vs "登山"). Changed to soft filter: top candidates pass directly, rest filtered by ratio, insufficient results → keep all.
- Parameters `BM25_KEYWORD_FILTER_RATIO` (default 0) and `BM25_KEYWORD_FILTER_MIN` (default 0)

### Graph Channel (Knowledge Graph — Neo4j entity expansion)
- `GRAPH_DEPTH = 1` (one-hop expansion)
- `GRAPH_LIMIT_PER_NODE = 4` (max 4 expansions per node)

---

## IV. Fusion & Denoising (recall_fusion.py)

### RRF Fusion
- `RRF_K = 60` (Reciprocal Rank Fusion)
- **Multi-channel accumulation**: same memory hit by multiple channels → RRF scores **accumulate** (not max), building cumulative ranking advantage
- `RRF_RELATIVE_KEEP_RATIO = 0.95`

### Hook 1: Channel-level scoring (before rrf_fuse)
- `fusion_transform_channel`:
  - graph channel: `graph_depth_score` → 1-hop=1.0×, 2-hop=0.5×, 3-hop=0.33×
  - bm25 channel: ensures every entry has `sort_key` fallback

### PRF Expansion (KG backfills vector, refactored 2026-08-21)
- **Trigger now prioritizes entity/relationship evidence, no longer requires literal token overlap**
  - Method A: subj/obj in raw_triples overlaps with query → trigger
  - Method B: jieba token literal overlap ≥ `PRF_TOKEN_OVERLAP_MIN` (default 0 = off)
- Expansion round score折扣 9折（×0.9）
- Dedupe by `_qdrant_pid`

### Hook 2: Graph hit boost (after rrf_fuse)
- `fusion_boost_graph_hits`: graph-hit entries `sort_key × 1.3`
- Condition: `"graph" in _channels` AND summary contains graph entity names

### Hook 3: Post-fusion processing (before kg_verify)
- `fusion_post_fuse`:
  1. **Importance weighting**: `sort_key × (0.5 + importance)`, 0~1 maps to 0.5×~1.5×
  2. **Temporal decay**: `sort_key × 0.5^(Δdays/180)`, 180-day half-life
  3. `relation` type points restored to readable text via `parent_summary`
  4. Sort by sort_key descending

### kg_verify_v2 (refactored 2026-08-21)
- query + each summary **independently embedded**, cosine sim computed separately
- `sim < 0.60` → direct discard
- `0.60 ≤ sim < 0.70` → keep, flag `is_weak=True`
- `sim ≥ 0.70` → keep, flag `is_weak=False`
- **Comprehensive ranking**: `sort_key = sort_key * (1 - w) + sim * w` (w=`KG_SIM_RANKING_WEIGHT`, default 0.5)
  - No longer purely re-ranks by sim; preserves accumulated RRF+GraphBoost+Importance+TimeDecay
- Final sort by sort_key (comprehensive score) descending, take top 5, **no padding**

---

## V. Association Expansion (2026-09-01 NEW)

### Trigger Conditions
- `ASSOC_ENABLED = 1` (switch, default on)
- Has `filter_entities` (L3/L2 recalled entities) OR entities extracted by jieba from query/hits

### Step 1: Neo4j Multi-Hop Expansion
- Starting from seed entities, diffuse through Neo4j graph for `ASSOC_MAX_HOPS` hops (default 2)
- Max `ASSOC_MAX_NEIGHBORS` neighbor nodes per hop (default 6)
- Tracks real `hop_depth` and full `association_path` (seed → intermediate → ...) for each expanded entity

### Step 2: Expansion Query Vectorization
- Concatenate seed summaries + all expanded entity names into one text
- Generate vector via embed function

### Step 3: Qdrant Full-Collection Search
- Search all collections with expansion vector
- `ASSOC_MAX_CANDIDATES` controls max associative candidate count (default 20)

### Step 4: Association Scoring
Comprehensive score per associative candidate:

```
assoc_score = overlap × 0.4 + depth_decay × 0.25 + importance × 0.2 + temporal × 0.15
```

- **entity_overlap**: overlap rate between candidate memory and seed entities
- **depth_decay**: `ASSOC_DEPTH_DECAY ^ (hop_depth - 1)`, more hops = more decay (default 0.5)
- **importance**: importance score set at write time
- **temporal**: newer memories score higher (full score within half year, decay to 0.5 after 2 years)

Candidates below `ASSOC_ACTIVATION_THRESHOLD` (default 0.1) AND hop > 1 are directly discarded.

### Step 5: Merge + Unified Reranker (single call)
```
seed atom (Pre-filter candidate pool)
    +
assoc candidates
    │
    ↓ Exact summary dedupe
Unified candidate pool
    │
    ↓ Single Reranker HTTP call
Rerank scores (rerank_score)
    │
    ↓
final_score = rerank × 0.6 + signal × 0.4
    │(signal = entity_overlap for seed, assoc_score for assoc)
    ↓
Pre-filter (threshold 0.55)
    │
    ↓
Final Top-K
```

**Effect**: Reranker calls reduced from 2/query → 1/query

---

## VI. Output Fields (updated 2026-09-01)

Each final memory entry contains:

| Field | Description |
|-------|-------------|
| `summary` | Memory summary text |
| `score` | Raw vector similarity score |
| `rerank_score` | Reranker P(yes) score (0~1) |
| `final_score` | Comprehensive score (rerank × 0.6 + signal × 0.4) |
| `entity_overlap` | Overlap rate with seed entities (seed memories) |
| `assoc_score` | Association comprehensive score (associative memories) |
| `hop_depth` | Expansion hop count (associative, 1~N) |
| `association_path` | Full expansion path (associative) |
| `recall_reason` | Trigger reason (direct match / associated via X) |
| `_is_assoc` | Is associative memory (True/False) |
| `importance` | Importance score |
| `event_time` | Event time |
| `source` | Source (vec / bm25 / graph / assoc) |

---

## VII. Intent Filtering

- Verb intents (喝/看/吃/爬): **softened 2026-08-21, no longer hard filter**; defaults to soft ranking factor
- Attribute nouns (habits/interests/personality): weak signal, miss → sink to bottom, don't delete
- Parameters `INTENT_VERB_HARD_FILTER` (default False) and `INTENT_VERB_SOFT_WEIGHT` (default 0.05)

---

## VIII. Output Rules

- Return exactly as many entries as in the database, **never pad**
- Graph channel is supplementary only; entries must satisfy: Summary ≥ 6 chars, not concatenated triples

---

## IX. Debug Logs

Set env var `MEMORY_OS_RECALL_DEBUG=1` to output to `logs/recall-debug.log`:

```
[RECALL] query="..."
  vec_raw=N bm25_raw=N bm25_filtered=N graph_raw=N prf_kg=N rrf=N kg_verified=N final=N
  [0] score=0.xxx sort_key=0.xxx sim=0.xxx src=vec summary="..."
```

---

## X. Parameter Index (recall_config.py)

### Basic Recall
| Parameter | Default | Purpose |
|-----------|---------|---------|
| `BM25_KEYWORD_FILTER_RATIO` | 0.0 | BM25 keyword filter ratio, 0=off |
| `BM25_KEYWORD_FILTER_MIN` | 0 | BM25 keyword filter minimum keep count |
| `PRF_TOKEN_OVERLAP_MIN` | 0 | PRF literal trigger token count, 0=off |
| `KG_SIM_RANKING_WEIGHT` | 0.5 | kg_verify sim weight in comprehensive ranking |
| `INTENT_VERB_HARD_FILTER` | False | Verb intent hard filter switch |
| `INTENT_VERB_SOFT_WEIGHT` | 0.05 | Verb intent soft weighting magnitude |

### Association Expansion (2026-09-01 NEW)
| Parameter | Default | Purpose |
|-----------|---------|---------|
| `ASSOC_ENABLED` | 1 | Association expansion switch, 0=off |
| `ASSOC_MAX_HOPS` | 2 | Neo4j max expansion hops |
| `ASSOC_MAX_NEIGHBORS` | 6 | Max neighbor expansions per hop |
| `ASSOC_ACTIVATION_THRESHOLD` | 0.1 | Association activation threshold (discard if below this AND hop>1) |
| `ASSOC_DEPTH_DECAY` | 0.5 | Hop depth decay coefficient (each additional hop ×0.5) |
| `ASSOC_MAX_CANDIDATES` | 20 | Max associative candidate count |

### Debug
| Parameter | Default | Purpose |
|-----------|---------|---------|
| `RECALL_DEBUG` | "0" | Debug log switch, "1"=on |

---

## XI. Related Files

| File | Purpose |
|------|---------|
| `src/index.js` | Plugin entry, `before_prompt_build` hook registration |
| `scripts/recall_4layer.py` | Recall main script (Step 1~6, association integrated) |
| `scripts/process_dream.py` | Embedding + Qdrant low-level read/write |
| `scripts/recall_gate.py` | Hook gate (should_skip_recall / is_discardable) |
| `scripts/recall_fusion.py` | Fusion layer + Association Expansion (RRF / boost / importance / time_decay / kg_verify_v2 / association_expand) |
| `scripts/recall_config.py` | All tunable parameters "parameter hub" |
| `scripts/bm25_index.py` | BM25 sparse index (rank_bm25 + jieba) |
| `scripts/extract_prompt.md` | KO extraction spec (pre-write extraction standard) |
