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
import calendar as _cal
import asyncio
import datetime as dt
import io
import os
import random
import sys

import flet as ft

from ai_client import (
    AI_SKILLS,
    SKILL_BY_ID,
    build_role_system,
    chat_completion,
    extract_load_requests,
    extract_tasks,
    parse_role_card,
    parse_state_block,
)
from models import DATA_DIR, Database, fmt_deadline, get_quotes, next_deadline, parse_deadline, save_quotes
from notifications import Notifier, notify

APP_NAME = "天野陽菜"
APP_VERSION = "v1.1.0"      # 每次构建手动递增，便于确认手机上是哪个包
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
# 标签调色板（索引与 models.TAG_PALETTE_SIZE 对齐）
# ---------------------------------------------------------------------------
TAG_COLORS = [
    ft.Colors.BLUE_600, ft.Colors.TEAL_600, ft.Colors.PURPLE_600,
    ft.Colors.GREEN_600, ft.Colors.BROWN_600, ft.Colors.PINK_600,
    ft.Colors.CYAN_600, ft.Colors.ORANGE_800, ft.Colors.INDIGO_600,
    ft.Colors.AMBER_700, ft.Colors.DEEP_PURPLE_600, ft.Colors.LIGHT_BLUE_600,
]


def _tag_color(color_idx):
    return TAG_COLORS[color_idx % len(TAG_COLORS)]


# ---------------------------------------------------------------------------
# 主应用
# ---------------------------------------------------------------------------
class TaskApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.db = Database()
        self.notifier = Notifier(self.db)

        self.stack = []                 # 导航栈：[(parent_id, title), ...]，空 = 顶层
        self._show_done = False         # 是否在全局「完成区」界面
        self._tag_filter = None         # 首页标签筛选（None = 全部）
        self._sort_by_deadline = False  # 是否按截止时间排序
        self._search_mode = False       # 是否在搜索界面
        self._search_query = ""         # 搜索关键词
        self._search_field = None       # 搜索输入框（保持引用避免失焦）
        self._search_results = None     # 搜索结果列
        self._calendar_view = False     # 是否在日历视图
        today = dt.date.today()
        self._calendar_year = today.year
        self._calendar_month = today.month
        self._selected_day = today      # 日历选中的日期
        self._ai_center = False         # 是否在 AI 中心
        self._ai_session_id = None      # 当前打开的 AI 会话 id
        self._ai_input = None           # 对话输入框（保持引用避免失焦）
        self._ai_busy = False           # 是否正在等待 AI 回复
        self._role_loaded_sections = {} # roleplay 会话已加载的角色卡段
        self._dismissed_stack = []      # 滑动删除后的子树快照栈（逐个撤销）
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
        self.share = ft.Share()
        try:
            self.page._services.register_service(self.share)
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
        if self._show_done:
            self._update_appbar(None, "完成区", done=True)
            self.scroll.controls = self._render_done()
        elif self._ai_center:
            self._update_appbar(None, "AI", ai=True)
            self.scroll.controls = self._render_ai_center()
        elif self._ai_session_id is not None:
            self._update_appbar(None, self._ai_session_title(), ai_chat=True)
            self.scroll.controls = self._render_ai_chat()
        elif self._search_mode:
            self._update_appbar(None, "搜索", search=True)
            self.scroll.controls = self._render_search()
        elif self._calendar_view:
            self._update_appbar(None, "日历", calendar=True)
            self.scroll.controls = self._render_calendar()
        else:
            parent_id, title = self._current()
            self._update_appbar(parent_id, title)
            if parent_id is None:
                self.scroll.controls = self._render_home()
            else:
                self.scroll.controls = self._render_level(parent_id)
        self._set_fab(not (
            self._show_done or self._search_mode or self._calendar_view
            or self._ai_center or self._ai_session_id is not None
        ))
        self.page.update()

    def _update_appbar(self, parent_id, title, done=False, search=False, calendar=False, ai=False, ai_chat=False):
        if done:
            self.page.appbar.leading = ft.IconButton(
                icon=ft.Icons.ARROW_BACK, tooltip="返回",
                icon_color=ft.Colors.ON_PRIMARY, on_click=self._close_done,
            )
            self.page.appbar.title = ft.Text("完成区")
            self.page.appbar.actions = [self._settings_icon()]
        elif search:
            self.page.appbar.leading = ft.IconButton(
                icon=ft.Icons.ARROW_BACK, tooltip="返回",
                icon_color=ft.Colors.ON_PRIMARY, on_click=self._close_search,
            )
            self.page.appbar.title = ft.Text("搜索")
            self.page.appbar.actions = [self._settings_icon()]
        elif calendar:
            self.page.appbar.leading = ft.IconButton(
                icon=ft.Icons.ARROW_BACK, tooltip="返回",
                icon_color=ft.Colors.ON_PRIMARY, on_click=self._close_calendar,
            )
            self.page.appbar.title = ft.Text("日历")
            self.page.appbar.actions = [self._settings_icon()]
        elif ai:
            self.page.appbar.leading = ft.IconButton(
                icon=ft.Icons.ARROW_BACK, tooltip="返回",
                icon_color=ft.Colors.ON_PRIMARY, on_click=self._close_ai_center,
            )
            self.page.appbar.title = ft.Text("AI")
            self.page.appbar.actions = [self._settings_icon()]
        elif ai_chat:
            self.page.appbar.leading = ft.IconButton(
                icon=ft.Icons.ARROW_BACK, tooltip="返回",
                icon_color=ft.Colors.ON_PRIMARY, on_click=self._close_ai_chat,
            )
            self.page.appbar.title = ft.Text(title)
            self.page.appbar.actions = [self._settings_icon()]
        else:
            self.page.appbar.leading = (
                ft.IconButton(
                    icon=ft.Icons.ARROW_BACK, tooltip="返回",
                    icon_color=ft.Colors.ON_PRIMARY, on_click=self._back,
                )
                if self.stack
                else None
            )
            self.page.appbar.title = ft.Text(title)
            self.page.appbar.actions = [
                self._ai_icon(), self._search_icon(), self._calendar_icon(),
                self._done_icon(), self._settings_icon(),
            ]

    def _ai_icon(self):
        return ft.IconButton(
            icon=ft.Icons.AUTO_AWESOME, tooltip="AI",
            icon_color=ft.Colors.ON_PRIMARY, on_click=self._open_ai_center,
        )

    def _calendar_icon(self):
        return ft.IconButton(
            icon=ft.Icons.CALENDAR_MONTH, tooltip="日历",
            icon_color=ft.Colors.ON_PRIMARY, on_click=self._open_calendar,
        )

    def _search_icon(self):
        return ft.IconButton(
            icon=ft.Icons.SEARCH, tooltip="搜索",
            icon_color=ft.Colors.ON_PRIMARY, on_click=self._open_search,
        )

    def _done_icon(self):
        return ft.IconButton(
            icon=ft.Icons.CHECK_CIRCLE, tooltip="完成区",
            icon_color=ft.Colors.ON_PRIMARY, on_click=self._open_done,
        )

    def _settings_icon(self):
        return ft.IconButton(
            icon=ft.Icons.SETTINGS, tooltip="设置",
            icon_color=ft.Colors.ON_PRIMARY, on_click=self._open_settings,
        )

    def _set_fab(self, visible):
        fab = self.page.floating_action_button
        if fab is not None:
            fab.visible = visible

    # ---------- AI 中心 ----------
    def _open_ai_center(self, e=None):
        self._ai_center = True
        self._render()

    def _close_ai_center(self, e=None):
        self._ai_center = False
        self._render()

    def _render_ai_center(self):
        controls = [
            ft.Container(
                margin=ft.Margin(top=8, left=12, right=12, bottom=4),
                content=ft.Text(
                    "选择技能开始新的对话，或继续之前的会话",
                    size=13, color=ft.Colors.BLUE_GREY_600,
                ),
            )
        ]
        for skill in AI_SKILLS:
            controls.append(ft.ListTile(
                leading=ft.Icon(ft.Icons.AUTO_AWESOME, color=ft.Colors.BLUE_700),
                title=ft.Text(skill["name"], weight=ft.FontWeight.W_600),
                subtitle=ft.Text(skill["description"], size=12),
                trailing=ft.Icon(ft.Icons.ADD_CIRCLE_OUTLINE, color=ft.Colors.BLUE_700),
                on_click=lambda e, sid=skill["id"]: (
                    self._choose_roleplay() if sid == "roleplay"
                    else self._start_ai_session(sid)
                ),
                min_height=64,
            ))

        controls.append(ft.Container(
            margin=ft.Margin(top=16, left=12, right=12, bottom=4),
            content=ft.Text("我的会话", size=13, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.BLUE_GREY_700),
        ))
        sessions = self.db.list_ai_sessions()
        if not sessions:
            controls.append(self._hint_text("还没有 AI 会话"))
        for sess in sessions:
            skill = SKILL_BY_ID.get(sess["skill_id"])
            skill_name = skill["name"] if skill else sess["skill_id"]
            controls.append(ft.ListTile(
                leading=ft.Icon(ft.Icons.FORUM, color=ft.Colors.BLUE_GREY_500),
                title=ft.Text(sess["title"], max_lines=1,
                              overflow=ft.TextOverflow.ELLIPSIS),
                subtitle=ft.Text(f"{skill_name} · {sess['updated_at']}", size=11,
                                 color=ft.Colors.BLUE_GREY_400),
                trailing=ft.PopupMenuButton(
                    icon=ft.Icons.MORE_VERT,
                    icon_color=ft.Colors.BLUE_GREY_500,
                    items=[
                        ft.PopupMenuItem(
                            content=ft.Text("继续"),
                            on_click=lambda e, s=sess["id"]: self._open_ai_session(s),
                        ),
                        ft.PopupMenuItem(
                            content=ft.Text("删除"),
                            on_click=lambda e, s=sess["id"]: self._delete_ai_session(s),
                        ),
                    ],
                ),
                on_click=lambda e, s=sess["id"]: self._open_ai_session(s),
                min_height=58,
            ))
        return controls

    def _delete_ai_session(self, session_id):
        self.db.delete_ai_session(session_id)
        self._render()

    # ---------- 角色扮演：角色卡 ----------
    def _choose_roleplay(self):
        cards = self.db.list_role_cards()
        dropdown = ft.Dropdown(
            label="选择角色卡",
            options=[ft.dropdown.Option(key=c["id"], text=c["name"]) for c in cards],
            value=cards[0]["id"] if cards else None,
        )
        status = ft.Text(
            "还没有角色卡，先导入一份角色卡文件（txt 或 json）",
            size=12, color=ft.Colors.BLUE_GREY_600,
        ) if not cards else ft.Text("", size=12)
        actions = [
            ft.TextButton("导入角色卡", on_click=lambda e: self._import_role_card(dlg)),
            ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
            ft.FilledButton(
                content="开始对话",
                on_click=lambda e: self._begin_roleplay(dropdown.value, dlg),
            ),
        ]
        if cards:
            actions.insert(1, ft.TextButton(
                "删除所选角色卡",
                on_click=lambda e: self._delete_selected_role_card(dropdown, dlg),
            ))
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("开始角色扮演"),
            content=ft.Column(
                [
                    status,
                    dropdown if cards else ft.Text("暂无角色卡", size=13,
                                                   color=ft.Colors.GREY),
                ],
                tight=True,
                spacing=10,
            ),
            actions=actions,
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _delete_selected_role_card(self, dropdown, dlg):
        card_id = dropdown.value if dropdown is not None else None
        if not card_id:
            self._toast("请先选择要删除的角色卡")
            return
        card = self.db.get_role_card(card_id)
        if not card:
            return
        self.page.pop_dialog()
        confirm = ft.AlertDialog(
            modal=True,
            title=ft.Text("删除角色卡"),
            content=ft.Text(f"将删除「{card['name']}」及其全部聊天记录，确定？"),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(
                    content="删除",
                    on_click=lambda e: self._do_delete_role_card(card_id),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(confirm)

    def _do_delete_role_card(self, card_id):
        self.page.pop_dialog()
        for sess in self.db.list_ai_sessions():
            if sess.get("role_card_id") == card_id:
                self.db.delete_ai_session(sess["id"])
        self.db.delete_role_card(card_id)
        self._toast("角色卡已删除")
        self._choose_roleplay()

    def _begin_roleplay(self, card_id, dlg=None):
        if dlg is not None:
            self.page.pop_dialog()
        if not card_id:
            self._toast("请先选择或导入角色卡")
            return
        card = self.db.get_role_card(card_id)
        if not card:
            self._toast("角色卡不存在")
            return
        # 每个角色一个永久聊天框：直接继续已有 roleplay 会话
        existing = None
        for sess in self.db.list_ai_sessions():
            if sess["skill_id"] == "roleplay" and sess.get("role_card_id") == card_id:
                existing = sess
                break
        if existing:
            sid = existing["id"]
        else:
            sid = self.db.create_ai_session(
                "roleplay", f"角色扮演 · {card['name']}", role_card_id=card_id
            )
        self._ai_session_id = sid
        self._ai_center = False
        self._render()

    async def _import_role_card(self, dlg=None):
        if dlg is not None:
            self.page.pop_dialog()
        try:
            files = await self.file_picker.pick_files(
                dialog_title="选择角色卡",
                allowed_extensions=["txt", "json"],
            )
        except Exception as ex:
            self._toast(f"选择失败：{ex}")
            return
        if not files:
            self._toast("已取消")
            return
        fp = files[0]
        try:
            text = None
            b = getattr(fp, "bytes", None)
            if b:
                text = b.decode("utf-8", "replace")
            elif getattr(fp, "path", None):
                with open(fp.path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            if not text:
                raise ValueError("文件为空")
            raw_name = getattr(fp, "name", None)
            if not raw_name and getattr(fp, "path", None):
                raw_name = os.path.basename(fp.path)
            name = raw_name or "角色卡"
            content = text
            try:
                import json as _json
                data = _json.loads(text)
                if isinstance(data, dict):
                    name = str(data.get("name") or data.get("角色名") or name)
                    content = str(
                        data.get("content") or data.get("system") or data.get("设定")
                        or text
                    )
            except Exception:
                pass
            self.db.create_role_card(name, content)
            self._toast(f"已导入角色卡：{name}")
            self._choose_roleplay()
        except Exception as ex:
            self._toast(f"导入失败：{ex}")

    def _start_ai_session(self, skill_id, project_id=None):
        skill = SKILL_BY_ID.get(skill_id)
        if not skill:
            return
        title = skill["name"]
        it = self.db.get(project_id) if project_id else None
        if it:
            title = f"{skill['name']} · {it['title']}"
        sid = self.db.create_ai_session(skill_id, title, project_id)
        self._ai_session_id = sid
        self._ai_center = False
        if project_id and it and skill["kind"] == "decompose":
            kids = self.db.children(project_id)
            ctx = f"项目：{it['title']}\n截止时间：{it['deadline'] or '未设置'}"
            if kids:
                ctx += "\n已有子任务：" + "、".join(k["title"] for k in kids[:10])
            self.db.append_ai_message(sid, "user", ctx)
        self._render()
        if project_id and it and skill["kind"] == "decompose":
            # 自动触发第一轮 AI 回复
            runner = getattr(self.page, "run_task", None)
            if runner:
                runner(self._send_ai_message, None)

    def _open_ai_session(self, session_id):
        self._ai_session_id = session_id
        self._ai_center = False
        self._render()

    def _close_ai_chat(self, e=None):
        self._ai_session_id = None
        self._ai_input = None
        self._ai_center = True
        self._render()

    def _ai_session_title(self):
        sess = self.db.get_ai_session(self._ai_session_id) if self._ai_session_id else None
        return sess["title"] if sess else "AI 对话"

    def _role_state_card(self, card_id, session_id):
        state = self.db.role_card_state(card_id)
        affection = state.get("好感度", "未建立")
        emotion = state.get("当前情绪", "平静")
        memory = state.get("记忆", "")
        important = state.get("重要记忆", "")
        lines = [
            ft.Row(
                [
                    ft.Text("好感度", size=11, color=ft.Colors.BLUE_GREY_500),
                    ft.Text(str(affection), size=13, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.BLUE_700),
                    ft.Container(width=8),
                    ft.Text("情绪", size=11, color=ft.Colors.BLUE_GREY_500),
                    ft.Text(str(emotion), size=13, color=ft.Colors.BLUE_GREY_700),
                    ft.Container(expand=True),
                    ft.TextButton("重置关系", on_click=lambda e: self._reset_role_relation(card_id)),
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        ]
        if memory:
            lines.append(ft.Text(f"记忆：{memory}", size=12,
                                 color=ft.Colors.BLUE_GREY_600,
                                 max_lines=2, overflow=ft.TextOverflow.ELLIPSIS))
        if important:
            lines.append(ft.Text(f"重要：{important}", size=12,
                                 color=ft.Colors.BLUE_700,
                                 max_lines=2, overflow=ft.TextOverflow.ELLIPSIS))
        return ft.Container(
            margin=ft.Margin(left=12, right=12, top=8, bottom=4),
            padding=ft.Padding(left=12, right=8, top=8, bottom=8),
            bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.LIGHT_BLUE_200),
            border_radius=10,
            content=ft.Column(lines, tight=True, spacing=4),
        )

    def _reset_role_relation(self, card_id):
        card = self.db.get_role_card(card_id)
        if not card:
            return
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("重置关系"),
            content=ft.Text(
                f"将清空「{card['name']}」的对话历史、记忆、情绪和好感度，"
                "角色卡本身保留。确定？"
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(
                    content="确认重置",
                    on_click=lambda e: self._do_reset_role_relation(card_id),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _do_reset_role_relation(self, card_id):
        self.page.pop_dialog()
        # 删除该角色所有 roleplay 会话，重置动态状态
        for sess in self.db.list_ai_sessions():
            if sess["skill_id"] == "roleplay" and sess.get("role_card_id") == card_id:
                self.db.delete_ai_session(sess["id"])
        self.db.save_role_card_state(card_id, {})
        if self._ai_session_id is not None:
            self._ai_session_id = None
        self._ai_input = None
        self._ai_center = True
        self._render()
        self._toast("关系已重置，角色卡保留")

    def _render_ai_chat(self):
        sess = self.db.get_ai_session(self._ai_session_id) if self._ai_session_id else None
        if not sess:
            return [self._hint_text("会话不存在")]
        skill = SKILL_BY_ID.get(sess["skill_id"])
        kind = skill["kind"] if skill else "chat"
        messages = self.db.get_ai_messages(sess["id"])

        rows = []
        role_label = None
        if skill and skill["id"] == "roleplay" and sess.get("role_card_id"):
            rows.append(self._role_state_card(sess["role_card_id"], sess["id"]))
            card = self.db.get_role_card(sess["role_card_id"])
            role_label = card["name"] if card else "角色"
        for i, msg in enumerate(messages):
            rows.append(self._ai_message_row(
                msg,
                is_last=(i == len(messages) - 1),
                kind=kind,
                role_label=role_label,
            ))

        if kind == "decompose":
            last_assistant = next(
                (m for m in reversed(messages) if m["role"] == "assistant"), None
            )
            # 只有最后一条 AI 回复里真有可解析的大纲时才显示按钮，避免占位
            if last_assistant and extract_tasks(last_assistant["content"]):
                rows.append(ft.Container(
                    padding=ft.Padding(left=12, right=12, top=8, bottom=4),
                    content=ft.FilledButton(
                        content="预览并生成任务树",
                        icon=ft.Icons.PLAYLIST_ADD,
                        on_click=lambda e, s=sess["id"]: self._preview_ai_tasks(s),
                    ),
                ))

        if self._ai_input is None:
            self._ai_input = ft.TextField(
                hint_text="回复 AI，或输入新问题",
                expand=True,
                min_lines=1,
                max_lines=3,
                on_submit=self._send_ai_message,
                disabled=self._ai_busy,
            )
        else:
            self._ai_input.disabled = self._ai_busy

        rows.append(ft.Container(
            padding=ft.Padding(left=12, right=12, top=8, bottom=8),
            content=ft.Row(
                [
                    self._ai_input,
                    ft.IconButton(
                        icon=ft.Icons.SEND,
                        icon_color=ft.Colors.WHITE,
                        bgcolor=ft.Colors.BLUE_700,
                        tooltip="发送",
                        disabled=self._ai_busy,
                        on_click=self._send_ai_message,
                    ),
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.END,
            ),
        ))
        return rows

    def _ai_message_row(self, msg, is_last=False, kind="chat", role_label=None):
        role = msg["role"]
        content = msg["content"]
        is_user = role == "user"
        bubble_color = (
            ft.Colors.with_opacity(0.16, ft.Colors.BLUE_700)
            if is_user else ft.Colors.WHITE
        )
        border = ft.Border.all(
            1, ft.Colors.with_opacity(0.25, ft.Colors.LIGHT_BLUE_200)
        ) if not is_user else None
        actions = None
        if not is_user and kind == "chat" and is_last:
            actions = ft.Row(
                [
                    ft.IconButton(icon=ft.Icons.COPY, icon_size=16,
                                  tooltip="复制", on_click=lambda e, t=content: self._copy_text(t)),
                    ft.IconButton(icon=ft.Icons.SHARE, icon_size=16,
                                  tooltip="分享", on_click=lambda e, t=content: self._share_text(t)),
                    ft.IconButton(icon=ft.Icons.NOTES, icon_size=16,
                                  tooltip="存为备注", on_click=lambda e, t=content: self._save_ai_note(t)),
                ],
                spacing=0,
            )
        bubble_parts = [
            ft.Text(
                "你" if is_user else (role_label or "AI"),
                size=11, color=ft.Colors.BLUE_GREY_500,
                weight=ft.FontWeight.BOLD,
            ),
            ft.Text(content, size=14),
        ]
        if actions is not None:
            bubble_parts.append(actions)
        return ft.Container(
            margin=ft.Margin(left=12, right=12, top=6, bottom=2),
            padding=ft.Padding(left=14, right=14, top=10, bottom=10),
            bgcolor=bubble_color,
            border=border,
            border_radius=12,
            content=ft.Column(
                bubble_parts,
                spacing=6,
                tight=True,
            ),
        )

    async def _send_ai_message(self, e):
        if self._ai_session_id is None or self._ai_busy:
            return
        # 发送按钮的 e.control 是 IconButton，没有 value；
        # 统一从输入框取值，兼容 on_submit 与 on_click 两种触发方式。
        text = (self._ai_input.value or "").strip() if self._ai_input is not None else ""
        if text:
            self.db.append_ai_message(self._ai_session_id, "user", text)
            if self._ai_input is not None:
                self._ai_input.value = ""

        sess = self.db.get_ai_session(self._ai_session_id)
        if not sess:
            return
        self._ai_busy = True
        self._render()
        try:
            reply = await self._chat_with_role_card(sess)
            if reply is None:
                return
            clean, state = parse_state_block(reply)
            if sess.get("role_card_id") and state:
                self._merge_role_state(sess["role_card_id"], state)
            if clean:
                self.db.append_ai_message(sess["id"], "assistant", clean)
        except Exception as ex:
            self._toast(f"AI 错误：{ex}")
        finally:
            self._ai_busy = False
            self._render()

    async def _chat_with_role_card(self, sess):
        """带角色卡按需加载的 AI 请求；AI 请求 @load 时自动注入后重试。"""
        skill = SKILL_BY_ID.get(sess["skill_id"])
        cfg = self.db.get_ai_config()
        card = None
        if sess.get("role_card_id"):
            card = self.db.get_role_card(sess["role_card_id"])
        loaded = set(self._role_loaded_sections.get(sess["id"], set()))

        for _ in range(3):
            if card:
                state = self.db.role_card_state(card["id"])
                system = build_role_system(card["content"], state, loaded)
            else:
                system = skill["system"] if skill else "你是一个简洁的 AI 助手。"
            history = self.db.get_ai_messages(sess["id"], limit=200)
            if not history:
                return None
            if history and history[-1]["role"] == "user":
                last_text = history[-1]["content"]
                if any(k in last_text for k in
                       ("一定要记得", "别忘了", "记住", "很重要", "请记住", "非常重要")):
                    system += (
                        "\n\n注意：用户本轮明确要求你记住某些内容。"
                        "请把它逐字保留到 ---STATE--- 的 重要记忆= 字段，不要压缩丢失。"
                    )
            payload = [{"role": "system", "content": system}]
            payload.extend(
                {"role": m["role"], "content": m["content"]} for m in history
            )
            reply = await asyncio.to_thread(
                chat_completion,
                cfg["ai_base_url"], cfg["ai_api_key"], cfg["ai_model"],
                payload,
            )
            clean, loads = extract_load_requests(reply)
            if not loads:
                return reply
            loaded.update(loads)
            self._role_loaded_sections[sess["id"]] = loaded
            # 不把 @load 请求存为回复，继续重试
        return clean

    def _merge_role_state(self, card_id, state):
        old = self.db.role_card_state(card_id)
        for key, value in state.items():
            if value:
                old[key] = value
        self.db.save_role_card_state(card_id, old)

    def _copy_text(self, text):
        self.page.run_task(self._do_copy_text, text)

    async def _do_copy_text(self, text):
        try:
            await self.page.clipboard.set(text)
            self._toast("已复制")
        except Exception:
            self._toast("复制失败")

    def _share_text(self, text):
        try:
            self.page.run_task(self._do_share_text, text)
        except Exception:
            self._toast("分享失败")

    async def _do_share_text(self, text):
        try:
            await self.share.share_text(text)
        except Exception:
            self._toast("分享失败")

    def _save_ai_note(self, text):
        # 有项目上下文时直接追加备注；否则提示从项目进入。
        sess = self.db.get_ai_session(self._ai_session_id) if self._ai_session_id else None
        pid = sess["project_id"] if sess else None
        if pid and self.db.get(pid):
            it = self.db.get(pid)
            old = it.get("note") or ""
            self.db.update(pid, note=(old + "\n" + text).strip())
            self._toast("已追加到项目备注")
            return
        self._toast("请从项目菜单进入 AI 会话，才能保存备注")

    # ---------- AI 任务树落库 ----------
    def _preview_ai_tasks(self, session_id):
        sess = self.db.get_ai_session(session_id)
        if not sess or sess["skill_id"] not in ("grill_decompose", "quick_decompose"):
            return
        pid = sess.get("project_id")
        if not pid or not self.db.get(pid):
            self._toast("这个会话没有关联项目")
            return
        last = self.db.get_ai_messages(session_id)
        assistant = [m for m in last if m["role"] == "assistant"]
        if not assistant:
            self._toast("AI 还没有生成任务树")
            return
        rows = extract_tasks(assistant[-1]["content"])
        if not rows:
            self._toast("AI 回复里没有可解析的任务大纲")
            return
        preview = "\n".join("  " * lvl + title for lvl, title in rows)
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("确认生成任务树"),
            content=ft.Column(
                [
                    ft.Text("将追加到项目下方，不覆盖现有子任务：", size=12,
                            color=ft.Colors.BLUE_GREY_600),
                    ft.Container(
                        padding=ft.Padding(left=10, right=10, top=8, bottom=8),
                        bgcolor=ft.Colors.with_opacity(0.08, ft.Colors.LIGHT_BLUE_300),
                        border_radius=8,
                        content=ft.Text(preview, size=13),
                    ),
                ],
                tight=True,
                spacing=8,
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(
                    content="确认追加",
                    on_click=lambda e, s=session_id, r=rows, p=pid: self._apply_ai_tasks(s, r, p),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _apply_ai_tasks(self, session_id, rows, project_id):
        self.page.pop_dialog()
        project = self.db.get(project_id)
        if not project:
            return
        # AI 大纲第一行往往是项目名本身；与项目标题一致时跳过，避免重复。
        if rows and rows[0][0] == 0 and rows[0][1].strip() == project["title"].strip():
            rows = rows[1:]
        stack = [(0, project_id)]
        added = 0
        for level, title in rows:
            while stack and level < stack[-1][0]:
                stack.pop()
            parent = stack[-1][1] if stack else project_id
            new_id = self.db.add(parent, title)
            added += 1
            stack.append((level, new_id))
        self._toast(f"已追加 {added} 个任务")
        self._render()

    # ---------- 搜索 ----------
    def _open_search(self, e=None):
        self._search_mode = True
        self._render()

    def _close_search(self, e=None):
        self._search_mode = False
        self._search_field = None
        self._render()

    def _render_search(self):
        if self._search_field is None:
            self._search_field = ft.TextField(
                value=self._search_query, label="搜索任务",
                prefix_icon=ft.Icons.SEARCH, autofocus=True,
                on_change=self._on_search_change,
            )
        if self._search_results is None:
            self._search_results = ft.Column(spacing=0)
        self._refresh_search_results()
        return [
            ft.Container(
                padding=ft.Padding(left=12, right=12, top=8, bottom=4),
                content=self._search_field,
            ),
            self._search_results,
        ]

    def _refresh_search_results(self):
        if self._search_results is not None:
            self._search_results.controls = self._build_search_results()

    def _build_search_results(self):
        q = self._search_query.strip()
        if not q:
            return [self._hint_text("输入关键词搜索所有任务")]
        results = self.db.search_items(q)
        if not results:
            return [self._hint_text("没有找到匹配的任务")]
        return [self._search_result_row(it) for it in results]

    def _on_search_change(self, e):
        self._search_query = e.control.value or ""
        self._refresh_search_results()
        try:
            self._search_results.update()
        except Exception:
            pass

    def _search_result_row(self, it):
        item_id = it["id"]
        done = bool(it["done"])
        meta = []
        if it["deadline"]:
            meta.append(self._deadline_pill(it["deadline"]))
        tag = self.db.tag_by_id(it.get("tag_id"))
        if tag:
            meta.append(self._tag_pill(tag))
        title_style = None
        if done:
            title_style = ft.TextStyle(
                decoration=ft.TextDecoration.LINE_THROUGH, color=ft.Colors.GREY
            )
        return self._card(
            margin=ft.Margin(left=12, right=12, top=5, bottom=5),
            content=ft.Row(
                [
                    ft.Icon(
                        ft.Icons.CHECK_CIRCLE if done else ft.Icons.RADIO_BUTTON_UNCHECKED,
                        color=ft.Colors.TEAL if done else ft.Colors.BLUE_GREY_300,
                        size=20,
                    ),
                    ft.Container(
                        expand=True,
                        on_click=lambda e, i=item_id: self._on_search_result_click(i),
                        content=ft.Column(
                            [
                                ft.Text(it["title"], size=15, style=title_style,
                                        max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                                ft.Text(self.db.title_path(item_id), size=11,
                                        color=ft.Colors.BLUE_GREY_400),
                                ft.Row(meta, spacing=8) if meta else None,
                            ],
                            spacing=3,
                        ),
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _on_search_result_click(self, item_id):
        self._close_search()
        if self.db.has_children(item_id):
            self._enter_children(item_id)
        else:
            self._open_edit(item_id)

    # ---------- 日历 ----------
    def _open_calendar(self, e=None):
        self._calendar_view = True
        self._render()

    def _close_calendar(self, e=None):
        self._calendar_view = False
        self._render()

    def _calendar_items_by_day(self):
        """返回 {date: [items, ...]}（仅当月内截止日期）。"""
        by_day = {}
        for it in self.db.items_with_deadline():
            d = parse_deadline(it["deadline"])
            if d is None:
                continue
            key = d.date()
            by_day.setdefault(key, []).append(it)
        return by_day

    def _calendar_prev(self, e):
        self._calendar_month -= 1
        if self._calendar_month < 1:
            self._calendar_month = 12
            self._calendar_year -= 1
        self._render()

    def _calendar_next(self, e):
        self._calendar_month += 1
        if self._calendar_month > 12:
            self._calendar_month = 1
            self._calendar_year += 1
        self._render()

    def _select_day(self, day):
        self._selected_day = day
        self._render()

    def _render_calendar(self):
        by_day = self._calendar_items_by_day()
        today = dt.date.today()
        controls = []

        # 月份导航
        controls.append(ft.Row(
            [
                ft.IconButton(icon=ft.Icons.CHEVRON_LEFT, on_click=self._calendar_prev),
                ft.Text(f"{self._calendar_year}年{self._calendar_month}月",
                        size=16, weight=ft.FontWeight.BOLD),
                ft.IconButton(icon=ft.Icons.CHEVRON_RIGHT, on_click=self._calendar_next),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=8,
        ))

        # 星期标题（周一开头）
        controls.append(ft.Row(
            [ft.Text(w, size=12, color=ft.Colors.BLUE_GREY_500,
                     width=52, text_align=ft.TextAlign.CENTER)
             for w in ["一", "二", "三", "四", "五", "六", "日"]],
            alignment=ft.MainAxisAlignment.CENTER,
        ))

        # 月历格子
        cal = _cal.Calendar(firstweekday=0)  # 周一开头
        for week in cal.monthdatescalendar(self._calendar_year, self._calendar_month):
            controls.append(ft.Row(
                [self._calendar_cell(day, by_day, today) for day in week],
                alignment=ft.MainAxisAlignment.CENTER,
            ))

        controls.append(ft.Container(height=6))

        # 选中日任务列表
        sd = self._selected_day
        if sd is not None and (sd.year, sd.month) == (self._calendar_year, self._calendar_month):
            controls.append(self._section_title(
                f"{sd.month}月{sd.day}日 任务 ({len(by_day.get(sd, []))})"))
            items = by_day.get(sd, [])
            if not items:
                controls.append(self._hint_text("当天没有截止任务"))
            for it in items:
                controls.append(self._calendar_day_row(it))
        else:
            controls.append(self._hint_text("点选日期查看当天任务"))

        return controls

    def _calendar_cell(self, day, by_day, today):
        if day.month != self._calendar_month:
            return ft.Container(width=52, height=52)   # 跨月留白
        count = len(by_day.get(day, []))
        is_today = (day == today)
        is_selected = (self._selected_day == day)
        return ft.Container(
            width=52, height=52,
            alignment=ft.Alignment(0, 0),
            bgcolor=ft.Colors.PRIMARY if is_selected else (
                ft.Colors.with_opacity(0.12, ft.Colors.PRIMARY) if is_today else None
            ),
            border_radius=10,
            on_click=lambda e, d=day: self._select_day(d),
            content=ft.Column(
                [
                    ft.Text(str(day.day), size=15,
                            weight=ft.FontWeight.BOLD if (is_today or is_selected) else None,
                            color=ft.Colors.ON_PRIMARY if is_selected else None),
                    ft.Container(
                        width=6, height=6, border_radius=3,
                        bgcolor=ft.Colors.BLUE_600 if count else ft.Colors.TRANSPARENT,
                    ) if count else ft.Container(width=6, height=6),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=2,
            ),
        )

    def _calendar_day_row(self, it):
        item_id = it["id"]
        done = bool(it["done"])
        title_style = None
        if done:
            title_style = ft.TextStyle(
                decoration=ft.TextDecoration.LINE_THROUGH, color=ft.Colors.GREY)
        return ft.ListTile(
            content_padding=ft.Padding(left=16, right=8, top=0, bottom=0),
            leading=ft.Checkbox(
                value=done, active_color=ft.Colors.PRIMARY,
                on_change=lambda e, i=item_id: self._on_toggle(e, i),
            ),
            title=ft.Text(it["title"], size=14, style=title_style,
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=self._deadline_pill(it["deadline"]) if it["deadline"] else None,
            trailing=self._item_menu(item_id, self.db.has_children(item_id)),
            dense=True,
            min_height=48,
        )

    # ---------- 排序 / 截止时间辅助 ----------
    def _own_deadline_ts(self, it):
        d = parse_deadline(it["deadline"])
        return d.timestamp() if d else float("inf")

    def _group_deadline_ts(self, item_id):
        d = self.db.subtree_earliest_deadline(item_id)
        return d.timestamp() if d else float("inf")

    def _sort_items(self, items, group=False):
        if self._sort_by_deadline:
            if group:
                items.sort(key=lambda it: self._group_deadline_ts(it["id"]))
            else:
                items.sort(key=lambda it: self._own_deadline_ts(it))
        else:
            items.sort(key=lambda it: it["id"])

    # ---------- 首页（两层分组） ----------
    def _stats_card(self):
        s = self.db.stats_overview()
        cells = []
        for label, val in [
            ("任务", str(s["total"])),
            ("进行中", str(s["active"])),
            ("本周完成", str(s["week_done"])),
            ("连续打卡", f"{s['streak']}天"),
        ]:
            cells.append(ft.Column(
                [
                    ft.Text(val, size=18, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.BLUE_800),
                    ft.Text(label, size=11, color=ft.Colors.BLUE_GREY_500),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2,
            ))
        return ft.Container(
            margin=ft.Margin(left=12, right=12, top=6, bottom=2),
            padding=ft.Padding(left=8, right=8, top=10, bottom=10),
            border_radius=12,
            bgcolor=ft.Colors.with_opacity(0.5, ft.Colors.LIGHT_BLUE_50),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.4, ft.Colors.LIGHT_BLUE_100)),
            content=ft.Row(cells, alignment=ft.MainAxisAlignment.SPACE_AROUND),
        )

    def _render_home(self):
        controls = []
        if self._quote:
            qc = self._quote_card()
            if qc is not None:
                controls.append(qc)
        controls.append(self._stats_card())
        controls.append(self._home_toolbar())

        roots = [r for r in self.db.roots() if not r["done"]]
        if self._tag_filter:
            roots = [r for r in roots if self._item_tag_name(r["id"]) == self._tag_filter]
        self._sort_items(roots, group=True)

        if not roots:
            controls.append(self._empty_hint("还没有项目，点右下角 + 新建"))
        for i, r in enumerate(roots):
            kids = [k for k in self.db.children(r["id"]) if not k["done"]]
            self._sort_items(kids)
            controls.append(self._level1_row(r, kids))
            if i < len(roots) - 1:
                controls.append(ft.Container(height=10))   # 项目间留白（无色）
        return controls

    def _home_toolbar(self):
        chips = [
            ft.Chip(
                label=ft.Text("全部"), selected=(self._tag_filter is None),
                selected_color=ft.Colors.BLUE_600,
                on_select=lambda e: self._set_tag_filter(None),
            )
        ]
        for name, color_idx in self.db.all_tags():
            col = _tag_color(color_idx)
            chips.append(ft.Chip(
                label=ft.Text(name), selected=(self._tag_filter == name),
                selected_color=col,
                on_select=lambda e, n=name: self._set_tag_filter(n),
            ))
        seg = ft.SegmentedButton(
            segments=[
                ft.Segment(value="default", label=ft.Text("默认")),
                ft.Segment(value="deadline", label=ft.Text("截止时间")),
            ],
            selected=["deadline" if self._sort_by_deadline else "default"],
            allow_empty_selection=False,
            on_change=self._on_sort_change,
        )
        return ft.Container(
            padding=ft.Padding(left=12, right=12, top=4, bottom=2),
            content=ft.Column([
                ft.Row(chips, scroll=ft.ScrollMode.AUTO, spacing=6),
                ft.Row([
                    ft.Text("排序：", size=12, color=ft.Colors.BLUE_GREY_600),
                    seg,
                ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ], spacing=8),
        )

    def _set_tag_filter(self, name):
        self._tag_filter = name
        self._render()

    def _on_sort_change(self, e):
        self._sort_by_deadline = "deadline" in (e.control.selected or set())
        self._render()

    def _item_tag_name(self, item_id):
        tag = self.db.tag_by_id(self.db.get(item_id).get("tag_id"))
        return tag[0] if tag else None

    # ---------- 子视图（单层卡片列表） ----------
    def _render_level(self, parent_id):
        children = [c for c in self.db.children(parent_id) if not c["done"]]
        self._sort_items(children)
        if not children:
            return [self._empty_hint("这个项目下还没有子任务")]
        return [self._item_row(c) for c in children]

    # ---------- 全局完成区 ----------
    def _render_done(self):
        controls = []
        done_roots = [r for r in self.db.roots() if r["done"]]
        controls.append(self._section_title(f"已完成的大项目 ({len(done_roots)})"))
        if done_roots:
            self._sort_items(done_roots, group=True)
            for r in done_roots:
                controls.append(self._done_group(r))
        else:
            controls.append(self._hint_text("暂无已完成的大项目"))

        standalone = []
        for r in self.db.roots():
            if r["done"]:
                continue
            for c in self.db.children(r["id"]):
                if c["done"]:
                    standalone.append((r, c))
        controls.append(self._section_title(f"进行中项目下的已完成子任务 ({len(standalone)})"))
        if standalone:
            for parent, c in standalone:
                controls.append(self._done_row(c, parent_title=parent["title"]))
        else:
            controls.append(self._hint_text("暂无"))

        controls.append(ft.Container(
            padding=ft.Padding(top=12, bottom=20),
            alignment=ft.Alignment(0, 0),
            content=ft.TextButton("清除全部已完成", on_click=self._confirm_clear_all),
        ))
        return controls

    def _section_title(self, text):
        return ft.Container(
            margin=ft.Margin(left=16, right=12, top=12, bottom=2),
            content=ft.Text(text, size=13, weight=ft.FontWeight.BOLD,
                            color=ft.Colors.BLUE_GREY_600),
        )

    def _hint_text(self, text):
        return ft.Container(
            padding=ft.Padding(left=16, right=12, top=6, bottom=6),
            content=ft.Text(text, size=12, color=ft.Colors.GREY),
        )

    def _empty_hint(self, text="还没有项目"):
        return ft.Container(
            padding=ft.Padding(top=90, left=24, right=24),
            alignment=ft.Alignment(0, 0),
            content=ft.Column(
                [
                    ft.Icon(ft.Icons.CHECKLIST, size=64, color=ft.Colors.GREY),
                    ft.Text(text, size=15, color=ft.Colors.GREY),
                    ft.Text("点右下角 + 新建", size=12, color=ft.Colors.GREY),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
        )

    # ================= 列表行（卡片式，用于子视图） =================
    def _item_row(self, it):
        item_id = it["id"]
        done = bool(it["done"])
        has_children = self.db.has_children(item_id)

        title_style = None
        if done:
            title_style = ft.TextStyle(
                decoration=ft.TextDecoration.LINE_THROUGH, color=ft.Colors.GREY
            )
        title = ft.Text(
            it["title"], size=15, weight=ft.FontWeight.W_500, style=title_style,
            max_lines=2, overflow=ft.TextOverflow.ELLIPSIS,
        )
        meta = []
        if it["deadline"]:
            meta.append(self._deadline_pill(it["deadline"]))
        ni = self._note_icon(it)
        if ni:
            meta.append(ni)
        rp = self._repeat_pill(it)
        if rp:
            meta.append(rp)
        if has_children:
            dd, tt = self.db.subtree_stats(item_id)
            meta.append(ft.Text(f"进度 {dd}/{tt}", size=12, color=ft.Colors.BLUE_GREY_400))
        meta_row = ft.Row(meta, spacing=8) if meta else None

        return self._dismiss_wrap(self._card(
            margin=ft.Margin(left=12, right=12, top=5, bottom=5),
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
                    self._item_menu(item_id, has_children),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        ), item_id)

    def _card(self, margin, opacity=1.0, content=None):
        return ft.Container(
            margin=margin,
            padding=ft.Padding(left=6, right=4, top=6, bottom=6),
            border_radius=12,
            bgcolor=ft.Colors.WHITE,
            border=ft.Border.all(1, ft.Colors.with_opacity(0.35, ft.Colors.LIGHT_BLUE_100)),
            shadow=ft.BoxShadow(
                blur_radius=4,
                color=ft.Colors.with_opacity(0.06, ft.Colors.BLUE_GREY_700),
                offset=ft.Offset(0, 1),
            ),
            opacity=opacity,
            content=content,
        )

    def _deadline_pill(self, deadline):
        overdue = False
        d = parse_deadline(deadline)
        if d is not None and d < dt.datetime.now():
            overdue = True
        dl_color = self._deadline_color(deadline)
        return ft.Container(
            padding=ft.Padding(left=8, right=8, top=2, bottom=2),
            border_radius=8,
            bgcolor=ft.Colors.with_opacity(0.14, dl_color),
            content=ft.Text(
                fmt_deadline(deadline),
                size=11, color=dl_color,
                weight=ft.FontWeight.BOLD if overdue else None,
            ),
        )

    def _tag_pill(self, tag):
        name, color_idx = tag
        col = _tag_color(color_idx)
        return ft.Container(
            padding=ft.Padding(left=8, right=8, top=2, bottom=2),
            border_radius=8,
            bgcolor=ft.Colors.with_opacity(0.16, col),
            content=ft.Text(name, size=11, color=col, weight=ft.FontWeight.BOLD),
        )

    def _note_icon(self, it):
        if not (it.get("note") or "").strip():
            return None
        return ft.Icon(ft.Icons.NOTES, size=14, color=ft.Colors.BLUE_GREY_400)

    def _repeat_pill(self, it):
        rt = it.get("repeat_type", "")
        if not rt:
            return None
        if rt == "daily":
            label = "每天"
        elif rt == "weekly":
            label = "每周"
        else:
            label = f"每{int(it.get('repeat_interval') or 1)}天"
        return ft.Container(
            padding=ft.Padding(left=8, right=8, top=2, bottom=2),
            border_radius=8,
            bgcolor=ft.Colors.with_opacity(0.14, ft.Colors.BLUE_GREY_500),
            content=ft.Text(label, size=11, color=ft.Colors.BLUE_GREY_600),
        )

    def _item_menu(self, item_id, has_children, done_ctx=False):
        menu = []
        if not done_ctx:
            menu.append(ft.PopupMenuItem(
                content=ft.Text("AI 拆解"),
                icon=ft.Icons.SMART_TOY,
                on_click=lambda e, i=item_id: self._start_ai_session(
                    "grill_decompose", project_id=i
                ),
            ))
        menu.append(
            ft.PopupMenuItem(
                content=ft.Text("添加子项目"),
                icon=ft.Icons.ADD_CIRCLE_OUTLINE,
                on_click=lambda e, i=item_id: self._open_edit(parent_id=i),
            )
        )
        if has_children:
            menu.append(ft.PopupMenuItem(
                content=ft.Text("进入子项目"),
                icon=ft.Icons.CHEVRON_RIGHT,
                on_click=lambda e, i=item_id: self._enter_children(i),
            ))
        if done_ctx:
            menu.append(ft.PopupMenuItem(
                content=ft.Text("撤销完成"),
                icon=ft.Icons.UNDO,
                on_click=lambda e, i=item_id: self._undo_completed(i),
            ))
        else:
            menu.append(ft.PopupMenuItem(
                content=ft.Text("编辑"),
                icon=ft.Icons.EDIT,
                on_click=lambda e, i=item_id: self._open_edit(i),
            ))
        menu.append(ft.PopupMenuItem(
            content=ft.Text("删除"),
            icon=ft.Icons.DELETE,
            on_click=lambda e, i=item_id: self._confirm_delete(i),
        ))
        return ft.PopupMenuButton(
            icon=ft.Icons.MORE_VERT,
            icon_color=ft.Colors.BLUE_GREY_600,
            tooltip="更多操作",
            items=menu,
        )

    # ---------- 滑动手势 ----------
    def _dismiss_wrap(self, tile, item_id, done=False):
        """把一行包成可滑动组件：左滑删除、右滑完成/恢复。"""
        return ft.Dismissible(
            content=tile,
            dismiss_direction=ft.DismissDirection.HORIZONTAL,
            dismiss_thresholds={
                ft.DismissDirection.END_TO_START: 0.85,
                ft.DismissDirection.START_TO_END: 0.85,
            },
            background=ft.Container(
                alignment=ft.Alignment(0, 0),
                bgcolor=ft.Colors.TEAL,
                padding=ft.Padding(left=20, right=20, top=0, bottom=0),
                content=ft.Row(
                    [
                        ft.Icon(ft.Icons.CHECK_CIRCLE, color=ft.Colors.WHITE),
                        ft.Text("继续滑动完成", color=ft.Colors.WHITE,
                                weight=ft.FontWeight.BOLD),
                    ],
                    spacing=6,
                ),
            ),
            secondary_background=ft.Container(
                alignment=ft.Alignment(0, 0),
                bgcolor=ft.Colors.RED_600,
                padding=ft.Padding(left=20, right=20, top=0, bottom=0),
                content=ft.Row(
                    [
                        ft.Icon(ft.Icons.DELETE, color=ft.Colors.WHITE),
                        ft.Text("继续滑动删除", color=ft.Colors.WHITE,
                                weight=ft.FontWeight.BOLD),
                    ],
                    spacing=6,
                ),
            ),
            on_dismiss=lambda e, i=item_id, d=done: self._on_dismiss(e, i, d),
        )

    def _on_dismiss(self, e, item_id, done):
        direction = getattr(e, "direction", None)
        if direction == ft.DismissDirection.END_TO_START:
            self._dismiss_delete(item_id)
        elif direction == ft.DismissDirection.START_TO_END:
            self._dismiss_complete(item_id, done)

    def _dismiss_delete(self, item_id):
        it = self.db.get(item_id)
        if not it:
            return
        snapshot = self.db.snapshot_subtree(item_id)
        self.db.delete(item_id)
        self._dismissed_stack.append(snapshot)
        self._render()
        self._toast(
            f"已删除「{it['title']}」",
            action_label="撤销",
            on_undo=self._undo_dismissed,
        )

    def _undo_dismissed(self):
        if not self._dismissed_stack:
            return
        snap = self._dismissed_stack.pop()
        self.db.restore_subtree(snap)
        self._render()
        # 还有更早的待撤销删除时，链式弹出下一个撤销提示，可逐个撤回
        if self._dismissed_stack:
            nxt = next(
                (it for it in self._dismissed_stack[-1]["items"]
                 if it.get("parent_id") is None),
                None,
            )
            title = nxt["title"] if nxt else "项目"
            self._toast(
                f"已删除「{title}」",
                action_label="撤销",
                on_undo=self._undo_dismissed,
            )

    def _dismiss_complete(self, item_id, done):
        if done:
            self._undo_completed(item_id)
            return
        it = self.db.get(item_id)
        if not it:
            return
        if self.db.has_children(item_id):
            self._render()
            self._confirm_complete_group(item_id)
            return
        if it.get("repeat_type"):
            self._complete_recurring(item_id, it)
        else:
            self.db.set_done(item_id, True)
            self.db.log_completion(item_id)
            self._render()

    # ================= 首页两层分组 =================
    def _level1_row(self, root, kids):
        """第一层项目行：大字号（ListTile，最稳定的原生行组件）。"""
        item_id = root["id"]
        meta = []
        if root["deadline"]:
            meta.append(self._deadline_pill(root["deadline"]))
        ni = self._note_icon(root)
        if ni:
            meta.append(ni)
        rp = self._repeat_pill(root)
        if rp:
            meta.append(rp)
        dd, tt = self.db.subtree_stats(item_id)
        meta.append(ft.Text(f"进度 {dd}/{tt}", size=12, color=ft.Colors.BLUE_GREY_400))
        tag = self.db.tag_by_id(root.get("tag_id"))
        if tag:
            meta.append(self._tag_pill(tag))

        root_tile = ft.ListTile(
                leading=ft.Checkbox(
                    value=False, active_color=ft.Colors.PRIMARY,
                    on_change=lambda e, i=item_id: self._on_toggle(e, i),
                ),
                title=ft.Text(root["title"], size=17, weight=ft.FontWeight.W_600,
                               max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                subtitle=ft.Row(meta, spacing=6) if meta else None,
                trailing=self._item_menu(item_id, bool(kids)),
                on_click=lambda e, i=item_id: self._enter_children(i),
                on_long_press=lambda e, i=item_id: self._open_edit(i),
                min_height=62,
            )
        tiles = [self._dismiss_wrap(root_tile, item_id)]
        for k in kids:
            tiles.append(self._dismiss_wrap(self._level2_row(k), k["id"]))
        return ft.Column(tiles, spacing=0)

    def _level2_row(self, it):
        """第二层项目行：小字号 + 缩进（ListTile）。"""
        item_id = it["id"]
        has_children = self.db.has_children(item_id)
        meta = []
        if it["deadline"]:
            meta.append(self._deadline_pill(it["deadline"]))
        ni = self._note_icon(it)
        if ni:
            meta.append(ni)
        rp = self._repeat_pill(it)
        if rp:
            meta.append(rp)
        return ft.ListTile(
            content_padding=ft.Padding(left=52, right=8, top=0, bottom=0),
            leading=ft.Checkbox(
                value=False, active_color=ft.Colors.PRIMARY,
                on_change=lambda e, i=item_id: self._on_toggle(e, i),
            ),
            title=ft.Text(it["title"], size=14, max_lines=1,
                          overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=ft.Row(meta, spacing=6) if meta else None,
            trailing=self._item_menu(item_id, has_children),
            on_click=lambda e, i=item_id, hc=has_children: self._on_row_click(i, hc),
            on_long_press=lambda e, i=item_id: self._open_edit(i),
            dense=True,
            min_height=48,
        )

    # ================= 完成区（ListTile，与首页一致） =================
    def _done_group(self, root):
        item_id = root["id"]
        kids = self.db.children(item_id)
        self._sort_items(kids)
        root_tile = ft.ListTile(
                leading=ft.Checkbox(
                    value=True, active_color=ft.Colors.PRIMARY,
                    on_change=lambda e, i=item_id: self._undo_completed(i),
                ),
                title=ft.Text(root["title"], size=17, weight=ft.FontWeight.W_600,
                              style=ft.TextStyle(
                                  decoration=ft.TextDecoration.LINE_THROUGH,
                                  color=ft.Colors.GREY),
                              max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                trailing=self._item_menu(item_id, bool(kids), done_ctx=True),
                min_height=58,
            )
        tiles = [self._dismiss_wrap(root_tile, item_id, done=True)]
        for k in kids:
            tiles.append(self._dismiss_wrap(self._done_child_row(k), k["id"], done=True))
        return ft.Column(tiles, spacing=0)

    def _done_child_row(self, it):
        item_id = it["id"]
        meta = []
        if it["deadline"]:
            meta.append(self._deadline_pill(it["deadline"]))
        return ft.ListTile(
            content_padding=ft.Padding(left=52, right=8, top=0, bottom=0),
            leading=ft.Icon(ft.Icons.CHECK_CIRCLE, size=20, color=ft.Colors.TEAL),
            title=ft.Text(it["title"], size=14,
                          style=ft.TextStyle(
                              decoration=ft.TextDecoration.LINE_THROUGH,
                              color=ft.Colors.GREY),
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=ft.Row(meta, spacing=6) if meta else None,
            trailing=self._item_menu(item_id, False, done_ctx=True),
            dense=True,
            min_height=46,
        )

    def _done_row(self, it, parent_title):
        item_id = it["id"]
        return self._dismiss_wrap(ft.ListTile(
            leading=ft.Icon(ft.Icons.CHECK_CIRCLE, size=20, color=ft.Colors.TEAL),
            title=ft.Text(it["title"], size=14,
                          style=ft.TextStyle(
                              decoration=ft.TextDecoration.LINE_THROUGH,
                              color=ft.Colors.GREY),
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=ft.Text(f"属于：{parent_title}", size=11,
                             color=ft.Colors.BLUE_GREY_400),
            trailing=self._item_menu(item_id, False, done_ctx=True),
            min_height=52,
        ), item_id, done=True)

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
        self._render()

    def _back(self, e=None):
        if self._show_done:
            self._close_done()
        elif self._ai_session_id is not None:
            self._close_ai_chat()
        elif self._ai_center:
            self._close_ai_center()
        elif self._search_mode:
            self._close_search()
        elif self._calendar_view:
            self._close_calendar()
        elif self.stack:
            self.stack.pop()
            self._render()

    def _on_key(self, e):
        key = getattr(e, "key", "")
        if getattr(e, "ctrl", False):
            return
        # 在 AI 对话/搜索输入框里，Delete/Backspace 是删字符，不应触发页面返回
        if key in ("Backspace", "Delete") and (
            self._ai_session_id is not None or self._search_mode
        ):
            return
        if key in ("Escape", "Backspace"):
            self._back()

    def _on_add(self, e):
        parent_id = self.stack[-1][0] if self.stack else None
        self._open_edit(parent_id=parent_id)

    # ---- 勾选完成：有子项需确认（防误触）；重复任务滚动；叶子直接完成 ----
    def _on_toggle(self, e, item_id):
        new_val = bool(e.control.value)
        if new_val and self.db.has_children(item_id):
            e.control.value = False          # 先回退勾选，等确认
            self.page.update()
            self._confirm_complete_group(item_id)
            return
        it = self.db.get(item_id)
        if new_val and (it.get("repeat_type") or ""):
            self._complete_recurring(item_id, it)
            return
        if new_val:
            self.db.set_done(item_id, True)
            self.db.log_completion(item_id)
        else:
            self.db.set_done(item_id, False)
        self._render()

    def _complete_recurring(self, item_id, it):
        """重复任务完成：滚动截止时间 + 重新武装（不进入完成区），并记完成日志。"""
        new_dl = next_deadline(
            it["deadline"], it.get("repeat_type", ""), it.get("repeat_interval", 1)
        )
        self.db.update(item_id, deadline=new_dl)
        self.db.log_completion(item_id)
        if new_dl:
            self._toast(f"已完成，下次：{fmt_deadline(new_dl)}")
        else:
            self._toast("已完成，明天继续")
        self._render()

    def _confirm_complete_group(self, item_id):
        it = self.db.get(item_id)
        if not it:
            return
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("整个项目都完成了吗？"),
            content=ft.Text(f"「{it['title']}」\n确认后将连同所有子任务一起移入完成区。"),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(content="确认完成", on_click=lambda e: self._do_complete_group(item_id)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _do_complete_group(self, item_id):
        self.page.pop_dialog()
        ids = self.db._subtree_ids(item_id)
        self.db.set_subtree_done(item_id, True)
        self.db.log_completions(ids)
        self._render()

    # ---- 完成区 ----
    def _open_done(self, e=None):
        self._show_done = True
        self._render()

    def _close_done(self, e=None):
        self._show_done = False
        self._render()

    def _undo_completed(self, item_id):
        """撤销完成：整组（含后代）恢复未完成，放回首页。"""
        self.db.set_subtree_done(item_id, False)
        self._render()

    def _all_done_items(self):
        """返回所有应处于完成区的条目 id（完成的大项目整组 + 进行中项目下完成的子任务）。"""
        ids = []
        for r in self.db.roots():
            if r["done"]:
                ids.extend(self.db._subtree_ids(r["id"]))
            else:
                for c in self.db.children(r["id"]):
                    if c["done"]:
                        ids.append(c["id"])
        return ids

    def _confirm_clear_all(self, e):
        ids = self._all_done_items()
        if not ids:
            self._toast("完成区是空的")
            return
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("清除全部已完成"),
            content=ft.Text(f"将删除完成区里的 {len(ids)} 个项目（含其子项目），确定？"),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(content="删除", on_click=lambda e: self._do_clear_all()),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _do_clear_all(self):
        self.page.pop_dialog()
        for r in self.db.roots():
            if r["done"]:
                self.db.delete(r["id"])            # 整组级联删除
            else:
                for c in self.db.children(r["id"]):
                    if c["done"]:
                        self.db.delete(c["id"])
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
        body = [
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
        ]

        # 备注
        self._note_field = ft.TextField(
            label="备注（可选）", value=(it.get("note", "") if it else ""),
            multiline=True, min_lines=2, max_lines=4,
        )
        body.append(self._note_field)

        # 标签选择器（仅第一层大项目可分配标签）
        self._tag_field = None
        is_root = (item_id is None and parent_id is None) or (
            item_id is not None and it is not None and it["parent_id"] is None
        )
        if is_root:
            current_tag = ""
            if it and it.get("tag_id"):
                t = self.db.tag_by_id(it["tag_id"])
                current_tag = t[0] if t else ""
            self._tag_field = ft.TextField(
                label="标签（可选）", value=current_tag,
                hint_text="输入标签名，或点下方已有标签",
            )
            quick = []
            for name, color_idx in self.db.all_tags():
                col = _tag_color(color_idx)
                quick.append(ft.OutlinedButton(
                    content=ft.Text(name, color=col),
                    on_click=lambda e, n=name: self._set_tag_field(n),
                ))
            body.append(self._tag_field)
            if quick:
                body.append(ft.Row(quick, scroll=ft.ScrollMode.AUTO, spacing=6))

        # 重复任务（仅叶子项）
        self._repeat_dropdown = None
        self._repeat_interval_field = None
        is_leaf = (item_id is None) or (it is not None and not self.db.has_children(item_id))
        if is_leaf:
            self._repeat_dropdown = ft.Dropdown(
                label="重复", value=(it.get("repeat_type", "") if it else ""),
                options=[
                    ft.dropdown.Option("", "不重复"),
                    ft.dropdown.Option("daily", "每天"),
                    ft.dropdown.Option("weekly", "每周"),
                    ft.dropdown.Option("interval", "每 N 天"),
                ],
            )
            self._repeat_interval_field = ft.TextField(
                label="间隔天数", value=str((it.get("repeat_interval", 1) if it else 1) or 1),
                keyboard_type=ft.KeyboardType.NUMBER, width=90,
            )
            body.append(ft.Row(
                [self._repeat_dropdown, self._repeat_interval_field],
                spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ))

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("编辑项目" if item_id else "新建项目"),
            content=ft.Column(body, tight=True, spacing=12),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(content="保存", on_click=self._save_edit),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _set_tag_field(self, name):
        if self._tag_field is not None:
            self._tag_field.value = name
            self._tag_field.update()

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
            item_id = self._editing_id
        else:
            item_id = self.db.add(self._target_parent, title, self._dl_state.to_str())
        if self._tag_field is not None:   # 仅第一层大项目有标签字段
            tag = (self._tag_field.value or "").strip()
            self.db.set_item_tag(item_id, tag or None)
        if self._note_field is not None:
            self.db.update(item_id, note=(self._note_field.value or "").strip())
        if self._repeat_dropdown is not None:
            rt = self._repeat_dropdown.value or ""
            ri = 0
            if rt == "interval":
                try:
                    ri = max(1, int((self._repeat_interval_field.value or "1").strip() or 1))
                except ValueError:
                    ri = 1
            self.db.update(item_id, repeat_type=rt, repeat_interval=ri)
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
                    ft.Text(f"版本 {APP_VERSION}", size=12, color=ft.Colors.BLUE_700,
                            weight=ft.FontWeight.BOLD),
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
                        leading=ft.Icon(ft.Icons.AUTO_AWESOME),
                        title=ft.Text("AI 设置"),
                        subtitle=ft.Text("接口地址 / API Key / 模型 / 测试连接", size=11),
                        on_click=self._open_ai_settings,
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

    def _open_ai_settings(self, e):
        self.page.pop_dialog()
        cfg = self.db.get_ai_config()
        base_field = ft.TextField(
            label="接口地址 (Base URL)", value=cfg.get("ai_base_url", ""),
            hint_text="https://api.deepseek.com/v1",
        )
        model_field = ft.TextField(
            label="模型名", value=cfg.get("ai_model", ""),
            hint_text="deepseek-chat",
        )
        key_field = ft.TextField(
            label="API Key",
            value=cfg.get("ai_api_key", ""),
            password=True,
            can_reveal_password=True,
            hint_text="sk-...（只保存在本机）",
        )
        status = ft.Text("", size=12, color=ft.Colors.BLUE_GREY_600)

        def save(e):
            self.db.set_ai_config(
                base_url=base_field.value, model=model_field.value,
                api_key=key_field.value,
            )
            self.page.pop_dialog()
            self._toast("AI 设置已保存")

        def test(e):
            # 只按当前输入测试，不落库（避免「只想试一下」却把填错的 Key 存进去）
            cfg2 = {
                "ai_base_url": (base_field.value or "").strip().rstrip("/"),
                "ai_model": (model_field.value or "").strip(),
                "ai_api_key": (key_field.value or "").strip(),
            }
            status.value = "正在测试连接…"
            self.page.update()
            self.page.run_task(self._test_ai_conn, cfg2, status)

        def clear_key(e):
            self.db.clear_ai_key()
            key_field.value = ""
            status.value = "API Key 已清除"
            self.page.update()

        dlg = ft.AlertDialog(
            modal=True,
            scrollable=True,
            title=ft.Text("AI 设置"),
            content=ft.Column(
                [
                    ft.Text("默认 DeepSeek，也兼容通义千问 / Kimi 等 OpenAI 兼容接口。",
                            size=12, color=ft.Colors.BLUE_GREY_600),
                    base_field,
                    model_field,
                    key_field,
                    status,
                ],
                tight=True,
                spacing=10,
            ),
            actions=[
                ft.TextButton("清除 Key", on_click=clear_key),
                ft.TextButton("测试连接", on_click=test),
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(content="保存", on_click=save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    async def _test_ai_conn(self, cfg, status):
        try:
            reply = await asyncio.to_thread(
                chat_completion,
                cfg["ai_base_url"], cfg.get("ai_api_key", ""), cfg["ai_model"],
                [{"role": "user", "content": "回复“连接成功”四个字"}],
                timeout=20,
            )
            status.value = f"连接成功：{reply[:60]}"
        except Exception as ex:
            status.value = f"失败：{ex}"
        self.page.update()

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
        action = None
        if action_label and on_undo:
            action = ft.SnackBarAction(
                label=action_label,
                on_click=lambda e: (on_undo() if on_undo else None),
            )
        sb = ft.SnackBar(
            content=ft.Text(msg),
            behavior=ft.SnackBarBehavior.FLOATING,
            duration=3000,
            action=action,
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
