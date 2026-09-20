"""
Memory OS MCP Server
=====================

双入口架构中的 MCP 入口（与 src/index.js 平行）。

目的：
- src/index.js 负责 OpenClaw 内的 before_prompt_build 自动注入
- 本文件负责把同一套 Python 脚本暴露为标准 MCP Tools
- 复用 scripts/recall_4layer.py + scripts/write_4layer.py + scripts/process_dream.py
- 不修改任何现有 Python 脚本

适用客户端：
- Codex / Claude Desktop / Dify（通过 MCP client 接入）
- 任何支持 MCP 协议的客户端

启动方式（stdio 模式，最常见）：
  ~/.openclaw/workspace/memory-os/venv/bin/python3 \\
    ~/.openclaw/workspace/memory-os/memory-os-plugin/src/mcp_server.py

启动方式（HTTP/SSE 模式，给 Dify 远程连接）：
  ~/.openclaw/workspace/memory-os/venv/bin/python3 \\
    ~/.openclaw/workspace/memory-os/memory-os-plugin/src/mcp_server.py --transport sse --host 127.0.0.1 --port 8766
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

# mcp 官方 SDK（2.x 用 mcp.server.fastmcp）
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("[memory-os-mcp] 缺少依赖: pip install mcp", file=sys.stderr)
    sys.exit(1)


# ── 路径与配置 ──────────────────────────────────────────────────────────
PLUGIN_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_DIR / "scripts"
PYTHON_BIN = os.environ.get("MEMORY_OS_PYTHON") or str(
    Path.home() / ".openclaw/workspace/memory-os/venv/bin/python3"
)
RECALL_SCRIPT = str(SCRIPTS_DIR / "recall_4layer.py")
WRITE_SCRIPT = str(SCRIPTS_DIR / "write_4layer.py")

DEFAULT_TIMEOUT_S = int(os.environ.get("MEMORY_OS_MCP_TIMEOUT_S", "30"))


def _build_env() -> dict[str, str]:
    """从环境变量构造 Python 子进程环境（与 index.js 的 buildEnv 等价）。"""
    env = {
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }
    # Neo4j
    if v := os.environ.get("MEMORY_OS_NEO4J_URI"):
        env["MEMORY_OS_NEO4J_URI"] = v
    if v := os.environ.get("MEMORY_OS_NEO4J_USER"):
        env["MEMORY_OS_NEO4J_USER"] = v
    if v := os.environ.get("MEMORY_OS_NEO4J_PASSWORD"):
        env["MEMORY_OS_NEO4J_PASSWORD"] = v
    # Qdrant
    if v := os.environ.get("MEMORY_OS_QDRANT_HOST"):
        env["MEMORY_OS_QDRANT_HOST"] = v
    if v := os.environ.get("MEMORY_OS_QDRANT_PORT"):
        env["MEMORY_OS_QDRANT_PORT"] = v
    # Embedding 模型
    if v := os.environ.get("MEMORY_OS_EMBEDDING_MODEL"):
        env["MEMORY_OS_EMBEDDING_MODEL"] = v
    # 去重阈值
    if v := os.environ.get("MEMORY_OS_DEDUP_THRESHOLD"):
        env["MEMORY_OS_DEDUP_THRESHOLD"] = v
    # 融合算法
    if v := os.environ.get("MEMORY_OS_FUSION_ALGORITHM"):
        env["MEMORY_OS_FUSION_ALGORITHM"] = v
    return env


def _run_python(
    script: str,
    args: list[str],
    input_data: Optional[dict | str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    """
    同步调用 Python 脚本（与 index.js 的 runPython 等价）。
    返回 {ok, stdout, stderr, code, payload(parsed JSON if possible)}。

    关键：cwd 必须设成 script 所在目录，让脚本能找到兄弟模块（config/、models/ 路径相对）。
    """
    cmd = [PYTHON_BIN, script, *args]
    stdin_payload = None
    if input_data is not None:
        if isinstance(input_data, (dict, list)):
            stdin_payload = json.dumps(input_data, ensure_ascii=False)
        else:
            stdin_payload = str(input_data)

    script_dir = str(Path(script).parent)

    try:
        proc = subprocess.run(
            cmd,
            input=stdin_payload,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=script_dir,
            env={**os.environ, **_build_env()},
        )
        stdout = proc.stdout
        stderr = proc.stderr
        code = proc.returncode

        # 兼容两种输出格式：
        # 1) recall_4layer.py recall：整个 stdout 就是一块 JSON，直接 parse
        # 2) write_4layer.py ingest/update/delete/confirm：先打 [INFO] 日志再打 JSON，取最后一个非空行
        stdout_stripped = stdout.strip()
        payload = None
        last_line = ""
        # 先试整体解析（覆盖 recall 路径）
        try:
            payload = json.loads(stdout_stripped)
        except json.JSONDecodeError:
            # 退化：取最后一个非空行（write_4layer 路径）
            for line in stdout.splitlines():
                stripped = line.strip()
                if stripped:
                    last_line = stripped
            if last_line:
                try:
                    payload = json.loads(last_line)
                except json.JSONDecodeError:
                    payload = None

        return {
            "ok": code == 0,
            "code": code,
            "stdout": stdout,
            "stderr": stderr,
            "payload": payload,
            "last_line": last_line,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "code": -1,
            "stdout": e.stdout or "",
            "stderr": (e.stderr or "") + f"\n[timeout after {timeout_s}s]",
            "payload": None,
            "timeout": True,
        }
    except Exception as e:
        return {
            "ok": False,
            "code": -1,
            "stdout": "",
            "stderr": f"[spawn error] {e}",
            "payload": None,
        }


# ── MCP Server 定义 ─────────────────────────────────────────────────────
mcp = FastMCP(
    "memory-os",
    instructions=(
        "Memory OS：4 层长期记忆系统（Neo4j + Qdrant + BM25 + Embedding + Reranker）。\n"
        "使用建议：\n"
        "- 涉及用户历史/偏好/事实 → 先调 memory_recall 查记忆\n"
        "- 写入新记忆 → memory_ingest，传 4 层 JSON\n"
        "- 修改/删除 → memory_update / memory_delete（两阶段或快捷模式）\n"
        "- 服务异常 → memory_health 自检"
    ),
)


# ── Tool: memory_recall ─────────────────────────────────────────────────
@mcp.tool(
    name="memory_recall",
    description=(
        "从 Memory OS 长期记忆查询（4 层融合召回 L0/L1/L2/L3）。\n"
        "返回分层结构：persona(L3 画像) / scenario(L2 场景) / atom(L1 事实) / raw(L0 原文)。\n"
        "默认全开 4 层。可用 layers 指定召回顺序（如 'L3,L2'）。"
    ),
)
async def memory_recall(
    query: str,
    top_k: int = 5,
    include_persona: bool = True,
    include_scenario: bool = True,
    layers: str = "",
) -> dict[str, Any]:
    """
    异步包裹：FastMCP 1.30.0 同步 tool 函数会阻塞 event loop 触发 'timeout' 误报。
    用 asyncio.to_thread 把阻塞 subprocess 调用扔到线程池。
    """
    import asyncio

    args = [
        "recall",
        "--query",
        query,
        "--top-k",
        str(int(top_k)),
    ]
    if layers and layers.strip():
        args.extend(["--layers", layers])

    res = await asyncio.to_thread(_run_python, RECALL_SCRIPT, args, None, DEFAULT_TIMEOUT_S)

    if res.get("timeout"):
        return {
            "ok": False,
            "error": "recall_timeout",
            "message": f"Memory OS 召回超时（{DEFAULT_TIMEOUT_S} 秒未返回），embed/reranker 模型可能冷启动中或卡死",
            "suggested_next": "请调用 memory_health 检查 4 个端口，必要时拉起后重试",
        }

    if not res["ok"] and res["payload"] is None:
        return {
            "ok": False,
            "error": "service_unavailable",
            "message": f"Memory OS 召回失败: {res['stderr'][-500:]}",
            "stdout_tail": res["stdout"][-500:],
            "stderr_tail": res["stderr"][-500:],
            "suggested_next": "请调用 memory_health 检查服务状态",
        }

    payload = res["payload"] or {}
    # 检查是否真的召回到了内容
    counts = {
        "persona": len(payload.get("persona") or []),
        "scenario": len(payload.get("scenario") or []),
        "atom": len(payload.get("atom") or []),
        "assoc_candidates": len(payload.get("assoc_candidates") or []),
        "memories": len(payload.get("memories") or []),
    }
    total = sum(counts.values())
    payload["ok"] = True
    payload["empty"] = total == 0
    payload["counts"] = counts
    if total == 0:
        payload["message"] = f"召回完成，但 4 层记忆中没有与 query 相关的内容（共 0 条）"
    return payload


# ── Tool: memory_ingest ─────────────────────────────────────────────────
@mcp.tool(
    name="memory_ingest",
    description=(
        "把一段对话存入 Memory OS 长期记忆（Neo4j + Qdrant，4 层架构 L0/L1/L2/L3）。\n"
        "推荐传 memory_json（4 层 JSON 字符串），避免 MCP 嵌套数组被展平。\n"
        "兼容老格式：传 kos 数组（自动当 L1）。\n"
        "抽取规范见 scripts/extract_prompt.md。"
    ),
)
async def memory_ingest(
    memory_json: Any = None,  # 接受 str / dict，避免 MCP 客户端序列化后变 dict 导致 Pydantic 校验失败
    memory: Optional[dict[str, Any]] = None,
    kos: Optional[list[dict[str, Any]]] = None,
    source: Optional[str] = None,
) -> dict[str, Any]:
    """异步：避免阻塞 asyncio event loop。"""
    import asyncio
    # 解析 payload
    if memory_json is not None:
        if isinstance(memory_json, str):
            try:
                payload = json.loads(memory_json)
            except json.JSONDecodeError as e:
                return {"ok": False, "error": "memory_json 解析失败", "detail": str(e)}
        elif isinstance(memory_json, dict):
            # FastMCP client 有时把 JSON 字符串当 dict 直接传过来
            payload = memory_json
        else:
            return {"ok": False, "error": "memory_json 必须是 string 或 dict", "got_type": str(type(memory_json).__name__)}
    elif memory and isinstance(memory, "dict"):
        payload = memory
    elif kos and len(kos) > 0:
        l0 = (memory or {}).get("l0") or {}
        payload = {
            "l0": {
                "scene_summary": l0.get("scene_summary") or source or "",
                "source": l0.get("source") or source or "",
            },
            "l1": {"kos": kos},
        }
    else:
        return {"ok": False, "error": "memory_json / memory / kos 至少传一个"}

    # 写临时文件传给 write_4layer.py
    import tempfile

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="memory-os-mcp-", delete=False, encoding="utf-8"
    )
    try:
        json.dump(payload, tmp, ensure_ascii=False)
        tmp.flush()
        tmp.close()

        def _do_ingest():
            return _run_python(WRITE_SCRIPT, ["ingest", "--file", tmp.name])

        res = await asyncio.to_thread(_do_ingest)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    if res["payload"] is not None:
        return res["payload"]
    return {
        "ok": res["ok"],
        "raw": res["stdout"][-500:],
        "stderr": res["stderr"][-500:],
    }


# ── Tool: memory_update（两阶段）───────────────────────────────────────
@mcp.tool(
    name="memory_update",
    description=(
        "更新 Memory OS 4 层记忆。两阶段：\n"
        "- confirm=false（默认）：召回候选 + 返回 token\n"
        "- confirm=true：带 token 真更新\n"
        "快捷模式：传 target_pid + target_collection + target_layer 直接更新（跳过 token）。"
    ),
)
async def memory_update(
    query: str,
    memory: Optional[dict[str, Any]] = None,
    kos: Optional[list[dict[str, Any]]] = None,
    top_k: int = 5,
    confirm: bool = False,
    token: Optional[str] = None,
    target_pid: Optional[str] = None,
    target_collection: Optional[str] = None,
    target_layer: Optional[str] = None,
) -> dict[str, Any]:
    """异步：避免阻塞 asyncio event loop。"""
    import asyncio

    # 构造 4 层 payload
    if memory and isinstance(memory, "dict"):
        payload4 = memory
    elif kos and len(kos) > 0:
        payload4 = {"l1": {"kos": kos}}
    else:
        return {"ok": False, "error": "memory 或 kos 至少传一个"}

    import tempfile

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="memory-os-update-", delete=False, encoding="utf-8"
    )
    try:
        json.dump(payload4, tmp, ensure_ascii=False)
        tmp.flush()
        tmp.close()

        # 第二阶段
        if confirm:
            if target_pid and target_collection and target_layer:
                args = [
                    "update",
                    "--target-pid", target_pid,
                    "--target-collection", target_collection,
                    "--target-layer", target_layer,
                    "--file", tmp.name,
                ]
            elif token:
                args = ["confirm", "--token", token, "--file", tmp.name]
            else:
                return {
                    "ok": False,
                    "error": "confirm=true 时必须传 token 或 target_pid+target_collection+target_layer",
                }
        else:
            # 第一阶段：召回候选 + 生成 token
            args = [
                "update",
                "--query", query,
                "--file", tmp.name,
                "--top-k", str(int(top_k)),
            ]

        res = await asyncio.to_thread(_run_python, WRITE_SCRIPT, args, None, DEFAULT_TIMEOUT_S)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    if res["payload"] is not None:
        return res["payload"]
    return {
        "ok": res["ok"],
        "raw": res["stdout"][-500:],
        "stderr": res["stderr"][-500:],
    }


# ── Tool: memory_delete（两阶段）───────────────────────────────────────
@mcp.tool(
    name="memory_delete",
    description=(
        "从 Memory OS 4 层记忆（L0/L1/L2/L3）删除。两阶段：\n"
        "- confirm=false（默认）：召回候选 + 返回 token\n"
        "- confirm=true：带 token 真删\n"
        "快捷模式：传 target_pid + confirm=true，默认走级联追溯删除（一次清干净 L0/L1/L2/L3 + Neo4j 节点）。"
    ),
)
async def memory_delete(
    query: Optional[str] = None,
    top_k: int = 5,
    layer: Optional[str] = None,
    confirm: bool = False,
    token: Optional[str] = None,
    target_pid: Optional[str] = None,
    target_collection: Optional[str] = None,
    target_layer: Optional[str] = None,
    cascade: bool = True,
    selected_pids: Optional[list[str]] = None,
) -> dict[str, Any]:
    """异步：避免阻塞 asyncio event loop。"""
    import asyncio
    # 第二阶段
    if confirm:
        if target_pid and cascade:
            args = ["delete", "--pid", target_pid, "--cascade"]
            res = await asyncio.to_thread(_run_python, WRITE_SCRIPT, args, None, DEFAULT_TIMEOUT_S)
        elif target_pid and target_collection and target_layer:
            args = [
                "delete",
                "--direct-pid", target_pid,
                "--direct-collection", target_collection,
                "--direct-layer", target_layer,
                "--query", query or "",
            ]
            if selected_pids:
                args.extend(["--selected-pids", ",".join(selected_pids)])
            res = await asyncio.to_thread(_run_python, WRITE_SCRIPT, args, None, DEFAULT_TIMEOUT_S)
        elif token:
            args = ["confirm", "--token", token]
            if selected_pids:
                args.extend(["--selected-pids", ",".join(selected_pids)])
            res = await asyncio.to_thread(_run_python, WRITE_SCRIPT, args, None, DEFAULT_TIMEOUT_S)
        else:
            return {
                "ok": False,
                "error": "confirm=true 时必须传 token 或 target_pid",
            }
    else:
        # 第一阶段：召回候选 + 生成 token
        args = ["delete", "--query", query or "", "--top-k", str(int(top_k))]
        if layer:
            args.extend(["--layer", layer])
        res = await asyncio.to_thread(_run_python, WRITE_SCRIPT, args, None, DEFAULT_TIMEOUT_S)

    if res["payload"] is not None:
        return res["payload"]
    return {
        "ok": res["ok"],
        "raw": res["stdout"][-500:],
        "stderr": res["stderr"][-500:],
    }


# ── Tool: memory_health ─────────────────────────────────────────────────
@mcp.tool(
    name="memory_health",
    description=(
        "检查 Memory OS 服务健康状态。\n"
        "默认快速模式（< 2s）：查 4 个端口（Neo4j 7687 / Qdrant 6333 / Embed 8765 / Reranker 8877）。\n"
        "deep=true：跑 11 项完整自检（5-30s）。\n"
        "embed/reranker 端口 up 不等于模型就绪，会额外探测 /health 验证。"
    ),
)
async def memory_health(deep: bool = False) -> dict[str, Any]:
    """
    异步健康检查。FastMCP 1.30.0 不允许同步 tool 阻塞 event loop。
    """
    import asyncio
    import socket

    PORTS = {
        "neo4j": 7687,
        "qdrant": 6333,
        "embed": 8765,
        "reranker": 8877,
    }

    def check_port(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except (OSError, socket.timeout):
            return False

    def probe_model(port: int) -> dict[str, Any]:
        """模拟 index.js 的 probeModelReady。"""
        import urllib.request

        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/health", method="GET"
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return {"ready": resp.status == 200, "status": resp.status}
        except Exception as e:
            return {"ready": False, "error": str(e)}
    import socket

    PORTS = {
        "neo4j": 7687,
        "qdrant": 6333,
        "embed": 8765,
        "reranker": 8877,
    }

    def check_port(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except (OSError, socket.timeout):
            return False

    def probe_model(port: int) -> dict[str, Any]:
        """模拟 index.js 的 probeModelReady。"""
        import urllib.request

        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/health", method="GET"
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return {"ready": resp.status == 200, "status": resp.status}
        except Exception as e:
            return {"ready": False, "error": str(e)}

    port_checks = {}
    model_checks = {}
    for name, port in PORTS.items():
        up = check_port(port)
        port_checks[name] = {"port": port, "up": up}
        if up and name in ("embed", "reranker"):
            model_checks[name] = probe_model(port)

    all_ports_up = all(c["up"] for c in port_checks.values())
    all_models_ready = all(m.get("ready") for m in model_checks.values())
    all_up = all_ports_up and all_models_ready

    result = {
        "mode": "deep" if deep else "fast",
        "timestamp": __import__("datetime").datetime.now().isoformat(),
        "ports": port_checks,
        "models_ready": model_checks,
        "all_up": all_up,
        "summary": "全部就绪" if all_up else "有服务未就绪",
    }

    if deep:
        # 复用 selfCheck（spawn Python 跑完整 11 项）
        script_path = PLUGIN_DIR / "scripts" / "service_lifecycle.py"
        sys.path.insert(0, str(PLUGIN_DIR / "scripts"))
        try:
            # 简化：直接走 subprocess 跑 selfCheck
            res = await asyncio.to_thread(
                _run_python,
                str(PLUGIN_DIR / "scripts" / "service_lifecycle.py"),
                ["selfcheck"],
                None,
                30,
            )
            result["deep_raw"] = res["stdout"][-2000:]
        except Exception as e:
            result["deep_error"] = str(e)

    return result


# ── 入口 ────────────────────────────────────────────────────────────────
def main():
    import argparse

    parser = argparse.ArgumentParser(description="Memory OS MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="传输协议：stdio（默认）或 sse（HTTP/SSE，给远程客户端）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="SSE 模式监听地址")
    parser.add_argument("--port", type=int, default=8766, help="SSE 模式监听端口")
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        # FastMCP 的 sse/streamable-http 在不同版本参数略有差异
        try:
            mcp.run(transport="sse", host=args.host, port=args.port)
        except TypeError:
            # 兼容某些版本的 mcp 用 settings
            mcp.settings.host = args.host
            mcp.settings.port = args.port
            mcp.run(transport="sse")


if __name__ == "__main__":
    main()
