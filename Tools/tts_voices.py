"""音色注册表 + 趣味音效预设（需求③）。

解决的真实问题：原来 tts_local.py 的注释写着
    「微软较新的 Latest 语音（Xiaohan/Xiaorui/Xiaomo/Xiaochen 及 Yunze 云泽）
      在该端点不返回音频（No audio was received），故不收录」
也就是说：音色不是"加一行就行"，**加了不一定能出声**，而且随网络/时间变化。

本模块的做法：
  1. 把候选音色**全都收录**（不再靠人工试错剔除）；
  2. 由 Tools/tts_health.py 在启动时自动探活，只把**真能出声的**摆给用户看；
  3. 音效（花栗鼠/大叔/机器人…）用项目自带的完整版 ffmpeg 做后处理，
     与音色正交 —— 任意音色都能叠任意音效。
"""

from __future__ import annotations

# (友好名, edge_tts 语音 id, 分类, 性别, 标签)
_V = [
    # ---------------- 普通话 ----------------
    ("小艺", "zh-CN-XiaoyiNeural", "普通话", "女", ["温柔", "默认"]),
    ("晓晓", "zh-CN-XiaoxiaoNeural", "普通话", "女", ["活泼"]),
    ("晓萱", "zh-CN-XiaoxuanNeural", "普通话", "女", ["温婉"]),
    ("晓涵", "zh-CN-XiaohanNeural", "普通话", "女", ["知性"]),
    ("晓梦", "zh-CN-XiaomengNeural", "普通话", "女", ["甜"]),
    ("晓墨", "zh-CN-XiaomoNeural", "普通话", "女", ["成熟"]),
    ("晓睿", "zh-CN-XiaoruiNeural", "普通话", "女", ["稳重"]),
    ("晓辰", "zh-CN-XiaochenNeural", "普通话", "女", ["亲和"]),
    ("云希", "zh-CN-YunxiNeural", "普通话", "男", ["阳光"]),
    ("云扬", "zh-CN-YunyangNeural", "普通话", "男", ["播音"]),
    ("云健", "zh-CN-YunjianNeural", "普通话", "男", ["沉稳"]),
    ("云夏", "zh-CN-YunxiaNeural", "普通话", "男", ["清新"]),
    ("云泽", "zh-CN-YunzeNeural", "普通话", "男", ["磁性"]),
    ("云枫", "zh-CN-YunfengNeural", "普通话", "男", ["低沉"]),
    ("云皓", "zh-CN-YunhaoNeural", "普通话", "男", ["活力"]),
    # ---------------- 方言 ----------------
    ("东北话", "zh-CN-liaoning-XiaobeiNeural", "方言", "女", ["东北", "逗"]),
    ("陕西话", "zh-CN-shaanxi-XiaoniNeural", "方言", "女", ["陕西"]),
    # ---------------- 粤语 ----------------
    ("粤语·晓曼", "zh-HK-HiuMaanNeural", "粤语", "女", []),
    ("粤语·晓佳", "zh-HK-HiuGaaiNeural", "粤语", "女", []),
    ("粤语·云龙", "zh-HK-WanLungNeural", "粤语", "男", []),
    # ---------------- 台湾腔 ----------------
    ("台湾腔·晓臻", "zh-TW-HsiaoChenNeural", "台湾腔", "女", ["软"]),
    ("台湾腔·晓雨", "zh-TW-HsiaoYuNeural", "台湾腔", "女", ["软"]),
    ("台湾腔·云哲", "zh-TW-YunJheNeural", "台湾腔", "男", []),
    # ---------------- 日语 ----------------
    ("日语·七海", "ja-JP-NanamiNeural", "日语", "女", ["二次元"]),
    ("日语·圭太", "ja-JP-KeitaNeural", "日语", "男", []),
    # ---------------- 英语 ----------------
    ("英语·Aria", "en-US-AriaNeural", "英语", "女", []),
    ("英语·Guy", "en-US-GuyNeural", "英语", "男", []),
    ("英音·Sonia", "en-GB-SoniaNeural", "英语", "女", ["英音"]),
    # ---------------- 韩语 ----------------
    ("韩语·善熙", "ko-KR-SunHiNeural", "韩语", "女", []),
    ("韩语·仁俊", "ko-KR-InJoonNeural", "韩语", "男", []),
]

VOICES: dict = {}          # 友好名 -> {id, cat, gender, tags}
for _name, _vid, _cat, _gender, _tags in _V:
    VOICES[_name] = {"id": _vid, "cat": _cat, "gender": _gender, "tags": _tags}

EDGE_VOICES: dict = {n: v["id"] for n, v in VOICES.items()}   # 兼容旧接口
DEFAULT_EDGE_VOICE = "zh-CN-XiaoyiNeural"
GENDER_DEFAULT = {"男生": "zh-CN-YunxiNeural", "女生": "zh-CN-XiaoyiNeural"}

# ---------------- 趣味音效（ffmpeg 滤镜链）----------------
# 说明：先 aresample 到固定 24k，再用 asetrate 变调，然后 aresample 归一化，
# 最后用 atempo 把语速改回原样 —— 这样 net 效果只有"变调"，不依赖输入采样率。
FX_PRESETS: dict = {
    "花栗鼠": "aresample=24000,asetrate=32400,aresample=24000,atempo=0.7407",
    "萝莉":   "aresample=24000,asetrate=30000,aresample=24000,atempo=0.8",
    "大叔":   "aresample=24000,asetrate=19680,aresample=24000,atempo=1.2195",
    "低沉":   "aresample=24000,asetrate=18000,aresample=24000,atempo=1.3333",
    "机器人": "acrusher=bits=8:mode=log:aa=1,aphaser=in_gain=0.6:out_gain=0.9:delay=3:decay=0.4:speed=0.5",
    "电音":   "vibrato=f=12:d=0.6",
    "山洞":   "aecho=0.8:0.88:60|120:0.4|0.25",
    "混响":   "aecho=0.8:0.9:40|80|120:0.5|0.3|0.15",
    "电话":   "highpass=f=300,lowpass=f=3400",
    "慢速":   "atempo=0.75",
    "快速":   "atempo=1.55",
}

RANDOM_NAMES = ("随机", "random")
EMOTION_NAME = "情绪"

# ---------------- 情绪 -> 音色/音效（趣味联动）----------------
_EMOTION_RULES = [
    (("哈哈", "嘿嘿", "嘻嘻", "开心", "太好了", "好耶", "！！", "🎉"), "晓晓", None),
    (("呜", "难过", "伤心", "哭", "委屈", "o(╥﹏╥)o"), "小艺", "慢速"),
    (("哼", "讨厌", "生气", "气死", "烦", "╯﹏╰"), "云健", "大叔"),
    (("抱抱", "别难过", "安慰", "乖", "摸摸"), "小艺", "慢速"),
]


def is_voice_name(name: str) -> bool:
    return name in VOICES or name in GENDER_DEFAULT or name in RANDOM_NAMES or name == EMOTION_NAME


def is_fx_name(name: str) -> bool:
    return name in FX_PRESETS


def voice_id_of(name: str) -> str | None:
    if name in VOICES:
        return VOICES[name]["id"]
    return GENDER_DEFAULT.get(name)


def fx_chain(name: str) -> str | None:
    return FX_PRESETS.get(name)


def split_selection(sel: str):
    """把 '晓晓+花栗鼠' 拆成 (音色名, 音效名)。任一段可为空。"""
    if not sel:
        return None, None
    parts = [p.strip() for p in str(sel).replace("＋", "+").split("+") if p.strip()]
    voice = fx = None
    for p in parts:
        if is_fx_name(p) and fx is None:
            fx = p
        elif voice is None:
            voice = p
        elif fx is None:
            fx = p
    return voice, fx


def emotion_pick(text: str):
    """按回复文本的情绪挑 (音色名, 音效名)。"""
    t = text or ""
    for keys, v, fx in _EMOTION_RULES:
        if any(k in t for k in keys):
            return v, fx
    return None, None


# ---------------- 探活文案（必须按语种匹配）----------------
# 坑：用中文「你好呀」去探活英语音色，edge 端点会直接返回 NoAudioReceived，
# 从而把本来可用的英语音色误判为不可用。探活文案必须和音色语种一致。
_PROBE_TEXT_BY_PREFIX = {
    "zh-CN": "你好呀",
    "zh-HK": "你好呀",
    "zh-TW": "你好呀",
    "ja-JP": "こんにちは",
    "en-US": "Hello there",
    "en-GB": "Hello there",
    "ko-KR": "안녕하세요",
}


def probe_text_for(vid: str) -> str:
    for pre, txt in _PROBE_TEXT_BY_PREFIX.items():
        if str(vid).startswith(pre):
            return txt
    return "你好呀"


def available_names(healthy: set | None = None) -> list:
    """返回可展示的音色名。healthy 为 None（未探活）时返回全部。"""
    names = [n for n in VOICES]
    if healthy is None:
        return names
    out = [n for n in names if VOICES[n]["id"] in healthy]
    return out or names          # 探活全失败时不要把自己锁死，仍返回全部


def grouped_listing(healthy: set | None = None) -> str:
    """按分类分组输出音色清单 + 音效清单。"""
    names = available_names(healthy)
    cats: dict = {}
    for n in names:
        cats.setdefault(VOICES[n]["cat"], []).append(n)
    lines = []
    for cat, ns in cats.items():
        lines.append(f"  【{cat}】" + "、".join(ns))
    lines.append("  【音效】（可叠加，写法：音色+音效，如 晓晓+花栗鼠）"
                 + "、".join(FX_PRESETS))
    lines.append(f"  【特殊】随机、情绪（按回复内容自动换）")
    return "\n".join(lines)
