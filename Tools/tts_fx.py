"""趣味音效：用项目自带的完整版 ffmpeg 给合成音频做后处理（需求③）。

为什么音效单独成模块：音色（谁在说）和音效（怎么说）是正交的两件事。
任意音色都能叠任意音效，写法 `晓色+花栗鼠`。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
import tempfile


def ffmpeg_path():
    """优先项目自带的完整版 ffmpeg（Tools/ffmpeg/ffmpeg.exe，64MB，滤镜齐全）。"""
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg", "ffmpeg.exe")
    if os.path.isfile(local):
        return local
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    return shutil.which("ffmpeg")


def apply_fx(src: str, chain: str, timeout: float = 30.0):
    """对音频文件应用 ffmpeg 滤镜链，返回新文件路径。

    失败一律返回 None —— 调用方回落到原音频，绝不因为音效把语音搞没。
    """
    if not chain:
        return src
    ff = ffmpeg_path()
    if not ff:
        print("[音效] 找不到 ffmpeg，跳过音效")
        return None
    dst = os.path.join(tempfile.gettempdir(), f"foxfx_{uuid.uuid4().hex}.wav")
    try:
        subprocess.run(
            [ff, "-y", "-i", src, "-af", chain, "-ar", "24000", "-ac", "1", dst],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout,
        )
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        print(f"[音效] ffmpeg 失败（{chain[:50]}）：{tail[-1] if tail else e}")
        return None
    except Exception as e:
        print(f"[音效] 应用失败：{type(e).__name__}: {e}")
        return None
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        print("[音效] ffmpeg 产物为空，回落原音频")
        return None
    return dst


def cleanup(path: str):
    try:
        if path and os.path.isfile(path) and os.path.basename(path).startswith("foxfx_"):
            os.remove(path)
    except Exception:
        pass
