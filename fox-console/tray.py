# -*- coding: utf-8 -*-
"""小狐狸控制台 —— 托盘常驻模式（pywin32 实现，无第三方 GUI 依赖）。

做什么：
  * 后台起 Flask 服务（werkzeug make_server，线程里跑），不弹控制台窗口；
  * 在系统托盘放一个狐狸图标：双击打开控制台页面；
  * 右键菜单：打开控制台 / 打开机器人日志 / 重启机器人 / 复制访问地址 / 退出。

用法：
    python tray.py                 # 托盘常驻，端口/目录规则与 server.py 一致
    python tray.py --selftest 5    # 自检：起服务 + 建图标 + 跑 5 秒消息循环后退出

依赖：pywin32（本机 venv 已有）。若导入失败，会退回前台窗口模式，不会静默失败。
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_LOG_FILE = HERE / "logs" / "tray.log"

WM_TRAY = 0x0400 + 20          # WM_USER + 20
IDI_APP = 1
MENU_OPEN, MENU_COPY, MENU_LOGS, MENU_RESTART, MENU_QUIT = 1001, 1002, 1003, 1004, 1005

WND_CLASS = "FoxConsoleTray"
WND_TITLE = "小狐狸控制台"

_httpd = None
_runtime: dict = {}
_hwnd = 0
_menu = None


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [托盘] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def ensure_stdout() -> None:
    """pythonw 启动时 stdout 可能是 None，print 会报错 —— 这时把输出接到日志文件。"""
    if sys.stdout is None or sys.stderr is None:
        try:
            _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            fh = open(_LOG_FILE, "a", encoding="utf-8", buffering=1)
            if sys.stdout is None:
                sys.stdout = fh
            if sys.stderr is None:
                sys.stderr = fh
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 服务线程
# --------------------------------------------------------------------------- #
def start_server(runtime: dict):
    """用 werkzeug 的 make_server 起服务（比 app.run 更好控制生命周期）。"""
    from werkzeug.serving import make_server
    import server as console

    httpd = make_server(runtime["host"], runtime["port"], console.app, threaded=True)
    t = threading.Thread(target=httpd.serve_forever, name="fox-console-http", daemon=True)
    t.start()
    return httpd


# --------------------------------------------------------------------------- #
# 菜单动作
# --------------------------------------------------------------------------- #
def act_open() -> None:
    try:
        webbrowser.open(_runtime.get("url") or "http://127.0.0.1:5010/")
    except Exception as exc:
        log(f"打开浏览器失败：{exc}")


def act_copy() -> None:
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(_runtime.get("url") or "")
        win32clipboard.CloseClipboard()
        log("访问地址已复制到剪贴板")
    except Exception as exc:
        log(f"复制失败：{exc}")


def act_logs() -> None:
    try:
        import fox_core as core
        path = core.LOG_DIR
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))          # noqa: S606 - 打开资源管理器
    except Exception as exc:
        log(f"打开日志目录失败：{exc}")


def act_restart() -> None:
    def _work():
        try:
            import fox_core as core
            st = core.bot_status()
            if not st["running"]:
                log("机器人当前没在运行，跳过重启。")
                return
            res = core.restart_bot()
            log("重启结果：" + str(res.get("note") or res))
        except Exception as exc:
            log(f"重启失败：{exc}")
    threading.Thread(target=_work, daemon=True).start()


def act_quit() -> None:
    global _httpd
    log("正在退出…")
    try:
        if _httpd is not None:
            _httpd.shutdown()
    except Exception:
        pass
    try:
        import win32gui
        win32gui.DestroyWindow(_hwnd)
    except Exception:
        pass
    os._exit(0)


# --------------------------------------------------------------------------- #
# 窗口与托盘
# --------------------------------------------------------------------------- #
def icon_path() -> str:
    p = HERE / "static" / "favicon.ico"
    return str(p if p.exists() else (HERE / "static" / "index.html"))


def build_menu():
    import win32con
    import win32gui
    menu = win32gui.CreatePopupMenu()
    win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_OPEN, "打开控制台")
    win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_COPY, "复制访问地址")
    win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_LOGS, "打开日志目录")
    win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
    win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_RESTART, "重启机器人")
    win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
    win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_QUIT, "退出控制台")
    return menu


def run_tray(selftest_seconds: float = 0.0) -> int:
    global _hwnd, _menu, _httpd
    try:
        import win32api
        import win32con
        import win32gui
    except Exception as exc:
        log(f"pywin32 不可用（{exc}），退回前台窗口模式。")
        import server as console
        console.app.run(host=_runtime["host"], port=_runtime["port"], debug=False, threaded=True)
        return 0

    messages: list = []

    def on_menu(cmd: int) -> None:
        if cmd == MENU_OPEN:
            act_open()
        elif cmd == MENU_COPY:
            act_copy()
        elif cmd == MENU_LOGS:
            act_logs()
        elif cmd == MENU_RESTART:
            act_restart()
        elif cmd == MENU_QUIT:
            act_quit()

    def wnd_proc(hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                if lparam == win32con.WM_LBUTTONDBLCLK:
                    act_open()
                elif lparam in (win32con.WM_RBUTTONUP, win32con.WM_LBUTTONUP):
                    win32gui.SetForegroundWindow(hwnd)
                    pos = win32gui.GetCursorPos()
                    cmd = win32gui.TrackPopupMenu(_menu, win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON,
                                                  pos[0], pos[1], 0, hwnd, None)
                    if cmd:
                        win32gui.PostMessage(hwnd, win32con.WM_COMMAND, cmd, 0)
            elif msg == win32con.WM_COMMAND:
                on_menu(int(wparam) & 0xFFFF)
            elif msg == win32con.WM_DESTROY:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
                win32gui.PostQuitMessage(0)
                return 0
        except Exception as exc:
            messages.append(f"wnd_proc 异常：{exc}")
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    wc = win32gui.WNDCLASS()
    wc.hInstance = win32api.GetModuleHandle(None)
    wc.lpszClassName = WND_CLASS
    wc.lpfnWndProc = wnd_proc
    try:
        class_atom = win32gui.RegisterClass(wc)
    except Exception:
        class_atom = WND_CLASS          # 已注册过就用类名

    _hwnd = win32gui.CreateWindow(class_atom, WND_TITLE, win32con.WS_OVERLAPPED,
                                  0, 0, 0, 0, 0, 0, wc.hInstance, None)
    _menu = build_menu()

    hicon = win32gui.LoadImage(0, icon_path(), win32con.IMAGE_ICON, 0, 0,
                               win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE)
    nid = (_hwnd, IDI_APP, win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
           WM_TRAY, hicon, "小狐狸控制台（双击打开）")
    try:
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
    except Exception as exc:
        raise RuntimeError(
            f"无法把图标放进系统托盘（{exc}）。常见原因：只在沙箱/无桌面会话里运行，"
            f"或安全软件拦了 Shell_NotifyIcon。") from exc
    log(f"托盘图标已就绪：{_runtime['url']}")

    if selftest_seconds > 0:
        log(f"自检模式：{selftest_seconds:.0f} 秒后自动退出")
        t0 = time.time()
        while time.time() - t0 < selftest_seconds:
            win32gui.PumpWaitingMessages()
            time.sleep(0.05)
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (_hwnd, IDI_APP))
        except Exception as exc:
            # 删图标失败不影响使用：进程退出后 Windows 会自动清掉
            log(f"提示：自检结束时删图标失败（{exc}），进程退出后系统会自动清理。")
        if messages:
            for m in messages:
                log(m)
            return 1
        log("自检通过：图标/菜单/消息循环都正常")
        return 0

    win32gui.PumpMessages()
    if messages:
        for m in messages:
            log(m)
        return 1
    return 0


# --------------------------------------------------------------------------- #
def main() -> int:
    ensure_stdout()
    parser = argparse.ArgumentParser(description="小狐狸控制台（托盘常驻）")
    parser.add_argument("--port", type=int, default=int(os.environ.get("FOX_CONSOLE_PORT") or 0))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--bot-dir", default=None)
    parser.add_argument("--no-token", action="store_true")
    parser.add_argument("--token", default=None)
    parser.add_argument("--force-port", action="store_true")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--selftest", type=float, default=0, help="自检 N 秒后自动退出")
    args = parser.parse_args()

    import server as console
    import fox_core as core

    try:
        _runtime.update(console.prepare_runtime(
            host=args.host, port=args.port, bot_dir=args.bot_dir, token=args.token,
            no_token=args.no_token, force_port=args.force_port))
    except core.CoreError as exc:
        print(f"[错误] {exc}")
        return 2

    global _httpd
    _httpd = start_server(_runtime)
    log(f"服务已在后台启动：{_runtime['url']}")

    if args.open or args.selftest:
        threading.Timer(1.0, act_open if args.open else (lambda: None)).start()

    try:
        return run_tray(selftest_seconds=args.selftest)
    except Exception as exc:
        if args.selftest:
            print(f"[托盘] 自检失败：{exc}")
            print("[托盘] 说明：托盘图标需要真实桌面会话；在沙箱/服务会话里会被拒绝。")
            return 3
        traceback.print_exc()
        print(f"[托盘] 图标创建失败（{exc}）")
        print("[托盘] 退回前台窗口模式（按 Ctrl+C 退出）；这样功能都能用，只是没有托盘图标。")
        console.app.run(host=_runtime["host"], port=_runtime["port"], debug=False, threaded=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
