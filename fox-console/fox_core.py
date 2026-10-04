# -*- coding: utf-8 -*-
"""小狐狸控制台 —— 核心逻辑层（不依赖 Flask，可单独测试）。

设计原则
  1. 只碰"配置类"文件（config.json / prerequisites / *.ini / voice_prefs.json），
     不碰 main.py、Tools/、plugins/ 里的任何代码。
  2. 每次写盘前先备份，写盘用「临时文件 + os.replace」原子替换，
     避免断电/崩溃留下半个 JSON。
  3. 读不懂的文件绝不猜着写：先校验，校验不过就报错返回，保持原样。
  4. 不认识的键一律原样保留（机器人以后新增的配置不会被控制台抹掉）。
  5. 写盘编码固定 UTF-8（无 BOM）+ ensure_ascii=False + LF —— 与 main.py 一致。

所有路径默认指向 ../Jianer_Next_QQ_Bot，可用环境变量 FOX_BOT_DIR 覆盖（测试用）。
"""
from __future__ import annotations

import ctypes
import datetime
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
CONSOLE_DIR = Path(__file__).resolve().parent
DEFAULT_BOT_DIR = CONSOLE_DIR.parent / "Jianer_Next_QQ_Bot"
BOT_DIR = Path(os.environ.get("FOX_BOT_DIR") or DEFAULT_BOT_DIR).resolve()

CONFIG_PATH = BOT_DIR / "config.json"
PRESET_DIR = BOT_DIR / "prerequisites"
PRESET_JSON = PRESET_DIR / "current.json"
VOICE_PREFS_PATH = BOT_DIR / "voice_prefs.json"
SUPER_INI = BOT_DIR / "Super_User.ini"
MANAGE_INI = BOT_DIR / "Manage_User.ini"
LOCK_PATH = BOT_DIR / "jianer.lock"
BOT_ENTRY = BOT_DIR / "main.py"
VENV_PY = BOT_DIR / "venv" / "Scripts" / "python.exe"
START_ALL_BAT = CONSOLE_DIR.parent / "jianer-start.bat"
START_BOT_BAT = BOT_DIR / "start_jianer.bat"

CONSOLE_JSON = Path(os.environ.get("FOX_CONSOLE_JSON") or (CONSOLE_DIR / "console.json")).resolve()
# 备份目录默认在控制台自己的目录下；FOX_BACKUP_DIR 用于测试时隔离（避免污染真实备份）
BACKUP_DIR = Path(os.environ.get("FOX_BACKUP_DIR") or (CONSOLE_DIR / "backups")).resolve()
LOG_DIR = CONSOLE_DIR / "logs"
PREVIEW_DIR = CONSOLE_DIR / "preview"
TTS_WORKER = CONSOLE_DIR / "tts_worker.py"
VOICES_CACHE = CONSOLE_DIR / "voices_cache.json"

CONSOLE_VERSION = "1.0.0"
MAX_BACKUPS_PER_FILE = 40

LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
AI_BACKENDS = ["cloud", "local"]
CHAT_MODES = [
    ("Ds", "DeepSeek（默认）"),
    ("GoogleGemini", "Google Gemini（读图）"),
    ("Net", "ChatGPT-4o-mini"),
    ("GPT-3.5", "ChatGPT-3.5"),
    ("WebSearch", "联网搜索（免 Key）"),
]
TTS_PRESETS = [
    ("小艺", "zh-CN-XiaoyiNeural", "温柔女声（默认）"),
    ("晓晓", "zh-CN-XiaoxiaoNeural", "活泼女声"),
    ("晓萱", "zh-CN-XiaoxuanNeural", "温婉女声"),
    ("云希", "zh-CN-YunxiNeural", "阳光男声"),
    ("云扬", "zh-CN-YunyangNeural", "专业男声"),
    ("云健", "zh-CN-YunjianNeural", "沉稳男声"),
    ("云夏", "zh-CN-YunxiaNeural", "清新男声"),
]
GENDER_VOICES = {"男生": "zh-CN-YunxiNeural", "女生": "zh-CN-XiaoyiNeural"}
DEFAULT_TTS_VOICE = "zh-CN-XiaoyiNeural"

NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


class CoreError(Exception):
    """业务错误：带 HTTP 语义的状态码，server 层直接转成响应。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def read_bytes(path: Path) -> bytes:
    return Path(path).read_bytes()


def decode_utf8(raw: bytes) -> tuple[str | None, str | None]:
    """严格解码 UTF-8。返回 (文本, 错误)。带 BOM 也接受。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        pos = exc.start
        ctx = raw[max(0, pos - 40): pos + 20].decode("utf-8", "replace")
        return None, f"非法 UTF-8 字节（偏移 {pos}，上下文 …{ctx}…）"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


BACKUP_NAME_RE = re.compile(r"^(?P<target>.+)\.\d{8}-\d{6}\..+\.bak$")


def backup_target(name: str) -> str:
    """从备份文件名反推它备份的是哪个文件。

    命名格式：<原名>.<yyyymmdd-HHMMSS>.<原因>[.序号].bak
    —— 原名本身含点（如 config.json），所以不能简单地 split(".")[0]。
    """
    m = BACKUP_NAME_RE.match(str(name or ""))
    if m:
        return m.group("target")
    return re.sub(r"(\.\d+)?\.bak$", "", str(name or ""))


def backup_file(path: Path, reason: str = "save") -> str | None:
    """备份一个已存在的文件到 backups/。返回备份文件名（相对 backups/）。"""
    path = Path(path)
    if not path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{path.name}.{stamp()}.{reason}.bak"
    dst = BACKUP_DIR / name
    i = 1
    while dst.exists():
        dst = BACKUP_DIR / f"{path.name}.{stamp()}.{reason}.{i}.bak"
        i += 1
    shutil.copy2(path, dst)
    _prune_backups(path.name)
    return dst.name


def _prune_backups(original_name: str) -> None:
    try:
        items = sorted(BACKUP_DIR.glob(f"{original_name}.*.bak"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for old in items[MAX_BACKUPS_PER_FILE:]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def load_json_or(path: Path, default: Any) -> Any:
    try:
        raw = read_bytes(path)
    except FileNotFoundError:
        return default
    text, err = decode_utf8(raw)
    if err:
        raise CoreError(f"{Path(path).name} 编码损坏：{err}", 500)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CoreError(f"{Path(path).name} 不是合法 JSON：第 {exc.lineno} 行 {exc.msg}", 500)


def dump_json_bytes(obj: Any, indent: int = 2) -> bytes:
    text = json.dumps(obj, ensure_ascii=False, indent=indent)
    return text.replace("\r\n", "\n").encode("utf-8")


def write_json(path: Path, obj: Any, reason: str = "save", indent: int = 2) -> dict:
    """校验过的对象写盘：备份 -> 原子替换 -> 回读校验。"""
    bak = backup_file(path, reason)
    atomic_write_bytes(path, dump_json_bytes(obj, indent))
    # 回读校验，确保落盘的内容真能解析
    back = load_json_or(path, None)
    if back != obj:
        raise CoreError(f"{Path(path).name} 回读校验失败，已保留备份 {bak}", 500)
    return {
        "backup": bak,
        "path": str(path),
        "size": path.stat().st_size,
        "mtime": path.stat().st_mtime,
    }


def file_meta(path: Path) -> dict:
    p = Path(path)
    try:
        st = p.stat()
        return {"exists": True, "size": st.st_size, "mtime": st.st_mtime,
                "mtime_str": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
    except FileNotFoundError:
        return {"exists": False, "size": 0, "mtime": 0, "mtime_str": ""}


def safe_child(base: Path, name: str) -> Path | None:
    """把 name 限制成 base 目录下的单层文件名，越界返回 None。"""
    if not isinstance(name, str):
        return None
    name = name.strip().replace("\\", "/")
    if not name or "/" in name or name in (".", "..") or ":" in name:
        return None
    if name.startswith("~") or name.startswith("."):
        return None
    base = Path(base).resolve()
    try:
        target = (base / name).resolve()
        if os.path.commonpath([str(base), str(target)]) != str(base):
            return None
    except Exception:
        return None
    return target


def to_int_list(value: Any) -> list:
    """把 ['123', 123, 'abc', '', None] 规整成 [123]。非数字直接丢弃。"""
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    out, seen = [], set()
    for item in value:
        s = str(item).strip()
        if not s or s.lower() in ("none", "null"):
            continue
        if not s.isdigit():
            continue
        n = int(s)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def to_str_list(value: Any, keep_empty: bool = False) -> list:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    out = []
    for item in value:
        s = str(item)
        if s == "" and not keep_empty:
            continue
        out.append(s)
    return out


# --------------------------------------------------------------------------- #
# 配置：读写与校验
# --------------------------------------------------------------------------- #
REQUIRED_TOP = ["owner", "black_list", "silents", "Connection", "Log_level", "protocol", "Others", "uin"]

DEFAULT_CONFIG: dict = {
    "owner": [],
    "black_list": [],
    "silents": [],
    "Connection": {"mode": "FWS", "host": "127.0.0.1", "port": 5004,
                   "listener_host": "127.0.0.1", "listener_port": 5003, "retries": 5, "satori_token": ""},
    "Log_level": "INFO",
    "protocol": "OneBot",
    "Others": {},
    "uin": 0,
}

DEFAULT_TTS = {"voiceColor": DEFAULT_TTS_VOICE, "rate": "+0%", "volume": "+0%", "pitch": "+0Hz"}

_STR_FIELDS = [
    "gemini_key", "gemini_base_url", "gemini_model", "openai_key", "deepseek_key",
    "cloud_base_url", "cloud_model", "local_base_url", "local_model",
    "bot_name", "bot_name_en", "reminder", "slogan", "asr_model", "asr_language",
    "thinking_bubble_text",
]
_BOOL_FIELDS = ["asr_enabled", "private_reply_quote", "private_typing_bubble",
                "quote_only_first_chunk", "cloud_reasoning", "thinking_bubble"]
_INT_FIELDS = ["asr_timeout_seconds", "asr_max_seconds", "thinking_bubble_max"]
_FLOAT_FIELDS = ["thinking_bubble_delay"]
# 数值字段的合理区间（越界就夹住并告警），键 -> (最小, 最大)
_NUM_RANGES = {
    "asr_timeout_seconds": (5, 300),
    "asr_max_seconds": (0, 3600),
    "thinking_bubble_delay": (0.1, 30.0),
    "thinking_bubble_max": (5, 600),
}
_LIST_FIELDS = ["compliment", "poke_rejection_phrases"]
_UID_FIELDS = ["ROOT_User", "Auto_approval"]

TTS_RATE_RE = re.compile(r"^[+-]\d{1,3}%$")
TTS_HZ_RE = re.compile(r"^[+-]\d{1,4}Hz$")


def normalize_config(raw: Any) -> tuple[dict, list]:
    """校验 + 规整配置。返回 (新配置, 警告列表)。不修改入参，未识别的键原样保留。"""
    warns: list[str] = []
    if not isinstance(raw, dict):
        raise CoreError("config 必须是一个 JSON 对象")

    cfg = json.loads(json.dumps(raw, ensure_ascii=False))  # 深拷贝

    for key in REQUIRED_TOP:
        if key not in cfg:
            cfg[key] = json.loads(json.dumps(DEFAULT_CONFIG[key]))
            warns.append(f"缺少顶层键 {key}，已补默认值")

    for key in ("owner", "black_list", "silents"):
        before = cfg[key]
        cfg[key] = to_int_list(before)
        if before != cfg[key]:
            warns.append(f"{key} 已规整为纯数字列表（丢弃了非数字项）")

    conn = cfg.get("Connection")
    if not isinstance(conn, dict):
        raise CoreError("Connection 必须是对象")
    mode = str(conn.get("mode", "FWS")).upper()
    conn["mode"] = mode if mode in ("FWS", "HTTP") else "FWS"
    if conn["mode"] != mode:
        warns.append(f"Connection.mode={mode!r} 不合法，已改为 FWS")
    conn["host"] = str(conn.get("host") or "127.0.0.1").strip()
    conn["listener_host"] = str(conn.get("listener_host") or "127.0.0.1").strip()
    conn["satori_token"] = str(conn.get("satori_token") or "")
    for pk in ("port", "listener_port"):
        try:
            port = int(conn.get(pk, 0) or 0)
        except (TypeError, ValueError):
            port = 0
        if not (0 < port < 65536):
            port = 5004 if pk == "port" else 5003
            warns.append(f"Connection.{pk} 非法，已回到默认 {port}")
        conn[pk] = port
    try:
        retries = int(conn.get("retries", 5))
    except (TypeError, ValueError):
        retries = 5
    conn["retries"] = max(0, min(100, retries))

    lvl = str(cfg.get("Log_level", "INFO")).upper()
    if lvl not in LOG_LEVELS:
        warns.append(f"Log_level={lvl!r} 不在 {LOG_LEVELS}，已改为 INFO")
        lvl = "INFO"
    cfg["Log_level"] = lvl

    proto = str(cfg.get("protocol", "OneBot"))
    if proto not in ("OneBot", "Satori"):
        warns.append(f"protocol={proto!r} 不合法，已改为 OneBot")
        proto = "OneBot"
    cfg["protocol"] = proto

    try:
        cfg["uin"] = int(cfg.get("uin") or 0)
    except (TypeError, ValueError):
        cfg["uin"] = 0

    others = cfg.get("Others")
    if not isinstance(others, dict):
        raise CoreError("Others 必须是对象")

    for f in _STR_FIELDS:
        if f in others and not isinstance(others[f], str):
            others[f] = str(others[f])
            warns.append(f"Others.{f} 已转为字符串")
    for f in _BOOL_FIELDS:
        if f in others and not isinstance(others[f], bool):
            v = str(others[f]).strip().lower()
            others[f] = v in ("1", "true", "yes", "on", "是", "开")
            warns.append(f"Others.{f} 已转为布尔值")
    for f in _INT_FIELDS:
        if f in others:
            try:
                others[f] = int(others[f])
            except (TypeError, ValueError):
                del others[f]
                warns.append(f"Others.{f} 不是整数，已删除该项")
    for f in _FLOAT_FIELDS:
        if f in others:
            try:
                others[f] = float(others[f])
            except (TypeError, ValueError):
                del others[f]
                warns.append(f"Others.{f} 不是数字，已删除该项")
    for f, (lo, hi) in _NUM_RANGES.items():
        if f in others:
            try:
                v = float(others[f])
            except (TypeError, ValueError):
                continue
            clamped = max(lo, min(hi, v))
            if clamped != v:
                warns.append(f"Others.{f}={v:g} 超出合理区间 [{lo:g}, {hi:g}]，已夹到 {clamped:g}")
                others[f] = int(clamped) if f in _INT_FIELDS else clamped
    for f in _LIST_FIELDS:
        if f in others and not isinstance(others[f], list):
            others[f] = to_str_list(others[f])
            warns.append(f"Others.{f} 已从单个值转为列表")
    for f in _UID_FIELDS:
        before = others.get(f)
        if before is not None:
            fixed = [str(x) for x in to_int_list(before)]
            if fixed != before:
                others[f] = fixed
                warns.append(f"Others.{f} 已规整为 QQ 号字符串列表")

    backend = str(others.get("ai_backend", "local")).strip().lower()
    if backend not in AI_BACKENDS:
        warns.append(f"Others.ai_backend={backend!r} 不合法，已改为 local")
        backend = "local"
    others["ai_backend"] = backend

    mode_val = str(others.get("default_mode", "Ds")).strip()
    if mode_val not in [k for k, _ in CHAT_MODES]:
        warns.append(f"Others.default_mode={mode_val!r} 不在可选模式里，已改为 Ds")
        mode_val = "Ds"
    others["default_mode"] = mode_val

    extra = others.get("cloud_extra_headers")
    if extra is not None and not isinstance(extra, dict):
        warns.append("Others.cloud_extra_headers 必须是对象，已清空")
        others["cloud_extra_headers"] = {}
    elif isinstance(extra, dict):
        others["cloud_extra_headers"] = {str(k): str(v) for k, v in extra.items()}

    tts = others.get("TTS")
    if not isinstance(tts, dict):
        others["TTS"] = dict(DEFAULT_TTS)
        warns.append("Others.TTS 缺失或不是对象，已补默认值")
        tts = others["TTS"]
    for k, default in DEFAULT_TTS.items():
        if k not in tts or not isinstance(tts[k], str):
            tts[k] = default
            warns.append(f"TTS.{k} 已回到默认 {default}")
    if not TTS_RATE_RE.match(tts["rate"]):
        tts["rate"] = "+0%"
        warns.append("TTS.rate 格式应为 +0% 这类，已回到 +0%")
    if not TTS_RATE_RE.match(tts["volume"]):
        tts["volume"] = "+0%"
        warns.append("TTS.volume 格式应为 +0% 这类，已回到 +0%")
    if not TTS_HZ_RE.match(tts["pitch"]):
        tts["pitch"] = "+0Hz"
        warns.append("TTS.pitch 格式应为 +0Hz 这类，已回到 +0Hz")

    for f in ("bot_name", "bot_name_en"):
        if not str(others.get(f) or "").strip():
            others[f] = "小狐狸" if f == "bot_name" else "XiaoHuLi"
            warns.append(f"Others.{f} 为空，已补默认值")

    # 模型注册表：位置是 Others.models（Tools/model_registry.py 就是这么读的）。
    # 顶层如果存在一个 models（早期版本的写法），自动搬进去。
    if "models" in cfg and "models" not in others:
        moved = cfg.pop("models")
        others["models"] = moved
        warns.append("检测到顶层 models，已按机器人代码的读取位置搬到 Others.models")
    if "models" in cfg and "models" in others:
        warns.append("顶层 models 与 Others.models 同时存在，已删除顶层那份（以 Others.models 为准）")
        cfg.pop("models")
    if "models" in others:
        others["models"], m_warns = normalize_models(others["models"])
        warns.extend(m_warns)

    return cfg, warns


# --------------------------------------------------------------------------- #
# 模型注册表（需求⑤「多模型切换」的配置结构，见《五项功能设计方案》§5.2）
# --------------------------------------------------------------------------- #
PROVIDER_TYPES = ["openai", "gemini", "anthropic"]


def normalize_models(raw: Any) -> tuple[dict, list]:
    """校验 models 段：providers / registry / default / fallback_chain。

    原则：只规整"本来就有的键"，不臆造新键 —— 这样保存一次和保存两次的
    config.json 逐字节一致（model_registry.py 那边对缺省键都有 .get 兜底）。
    """
    warns: list[str] = []
    if not isinstance(raw, dict):
        raise CoreError("models 必须是对象：{providers:{}, registry:[], default:'', fallback_chain:[]}")

    providers = raw.get("providers")
    if not isinstance(providers, dict):
        warns.append("models.providers 必须是对象，已清空")
        providers = {}
    clean_providers: dict = {}
    for pid, p in providers.items():
        pid = str(pid).strip()
        if not pid:
            continue
        if not isinstance(p, dict):
            warns.append(f"models.providers.{pid} 不是对象，已跳过")
            continue
        entry = json.loads(json.dumps(p, ensure_ascii=False))
        if "type" in entry:
            ptype = str(entry["type"] or "openai").strip().lower()
            if ptype not in PROVIDER_TYPES:
                warns.append(f"provider {pid} 的 type={ptype!r} 不认识，已按 openai 处理")
                ptype = "openai"
            entry["type"] = ptype
        for sk in ("base_url", "key_env"):
            if sk in entry and not isinstance(entry[sk], str):
                entry[sk] = str(entry[sk])
                warns.append(f"provider {pid}.{sk} 已转为字符串")
        if "extra_headers" in entry:
            eh = entry["extra_headers"]
            if not isinstance(eh, dict):
                warns.append(f"provider {pid}.extra_headers 不是对象，已清空")
                entry["extra_headers"] = {}
            else:
                entry["extra_headers"] = {str(k): str(v) for k, v in eh.items()}
        clean_providers[pid] = entry

    registry = raw.get("registry")
    if not isinstance(registry, list):
        warns.append("models.registry 必须是数组，已清空")
        registry = []
    clean_registry: list = []
    seen_ids: set = set()
    for item in registry:
        if not isinstance(item, dict):
            warns.append("registry 里有一项不是对象，已跳过")
            continue
        mid = str(item.get("id") or "").strip()
        if not mid:
            warns.append("有一项 model 缺少 id，已跳过")
            continue
        if mid in seen_ids:
            warns.append(f"模型 id {mid} 重复，后面的已跳过")
            continue
        prov = str(item.get("provider") or "").strip()
        if not prov:
            warns.append(f"模型 {mid} 没写 provider，已跳过")
            continue
        if prov not in clean_providers:
            warns.append(f"模型 {mid} 引用了不存在的 provider {prov!r}，已跳过")
            continue
        seen_ids.add(mid)
        entry = json.loads(json.dumps(item, ensure_ascii=False))
        entry["id"] = mid
        entry["provider"] = prov
        entry["model"] = str(entry.get("model") or "").strip()
        for sk in ("label",):
            if sk in entry and not isinstance(entry[sk], str):
                entry[sk] = str(entry[sk])
        for lk in ("tags", "aliases"):
            if lk in entry:
                if not isinstance(entry[lk], list):
                    warns.append(f"模型 {mid}.{lk} 不是数组，已转换")
                entry[lk] = to_str_list(entry[lk])
        if "reasoning" in entry and not isinstance(entry["reasoning"], bool):
            entry["reasoning"] = bool(entry["reasoning"])
        clean_registry.append(entry)

    default_id = str(raw.get("default") or "").strip()
    if default_id and default_id not in seen_ids:
        warns.append(f"models.default={default_id!r} 不在 registry 里，已改为空")
        default_id = ""
    if not default_id and clean_registry:
        default_id = str(clean_registry[0]["id"])
        warns.append(f"models.default 为空，已取第一个模型 {default_id}")

    chain = raw.get("fallback_chain")
    chain = to_str_list(chain) if isinstance(chain, (list, tuple)) else []
    bad = [x for x in chain if x not in seen_ids]
    if bad:
        warns.append(f"fallback_chain 里的 {'、'.join(bad)} 不在 registry 里，已剔除")
    chain = [x for x in chain if x in seen_ids]

    out = json.loads(json.dumps(raw, ensure_ascii=False))
    out["providers"] = clean_providers
    out["registry"] = clean_registry
    out["default"] = default_id
    out["fallback_chain"] = chain
    return out, warns


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise CoreError(f"找不到 {CONFIG_PATH}", 404)
    return load_json_or(CONFIG_PATH, {})


def save_config(data: Any, expected_mtime: float | None = None, force: bool = False,
                restart: str = "never") -> dict:
    """保存配置。expected_mtime 用于防止覆盖别的窗口/机器人刚写的内容。

    restart: never=不重启 / auto=机器人本来在跑才重启 / force=总是重启（一键生效）。
    """
    if restart not in ("never", "auto", "force"):
        raise CoreError("restart 只能是 never / auto / force")
    if expected_mtime and not force and CONFIG_PATH.exists():
        cur = CONFIG_PATH.stat().st_mtime
        if abs(cur - float(expected_mtime)) > 1.5:
            raise CoreError(
                "配置文件刚刚被其它程序（可能是机器人自己）改过，为避免覆盖已中止。"
                "请先刷新页面再改。", 409)
    cfg, warns = normalize_config(data)
    meta = write_json(CONFIG_PATH, cfg, reason="config")
    result: dict = {"warnings": warns, "meta": meta, "config": cfg}

    if restart in ("auto", "force"):
        status = bot_status()
        if status["running"] or restart == "force":
            try:
                result["restart"] = restart_bot()
            except CoreError as exc:
                result["restart"] = {"ok": False, "note": str(exc)}
        else:
            result["restart"] = {"ok": True, "skipped": True,
                                 "note": "机器人当前没在运行，配置已保存，未执行重启。"}
    return result


def config_health() -> dict:
    """体检：编码 / JSON / 必填键 / 疑似被 '?' 破坏的中文。"""
    report: dict = {"path": str(CONFIG_PATH), "checks": [], "ok": True}
    meta = file_meta(CONFIG_PATH)
    report.update(meta)

    def check(name: str, ok: bool, detail: str = ""):
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            report["ok"] = False

    if not meta["exists"]:
        check("文件存在", False, "config.json 不存在")
        return report

    raw = read_bytes(CONFIG_PATH)
    text, err = decode_utf8(raw)
    check("UTF-8 编码合法", err is None, err or "无非法字节")
    if err:
        return report

    try:
        cfg = json.loads(text)
        check("JSON 可解析", True, f"{len(text)} 字符")
    except json.JSONDecodeError as exc:
        check("JSON 可解析", False, f"第 {exc.lineno} 行：{exc.msg}")
        return report

    missing = [k for k in REQUIRED_TOP if k not in (cfg if isinstance(cfg, dict) else {})]
    check("必填顶层键齐全", not missing, "缺少：" + "、".join(missing) if missing else "8 个必填键都在")

    suspects = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if "?" in line and re.search(r"[\u4e00-\u9fff]", line):
            suspects.append({"line": lineno, "text": line.strip()[:80]})
        if "\ufffd" in line:
            suspects.append({"line": lineno, "text": line.strip()[:80]})
    check("中文未被 '?' 破坏", not suspects,
          "疑似损坏：" + "；".join(f"第 {s['line']} 行" for s in suspects[:5]) if suspects else "未发现可疑替换")
    report["suspects"] = suspects
    return report


# --------------------------------------------------------------------------- #
# 预设（prerequisites/current.json + *.txt）
# --------------------------------------------------------------------------- #
PRESET_ID_RE = re.compile(r"^[0-9A-Za-z_\u4e00-\u9fff-]{1,64}$")


def load_presets() -> dict:
    data = load_json_or(PRESET_JSON, {})
    if not isinstance(data, dict):
        raise CoreError("prerequisites/current.json 顶层必须是对象", 500)
    return data


def _preset_file_path(name: Any) -> Path:
    p = safe_child(PRESET_DIR, str(name or ""))
    if p is None or p.suffix.lower() != ".txt":
        raise CoreError(f"非法的预设文件名：{name!r}（只能是 prerequisites/ 下的 .txt 单层文件名）")
    return p


def preset_detail(pid: str) -> dict:
    presets = load_presets()
    if pid not in presets:
        raise CoreError(f"预设 {pid} 不存在", 404)
    info = presets[pid] if isinstance(presets[pid], dict) else {}
    path = _preset_file_path(info.get("path") or f"{pid}.txt")
    content = ""
    if path.exists():
        raw = read_bytes(path)
        content, err = decode_utf8(raw)
        if err:
            raise CoreError(f"{path.name} 编码损坏：{err}", 500)
    return {"id": pid, "name": info.get("name", ""), "info": info.get("info", ""),
            "uid": to_int_list(info.get("uid")), "path": path.name, "content": content,
            "file": file_meta(path)}


def save_preset(payload: dict) -> dict:
    """新建或更新一个预设（含正文）。id 可作为 rename 源。"""
    if not isinstance(payload, dict):
        raise CoreError("请求体必须是对象")
    original_id = str(payload.get("original_id") or "").strip()
    pid = str(payload.get("id") or original_id).strip()
    if not PRESET_ID_RE.match(pid):
        raise CoreError("预设 ID 只能是中英文、数字、下划线、短横线（1-64 字）")

    presets = load_presets()
    if original_id and original_id != pid:
        if original_id not in presets:
            raise CoreError(f"原预设 {original_id} 不存在", 404)
        if pid in presets:
            raise CoreError(f"预设 ID {pid} 已存在", 409)

    path_name = str(payload.get("path") or f"{pid}.txt").strip()
    path = _preset_file_path(path_name)

    name = str(payload.get("name") or pid).strip()
    info_text = str(payload.get("info") or "").strip()
    uid = to_int_list(payload.get("uid"))

    # 同一个用户不能同时挂在多个预设上（机器人是按遍历顺序取最后一个，行为不确定）
    for other_id, other in presets.items():
        if other_id in (original_id, pid) or not isinstance(other, dict):
            continue
        dup = set(to_int_list(other.get("uid"))) & set(uid)
        if dup:
            raise CoreError(f"QQ 号 {'、'.join(str(x) for x in sorted(dup))} 已在预设「{other.get('name', other_id)}」里，"
                            f"同一个用户只能属于一个预设")

    content = payload.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise CoreError("预设正文必须是文本")

    if original_id and original_id != pid:
        del presets[original_id]
    presets[pid] = {"name": name, "info": info_text, "uid": uid, "path": path.name}

    warnings = []
    if path.name != f"{pid}.txt":
        warnings.append(f"预设 ID 是 {pid}，正文文件名是 {path.name}（不强制同名，机器人按 path 读取）")
    if not content.strip():
        warnings.append("正文是空的，机器人会把这套预设当作空系统提示词")

    write_json(PRESET_JSON, presets, reason="presets", indent=4)
    atomic_write_bytes(path, content.encode("utf-8"))
    return {"ok": True, "id": pid, "warnings": warnings, "file": file_meta(path)}


def delete_preset(pid: str, delete_file: bool = False) -> dict:
    presets = load_presets()
    if pid not in presets:
        raise CoreError(f"预设 {pid} 不存在", 404)
    if pid == "Normal":
        raise CoreError("Normal 是兜底预设，删除它会让没登记的用户拿不到系统提示词，已拒绝")
    info = presets.pop(pid)
    removed_file = None
    if delete_file and isinstance(info, dict) and info.get("path"):
        p = safe_child(PRESET_DIR, str(info["path"]))
        if p and p.exists() and p.suffix.lower() == ".txt":
            backup_file(p, reason="preset-del")
            p.unlink()
            removed_file = p.name
    write_json(PRESET_JSON, presets, reason="presets", indent=4)
    return {"ok": True, "removed_file": removed_file}


def list_preset_files() -> list:
    if not PRESET_DIR.exists():
        return []
    out = []
    for p in sorted(PRESET_DIR.glob("*.txt")):
        m = file_meta(p)
        out.append({"name": p.name, **m})
    return out


# --------------------------------------------------------------------------- #
# 用户与权限
# --------------------------------------------------------------------------- #
def load_ini_list(path: Path) -> list:
    if not Path(path).exists():
        return []
    raw = read_bytes(path)
    text, err = decode_utf8(raw)
    if err:
        raise CoreError(f"{Path(path).name} 编码损坏：{err}", 500)
    out, seen = [], set()
    for line in text.splitlines():
        s = line.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def get_users() -> dict:
    cfg = load_config()
    others = cfg.get("Others") or {}
    return {
        "owner": to_int_list(cfg.get("owner")),
        "black_list": to_int_list(cfg.get("black_list")),
        "silents": to_int_list(cfg.get("silents")),
        "root_user": to_int_list(others.get("ROOT_User")),
        "auto_approval": to_int_list(others.get("Auto_approval")),
        "super_user": load_ini_list(SUPER_INI),
        "manage_user": load_ini_list(MANAGE_INI),
        "ini_files": {"super": file_meta(SUPER_INI), "manage": file_meta(MANAGE_INI)},
    }


def save_users(payload: dict, expected_mtime: float | None = None) -> dict:
    cfg = load_config()
    others = cfg.setdefault("Others", {})

    for key, field in (("owner", "owner"), ("black_list", "black_list"), ("silents", "silents")):
        if key in payload:
            cfg[field] = to_int_list(payload[key])
    if "root_user" in payload:
        others["ROOT_User"] = [str(x) for x in to_int_list(payload["root_user"])]
    if "auto_approval" in payload:
        others["Auto_approval"] = [str(x) for x in to_int_list(payload["auto_approval"])]

    warnings = []
    root = others.get("ROOT_User") or []
    if not root:
        warnings.append("ROOT_User 为空：管理员操作通知会被静默跳过（不影响其它功能）")

    meta = save_config(cfg, expected_mtime=expected_mtime)

    def _write_ini(path: Path, values) -> None:
        lines = [str(x).strip() for x in (values or []) if str(x).strip()]
        backup_file(path, reason="users")
        atomic_write_bytes(path, ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8"))

    if "super_user" in payload:
        _write_ini(SUPER_INI, payload["super_user"])
    if "manage_user" in payload:
        _write_ini(MANAGE_INI, payload["manage_user"])

    return {"ok": True, "warnings": warnings + meta["warnings"], "meta": meta["meta"]}


# --------------------------------------------------------------------------- #
# 语音偏好
# --------------------------------------------------------------------------- #
def load_voice_prefs() -> dict:
    data = load_json_or(VOICE_PREFS_PATH, {})
    if not isinstance(data, dict):
        return {"on": {}, "seen": {}, "voice": {}}
    return {
        "on": data.get("on") or {},
        "seen": data.get("seen") or {},
        "voice": data.get("voice") or {},
    }


def save_voice_prefs(payload: dict) -> dict:
    """保存每会话音色/开关。

    on / voice 以提交的内容为准整体替换；seen（机器人运行时记的"最后聊天时间"）
    是运行时数据，控制台只做保留/删除，不参与覆盖 —— 否则没开语音的老会话
    会被机器人重新问一遍"要不要开语音"。
    """
    on = payload.get("on")
    voice = payload.get("voice")
    if not isinstance(on, dict) or not isinstance(voice, dict):
        raise CoreError("on / voice 必须是对象")
    remove = {str(k) for k in (payload.get("remove") or [])}
    current = load_voice_prefs()

    for key in list(on.keys()) + list(voice.keys()) + list(remove):
        if not re.match(r"^[ug]\d+$", str(key)):
            raise CoreError(f"非法的会话键 {key!r}（应为 u<QQ号> 或 g<群号>）")

    clean_on = {str(k): bool(v) for k, v in on.items() if str(k) not in remove}
    clean_voice = {str(k): str(v) for k, v in voice.items()
                   if str(k) not in remove and str(v).strip()}
    try:
        clean_seen = {str(k): float(v) for k, v in (current["seen"] or {}).items()
                      if str(k) not in remove}
    except (TypeError, ValueError):
        clean_seen = {}

    meta = write_json(VOICE_PREFS_PATH, {"on": clean_on, "seen": clean_seen, "voice": clean_voice},
                      reason="voiceprefs")
    return {"ok": True, "meta": meta, "removed": sorted(remove),
            "kept_seen": len(clean_seen)}


# --------------------------------------------------------------------------- #
# Key 来源
# --------------------------------------------------------------------------- #
def key_status() -> dict:
    cfg = load_config()
    others = cfg.get("Others") or {}
    out: dict = {}
    for name, field, envs in (
        ("deepseek", "deepseek_key", ["JIANER_API_KEY", "DEEPSEEK_API_KEY"]),
        ("gemini", "gemini_key", ["JIANER_GEMINI_KEY", "GEMINI_API_KEY"]),
        ("openai", "openai_key", ["JIANER_OPENAI_KEY", "OPENAI_API_KEY"]),
    ):
        in_conf = bool(str(others.get(field) or "").strip())
        env_hit = next((e for e in envs if (os.environ.get(e) or "").strip()), None)
        source = f"环境变量 {env_hit}" if env_hit else ("config.json" if in_conf else "未配置")
        out[name] = {"field": field, "in_config": in_conf, "env_var": env_hit,
                     "source": source, "ok": bool(env_hit or in_conf)}
    return out


# --------------------------------------------------------------------------- #
# 进程 / 端口 / 启停
# --------------------------------------------------------------------------- #
def _pid_creation_time(pid: int) -> float | None:
    """进程创建时间（Unix 秒）。ctypes 直取，不依赖 WMI / PowerShell。"""
    if not pid or pid <= 0:
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None
        try:
            creation = ctypes.c_ulonglong(0)
            exit_t = ctypes.c_ulonglong(0)
            kernel = ctypes.c_ulonglong(0)
            user = ctypes.c_ulonglong(0)
            ok = k32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_t),
                                     ctypes.byref(kernel), ctypes.byref(user))
            if not ok or creation.value <= 0:
                return None
            return creation.value / 10_000_000 - 11644473600  # FILETIME(1601) -> Unix(1970)
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None


def pid_detail(pid: int | None) -> dict:
    """一个 PID 的实况：是否活着、是什么程序、什么时候启动的。"""
    pid = int(pid or 0)
    if pid <= 0:
        return {"pid": 0, "alive": False}
    image = _pid_image(pid)
    alive = bool(image) or _pid_alive(pid)
    created = _pid_creation_time(pid) if alive else None
    return {
        "pid": pid,
        "alive": alive,
        "image": image,
        "name": os.path.basename(image) if image else None,
        "is_python": bool(image) and os.path.basename(image).lower() in ("python.exe", "pythonw.exe"),
        "is_venv_python": bool(image) and os.path.normcase(image) == os.path.normcase(str(VENV_PY)),
        "started_ts": created,
        "started_str": datetime.datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M:%S") if created else "",
    }


def _pid_image(pid: int) -> str | None:
    """用 ctypes 取进程可执行文件路径（不依赖 PowerShell，安全沙箱下也能用）。"""
    if not pid or pid <= 0:
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint(1024)
            ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            return buf.value if ok else None
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        SYNCHRONIZE = 0x00100000
        h = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not h:
            return False
        k32.CloseHandle(h)
        return True
    except Exception:
        return False


def _ps_lines(script: str, timeout: float = 12.0) -> tuple[bool, list]:
    """执行 PowerShell，把结果写进临时文件再读（不用管道，避免沙箱/权限问题）。

    返回 (可用?, 行列表)。可用=False 表示 PowerShell 本身跑不起来或脚本报错
    （例如沙箱里 WMI/CIM 被拦），此时不能把"空列表"当成"没有进程"。
    """
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return False, []
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LOG_DIR / f".ps-{os.getpid()}-{int(time.time() * 1000)}.txt"
    full = f"{script} | Out-File -FilePath '{tmp}' -Encoding utf8"
    try:
        proc = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", full],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=timeout, creationflags=NO_WINDOW)
        if proc.returncode != 0 or not tmp.exists():
            return False, []
        text, err = decode_utf8(read_bytes(tmp))
        return True, [l.rstrip() for l in (text or "").splitlines() if l.strip()]
    except Exception:
        return False, []
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def scan_bot_processes() -> tuple[bool, list]:
    """找在跑 main.py 的 python 进程。返回 (扫描是否可用, [{pid, started}])。

    只认命令行里带本项目 main.py 全路径的进程 —— 别的 python（包括控制台自己）
    不会被误判成机器人。
    """
    entry = str(BOT_ENTRY).replace("'", "''")
    script = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
              f"Where-Object {{ $_.CommandLine -like '*{entry}*' }} | "
              "ForEach-Object { \"$($_.ProcessId)|$($_.CreationDate)\" }")
    available, lines = _ps_lines(script)
    out = []
    for line in lines:
        parts = line.split("|")
        if parts and parts[0].strip().isdigit():
            out.append({"pid": int(parts[0].strip()),
                        "started": parts[1].strip() if len(parts) > 1 else ""})
    return available, out


def lock_pid() -> int | None:
    try:
        text = LOCK_PATH.read_text(encoding="utf-8", errors="replace").strip()
        return int(re.match(r"\d+", text).group()) if re.match(r"\d+", text) else None
    except Exception:
        return None


def port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def cmdline_of(pid: int, ttl: float = 5.0) -> str | None:
    """取某个 PID 的命令行（CIM）。带 5 秒缓存，避免状态接口每次都起 PowerShell。"""
    pid = int(pid or 0)
    if pid <= 0:
        return None
    now = time.time()
    hit = _CMDLINE_CACHE.get(pid)
    if hit and now - hit[1] < ttl:
        return hit[0]
    script = (f"Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" | "
              "ForEach-Object { $_.CommandLine }")
    ok, lines = _ps_lines(script)
    val = lines[0].strip() if ok and lines else None
    _CMDLINE_CACHE[pid] = (val, now)
    return val


_CMDLINE_CACHE: dict = {}


def lock_pid_is_main(pid: int | None) -> tuple[bool, str | None]:
    """锁文件里的 PID 到底是不是在跑 main.py？

    为什么要单独查一次：`start_jianer.bat` 里是 `python main.py`（相对路径），
    命令行里**没有**绝对路径，所以"全盘扫描 main.py 全路径"会把真正在跑的机器人漏掉，
    进而误报"未运行 / 残留锁"。这里直接按锁文件里的 PID 查它自己的命令行。
    """
    if not pid:
        return False, None
    cl = cmdline_of(pid)
    if not cl:
        return False, None
    low = cl.lower().replace("/", "\\")
    if "main.py" not in low:
        return False, cl
    if str(BOT_ENTRY).lower() in low or str(BOT_DIR).lower() in low:
        return True, cl
    # 命令行只有 main.py 这种相对写法：再要求解释器在本项目 venv 里（或就是发布包 python）
    return True, cl


def bot_status() -> dict:
    """机器人状态（三态，绝不硬猜）。

    判定依据按可靠度排序：
      1. **confirmed**：命令行里确实带本项目 main.py 的进程存在。两条路：
         a) 全盘扫描（能抓到用绝对路径启动的，比如控制台自己拉起的）；
         b) 直接查 jianer.lock 里那个 PID 的命令行 —— `start_jianer.bat` 用的是
            `python main.py` 相对写法，全盘扫描会漏，必须这样补。
      2. **probable** ：命令行读不到时，用「进程启动时间 ≈ 锁文件写入时间」交叉验证
         （机器人启动第一件事就是写锁，对得上基本就是它）。
      3. **none**：没有迹象。

    为什么不能像 main.py 那样只看 PID 死活：PID 会被系统复用，本机出现过锁里的 PID
    落到无关 python 进程上的情况，那时误判"运行中"会拒绝启动/误杀进程。
    """
    scan_available, procs = scan_bot_processes()
    confirmed = bool(procs)
    lock = lock_pid()
    lock_info = pid_detail(lock) if lock else None
    lock_mtime = file_meta(LOCK_PATH)["mtime"]
    lock_cmdline = None
    lock_is_main = False

    lock_alive_python = bool(lock_info and lock_info.get("alive") and lock_info.get("is_python"))
    if not confirmed and lock_alive_python:
        lock_is_main, lock_cmdline = lock_pid_is_main(lock)
        if lock_is_main:
            confirmed = True
            procs = [{"pid": lock, "started": lock_info.get("started_str", "")}]

    probable = False
    time_gap = None
    if (not confirmed and lock_alive_python
            and lock_info.get("started_ts") and lock_mtime):
        time_gap = lock_mtime - lock_info["started_ts"]
        probable = -180.0 <= time_gap <= 15.0

    # 只有"时间也对不上、命令行也不是 main.py"时，才算真·残留锁（PID 被复用）
    stale_lock = bool(scan_available and not confirmed and not probable and lock_alive_python)

    if confirmed:
        how = "全盘扫描" if lock_is_main is False else "锁文件 PID 的命令行"
        confidence = "confirmed"
        detection = f"已确认 main.py 进程在运行（{how}，PID {', '.join(str(p['pid']) for p in procs)}）"
    elif probable:
        confidence = "probable"
        detection = (f"读不到命令行（权限/沙箱），按锁文件推断：PID {lock} 的启动时间与 "
                     f"jianer.lock 写入时间相差 {abs(time_gap):.0f} 秒 —— 很可能正在运行")
    elif not scan_available:
        confidence, detection = "none", "进程扫描不可用，且锁文件的 PID 与写入时间对不上，无法确认"
    else:
        confidence, detection = "none", "未发现运行中的 main.py 进程"

    cfg_port = 5004
    try:
        cfg_port = int(((load_config().get("Connection") or {}).get("port")) or 5004)
    except Exception:
        pass

    hint = ""
    if stale_lock:
        hint = (f"jianer.lock 里的 PID {lock} 仍然存活，但命令行里没有本项目 main.py，"
                f"而且它的启动时间（{lock_info.get('started_str') or '未知'}）与锁文件写入时间"
                f"（{file_meta(LOCK_PATH)['mtime_str']}）对不上 —— 判定为 PID 被复用后的残留锁。"
                f"main.py 的启动检查只看 PID 死活，会因此报「检测到机器人已在运行」并拒绝启动。")
    elif probable:
        hint = (f"读不到 PID {lock} 的命令行（权限/沙箱限制），但它的启动时间与锁文件写入时间吻合，"
                f"判断为机器人本体正在运行 —— 停止机器人会按锁文件里的这个 PID 去结束它。"
                f"如果安全软件拦了 WMI/CIM，这个判断可能不准，请自行确认。")

    return {
        "running": confirmed or probable,
        "confirmed": confirmed,
        "probable": probable,
        "confidence": confidence,
        "pids": [p["pid"] for p in procs] or ([lock] if probable else []),
        "processes": procs,
        "detection": detection,
        "scan_available": scan_available,
        "lock": lock_info,
        "lock_mtime": lock_mtime,
        "lock_mtime_str": file_meta(LOCK_PATH)["mtime_str"],
        "lock_time_gap": time_gap,
        "stale_lock": stale_lock,
        "stale_lock_hint": hint,
        "entry": str(BOT_ENTRY),
        "onebot_port": cfg_port,
        "onebot_open": port_open("127.0.0.1", cfg_port),
        "listener_port_open": port_open("127.0.0.1", 5003),
    }


def clear_stale_lock(force: bool = False) -> dict:
    """清理残留的 jianer.lock。只有确实没在跑时才允许（force 可越过确认）。"""
    status = bot_status()
    if status["confirmed"] and not force:
        raise CoreError("已经确认机器人在运行，不能清理锁文件。", 409)
    if status.get("probable") and not force:
        raise CoreError("锁文件的 PID 启动时间与写入时间吻合，机器人很可能正在运行 —— 不能清理。"
                        "如果你确定它没在跑，请用「强行清理」。", 409)
    if not status["scan_available"] and not force:
        raise CoreError("进程扫描不可用，无法确认机器人是否在运行。"
                        "如果你确定它没在跑，请用「强行清理」。", 409)
    if not LOCK_PATH.exists():
        return {"ok": True, "note": "本来就没有 jianer.lock。"}
    lock = lock_pid()
    info = pid_detail(lock) if lock else None
    bak = backup_file(LOCK_PATH, reason="stale-lock")
    LOCK_PATH.unlink()
    return {"ok": True, "removed_pid": lock, "pid_detail": info, "backup": bak,
            "note": "锁文件已清理（已备份），现在可以正常启动机器人了。"}


def start_bot(mode: str = "bot") -> dict:
    """启动机器人。mode=bot 只跑 main.py（日志进控制台 logs/）；mode=all 跑一键启动脚本。"""
    status = bot_status()
    if status["confirmed"]:
        raise CoreError("机器人已经在运行了（如需重启请先停止）", 409)
    if status.get("probable"):
        raise CoreError("锁文件显示机器人很可能正在运行（" + status["stale_lock_hint"] +
                        "）—— 如果真的没在跑，请先清理锁文件再启动。", 409)
    if status.get("stale_lock"):
        raise CoreError("检测到残留锁文件： " + status["stale_lock_hint"] +
                        " 请先在「概览」里清理它，否则 main.py 会拒绝启动。", 409)

    if mode == "all":
        bat = START_ALL_BAT if START_ALL_BAT.exists() else START_BOT_BAT
        if not bat.exists():
            raise CoreError(f"找不到启动脚本：{START_ALL_BAT}", 404)
        subprocess.Popen(["cmd", "/c", "start", "", str(bat)], cwd=str(bat.parent),
                         creationflags=DETACHED, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "mode": "all", "script": str(bat),
                "note": "已在新窗口启动一键脚本（NapCat + 机器人）。该窗口的日志不在控制台里。"}

    py = VENV_PY if VENV_PY.exists() else Path(sys.executable)
    if not BOT_ENTRY.exists():
        raise CoreError(f"找不到 {BOT_ENTRY}", 404)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"bot-{stamp()}.log"
    fh = open(log_path, "ab", buffering=0)
    fh.write(f"===== {now_str()} 由小狐狸控制台启动：{py} {BOT_ENTRY.name} =====\n".encode("utf-8"))
    # PYTHONUNBUFFERED：机器人是长驻进程，标准输出重定向到文件时默认是块缓冲的，
    # 不加这个的话日志要等缓冲区满（或进程退出）才出现，看起来像"启动了却没输出"。
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = subprocess.Popen([str(py), str(BOT_ENTRY)], cwd=str(BOT_DIR), env=env,
                                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                creationflags=DETACHED, close_fds=True)
    except Exception as exc:
        fh.close()
        raise CoreError(f"启动失败：{exc}", 500)
    finally:
        try:
            fh.close()
        except Exception:
            pass
    return {"ok": True, "mode": "bot", "pid": proc.pid, "log": str(log_path),
            "note": "日志正在写入控制台的 logs 目录，可在「实时日志」查看。"}


def stop_bot(force: bool = False) -> dict:
    """停止机器人。只结束"确认是 main.py"的进程；扫描不可用时退到"锁文件时间吻合"的推断。"""
    status = bot_status()
    _, procs = scan_bot_processes()
    pids = [p["pid"] for p in procs]
    inferred = False
    if not pids and status.get("probable") and status.get("lock"):
        pids = [status["lock"]["pid"]]
        inferred = True
    if not pids:
        if not status["scan_available"]:
            return {"ok": False, "stopped": [], "still_running": [],
                    "note": "进程扫描不可用（PowerShell/WMI 被拦），且锁文件的时间对不上，"
                            "无法确认机器人在不在跑；为避免误杀无关进程，本次不执行任何结束操作。"}
        return {"ok": True, "stopped": [], "still_running": [],
                "note": "没有发现运行中的 main.py 进程，无需停止。"}

    killed = []
    for pid in pids:
        try:
            args = ["taskkill", "/PID", str(pid)] + (["/F"] if force else ["/T"])
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, creationflags=NO_WINDOW)
            killed.append(pid)
        except Exception:
            pass
    time.sleep(1.2)
    _, still_procs = scan_bot_processes()
    for p in still_procs:
        try:
            subprocess.run(["taskkill", "/PID", str(p["pid"]), "/F"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10, creationflags=NO_WINDOW)
        except Exception:
            pass
    time.sleep(0.8)
    after = bot_status()
    if inferred:
        # 锁文件推断出来的目标：只能靠"它还在不在"判断，不能凭空报成功
        left = [pids[0]] if after.get("probable") else []
    else:
        left = [p["pid"] for p in after["processes"]] if after["confirmed"] else []
    note = "已停止机器人进程。" if not left else "仍有进程没退出，请到任务管理器确认。"
    if left and not force:
        note += "（可能是权限不足：控制台结束不了以更高权限运行的进程，可改用「强行停止」或手动关闭）。"
    return {"ok": not left, "stopped": killed, "still_running": left,
            "inferred_from_lock": inferred, "note": (f"按 jianer.lock 推断出 PID {pids[0]}：" if inferred else "") + note}


# --------------------------------------------------------------------------- #
# 重启机器人（保存配置后"一键生效"）
# --------------------------------------------------------------------------- #
def wait_bot_stopped(timeout: float = 12.0) -> bool:
    """等到机器人确实不在了（锁文件被清掉 或 进程消失）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = bot_status()
        if not st["confirmed"] and not st.get("probable") and not LOCK_PATH.exists():
            return True
        time.sleep(0.5)
    st = bot_status()
    return not st["confirmed"] and not st.get("probable")


def wait_bot_started(timeout: float = 40.0) -> dict:
    """等机器人起来：main.py 起来后第一件事就是写 jianer.lock，所以以锁文件为准。

    另外盯一眼日志里有没有"已在运行/拒绝启动"这类字样，避免傻等。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if LOCK_PATH.exists():
            return {"ok": True, "waited": round(timeout - (deadline - time.time()), 1),
                    "lock_pid": lock_pid()}
        time.sleep(0.5)
    return {"ok": False, "note": f"等了 {timeout:.0f} 秒也没等到 jianer.lock，"
                                 f"机器人可能启动失败，请到「实时日志」看最后的输出。"}


def restart_bot(mode: str = "bot", wait: bool = True) -> dict:
    """重启机器人：停止 -> 等它真的退出 -> 重新启动 -> 等锁文件出现。

    只重启 main.py；NapCat（QQ 协议端）不动 —— 它本来就是常驻的。
    """
    before = bot_status()
    result: dict = {"was_running": before["running"], "confidence": before["confidence"]}

    if before["running"]:
        result["stop"] = stop_bot(force=False)
        if wait:
            result["stopped_clean"] = wait_bot_stopped(timeout=15)
            if not result["stopped_clean"]:
                result["ok"] = False
                result["note"] = ("旧进程 15 秒内没退出，为避免起两个实例，已中止重启。"
                                  "可以到「连接与运维」手动停止后再试。")
                return result
    else:
        result["stop"] = {"ok": True, "stopped": [], "note": "重启前机器人并未运行。"}
        if before.get("stale_lock"):
            # 残留锁会让 main.py 自己拒绝启动，先清掉（已备份）
            try:
                result["clear_lock"] = clear_stale_lock(force=True)
            except CoreError as exc:
                result["ok"] = False
                result["note"] = f"清理残留锁失败：{exc}"
                return result

    try:
        result["start"] = start_bot(mode)
    except CoreError as exc:
        result["ok"] = False
        result["note"] = f"重新启动失败：{exc}"
        return result

    if wait:
        result["started"] = wait_bot_started(timeout=40)
        result["ok"] = bool(result["started"].get("ok"))
        result["note"] = ("机器人已重启，配置已生效。"
                          if result["ok"] else result["started"].get("note", "启动结果未知"))
    else:
        result["ok"] = True
        result["note"] = "已发出重启指令。"
    result["log"] = result.get("start", {}).get("log")
    return result


# --------------------------------------------------------------------------- #
# 控制台自身设置（console.json）
# --------------------------------------------------------------------------- #
CONSOLE_SETTING_DEFAULTS = {
    "auto_restart": False,      # 保存配置后自动重启机器人
    "restart_mode": "auto",     # auto=只在机器人本来就在跑时重启；force=总是重启
    "port": 5010,
}


def console_settings() -> dict:
    data = load_console_json()
    out = dict(CONSOLE_SETTING_DEFAULTS)
    for k, v in CONSOLE_SETTING_DEFAULTS.items():
        if k in data:
            out[k] = data[k]
    out["auto_restart"] = bool(out["auto_restart"])
    if out["restart_mode"] not in ("auto", "force"):
        out["restart_mode"] = "auto"
    return out


def save_console_settings(patch: dict) -> dict:
    if not isinstance(patch, dict):
        raise CoreError("设置必须是对象")
    data = load_console_json()
    for k in ("auto_restart", "restart_mode", "port"):
        if k in patch:
            data[k] = patch[k]
    if "auto_restart" in patch:
        data["auto_restart"] = bool(patch["auto_restart"])
    if data.get("restart_mode") not in (None, "auto", "force"):
        raise CoreError("restart_mode 只能是 auto 或 force")
    data.setdefault("token", ensure_token())
    atomic_write_bytes(CONSOLE_JSON, dump_json_bytes(data))
    return console_settings()


def start_tray(current_port: int | None = None, wait: float = 10.0) -> dict:
    """后台拉起一份"托盘常驻"实例（pythonw，无窗口），并等它报出新端口。"""
    if not TRAY_BAT.exists():
        raise CoreError(f"缺少 {TRAY_BAT}", 404)
    try:
        subprocess.Popen(["cmd", "/c", "start", "", str(TRAY_BAT)], cwd=str(CONSOLE_DIR),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=DETACHED, close_fds=True)
    except Exception as exc:
        raise CoreError(f"拉起托盘实例失败：{exc}", 500)

    deadline = time.time() + wait
    found_port = None
    while time.time() < deadline:
        try:
            p = int(load_console_json().get("port") or 0)
        except Exception:
            p = 0
        if p and p != current_port and port_open("127.0.0.1", p):
            found_port = p
            break
        time.sleep(0.5)

    if not found_port:
        return {"ok": True, "port": None,
                "note": "托盘实例已拉起，但没等到它报出端口。请查看托盘图标，"
                        "或看 logs/tray.log（多半是端口被占/启动失败）。"}
    try:
        tok = load_console_json().get("token")
    except Exception:
        tok = None
    url = f"http://127.0.0.1:{found_port}/" + (f"?token={tok}" if tok else "")
    return {"ok": True, "port": found_port, "url": url,
            "note": f"托盘实例已在 http://127.0.0.1:{found_port}/ 上运行（无窗口、图标在托盘）。"
                    f"确认它能打开后，就可以关掉当前这个窗口了。"}


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def list_logs() -> list:
    if not LOG_DIR.exists():
        return []
    out = []
    for p in sorted(LOG_DIR.glob("*.log"), key=lambda x: x.stat().st_mtime, reverse=True):
        m = file_meta(p)
        out.append({"name": p.name, **m})
    return out


def read_log(name: str, tail: int = 300) -> dict:
    p = safe_child(LOG_DIR, str(name or ""))
    if p is None or p.suffix != ".log" or not p.exists():
        raise CoreError(f"日志 {name!r} 不存在", 404)
    raw = read_bytes(p)
    text, err = decode_utf8(raw)
    if err:
        text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    tail = max(1, min(5000, int(tail or 300)))
    return {"name": p.name, "total_lines": len(lines), "lines": lines[-tail:],
            "meta": file_meta(p), "truncated": len(lines) > tail}


# --------------------------------------------------------------------------- #
# TTS：声线清单 + 试听
# --------------------------------------------------------------------------- #
def _run_worker(args: list, log_name: str, timeout: float = 60.0) -> tuple[bool, str]:
    py = VENV_PY if VENV_PY.exists() else Path(sys.executable)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / log_name
    with open(log_path, "wb") as fh:
        try:
            proc = subprocess.run([str(py), str(TTS_WORKER), *args], cwd=str(CONSOLE_DIR),
                                  stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                  timeout=timeout, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return False, f"超时（{timeout:.0f}s）"
        except Exception as exc:
            return False, f"无法执行 {py}：{exc}"
    text, _ = decode_utf8(read_bytes(log_path))
    return proc.returncode == 0, (text or "").strip()


def list_edge_voices(force: bool = False) -> dict:
    """edge-tts 的完整声线清单（缓存 24 小时）；失败时只返回内置的 7 个友好音色。"""
    cache_ok = False
    if VOICES_CACHE.exists() and not force:
        try:
            data = load_json_or(VOICES_CACHE, {})
            if isinstance(data, dict) and data.get("fetched_at", 0) > time.time() - 86400:
                return {"builtin": [{"name": n, "id": i, "desc": d} for n, i, d in TTS_PRESETS],
                        "gender": GENDER_VOICES, "default": DEFAULT_TTS_VOICE,
                        "edge": data.get("voices", []), "cached": True, "error": None}
        except Exception:
            pass
    ok, out = _run_worker(["--list"], "voices.log", timeout=45)
    voices = []
    if ok and out.startswith("["):
        try:
            voices = json.loads(out)
        except Exception:
            voices = []
    if voices:
        atomic_write_bytes(VOICES_CACHE, dump_json_bytes({"fetched_at": time.time(), "voices": voices}))
        cache_ok = True
    return {"builtin": [{"name": n, "id": i, "desc": d} for n, i, d in TTS_PRESETS],
            "gender": GENDER_VOICES, "default": DEFAULT_TTS_VOICE,
            "edge": voices, "cached": cache_ok,
            "error": None if voices else out[:400] or "无法获取在线声线清单（可继续用内置 7 个音色）"}


def tts_preview(text: str, voice: str, rate: str, volume: str, pitch: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise CoreError("试听文本不能为空")
    if len(text) > 200:
        text = text[:200]
    if not TTS_RATE_RE.match(rate or "+0%"):
        rate = "+0%"
    if not TTS_RATE_RE.match(volume or "+0%"):
        volume = "+0%"
    if not TTS_HZ_RE.match(pitch or "+0Hz"):
        pitch = "+0Hz"
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    out_name = f"preview-{stamp()}-{secrets.token_hex(3)}.mp3"
    out_path = PREVIEW_DIR / out_name
    ok, log = _run_worker(["--say", str(out_path), text, voice or DEFAULT_TTS_VOICE, rate, volume, pitch],
                          "tts.log", timeout=60)
    if not ok or not out_path.exists() or out_path.stat().st_size == 0:
        raise CoreError(f"合成失败：{log[-400:] or '未知错误'}", 502)
    return {"ok": True, "file": out_name, "url": f"/preview/{out_name}", "size": out_path.stat().st_size}


def preview_file(name: str) -> Path:
    p = safe_child(PREVIEW_DIR, str(name or ""))
    if p is None or p.suffix != ".mp3" or not p.exists():
        raise CoreError("试听文件不存在", 404)
    return p


# --------------------------------------------------------------------------- #
# 备份
# --------------------------------------------------------------------------- #
def list_backups() -> list:
    if not BACKUP_DIR.exists():
        return []
    out = []
    for p in sorted(BACKUP_DIR.glob("*.bak"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        target = backup_target(p.name)
        out.append({"name": p.name, "target": target, "size": st.st_size,
                    "mtime": st.st_mtime,
                    "mtime_str": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
    return out[:200]


ALLOWED_RESTORE_TARGETS = {"config.json", "current.json", "voice_prefs.json",
                           "Super_User.ini", "Manage_User.ini"}


def restore_backup(name: str) -> dict:
    src = safe_child(BACKUP_DIR, str(name or ""))
    if src is None or src.suffix != ".bak" or not src.exists():
        raise CoreError("备份文件不存在", 404)
    target_name = backup_target(src.name)
    if target_name not in ALLOWED_RESTORE_TARGETS:
        raise CoreError(f"{target_name} 不允许从控制台还原（只允许配置文件）")
    if target_name.endswith(".txt"):
        dst = PRESET_DIR / target_name
    elif target_name == "current.json":
        dst = PRESET_JSON
    else:
        dst = BOT_DIR / target_name

    raw = read_bytes(src)
    if target_name.endswith(".json"):
        text, err = decode_utf8(raw)
        if err:
            raise CoreError(f"备份文件编码损坏：{err}", 500)
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise CoreError(f"备份不是合法 JSON：{exc}", 400)
    backup_file(dst, reason="before-restore")
    atomic_write_bytes(dst, raw)
    return {"ok": True, "target": str(dst), "restored_from": src.name, "size": len(raw)}


# --------------------------------------------------------------------------- #
# 概览
# --------------------------------------------------------------------------- #
def overview() -> dict:
    health = config_health()
    status = bot_status()
    try:
        cfg = load_config()
        others = cfg.get("Others") or {}
        backend = {
            "ai_backend": others.get("ai_backend", "local"),
            "cloud_base_url": others.get("cloud_base_url", ""),
            "cloud_model": others.get("cloud_model", ""),
            "local_base_url": others.get("local_base_url", ""),
            "local_model": others.get("local_model", ""),
            "default_mode": others.get("default_mode", "Ds"),
            "bot_name": others.get("bot_name", ""),
            "keys": key_status(),
        }
        cfg_ok = True
    except CoreError as exc:
        backend, cfg_ok = {"error": str(exc)}, False

    return {
        "version": CONSOLE_VERSION,
        "bot_dir": str(BOT_DIR),
        "console_dir": str(CONSOLE_DIR),
        "python_for_bot": str(VENV_PY if VENV_PY.exists() else sys.executable),
        "config_health": health,
        "config_ok": cfg_ok,
        "bot": status,
        "backend": backend,
        "files": {
            "config.json": file_meta(CONFIG_PATH),
            "prerequisites/current.json": file_meta(PRESET_JSON),
            "voice_prefs.json": file_meta(VOICE_PREFS_PATH),
            "Super_User.ini": file_meta(SUPER_INI),
            "Manage_User.ini": file_meta(MANAGE_INI),
            "jianer.lock": file_meta(LOCK_PATH),
        },
        "counts": {
            "presets": len(load_presets()) if PRESET_JSON.exists() else 0,
            "backups": len(list_backups()),
            "logs": len(list_logs()),
        },
        "timestamp": now_str(),
    }


# --------------------------------------------------------------------------- #
# 字段审计：机器人代码真正读了哪些配置键 / 界面声明了哪些 / 配置里实际有哪些
# --------------------------------------------------------------------------- #
OTHERS_KEY_RE = re.compile(r"""others[^\n]{0,24}?\.get\(\s*['"]([A-Za-z0-9_]+)['"]""")
OTHERS_IDX_RE = re.compile(r"""others[^\n]{0,24}?\[\s*['"]([A-Za-z0-9_]+)['"]\s*\]""")
# 顶层键：Hyper 框架是按 config_json["xxx"] 读的；项目代码里这么读的很少
TOP_KEY_RE = re.compile(r"""config_json\[\s*['"]([A-Za-z0-9_]+)['"]\s*\]""")
# 形如 _cfg_number("asr_timeout_seconds", 30) 的取值助手：名字里带 cfg/config/setting 才算
CFG_CALL_RE = re.compile(r"""(?:cfg|config|setting|conf)\w*\(\s*['"]([A-Za-z0-9_]+)['"]""", re.I)
# 「别名」写法：o = _others() / _cfg = config.others 之后用 o.get("key")
ALIAS_DEF_RE = re.compile(r"""^\s*([A-Za-z_]\w*)\s*=\s*(?:_others\(\)|[\w.]*others)\s*$""")
UI_OTHERS_RE = re.compile(r"""path:\s*['"]Others\.([A-Za-z0-9_]+)['"]""")
# 界面里不走 path 声明、由自定义编辑器改的键（app.js 里的 UI_EXTRA_KEYS）
UI_EXTRA_RE = re.compile(r"""const UI_EXTRA_KEYS\s*=\s*\[(.*?)\]""", re.S)

# 只扫"机器人本体"：官方向导 app.py 和一堆测试脚本不是机器人运行时的一部分，
# 它们读的键会把审计结果搅浑（比如向导自己读 Others/ROOT_User）。
AUDIT_INCLUDE = ["main.py", "AI_bot", "Tools", "plugins", "prerequisites"]
SKIP_FILES = {"app.py", "test_cloud.py", "verify_cloud.py", "extract_strings.py",
              "read_backend.py"}


def _iter_bot_sources() -> list[Path]:
    """机器人本体的 .py：main.py + AI_bot/ + Tools/ + plugins/ + prerequisites/。"""
    out: list[Path] = []
    for name in AUDIT_INCLUDE:
        target = BOT_DIR / name
        if target.is_file():
            if target.name not in SKIP_FILES:
                out.append(target)
        elif target.is_dir():
            for p in target.rglob("*.py"):
                if "__pycache__" in p.parts or p.name in SKIP_FILES:
                    continue
                out.append(p)
    return sorted(set(out))


def field_audit() -> dict:
    """把"代码在用"、"界面能改"、"配置里已有"三份清单对起来，找出漂移。

    这是把控制台接到还在开发中的功能（比如多模型切换）上的关键：
    机器人代码新增一个 others.get("xxx")（或 `o = _others()` 之后 `o.get("xxx")`）
    之后，这里会立刻把 xxx 标成「代码在用但界面没有」，而不是等你发现界面改不了。
    """
    code_keys: dict[str, list] = {}
    top_keys: dict[str, list] = {}
    files = _iter_bot_sources()
    for p in files:
        try:
            text, err = decode_utf8(read_bytes(p))
        except Exception:
            continue
        if err:
            continue
        rel = str(p.relative_to(BOT_DIR))
        lines = text.splitlines()
        # 先收集本文件里的 "others 别名"（o = _others() / c = config.others），
        # 再按别名找它读了哪些键 —— 否则 ai_backend.py 那种 o.get("cloud_reasoning")
        # 会被漏掉。
        aliases = set()
        for line in lines:
            m = ALIAS_DEF_RE.match(line)
            if m:
                aliases.add(m.group(1))
        alias_re = (re.compile(
            r"""(?:%s)\s*(?:\.get\(\s*|\[\s*)['"]([A-Za-z0-9_]+)['"]"""
            % "|".join(re.escape(a) for a in aliases)) if aliases else None)

        for lineno, line in enumerate(lines, 1):
            if line.lstrip().startswith("#"):
                continue
            patterns = [(OTHERS_KEY_RE, code_keys), (OTHERS_IDX_RE, code_keys),
                        (TOP_KEY_RE, top_keys), (CFG_CALL_RE, code_keys)]
            if alias_re is not None:
                patterns.append((alias_re, code_keys))
            for rx, bucket in patterns:
                for m in rx.finditer(line):
                    key = m.group(1)
                    if key in ("Others", "others"):
                        continue
                    bucket.setdefault(key, [])
                    if len(bucket[key]) < 6:
                        bucket[key].append({"file": rel, "line": lineno,
                                            "text": line.strip()[:110]})

    ui_text = ""
    try:
        ui_text, _ = decode_utf8(read_bytes(CONSOLE_DIR / "static" / "app.js"))
    except Exception:
        ui_text = ""
    ui_others = set(UI_OTHERS_RE.findall(ui_text or ""))
    ui_top: set = set()
    # app.js 里显式声明"由自定义编辑器维护"的键（模型注册表、用户名单等）
    m = UI_EXTRA_RE.search(ui_text or "")
    if m:
        for key in re.findall(r"""['"]([A-Za-z0-9_.]+)['"]""", m.group(1)):
            if key.startswith("Others."):
                ui_others.add(key[len("Others."):])
            else:
                ui_top.add(key)
    ui_others = sorted(ui_others)
    ui_top = sorted(ui_top)

    try:
        cfg = load_config()
    except CoreError:
        cfg = {}
    cfg_others = sorted((cfg.get("Others") or {}).keys())
    cfg_top = sorted(cfg.keys())

    def group(code: dict, ui: list, present: list) -> dict:
        read = set(code)
        ui_set, pres = set(ui), set(present)
        return {
            "read_by_code": {k: code[k] for k in sorted(read)},
            "declared_in_ui": sorted(ui_set),
            "present_in_config": sorted(pres),
            "missing_in_ui": sorted(read - ui_set),          # 代码在用，界面改不了
            "ui_but_unread": sorted(ui_set - read),          # 界面有，但代码根本没读
            "in_config_but_unread": sorted(pres - read),     # 配置里有，但代码没读（可能是拼错/遗留）
        }

    others = group(code_keys, ui_others, cfg_others)
    top = group(top_keys, ui_top, cfg_top)

    has_models = isinstance((cfg.get("Others") or {}).get("models"), dict)
    models_read = "models" in code_keys
    if has_models and models_read:
        models_note = ("config.json → Others.models 已有注册表，且 Tools/model_registry.py 正在读取它 —— "
                       "在「模型注册表」页改完保存、重启机器人即生效。")
    elif has_models:
        models_note = ("config.json → Others.models 已有注册表，但没扫到读取它的代码 —— "
                       "可能需求⑤还没落地。")
    else:
        models_note = ("config.json → Others.models 还没有内容。点「从当前配置生成初始注册表」建一份；"
                       "Tools/model_registry.py 已经在读这个位置了，所以建好就能用。")

    return {
        "scanned_files": len(files),
        "others": others,
        "top_level": top,
        "models_note": models_note,
        "generated_at": now_str(),
    }


# --------------------------------------------------------------------------- #
# 开机自启（启动文件夹里放一个只负责拉起托盘模式的 bat）
# --------------------------------------------------------------------------- #
STARTUP_DIR = Path(os.environ.get("FOX_STARTUP_DIR")
                   or ((Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
                        / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")))
AUTOSTART_NAME = "小狐狸控制台.bat"
TRAY_BAT = CONSOLE_DIR / "start-console-tray.bat"


def autostart_status() -> dict:
    p = STARTUP_DIR / AUTOSTART_NAME
    return {
        "enabled": p.exists(),
        "path": str(p),
        "startup_dir": str(STARTUP_DIR),
        "startup_dir_exists": STARTUP_DIR.is_dir(),
        "launcher": str(TRAY_BAT),
        "launcher_exists": TRAY_BAT.exists(),
    }


def set_autostart(enabled: bool) -> dict:
    """开关开机自启。只写/删启动文件夹里的一个 bat，不动注册表。"""
    if not STARTUP_DIR.is_dir():
        raise CoreError(f"找不到启动文件夹：{STARTUP_DIR}", 404)
    p = STARTUP_DIR / AUTOSTART_NAME
    if enabled:
        if not TRAY_BAT.exists():
            raise CoreError(f"缺少托盘启动脚本 {TRAY_BAT}，无法配置自启", 404)
        content = ("@echo off\r\n"
                   "rem Little Fox Console - autostart (generated; delete this file to disable)\r\n"
                   f'cd /d "{CONSOLE_DIR}"\r\n'
                   f'start "" "{TRAY_BAT}"\r\n')
        try:
            # bat 会被 cmd 按 OEM 代码页读取：纯 ASCII 路径没问题，含中文才退回 GBK
            atomic_write_bytes(p, content.encode("ascii"))
        except UnicodeEncodeError:
            atomic_write_bytes(p, content.encode("gbk", errors="replace"))
        except OSError as exc:
            raise CoreError(f"写入启动文件夹失败：{exc}（可能被安全软件拦住）", 500)
    elif p.exists():
        backup_file(p, reason="autostart-off")
        try:
            p.unlink()
        except OSError as exc:
            raise CoreError(f"删除启动项失败：{exc}", 500)
    return autostart_status()


# --------------------------------------------------------------------------- #
# 控制台自身的 token
# --------------------------------------------------------------------------- #
def load_console_json() -> dict:
    data = load_json_or(CONSOLE_JSON, {})
    return data if isinstance(data, dict) else {}


def _http_get_json(url: str, headers: dict | None = None, timeout: float = 8.0) -> tuple[int, str]:
    """不带代理的 GET（等价于 openai SDK 的 trust_env=False），返回 (状态码, 正文前 800 字)。"""
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(4000).decode("utf-8", "replace")
            return resp.status, body[:800]
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode("utf-8", "replace") if hasattr(exc, "read") else ""
        return exc.code, (body or str(exc))[:800]
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def test_backend(kind: str) -> dict:
    """探测某个后端能不能连通：拉一次 /models。只读，不发任何业务请求。"""
    cfg = load_config()
    others = cfg.get("Others") or {}
    kind = (kind or "").strip().lower()

    if kind in ("cloud", "local"):
        if kind == "cloud":
            base = str(others.get("cloud_base_url") or "").strip() or "https://api.deepseek.com/v1"
            model = others.get("cloud_model") or ""
        else:
            base = str(others.get("local_base_url") or "").strip() or "http://localhost:1234/v1"
            model = others.get("local_model") or ""
        key = str(others.get("deepseek_key") or "").strip() or \
            (os.environ.get("JIANER_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or "").strip() or "lm-studio"
        url = base.rstrip("/") + "/models"
        status, body = _http_get_json(url, {"Authorization": f"Bearer {key}"})
        return {"kind": kind, "url": url, "model": model, "status": status,
                "ok": status == 200, "detail": body}

    if kind == "gemini":
        base = str(others.get("gemini_base_url") or "https://generativelanguage.googleapis.com/").strip()
        key = str(others.get("gemini_key") or "").strip() or \
            (os.environ.get("JIANER_GEMINI_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip()
        if not key:
            return {"kind": kind, "url": base, "status": 0, "ok": False, "detail": "没有配置 Gemini Key"}
        url = base.rstrip("/") + f"/v1beta/models?key={key}"
        status, body = _http_get_json(url)
        return {"kind": kind, "url": base, "model": others.get("gemini_model") or "",
                "status": status, "ok": status == 200, "detail": body}

    if kind == "openai":
        key = str(others.get("openai_key") or "").strip() or \
            (os.environ.get("JIANER_OPENAI_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
        if not key:
            return {"kind": kind, "url": "https://api.openai.com/v1", "status": 0,
                    "ok": False, "detail": "没有配置 OpenAI Key"}
        url = "https://api.openai.com/v1/models"
        status, body = _http_get_json(url, {"Authorization": f"Bearer {key}"})
        return {"kind": kind, "url": url, "status": status, "ok": status == 200, "detail": body}

    if kind == "edge_tts":
        data = list_edge_voices()
        return {"kind": kind, "url": "edge-tts", "status": 200 if data.get("edge") else 0,
                "ok": bool(data.get("edge")) or bool(data.get("builtin")),
                "detail": f"在线声线 {len(data.get('edge') or [])} 个；内置 {len(data.get('builtin') or [])} 个。"
                          + (data.get("error") or "")}

    raise CoreError(f"不认识的后端类型 {kind!r}")


def ensure_token(env_override: str | None = None) -> str:
    tok = (env_override or os.environ.get("FOX_CONSOLE_TOKEN") or "").strip()
    if tok:
        return tok
    data = load_console_json()
    tok = str(data.get("token") or "").strip()
    if not tok:
        tok = secrets.token_urlsafe(24)
    data["token"] = tok
    data.setdefault("port", 5010)
    data["bot_dir"] = str(BOT_DIR)
    data["updated_at"] = now_str()
    atomic_write_bytes(CONSOLE_JSON, dump_json_bytes(data))
    return tok
