"""统一的 AI 后端解析器。

背景：机器人原先只有「本地 LM Studio」与「DeepSeek 官方」二选一，靠 config.json 里
local_base_url 是否为空来判断。现需要额外支持第三方中转站（自带 base_url + 特定模型名），
故把「用哪个后端、用哪个模型、要不要开思考模式」集中到本文件，避免各调用点各写一套判断。

config.json -> Others 段支持的字段：
    local_base_url  : 本地 LM Studio 地址，如 http://localhost:1234/v1（默认走本地）
    local_model     : 本地模型名，如 google/gemma-3-4b
    deepseek_key    : API Key。本地模式下任意非空字符串即可（LM Studio 不校验）
    cloud_base_url  : 云端中转站地址；留空则回落到 DeepSeek 官方 https://api.deepseek.com/
    cloud_model     : 云端模型名，如 deepseek-v4-flash
    cloud_reasoning : 云端模型是否开启思考（思维链）模式；不写则按模型名自动判断（默认 True 倾向）
    ai_backend      : "local" | "cloud"，决定走本地还是云端；缺省为 "local"
    cloud_extra_headers : 云端额外请求头（部分中转站要带 sso-ak 之类的头），缺省 {}
"""
from __future__ import annotations

import os

# 云端模型是否属于「思维链」模型：这些模型默认返回 reasoning_content，
# 调用时应显式带上思考开关，且回复里需要把思考过程与正文分开处理。
_REASONING_HINTS = ("reasoner", "thinking", "thinking-mode", "-r1", "qwq")


def _others() -> dict:
    """从运行中的 Configurator 读 Others 段；读不到就返回空 dict（走默认分支）。"""
    try:
        from Hyper import Configurator
        return Configurator.cm.get_cfg().others or {}
    except Exception:
        return {}


def is_cloud_mode() -> bool:
    """当前是否走云端。ai_backend 显式为 cloud 时才走云端，缺省保持本地（不破坏原有行为）。"""
    return str(_others().get("ai_backend", "local")).strip().lower() == "cloud"


def resolve(default_mode: str = "deepseek-chat") -> tuple[str, str, dict, bool]:
    """返回 (base_url, model_name, extra_body, use_proxy_bypass)。

    base_url 为空串表示用 DeepSeek 官方地址。
    use_proxy_bypass 恒为 True：本地与中转站都需要 trust_env=False 绕过环境代理。

    需求⑤：若传入的是**模型注册表里的 id/别名**（Tools/model_registry.py），
    则按注册表解析供应商与模型名；否则完全走原逻辑（向后兼容，不影响已有部署）。
    """
    o = _others()

    # ---- 优先：模型注册表（~模型 切换走这条）----
    try:
        from Tools import model_registry
        p, e = model_registry.resolve_model(default_mode)
        if p is not None:
            base = str(p.get("base_url") or "").strip()
            model = str(e.get("model") or "").strip()
            extra: dict = {}
            if e.get("reasoning"):
                extra["return_reasoning"] = True
            if isinstance(e.get("extra_body"), dict):
                extra.update(e["extra_body"])
            return base, model, extra, True
    except Exception as ex:
        print(f"[ai_backend] 注册表解析失败，回落旧逻辑：{type(ex).__name__}: {ex}")

    if is_cloud_mode():
        model = str(o.get("cloud_model") or default_mode).strip()
        # 中转站的模型名通常带思考属性，显式传思考开关，避免平台默认行为不一致。
        # cloud_reasoning 若在 config.json 里显式写了，就以它为准（控制台可改）；
        # 没写（None）则维持原来的"按模型名猜"行为，不影响已有部署。
        extra: dict = {}
        explicit = o.get("cloud_reasoning")
        if explicit is None:
            wants_reasoning = ("reasoning" in model
                               or any(h in model.lower() for h in _REASONING_HINTS))
        else:
            wants_reasoning = bool(explicit)
        if wants_reasoning:
            extra["return_reasoning"] = True
        base_url = str(o.get("cloud_base_url") or "").strip()
        return base_url, model, extra, True

    base_url = str(o.get("local_base_url") or "").strip()
    if base_url:
        # 本地模型：模型名以 local_model 为准，extra_body 留空（LM Studio 不认云端私有参数）
        return base_url, str(o.get("local_model") or default_mode), {}, True

    # 既没 ai_backend=cloud 也没 local_base_url -> DeepSeek 官方
    model = default_mode
    extra = {"return_reasoning": True} if "reasoner" in default_mode else {}
    return "", model, extra, True


def _user_env(name: str) -> str:
    """读 Windows 用户级环境变量（HKCU\\Environment）作为兜底。

    为什么需要：Key 已改为放环境变量（见 安装与配置说明.md §4.1），但环境变量只对**之后新开的**
    进程生效 —— 如果机器人是从环境块较旧的父进程（旧的 cmd 窗口 / 托盘）拉起来的，就读不到 Key，
    表现成"明明设过了却提示未配置"。注册表里的值是持久化的，这里作为最后一道兜底。
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as _k:
            _v, _ = winreg.QueryValueEx(_k, name)
            return str(_v or "").strip()
    except Exception:
        return ""


def _env_key(names) -> tuple[str, str]:
    """按顺序取第一个非空的环境变量，先看进程环境、再看用户级注册表。返回 (值, 变量名)。"""
    for _n in names:
        _v = (os.environ.get(_n) or "").strip() or _user_env(_n)
        if _v:
            return _v, _n
    return "", ""


def get_api_key(base_url: str | None = None) -> str:
    """取 API Key。优先级：provider 专属环境变量 > 通用环境变量 > config.json > 占位值。

    推荐把 Key 放进环境变量，config.json 的 deepseek_key 留空：
    配置文件很容易被随手打包/上传，而环境变量不在项目目录里。
    支持两个通用变量名：JIANER_API_KEY（本项目专用，优先）与 DEEPSEEK_API_KEY。

    需求⑤：传了 base_url 时，优先用**该 provider 在 models.providers 里配置的 key_env**，
    这样多个供应商可以各用各的 Key；只有一个通用 Key 时行为与原来完全一致。
    """
    if base_url:
        try:
            from Tools import model_registry
            for _pk, _p in model_registry.providers().items():
                if str(_p.get("base_url") or "").strip() == str(base_url).strip():
                    _env = str(_p.get("key_env") or "").strip()
                    if _env:
                        _pv = (os.environ.get(_env) or "").strip() or _user_env(_env)
                        if _pv:
                            return _pv
        except Exception:
            pass
    _v, _ = _env_key(("JIANER_API_KEY", "DEEPSEEK_API_KEY"))
    if _v:
        return _v
    key = str(_others().get("deepseek_key") or "").strip()
    return key or "lm-studio"


def api_key_source() -> str:
    """说明当前 Key 来自哪里，供 ~后端状态 / 自检打印，避免"以为填了其实没生效"。"""
    for _n in ("JIANER_API_KEY", "DEEPSEEK_API_KEY"):
        _v = (os.environ.get(_n) or "").strip() or _user_env(_n)
        if _v:
            return f"环境变量 {_n}"
    if str(_others().get("deepseek_key") or "").strip():
        return "config.json 的 deepseek_key"
    return "默认占位值（未配置）"


def get_extra_headers() -> dict:
    """云端中转站可能要求额外请求头。"""
    o = _others()
    raw = o.get("cloud_extra_headers") or {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def describe() -> str:
    """给 QQ 指令回显用的一句话描述。"""
    base_url, model, _, _ = resolve()
    if is_cloud_mode():
        host = base_url or "https://api.deepseek.com/ (官方)"
        return f"云端 DeepSeek｜模型 {model}｜{host}"
    if base_url:
        return f"本地模型｜{model}｜{base_url}"
    return f"DeepSeek 官方｜{model}"
