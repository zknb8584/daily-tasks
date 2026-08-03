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

    # ---------------- 增删改查 ----------------
    def add(self, parent_id, title, deadline="") -> int:
        created = dt.datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO items(parent_id,title,deadline,created_at) VALUES(?,?,?,?)",
                (parent_id, title, deadline, created),
            )
            return cur.lastrowid

    def update(self, item_id, title=None, deadline=None):
        sets, vals = [], []
        if title is not None:
            sets.append("title=?"); vals.append(title)
        if deadline is not None:
            sets.append("deadline=?"); vals.append(deadline)
        if not sets:
            return
        vals.append(item_id)
        with self._conn() as c:
            c.execute(f"UPDATE items SET {','.join(sets)} WHERE id=?", vals)

    def set_done(self, item_id, done: bool):
        with self._conn() as c:
            c.execute("UPDATE items SET done=? WHERE id=?", (1 if done else 0, item_id))

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

    def delete_many(self, ids):
        if not ids:
            return
        q = ",".join("?" * len(ids))
        with self._conn() as c:
            c.execute(f"DELETE FROM items WHERE id IN ({q})", ids)

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
        return json.dumps(
            {
                "app": "daily_tasks",
                "version": 1,
                "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
                "items": items,
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
            for it in items:
                c.execute(
                    "INSERT INTO items(id,parent_id,title,deadline,done,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        it["id"],
                        it.get("parent_id"),
                        it.get("title", ""),
                        it.get("deadline", ""),
                        1 if it.get("done") else 0,
                        it.get("created_at", ""),
                    ),
                )
