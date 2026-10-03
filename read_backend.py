# -*- coding: utf-8 -*-
"""供 jianer-start.bat 调用：把启动器需要的配置读出来，只输出一行。

输出格式（ASCII，单行）：
    <ai_backend>|<local_model>|<key_ok>

  ai_backend  config.json -> Others.ai_backend（local / cloud），缺省 local
  local_model config.json -> Others.local_model（本地模式要加载的模型名）
  key_ok      1 / 0：能否在环境变量或 config.json 里拿到 API Key

设计要点：
1. 只输出一行、只用 ASCII，交给 bat 用 for /f "tokens=1,2,3 delims=|" 解析 ——
   这样模型名不用再写死在 bat 里（原来 bat 硬编码 google/gemma-3-4b，
   一旦在 config.json 改了 local_model，启动器仍加载旧模型名，AI 回复必失败）。
2. config.json 默认按**本文件所在目录**查找，不再依赖当前工作目录；
   仍然接受第一个参数指定目录（保持向后兼容）。
3. 读不到就退回 local（宁可多起一次 LM Studio，也不要让 bot 起不来）；
   此时 key_ok 输出 1 表示"不确定"，避免误报"没有 Key"。
"""
import json
import os
import sys

DEFAULT_DIR = os.path.dirname(os.path.abspath(__file__))


def read(bot_dir):
    with open(os.path.join(bot_dir, "config.json"), "r", encoding="utf-8") as f:
        others = (json.load(f).get("Others") or {})
    backend = str(others.get("ai_backend", "local")).strip() or "local"
    model = str(others.get("local_model", "google/gemma-3-4b")).strip() or "google/gemma-3-4b"
    key_env = (os.environ.get("JIANER_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    key_cfg = str(others.get("deepseek_key") or "").strip()
    key_ok = "1" if (key_env or key_cfg) else "0"
    return backend, model, key_ok


def main():
    bot_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIR
    try:
        backend, model, key_ok = read(bot_dir)
    except Exception:
        print("local|google/gemma-3-4b|1")
        return 0
    print(f"{backend}|{model}|{key_ok}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
