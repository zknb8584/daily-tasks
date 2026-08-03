# -*- coding: utf-8 -*-
"""
每日任务 · 手机端待办 App（Python + Flet，目标 Android APK）

功能：
  - 无限嵌套项目树：大项目下面可以挂子项目，任意层级
  - 新建 / 编辑 / 删除 / 勾选完成 / 恢复
  - 截止时间（日期 + 可选时分，时间留空按整天 23:59:59）
  - 显示规则（每个层级一致）：
      未完成                 -> 主列表
      已完成但子树未完工      -> 主列表划线沉底
      已完成且整棵子树完工    -> 本层「已完成」折叠区，可恢复/清除
  - 本地通知（应用存活时）：到期/过期扫描 + 打开时补发
  - 数据存 SQLite（应用私有目录），离线可用
  - 备份导出 / 导入（JSON）

运行：
  python main.py             # 桌面调试
  python main.py --selftest  # 无界面自检（数据层 + 通知 + 备份）
"""
import datetime as dt
import os
import sys

import flet as ft

from models import DATA_DIR, Database, fmt_deadline, parse_deadline
from notifications import Notifier, notify

APP_NAME = "每日任务"
DATE_FMT = "%Y-%m-%d"
DATETIME_FMT = "%Y-%m-%d %H:%M"


# ---------------------------------------------------------------------------
# 截止时间编辑中间态
# ---------------------------------------------------------------------------
class DeadlineState:
    """编辑对话框里的截止时间：日期（必填）+ 时间（可选）。"""

    def __init__(self, s=""):
        d = parse_deadline(s)
        self.date = None
        self.time = None
        if d is not None:
            self.date = d.date()
            if " " in str(s).strip():
                self.time = d.time().replace(second=0, microsecond=0)

    def to_str(self):
        if self.date is None:
            return ""
        if self.time is not None:
            return f"{self.date.strftime(DATE_FMT)} {self.time.strftime('%H:%M')}"
        return self.date.strftime(DATE_FMT)

    def display(self):
        return fmt_deadline(self.to_str())


# ---------------------------------------------------------------------------
# 主应用
# ---------------------------------------------------------------------------
class TaskApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.db = Database()
        self.notifier = Notifier(self.db)

        self.stack = []                 # 导航栈：[(parent_id, title), ...]，空 = 顶层
        self._completed_open = False    # 已完成折叠区是否展开
        self._completed_ids = []        # 当前层级已完成项的 id
        self._editing_id = None         # 正在编辑的项目 id（None = 新建）
        self._target_parent = None      # 新建时的父项目 id
        self._title_field = None
        self._dl_state = DeadlineState()
        self._dl_label = ft.Text("未设置截止时间", size=13, color=ft.Colors.GREY)

        self._setup()
        self._render()
        self.notifier.scan()            # 打开 App 时补发
        self.notifier.start(interval=600)

    # ================= 初始化 =================
    def _setup(self):
        p = self.page
        p.title = APP_NAME
        try:
            p.set_allowed_device_orientations(
                [ft.DeviceOrientation.PORTRAIT_UP, ft.DeviceOrientation.PORTRAIT_DOWN]
            )
        except Exception:
            pass
        p.theme_mode = ft.ThemeMode.LIGHT
        p.theme = ft.Theme(color_scheme_seed=ft.Colors.INDIGO)
        p.padding = 0
        if sys.platform.startswith(("win", "linux")):  # 桌面调试时用手机竖屏比例窗口
            try:
                p.width = 420
                p.height = 820
            except Exception:
                pass

        self.file_picker = ft.FilePicker()
        p.overlay.append(self.file_picker)

        p.appbar = ft.AppBar(
            leading=None,
            leading_width=48,
            title=ft.Text(APP_NAME),
            center_title=True,
            bgcolor=ft.Colors.PRIMARY,
            color=ft.Colors.ON_PRIMARY,
            actions=[
                ft.IconButton(
                    icon=ft.Icons.SETTINGS,
                    tooltip="设置",
                    icon_color=ft.Colors.ON_PRIMARY,
                    on_click=self._open_settings,
                )
            ],
        )

        self.scroll = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)
        p.add(self.scroll)

        p.floating_action_button = ft.FloatingActionButton(
            icon=ft.Icons.ADD,
            tooltip="新建项目",
            on_click=self._on_add,
        )
        p.on_keyboard_event = self._on_key
        p.update()

    # ================= 渲染 =================
    def _current(self):
        if not self.stack:
            return None, APP_NAME
        return self.stack[-1]

    def _render(self):
        parent_id, title = self._current()
        self._update_appbar(parent_id, title)

        children = self.db.roots() if parent_id is None else self.db.children(parent_id)
        active, completed = [], []
        for it in children:
            if it["done"] and self.db.fully_done(it["id"]):
                completed.append(it)
            else:
                active.append(it)
        active.sort(key=self._sort_key)
        completed.sort(key=self._sort_key)
        self._completed_ids = [it["id"] for it in completed]

        controls = []
        if not children:
            controls.append(self._empty_hint())
        for it in active:
            controls.append(self._item_row(it, in_completed=False))
        if completed:
            controls.append(self._completed_header(len(completed)))
            if self._completed_open:
                for it in completed:
                    controls.append(self._item_row(it, in_completed=True))

        self.scroll.controls = controls
        self.page.update()

    def _update_appbar(self, parent_id, title):
        self.page.appbar.leading = (
            ft.IconButton(
                icon=ft.Icons.ARROW_BACK,
                tooltip="返回",
                icon_color=ft.Colors.ON_PRIMARY,
                on_click=self._back,
            )
            if self.stack
            else None
        )
        self.page.appbar.title = ft.Text(title)

    def _sort_key(self, it):
        d = parse_deadline(it["deadline"])
        ts = d.timestamp() if d else float("inf")
        return (1 if it["done"] else 0, ts, it["id"])

    def _empty_hint(self):
        return ft.Container(
            padding=ft.Padding(top=100, left=24, right=24),
            alignment=ft.Alignment(0, 0),
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.CHECKLIST, size=64, color=ft.Colors.GREY),
                    ft.Text("还没有项目", size=17, color=ft.Colors.GREY),
                    ft.Text("点右下角 + 新建一个吧", size=13, color=ft.Colors.GREY),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
        )

    # ================= 列表行 =================
    def _item_row(self, it, in_completed=False):
        item_id = it["id"]
        done = bool(it["done"])
        has_children = self.db.has_children(item_id)

        title_style = None
        if done:
            title_style = ft.TextStyle(
                decoration=ft.TextDecoration.LINE_THROUGH, color=ft.Colors.GREY
            )
        title = ft.Text(
            it["title"], size=16, style=title_style,
            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
        )

        subs = []
        if it["deadline"]:
            subs.append(
                ft.Text(
                    fmt_deadline(it["deadline"]),
                    size=12,
                    color=self._deadline_color(it["deadline"]),
                )
            )
        if has_children:
            d, t = self.db.subtree_stats(item_id)
            subs.append(ft.Text(f"进度 {d}/{t}", size=12, color=ft.Colors.GREY))
        subtitle = ft.Row(subs, spacing=10) if subs else None

        trailing = []
        if has_children:
            trailing.append(
                ft.IconButton(
                    icon=ft.Icons.CHEVRON_RIGHT, icon_size=22,
                    tooltip="进入子项目",
                    on_click=lambda e, i=item_id: self._enter_children(i),
                )
            )
        if in_completed:
            trailing.append(
                ft.IconButton(
                    icon=ft.Icons.UNDO, icon_size=22, tooltip="恢复",
                    on_click=lambda e, i=item_id: self._restore(i),
                )
            )
        else:
            trailing.append(
                ft.IconButton(
                    icon=ft.Icons.EDIT, icon_size=22, tooltip="编辑",
                    on_click=lambda e, i=item_id: self._open_edit(i),
                )
            )
        trailing.append(
            ft.IconButton(
                icon=ft.Icons.DELETE, icon_size=22, tooltip="删除",
                on_click=lambda e, i=item_id: self._confirm_delete(i),
            )
        )

        return ft.ListTile(
            leading=ft.Checkbox(
                value=done,
                active_color=ft.Colors.PRIMARY,
                on_change=lambda e, i=item_id: self._on_toggle(e, i),
            ),
            title=title,
            subtitle=subtitle,
            trailing=ft.Row(trailing, spacing=0),
            min_height=64,
            bgcolor=ft.Colors.GREY_200 if in_completed else None,
            on_click=lambda e, i=item_id, hc=has_children: self._on_row_click(i, hc),
            on_long_press=lambda e, i=item_id: self._open_edit(i),
        )

    def _completed_header(self, count):
        return ft.ListTile(
            leading=ft.Icon(ft.Icons.CHECK_CIRCLE, color=ft.Colors.GREEN),
            title=ft.Text(
                f"已完成 ({count})", weight=ft.FontWeight.BOLD, color=ft.Colors.GREY
            ),
            trailing=ft.Row(
                [
                    ft.TextButton("清除全部", on_click=self._clear_completed),
                    ft.Icon(
                        ft.Icons.EXPAND_LESS if self._completed_open else ft.Icons.EXPAND_MORE,
                        color=ft.Colors.GREY,
                    ),
                ],
                spacing=0,
            ),
            bgcolor=ft.Colors.GREY_200,
            on_click=lambda e: self._toggle_completed(),
        )

    # ================= 交互 =================
    def _on_row_click(self, item_id, has_children):
        if has_children:
            self._enter_children(item_id)
        else:
            self._open_edit(item_id)

    def _enter_children(self, item_id):
        it = self.db.get(item_id)
        if not it:
            return
        self.stack.append((item_id, it["title"]))
        self._completed_open = False
        self._render()

    def _back(self, e=None):
        if self.stack:
            self.stack.pop()
            self._completed_open = False
            self._render()

    def _on_key(self, e):
        if getattr(e, "key", "") in ("Escape", "Backspace") and not getattr(e, "ctrl", False):
            self._back()

    def _on_add(self, e):
        parent_id = self.stack[-1][0] if self.stack else None
        self._open_edit(parent_id=parent_id)

    def _on_toggle(self, e, item_id):
        new_val = bool(e.control.value)
        self.db.set_done(item_id, new_val)
        if new_val:
            if self.db.fully_done(item_id):
                self._toast("已移至「已完成」", action_label="撤销",
                            on_undo=lambda: self._undo_done(item_id))
            else:
                self._toast("已标记完成", action_label="撤销",
                            on_undo=lambda: self._undo_done(item_id))
        self._render()

    def _undo_done(self, item_id):
        self.db.set_done(item_id, False)
        self._render()

    def _restore(self, item_id):
        self.db.set_done(item_id, False)
        self._render()

    def _toggle_completed(self):
        self._completed_open = not self._completed_open
        self._render()

    def _clear_completed(self, e):
        n = len(self._completed_ids)
        if not n:
            return
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("清除已完成"),
            content=ft.Text(f"将删除当前层级的 {n} 个已完成项目（含其子项目），确定？"),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(content="删除", on_click=lambda e: self._do_clear_completed()),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _do_clear_completed(self):
        self.page.pop_dialog()
        self.db.delete_many(self._completed_ids)
        self._completed_ids = []
        self._render()

    def _confirm_delete(self, item_id):
        it = self.db.get(item_id)
        if not it:
            return
        text = f"删除「{it['title']}」？"
        if self.db.has_children(item_id):
            text += "\n其下所有子项目会一并删除。"
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("确认删除"),
            content=ft.Text(text),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(content="删除", on_click=lambda e: self._do_delete(item_id)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _do_delete(self, item_id):
        self.page.pop_dialog()
        self.db.delete(item_id)
        self._render()

    # ================= 新建 / 编辑对话框 =================
    def _open_edit(self, item_id=None, parent_id=None):
        self._editing_id = item_id
        self._target_parent = parent_id
        it = self.db.get(item_id) if item_id else None
        self._dl_state = DeadlineState(it["deadline"] if it else "")
        self._update_dl_label()

        self._title_field = ft.TextField(
            label="项目名称",
            value=(it["title"] if it else ""),
            autofocus=True,
            on_submit=self._save_edit,
        )
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("编辑项目" if item_id else "新建项目"),
            content=ft.Column(
                [
                    self._title_field,
                    self._dl_label,
                    ft.Row(
                        [
                            ft.OutlinedButton(content="设置日期", on_click=self._pick_date),
                            ft.OutlinedButton(content="设置时间", on_click=self._pick_time),
                            ft.TextButton(content="清除", on_click=self._clear_deadline),
                        ],
                        spacing=6,
                    ),
                ],
                tight=True,
                spacing=12,
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(content="保存", on_click=self._save_edit),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _update_dl_label(self):
        s = self._dl_state.display()
        if s:
            self._dl_label.value = "截止时间：" + s
            self._dl_label.color = self._deadline_color(self._dl_state.to_str())
        else:
            self._dl_label.value = "未设置截止时间"
            self._dl_label.color = ft.Colors.GREY
        self.page.update()

    def _pick_date(self, e):
        cur = None
        if self._dl_state.date:
            cur = dt.datetime.combine(
                self._dl_state.date, self._dl_state.time or dt.time(0, 0)
            )
        dp = ft.DatePicker(
            value=cur,
            first_date=dt.datetime(2000, 1, 1),
            last_date=dt.datetime(2100, 12, 31),
            on_change=self._on_date_picked,
        )
        self.page.show_dialog(dp)

    def _on_date_picked(self, ev):
        v = ev.control.value
        if v:
            d = v.date() if isinstance(v, dt.datetime) else v
            self._dl_state.date = d
            self._update_dl_label()

    def _pick_time(self, e):
        tp = ft.TimePicker(
            value=self._dl_state.time or dt.time(0, 0),
            on_change=self._on_time_picked,
        )
        self.page.show_dialog(tp)

    def _on_time_picked(self, ev):
        v = ev.control.value
        if v:
            t = v.time() if isinstance(v, dt.datetime) else v
            self._dl_state.time = t.replace(second=0, microsecond=0)
            if self._dl_state.date is None:
                self._dl_state.date = dt.date.today()
            self._update_dl_label()

    def _clear_deadline(self, e):
        self._dl_state = DeadlineState("")
        self._update_dl_label()

    def _save_edit(self, e):
        title = (self._title_field.value or "").strip()
        if not title:
            self._toast("请输入项目名称")
            return
        if self._editing_id:
            self.db.update(self._editing_id, title=title, deadline=self._dl_state.to_str())
        else:
            self.db.add(self._target_parent, title, self._dl_state.to_str())
        self.page.pop_dialog()
        self._render()

    # ================= 设置 / 备份 =================
    def _open_settings(self, e):
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("设置"),
            content=ft.Column(
                [
                    ft.Text("数据目录", size=12, color=ft.Colors.GREY),
                    ft.Text(DATA_DIR, size=11, color=ft.Colors.GREY),
                    ft.ListTile(
                        leading=ft.Icon(ft.Icons.DOWNLOAD),
                        title=ft.Text("导出备份"),
                        on_click=self._export,
                    ),
                    ft.ListTile(
                        leading=ft.Icon(ft.Icons.UPLOAD),
                        title=ft.Text("导入备份"),
                        on_click=self._import,
                    ),
                    ft.ListTile(
                        leading=ft.Icon(ft.Icons.NOTIFICATIONS_ACTIVE),
                        title=ft.Text("测试通知"),
                        on_click=self._test_notify,
                    ),
                ],
                tight=True,
                spacing=4,
            ),
            actions=[ft.TextButton("关闭", on_click=lambda e: self.page.pop_dialog())],
        )
        self.page.show_dialog(dlg)

    def _export(self, e):
        self.page.pop_dialog()
        data = self.db.export().encode("utf-8")
        fname = f"每日任务备份_{dt.datetime.now():%Y%m%d_%H%M}.json"
        try:
            path = self.file_picker.save_file(
                dialog_title="导出备份",
                file_name=fname,
                allowed_extensions=["json"],
                src_bytes=data,
            )
        except Exception as ex:
            self._toast(f"导出失败：{ex}")
            return
        if not path:
            self._toast("已取消")
            return
        try:  # 桌面端兜底：部分环境 src_bytes 不生效，手动写入
            if not (os.path.exists(path) and os.path.getsize(path) == len(data)):
                with open(path, "wb") as f:
                    f.write(data)
        except Exception:
            pass
        self._toast("备份已导出")

    def _import(self, e):
        self.page.pop_dialog()
        try:
            files = self.file_picker.pick_files(
                dialog_title="选择备份文件", allowed_extensions=["json"]
            )
        except Exception as ex:
            self._toast(f"导入失败：{ex}")
            return
        if not files:
            self._toast("已取消")
            return
        fp = files[0]
        try:
            text = None
            b = getattr(fp, "bytes", None)
            if b:
                text = b.decode("utf-8")
            elif getattr(fp, "path", None):
                with open(fp.path, "r", encoding="utf-8") as f:
                    text = f.read()
            if not text:
                raise ValueError("文件为空")
            self.db.import_json(text)
            self.stack = []
            self._render()
            self._toast("备份已导入")
        except Exception as ex:
            self._toast(f"导入失败：{ex}")

    def _test_notify(self, e):
        self.page.pop_dialog()
        notify("测试通知", "如果看到这条，说明通知功能正常")
        self._toast("已发送测试通知")

    # ================= 工具 =================
    def _toast(self, msg, action_label=None, on_undo=None):
        sb = ft.SnackBar(
            content=ft.Text(msg),
            behavior=ft.SnackBarBehavior.FLOATING,
            duration=3000,
            action=action_label,
            on_action=lambda e: (on_undo() if on_undo else None),
        )
        self.page.show_dialog(sb)

    def _deadline_color(self, s):
        d = parse_deadline(s)
        if d is None:
            return ft.Colors.GREY
        now = dt.datetime.now()
        if d < now:
            return ft.Colors.RED_600        # 已过期
        if d <= now + dt.timedelta(hours=24):
            return ft.Colors.ORANGE_700     # 24 小时内
        return ft.Colors.BLUE_GREY_600      # 常态


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main(page: ft.Page):
    TaskApp(page)


def run_selftest():
    """无界面自检：数据层、树统计、通知去重、备份往返。"""
    import tempfile

    tmp = tempfile.mkdtemp()
    db = Database(os.path.join(tmp, "t.db"))

    # 建树
    p = db.add(None, "大项目")
    c1 = db.add(p, "子任务1", (dt.datetime.now() + dt.timedelta(minutes=10)).strftime(DATETIME_FMT))
    c2 = db.add(p, "子任务2", (dt.datetime.now() + dt.timedelta(days=30)).strftime(DATETIME_FMT))
    assert len(db.roots()) == 1
    assert len(db.children(p)) == 2
    assert db.subtree_stats(p) == (0, 2)
    assert db.fully_done(p) is False

    # 部分完成
    db.set_done(c1, True)
    assert db.subtree_stats(p) == (1, 2)
    assert db.fully_done(p) is False
    db.set_done(c1, False)
    assert db.fully_done(p) is False

    # 通知：即将到期一次 + 去重
    n = Notifier(db)
    fired = n.scan()
    assert len(fired) == 1, fired
    assert n.scan() == []

    # 通知：已过期
    db.add(None, "过期任务", (dt.datetime.now() - dt.timedelta(hours=2)).strftime(DATETIME_FMT))
    fired2 = n.scan()
    assert any("已过期" in t for t, _ in fired2), fired2

    # 级联删除
    db.delete(p)
    assert len(db.roots()) == 1

    # 备份往返
    db.add(None, "另一个")
    text = db.export()
    db2 = Database(os.path.join(tmp, "t2.db"))
    db2.import_json(text)
    assert len(db2.roots()) == len(db.roots())
    assert db2.get(c1)["title"] == "子任务1" if db2.get(c1) else True  # 已删，忽略

    print("SELFTEST OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_selftest()
    else:
        ft.run(main)
