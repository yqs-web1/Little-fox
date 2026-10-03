"""模型注册表 + 三级作用域解析（用户 > 群 > 全局）。

要解决的现状问题：
  `main.py:132` 的 `EnableNetwork` 是**模块级全局变量**，`main.py:1107-1135` 直接赋值 ——
  A 用户发 `~Gemini`，B、C、D 所有人一起被切走。这是当前最严重的功能缺陷之一。
  本模块把"用哪个模型"变成**按请求解析**，并支持用户/群/全局三级就近覆盖。

配置（config.json → Others.models）：
  providers : 供应商（base_url / key_env / type），可被多个模型复用
  registry  : 模型清单（id / provider / model / label / tags / aliases / reasoning）
  default   : 全局默认模型 id
  fallback_chain : 主模型不可用时的降级顺序
"""

from __future__ import annotations

import threading
import time

from Tools import concurrency

_health_lock = threading.Lock()
_health_cache = concurrency.LRUCache(maxsize=64, ttl=300)   # provider -> (ok, msg, ts)


def _others() -> dict:
    try:
        from Hyper import Configurator
        return Configurator.cm.get_cfg().others or {}
    except Exception:
        return {}


def _models_cfg() -> dict:
    m = _others().get("models")
    return m if isinstance(m, dict) else {}


def providers() -> dict:
    p = _models_cfg().get("providers")
    return p if isinstance(p, dict) else {}


def registry() -> list:
    r = _models_cfg().get("registry")
    return [x for x in r if isinstance(x, dict)] if isinstance(r, list) else []


def default_id() -> str:
    d = str(_models_cfg().get("default") or "").strip()
    if d:
        return d
    reg = registry()
    return reg[0]["id"] if reg else ""


def find(key: str) -> dict | None:
    """按 id 或别名（大小写不敏感）查模型条目。"""
    if not key:
        return None
    k = str(key).strip()
    kl = k.lower()
    for e in registry():
        if str(e.get("id", "")).lower() == kl:
            return e
    for e in registry():
        for a in (e.get("aliases") or []):
            if str(a).lower() == kl:
                return e
    # 允许直接用序号（~模型 3）
    if k.isdigit():
        idx = int(k) - 1
        reg = registry()
        if 0 <= idx < len(reg):
            return reg[idx]
    return None


def fallback_ids() -> list:
    ch = _models_cfg().get("fallback_chain")
    if isinstance(ch, list) and ch:
        return [str(x) for x in ch]
    reg = registry()
    return [e["id"] for e in reg][:3]


# --------------------------------------------------------------------------
# 三级作用域：用户 > 群 > 全局
# --------------------------------------------------------------------------
def scoped_model_id(qq=None, group_id=None) -> str:
    """就近解析：用户偏好 > 群偏好 > 全局默认。"""
    try:
        from Tools import userstore
        if qq is not None:
            v = userstore.pref_get(qq, "model")
            if v and find(v):
                return find(v)["id"]
        if group_id is not None:
            v = userstore.group_pref_get(group_id, "model")
            if v and find(v):
                return find(v)["id"]
    except Exception as e:
        print(f"[模型] 作用域解析失败，回落全局默认：{e}")
    return default_id()


def set_user_model(qq, key: str) -> tuple[bool, str]:
    e = find(key)
    if not e:
        return False, f"没找到模型「{key}」"
    from Tools import userstore
    userstore.pref_set(qq, "model", e["id"])
    return True, render_entry(e)


def set_group_model(group_id, key: str) -> tuple[bool, str]:
    e = find(key)
    if not e:
        return False, f"没找到模型「{key}」"
    from Tools import userstore
    userstore.group_pref_set(group_id, "model", e["id"])
    return True, render_entry(e)


def set_global_model(key: str) -> tuple[bool, str]:
    """写回 config.json 的 Others.models.default，同时更新内存。"""
    e = find(key)
    if not e:
        return False, f"没找到模型「{key}」"
    try:
        from Hyper import Configurator
        cfg = Configurator.cm.get_cfg()
        m = cfg.others.get("models")
        if not isinstance(m, dict):
            m = {}
            cfg.others["models"] = m
        m["default"] = e["id"]
        # 落盘
        path = "config.json"
        import json
        data = json.load(open(path, encoding="utf-8"))
        data.setdefault("Others", {}).setdefault("models", {})["default"] = e["id"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True, render_entry(e)
    except Exception as ex:
        return False, f"写入全局默认失败：{type(ex).__name__}: {ex}"


# --------------------------------------------------------------------------
# 解析成客户端参数
# --------------------------------------------------------------------------
def resolve_model(model_id: str | None):
    """返回 (provider_cfg, entry) —— 找不到注册表条目时返回 (None, None)。"""
    e = find(model_id) if model_id else None
    if not e:
        return None, None
    p = providers().get(str(e.get("provider") or ""))
    if not isinstance(p, dict):
        return None, None
    return p, e


def is_registry_model(model_id: str | None) -> bool:
    return find(model_id) is not None


def render_entry(e: dict) -> str:
    tags = ("｜" + "/".join(str(t) for t in (e.get("tags") or []))) if e.get("tags") else ""
    return f"{e.get('label') or e.get('id')}（{e.get('model')}）{tags}"


def listing(current: str = "") -> str:
    reg = registry()
    if not reg:
        return "当前没有配置模型注册表（config.json → Others.models.registry）"
    lines = []
    for i, e in enumerate(reg, 1):
        mark = " ← 当前" if e.get("id") == current else ""
        hs = health_of(str(e.get("provider") or ""))
        badge = "" if hs is None else (" ✅" if hs[0] else " ⚠️")
        lines.append(f"  {i}. {render_entry(e)}{badge}{mark}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 健康检查（启动/按需探活，结果带 TTL 缓存）
# --------------------------------------------------------------------------
def health_of(provider_key: str):
    v = _health_cache.get(provider_key)
    return v if v is not None else None


def _probe_sync(provider_key: str, timeout: float = 8.0):
    p = providers().get(provider_key)
    if not isinstance(p, dict):
        return False, "provider 未定义"
    base = str(p.get("base_url") or "").strip()
    key_env = str(p.get("key_env") or "").strip()
    import os
    api_key = (os.environ.get(key_env, "") if key_env else "") or "none"
    try:
        import openai
        import httpx
        if p.get("type") == "gemini":
            return True, "gemini（未探活）"
        cli = openai.OpenAI(api_key=api_key, base_url=base or "https://api.deepseek.com/",
                            http_client=httpx.Client(proxy=None, trust_env=False,
                                                     timeout=httpx.Timeout(timeout, connect=4.0)))
        cli.models.list()
        return True, "OK"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"


def probe(provider_keys=None) -> dict:
    """探活指定 provider（默认全部），结果写入 TTL 缓存。返回 {provider: (ok, msg)}。"""
    keys = provider_keys if provider_keys is not None else list(providers())
    out = {}
    for k in keys:
        ok, msg = _probe_sync(k)
        _health_cache.set(k, (ok, msg, time.time()))
        out[k] = (ok, msg)
    return out


def health_report() -> str:
    provs = providers()
    if not provs:
        return "未配置任何 provider"
    lines = []
    for k in provs:
        h = health_of(k)
        if h is None:
            lines.append(f"  {k}: 未探活（发 {''}~模型 状态 触发）")
        else:
            ok, msg, ts = h
            lines.append(f"  {k}: {'✅ 正常' if ok else '⚠️ ' + msg}（{time.strftime('%H:%M:%S', time.localtime(ts))}）")
    return "\n".join(lines)


def auto_pick(has_image: bool = False, text: str = "") -> str:
    """智能路由：按任务特征挑模型。找不到合适的就回默认。"""
    reg = registry()
    if not reg:
        return default_id()
    tags_map = {str(e.get("id")): [str(t) for t in (e.get("tags") or [])] for e in reg}

    def first_with(tag):
        for e in reg:
            if tag in tags_map.get(str(e.get("id")), []):
                return str(e["id"])
        return ""

    if has_image:
        m = first_with("多模态") or first_with("看图")
        if m:
            return m
    t = text or ""
    if any(k in t for k in ("推理", "证明", "为什么", "推导", "算法", "代码", "debug", "报错")):
        m = first_with("推理")
        if m:
            return m
    if len(t) <= 12:
        m = first_with("快")
        if m:
            return m
    return default_id()
