# Tools/tts_local.py
# 语音合成（TTS）封装 —— 用于「~语音 文字」按需指令 与 自动语音回复。
#
# 默认引擎：edge_tts（微软神经语音，云端、自然好听、无需 API Key、免费）。
# 兜底引擎：pyttsx3（系统 SAPI5，完全离线；仅在 edge_tts 不可用/无网时启用，音质一般）。
# 依赖：edge_tts 已由项目安装；pyttsx3 可选（离线兜底）。两者皆缺则优雅降级返回 False，机器人不崩。
#
# 设计要点：
#   1. 合成与发送解耦：tts_local() 只负责“文字→wav 文件”，
#      speak_and_send() 负责“wav→base64→QQ 语音段→发送”。
#   2. 群聊/私聊自动判定：speak_and_send 通过 event 是否带 group_id 决定发群还是发私聊。
#   3. base64 内联发送：NapCat 从内存转码 silk，不依赖磁盘文件。

import os
import re
import base64
import asyncio
import traceback


# ---- 友好音色名 -> edge_tts 神经语音 id（男声=Yun*，女声=Xiao*）----
# 仅收录「当前网络端点（消费级 edge 端点）实测可正常合成」的音色。
# 微软较新的 Latest 语音（Xiaohan/Xiaorui/Xiaomo/Xiaochen 等 及 Yunze 云泽）在该端点
# 不返回音频（No audio was received），故不收录，避免用户选中后合成失败/回退。
EDGE_VOICES = {
    # 女声（经典 zh-CN 女声，已实测可用）
    "小艺": "zh-CN-XiaoyiNeural",     # 温柔女声（默认）
    "晓晓": "zh-CN-XiaoxiaoNeural",   # 活泼女声
    "晓萱": "zh-CN-XiaoxuanNeural",   # 温婉女声
    # 男声（经典 zh-CN 男声，已实测可用）
    "云希": "zh-CN-YunxiNeural",      # 阳光男声
    "云扬": "zh-CN-YunyangNeural",    # 专业男声（新闻感）
    "云健": "zh-CN-YunjianNeural",    # 沉稳男声
    "云夏": "zh-CN-YunxiaNeural",     # 清新男声
}
DEFAULT_EDGE_VOICE = "zh-CN-XiaoyiNeural"

# 性别快捷：男生 / 女生 各取一个有代表性的默认声线
GENDER_DEFAULT = {"男生": "zh-CN-YunxiNeural", "女生": "zh-CN-XiaoyiNeural"}


def list_voices() -> str:
    """返回所有可选声线的格式化清单（按性别分组），用于 ~音色 帮助。"""
    male, female = [], []
    for name, vid in EDGE_VOICES.items():
        line = f"  {name}（{'男' if vid.startswith('zh-CN-Yun') else '女'}声）" + ("（默认）" if vid == DEFAULT_EDGE_VOICE else "")
        (male if vid.startswith("zh-CN-Yun") else female).append(line)
    return "可选女声：\n" + "\n".join(female) + "\n可选男声：\n" + "\n".join(male)


# ---- 解析 ~语音 指令参数 ---------------------------------------------------
# 语法： [音色:名称] [语速:倍数|快|慢|正常] 要朗读的正文
#   音色:小艺 / 音色:晓晓 / 音色:云希 ……  或直接使用完整 id（如 zh-CN-YunxiNeural）
#   语速:1.3 / 语速:快 / 语速:慢 ……
def parse_voice_args(rest: str):
    """从 ~语音 后的内容解析 音色/语速/正文，返回 (text, voice, rate)。
    rate 为绝对 wpm（0 表示用默认）。"""
    text = (rest or "").strip()
    voice = None
    rate = 0

    # 音色：音色:晓妍 / 音色：XiaoY
    m = re.search(r"音色[:：]\s*(\S+)", text)
    if m:
        voice = m.group(1).strip("\"'“”")
        text = (text[:m.start()] + text[m.end():]).strip()

    # 语速：语速:1.3 / 语速:快 / 语速:0.8
    m = re.search(r"语速[:：]\s*(\S+)", text)
    if m:
        token = m.group(1).strip("\"'“”")
        preset = {"快": 1.3, "稍快": 1.15, "慢": 0.75, "稍慢": 0.85,
                  "正常": 1.0, "标准": 1.0}
        if token in preset:
            rate = int(200 * preset[token])
        else:
            try:
                rate = int(200 * max(0.4, min(2.5, float(token))))
            except ValueError:
                pass  # 无法解析则保持默认语速
        text = (text[:m.start()] + text[m.end():]).strip()

    return text, voice, rate


def _resolve_voice(voice):
    """名称 -> edge_tts 语音 id。支持友好名 / 性别快捷(男生|女生) / 完整 id / 透传（作 SAPI5 子串）。"""
    if not voice:
        return DEFAULT_EDGE_VOICE
    if voice in EDGE_VOICES:
        return EDGE_VOICES[voice]
    if voice in GENDER_DEFAULT:
        return GENDER_DEFAULT[voice]
    return voice  # 完整 edge_tts id 或 SAPI5 子串，透传


def _rate_to_edge(mult):
    """倍数(相对默认) -> edge_tts 相对速率，如 1.3 -> +30%。"""
    pct = int(round((mult - 1.0) * 100))
    return f"{'+' if pct >= 0 else '-'}{abs(pct)}%"


# ---- 离线兜底：pyttsx3 合成到 wav（同步，放线程跑）---------------------------
def _pick_chinese_voice(engine):
    try:
        for v in (engine.getProperty("voices") or []):
            if any(k in (getattr(v, "name", "") + getattr(v, "id", "")).lower()
                   for k in ("chinese", "中文", "zh", "普通话")):
                return v.id
    except Exception:
        pass
    return None


def _speak_pyttsx3(text, out_path, voice=None, rate=0, volume=100):
    import pyttsx3
    engine = pyttsx3.init()
    try:
        if not voice:
            voice = _pick_chinese_voice(engine)
        if voice:
            try:
                engine.setProperty("voice", voice)
            except Exception:
                pass
        if rate and int(rate) != 0:
            engine.setProperty("rate", int(rate))
        if volume and int(volume) != 100:
            engine.setProperty("volume", max(0.0, min(1.0, int(volume) / 100.0)))
        engine.save_to_file(text, out_path)
        engine.runAndWait()
    finally:
        try:
            engine.stop()
        except Exception:
            pass
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    return False


def _next_wav_path():
    out_dir = os.path.abspath("./responseVoice")
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    while True:
        cand = os.path.join(out_dir, f"tts_local_{n}.wav")
        if not os.path.exists(cand):
            return cand
        n += 1
        if n > 9999:
            return os.path.join(out_dir, f"tts_local_{os.getpid()}.wav")


# ---- 对外：合成，返回 wav 绝对路径；失败返回 False -----------------------
async def tts_local(text, voice=None, rate=0, volume=100):
    if not text or not str(text).strip():
        return False
    v = _resolve_voice(voice)
    r = _rate_to_edge(rate / 200.0) if rate else "+0%"  # rate=0 用默认 +0%

    # 1) 优先云端神经语音（自然好听，无需 Key）
    try:
        from Tools.tools import amain
        out = await amain(str(text), v, r, "+0%", "+0Hz")
        if out:
            return out
        print("[本地TTS] edge_tts 未产出音频，尝试离线兜底")
    except ImportError:
        print("[本地TTS] edge_tts 未安装，回退离线 pyttsx3")
    except Exception as e:
        print(f"[本地TTS/edge] 云端合成失败（可能无网）：{e}")

    # 2) 兜底离线 pyttsx3（音质一般，仅无网/云端不可用时）
    try:
        return await asyncio.to_thread(_speak_pyttsx3, text, _next_wav_path(), voice, rate, volume)
    except ImportError:
        print("[本地TTS] pyttsx3 也未安装：请在本机 venv 执行 pip install pyttsx3 pywin32")
        return False
    except Exception as e:
        print(f"[本地TTS/pyttsx3] 离线合成也失败：{e}\n{traceback.format_exc()}")
        return False


# ---- 对外：合成并发送语音（自动判定群聊/私聊，base64 内联）------------------
async def speak_and_send(actions, Manager, Segments, event, text,
                         voice=None, rate=0, volume=100):
    """云端/离线合成 text 并作为 QQ 语音发送。成功返回 True，失败返回 False。"""
    wav = await tts_local(text, voice=voice, rate=rate, volume=volume)
    if not wav:
        return False

    try:
        with open(wav, "rb") as _f:
            record_file = "base64://" + base64.b64encode(_f.read()).decode("ascii")
    except Exception as _e:
        print(f"[本地TTS] 音频 base64 编码失败：{_e}")
        return False

    # 群聊事件带 group_id，私聊事件不带 —— 据此自动选发送目标
    gid = getattr(event, "group_id", None)
    try:
        if gid is not None:
            await actions.send(group_id=gid,
                               message=Manager.Message(Segments.Record(record_file)))
        else:
            await actions.send(user_id=event.user_id,
                               message=Manager.Message(Segments.Record(record_file)))
    except Exception as _e:
        # base64 发送异常时回退为文件路径发送
        print(f"[本地TTS] base64 发送失败，尝试文件路径发送：{_e}")
        try:
            if gid is not None:
                await actions.send(group_id=gid,
                                   message=Manager.Message(Segments.Record(wav)))
            else:
                await actions.send(user_id=event.user_id,
                                   message=Manager.Message(Segments.Record(wav)))
        except Exception as _e2:
            print(f"[本地TTS] 文件回退发送也失败：{_e2}")
            return False

    # base64 内联后 NapCat 不再依赖磁盘文件，立即清理本地缓存
    try:
        if os.path.exists(wav):
            os.remove(wav)
    except Exception as _e:
        print(f"[本地TTS] 删除缓存音频失败：{_e}")

    return True
