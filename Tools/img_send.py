"""图片发送公共模块：把本地图片可靠地发给 QQ/NapCat。

发图策略（防御式）：
1. 优先用 file:/// 绝对路径（正斜杠、三斜杠）发送——NapCat 直接读本地文件，最稳，
   且能绕开 Hyper 把 Windows 路径拼成 file://C:/... 这种畸形 URI 的 bug；
2. 若本地文件发送抛异常，回退到 base64://（不依赖 NapCat 读盘，但体积会膨胀约 33%）；
3. 两者都失败才返回异常说明，绝不静默吞掉。

另外提供 _is_image_bytes 用文件头魔数判定真实图片，避免被不规范的 content-type 误杀。
"""
import os
import random
import base64
import io
from PIL import Image


def _is_image_bytes(b: bytes) -> bool:
    if not b or len(b) < 12:
        return False
    if b[:3] == b"\xff\xd8\xff":                       # JPEG
        return True
    if b[:8] == b"\x89PNG\r\n\x1a\n":                 # PNG
        return True
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":        # WebP
        return True
    if b[:6] in (b"GIF87a", b"GIF89a"):                # GIF
        return True
    return False


def _to_jpg_file(path: str) -> str:
    """把图片（含 webp）转成 JPG 落盘，返回 jpg 路径；失败则原样返回 path。"""
    jpg = os.path.abspath(f"temps/_jpg_{os.getpid()}_{random.randint(0, 999999999)}.jpg")
    try:
        with Image.open(path) as im:
            if im.mode in ("RGBA", "P", "LA"):
                im = im.convert("RGB")
            im.save(jpg, format="JPEG", quality=92)
        return jpg
    except Exception:
        return path


def img_to_b64(path: str) -> str:
    """把图片转 JPG 后以 base64:// 形式返回（兜底发送用）。"""
    try:
        with Image.open(path) as im:
            if im.mode in ("RGBA", "P", "LA"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=92)
            data = buf.getvalue()
    except Exception:
        with open(path, "rb") as f:
            data = f.read()
    return "base64://" + base64.b64encode(data).decode("ascii")


def _cleanup(*paths):
    for p in paths:
        try:
            os.remove(p)
        except Exception:
            pass


async def send_image(actions, Manager, Segments, group_id, path, text: str = ""):
    """发图主入口：file:/// 优先，base64:// 兜底。

    返回 None 表示发送成功（会清理临时文件）；否则返回异常说明文本。
    """
    jpg = _to_jpg_file(path)
    uri = "file:///" + os.path.abspath(jpg).replace("\\", "/")
    segs = [Segments.Image(uri)]
    if text:
        segs.append(Segments.Text(text))
    try:
        await actions.send(group_id=group_id, message=Manager.Message(*segs))
        _cleanup(path, jpg)
        return None
    except Exception as e1:
        try:
            segs2 = [Segments.Image(img_to_b64(jpg))]
            if text:
                segs2.append(Segments.Text(text))
            await actions.send(group_id=group_id, message=Manager.Message(*segs2))
            _cleanup(path, jpg)
            return None
        except Exception as e2:
            return f"{type(e1).__name__}: {e1}；base64也失败: {type(e2).__name__}: {e2}"
