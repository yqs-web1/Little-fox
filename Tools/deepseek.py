import openai, time, httpx
import traceback
from Tools.AI_tools import *
from Tools import ai_backend
from Hyper import Configurator

class dsr114():
    def __init__(self, prompt, message, user_lists, uid, mode, bn, key) -> None:
        self.prompt = prompt
        self.message = message
        self.user_lists = user_lists
        self.uid = uid
        self.bn = bn
        self.mode = mode
        # key 每次请求时从 config.json 现取，这样在 QQ 里切换后端/换 Key 无需重启进程
        self.key = key
        self._client = None
        self._client_sig = None

    def _get_client(self, base_url: str):
        # base_url 为空 -> 走 DeepSeek 官方（https://api.deepseek.com/）
        # base_url 非空 -> 走自填的云端中转 / 本地 LM Studio，两者都要求绕过环境代理：
        #   1. 本地 127.0.0.1 被系统代理（HTTP_PROXY 等）接管会直接 Connection error；
        #   2. 中转站的流式 SSE 响应经代理会被缓冲破坏，退化成非流式 JSON，机器人拿不到分片。
        # 注意：httpx 的 proxy=None 等于默认值，并不禁用环境代理，必须 trust_env=False。
        # 签名不同（base_url 或 key 变了）就重建 client，否则换了后端还在用旧连接池。
        key = ai_backend.get_api_key()
        sig = (base_url, key)
        if self._client is None or self._client_sig != sig:
            self._client = openai.OpenAI(
                api_key=key,
                base_url=base_url or "https://api.deepseek.com/",
                default_headers=ai_backend.get_extra_headers() or None,
                http_client=httpx.Client(proxy=None, trust_env=False, timeout=httpx.Timeout(120.0, connect=20.0)),
            )
            self._client_sig = sig
        return self._client

    @staticmethod
    def _fix_alternation(messages):
        """Gemma 等本地模型要求 user/assistant 严格交替。
        之前请求失败（断连/报错）会在历史里留下连续 user 消息（没有 assistant 回复），
        导致 Jinja 模板报 'Conversation roles must alternate' 400。
        把连续同角色消息合并成一条即可修复，且不丢内容。"""
        fixed = []
        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""
            if fixed and fixed[-1]["role"] == role and role in ("user", "assistant"):
                prev = fixed[-1]["content"] or ""
                fixed[-1]["content"] = (prev + "\n" + content) if prev else content
            else:
                fixed.append({"role": role, "content": content})
        # gemma 模板要求 system（若有）之后必须 user 开头：
        # 丢弃历史开头的 assistant（旧裁剪逻辑留下的损伤，会导致 400）
        start = 1 if (fixed and fixed[0]["role"] == "system") else 0
        while len(fixed) > start and fixed[start]["role"] == "assistant":
            del fixed[start]
        return fixed

    def Response(self):
        try:
            mode = self.mode #"deepseek-chat" or "deepseek-reasoner"
            input_data = self.message
            user_lists = self.user_lists
            uid = str(self.uid) 
            system_message = {"role": "system", "content": self.prompt} 

            if uid not in user_lists:
                user_lists[uid] = [system_message]
            else:
                user_input: list = user_lists[uid]
                if len(user_input) > 0 and user_input[0]["role"] != "system":
                    user_input = [msg for msg in user_input if msg['role'] != 'system']
                    user_input.insert(0,system_message) # Insert at the beginning

            user_input: list = user_lists[uid]
            history_limit = 6  # 保留偶数条对话（user/assistant 成对），确保裁剪后仍是 user 开头
            if len(user_input) - 1 > history_limit:
                drop = (len(user_input) - 1) - history_limit
                if drop % 2 == 1:
                    drop += 1  # 成对丢弃：裁掉奇数条会让 system 后紧跟 assistant，gemma 模板会报 400
                user_input = [user_input[0]] + user_input[1 + drop:]

            user_input.append({"role": "user", "content": input_data})
            print(str(self.uid) + " 的上下文：" + str(len(user_input)))

            # 统一由 ai_backend 解析：本地 LM Studio / 云端中转站 / DeepSeek 官方
            base_url, model_name, extra_body, _ = ai_backend.resolve(mode)
            print(f"[deepseek] 后端: {ai_backend.describe()}")

            client = self._get_client(base_url)
            # 请求前修复历史角色交替（否则 gemma 模板会报 400）
            user_input[:] = self._fix_alternation(user_input)
            try:
                req = dict(
                    messages=user_input,
                    model=model_name,
                    stream=True,
                    temperature=0.6,
                    presence_penalty=0.4,
                )
                if extra_body:
                    req["extra_body"] = extra_body
                try:
                    chat_completion = client.chat.completions.create(**req)
                except openai.BadRequestError:
                    # 部分中转站只实现了 OpenAI 的最小参数集，presence_penalty / extra_body
                    # 会被判为非法参数直接 400。去掉这些可选参数重试一次。
                    req.pop("presence_penalty", None)
                    req.pop("extra_body", None)
                    print("[deepseek] 400：去掉 presence_penalty/extra_body 后重试")
                    chat_completion = client.chat.completions.create(**req)

                splitter = StreamSplitter()
                for message, _ in splitter.split_stream(chat_completion, 'openai'):
                    # print(f"[{time.time()}] RESPONSE: {repr(message)}")
                    yield message, 'message'

                try: # 仅在使用 reasoner 模型时需要
                    reasoning = chat_completion.choices[0].message.model_extra['reasoning_content']
                except Exception:
                    # print("无法使用思考")
                    reasoning = ""

                user_input.append({"role": "assistant", "content": splitter.full_content})
                user_lists[uid] = user_input 
                yield user_lists, 'user_lists'

            except openai.NotFoundError as e:
                print(f"OpenAI API Error: {e}")
                yield f"模型 '{mode}' 无法找到. 请检查模型名称是否正确，以及你的API KEY是否有权限访问该模型。\
{self.bn}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3", 'message'

            except openai.PermissionDeniedError as e:
                error_response = str(e)
                if 'insufficient_user_quota' in error_response:
                    yield f"无效的 API KEY 是因为 配额已用尽 。\
{self.bn}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3", 'message'
                else:
                    raise 

            except openai.BadRequestError as e:
                print(f"Deepseek bad request Error: {e}")
                yield f"与 DeepSeek 通信出现问题: {e}。\
{self.bn}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3", 'message'

        except Exception as e:
            print(traceback.format_exc())
            yield f"{type(e)}\n{self.bn}发生错误，不能回复你的消息了，请稍候再试吧 ε(┬┬﹏┬┬)3", 'message'