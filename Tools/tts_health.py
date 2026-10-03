"""音色探活（需求③）。

要解决的原问题：tts_local.py 里的注释说
    「微软较新的 Latest 语音（Xiaohan/Xiaorui/Xiaomo/Xiaochen 及 Yunze）
      在该端点不返回音频，故不收录」
也就是说，光看名字无法判断某个音色到底能不能出声，而且随网络/时间还会变。

本模块对每个候选音色合成一句极短文本，成功的记进 data/voice_health.json，
~音色 列表只展示**真能出声的**。启动时后台异步探活，不阻塞机器人。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
import uuid

from Tools import tts_voices

_BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(_BOT_DIR, "data", "voice_health.json")
_PROBE_TEXT = "你好呀"
_PROBE_TTL = 7 * 86400      # 一周内不重复探活

_lock = threading.Lock()
_healthy: set | None = None      # 通过探活的 voice id 集合；None = 还没探测过
_probing = False


def _load_cache():
    try:
        d = json.load(open(CACHE, encoding="utf-8"))
        ts = d.get("ts") or 0
        if time.time() - ts > _PROBE_TTL:
            return None, 0
        return set(d.get("healthy") or []), ts
    except Exception:
        return None, 0


def _save_cache(healthy: set):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "healthy": sorted(healthy)}, f,
                      ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[音色探活] 写缓存失败：{e}")


def healthy_ids() -> set | None:
    """返回已确认可用的 voice id 集合；None 表示从未探活过（此时展示全部）。"""
    with _lock:
        return set(_healthy) if _healthy is not None else None


def _probe_one(vid: str, timeout: float = 12.0):
    out = os.path.join(tempfile.gettempdir(), f"vh_{uuid.uuid4().hex}.mp3")
    # 探活文案按语种选：用中文探英语音色会被端点判为无音频，误杀好音色
    text = tts_voices.probe_text_for(vid)
    try:
        import edge_tts

        async def _run():
            c = edge_tts.Communicate(text, vid)
            await asyncio.wait_for(c.save(out), timeout=timeout)

        asyncio.run(_run())
        size = os.path.getsize(out) if os.path.isfile(out) else 0
        return (size > 600), ("" if size > 600 else "返回空音频")
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:70]}"
    finally:
        try:
            if os.path.exists(out):
                os.remove(out)
        except Exception:
            pass


def probe_all(concurrency: int = 4, verbose: bool = True) -> dict:
    """探活全部候选音色。返回 {id: (ok, msg)}。"""
    global _healthy, _probing
    with _lock:
        if _probing:
            return {}
        _probing = True
    results: dict = {}
    try:
        from concurrent.futures import ThreadPoolExecutor
        items = [(n, v["id"]) for n, v in tts_voices.VOICES.items()]
        with ThreadPoolExecutor(max_workers=max(1, concurrency),
                                thread_name_prefix="voice-probe") as pool:
            futs = {pool.submit(_probe_one, vid): (n, vid) for n, vid in items}
            for f in futs:
                n, vid = futs[f]
                try:
                    ok, msg = f.result()
                except Exception as e:
                    ok, msg = False, f"{type(e).__name__}: {e}"
                results[vid] = (ok, msg)
                if verbose and not ok:
                    print(f"[音色探活] ✘ {n}({vid})：{msg}")
        good = {vid for vid, (ok, _m) in results.items() if ok}
        with _lock:
            _healthy = good
        _save_cache(good)
        if verbose:
            print(f"[音色探活] 完成：{len(good)}/{len(results)} 个音色可用"
                  f"（不可用的不会出现在 ~音色 列表里）")
    except Exception as e:
        print(f"[音色探活] 整体失败：{type(e).__name__}: {e}")
    finally:
        with _lock:
            _probing = False
    return results


def start_background_probe(force: bool = False):
    """启动时调用：读缓存，必要时后台线程探活（不阻塞启动）。"""
    global _healthy
    cached, ts = _load_cache()
    if cached is not None and not force:
        with _lock:
            _healthy = cached
        age_h = (time.time() - ts) / 3600 if ts else 0
        print(f"[音色探活] 使用缓存：{len(cached)} 个音色可用（{age_h:.1f} 小时前探测）")
        return
    t = threading.Thread(target=probe_all, kwargs={"verbose": True},
                         name="voice-probe", daemon=True)
    t.start()
    print("[音色探活] 已开始后台探活（不影响启动）")


def summary() -> str:
    h = healthy_ids()
    if h is None:
        return "尚未探活（发 ~音色 检测 可立即探测）"
    total = len(tts_voices.VOICES)
    bad = [n for n, v in tts_voices.VOICES.items() if v["id"] not in h]
    head = f"音色探活：{len(h)}/{total} 可用"
    if bad:
        head += "\n不可用：" + "、".join(bad[:12]) + ("…" if len(bad) > 12 else "")
    return head
