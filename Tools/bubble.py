"""私聊「正在思考」占位气泡：延迟出现 → 回复到手前撤回。

背景：QQ 消息流里那个三点气泡是 QQ 开放平台给官方智能体的能力，
协议号（NapCat）只能用 set_input_status 触发「正在输入中…」（显示在名字下方），
拿不到气泡形态。要做到消息流里的气泡，只能自己发一条占位消息再撤回。

设计要点：

1. **延迟出现**（delay，默认 1.5 秒）
   只有生成超过 delay 秒才真正发出占位气泡。秒回的对话（实测常见 0~1 秒）
   完全不发、也不撤回，因此不会留下任何「已撤回」噪音。

2. **统一撤回**
   所有回复出口（reply_send / AIKernal.send_message / speak_and_send）在真正
   发送内容前都会调用 clear()：占位气泡若还没发出就取消定时器，若已发出就撤回。
   这样无论走文本、语音还是流式分片，都不会出现「... 一直留在聊天里」。

3. **兜底超时**
   万一某条路径异常没来得及 clear，占位气泡会在 max_seconds 后自己收掉。

4. **线程/事件循环隔离**
   Hyper 对每条消息执行 asyncio.run(handler(...))，每个 handler 有独立的
   事件循环。因此任务表以 (事件循环, 用户号) 为键，避免不同 handler 互相干扰。
"""

import asyncio
import threading

_TASKS: dict = {}   # (loop, user_id) -> asyncio.Task
_SENT: dict = {}    # (loop, user_id) -> message_id
_LOCK = threading.Lock()


def _keys(event):
    """返回 (key, user_id)。拿不到事件循环则返回 (None, None)。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None, None
    uid = getattr(event, "user_id", None)
    if uid is None:
        return None, None
    return (loop, str(uid)), str(uid)


def _others() -> dict:
    try:
        from Hyper import Configurator
        return Configurator.cm.get_cfg().others or {}
    except Exception:
        return {}


def _noop(*_a, **_k):
    return None


def is_private(event) -> bool:
    return getattr(event, "group_id", None) is None


async def _send(actions, Manager, Segments, event, text):
    """发占位气泡，返回 message_id（取不到则 None）。"""
    try:
        ret = await actions.send(
            user_id=event.user_id,
            message=Manager.Message(Segments.Text(text)),
        )
    except Exception as e:
        print(f"[bubble] 占位气泡发送失败：{type(e).__name__}: {e}")
        return None
    # actions.send 返回的通常是已 fetch 的 Ret；取不到就再 fetch 一次
    mid = getattr(getattr(ret, "data", None), "message_id", None)
    if mid is None:
        try:
            from Hyper import Manager as _M
            mid = _M.Ret.fetch(ret).data.message_id
        except Exception:
            pass
    return mid


async def _recall(actions, mid):
    if mid is None:
        return
    try:
        await actions.del_message(mid)
    except Exception as e:
        print(f"[bubble] 占位气泡撤回失败：{type(e).__name__}: {e}")


async def _worker(actions, Manager, Segments, event, key, delay, text, max_seconds):
    mid = None
    try:
        await asyncio.sleep(delay)          # 期间被 clear() 取消 → 根本不发
        mid = await _send(actions, Manager, Segments, event, text)
        if mid is not None:
            _SENT[key] = mid
            await asyncio.sleep(max_seconds)   # 兜底：没人来撤回就自己收掉
            await _recall(actions, mid)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[bubble] 占位气泡任务异常：{type(e).__name__}: {e}")
    finally:
        with _LOCK:
            _TASKS.pop(key, None)
            _SENT.pop(key, None)


def begin(actions, Manager, Segments, event, enabled: bool = True,
          delay: float = 1.5, text: str = "...", max_seconds: float = 60.0) -> bool:
    """开始计时；delay 秒内没有被 clear() 就发出占位气泡。仅私聊生效。"""
    if not enabled or not is_private(event):
        return False
    key, _uid = _keys(event)
    if key is None:
        return False

    with _LOCK:
        old = _TASKS.pop(key, None)
        old_mid = _SENT.pop(key, None)
    if old is not None and not old.done():
        old.cancel()
    if old_mid is not None:
        # 同一会话上一条的占位气泡还挂着，先收掉，避免「...」堆积
        asyncio.ensure_future(_recall(actions, old_mid))

    task = asyncio.get_running_loop().create_task(
        _worker(actions, Manager, Segments, event, key, delay, text, max_seconds))
    with _LOCK:
        _TASKS[key] = task
    task.add_done_callback(_noop)
    return True


async def clear(actions, event) -> bool:
    """真正发送内容前调用：取消未发出的定时器 / 撤回已发出的占位气泡。

    幂等，可在多条发送路径上重复调用。返回是否做过动作。
    """
    key, _uid = _keys(event)
    if key is None:
        return False
    with _LOCK:
        task = _TASKS.pop(key, None)
        mid = _SENT.pop(key, None)
    acted = False
    if task is not None and not task.done():
        task.cancel()
        acted = True
    if mid is not None:
        await _recall(actions, mid)
        acted = True
    return acted


def config_of(name: str, default):
    """从 config.json 的 Others 读一个开关/参数。"""
    try:
        return _others().get(name, default)
    except Exception:
        return default
