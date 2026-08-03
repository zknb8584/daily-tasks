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
  - 每日一句：用户自写句子，打开 App 随机显示一条（点击换一句）
  - 自定义背景图：从相册选一张作为背景（可选 Pillow 缩放，无则原图）
  - 数据存 SQLite（应用私有目录），离线可用
  - 备份导出 / 导入（JSON）

运行：
  python main.py             # 桌面调试
  python main.py --selftest  # 无界面自检（数据层 + 通知 + 备份）
"""
import base64
import datetime as dt
import io
import os
import random
import sys

import flet as ft

from models import DATA_DIR, Database, fmt_deadline, get_quotes, parse_deadline, save_quotes
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
# 图片 → base64 data URI（背景图用，跨桌面/安卓最稳）
# ---------------------------------------------------------------------------
def _image_to_data_uri(path) -> str | None:
    """读图并转成 data URI。本机有 Pillow 则缩到最长边 1280px 再编码，
    否则直接用原图 base64（稍重但功能可用）。失败返回 None。"""
    data = None
    mime = "image/jpeg"
    try:
        from PIL import Image as PILImage, ImageOps  # Pillow 可选

        im = PILImage.open(path)
        im = ImageOps.exif_transpose(im)
        im.thumbnail((1280, 1280))
        buf = io.BytesIO()
        if im.mode in ("RGBA", "LA", "P"):
            im.convert("RGBA").save(buf, format="PNG")
            mime = "image/png"
        else:
            im.convert("RGB").save(buf, format="JPEG", quality=82)
        data = buf.getvalue()
    except Exception:
        try:
            with open(path, "rb") as f:
                data = f.read()
            head = data[:12]
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                mime = "image/png"
            elif head[:3] == b"\xff\xd8\xff":
                mime = "image/jpeg"
            elif head[:6] in (b"GIF87a", b"GIF89a"):
                mime = "image/gif"
            else:
                mime = "image/webp"
        except OSError:
            return None
    if not data:
        return None
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


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
        self._quote = None              # 每日一句（随机选的）

        self._setup()
        self._apply_background()
        self._pick_quote()
        self._render()
        self.notifier.scan()            # 打开 App 时补发
        self.notifier.start(interval=600)

    # ================= 初始化 =================
    def _setup(self):
        p = self.page
        p.title = APP_NAME
        p.theme_mode = ft.ThemeMode.LIGHT
        p.theme = ft.Theme(color_scheme_seed=ft.Colors.BLUE)  # 冷静蓝主题
        p.padding = 0
        if sys.platform.startswith(("win", "linux")):  # 桌面调试时用手机竖屏比例窗口
            try:
                p.width = 420
                p.height = 820
            except Exception:
                pass

        self.file_picker = ft.FilePicker()
        # 0.86 起 FilePicker 是 Service：注册为前端服务绑定（不渲染），
        # 不能加进 overlay/控件树，否则客户端会渲染成红色 Unknown control 框
        try:
            self.page._services.register_service(self.file_picker)
        except Exception:
            pass

        p.appbar = ft.AppBar(
            leading=None,
            leading_width=48,
            title=ft.Text(APP_NAME),
            center_title=True,
            bgcolor=ft.Colors.BLUE_800,
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

        # 主体：背景图（可选）放在最底层，内容层叠一块半透明白遮罩保证可读性
        self.scroll = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)
        self._bg_overlay = ft.Container(
            content=self.scroll,
            bgcolor=ft.Colors.WHITE,
            expand=True,
        )
        self._bg_root = ft.Container(
            content=self._bg_overlay,
            expand=True,
            image=None,
        )
        p.add(self._bg_root)

        p.floating_action_button = ft.FloatingActionButton(
            icon=ft.Icons.ADD,
            tooltip="新建项目",
            bgcolor=ft.Colors.BLUE_800,
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
        if parent_id is None and self._quote:   # 根界面顶部显示每日一句
            qc = self._quote_card()
            if qc is not None:
                controls.append(qc)
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

    # ================= 列表行（卡片式） =================
    def _item_row(self, it, in_completed=False):
        item_id = it["id"]
        done = bool(it["done"])
        has_children = self.db.has_children(item_id)

        # 标题
        title_style = None
        if done:
            title_style = ft.TextStyle(
                decoration=ft.TextDecoration.LINE_THROUGH, color=ft.Colors.GREY
            )
        title = ft.Text(
            it["title"], size=16, weight=ft.FontWeight.W_500, style=title_style,
            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
        )

        # 元信息行：截止时间胶囊 + 进度
        meta = []
        if it["deadline"]:
            overdue = False
            d = parse_deadline(it["deadline"])
            if d is not None and d < dt.datetime.now():
                overdue = True
            dl_color = self._deadline_color(it["deadline"])
            meta.append(
                ft.Container(
                    padding=ft.Padding(left=8, right=8, top=2, bottom=2),
                    border_radius=8,
                    bgcolor=ft.Colors.with_opacity(0.14, dl_color),
                    content=ft.Text(
                        fmt_deadline(it["deadline"]),
                        size=11, color=dl_color,
                        weight=ft.FontWeight.BOLD if overdue else None,
                    ),
                )
            )
        if has_children:
            dd, tt = self.db.subtree_stats(item_id)
            meta.append(ft.Text(f"进度 {dd}/{tt}", size=12, color=ft.Colors.BLUE_GREY_400))
        meta_row = ft.Row(meta, spacing=8) if meta else None

        # ⋯ 菜单：添加子项目 / 进入子项目 / 编辑 / 删除
        menu_items = [
            ft.PopupMenuItem(
                content=ft.Text("添加子项目"),
                icon=ft.Icons.ADD_CIRCLE_OUTLINE,
                on_click=lambda e, i=item_id: self._open_edit(parent_id=i),
            )
        ]
        if has_children:
            menu_items.append(
                ft.PopupMenuItem(
                    content=ft.Text("进入子项目"),
                    icon=ft.Icons.CHEVRON_RIGHT,
                    on_click=lambda e, i=item_id: self._enter_children(i),
                )
            )
        if in_completed:
            menu_items.append(
                ft.PopupMenuItem(
                    content=ft.Text("恢复"),
                    icon=ft.Icons.UNDO,
                    on_click=lambda e, i=item_id: self._restore(i),
                )
            )
        else:
            menu_items.append(
                ft.PopupMenuItem(
                    content=ft.Text("编辑"),
                    icon=ft.Icons.EDIT,
                    on_click=lambda e, i=item_id: self._open_edit(i),
                )
            )
        menu_items.append(
            ft.PopupMenuItem(
                content=ft.Text("删除"),
                icon=ft.Icons.DELETE,
                on_click=lambda e, i=item_id: self._confirm_delete(i),
            )
        )

        # 卡片
        return ft.Container(
            margin=ft.Margin(left=12, right=12, top=5, bottom=5),
            padding=ft.Padding(left=6, right=4, top=6, bottom=6),
            border_radius=12,
            bgcolor=ft.Colors.WHITE,
            border=ft.Border.all(1, ft.Colors.with_opacity(0.35, ft.Colors.LIGHT_BLUE_100)),
            shadow=ft.BoxShadow(
                blur_radius=4,
                color=ft.Colors.with_opacity(0.06, ft.Colors.BLUE_GREY_700),
                offset=ft.Offset(0, 1),
            ),
            opacity=0.6 if done else 1.0,
            content=ft.Row(
                [
                    ft.Checkbox(
                        value=done,
                        active_color=ft.Colors.PRIMARY,
                        on_change=lambda e, i=item_id: self._on_toggle(e, i),
                    ),
                    ft.Container(
                        expand=True,
                        on_click=lambda e, i=item_id, hc=has_children: self._on_row_click(i, hc),
                        on_long_press=lambda e, i=item_id: self._open_edit(i),
                        content=ft.Column(
                            [title, meta_row] if meta_row else [title],
                            spacing=4,
                        ),
                    ),
                    ft.PopupMenuButton(
                        icon=ft.Icons.MORE_VERT,
                        icon_color=ft.Colors.BLUE_GREY_600,
                        tooltip="更多操作",
                        items=menu_items,
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _completed_header(self, count):
        return ft.Container(
            margin=ft.Margin(left=16, right=12, top=10, bottom=2),
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.CHECK_CIRCLE, size=18, color=ft.Colors.TEAL),
                    ft.Text(
                        f"已完成 ({count})",
                        size=13, weight=ft.FontWeight.BOLD, color=ft.Colors.BLUE_GREY_600,
                    ),
                    ft.Container(expand=True),
                    ft.TextButton("清除全部", on_click=self._clear_completed),
                    ft.Icon(
                        ft.Icons.EXPAND_LESS if self._completed_open else ft.Icons.EXPAND_MORE,
                        size=18, color=ft.Colors.BLUE_GREY_600,
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=6,
            ),
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

    # ================= 每日一句 =================
    def _pick_quote(self):
        """随机选一句每日一句；没有句子则为 None。"""
        quotes = get_quotes()
        self._quote = random.choice(quotes) if quotes else None

    def _quote_card(self):
        if not self._quote:
            return None
        return ft.Container(
            on_click=lambda e: self._shuffle_quote(),
            ink=True,
            margin=ft.Margin(top=8, left=12, right=12, bottom=4),
            padding=ft.Padding(left=16, right=16, top=12, bottom=12),
            bgcolor=ft.Colors.with_opacity(0.55, ft.Colors.LIGHT_BLUE_50),
            border_radius=10,
            border=ft.Border.all(1, ft.Colors.with_opacity(0.4, ft.Colors.LIGHT_BLUE_100)),
            content=ft.Column(
                [
                    ft.Text("每日一句 · 点一下换一句", size=11,
                            color=ft.Colors.BLUE_600, weight=ft.FontWeight.BOLD),
                    ft.Text(f"「{self._quote}」", size=14, italic=True,
                            color=ft.Colors.BLUE_GREY_700),
                ],
                spacing=6,
            ),
        )

    def _shuffle_quote(self):
        self._pick_quote()
        self._render()

    def _edit_quotes(self, e):
        self.page.pop_dialog()  # 先关掉设置对话框
        field = ft.TextField(
            label="每日一句（每行一句，空行忽略）",
            value="\n".join(get_quotes()),
            multiline=True,
            min_lines=5,
            max_lines=9,
        )
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("编辑每日一句"),
            content=field,
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(content="保存", on_click=lambda e: self._save_quotes(field)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _save_quotes(self, field):
        save_quotes(field.value or "")
        self.page.pop_dialog()
        self._pick_quote()
        self._render()

    # ================= 自定义背景图 =================
    def _apply_background(self):
        """根据已保存的背景图刷新页面背景；无图则纯白。"""
        path = self.db.get_bg_image()
        if path:
            src = _image_to_data_uri(path)
            if src:
                self._bg_root.image = ft.DecorationImage(src=src, fit=ft.BoxFit.COVER)
                self._bg_overlay.bgcolor = ft.Colors.with_opacity(0.82, ft.Colors.WHITE)
            else:
                self._bg_root.image = None
                self._bg_overlay.bgcolor = ft.Colors.WHITE
        else:
            self._bg_root.image = None
            self._bg_overlay.bgcolor = ft.Colors.WHITE
        self.page.update()

    async def _set_bg_image(self, e):
        self.page.pop_dialog()
        try:
            files = await self.file_picker.pick_files(
                dialog_title="选择背景图片",
                file_type=ft.FilePickerFileType.IMAGE,
                with_data=True,
            )
        except Exception as ex:
            self._toast(f"选择失败：{ex}")
            return
        if not files:
            return
        fp = files[0]
        src_path = getattr(fp, "path", None)
        try:
            if src_path:
                self.db.set_bg_image(src_path)
            elif getattr(fp, "bytes", None):
                tmp = os.path.join(DATA_DIR, "_bg_pick_tmp")
                with open(tmp, "wb") as f:
                    f.write(fp.bytes)
                try:
                    self.db.set_bg_image(tmp)
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            else:
                self._toast("无法读取图片")
                return
        except (ValueError, OSError) as ex:
            self._toast(f"设置背景失败：{ex}")
            return
        self._apply_background()
        self._toast("背景图已设置")

    def _clear_bg_image(self, e):
        self.page.pop_dialog()
        self.db.clear_bg_image()
        self._apply_background()
        self._toast("背景图已清除")

    # ================= 设置 / 备份 =================
    def _open_settings(self, e):
        dlg = ft.AlertDialog(
            modal=True,
            scrollable=True,
            title=ft.Text("设置"),
            content=ft.Column(
                [
                    ft.Text("数据目录", size=12, color=ft.Colors.GREY),
                    ft.Text(DATA_DIR, size=11, color=ft.Colors.GREY),
                    ft.ListTile(
                        leading=ft.Icon(ft.Icons.FORMAT_QUOTE),
                        title=ft.Text("编辑每日一句"),
                        subtitle=ft.Text("每行一句，打开 App 随机显示", size=11),
                        on_click=self._edit_quotes,
                    ),
                    ft.ListTile(
                        leading=ft.Icon(ft.Icons.IMAGE),
                        title=ft.Text("设置背景图"),
                        subtitle=ft.Text("从相册选一张作为背景", size=11),
                        on_click=self._set_bg_image,
                    ),
                    ft.ListTile(
                        leading=ft.Icon(ft.Icons.IMAGE_NOT_SUPPORTED),
                        title=ft.Text("清除背景图"),
                        on_click=self._clear_bg_image,
                    ),
                    ft.ListTile(
                        leading=ft.Icon(ft.Icons.DOWNLOAD),
                        title=ft.Text("导出备份"),
                        on_click=self._export,
                    ),
                    ft.ListTile(
                        leading=ft.Icon(ft.Icons.PLAYLIST_ADD),
                        title=ft.Text("导入计划"),
                        subtitle=ft.Text("从文件追加任务，不覆盖现有", size=11),
                        on_click=self._import_plan,
                    ),
                    ft.ListTile(
                        leading=ft.Icon(ft.Icons.UPLOAD_FILE),
                        title=ft.Text("导入备份（覆盖现有）"),
                        subtitle=ft.Text("整体还原备份，会替换全部任务", size=11),
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

    async def _export(self, e):
        self.page.pop_dialog()
        data = self.db.export().encode("utf-8")
        fname = f"每日任务备份_{dt.datetime.now():%Y%m%d_%H%M}.json"
        try:
            path = await self.file_picker.save_file(
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

    async def _import(self, e):
        self.page.pop_dialog()
        try:
            files = await self.file_picker.pick_files(
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

    async def _import_plan(self, e):
        """从外部导入计划：追加为新项目，不覆盖现有任务。"""
        self.page.pop_dialog()
        try:
            files = await self.file_picker.pick_files(
                dialog_title="选择计划文件", allowed_extensions=["json"]
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
            n = self.db.import_plan(text)
            self._render()
            self._toast(f"已导入计划：追加 {n} 个任务，原有数据未改动")
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
        """冷静蓝梯度：浅蓝=常态，中蓝=24h 内，深蓝=已过期。"""
        d = parse_deadline(s)
        if d is None:
            return ft.Colors.BLUE_GREY_400
        now = dt.datetime.now()
        if d < now:
            return ft.Colors.BLUE_900        # 已过期（深蓝，加粗提示）
        if d <= now + dt.timedelta(hours=24):
            return ft.Colors.BLUE_600        # 24 小时内
        return ft.Colors.LIGHT_BLUE_400      # 常态


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
async def main(page: ft.Page):
    # 锁定竖屏（async 方法需 await）
    try:
        await page.set_allowed_device_orientations(
            [ft.DeviceOrientation.PORTRAIT_UP, ft.DeviceOrientation.PORTRAIT_DOWN]
        )
    except Exception:
        pass
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

    # 每日一句
    save_quotes("今天也要加油\n保持冷静")
    assert get_quotes() == ["今天也要加油", "保持冷静"]
    save_quotes("")

    # 背景图模型往返
    png_path = os.path.join(tmp, "b.png")
    with open(png_path, "wb") as f:
        f.write(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        ))
    db.set_bg_image(png_path)
    assert db.get_bg_image() is not None
    assert _image_to_data_uri(db.get_bg_image()).startswith("data:image/png;base64,")
    db.clear_bg_image()
    assert db.get_bg_image() is None

    print("SELFTEST OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        run_selftest()
    else:
        ft.run(main)
