# -*- coding: utf-8 -*-
"""端到端验证：走机器人真实的调用链（dsr114 -> ai_backend -> 云端 DeepSeek）。

比 test_cloud.py 更严格——它检验的是「代码改得对不对」，而不只是「网络通不通」。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

from Tools import ai_backend
from Tools.deepseek import dsr114

SWITCH = "--cloud" in sys.argv
if SWITCH:
    cfg = Configurator.cm.get_cfg().others
    cfg["ai_backend"] = "cloud"
    print("[test] 已切到 cloud 模式\n")

print(f"[test] 当前后端: {ai_backend.describe()}")
base_url, model, extra, _ = ai_backend.resolve()
print(f"[test] base_url = {base_url or '(官方)'}")
print(f"[test] model    = {model}")
print(f"[test] extra    = {extra}\n")

d = dsr114(
    prompt="你是一个测试助手，请简短回答。",
    message="用一句话介绍你自己，顺便说一句你是什么模型。",
    user_lists={},
    uid="99999",
    mode="deepseek-chat",
    bn="小狐狸",
    key=ai_backend.get_api_key(),
)

print("[test] --- 流式输出 ---")
content, lists = "", None
for partial, rtype in d.Response():
    if rtype == "user_lists":
        lists = partial
        continue
    content += partial
    print(partial, end="", flush=True)

print("\n\n[test] --- 结果 ---")
print(f"内容长度: {len(content)}")
print(f"历史已写回: {lists is not None and '99999' in lists}")
ok = len(content.strip()) > 10 and lists is not None
print(f"结论: {'通过 PASS' if ok else '失败 FAIL'}")
sys.exit(0 if ok else 1)
