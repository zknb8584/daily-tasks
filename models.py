# -*- coding: utf-8 -*-
"""
数据层：SQLite 存储「无限嵌套」任务树。

每个项目一条记录，通过 parent_id 指向父项目（顶层项目 parent_id 为空）。
任意一层都能继续挂子项目、都能独立勾选完成。
"""
import datetime as dt
import json
import os
import sqlite3
from contextlib import contextmanager

# ---------------------------------------------------------------------------
# 存储路径
# ---------------------------------------------------------------------------
def _data_dir() -> str:
    """定位可写数据目录：
    1) FLET_APP_STORAGE_DATA —— Flet 移动端运行时自动注入的应用私有数据目录；
    2) ANDROID_ARGUMENT     —— 旧 buildozer/p4a 环境变量（兼容）；
    3) 项目下 data/         —— 桌面调试用。"""
    for var in ("FLET_APP_STORAGE_DATA", "ANDROID_ARGUMENT"):
        v = os.environ.get(var)
        if v:
            d = os.path.join(v, "files")
            try:
                os.makedirs(d, exist_ok=True)
                return d
            except OSError:
                continue
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(d, exist_ok=True)
    return d


DATA_DIR = _data_dir()
DB_PATH = os.path.join(DATA_DIR, "tasks.db")
QUOTES_PATH = os.path.join(DATA_DIR, "quotes.txt")

# 标签调色板大小（颜色索引取 0..N-1，循环分配）
TAG_PALETTE_SIZE = 12


# ---------------------------------------------------------------------------
# 每日一句（每行一句）
# ---------------------------------------------------------------------------
def get_quotes() -> list:
    """读取每日一句，按行切分、去空行。"""
    try:
        with open(QUOTES_PATH, encoding="utf-8") as f:
            return [ln.strip() for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return []


def save_quotes(text: str):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(QUOTES_PATH, "w", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------------------
# 截止时间：存储格式 "YYYY-MM-DD"（整天）或 "YYYY-MM-DD HH:MM"
# ---------------------------------------------------------------------------
def parse_deadline(s) -> dt.datetime | None:
    """把存储字符串解析成 datetime。
    只有日期的按「当天 23:59:59」处理（整天截止）。"""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(s, fmt)
            if fmt == "%Y-%m-%d":
                d = d.replace(hour=23, minute=59, second=59)
            return d
        except ValueError:
            continue
    return None


def next_deadline(s, repeat_type="", repeat_interval=1):
    """重复任务完成后的下一个截止时间（存储格式串）；无截止或无重复返回原值。"""
    d = parse_deadline(s)
    if d is None:
        return s
    if repeat_type == "daily":
        d = d + dt.timedelta(days=1)
    elif repeat_type == "weekly":
        d = d + dt.timedelta(days=7)
    elif repeat_type == "interval":
        d = d + dt.timedelta(days=max(1, int(repeat_interval or 1)))
    else:
        return s
    has_time = " " in str(s).strip()
    if has_time:
        return d.strftime("%Y-%m-%d %H:%M")
    return d.strftime("%Y-%m-%d")


def fmt_deadline(s) -> str:
    """把存储字符串显示成中文友好的文本。"""
    d = parse_deadline(s)
    if d is None:
        return ""
    now = dt.datetime.now()
    if d.year != now.year:
        year = f"{d.year}年"
    else:
        year = ""
    has_time = " " in str(s).strip()
    if has_time:
        return f"{year}{d.month}月{d.day}日 {d.hour:02d}:{d.minute:02d}"
    return f"{year}{d.month}月{d.day}日"


# ---------------------------------------------------------------------------
# 数据库访问
# ---------------------------------------------------------------------------
class Database:
    def __init__(self, path=DB_PATH):
        self.path = path
        self._init()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self):
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS items(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_id INTEGER,
                    title TEXT NOT NULL,
                    deadline TEXT DEFAULT '',
                    done INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )"""
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)"
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS tags(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    color INTEGER DEFAULT 0
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS completions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    done_at TEXT NOT NULL
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS ai_sessions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    project_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS ai_messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            # 迁移：给 items 补新列（老库升级用）
            cols = [r["name"] for r in c.execute("PRAGMA table_info(items)")]
            col_sql = {
                "tag_id": "INTEGER",
                "note": "TEXT DEFAULT ''",
                "repeat_type": "TEXT DEFAULT ''",
                "repeat_interval": "INTEGER DEFAULT 0",
            }
            for name, typ in col_sql.items():
                if name not in cols:
                    c.execute(f"ALTER TABLE items ADD COLUMN {name} {typ}")

    # ---------------- 增删改查 ----------------
    def add(self, parent_id, title, deadline="") -> int:
        created = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO items(parent_id,title,deadline,created_at) VALUES(?,?,?,?)",
                (parent_id, title, deadline, created),
            )
            return cur.lastrowid

    def update(self, item_id, **fields):
        """通用字段更新：title / deadline / note / repeat_type / repeat_interval。"""
        sets, vals = [], []
        for k, v in fields.items():
            if v is not None:
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            return
        vals.append(item_id)
        with self._conn() as c:
            c.execute(f"UPDATE items SET {','.join(sets)} WHERE id=?", vals)

    def set_done(self, item_id, done: bool):
        with self._conn() as c:
            c.execute("UPDATE items SET done=? WHERE id=?", (1 if done else 0, item_id))

    def set_subtree_done(self, item_id, done: bool):
        """整棵子树（含自身）统一标记完成/未完成。"""
        ids = self._subtree_ids(item_id)
        with self._conn() as c:
            c.executemany(
                "UPDATE items SET done=? WHERE id=?",
                [(1 if done else 0, i) for i in ids],
            )

    def subtree_earliest_deadline(self, item_id):
        """整组最早截止时间（自身+后代）；没有则 None。用于按 deadline 排序。"""
        best = None
        stack = [item_id]
        with self._conn() as c:
            while stack:
                pid = stack.pop()
                row = c.execute("SELECT deadline FROM items WHERE id=?", (pid,)).fetchone()
                if row:
                    d = parse_deadline(row["deadline"])
                    if d is not None and (best is None or d < best):
                        best = d
                for r in c.execute("SELECT id FROM items WHERE parent_id=?", (pid,)):
                    stack.append(r["id"])
        return best

    # ---------------- 标签 ----------------
    def get_or_create_tag(self, name) -> int | None:
        """按名取标签 id；不存在则创建并自动分配颜色（创建顺序取色）。"""
        name = (name or "").strip()
        if not name:
            return None
        with self._conn() as c:
            row = c.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
            if row:
                return row["id"]
            n = c.execute("SELECT COUNT(*) AS n FROM tags").fetchone()["n"]
            c.execute("INSERT INTO tags(name, color) VALUES(?,?)", (name, n % TAG_PALETTE_SIZE))
            return c.execute(
                "SELECT id FROM tags WHERE name=?", (name,)
            ).fetchone()["id"]

    def all_tags(self):
        """返回 [(name, color_index), ...]"""
        with self._conn() as c:
            rows = c.execute("SELECT name, color FROM tags ORDER BY id").fetchall()
        return [(r["name"], r["color"]) for r in rows]

    def tag_by_id(self, tag_id):
        if tag_id is None:
            return None
        with self._conn() as c:
            row = c.execute("SELECT name, color FROM tags WHERE id=?", (tag_id,)).fetchone()
        return (row["name"], row["color"]) if row else None

    def set_item_tag(self, item_id, tag_name):
        """给项目设置标签（空/None 清除）。"""
        tid = self.get_or_create_tag(tag_name) if (tag_name or "").strip() else None
        with self._conn() as c:
            c.execute("UPDATE items SET tag_id=? WHERE id=?", (tid, item_id))

    # ---------------- 搜索 ----------------
    def search_items(self, q: str):
        """按标题模糊搜索全部任务（含子任务）。"""
        q = (q or "").strip()
        if not q:
            return []
        like = f"%{q}%"
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM items WHERE title LIKE ? COLLATE NOCASE ORDER BY created_at",
                (like,),
            ).fetchall()
        return [dict(r) for r in rows]

    def title_path(self, item_id):
        """返回标题路径，如 毕业论文 › 方法论。"""
        parts = []
        cur = self.get(item_id)
        seen = set()
        while cur and cur["id"] not in seen:
            seen.add(cur["id"])
            parts.append(cur["title"])
            pid = cur.get("parent_id")
            cur = self.get(pid) if pid is not None else None
        return " › ".join(reversed(parts))

    # ---------------- 完成日志 / 统计 ----------------
    def log_completion(self, item_id):
        with self._conn() as c:
            c.execute(
                "INSERT INTO completions(item_id, done_at) VALUES(?,?)",
                (item_id, dt.datetime.now().isoformat(timespec="seconds")),
            )

    def log_completions(self, ids):
        if not ids:
            return
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            c.executemany(
                "INSERT INTO completions(item_id, done_at) VALUES(?,?)",
                [(i, now) for i in ids],
            )

    def stats_overview(self):
        """统计概览：总任务/已完成/进行中/本周完成/连续打卡天数。"""
        today = dt.date.today()
        week_ago = today - dt.timedelta(days=7)
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) n FROM items").fetchone()["n"]
            done = c.execute("SELECT COUNT(*) n FROM items WHERE done=1").fetchone()["n"]
            week_done = c.execute(
                "SELECT COUNT(*) n FROM completions WHERE done_at >= ?",
                (dt.datetime.combine(week_ago, dt.time.min).isoformat(timespec="seconds"),),
            ).fetchone()["n"]
            days = c.execute(
                "SELECT DISTINCT substr(done_at,1,10) d FROM completions ORDER BY d"
            ).fetchall()
        day_set = {r["d"] for r in days}
        # 连续打卡：从今天（或昨天，若今天还没打卡）往前数
        streak = 0
        cursor = today
        if cursor.isoformat() not in day_set:
            cursor = today - dt.timedelta(days=1)
        while cursor.isoformat() in day_set:
            streak += 1
            cursor -= dt.timedelta(days=1)
        return {
            "total": total,
            "done": done,
            "active": total - done,
            "week_done": week_done,
            "streak": streak,
        }

    def get(self, item_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            return dict(row) if row else None

    def get_many(self, ids):
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        with self._conn() as c:
            rows = c.execute(f"SELECT * FROM items WHERE id IN ({q})", ids).fetchall()
        return {r["id"]: dict(r) for r in rows}

    def delete(self, item_id):
        """级联删除整棵子树。"""
        ids = self._subtree_ids(item_id)
        with self._conn() as c:
            c.executemany("DELETE FROM items WHERE id=?", [(i,) for i in ids])
            q = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM completions WHERE item_id IN ({q})", ids)

    def delete_many(self, ids):
        if not ids:
            return
        q = ",".join("?" * len(ids))
        with self._conn() as c:
            c.execute(f"DELETE FROM items WHERE id IN ({q})", ids)
            c.execute(f"DELETE FROM completions WHERE item_id IN ({q})", ids)

    def children(self, parent_id):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM items WHERE parent_id IS ? ORDER BY created_at, id",
                (parent_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def roots(self):
        return self.children(None)

    def has_children(self, item_id) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM items WHERE parent_id=? LIMIT 1", (item_id,)
            ).fetchone()
        return row is not None

    # ---------------- 树统计 ----------------
    def _subtree_ids(self, item_id):
        ids = [item_id]
        stack = [item_id]
        with self._conn() as c:
            while stack:
                pid = stack.pop()
                for r in c.execute("SELECT id FROM items WHERE parent_id=?", (pid,)):
                    ids.append(r["id"])
                    stack.append(r["id"])
        return ids

    def subtree_stats(self, item_id):
        """后代统计：(完成节点数, 总节点数)，不含自身。用于进度显示。"""
        done = total = 0
        stack = [item_id]
        with self._conn() as c:
            while stack:
                pid = stack.pop()
                for r in c.execute("SELECT id, done FROM items WHERE parent_id=?", (pid,)):
                    total += 1
                    if r["done"]:
                        done += 1
                    stack.append(r["id"])
        return done, total

    def fully_done(self, item_id) -> bool:
        """是否「整棵子树完工」：自身 done 且所有后代 done。"""
        it = self.get(item_id)
        if not it or not it["done"]:
            return False
        stack = [item_id]
        with self._conn() as c:
            while stack:
                pid = stack.pop()
                for r in c.execute(
                    "SELECT id, done FROM items WHERE parent_id=?", (pid,)
                ):
                    if not r["done"]:
                        return False
                    stack.append(r["id"])
        return True

    # ---------------- 通知扫描 ----------------
    def due_items(self):
        """返回所有未完成且设了截止时间的项目。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM items WHERE deadline != '' AND done=0"
            ).fetchall()
        return [dict(r) for r in rows]

    def items_with_deadline(self):
        """返回所有设了截止时间的项目（含已完成），供日历按日分组。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM items WHERE deadline != '' ORDER BY deadline"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 设置键值 ----------------
    def get_setting(self, key, default=None):
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        with self._conn() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ---------------- AI 配置 ----------------
    AI_DEFAULTS = {
        "ai_base_url": "https://api.deepseek.com/v1",
        "ai_model": "deepseek-chat",
    }

    def get_ai_config(self) -> dict:
        cfg = dict(self.AI_DEFAULTS)
        for key in ("ai_base_url", "ai_model", "ai_api_key"):
            val = self.get_setting(key, "")
            if val:
                cfg[key] = val
        return cfg

    def set_ai_config(self, base_url=None, model=None, api_key=None):
        if base_url is not None:
            self.set_setting("ai_base_url", base_url.strip().rstrip("/"))
        if model is not None:
            self.set_setting("ai_model", model.strip())
        if api_key is not None:
            self.set_setting("ai_api_key", api_key.strip())

    def clear_ai_key(self):
        self.set_setting("ai_api_key", "")

    # ---------------- AI 会话 ----------------
    def create_ai_session(self, skill_id: str, title: str, project_id=None) -> int:
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO ai_sessions(skill_id,title,project_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (skill_id, title, project_id, now, now),
            )
            return cur.lastrowid

    def list_ai_sessions(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ai_sessions ORDER BY updated_at DESC LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_ai_session(self, session_id) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM ai_sessions WHERE id=?", (session_id,)
            ).fetchone()
        return dict(row) if row else None

    def touch_ai_session(self, session_id):
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            c.execute(
                "UPDATE ai_sessions SET updated_at=? WHERE id=?", (now, session_id)
            )

    def rename_ai_session(self, session_id, title):
        with self._conn() as c:
            c.execute(
                "UPDATE ai_sessions SET title=?, updated_at=? WHERE id=?",
                (title, dt.datetime.now().isoformat(timespec="seconds"), session_id),
            )

    def delete_ai_session(self, session_id):
        with self._conn() as c:
            c.execute("DELETE FROM ai_messages WHERE session_id=?", (session_id,))
            c.execute("DELETE FROM ai_sessions WHERE id=?", (session_id,))

    def append_ai_message(self, session_id, role: str, content: str):
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            c.execute(
                "INSERT INTO ai_messages(session_id,role,content,created_at) "
                "VALUES(?,?,?,?)",
                (session_id, role, content, now),
            )
            c.execute(
                "UPDATE ai_sessions SET updated_at=? WHERE id=?", (now, session_id)
            )

    def get_ai_messages(self, session_id, limit=200):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ai_messages WHERE session_id=? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return list(reversed([dict(r) for r in rows]))

    # ---------------- 子树快照 / 恢复（滑动删除撤销） ----------------
    def snapshot_subtree(self, item_id) -> dict:
        """导出整棵子树（含根 + 完成日志），供 Dismissible 删除后一键撤销重建。"""
        ids = self._subtree_ids(item_id)
        q = ",".join("?" * len(ids))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM items WHERE id IN ({q}) ORDER BY id", ids
            ).fetchall()
            comp = c.execute(
                f"SELECT item_id, done_at FROM completions WHERE item_id IN ({q}) ORDER BY id",
                ids,
            ).fetchall()
        return {"items": [dict(r) for r in rows], "completions": [dict(r) for r in comp]}

    def restore_subtree(self, snapshot: dict) -> int:
        """恢复快照中的整棵子树（自动重映射 id），返回新根 id。"""
        old_to_new = {}
        new_root = None
        with self._conn() as c:
            for it in snapshot.get("items", []):
                title = str(it.get("title", "")).strip()
                if not title:
                    continue
                parent = old_to_new.get(it.get("parent_id"))
                cur = c.execute(
                    "INSERT INTO items(parent_id,title,deadline,done,created_at,"
                    "tag_id,note,repeat_type,repeat_interval) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        parent,
                        title,
                        it.get("deadline", ""),
                        1 if it.get("done") else 0,
                        it.get("created_at", ""),
                        it.get("tag_id"),
                        it.get("note", ""),
                        it.get("repeat_type", ""),
                        it.get("repeat_interval", 0),
                    ),
                )
                old_to_new[it["id"]] = cur.lastrowid
                if it.get("parent_id") is None:
                    new_root = cur.lastrowid
            # 完成日志重映射到新 id，撤销后统计不丢历史
            for comp in snapshot.get("completions", []):
                new_item = old_to_new.get(comp.get("item_id"))
                if new_item:
                    c.execute(
                        "INSERT INTO completions(item_id, done_at) VALUES(?,?)",
                        (new_item, comp.get("done_at", "")),
                    )
        return new_root

    # ---------------- 自定义背景图 ----------------
    def set_bg_image(self, src_path) -> str:
        """把图片复制进数据目录，返回最终路径。"""
        import shutil

        ext = os.path.splitext(str(src_path))[1].lower() or ".png"
        dest = os.path.join(DATA_DIR, "bg_image" + ext)
        old = self.get_setting("bg_image", "")
        if old and os.path.abspath(old) != os.path.abspath(dest):
            try:
                os.remove(old)
            except OSError:
                pass
        try:
            shutil.copyfile(src_path, dest)
        except OSError as e:
            raise ValueError(f"无法保存背景图：{e}")
        self.set_setting("bg_image", dest)
        return dest

    def get_bg_image(self) -> str | None:
        p = self.get_setting("bg_image", "")
        if p and os.path.exists(p):
            return p
        return None

    def clear_bg_image(self):
        p = self.get_setting("bg_image", "")
        if p:
            try:
                os.remove(p)
            except OSError:
                pass
        self.set_setting("bg_image", "")

    # ---------------- 备份 ----------------
    def export(self) -> str:
        with self._conn() as c:
            items = [dict(r) for r in c.execute("SELECT * FROM items ORDER BY id")]
            tags = [dict(r) for r in c.execute("SELECT * FROM tags ORDER BY id")]
        return json.dumps(
            {
                "app": "daily_tasks",
                "version": 2,
                "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
                "items": items,
                "tags": tags,
            },
            ensure_ascii=False,
            indent=2,
        )

    def import_json(self, text: str):
        data = json.loads(text)
        if data.get("app") != "daily_tasks":
            raise ValueError("不是本应用导出的备份文件")
        items = data.get("items", [])
        with self._conn() as c:
            c.execute("DELETE FROM items")
            c.execute("DELETE FROM tags")
            for t in data.get("tags", []):
                c.execute(
                    "INSERT INTO tags(id,name,color) VALUES(?,?,?)",
                    (t["id"], t.get("name", ""), t.get("color", 0)),
                )
            for it in items:
                c.execute(
                    "INSERT INTO items(id,parent_id,title,deadline,done,created_at,"
                    "tag_id,note,repeat_type,repeat_interval) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        it["id"],
                        it.get("parent_id"),
                        it.get("title", ""),
                        it.get("deadline", ""),
                        1 if it.get("done") else 0,
                        it.get("created_at", ""),
                        it.get("tag_id"),
                        it.get("note", ""),
                        it.get("repeat_type", ""),
                        it.get("repeat_interval", 0),
                    ),
                )

    def import_plan(self, text: str) -> int:
        """从外部「导入计划」：把文件里的任务树【追加】进来，不覆盖现有数据。

        文件里的根项目变成新的顶层项目，子项目挂回对应父项目下；
        所有 id 重新分配，与现有数据无冲突。返回导入的条目数。
        """
        data = json.loads(text)
        if data.get("app") != "daily_tasks":
            raise ValueError("不是本应用的备份/计划文件")
        # 先按名字重建标签（旧 tag_id → 新 tag_id）
        tag_map = {}
        for t in data.get("tags", []):
            name = str(t.get("name", "")).strip()
            if name:
                tag_map[t["id"]] = self.get_or_create_tag(name)
        id_map = {}
        count = 0
        with self._conn() as c:
            for it in data.get("items", []):
                title = str(it.get("title", "")).strip()
                if not title:
                    continue
                tag_id = tag_map.get(it.get("tag_id")) if it.get("tag_id") is not None else None
                new_id = c.execute(
                    "INSERT INTO items(parent_id,title,deadline,done,created_at,tag_id,"
                    "note,repeat_type,repeat_interval) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        id_map.get(it.get("parent_id")),
                        title,
                        it.get("deadline", ""),
                        1 if it.get("done") else 0,
                        it.get("created_at")
                        or dt.datetime.now().isoformat(timespec="seconds"),
                        tag_id,
                        it.get("note", ""),
                        it.get("repeat_type", ""),
                        it.get("repeat_interval", 0),
                    ),
                ).lastrowid
                id_map[it["id"]] = new_id
                count += 1
        return count
