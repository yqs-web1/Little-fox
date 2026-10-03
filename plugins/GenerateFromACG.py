import os
import random
import json
import unicodedata
import requests
from PIL import Image
from Hyper import Configurator
from Tools.img_send import send_image, _is_image_bytes

Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

reminder = Configurator.cm.get_cfg().others["reminder"]
bot_name = Configurator.cm.get_cfg().others["bot_name"]
TRIGGHT_KEYWORD = "生图 ACG"
HELP_MESSAGE = f'''{reminder}生图 ACG (任意类型，必填) —> {bot_name}制作精美二次元壁纸
{reminder}生图 ACG 帮助 —> 查看{bot_name}的生图帮助菜单'''

# 图源：栗次元 API(t.alcy.cc)，2026-10-02 实测全部可用。
# 旧 LoliAPI(www.loliapi.com) 已失效（302 跳 404），不再使用。
# 每个类型独立端点，保证"电脑壁纸/手机壁纸/头像/背景"产出真正有区别。
SOURCES = {
    "随机":     ["https://t.alcy.cc/ycy", "https://www.dmoe.cc/random.php", "https://api.paugram.com/wallpaper/"],
    "电脑壁纸": ["https://t.alcy.cc/pc", "https://t.alcy.cc/moe", "https://t.alcy.cc/ys"],
    "手机壁纸": ["https://t.alcy.cc/mp", "https://t.alcy.cc/moemp", "https://api.paugram.com/wallpaper/?type=mobile"],
    "头像":     ["https://t.alcy.cc/tx", "https://t.alcy.cc/moez", "https://t.alcy.cc/moemp"],
    "背景":     ["https://t.alcy.cc/fj", "https://t.alcy.cc/bd", "https://t.alcy.cc/ysz"],
}

# 类型别名，方便用户口语化输入
ALIASES = {
    "随机": "随机", "随机壁纸": "随机", "自适应": "随机",
    "电脑壁纸": "电脑壁纸", "电脑": "电脑壁纸", "pc": "电脑壁纸", "横屏": "电脑壁纸", "横图": "电脑壁纸",
    "手机壁纸": "手机壁纸", "手机": "手机壁纸", "竖屏": "手机壁纸", "竖图": "手机壁纸",
    "头像": "头像", "头像图": "头像", "方形": "头像",
    "背景": "背景", "背景图": "背景", "风景": "背景",
}

HELP_TEXT = f'''{bot_name}可生成精美 ACG 壁纸噢~ヾ(≧∪≦*)ノ〃
{reminder}生图 ACG 随机 -> 根据设备自动适配
{reminder}生图 ACG 电脑壁纸 -> 电脑端高清横图
{reminder}生图 ACG 手机壁纸 -> 移动端竖图
{reminder}生图 ACG 头像 -> 适合做头像的图片
{reminder}生图 ACG 背景 -> 风景/背景图

举个🍐子：{reminder}生图 ACG 电脑壁纸 -> {bot_name}生成一张横屏高清壁纸
快来试试吧Ｏ(≧▽≦)Ｏ '''


def _ext_by_type(ctype: str) -> str:
    if "jpeg" in ctype or "jpg" in ctype:
        return ".jpg"
    if "webp" in ctype:
        return ".webp"
    if "png" in ctype:
        return ".png"
    return ".jpg"


def _save_bytes(b: bytes) -> str:
    os.makedirs("temps", exist_ok=True)
    path = os.path.abspath(f"temps/acg_{random.randint(0, 999999999)}.jpg")
    with open(path, "wb") as f:
        f.write(b)
    return path


def _fetch_image(kind: str) -> str | None:
    """按类型取图：机器人端先下载为本地文件（多源回退），返回绝对路径；失败返回 None。
    判定规则：status=200 且（content-type 以 image 开头 或 文件头魔数是真实图片）且体积 > 2KB。
    不再死磕 image/* —— 很多图床 content-type 不规范，用魔数更准，避免被误判成“图源失效”。"""
    pool = SOURCES.get(kind, SOURCES["随机"])
    for api in pool:
        try:
            r = requests.get(api, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            ctype = r.headers.get("content-type", "")
            if r.status_code == 200 and (ctype.startswith("image") or _is_image_bytes(r.content)) and len(r.content) > 2048:
                return _save_bytes(r.content)
            print(f"[生图ACG] 源异常 {api}: status={r.status_code} type={ctype} len={len(r.content)}")
        except Exception as e:
            print(f"[生图ACG] 源失败 {api}: {e}")

    # 兜底：Lolicon 接口（返回 Pixiv 直链再下载）。
    # 关键：必须带 proxy 参数，否则返回的是 i.pixiv.re 反代地址，而该反代经常挂掉导致兜底失效。
    # 这里用与 Pixiv 插件一致的可用反代 pixiv.t.sr-studio.top。
    try:
        lolicon_url = "https://api.lolicon.app/setu/v2?num=1&r18=0&excludeAI=false&proxy=pixiv.t.sr-studio.top"
        r = requests.get(lolicon_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            data = (r.json() or {}).get("data") or []
            if data:
                urls = data[0].get("urls", {})
                img_url = urls.get("original") or urls.get("regular") or urls.get("medium")
                if img_url:
                    r2 = requests.get(img_url, timeout=25,
                                      headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.pixiv.net/"})
                    if r2.status_code == 200 and _is_image_bytes(r2.content) and len(r2.content) > 2048:
                        return _save_bytes(r2.content)
    except Exception as e:
        print(f"[生图ACG] Lolicon 兜底失败: {e}")

    return None


def _match_kind(result: str) -> str | None:
    """把用户输入匹配到具体类型；支持别名，返回规范类型名或 None"""
    for key in SOURCES:
        if key in result:
            return key
    low = result.lower()
    for alias, kind in ALIASES.items():
        if alias.lower() in low:
            return kind
    return None


async def on_message(event, actions, Manager, Segments, order, time, cooldowns,
                     Super_User, Manage_User, ROOT_User, bot_name):
    # 兼容性匹配：NFKC 归一化（全角ＡＣＧ/全角空格→半角）+ 去空格，
    # "生图 ACG 随机 / 生图ACG随机 / 生图　ＡＣＧ　随机 / 生图 ACG" 全部命中
    compact = unicodedata.normalize("NFKC", order).replace(" ", "")
    start_index = compact.find("生图ACG")
    if start_index == -1:
        return False

    result = compact[start_index + len("生图ACG"):].strip()
    if not result:
        result = "随机"  # 只发 ~生图 ACG 不带类型时，默认随机
    user_id = event.user_id
    current_time = time.time()

    if user_id in cooldowns and current_time - cooldowns[user_id] < 18:
        if not (str(event.user_id) in Super_User or str(event.user_id) in ROOT_User or str(event.user_id) in Manage_User):
            time_remaining = 18 - (current_time - cooldowns[user_id])
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"18秒个人cd，请等待 {time_remaining:.1f} 秒后重试")))
            return True

    if "帮助" in result:
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(HELP_TEXT)))
        return True

    kind = _match_kind(result)
    if kind is None:
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("指定的类型不存在")))
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(HELP_TEXT)))
        return True

    selfID = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}正在制作{kind} ヾ(≧▽≦*)o")))

    path = _fetch_image(kind)
    try:
        await actions.del_message(selfID.data.message_id)
    except Exception:
        pass

    if path:
        err = await send_image(
            actions, Manager, Segments, event.group_id, path,
            text=f"{kind}生成 结束！✧*。٩(>ω<*)و✧*。")
        if err is None:
            cooldowns[user_id] = current_time
        else:
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
                f"因为 {err}\n{bot_name}不能生成图片了，请稍候再试吧 o(TヘTo)")))
    else:
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
            f"{kind}的图源这次全都失效了…{bot_name}也拉不到图 (｡•́︿•̀｡) 请稍后再试吧")))

    return True
