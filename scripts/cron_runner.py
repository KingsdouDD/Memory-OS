#!/usr/bin/env python3
"""Memory OS - Cron Runner（新架构）

设计原则：
  - **Agent 抽取 + 脚本写库**：agent 读梦境文件 + extract_prompt.md 抽 KO，
    脚本只负责 spawn agent + 捕获 agent 文本输出（作为 QQ 通知内容）
  - 失败兜底：agent spawn 失败或超时，agent 自己的 error 输出就是通知
  - 路径全从 openclaw.json 读，不硬编码

调用链：
  launchd 3:30
    → cron_runner.py
      → spawn isolated agent
        → 读梦境文件
        → 按 extract_prompt.md 抽 KO（scene_summary + kos 数组）
        → 写临时 KO JSON 文件
        → process_dream.py ingest-kos --file <KO.json>
        → 文本输出（成功/失败描述）
      → agent 文本自动经 announce 发 QQ（delivery=announce 时）
      或 cron_runner.py 捕获 stdout 备用

异常时，落 /tmp/memory-os-cron-fail.log（不静默吞）。
"""

import os
import sys
import json
import argparse
import subprocess
import tempfile
import traceback
from pathlib import Path
from datetime import datetime, timezone, timedelta


# ============================================================
# 路径：动态从 openclaw.json 读，不硬编码
# ============================================================
SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"


def load_openclaw_config() -> dict:
    """读 openclaw.json，支持环境变量 OPENCLAW_CONFIG 覆盖"""
    config_path = Path(os.environ.get("OPENCLAW_CONFIG", DEFAULT_OPENCLAW_CONFIG))
    return json.loads(config_path.read_text(encoding="utf-8"))


def get_workspace() -> Path:
    """从 openclaw.json 读 workspace 路径"""
    cfg = load_openclaw_config()
    ws = (cfg.get("agents") or {}).get("defaults") or {}
    ws_path = ws.get("workspace")
    if ws_path:
        return Path(ws_path)
    return Path.home() / ".openclaw" / "workspace"


def get_openclaw_bin() -> Path:
    """找 openclaw 可执行文件"""
    ws = get_workspace()
    candidates = [
        ws.parent / "bin" / "openclaw",
        Path("/opt/homebrew/bin/openclaw"),
        Path("/usr/local/bin/openclaw"),
        Path("/opt/homebrew/lib/node_modules/openclaw/bin/openclaw.js"),
    ]
    for p in candidates:
        if p.exists():
            return p
    # fallback: 沿 PATH 找
    for d in os.environ.get("PATH", "").split(":"):
        p = Path(d) / "openclaw"
        if p.exists():
            return p
    raise RuntimeError("openclaw 可执行文件找不到")


# 动态路径
WORKSPACE = get_workspace()
OPENCLAW_BIN = get_openclaw_bin()
DREAM_DIR = WORKSPACE / "memory" / "dreaming"
EXTRACT_PROMPT = WORKSPACE / "memory-os-plugin" / "scripts" / "extract_prompt.md"
PROCESS_DREAM = WORKSPACE / "memory-os-plugin" / "scripts" / "process_dream.py"
PYTHON_BIN = WORKSPACE / "memory-os" / "venv" / "bin" / "python3"
FAIL_LOG = "/tmp/memory-os-cron-fail.log"

# 时区
CN_TZ = timezone(timedelta(hours=8))


def _parse_args():
    parser = argparse.ArgumentParser(description="Memory OS Dream Ingestion Cron Runner")
    parser.add_argument("--date", default=None, help="指定日期（YYYY-MM-DD），不传默认当天")
    return parser.parse_args()


def _resolve_target_date(args) -> tuple:
    """返回 (date_str, light_file_path)"，支持命令行覆盖"""
    if args.date:
        date_str = args.date
    else:
        date_str = datetime.now(CN_TZ).strftime("%Y-%m-%d")
    return date_str, DREAM_DIR / "light" / f"{date_str}.md"


CN_TZ = timezone(timedelta(hours=8))


# ============================================================
# Agent Prompt：读梦境 + 抽 KO + 写库
# ============================================================
MEMORY_DREAMING_AGENT_PROMPT = """你是 Memory OS 梦境入库任务。

请按以下步骤执行（全部执行，不跳过）：

## 第一步：读梦境文件
读取文件：{dream_file}
不要做任何修改。

## 第二步：按 extract_prompt.md 抽取 KO
读取 {extract_prompt}，严格按照里面的格式要求抽取 KO。
- 一个场景 = 一条 KO（不是每个候选都抽）
- KO 结构：type / summary（≤80字）/ state / entities（真实人名物名）/ relations（白名单谓词）/ tags / importance（0.0-1.0）/ source / event_time / valid_time
- 只抽有实质内容的事实/经验/事件，不要纯情绪感慨，不要重复库内已有事实
- 如果文件里只有 staged 候选碎片（confidence 标注）或无实质内容，返回空数组

## 第三步：写库
如果抽到了 KO：
1. 把 KO 数组保存到临时文件 /tmp/memory_os_dream_ko.json
2. 执行：{python_bin} {process_dream} ingest-kos --file /tmp/memory_os_dream_ko.json
3. 报告写库结果（create/update/skipped/errors 数量）

如果没抽到 KO：
- 简单报告"无有效内容，跳过"即可

## 输出格式
最终输出一段自然语言简报，格式：
- 有 KO 写入：✅ Memory OS 梦境入库完成（YYYY-MM-DD）\n📁 文件：light/YYYY-MM-DD.md\n🧠 抽取 KO：N\n🔗 Neo4j：X 实体 / Y 关系\n📦 Qdrant：Z 写入 / W 更新 / E 错误
- 无 KO：ℹ️ 梦境 YYYY-MM-DD 无有效内容，跳过。

只输出一段文字，不要 Markdown 格式符号（QQ 显示用）。
"""


def build_agent_prompt(today: str, today_light_file: Path) -> str:
    """构造发给 agent 的完整 prompt"""
    return MEMORY_DREAMING_AGENT_PROMPT.format(
        dream_file=str(today_light_file),
        extract_prompt=str(EXTRACT_PROMPT),
        process_dream=str(PROCESS_DREAM),
        python_bin=str(PYTHON_BIN),
    )


# ============================================================
# 失败落 log
# ============================================================
def log_fail(scope: str, payload: str):
    """任何外部失败都落 log，不静默"""
    try:
        ts = datetime.now(CN_TZ).isoformat()
        with open(FAIL_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n=== {scope} @ {ts} ===\n")
            f.write(payload)
            f.write("\n")
    except Exception:
        pass


# ============================================================
# 主流程
# ============================================================
def main():
    args = _parse_args()
    TODAY, TODAY_LIGHT_FILE = _resolve_target_date(args)

    # 检查梦境文件是否存在
    if not TODAY_LIGHT_FILE.exists():
        msg = f"ℹ️ 梦境 {TODAY} 无 light 文件，跳过。"
        print(msg)
        return

    # 检查必要文件
    missing = []
    for p in [EXTRACT_PROMPT, PROCESS_DREAM, PYTHON_BIN]:
        if not p.exists():
            missing.append(str(p))
    if missing:
        err = RuntimeError(f"必要文件缺失: {missing}")
        log_fail("missing_files", traceback.format_exception(type(err), err, err.__traceback__))
        print(f"❌ {err}")
        return

    prompt = build_agent_prompt(TODAY, TODAY_LIGHT_FILE)

    # 用 openclaw agent --local spawn isolated agent
    # --local 在本机跑，不走 Gateway（cron 场景更稳）
    # stdout = agent 输出的自然语言简报（QQ 通知内容）
    import uuid
    session_key = f"memory-dreaming:{TODAY}:{uuid.uuid4().hex[:8]}"
    proc = subprocess.run(
        [
            str(OPENCLAW_BIN),
            "agent",
            "--local",
            "--session-key", session_key,
            "--message", prompt,
        ],
        capture_output=True,
        text=True,
        timeout=1800,  # 30 分钟超时
    )

    # agent 输出在 stdout；非 0 返回码算失败
    if proc.returncode != 0:
        err_msg = (
            f"❌ Memory OS cron 跑挂了（exit {proc.returncode}）\n"
            f"stderr（截 1000）：\n{proc.stderr[-1000:]}"
        )
        log_fail("agent_failed", err_msg)
        print(err_msg)
        return

    output = proc.stdout.strip()
    if not output:
        output = f"✅ Memory OS 梦境入库完成（{TODAY}）\n（agent 输出为空，见日志）"

    print(output)


if __name__ == "__main__":
    main()
