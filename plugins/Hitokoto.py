import aiohttp
from Hyper import Configurator

Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

reminder = Configurator.cm.get_cfg().others["reminder"]
bot_name = Configurator.cm.get_cfg().others["bot_name"]
TRIGGHT_KEYWORD = "一言"
HELP_MESSAGE = f"{reminder}一言 [分类] —> {bot_name}送上随机一句治愈话语（分类可填：动画/漫画/游戏/小说/诗词/影视/哲学/抖机灵）"

# 一言分类映射（v1.hitokoto.cn 的 c 参数）
CAT_MAP = {
    "动画": "a", "动漫": "a",
    "漫画": "b",
    "游戏": "c",
    "小说": "d",
    "互联网": "e", "网络": "e",
    "诗词": "f",
    "网易云": "g", "音乐": "g",
    "影视": "h", "电影": "h",
    "哲学": "i",
    "抖机灵": "j", "段子": "j",
}


async def _send(event, actions, Manager, Segments, message):
    """兼容群聊与私聊发送"""
    if hasattr(event, "group_id"):
        await actions.send(group_id=event.group_id, message=message)
    else:
        await actions.send(user_id=event.user_id, message=message)


async def on_message(event, actions, Manager, Segments, order, bot_name):
    # order 已经是去掉前缀 ~ 之后的内容，形如 "一言 动漫"
    tail = order[len(TRIGGHT_KEYWORD):].strip() if order.startswith(TRIGGHT_KEYWORD) else order.strip()
    cat = ""
    if tail in CAT_MAP:
        cat = f"?c={CAT_MAP[tail]}"

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(10)) as session:
            async with session.get(f"https://v1.hitokoto.cn/{cat}") as resp:
                data = await resp.json()

        text = data.get("hitokoto", "").strip()
        source = (data.get("from") or "").strip()
        who = (data.get("from_who") or "").strip()

        if not text:
            await _send(event, actions, Manager, Segments,
                        Manager.Message(Segments.Text(f"{bot_name}没能取到句子，待会儿再试试吧 (｡•́︿•̀｡)")))
            return True

        out = text
        if who:
            out += f"\n—— {who}"
        elif source:
            out += f"\n—— 《{source}》"
        await _send(event, actions, Manager, Segments, Manager.Message(Segments.Text(out)))
    except Exception as e:
        print(f"[一言] 获取失败: {e}")
        await _send(event, actions, Manager, Segments,
                    Manager.Message(Segments.Text(f"{bot_name}一言获取失败了…可能是网络问题(｡•́︿•̀｡)")))
    return True
