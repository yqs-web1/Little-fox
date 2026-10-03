"""私聊「对方正在输入」气泡。

实现依据（已逐处核对 NapCat 源码 NapCat.Shell/napcat.mjs）：
  - 接口名 set_input_status，参数 user_id(String) + event_type(Number)（:77278 的 schema）
  - 其 _handle 中 chatType 写死为 KCHATTYPEC2C（:77295）→ **只有私聊有效**
  - user_id 会经 getUidByUinV2 解析，非好友会抛 "uid is empty"（:77292-77293）

因此本模块遵循三条原则：
  1. 群聊一律不发（发了也是无效请求，白多一次往返）
  2. 任何异常都静默降级 —— 气泡是锦上添花，绝不能因为它打断正常回复
  3. QQ 客户端的气泡会自行超时淡出，发送真实消息也会立刻消掉它，
     所以没有「显式关闭」接口；长回复期间靠后台协程定时重发来维持可见

生命周期：Hyper 对每条消息执行 asyncio.run(handler(...))，handler 返回时
asyncio.run 会取消所有残留任务，因此刷新协程无需手工回收。
"""

import asyncio
import threading

REFRESH_SECONDS = 4.0   # 气泡在 QQ 里大约几秒后淡出，4 秒重发一次足以维持
_TASKS: set = set()     # 强引用，防止任务被 GC 提前回收（asyncio 只持弱引用）
_LOCK = threading.Lock()


async def _emit(actions, user_id) -> bool:
    """发一次「正在输入」。失败静默返回 False。"""
    try:
        await actions.custom.set_input_status(user_id=str(user_id), event_type=1)
        return True
    except Exception as e:
        # 非好友 / uid 解析失败 / 接口不存在，都走这里；不能向上抛
        print(f"[presence] 输入状态发送失败（{user_id}）：{type(e).__name__}: {e}")
        return False


async def _refresh(actions, user_id, interval):
    """先立刻亮一次气泡，之后每 interval 秒刷新一次，直到被取消。"""
    try:
        while True:
            await _emit(actions, user_id)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise                      # 正常退出路径：handler 结束由 asyncio.run 取消
    except Exception as e:
        print(f"[presence] 气泡刷新协程异常退出（{user_id}）：{type(e).__name__}: {e}")


def is_private(event) -> bool:
    """私聊事件不带 group_id。"""
    return getattr(event, "group_id", None) is None


def start_typing(actions, event, enabled: bool = True,
                 interval: float = REFRESH_SECONDS) -> bool:
    """开启「正在输入」气泡。仅私聊生效，幂等，失败静默。

    返回是否真的启动了刷新协程。
    """
    if not enabled or not is_private(event):
        return False
    user_id = getattr(event, "user_id", None)
    if user_id is None:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False               # 不在事件循环里（不该发生），安全跳过

    task = loop.create_task(_refresh(actions, user_id, interval))
    with _LOCK:
        _TASKS.add(task)
    task.add_done_callback(_discard)
    return True


def _discard(task) -> None:
    with _LOCK:
        _TASKS.discard(task)


async def stop_typing() -> None:
    """停止**当前事件循环**（即当前这条消息的处理）的气泡刷新。

    只清理挂在当前 loop 上的任务，避免误杀其它会话。
    一般无需调用：handler 返回时 asyncio.run 会自动取消残留任务。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    with _LOCK:
        victims = [t for t in _TASKS if not t.done() and t.get_loop() is loop]
    for t in victims:
        t.cancel()
