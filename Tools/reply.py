"""统一的回复出口：自动判定群聊/私聊，并按配置为私聊附加「引用回复」。

背景：原先 main.py 私聊分支里有 8 处直接 actions.send(user_id=..., ...)，
另有多处帮手函数（_voice_reply 等）各发各的，全都没有引用段。
要做到「私聊不管回什么都以引用方式回复」，必须收敛到单一出口 ——
否则以后新增命令必然漏掉，而且漏了不容易发现。

群聊行为保持原样（不引用）：群聊的引用只由 AIKernal 在流式首条上附加，
避免把群里每个 bot 回复都挂上引用导致刷屏。
"""


def _others() -> dict:
    try:
        from Hyper import Configurator
        return Configurator.cm.get_cfg().others or {}
    except Exception:
        return {}


def _flag(name: str, default: bool = True) -> bool:
    try:
        return bool(_others().get(name, default))
    except Exception:
        return default


def user_quoted(event) -> bool:
    """用户这条消息本身是不是"引用了某条消息"发出来的（长按 → 引用）。

    用途：私聊默认不再给每条回复都挂引用 —— 只在"用户先引用了消息"时才回引用。
    原因（线上实测）：每条回复都挂引用时，一旦回复正文为空（模型没返回内容），
    QQ 里就只剩一个引用块，看起来像机器人在复读；而"语音 + 引用"的组合显示也不正常。
    """
    try:
        from Hyper import Segments as _S
        for _i in (getattr(event, "message", None) or []):
            if isinstance(_i, _S.Reply):
                return True
    except Exception:
        return False
    return False


def _segments(Segments, message):
    """把 str / 单个 Segment / Segment 列表 统一成列表。

    纯空白字符串按"没内容"处理（原来只判 message 是否为空字符串，
    "   " 会被当成有内容发出去 —— 又是一条只有空白的消息）。
    """
    if message is None:
        return []
    if isinstance(message, str):
        return [Segments.Text(message)] if message.strip() else []
    if isinstance(message, (list, tuple)):
        return [s for s in message if s is not None]
    return [message]


async def reply_send(actions, Manager, Segments, event, message, quote=None):
    """向 event 的来源会话回复。

    quote=None  -> 私聊跟随 config 的 private_reply_quote（默认 True），群聊不引用
    quote=True  -> 强制带引用
    quote=False -> 强制不带引用

    发送失败不抛异常（打印后返回 None），避免一条回复发不出去就打断后续流程。
    """
    segs = _segments(Segments, message)
    if not segs:
        return None

    # 真正要发内容了 —— 先把「正在思考」占位气泡收掉（未发出则取消定时器）
    try:
        from Tools import bubble
        await bubble.clear(actions, event)
    except Exception as _e:
        print(f"[reply] 清理占位气泡失败（忽略）：{type(_e).__name__}: {_e}")

    gid = getattr(event, "group_id", None)
    if quote is None:
        quote = _flag("private_reply_quote", True) if gid is None else False
    if quote and gid is None:
        # 需求：私聊**只有纯文字回复**才带引用 ——
        #   * 用户必须自己先引用了某条消息（否则不回引用，见 user_quoted 的说明）；
        #   * 且这条回复必须全是文字段：语音/图片/文件/转发等一律不挂引用
        #     （实测"语音 + 引用"在 QQ 上引用块显示不正常）。
        if not user_quoted(event) or not all(isinstance(_s, Segments.Text) for _s in segs):
            quote = False

    try:
        if gid is not None:
            return await actions.send(group_id=gid, message=Manager.Message(*segs))

        mid = getattr(event, "message_id", None)
        if quote and mid is not None:
            segs = [Segments.Reply(mid)] + segs
        return await actions.send(user_id=event.user_id, message=Manager.Message(*segs))
    except Exception as e:
        print(f"[reply] 发送失败：{type(e).__name__}: {e}")
        return None


def quote_target(event):
    """私聊且"用户引用了消息"时返回要引用的 message_id，否则 None。

    给 speak_and_send(reply_to=...) 这类不走 reply_send 的发送路径用，
    保证"语音回复"也遵守同一条约定（但语音目前不挂引用，见 main.py 里的说明）。
    """
    if getattr(event, "group_id", None) is not None:
        return None
    if not _flag("private_reply_quote", True):
        return None
    if not user_quoted(event):
        return None
    return getattr(event, "message_id", None)
