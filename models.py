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
_UNSET = object()

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
        # timeout=15：后台扫描线程与 UI 并发写时等待锁，而不是立刻抛 database is locked
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self):
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")  # WAL：读写互不阻塞，降低并发锁冲突
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
                    role_card_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS ai_role_cards(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    state TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS ai_user_cards(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS ai_role_relations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_card_id INTEGER NOT NULL DEFAULT 0,
                    role_card_id INTEGER NOT NULL,
                    relation TEXT DEFAULT '',
                    affection TEXT DEFAULT '',
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_card_id, role_card_id)
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS ai_character_relations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role_card_id_a INTEGER NOT NULL,
                    role_card_id_b INTEGER NOT NULL,
                    relation TEXT DEFAULT '',
                    affection INTEGER DEFAULT 50,
                    updated_at TEXT NOT NULL,
                    UNIQUE(role_card_id_a, role_card_id_b)
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS ai_drafts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    card_type TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS ai_worlds(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    content TEXT DEFAULT '',
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
            c.execute(
                """CREATE TABLE IF NOT EXISTS ai_group_chats(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS ai_group_members(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    role_card_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(group_id, role_card_id)
                )"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS ai_group_messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    role_name TEXT DEFAULT '',
                    role_card_id INTEGER,
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
            sess_cols = [r["name"] for r in c.execute("PRAGMA table_info(ai_sessions)")]
            if "role_card_id" not in sess_cols:
                c.execute("ALTER TABLE ai_sessions ADD COLUMN role_card_id INTEGER")
            if "current_scene" not in sess_cols:
                c.execute("ALTER TABLE ai_sessions ADD COLUMN current_scene TEXT DEFAULT ''")
            if "meta" not in sess_cols:
                c.execute("ALTER TABLE ai_sessions ADD COLUMN meta TEXT DEFAULT '{}'")
            if "user_card_id" not in sess_cols:
                c.execute("ALTER TABLE ai_sessions ADD COLUMN user_card_id INTEGER")
            card_cols = [r["name"] for r in c.execute("PRAGMA table_info(ai_role_cards)")]
            if "state" not in card_cols:
                c.execute("ALTER TABLE ai_role_cards ADD COLUMN state TEXT DEFAULT '{}'")
            if "world_id" not in card_cols:
                c.execute("ALTER TABLE ai_role_cards ADD COLUMN world_id INTEGER")
            if "autonomy" not in card_cols:
                c.execute("ALTER TABLE ai_role_cards ADD COLUMN autonomy INTEGER DEFAULT 50")
            if "behavior" not in card_cols:
                c.execute("ALTER TABLE ai_role_cards ADD COLUMN behavior TEXT DEFAULT '{}'")
            if "active_speech_frequency" not in card_cols:
                c.execute("ALTER TABLE ai_role_cards ADD COLUMN active_speech_frequency INTEGER DEFAULT 30")
            group_cols = [r["name"] for r in c.execute("PRAGMA table_info(ai_group_chats)")]
            if "current_scene" not in group_cols:
                c.execute("ALTER TABLE ai_group_chats ADD COLUMN current_scene TEXT DEFAULT ''")
            if "user_card_id" not in group_cols:
                c.execute("ALTER TABLE ai_group_chats ADD COLUMN user_card_id INTEGER")
            if "suggestion_mode" not in group_cols:
                c.execute("ALTER TABLE ai_group_chats ADD COLUMN suggestion_mode INTEGER DEFAULT 0")

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

    def all_items(self):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM items ORDER BY id").fetchall()
        return [dict(r) for r in rows]

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

    # ---------------- AI 世界观档案 ----------------
    def create_world(self, name: str, content: str = "") -> int:
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO ai_worlds(name,content,created_at,updated_at) "
                "VALUES(?,?,?,?)",
                (name.strip(), content.strip(), now, now),
            )
            return cur.lastrowid

    def list_worlds(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT w.*, "
                "(SELECT COUNT(*) FROM ai_role_cards r WHERE r.world_id=w.id) "
                "AS card_count "
                "FROM ai_worlds w ORDER BY w.updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_world(self, world_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM ai_worlds WHERE id=?", (world_id,)).fetchone()
        return dict(row) if row else None

    def update_world(self, world_id, name=None, content=None):
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            cur = c.execute("SELECT name, content FROM ai_worlds WHERE id=?", (world_id,))
            row = cur.fetchone()
            if not row:
                return
            new_name = (name if name is not None else row["name"]).strip()
            new_content = (content if content is not None else row["content"]).strip()
            c.execute(
                "UPDATE ai_worlds SET name=?, content=?, updated_at=? WHERE id=?",
                (new_name, new_content, now, world_id),
            )
        self._sync_world_to_role_cards(world_id)

    def _sync_world_to_role_cards(self, world_id):
        world = self.get_world(world_id)
        if not world:
            return
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, content FROM ai_role_cards WHERE world_id=?",
                (world_id,),
            ).fetchall()
            for row in rows:
                content = row["content"] or ""
                lines = []
                in_world = False
                replaced = False
                for line in content.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("[") and stripped.endswith("]"):
                        section = stripped[1:-1].strip()
                        in_world = section == "世界观"
                        if in_world:
                            replaced = True
                            continue
                    if in_world:
                        continue
                    lines.append(line)
                body = (world["name"] + "\n" + world["content"]).strip()
                new_content = "\n".join(lines).strip()
                if replaced or body:
                    new_content = new_content.rstrip()
                    new_content += f"\n\n[世界观]\n{body}"
                c.execute(
                    "UPDATE ai_role_cards SET content=? WHERE id=?",
                    (new_content.strip(), row["id"]),
                )

    def delete_world(self, world_id) -> bool:
        with self._conn() as c:
            bound = c.execute(
                "SELECT COUNT(*) n FROM ai_role_cards WHERE world_id=?", (world_id,)
            ).fetchone()["n"]
            if bound:
                return False
            c.execute("DELETE FROM ai_worlds WHERE id=?", (world_id,))
            return True

    def role_cards_by_world(self, world_id):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ai_role_cards WHERE world_id=? ORDER BY created_at DESC",
                (world_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def export_world(self, world_id) -> dict:
        world = self.get_world(world_id)
        if not world:
            return {}
        return {
            "app": "daily_tasks_world",
            "world": world,
            "role_cards": self.role_cards_by_world(world_id),
        }

    # ---------------- AI 会话 ----------------
    def create_ai_session(self, skill_id: str, title: str, project_id=None,
                          role_card_id=None, user_card_id=None) -> int:
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO ai_sessions("
                "skill_id,title,project_id,role_card_id,user_card_id,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (skill_id, title, project_id, role_card_id, user_card_id, now, now),
            )
            return cur.lastrowid

    def create_role_card(self, name: str, content: str, world_id=None) -> int:
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO ai_role_cards(name,content,world_id,created_at) "
                "VALUES(?,?,?,?)",
                (name.strip(), content.strip(), world_id, now),
            )
            return cur.lastrowid

    def create_user_card(self, name: str, content: str) -> int:
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO ai_user_cards(name,content,created_at,updated_at) "
                "VALUES(?,?,?,?)",
                (name.strip(), content.strip(), now, now),
            )
            return cur.lastrowid

    def list_user_cards(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ai_user_cards ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_user_card(self, card_id):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM ai_user_cards WHERE id=?", (card_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_user_card(self, card_id, name=None, content=None):
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            cur = c.execute("SELECT * FROM ai_user_cards WHERE id=?", (card_id,))
            row = cur.fetchone()
            if not row:
                return
            new_name = (name if name is not None else row["name"]).strip()
            new_content = (content if content is not None else row["content"]).strip()
            c.execute(
                "UPDATE ai_user_cards SET name=?, content=?, updated_at=? WHERE id=?",
                (new_name, new_content, now, card_id),
            )

    def delete_user_card(self, card_id):
        with self._conn() as c:
            c.execute("DELETE FROM ai_user_cards WHERE id=?", (card_id,))
            c.execute(
                "DELETE FROM ai_role_relations WHERE user_card_id=?",
                (card_id,),
            )
            c.execute(
                "UPDATE ai_sessions SET user_card_id=NULL WHERE user_card_id=?",
                (card_id,),
            )
            c.execute(
                "UPDATE ai_group_chats SET user_card_id=NULL WHERE user_card_id=?",
                (card_id,),
            )

    def get_role_relation(self, user_card_id, role_card_id):
        user_id = user_card_id or 0
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM ai_role_relations "
                "WHERE user_card_id=? AND role_card_id=?",
                (user_id, role_card_id),
            ).fetchone()
        return dict(row) if row else None

    def save_role_relation(self, user_card_id, role_card_id,
                           relation="", affection=""):
        user_id = user_card_id or 0
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            c.execute(
                "INSERT INTO ai_role_relations("
                "user_card_id,role_card_id,relation,affection,updated_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_card_id,role_card_id) DO UPDATE SET "
                "relation=excluded.relation, affection=excluded.affection, "
                "updated_at=excluded.updated_at",
                (user_id, role_card_id, relation or "", affection or "", now),
            )

    def update_role_card(self, card_id, name=None, content=None, world_id=_UNSET):
        with self._conn() as c:
            cur = c.execute("SELECT * FROM ai_role_cards WHERE id=?", (card_id,))
            row = cur.fetchone()
            if not row:
                return
            new_name = (name if name is not None else row["name"]).strip()
            new_content = (content if content is not None else row["content"]).strip()
            new_world = row["world_id"] if world_id is _UNSET else world_id
            c.execute(
                "UPDATE ai_role_cards SET name=?, content=?, world_id=? WHERE id=?",
                (new_name, new_content, new_world, card_id),
            )

    def set_role_autonomy(self, card_id, autonomy):
        value = max(0, min(100, int(autonomy or 50)))
        with self._conn() as c:
            c.execute(
                "UPDATE ai_role_cards SET autonomy=? WHERE id=?",
                (value, card_id),
            )

    def get_role_behavior(self, card_id) -> dict:
        card = self.get_role_card(card_id)
        if not card:
            return {}
        try:
            behavior = json.loads(card.get("behavior") or "{}")
            return behavior if isinstance(behavior, dict) else {}
        except (TypeError, ValueError):
            return {}

    def save_role_behavior(self, card_id, behavior: dict):
        with self._conn() as c:
            c.execute(
                "UPDATE ai_role_cards SET behavior=? WHERE id=?",
                (json.dumps(behavior, ensure_ascii=False), card_id),
            )

    def set_role_speech_frequency(self, card_id, frequency):
        value = max(0, min(100, int(frequency or 30)))
        with self._conn() as c:
            c.execute(
                "UPDATE ai_role_cards SET active_speech_frequency=? WHERE id=?",
                (value, card_id),
            )

    # ---------------- AI 生成草稿 ----------------
    def create_draft(self, name, content, card_type=""):
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO ai_drafts(name,content,card_type,created_at,updated_at) "
                "VALUES(?,?,?,?,?)",
                (name.strip(), content.strip(), card_type, now, now),
            )
            return cur.lastrowid

    def list_drafts(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ai_drafts ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_draft(self, draft_id):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM ai_drafts WHERE id=?", (draft_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete_draft(self, draft_id):
        with self._conn() as c:
            c.execute("DELETE FROM ai_drafts WHERE id=?", (draft_id,))

    def list_role_cards(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ai_role_cards ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_role_card(self, card_id):
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM ai_role_cards WHERE id=?", (card_id,)
            ).fetchone()
        return dict(row) if row else None

    def role_card_state(self, card_id) -> dict:
        card = self.get_role_card(card_id)
        if not card:
            return {}
        try:
            state = json.loads(card.get("state") or "{}")
            return state if isinstance(state, dict) else {}
        except (TypeError, ValueError):
            return {}

    def save_role_card_state(self, card_id, state: dict):
        with self._conn() as c:
            c.execute(
                "UPDATE ai_role_cards SET state=? WHERE id=?",
                (json.dumps(state, ensure_ascii=False), card_id),
            )

    def delete_role_card(self, card_id):
        with self._conn() as c:
            c.execute(
                "DELETE FROM ai_group_members WHERE role_card_id=?",
                (card_id,),
            )
            c.execute(
                "DELETE FROM ai_role_relations WHERE role_card_id=?",
                (card_id,),
            )
            c.execute(
                "DELETE FROM ai_character_relations "
                "WHERE role_card_id_a=? OR role_card_id_b=?",
                (card_id, card_id),
            )
            c.execute("DELETE FROM ai_role_cards WHERE id=?", (card_id,))

    def delete_role_relations_for_role(self, role_card_id):
        with self._conn() as c:
            c.execute(
                "DELETE FROM ai_role_relations WHERE role_card_id=?",
                (role_card_id,),
            )
            c.execute(
                "DELETE FROM ai_character_relations "
                "WHERE role_card_id_a=? OR role_card_id_b=?",
                (role_card_id, role_card_id),
            )

    def get_character_relation(self, role_a_id, role_b_id):
        a, b = sorted([role_a_id, role_b_id])
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM ai_character_relations "
                "WHERE role_card_id_a=? AND role_card_id_b=?",
                (a, b),
            ).fetchone()
        return dict(row) if row else None

    def save_character_relation(self, role_a_id, role_b_id,
                                relation="", affection=50):
        a, b = sorted([role_a_id, role_b_id])
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            c.execute(
                "INSERT INTO ai_character_relations("
                "role_card_id_a,role_card_id_b,relation,affection,updated_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(role_card_id_a,role_card_id_b) DO UPDATE SET "
                "relation=excluded.relation, affection=excluded.affection, "
                "updated_at=excluded.updated_at",
                (a, b, relation or "", int(affection or 50), now),
            )

    def delete_role_relation(self, user_card_id, role_card_id):
        user_id = user_card_id or 0
        with self._conn() as c:
            c.execute(
                "DELETE FROM ai_role_relations "
                "WHERE user_card_id=? AND role_card_id=?",
                (user_id, role_card_id),
            )

    def delete_character_relation(self, role_a_id, role_b_id):
        a, b = sorted([role_a_id, role_b_id])
        with self._conn() as c:
            c.execute(
                "DELETE FROM ai_character_relations "
                "WHERE role_card_id_a=? AND role_card_id_b=?",
                (a, b),
            )

    def list_role_relations(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT r.*, "
                "COALESCE(uc.name, '我') AS user_name, "
                "rc.name AS role_name "
                "FROM ai_role_relations r "
                "LEFT JOIN ai_user_cards uc ON uc.id=r.user_card_id "
                "LEFT JOIN ai_role_cards rc ON rc.id=r.role_card_id "
                "ORDER BY r.updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_character_relations(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT r.*, "
                "a.name AS role_a_name, "
                "b.name AS role_b_name "
                "FROM ai_character_relations r "
                "LEFT JOIN ai_role_cards a ON a.id=r.role_card_id_a "
                "LEFT JOIN ai_role_cards b ON b.id=r.role_card_id_b "
                "ORDER BY r.updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

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

    def update_ai_session_user_card(self, session_id, user_card_id):
        with self._conn() as c:
            c.execute(
                "UPDATE ai_sessions SET user_card_id=? WHERE id=?",
                (user_card_id, session_id),
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

    def get_ai_session_scene(self, session_id) -> str:
        with self._conn() as c:
            row = c.execute(
                "SELECT current_scene FROM ai_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
        return (row["current_scene"] if row else "") or ""

    def set_ai_session_scene(self, session_id, scene: str):
        with self._conn() as c:
            c.execute(
                "UPDATE ai_sessions SET current_scene=? WHERE id=?",
                (scene.strip(), session_id),
            )

    def get_ai_session_meta(self, session_id) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT meta FROM ai_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return {}
        try:
            meta = json.loads(row["meta"] or "{}")
            return meta if isinstance(meta, dict) else {}
        except (TypeError, ValueError):
            return {}

    def set_ai_session_meta(self, session_id, meta: dict):
        with self._conn() as c:
            c.execute(
                "UPDATE ai_sessions SET meta=? WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), session_id),
            )

    # ---------------- AI 群聊 ----------------
    def create_group_chat(self, title: str, user_card_id=None) -> int:
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO ai_group_chats("
                "title,user_card_id,created_at,updated_at) VALUES(?,?,?,?)",
                (title, user_card_id, now, now),
            )
            return cur.lastrowid

    def list_group_chats(self):
        with self._conn() as c:
            rows = c.execute(
                "SELECT g.id, g.title, g.updated_at, "
                "(SELECT COUNT(*) FROM ai_group_members m "
                "WHERE m.group_id=g.id) AS member_count "
                "FROM ai_group_chats g ORDER BY g.updated_at DESC LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_group_chat(self, group_id) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM ai_group_chats WHERE id=?", (group_id,)
            ).fetchone()
        return dict(row) if row else None

    def add_group_member(self, group_id, role_card_id):
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO ai_group_members("
                "group_id,role_card_id,created_at) VALUES(?,?,?)",
                (group_id, role_card_id, now),
            )

    def group_members(self, group_id):
        with self._conn() as c:
            rows = c.execute(
                "SELECT r.* FROM ai_role_cards r "
                "JOIN ai_group_members m ON m.role_card_id=r.id "
                "WHERE m.group_id=? ORDER BY m.id",
                (group_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_group_chat(self, group_id):
        with self._conn() as c:
            c.execute("DELETE FROM ai_group_messages WHERE group_id=?", (group_id,))
            c.execute("DELETE FROM ai_group_members WHERE group_id=?", (group_id,))
            c.execute("DELETE FROM ai_group_chats WHERE id=?", (group_id,))

    def append_group_message(self, group_id, role, content,
                             role_name="", role_card_id=None):
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            c.execute(
                "INSERT INTO ai_group_messages("
                "group_id,role,role_name,role_card_id,content,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (group_id, role, role_name, role_card_id, content, now),
            )
            c.execute(
                "UPDATE ai_group_chats SET updated_at=? WHERE id=?",
                (now, group_id),
            )

    def get_group_messages(self, group_id, limit=200):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ai_group_messages WHERE group_id=? "
                "ORDER BY id DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
        return list(reversed([dict(r) for r in rows]))

    def get_group_scene(self, group_id) -> str:
        with self._conn() as c:
            row = c.execute(
                "SELECT current_scene FROM ai_group_chats WHERE id=?",
                (group_id,),
            ).fetchone()
        return (row["current_scene"] if row else "") or ""

    def set_group_scene(self, group_id, scene: str):
        with self._conn() as c:
            c.execute(
                "UPDATE ai_group_chats SET current_scene=? WHERE id=?",
                (scene.strip(), group_id),
            )

    def get_group_suggestion_mode(self, group_id) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT suggestion_mode FROM ai_group_chats WHERE id=?",
                (group_id,),
            ).fetchone()
        return bool(row and row["suggestion_mode"])

    def set_group_suggestion_mode(self, group_id, enabled: bool):
        with self._conn() as c:
            c.execute(
                "UPDATE ai_group_chats SET suggestion_mode=? WHERE id=?",
                (1 if enabled else 0, group_id),
            )

    # ---------------- 子树快照 / 恢复（滑动删除撤销） ----------------
    def snapshot_subtree(self, item_id) -> dict:
        """导出整棵子树（含根 + 完成日志 + 原父 id），供删除后一键撤销重建。"""
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
            root = c.execute("SELECT parent_id FROM items WHERE id=?", (item_id,)).fetchone()
        return {
            "items": [dict(r) for r in rows],
            "completions": [dict(r) for r in comp],
            "root_parent_id": root["parent_id"] if root else None,
        }

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
                if parent is None and it.get("parent_id") is not None:
                    # 父节点不在快照里（例如只删了子项目）：若原父仍存在则挂回原处
                    exists = c.execute(
                        "SELECT 1 FROM items WHERE id=?", (it.get("parent_id"),)
                    ).fetchone()
                    if exists:
                        parent = it.get("parent_id")
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
                if it.get("parent_id") is None or new_root is None:
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

    def ancestors(self, item_id):
        """返回从根到父的 id 列表（不含自身）。"""
        ids = []
        cur = item_id
        with self._conn() as c:
            while True:
                row = c.execute(
                    "SELECT parent_id FROM items WHERE id=?", (cur,)
                ).fetchone()
                if not row or row["parent_id"] is None:
                    break
                ids.append(row["parent_id"])
                cur = row["parent_id"]
        return list(reversed(ids))

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
    # AI 内容表（备份 key → 表名）。群聊/会话/消息属于对话历史，不入备份。
    AI_TABLE_KEYS = {
        "role_cards": "ai_role_cards",
        "worlds": "ai_worlds",
        "user_cards": "ai_user_cards",
        "drafts": "ai_drafts",
        "role_relations": "ai_role_relations",
        "character_relations": "ai_character_relations",
    }

    def _replace_table(self, c, table, rows):
        """整体替换一张表：DELETE 后按行重建，保留原始 id，只插备份里有的列。"""
        c.execute(f"DELETE FROM {table}")
        if not rows:
            return
        cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
        for row in rows:
            keys = [k for k in row if k in cols]
            if not keys:
                continue
            sql = (
                f"INSERT INTO {table}({','.join(keys)}) "
                f"VALUES({','.join('?' * len(keys))})"
            )
            c.execute(sql, [row[k] for k in keys])

    def export(self) -> str:
        with self._conn() as c:
            items = [dict(r) for r in c.execute("SELECT * FROM items ORDER BY id")]
            tags = [dict(r) for r in c.execute("SELECT * FROM tags ORDER BY id")]
            ai = {}
            for key, table in self.AI_TABLE_KEYS.items():
                ai[key] = [
                    dict(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY id")
                ]
        return json.dumps(
            {
                "app": "daily_tasks",
                "version": 4,
                "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
                "items": items,
                "tags": tags,
                **ai,
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
            # AI 内容表：只替换备份里含有的表（角色卡 state/人设/草稿/关系随之恢复）。
            # 群聊/会话是对话历史，不入备份，保留不动。
            for key, table in self.AI_TABLE_KEYS.items():
                if key in data:
                    self._replace_table(c, table, data.get(key, []) or [])

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
