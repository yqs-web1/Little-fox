# -*- coding: utf-8 -*-
"""静态检查：app.js 里用 $('#id') 引用的元素，必须在 index.html 里存在，
或者由 app.js 自己动态创建（i.id = 'xxx' / 模板串里的 id="xxx"）。

没有浏览器时，这是防止"页面一打开就 null.addEventListener 报错"的第一道网。
用法：python tests/check_dom_refs.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
html = (STATIC / "index.html").read_text(encoding="utf-8")
js = (STATIC / "app.js").read_text(encoding="utf-8")

html_ids = set(re.findall(r'id="([^"]+)"', html))
js_created = set(re.findall(r"""\.id\s*=\s*['"]([^'"]+)['"]""", js))
# 模板串里的 id="xxx"
js_created |= set(re.findall(r"""id="([A-Za-z0-9_-]+)\"""", js))
# app.js 显式声明的动态 id 清单
declared = re.search(r"const DYNAMIC_IDS\s*=\s*\[(.*?)\]", js, re.S)
if declared:
    js_created |= set(re.findall(r"""['"]([A-Za-z0-9_-]+)['"]""", declared.group(1)))
else:
    print("[警告] 没找到 DYNAMIC_IDS 声明，动态创建的 id 无法被静态确认")

selectors = set(re.findall(r"""\$\('#([A-Za-z0-9_-]+)'\)""", js))
selectors |= set(re.findall(r"""\$\$\('#([A-Za-z0-9_-]+)'\)""", js))
selectors |= set(re.findall(r"""querySelector\('#([A-Za-z0-9_-]+)'\)""", js))

missing = sorted(s for s in selectors if s not in html_ids and s not in js_created)
unused = sorted(i for i in html_ids
                if i not in selectors
                and not re.search(r"""['"#]""" + re.escape(i) + r"""['"]""", js))

print(f"index.html 里的 id      : {len(html_ids)}")
print(f"app.js 动态创建的 id     : {sorted(js_created) or '无'}")
print(f"app.js 引用的 #id       : {len(selectors)}")
print(f"引用了但找不到定义       : {missing or '无（全部存在）'}")
print(f"HTML 定义但 JS 没用到    : {len(unused)} 个 -> {unused}")

# 反向检查：JS 里出现的 id 若两个文件都没有，就是笔误
suspects = [s for s in selectors if s not in html_ids and s not in js_created]
ok = not suspects
print("\n结论：" + ("引用关系自洽" if ok else f"有 {len(suspects)} 个悬空引用！"))
sys.exit(0 if ok else 1)
