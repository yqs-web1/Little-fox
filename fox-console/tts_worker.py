# -*- coding: utf-8 -*-
"""edge-tts 子进程工作器。

单独放一个文件，是为了让控制台主进程不必依赖 edge-tts / asyncio 的事件循环，
并且合成失败时错误信息能落到控制台的 logs/ 里。

用法：
    python tts_worker.py --list
    python tts_worker.py --say <输出.mp3> <文本> <voice> <rate> <volume> <pitch>
"""
from __future__ import annotations

import asyncio
import json
import sys


def do_list() -> int:
    try:
        import edge_tts
    except Exception as exc:
        print(f"edge-tts 不可用：{exc}")
        return 3
    try:
        voices = asyncio.run(edge_tts.list_voices())
    except Exception as exc:
        print(f"获取声线清单失败：{exc}")
        return 4
    out = []
    for v in voices:
        locale = str(v.get("Locale") or "")
        if not locale.lower().startswith("zh"):
            continue
        out.append({
            "name": v.get("ShortName"),
            "gender": v.get("Gender"),
            "locale": locale,
            "friendly": v.get("FriendlyName"),
            "local_name": v.get("LocalName") or (v.get("VoiceTag") or {}).get("LocalName"),
        })
    out.sort(key=lambda x: x["name"] or "")
    print(json.dumps(out, ensure_ascii=False))
    return 0


def do_say(args: list) -> int:
    if len(args) < 6:
        print("参数不足：需要 <输出> <文本> <voice> <rate> <volume> <pitch>")
        return 2
    out_path, text, voice, rate, volume, pitch = args[:6]
    try:
        import edge_tts
    except Exception as exc:
        print(f"edge-tts 不可用：{exc}")
        return 3
    try:
        comm = edge_tts.Communicate(text, voice, rate=rate, volume=volume, pitch=pitch)
        asyncio.run(comm.save(out_path))
    except Exception as exc:
        print(f"合成失败（{voice} {rate} {volume} {pitch}）：{exc}")
        return 5
    print(f"OK -> {out_path}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--list":
        return do_list()
    if args[0] == "--say":
        return do_say(args[1:])
    print(f"未知参数：{args[0]}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
