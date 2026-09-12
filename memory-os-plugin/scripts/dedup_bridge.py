"""
dedup_bridge.py
================
写入层去重决策桥接器（位于 scripts/ 目录，供 write_4layer.py 调用）。

当前实现（2026-09-12 老豆最终决定）：
  - 只有 L0 做去重：LLM 纯字面对比（不是向量 ANN，不是语义判断）
  - L1/L2/L3：不做任何对比，直接 CREATE
  - LLM 判断：两段文字是否字面上说的是同一件事（字面相同则 SKIP）
  - LLM 配置从 ~/.openclaw/openclaw.json 动态读取

接口签名
--------
    dedup_decide_layer_action(state, candidates, layer=None, new_text=None, qdrant_client=None)
        -> (action: str, reason: str)
"""

import json
import urllib.request
import urllib.error

from recall_config import RecallConfig


def _load_llm_conf():
    """从 ~/.openclaw/openclaw.json 动态读取当前默认 LLM 配置。"""
    try:
        cfg_path = os.path.expanduser("~/.openclaw/openclaw.json")
        with open(cfg_path) as f:
            config = json.load(f)
        defaults = config.get("agents", {}).get("defaults", {})
        primary = defaults.get("model", {}).get("primary", "minimax/MiniMax-M2.7")
        providers = config.get("models", {}).get("providers", {})
        owner = primary.split("/")[0] if "/" in primary else primary
        for pname, pcfg in providers.items():
            if pname.lower() == owner.lower():
                return {
                    "base_url": pcfg.get("baseUrl", ""),
                    "api_key": pcfg.get("apiKey", ""),
                    "model": primary,
                }
        return None
    except Exception as e:
        print(f"[warn] 读取 LLM 配置失败: {e}", file=sys.stderr)
        return None


_LLM_CONF = None


def _get_llm_conf():
    global _LLM_CONF
    if _LLM_CONF is None:
        _LLM_CONF = _load_llm_conf()
    return _LLM_CONF


def _llm_judge_text_same(new_text, cand_text):
    """调 LLM 判断两段文字是否字面上相同（描述同一件事）。
    返回 True（字面相同）/ False（字面不同）/ None（调用失败）。
    """
    conf = _get_llm_conf()
    if not conf:
        return None

    prompt = (
        "判断以下两段文字是否在字面上描述同一件事。\n"
        "要求：只有当两段文字说的是完全相同的同一件事，才回答「相同」；\n"
        "否则回答「不同」。不要做语义推断，只看字面意思是否相同。\n\n"
        f"A：{new_text}\n\n"
        f"B：{cand_text}\n\n"
        "只回答「相同」或「不同」，不要解释。"
    )
    try:
        req_body = json.dumps({
            "model": conf["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 8,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{conf['base_url']}/chat/completions",
            data=req_body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {conf['api_key']}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            answer = data["choices"][0]["message"]["content"].strip()
        if "相同" in answer:
            return True
        if "不同" in answer:
            return False
        return None
    except Exception as e:
        print(f"[warn] LLM 去重调用失败: {e}", file=sys.stderr)
        return None


def dedup_decide_layer_action(state, candidates, layer=None, new_text=None, qdrant_client=None):
    """
    去重决策（2026-09-12 老豆最终决定）。

    核心原则：
      - 只对比 L0
      - 调用者负责只传 L0 候选
      - L1/L2/L3：不做对比，直接 CREATE

    Returns:
        (action, reason)：
          - CREATE：字面不同，写入
          - SKIP：字面相同，跳过
          - DISCARD：状态异常丢弃
    """
    state = state or "active"

    # L1/L2/L3：不做任何对比，直接 CREATE
    if layer != "L0":
        if state == "uncertain":
            return "DISCARD", "state=uncertain, discarded"
        return "CREATE", f"{layer} 不做去重对比，直接创建"

    # ---- L0 去重：LLM 纯字面对比 ----
    if not new_text:
        return "CREATE", "L0 无文本，新建"

    # 快速路径：字面完全相等直接 SKIP（避免调用 LLM）
    if candidates:
        for cand in candidates:
            cand_text = (cand.get("summary") or "").strip()
            if cand_text and cand_text == new_text:
                return "SKIP", f"L0 字面完全相同，跳过（pid={cand.get('pid')}）"

    # LLM 字面对比
    if candidates:
        for cand in candidates:
            cand_text = (cand.get("summary") or "").strip()
            if not cand_text:
                continue
            result = _llm_judge_text_same(new_text, cand_text)
            if result is True:
                return "SKIP", f"LLM 判 L0 字面相同，跳过（pid={cand.get('pid')}）"
            # result is False or None → 继续下一个候选

    return "CREATE", "L0 字面无重复，新建"
