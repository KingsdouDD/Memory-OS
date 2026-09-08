# Memory OS Proactive Recall — Deep Dive

## Recall Flow Overview (updated 2026-09-08)

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
│  Step 3: Graph Channel (direct, no PRF)   │  Neo4j 1-hop → normalized → fusion
│  Step 4: L1 Vector Recall (Qdrant ANN)    │  Single path, top 20, score >= 0.62
│  Step 4.5: Entity Overlap Rerank          │  Weighted signal, not hard filter
└──────────────────────────────────────────┘
   │ atom (vector) + graph_items (Neo4j direct)
   ▼
┌──────────────────────────────────────────┐
│  Step 3.5: Graph Fusion Integration       │  Changed (2026-09-08)
│  graph_items normalized + merged_atom
│  → fusion_boost_graph_hits (boost x1.3)
│  → fusion_post_fuse (importance + time decay)
└──────────────────────────────────────────┘
   │ unified candidate pool
   ▼
┌──────────────────────────────────────────┐
│  Unified Reranker (single call)          │
│  final_score = rerank x 0.6 + entity_overlap x 0.4
│  Hard filter: rerank_score < 0.55 → discard
│  Final Top-K output
└──────────────────────────────────────────┘
   │
   ▼
  LLM Output
```

---

## I. Gate (recall_gate.py)

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `HOOK_MIN_LEN` | 7 | Text < 7 chars → skip |
| `HOOK_MAX_LEN` | 300 | Text > 300 chars → skip |
| `HOOK_SKIP_FILLER` | 30+ word whitelist | Match filler words → skip |
| `HOOK_SKIP_SWEAR` | Profanity regex | Match profanity → skip |
| `FILLER_PATTERNS` | Pure emotion regex | Pattern match for emotion-only messages → skip |
| Pure command | Verb start + no Chinese noun + word count < 3 | English command strings → skip |
| Repetitive chars | `len(set(s)) <= 2 && n > 6` | Repetitive chars → skip |
| **Noun gate** | **Softened** | **No hard skip when query has no noun; log only** |

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
- Uses jieba posseg to extract nouns from hit summaries
- Seeds subsequent steps

### L1 Main Recall
See "Three-Path Recall" section.

---

## III. Three-Path Recall (process_dream.py)

### Vector Channel (Dense Vector — Qdrant ANN)
- `VEC_TOP_K_DEFAULT = 3` (internal qdrant_search top_k)
- `VEC_TOP_K_MULTIPLIER = 1` (recall top_k x multiplier)
- `VEC_MIN_SCORE = 0.70` (cosine below 0.70 directly discarded)

### BM25 Channel (Sparse — rank_bm25 + jieba)
- `BM25_TOP_K = 5` (candidate count)
- **Keyword filter softened**: no longer hard-blocks. BM25 handles term relevance; extra literal keyword filter kills synonyms. Changed to soft filter: top candidates pass directly, rest filtered by ratio, insufficient results → keep all.
- Parameters `BM25_KEYWORD_FILTER_RATIO` (default 0) and `BM25_KEYWORD_FILTER_MIN` (default 0)

### Graph Channel (Knowledge Graph — Neo4j 1-hop, no PRF expansion)
- `GRAPH_DEPTH = 1` (one-hop expansion, no multi-hop diffusion)
- `GRAPH_LIMIT_PER_NODE = 4` (max 4 expansions per node)
- **Changed (2026-09-08)**: No PRF trigger gate; direct 1-hop recall from Neo4j via `_graph_channel_with_sim()`. Results are normalized (`_channels=["graph"]`, `sort_key=graph_sim`) and fed directly into the fusion pool alongside vector recall candidates.

---

## IV. Fusion & Denoising (recall_fusion.py)

### RRF Fusion
- `RRF_K = 60` (Reciprocal Rank Fusion)
- **Multi-channel accumulation**: same memory hit by multiple channels → RRF scores **accumulate** (not max), building cumulative ranking advantage
- `RRF_RELATIVE_KEEP_RATIO = 0.95`

### Hook 1: Channel-level scoring (before rrf_fuse)
- `fusion_transform_channel`:
  - graph channel: `graph_depth_score` → 1-hop=1.0x, 2-hop=0.5x, 3-hop=0.33x
  - bm25 channel: ensures every entry has `sort_key` fallback

### Graph Channel — Direct Integration (replaces old PRF, 2026-09-08)
- **No multi-hop PRF expansion**. Single 1-hop Neo4j call via `_graph_channel_with_sim()`.
- Graph results are normalized and fed into the same fusion pipeline as vector candidates.
- **Previously**: PRF expansion was a separate conditional step gated by `PRF_MIN_GRAPH_SIM = 0.62`. Results were only used to supplement `filter_entities` but never entered the final candidate pool.
- **Now**: `fusion_boost_graph_hits` + `fusion_post_fuse` are called in Step 3.5, ensuring graph hits genuinely participate in ranking.
- Dedupe by `_qdrant_pid`

### Hook 2: Graph hit boost (after rrf_fuse)
- `fusion_boost_graph_hits`: graph-hit entries `sort_key x 1.3`
- Condition: `"graph" in _channels` AND summary contains graph entity names
- **Changed (2026-09-08)**: Now actually called in recall_4layer.py Step 3.5. Previously imported but never invoked.

### Hook 3: Post-fusion processing
- `fusion_post_fuse`:
  1. **Importance weighting**: `sort_key x (0.5 + importance)`, 0~1 maps to 0.5x~1.5x
  2. **Temporal decay**: `sort_key x 0.5^(Δdays/180)`, 180-day half-life
  3. `relation` type points restored to readable text via `parent_summary`
  4. Sort by sort_key descending
- **Called in recall_4layer.py Step 3.5 (previously imported but never invoked)**

### kg_verify_v2 (deprecated — not called in current pipeline)
- This function exists in recall_fusion.py but is **not called** in the current recall_4layer.py pipeline.
- Historically used query-memory embedding verification to filter low-similarity candidates; now bypassed.

---

## V. Step 3.5: Graph Fusion Integration (2026-09-08)

This step is the key change:

```
Step 4: atom (vector candidates) + graph_items (Neo4j 1-hop direct hits)
   │
   ▼
Normalization: graph_items get _channels=["graph"], sort_key=graph_sim
   │
   ▼
fusion_boost_graph_hits: graph entries sort_key x 1.3
   │
   ▼
fusion_post_fuse: importance weighting + temporal decay
   │
   ▼
Unified candidate pool (all channels merged)
```

After this, the unified pool goes to the Reranker stage.

---

## VI. Reranker Stage

```
Unified candidate pool
   │
   ▼
Single Reranker HTTP call (all candidates at once)
   │
   ▼
final_score = rerank x 0.6 + entity_overlap x 0.4
   │
   ▼
Hard filter: rerank_score < 0.55 → discard
   │
   ▼
Final Top-K
```

---

## VII. Association Expansion

Association expansion (Neo4j multi-hop → Qdrant) is **available but excluded from the current merged pipeline**:
- `ASSOC_ENABLED` switch still exists (default on)
- Triggered when `filter_entities` is present
- Multi-hop expansion would pollute results with off-topic hops
- Can be re-integrated in a future iteration

---

## VIII. Output Fields

Each final memory entry contains:

| Field | Description |
|-------|-------------|
| `summary` | Memory summary text |
| `score` | Raw vector similarity score |
| `rerank_score` | Reranker P(yes) score (0~1) |
| `final_score` | Comprehensive score (rerank x 0.6 + entity_overlap x 0.4) |
| `entity_overlap` | Overlap rate with seed entities |
| `recall_reason` | Trigger reason (direct match / 图谱直接召回) |
| `source` | Source (vec / bm25 / graph) |
| `importance` | Importance score |
| `event_time` | Event time |
| `_channels` | Channels that hit this memory (for fusion debugging) |

---

## IX. Intent Filtering

- Verb intents (喝/看/吃/爬): **softened**, no longer hard filter; defaults to soft ranking factor
- Attribute nouns (habits/interests/personality): weak signal, miss → sink to bottom, don't delete
- Parameters `INTENT_VERB_HARD_FILTER` (default False) and `INTENT_VERB_SOFT_WEIGHT` (default 0.05)

---

## X. Output Rules

- Return exactly as many entries as in the database, **never pad**
- Entries must satisfy: Summary >= 6 chars, not concatenated triples

---

## XI. Debug Logs

Set env var `MEMORY_OS_RECALL_DEBUG=1` to output to `logs/recall-debug.log`:

```
[RECALL] query="..."
  vec_raw=N bm25_raw=N bm25_filtered=N graph_raw=N rrf=N final=N
  [0] score=0.xxx sort_key=0.xxx src=vec summary="..."
```

---

## XII. Parameter Index (recall_config.py)

### Basic Recall
| Parameter | Default | Purpose |
|-----------|---------|---------|
| `BM25_KEYWORD_FILTER_RATIO` | 0.0 | BM25 keyword filter ratio, 0=off |
| `BM25_KEYWORD_FILTER_MIN` | 0 | BM25 keyword filter minimum keep count |
| `KG_SIM_RANKING_WEIGHT` | 0.5 | kg_verify sim weight (deprecated) |
| `INTENT_VERB_HARD_FILTER` | False | Verb intent hard filter switch |
| `INTENT_VERB_SOFT_WEIGHT` | 0.05 | Verb intent soft weighting magnitude |

### Graph Channel
| Parameter | Default | Purpose |
|-----------|---------|---------|
| `GRAPH_DEPTH` | 1 | Neo4j expansion depth (1 = no multi-hop) |
| `GRAPH_LIMIT_PER_NODE` | 4 | Max expansions per node |

### Reranker
| Parameter | Default | Purpose |
|-----------|---------|---------|
| `RERANK_THRESHOLD` | 0.55 | Hard filter: rerank < 0.55 → discard |

### Association Expansion (available but excluded from current pipeline)
| Parameter | Default | Purpose |
|-----------|---------|---------|
| `ASSOC_ENABLED` | 1 | Association expansion switch, 0=off |
| `ASSOC_MAX_HOPS` | 2 | Neo4j max expansion hops |
| `ASSOC_MAX_NEIGHBORS` | 6 | Max neighbor expansions per hop |
| `ASSOC_ACTIVATION_THRESHOLD` | 0.1 | Activation threshold |
| `ASSOC_DEPTH_DECAY` | 0.5 | Hop depth decay coefficient |
| `ASSOC_MAX_CANDIDATES` | 20 | Max associative candidate count |

### Debug
| Parameter | Default | Purpose |
|-----------|---------|---------|
| `RECALL_DEBUG` | "0" | Debug log switch, "1"=on |

---

## XIII. Related Files

| File | Purpose |
|------|---------|
| `src/index.js` | Plugin entry, `before_prompt_build` hook registration |
| `scripts/recall_4layer.py` | Recall main script (Step 1~4 + Step 3.5 fusion) |
| `scripts/process_dream.py` | Embedding + Qdrant low-level read/write |
| `scripts/recall_gate.py` | Hook gate (should_skip_recall / is_discardable) |
| `scripts/recall_fusion.py` | Fusion layer (fusion_boost_graph_hits / fusion_post_fuse / association_expand) |
| `scripts/recall_config.py` | All tunable parameters |
| `scripts/bm25_index.py` | BM25 sparse index |
| `scripts/extract_prompt.md` | KO extraction spec |
