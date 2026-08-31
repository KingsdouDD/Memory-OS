#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Recall 门槛：判断一段文本是否值得触发召回 / 是否应丢弃。

这是 process_dream.py 之前内联的所有前置门槛常量 + 函数的抽出版本，
专门用于在不动主流程的情况下调整门槛规则。

包含两类判断：
  1. should_skip_recall(text)  → Hook 召回门控
     - 太短/太长
     - 纯语气词白名单
     - 粗言秽语
     - 纯情绪宣泄 / 临时生理（COMPILED_FILLER）
     - 纯命令式
     - 重复字符
     返回 (skip: bool, reason: str)
     skip=True 表示不该走召回流程；reason 是给 hook-trace.md 用的可读标签。

  2. is_discardable(text)  → LLM 抽取之后的 Python 兜底
     - KO summary 太短
     - 命中 SWEAR
     - 命中 FILLER（COMPILED_FILLER，按 .match 从头匹配）
     返回 bool。True 表示该 KO 应丢弃。

所有可调阈值 / 白名单 / 正则都从 recall_config.RecallConfig 读取，
默认值等于原硬编码值，改环境变量即可生效（需重启 gateway 重新 import）（需重启 gateway 重新 import）。
"""

import re
import os
from typing import Tuple

from recall_config import RecallConfig

# DEBUG 日志路径
_DEBUG_LOG = os.environ.get(
    "MEMORY_OS_RECALL_DEBUG_LOG",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "recall-debug.log")
)

def _debug_log(msg: str):
    """追加写 DEBUG 日志，立即 flush。"""
    try:
        os.makedirs(os.path.dirname(_DEBUG_LOG), exist_ok=True)
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
            f.flush()
    except Exception:
        pass

# ============================================================
# 从 RecallConfig 编译出正则（每次 import 时编译一次）
# - 改 env 后需重启 gateway 才会重新编译
# ============================================================

# lowercase 一次，方便 s.lower() in set 比较
HOOK_SKIP_FILLER = {s.lower() for s in RecallConfig.HOOK_SKIP_FILLER}

# KO 抽取后兜底用的 SWEAR 词表
SWEAR_PATTERNS = list(RecallConfig.SWEAR_PATTERNS)
COMPILED_SWEAR = [re.compile(p, re.IGNORECASE) for p in SWEAR_PATTERNS]

# Hook 路径的粗言秽语（比 KO 兜底更宽松）
HOOK_SKIP_SWEAR_EXTRA = list(RecallConfig.HOOK_SKIP_SWEAR)
COMPILED_HOOK_SWEAR = [re.compile(p, re.IGNORECASE) for p in HOOK_SKIP_SWEAR_EXTRA]

# 纯情绪宣泄 / 临时生理（按 .match 从头匹配）
FILLER_PATTERNS = list(RecallConfig.FILLER_PATTERNS)
COMPILED_FILLER = [re.compile(p, re.IGNORECASE) for p in FILLER_PATTERNS]

# 长度阈值
HOOK_MIN_LEN = RecallConfig.HOOK_MIN_LEN
HOOK_MAX_LEN = RecallConfig.HOOK_MAX_LEN


# ============================================================
# jieba 词性闸门（开发者 2026-08-08 加）
# 原理：query 必须含至少 1 个名词，否则跳过召回。
# 3 字 query "看一下" 没有名词 → 跳过。
# ============================================================
try:
    import jieba.posseg as _pseg
    _JIEBA_OK = True
except ImportError:
    _JIEBA_OK = False


def _has_noun(query: str) -> bool:
    """query 里是否有至少 1 个名词（jieba 词性以 n 开头）。
    中英文都认：
      - 中文名词（n, nr, ns, nt, nz, vn）
      - 英文 / 数字 / 下划线拼接词（Memory、openclaw、neo4j）都算实体
    """
    if not _JIEBA_OK:
        # 退化为“英文单词 >= 2 个”启发式
        return len(re.findall(r"[A-Za-z0-9_-]{2,}", query)) >= 1
    for w in _pseg.cut(query):
        flag = w.flag or ""
        # 名词词性族
        if flag.startswith("n"):
            return True
        # 英文 / 数字 / 下划线拼接词（Memory、openclaw、neo4j）都算实体
        if re.match(r"^[A-Za-z][A-Za-z0-9_-]+$", w.word):
            return True
    return False


# ============================================================
# Hook 召回门控（before_prompt_build 时调用）
# ============================================================

def should_skip_recall(text: str) -> Tuple[bool, str]:
    """判断一条用户消息是否值得触发记忆召回。

    返回 (skip, reason)：
      - skip=True, reason="<原因标签>"  → 不走召回，记一条 recall_skipped 日志
      - skip=False, reason=""           → 继续走 vec + graph 通道
    """
    _debug_log(f"[GATE_ENTER] text={text!r} len={len(text)}")

    if not text:
        _debug_log(f"[GATE_SKIP] reason=empty text={text!r}")
        return True, "empty"
    s = text.strip()
    if not s:
        _debug_log(f"[GATE_SKIP] reason=empty_stripped text={text!r}")
        return True, "empty"

    n = len(s)
    if n < HOOK_MIN_LEN:
        _debug_log(f"[GATE_SKIP] reason=too_short len={n} min={HOOK_MIN_LEN} text={text!r}")
        return True, "too_short"
    if n > HOOK_MAX_LEN:
        _debug_log(f"[GATE_SKIP] reason=too_long len={n} max={HOOK_MAX_LEN} text={text!r}")
        return True, "too_long"

    # 纯语气词 / 系统回应
    if s.lower() in HOOK_SKIP_FILLER:
        _debug_log(f"[GATE_SKIP] reason=filler text={text!r}")
        return True, "filler"
    stripped = s.rstrip("!.。?？,，~～ ")
    if stripped.lower() in HOOK_SKIP_FILLER:
        _debug_log(f"[GATE_SKIP] reason=filler_with_punct text={text!r}")
        return True, "filler_with_punct"

    # 粗言秽语
    for p in COMPILED_HOOK_SWEAR:
        if p.search(s):
            _debug_log(f"[GATE_SKIP] reason=swear pattern={p.pattern!r} text={text!r}")
            return True, "swear"

    # 纯情绪宣泄 / 临时生理（仅在 query 完全是这种句式时跳过）
    for p in COMPILED_FILLER:
        if p.match(s):
            _debug_log(f"[GATE_SKIP] reason=emotional_vent pattern={p.pattern!r} text={text!r}")
            return True, "emotional_vent"

    # 纯命令式（动词开头、无中文名词、单词数 < 3）
    chinese_chars = sum(1 for c in s if '\u4e00' <= c <= '\u9fff')
    word_count = len(s.split())
    if chinese_chars == 0 and word_count < 3:
        _debug_log(f"[GATE_SKIP] reason=pure_command chinese_chars={chinese_chars} word_count={word_count} text={text!r}")
        return True, "pure_command"

    # 重复字符（"啊啊啊啊啊"）
    if len(set(s.replace(" ", ""))) <= 2 and n > 6:
        _debug_log(f"[GATE_SKIP] reason=repeated_chars unique_chars={len(set(s.replace(" ", "")))} text={text!r}")
        return True, "repeated_chars"

    # ── 2026-08-21：名词闸门软化 ──
    # 不再硬跳过，只记录。query 即使无名词也可能是有意义的记忆查询，交给召回链路判断。
    has_noun = _has_noun(s)
    _debug_log(f"[GATE_NOUN] text={text!r} has_noun={has_noun}")

    _debug_log(f"[GATE_PASS] text={text!r}")
    return False, ""


# ============================================================
# LLM 抽取 KO 之后的 Python 兜底
# ============================================================

def is_discardable(text: str) -> bool:
    """判断一条 KO summary 是否该丢弃（被 LLM 误抽到的脏数据）。

    返回 True 表示应丢弃。
    """
    if not text:
        return True
    if len(text.strip()) < 4:
        return True
    for p in COMPILED_SWEAR:
        if p.search(text):
            return True
    for p in COMPILED_FILLER:
        if p.match(text.strip()):
            return True
    return False


# ============================================================
# 自检（python3 recall_gate.py 可直接跑）
# ============================================================

if __name__ == "__main__":
    test_cases = [
        ("嗯", "too_short", 1),
        ("好的", "too_short", 2),
        ("嗯，现在真不好玩呀，还是以前小时候好玩，那个时候什么都", "", 27),
        ("继续", "too_short", 2),
        ("你他妈有病吧", "too_short", 6),
        ("hi", "too_short", 2),
        ("今天好累。", "too_short", 5),
        ("查一下 Qdrant 怎么用", "", 12),
        ("啊啊啊啊啊", "too_short", 6),
        ("run", "too_short", 3),
        ("小时候在外婆家玩的那段日子", "", 14),
        ("外婆也不知道在干嘛，这大热天的", "", 17),
        ("想起你和你妈那个辣条的事", "", 12),
        ("好累", "too_short", 2),
    ]
    ok = 0
    fail = 0
    for text, want_reason, want_len_match in test_cases:
        skip, reason = should_skip_recall(text)
        got_len = len(text)
        reason_match = (reason == want_reason)
        mark = "✅" if reason_match else "❌"
        if mark == "✅":
            ok += 1
        else:
            fail += 1
        print(f"{mark} '{text[:40]:<40}' skip={skip} reason='{reason}'  (len={got_len}, want='{want_reason}')")
    print(f"\n{ok} passed, {fail} failed")