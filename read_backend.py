# -*- coding: utf-8 -*-
"""供 jianer-start.bat 调用：打印 config.json 里的 ai_backend 值（local / cloud）。

单独成文件是因为 cmd 内联解析 JSON 不可靠——项目路径含中文，且 config.json 是 UTF-8。
输出仅一行纯 ASCII，交给 bat 做分支判断。
"""
import json
import sys

bot_dir = sys.argv[1] if len(sys.argv) > 1 else "."
try:
    with open(f"{bot_dir}/config.json", "r", encoding="utf-8") as f:
        others = json.load(f).get("Others") or {}
    print(str(others.get("ai_backend", "local")).strip() or "local")
except Exception as e:
    # 读失败就退回 local：宁可多起一次 LM Studio，也不要让 bot 起不来
    print("local", file=sys.stderr)
    print("local")
