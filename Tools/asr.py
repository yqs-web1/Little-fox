"""
本地语音识别（ASR）模块 —— 让机器人「听懂」QQ 语音消息。

流程：OneBot Record 片段 -> 解析语音文件(silk) -> 解码为 wav -> faster-whisper 转写。
全程离线，不需要任何外部 API key。
  - silk 解码：pysilk（QQ 语音是 silk v3 编码，无 ffmpeg 也能解）
  - 识别引擎：faster-whisper（OpenAI Whisper 的 CTranslate2 实现，CPU 友好）
  - 模型从 HuggingFace 下载，已配置国内镜像 HF_ENDPOINT=https://hf-mirror.com

设计要点：
  - 模型懒加载并全局缓存，多线程串行调用（_RUN_LOCK）避免并发冲突。
  - 若缺依赖，函数抛清晰错误，由调用方捕获，机器人不会崩。
  - 转写在线程池执行，不阻塞 Hyper 事件循环。
"""

import os
import json
import base64
import tempfile
import threading

# 国内机器从 HF 镜像拉模型，避免直连 huggingface.co 超时
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

_MODEL = None
_MODEL_NAME = None
_LOAD_LOCK = threading.Lock()   # 仅用于模型加载
_RUN_LOCK = threading.Lock()    # 序列化 transcribe 调用


def _cfg_number(key, default):
    """从 config.json 的 Others 段取一个数字（控制台可改这两项）。

    asr_timeout_seconds : 下载语音的超时秒数（默认 30）
    asr_max_seconds     : 单条语音最长处理秒数，0 = 不限制（默认 0）
    读不到（没接 Configurator / 没配这项）就用默认值，绝不让 ASR 因为读配置而失败。
    """
    try:
        from Hyper import Configurator
        raw = (Configurator.cm.get_cfg().others or {}).get(key)
        if raw is None or raw == "":
            return default
        return float(raw)
    except Exception:
        return default


def _load_model(name="base"):
    global _MODEL, _MODEL_NAME
    with _LOAD_LOCK:
        if _MODEL is not None and _MODEL_NAME == name:
            return _MODEL
        try:
            from faster_whisper import WhisperModel
        except Exception as e:
            raise RuntimeError(
                "faster-whisper 未安装。请在 venv 执行：pip install faster-whisper"
            ) from e
        # 优先用本地模型目录（手动下载，避免 huggingface_hub 缓存异常）
        _local = os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "models", f"faster-whisper-{name}"))
        if os.path.isdir(_local) and os.path.isfile(os.path.join(_local, "model.bin")):
            print(f"[ASR] 从本地目录加载模型: {_local}")
            _MODEL = WhisperModel(_local, device="cpu", compute_type="int8")
        else:
            # 回退：从 HuggingFace Hub 下载/加载
            _MODEL = WhisperModel(name, device="cpu", compute_type="int8")
        _MODEL_NAME = name
        return _MODEL


def _resolve_record_file(seg):
    """从 Record 片段解析出本地可读取的音频文件路径。"""
    f = (getattr(seg, "url", "") or getattr(seg, "file", "") or "").strip()
    if not f:
        return None
    if f.startswith("base64:"):
        raw = base64.b64decode(f[len("base64:"):])
        p = tempfile.mktemp(suffix=".silk")
        with open(p, "wb") as fp:
            fp.write(raw)
        return p
    if f.startswith("http://") or f.startswith("https://"):
        return _download(f)
    if f.startswith("file://"):
        return f[len("file://"):]
    if os.path.isfile(f):
        return f
    return None


def _download(url, timeout=None):
    import urllib.request
    if not timeout:
        timeout = _cfg_number("asr_timeout_seconds", 30)   # 控制台「ASR 下载超时」可调
    p = tempfile.mktemp(suffix=".silk")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(p, "wb") as fp:
        fp.write(resp.read())
    return p


def _get_ffmpeg():
    """定位 ffmpeg 二进制：优先项目自带完整版（含 silk 解码器）。"""
    # 1) 项目自带完整版 ffmpeg（下载自 johnvansickle，含 silk 解码器）
    _local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "ffmpeg.exe")
    if os.path.isfile(_local):
        return _local
    # 2) imageio-ffmpeg 自带的（精简版，可能不含 silk）
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    # 3) 系统 PATH 中的 ffmpeg
    import shutil
    _p = shutil.which("ffmpeg")
    if _p:
        return _p
    raise RuntimeError(
        "找不到 ffmpeg。请在 venv 执行：pip install imageio-ffmpeg，"
        "或把完整版 ffmpeg.exe 放到 Tools/ffmpeg/"
    )


def _wrap_pcm_to_wav(pcm_path, wav_path, sample_rate=24000):
    """把 pysilk 解码出的裸 PCM(16bit 单声道) 包成标准 wav。"""
    import wave
    with open(pcm_path, "rb") as fp:
        pcm = fp.read()
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)


def _decode_silk_to_wav(path):
    """silk v3 -> wav。已为标准格式（wav/mp3/...）则原样返回。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".wav", ".mp3", ".flac", ".m4a", ".ogg"):
        return path
    # 优先用 pysilk 解码 silk v3 -> 裸 PCM -> wav（QQ 语音通常是 24kHz）
    try:
        import pysilk
        _pcm = path + ".pcm"
        with open(path, "rb") as _fin, open(_pcm, "wb") as _fout:
            pysilk.decode(_fin, _fout, sample_rate=24000)
        _out = path + ".wav"
        _wrap_pcm_to_wav(_pcm, _out, sample_rate=24000)
        return _out
    except Exception as e:
        print(f"[ASR] pysilk 解码失败（{e}），尝试 ffmpeg 回退")
    # 回退：ffmpeg（需含 silk 解码器的完整版，放在 Tools/ffmpeg/ 或 PATH）
    _ff = _get_ffmpeg()
    _out = path + ".wav"
    import subprocess
    try:
        subprocess.run([_ff, "-y", "-i", path, _out], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as ex:
        raise RuntimeError(
            f"ffmpeg 解码语音失败（{ex}）；若报缺少解码器，请放置完整版 ffmpeg 到 Tools/ffmpeg/"
        ) from ex
    if not os.path.isfile(_out):
        raise RuntimeError("ffmpeg 未生成 wav 文件")
    return _out


def transcribe_record(seg, model="base", language="zh"):
    """给定 OneBot Record 片段，返回转写文本；失败/空返回空串。"""
    path = _resolve_record_file(seg)
    if not path:
        print("[ASR] 无法解析语音文件路径")
        return ""
    wav = _decode_silk_to_wav(path)
    return transcribe_wav(wav, model=model, language=language)


def _is_riff_wav(path):
    """按文件头判断是不是真正的 wav（不能看扩展名，见 _ensure_16k_wav 说明）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    except Exception:
        return False


def _ensure_16k_wav(path):
    """保证拿到真正的 16k 单声道 wav。

    注意：这里必须按文件头判断真实格式，**不能只看扩展名** ——
    edge-tts 保存出来的文件虽然叫 .wav，内容其实是 MP3
    （audio-24khz-48kbitrate-mono-mp3），直接交给标准库 wave 会报
        wave.Error: file does not start with RIFF id
    非真 wav 一律用项目自带的完整版 ffmpeg 转成 16k 单声道 wav。
    """
    if _is_riff_wav(path):
        return path
    import subprocess
    _ff = _get_ffmpeg()
    _out = os.path.splitext(path)[0] + ".16k.wav"
    subprocess.run([_ff, "-y", "-i", path, "-ar", "16000", "-ac", "1", _out],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.isfile(_out):
        raise RuntimeError("ffmpeg 未生成 16k wav")
    return _out


def _decode_wav_to_float32(path, target_sr=16000):
    """wav -> faster-whisper 需要的 float32 单声道 numpy 数组。

    为什么不用 faster-whisper 自带的解码器：它内部走 PyAV 的
        av.open(input_file, mode="r", metadata_errors="ignore")
    而 PyAV 19 已移除 metadata_errors 参数，会抛
        TypeError: open() got an unexpected keyword argument 'metadata_errors'
    直接传 numpy 数组可让 faster-whisper 跳过 decode_audio（见其 transcribe() 里的
    `if not isinstance(audio, np.ndarray)` 判断），从而完全绕开该不兼容，
    且不引入任何新的第三方依赖（wave 是标准库，numpy 本项目已依赖）。
    """
    import wave
    import numpy as np

    with wave.open(path, "rb") as wf:
        n_ch = wf.getnchannels()
        sw = wf.getsampwidth()
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    if sw == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif sw == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise RuntimeError(f"不支持的采样位宽：{sw * 8} bit")

    if n_ch > 1:
        data = data.reshape(-1, n_ch).mean(axis=1)

    # whisper 固定要求 16kHz；QQ 语音解码出来通常是 24kHz，这里线性重采样
    if sr != target_sr and len(data) > 1:
        n_out = int(round(len(data) * target_sr / sr))
        data = np.interp(np.linspace(0, len(data) - 1, n_out),
                         np.arange(len(data)), data).astype(np.float32)

    return np.ascontiguousarray(data, dtype=np.float32)


def transcribe_wav(wav_path, model="base", language="zh"):
    m = _load_model(model)
    audio = _decode_wav_to_float32(_ensure_16k_wav(wav_path))

    # 超长语音只转写前 N 秒（asr_max_seconds，0=不限制）：避免一条 5 分钟的语音
    # 把 _RUN_LOCK 占住、后面排队的消息全部堵死。
    _max_s = _cfg_number("asr_max_seconds", 0)
    if _max_s and len(audio) > int(_max_s * 16000):
        _orig = len(audio) / 16000.0
        audio = audio[: int(_max_s * 16000)]
        print(f"[ASR] 语音 {_orig:.1f}s 超过 asr_max_seconds={_max_s:.0f}，只转写前 {_max_s:.0f} 秒")

    # Whisper 中文默认会输出繁体且不带标点。给一句简体的 initial_prompt
    # 作为解码偏置，可稳定得到「简体 + 带标点」的结果（实测有效）。
    _prompt = "以下是普通话的句子，请用简体中文转写，并加上标点符号。" if language == "zh" else None
    with _RUN_LOCK:
        segments, _info = m.transcribe(audio, language=language, beam_size=5,
                                       initial_prompt=_prompt)
        text = "".join(s.text for s in segments).strip()
    return text
