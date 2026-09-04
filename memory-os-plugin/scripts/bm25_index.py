#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BM25 稀疏索引模块（Memory OS 召回第三通道）

设计原则：
  - 全程不动 process_dream.py / recall_fusion.py 任何已有函数
  - 只在 recall_fusion.py 的 fusion_transform_channel("bm25") 里被调用
  - 索引路径固定，不走环境变量（避免复杂化）

BM25 通道说明：
  - Qdrant 是"语义相似"召回，对精确词（人名/数字/专有名词）有时召不回
  - BM25 是"精确词匹配"，和人名/数字完全匹配的字面词一定召回来
  - 两路并行召回，RRF 融合，互补盲区
  - rank_bm25 库负责打分，jieba 负责分词（已有）

索引持久化：
  - 索引保存在 /tmp/memory_os_bm25.pkl（内存回收时自动清空，进程重启后重建）
  - write_kos_v5 回调 trigger_bm25_rebuild() 异步触发重建
"""

import os
import sys
import pickle
import threading
import hashlib
from pathlib import Path

try:
    import jieba
except ImportError:
    jieba = None

try:
    from rank_bm25 import BM25Okapi
except ImportError:
    BM25Okapi = None


INDEX_DIR = Path("/tmp")
INDEX_FILE = INDEX_DIR / "memory_os_bm25.pkl"
BM25_K1 = 1.5
BM25_B = 0.75
BM25_TOP_K = 20  # BM25 通道拉 top-20 候选


# ============================================================
# 持久化结构
# ============================================================

class BM25Index:
    def __init__(self):
        self.tokenized_corpus: list[list[str]] = []
        self.documents: list[dict] = []  # {"summary": str, "pid": str, "payload": dict}
        self.bm25: "BM25Okapi | None" = None
        self._corpus_hash: str = ""  # corpus 内容的 MD5，用于判断是否需要重建

    def corpus_hash(self) -> str:
        s = "|".join(d["summary"] for d in self.documents)
        return hashlib.md5(s.encode()).hexdigest()[:12]


# ============================================================
# 全局索引（进程内单例）
# ============================================================

_global_index: "BM25Index | None" = None
_global_lock = threading.Lock()
_rebuild_pending = False


def _load_index() -> "BM25Index | None":
    """从磁盘加载已序列化的 BM25 索引"""
    if not INDEX_FILE.exists():
        return None
    try:
        with open(INDEX_FILE, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _save_index(idx: BM25Index):
    """持久化 BM25 索引到磁盘"""
    try:
        with open(INDEX_FILE, "wb") as f:
            pickle.dump(idx, f)
    except Exception as e:
        print(f"[warn] BM25 index save failed: {e}", file=sys.stderr)


# ============================================================
# 核心：全量重建
# ============================================================

def _tokenize(text: str) -> list[str]:
    """用 jieba 分词，返回词列表（去停用词）"""
    if not jieba:
        return text.split()
    # jieba 分词结果
    tokens = list(jieba.cut(text))
    # 停用词（极简版：单字 + 英文单字母）
    stop = {"的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都",
            "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你",
            "会", "着", "没有", "看", "好", "自己", "这", "那", "他", "她",
            "它", "们", "吗", "吧", "啊", "呢", "哦", "嗯", "噢", "呀",
            "a", "b", "c", "i", "the", "is", "to", "and", "of", "in"}
    return [t for t in tokens if len(t) >= 2 and t not in stop]


def _fetch_all_summaries_from_qdrant() -> list[dict]:
    """从 Qdrant 所有 collection 拉全部记忆的 summary 和 payload"""
    try:
        from process_dream import _qdrant_client, RecallConfig
    except Exception:
        return []

    try:
        client = _qdrant_client()
    except Exception:
        return []

    collections = list(RecallConfig.COLLECTIONS)
    documents = []

    for coll in collections:
        try:
            # scroll API 拿全量（batch=1000）
            offset = None
            while True:
                res, offset = client.scroll(
                    collection_name=coll,
                    limit=1000,
                    with_payload=True,
                    offset=offset,
                )
                for pt in res:
                    payload = pt.payload or {}
                    summary = payload.get("summary") or payload.get("text") or ""
                    if not summary:
                        continue
                    documents.append({
                        "summary": summary,
                        "pid": str(pt.id),
                        "collection": coll,
                        "payload": payload,
                    })
                if offset is None:
                    break
        except Exception:
            continue

    return documents


def build_index() -> "BM25Index":
    """全量重建 BM25 索引"""
    global _global_index

    print("[bm25] building index...", file=sys.stderr)
    documents = _fetch_all_summaries_from_qdrant()
    if not documents:
        idx = BM25Index()
        _global_index = idx
        return idx

    tokenized = [_tokenize(d["summary"]) for d in documents]
    bm25 = BM25Okapi(tokenized)

    idx = BM25Index()
    idx.documents = documents
    idx.tokenized_corpus = tokenized
    idx.bm25 = bm25

    _save_index(idx)
    _global_index = idx
    print(f"[bm25] index built: {len(documents)} docs", file=sys.stderr)
    return idx


def get_index(lazy: bool = True) -> "BM25Index | None":
    """获取全局 BM25 索引（进程内缓存）"""
    global _global_index

    if _global_index is not None:
        return _global_index

    if not lazy:
        return build_index()

    # 懒加载：先尝试读磁盘缓存
    idx = _load_index()
    if idx is not None and idx.bm25 is not None:
        _global_index = idx
        print(f"[bm25] index loaded from cache: {len(idx.documents)} docs", file=sys.stderr)
        return idx

    return None


# ============================================================
# 查询
# ============================================================

def bm25_search(query: str, top_k: int = BM25_TOP_K) -> list[dict]:
    """
    BM25 通道召回入口。

    返回格式跟 vec_channel 兼容：
        list[{
            "summary": str,
            "pid": str,
            "collection": str,
            "score": float,          # BM25 raw score
            "norm_score": float,     # 归一化到 0~1
            "source": "bm25",
            "payload": dict,
        }]
    """
    if BM25Okapi is None:
        return []

    idx = get_index(lazy=True)
    if idx is None or idx.bm25 is None:
        return []

    tokens = _tokenize(query)
    if not tokens:
        return []

    raw_scores = idx.bm25.get_scores(tokens)

    # 打包 (doc_index, score)
    scored = [(i, raw_scores[i]) for i in range(len(idx.documents)) if raw_scores[i] > 0]
    scored.sort(key=lambda x: -x[1])

    max_score = scored[0][1] if scored else 1.0
    results = []
    for rank, (doc_idx, raw) in enumerate(scored[:top_k], start=1):
        doc = idx.documents[doc_idx]
        results.append({
            "summary": doc["summary"],
            "pid": doc["pid"],
            "collection": doc["collection"],
            "score": round(raw, 4),
            "norm_score": round(raw / max_score, 4),  # 归一化，用于 RRF 融合
            "source": "bm25",
            "rank": rank,
            "payload": doc["payload"],
        })

    return results


# ============================================================
# 异步重建触发（供 write_kos_v5 回调）
# ============================================================

def trigger_bm25_rebuild(async_build: bool = True):
    """
    触发 BM25 索引全量重建。
    - async=True（默认）：新线程后台跑，不阻塞写入
    - async=False：同步跑，写入线程等重建完成
    """
    global _rebuild_pending

    def _rebuild():
        global _rebuild_pending
        try:
            build_index()
        finally:
            _rebuild_pending = False

    if async_build:
        if _rebuild_pending:
            return  # 已在重建中，跳过
        _rebuild_pending = True
        t = threading.Thread(target=_rebuild, daemon=True)
        t.start()
    else:
        _rebuild()


# ============================================================
# 自检
# ============================================================

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).parent))

    print("BM25 index self-check...")
    idx = get_index(lazy=False)
    if idx:
        print(f"  docs: {len(idx.documents)}")
        print("  sample search: 用户")
        results = bm25_search("用户", top_k=5)
        for r in results:
            print(f"    [{r['norm_score']:.3f}] {r['summary'][:60]}")
    else:
        print("  index empty (no docs in Qdrant yet)")
