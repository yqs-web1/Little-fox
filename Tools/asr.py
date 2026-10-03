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


def _download(url, timeout=30):
    import urllib.request
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


def transcribe_wav(wav_path, model="base", language="zh"):
    m = _load_model(model)
    with _RUN_LOCK:
        segments, _info = m.transcribe(wav_path, language=language, beam_size=5)
        text = "".join(s.text for s in segments).strip()
    return text
