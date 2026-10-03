"""跨线程、跨事件循环的并发原语。

**关键约束（决定了本模块的实现方式）**：
Hyper 对每条消息执行 `asyncio.run(handler(...))`，也就是
**每条消息一个线程 + 一个全新的事件循环**（见 Hyper/Adapters/OneBot.py:193,265）。
因此：
  - `asyncio.Semaphore/Lock/Event` **不能跨消息复用** —— 在循环 A 里创建的对象拿到
    循环 B 使用会直接抛 "attached to a different loop"。
  - 想真正给「全局资源」限流（ASR 并发、TTS 并发、发消息速率），
    必须用 threading 原语，再用本模块的 `aslot()` 等包装器在**不阻塞事件循环**的前提下取用。

本模块只依赖标准库。
"""
from __future__ import annotations

import asyncio
import functools
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager


# --------------------------------------------------------------------------
# 并发槽位：threading.Semaphore，可同步用也可异步用
# --------------------------------------------------------------------------
class Gate:
    """有界并发闸门。跨线程/跨事件循环安全。"""

    def __init__(self, limit: int, name: str = "gate"):
        self.limit = max(1, int(limit))
        self.name = name
        self._sem = threading.BoundedSemaphore(self.limit)
        self._waiting = 0
        self._lock = threading.Lock()

    @contextmanager
    def slot(self, timeout: float | None = None):
        """同步用法：with gate.slot(): ...（在线程池里跑的任务用它）"""
        with self._lock:
            self._waiting += 1
        try:
            ok = self._sem.acquire(timeout=timeout) if timeout else self._sem.acquire()
            if not ok:
                raise TimeoutError(f"{self.name} 等待槽位超时")
        finally:
            with self._lock:
                self._waiting -= 1
        try:
            yield
        finally:
            self._sem.release()

    @property
    def waiting(self) -> int:
        with self._lock:
            return self._waiting


@asynccontextmanager
async def aslot(gate: Gate, timeout: float | None = None):
    """异步用法：async with aslot(gate, 10): ...（轮询取用，不阻塞事件循环）"""
    deadline = None if timeout is None else time.monotonic() + timeout
    with gate._lock:
        gate._waiting += 1
    acquired = False
    try:
        while True:
            if gate._sem.acquire(blocking=False):
                acquired = True
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"{gate.name} 等待槽位超时")
            await asyncio.sleep(0.02)
        yield
    finally:
        with gate._lock:
            gate._waiting -= 1
        if acquired:
            gate._sem.release()


# --------------------------------------------------------------------------
# 令牌桶限流（发消息洪水控制用）
# --------------------------------------------------------------------------
class TokenBucket:
    """线程安全的令牌桶。rate = 每秒补充的令牌数。"""

    def __init__(self, rate: float, capacity: float | None = None):
        self.rate = max(0.01, float(rate))
        self.capacity = float(capacity if capacity is not None else max(1.0, self.rate))
        self._tokens = self.capacity
        self._ts = time.monotonic()
        self._lock = threading.Lock()

    def _refill_locked(self):
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._ts) * self.rate)
        self._ts = now

    def try_acquire(self, n: float = 1.0) -> bool:
        with self._lock:
            self._refill_locked()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    async def acquire(self, n: float = 1.0, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.try_acquire(n):
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)


# --------------------------------------------------------------------------
# 熔断器
# --------------------------------------------------------------------------
class CircuitBreaker:
    """连续失败到阈值就断开一段时间，避免对着挂掉的端点死磕。"""

    def __init__(self, fail_threshold: int = 5, reset_seconds: float = 60.0):
        self.fail_threshold = max(1, int(fail_threshold))
        self.reset_seconds = float(reset_seconds)
        self._fails = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._fails < self.fail_threshold:
                return "closed"
            if time.monotonic() - self._opened_at >= self.reset_seconds:
                return "half-open"
            return "open"

    def allow(self) -> bool:
        with self._lock:
            if self._fails < self.fail_threshold:
                return True
            if time.monotonic() - self._opened_at >= self.reset_seconds:
                self._fails = self.fail_threshold - 1   # half-open：放一个探测请求
                return True
            return False

    def record_ok(self):
        with self._lock:
            self._fails = 0

    def record_fail(self):
        with self._lock:
            self._fails += 1
            if self._fails >= self.fail_threshold:
                self._opened_at = time.monotonic()


# --------------------------------------------------------------------------
# 带 TTL 的 LRU 缓存（ASR 结果、模型健康等）
# --------------------------------------------------------------------------
_MISS = object()


class LRUCache:
    def __init__(self, maxsize: int = 512, ttl: float | None = None):
        self.maxsize = max(1, int(maxsize))
        self.ttl = ttl
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            item = self._d.get(key)
            if item is None:
                return default
            exp, val = item
            if exp is not None and time.monotonic() > exp:
                self._d.pop(key, None)
                return default
            self._d.move_to_end(key)
            return val

    def set(self, key, value):
        with self._lock:
            exp = None if self.ttl is None else time.monotonic() + self.ttl
            self._d[key] = (exp, value)
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)

    def __len__(self):
        with self._lock:
            return len(self._d)


# --------------------------------------------------------------------------
# 单飞（同一份工作并发只做一次）—— 给 ASR 这类「转发同一段语音」的场景
# --------------------------------------------------------------------------
_INFLIGHT: dict = {}
_INFLIGHT_LOCK = threading.Lock()


def single_flight(key, fn, cache: LRUCache | None = None):
    """并发调用同一个 key 时只真正执行一次，其余等待并复用结果。

    fn 是**同步**函数（本模块用于跑在线程池里的本地 ASR/网络请求）。
    """
    if cache is not None:
        hit = cache.get(key, _MISS)
        if hit is not _MISS:
            return hit

    with _INFLIGHT_LOCK:
        ev = _INFLIGHT.get(key)
        owner = ev is None
        if owner:
            ev = threading.Event()
            _INFLIGHT[key] = ev

    if owner:
        try:
            result = fn()
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.pop(key, None)
                ev.set()
        if cache is not None:
            cache.set(key, result)
        return result

    # 等owner完成；等不到（失败/超时）就退化为自己跑
    ev.wait(timeout=120)
    if cache is not None:
        hit = cache.get(key, _MISS)
        if hit is not _MISS:
            return hit
    return fn()


# --------------------------------------------------------------------------
# 受限 CPU 线程池：ffmpeg 音效、本地 whisper 等 CPU 密集活儿共用
# --------------------------------------------------------------------------
_cpu_pool: ThreadPoolExecutor | None = None
_cpu_lock = threading.Lock()


def cpu_pool(max_workers: int | None = None) -> ThreadPoolExecutor:
    global _cpu_pool
    with _cpu_lock:
        if _cpu_pool is None:
            n = max_workers or max(2, min(4, (os.cpu_count() or 4) // 2))
            _cpu_pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="fox-cpu")
        return _cpu_pool


async def run_cpu(fn, *args, **kwargs):
    """把 CPU 密集函数放到受限线程池执行，不阻塞事件循环、也不无限起线程。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(cpu_pool(), functools.partial(fn, *args, **kwargs))


# --------------------------------------------------------------------------
# 全局共享实例（各模块复用，避免每处各写一套）
# --------------------------------------------------------------------------
ASR_GATE = Gate(12, "asr")          # 云端 ASR 并发
ASR_LOCAL_GATE = Gate(2, "asr-local")   # 本地 whisper（CPU 密集，不能放开）
TTS_GATE = Gate(8, "tts")           # edge-tts 合成并发
LLM_GATE = Gate(40, "llm")          # 大模型请求并发
SEND_BUCKET = TokenBucket(5.0, 8.0)  # 全局发消息速率（QQ 洪水控制）
