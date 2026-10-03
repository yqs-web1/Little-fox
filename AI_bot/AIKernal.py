from __future__ import annotations
from Tools.GoogleAI import Context, Parts, Roles, Schema
from Tools.SearchOnline import network_gpt as SearchOnline
from Tools.deepseek import dsr114 as deepseek
from Tools.web_search import websearch_gemma as websearch
from Tools.Sanitizer_Tools import sanitize_for_tts
from Tools.tools import replace_at_with_nickname, get_user_nickname, replace_scheme_with_http
from Tools import ai_backend
from Hyper import Configurator
Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())
from Hyper import Listener, Events, Logger, Manager, Segments
from Hyper.Utils import Logic
from Hyper.Events import *
from typing import Union

MAX_MESSAGE_LENGTH = 3
if __name__ == "__main__":
    from main import ContextManager

class AIKernal:
    def __init__(self, actions: Listener.Actions, config: Configurator.Config,
                 bot_name: str, reminder: str = "") -> None:
        self.bot_name = bot_name
        self.actions = actions
        self.reminder = reminder
        self.config = config
        self.replace_at_with_nickname = replace_at_with_nickname
        self.url = ""
        self.sended = False
        self.sendedID = []
        self.messages_for_node = []
        self.enable_forward_msg_num = False
        self.result = ""
        self.reply_private_msg = False
        self.event = None
        self.emit_text = True  # 语音对话模式：False 时只累积文本、不向 QQ 发送文字
        # 私聊引用回复：私聊不管回什么都引用对方那条消息
        self.private_quote = bool(config.others.get("private_reply_quote", True))
        # 流式回复会被拆成多条，默认只让「首条」带引用：
        # 否则一条回复会连刷 5~8 个引用同一条消息的气泡，既吵又加重 QQ 洪水控制
        self.quote_only_first = bool(config.others.get("quote_only_first_chunk", True))

    async def generate_response(self, EnableNetwork: str, cmc: ContextManager, sys_prompt: str, user_lists: dict,
                                event: Union[Events.GroupMessageEvent, Events.PrivateMessageEvent],
                                emit_text: bool = True, model_id: str | None = None):
        self.url = ""
        self.sended = False
        self.sendedID = []
        self.messages_for_node = []
        self.enable_forward_msg_num = False
        self.result = ""
        self.reply_private_msg = False
        self.event = event
        self.emit_text = emit_text  # 语音对话模式传 False：只返回文本，不发文字
        self.user_lists = user_lists

        if isinstance(event, Events.PrivateMessageEvent):
            self.reply_private_msg = True

        # ---- 需求⑤：模型注册表优先 ----
        # 原实现用 `match EnableNetwork` 同时承担「选哪个模型」和「走哪条链路」，
        # 传注册表 id（如 ds-chat）会一个分支都不匹配 → 什么都不回复。
        # 现在先看是不是注册表模型：是则按 provider.type 分派，否则沿用旧的模式名分派。
        _provider = _entry = None
        if model_id:
            try:
                from Tools import model_registry
                _provider, _entry = model_registry.resolve_model(model_id)
                if _entry is None:
                    print(f"[AIKernal] 模型 {model_id!r} 不在注册表里，改用模式名分派")
            except Exception as _e:
                print(f"[AIKernal] 注册表解析失败（回落模式名分派）：{type(_e).__name__}: {_e}")

        if _entry is not None:
            _ptype = str((_provider or {}).get("type") or "openai").lower()
            if _ptype == "gemini":
                new = await self.build_message_content()
                response_stream = cmc.get_context(
                    event.user_id, event.group_id, sys_prompt, sys_prompt, self.config
                ).gen_content(Roles.User(*new), model_override=_entry.get("model"))
                await self.handle_message_stream(response_stream, False)
            else:
                # OpenAI 兼容链路（中转站 / 硅基流动 / 本地 LM Studio 都走这里）
                msg = await self.process_reply_message("")
                msg += str(await self.replace_at_with_nickname(event.message, Manager, Segments, self.actions))
                search = deepseek(
                    sys_prompt, msg, self.user_lists, event.user_id,
                    str(_entry.get("id")), self.bot_name,
                    ai_backend.get_api_key(str((_provider or {}).get("base_url") or ""))
                )
                await self.handle_message_stream(search.Response())
        else:
            match EnableNetwork:
                case "GoogleGemini":
                    new = await self.build_message_content()
                    response_stream = cmc.get_context(event.user_id, event.group_id, sys_prompt, sys_prompt, self.config).gen_content(
                        Roles.User(*new),
                        model_override=self.config.others.get("gemini_model", "gemini-2.0-flash-exp")
                    )
                    await self.handle_message_stream(response_stream, False)

                case "GPT-3.5" | "Net":
                    model_name = "gpt-3.5-turbo-16k" if EnableNetwork == "GPT-3.5" else "gpt-4o-mini"
                    msg = await self.process_reply_message("")
                    msg += str(await self.replace_at_with_nickname(event.message, Manager, Segments, self.actions))
                    search = SearchOnline(
                        sys_prompt, msg, self.user_lists, event.user_id,
                        model_name, self.bot_name,
                        self.config.others["openai_key"]
                    )
                    await self.handle_message_stream(search.Response())

                case "Ds":
                    msg = await self.process_reply_message("")
                    msg += str(await self.replace_at_with_nickname(event.message, Manager, Segments, self.actions))
                    # 模型名交给 ai_backend 决定（本地走 local_model，云端走 cloud_model，官方走 deepseek-chat）
                    search = deepseek(
                        sys_prompt, msg, self.user_lists, event.user_id,
                        "deepseek-chat", self.bot_name,
                        ai_backend.get_api_key()
                    )
                    await self.handle_message_stream(search.Response())

                case "WebSearch":
                    msg = await self.process_reply_message("")
                    msg += str(await self.replace_at_with_nickname(event.message, Manager, Segments, self.actions))
                    ws = websearch(
                        sys_prompt, msg, self.user_lists, event.user_id,
                        "web-search", self.bot_name,
                        config.others["deepseek_key"]
                    )
                    await self.handle_message_stream(ws.Response())

        self.result = self.result.rstrip()
        await self.finalize_messages()

        if not self.sended:
            # 修复：模型不可用 / 返回为空时，原来会发一条**空文字**消息 —— 在 QQ 里表现为
            # "只有一个引用块、没有正文"，用户完全看不懂（线上就是这么报障的：只看到被引用的话）。
            # 这里换成一句能看懂、能照做的提示。（语音模式 emit_text=False 时 send_message 本就不发文字）
            _fallback = self.result or (
                f"（这次我没能生成回复：当前模型可能不可用、或没有返回任何内容。"
                f"发 {self.reminder}模型 可以看看有哪些模型，换一个再试）")
            await self.send_message([Segments.Text(_fallback)], True)

        return cmc, self.user_lists, self.result

    async def process_reply_message(self, msg):
        # 优先处理引用消息
        if isinstance(self.event.message[0], Segments.Reply):
            content = await self.actions.get_msg(self.event.message[0].id)
            message = gen_message({"message": content.data["message"]})
            for i in message:
                if isinstance(i, Segments.Text):
                    msg += f"{i.text} "
                elif isinstance(i, Segments.At):
                    msg += f"@{await get_user_nickname(i.qq, Manager, self.actions)} "

        return msg

    async def build_message_content(self):
        new = []
        # 处理引用消息中的内容
        if isinstance(self.event.message[0], Segments.Reply):
            content = await self.actions.get_msg(self.event.message[0].id)
            message = gen_message({"message": content.data["message"]})
            for i in message:
                await self.handle_content_item(i, new)
                
        # 处理当前消息内容
        for i in self.event.message:
            await self.handle_content_item(i, new)
        return new

    async def handle_content_item(self, item, container: list):
        if isinstance(item, Segments.Text):
            container.append(Parts.Text(item.text.replace(self.reminder, "", 1)))
        elif isinstance(item, Segments.Image):
            url = item.file if item.file.startswith("http") else item.url
            print(f"AI: URL位置 {replace_scheme_with_http(url)}")
            container.append(Parts.File.upload_from_url(replace_scheme_with_http(url)))
            print("AI: 有图")
        elif isinstance(item, Segments.At):
            nickname = await get_user_nickname(item.qq, Manager, self.actions)
            container.append(Parts.Text(f"@{nickname}"))
        else:
            container.append(Parts.Text(str(item)))

    async def handle_message_stream(self, response_stream, is_openai=True):
        for partial, r_type in response_stream:
            if is_openai:
                if r_type != 'message':
                    self.user_lists = partial
                    continue

            # 兜底过滤：空/纯空白分片不发送（否则 QQ 显示"该消息类型暂不支持查看"）
            if not str(partial).strip():
                continue

            message = Segments.Text(str(partial))
            if self.enable_forward_msg_num:
                self.messages_for_node.append(message)
            else:
                if not self.sended:
                    await self.send_message([message], True)
                else:
                    await self.send_message([message])
                self.messages_for_node.append(message)
            
            if len(self.messages_for_node) > MAX_MESSAGE_LENGTH - 1 and not self.enable_forward_msg_num and not self.reply_private_msg:
                self.enable_forward_msg_num = True

            if self.enable_forward_msg_num and len(self.messages_for_node) == MAX_MESSAGE_LENGTH + 1:
                self.sendedID.append(await self.send_message([Segments.Text(r"**[thinking]**")]))

            self.sended = True
            self.result += str(partial) + '\n'

    def _should_quote_private(self) -> bool:
        """这一条私聊回复是否要挂引用段。"""
        if not self.private_quote:
            return False
        if getattr(self.event, "message_id", None) is None:
            return False           # 拿不到原消息 ID 就没法引用，安全跳过
        # 策略：私聊**纯文字回复总是带引用**（本类只发文字段）；
        # 语音/图片等各自走别的发送路径，由 main.py 显式 reply_to=None 控制（不引用）。
        if self.quote_only_first:
            # self.sended 由 handle_message_stream 维护：发出首条之前恒为 False
            return not self.sended
        return True

    async def send_message(self, msg: list[Segments.Base], is_reply=False) -> Manager.Ret:
        if not self.emit_text:
            return None  # 语音对话模式：不向 QQ 发送任何文字
        if self.reply_private_msg:
            # 首条内容发出前，先把私聊的「正在思考」占位气泡收掉
            try:
                from Tools import bubble
                await bubble.clear(self.actions, self.event)
            except Exception as _e:
                print(f"[AIKernal] 清理占位气泡失败（忽略）：{type(_e).__name__}: {_e}")
            _segs = list(msg)
            if self._should_quote_private():
                _segs = [Segments.Reply(self.event.message_id)] + _segs
            return await self.actions.send(
                user_id=self.event.user_id,
                message=Manager.Message(*_segs)
            )
        else:
            if is_reply:
                return await self.actions.send(
                    group_id=self.event.group_id,
                    message=Manager.Message(Segments.Reply(self.event.message_id), *msg)
                )
            else:
                return await self.actions.send(
                    group_id=self.event.group_id,
                    message=Manager.Message(*msg)
                )

    async def finalize_messages(self):
        if not self.emit_text:
            return  # 语音对话模式：文字未真正发送，无需转发收尾/撤回
        if self.enable_forward_msg_num:
            # 删除临时消息
            for msg_id in self.sendedID:
                await self.actions.del_message(msg_id.data.message_id) # 禁用消息连续撤回以防止QQ检测
            
            for m in range(len(self.messages_for_node)):
                self.messages_for_node[m] = Segments.CustomNode(
                    str(self.event.self_id),
                    self.bot_name,
                    Manager.Message(self.messages_for_node[m])
                )
            
            # 发送合并转发
            if len(self.messages_for_node) > MAX_MESSAGE_LENGTH:
                await self.actions.send_group_forward_msg(
                    group_id=self.event.group_id,
                    message=Manager.Message(*self.messages_for_node)
                )