# -*- coding: utf-8 -*-
"""小狐狸控制台 —— Flask 服务端。

启动：
    venv\\Scripts\\python.exe server.py            # 默认 127.0.0.1:5010
    python server.py --port 5010 --no-token       # 关闭鉴权（仅调试用）

安全：
  * 只绑定 127.0.0.1，不接受外部连接；
  * 除 /api/health 外所有接口都要 token（查询串 / Cookie / X-Auth-Token 三选一）；
  * 所有写操作先备份、后原子替换，越界路径一律拒绝（见 fox_core.safe_child）。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import socket
import sys
import threading
import time
import traceback
from functools import wraps

from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

import fox_core as core

COOKIE_NAME = "fox_console_token"
TOKEN: str | None = None
STATIC_DIR = core.CONSOLE_DIR / "static"

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
if hasattr(app, "json"):            # Flask >= 2.2：中文直接输出，不用 \uXXXX
    app.json.ensure_ascii = False
    app.json.sort_keys = False
mimetypes.add_type("audio/mpeg", ".mp3")


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
def _token_ok(candidate: str | None) -> bool:
    return bool(TOKEN) and bool(candidate) and secrets.compare_digest(str(candidate), TOKEN)


def _authorized() -> bool:
    if TOKEN is None:
        return True
    if _token_ok(request.args.get("token")):
        return True
    if _token_ok(request.cookies.get(COOKIE_NAME)):
        return True
    if _token_ok(request.headers.get("X-Auth-Token")):
        return True
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer ") and _token_ok(auth[7:].strip()):
        return True
    return False


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _authorized():
            return jsonify({
                "ok": False,
                "error": {
                    "type": "unauthorized",
                    "message": "未授权：请打开控制台窗口里打印的 http://127.0.0.1:…/?token=… 地址，"
                               "或用请求头 X-Auth-Token 携带 token。",
                },
            }), 401
        return fn(*args, **kwargs)

    return wrapper


@app.after_request
def _issue_cookie(resp: Response) -> Response:
    tok = request.args.get("token")
    if TOKEN and _token_ok(tok):
        resp.set_cookie(COOKIE_NAME, TOKEN, httponly=True, samesite="Lax",
                        max_age=60 * 60 * 24 * 30)
    ct = resp.headers.get("Content-Type", "")
    if ct.startswith("application/json") and "charset" not in ct:
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.errorhandler(core.CoreError)
def _core_error(exc: core.CoreError):
    return jsonify({"ok": False, "error": {"type": "core", "message": str(exc)}}), exc.status


@app.errorhandler(HTTPException)
def _http_error(exc: HTTPException):
    """Flask 自己的 404/405 等不要被下面的兜底改写成 500。"""
    return jsonify({"ok": False,
                    "error": {"type": exc.name, "message": exc.description or exc.name}}), exc.code


@app.errorhandler(Exception)
def _any_error(exc: Exception):
    if isinstance(exc, core.CoreError):
        return _core_error(exc)
    if isinstance(exc, HTTPException):
        return _http_error(exc)
    traceback.print_exc()
    return jsonify({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}), 500


def body() -> dict:
    data = request.get_json(silent=True)
    if data is None:
        raise core.CoreError("请求体必须是 JSON")
    if not isinstance(data, dict):
        raise core.CoreError("请求体必须是 JSON 对象")
    return data


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    if not (STATIC_DIR / "index.html").exists():
        return "static/index.html 缺失", 500
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/preview/<path:name>")
def preview(name: str):
    path = core.preview_file(name)
    return send_file(path, mimetype="audio/mpeg", conditional=True)


# --------------------------------------------------------------------------- #
# 只读接口
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "version": core.CONSOLE_VERSION,
        "bot_dir": str(core.BOT_DIR),
        "console_dir": str(core.CONSOLE_DIR),
        "auth_required": TOKEN is not None,
        "authorized": _authorized(),
    })


@app.get("/api/meta")
@require_auth
def meta():
    return jsonify({
        "ok": True,
        "version": core.CONSOLE_VERSION,
        "bot_dir": str(core.BOT_DIR),
        "config_path": str(core.CONFIG_PATH),
        "preset_dir": str(core.PRESET_DIR),
        "modes": [{"value": k, "label": v} for k, v in core.CHAT_MODES],
        "backends": core.AI_BACKENDS,
        "log_levels": core.LOG_LEVELS,
        "tts_presets": [{"name": n, "id": i, "desc": d} for n, i, d in core.TTS_PRESETS],
        "gender_voices": core.GENDER_VOICES,
        "default_voice": core.DEFAULT_TTS_VOICE,
        "restore_targets": sorted(core.ALLOWED_RESTORE_TARGETS),
    })


@app.get("/api/overview")
@require_auth
def overview():
    return jsonify({"ok": True, "data": core.overview()})


@app.get("/api/config")
@require_auth
def get_config():
    cfg = core.load_config()
    return jsonify({"ok": True, "config": cfg, "health": core.config_health(),
                    "meta": core.file_meta(core.CONFIG_PATH)})


@app.post("/api/config")
@require_auth
def post_config():
    data = body()
    if "config" not in data:
        raise core.CoreError("缺少 config 字段")
    if "restart" in data:
        restart = str(data.get("restart") or "never").lower()
    else:
        st = core.console_settings()
        restart = (st.get("restart_mode") or "auto") if st.get("auto_restart") else "never"
    result = core.save_config(data["config"], data.get("expected_mtime"),
                              bool(data.get("force")), restart=restart)
    return jsonify({"ok": True, **result})


@app.get("/api/settings")
@require_auth
def get_console_settings():
    """控制台自身设置（存在 console.json，不是机器人配置）。"""
    return jsonify({"ok": True, "settings": core.console_settings()})


@app.post("/api/settings")
@require_auth
def post_console_settings():
    data = body()
    return jsonify({"ok": True, "settings": core.save_console_settings(data.get("settings") or data)})


@app.get("/api/autostart")
@require_auth
def get_autostart():
    return jsonify({"ok": True, "data": core.autostart_status()})


@app.post("/api/autostart")
@require_auth
def post_autostart():
    data = body()
    return jsonify({"ok": True, "data": core.set_autostart(bool(data.get("enabled")))})


@app.get("/api/field-audit")
@require_auth
def field_audit():
    """字段审计：代码在用 / 界面能改 / 配置里已有，三份清单对照。"""
    return jsonify({"ok": True, "data": core.field_audit()})


@app.get("/api/presets")
@require_auth
def get_presets():
    return jsonify({"ok": True, "presets": core.load_presets(),
                    "files": core.list_preset_files(),
                    "meta": core.file_meta(core.PRESET_JSON)})


@app.get("/api/presets/<pid>")
@require_auth
def get_preset(pid: str):
    return jsonify({"ok": True, "data": core.preset_detail(pid)})


@app.put("/api/presets/<pid>")
@require_auth
def put_preset(pid: str):
    data = body()
    data.setdefault("id", pid)
    return jsonify(core.save_preset(data))


@app.post("/api/presets")
@require_auth
def post_preset():
    return jsonify(core.save_preset(body()))


@app.delete("/api/presets/<pid>")
@require_auth
def del_preset(pid: str):
    delete_file = str(request.args.get("delete_file", "0")).lower() in ("1", "true", "yes")
    return jsonify(core.delete_preset(pid, delete_file=delete_file))


@app.get("/api/users")
@require_auth
def get_users():
    return jsonify({"ok": True, "data": core.get_users(),
                    "meta": core.file_meta(core.CONFIG_PATH)})


@app.post("/api/users")
@require_auth
def post_users():
    data = body()
    return jsonify(core.save_users(data.get("data") or data, data.get("expected_mtime")))


@app.get("/api/voiceprefs")
@require_auth
def get_voiceprefs():
    return jsonify({"ok": True, "data": core.load_voice_prefs(),
                    "meta": core.file_meta(core.VOICE_PREFS_PATH)})


@app.post("/api/voiceprefs")
@require_auth
def post_voiceprefs():
    return jsonify(core.save_voice_prefs(body().get("data") or body()))


@app.get("/api/voices")
@require_auth
def get_voices():
    force = str(request.args.get("force", "0")).lower() in ("1", "true", "yes")
    return jsonify({"ok": True, "data": core.list_edge_voices(force=force)})


@app.get("/api/backups")
@require_auth
def get_backups():
    return jsonify({"ok": True, "backups": core.list_backups()})


@app.post("/api/backups/restore")
@require_auth
def post_restore():
    data = body()
    return jsonify(core.restore_backup(data.get("name", "")))


@app.get("/api/logs")
@require_auth
def get_logs():
    return jsonify({"ok": True, "logs": core.list_logs()})


@app.get("/api/logs/<path:name>")
@require_auth
def get_log(name: str):
    tail = request.args.get("tail", "300")
    return jsonify({"ok": True, "data": core.read_log(name, tail=int(tail) if str(tail).isdigit() else 300)})


@app.get("/api/bot/status")
@require_auth
def bot_status():
    return jsonify({"ok": True, "data": core.bot_status()})


@app.post("/api/bot/start")
@require_auth
def bot_start():
    mode = str((body().get("mode") or "bot")).lower()
    if mode not in ("bot", "all"):
        raise core.CoreError("mode 只能是 bot 或 all")
    return jsonify(core.start_bot(mode))


@app.post("/api/bot/stop")
@require_auth
def bot_stop():
    force = bool(body().get("force"))
    return jsonify(core.stop_bot(force=force))


@app.post("/api/bot/clear-lock")
@require_auth
def bot_clear_lock():
    data = body()
    return jsonify(core.clear_stale_lock(force=bool(data.get("force"))))


@app.post("/api/bot/restart")
@require_auth
def bot_restart():
    """停止 -> 等真的退出 -> 启动 -> 等锁出现。整体一次调用，避免前端分两步各说各话。"""
    data = body()
    mode = str(data.get("mode") or "bot").lower()
    if mode not in ("bot", "all"):
        raise core.CoreError("mode 只能是 bot 或 all")
    return jsonify(core.restart_bot(mode=mode))


@app.post("/api/bot/tray")
@require_auth
def bot_tray():
    """把控制台切换成"托盘常驻"实例（后台起一份，无窗口）。"""
    cur_port = request.host.split(":")[-1] if ":" in request.host else None
    try:
        cur_port = int(cur_port) if cur_port else None
    except ValueError:
        cur_port = None
    data = body()
    return jsonify(core.start_tray(current_port=cur_port))


@app.post("/api/model/test")
@require_auth
def model_test():
    kind = str(body().get("kind") or "")
    return jsonify({"ok": True, "data": core.test_backend(kind)})


@app.post("/api/shutdown")
@require_auth
def shutdown():
    """优雅关闭控制台（供后台无人值守场景使用）。"""
    data = body()
    if not data.get("confirm"):
        raise core.CoreError("需要 confirm=true 才会关闭控制台")

    def _bye():
        time.sleep(0.4)
        os._exit(0)

    threading.Thread(target=_bye, daemon=True).start()
    return jsonify({"ok": True, "note": "控制台正在关闭，可以直接关掉这个页面了。"})


@app.post("/api/tts/preview")
@require_auth
def tts_preview():
    data = body()
    return jsonify(core.tts_preview(data.get("text", ""), data.get("voice", ""),
                                    data.get("rate", "+0%"), data.get("volume", "+0%"),
                                    data.get("pitch", "+0Hz")))


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def port_in_use(host: str, port: int) -> str | None:
    """探测端口是否已被占用。

    返回描述字符串表示"已被占用"。特别注意 Windows 上 SO_REUSEADDR 允许两个进程
    绑同一端口，重复启动会出现"两个控制台同时应答、页面时而新时而旧"的怪现象，
    所以启动前必须先探一下。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        try:
            s.connect((host, port))
        except OSError:
            return None
        try:
            s.sendall(b"GET /api/health HTTP/1.0\r\nHost: %s\r\n\r\n" % host.encode())
            banner = s.recv(400).decode("utf-8", "replace")
        except OSError:
            banner = ""
    if "fox_console" in banner or '"version"' in banner:
        return "已经有一个小狐狸控制台在这个端口上运行"
    return "该端口被别的程序占用"


def apply_bot_dir(bot_dir: str | None) -> None:
    """把 core 里的所有路径指向指定机器人目录（测试 / 多实例用）。"""
    if not bot_dir:
        return
    core.BOT_DIR = core.Path(bot_dir).resolve()
    core.CONFIG_PATH = core.BOT_DIR / "config.json"
    core.PRESET_DIR = core.BOT_DIR / "prerequisites"
    core.PRESET_JSON = core.PRESET_DIR / "current.json"
    core.VOICE_PREFS_PATH = core.BOT_DIR / "voice_prefs.json"
    core.SUPER_INI = core.BOT_DIR / "Super_User.ini"
    core.MANAGE_INI = core.BOT_DIR / "Manage_User.ini"
    core.LOCK_PATH = core.BOT_DIR / "jianer.lock"
    core.BOT_ENTRY = core.BOT_DIR / "main.py"
    core.VENV_PY = core.BOT_DIR / "venv" / "Scripts" / "python.exe"


def prepare_runtime(host: str = "127.0.0.1", port: int = 0, bot_dir: str | None = None,
                    token: str | None = None, no_token: bool = False,
                    force_port: bool = False, quiet: bool = False) -> dict:
    """定好 token / 端口 / 目录，并把启动横幅打印出来。

    `main()`（前台窗口模式）和 `tray.py`（托盘模式）共用这一份，
    保证两边的端口占用检测、token 复用、路径处理完全一致。
    """
    global TOKEN
    apply_bot_dir(bot_dir)

    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"[警告] 指定了 {host}：控制台能改机器人配置，强烈建议只监听 127.0.0.1。")

    TOKEN = None if no_token else core.ensure_token(token)

    if not port:
        try:
            port = int(core.load_console_json().get("port") or 5010)
        except Exception:
            port = 5010

    note = ""
    busy = port_in_use(host, port)
    if busy and not force_port:
        original = port
        for candidate in range(port + 1, port + 10):
            if not port_in_use(host, candidate):
                port = candidate
                break
        else:
            raise core.CoreError(
                f"无法启动：{busy}（{host}:{original}），{original + 1}~{original + 9} 也都被占用。"
                f"请先关掉那个控制台窗口，或显式指定端口：--port 5050", 409)
        note = f"{host}:{original} {busy}，已自动改用端口 {port}。"
        try:
            data = core.load_console_json()
            data["port"] = port
            core.atomic_write_bytes(core.CONSOLE_JSON, core.dump_json_bytes(data))
        except Exception:
            pass

    url = f"http://{host}:{port}/"
    if TOKEN:
        url += f"?token={TOKEN}"

    if not quiet:
        print("=" * 68)
        print(f"  小狐狸控制台 v{core.CONSOLE_VERSION}")
        print(f"  机器人目录 : {core.BOT_DIR}")
        print(f"  配置文件   : {core.CONFIG_PATH}")
        if note:
            print(f"  [提示] {note}")
        if TOKEN is None:
            print(f"  ⚠ 鉴权已关闭，请勿让本机以外的人访问")
            print(f"  访问地址   : http://{host}:{port}/")
        else:
            print(f"  访问地址   : {url}")
            print(f"  （首次用上面这个带 token 的地址打开，之后浏览器会记住 30 天）")
        print(f"  Ctrl+C 退出（后台/托盘模式可用 POST /api/shutdown 关闭）")
        print("=" * 68)
        sys.stdout.flush()

    return {"host": host, "port": port, "token": TOKEN, "url": url, "note": note}


def main() -> int:
    parser = argparse.ArgumentParser(description="小狐狸控制台")
    parser.add_argument("--port", type=int, default=int(os.environ.get("FOX_CONSOLE_PORT") or 0))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--bot-dir", default=None, help="机器人目录（默认 ../Jianer_Next_QQ_Bot）")
    parser.add_argument("--no-token", action="store_true", help="关闭鉴权（仅本机调试）")
    parser.add_argument("--token", default=None, help="指定 token")
    parser.add_argument("--force-port", action="store_true", help="端口被占用时也强行启动")
    args = parser.parse_args()

    try:
        rt = prepare_runtime(host=args.host, port=args.port, bot_dir=args.bot_dir,
                             token=args.token, no_token=args.no_token,
                             force_port=args.force_port)
    except core.CoreError as exc:
        print(f"[错误] {exc}")
        return 2

    app.run(host=rt["host"], port=rt["port"], debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
