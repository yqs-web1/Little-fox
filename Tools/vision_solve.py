import os
import base64
import uuid
import httpx

from Hyper import Configurator
from openai import OpenAI
from Tools import ai_backend  # Key 统一走 ai_backend.get_api_key()（环境变量优先）

Configurator.cm = Configurator.ConfigManager(Configurator.Config(file="config.json").load_from_file())

# 本机 LM Studio 里的多模态模型（gemma3 架构，支持图片输入）
VISION_MODEL = "google/gemma-3-4b"

# 高数解题时的系统提示：严谨的高等数学老师
MATH_SYS_PROMPT = (
    "你是一位严谨、耐心的高等数学老师。请针对用户给出的高等数学题目，"
    "给出清晰、完整的解题过程：先说明用到的定理/方法，再逐步推导，"
    "最后给出明确的最终答案。所有数学公式务必使用 LaTeX 表示（行内用 $...$，独立公式用 $$...$$）。"
    "如果题目条件不足或存在歧义，请指出并给出在合理假设下的解答。"
)


def _get_image_url(seg_image):
    """从 Segments.Image 取出可访问的图片地址（优先 http 直链）"""
    file_url = getattr(seg_image, "file", "") or ""
    if isinstance(file_url, str) and file_url.startswith("http"):
        return file_url
    return getattr(seg_image, "url", "") or ""


def _to_b64_data_url(path: str, mime: str = "image/png") -> str:
    with open(path, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode("ascii")


def _get_deepseek_client():
    """复刻 deepseek.py 的本地/官方双模式：有 local_base_url 走本地，否则走 DeepSeek 官方"""
    others = Configurator.cm.get_cfg().others
    key = ai_backend.get_api_key()
    local_base_url = others.get("local_base_url", "")
    if local_base_url:
        client = OpenAI(
            api_key=key,
            base_url=local_base_url,
            # trust_env=False 彻底绕过环境代理变量，防止 localhost 请求被丢给代理
            http_client=httpx.Client(proxy=None, trust_env=False),
        )
        model = others.get("local_model", "google/gemma-3-4b")
        return client, model
    client = OpenAI(api_key=key, base_url="https://api.deepseek.com/")
    # 官方推理模型对数学推导更强；若想用普通模型改成 "deepseek-chat"
    return client, "deepseek-reasoner"


def _get_vision_client():
    """Gemma 多模态走本地 LM Studio（127.0.0.1 必须绕过代理，trust_env=False 彻底无视代理变量）"""
    others = Configurator.cm.get_cfg().others
    key = ai_backend.get_api_key()
    local_base_url = others.get("local_base_url", "") or "http://localhost:1234/v1"
    client = OpenAI(
        api_key=key,
        base_url=local_base_url,
        http_client=httpx.Client(proxy=None, trust_env=False),
    )
    return client


def _download(url: str, save_path: str) -> bool:
    try:
        import requests
        r = requests.get(url, timeout=25)
        r.raise_for_status()
        with open(save_path, "wb") as f:
            f.write(r.content)
        return True
    except Exception as e:
        print(f"[vision_solve] 图片下载失败: {e}")
        return False


async def _recognize_problem(client, image_payload) -> str:
    """让 Gemma 识别图片中的高数题面；不是高数题返回 'NOT_MATH'"""
    resp = client.chat.completions.create(
        model=VISION_MODEL,
        temperature=0.2,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text":
                    "请识别这张图片中的高等数学题目。"
                    "如果它确实是一道高等数学题（如微积分、线性代数、概率统计、微分方程等），"
                    "请只输出题目的文字描述，公式用 LaTeX 表示，不要解答；"
                    "如果图片不是高等数学题（例如表情包、风景、人物、普通聊天截图等），"
                    "请只回复 NOT_MATH 这四个字母。"},
                image_payload,
            ],
        }],
    )
    return (resp.choices[0].message.content or "").strip()


async def _solve(client, model, problem: str, extra_text: str) -> str:
    user_content = f"题目：\n{problem}"
    if extra_text:
        user_content += f"\n\n用户补充：{extra_text}"
    resp = client.chat.completions.create(
        model=model,
        temperature=0.3,
        messages=[
            {"role": "system", "content": MATH_SYS_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


async def solve_math_image(event_message, extra_text: str = "", bot_name: str = "小狐狸") -> str:
    """高数拍照解题主入口。返回要发送给用户的文本（已排版）。"""
    # 1. 取第一张图片
    image_seg = None
    for item in event_message:
        if type(item).__name__ == "Image" or (hasattr(item, "__class__") and item.__class__.__name__ == "Image"):
            image_seg = item
            break
    if image_seg is None:
        return f"{bot_name}没有在这条消息里看到图片呀，发一张题目截图我才能帮你看哦 (｡･ω･｡)"

    img_url = _get_image_url(image_seg)
    if not img_url:
        return f"{bot_name}拿到了图片但取不到可访问的地址，可能是消息格式不支持 (｡•́︿•̀｡)"

    # 2. 准备图片载荷：优先本地下载转 base64，失败则用原 URL
    os.makedirs("temps", exist_ok=True)
    img_path = os.path.abspath(f"temps/math_img_{uuid.uuid4().hex}.png")
    image_payload = None
    if _download(img_url, img_path):
        try:
            image_payload = {"type": "image_url", "image_url": {"url": _to_b64_data_url(img_path)}}
        except Exception as e:
            print(f"[vision_solve] base64 失败: {e}")
    if image_payload is None:
        image_payload = {"type": "image_url", "image_url": {"url": img_url}}

    try:
        vclient = _get_vision_client()
        problem = await _recognize_problem(vclient, image_payload)
    except Exception as e:
        print(f"[vision_solve] 视觉识别失败: {e}")
        problem = ""
    finally:
        if os.path.exists(img_path):
            try:
                os.remove(img_path)
            except Exception:
                pass

    if not problem:
        return (f"{bot_name}看图识别失败了…大概率是本机还没加载多模态模型 Gemma-3-4B，"
                f"或者图片下载不了。\n请先在本机执行：lms load google/gemma-3-4b "
                f"（或在 LM Studio 里加载 Gemma-3-4B），再试一次 (｡•́︿•̀｡)")
    if problem == "NOT_MATH":
        return (f"这看起来不像一道高等数学题哦～\n如果你想让我解高数题，"
                f"发一张清晰的高等数学题目截图，或者加上 {bot_name} 的 ~高数 前缀就好 (｡･ω･｡)")

    # 3. 交给 DeepSeek（或本地模型）解答
    try:
        dclient, dmodel = _get_deepseek_client()
        answer = await _solve(dclient, dmodel, problem, extra_text)
    except Exception as e:
        print(f"[vision_solve] 解题失败: {e}")
        return (f"{bot_name}看到题目啦，但解答时出错了（可能是 DeepSeek 的 key 没填对、"
                f"或网络问题）。\n——识别到的题目：\n{problem}\n\n"
                f"提示：用官方 DeepSeek 请在 config.json 的 Others.deepseek_key 填你的 sk-...，"
                f"并把 local_base_url 清空 (｡•́︿•̀｡)")

    if not answer:
        return f"{bot_name}没解出东西来，题目可能是：\n{problem}\n再试一次或换个截图吧 (｡•́︿•̀｡)"

    return f"【识别到的题目】\n{problem}\n\n【解答】\n{answer}"
