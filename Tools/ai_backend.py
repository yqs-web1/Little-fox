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
    cloud_reasoning : 云端模型是否开启思考（思维链）模式，默认 True
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
    """
    o = _others()

    if is_cloud_mode():
        model = str(o.get("cloud_model") or default_mode).strip()
        # 中转站的模型名通常带思考属性，显式传思考开关，避免平台默认行为不一致
        extra: dict = {}
        if "reasoning" in model or any(h in model.lower() for h in _REASONING_HINTS):
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


def get_api_key() -> str:
    """取 API Key。优先级：环境变量 > config.json > 占位值。

    推荐把 Key 放进环境变量，config.json 的 deepseek_key 留空：
    配置文件很容易被随手打包/上传，而环境变量不在项目目录里。
    支持两个变量名：JIANER_API_KEY（本项目专用，优先）与 DEEPSEEK_API_KEY（通用）。
    本地模式也要求非空（LM Studio 会忽略该值，但 openai SDK 不允许空 key）。
    """
    for _env_name in ("JIANER_API_KEY", "DEEPSEEK_API_KEY"):
        _v = (os.environ.get(_env_name) or "").strip()
        if _v:
            return _v
    key = str(_others().get("deepseek_key") or "").strip()
    return key or "lm-studio"


def api_key_source() -> str:
    """说明当前 Key 来自哪里，供 ~后端状态 / 自检打印，避免"以为填了其实没生效"。"""
    for _env_name in ("JIANER_API_KEY", "DEEPSEEK_API_KEY"):
        if (os.environ.get(_env_name) or "").strip():
            return f"环境变量 {_env_name}"
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
