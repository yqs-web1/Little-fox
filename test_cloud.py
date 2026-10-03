# -*- coding: utf-8 -*-
"""云端 DeepSeek 连通性测试。

用途：确定中转站正确的 base_url + 模型名组合。脚本会依次尝试若干候选端点，
对每个端点先拉 /models（若支持）再发一条极短的 chat 请求，打印结果。

用法：
    venv\\Scripts\\python.exe test_cloud.py <你的KEY>
    venv\\Scripts\\python.exe test_cloud.py <你的KEY> --only <端点序号>

注意：本脚本会把 key 写进 config.json（供机器人使用），跑完请自行确认 config.json 权限。
"""
from __future__ import annotations

import json
import os
import sys
import httpx

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, "config.json")

# 候选端点：从中转站前端与常见网关命名推出来的，逐个实测
CANDIDATES = [
    "https://d.intern-ai.org.cn/api/v1",
    "https://d.intern-ai.org.cn/v1",
    "https://discovery.intern-ai.org.cn/api/llm/v1",
    "https://discovery.intern-ai.org.cn/api/openai/v1",
    "https://discovery.intern-ai.org.cn/api/serving/v1",
    "https://api.deepseek.com",  # 官方，作为对照
]
MODELS = ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "intern-s2"]


def probe_models(base: str, key: str) -> tuple[int, str]:
    try:
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"},
                      timeout=15.0, proxy=None, trust_env=False)
        return r.status_code, r.text[:200]
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def probe_chat(base: str, key: str, model: str) -> tuple[int, str]:
    body = {"model": model, "messages": [{"role": "user", "content": "说'ok'"}],
            "max_tokens": 8, "stream": False}
    try:
        r = httpx.post(f"{base}/chat/completions",
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                       json=body, timeout=45.0, proxy=None, trust_env=False)
        return r.status_code, r.text[:300]
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    key = sys.argv[1].strip()
    only = None
    if "--only" in sys.argv:
        only = int(sys.argv[sys.argv.index("--only") + 1])

    ok_pair = None
    for idx, base in enumerate(CANDIDATES):
        if only is not None and idx != only:
            continue
        print(f"\n{'=' * 70}\n[{idx}] {base}")
        code, text = probe_models(base, key)
        print(f"  GET  /models          -> {code}")
        if code == 200 and '"data"' in text:
            try:
                ids = [m.get("id") for m in json.loads(text).get("data", [])]
                print(f"       可用模型: {', '.join(str(i) for i in ids[:15])}")
            except Exception:
                print(f"       {text[:150]}")
        elif code != 200:
            print(f"       {text[:150]}")

        for model in MODELS:
            code, text = probe_chat(base, key, model)
            flag = "OK " if code == 200 else "   "
            print(f"  {flag}POST /chat  {model:<24} -> {code}")
            if code == 200:
                try:
                    msg = json.loads(text)["choices"][0]["message"]
                    print(f"       => {str(msg.get('content'))[:80]!r}")
                except Exception:
                    print(f"       {text[:150]}")
                ok_pair = ok_pair or (base, model)
            elif code not in (401, 403):
                print(f"       {text[:150]}")

    print(f"\n{'=' * 70}")
    if ok_pair:
        base, model = ok_pair
        print(f"可用组合: base_url={base}  model={model}")
        print("把上面的值填到 config.json 的 cloud_base_url / cloud_model，")
        print("并把 deepseek_key 设为你的 Key，ai_backend 设为 cloud。")
    else:
        print("没有可用组合。请确认 Key 是否正确/是否有余额，或核对平台文档里的 base_url。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
