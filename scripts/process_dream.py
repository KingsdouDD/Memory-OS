#!/usr/bin/env python3
import os
os.environ['NO_PROXY'] = '127.0.0.1,localhost,::1'
os.environ['no_proxy'] = '127.0.0.1,localhost,::1'
"""Memory OS - 融合 recall + LLM 抽取脚本

设计原则：
  - 不写死模型/baseUrl/key：每次启动从 ~/.openclaw/openclaw.json 动态读取
    （agents.defaults.model.primary + models.providers.custom.*），跟着 OpenClaw 走
  - LLM 抽取 Knowledge Object（KO）JSON 数组，prompt 强制要求纯 JSON 输出
  - DISCARD 过滤在 prompt 和 Python 两层都做
  - 融合查询：Qdrant 向量通道 + Neo4j 图通道，RRF 重排
  - 输出精简：每条 4 字段（summary / relation / score / source），top_k 默认 8

CLI：
  recall --query "..." --top-k 8 --rrf-k 60
  ingest-kos --file <kos.json>      # 纯写库（agent 已抽取好的 KO JSON）
"""

import os
import sys
import json
import argparse
import re
import socket
import urllib.request
import urllib.error
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone

# 所有可调参数都集中在 recall_config.RecallConfig
from recall_config import RecallConfig
# 召回 / KO 抽取的前置门槛函数从 recall_gate 导入（已转用 RecallConfig）
from recall_gate import (
    is_discardable,
    should_skip_recall,
)
# 融合层（B 版）：通道打分 / 图命中 boost / 融合后处理 / kg_verify v2 / 写入文本构造
from recall_fusion import (
    fusion_transform_channel,
    fusion_boost_graph_hits,
    fusion_post_fuse,
    kg_verify_v2,
    build_qdrant_text, build_relation_text,
    fusion_graphrag_hooks,
    clean_ko_for_write,
)
try:
    from bm25_index import bm25_search, trigger_bm25_rebuild
    BM25_AVAILABLE = True
except Exception:
    BM25_AVAILABLE = False
    trigger_bm25_rebuild = None

# 强制 IPv4 优先（Qdrant / Neo4j 本地服务只监听 IPv4，Python 默认可能走 IPv6）
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_first_getaddrinfo(host, *args, **kwargs):
    infos = _orig_getaddrinfo(host, *args, **kwargs)
    # 把 IPv4 排到最前
    return sorted(infos, key=lambda i: 0 if i[0] == socket.AF_INET else 1)
socket.getaddrinfo = _ipv4_first_getaddrinfo

# --- Config from env（插件注入） ---
NEO4J_URI = os.environ.get("MEMORY_OS_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("MEMORY_OS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("MEMORY_OS_NEO4J_PASSWORD", "openclaw")
QDRANT_HOST = os.environ.get("MEMORY_OS_QDRANT_HOST", "127.0.0.1")
QDRANT_PORT = int(os.environ.get("MEMORY_OS_QDRANT_PORT", "6333"))
EMBED_URL = os.environ.get("MEMORY_OS_EMBED_URL", "http://127.0.0.1:8765/embed")
DEDUP_THRESHOLD = RecallConfig.DEDUP_THRESHOLD

CN_TZ = timezone(timedelta(hours=8))
OPENCLAW_CONFIG_PATH = Path(os.environ.get("OPENCLAW_CONFIG", str(Path.home()) + "/.openclaw/openclaw.json"))
HOOK_LOG_PATH = Path(os.environ.get(
    "MEMORY_OS_HOOK_LOG",
    str(Path.home()) + "/.openclaw/workspace/memory-os/logs/hook-trace.md",
))


# 上一轮 graph 通道召回出的 KG 三元组（neo4j_expand 写入，recall_for_hook 读取后写日志）
# 🔧 2026-08-10 删除：hook 走 log_hook_event，不读 module-level 缓存。死变量，留着只会让人误以为有用。


def _now_cn() -> str:
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def log_hook_event(event_type, **fields):
    """统一日志写入 hook-trace.md，直接在电脑上 cat/less 看。
    每条事件渲染成一段可读 markdown：
      ### HH:MM:SS event_type
      - key: value
      - key: value
    """
    try:
        ts_full = datetime.now(CN_TZ)
        ts_str = ts_full.isoformat(timespec="seconds")
        # 短时间戳（H2 标题用，一眼能扫到节奏）
        ts_short = ts_full.strftime("%H:%M:%S")

        lines = [f"### {ts_short} {event_type}", ""]
        for k, v in fields.items():
            # 多行字段（list / dict / 长字符串）走代码块，标量走 bullet
            if isinstance(v, (list, dict)):
                pretty = json.dumps(v, ensure_ascii=False, indent=2)
                lines.append(f"- **{k}**:")
                lines.append("")
                lines.append("```json")
                lines.append(pretty)
                lines.append("```")
            else:
                s = str(v)
                if "\n" in s or len(s) > 80:
                    lines.append(f"- **{k}**:")
                    lines.append("")
                    lines.append("```")
                    lines.append(s)
                    lines.append("```")
                else:
                    lines.append(f"- **{k}**: {s}")
            lines.append("")

        HOOK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HOOK_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n\n")
    except Exception as e:
        print(f"[warn] log_hook_event failed: {e}", file=sys.stderr)


_config_dump_done = False


def log_config_dump_once():
    """开发者 2026-08-09 删：config_dump 是噪音，记到日志里 30+ 个正则/常量，没必要。
    如果要查看生效参数，运行时跑：
      python3 process_dream.py --help   (没有)
      或直接看 recall_config.py 的默认值
    """
    pass

# ============================================================
# LLM 调用（OpenAI-compatible /chat/completions）
# ============================================================
# ============================================================
# summary 质量校验（开发者 2026-08-10 加）
# 防"印象式"垃圾记忆入库："外婆做事认真"、"用户童年充满快乐"
# 这类抽象/泛泛句召回时相关性差，写入时就该拦下。
# 判定：
#   - 太抽象：只含状态形容词（认真/快乐/正能量），无具体动作/事件
#   - 泛泛总结："充满了...""主要是..." 这类
#   - 无实体：一个实体名都没有（外婆/用户/助手…）
# ============================================================

_VAGUE_PATTERNS = [
    # "充满了" 只杀纯抽象总结（充满了快乐/幸福），"充满了...钓龙虾摸鱼" 是具体活动要留
    r"充满了(快乐|幸福|回忆|欢乐|爱|童年|阳光)",
    r"主要是|基本上是|总的来说|一般般|还不错",
    r"的性格|的特点|的为人|很认真|很善良|很正能量|很好|很棒",
    r"生活充满了|日常就是|平时就是",
]

# 抽象状态词：出现即怀疑是印象式记忆
_VAGUE_STATE_WORDS = {
    "认真", "善良", "正能量", "快乐", "开心", "幸福", "温柔", "体贴",
    "好", "棒", "厉害", "聪明", "可爱", "懂事", "努力", "勤奋",
    "强迫症", "性格", "特点", "为人", "样子", "生活", "日常",
}


# 泛动词：没有具体对象，不能证明是具体事件（做事/发生/进行…）
# 2026-08-11 扩充：干/做/搞/记/记得… 这类 query 侧泛动词，
# 如果当强意图词，摘要里几乎不会出现这些字 → 全部过滤 → 召回空。
# 好玩/开心… 是评价词，同样不该当强意图（摘要写的是“玩耍/开心”，不是“好玩”）
_GENERIC_VERBS = {"做事", "发生", "进行", "开始", "继续", "起来", "成为", "觉得", "感到", "认为",
                 "干", "做", "搞", "弄", "记", "记得", "知道", "想", "说", "讲", "问", "告诉", "了解",
                 "好玩", "开心", "快乐", "高兴", "有趣"}
# 系动词：后跟抽象状态时不算具体动作
_COPULA_VERBS = {"是", "有", "在", "像", "成为", "属于", "包含"}
# jieba 可能把情感动词标成名词（疼爱/n），显式补白名单
_EMOTION_VERBS = {"疼爱", "喜欢", "爱", "讨厌", "想念", "思念", "关心", "照顾", "陪伴", "保护", "支持", "鼓励"}
# 活动性动词：本身构成具体场景（玩耍/散步/爬山/游泳…），无需再带宾语
_ACTIVITY_VERBS = {"玩耍", "玩", "散步", "爬山", "游泳", "钓鱼", "跑步", "打球", "唱歌", "跳舞", "学习", "工作", "做饭", "买菜"}


def _has_concrete_action(summary):
    """判断 summary 是否有具体动作：动词 + 具体宾语。
    规则：jieba 分词后，存在一个动词 v，且 summary 里还有一个
    【名词/人名/专名】出现在 v 之后（作为宾语），且这个名词不是抽象状态词。
    "疼用户" → 动词"疼" + 名词"用户" ✅ 具体
    "做事有强迫症" → 动词"做事"(泛动词) + "强迫症"(抽象) ❌ 印象式
    """
    import jieba.posseg as pseg
    try:
        words = [(w.word, w.flag or "") for w in pseg.cut(summary)]
    except Exception:
        return True  # 分词失败不拦（保守放行）
    for i, (word, flag) in enumerate(words):
        is_verb = flag.startswith("v") or word in _EMOTION_VERBS
        if not is_verb:
            continue
        if word in _GENERIC_VERBS:
            continue
        if word in _ACTIVITY_VERBS:
            # 活动性动词（玩耍/散步/爬山）本身就算具体场景，直接判定具体
            return True
        if word in _COPULA_VERBS:
            # 系动词（是/有/在）：一律不算具体动作。
            # "有轻微强迫症"、"是疼用户的家人"——靠其他动词（疼/做/买）判断，
            # 系动词本身太弱，容易被"性格非常正能量"里的"能量"误判为宾语。
            continue
        # 普通动词：后面跟 名词(n)/处所(s)/代词(r) 都算具体宾语；
        # 动词/形容词（"好吃的"标 v、"买"后接物）也放行，只要不是抽象状态词
        for j in range(i + 1, len(words)):
            w2, f2 = words[j]
            if f2 in ("x", "w", "d", "p", "c", "u", "uj", "ul", "uz"):
                continue  # 标点/副词/介词/连词/助词跳过
            if w2 in _VAGUE_STATE_WORDS:
                continue
            return True
    return False


def _is_vague_summary(summary):
    """判定 summary 是否是抽象/泛泛句（应丢弃）。"""
    if not summary:
        return True
    s = summary.strip()
    # 太短
    if len(s) < 6:
        return True
    # 命中泛泛模式
    for p in _VAGUE_PATTERNS:
        if re.search(p, s):
            return True
    # 无具体动作 → 印象式（"外婆做事认真"这种）
    if not _has_concrete_action(s):
        return True
    return False


def parse_ko_json(raw):
    """从 LLM 输出里抓 JSON 数组。LLM 可能输出 think block、markdown 包装，先剥。"""
    if not raw:
        return []
    s = raw.strip()
    # 1) 剥 think block (M3 / DeepSeek / QwQ 等推理模型) - 不区分大小写
    #    兼容两种情况：①完整闭合的 <think>...</think>  ②只开了 <think> 后面直接输出 JSON 的
    s_after_closed = re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL | re.IGNORECASE).strip()
    if s_after_closed != s:
        # 有完整闭合块，剥完
        s = s_after_closed
    else:
        # 没匹配到闭合块 → 看是不是只开了 <think> 后面直接出 JSON
        think_open = re.search(r"<think>", s, flags=re.IGNORECASE)
        if think_open:
            bracket_pos = s.find("[", think_open.end())
            if bracket_pos != -1:
                # 保留 think 块之前的纯文本 + 从 [ 开始的 JSON
                s = (s[:think_open.start()] + s[bracket_pos:]).strip()
            else:
                # 根本没有 JSON，直接砍掉 think 块
                s = s[:think_open.start()].strip()
    s = re.sub(r"<thinking>.*?</thinking>", "", s, flags=re.DOTALL | re.IGNORECASE).strip()
    # 2) 剥 ```json ... ``` 或 ``` ... ```
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", s, flags=re.DOTALL)
    if m:
        s = m.group(1)
    # 3) 找到第一个 [ 和最后一个 ]
    if "[" in s and "]" in s:
        s = s[s.index("["): s.rindex("]") + 1]
    # 4) 抢救：如果上面都找不到，尝试匹配最外层的 {...} 块组成 fallback
    if not s.startswith("["):
        objs = re.findall(r"\{[^{}]*\}", raw, flags=re.DOTALL)
        if objs:
            s = "[" + ",".join(objs) + "]"
    try:
        arr = json.loads(s)
    except Exception as e:
        print(f"[warn] KO json parse failed: {e}\nraw head: {raw[:200]}", file=sys.stderr)
        return []
    if not isinstance(arr, list):
        return []
    # 二次过滤：DISCARD 检查
    out = []
    for ko in arr:
        if not isinstance(ko, dict):
            continue
        summary = (ko.get("summary") or "").strip()
        if not summary or len(summary) < 4:
            continue
        if is_discardable(summary):
            continue
        # 🔧 2026-08-10 加：抽象/印象式 summary 直接丢弃
        # （"外婆做事认真"这类无具体事件的记忆，召回时是噪音）
        if _is_vague_summary(summary):
            print(f"[info] dropped vague summary: {summary[:60]}", file=sys.stderr)
            continue
        ko["summary"] = summary[:200]
        out.append(ko)
    return out


# ============================================================
# ============================================================
# DISCARD 过滤（prompt 之外的 Python 兜底）
# - 常量 / 正则 / is_discardable 函数都已迁移到 recall_gate.py
# ============================================================


# Embedding client (HTTP) - 本地代理优先
# ============================================================
def embed(texts):
    """调本地 embedding 代理 (端口 8765)。
    进程 dead 时自动拉起服务（不常驻，只在使用时拉）。
    失败时不抛异常，返回空列表，让 Writer 跳过 Qdrant 写入（Neo4j 仍正常）。"""
    from service_lifecycle import ensure_service_up
    try:
        ensure_service_up(8765, max_wait=90)
    except Exception as e:
        print(f"[warn] ensure embed service up failed: {e}", file=sys.stderr)
        return []
    try:
        body = json.dumps({"texts": texts if isinstance(texts, list) else [texts]}).encode("utf-8")
        req = urllib.request.Request(
            EMBED_URL, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
            return data.get("vectors") or data.get("embeddings") or []
    except Exception as e:
        print(f"[warn] embed service unreachable: {e}", file=sys.stderr)
        return []


# ============================================================
# Vector channel (Qdrant) - 强制 IPv4
# ============================================================
def _qdrant_rest_url(path):
    return f"http://{QDRANT_HOST}:{QDRANT_PORT}{path}"


def qdrant_search(query_vec, collections, top_k=None):
    if top_k is None:
        top_k = RecallConfig.VEC_TOP_K_DEFAULT
    hits = []
    import requests
    for coll in collections:
        try:
            # 1. search 获取 point id + score
            resp = requests.post(
                _qdrant_rest_url(f"/collections/{coll}/points/search"),
                json={"vector": query_vec, "limit": top_k},
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            data = resp.json()
            results = data.get("result") or []
            if not results:
                continue
            # 2. 逐个 GET 获取完整 payload
            for r in results:
                pid = r["id"]
                try:
                    gr = requests.get(
                        _qdrant_rest_url(f"/collections/{coll}/points/{pid}"),
                        timeout=10,
                    )
                    if gr.status_code == 200:
                        pd = gr.json().get("result", {}).get("payload") or {}
                        hits.append((coll, {
                            "id": str(pid),
                            "score": float(r["score"]),
                            "payload": pd,
                        }))
                except Exception:
                    pass
        except Exception as e:
            print(f"[warn] qdrant {coll}: {e}", file=sys.stderr)
    # 按 score 全局降序排
    hits.sort(key=lambda x: -x[1]["score"])
    # score 硬阈值过滤
    min_score = getattr(RecallConfig, "VEC_MIN_SCORE", 0.0)
    if min_score > 0:
        before = len(hits)
        hits = [(c, h) for c, h in hits if h["score"] >= min_score]
        dropped = before - len(hits)
        if dropped > 0:
            print(f"[info] qdrant_search: dropped {dropped} hits below score {min_score}", file=sys.stderr)
    return hits


# ============================================================
# Graph channel (Neo4j)
# ============================================================
def _tokenize_for_kg(text):
    """中文+英文混合分词，优先用 jieba，缺失则 fallback 到滑动窗口。"""
    import re as _re
    tokens = []
    # 英文/数字单词直接保留
    tokens.extend(_re.findall(r'[\w]+', text))
    # 中文：jieba 直接吃原始 text，不做任何预替换
    try:
        import jieba
        for w in jieba.cut(text):
            w = w.strip()
            if len(w) >= 2 and _re.search(r'[\u4e00-\u9fff]', w):
                tokens.append(w)
    except ImportError:
        # fallback：2/3/4 字滑动窗口
        for n in (2, 3, 4):
            tokens.extend(_re.findall(rf'[\u4e00-\u9fff]{{{n}}}', text))
    return list({t for t in tokens if len(t) >= 2})


# ============================================================
# 词级意图加权（开发者 2026-08-10 加）
# 纯向量召回抓不住"喝/看/吃"这种动词意图：
#   query="用户喜欢喝什么" → 向量全被"用户"占权重，童年回忆全涌进来
#   query="外婆的饮食习惯" → "瑜伽"混进（句式太像）
# 解法：jieba 分词，分【意图词（动词）】和【实体词（名词）】，
# 排序时命中意图词的记忆显著加分，命中实体词的轻微加分。
# ============================================================

# 隐私敏感词：query 涉及这些时，不命中就强过滤（宁缺毋滥）
_PRIVACY_WORDS = {"密码", "账号", "余额", "银行卡", "支付", "转账", "验证码", "登录"}


_QUERY_STOPWORDS = {
    "的", "了", "吗", "呢", "啊", "吧", "么", "什么", "怎么", "如何", "为啥", "为什么",
    "在", "和", "跟", "与", "及", "一个", "一起", "有", "是", "就", "都", "也", "还",
    "这", "那", "我", "你", "他", "她", "它", "我们", "你们", "他们", "最近", "现在",
    "经常", "平时", "喜欢", "想要", "会", "能", "可以", "做", "什么", "哪里", "谁",
}


# 属性名词：描述"要哪类记忆"，而不是"谁/什么"。命中才算相关。
# 如 "饮食习惯" → summary 必须提到吃/喝；"性格特点" → 必须提到性格。
# 这些词当意图词用（首字前缀匹配），不当实体词。
_ATTRIBUTE_NOUNS = {
    "习惯", "爱好", "性格", "特点", "饮食", "口味", "偏好", "喜好",
    "兴趣", "日常", "生活", "工作", "学习", "穿着", "长相", "外貌",
    "家庭", "工作", "身体", "健康", "运动", "娱乐", "休闲",
}
# 属性词根：只要名词里含任一字根（如"饮食习惯"含"习惯"），就当意图词。
# 覆盖 jieba 把复合属性词切成一个整词的情况。
_ATTRIBUTE_ROOTS = ("习惯", "爱好", "性格", "特点", "饮食", "口味", "偏好",
                    "喜好", "兴趣", "长相", "外貌", "日常", "穿着")


def _extract_query_keywords(query):
    """从 query 提取 (意图词列表, 实体词列表)。
    意图词 = 动词（喝、看、吃、爬、玩…）+ 属性名词（习惯、爱好、性格…）
             —— 最能表达"想找什么"，summary 必须命中才算相关
    实体词 = 人名 / 地名 / 专名（外婆、用户、Memory OS…）
    """
    import jieba.posseg as pseg
    intent, entity = [], []
    try:
        for w in pseg.cut(query):
            word = w.word.strip()
            flag = w.flag or ""
            if not word or word in _QUERY_STOPWORDS:
                continue
            # 英文 / 专名（Memory、OS、neo4j）算实体
            if re.match(r"^[A-Za-z][A-Za-z0-9_-]+$", word):
                entity.append(word.lower())
                continue
            if word in _PRIVACY_WORDS:
                intent.append(word)
                continue
            if word in _ATTRIBUTE_NOUNS or any(root in word for root in _ATTRIBUTE_ROOTS):
                intent.append(word)
                continue
            if flag.startswith("n"):
                # 名词：人名地名(nr/ns)算实体，普通名词(n)也先算实体
                entity.append(word)
            elif flag.startswith("v"):
                # 泛动词（干/做/记/记得…）没有具体意图，不当强意图词
                # 2026-08-11 修复：否则摘要里没这些字 → 全滤光 → 召回空
                if word in _GENERIC_VERBS:
                    continue
                # 动词 → 意图词
                intent.append(word)
    except Exception:
        pass
    return intent, entity


def _kw_hit_count(summary, words, prefix=False):
    """summary 命中了几个词。
    🔧 2026-08-10 修复：支持前缀匹配（动词词形变化）：
      "看过" 应命中 summary 里的"看"。动词取【首字】前缀即可（"看"）。
      实体词（外婆/用户/爬山）必须全词匹配，避免"外"误伤"外婆"。
    prefix=True → 只要 summary 含词的首字符就算命中（仅意图词用）
    prefix=False → 全词匹配（实体词用）
    """
    if not words or not summary:
        return 0
    s = summary.lower()
    hit = 0
    for w in words:
        if not w:
            continue
        wl = w.lower()
        if prefix and wl:
            if wl[0] in s:
                hit += 1
        elif wl and wl in s:
            hit += 1
    return hit


def neo4j_entity_search(query_text, limit=8):
    """按 query 文本分词后逐词 CONTAINS 匹配 Neo4j entity 名字，返回去重后的候选名字列表。
    使用 jieba 中文分词（fallback 滑动窗口）确保「快乐」这类短词能被切出来。
    """
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USER, NEO4J_PASSWORD),
        # 🔧 2026-08-10 修复：老数据缺 event_time_start 等属性，
        # Neo4j 5+ 对不存在的 property key 发 notification → 每次召回刷几十条 warning。
        # 查询端已用 coalesce 兜底，notification 纯噪音，关掉。
        notifications_min_severity="OFF",
    )
    seen = set()
    names = []
    try:
        with driver.session() as session:
            tokens = _tokenize_for_kg(query_text)
            if not tokens:
                tokens = [query_text]

            cypher = """
            MATCH (n)
            WHERE n.name IS NOT NULL AND any(t in $tokens WHERE toLower(n.name) CONTAINS toLower(t))
            RETURN n.name AS name, labels(n) AS labels
            LIMIT $limit
            """
            for rec in session.run(cypher, tokens=tokens, limit=limit).data():
                name = rec["name"]
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                names.append(name)
    finally:
        driver.close()
    return names


def neo4j_expand(entity_names, depth=None, limit_per_node=None):
    if depth is None:
        depth = RecallConfig.GRAPH_DEPTH
    if limit_per_node is None:
        limit_per_node = RecallConfig.GRAPH_LIMIT_PER_NODE
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USER, NEO4J_PASSWORD),
        # 🔧 2026-08-10 修复：老数据缺 event_time_start 等属性，
        # Neo4j 5+ 对不存在的 property key 发 notification → 每次召回刷几十条 warning。
        # 查询端已用 coalesce 兜底，notification 纯噪音，关掉。
        notifications_min_severity="OFF",
    )
    results = []
    try:
        with driver.session() as session:
            # depth=1 → -[r]-（1 跳）；depth=2 → -[*1..2]-（1 到 2 跳全返）
            rel_pattern = "-[r]-" if depth <= 1 else f"-[*1..{int(depth)}]-"
            if depth <= 1:
                cypher = """
                MATCH (n {name: $name})-[r]-(m)
                WHERE m.name IS NOT NULL
                RETURN n.name AS subj, type(r) AS pred, m.name AS obj,
                       coalesce(r.status, 'active') AS status, labels(m) AS labels,
                       coalesce(r.ko_summary, '') AS ko_summary,
                       r.event_time_start AS et_start, r.event_time_end AS et_end,
                       coalesce(r.event_time_expression, '') AS et_expr,
                       coalesce(r.event_time_precision, 'unknown') AS et_prec,
                       r.valid_time_start AS vt_start, r.valid_time_end AS vt_end,
                       coalesce(r.valid_time_end_type, 'open') AS vt_end_type,
                       coalesce(r.recorded_at, '') AS recorded_at,
                       coalesce(r.source_time, '') AS source_time
                LIMIT $limit
                """
            else:
                cypher = """
                MATCH (n {name: $name})-[r*1..$depth]-(m)
                WHERE m.name IS NOT NULL
                RETURN n.name AS subj, type(r[0]) AS pred, m.name AS obj,
                       'active' AS status, labels(m) AS labels,
                       coalesce(r[0].ko_summary, '') AS ko_summary,
                       r[0].event_time_start AS et_start, r[0].event_time_end AS et_end,
                       coalesce(r[0].event_time_expression, '') AS et_expr,
                       coalesce(r[0].event_time_precision, 'unknown') AS et_prec,
                       r[0].valid_time_start AS vt_start, r[0].valid_time_end AS vt_end,
                       coalesce(r[0].valid_time_end_type, 'open') AS vt_end_type,
                       coalesce(r[0].recorded_at, '') AS recorded_at,
                       coalesce(r[0].source_time, '') AS source_time
                LIMIT $limit
                """
            for name in entity_names:
                for rec in session.run(cypher, name=name, depth=int(depth), limit=limit_per_node).data():
                    subj, pred, obj = rec["subj"], rec["pred"], rec["obj"]
                    labels = rec.get("labels") or []
                    kind = labels[0] if labels else "Node"
                    ko_summary = rec.get("ko_summary") or ""
                    if ko_summary:
                        summary = ko_summary
                    else:
                        summary = f"{subj} {pred} {obj}"
                    # 组装该关系的时间四元组（从 Neo4j 边属性读出来）
                    edge_event_time = {
                        "start": rec.get("et_start"),
                        "end": rec.get("et_end"),
                        "expression": rec.get("et_expr") or "",
                        "precision": rec.get("et_prec") or "unknown",
                    }
                    edge_valid_time = {
                        "start": rec.get("vt_start"),
                        "end": rec.get("vt_end"),
                        "end_type": rec.get("vt_end_type") or "open",
                    }
                    results.append({
                        "summary": summary,
                        "relation": f"{kind}:{subj} -[{pred}]-> {kind}:{obj}",
                        "score": 1.0,
                        "source": "graph",
                        "anchor": name,
                        "depth": depth,
                        "raw_triples": [{"subj": subj, "pred": pred, "obj": obj, "ko_summary": ko_summary}],
                        "labels": labels,
                        "event_time": edge_event_time,
                        "valid_time": edge_valid_time,
                        "recorded_at": rec.get("recorded_at") or "",
                        "source_time": rec.get("source_time") or "",
                    })
    finally:
        driver.close()
    return results


# ============================================================
# 融合：Reciprocal Rank Fusion
# ============================================================
def rrf_fuse(channels, k=60):
    """RRF 融合，按 summary 全键去重。
    🔧 2026-08-10 修复：原 payload_map 同 key 合并时 src 拼成 "vec+kg_prf+graph"
    导致后续判断 'graph' in source 不准。现在只保留首个通道的 source（记录 *_channels）。
    """
    scores, payload_map = {}, {}
    for channel_results in channels:
        for rank, item in enumerate(channel_results, start=1):
            key = item.get("summary", "")
            if not key:
                continue
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            if key not in payload_map:
                payload_map[key] = dict(item)
                payload_map[key]["_channels"] = [item.get("source", "")]
            else:
                # 只追加到 _channels，不动 source
                chs = payload_map[key].get("_channels") or []
                src_now = item.get("source", "")
                if src_now and src_now not in chs:
                    chs.append(src_now)
                payload_map[key]["_channels"] = chs
    fused = sorted(payload_map.values(), key=lambda x: scores[x["summary"]], reverse=True)
    if fused:
        max_score = scores[fused[0]["summary"]]
        for item in fused:
            item["score"] = round(scores[item["summary"]] / max_score, 4)
    return fused


# ============================================================
# KG 验证（第三层）：实体重叠过滤（不做 LLM 调用）
# ============================================================
# 开发者 2026-08-10：kg_verify 已废弃，召回主流程用 recall_fusion.kg_verify_v2。
# 本地 kg_verify 是死代码。删除避免后人改错地方。
# 主 recall 入口
# ============================================================
# 从 RecallConfig 读取要查询的 collection 列表（env 可覆盖）
COLLECTIONS_TO_QUERY = list(RecallConfig.COLLECTIONS)


def recall(query, top_k=None, rrf_k=None):
    if top_k is None:
        top_k = RecallConfig.RECALL_DEFAULT_TOP_K
    if rrf_k is None:
        rrf_k = RecallConfig.RECALL_DEFAULT_RRF_K
    """手动调用（被动查）的 recall，不做内容门控。"""
    try:
        qvec = embed(query)
        if qvec and isinstance(qvec[0], list):
            qvec = qvec[0]
    except Exception as e:
        print(f"[warn] embed failed: {e}", file=sys.stderr)
        qvec = []

    vec_channel = []
    if qvec:
        vec_hits = qdrant_search(qvec, COLLECTIONS_TO_QUERY, top_k=top_k * RecallConfig.VEC_TOP_K_MULTIPLIER)
        for coll, hit in vec_hits:
            payload = hit["payload"]
            text = payload.get("summary") or payload.get("text") or ""
            if not text:
                continue
            vec_channel.append({
                "summary": text,
                "relation": payload.get("memory_type", ""),
                "score": hit["score"],
                "source": "vec",
                "collection": coll,
                "_qdrant_pid": hit.get("id"),
                "_point_type": payload.get("_point_type", ""),
                "_relation_text": payload.get("summary", "") or "",
                "parent_summary": payload.get("parent_summary") or "",
                "importance": payload.get("importance", 0.5),
                "ts": payload.get("ts", ""),
                "tags": payload.get("tags") or [],
                # ⏰ 4 个时间字段（从 Qdrant payload 读）
                "event_time": payload.get("event_time") or {},
                "valid_time": payload.get("valid_time") or {},
                "recorded_at": payload.get("recorded_at") or "",
                "source_time": payload.get("source_time") or "",
            })

    graph_channel = []
    try:
        entity_names = neo4j_entity_search(query, limit=5)
        if entity_names:
            graph_channel = neo4j_expand(entity_names)
    except Exception as e:
        print(f"[warn] neo4j channel failed: {e}", file=sys.stderr)

    # ===== BM25 稀疏通道（并行召回）=====
    bm25_channel = []
    if BM25_AVAILABLE:
        try:
            bm25_results = bm25_search(query, top_k=RecallConfig.BM25_TOP_K)  # BM25 精确匹配，顶5条
            for r in bm25_results:
                payload = r.get("payload", {})
                bm25_channel.append({
                    "summary": r.get("summary") or "",
                    "relation": payload.get("memory_type", ""),
                    "score": r.get("norm_score", r.get("score", 0)),
                    "source": "bm25",
                    "collection": r.get("collection", ""),
                    "_qdrant_pid": r.get("pid"),
                    "importance": payload.get("importance", 0.5),
                    "ts": payload.get("ts", ""),
                    "tags": payload.get("tags") or [],
                    "event_time": payload.get("event_time") or {},
                    "valid_time": payload.get("valid_time") or {},
                    "recorded_at": payload.get("recorded_at") or "",
                    "source_time": payload.get("source_time") or "",
                })
        except Exception as e:
            print(f"[warn] bm25 channel failed: {e}", file=sys.stderr)

    # ===== BM25 关键词过滤：降为软过滤，不阻断高 BM25 分结果（开发者 2026-08-21）=====
    # BM25 本身负责词项相关性排序，额外字面硬过滤会误杀同义表达。
    # 例如 query="爬山"，summary="去黄山登山"，无"爬"字但语义完全相关，不应丢弃。
    # 改为：
    #   1. 高 BM25 分数结果直接保留（bm25_rank <= BM25_KEYWORD_FILTER_MIN，全部过）
    #   2. 其余结果：要求 BM25_KEYWORD_FILTER_RATIO 比例的结果含关键词，不足则全部保留
    #   3. 关键词过滤只打标记，不阻断任何结果
    try:
        import jieba
        query_keywords = set(jieba.cut(query))
        query_keywords = {w for w in query_keywords if len(w) >= 2}
    except Exception:
        query_keywords = set()

    bm25_before_kwfilter = len(bm25_channel)
    bm25_with_kw = 0
    for item in bm25_channel:
        summary = item.get("summary") or ""
        has_kw = bool(query_keywords and any(kw in summary for kw in query_keywords))
        item["_bm25_has_keyword"] = has_kw
        if has_kw:
            bm25_with_kw += 1

    ratio = RecallConfig.BM25_KEYWORD_FILTER_RATIO
    min_keep = RecallConfig.BM25_KEYWORD_FILTER_MIN
    total = len(bm25_channel)

    # 按 BM25 分数降序排列，保留头部高分结果
    bm25_channel.sort(key=lambda x: -float(x.get("score", 0)))

    # 头部 BM25_KEYWORD_FILTER_MIN 条直接保留（强制保底）
    safe_keep = min(min_keep, total) if min_keep > 0 else 0

    if ratio > 0 and total > safe_keep:
        required = max(safe_keep, int(total * ratio))
        # 从 safe_keep 位置开始：含关键词的优先保留，直到达到 required 条
        kw_pass = [item for item in bm25_channel[safe_keep:] if item.get("_bm25_has_keyword")]
        kw_pass += [item for item in bm25_channel[safe_keep:] if not item.get("_bm25_has_keyword")]
        bm25_channel = bm25_channel[:safe_keep] + kw_pass[:required - safe_keep]
    # ratio=0 或 min_keep >= total → 全部保留

    # ===== fusion layer 接入点 1：通道级打分（rrf_fuse 之前）=====
    vec_channel = fusion_transform_channel(vec_channel, "vec")
    graph_channel = fusion_transform_channel(graph_channel, "graph")
    bm25_channel = fusion_transform_channel(bm25_channel, "bm25")
    # GraphRAG 预留钩子（未来接社区摘要 / 实体子图）
    graph_channel = fusion_graphrag_hooks(graph_channel)

    # ===== 业眾改造 2026-08-09：KG 召回携带 ko_summary 后，反哺 vec 通道召回 =====
    # 业界 GraphRAG / HippoRAG 思路：KG 不当最终结果，只当路由。
    # RRF 融合中，ko_summary 当 query 扩展项，反哺 qdrant 召一轮。
    # 修复 2026-08-09 19:38：扩展不能只用 ko_summary（会被实体名带偏召不相关 KO），
    # 要拼回原始 query 保持主题不偏。
    # 🔧 2026-08-10 修复 #2：原阈值 len(s) > 6 太严格，把 "外婆爬山" 这种 ko_summary 一刀切掉。
    #     放宽到 len(s) >= 2（实体名至少 2 字），并记录原始全 ID 便于按 point id 去重。
    # ── 2026-08-21：PRF 触发逻辑重构 ──
    # 优先级：
    #   1. 实体/关系 evidence（raw_triples 里的 subj/obj）→ 主要触发信号，稳定可靠
    #   2. 字面 token 重叠（PRF_TOKEN_OVERLAP_MIN > 0 时启用）→ 兜底，不作唯一条件
    # 不再强制要求字面重叠才触发 PRF，避免"爬山"vs"登山"等同义表达被拦。
    kg_summaries = []
    query_tokens = set(_tokenize_for_kg(query))
    for g in graph_channel:
        s = g.get("summary") or ""
        if not s or len(s) < 2 or " -[" in s:
            continue
        # 方式A：实体/关系 evidence（优先）
        raw_triples = g.get("raw_triples", []) or []
        has_entity_evidence = False
        for triple in raw_triples:
            subj = (triple.get("subj") or "").lower()
            obj = (triple.get("obj") or "").lower()
            if subj and subj in query.lower():
                has_entity_evidence = True
                break
            if obj and obj in query.lower():
                has_entity_evidence = True
                break
        # 方式B：字面 token 重叠（仅在 PRF_TOKEN_OVERLAP_MIN > 0 时启用）
        has_token_evidence = False
        token_min = RecallConfig.PRF_TOKEN_OVERLAP_MIN
        if token_min > 0 and query_tokens:
            s_lower = s.lower()
            overlap = sum(1 for t in query_tokens if t.lower() in s_lower)
            if overlap >= token_min:
                has_token_evidence = True
        # 任一 evidence 满足即可
        if has_entity_evidence or has_token_evidence:
            kg_summaries.append(s)
    # 🔧 2026-08-10 修复：记录 PRF 召回的 qdrant point id，RRF 后按 id 去重（不依赖文本）
    prf_seen_pids = set()
    if kg_summaries and qvec:
        # 拼接：原始 query + 2~3 条 ko_summary 一起作 query
        expansion_text = (query + " ") + " ".join(kg_summaries[:3])
        if expansion_text:
            try:
                exp_vec = embed(expansion_text)
                if exp_vec and isinstance(exp_vec[0], list):
                    exp_vec = exp_vec[0]
                if exp_vec:
                    exp_hits = qdrant_search(exp_vec, COLLECTIONS_TO_QUERY, top_k=top_k)
                    for coll, hit in exp_hits:
                        pid = hit.get("id")
                        if pid:
                            prf_seen_pids.add(pid)
                        payload = hit["payload"]
                        text = payload.get("summary") or payload.get("text") or ""
                        if not text:
                            continue
                        vec_channel.append({
                            "summary": text,
                            "relation": payload.get("memory_type", ""),
                            "score": hit["score"] * 0.9,  # RRF 扩展轮打9折，避免拉跨原结果
                            "source": "vec+kg_prf",
                            "collection": coll,
                            "_qdrant_pid": pid,
                            "importance": payload.get("importance", 0.5),
                            "ts": payload.get("ts", ""),
                            "tags": payload.get("tags") or [],
                            # ⏰ 4 个时间字段
                            "event_time": payload.get("event_time") or {},
                            "valid_time": payload.get("valid_time") or {},
                            "recorded_at": payload.get("recorded_at") or "",
                            "source_time": payload.get("source_time") or "",
                        })
            except Exception as e:
                print(f"[warn] KG→vec RRF 扩展轮召回失败: {e}", file=sys.stderr)
    # 🔧 2026-08-10：记录 Round 1 召回的 pid，PRF 轮遇同 pid 去掉
    prf_seen_pids_round1 = set()
    for v in vec_channel:
        if v.get("source") == "vec":
            pid = v.get("_qdrant_pid")
            if pid:
                prf_seen_pids_round1.add(pid)

    fused = rrf_fuse([vec_channel, graph_channel, bm25_channel], k=rrf_k)

    # ===== fusion layer 接入点 2：图命中 boost（rrf_fuse 之后）=====
    # 原来的 *1.0 是 bug，现在用可配 boost=1.3
    graph_entity_names = set()
    for item in graph_channel:
        for r in item.get("raw_triples", []) or []:
            graph_entity_names.add(r.get("subj", "").lower())
            graph_entity_names.add(r.get("obj", "").lower())
    fused = fusion_boost_graph_hits(fused, graph_entity_names, boost=1.3)

    # ===== fusion layer 接入点 3：融合后处理（importance 加权 + 时间衰减）=====
    fused = fusion_post_fuse(fused)

    # ===== 业眾改造 2026-08-09：最终输出阶段，graph 通道只当补充信息，不进主选表 =====
    # 业界做法：KG 是路由不是产物。kg_verify_v2 后的 top-N 里只保留 vec / vec+kg_prf 通道的，
    # graph 通道的原始三元组只能作为弱补位放在最后（数量上限）
    kg_verified = kg_verify_v2(fused, query)

    # 🔧 2026-08-10 修复 #2收尾：vec 通道按 _qdrant_pid 去重（PRF 轮 / Round 1 同 pid 留高分）
    vec_results = []
    graph_results = []
    seen_pids = set()        # 主键：qdrant point id（PRF / Round 1 同 id 去重）
    seen_summaries = set()   # 兜底：summary 前 60 字（pid 缺失时）
    for item in kg_verified:
        src = item.get("source", "")
        pid = item.get("_qdrant_pid")
        key = (item.get("summary") or "")[:60].strip()
        if not key:
            continue
        if pid and pid in seen_pids:
            continue
        if not pid and key in seen_summaries:
            continue
        if pid:
            seen_pids.add(pid)
        else:
            seen_summaries.add(key)
        if src.startswith("vec"):
            vec_results.append(item)
        else:
            graph_results.append(item)

    # ============================================================
    # 召回输出端（开发者铁律：NEVER 凑数）
    # ============================================================
    # 凑数禁令（铁律）：
    #   - 库里召回多少条就返回多少条
    #   - NEVER 为了凑到某个目标数而拉低阈值 / 补空 / 补不相关
    #   - 库里只有 1 条相关的就返 1 条，0 条就返 0 条
    #   - 凑数 = 召回污染，比少更糟
    # ============================================================

    # 主力：vec 召回，库里多少就返多少，绝不凑
    output = []
    for item in vec_results:
        et = item.get("event_time") or {}
        vt = item.get("valid_time") or {}
        output.append({
            "summary": item["summary"],
            "relation": item.get("relation", ""),
            # 🔧 2026-08-10 修复：score 显示 sim（query↔记忆真实相似度 0~1），
            # 不再显示被 importance 加权污染的 RRF 分数（之前会出现 1.19 这种 >1 的假高分）
            "score": item.get("sim") if item.get("sim") is not None else item.get("score", 0),
            "sort_key": item.get("sort_key", 0),
            "sim": item.get("sim", 0),
            "source": item.get("source", ""),
            # 🔧 2026-08-29 修复：pid 全链路传递，delete 工具依赖此字段
            "_qdrant_pid": item.get("_qdrant_pid"),
            # ⏰ 4 个时间字段（event_time + recorded_at 是主要召回信息）
            "event_time": et,
            "valid_time": vt,
            "recorded_at": item.get("recorded_at") or "",
            "source_time": item.get("source_time") or "",
        })

    # 补位：graph 召回，库里多少就遍历多少，绝不凑
    graph_added = 0
    for item in graph_results:
        summary = item.get("summary", "")
        if not summary or len(summary) < 6:
            continue
        # 检查是不是拼出来的干三元组：subj pred obj 格式（中间一个空格隔开）
        # ko_summary 是自然语言句子，应该 > 10 字 且 不能只是 "X Y Z" 三个词的拼凑
        parts = summary.split()
        if len(parts) <= 3:
            continue  # 太短，肯定是拼的
        # 如果是以 [大写谓词] 隔开的（如 "外婆 RELATES_TO 君君"）也跳过
        if any(p.isupper() and len(p) > 3 for p in parts):
            continue
        et = item.get("event_time") or {}
        vt = item.get("valid_time") or {}
        output.append({
            "summary": summary,
            "relation": item.get("relation", ""),
            "score": item.get("sim") if item.get("sim") is not None else item.get("score", 0),
            "sort_key": item.get("sort_key", 0),
            "source": item.get("source", ""),
            # ⏰ 4 个时间字段
            "event_time": et,
            "valid_time": vt,
            "recorded_at": item.get("recorded_at") or "",
            "source_time": item.get("source_time") or "",
        })
        graph_added += 1

    # 🔧 2026-08-10 修复：输出端统一按 sim 降序混排（vec + graph 一起）。
    # 之前 graph 永远垫底（补位设计），导致 sim=0.77 的 graph 记忆排在 sim=0.69 的 vec 后面，
    # 用户看到 score 和顺序对不上。sim 是唯一可信的相关性度量，混排最直观。
    # ── 2026-08-21：综合排序 ──
    # sort_key 在 kg_verify_v2 里已 = sort_key*(1-w) + sim*w，综合了多通道证据。
    # 用 sort_key 排序，不再用裸 sim（避免抹掉 RRF+boost+importance+time_decay 积累）。
    output.sort(key=lambda x: -float(x.get("sort_key", x.get("score", 0))))

    # ── 2026-08-21：调试日志（全链路候选数量统计）──
    _dbg = os.environ.get("MEMORY_OS_RECALL_DEBUG", "0")
    if _dbg == "1":
        _log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
        try:
            os.makedirs(_log_dir, exist_ok=True)
            with open(os.path.join(_log_dir, "recall-debug.log"), "a", encoding="utf-8") as _lf:
                _lf.write(f"[RECALL] query={query[:60]!r}\n")
                _lf.write(
                    f"  vec_raw={len(vec_channel)} "
                    f"bm25_raw={bm25_before_kwfilter} "
                    f"bm25_filtered={bm25_before_kwfilter - len(bm25_channel)} "
                    f"graph_raw={len(graph_channel)} "
                    f"prf_kg={len(kg_summaries)} "
                    f"rrf={len(fused)} "
                    f"kg_verified={len(kg_verified)} "
                    f"final={len(output)}\n"
                )
                for _idx, _item in enumerate(output):
                    _lf.write(
                        f"  [{_idx}] score={_item.get('score',0):.3f} "
                        f"sort_key={_item.get('sort_key',0):.3f} "
                        f"sim={_item.get('sim',0):.3f} "
                        f"src={_item.get('source','')} "
                        f"summary={_item.get('summary','')[:40]!r}\n"
                    )
                _lf.write("\n")
        except Exception:
            pass

    return {
        "memories": output,
        "query": query,
        "channels": {
            "vec": len(vec_channel),
            "bm25": len(bm25_channel),
            "bm25_kw_filtered": bm25_before_kwfilter - len(bm25_channel),
            "graph": len(graph_channel),
            "prf_kg_summaries": len(kg_summaries),
        },
    }


# ============================================================
# ============================================================
# Hook 门控：所有常量与函数都已迁移到 recall_gate.py
# - HOOK_MIN_LEN / HOOK_MAX_LEN / HOOK_SKIP_FILLER / HOOK_SKIP_SWEAR_EXTRA / should_skip_recall
# ============================================================


def recall_for_hook(query, top_k=None, rrf_k=None):
    """Hook 调用的 recall：先门控，通过后才走融合通道。
    返回 dict：
      - skip=True → {skipped: True, reason: "..."}
      - skip=False → {memories: [...], channels: {...}, query: ...}
    每次调用都写一条 hook-trace.md 日志。
    """
    # 进程启动时 dump 一次当前生效参数
    log_config_dump_once()
    if top_k is None:
        top_k = RecallConfig.RECALL_DEFAULT_TOP_K
    if rrf_k is None:
        rrf_k = RecallConfig.RECALL_DEFAULT_RRF_K
    skip, reason = should_skip_recall(query)
    if skip:
        log_hook_event(
            "recall_skipped",
            query=query[:200],
            reason=reason,
            len=len(query),
            n_memories=0,
        )
        return {"skipped": True, "reason": reason, "memories": [], "query": query}
    result = recall(query, top_k=top_k, rrf_k=rrf_k)
    result["skipped"] = False
    result["reason"] = ""
    # ----- 开发者 2026-08-09 删：JS 端 before_prompt_build 已经会写 injection_committed，
    # ----- 这里再写 recall_injected 是同一份数据的双写（query/memories/channels 全重叠）。
    # ----- 召回成功路径只在 JS 端落盘；本函数返回 dict 让 JS 用。
    return result


# ============================================================
# 梦境摄取（每日任务）
# ============================================================
def resolve_dream_path(date=None, kind="light", file=None):
    """解析 ingest 目标文件路径。优先级：--file > --date+--kind > 当天 light。
    date 默认 = 当前 CN 日期。
    """
    if file:
        return Path(file)
    if date is None:
        date = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    base = Path(str(Path.home()) + "/.openclaw/workspace/memory/dreaming")
    return base / kind / f"{date}.md"


def find_yesterday_dream_files():
    yesterday = (datetime.now(CN_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    base = Path(str(Path.home()) + "/.openclaw/workspace/memory/dreaming")
    candidates = [
        base / "light" / f"{yesterday}.md",
        base / "rem" / f"{yesterday}.md",
    ]
    return [p for p in candidates if p.exists()]


# ============================================================
# Writer: Neo4j + Qdrant (MERGE / cosine-dedup)
# ============================================================
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct


def _qdrant_client():
    return QdrantClient(url=f"http://{QDRANT_HOST}:{QDRANT_PORT}")


def qdrant_collection_for(kotype):
    return f"memory_{kotype}"


def qdrant_ensure_collection(name, vector_size=1024):
    try:
        client = _qdrant_client()
        client.get_collection(name)
    except Exception:
        try:
            client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )
        except Exception as e:
            print(f"[warn] ensure collection {name}: {e}", file=sys.stderr)


def qdrant_dedup_check(client, collection, query_vec, threshold):
    """ANN 查重：cosine > threshold 返回命中的 point id 列表（top-3）。
    不再返回 True/False，改为返回命中的 [point_id] 列表。
    空列表 = 无重复，命中则列表长度 >= 1。
    """
    try:
        res = client.query_points(collection_name=collection, query=query_vec, limit=3)
        hits = res.points
    except Exception:
        return []
    matched = [str(h.id) for h in hits if h.score >= threshold]
    return matched if matched else []


def qdrant_upsert_point(client, collection, point_id, vector, payload):
    client.upsert(
        collection_name=collection,
        points=[PointStruct(id=point_id, vector=vector, payload=payload)],
    )


def _now_cn_iso():
    """写库瞬间的 CN 时区 ISO8601 时间戳。"""
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def _normalize_time_fields(ko, source_path=None):
    """Python 端强制补齐 4 个时间字段。
    - event_time / valid_time 由 LLM 抽，允许 null / 模糊 expression
    - recorded_at：写库瞬间（必填，Python 端强制）
    - source_time：上游材料时间戳（梦境文件 mtime / 微信消息 timestamp / 文档 mtime）
    """
    now_iso = _now_cn_iso()
    # recorded_at：始终强制覆盖为写库瞬间
    ko["recorded_at"] = now_iso
    # source_time：从 source_path 推断；LLM 给的 source_time 仅在没有路径时保留
    src_time = None
    if source_path:
        try:
            src_time = datetime.fromtimestamp(
                Path(source_path).stat().st_mtime, tz=CN_TZ
            ).isoformat(timespec="seconds")
        except Exception:
            src_time = None
    if not src_time:
        # 退路：从 ko['source'] 推断（如 dream:light:2026-08-08 → 23:59）
        src = ko.get("source") or ""
        m = re.search(r"(\d{4}-\d{2}-\d{2})", src)
        if m:
            src_time = f"{m.group(1)}T23:59:59+08:00"
    ko["source_time"] = src_time or now_iso
    # event_time / valid_time 保证是 dict，缺字段补 None
    et = ko.get("event_time")
    if not isinstance(et, dict):
        et = {}
    ko["event_time"] = {
        "start": et.get("start"),
        "end": et.get("end"),
        "expression": et.get("expression") or "",
        "precision": et.get("precision") or "unknown",
    }
    vt = ko.get("valid_time")
    if not isinstance(vt, dict):
        vt = {}
    ko["valid_time"] = {
        "start": vt.get("start"),
        "end": vt.get("end"),
        "end_type": vt.get("end_type") or "open",
    }
    return ko


def _relation_time_fields(rel):
    """关系也带 4 个时间字段（如果 LLM 没给就给空 dict，由 SET 默认补）"""
    et = rel.get("event_time")
    if not isinstance(et, dict):
        et = {}
    vt = rel.get("valid_time")
    if not isinstance(vt, dict):
        vt = {}
    return {
        "event_time_start": et.get("start"),
        "event_time_end": et.get("end"),
        "event_time_expression": et.get("expression") or "",
        "event_time_precision": et.get("precision") or "unknown",
        "valid_time_start": vt.get("start"),
        "valid_time_end": vt.get("end"),
        "valid_time_end_type": vt.get("end_type") or "open",
    }


def neo4j_upsert_ko(ko):
    """单个 KO → Neo4j（MERGE 实体 + MERGE 关系）。
    Cypher MERGE 的关系类型必须在 schema 里写死，所以这里按 predicate 拼接 cypher。
    实体节点和关系边都带 4 个时间字段（event_time / valid_time / recorded_at / source_time）。

    🔧 2026-08-10：写入端硬卡 schema 白名单。predicate 不在 ALLOWED_RELATIONSHIPS →
    MENTIONED_IN；label 不在 ALLOWED_LABELS → Concept；中文谓词 / 拒收词 → MENTIONED_IN。
    """
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USER, NEO4J_PASSWORD),
        # 🔧 2026-08-10 修复：老数据缺 event_time_start 等属性，
        # Neo4j 5+ 对不存在的 property key 发 notification → 每次召回刷几十条 warning。
        # 查询端已用 coalesce 兜底，notification 纯噪音，关掉。
        notifications_min_severity="OFF",
    )
    written = {"entities": 0, "relations": 0}
    # 时间字段从 KO 顶层读
    et = ko.get("event_time") or {}
    vt = ko.get("valid_time") or {}
    rec_at = ko.get("recorded_at") or _now_cn_iso()
    src_at = ko.get("source_time") or rec_at
    # 白名单从 RecallConfig 读（唯一权威源）
    ALLOWED_REL = RecallConfig.ALLOWED_RELATIONSHIPS
    ALLOWED_LABELS = RecallConfig.ALLOWED_LABELS
    DENIED_WORDS = RecallConfig.DENIED_PREDICATE_WORDS
    try:
        with driver.session() as session:
            # 1) 实体 — 节点带 4 个时间字段
            for ent in ko.get("entities") or []:
                name = (ent.get("name") or "").strip()
                if not name:
                    continue
                label = ent.get("label") or "Concept"
                # label 白名单外降级为 Concept
                if label not in ALLOWED_LABELS:
                    label = "Concept"
                session.run(
                    f"""MERGE (n:{label} {{name: $name}})
                       SET n.updated = $ts,
                           n.event_time_start = $et_start,
                           n.event_time_end = $et_end,
                           n.event_time_expression = $et_expr,
                           n.event_time_precision = $et_prec,
                           n.valid_time_start = $vt_start,
                           n.valid_time_end = $vt_end,
                           n.valid_time_end_type = $vt_end_type,
                           n.recorded_at = $rec_at,
                           n.source_time = $src_at""",
                    name=name, ts=_now_cn(),
                    et_start=et.get("start"), et_end=et.get("end"),
                    et_expr=et.get("expression") or "", et_prec=et.get("precision") or "unknown",
                    vt_start=vt.get("start"), vt_end=vt.get("end"),
                    vt_end_type=vt.get("end_type") or "open",
                    rec_at=rec_at, src_at=src_at,
                )
                written["entities"] += 1
            # 2) 关系 — 边也带 4 个时间字段（来自每条 rel 自己的 event_time / valid_time）
            for rel in ko.get("relations") or []:
                subj = (rel.get("subject") or "").strip()
                obj = (rel.get("object") or "").strip()
                raw_pred = (rel.get("predicate") or "MENTIONED_IN").strip()
                status = rel.get("status") or "active"
                if not subj or not obj:
                    continue
                # 🔧 硬卡 schema：先拒中文 / 拒收词 → 再大写查白名单
                pred = _sanitize_predicate(raw_pred, ALLOWED_REL, DENIED_WORDS)
                # 这条关系自己的时间字段（若 LLM 未抽，用 KO 顶层）
                r_et = rel.get("event_time") if isinstance(rel.get("event_time"), dict) else et
                r_vt = rel.get("valid_time") if isinstance(rel.get("valid_time"), dict) else vt
                r_et_start = r_et.get("start") if isinstance(r_et, dict) else None
                r_et_end = r_et.get("end") if isinstance(r_et, dict) else None
                r_et_expr = (r_et.get("expression") if isinstance(r_et, dict) else None) or ""
                r_et_prec = (r_et.get("precision") if isinstance(r_et, dict) else None) or "unknown"
                r_vt_start = r_vt.get("start") if isinstance(r_vt, dict) else None
                r_vt_end = r_vt.get("end") if isinstance(r_vt, dict) else None
                r_vt_end_type = (r_vt.get("end_type") if isinstance(r_vt, dict) else None) or "open"
                # 一条 cypher 搞定：MATCH 端点 → OPTIONAL MATCH 旧关系 → 命中就更新，否则创建
                cypher = f"""
                MATCH (a {{name: $subj}})
                MATCH (b {{name: $obj}})
                OPTIONAL MATCH (a)-[old_r:{pred}]->(b)
                WITH a, b, old_r
                FOREACH (_ IN CASE WHEN old_r IS NULL THEN [1] ELSE [] END |
                    CREATE (a)-[new_r:{pred}]->(b)
                    SET new_r.status = $status,
                        new_r.source = $source,
                        new_r.ko_summary = $summary,
                        new_r.created = $ts,
                        new_r.updated = $ts,
                        new_r.event_time_start = $et_start,
                        new_r.event_time_end = $et_end,
                        new_r.event_time_expression = $et_expr,
                        new_r.event_time_precision = $et_prec,
                        new_r.valid_time_start = $vt_start,
                        new_r.valid_time_end = $vt_end,
                        new_r.valid_time_end_type = $vt_end_type,
                        new_r.recorded_at = $rec_at,
                        new_r.source_time = $src_at
                )
                FOREACH (_ IN CASE WHEN old_r IS NOT NULL THEN [1] ELSE [] END |
                    SET old_r.status = $status,
                        old_r.source = $source,
                        old_r.ko_summary = $summary,
                        old_r.updated = $ts,
                        old_r.event_time_start = $et_start,
                        old_r.event_time_end = $et_end,
                        old_r.event_time_expression = $et_expr,
                        old_r.event_time_precision = $et_prec,
                        old_r.valid_time_start = $vt_start,
                        old_r.valid_time_end = $vt_end,
                        old_r.valid_time_end_type = $vt_end_type,
                        old_r.recorded_at = $rec_at,
                        old_r.source_time = $src_at
                )
                """
                session.run(
                    cypher,
                    subj=subj, obj=obj,
                    status=status, source=ko.get("source", ""),
                    summary=ko.get("summary", ""),
                    ts=_now_cn(),
                    et_start=r_et_start, et_end=r_et_end,
                    et_expr=r_et_expr, et_prec=r_et_prec,
                    vt_start=r_vt_start, vt_end=r_vt_end,
                    vt_end_type=r_vt_end_type,
                    rec_at=rec_at, src_at=src_at,
                )
                written["relations"] += 1
    finally:
        driver.close()
    return written


def _sanitize_predicate(raw, allowed, denied_words):
    """谓词规范化：拒中文 / 拒收词 → 大写查白名单 → 不在降 MENTIONED_IN。
    只接受 ASCII 大写蛇形命名（LUKE_CASE）。中文 / 空格 / 含中文的词一律降级。
    """
    pred = (raw or "").strip()
    if not pred:
        return "MENTIONED_IN"
    # 拒中文 / 拒收词原形
    if pred in denied_words:
        return "MENTIONED_IN"
    # 含中文 / 空格 / 包含系词字符 → 拒
    if not pred.isascii() or " " in pred:
        return "MENTIONED_IN"
    # 大写查白名单
    pred = pred.upper()
    if pred in allowed:
        return pred
    return "MENTIONED_IN"



# ============================================================
# 写入决策 v5（Mem0 风格：KG + ANN + LLM 三态决策）
# ============================================================
# 开发者 2026-08-10：方案 5
# 三态：CREATE / UPDATE / OVERRIDE / SKIP
# 失败兜底：LLM 调用失败 → 默认 CREATE（不阻塞写入）
# 决策落盘：memory-os/logs/write-decision.md
# 所有可调参数都从 RecallConfig 读，方便改
# ============================================================

def _neo4j_session():
    """返回 Neo4j driver session（用于软删旧关系等细粒度操作）。"""
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USER, NEO4J_PASSWORD),
        # 🔧 2026-08-10 修复：老数据缺 event_time_start 等属性，
        # Neo4j 5+ 对不存在的 property key 发 notification → 每次召回刷几十条 warning。
        # 查询端已用 coalesce 兜底，notification 纯噪音，关掉。
        notifications_min_severity="OFF",
    )
    return driver, driver.session()


def _gen_pid_v5(ko, collection=None):
    """pid 按事实指纹生成（同一事实 → 同 pid，自动去重）。
    🔧 2026-08-11 修复：去掉 collection 名！
    原来 pid = md5("qdrant-v5|memory_fact|指纹")，同事实换个 type（fact→preference）
    就生成不同 PID → 跨库重复的结构性根源。
    现在 PID 只由 entities+relations 决定：同事实不管存哪个 collection 都是同 PID，
    配合 _execute_create_v5 的跨库 PID 检查即可物理去重。
    collection 参数保留兼容旧调用（忽略）。
    """
    fact_parts = sorted(
        (e.get("name") or "").strip()
        for e in (ko.get("entities") or [])
        if (e.get("name") or "").strip()
    )
    rel_parts = sorted(
        f"{(r.get('subject') or '').strip()}|{r.get('predicate') or ''}|{(r.get('object') or '').strip()}"
        for r in (ko.get("relations") or [])
        if (r.get("subject") or "").strip() and (r.get("object") or "").strip()
    )
    fp = "::".join(fact_parts) + "||" + "::".join(rel_parts)
    # 🔧 2026-08-11：v6 前缀，和旧 v5 PID 区分（旧 PID 含 collection，语义不同）
    pid_src = ("qdrant-v6|" + fp).encode("utf-8")
    return int(hashlib.md5(pid_src).hexdigest()[:16], 16)


def _build_payload_v5(ko, kotype, imp, pid=None):
    """Qdrant payload 统一构造。"""
    return {
        "summary": ko.get("summary", ""),
        "memory_type": kotype,
        "entities": [e.get("name", "") for e in (ko.get("entities") or [])],
        "tags": ko.get("tags") or [],
        "importance": imp,
        "source": ko.get("source", ""),
        "evidence": ko.get("evidence", ""),
        "relations": ko.get("relations") or [],
        "ts": _now_cn(),
        "event_time": ko.get("event_time") or {},
        "valid_time": ko.get("valid_time") or {},
        "recorded_at": ko.get("recorded_at") or "",
        "source_time": ko.get("source_time") or "",
        "_fact_fingerprint_v5": pid,   # 用于 PID 碰撞检测
    }


def _ann_find_candidates(client, collection, ko, top_k=None):
    """ANN 召回 top-K 候选（用 WRITE_ANN_RECALL_THRESHOLD，召回宽让 LLM 决策严）。
    🔧 2026-08-10 修复：跨全部 collection 查。原来只查当前 collection，
    同一事实换 type（fact→preference）就检测不到重复 → 库里堆满跨类型重复。
    现在查所有 collection 合并候选，按 score 降序取 top_k。
    """
    if top_k is None:
        top_k = RecallConfig.WRITE_DECISION_TOP_K
    if client is None:
        return []
    try:
        text, _ = build_qdrant_text(ko)
        if not text.strip():
            return []
        vecs = embed(text)
        if not vecs:
            return []
        vec = vecs[0] if isinstance(vecs[0], list) else vecs
        out = []
        # 跨全部 collection 查（跳过目标 collection 本身会漏，全查最稳）
        for coll in RecallConfig.COLLECTIONS:
            try:
                res = client.query_points(
                    collection_name=coll,
                    query=vec,
                    limit=top_k,
                    score_threshold=RecallConfig.WRITE_ANN_RECALL_THRESHOLD,
                )
                for hit in res.points:
                    out.append({
                        "pid": hit.id,
                        "collection": coll,
                        "summary": (hit.payload.get("summary") or "")[:200],
                        "score": float(hit.score),
                        "memory_type": hit.payload.get("memory_type", ""),
                    })
            except Exception:
                continue  # collection 不存在或查询失败就跳过
        # 按 score 降序，取 top_k
        out.sort(key=lambda x: -x["score"])
        return out[:top_k]
    except Exception as e:
        print(f"[warn] ann_find_candidates failed: {e}", file=sys.stderr)
        return []


def _rule_decide_action(ko, candidates):
    """规则决策（无 LLM）：基于 ANN 分数 + KO.state 字段判断操作类型。
    - 无候选 → CREATE
    - 新 KO state=uncertain → INVALIDATE（有疑虑，不新建）
    - 新 KO state=historical + 有候选 → UPDATE（旧状态标记历史，新写当前）
    - 最高分 >= DEDUP_THRESHOLD → SKIP（几乎一样，跳过）
    - 最高分 >= WRITE_ANN_RECALL_THRESHOLD → UPDATE:1（相似，merge 补充信息）
    - 否则 → CREATE
    """
    # 新 KO 的 state 字段判断（来自新 extract_prompt.md）
    state = ko.get("state", "active")

    if not candidates:
        # 无候选：state=uncertain 也不写，其他都 CREATE
        if state == "uncertain":
            return "DISCARD", f"state=uncertain, no candidates, discarded"
        return "CREATE", "no candidates"

    best = max(candidates, key=lambda c: c.get("score", 0))
    score = float(best.get("score", 0))

    # state=uncertain：有候选但新信息不确定，标记旧记录为 uncertain
    if state == "uncertain":
        return "INVALIDATE", f"state=uncertain, score={score:.3f}, invalidate old"

    # state=historical：有候选，说明库里有旧记录，标记旧为历史再写新
    if state == "historical":
        return "UPDATE:1", f"state=historical, score={score:.3f}, update old to historical"

    if score >= RecallConfig.DEDUP_THRESHOLD:
        return "SKIP", f"dup score={score:.3f} >= {RecallConfig.DEDUP_THRESHOLD}"
    if score >= RecallConfig.WRITE_ANN_RECALL_THRESHOLD:
        return "UPDATE:1", f"similar score={score:.3f}, merge supplement"
    return "CREATE", f"new score={score:.3f} < recall threshold"


def _log_decision(action, reason, ko, candidates):
    """决策事件落盘 → write-decision.md。"""
    try:
        log_path = Path(RecallConfig.WRITE_DECISION_LOG_PATH)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = _now_cn()
        summary = ko.get("summary", "")[:120]
        # 🔧 2026-08-10 修复 #6：加文件锁防并发 ingest 交错写行
        import fcntl
        with log_path.open("a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(f"\n### {ts} decision\n")
                f.write(f"- action: {action}\n")
                f.write(f"- reason: {reason}\n")
                f.write(f"- summary: {summary}\n")
                f.write(f"- n_candidates: {len(candidates)}\n")
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"[warn] _log_decision failed: {e}", file=sys.stderr)


def _execute_create_v5(ko, kotype, collection, client, report):
    """CREATE：Neo4j + Qdrant 全量新建。"""
    try:
        w = neo4j_upsert_ko(ko)
        report["neo4j"]["entities"] += w["entities"]
        report["neo4j"]["relations"] += w["relations"]
    except Exception as e:
        print(f"[warn] create neo4j failed: {e}", file=sys.stderr)
        report["neo4j_errors"] += 1

    if client is None:
        return
    try:
        text, imp = build_qdrant_text(ko)
        if not text.strip():
            return
        vecs = embed(text)
        if not vecs:
            report["qdrant_errors"] += 1
            print(f"[warn] [CREATE] embed failed, Qdrant skipped (Neo4j already written) summary={ko.get('summary', '')[:60]}", file=sys.stderr)
            return
        vec = vecs[0] if isinstance(vecs[0], list) else vecs
        pid = _gen_pid_v5(ko)
        payload = _build_payload_v5(ko, kotype, imp, pid)
        # 【修复 F + 2026-08-11 跨库化】CREATE 也做 PID 存在性检查：
        # LLM 说 CREATE，但库里同实体+关系（同 PID）已存在时，
        # 说明是「表述差异大 → ANN 没召回 → LLM 没看到候选」的同一事实。
        # 由于 PID 现在跨库唯一（v6 指纹不含 collection），必须查全部 collection：
        # 同事实可能已存在 memory_fact 里，现在要写 memory_preference → 查出来 → 覆盖去重。
        pid_exists = False
        existing_collection = None
        try:
            for coll in RecallConfig.COLLECTIONS:
                try:
                    existing = client.retrieve(collection_name=coll, ids=[pid])
                except Exception:
                    continue
                if existing:
                    pid_exists = True
                    existing_collection = coll
                    old_fp = (existing[0].payload or {}).get("_fact_fingerprint_v5", "")
                    if old_fp and old_fp != pid:
                        print(f"[warn] [CREATE] PID collision! pid={pid} coll={coll}", file=sys.stderr)
                    break
        except Exception as e:
            print(f"[warn] [CREATE] retrieve failed (pid={pid}): {e}", file=sys.stderr)

        if pid_exists and existing_collection and existing_collection != collection:
            # 同事实已在别的 collection → 覆盖旧坑（保持原 collection），并删掉多余的
            # 新 collection 里的同 PID（如果 ANN 兜底时已在别处写过）
            try:
                client.delete(collection_name=collection, points_selector=[pid])
            except Exception:
                pass
            qdrant_upsert_point(client, existing_collection, pid, vec, payload)
            report["qdrant_updated"] += 1
        else:
            qdrant_upsert_point(client, collection, pid, vec, payload)
            if pid_exists:
                report["qdrant_updated"] += 1
            else:
                report["qdrant_written"] += 1

        # ===== 双重向量：关系单独写一个 point（LightRAG 架构）=====
        rel_text = build_relation_text(ko)
        if rel_text.strip():
            rel_vecs = embed(rel_text)
            if rel_vecs:
                rel_vec = rel_vecs[0] if isinstance(rel_vecs[0], list) else rel_vecs
                import uuid as _uuid
                rel_pid = str(_uuid.uuid4())
                rel_payload = dict(payload)
                rel_payload["_point_type"] = "relation"
                rel_payload["_parent_pid"] = pid
                rel_payload["parent_summary"] = payload.get("summary") or ""
                try:
                    qdrant_upsert_point(client, collection, rel_pid, rel_vec, rel_payload)
                    report["qdrant_written"] += 1
                except Exception as e:
                    print(f"[warn] [CREATE] rel vector write failed: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] create qdrant failed: {e}", file=sys.stderr)
        report["qdrant_errors"] += 1


def _execute_update_v5(ko, kotype, collection, client, target_pid, report):
    """UPDATE：Neo4j 追加 properties + Qdrant 同 pid 重新 upsert。
    🔧 2026-08-11 修复 #5：目标 pid 不存在时降级 CREATE（不凭空建 point）。
    """
    if client is not None:
        # 校验目标 pid 是否真实存在（候选可能已被删/过期）
        try:
            old_pts = client.retrieve(collection_name=collection, ids=[target_pid])
            if not old_pts:
                # 候选不存在 → 降级为 CREATE（新事实）
                print(f"[warn] [UPDATE] target pid {target_pid} not found in {collection}, fallback CREATE", file=sys.stderr)
                report["update"] -= 1
                report["create"] += 1
                _execute_create_v5(ko, kotype, collection, client, report)
                return
        except Exception as e:
            print(f"[warn] [UPDATE] retrieve check failed: {e}", file=sys.stderr)

    try:
        w = neo4j_upsert_ko(ko)
        report["neo4j"]["entities"] += w["entities"]
        report["neo4j"]["relations"] += w["relations"]
    except Exception as e:
        print(f"[warn] update neo4j failed: {e}", file=sys.stderr)
        report["neo4j_errors"] += 1

    if client is None:
        return
    try:
        text, imp = build_qdrant_text(ko)
        if not text.strip():
            return
        vecs = embed(text)
        if not vecs:
            report["qdrant_errors"] += 1
            print(f"[warn] [UPDATE] embed failed, Qdrant skipped (Neo4j already written) summary={ko.get('summary', '')[:60]}", file=sys.stderr)
            return
        vec = vecs[0] if isinstance(vecs[0], list) else vecs
        # 🔧 2026-08-10 修复：pid 可能是 uuid 字符串（老数据），int() 会崩。原样使用。
        pid = target_pid
        payload = _build_payload_v5(ko, kotype, imp, pid)

        # 🔧 2026-08-11 修复 #1：UPDATE 是【合并】不是【覆盖】。
        # 原来直接 upsert，新 summary/entities 覆盖旧 point → 旧信息丢失，
        # LLM prompt 说 "merge properties" 但代码没做。
        # 现在：先 retrieve 旧 payload，entities/tags/evidence 并集合并。
        try:
            old_pts = client.retrieve(collection_name=collection, ids=[pid])
            if old_pts:
                old_pl = old_pts[0].payload or {}
                # entities 并集（保持顺序，去重）
                old_ents = list(old_pl.get("entities") or [])
                new_ents = payload.get("entities") or []
                seen = set(old_ents)
                for e in new_ents:
                    if e not in seen:
                        old_ents.append(e)
                        seen.add(e)
                payload["entities"] = old_ents
                # tags 并集
                old_tags = list(old_pl.get("tags") or [])
                for t in (payload.get("tags") or []):
                    if t not in old_tags:
                        old_tags.append(t)
                payload["tags"] = old_tags
                # 🔧 2026-08-15 修复 #3：summary 也合并，保留旧细节不被新内容覆盖
                # 旧 BUG：直接用新 summary 覆盖旧 → "采桑葚摘桑叶" 被 "养了很多蚕白金色" 覆盖丢失
                old_summary = (old_pl.get("summary") or "").strip()
                new_summary = (payload.get("summary") or "").strip()
                if old_summary and new_summary and old_summary != new_summary:
                    payload["summary"] = f"{old_summary} | {new_summary}"
                # 保留旧的 evidence（补充而非替换）
                old_ev = old_pl.get("evidence") or ""
                new_ev = payload.get("evidence") or ""
                payload["evidence"] = f"{old_ev} | {new_ev}".strip(" |")
                # 记录合并来源
                payload["_merged_from"] = old_pl.get("summary", "")[:100]
        except Exception as e:
            print(f"[warn] [UPDATE] retrieve old payload failed (pid={pid}): {e}", file=sys.stderr)

        payload["updated_from"] = pid
        qdrant_upsert_point(client, collection, pid, vec, payload)
        report["qdrant_updated"] += 1

        # ===== 双重向量：UPDATE 时关系向量也同步更新 =====
        rel_text = build_relation_text(ko)
        if rel_text.strip():
            rel_vecs = embed(rel_text)
            if rel_vecs:
                rel_vec = rel_vecs[0] if isinstance(rel_vecs[0], list) else rel_vecs
                import uuid as _uuid
                rel_pid = str(_uuid.uuid4())
                rel_payload = dict(payload)
                rel_payload["_point_type"] = "relation"
                rel_payload["_parent_pid"] = pid
                rel_payload["parent_summary"] = payload.get("summary") or ""
                try:
                    qdrant_upsert_point(client, collection, rel_pid, rel_vec, rel_payload)
                except Exception as e:
                    print(f"[warn] [UPDATE] rel vector upsert failed: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] update qdrant failed: {e}", file=sys.stderr)
        report["qdrant_errors"] += 1


def _execute_override_v5(ko, kotype, collection, client, target_pid, report):
    """OVERRIDE：Neo4j 软删旧关系 + 创建新关系 + Qdrant payload 全替换。"""
    try:
        driver, session = _neo4j_session()
        with session as s:
            for r in (ko.get("relations") or []):
                subj = (r.get("subject") or "").strip()
                obj = (r.get("object") or "").strip()
                pred = (r.get("predicate") or "MENTIONED_IN").strip().upper()
                if not subj or not obj:
                    continue
                # 【修复 E】谓词白名单校验，防止 LLM 抽出的谓词做 Cypher 注入
                # 只允许 ALLOWED_RELATIONSHIPS 里的关系（recall_config 里拍板的 49 个）
                if pred not in RecallConfig.ALLOWED_RELATIONSHIPS:
                    pred = "MENTIONED_IN"
                s.run(
                    f"""
                    MATCH (a {{name: $subj}})-[old:`{pred}`]->(b {{name: $obj}})
                    WHERE old.status = 'active'
                    SET old.status = 'superseded',
                        old.superseded_at = datetime(),
                        old.superseded_by = $new_summary
                    """,
                    subj=subj, obj=obj,
                    new_summary=ko.get("summary", "")[:200],
                )
        driver.close()
        w = neo4j_upsert_ko(ko)
        report["neo4j"]["entities"] += w["entities"]
        report["neo4j"]["relations"] += w["relations"]
    except Exception as e:
        print(f"[warn] override neo4j failed: {e}", file=sys.stderr)
        report["neo4j_errors"] += 1

    if client is None:
        return
    try:
        text, imp = build_qdrant_text(ko)
        if not text.strip():
            return
        vecs = embed(text)
        if not vecs:
            report["qdrant_errors"] += 1
            print(f"[warn] [OVERRIDE] embed failed, Qdrant skipped (Neo4j already written) summary={ko.get('summary', '')[:60]}", file=sys.stderr)
            return
        vec = vecs[0] if isinstance(vecs[0], list) else vecs
        # 🔧 2026-08-10 修复：pid 可能是 uuid 字符串（老数据），int() 会崩。原样使用。
        pid = target_pid
        payload = _build_payload_v5(ko, kotype, imp, pid)
        # 🔧 2026-08-11 修复 #2：OVERRIDE 记录被覆盖的旧内容（supersedes 链），
        # 原来 supersedes=pid 是自己引用自己，无意义。
        try:
            old_pts = client.retrieve(collection_name=collection, ids=[pid])
            if old_pts:
                old_pl = old_pts[0].payload or {}
                payload["supersedes"] = {
                    "summary": old_pl.get("summary", "")[:200],
                    "ts": old_pl.get("ts", ""),
                }
                # 保留旧 point 的历史（写入独立的历史 point，避免覆盖后旧信息彻底丢失）
                old_pl["superseded_by"] = ko.get("summary", "")[:200]
                old_pl["_superseded"] = True
                hist_pid = str(pid) + "_hist_" + str(abs(hash(ko.get("summary", "")) % 100000))
                try:
                    client.upsert(
                        collection_name=collection,
                        points=[PointStruct(id=hist_pid, vector=vecs[0] if isinstance(vecs[0], list) else vecs, payload=old_pl)],
                    )
                except Exception as e:
                    print(f"[warn] [OVERRIDE] history point write failed: {e}", file=sys.stderr)
        except Exception as e:
            print(f"[warn] [OVERRIDE] retrieve old failed: {e}", file=sys.stderr)
        qdrant_upsert_point(client, collection, pid, vec, payload)
        report["qdrant_overridden"] += 1
    except Exception as e:
        print(f"[warn] override qdrant failed: {e}", file=sys.stderr)
        report["qdrant_errors"] += 1


def write_kos_v5(kos):
    """Mem0 风格写入决策：KG + ANN + LLM 三态决策。"""
    report = {
        "create": 0, "update": 0, "override": 0, "skipped": 0, "errors": 0,
        "neo4j": {"entities": 0, "relations": 0},
        "qdrant_written": 0, "qdrant_updated": 0, "qdrant_overridden": 0,
        "neo4j_errors": 0, "qdrant_errors": 0,
    }
    client = None
    try:
        client = _qdrant_client()
    except Exception as e:
        print(f"[warn] qdrant client init failed: {e}", file=sys.stderr)

    for ko in kos:
        try:
            ko, dropped = clean_ko_for_write(ko)
            if dropped:
                continue
            ko = _normalize_time_fields(ko)
            kotype = ko.get("type") or "fact"
            collection = qdrant_collection_for(kotype)
            if client is not None:
                try:
                    qdrant_ensure_collection(collection)
                except Exception as e:
                    print(f"[warn] ensure collection {collection}: {e}", file=sys.stderr)

            candidates = _ann_find_candidates(client, collection, ko)
            action, reason = _rule_decide_action(ko, candidates)

            if action == "CREATE":
                report["create"] += 1
                _execute_create_v5(ko, kotype, collection, client, report)
            elif action == "SKIP":
                report["skipped"] += 1
            elif action.startswith("UPDATE:"):
                # 🔧 2026-08-10 修复：UPDATE:N 的 N 是候选序号（1-based），
                # 不是 pid！原来 int("1")=1 直接当 pid 用，写到错误的 point。
                # 现在映射回候选的 pid + collection（跨库候选必须写回原 collection）。
                idx = int(action.split(":", 1)[1]) - 1
                if 0 <= idx < len(candidates):
                    cand = candidates[idx]
                    report["update"] += 1
                    _execute_update_v5(ko, kotype, cand.get("collection", collection), client, cand["pid"], report)
                else:
                    report["errors"] += 1
                    print(f"[warn] UPDATE 序号越界: {action} (candidates={len(candidates)})", file=sys.stderr)
            elif action.startswith("OVERRIDE:"):
                idx = int(action.split(":", 1)[1]) - 1
                if 0 <= idx < len(candidates):
                    cand = candidates[idx]
                    report["override"] += 1
                    _execute_override_v5(ko, kotype, cand.get("collection", collection), client, cand["pid"], report)
                else:
                    report["errors"] += 1
                    print(f"[warn] OVERRIDE 序号越界: {action} (candidates={len(candidates)})", file=sys.stderr)
            else:
                report["create"] += 1
                _execute_create_v5(ko, kotype, collection, client, report)

            _log_decision(action, reason, ko, candidates)
        except Exception as e:
            report["errors"] += 1
            print(f"[warn] write_kos_v5 ko failed: {e}", file=sys.stderr)
            _log_decision("ERROR", str(e), ko, [])

    # BM25 索引异步全量重建（不影响本次写库）
    if trigger_bm25_rebuild is not None:
        trigger_bm25_rebuild(async_build=True)

    return report


def ingest_kos_json(kos, source_hint=None):
    """纯写库：吃 KO JSON 数组（agent 已用系统模型抽取好），不碰 LLM。
    兼容两种格式：
      - 新 prompt 输出：{"scene_summary": "...", "kos": [...]}
      - 旧格式直接是：[...]（向后兼容）
    source_hint: 可选来源说明（如 dream:light:2026-08-13），只进日志不进库。
    """
    # 兼容新 prompt 的 scene_summary 包裹格式
    if isinstance(kos, dict) and "kos" in kos:
        scene_summary = kos.get("scene_summary", "")
        kos = kos["kos"]
    else:
        scene_summary = ""

    if not kos:
        return {
            "extracted": 0, "kos": [],
            "write_report": {
                "create": 0, "update": 0, "override": 0, "skipped": 0, "errors": 0,
                "neo4j": {"entities": 0, "relations": 0},
                "qdrant_written": 0, "qdrant_updated": 0, "qdrant_overridden": 0,
                "neo4j_errors": 0, "qdrant_errors": 0,
            },
        }
    write_report = write_kos_v5(kos)
    return {"extracted": len(kos), "scene_summary": scene_summary, "kos": kos, "write_report": write_report}


# ============================================================
# 按 query 召回 → 再操作（update / delete）
# ============================================================

def _find_pid_by_summary(summary, top_k=3):
    """用 summary 文本反向查 Qdrant，找到 point id 和 collection。
    返回第一个匹配（最高分）的 (pid, collection)。"""
    text = summary.strip()
    if not text:
        return None, None
    try:
        vecs = embed(text)
        if not vecs:
            return None, None
        vec = vecs[0] if isinstance(vecs[0], list) else vecs
        client = _qdrant_client()
        best_pid, best_coll, best_score = None, None, 0.0
        for coll in RecallConfig.COLLECTIONS:
            try:
                res = client.query_points(collection_name=coll, query=vec, limit=top_k, score_threshold=0.61)
                for hit in res.points:
                    if hit.score > best_score:
                        best_score = hit.score
                        best_pid = hit.id
                        best_coll = coll
            except Exception:
                continue
        return best_pid, best_coll
    except Exception as e:
        print(f"[warn] _find_pid_by_summary failed: {e}", file=sys.stderr)
        return None, None


def _delete_by_filter(summary, collection=None):
    """直接用 summary 精确匹配删 Qdrant point，同时软删 Neo4j 关联。"""
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    client = _qdrant_client()
    deleted_qdrant = 0
    deleted_neo4j = 0
    target_collections = collection and [collection] or RecallConfig.COLLECTIONS

    for coll in target_collections:
        try:
            result = client.delete(
                collection_name=coll,
                points_selector=Filter(
                    must=[FieldCondition(key='summary', match=MatchValue(value=summary))]
                )
            )
            # DeleteResult 没有直接返回删除数量，尝试从响应里读
            if hasattr(result, 'deleted') and result.deleted:
                deleted_qdrant += result.deleted
        except Exception as e:
            print(f"[warn] delete qdrant {coll} by filter: {e}", file=sys.stderr)

    # Neo4j 软删：按 ko_summary 精确匹配关系，标记 status=deleted
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD), notifications_min_severity="OFF")
        with driver.session() as session:
            res = session.run(
                """MATCH ()-[r]->()
                   WHERE r.ko_summary = $summary
                   SET r.status = 'deleted', r.updated = $ts
                   RETURN count(r) AS cnt""",
                summary=summary, ts=_now_cn()
            )
            deleted_neo4j += res.single().get('cnt', 0) if res.peek() else 0
        driver.close()
    except Exception as e:
        print(f"[warn] delete neo4j: {e}", file=sys.stderr)

    return {'qdrant_deleted': deleted_qdrant, 'neo4j_deleted': deleted_neo4j}


def _delete_by_pids(pids, collection=None):
    """根据 pid 列表删 Qdrant 点，Neo4j 同步软删关联关系。"""
    from qdrant_client.models import PointIdsList
    client = _qdrant_client()
    deleted_qdrant = 0
    deleted_neo4j = 0
    # 同步所有 collection
    for coll in (collection and [collection] or RecallConfig.COLLECTIONS):
        try:
            existing_pids = []
            if pids:
                try:
                    pts = client.retrieve(collection_name=coll, ids=pids, with_payload=True)
                    existing_pids = [p.id for p in pts]
                except Exception:
                    pass
            if existing_pids:
                client.delete(collection_name=coll, points_selector=PointIdsList(points=existing_pids))
                deleted_qdrant += len(existing_pids)
        except Exception as e:
            print(f"[warn] delete qdrant {coll}: {e}", file=sys.stderr)

    # Neo4j 软删：按 ko_summary 精确匹配关系，标记 status=deleted
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD), notifications_min_severity="OFF")
        with driver.session() as session:
            for pid in pids:
                # 关系上的 ko_summary 存的是 summary 原文，靠它定位
                result = session.run(
                    """MATCH ()-[r]->()
                       WHERE r.ko_summary IS NOT NULL
                       SET r.status = 'deleted', r.updated = $ts
                       RETURN count(r) AS cnt""",
                    ts=_now_cn()
                )
                deleted_neo4j += result.single().get("cnt", 0) if result.peek() else 0
        driver.close()
    except Exception as e:
        print(f"[warn] delete neo4j: {e}", file=sys.stderr)

    return {"qdrant_deleted": deleted_qdrant, "neo4j_deleted": deleted_neo4j}


def delete_memories_json(query, top_k=None):
    """精准召回 → 直接用 summary 删 Qdrant + Neo4j 联动。"""
    if top_k is None:
        top_k = 5
    result = recall(query, top_k=top_k)
    memories = result.get("memories", [])

    if not memories:
        return {"found": False, "message": "没有找到匹配的记忆，无需删除", "deleted": {}}

    best = memories[0]
    summary = best.get("summary", "")[:120]
    deleted = _delete_by_filter(summary)
    return {
        "found": True,
        "summary": summary,
        "deleted": deleted,
    }

def update_memories_json(query, kos, top_k=None):
    """召回 → 找到目标记忆 → 用新 KO 内容更新（覆盖式 MERGE）。"""
    if top_k is None:
        top_k = 5
    if not kos:
        return {"found": False, "message": "kos 不能为空", "updated": {}}

    result = recall(query, top_k=top_k)
    memories = result.get("memories", [])

    if not memories:
        return {"found": False, "message": "没有找到匹配的记忆，无法更新", "updated": {}}

    # 取第一条（最高分），靠 summary 重新查 Qdrant 获取 pid
    best = memories[0]
    old_summary = best.get("summary", "")[:120]
    pid, collection = _find_pid_by_summary(old_summary)

    if not pid:
        return {"found": False, "message": "该记忆无法定位到具体 point id，请尝试更精确的 query", "old_summary": old_summary}

    # 用第一条 KO 更新（kos[0]），走 _execute_update_v5 路径
    ko = kos[0]
    # 保留原 recorded_at 不变
    ko["recorded_at"] = best.get("recorded_at", "") or ko.get("recorded_at", "")

    client = _qdrant_client()

    report = {"update": 0, "qdrant_written": 0, "qdrant_updated": 0, "neo4j": {"entities": 0, "relations": 0}}
    _execute_update_v5(ko, ko.get("type", "fact"), collection or RecallConfig.COLLECTIONS[0], client, pid, report)

    return {
        "found": True,
        "updated_pid": str(pid),
        "updated_collection": collection,
        "old_summary": old_summary,
        "new_summary": ko.get("summary", "")[:80],
        "report": report,
    }


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_recall = sub.add_parser("recall")
    p_recall.add_argument("--query", required=True)
    p_recall.add_argument("--top-k", type=int, default=5)
    p_recall.add_argument("--rrf-k", type=int, default=60)
    p_recall.add_argument("--hook", action="store_true", help="走 hook 路径，启用门控")

    p_ingest = sub.add_parser("ingest-kos")
    p_ingest.add_argument("--file", required=True, help="KO JSON 文件（agent 抽取好的数组）")
    p_ingest.add_argument("--source", default=None, help="可选来源说明，仅日志用")

    p_delete = sub.add_parser("delete-memories")
    p_delete.add_argument("--query", required=True, help="用于召回目标记忆的 query")
    p_delete.add_argument("--top-k", type=int, default=5)

    p_update = sub.add_parser("update-memories")
    p_update.add_argument("--query", required=True, help="用于召回目标记忆的 query")
    p_update.add_argument("--file", required=True, help="KO JSON 文件（新内容，agent 抽取好的数组）")
    p_update.add_argument("--top-k", type=int, default=5)

    args = parser.parse_args()

    if args.cmd == "recall":
        fn = recall_for_hook if args.hook else recall
        print(json.dumps(fn(args.query, top_k=args.top_k, rrf_k=args.rrf_k), ensure_ascii=False))
    elif args.cmd == "ingest-kos":
        kos = json.loads(Path(args.file).read_text(encoding="utf-8"))
        print(json.dumps(ingest_kos_json(kos, source_hint=args.source), ensure_ascii=False))
    elif args.cmd == "delete-memories":
        print(json.dumps(delete_memories_json(args.query, top_k=args.top_k), ensure_ascii=False))
    elif args.cmd == "update-memories":
        kos = json.loads(Path(args.file).read_text(encoding="utf-8"))
        print(json.dumps(update_memories_json(args.query, kos, top_k=args.top_k), ensure_ascii=False))


if __name__ == "__main__":
    main()
