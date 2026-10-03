"""免 key 联网搜索（DuckDuckGo），供本地 gemma 做 RAG 回答使用。

设计要点：
- 全程不依赖任何 API Key（DuckDuckGo HTML 全文 + Instant Answer API 回退）；
- 所有网络异常都被吞掉并返回空列表，机器人不会因为搜不到而崩溃；
- HTTP 请求一律 trust_env=False + proxy=None，避免继承到沙箱/系统代理导致连不上外网。
"""
import httpx
import re
import html as _html
from urllib.parse import unquote

_DDG_HTML = "https://html.duckduckgo.com/html/"
_DDG_API = "https://api.duckduckgo.com/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _text(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return _html.unescape(s).strip()


def _decode_url(href: str) -> str:
    m = re.search(r"uddg=([^&]+)", href)
    if m:
        return unquote(m.group(1))
    if href.startswith("http"):
        return href
    return ""


def search(query: str, num_results: int = 6, timeout: int = 15) -> list:
    """返回 list[dict(title,url,snippet)]；任何网络异常都返回空列表（不抛）。"""
    results: list = []

    # 1) DuckDuckGo HTML 全文结果
    try:
        r = httpx.get(
            _DDG_HTML, params={"q": query}, headers=_HEADERS,
            timeout=timeout, follow_redirects=True,
            proxy=None, trust_env=False,
        )
        if r.status_code == 200:
            body = r.text
            blocks = re.findall(
                r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.S
            )
            snippets = re.findall(
                r'class="result__snippet"[^>]*>(.*?)</a>', body, re.S
            )
            for i, (href, title) in enumerate(blocks):
                url = _decode_url(href)
                snippet = _text(snippets[i]) if i < len(snippets) else ""
                title = _text(title)
                if url and title:
                    results.append({"title": title, "url": url, "snippet": snippet})
    except Exception as e:
        print(f"[web_search] DDG HTML 失败：{e}")

    # 2) 回退：Instant Answer API（对于事实类问题很稳）
    if len(results) < 2:
        try:
            r2 = httpx.get(
                _DDG_API,
                params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
                headers=_HEADERS, timeout=timeout,
                proxy=None, trust_env=False,
            )
            if r2.status_code == 200:
                data = r2.json()
                if data.get("AbstractText"):
                    results.append({
                        "title": data.get("AbstractSource", "DuckDuckGo"),
                        "url": data.get("AbstractURL", ""),
                        "snippet": data.get("AbstractText", ""),
                    })
                for t in data.get("RelatedTopics", [])[:num_results]:
                    if isinstance(t, dict) and t.get("Text") and t.get("FirstURL"):
                        results.append({
                            "title": _text(t["Text"])[:60],
                            "url": t["FirstURL"],
                            "snippet": _text(t["Text"]),
                        })
        except Exception as e:
            print(f"[web_search] DDG API 失败：{e}")

    # 去重（同一链接只保留一条）
    seen, uniq = set(), []
    for it in results:
        if it["url"] and it["url"] not in seen:
            seen.add(it["url"])
            uniq.append(it)
    return uniq[:num_results]


def format_context(query: str, items: list) -> str:
    if not items:
        return "（本次未能获取到网络搜索结果，请基于你的知识回答）"
    lines = [
        f"[{i}] 标题：{it['title']}\n   来源：{it['url']}\n   摘要：{it['snippet']}"
        for i, it in enumerate(items, 1)
    ]
    return "【网络搜索结果】\n" + "\n\n".join(lines)


# ===================== 基于本地 gemma 的联网回答 =====================
import openai
import time
import traceback
from Tools.AI_tools import StreamSplitter
from Tools import ai_backend
from Hyper import Configurator

_SEARCH_SYSTEM_TMPL = """你是{bot_name}，一个可爱、简洁的QQ机器人。下面提供了针对用户问题的网络搜索结果（可能为空）。
请严格遵循：
1. 用简体中文、自然口语化地回答；
2. 优先使用搜索结果中的事实，并在回答中自然地标注来源（如“（来源：xxx）”或“据搜索结果显示”）；
3. 若搜索结果不足以回答，请诚实说明“网上暂时没有特别明确的信息”，并用你的知识补充，但要清楚区分“来自网络搜索”和“我的推测”；
4. 严禁编造不存在的来源链接或数据；
5. 回答简洁，适合QQ聊天（约200字以内）。"""


def search_system_prompt() -> str:
    """联网模式的系统提示。

    修复：原来这里把"你是小狐狸"硬编码在常量里，改了 config.json 的 bot_name
    联网模式的人格也不会跟着变。现在每次调用都从 config.json 现取。
    """
    try:
        others = Configurator.cm.get_cfg().others or {}
        name = str(others.get("bot_name") or "").strip()
    except Exception:
        name = ""
    return _SEARCH_SYSTEM_TMPL.format(bot_name=name or "小狐狸")


class websearch_gemma:
    """联网搜索版回答器：先抓网页结果，再交给本地 gemma 依据结果作答。"""

    def __init__(self, prompt, message, user_lists, uid, mode, bn, key) -> None:
        self.prompt = prompt
        self.message = message
        self.user_lists = user_lists
        self.uid = uid
        self.mode = mode
        self.bn = bn
        self.key = key
        self._client = None
        self._client_sig = None

    def _get_client(self, local_base_url: str):
        # key/base_url 都现取，切换后端或换 Key 无需重启
        key = ai_backend.get_api_key()
        if self._client is None or self._client_sig != (local_base_url, key):
            self._client = openai.OpenAI(
                api_key=key,
                base_url=local_base_url or "https://api.deepseek.com/",
                default_headers=ai_backend.get_extra_headers() or None,
                http_client=httpx.Client(proxy=None, trust_env=False, timeout=httpx.Timeout(120.0, connect=20.0)),
            )
            self._client_sig = (local_base_url, key)
        return self._client

    def Response(self):
        try:
            query = (self.message or "").strip()
            user_lists = self.user_lists
            uid = str(self.uid)
            system_message = {"role": "system", "content": search_system_prompt()}

            if uid not in user_lists:
                user_lists[uid] = [system_message]
            else:
                user_input = user_lists[uid]
                if user_input and user_input[0].get("role") != "system":
                    user_input = [m for m in user_input if m.get("role") != "system"]
                    user_input.insert(0, system_message)

            # 与 Ds 模式共用同一套后端解析（本地 LM Studio / 云端中转 / 官方）
            _base_url, local_model, _extra, _ = ai_backend.resolve("google/gemma-3-4b")

            # 取最近一轮历史，避免搜索上下文反复累积撑爆上下文
            base = list(user_lists.get(uid, [system_message]))
            if len(base) > 3:
                base = [base[0]] + base[-2:]

            items = search(query)
            context = format_context(query, items)
            messages = base + [{"role": "user", "content": f"问题：{query}\n\n{context}"}]

            client = self._get_client(_base_url)
            model_name = local_model
            try:
                chat_completion = client.chat.completions.create(
                    messages=messages,
                    model=model_name,
                    stream=True,
                    temperature=0.6,
                    presence_penalty=0.4,
                )
            except openai.BadRequestError:
                # 云端中转站可能不认 presence_penalty，去掉后重试
                chat_completion = client.chat.completions.create(
                    messages=messages,
                    model=model_name,
                    stream=True,
                    temperature=0.6,
                )

            splitter = StreamSplitter()
            for message, _ in splitter.split_stream(chat_completion, 'openai'):
                if message and message.strip():
                    yield message, 'message'

            # 写回历史（不含搜索上下文，保持干净；成对裁剪防止历史过长）
            hist = user_lists[uid]
            hist.append({"role": "user", "content": query})
            hist.append({"role": "assistant", "content": splitter.full_content})
            history_limit = 6
            if len(hist) - 1 > history_limit:
                drop = (len(hist) - 1) - history_limit
                if drop % 2 == 1:
                    drop += 1
                hist = [hist[0]] + hist[1 + drop:]
            user_lists[uid] = hist
            yield user_lists, 'user_lists'

        except Exception as e:
            print(traceback.format_exc())
            yield f"{type(e)}\n{self.bn}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3", 'message'
