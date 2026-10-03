"""用户档案库：SQLite(WAL) 存储用户身份、行为统计、偏好与好感度。

为什么必须换掉 JSON（这是需求④「支持 100 人同时访问」的地基）：
  `voice_prefs.json` / `consent_asked.json` 原来是「整文件读 → 改 → 整文件写」且**无锁**。
  Hyper 每条消息一个线程，100 人并发下必然出现：
    1) 后写覆盖先写 —— 用户设置莫名回退；
    2) 读到写了一半的截断 JSON —— `except` 静默返回 {}，设置"自己变回默认"。
  这是已经存在的数据损坏风险，不是假设。

设计：
  - `data/fox.db`，WAL 模式（并发读 + 单写不互相阻塞）。
  - 连接按线程持有（`threading.local`）—— sqlite3 连接不可跨线程复用，而 Hyper 是一消息一线程。
  - 计数一律用原子 SQL（`SET x = x + 1`），不做「读出来加一再写回」。
  - 任何数据库异常都被吞掉并打印，绝不让档案库故障拖垮聊天。
  - 首次启动自动把 voice_prefs.json / consent_asked.json 迁进来，原文件留档。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

_BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_BOT_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "fox.db")

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False

RELATIONS = ((151, "挚友"), (61, "熟人"), (21, "眼熟"), (0, "陌生"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  qq             TEXT PRIMARY KEY,
  nickname       TEXT,
  card           TEXT,
  gender         TEXT,
  age            INTEGER,
  level          INTEGER,
  first_seen     INTEGER NOT NULL,
  last_seen      INTEGER NOT NULL,
  msg_count      INTEGER NOT NULL DEFAULT 0,
  voice_count    INTEGER NOT NULL DEFAULT 0,
  ai_count       INTEGER NOT NULL DEFAULT 0,
  char_count     INTEGER NOT NULL DEFAULT 0,
  active_days    INTEGER NOT NULL DEFAULT 0,
  streak_days    INTEGER NOT NULL DEFAULT 0,
  last_active_day TEXT,
  today_count    INTEGER NOT NULL DEFAULT 0,
  affinity       INTEGER NOT NULL DEFAULT 0,
  affinity_today INTEGER NOT NULL DEFAULT 0,
  affinity_date  TEXT,
  relation       TEXT NOT NULL DEFAULT '陌生',
  nickname_pref  TEXT,
  tags           TEXT NOT NULL DEFAULT '[]',
  note           TEXT,
  revoked        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS prefs (
  qq      TEXT PRIMARY KEY,
  voice   TEXT,
  model   TEXT,
  preset  TEXT,
  extra   TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS group_prefs (
  group_id TEXT PRIMARY KEY,
  voice    TEXT,
  model    TEXT,
  extra    TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS interactions (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  qq       TEXT NOT NULL,
  ts       INTEGER NOT NULL,
  kind     TEXT,
  group_id TEXT,
  summary  TEXT
);
CREATE INDEX IF NOT EXISTS idx_inter_qq_ts ON interactions(qq, ts DESC);

-- 通用键值：承接原有的 voice_prefs / consent 语义，避免大改调用点
CREATE TABLE IF NOT EXISTS kv (
  scope TEXT NOT NULL,
  key   TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (scope, key)
);
"""


def _conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=5000")
        _local.conn = c
    return c


def init():
    """建表 + 一次性迁移。可重复调用。"""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        try:
            con = _conn()
            con.executescript(_SCHEMA)
            _migrate_json(con)
            _initialized = True
            print(f"[用户库] 就绪：{DB_PATH}")
        except Exception as e:
            print(f"[用户库] 初始化失败（档案功能将降级为不可用）：{type(e).__name__}: {e}")


def _migrate_json(con):
    """把旧的 voice_prefs.json / consent_asked.json 导入 kv 表（只做一次）。"""
    cur = con.execute("SELECT COUNT(*) AS n FROM kv")
    if cur.fetchone()["n"] > 0:
        return
    vp = os.path.join(_BOT_DIR, "voice_prefs.json")
    n_vp = n_consent = 0
    if os.path.isfile(vp):
        try:
            d = json.load(open(vp, encoding="utf-8"))
            for field, key in (("on", "voice_on"), ("seen", "voice_seen"), ("voice", "voice_sel")):
                for scope, val in (d.get(field) or {}).items():
                    con.execute("INSERT OR REPLACE INTO kv(scope,key,value) VALUES(?,?,?)",
                                (scope, key, json.dumps(val, ensure_ascii=False)))
                    n_vp += 1
        except Exception as e:
            print(f"[用户库] 迁移 voice_prefs.json 失败：{e}")
    cc = os.path.join(_BOT_DIR, "consent_asked.json")
    if os.path.isfile(cc):
        try:
            for qq in json.load(open(cc, encoding="utf-8")):
                con.execute("INSERT OR REPLACE INTO kv(scope,key,value) VALUES(?,?,?)",
                            (f"u{qq}", "consent", "1"))
                n_consent += 1
        except Exception as e:
            print(f"[用户库] 迁移 consent_asked.json 失败：{e}")
    if n_vp or n_consent:
        print(f"[用户库] 已迁移 voice_prefs({n_vp} 项) / consent({n_consent} 人)")


# --------------------------------------------------------------------------
# 通用键值（承接 voice_prefs / consent）
# --------------------------------------------------------------------------
def kv_get(scope: str, key: str, default=None):
    try:
        init()
        row = _conn().execute("SELECT value FROM kv WHERE scope=? AND key=?",
                              (str(scope), str(key))).fetchone()
        if row is None:
            return default
        v = row["value"]
        try:
            return json.loads(v)
        except Exception:
            return v
    except Exception as e:
        print(f"[用户库] kv_get 失败：{e}")
        return default


def kv_set(scope: str, key: str, value):
    try:
        init()
        _conn().execute("INSERT OR REPLACE INTO kv(scope,key,value) VALUES(?,?,?)",
                        (str(scope), str(key), json.dumps(value, ensure_ascii=False)))
        return True
    except Exception as e:
        print(f"[用户库] kv_set 失败：{e}")
        return False


def kv_all(scope: str) -> dict:
    try:
        init()
        rows = _conn().execute("SELECT key,value FROM kv WHERE scope=?", (str(scope),)).fetchall()
        out = {}
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except Exception:
                out[r["key"]] = r["value"]
        return out
    except Exception as e:
        print(f"[用户库] kv_all 失败: {e}")
        return {}


# --------------------------------------------------------------------------
# 用户档案
# --------------------------------------------------------------------------
def _today() -> str:
    return time.strftime("%Y-%m-%d")


def relation_of(affinity: int) -> str:
    for threshold, name in RELATIONS:
        if affinity >= threshold:
            return name
    return "陌生"


def touch(qq, nickname: str | None = None, group_id=None, kind: str = "text",
          char_count: int = 0, is_ai: bool = False):
    """记录一次互动（原子更新），返回更新后的档案 dict。"""
    try:
        init()
        con = _conn()
        now = int(time.time())
        day = _today()
        q = str(qq)

        con.execute("BEGIN IMMEDIATE")
        try:
            row = con.execute("SELECT * FROM users WHERE qq=?", (q,)).fetchone()
            if row is None:
                con.execute(
                    "INSERT INTO users(qq,nickname,first_seen,last_seen,msg_count,char_count,"
                    "active_days,streak_days,last_active_day,today_count) "
                    "VALUES(?,?,?,?,1,?,1,1,?,1)",
                    (q, nickname, now, now, int(char_count), day))
            else:
                # 今天是否已互动过：决定 today_count / streak
                same_day = (row["last_active_day"] == day)
                streak = row["streak_days"]
                active = row["active_days"]
                if not same_day:
                    active += 1
                    # 连续天数：判断上次互动是不是"昨天"
                    try:
                        last = time.strptime(row["last_active_day"] or "", "%Y-%m-%d")
                        prev = time.localtime(now - 86400)
                        streak = streak + 1 if (last.tm_yday == prev.tm_yday and last.tm_year == prev.tm_year) else 1
                    except Exception:
                        streak = 1
                con.execute(
                    "UPDATE users SET last_seen=?, msg_count=msg_count+1, char_count=char_count+?, "
                    "voice_count=voice_count+?, ai_count=ai_count+?, today_count=?, "
                    "active_days=?, streak_days=?, last_active_day=?, "
                    "nickname=COALESCE(?, nickname) WHERE qq=?",
                    (now, int(char_count),
                     1 if kind == "voice" else 0,
                     1 if is_ai else 0,
                     (row["today_count"] + 1) if same_day else 1,
                     active, streak, day, nickname, q))
            con.execute("INSERT INTO interactions(qq,ts,kind,group_id,summary) VALUES(?,?,?,?,?)",
                        (q, now, kind, str(group_id) if group_id is not None else None, None))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return get(q) or {}
    except Exception as e:
        print(f"[用户库] touch 失败：{type(e).__name__}: {e}")
        return {}


def get(qq):
    try:
        init()
        row = _conn().execute("SELECT * FROM users WHERE qq=?", (str(qq),)).fetchone()
        return dict(row) if row else None
    except Exception as e:
        print(f"[用户库] get 失败：{e}")
        return None


def list_users(limit: int = 30, order: str = "last_seen"):
    try:
        init()
        col = {"last_seen": "last_seen", "affinity": "affinity", "msg_count": "msg_count"}.get(order, "last_seen")
        rows = _conn().execute(
            f"SELECT * FROM users WHERE revoked=0 ORDER BY {col} DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[用户库] list_users 失败：{e}")
        return []


def forget(qq) -> bool:
    """「忘记我」：物理删除该用户全部数据。"""
    try:
        init()
        q = str(qq)
        con = _conn()
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("DELETE FROM prefs WHERE qq=?", (q,))
            con.execute("DELETE FROM interactions WHERE qq=?", (q,))
            con.execute("DELETE FROM users WHERE qq=?", (q,))
            con.execute("DELETE FROM kv WHERE scope=?", (f"u{q}",))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return True
    except Exception as e:
        print(f"[用户库] forget 失败：{e}")
        return False


# --------------------------------------------------------------------------
# 好感度
# --------------------------------------------------------------------------
def add_affinity(qq, delta: int, daily_cap: int = 30) -> int:
    """加好感度（正数受每日上限约束）。返回新的好感度。"""
    try:
        init()
        con = _conn()
        q = str(qq)
        day = _today()
        con.execute("BEGIN IMMEDIATE")
        try:
            row = con.execute("SELECT affinity, affinity_today, affinity_date FROM users WHERE qq=?",
                              (q,)).fetchone()
            if row is None:
                con.execute("COMMIT")
                return 0
            today = 0 if row["affinity_date"] != day else (row["affinity_today"] or 0)
            if delta > 0:
                delta = max(0, min(delta, daily_cap - today))
            new_today = today + max(0, delta)
            new_aff = (row["affinity"] or 0) + delta
            con.execute("UPDATE users SET affinity=?, affinity_today=?, affinity_date=?, relation=? WHERE qq=?",
                        (new_aff, new_today, day, relation_of(new_aff), q))
            con.execute("COMMIT")
            return new_aff
        except Exception:
            con.execute("ROLLBACK")
            raise
    except Exception as e:
        print(f"[用户库] add_affinity 失败：{e}")
        return 0


# --------------------------------------------------------------------------
# 偏好（每用户 / 每群）
# --------------------------------------------------------------------------
def pref_get(qq, key: str, default=None):
    return kv_get(f"u{qq}", f"pref_{key}", default)


def pref_set(qq, key: str, value) -> bool:
    return kv_set(f"u{qq}", f"pref_{key}", value)


def group_pref_get(group_id, key: str, default=None):
    return kv_get(f"g{group_id}", f"pref_{key}", default)


def group_pref_set(group_id, key: str, value) -> bool:
    return kv_set(f"g{group_id}", f"pref_{key}", value)


# --------------------------------------------------------------------------
# 画像注入（需求④的核心：让机器人"认得"这个人）
# --------------------------------------------------------------------------
def render_for_prompt(qq, fallback_name: str = "") -> str:
    """生成 2~3 行的极简画像，拼进 system prompt。

    Token 成本必须可控 —— 只放摘要，不放流水。
    """
    try:
        p = get(qq)
    except Exception:
        p = None
    if not p:
        return ""
    try:
        name = p.get("nickname_pref") or p.get("card") or p.get("nickname") or fallback_name or str(qq)
        aff = p.get("affinity") or 0
        rel = p.get("relation") or relation_of(aff)
        today = p.get("today_count") or 0
        streak = p.get("streak_days") or 0

        line1 = f"【当前对话者】{name}（QQ {qq}）｜关系：{rel}（好感度 {aff}）"
        if today > 1:
            line1 += f"｜今天第 {today} 次找我"
        if streak > 1:
            line1 += f"｜已连续 {streak} 天"

        bits = []
        if p.get("nickname_pref"):
            bits.append(f'希望被叫「{p["nickname_pref"]}」')
        v = pref_get(qq, "voice")
        if v:
            bits.append(f"常用音色 {v}")
        m = pref_get(qq, "model")
        if m:
            bits.append(f"偏好模型 {m}")
        line2 = ("【偏好】" + "｜".join(bits)) if bits else ""

        note = (p.get("note") or "").strip()
        line3 = f"【备注】{note}" if note else ""

        return "\n".join(x for x in (line1, line2, line3) if x)
    except Exception as e:
        print(f"[用户库] render_for_prompt 失败：{e}")
        return ""


def stats() -> dict:
    try:
        init()
        con = _conn()
        n_users = con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        n_inter = con.execute("SELECT COUNT(*) AS n FROM interactions").fetchone()["n"]
        n_kv = con.execute("SELECT COUNT(*) AS n FROM kv").fetchone()["n"]
        return {"users": n_users, "interactions": n_inter, "kv": n_kv, "db": DB_PATH}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def prune_interactions(keep_days: int = 90) -> int:
    """裁剪过期流水（隐私 + 体积控制）。"""
    try:
        init()
        cutoff = int(time.time()) - keep_days * 86400
        cur = _conn().execute("DELETE FROM interactions WHERE ts < ?", (cutoff,))
        return cur.rowcount or 0
    except Exception as e:
        print(f"[用户库] prune 失败：{e}")
        return 0
