# -*- coding: utf-8 -*-
"""
本地通知模块。

方案 A（应用存活时检查）：
  - App 打开时扫描一次，之后后台线程每 N 分钟扫描一次；
  - 对「即将到期 / 已过期」的任务弹出系统通知（plyer 调 Android 原生通知）；
  - 通知去重集合持久化在 SQLite，同一个任务同一阶段只提醒一次。

App 被系统杀掉后不再触发 —— 这是方案 A 的已知边界，满足「简单本地通知」。
"""
import datetime as dt
import json
import threading
import time

from models import Database, fmt_deadline, parse_deadline

try:  # plyer 在桌面端/打包环境都可能缺失，缺失时降级为打印
    from plyer import notification as _plyer
    _HAS_PLYER = True
except Exception:  # pragma: no cover
    _HAS_PLYER = False

# 对外公开：当前构建是否真的能发系统通知（flet Android 运行时无 plyer 后端，恒为 False）
SYSTEM_NOTIFY_OK = _HAS_PLYER


def notify(title: str, message: str):
    if not _HAS_PLYER:
        print(f"[通知] {title} | {message}")
        return
    try:
        _plyer.notify(title=title, message=message, app_name="每日任务", timeout=5)
    except Exception as e:  # 通知失败不影响主流程
        print("notify error:", e)


class Notifier:
    """扫描到期任务并去重通知。"""

    def __init__(self, db: Database, remind_before_hours: float = 1.0):
        self.db = db
        self.remind_before = remind_before_hours
        self._stop = threading.Event()

    # ---------------- 去重状态 ----------------
    def _load_notified(self) -> set:
        raw = self.db.get_setting("notified", "[]")
        try:
            return set(json.loads(raw))
        except Exception:
            return set()

    def _save_notified(self, keys):
        self.db.set_setting("notified", json.dumps(sorted(keys)))

    # ---------------- 分类 ----------------
    def _category(self, deadline: dt.datetime, now: dt.datetime):
        if deadline < now:
            return "overdue"  # 已过期
        if deadline <= now + dt.timedelta(hours=self.remind_before):
            return "due"  # 即将到期
        return None

    # ---------------- 单次扫描 ----------------
    def scan(self):
        """扫描一次，返回本批发出的通知 [(标题, 内容)]。"""
        now = dt.datetime.now()
        keys = self._load_notified()
        fired = []

        for it in self.db.due_items():
            d = parse_deadline(it["deadline"])
            if d is None:
                continue
            cat = self._category(d, now)
            if not cat:
                continue
            key = f"{it['id']}:{cat}"
            if key in keys:
                continue
            if cat == "overdue":
                title = "任务已过期"
                message = f"{it['title']} · {fmt_deadline(it['deadline'])}"
            else:
                title = "任务即将到期"
                message = f"{it['title']} · {fmt_deadline(it['deadline'])}"
            notify(title, message)
            fired.append((title, message))
            keys.add(key)

        # 清理：只保留「仍然属于该阶段」的提醒记录；任务删除/改期/阶段变化后允许再次提醒
        pruned = set()
        alive = {it["id"]: it for it in self.db.due_items()}
        for k in keys:
            try:
                iid, cat = k.rsplit(":", 1)
                iid = int(iid)
            except ValueError:
                continue
            it = alive.get(iid)
            if it is None:
                continue
            d = parse_deadline(it["deadline"])
            if d is None:
                continue
            if self._category(d, dt.datetime.now()) == cat:
                pruned.add(k)
        self._save_notified(pruned)
        return fired

    # ---------------- 应用内提醒 ----------------
    def pending_alerts(self):
        """当前所有「即将到期/已过期」的任务（首页横幅用，不受去重影响）。"""
        now = dt.datetime.now()
        out = []
        for it in self.db.due_items():
            d = parse_deadline(it["deadline"])
            if d is None:
                continue
            cat = self._category(d, now)
            if cat:
                out.append({
                    "id": it["id"],
                    "title": it["title"],
                    "deadline": it["deadline"],
                    "category": cat,
                })
        return out

    # ---------------- 后台线程 ----------------
    def start(self, interval: int = 600):
        def loop():
            while not self._stop.is_set():
                try:
                    self.scan()
                except Exception as e:
                    print("scan error:", e)
                self._stop.wait(interval)

        t = threading.Thread(target=loop, daemon=True, name="notify-scan")
        t.start()

    def stop(self):
        self._stop.set()
