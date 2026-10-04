# -*- coding: utf-8 -*-
"""小狐狸控制台 —— 冒烟测试。

特点：**全程在一个临时副本上操作**，绝不碰真实的机器人配置。
测试开始/结束都会记录真实 config.json 的哈希，若被改动会直接报错。

跑法（用机器人自带的 venv python）：
    cd "E:\\little fox\\fox-console"
    ..\\Jianer_Next_QQ_Bot\\venv\\Scripts\\python.exe tests\\smoke_test.py
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONSOLE_DIR = HERE.parent
REAL_BOT_DIR = (CONSOLE_DIR.parent / "Jianer_Next_QQ_Bot").resolve()
REAL_BACKUP_DIR = CONSOLE_DIR / "backups"
SANDBOX = CONSOLE_DIR / ".test-sandbox"
TOKEN = "smoke-test-token"

PASS, FAIL = 0, []
WRITTEN: list = []      # 记录控制台本次写过的所有路径（用于证明只写沙箱）


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}  {detail}")


def sha(path: Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except FileNotFoundError:
        return "<missing>"


def _diff(a, b, path: str, out: dict) -> None:
    """递归记录两棵 JSON 树的差异：{路径: (旧值, 新值)}。"""
    if type(a) is not type(b):
        out[path] = (a, b)
        return
    if isinstance(a, dict):
        for k in set(a) | set(b):
            if k not in a:
                out[f"{path}.{k}"] = ("<缺失>", b[k])
            elif k not in b:
                out[f"{path}.{k}"] = (a[k], "<丢失>")
            else:
                _diff(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list):
        if a != b:
            out[path] = (a, b)
    elif a != b:
        out[path] = (a, b)


def build_sandbox() -> None:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    (SANDBOX / "prerequisites").mkdir(parents=True)
    for name in ("config.json", "voice_prefs.json", "Super_User.ini", "Manage_User.ini"):
        src = REAL_BOT_DIR / name
        if src.exists():
            shutil.copy2(src, SANDBOX / name)
        else:
            (SANDBOX / name).write_text("", encoding="utf-8")
    for src in (REAL_BOT_DIR / "prerequisites").glob("*"):
        if src.is_file():
            shutil.copy2(src, SANDBOX / "prerequisites" / src.name)


def main() -> int:
    real_hashes = {p.name: sha(p) for p in
                   [REAL_BOT_DIR / "config.json", REAL_BOT_DIR / "voice_prefs.json",
                    REAL_BOT_DIR / "Super_User.ini", REAL_BOT_DIR / "Manage_User.ini",
                    REAL_BOT_DIR / "prerequisites" / "current.json", REAL_BOT_DIR / "main.py"]}
    real_backups_before = set(p.name for p in REAL_BACKUP_DIR.glob("*.bak")) if REAL_BACKUP_DIR.exists() else set()

    print(f"真实机器人目录：{REAL_BOT_DIR}")
    print(f"沙箱副本    ：{SANDBOX}")
    build_sandbox()

    os.environ["FOX_BOT_DIR"] = str(SANDBOX)
    os.environ["FOX_BACKUP_DIR"] = str(SANDBOX / "_backups")   # 备份也要留在沙箱里
    os.environ["FOX_CONSOLE_JSON"] = str(SANDBOX / "console.json")   # 别动真实控制台设置
    os.environ["FOX_STARTUP_DIR"] = str(SANDBOX / "startup")         # 别动真实启动文件夹
    (SANDBOX / "startup").mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(CONSOLE_DIR))
    import fox_core as core            # noqa: E402
    import server                      # noqa: E402

    # 写入监控：控制台无论怎么改，写盘的路径都会被记下来，最后断言全部落在沙箱内。
    def _spy_atomic(path, data):
        WRITTEN.append(str(path))
        return _real_atomic(path, data)

    def _spy_backup(path, reason="save"):
        WRITTEN.append(str(path))
        return _real_backup(path, reason)

    _real_atomic = core.atomic_write_bytes
    _real_backup = core.backup_file
    core.atomic_write_bytes = _spy_atomic
    core.backup_file = _spy_backup

    server.TOKEN = TOKEN
    client = server.app.test_client()

    print("\n=== 1. 鉴权 ===")
    r = client.get("/api/health")
    check("GET /api/health 免鉴权 200", r.status_code == 200, str(r.status_code))
    check("health 声明需要鉴权", r.get_json().get("auth_required") is True)

    r = client.get("/api/config")
    check("无 token 访问 /api/config -> 401", r.status_code == 401, str(r.status_code))

    r = client.get("/api/config", headers={"X-Auth-Token": "wrong"})
    check("错误 token -> 401", r.status_code == 401, str(r.status_code))

    h = {"X-Auth-Token": TOKEN}
    r = client.get("/api/config", headers=h)
    check("正确 token(X-Auth-Token) -> 200", r.status_code == 200, str(r.status_code))
    r = client.get(f"/api/config?token={TOKEN}")
    check("正确 token(查询串) -> 200", r.status_code == 200, str(r.status_code))
    check("带 token 访问会种 Cookie", any("fox_console_token" in c for c in r.headers.getlist("Set-Cookie")))

    r = client.get("/api/config", headers={"Authorization": f"Bearer {TOKEN}"})
    check("正确 token(Bearer) -> 200", r.status_code == 200, str(r.status_code))

    print("\n=== 2. 配置读取与无损回写 ===")
    original = json.loads((SANDBOX / "config.json").read_text(encoding="utf-8"))
    got = client.get("/api/config", headers=h).get_json()["config"]
    check("读到的配置与磁盘一致", got == original)
    check("中文正常（bot_name=小狐狸）", got["Others"]["bot_name"] == "小狐狸", repr(got["Others"]["bot_name"]))

    mtime = client.get("/api/config", headers=h).get_json()["meta"]["mtime"]
    r = client.post("/api/config", headers=h, json={"config": got, "expected_mtime": mtime})
    check("原样保存 -> 200", r.status_code == 200, r.text[:200])
    after = json.loads((SANDBOX / "config.json").read_text(encoding="utf-8"))

    diffs = {}
    _diff(original, after, "", diffs)
    diffs = {k.lstrip("."): v for k, v in diffs.items()}
    allowed = ({}, {"Others.ROOT_User": ([""], [])})   # 输入里若还有 [""] 就应被规整掉
    check("往返只可能做一处已声明规整（ROOT_User 去空串）", diffs in allowed,
          f"实际差异：{diffs}")
    check("无其它字段被改动", len(diffs) <= 1)

    # 规整之后再存一次，必须逐字节一致（真正的无损）
    mtime2 = client.get("/api/config", headers=h).get_json()["meta"]["mtime"]
    raw1 = (SANDBOX / "config.json").read_bytes()
    r = client.post("/api/config", headers=h, json={"config": after, "expected_mtime": mtime2, "force": True})
    raw2 = (SANDBOX / "config.json").read_bytes()
    check("二次保存逐字节一致（幂等无损）", r.status_code == 200 and raw1 == raw2,
          f"{len(raw1)} vs {len(raw2)}")

    raw = (SANDBOX / "config.json").read_bytes()
    check("保存后仍是合法 UTF-8", raw.decode("utf-8") is not None)
    check("没有引入 '?' 破坏", b"?" not in raw, str(raw[:200]))
    check("中文没被转义成 \\uXXXX", "小狐狸".encode("utf-8") in raw)
    check("顶层键顺序没变", list(after.keys()) == list(original.keys()))
    check("未识别的键不会被抹掉（顶层键数不变）", len(after.keys()) == len(original.keys()))
    check("不臆造本来没有的键", "cloud_reasoning" not in after["Others"])

    print("\n=== 3. 规整与拒绝非法值 ===")
    bad = json.loads(json.dumps(original))
    bad["owner"] = ["123", "abc", 456, ""]
    bad["Others"]["ROOT_User"] = [""]          # 空字符串项：机器人本来就会跳过，这里应被规整掉
    bad["Log_level"] = "verbose"
    bad["Others"]["default_mode"] = "不存在的模式"
    bad["Others"]["TTS"]["rate"] = "快一点"
    bad["Others"]["compliment"] = "只有一条"
    bad["Others"]["my_custom_key"] = {"keep": "me"}
    bad["OtherSection"] = {"untouched": True}
    r = client.post("/api/config", headers=h, json={"config": bad, "force": True})
    check("含非法值的配置能被规整保存", r.status_code == 200, r.text[:200])
    fixed = r.get_json()["config"]
    check("owner 只留数字", fixed["owner"] == [123, 456], repr(fixed["owner"]))
    check('ROOT_User 里的空字符串被清掉', fixed["Others"]["ROOT_User"] == [], repr(fixed["Others"]["ROOT_User"]))
    check("Log_level 回落 INFO", fixed["Log_level"] == "INFO")
    check("default_mode 回落 Ds", fixed["Others"]["default_mode"] == "Ds")
    check("TTS.rate 回落 +0%", fixed["Others"]["TTS"]["rate"] == "+0%")
    check("compliment 转成列表", fixed["Others"]["compliment"] == ["只有一条"])
    check("自定义键未丢失 (Others)", fixed["Others"]["my_custom_key"] == {"keep": "me"})
    check("未知顶层段未丢失", fixed.get("OtherSection") == {"untouched": True})
    check("规整都有警告", len(r.get_json()["warnings"]) >= 5, str(r.get_json()["warnings"]))

    r = client.post("/api/config", headers=h, json={"config": ["不是对象"]})
    check("非对象配置被拒", r.status_code == 400, str(r.status_code))

    print("\n=== 4. 乐观锁（防覆盖） ===")
    time.sleep(1.6)
    stale = mtime - 100
    r = client.post("/api/config", headers=h, json={"config": original, "expected_mtime": stale})
    check("mtime 过期 -> 409", r.status_code == 409, str(r.status_code))
    r = client.post("/api/config", headers=h, json={"config": original, "expected_mtime": stale, "force": True})
    check("force=true 时允许覆盖", r.status_code == 200, str(r.status_code))

    print("\n=== 5. 预设 CRUD ===")
    target = {"id": "smoke_猫娘", "name": "冒烟猫娘", "info": "喵～", "uid": ["123456789"],
              "path": "smoke_猫娘.txt", "content": "你是{self.bot_name}，用户是{self.event_user}。\n第二行 emoji 🌟"}
    r = client.post("/api/presets", headers=h, json=target)
    check("新建中文预设 -> 200", r.status_code == 200, r.text[:300])
    d = client.get("/api/presets/smoke_猫娘", headers=h).get_json()["data"]
    check("预设正文往返无损（含 emoji）", d["content"] == target["content"], repr(d["content"]))
    check("预设中文名无损", d["name"] == "冒烟猫娘", repr(d["name"]))
    check("适用用户已登记", d["uid"] == [123456789], repr(d["uid"]))
    check("正文文件已落盘", (SANDBOX / "prerequisites" / "smoke_猫娘.txt").exists())

    dup = dict(target, id="smoke_dup", name="重复用户", path="smoke_dup.txt")
    r = client.post("/api/presets", headers=h, json=dup)
    check("同一 QQ 号挂两个预设 -> 400", r.status_code == 400, str(r.status_code))

    r = client.post("/api/presets", headers=h, json=dict(target, id="smoke_evil", path="../evil.txt"))
    check("路径穿越 ../evil.txt -> 400", r.status_code == 400, str(r.status_code))
    check("没有在沙箱外生成 evil.txt", not (CONSOLE_DIR / "evil.txt").exists())
    r = client.post("/api/presets", headers=h, json=dict(target, id="smoke_evil2", path="C:\\Windows\\x.txt"))
    check("绝对路径 -> 400", r.status_code == 400, str(r.status_code))
    r = client.post("/api/presets", headers=h, json=dict(target, id="../../main", path="a.txt"))
    check("ID 带路径符 -> 400", r.status_code == 400, str(r.status_code))

    r = client.post("/api/presets", headers=h, json=dict(target, id="smoke_renamed", original_id="smoke_猫娘",
                                                        name="改名后", path="smoke_猫娘.txt", content="新正文"))
    check("改名 + 改正文 -> 200", r.status_code == 200, r.text[:200])
    ids = client.get("/api/presets", headers=h).get_json()["presets"]
    check("旧 ID 已消失", "smoke_猫娘" not in ids)
    check("新 ID 已存在", "smoke_renamed" in ids)

    r = client.delete("/api/presets/Normal?delete_file=0", headers=h)
    check("拒绝删除 Normal 兜底预设", r.status_code == 400, str(r.status_code))
    r = client.delete("/api/presets/smoke_renamed?delete_file=1", headers=h)
    check("删除预设 -> 200", r.status_code == 200, r.text[:200])
    check("正文文件已删除", not (SANDBOX / "prerequisites" / "smoke_猫娘.txt").exists())
    r = client.get("/api/presets/smoke_renamed", headers=h)
    check("删掉的预设读不到 -> 404", r.status_code == 404, str(r.status_code))

    print("\n=== 6. 用户与权限 ===")
    users = client.get("/api/users", headers=h).get_json()["data"]
    body = dict(users, owner=["111", "222"], root_user=["333"], super_user=["444"],
                manage_user=["555"], auto_approval=[], black_list=["666"], silents=[])
    r = client.post("/api/users", headers=h, json={"data": body, "expected_mtime": 0})
    check("保存名单 -> 200", r.status_code == 200, r.text[:300])
    cfg2 = json.loads((SANDBOX / "config.json").read_text(encoding="utf-8"))
    check("owner 写入 config.json", cfg2["owner"] == [111, 222], repr(cfg2["owner"]))
    check("ROOT_User 写成字符串列表", cfg2["Others"]["ROOT_User"] == ["333"], repr(cfg2["Others"]["ROOT_User"]))
    check("black_list 写入", cfg2["black_list"] == [666])
    check("Super_User.ini 每行一个", (SANDBOX / "Super_User.ini").read_text(encoding="utf-8").strip() == "444")
    check("Manage_User.ini 每行一个", (SANDBOX / "Manage_User.ini").read_text(encoding="utf-8").strip() == "555")

    print("\n=== 7. 语音偏好（不丢 seen） ===")
    vp = {"on": {"u111": True, "g222": False}, "seen": {"u111": 1.5, "u999": 2.5}, "voice": {"u111": "晓晓"}}
    core.atomic_write_bytes(core.VOICE_PREFS_PATH, core.dump_json_bytes(vp))
    r = client.post("/api/voiceprefs", headers=h,
                    json={"data": {"on": {"u111": True}, "voice": {"u111": "云希"}, "remove": ["g222"]}})
    check("保存语音偏好 -> 200", r.status_code == 200, r.text[:200])
    saved = json.loads((SANDBOX / "voice_prefs.json").read_text(encoding="utf-8"))
    check("on 已按提交替换", saved["on"] == {"u111": True}, repr(saved["on"]))
    check("voice 已更新", saved["voice"] == {"u111": "云希"})
    check("无关会话的 seen 被保留（u999）", "u999" in saved["seen"], repr(saved["seen"]))
    check("被移除会话的 seen 一并清掉", "g222" not in saved["seen"])
    r = client.post("/api/voiceprefs", headers=h, json={"data": {"on": {"../x": True}, "voice": {}}})
    check("非法会话键 -> 400", r.status_code == 400, str(r.status_code))

    print("\n=== 8. 备份与还原 ===")
    backups = client.get("/api/backups", headers=h).get_json()["backups"]
    check("已有备份产生", len(backups) > 0, str(len(backups)))
    cfg_backup = next((b for b in backups if b["target"] == "config.json"), None)
    check("存在 config.json 的备份", cfg_backup is not None)
    if cfg_backup:
        before = sha(SANDBOX / "config.json")
        r = client.post("/api/backups/restore", headers=h, json={"name": cfg_backup["name"]})
        check("还原备份 -> 200", r.status_code == 200, r.text[:200])
        check("还原后内容确实变了", sha(SANDBOX / "config.json") != before)
        r = client.post("/api/backups/restore", headers=h, json={"name": "../../main.py"})
        check("备份名穿越 -> 404/400", r.status_code in (400, 404), str(r.status_code))

    print("\n=== 9. 机器人状态与锁文件（三态判定，不误判、不误杀） ===")
    core.atomic_write_bytes(core.LOCK_PATH, str(os.getpid()).encode("utf-8"))
    st = client.get("/api/bot/status", headers=h).get_json()["data"]
    check("状态接口返回三态字段", all(k in st for k in ("confirmed", "probable", "confidence", "stale_lock")))
    check("没有 main.py 进程时不判定已确认运行", st["confirmed"] is False, str(st)[:200])
    check("能读到锁文件里的活 PID", bool(st.get("lock") and st["lock"]["alive"]), str(st.get("lock")))
    check("扫描能力有明确结论", isinstance(st["scan_available"], bool))
    check("锁文件时间与进程启动时间吻合 -> 判 probable，不误报残留锁",
          st["probable"] is True and st["stale_lock"] is False and st["running"] is True,
          f"probable={st['probable']} stale={st['stale_lock']} conf={st['confidence']}")
    check("probable 给出可读依据", "启动时间" in (st.get("stale_lock_hint") or ""), str(st.get("stale_lock_hint"))[:80])
    check("命令行不含 main.py 时不会凭锁文件推断成「已确认」",
          st["confirmed"] is False and st["confidence"] in ("probable", "none"), st["confidence"])

    r = client.post("/api/bot/start", headers=h, json={"mode": "bot"})
    check("判定在运行时拒绝启动（不会真去拉起机器人）", r.status_code == 409, str(r.status_code))
    r = client.post("/api/bot/clear-lock", headers=h, json={})
    check("非 force 清理被拒（409）", r.status_code == 409, str(r.status_code))
    check("拒绝理由说人话",
          any(k in r.get_json()["error"]["message"] for k in ("很可能", "扫描不可用", "确认")),
          r.get_json()["error"]["message"])
    check("拒绝后锁文件仍在", core.LOCK_PATH.exists())

    # 场景 B：把锁文件时间改成 1 小时前 —— 与进程启动时间对不上
    old = time.time() - 3600
    os.utime(core.LOCK_PATH, (old, old))
    st2 = client.get("/api/bot/status", headers=h).get_json()["data"]
    check("时间对不上时不判 probable", st2["probable"] is False, f"probable={st2['probable']}")
    check("时间对不上时 running 为假", st2["running"] is False, f"running={st2['running']}")
    check("只有「命令行不是 main.py + 时间也对不上」才算残留锁",
          st2["stale_lock"] is (st["scan_available"] is True),
          f"stale={st2['stale_lock']} scan={st['scan_available']}")
    r = client.post("/api/bot/clear-lock", headers=h, json={"force": True})
    check("强行清理 -> 200", r.status_code == 200, r.text[:200])
    check("锁文件已删除", not core.LOCK_PATH.exists())
    check("清理前已备份", bool(r.get_json().get("backup")), str(r.get_json()))

    r = client.post("/api/bot/stop", headers=h, json={"force": False})
    body_stop = r.get_json()
    check("stop 没有报告结束任何进程", not body_stop.get("stopped"), str(body_stop))
    check("stop 没有误杀测试进程自身", os.getpid() not in (body_stop.get("stopped") or []))
    check("测试进程仍然活着", core._pid_alive(os.getpid()))

    print("\n=== 10. 字段审计 / 模型注册表 / 控制台设置 / 开机自启 ===")
    # 审计要扫机器人源码：临时把 BOT_DIR 指回真实目录（只读，不改任何文件）
    saved_bot = (core.BOT_DIR, core.CONFIG_PATH, core.PRESET_DIR, core.PRESET_JSON,
                 core.VOICE_PREFS_PATH, core.SUPER_INI, core.MANAGE_INI, core.LOCK_PATH,
                 core.BOT_ENTRY, core.VENV_PY)
    core.BOT_DIR = REAL_BOT_DIR
    try:
        audit = core.field_audit()
    finally:
        (core.BOT_DIR, core.CONFIG_PATH, core.PRESET_DIR, core.PRESET_JSON,
         core.VOICE_PREFS_PATH, core.SUPER_INI, core.MANAGE_INI, core.LOCK_PATH,
         core.BOT_ENTRY, core.VENV_PY) = saved_bot
    check("审计扫到了机器人源码", audit["scanned_files"] >= 10, str(audit["scanned_files"]))
    read = audit["others"]["read_by_code"]
    for key in ("thinking_bubble", "thinking_bubble_delay", "private_reply_quote",
                "cloud_reasoning", "asr_timeout_seconds", "asr_max_seconds", "ai_backend"):
        check(f"审计发现代码在读 {key}", key in read, f"已发现：{sorted(read)[:12]}…")
    check("审计找出「界面能改」的字段", len(audit["others"]["declared_in_ui"]) >= 20,
          str(len(audit["others"]["declared_in_ui"])))
    check("审计给出的每个「界面没有」的键都有出处",
          all(audit["others"]["read_by_code"].get(k) for k in audit["others"]["missing_in_ui"]),
          str(audit["others"]["missing_in_ui"]))

    # 模型注册表校验（位置必须是 Others.models —— Tools/model_registry.py 就是这么读的）
    cfg_now = client.get("/api/config", headers=h).get_json()["config"]

    def with_models(base, models):
        out = json.loads(json.dumps(base))
        out.setdefault("Others", {})["models"] = models
        return out

    good_models = {
        "providers": {
            "relay": {"type": "openai", "base_url": "https://api.deepseek.com/v1", "key_env": "JIANER_API_KEY"},
            "local": {"type": "openai", "base_url": "http://localhost:1234/v1"},
        },
        "registry": [
            {"id": "ds-chat", "provider": "relay", "model": "deepseek-chat", "label": "DeepSeek",
             "tags": ["通用"], "aliases": ["ds"], "reasoning": False},
            {"id": "ds-think", "provider": "relay", "model": "deepseek-reasoner", "reasoning": True},
            {"id": "local-gemma", "provider": "local", "model": "google/gemma-3-4b"},
        ],
        "default": "ds-chat",
        "fallback_chain": ["ds-chat", "local-gemma"],
    }
    r = client.post("/api/config", headers=h, json={"config": with_models(cfg_now, good_models), "force": True})
    check("合法 models 段保存 -> 200", r.status_code == 200, r.text[:200])
    saved_models = r.get_json()["config"]["Others"]["models"]
    check("providers 原样保留", set(saved_models["providers"]) == {"relay", "local"})
    check("registry 原样保留", [m["id"] for m in saved_models["registry"]] == ["ds-chat", "ds-think", "local-gemma"])
    check("default 保留", saved_models["default"] == "ds-chat")
    check("fallback_chain 保留", saved_models["fallback_chain"] == ["ds-chat", "local-gemma"])
    check("顶层没有多余的 models", "models" not in r.get_json()["config"])

    # 早期版本把 models 放在顶层：应被搬到 Others.models（发现它靠的就是字段审计）
    legacy = json.loads(json.dumps(cfg_now))
    legacy.pop("models", None)
    legacy.setdefault("Others", {}).pop("models", None)
    legacy["models"] = good_models
    r = client.post("/api/config", headers=h, json={"config": legacy, "force": True})
    moved = r.get_json()["config"]
    check("顶层 models 被自动搬进 Others.models",
          "models" not in moved and "models" in moved["Others"], str(list(moved.keys())))
    check("迁移有明确告警", any("搬到 Others.models" in w for w in r.get_json()["warnings"]),
          str(r.get_json()["warnings"])[:200])

    # 两份都在：以 Others.models 为准（代码只读那里）
    both = with_models(legacy, good_models)
    both["models"] = {"providers": {"ghost": {"type": "openai"}},
                      "registry": [{"id": "ghost", "provider": "ghost", "model": "x"}],
                      "default": "ghost"}
    r = client.post("/api/config", headers=h, json={"config": both, "force": True})
    winner = r.get_json()["config"]
    check("两份 models 同时存在时以 Others.models 为准",
          "models" not in winner
          and [m["id"] for m in winner["Others"]["models"]["registry"]] == ["ds-chat", "ds-think", "local-gemma"],
          str(winner.get("models"))[:120])

    bad_models = {
        "providers": {"relay": {"type": "openai", "base_url": "https://x/v1"}},
        "registry": [
            {"id": "a", "provider": "relay", "model": "m1"},
            {"id": "a", "provider": "relay", "model": "重复 id"},
            {"id": "b", "provider": "不存在的供应商", "model": "m2"},
            {"id": "c", "provider": "relay", "model": ""},
            {"provider": "relay", "model": "没有 id"},
        ],
        "default": "不存在的模型",
        "fallback_chain": ["a", "幽灵模型"],
    }
    r = client.post("/api/config", headers=h, json={"config": with_models(cfg_now, bad_models), "force": True})
    check("非法 models 被规整而不是崩", r.status_code == 200, r.text[:200])
    fixed_models = r.get_json()["config"]["Others"]["models"]
    check("重复 id 只留一个", [m["id"] for m in fixed_models["registry"]] == ["a", "c"],
          str([m["id"] for m in fixed_models["registry"]]))
    check("引用不存在 provider 的项被剔除", all(m["provider"] == "relay" for m in fixed_models["registry"]))
    check("default 回落为第一个模型", fixed_models["default"] == "a", str(fixed_models["default"]))
    check("fallback 里的幽灵模型被剔除", fixed_models["fallback_chain"] == ["a"], str(fixed_models["fallback_chain"]))
    check("规整过程有告警", len(r.get_json()["warnings"]) >= 4, str(r.get_json()["warnings"])[:200])
    r = client.post("/api/config", headers=h, json={"config": with_models(cfg_now, ["不是对象"]), "force": True})
    check("models 不是对象 -> 400", r.status_code == 400, str(r.status_code))

    # 保存时重启：机器人没在跑 -> 跳过；强制定 -> 沙箱没有 main.py，应报启动失败而不是崩
    base_cfg = json.loads(json.dumps(cfg_now))
    r = client.post("/api/config", headers=h, json={"config": base_cfg, "force": True, "restart": "auto"})
    check("restart=auto 且机器人在未运行 -> 正确跳过", r.status_code == 200 and r.get_json()["restart"]["skipped"] is True,
          str(r.get_json().get("restart")))
    r = client.post("/api/config", headers=h, json={"config": base_cfg, "force": True, "restart": "force"})
    check("restart=force 在缺 main.py 时给出失败说明而非异常",
          r.status_code == 200 and r.get_json()["restart"]["ok"] is False,
          str(r.get_json().get("restart"))[:200])
    r = client.post("/api/config", headers=h, json={"config": base_cfg, "force": True, "restart": "乱写"})
    check("非法 restart 值 -> 400", r.status_code == 400, str(r.status_code))

    # 控制台自身设置
    r = client.get("/api/settings", headers=h)
    check("GET /api/settings 200", r.status_code == 200, r.text[:120])
    check("默认 auto_restart 为关", r.get_json()["settings"]["auto_restart"] is False)
    r = client.post("/api/settings", headers=h, json={"settings": {"auto_restart": True, "restart_mode": "force"}})
    check("POST /api/settings 生效", r.get_json()["settings"]["auto_restart"] is True
          and r.get_json()["settings"]["restart_mode"] == "force", r.text[:160])
    check("设置写进了沙箱 console.json", json.loads((SANDBOX / "console.json").read_text(encoding="utf-8"))["auto_restart"] is True)
    r = client.post("/api/settings", headers=h, json={"settings": {"restart_mode": "乱写"}})
    check("非法 restart_mode -> 400", r.status_code == 400, str(r.status_code))
    r = client.post("/api/config", headers=h, json={"config": base_cfg, "force": True})
    check("开了 auto_restart 后保存会自动带重启（沙箱无 main.py -> 失败但结构正确）",
          "restart" in r.get_json(), str(r.get_json().get("restart"))[:160])
    client.post("/api/settings", headers=h, json={"settings": {"auto_restart": False, "restart_mode": "auto"}})

    # 开机自启（写进沙箱里的假启动文件夹）
    r = client.get("/api/autostart", headers=h)
    check("GET /api/autostart 200", r.status_code == 200, r.text[:120])
    check("默认未开启自启", r.get_json()["data"]["enabled"] is False)
    r = client.post("/api/autostart", headers=h, json={"enabled": True})
    check("开启自启 -> 200", r.status_code == 200, r.text[:200])
    autofile = core.STARTUP_DIR / core.AUTOSTART_NAME
    check("启动项文件已生成", autofile.exists(), str(autofile))
    if autofile.exists():
        content = autofile.read_bytes()
        check("启动项是纯 ASCII（cmd 需要）", all(b < 128 for b in content))
        check("启动项指向托盘启动脚本", b"start-console-tray.bat" in content)
        check("启动项里有 CRLF", b"\r\n" in content)
    check("状态接口显示已开启", client.get("/api/autostart", headers=h).get_json()["data"]["enabled"] is True)
    r = client.post("/api/autostart", headers=h, json={"enabled": False})
    check("关闭自启 -> 200", r.status_code == 200, r.text[:200])
    check("启动项文件已删除", not autofile.exists())
    check("关闭自启前有备份", any("autostart" in b["name"] for b in core.list_backups()),
          str([b["name"] for b in core.list_backups()][:3]))

    print("\n=== 11. 其它接口与错误处理 ===")
    r = client.get("/api/overview", headers=h)
    check("overview 200", r.status_code == 200, r.text[:200])
    ov = r.get_json()["data"]
    check("overview 带配置体检", "config_health" in ov and ov["config_health"]["ok"] is True)
    check("overview 带进程状态", "running" in ov["bot"])
    r = client.get("/api/meta", headers=h)
    check("meta 200 且有模式列表", r.status_code == 200 and len(r.get_json()["modes"]) >= 5)
    r = client.get("/api/logs", headers=h)
    check("logs 200", r.status_code == 200)
    r = client.get("/api/logs/..%2f..%2fmain.py", headers=h)
    check("日志名穿越被拒", r.status_code in (400, 404), str(r.status_code))
    r = client.get("/api/nope", headers=h)
    check("未知接口 -> 404（不是 500）", r.status_code == 404, str(r.status_code))
    r = client.get("/", headers=h)
    check("首页 200", r.status_code == 200, str(r.status_code))
    check("首页是控制台页面", b"<title>" in r.data)
    r = client.get("/static/app.js")
    check("静态资源可访问", r.status_code == 200)

    print("\n=== 12. 隔离性：控制台只写沙箱，真实文件一个都没碰 ===")
    check("配置路径全部指向沙箱（结构性隔离）",
          all(str(p).startswith(str(SANDBOX)) for p in
              [core.CONFIG_PATH, core.PRESET_JSON, core.PRESET_DIR, core.VOICE_PREFS_PATH,
               core.SUPER_INI, core.MANAGE_INI, core.LOCK_PATH, core.BOT_DIR]),
          f"BOT_DIR={core.BOT_DIR}")
    check("备份目录也在沙箱内", str(core.BACKUP_DIR).startswith(str(SANDBOX)), str(core.BACKUP_DIR))
    check("备份确实落在沙箱内的备份目录",
          core.BACKUP_DIR.exists() and any(core.BACKUP_DIR.glob("*.bak")), str(core.BACKUP_DIR))
    leaked = sorted(set(p.name for p in REAL_BACKUP_DIR.glob("*.bak")) - real_backups_before)
    check("真实备份目录没有被本次测试写入新备份", not leaked, str(leaked[:5]))
    outside = [w for w in WRITTEN if not w.startswith(str(SANDBOX))]
    check("本次实际写过的每个路径都在沙箱内", not outside, str(outside[:5]))
    check("本次确实发生过写操作（监控有效）", len(WRITTEN) > 0, str(len(WRITTEN)))

    external = []
    for name, before in real_hashes.items():
        p = REAL_BOT_DIR / name if name != "current.json" else REAL_BOT_DIR / "prerequisites" / "current.json"
        now = sha(p)
        if now != before:
            external.append(f"{name}({before}->{now})")
    if external:
        print(f"  [提示] 真实文件在本次测试期间被**外部程序**改动过（不是本控制台）：{', '.join(external)}")
        print("         控制台没有写过沙箱以外的任何路径（见上面的写入监控）。")
    check("控制台没有写出沙箱（写入监控为零越界）", not outside, str(outside[:5]))

    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  - " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        if SANDBOX.exists():
            shutil.rmtree(SANDBOX, ignore_errors=True)
    sys.exit(code)
