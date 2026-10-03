from Hyper import Configurator
import aiohttp, os, random, unicodedata
from PIL import Image
from Tools.img_send import send_image, _is_image_bytes

Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

reminder = Configurator.cm.get_cfg().others["reminder"]
bot_name = Configurator.cm.get_cfg().others["bot_name"]
TRIGGHT_KEYWORD = "生图 Pixiv"
HELP_MESSAGE = f"{reminder}生图 Pixiv (标签，必填，用&分割) —> {bot_name}浏览P站"

# Pixiv 图床反代（2026-10-02 实测 i.pixiv.re 已失效，改用 Lolicon 自带 proxy 反代域名）。
# 注意：本地沙箱连不通该反代属正常现象，在用户本机（真实网络）通常可用，需本机实测。
PIXIV_PROXY = "pixiv.t.sr-studio.top"

CENSORED_WORDS = [
    "R-18", "r-18", "R-18G", "r-18g", "R18", "r18", "R18G", "r18g", "R_18", "r_18", "R_18G", "r_18g",
    "即将脱落的胸罩", "NSFW", "nsfw", "成人向"
]


def _safe_save(resp_bytes: bytes, ctype: str) -> str | None:
    if not resp_bytes or len(resp_bytes) < 2048:
        return None
    if not _is_image_bytes(resp_bytes):   # 用文件头魔数兜底，避免 content-type 不规范被误杀
        return None
    ext = ".jpg"
    if "webp" in ctype:
        ext = ".webp"
    elif "png" in ctype:
        ext = ".png"
    os.makedirs("temps", exist_ok=True)
    path = os.path.abspath(f"temps/pixiv_{random.randint(0, 999999999)}{ext}")
    with open(path, "wb") as f:
        f.write(resp_bytes)
    return path


async def _download_pixiv_image(urls: dict) -> str | None:
    """机器人端下载 Pixiv 图片为本地文件（original 失败降级 regular/medium）。
    不再把外链甩给 NapCat 下载，从根上规避 '下载文件失败：Not Found'。"""
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.pixiv.net/",
    }
    for key in ("original", "regular", "medium"):
        u = urls.get(key)
        if not u:
            continue
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), timeout=aiohttp.ClientTimeout(25)) as session:
                async with session.get(u, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        ctype = resp.headers.get("content-type", "")
                        return _safe_save(data, ctype)
        except Exception as e:
            print(f"[生图Pixiv] 下载失败 {key}: {e}")
    return None


async def on_message(event, actions, Manager, Segments, order, time, cooldowns1,
                     traceback, datetime, bot_name, generating):

    global reminder
    # 兼容性匹配：NFKC 归一化 + 去空格，各种空格/全角变体都能命中
    compact = unicodedata.normalize("NFKC", order).replace(" ", "")
    start_index = compact.find("生图Pixiv")
    if start_index == -1:
        return False

    selfID = await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}正在从 Pixiv 生成 ヾ(≧▽≦*)o")))

    if not generating:
        user_id = event.user_id
        current_time = time.time()

        # 个人冷却
        if user_id in cooldowns1 and current_time - cooldowns1[user_id] < 18:
            time_remaining1 = 18 - (current_time - cooldowns1[user_id])
            await actions.del_message(selfID.data.message_id)
            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"18秒个人cd，请等待 {time_remaining1:.1f} 秒后重试")))
            return True
        else:
            result = compact[start_index + len("生图Pixiv"):].strip()
            # 追加 proxy 反代参数，让 Lolicon 直接返回反代图床 URL（绕开已死的 i.pixiv.re）
            url_setted = "https://api.lolicon.app/setu/v2?num=1&r18=0&excludeAI=false" + f"&proxy={PIXIV_PROXY}"

            tags = result.split("&")
            for TagIndex in range(len(tags)):
                url_setted = url_setted + "&tag=" + tags[TagIndex]

            print(url_setted)

            try:
                async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False), timeout=aiohttp.ClientTimeout(10)) as session:
                    async with session.get(url=url_setted) as response:
                        request = await response.json()
            except Exception as e:
                request = "Failed\n" + traceback.format_exc()

            print("请求成功")

            # 接口失败
            if "Failed" in request:
                print(request)
                await actions.del_message(selfID.data.message_id)
                emessage = f'''{bot_name}无法访问接口了，请稍后重试 ε(┬┬﹏┬┬)3'''
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(emessage)))
                return True

            data_normal = request.get('data', [])
            # 标签过严 / 无结果
            if len(data_normal) < 1:
                await actions.del_message(selfID.data.message_id)
                emessage = f'''你给{bot_name}的标签太严格啦！（生气），换几个标签试试吧 ＞﹏＜'''
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(emessage)))
                return True

            data = data_normal[0]
            info = f'''标题：{data['title']}
Pixiv ID：{data['pid']}
作者：{data['author']}
作者ID：{data['uid']}
AI参与：{'是' if data.get('aiType') == 1 else '否'}
创作时间：{datetime.datetime.fromtimestamp(data['uploadDate'] / 1000).strftime('%Y-%m-%d')}
标签：{data['tags']}'''

            # 涩图过滤
            # 修复：data['tags'] 是标签**列表**，原来的 `word in data['tags']` 是"精确相等"匹配，
            # 只有标签与敏感词一字不差才拦得住（像 "R-18 (漫画)" 这种就漏过去了）。
            # 这里改成对整串标签做不区分大小写的子串匹配。
            _tags_text = " ".join(str(t) for t in (data.get('tags') or [])).lower()
            if any(str(word).lower() in _tags_text for word in CENSORED_WORDS):
                await actions.del_message(selfID.data.message_id)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"你要的图片实在太涩啦！{bot_name}都不敢看了 (⓿_⓿)")))
                return True

            # 机器人端先把图片下载到本地，再发本地文件（NapCat 不再下载外链）
            img_path = await _download_pixiv_image(data['urls'])
            if not img_path:
                await actions.del_message(selfID.data.message_id)
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(f"{bot_name}生图失败了，再试一次吧（哭）(○´･д･)ﾉ")))
                return True

            await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(info)))
            err = await send_image(actions, Manager, Segments, event.group_id, img_path)
            await actions.del_message(selfID.data.message_id)
            if err is None:
                cooldowns1[user_id] = current_time
            else:
                await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text(
                    f"{bot_name}图片发送失败({err})，再试一次吧（哭）(○´･д･)ﾉ")))
            return True

    else:
        await actions.del_message(selfID.data.message_id)
        await actions.send(group_id=event.group_id, message=Manager.Message(Segments.Text("前面还有一张图在生成呢，请稍候再试吧 (*/ω＼*)")))
        return True
