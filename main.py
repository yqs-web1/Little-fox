# -*- coding: utf-8 -*-

# ---- 单实例锁（文件 O_EXCL 原子创建 + PID 存活校验，真正无竞态） ----
# 必须在最顶部、任何 import 之前执行，防止重复双击启动导致多实例抢答/重复发语音
import sys, os, ctypes, errno

# 本地(127.0.0.1/localhost)连接不走代理，避免 OneBot WS 被系统/环境代理拦截(502)
for _np in ("no_proxy", "NO_PROXY"):
    _cur = os.environ.get(_np, "")
    if "127.0.0.1" not in _cur:
        os.environ[_np] = (_cur + ",127.0.0.1,localhost").strip(",")

_LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "jianer.lock")

def _pid_alive(pid: int) -> bool:
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False

while True:
    try:
        _fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(_fd, "w") as _f:
            _f.write(str(os.getpid()))
        break
    except OSError:
        # 锁文件已存在：若持有者仍活着则退出，否则清理残留后重试
        try:
            with open(_LOCK_PATH, "r") as _f:
                _pid = int((_f.read() or "0").strip() or 0)
        except Exception:
            _pid = 0
        if _pid and _pid_alive(_pid):
            print(f"检测到机器人已在运行（PID {_pid}），为避免多实例重复回复，本次启动已取消。")
            sys.exit(0)
        try:
            os.remove(_LOCK_PATH)
        except Exception:
            pass

import atexit
atexit.register(lambda: os.path.exists(_LOCK_PATH) and os.remove(_LOCK_PATH))
# ---------------------------------------------------------------------------

# 小狐狸 QQ 机器人项目（基于 Jianer QQ Bot / HypeR_bot 框架二次修改）
# Made by 思锐工作室
# link: https://github.com/SRInternet-Studio/Jianer_QQ_bot/

# import Tools functions
from Tools.tools import * 
print(title() + "\nWelcome to Jianer QQ Bot, Starting Kernal now...", end="\r") 

from Tools.GoogleAI import Context
from Tools.Sanitizer_Tools import sanitize_for_tts
from Tools.tts_local import speak_and_send, parse_voice_args, list_voices, _resolve_voice, EDGE_VOICES, GENDER_DEFAULT, speakable_text
from AI_bot.AIKernal import AIKernal
from AI_bot.ContextManager import ContextManager, user_lists

import prerequisites.prerequisite as presets_tool

# import requirements
import faulthandler
faulthandler.enable()

import sys, os, asyncio, traceback, threading, base64
import importlib.util
import unicodedata   # NFKC 归一化：把全角字母/空格转半角，避免命令"看起来一样"却匹配失败
import inspect
import random, json
import uuid, re
import emoji
import time, datetime

# import framework
os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))
from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())
from Tools import ai_backend  # 本地/云端 AI 后端解析（必须在 Configurator.cm 初始化之后导入）
from Tools.reply import reply_send, quote_target  # 统一回复出口：私聊自动带引用
from Tools import presence  # 私聊「正在输入」气泡（NapCat set_input_status）
from Tools import bubble  # 私聊「正在思考」占位气泡（延迟出现 + 回复前撤回）
from Hyper import Listener, Events, Logger, Manager, Segments
from Hyper.Utils import Logic
from Hyper.Events import *

config = Configurator.cm.get_cfg()
reminder: str = config.others["reminder"]
bot_name = config.others["bot_name"] #星·简
bot_name_en = config.others["bot_name_en"] #Shining girl
bot_owner = config.owner[0]
ONE_SLOGAN: str = config.others["slogan"]
CONFUSED_WORD: str = config.others.get("confused_words", 
    "不能这么做！那是一块丞待开发的禁地，可能很危险，{bot_name}很胆小……꒰>﹏< ꒱")

# ---- 新用户同意询问：每位用户首次私聊只问一次"是否愿意"，之后记住不再问 ----
CONSENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "consent_asked.json")

def load_consent() -> set:
    try:
        with open(CONSENT_FILE, "r", encoding="utf-8") as _f:
            return set(json.load(_f))
    except Exception:
        return set()

def save_consent(asked: set):
    try:
        # 同样改为原子替换：并发写不会再产生截断 JSON
        _atomic_write_json(CONSENT_FILE, sorted(asked))
    except Exception as _e:
        print(f"保存同意记录失败: {_e}")

ROOT_User: list = config.others["ROOT_User"]
Super_User: list = []
Manage_User: list = []

logger = Logger.Logger()
logger.set_level(config.log_level)
version_name = "3.1 - 𝑵𝒆𝒙𝒕 𝑹𝒆𝒍𝒆𝒂𝒔𝒆"

stop_working = False
Wait_for_add_in = False

cooldowns = {}
cooldowns1 = {}
second_start = time.time()
in_timing = False
generating = False
emoji_send_count: datetime = None
emoji_plus_one_off = False
self_service_titles = False

# AI Settings
EnableNetwork = config.others.get("default_mode", "Ds")

# ============ 需求④⑤ 新增：用户档案 + 模型三级解析 ============
from Tools import userstore as _userstore
from Tools import model_registry as _models

try:
    _userstore.init()
except Exception as _e:
    print(f"[启动] 用户库初始化失败（档案功能降级）：{_e}")


def model_for(event) -> str:
    """按 用户 > 群 > 全局 解析当前该用哪个模型（需求⑤）。"""
    try:
        return _models.scoped_model_id(getattr(event, "user_id", None),
                                       getattr(event, "group_id", None))
    except Exception as _e:
        print(f"[模型] 作用域解析失败，回落默认_mode：{_e}")
        return ""


def profile_block(event, display_name: str = "") -> str:
    """生成用户画像文本，拼进 system prompt，让机器人"认得"这个人（需求④）。"""
    try:
        return _userstore.render_for_prompt(getattr(event, "user_id", None), display_name)
    except Exception:
        return ""


def touch_user(event, kind: str = "text", text: str = "", is_ai: bool = False):
    """记录一次互动并加好感度（需求④）。任何异常都不影响主流程。"""
    try:
        uid = getattr(event, "user_id", None)
        if uid is None:
            return
        _userstore.touch(uid, group_id=getattr(event, "group_id", None),
                         kind=kind, char_count=len(text or ""), is_ai=is_ai)
        if not is_ai:
            delta = 2 if kind == "voice" else 1
            _userstore.add_affinity(uid, delta,
                                    daily_cap=int(config.others.get("affinity_daily_cap", 30)))
    except Exception as _e:
        print(f"[用户库] 记录互动失败（忽略）：{type(_e).__name__}: {_e}")
sys_prompt = ""
cmc = ContextManager() # 上下文管理器

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def _set_ai_backend(backend: str) -> bool:
    """切换 AI 后端（local / cloud）：写回 config.json 并同步到运行中的 Configurator。

    写盘是为了下次启动仍生效；同步内存是为了当前进程立刻生效、无需重启。
    backend 非法值直接忽略，防止把配置写坏。
    """
    backend = str(backend).strip().lower()
    if backend not in ("local", "cloud"):
        return False
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("Others", {})["ai_backend"] = backend
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"sys: 写 config.json 失败: {e}")
        return False
    try:
        Configurator.cm.get_cfg().others["ai_backend"] = backend
    except Exception as e:
        print(f"sys: 同步内存配置失败: {e}")
        return False
    print(f"sys: AI backend -> {backend}")
    return True

# ---- 语音对话模式（按会话隔离：私聊=用户，群聊=群；默认关闭）----
# 首聊 / 超过 VOICE_IDLE_SECONDS 未聊 → 主动询问是否开启；开启后该会话 AI 回复只发语音、不发文字。
VOICE_PREFS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_prefs.json")
VOICE_IDLE_SECONDS = 1800  # 30 分钟：超过这么久没聊天，下一次视为“重新首次”，会再次询问

# 并发修复：voice_prefs / consent 原来是「整文件读-改-写」且无锁。
# Hyper 每条消息一个线程，多人同时改设置会丢写；更糟的是写到一半被另一线程读到，
# load 的 except 会静默返回 {}，表现为「设置自己变回默认」。
# 加锁 + 先写临时文件再 os.replace 原子替换，保证任何时刻磁盘上都是完整 JSON。
_VOICE_IO_LOCK = threading.Lock()


def _atomic_write_json(path: str, obj) -> None:
    with _VOICE_IO_LOCK:
        _tmp = path + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as _f:
            json.dump(obj, _f, ensure_ascii=False, indent=2)
        os.replace(_tmp, path)

def load_voice_prefs():
    try:
        with open(VOICE_PREFS_FILE, "r", encoding="utf-8") as _f:
            _d = json.load(_f)
            return (_d.get("on") or {}), (_d.get("seen") or {}), (_d.get("voice") or {})
    except Exception:
        return {}, {}, {}

def save_voice_prefs(on: dict, seen: dict, voice: dict):
    try:
        _atomic_write_json(VOICE_PREFS_FILE, {"on": on, "seen": seen, "voice": voice})
    except Exception as _e:
        print(f"保存语音偏好失败: {_e}")

_voice_on, _voice_seen, _voice_sel = load_voice_prefs()  # on/seen/voice: chat_key->值
_voice_pending = {}  # 已发送语音询问、等待对方“开/关”答复（仅内存态，重启即丢）

def get_chat_key(event):
    if getattr(event, "group_id", None) is not None:
        return f"g{event.group_id}"
    return f"u{event.user_id}"

def voice_on_for(event) -> bool:
    return bool(_voice_on.get(get_chat_key(event), False))

def set_voice_on(event, value: bool):
    _voice_on[get_chat_key(event)] = bool(value)
    save_voice_prefs(_voice_on, _voice_seen, _voice_sel)

def voice_sel_for(event):
    """本会话选定的声线（友好名/性别/音效/随机/情绪/完整 id），未选则返回默认女声「小艺」。"""
    _v = _voice_sel.get(get_chat_key(event)) or "小艺"
    # 防御：存储的声线若已失效，回退默认，避免合成静默失败。
    # 需求③：现在还要认得「音效 / 音色+音效 / 随机 / 情绪」这些新写法，否则一选就被重置。
    try:
        from Tools.tts_voices import is_voice_name, is_fx_name, split_selection
        _a, _b = split_selection(_v)
        _a_ok = bool(_a) and (is_voice_name(_a) or str(_a).startswith(("zh-", "ja-", "en-", "ko-")))
        if not (_a_ok or (_b and is_fx_name(_b))) or (_b and not is_fx_name(_b)):
            _v = "小艺"
    except Exception:
        if _v not in EDGE_VOICES and _v not in GENDER_DEFAULT and not str(_v).startswith("zh-CN-"):
            _v = "小艺"
    return _v

def set_voice_sel(event, name: str):
    _voice_sel[get_chat_key(event)] = name
    save_voice_prefs(_voice_on, _voice_seen, _voice_sel)

def mark_voice_seen(event):
    _voice_seen[get_chat_key(event)] = time.time()
    save_voice_prefs(_voice_on, _voice_seen, _voice_sel)

def is_voice_fresh(event) -> bool:
    """首聊（从未记录）或 超过 VOICE_IDLE_SECONDS 未聊 → 视为需重新询问"""
    _last = _voice_seen.get(get_chat_key(event))
    if _last is None:
        return True
    return (time.time() - float(_last)) > VOICE_IDLE_SECONDS

def classify_voice_choice(msg: str):
    """返回 'on' / 'off' / None（None=不是语音开关选择，按普通消息处理）"""
    m = (msg or "").strip()
    if not m:
        return None
    has_voice = ("语音" in m)
    if has_voice:
        # 先判拒绝（否定词优先，避免“不要”里的“要”、“你好呀”里的“好呀”被误判为接受）
        if any(k in m for k in ("不要", "不用", "别", "否", "关", "停", "关掉")):
            return "off"
        if any(k in m for k in ("开", "要", "需", "启", "用", "可以")):
            return "on"
        return None
    # 不含“语音”的极短语：直接 yes/no
    m2 = m.replace(" ", "").replace("，", "").replace("。", "").replace("~", "")
    if len(m2) <= 4:
        if m2 in ("要", "开", "好", "可以", "行", "愿意", "嗯", "好的", "yes", "y"):
            return "on"
        if m2 in ("不", "不用", "不要", "关", "否", "no", "n", "别", "算了"):
            return "off"
    return None

VOICE_OFFER_TEXT = (
    "🎙️ 要不要开启【语音对话】呀？开启后我会用语音念出回复、不再另发文字～\n"
    "回复「语音开」开启，或回复「语音关」保持纯文字；也可以直接忽略，以后随时发 ~语音开 再开。"
)

async def _voice_reply(actions, Manager, Segments, event, text):
    """语音相关提示统一发送：群聊发群、私聊发私聊（私聊按配置带引用回复）。"""
    await reply_send(actions, Manager, Segments, event, text)

def is_explicit_voice_command(user_message, reminder=""):
    """判断是否为「带命令前缀的明确语音指令」（~语音开/关、~音色、~语音、~更改TTS状态）。
    仅当消息确实带 reminder 前缀时才算，避免把用户回答问题时的裸“开/关/好”误判为指令。"""
    m = (user_message or "").strip()
    if not reminder or reminder not in m:
        return False
    _i = m.find(reminder)
    _cmd = unicodedata.normalize("NFKC", m[_i + len(reminder):]).strip()
    if not _cmd:
        return False
    if _cmd.startswith("语音") or _cmd.startswith("音色"):
        return True
    if _cmd == "更改TTS状态":
        return True
    return False


async def maybe_voice_offer(actions, Manager, Segments, event, user_message, reminder="") -> bool:
    """首聊/久未聊主动询问语音；并响应待确认的开/关选择。返回 True 表示已处理（调用方应 return）。

    关键修复：若用户发的是「带前缀的明确语音指令」（~语音开/关、~音色、~语音、~更改TTS状态），
    不要被主动询问拦截，直接交给下方指令逻辑处理，避免“开关要发两次才生效”的问题。"""
    _vk = get_chat_key(event)
    _is_cmd = is_explicit_voice_command(user_message, reminder)
    if _voice_pending.get(_vk):
        if _is_cmd:
            # 对方发的是明确指令，并非对询问的回复 → 清掉待确认，交给指令处理
            _voice_pending.pop(_vk, None)
            mark_voice_seen(event)
            return False
        _choice = classify_voice_choice(user_message)
        if _choice:
            set_voice_on(event, _choice == "on")
            _voice_pending.pop(_vk, None)
            await _voice_reply(actions, Manager, Segments, event,
                               "🔊 好耶，已为你开启语音对话！之后我用语音念回复，不另发文字啦~" if _choice == "on"
                               else "好的，保持纯文字聊天~ 想开语音随时发 ~语音开 就行 (◕‿◕)")
            mark_voice_seen(event)
            return True
        else:
            _voice_pending.pop(_vk, None)  # 对方发的是正常内容，清掉待确认，继续正常处理
    if is_voice_fresh(event) and not voice_on_for(event):
        if _is_cmd:
            # 一上来就发明确语音指令，直接交给指令处理，不重复询问
            mark_voice_seen(event)
            return False
        mark_voice_seen(event)
        _voice_pending[_vk] = True
        await _voice_reply(actions, Manager, Segments, event, VOICE_OFFER_TEXT)
        return True
    mark_voice_seen(event)
    return False


print(" " * 114, end="\r") # Staring Completed

# Plugin like
PLUGIN_FOLDER = "plugins"
if not os.path.exists(PLUGIN_FOLDER):
    os.makedirs(PLUGIN_FOLDER)

loaded_plugins = []
disabled_plugins = []
failed_plugins = []
plugins_help = ""

# 配置文件名
CONFIG_FILE = presets_tool.CONFIG_FILE
# 预设文件存放目录
PRESET_DIR = presets_tool.PRESET_DIR
# 默认预设名称
NORMAL_PRESET = presets_tool.NORMAL_PRESET

# 插件加载器 NEXT 3
def load_plugins():
    global loaded_plugins, disabled_plugins, failed_plugins, plugins_help, reminder, bot_name, PLUGIN_FOLDER
    plugins = []
    plugins_help = ""

    loaded_plugins.clear()
    disabled_plugins.clear()
    failed_plugins.clear()

    for filename in os.listdir(PLUGIN_FOLDER):
        module_name = filename  # Folder name as module name
        print(f"check file or directory: {filename}")

        if filename == "__pycache__":
            print("Directory __pycache__ not load.")
            continue

        # 检查是否禁用
        if filename.startswith("d_"):
            disabled_plugins.append(module_name)
            continue

        # 处理目录形式插件
        plugin_path = os.path.join(PLUGIN_FOLDER, filename)  # Full plugin path
        if os.path.isdir(plugin_path):
            setup_file = os.path.join(plugin_path, "setup.py")
            if os.path.exists(setup_file):
                try:
                    # Load setup.py
                    unique_module_name = f"{module_name}_{uuid.uuid4().hex}"  # Generate unique module name
                    spec = importlib.util.spec_from_file_location(unique_module_name, setup_file)
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[unique_module_name] = module
                    spec.loader.exec_module(module)
                    print(f"Loaded setup.py from folder plugin: {module_name}")

                    # Verify plugin
                    if hasattr(module, 'TRIGGHT_KEYWORD') and hasattr(module, 'on_message'):
                        if isinstance(module.TRIGGHT_KEYWORD, str):
                            plugins.append(module)  # Add module
                            loaded_plugins.append(unique_module_name) 
                            if hasattr(module, 'HELP_MESSAGE'):
                                if isinstance(module.HELP_MESSAGE, str):
                                    for help_message in [line.strip() for line in module.HELP_MESSAGE.splitlines() if line.strip()]:
                                        plugins_help += f"\n       {help_message}"

                            print(f"已加载插件: {unique_module_name} (关键词: {module.TRIGGHT_KEYWORD})")
                        else:
                            failed_plugins.append(f"{module_name} (TRIGGHT_KEYWORD 必须是字符串)")
                    else:
                        failed_plugins.append(f"{module_name} (缺少 TRIGGHT_KEYWORD：触发标识符 或 on_message：触发函数后端)")

                except FileNotFoundError as e:
                    failed_plugins.append(f"{module_name} (文件未找到: {e})")
                    print(f"加载插件 {unique_module_name} 失败，是因为: {e}")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]
                except ImportError as e:
                    failed_plugins.append(f"{module_name} (导入错误: {e})")
                    print(f"加载插件 {unique_module_name} 失败，是因为: \n{traceback.format_exc()}\n")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]
                except Exception as e:
                    failed_plugins.append(f"{module_name} (其他错误: {str(e)})")
                    print(f"加载插件 {unique_module_name} 失败: \n{traceback.format_exc()}\n")
                    if unique_module_name in sys.modules:
                        del sys.modules[unique_module_name]  # Cleanup

            else:
                print(f"目录 {filename} 中缺少 setup.py 文件")
                failed_plugins.append(f"{filename} (入口错误: 缺少 setup.py 文件)")

        # 处理文件形式插件
        elif filename.endswith(".py") or filename.endswith(".pyw"):
            module_name = filename[:-3] if filename.endswith(".py") else filename[:-4]

            # 检查是否禁用
            if filename.startswith("d_"):
                disabled_plugins.append(str(module_name)[3:])
                continue

            # 生成唯一的模块名
            unique_module_name = f"{module_name}_{uuid.uuid4().hex}"

            try:
                # 检查模块是否已经加载
                if unique_module_name in sys.modules:
                    print(f"模块 {unique_module_name} 已经加载，跳过")
                    continue

                # 创建模块规范
                spec = importlib.util.spec_from_file_location(unique_module_name, os.path.join(PLUGIN_FOLDER, filename))
                module = importlib.util.module_from_spec(spec)
                sys.modules[unique_module_name] = module  # 添加到 sys.modules
                spec.loader.exec_module(module)

                # 验证模块是否符合插件规范
                if hasattr(module, 'TRIGGHT_KEYWORD') and hasattr(module, 'on_message'):
                    if isinstance(module.TRIGGHT_KEYWORD, str):
                        plugins.append(module)  # 重要：把整个模块全tm加入到列表
                        loaded_plugins.append(unique_module_name)
                        if hasattr(module, 'HELP_MESSAGE'):
                            if isinstance(module.HELP_MESSAGE, str):
                                for help_message in [line.strip() for line in module.HELP_MESSAGE.splitlines() if line.strip()]:
                                    plugins_help += f"\n       {help_message}"

                        print(f"已加载插件: {unique_module_name} (关键词: {module.TRIGGHT_KEYWORD})")
                    else:
                        failed_plugins.append(f"{module_name} (TRIGGHT_KEYWORD 必须是字符串)")
                else:
                    failed_plugins.append(f"{module_name} (缺少 TRIGGHT_KEYWORD：触发标识符 或 on_message：触发函数后端)")

            except FileNotFoundError as e:
                failed_plugins.append(f"{module_name} (文件未找到: {e})")
                print(f"加载插件 {unique_module_name} 失败，原因是: {e}")
                if unique_module_name in sys.modules:
                    del sys.modules[unique_module_name]
            except ImportError as e:
                failed_plugins.append(f"{module_name} (导入错误: {e})")
                print(f"加载插件 {unique_module_name} 失败，原因是: \n{traceback.format_exc()}\n")
                if unique_module_name in sys.modules:
                    del sys.modules[unique_module_name]
            except Exception as e:
                failed_plugins.append(f"{module_name} (其他错误: {str(traceback.format_exc())})")
                print(f"加载插件 {unique_module_name} 失败: \n{traceback.format_exc()}\n")
                if unique_module_name in sys.modules:
                    del sys.modules[unique_module_name]  # Cleanup

        else:
            print(f"跳过非插件文件或目录: {filename}")

    print(f"成功加载 {len(loaded_plugins)} 个插件")
    return plugins

plugins = load_plugins() #在任何操作执行之前加载插件

# 插件运行器 NEXT 3
async def execute_plugins(isAny: bool, **main_context) -> bool: # 接受 main.py 的上下文，也就是所有的关键字
    has_plugin = False
    _error_notified = False  # 插件出错只提示用户一次，避免一个坏插件刷屏
    user_message = main_context["order"] if "order" in main_context else ""

    # 修复：长关键词优先匹配。原来按目录顺序匹配，短关键词会遮蔽长关键词
    # （典型：CheckAccount 的「开」先命中并 return True，导致 CheckGroup 的「开群」变成死代码）。
    _ordered_plugins = sorted(
        plugins, key=lambda _m: len(str(getattr(_m, "TRIGGHT_KEYWORD", "") or "")), reverse=True
    )
    for plugin_module in _ordered_plugins:
        # 匹配前做 NFKC 归一化 + 去空格：全角字母/空格、漏打空格都能命中
        _nm_msg = unicodedata.normalize("NFKC", f"{reminder}{user_message}").replace(" ", "")
        _nm_key = unicodedata.normalize("NFKC", f"{reminder}{plugin_module.TRIGGHT_KEYWORD}").replace(" ", "")
        if (not isAny and _nm_key in _nm_msg) or (isAny and plugin_module.TRIGGHT_KEYWORD == "Any"):
            try:
                # 动态构建参数
                on_message_params = inspect.signature(plugin_module.on_message).parameters
                kwargs = {}
                for param_name, param in on_message_params.items():
                    if param_name in main_context:
                        kwargs[param_name] = main_context[param_name]  # 从 main_context 获取
                    elif param.default is not inspect.Parameter.empty:
                        pass  # 使用默认值
                    else:
                        raise ValueError(f'''插件 {plugin_module.__name__} 未提供参数 {param_name} ：
无法在所有上下文中找到具有该标识符的变量且该标识符不具有默认值，这样的变量可能在定义前被使用或本就没有定义。
如果您是开发者，请在 main.py 中提供此值。如果您是用户，请忽略此消息并通知管理员及时地修复。
详见 https://github.com/SRInternet-Studio/Jianer_QQ_bot/wiki''')

                response = await plugin_module.on_message(**kwargs)  # 传递 event 和动态参数

                if response is not None:
                    if response == True:
                        has_plugin = True
                        break

            except Exception as e:
                print(f"\n插件 {plugin_module.__name__} 执行出错，是因为: \n{traceback.format_exc()}")
                if isAny:
                    # "Any" 类插件对每条消息都会执行，它出错绝不能拦下所有消息
                    continue
                # 修复：原来这里只把 has_plugin 置 True 就结束 —— 关键词明明命中了，
                # 用户却一个字都收不到，只有日志里有 traceback。现在补一句可见的提示。
                has_plugin = True
                if not _error_notified:
                    _error_notified = True
                    try:
                        _act = main_context.get("actions")
                        _Mgr = main_context.get("Manager")
                        _Seg = main_context.get("Segments")
                        _ev = main_context.get("event")
                        if _act and _Mgr and _Seg and _ev:
                            _notice = _Mgr.Message(_Seg.Text(
                                f"插件 {plugin_module.__name__} 执行出错了，这次没能处理你的消息 (｡•́︿•̀｡)"))
                            if getattr(_ev, "group_id", None) is not None:
                                await _act.send(group_id=_ev.group_id, message=_notice)
                            else:
                                await _act.send(user_id=_ev.user_id, message=_notice)
                    except Exception as _e2:
                        print(f"插件出错提示发送失败: {_e2}")
    
    return has_plugin

def load_blacklist():
    try:
        with open("blacklist.sr", "r", encoding="utf-8") as f:
            blacklist115 = set(line.strip() for line in f)  # 这里是集合
        return blacklist115
    except FileNotFoundError:
        return set() 
             
def has_emoji(s: str) -> bool: # emoji +1 功能
    # 判断找到的 emoji 数量是否为 1 并且字符串的长度大于等于 1
    return emoji.emoji_count(s) == 1 and len(s) == 1

def timing_message(actions: Listener.Actions):
    while True:
        if not os.path.isfile("timing_message.ini"):
            # 修复：原来这里直接 continue，是纯忙等（没有 sleep），
            # 该线程会在首个消息到达后 100% 占满一个 CPU 核心，且日志无任何提示。
            time.sleep(1)
            continue
        
        with open("timing_message.ini", "r", encoding="utf-8") as f:
            content = f.read().strip()
        
        if "⊕" in content:
            # 找到第一个换行符的位置
            first_newline_pos = content.find("\n")
            if first_newline_pos != -1:
                # 如果有换行，只在第一行查找⊕符号
                first_line = content[:first_newline_pos]
                remaining_lines = content[first_newline_pos:]
                if "⊕" in first_line:
                    time_part, message_part = first_line.split("⊕", 1)
                    # 合并消息部分和剩余行
                    full_message = message_part + remaining_lines
                else:
                    # 如果第一行没有⊕符号，使用整个内容作为消息
                    full_message = content
            else:
                # 如果没有换行，直接分割整个内容
                time_part, full_message = content.split("⊕", 1)
        else:
            # 如果没有⊕符号，使用整个内容作为消息
            full_message = content
            time_part = ""
        
        now = datetime.datetime.now()
        print(f"Current: {now.hour:02}:{now.minute:02}, target: {time_part}")
        if time_part and f"{now.hour:02}:{now.minute:02}" == time_part:
            print("send timing messages")
            asyncio.run(send_msg_all_groups(full_message, actions))
        
        time.sleep(60 - now.second)
        
async def send_msg_all_groups(text, actions: Listener.Actions, message: Manager.Message = None):
    echo = await actions.custom.get_group_list()
    result = Manager.Ret.fetch(echo)
    blacklist = load_blacklist()  # 必须在发送消息前加载黑名单
    print(f"sys: 群发 {result.data.raw}")
    for group in result.data.raw:
        group_id = str(group['group_id'])  # 将group_id转为字符串
        if group_id not in blacklist:  # 检查群组 ID 是否在黑名单中
            if message:
                await actions.send(group_id=group['group_id'], message=message)
            else:
                await actions.send(group_id=group['group_id'], message=Manager.Message(Segments.Text(text)))
            time.sleep(random.random()*3)
        else:
            print(f"群聊 {group_id} 在黑名单内，取消发送")


def Read_Settings():
    global Super_User, Manage_User
    
    def load_user_list(filename):
        if not os.path.exists(filename):
            with open(filename, 'w'):
                pass
            
        with open(filename, 'r') as f:
            return list({line.strip() for line in f if line.strip()})
    
    Super_User = load_user_list("Super_User.ini")
    Manage_User = load_user_list("Manage_User.ini")
    print(f'''————————————————
sys: User_Group loaded.
Super_User: {Super_User}
Manage_User: {Manage_User}
————————————————''')

def Write_Settings(s: list, m: list) -> bool:
    s = [item for item in s if item]
    m = [item for item in m if item]
    global Super_User, Manage_User
    su = ""
    for item in range(len(s)):
        su += s[item]
        if item != len(s) - 1:
            su += "\n"
    ma = ""
    for item in range(len(m)):
        ma += m[item]
        if item != len(m) - 1:
            ma += "\n"

    try:
        with open("Super_User.ini", "w") as f:
            f.write(su)
            f.close()
        with open("Manage_User.ini", "w") as f:
            f.write(ma)
            f.close()

        Super_User = s
        Manage_User = m

        return True
    except:
        return False

def _root_target():
    """取一个可用的 ROOT 通知目标：ROOT_User 里第一个非空项；没有则返回 None。"""
    for _u in ROOT_User:
        if str(_u).strip():
            return str(_u).strip()
    return None

async def notify_root(actions, Manager, Segments, text, *extra):
    """向 ROOT_User 发送管理员操作通知（extra 为随附的消息段，如群发时的图片）。

    ROOT_User 未配置（或还是 [""]）时静默跳过并打印提示：
    原来的 actions.send(user_id=ROOT_User[0], ...) 会朝空 QQ 号发送而抛异常，
    把同一条指令里排在后面的"用户可见回复"一起打断（例如添加黑名单会变成"添加失败"）。
    """
    _t = _root_target()
    if not _t:
        print("sys: ROOT_User 未配置，已跳过管理员通知（请在 config.json 的 Others.ROOT_User 填入你的 QQ 号）")
        return
    try:
        await actions.send(user_id=_t, message=Manager.Message(Segments.Text(text), *extra))
    except Exception as _e:
        print(f"sys: 向 ROOT_User({_t}) 发送通知失败：{_e}")

@Listener.reg
@Logic.ErrorHandler().handle_async
async def handler(event: Events.Event, actions: Listener.Actions) -> None:
    global in_timing, bot_name, bot_name_en, reminder, config, ONE_SLOGAN, CONFUSED_WORD, stop_working, Wait_for_add_in, version_name
    global Super_User, Manage_User, ROOT_User # 全局用户组
    global cmc, user_lists, sys_prompt, EnableNetwork # AI对话所必须
    ADMINS = Super_User + ROOT_User + Manage_User
    SUPERS = Super_User + ROOT_User
    AIbot = AIKernal(actions, config, bot_name, reminder)
    event.time_str = f"{datetime.datetime.now().hour:02}:{datetime.datetime.now().minute:02}:{datetime.datetime.now().second:02}"
    
    if stop_working:
        if ((user_id := getattr(event, "user_id", None)) and (message := getattr(event, "message", None)) 
            and str(message).startswith(reminder) and str(user_id) in ADMINS):
            stop_working = False
            if hasattr(event, "group_id"):
                await actions.send(
                    group_id=event.group_id,
                    message=Manager.Message(Segments.Text(f"{bot_name} 已从休眠中恢复 ♡=•ㅅ＜=)"))
                )
        else:
            print("sys: 触发停止运行事件")
            return

    if not in_timing:
        Read_Settings()
        in_timing = True
        thread = threading.Thread(target=timing_message, args=(actions,))
        thread.start()
        
    # 执行永久加载插件
    local_vars = globals().copy()
    local_vars.update(locals().copy())
    if await execute_plugins(True, **local_vars):
        return  # 只传递 event 作为位置参数
    
    if isinstance(event, Events.NotifyEvent): # 优先判断自定义事件
        if str(event.sub_type) == "poke" and int(event.target_id) == int(event.self_id): # 被戳一戳
            print(f"({event.user_id}) POKED")
            try:
                if event.group_id:
                    poke_result = await actions.custom.group_poke(group_id=event.group_id, user_id=event.user_id)
                    poke_result = Manager.Ret.fetch(poke_result).data.raw
                    if poke_result.get("status", "error") != "ok":
                        print(f"sys: 戳一戳失败 {poke_result}")
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(random.choice(config.others["poke_rejection_phrases"]))))
                elif event.user_id:
                    poke_result = await actions.custom.friend_poke(user_id=event.user_id)
                    poke_result = Manager.Ret.fetch(poke_result).data.raw
                    if poke_result.get("status", "error") != "ok":
                        print(f"sys: 戳一戳失败 {poke_result}")
                    await reply_send(actions, Manager, Segments, event,
                                     random.choice(config.others["poke_rejection_phrases"]))
            except KeyError:
                print("不接受戳一戳")
                
    if isinstance(event, Events.HyperListenerStartNotify):
        if os.path.exists("restart.temp"):
            with open("restart.temp", "r" ,encoding="utf-7") as f:
                group_id = f.read()
                f.close()
            os.remove("restart.temp")
            r_admin = f'''在 {event.time_str} QQ机器人已手动重启成功'''
            await notify_root(actions, Manager, Segments, r_admin) #管理员操作通知ROOT用户
            await actions.send(group_id=group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
欢迎! {bot_name} 已经重启成功！ 现在你可以发送 {reminder}帮助 来知道更多。''')))

    elif isinstance(event, Events.GroupMemberIncreaseEvent):
        if Wait_for_add_in:
            Wait_for_add_in = False
            return
        
        user = event.user_id
        welcome = f''' 加入{bot_name}的大家庭，{bot_name}是你最忠实可爱的伙伴噢o(*≧▽≦)ツ
随时和{bot_name}交流，你只需要在问题的前面加上 {reminder} 就可以啦！( •̀ ω •́ )✧
@{bot_name} 可以看看{bot_name}会做什么有趣的事情哦~o((>ω< ))o
祝你在{bot_name}的大家庭里生活愉快！♪(≧∀≦)ゞ☆'''
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={user}&spec=640"), Segments.Text("欢迎"), Segments.At(user), Segments.Text(welcome)))
        
    elif isinstance(event, Events.GroupMemberDecreaseEvent):
        user_nick = await get_user_nickname(event.user_id, Manager, actions)
        if user_nick:
            user_nick = f"@{user_nick} "
        else:
            user_nick = "有人又"

        text = f'''{user_nick}离开了{bot_name}的大家庭，{bot_name}好伤心o(TヘTo)……
大家一定要记得多来陪{bot_name}玩玩ヾ(•ω•`)o'''
        print(f"group: {event.user_id} 已离开群聊 {event.group_id}")
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(text)))

    elif isinstance(event, Events.GroupAddInviteEvent):
      keywords: list = config.others["Auto_approval"]
      cleaned_text = event.comment.strip().lower()

      for keyword in keywords:
        processed_keyword = keyword.strip().lower()
        if processed_keyword in cleaned_text: 
            try:
                user = event.user_id
                print(f"group: {await get_user_nickname(user, Manager, actions)} 的入群回答 {processed_keyword} 符合正确答案，已准许入群 {event.group_id}")
                await actions.set_group_add_request(flag=event.flag, sub_type=event.sub_type, approve=True, reason="")
                Wait_for_add_in = True
                welcome = f'''{await get_user_nickname(user, Manager, actions)} 的答案正确，欢迎加入{bot_name}的大家庭！o(*≧▽≦)ツ
随时和{bot_name}交流，只需在问题的前面加上 {reminder} 就可以啦！( •̀ ω •́ )✧
@{bot_name} 可以看看{bot_name}会做什么有趣的事情哦~o((>ω< ))o
祝你在{bot_name}的大家庭里生活愉快！♪(≧∀≦)ゞ☆'''  
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(f"http://q2.qlogo.cn/headimg_dl?dst_uin={user}&spec=640"), Segments.Text(welcome)))
                break
            except:
                traceback.print_exc()
          
    # elif isinstance(event, Events.FriendAddEvent):
    #     print("sys: 同意好友")
    #     await actions.custom.set_friend_add_request(flag=event.flag, approve=True, reason="")

    elif isinstance(event, Events.PrivateMessageEvent):
        # 私聊「对方正在输入」气泡：进分支就亮，长回复期间由后台协程每 4 秒刷新一次。
        # 仅私聊有效（NapCat 的 set_input_status 只实现了 C2C）；失败会静默降级，不影响回复。
        presence.start_typing(
            actions, event,
            enabled=bool(config.others.get("private_typing_bubble", True)))
        # 私聊「正在思考」占位气泡：延迟 delay 秒才发出（秒回不留痕迹），
        # 任何回复出口（reply_send / AIKernal / 语音）发送前会撤回它。
        bubble.begin(
            actions, Manager, Segments, event,
            enabled=bool(config.others.get("thinking_bubble", True)),
            delay=float(config.others.get("thinking_bubble_delay", 1.5)),
            text=str(config.others.get("thinking_bubble_text", "...")),
            max_seconds=float(config.others.get("thinking_bubble_max", 60)))
        # 新用户同意询问：每位用户首次私聊先问一句"是否愿意"，问过即记住、不再重复问
        _asked = load_consent()
        if str(event.user_id) not in _asked:
            _asked.add(str(event.user_id))
            save_consent(_asked)
            await reply_send(actions, Manager, Segments, event,
                f"嗨，我是{bot_name}～在陪你聊天之前先确认一下：你愿意让我这样陪你聊聊天吗？"
                f"愿意的话随便回我一句就好，之后咱们就正常聊啦 (｡･ω･｡)")
            return
        event_user = await get_user_nickname(event.user_id, Manager, actions)
        await _transcribe_voice(event, Segments)
        user_message, order = str(event.message).strip(), ""
        # 语音对话主动询问（首聊/久未聊）或响应待确认选择；返回 True 表示已处理
        if await maybe_voice_offer(actions, Manager, Segments, event, user_message, reminder):
            return
        sys_prompt = presets_tool.gen_presets(event.user_id, bot_name, bot_name_en, event_user)
        # 需求④：把该用户的档案摘要拼进 system prompt，让机器人认得他
        _pb = profile_block(event, event_user)
        if _pb:
            sys_prompt = f"{sys_prompt}\n\n{_pb}"
        touch_user(event, kind="text", text=user_message)
        presets = presets_tool.read_presets()
        if user_message.startswith(reminder):
            order_i = user_message.find(reminder)
            if order_i != -1:
                order = unicodedata.normalize("NFKC", user_message[order_i + len(reminder):]).strip()
                print(f"({event_user}) ORDER: {repr(order)}")

            if "帮助" == order or "用户帮助" == order:
                content = help_message(event)
                await reply_send(actions, Manager, Segments, event, content)
                return
            elif "角色扮演" == order:
                prerequisites_info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
{presets_tool.list_presets(presets, presets_tool.current_preset, reminder)}

发送相应的关键词，{bot_name}会尽力扮演不同角色和你交流哒！⌯>ᴗoᴗ⌯ .ᐟ.ᐟ
————————————————————
若您要管理这些角色，请前往群聊中发送相关指令哦o((>ω< ))o"""

                await reply_send(actions, Manager, Segments, event, prerequisites_info)
                return
            elif order in ("语音开", "开启语音", "语音对话开"):
                set_voice_on(event, True)
                await _voice_reply(actions, Manager, Segments, event, "🔊 已开启语音对话！之后我的回复会用语音念出来（不再另发文字）~")
                return
            elif order in ("语音关", "关闭语音", "语音对话关"):
                set_voice_on(event, False)
                await _voice_reply(actions, Manager, Segments, event, "已关闭语音对话，恢复纯文字回复~ 想开随时发 ~语音开")
                return
            elif order.startswith("音色"):
                # 需求③：~音色（列表）/ ~音色 晓晓 / ~音色 男生 / ~音色 花栗鼠 /
                #          ~音色 晓晓+花栗鼠 / ~音色 随机 / ~音色 情绪 / ~音色 检测
                _name = order[len("音色"):].strip()
                from Tools.tts_voices import is_voice_name, is_fx_name, split_selection
                if _name in ("检测", "探活", "test"):
                    import Tools.tts_health as _th
                    _res = await asyncio.to_thread(_th.probe_all)
                    _ok = sum(1 for v in _res.values() if v[0])
                    await _voice_reply(actions, Manager, Segments, event,
                                       f"🎧 音色探活完成：{_ok}/{len(_res)} 可用\n{_th.summary()}")
                elif not _name or _name in ("列表", "帮助", "?"):
                    await _voice_reply(actions, Manager, Segments, event,
                                       "🎙️ 本会话当前声线：" + voice_sel_for(event) + "\n" + list_voices()
                                       + f"\n用法：{reminder}音色 晓晓 ｜ {reminder}音色 花栗鼠 ｜ "
                                         f"{reminder}音色 晓晓+花栗鼠 ｜ {reminder}音色 随机 ｜ "
                                         f"{reminder}音色 情绪 ｜ {reminder}音色 检测")
                else:
                    _v, _fx = split_selection(_name)
                    _v_ok = (not _v) or is_voice_name(_v) or str(_v).startswith(("zh-", "ja-", "en-", "ko-"))
                    _fx_ok = (not _fx) or is_fx_name(_fx)
                    if _v_ok and _fx_ok and (_v or _fx):
                        set_voice_sel(event, _name)
                        _tail = f"\n（音效：{_fx}）" if _fx else ""
                        await _voice_reply(actions, Manager, Segments, event,
                                           f"🔊 已把本会话声线设为「{_name}」~ 之后语音对话和朗读都会用这个。{_tail}")
                    else:
                        await _voice_reply(actions, Manager, Segments, event,
                                           f"😶 没找到「{_name}」这个音色/音效哦。\n" + list_voices())
                return
            elif order.startswith("模型"):
                # 需求⑤：~模型（列表）/ ~模型 3（序号）/ ~模型 推理（别名）
                #         ~模型 群 <名>（本群默认，管理员）/ ~模型 全局 <名>（ROOT）/ ~模型 状态
                _rest = order[len("模型"):].strip()
                _cur = model_for(event)
                _entry = _models.find(_cur)
                _cur_txt = _models.render_entry(_entry) if _entry else (_cur or "未配置")
                if not _rest or _rest in ("列表", "帮助", "?"):
                    await reply_send(actions, Manager, Segments, event,
                        f"🧠 你当前的模型：{_cur_txt}\n{_models.listing(_cur)}\n\n"
                        f"用法：{reminder}模型 3 ｜ {reminder}模型 推理 ｜ "
                        f"{reminder}模型 群 <名> ｜ {reminder}模型 全局 <名> ｜ {reminder}模型 状态")
                elif _rest.startswith("状态"):
                    _models.probe()
                    await reply_send(actions, Manager, Segments, event,
                                     "各供应商健康状态：\n" + _models.health_report())
                elif _rest.startswith("群"):
                    _name = _rest[1:].strip()
                    if str(event.user_id) not in ADMINS:
                        await reply_send(actions, Manager, Segments, event, CONFUSED_WORD.format(bot_name=bot_name))
                    elif getattr(event, "group_id", None) is None:
                        await reply_send(actions, Manager, Segments, event, "群默认只能在群聊里设置哦～")
                    else:
                        _ok, _msg = _models.set_group_model(event.group_id, _name)
                        await reply_send(actions, Manager, Segments, event,
                                         ("✅ 本群默认模型已设为 " + _msg) if _ok else ("😶 " + _msg))
                elif _rest.startswith("全局"):
                    _name = _rest[2:].strip()
                    if str(event.user_id) not in ROOT_User:
                        await reply_send(actions, Manager, Segments, event, CONFUSED_WORD.format(bot_name=bot_name))
                    else:
                        _ok, _msg = _models.set_global_model(_name)
                        await reply_send(actions, Manager, Segments, event,
                                         ("✅ 全局默认模型已设为 " + _msg) if _ok else ("😶 " + _msg))
                elif _rest in ("自动", "auto"):
                    _userstore.pref_set(event.user_id, "model", "auto")
                    await reply_send(actions, Manager, Segments, event,
                                     "🤖 已为本会话开启智能路由：短问题走快模型、推理类走推理模型、带图走多模态。")
                else:
                    _ok, _msg = _models.set_user_model(event.user_id, _rest)
                    if _ok:
                        # 换了模型必须重建上下文：不同模型的历史格式不兼容
                        cmc.del_context(event.user_id, event.group_id)
                    await reply_send(actions, Manager, Segments, event,
                                     (f"✅ 已切到 {_msg}\n（只影响你自己，上下文已重置）") if _ok else ("😶 " + _msg))
                return
            elif order.startswith("用户"):
                # 需求④：~用户（看我自己的档案）
                _p = _userstore.get(event.user_id)
                if not _p:
                    await reply_send(actions, Manager, Segments, event, "还没有你的档案哦，先聊几句吧～")
                else:
                    await reply_send(actions, Manager, Segments, event,
                        f"📇 你的档案\n"
                        f"昵称：{_p.get('nickname') or event_user}\n"
                        f"关系：{_p.get('relation')}（好感度 {_p.get('affinity')}）\n"
                        f"发言：{_p.get('msg_count')} 条｜语音：{_p.get('voice_count')} 条\n"
                        f"连续互动：{_p.get('streak_days')} 天｜今天第 {_p.get('today_count')} 次\n"
                        f"——\n发 {reminder}模型 可看/切换你用的模型")
                return
            elif order.startswith("叫我"):
                # 需求④：让机器人记住你希望被怎么称呼
                _name = order[len("叫我"):].strip()
                if not _name:
                    await reply_send(actions, Manager, Segments, event, f"用法：{reminder}叫我 明明")
                elif len(_name) > 16:
                    await reply_send(actions, Manager, Segments, event, "称呼太长啦，16 个字以内好不好～")
                else:
                    _userstore.touch(event.user_id, nickname=_name)
                    _userstore.kv_set(f"u{event.user_id}", "nickname_pref", _name)
                    await reply_send(actions, Manager, Segments, event, f"好呀，那我以后就叫你「{_name}」啦～")
                return
            elif order.startswith("忘记我"):
                # 需求④：隐私 —— 物理删除该用户的全部档案
                _userstore.forget(event.user_id)
                await reply_send(actions, Manager, Segments, event,
                                 "好啦，关于你的档案我全都删掉了，我们重新认识一下吧～")
                return
            elif order.startswith("语音"):
                # 按需语音指令：~语音 [音色:名称] [语速:倍数|快|慢] 文字 —— 微软神经语音朗读，群聊/私聊通用
                _text, _voice, _rate = parse_voice_args(order[len("语音"):])
                if not _text:
                    await reply_send(actions, Manager, Segments, event,
                        f"请在 {reminder}语音 后面加上要朗读的文字，可附带 音色:名称 / 语速:倍数（快/慢）。"
                        f"例如：{reminder}语音 你好呀，我是{bot_name}～   或   {reminder}语音 语速:1.3 今天天气真好")
                else:
                    # 修复：先净化再判断 —— 纯表情/符号的文本净化后是空串，
                    # 原来会一路走到"语音合成失败：请确认本机已联网"，把"没东西可读"误报成网络故障。
                    _speak = speakable_text(_text)
                    if not _speak:
                        await reply_send(actions, Manager, Segments, event,
                            "这句话里没有可以朗读的文字哦（只有表情/符号/图片）～换句话再试试？")
                    else:
                        _ok = await speak_and_send(actions, Manager, Segments, event, _speak,
                                                   voice=_voice or voice_sel_for(event), rate=_rate,
                                                   reply_to=quote_target(event))
                        if not _ok:
                            await reply_send(actions, Manager, Segments, event,
                                "语音合成失败：请确认本机已联网（微软神经语音需联网），或已装离线兜底 pyttsx3（venv 执行 pip install pyttsx3 pywin32）。")
                return
            else:
                presets, p_info, is_changed = presets_tool.change_presets(presets, order, event)
                if is_changed:
                    # 清除ContextManager和user_lists中的单个用户上下文
                    cmc.del_context(event.user_id, event.group_id)
                    await reply_send(actions, Manager, Segments, event, p_info)
                    return

        # ===== 高数拍照解题（私聊：必须显式 ~高数 才触发，避免吞掉普通图片消息）=====
        try:
            _has_img = any(isinstance(i, Segments.Image) for i in event.message)
            if _has_img and order.startswith("高数"):
                from Tools.vision_solve import solve_math_image
                _extra = order[len("高数"):].strip()
                _reply = await solve_math_image(event.message, _extra, bot_name)
                await reply_send(actions, Manager, Segments, event, _reply)
                return
        except Exception as e:
            print(f"[高数拍照-私聊] 处理出错: {e}")

        # 语音对话模式：开启时只发语音、不发文字（emit_text=False 让 AIKernal 不发送文字，仅返回文本供 TTS）
        if voice_on_for(event):
            cmc, user_lists, result = await AIbot.generate_response(EnableNetwork, cmc, sys_prompt, user_lists, event, emit_text=False, model_id=model_for(event))
            # 修复：先净化再判断。纯表情/符号的回复净化后是空串，而原来的两条路
            # （语音合成失败 → reply_send(净化后的空串)）发的都是空内容，reply_send 会把空串直接丢掉，
            # 用户于是彻底收不到任何回复（既没声音也没文字）。
            _speak = speakable_text(result)
            _ok = False
            if _speak:
                _ok = await speak_and_send(actions, Manager, Segments, event, _speak,
                                           voice=voice_sel_for(event), reply_to=quote_target(event))
            if not _ok:
                # 兜底：能读就用净化后的文本；净化后为空（纯表情）就发原文；连原文都空才用占位符
                await reply_send(actions, Manager, Segments, event, _speak or (result or "").strip() or "……")
        else:
            # 文字模式：AIKernal 流式过程中已发送文字（含空回复兜底），此处不再发，否则每条回复重复两遍
            cmc, user_lists, result = await AIbot.generate_response(EnableNetwork, cmc, sys_prompt, user_lists, event, model_id=model_for(event))

    elif isinstance(event, Events.GroupMessageEvent):
        global second_start
        global generating
        global CONFIG_FILE, PRESET_DIR, NORMAL_PRESET
        global emoji_plus_one_off

        event_user = await get_user_nickname(event.user_id, Manager, actions)
        if event_user :
            event_user = event_user
        else:
            event_user = str(event.user_id)
                    
        # 初始化预设
        sys_prompt = presets_tool.gen_presets(event.user_id, bot_name, bot_name_en, event_user)
        # 需求④：群聊里同样注入说话人的档案摘要
        _pb = profile_block(event, event_user)
        if _pb:
            sys_prompt = f"{sys_prompt}\n\n{_pb}"
        presets = presets_tool.read_presets()
        
        if len(event.message) <= 0:
            return  # 只在函数中有效
        
        await _transcribe_voice(event, Segments)
        user_message = str(event.message).strip()
        order = ""

        # 语音对话主动询问（首聊/久未聊）或响应待确认选择；返回 True 表示已处理
        if await maybe_voice_offer(actions, Manager, Segments, event, user_message, reminder):
            return

        if "ping" == user_message:
            print(str(event.user_id))
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("pong! 爆炸！v(◦'ωˉ◦)~♡ ")))
            
        elif f"{bot_name}真棒" in user_message and str(reminder) not in user_message:
            try:
                compliments: list = config.others.get("compliment", ["谢谢夸奖 (◍•ᴗ•◍)❤"])
                # 修复：原写法 random.randint(0, len(compliments)) 上界越界，
                # 约 1/(n+1) 概率抛 IndexError，被下面的裸 except 吞掉后用户收不到任何回复。
                m = str(random.choice(compliments)) if compliments else "谢谢夸奖 (◍•ᴗ•◍)❤"
                compliment_result = await actions.custom.set_msg_emoji_like(group_id=event.group_id, message_id=event.message_id,emoji_id="66", is_add=True)
                compliment_result = Manager.Ret.fetch(compliment_result).data.raw
                if compliment_result.get("status", "error") != "ok":
                    print(f"sys: 表情回复失败 {compliment_result}")
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(m)))
            except:
                print("不接受夸赞")        

        global emoji_send_count
        if has_emoji(user_message) and not emoji_plus_one_off:
            if emoji_send_count is None or datetime.datetime.now() - emoji_send_count > datetime.timedelta(seconds=15):
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(user_message)))
                emoji_send_count = datetime.datetime.now()
            else:
                print(f"emoji +1 延迟 {abs(datetime.datetime.now() - emoji_send_count)} s")
        
        if user_message.startswith(reminder):
            if int(event.group_id) in config.black_list:
                print(f"sys: 黑名单内，拒绝群聊 {event.group_id} 的消息")
                await actions.send(group_id=event.group_id, message=Manager.Message(
                    Segments.Text(f'''❌ Error 403: Chat location restriction
Source Model: {EnableNetwork}
Location: This chat context is not permitted.
Version: {version_name}
Document: 安装与配置说明.md（本项目）

For more information, see the administrator or check the system logs.''')))
                return
    
            order_i = user_message.find(reminder)
            if order_i != -1:
                order = unicodedata.normalize("NFKC", user_message[order_i + len(reminder):]).strip()
                print(f"({event_user}) ORDER: {repr(order)}")

        if f"{reminder}重启" == user_message:
            if str(event.user_id) in ADMINS:
                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 重启QQ机器人'''
                await notify_root(actions, Manager, Segments, r_admin) #管理员操作通知ROOT用户
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"正在重启{bot_name}－O－……")))

                try:
                    with open("restart.temp", "w" ,encoding="utf-7") as f:
                        f.write(str(event.group_id))
                        f.close()
                except:
                    pass

                Listener.restart()
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        
        elif f"{reminder}重载插件" == user_message:
            if str(event.user_id) in ADMINS:
                global plugins
                plugins = load_plugins()

                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
外部后端已重载完成。发送 {reminder}插件视角 以查看更多信息。''')))
                
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        elif f"{reminder}禁用插件 " in user_message:
            if str(event.user_id) in ADMINS:
                message = user_message
                parts = message.split("禁用插件")
                if len(parts) > 1:
                    plugin_name = parts[-1].strip() # 获取命令后面的插件名
                    disable = True
                else: 
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}禁用插件 (plugin_name)\n参考：{reminder}禁用插件 Hello World")))

                if not plugin_name:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}禁用插件 (plugin_name)\n参考：{reminder}禁用插件 Hello World")))
                    return

                possible_paths = [
                    os.path.join(os.path.abspath(PLUGIN_FOLDER), f"{plugin_name}.py"),
                    os.path.join(os.path.abspath(PLUGIN_FOLDER), f"{plugin_name}.pyw"),
                    os.path.join(os.path.abspath(PLUGIN_FOLDER), plugin_name),  # 文件夹
                ]

                found_path = None
                for path in possible_paths:
                    if os.path.exists(path):
                        found_path = path
                        break

                if not found_path:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 找不到插件 {plugin_name}。''')))
                    return

                dirname, basename = os.path.split(found_path)

                new_name = "d_" + basename
                new_path = os.path.join(dirname, new_name)

                if not basename.startswith("d_"):
                    try:
                        os.rename(found_path, new_path)
                    except Exception as e:
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 禁用插件 {plugin_name} 时发生错误。
错误信息：{str(e)}''')))
                        return

                plugins = load_plugins()

                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
插件 {plugin_name} 已经成功禁用''')))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))

        elif f"{reminder}启用插件 " in user_message:
            if str(event.user_id) in ADMINS:
                message = user_message
                parts = message.split("启用插件")
                if len(parts) > 1:
                    plugin_name = parts[-1].strip() # 获取命令后面的插件名
                    disable = False
                else: 
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}启用插件 (plugin_name)\n参考：{reminder}启用插件 Hello World")))

                if not plugin_name:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}启用插件 (plugin_name)\n参考：{reminder}启用插件 Hello World")))
                    return

                possible_paths = [
                    os.path.join(os.path.abspath(PLUGIN_FOLDER), f"d_{plugin_name}.py"),
                    os.path.join(os.path.abspath(PLUGIN_FOLDER), f"d_{plugin_name}.pyw"),
                    os.path.join(os.path.abspath(PLUGIN_FOLDER), f"d_{plugin_name}"),  # 文件夹
                ]

                found_path = None
                for path in possible_paths:
                    if os.path.exists(path):
                        found_path = path
                        break

                if not found_path:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 找不到插件 {plugin_name}。''')))
                    return

                dirname, basename = os.path.split(found_path)

                if basename.startswith("d_"):
                    original_name = basename[2:]  # 去除 d_ 前缀，这意味着插件可以被执行
                    original_path = os.path.join(dirname, original_name)
                    try:
                        os.rename(found_path, original_path)
                    except Exception as e:
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 启用插件 {plugin_name} 时发生错误。
错误信息：{str(e)}''')))
                        return

                plugins = load_plugins() # 自动重载插件

                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
插件 {plugin_name} 已经成功启用''')))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        elif "GPT-4" == order:
            EnableNetwork = "Net"
            print(f"sys: AI Mode change to ChatGPT-4")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("嗯……我好像升级了！o((>ω< ))o")))
        elif "DeepSeek" == order:
            EnableNetwork = "Ds"
            print(f"sys: AI Mode change to DeepSeek")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("服务器……繁忙？ε٩(๑> ₃ <)۶з")))
        elif "云端" == order or "云端模型" == order:
            # 切到云端 DeepSeek（中转站 / 官方），不重启进程即可生效
            _set_ai_backend("cloud")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
                f"切换到云端模型啦 ☁️\n{ai_backend.describe()}\n（发 {reminder}本地 切回本地模型）")))
        elif "本地" == order or "本地模型" == order:
            _set_ai_backend("local")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
                f"切回本地模型啦 🏠\n{ai_backend.describe()}\n（发 {reminder}云端 切到云端）")))
        elif "后端状态" == order or "后端" == order:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
                f"当前 AI 后端\n{ai_backend.describe()}\n——\n{reminder}云端 / {reminder}本地 可切换")))
        elif "GPT-3.5" == order:
            EnableNetwork = "GPT-3.5"
            print(f"sys: AI Mode change to ChatGPT-3.5")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("切换到大模型中运行ο(=•ω＜=)ρ⌒☆")))
        elif "Gemini" == order:
            EnableNetwork = "GoogleGemini"
            print(f"sys: AI Mode change to Gemini")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}打开了新视界！o(*≧▽≦)ツ")))
        elif "联网" == order or "搜索" == order:
            EnableNetwork = "WebSearch"
            print(f"sys: AI Mode change to WebSearch")
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}打开联网搜索啦！以后每次回答我都会先去网上查一查 🌐（发 {reminder}DeepSeek 就能关掉）")))

        elif "列出黑名单" == order:
          if str(event.user_id) in ADMINS:
            try:
                with open("blacklist.sr", "r", encoding="utf-8") as f:
                    blacklist1 = set(line.strip() for line in f) 
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单列表加载完成: {blacklist1}")))
            except FileNotFoundError:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("黑名单列表加载失败,原因:没有文件")))
            except UnicodeDecodeError:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("黑名单列表加载失败,原因:解码失败")))
          else:
              await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        elif "添加黑名单 " in order:
            blacklist_file = "blacklist.sr"
            if str(event.user_id) in ADMINS:
                Toset2 = order[order.find("添加黑名单 ") + len("添加黑名单 "):].strip()
                blacklist114 = load_blacklist() # 加载现有的黑名单,防止已修改沒更新
                if Toset2 not in blacklist114:
                    blacklist114.add(Toset2) 
                    try:
                        with open(blacklist_file, "w", encoding="utf-8") as f:
                         for item in blacklist114:
                            f.write(item + "\n")  # 防止之前的丟失555，并添加换行符
                        r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 将群 {Toset2} 添加到禁止群发黑名单'''
                        await notify_root(actions, Manager, Segments, r_admin) #管理员操作通知ROOT用户
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单添加成功\n现在的群发黑名单: {blacklist114}")))
                    except Exception as e:
                       await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单添加失败, 是因为\n{e}")))
                else:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单添加失败,是因为{Toset2}已在黑名单")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        elif "删除黑名单 " in order:
            blacklist_file = "blacklist.sr"
            if str(event.user_id) in ADMINS:
                Toset1 = order[order.find("删除黑名单 ") + len("删除黑名单 "):].strip()
                blacklist117 = load_blacklist() # 加载现有的黑名单,防止已修改沒更新
                if Toset1 in blacklist117:
                    blacklist117.remove(Toset1) 
                    try:
                        with open(blacklist_file, "w", encoding="utf-8") as f:
                         for item in blacklist117:
                            f.write(item + "\n")  # 防止之前的丟失555，并添加换行符
                        r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 将群 {Toset1} 从禁止群发黑名单中删除'''
                        await notify_root(actions, Manager, Segments, r_admin) #管理员操作通知ROOT用户
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单删除成功\n现在黑名单: {blacklist117}")))
                    except Exception as e:
                       await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单删除失败, 是因为\n{e}")))
                else:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"黑名单删除失败, 是因为群{Toset1}不在黑名单")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
            
        elif "删除管理 " in order:
            r = ""
            r_admin = ""
            Toset = ""
            for i in event.message:
                if isinstance(i, Segments.At):
                    Toset = str(i.qq)
                    
            if str(event.user_id) in SUPERS:
                Toset = order[order.find("删除管理 ") + len("删除管理 "):].strip() if Toset == "" else Toset
                s = Super_User
                m = Manage_User
                if Toset in ROOT_User:
                    r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：指定的用户是 ROOT_User 且组 ROOT_User 为只读。'''
                    r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试夺取您的 ROOT_User 权限，已被阻止'''
                else:
                    if Toset in s:
                        s.remove(Toset)
                    if Toset in m:
                        m.remove(Toset)
                        
                    nick = await get_user_nickname(Toset, Manager, actions)
                    if Write_Settings(s, m):
                        r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nick} 现在是一个普通用户了。
现在发送 {reminder}帮助 了解你拥有的权限。'''
                        r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 删除了用户 {nick} 的管理员权限'''
                    else:
                        r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：设置文件不可写。'''
                        r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试删除用户 {nick} 的管理员权限，但因为无法读写配置文件导致修改失败'''
            else:
                r  = CONFUSED_WORD.format(bot_name=bot_name)

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
            if r_admin:
                await notify_root(actions, Manager, Segments, r_admin) #管理员操作通知ROOT用户
            
        elif "管理 " in order:
            r = ""
            r_admin = ""
            Toset = ""
            for i in event.message:
                if isinstance(i, Segments.At):
                    Toset = str(i.qq)
                    
            if str(event.user_id) in SUPERS:
                if "管理 M " in order:
                    Toset = order[order.find("管理 M ") + len("管理 M "):].strip() if Toset == "" else Toset
                    print(f"try to get_user {Toset}")
                    nikename = await get_user_nickname(Toset, Manager, actions)
                    print(str(nikename))
                    if len(nikename) == 0:
                        r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: {Toset} 不是一个有效的用户。'''
                    else:
                        nikename = nikename
                        m = Manage_User
                        s = Super_User
                        if Toset in Manage_User:
                            r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Manage_User 。'''
                        elif Toset in Super_User:
                            s.remove(Toset)
                            m.append(Toset)
                            if Write_Settings(s, m):
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Manage_User 。
现在发送 {reminder}帮助 了解你拥有的权限。'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 将用户 {nikename}(@{Toset}) 从 Super_User 设置为了 Manage_User '''
                            else:
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 设置文件不可写。'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试将用户 {nikename}(@{Toset}) 设置为 Manage_User 但因为无法读写配置文件导致修改失败'''
                        elif Toset in ROOT_User:
                            r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：指定的用户是 ROOT_User 且组 ROOT_User 为只读。'''
                            r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试改变您的 ROOT_User 权限，已被阻止'''
                        else:
                            m.append(Toset)
                            if Write_Settings(s, m):
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Manage_User 。
现在发送 {reminder}帮助 了解你拥有的权限。'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 将用户 {nikename}(@{Toset}) 设置为了 Manage_User '''
                            else:
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: 设置文件不可写'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试将用户 {nikename}(@{Toset}) 设置为 Manage_User 但因为无法读写配置文件导致修改失败'''
                       
                elif "管理 S " in order:
                    Toset = order[order.find("管理 S ") + len("管理 S "):].strip() if Toset == "" else Toset
                    print(f"try to get_user {Toset}")
                    nikename = await get_user_nickname(Toset, Manager, actions)
                    print(str(nikename))
                    if len(nikename) == 0:
                        r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败: {Toset} 不是一个有效的用户'''
                    else:
                        nikename = nikename
                        m = Manage_User
                        s = Super_User
                        if Toset in Manage_User:
                            m.remove(Toset)
                            s.append(Toset)
                            if Write_Settings(s, m):
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Super_User 。
现在发送 {reminder}帮助 了解你拥有的权限。'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 将用户 {nikename}(@{Toset}) 从 Manage_User 设置为了 Super_User '''
                            else:
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：设置文件不可写。'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试将用户 {nikename}(@{Toset}) 设置为 Super_User 但因为无法读写配置文件导致修改失败'''
                        elif Toset in Super_User:
                            r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Super_User 。'''
                        elif Toset in ROOT_User:
                            r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：指定的用户是 ROOT_User 且组 ROOT_User 为只读。'''
                            r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试改变您的 ROOT_User 权限，已被阻止'''
                        else:
                            s.append(Toset)
                            if Write_Settings(s, m):
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
成功: {nikename}(@{Toset}) 已加入管理组 Super_User 。
现在发送 {reminder}帮助 了解你拥有的权限。'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 将用户 {nikename}(@{Toset}) 设置为了 Super_User '''
                            else:
                                r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：设置文件不可写。'''
                                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 尝试将用户 {nikename}(@{Toset}) 设置为 Super_User 但因为无法读写配置文件导致修改失败'''
                else:
                    r = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
失败：只能设置 Manage_User 或 Super_User 。'''
            else:
                r  = CONFUSED_WORD.format(bot_name=bot_name)

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
            if r_admin:
                await notify_root(actions, Manager, Segments, r_admin) #管理员操作通知ROOT用户
            
        elif "让我访问" in order:
            if str(event.user_id) in ADMINS:
                manage_users = await asyncio.gather(*[get_user_nickname_with_userid(uid, Manager, actions) for uid in Manage_User])
                super_users = await asyncio.gather(*[get_user_nickname_with_userid(uid, Manager, actions) for uid in Super_User])
                root_users = await asyncio.gather(*[get_user_nickname_with_userid(uid, Manager, actions) for uid in ROOT_User])
                r = f"""{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
Manage_User: {", ".join(manage_users)}
————————————————————
Super_User: {", ".join(super_users)}
————————————————————
ROOT_User: {", ".join(root_users)}
————————————————————
If you are a Super_User or ROOT_User, you can manage these users. Use {reminder}帮助 to know more.
""".strip()
            
            else:
                r  = CONFUSED_WORD.format(bot_name=bot_name)
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(r)))

        elif "插件视角" in order:
            status = f'''{bot_name} {bot_name_en} - 插件视角
————————————————————
✅ 已加载插件 ({len(loaded_plugins)}):
{chr(10).join(f"{i+1}. {str(plugin).rsplit('_', 1)[0]}" for i, plugin in enumerate(loaded_plugins)) if loaded_plugins else "无"}

❌ 已禁用插件 ({len(disabled_plugins)}):
{chr(10).join(
    f"{i+1}. {str(plugin).replace('d_', '').split('.')[0]}" 
    for i, plugin in enumerate(disabled_plugins)) if disabled_plugins else "无"}

⚠️ 加载失败 ({len(failed_plugins)}):
{chr(10).join(f"{i+1}. {str(plugin)}" 
    for i, plugin in enumerate(failed_plugins)) 
if failed_plugins else "无"}'''

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(status)))
        elif "用户帮助" == order:
            content = help_message(event)
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(content)))
            
        elif "帮助" == order:
            if str(event.user_id) in ADMINS:
                content = [
                    (f"{reminder}让我访问", "检索有权限的用户"), # Managers' help content 管理员帮助
                    (f"{reminder}注销", "删除所有用户的上下文"),
                    (f"{reminder}修改 (hh:mm) (内容)", "改变定时消息时间与内容"),
                    (f"{reminder}感知", "查看运行状态"),
                    (f"{reminder}休眠", f"奖励{bot_name}精致睡眠 💤"),
                    (f"{reminder}重启", f"关闭所有线程和进程，关闭{bot_name}。然后重新启动{bot_name}。"),
                    (f"{reminder}启用插件（插件名称）", "启用特定插件"),
                    (f"{reminder}禁用插件（插件名称）", "忽略特定插件"),
                    (f"{reminder}重载插件", "重新加载所有插件"),
                    (f"{reminder}群发 (内容)", "在所有群聊中（黑名单群聊除外）发送一条消息"),
                    (f"{reminder}冷静 (@QQ+时间)/(@all)", "冷静用户一段时间"),
                    (f"{reminder}取消冷静 (@QQ)/(@all)", "解除用户冷静"),
                    (f"{reminder}送飞机票 (@QQ)", "将用户移出群聊"),
                    ("撤回【引用消息】", "撤回指定消息"),
                    (f"{reminder}群发黑名单", "管理群发消息时不会发送到的群聊"),
                    (f"{reminder}角色扮演", "管理角色预设"),
                    (f"{reminder}更改TTS状态", "切换本会话「语音对话」开关（默认关闭；首聊会主动询问）"),
                    (f"{reminder}音色 (声线名/男生/女生)", "设定本会话语音声线（男生/女生声线均可选）"),
                    (f"{reminder}语音开 / {reminder}语音关", "开启/关闭本会话语音对话（开启后回复只发语音、不另发文字）"),
                    (f"{reminder}表情复述", "切换是否开启表情复述功能（默认启用）")
                ]
                
                if str(event.user_id) in SUPERS:
                    content += [
                        (f"{reminder}管理 M (QQ号)", "为用户添加 Manage_User 权限"),
                        (f"{reminder}管理 S (QQ号)", "为用户添加 Super_User 权限"),
                        (f"{reminder}删除管理 (QQ号)", "删除指定用户所有权限"),
                        (f"{reminder}退出本群", "退出当前群聊")
                    ]
                    
                command_lines = [
                    f"{idx+1}. {cmd} —> {desc}"
                    for idx, (cmd, desc) in enumerate(content)
                ]
                
                content = "\n".join([
                    f"管理我们的{bot_name}\n————————————————————",
                    f"你拥有管理{bot_name}的权限，以下是你可以使用的命令。若要查看普通帮助，请@{bot_name} 或发送【{reminder}用户帮助】",
                    *command_lines,
                    "你的每一步操作，与用户息息相关。"
                ])
                
            else:
                content = help_message(event)
                
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(content)))

        elif (isinstance(event.message[0], Segments.At) and 
              str(event.message[0].qq) == str(event.self_id)): 
            
            has_valid_content = False
            for item in event.message[1:]:
                if isinstance(item, Segments.Text):
                    if str(item).strip():
                        has_valid_content = True
                        break
                else:
                    has_valid_content = True

            content = help_message(event) if not has_valid_content else f'''你要询问什么呢？嘻嘻(●'◡'●)
和我聊天不需要@我哟(＾Ｕ＾)ノ~
直接在你想对{bot_name}想说的话前面加上 {reminder} 就行啦'''
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(content)))

        elif "关于" == order: 
            framework = await actions.get_version_info()
            framework = framework.data.raw
            about = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
构建信息：
版本：{version_name}
由 {framework.get("app_name")} {framework.get("protocol_version")}-{framework.get("app_version")} 驱动
基于 Hype𝐑_bot 框架制作
————————————————————
第三方服务
1. DuckDuckGo（联网搜索，免 Key）
2. Lolicon API + 栗次元/兜底图源（ACG / Pixiv 图片）
3. 一言 v1.hitokoto.cn
4. 微软 Edge 神经语音 EdgeTTS（免 Key）
5. DeepSeek（本地 LM Studio 或云端中转，由 ai_backend 决定）
6. Google Gemini、OpenAI ChatGPT（需在 config.json 填对应 Key）
————————————————————
基于 Jianer QQ Bot（© 2019~{datetime.datetime.now().year} SR思锐团队，GPL-3.0）二次修改；本机为本地部署版'''

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(about)))

        elif "群发黑名单" == order:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(f'''{bot_name} {bot_name_en} - 群发黑名单管理控制面板
————————————————————
{reminder}列出黑名单 —> 显示所有黑名单群组
{reminder}删除黑名单 +群号 —> 允许群发消息到该群
{reminder}添加黑名单 +群号 —> 禁止群发消息到该群

如果想要关闭群发功能，请联系服务器管理员删除 `timing_message.ini` 文件。\n在关闭群发后，使用 -修改 功能即可重新启用。''')))
            
        elif f"{reminder}角色扮演" == user_message:
            prerequisites_info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
{presets_tool.list_presets(presets, presets_tool.current_preset, reminder)}

发送相应的关键词，{bot_name}会尽力扮演不同角色和你交流哒！⌯>ᴗoᴗ⌯ .ᐟ.ᐟ
————————————————————
若您是 Manage_User, Super_User 或 ROOT_User，你可以管理这些角色，尝试：
    {reminder}添加预设 [name] [info] : [content]
    {reminder}删除预设 [name]
其中，name 为角色名称， info 为预设简介， content 为预设内容。"""

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(prerequisites_info)))

        elif f"添加预设 " in order:
            if str(event.user_id) in ADMINS:
                match = re.match(r"添加预设\s+(.+?)\s+(.+?)\s*[:：]\s*(.+)", order, re.DOTALL)
                if not match:
                    prerequisites_info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
添加预设 格式错误。
用法：{reminder}添加预设 [name] [info] : [content]
其中，name 为角色名称， info 为预设简介， content 为预设内容。

示例：{reminder}添加预设 助手 让{bot_name}成为你有帮助的助手！ : 你是一个有帮助的助手。"""

                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(prerequisites_info)))
                    return 

                name, info, content = match.groups()
                
                # 唯一标识符看起来太乱了，这里使用随机数生成预设id
                while True:
                    preset_id = "p" + str(random.randint(1000000, 9999999))
                    if not os.path.exists(os.path.join(PRESET_DIR, f"{preset_id}.txt")):
                        break

                # 检查是否已经存在具有相同 name 的预设
                existing_preset_id = None
                for pid, pdata in presets.items():
                    if pdata["name"] == name:
                        existing_preset_id = pid
                        break

                if existing_preset_id:
                    # 如果存在，则更新已存在的预设文件
                    preset_id = existing_preset_id
                    preset_path = os.path.join(PRESET_DIR, presets[preset_id]["path"])
                    with open(preset_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    presets[preset_id]["info"] = info
                else:
                    # 如果不存在，则创建新的预设
                    preset_filename = f"{preset_id}.txt"
                    preset_path = os.path.join(PRESET_DIR, preset_filename)

                    with open(preset_path, "w", encoding="utf-8") as f:
                        f.write(content)

                    presets[preset_id] = {
                        "name": name,
                        "uid": [],
                        "info": info,
                        "path": preset_filename,
                    }
                    
                presets_tool.write_presets(presets)
                rootmsg = f"{'更新现有' if existing_preset_id else '添加'}预设: {name}"
                await notify_root(actions, Manager, Segments, f"用户 {event.user_id} 在群 {event.group_id} 中{rootmsg} ") #管理员操作通知ROOT用户
                prerequisites_info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
已{'更新现有' if existing_preset_id else '添加'}预设: {name}"""
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(prerequisites_info)))
        
            else:
                r  = CONFUSED_WORD.format(bot_name=bot_name)
            
        elif f"删除预设 " in order:
            if str(event.user_id) in ADMINS:
                match = re.match(r"删除预设\s+(.+)", order)
                if not match:
                    prerequisites_info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
删除预设 格式错误。
用法：{reminder}删除预设 [name] 
其中，name 为角色名称。

示例：{reminder}删除预设 助手"""

                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(prerequisites_info)))
                    return 

                name = match.group(1).strip()

                preset_id_to_delete = None
                for preset_id, preset_data in presets.items():
                    if preset_data["name"] == name:
                        preset_id_to_delete = preset_id
                        break

                if not preset_id_to_delete:
                    # 修复：找不到同名预设时 preset_id_to_delete 为 None，
                    # 原来的 del presets[None] 会抛 KeyError，而且会把"未找到"误报成"已删除"。
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
                        f"{bot_name} {bot_name_en} - 角色扮演后台\n————————————————————\n"
                        f"失败：没有找到名为「{name}」的预设。\n"
                        f"可用 {reminder}角色扮演 查看全部预设。")))
                    return

                if preset_id_to_delete:
                    # 删除预设文件
                    preset_path = os.path.join(PRESET_DIR, presets[preset_id_to_delete]["path"])
                    print(f"Removed {preset_path}")
                    os.remove(preset_path)

                # 从配置中删除预设
                del presets[preset_id_to_delete]
                
                presets_tool.write_presets(presets)
                await notify_root(actions, Manager, Segments, f"用户 {event.user_id} 在群 {event.group_id} 中删除 {name} 预设") #管理员操作通知ROOT用户
                prerequisites_info = f"""{bot_name} {bot_name_en} - 角色扮演后台
————————————————————
已删除预设: {name}"""
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(prerequisites_info)))

            else:
                r  = CONFUSED_WORD.format(bot_name=bot_name)
                
        elif "休眠" == order:
            if str(event.user_id) in ADMINS:
                stop_working = True
                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 休眠QQ机器人'''
                await notify_root(actions, Manager, Segments, r_admin) #管理员操作通知ROOT用户
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"谢谢喵，{bot_name}睡觉去了 ヾ(＠ ˘ω˘ ＠)ノ💤")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))

        elif f"{reminder}感知" in user_message:
            if str(event.user_id) in ADMINS:
                system_info = get_system_info()
                feel = f'''{bot_name} {bot_name_en} - {ONE_SLOGAN}
————————————————————
系统当前运行状况
运行时间：{seconds_to_hms(round(time.time() - second_start, 2))}
系统版本：{system_info["version_info"]}
体系结构：{system_info["architecture"]}
CPU占用：{str(system_info["cpu_usage"]) + "%"}
内存占用：{str(system_info["memory_usage_percentage"]) + "%"}'''
                for i, usage in enumerate(system_info["gpu_usage"]):
                    feel = feel + f"\nGPU {i} Usage：{usage * 100:.2f}%"
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(feel)))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
            
        elif f"{reminder}注销" in user_message:
            if str(event.user_id) in ADMINS:
                del cmc
                cmc = ContextManager()
                user_lists = {}
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"卸下包袱，{bot_name}更轻松了~ (/≧▽≦)/")))
                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 手动清空了所有用户的 AI 对话上下文'''
                await notify_root(actions, Manager, Segments, r_admin) #管理员操作通知ROOT用户
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
      
        elif f"{reminder}生成" == user_message:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Image(os.path.abspath("./assets/sc114.png"))))
            
        elif "修改 " in order:
            if str(event.user_id) in ADMINS:
                try:
                    tm = order[order.find("修改 ") + len("修改 "):].strip()
                    if not bool(re.match(r'^([01][0-9]|2[0-3]):([0-5][0-9])$', tm[:5])):
                        r = f'''{bot_name}不能识别给定的时间是什么 Σ( ° △ °|||)︴
举个🌰子：{reminder}修改 00:00 早安 —> 即可让{bot_name}在0点0分准时问候早安噢⌯oᴗo⌯'''
                    else:
                        timing_settings = f"{tm[:5]}⊕{tm[6::].strip()}"
                        with open("timing_message.ini", "w", encoding="utf-8") as f:
                            f.write(timing_settings)
                            f.close()
                        r = f"{bot_name}设置成功！(*≧▽≦) "
                        r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 将机器人的定时群发消息修改为时间：{tm[:5]} 
内容：{tm[6::]}'''
                        await notify_root(actions, Manager, Segments, r_admin) #管理员操作通知ROOT用户
                except Exception as e:
                    r = f'''{str(type(e))}
{bot_name}设置失败了…… (╥﹏╥)'''
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(r)))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
            
        elif f"{reminder}群发" in user_message:
            if str(event.user_id) in ADMINS:
                words = order.split(" ")
                if len(words) < 2 and len(event.message) == 1:
                    r = f'''群发格式错误 Σ( ° △ °|||)︴
举个🌰子：{reminder}群发 {bot_name}有更新新功能啦！ —> 在所有群聊中发送消息 “{bot_name}有更新新功能啦！”'''
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(r)))
                else:
                    print(f"消息长度: {len(event.message)}") 
                    if len(event.message) > 0 and isinstance(event.message[0], Segments.Text):
                        new_text = str(event.message[0]).replace(f"{reminder}群发 ", "", 1) if f"{reminder}群发 " in str(event.message[0]) else str(event.message[0]).replace(f"{reminder}群发", "", 1)
                        if len(event.message) > 1:
                            m = Manager.Message(Segments.Text(new_text), *event.message[1:])
                        else:
                            m = Manager.Message(Segments.Text(new_text))
                    else:
                        m = event.message

                    words.pop(0)
                    word = " ".join(words)
                    r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 启动群发消息：\n'''
                    await notify_root(actions, Manager, Segments, r_admin, *m) #管理员操作通知ROOT用户（带群发的图片）
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id), Segments.Text(f'''已启动群发消息：\n'''), *m))
                    await send_msg_all_groups(word, actions, m)
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
                
        elif f"{reminder}生草" == user_message:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("🌿")))

        elif "zzzz...涩图...嘿嘿..." in user_message:
            try:
                order = "生图 ACG 随机"
                local_vars = globals().copy()
                local_vars.update(locals().copy())
                if not await execute_plugins(False, **local_vars):
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}需要 GenerateFromACG 插件才能生成好看的涩图哦 (੭ु ˃̶͈̀ ω ˂̶͈́)੭ु⁾⁾")))
            except:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}需要 GenerateFromACG 插件才能生成好看的涩图哦 (੭ु ˃̶͈̀ ω ˂̶͈́)੭ु⁾⁾")))
                
        elif "取消冷静 " in order:
           if str(event.user_id) in ADMINS:
            start_index = order.find("取消冷静 ")
            if start_index != -1:
                result = order[start_index + len("取消冷静 "):].strip()
                numbers = re.findall(r'\d+', result)
                complete = False
                for i in event.message:
                    if isinstance(i, Segments.At):
                        # 修复：@全体成员的 At 片段 qq 是 "all"，str 出来没有数字，
                        # 原来 numbers[0] 会抛 IndexError（该分支没有 try），
                        # 而且会在到达下面 "@all" 分支之前就崩掉。非数字 At 交给 "@all" 分支处理。
                        if not str(i.qq).isdigit():
                            continue
                        print("At in loading...")
                        userid114 = str(i.qq)  
                        time114 = 0
                        await actions.set_group_ban(group_id=event.group_id,user_id=userid114,duration=time114)
                        complete = True
                        break

                if not complete:
                    if "@all" in order:
                        await actions.custom.set_group_whole_ban(group_id=event.group_id, enable=False)
                        complete = True
                    else:
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}取消冷静 @anyone/@all\n参考：{reminder}取消冷静 @Harcic#8042")))
     
           else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
                
        elif "冷静" in order:
            if str(event.user_id) in ADMINS:
                try:
                    start_index = order.find("冷静")
                    if start_index != -1:
                        result = order[start_index + len("冷静"):].strip()
                        numbers = re.findall(r'\d+', result)
                        complete = False
                        for i in event.message:
                            if isinstance(i, Segments.At):
                                # 修复：@全体成员(qq="all") 时 numbers 里只有时长，
                                # numbers[1] 会 IndexError，导致 ~冷静 @全体成员 60 永远失败。
                                if not str(i.qq).isdigit():
                                    continue
                                userid114 = str(i.qq)
                                time114 = numbers[-1] if numbers else 0
                                
                                if str(userid114) == str(event.user_id):
                                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"你抖M是吧！{bot_name}生气了！自己找个没人的地方自己处理自己去，懒得理你 ┗(•̀へ •́ ╮)")))
                                    complete = None
                                else:
                                    await actions.set_group_ban(group_id=event.group_id, user_id=userid114, duration=time114)
                                    complete = True
                                    break 
                        
                        if complete is not None:
                            if not complete:
                                if "@all" in order:
                                    await actions.custom.set_group_whole_ban(group_id=event.group_id, enable=True)
                                    complete = True
                                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：已冷静。")))
                                else:
                                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}冷静 @anyone/@all (seconds of duration)\n参考：{reminder}冷静 @Harcic#8042 128")))
                            else:
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：已冷静，时长 {time114} 秒。")))
                    
                except Exception as e:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"管理员：你的格式有误。\n格式：{reminder}冷静 @anyone/@all (seconds of duration)\n参考：{reminder}冷静 @Harcic#8042 128")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
          
        elif "送飞机票" in order:
          if str(event.user_id) in ADMINS:
                for i in event.message:
                    if isinstance(i, Segments.At):
                        await actions.set_group_kick(group_id=event.group_id,user_id=i.qq)
                        r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 使 {await get_user_nickname(i.qq, Manager, actions)} 退出了群聊：{event.group_id}'''
                        await notify_root(actions, Manager, Segments, r_admin) #管理员操作通知ROOT用户
          else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))  
        
        elif f"{reminder}退出本群" == user_message:
            if str(event.user_id) in SUPERS:
                r_admin = f'''用户 {await get_user_nickname(event.user_id, Manager, actions)} 在 {event.time_str} 使机器人退出了群聊：{event.group_id}'''
                await notify_root(actions, Manager, Segments, r_admin) #管理员操作通知ROOT用户
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"呜呜呜，各位再见了……")))
                await asyncio.sleep(3)
                await actions.custom.set_group_leave(group_id=event.group_id, is_dismiss=True)
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        elif "撤回" == user_message:
            if str(event.user_id) in ADMINS:
              if isinstance(event.message[0], Segments.Reply):
                try:
                  await actions.del_message(event.message[0].id)
                except:
                    pass
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
        elif order in ("语音开", "开启语音", "语音对话开"):
            set_voice_on(event, True)
            await _voice_reply(actions, Manager, Segments, event, "🔊 已开启语音对话！之后我的回复会用语音念出来（不再另发文字）~")
            return
        elif order in ("语音关", "关闭语音", "语音对话关"):
            set_voice_on(event, False)
            await _voice_reply(actions, Manager, Segments, event, "已关闭语音对话，恢复纯文字回复~ 想开随时发 ~语音开")
            return
        elif f"{reminder}更改TTS状态" == user_message:
            _new = not voice_on_for(event)
            set_voice_on(event, _new)
            await _voice_reply(actions, Manager, Segments, event,
                               ("🔊 已开启语音对话！之后我的回复会用语音念出来（不再另发文字）~"
                                if _new else "已关闭语音对话，恢复纯文字回复~ 想开随时发 ~语音开"))
            return

        elif order.startswith("模型"):
            # 需求⑤：群聊也可用；~模型 群 <名> 设置本群默认（管理员）
            _rest = order[len("模型"):].strip()
            _cur = model_for(event)
            _entry = _models.find(_cur)
            _cur_txt = _models.render_entry(_entry) if _entry else (_cur or "未配置")
            if not _rest or _rest in ("列表", "帮助", "?"):
                await _voice_reply(actions, Manager, Segments, event,
                    f"🧠 你当前的模型：{_cur_txt}\n{_models.listing(_cur)}\n\n"
                    f"用法：{reminder}模型 3 ｜ {reminder}模型 推理 ｜ "
                    f"{reminder}模型 群 <名>（管理员设本群默认）｜ {reminder}模型 状态")
            elif _rest.startswith("状态"):
                _models.probe()
                await _voice_reply(actions, Manager, Segments, event,
                                   "各供应商健康状态：\n" + _models.health_report())
            elif _rest.startswith("群"):
                if str(event.user_id) not in ADMINS:
                    await _voice_reply(actions, Manager, Segments, event, CONFUSED_WORD.format(bot_name=bot_name))
                else:
                    _ok, _msg = _models.set_group_model(event.group_id, _rest[1:].strip())
                    await _voice_reply(actions, Manager, Segments, event,
                                       ("✅ 本群默认模型已设为 " + _msg) if _ok else ("😶 " + _msg))
            elif _rest.startswith("全局"):
                if str(event.user_id) not in ROOT_User:
                    await _voice_reply(actions, Manager, Segments, event, CONFUSED_WORD.format(bot_name=bot_name))
                else:
                    _ok, _msg = _models.set_global_model(_rest[2:].strip())
                    await _voice_reply(actions, Manager, Segments, event,
                                       ("✅ 全局默认模型已设为 " + _msg) if _ok else ("😶 " + _msg))
            elif _rest in ("自动", "auto"):
                _userstore.pref_set(event.user_id, "model", "auto")
                await _voice_reply(actions, Manager, Segments, event, "🤖 已为你开启智能路由。")
            else:
                _ok, _msg = _models.set_user_model(event.user_id, _rest)
                if _ok:
                    cmc.del_context(event.user_id, event.group_id)
                await _voice_reply(actions, Manager, Segments, event,
                    (f"✅ 已切到 {_msg}\n（只影响你自己，上下文已重置）") if _ok else ("😶 " + _msg))
            return
        elif order.startswith("音色"):
            # 需求③：与私聊分支保持一致的支持范围（含 音效 / 随机 / 情绪 / 检测）
            _name = order[len("音色"):].strip()
            from Tools.tts_voices import is_voice_name, is_fx_name, split_selection
            if _name in ("检测", "探活", "test"):
                import Tools.tts_health as _th
                _res = await asyncio.to_thread(_th.probe_all)
                _ok = sum(1 for v in _res.values() if v[0])
                await _voice_reply(actions, Manager, Segments, event,
                                   f"🎧 音色探活完成：{_ok}/{len(_res)} 可用\n{_th.summary()}")
            elif not _name or _name in ("列表", "帮助", "?"):
                await _voice_reply(actions, Manager, Segments, event,
                                   "🎙️ 本会话当前声线：" + voice_sel_for(event) + "\n" + list_voices()
                                   + f"\n用法：{reminder}音色 晓晓 ｜ {reminder}音色 花栗鼠 ｜ "
                                     f"{reminder}音色 晓晓+花栗鼠 ｜ {reminder}音色 随机 ｜ "
                                     f"{reminder}音色 情绪 ｜ {reminder}音色 检测")
            else:
                _v, _fx = split_selection(_name)
                _v_ok = (not _v) or is_voice_name(_v) or str(_v).startswith(("zh-", "ja-", "en-", "ko-"))
                _fx_ok = (not _fx) or is_fx_name(_fx)
                if _v_ok and _fx_ok and (_v or _fx):
                    set_voice_sel(event, _name)
                    _tail = f"\n（音效：{_fx}）" if _fx else ""
                    await _voice_reply(actions, Manager, Segments, event,
                                       f"🔊 已把本会话声线设为「{_name}」~ 之后语音对话和朗读都会用这个。{_tail}")
                else:
                    await _voice_reply(actions, Manager, Segments, event,
                                       f"😶 没找到「{_name}」这个音色/音效哦。\n" + list_voices())
            return

        elif order.startswith("语音"):
            # 按需语音指令：~语音 [音色:名称] [语速:倍数|快|慢] 文字 —— 微软神经语音朗读，群聊/私聊通用
            _text, _voice, _rate = parse_voice_args(order[len("语音"):])
            if not _text:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
                    f"请在 {reminder}语音 后面加上要朗读的文字，可附带 音色:名称 / 语速:倍数（快/慢）。"
                    f"例如：{reminder}语音 你好呀，我是{bot_name}～   或   {reminder}语音 语速:1.3 今天天气真好")))
            else:
                # 修复：同私聊分支 —— 纯表情/符号没有可朗读内容时要说清楚，
                # 而不是回一句"语音合成失败，请检查联网"（把 A 类问题误报成 B 类故障）。
                _speak = speakable_text(_text)
                if not _speak:
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
                        "这句话里没有可以朗读的文字哦（只有表情/符号/图片）～换句话再试试？")))
                else:
                    _ok = await speak_and_send(actions, Manager, Segments, event, _speak,
                                               voice=_voice or voice_sel_for(event), rate=_rate)
                    if not _ok:
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
                            "语音合成失败：请确认本机已联网（微软神经语音需联网），或已装离线兜底 pyttsx3（venv 执行 pip install pyttsx3 pywin32）。")))
            return

        elif f"{reminder}表情复述" == user_message:
            if emoji_plus_one_off: 
                emoji_plus_one_off = False
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"开启表情复述成功！")))
            else:
                emoji_plus_one_off = True
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"关闭表情复述成功！")))
                
        elif f"{reminder}更改分配头衔开放状态" == user_message:
            global self_service_titles
            if str(event.user_id) in SUPERS:
                if self_service_titles:
                    self_service_titles = False
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"分配头衔功能已取消开放！")))
                else:
                    self_service_titles = True
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"分配头衔功能已开放！")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
                
        elif "给他人分配头衔" in order:
            if str(event.user_id) in SUPERS:
                try:
                    start_index = order.find("给他人分配头衔")
                    if start_index != -1:
                        result = order[start_index + len("给他人分配头衔"):].strip() 
                    match = re.search(r'(\d+)\s+(.+)', result)
                    if match:  
                        userid114 = match.group(1)  
                        title114 = match.group(2).strip() 

                        if len(title114) > 6:  
                            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("头衔不能超过6个字！")))
                        else:
                            try:  
                                # 修复：OneBot v11 的 set_group_special_title 参数名是 special_title，
                                # 不是 title（Hyper 的 actions.custom 会把 kwargs 原样透传给 OneBot 接口）。
                                await actions.custom.set_group_special_title(group_id=event.group_id, user_id=userid114, special_title=title114, duration=-1)
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("已设置！")))
                            except Exception as set_title_error:
                                print(f"设置头衔失败: {set_title_error}")
                                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"设置头衔失败：{set_title_error}")))

                    else:   
                        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("指令格式有误，请使用 用户ID 头衔 的格式。")))

                except Exception as e: 
                    print(f"处理分配头衔指令时出错: {e}")
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("格式有误或发生未知错误！")))
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(CONFUSED_WORD.format(bot_name=bot_name))))
                
        elif f"分配头衔 " in order:
            titletext = order[order.find("分配头衔 ") + len("分配头衔 "):].strip()
            if len(titletext) > 6:
                await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("头衔不能超过6个字！")))
            else:
                if str(event.user_id) in SUPERS:
                    # 修复：同上，参数名应为 special_title
                    await actions.custom.set_group_special_title(group_id=event.group_id,user_id=event.user_id,special_title=titletext,duration=-1)
                    await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("已设置！")))
                else:
                    if self_service_titles:
                        await actions.custom.set_group_special_title(group_id=event.group_id,user_id=event.user_id,special_title=titletext,duration=-1)
                        await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("已设置！")))
                    else:
                        await actions.send(group_id=event.group_id,message=Manager.Message(Segments.Text("当前功能未开放,请联系管理员(高级用户 或者 根用户)开放权限！")))
        else:
            # 没有匹配到用户发送的任何关键字，进入二级响应
            # 1. 检查用户是否是想要切换预设
            presets, p_info, is_changed = presets_tool.change_presets(presets, order, event)
            if is_changed:
                # 清除ContextManager和user_lists中的单个用户上下文
                cmc.del_context(event.user_id, event.group_id)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(p_info)))
                return 


            # 2. 检查用户是否要执行插件中的功能
            local_vars = globals().copy()
            local_vars.update(locals().copy())
            try:
                if await execute_plugins(False, **local_vars):
                    return  # 只传递 event 作为位置参数
            except Exception as e:
                print(f"处理插件时发生错误: {e}")
                return
            
            # 3. 全都匹配不到，进入AI回复
            # ===== 高数拍照解题（图片消息，插入在普通 AI 回复之前）=====
            try:
                _has_img = any(isinstance(i, Segments.Image) for i in event.message)
                _has_text = any(isinstance(i, Segments.Text) for i in event.message)
                # 群聊触发：~高数 前缀强触发；或纯图片（无文字、无命令）自动触发，避免表情包刷屏误触
                _trigger = order.startswith("高数") or (order == "" and _has_img and not _has_text)
                if _has_img and _trigger:
                    from Tools.vision_solve import solve_math_image
                    _extra = order[len("高数"):].strip() if order.startswith("高数") else ""
                    _reply = await solve_math_image(event.message, _extra, bot_name)
                    await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(_reply)))
                    return
            except Exception as e:
                print(f"[高数拍照-群聊] 处理出错: {e}")

            if len(order) < 2:  # 不响应小于两个字的废话
                return

            try:
                if voice_on_for(event):
                    # 语音对话模式：只发语音、不发文字（emit_text=False 让 AIKernal 不发文字，仅返回文本供 TTS）
                    cmc, user_lists, result = await AIbot.generate_response(EnableNetwork, cmc, sys_prompt, user_lists, event, emit_text=False, model_id=model_for(event))
                    # 修复：纯表情/符号的回复净化后为空 —— 原来语音和文字兜底发的都是空 Text
                    # （QQ 会显示"该消息类型暂不支持查看"），等于既没声音也没内容。
                    _speak = speakable_text(result)
                    _ok = False
                    if _speak:
                        _ok = await speak_and_send(actions, Manager, Segments, event, _speak, voice=voice_sel_for(event))
                    if not _ok:
                        # 兜底：优先净化后的文本；纯表情就发原文；都为真空才用占位符
                        await actions.send(group_id=event.group_id, message=Manager.Message(
                            Segments.Reply(event.message_id),
                            Segments.Text(_speak or (result or "").strip() or "……")))
                else:
                    # 文字模式：AIKernal 流式已发文字，此处不再发
                    cmc, user_lists, result = await AIbot.generate_response(EnableNetwork, cmc, sys_prompt, user_lists, event, model_id=model_for(event))

            except UnboundLocalError:
                raise
            except TimeoutError:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(f"哎呀，你问的问题太复杂了，{bot_name}想不出来了 ┭┮﹏┭┮")))
            except Exception as e:
                print(traceback.format_exc())
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Reply(event.message_id),Segments.Text(f"{type(e)}\n{bot_name}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3")))
      
def help_message(event) -> str:
    global EnableNetwork, bot_name, reminder, plugins_help
    if isinstance(event, Events.GroupMessageEvent):
        # 模型可用性标注：读 config 判断 key 是否已配置，未配置则提示用户
        try:
            _cfg_others = config.others
            _gemini_ok = bool(_cfg_others.get("gemini_key", ""))
            _openai_ok = bool(_cfg_others.get("openai_key", ""))
        except Exception:
            _gemini_ok = _openai_ok = False
        _badge = lambda ok: "" if ok else "（未配置Key）"
        # 云端/本地后端状态（供帮助菜单标注当前档位）
        try:
            _o = config.others
            _cloud_on = str(_o.get("ai_backend", "local")).strip().lower() == "cloud"
            _cloud_label = str(_o.get("cloud_model") or "deepseek-chat")
            _local_label = str(_o.get("local_model") or "未配置")
        except Exception:
            _cloud_on, _cloud_label, _local_label = False, "deepseek-chat", "未配置"

        return f'''如何与{bot_name}交流( •̀ ω •́ )✧
       注：对话前必须加上 {reminder} 噢！~
       {reminder}(任意问题，必填) —> {bot_name}回复
       {reminder}Gemini{"（当前）" if EnableNetwork == "GoogleGemini" else _badge(_gemini_ok)} —> {bot_name}切换到Google的多模态模型Gemini✅
       {reminder}GPT-4{"（当前）" if EnableNetwork == "Net" else _badge(_openai_ok)} —> {bot_name}切换到OpenAI的GPT4回复🌟
       {reminder}GPT-3.5{"（当前）" if EnableNetwork == "GPT-3.5" else _badge(_openai_ok)} —> {bot_name}切换到OpenAI最经典的模型回复🎈
       {reminder}DeepSeek{"（当前）" if EnableNetwork == "Ds" else ""} —> {bot_name}切换到DeepSeek模型✨
       {reminder}云端{"（当前）" if _cloud_on else ""} —> 切到云端 DeepSeek（{_cloud_label}）☁️
       {reminder}本地{"（当前）" if not _cloud_on else ""} —> 切回本地模型（{_local_label}）🏠
       {reminder}后端状态 —> 查看当前 AI 后端 🔍
       {reminder}联网{"（当前）" if EnableNetwork == "WebSearch" else ""} —> {bot_name}开启联网搜索（每次回答先查网络，无需Key）🌐{plugins_help}
       {reminder}高数 + 题目截图 —> {bot_name}看图并解答高等数学题（也可直接发清晰题图自动识别）📐
       {reminder}语音 [音色:名称] [语速:倍数|快|慢|正常] 文字 —> 朗读这句话（微软神经语音）🔊
       {reminder}语音开 / {reminder}语音关 —> 开启/关闭「语音对话」（开启后回复只发语音、不另发文字）
       {reminder}音色 晓晓 / {reminder}音色 云希 / {reminder}音色 男生 / {reminder}音色 女生 —> 设定本会话声线（男生/女生均可选）
       {reminder}更改TTS状态 —> 切换本会话「语音对话」开关（默认关闭；首聊会主动问你）
       {reminder}插件视角 —> 看看{bot_name}又收集了哪些好好用的工具🔮
       {reminder}角色扮演 —> {bot_name}切换不同的角色互动噢！~
快来聊天吧(*≧︶≦)'''
    elif isinstance(event, Events.PrivateMessageEvent):
        return f'''如何与{bot_name}私聊( •̀ ω •́ )✧
       (任意问题，必填) —> {bot_name}回复
       {reminder}联网 —> {bot_name}开启联网搜索（无需Key，每次回答先查网络）🌐
       {reminder}角色扮演 —> {bot_name}切换不同的角色互动噢！~
       {reminder}语音 [音色:名称] [语速:倍数|快|慢|正常] 文字 —> 朗读这句话（微软神经语音）🔊
       {reminder}语音开 / {reminder}语音关 —> 开启/关闭「语音对话」（开启后回复只发语音、不另发文字）
       {reminder}音色 晓晓 / {reminder}音色 云希 / {reminder}音色 男生 / {reminder}音色 女生 —> 设定本会话声线（男生/女生均可选）
其余功能请到群聊中使用哦o((>ω< ))o
快来聊天吧(*≧︶≦)'''

# ===== 断线自动重连守护（10-03 新增）=====
# 此前 NapCat 重启 / QQ 掉线会抛 WebSocketConnectionClosedException，
# Hyper 内部只捕获 ConnectionResetError，该异常会穿透导致整个进程退出。
# 这里在外层兜底：断线或连接重试耗尽后等待几秒自动重连，进程不再崩溃；
# Ctrl+C 关闭窗口仍然可以正常退出（Hyper 内部会 os._exit）。

def preflight_check():
    """启动前依赖自检：检查语音所需依赖，缺失则给出安装提示；不阻断启动。"""
    print("\n==== 启动前依赖自检 ====")
    # 主引擎：edge_tts（微软神经语音，云端，自然好听，无需 Key）
    try:
        import edge_tts
        print("[OK] edge_tts 已安装（云端神经语音，默认引擎）")
    except ImportError:
        print("[缺失] edge_tts 未安装 —— 语音将回退到离线 pyttsx3（Huihui，音质较差）。")
        print("       修复：在本机项目 venv 执行   pip install edge_tts")
    # 离线兜底引擎：pyttsx3（仅在无网 / 云端不可用时启用）
    try:
        import pyttsx3
        print("[OK] pyttsx3 已安装（离线兜底引擎）")
    except ImportError:
        print("[缺失] pyttsx3 未安装 —— 离线兜底不可用；无网时语音将静默跳过（仅回文字）。")
        print("       修复：在本机项目 venv 执行   pip install pyttsx3 pywin32")
    print("[提示] 默认音色为微软小艺(女声)；可用 ~音色 晓晓 / 云希 / 男生 / 女生 等切换声线。")
    print("[提示] 语音「默认关闭」；首聊或长时间未聊会主动询问是否开启语音对话，")
    print("       开启后回复只发语音、不发文字；也可随时发 ~语音开 / ~语音关 切换。")
    # API Key 自检：config.json 里已留空、Key 走环境变量，这里打印来源，避免"以为填了其实没生效"
    try:
        print(f"[提示] API Key 来源：{ai_backend.api_key_source()}")
        if ai_backend.api_key_source().startswith("默认占位值"):
            print("       ⚠ 未检测到 Key：请在 config.json 填 deepseek_key，或设置环境变量 JIANER_API_KEY 后重开启动脚本")
    except Exception as _e:
        print(f"[提示] API Key 来源检查失败：{_e}")
    print("=======================\n")

preflight_check()


# ===================== 语音识别（ASR）=====================
async def _transcribe_voice(event, Segments):
    """把 event.message 里的语音片段就地转写为文字（Record -> Text）。
    无语音 / 未启用 / 失败都安全跳过，不影响原流程。"""
    try:
        from Tools import asr
    except Exception as e:
        print(f"[ASR] 模块导入失败: {e}")
        return
    try:
        _cfg = config.others
    except Exception:
        _cfg = {}
    if not _cfg.get("asr_enabled", True):
        return
    _recs = [s for s in event.message if isinstance(s, Segments.Record)]
    if not _recs:
        return
    import asyncio
    _model = _cfg.get("asr_model", "base")
    _lang = _cfg.get("asr_language", "zh")
    print(f"[ASR] 检测到 {len(_recs)} 条语音，开始转写（模型={_model}, 语言={_lang}）...")
    for _seg in _recs:
        try:
            _txt = await asyncio.to_thread(asr.transcribe_record, _seg, model=_model, language=_lang)
        except Exception as e:
            print(f"[ASR] 转写出错: {e}")
            _txt = ""
        _i = event.message.index(_seg)
        event.message[_i] = Segments.Text(_txt if _txt else "（语音内容未能识别）")
    print("[ASR] 转写完成")


while True:
    try:
        Listener.run()
        # run() 正常返回 = 连接重试次数耗尽（NapCat 没开或长时间不可达）
        print("[守护] 连接重试耗尽，5 秒后自动重连 NapCat ...")
    except Exception as _e:
        print(f"[守护] 连接断开：{type(_e).__name__}: {_e}")
        print("[守护] 5 秒后自动重连 NapCat ...（若 NapCat 已关闭，请先重新启动 NapCat/QQ）")
    time.sleep(5)