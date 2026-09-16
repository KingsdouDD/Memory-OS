#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""融合算法 CLI：MCP 工具 memory_os_fusion 后端。

支持的子命令：
  - list              列出可用算法
  - current           显示当前激活的算法
  - switch <name>     切换到指定算法（不持久化，重启进程失效）
  - reset             重置回环境变量 MEMORY_OS_FUSION_ALGORITHM 指定的算法

输出固定 JSON 格式（status / data / ok / error）。
"""

import sys
import json

sys.path.insert(0, '.')
from recall_fusion import (
    get_fusion,
    set_fusion,
    reset_to_env_default,
    list_algorithms,
)


def main():
    if len(sys.argv) < 2:
        print(json.dumps({
            "ok": False,
            "error": "missing_command",
            "message": "usage: fusion_cli.py <list|current|switch|reset> [name]"
        }, ensure_ascii=False))
        sys.exit(1)

    cmd = sys.argv[1].lower().strip()

    if cmd == "list":
        print(json.dumps({
            "ok": True,
            "data": {
                "algorithms": list_algorithms(),
                "current": get_fusion().name,
            }
        }, ensure_ascii=False))

    elif cmd == "current":
        print(json.dumps({
            "ok": True,
            "data": {
                "current": get_fusion().name,
            }
        }, ensure_ascii=False))

    elif cmd == "switch":
        if len(sys.argv) < 3:
            print(json.dumps({
                "ok": False,
                "error": "missing_name",
                "message": "usage: fusion_cli.py switch <arithmetic|rrf>",
                "available": list_algorithms()
            }, ensure_ascii=False))
            sys.exit(1)
        name = sys.argv[2].lower().strip()
        try:
            inst = set_fusion(name)
            print(json.dumps({
                "ok": True,
                "data": {
                    "switched_to": inst.name,
                    "note": "运行时切换，进程重启后失效；如需持久化请设环境变量 MEMORY_OS_FUSION_ALGORITHM"
                }
            }, ensure_ascii=False))
        except ValueError as e:
            print(json.dumps({
                "ok": False,
                "error": "unknown_algorithm",
                "message": str(e),
                "available": list_algorithms()
            }, ensure_ascii=False))
            sys.exit(1)

    elif cmd == "reset":
        inst = reset_to_env_default()
        print(json.dumps({
            "ok": True,
            "data": {
                "reset_to": inst.name,
                "env_var": "MEMORY_OS_FUSION_ALGORITHM",
            }
        }, ensure_ascii=False))

    else:
        print(json.dumps({
            "ok": False,
            "error": "unknown_command",
            "message": f"unknown command: {cmd}",
            "available_commands": ["list", "current", "switch", "reset"]
        }, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()