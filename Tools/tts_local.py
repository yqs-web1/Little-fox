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
import uuid


# ---- 音色表（需求③：7 → 30，含方言/粤语/台湾腔/日语/英语/韩语 + 趣味音效）----
# 原注释说新版音色"该端点不返回音频"所以人工剔除；现在改为
# **全部收录 + 启动自动探活**，只把真能出声的展示给用户（Tools/tts_health.py）。
# 音色/音效/情绪规则都在 Tools/tts_voices.py。
from Tools.tts_voices import (          # noqa: E402
    EDGE_VOICES, GENDER_DEFAULT, DEFAULT_EDGE_VOICE, VOICES, FX_PRESETS,
    RANDOM_NAMES, EMOTION_NAME, is_voice_name, is_fx_name, split_selection,
    emotion_pick, voice_id_of, fx_chain, grouped_listing,
)

try:
    from Tools import tts_health
    tts_health.start_background_probe()
except Exception as _e:
    print(f"[音色探活] 启动失败（不影响使用）：{_e}")


def list_voices() -> str:
    """按分类列出可用音色 + 音效，用于 ~音色 帮助。"""
    try:
        from Tools import tts_health
        h = tts_health.healthy_ids()
    except Exception:
        h = None
    return grouped_listing(h)


def resolve_voice_and_fx(sel, text: str = ""):
    """把用户的选择解析成 (voice_id, fx滤镜链或None)。

    支持：音色名 / 性别快捷 / 完整 edge id / 音效名 / '音色+音效' /
          '随机'（每次随机挑一个可用音色）/ '情绪'（按回复文本挑）。
    """
    import random as _random

    name, fx_name = split_selection(sel)
    if not name and not fx_name:
        name = sel

    # 情绪联动
    if name == EMOTION_NAME:
        name, fx_name = emotion_pick(text)

    # 随机音色
    if name in RANDOM_NAMES:
        try:
            from Tools import tts_health
            pool = tts_health.healthy_ids()
        except Exception:
            pool = None
        cands = [v["id"] for v in VOICES.values() if (pool is None or v["id"] in pool)]
        name = _random.choice(cands) if cands else None

    if not name:
        vid = DEFAULT_EDGE_VOICE
    else:
        vid = voice_id_of(name) or name      # 完整 id / SAPI5 子串 直接透传

    chain = fx_chain(fx_name) if fx_name else None
    return vid, chain



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
    # 同样修复并发竞态：原实现是「检查存在 → 递增」，两线程可选中同一文件互相覆盖。
    out_dir = os.path.abspath("./responseVoice")
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"tts_local_{uuid.uuid4().hex}.wav")


# ---- 对外：合成，返回 wav 绝对路径；失败返回 False -----------------------
def _cfg_tts() -> dict:
    """config.json → Others.TTS（全局语音参数，控制台可改）。

    以前这几项在配置里存在但没人读，等于摆设；现在作为"没指定时"的默认值：
      voiceColor : 默认声线（填友好名或完整 zh-CN-* id）
      rate / volume / pitch : edge-tts 的相对值，形如 +20% / -10% / +5Hz
    读不到就返回空 dict，行为与改动前完全一致。
    """
    try:
        from Hyper import Configurator
        tts = (Configurator.cm.get_cfg().others or {}).get("TTS")
        return tts if isinstance(tts, dict) else {}
    except Exception:
        return {}


def speakable_text(text) -> str:
    """TTS 之前统一净化，返回**真正可朗读**的文本；空串 = 这段内容没有可读的字（纯表情/符号）。

    单独抽出来的原因：调用方必须能区分两种"没读出声音"——
      A. 没有可读内容（😊 / ** / 颜文字）—— 不是故障，应该直接发文字或提示用户；
      B. 合成或发送失败 —— 是故障，才该报"请检查联网"。
    以前两种情况走同一句"语音合成失败：请确认本机已联网…"，把 A 也误报成网络问题。
    """
    try:
        from Tools.Sanitizer_Tools import sanitize_for_tts
        return (sanitize_for_tts(text) or "").strip()
    except Exception:
        return (text or "").strip()


async def tts_local(text, voice=None, rate=0, volume=100):
    if not text or not str(text).strip():
        return False
    # 纯表情/符号净化后为空：不必往下走 edge→pyttsx3 两条注定失败的链路（还会白等好几秒）
    if not speakable_text(text):
        print("[本地TTS] 这段内容没有可朗读的文字（纯表情/符号），跳过合成")
        return False
    # 需求③：解析「音色 + 音效」，支持 随机 / 情绪 / "晓晓+花栗鼠" 组合
    _cfg = _cfg_tts()
    if not voice and _cfg.get("voiceColor"):
        voice = str(_cfg["voiceColor"]).strip() or None      # 全局默认声线（控制台里可改）
    v, fx = resolve_voice_and_fx(voice, str(text))
    r = _rate_to_edge(rate / 200.0) if rate else str(_cfg.get("rate") or "+0%")
    _vol = str(_cfg.get("volume") or "+0%")
    _pitch = str(_cfg.get("pitch") or "+0Hz")

    # 1) 优先云端神经语音（自然好听，无需 Key）
    try:
        from Tools.tools import amain
        out = await amain(str(text), v, r, _vol, _pitch)
        if not out and v != DEFAULT_EDGE_VOICE:
            # 选中的音色没产出音频时先回退默认音色重试一次。
            # 典型场景：选了英语/日语音色却要念中文 —— 端点会返回 NoAudioReceived，
            # 直接跌到 pyttsx3 是机械音，先试默认中文音色体验好得多。
            print(f"[本地TTS] 音色 {v} 未产出音频，回退默认音色重试")
            out = await amain(str(text), DEFAULT_EDGE_VOICE, r, _vol, _pitch)
        if out:
            # 叠音效：失败自动回落原音频，绝不因为音效把语音搞没
            if fx:
                try:
                    from Tools import tts_fx
                    fxed = await asyncio.to_thread(tts_fx.apply_fx, out, fx)
                    if fxed and fxed != out:
                        try:
                            os.remove(out)
                        except Exception:
                            pass
                        return fxed
                except Exception as _fe:
                    print(f"[本地TTS] 音效应用失败，回落原音：{_fe}")
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
                         voice=None, rate=0, volume=100, reply_to=None):
    """云端/离线合成 text 并作为 QQ 语音发送。成功返回 True，失败返回 False。

    reply_to: 私聊下要引用的消息 ID（None = 不引用）。
              语音也是一条回复，所以私聊时同样按「引用回复」的约定带引用段。
    """
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

    def _build(payload):
        segs = []
        if gid is None and reply_to is not None:
            segs.append(Segments.Reply(reply_to))
        segs.append(Segments.Record(payload))
        return Manager.Message(*segs)

    # 语音也是一条回复 —— 发送前先把「正在思考」占位气泡收掉
    try:
        from Tools import bubble
        await bubble.clear(actions, event)
    except Exception as _e:
        print(f"[本地TTS] 清理占位气泡失败（忽略）：{type(_e).__name__}: {_e}")

    try:
        if gid is not None:
            await actions.send(group_id=gid, message=_build(record_file))
        else:
            await actions.send(user_id=event.user_id, message=_build(record_file))
    except Exception as _e:
        # base64 发送异常时回退为文件路径发送
        print(f"[本地TTS] base64 发送失败，尝试文件路径发送：{_e}")
        try:
            if gid is not None:
                await actions.send(group_id=gid, message=_build(wav))
            else:
                await actions.send(user_id=event.user_id, message=_build(wav))
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
