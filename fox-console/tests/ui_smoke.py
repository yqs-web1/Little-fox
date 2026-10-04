# -*- coding: utf-8 -*-
"""用真实 Chromium 渲染控制台界面：抓 JS 报错、逐页截图、验证交互。

用法（先启动控制台）：
    python tests/ui_smoke.py http://127.0.0.1:5011 <token>

注意：本脚本**只读**，绝不点"保存/确认"类按钮 —— 不会改机器人配置，
也不会动开机自启（只检查开关状态，不切换）。截图输出到 fox-console/screenshots/。
"""
from __future__ import annotations

import pathlib
import sys

from playwright.sync_api import sync_playwright

OUT = pathlib.Path(__file__).resolve().parent.parent / "screenshots"
TABS = [("概览", "overview"), ("AI 模型与密钥", "ai"), ("模型注册表", "models"),
        ("人格与话术", "persona"), ("语音 TTS", "voice"), ("预设与角色", "presets"),
        ("用户与权限", "users"), ("连接与运维", "ops"), ("备份还原", "backup")]


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5011"
    token = sys.argv[2] if len(sys.argv) > 2 else ""
    OUT.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(("  [PASS] " if cond else "  [FAIL] ") + name + ("" if cond else f"  {detail}"))
        if not cond:
            failures.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1460, "height": 1040})
        page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
        page.on("requestfailed", lambda r: errors.append(f"[requestfailed] {r.url}"))
        page.on("dialog", lambda d: d.dismiss())      # 一律不确认，避免任何写操作

        url = f"{base}/?token={token}" if token else f"{base}/"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector("#tabs button", timeout=20000)
        page.wait_for_timeout(3500)

        print("=== 顶栏与概览 ===")
        print(f"  状态: {page.inner_text('#botPill')}")
        cards = page.query_selector_all("#ovCards .card")
        check("概览卡片渲染", len(cards) >= 8, str(len(cards)))
        for c in cards:
            t = c.query_selector("h2")
            b = c.query_selector(".big")
            print(f"    · {t.inner_text() if t else '?'} = {(b.inner_text() if b else '').strip()}")

        audit = page.inner_text("#ovAudit")
        check("字段审计面板有内容（不是加载中）", "加载中" not in audit and len(audit) > 20, audit[:60])
        check("字段审计列出了扫描范围", "扫描范围" in audit)
        print("  审计摘要: " + " | ".join(l.strip() for l in audit.splitlines()[:3]))

        checks = page.query_selector_all("#ovHealth .alert")
        check("配置体检有结果", len(checks) >= 4, str(len(checks)))

        print("\n=== 保存行为 / 常驻自启 ===")
        settings = page.inner_text("#f-console-settings")
        check("保存行为卡片渲染", "自动重启" in settings, settings[:60])
        auto = page.inner_text("#f-autostart")
        check("自启卡片渲染", "开机自动启动" in auto, auto[:60])
        auto_cb = page.query_selector("#f-autostart input[type=checkbox]")
        check("自启开关是关闭状态（未动你的启动项）", auto_cb is not None and not auto_cb.is_checked())

        print("\n=== 逐页渲染与截图 ===")
        for i, (label, key) in enumerate(TABS):
            page.click(f"#tabs button:has-text('{label}')")
            page.wait_for_timeout(900)
            panel = page.query_selector(f'.panel[data-panel="{key}"]')
            visible = panel is not None and panel.is_visible()
            check(f"「{label}」面板能打开", visible)
            path = OUT / f"{i:02d}-{key}.png"
            page.screenshot(path=str(path), full_page=True)
            print(f"    截图 {path.name}")

        print("\n=== 模型注册表内容 ===")
        page.click("#tabs button:has-text('模型注册表')")
        page.wait_for_timeout(900)
        prov_rows = page.query_selector_all("#modelsProviders .listrow")
        reg_rows = page.query_selector_all("#modelsRegistry .listrow")
        check("供应商行已渲染", len(prov_rows) > 0, str(len(prov_rows)))
        check("模型行已渲染", len(reg_rows) > 0, str(len(reg_rows)))
        banner = page.inner_text("#modelsBanner")
        check("注册表顶部给出「代码是否在读」的结论",
              ("已经在读" in banner) or ("还没有" in banner), banner[:80])
        print(f"  供应商 {len(prov_rows)} 行 / 模型 {len(reg_rows)} 行")
        page.screenshot(path=str(OUT / "10-models-detail.png"), full_page=True)

        print("\n=== 交互（只改内存，不写盘） ===")
        page.click("#tabs button:has-text('人格与话术')")
        page.wait_for_timeout(600)
        before = len(page.query_selector_all("#f-persona-compliment .listrow"))
        add = page.query_selector_all("#f-persona-compliment button")[-1]
        add.click()
        page.wait_for_timeout(400)
        after = len(page.query_selector_all("#f-persona-compliment .listrow"))
        check("点「添加一条夸奖语」条目 +1", after == before + 1, f"{before} -> {after}")
        dirty = page.query_selector("#dirtyBadge")
        check("出现「未保存修改」角标", dirty is not None and dirty.is_visible())
        check("其它行为两个新字段已渲染",
              page.query_selector("#f-persona-misc input, #f-persona-misc textarea") is not None)
        page.screenshot(path=str(OUT / "11-persona-dirty.png"), full_page=True)

        page.click("#tabs button:has-text('预设与角色')")
        page.wait_for_timeout(900)
        items = page.query_selector_all("#presetList .item")
        check("预设列表已渲染", len(items) > 0, str(len(items)))
        if items:
            items[0].click()
            page.wait_for_timeout(900)
            n = page.eval_on_selector("#presetContent", "el => el.value.length")
            check("点预设后正文被载入编辑器", n > 50, str(n))
            print(f"  预设正文 {n} 字符")
            page.screenshot(path=str(OUT / "12-preset-editor.png"), full_page=True)

        page.click("#tabs button:has-text('连接与运维')")
        page.wait_for_timeout(1500)
        opts = page.eval_on_selector_all("#logSelect option", "els => els.map(e => e.textContent)")
        print(f"  日志下拉: {opts if opts else '（还没有日志）'}")
        status = page.inner_text("#opsStatus")
        check("运维状态面板有内容", len(status) > 20, status[:40])

        page.click("#tabs button:has-text('语音 TTS')")
        page.wait_for_timeout(900)
        voices = page.query_selector_all("#voiceListBox .voice")
        check("在线声线清单已渲染", len(voices) > 0, str(len(voices)))
        print(f"  在线声线 {len(voices)} 条")

        browser.close()

    print("\n--- 浏览器控制台/网络错误 ---")
    if errors:
        for e in errors:
            print("  " + e)
    else:
        print("  无（没有 JS 报错、没有失败请求）")

    print(f"\n结论：{'全部通过' if not failures and not errors else '有失败项'}"
          f"（失败 {len(failures)} 项，JS 错误 {len(errors)} 条）")
    return 1 if (failures or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
