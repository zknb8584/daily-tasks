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
import time

import flet as ft

from ai_client import (
    AI_SKILLS,
    GRILL_PROGRESS_ITEMS,
    SKILL_BY_ID,
    build_group_role_system,
    build_role_system,
    chat_completion,
    extract_bracket_directives,
    extract_load_requests,
    extract_role_card,
    extract_scene_change,
    extract_tasks,
    format_group_history,
    has_remember_directive,
    match_world_book,
    parse_role_card,
    parse_grill_state,
    parse_state_block,
    post_history_instructions,
    role_greeting,
    select_group_speakers,
    tavern_to_role_card,
)
from models import DATA_DIR, Database, fmt_deadline, get_quotes, next_deadline, parse_deadline, save_quotes
from notifications import Notifier, notify

APP_NAME = "天野陽菜"
APP_VERSION = "v1.5.3"      # 每次构建手动递增，便于确认手机上是哪个包
DATE_FMT = "%Y-%m-%d"
DATETIME_FMT = "%Y-%m-%d %H:%M"

AI_CATEGORIES = [
    ("生成任务树", ["grill_decompose", "quick_decompose"]),
    ("回答与解惑", ["general_chat", "study_help"]),
    ("角色扮演", ["roleplay", "role_grill"]),
]


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
        self._ai_category = None        # AI 中心当前选中的板块名
        self._ai_session_id = None      # 当前打开的 AI 会话 id
        self._ai_input = None           # 对话输入框（保持引用避免失焦）
        self._ai_busy = False           # 是否正在等待 AI 回复
        self._ai_group_id = None        # 当前打开的群聊 id
        self._group_input = None        # 群聊输入框
        self._group_busy = False        # 是否正在等待群聊 AI 回复
        self._pending_import = None     # 等待绑定世界观的导入角色卡
        self._group_loaded_sections = {}  # 群聊里各角色已加载的角色卡段
        self._role_loaded_sections = {} # roleplay 会话已加载的角色卡段
        self._swipe_armed = {}          # 两段式滑动：记录是否已完成第一次滑动
        self._swipe_armed_at = {}       # 两段式滑动：第一次滑动的触发时间
        self._swipe_blocked = {}        # 反向拖动取消后，本次手势不再自动换操作
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
        elif self._ai_group_id is not None:
            self._update_appbar(None, self._group_title(), ai_group=True)
            self.scroll.controls = self._render_group_chat()
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
            or self._ai_group_id is not None
        ))
        self.page.update()

    def _update_appbar(self, parent_id, title, done=False, search=False,
                       calendar=False, ai=False, ai_chat=False, ai_group=False):
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
                icon_color=ft.Colors.ON_PRIMARY,
                on_click=self._close_ai_center_or_category,
            )
            self.page.appbar.title = ft.Text(
                self._ai_category or "AI"
            )
            self.page.appbar.actions = [self._settings_icon()]
        elif ai_chat:
            self.page.appbar.leading = ft.IconButton(
                icon=ft.Icons.ARROW_BACK, tooltip="返回",
                icon_color=ft.Colors.ON_PRIMARY, on_click=self._close_ai_chat,
            )
            self.page.appbar.title = ft.Text(title)
            actions = [self._settings_icon()]
            if self._ai_session_id:
                sess = self.db.get_ai_session(self._ai_session_id)
                if sess and sess["skill_id"] == "roleplay" and sess.get("role_card_id"):
                    card_id = sess["role_card_id"]
                    actions.insert(0, ft.IconButton(
                        icon=ft.Icons.INFO_OUTLINE,
                        tooltip="角色详情",
                        icon_color=ft.Colors.ON_PRIMARY,
                        on_click=lambda e: self._open_role_details(card_id),
                    ))
            self.page.appbar.actions = actions
        elif ai_group:
            self.page.appbar.leading = ft.IconButton(
                icon=ft.Icons.ARROW_BACK, tooltip="返回",
                icon_color=ft.Colors.ON_PRIMARY, on_click=self._close_group_chat,
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
        self._ai_category = None
        self._render()

    def _close_ai_center(self, e=None):
        self._ai_center = False
        self._ai_category = None
        self._render()

    def _select_ai_category(self, category):
        self._ai_category = category
        self._render()

    def _close_ai_category(self, e=None):
        self._ai_category = None
        self._render()

    def _close_ai_center_or_category(self, e=None):
        if self._ai_category:
            self._close_ai_category()
        else:
            self._close_ai_center()

    def _render_ai_center(self):
        controls = [
            ft.Container(
                margin=ft.Margin(top=8, left=12, right=12, bottom=4),
                content=ft.Text(
                    "选择一个板块，进入后查看功能和聊天记录",
                    size=13, color=ft.Colors.BLUE_GREY_600,
                ),
            )
        ]
        if not self._ai_category:
            category_desc = {
                "生成任务树": "AI 拷问拆解 / 快速拆解 / 任务树挂载",
                "回答与解惑": "通用问答 / 课堂速解",
                "角色扮演": "角色聊天 / 群聊 / 角色卡生成",
            }
            for category, _ in AI_CATEGORIES:
                controls.append(ft.ListTile(
                    leading=ft.Icon(
                        ft.Icons.FOLDER_OPEN,
                        color=ft.Colors.BLUE_700,
                    ),
                    title=ft.Text(category, weight=ft.FontWeight.W_600, size=17),
                    subtitle=ft.Text(
                        category_desc.get(category, ""),
                        size=12, color=ft.Colors.BLUE_GREY_500,
                    ),
                    trailing=ft.Icon(
                        ft.Icons.CHEVRON_RIGHT,
                        color=ft.Colors.BLUE_GREY_400,
                    ),
                    on_click=lambda e, c=category: self._select_ai_category(c),
                    min_height=72,
                ))
            return controls
        sessions = self.db.list_ai_sessions()
        groups = self.db.list_group_chats()
        role_cards = self.db.list_role_cards()
        for category, skill_ids in AI_CATEGORIES:
            if category != self._ai_category:
                continue
            controls.append(ft.Container(
                margin=ft.Margin(top=16, left=12, right=12, bottom=4),
                content=ft.Text(category, size=13, weight=ft.FontWeight.BOLD,
                                color=ft.Colors.BLUE_GREY_700),
            ))
            for skill_id in skill_ids:
                skill = SKILL_BY_ID.get(skill_id)
                if not skill:
                    continue
                if skill_id == "roleplay":
                    controls.append(ft.ListTile(
                        leading=ft.Icon(ft.Icons.PERSON, color=ft.Colors.INDIGO_700),
                        title=ft.Text(skill["name"], weight=ft.FontWeight.W_600),
                        subtitle=ft.Text(skill["description"], size=12),
                        trailing=ft.Icon(ft.Icons.ADD_CIRCLE_OUTLINE,
                                         color=ft.Colors.INDIGO_700),
                        on_click=lambda e: self._choose_roleplay(),
                        min_height=64,
                    ))
                else:
                    controls.append(ft.ListTile(
                        leading=ft.Icon(ft.Icons.AUTO_AWESOME,
                                        color=ft.Colors.BLUE_700),
                        title=ft.Text(skill["name"], weight=ft.FontWeight.W_600),
                        subtitle=ft.Text(skill["description"], size=12),
                        trailing=ft.Icon(ft.Icons.ADD_CIRCLE_OUTLINE,
                                         color=ft.Colors.BLUE_700),
                        on_click=lambda e, sid=skill_id: (
                            self._start_role_grill() if sid == "role_grill"
                            else self._start_ai_session(sid)
                        ),
                        min_height=64,
                    ))

            if category == "角色扮演":
                controls.append(ft.Container(
                    margin=ft.Margin(top=12, left=12, right=12, bottom=2),
                    content=ft.Text("角色聊天", size=12,
                                    weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.BLUE_GREY_500),
                ))
                if not role_cards:
                    controls.append(self._hint_text("还没有角色，先导入或生成一个"))
                for card in role_cards:
                    controls.append(self._role_chat_row(card))
                controls.append(ft.TextButton(
                    content="导入角色卡",
                    icon=ft.Icons.UPLOAD_FILE,
                    on_click=lambda e: self.page.run_task(self._import_role_card, None),
                ))
                controls.append(ft.TextButton(
                    content="AI 生成角色卡",
                    icon=ft.Icons.AUTO_AWESOME,
                    on_click=lambda e: self._open_role_card_generator(None),
                ))
                controls.append(ft.TextButton(
                    content="Grill-me 拷问生成",
                    icon=ft.Icons.QUESTION_ANSWER,
                    on_click=lambda e: self._start_role_grill(None),
                ))
                controls.append(ft.Container(
                    margin=ft.Margin(top=12, left=12, right=12, bottom=2),
                    content=ft.Text("我的群聊", size=12,
                                    weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.BLUE_GREY_500),
                ))
                controls.append(ft.TextButton(
                    content="新建群聊",
                    icon=ft.Icons.GROUPS,
                    on_click=lambda e: self._open_group_creator(),
                ))
                if not groups:
                    controls.append(self._hint_text("还没有群聊"))
                for g in groups:
                    controls.append(self._group_chat_row(g))

            cat_sessions = [
                s for s in sessions if s["skill_id"] in skill_ids
            ]
            if cat_sessions:
                controls.append(ft.Container(
                    margin=ft.Margin(top=12, left=12, right=12, bottom=2),
                    content=ft.Text(f"{category}记录", size=12,
                                    weight=ft.FontWeight.BOLD,
                                    color=ft.Colors.BLUE_GREY_500),
                ))
                for sess in cat_sessions:
                    controls.append(self._session_chat_row(sess))
        return controls

    def _session_chat_row(self, sess):
        skill = SKILL_BY_ID.get(sess["skill_id"])
        skill_name = skill["name"] if skill else sess["skill_id"]
        return ft.ListTile(
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
        )

    def _group_chat_row(self, g):
        return ft.ListTile(
            leading=ft.Icon(ft.Icons.FORUM, color=ft.Colors.INDIGO_700),
            title=ft.Text(g["title"], max_lines=1,
                          overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=ft.Text(
                f"{g['member_count']} 个角色 · {g['updated_at']}",
                size=11, color=ft.Colors.BLUE_GREY_400,
            ),
            trailing=ft.PopupMenuButton(
                icon=ft.Icons.MORE_VERT,
                icon_color=ft.Colors.BLUE_GREY_500,
                items=[
                    ft.PopupMenuItem(
                        content=ft.Text("继续"),
                        on_click=lambda e, gid=g["id"]: self._open_group_chat(gid),
                    ),
                    ft.PopupMenuItem(
                        content=ft.Text("删除"),
                        on_click=lambda e, gid=g["id"]: self._confirm_delete_group(gid),
                    ),
                ],
            ),
            on_click=lambda e, gid=g["id"]: self._open_group_chat(gid),
            min_height=58,
        )

    def _role_chat_row(self, card):
        world = self.db.get_world(card.get("world_id")) if card.get("world_id") else None
        subtitle = world["name"] if world else "未绑定世界观"
        return ft.ListTile(
            leading=ft.Icon(ft.Icons.PERSON, color=ft.Colors.INDIGO_700),
            title=ft.Text(card["name"], max_lines=1,
                          overflow=ft.TextOverflow.ELLIPSIS),
            subtitle=ft.Text(subtitle, size=11,
                             color=ft.Colors.BLUE_GREY_400),
            trailing=ft.PopupMenuButton(
                icon=ft.Icons.MORE_VERT,
                icon_color=ft.Colors.BLUE_GREY_500,
                items=[
                    ft.PopupMenuItem(
                        content=ft.Text("查看详情"),
                        on_click=lambda e, cid=card["id"]: self._open_role_details(cid),
                    ),
                    ft.PopupMenuItem(
                        content=ft.Text("删除"),
                        on_click=lambda e, cid=card["id"]: self._confirm_delete_role_card(cid),
                    ),
                ],
            ),
            on_click=lambda e, cid=card["id"]: self._begin_roleplay(cid),
            min_height=58,
        )

    def _delete_ai_session(self, session_id):
        self.db.delete_ai_session(session_id)
        self._render()

    # ---------- AI 群聊 ----------
    def _open_group_creator(self, e=None):
        cards = self.db.list_role_cards()
        if len(cards) < 2:
            self._toast("请先导入至少两个角色卡")
            return
        title_field = ft.TextField(
            label="群聊名称",
            hint_text="例如：周末小队",
        )
        checks = [
            (card["id"], ft.Checkbox(label=card["name"], value=False))
            for card in cards[:20]
        ]
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("新建群聊"),
            content=ft.Column(
                [title_field, *[cb for _, cb in checks]],
                tight=True,
                spacing=8,
                scroll=ft.ScrollMode.AUTO,
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(
                    content="创建群聊",
                    on_click=lambda e: self._create_group_chat(
                        title_field, checks, dlg
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _create_group_chat(self, title_field, checks, dlg):
        selected = [card_id for card_id, cb in checks if cb.value]
        if not 2 <= len(selected) <= 4:
            self._toast("请选择 2~4 个角色")
            return
        title = (title_field.value or "").strip() or "新群聊"
        group_id = self.db.create_group_chat(title)
        for card_id in selected:
            self.db.add_group_member(group_id, card_id)
        self.page.pop_dialog()
        self._open_group_chat(group_id)

    def _confirm_delete_group(self, group_id):
        group = self.db.get_group_chat(group_id)
        if not group:
            return
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("删除群聊"),
            content=ft.Text(f"将删除「{group['title']}」及其全部群消息，确定？"),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(
                    content="删除",
                    on_click=lambda e: self._delete_group_chat(group_id),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _delete_group_chat(self, group_id):
        self.page.pop_dialog()
        self.db.delete_group_chat(group_id)
        if self._ai_group_id == group_id:
            self._ai_group_id = None
            self._group_input = None
        self._ai_center = True
        self._render()

    def _open_group_chat(self, group_id):
        self._ai_group_id = group_id
        self._ai_center = False
        self._ai_session_id = None
        self._ai_input = None
        self._group_input = None
        self._render()

    def _close_group_chat(self, e=None):
        self._ai_group_id = None
        self._group_input = None
        self._ai_center = True
        self._render()

    def _group_title(self):
        g = self.db.get_group_chat(self._ai_group_id) if self._ai_group_id else None
        return g["title"] if g else "群聊"

    def _render_group_chat(self):
        g = self.db.get_group_chat(self._ai_group_id) if self._ai_group_id else None
        if not g:
            return [self._hint_text("群聊不存在")]
        members = self.db.group_members(g["id"])
        member_names = []
        for m in members:
            label = m["name"]
            if m.get("world_id"):
                world = self.db.get_world(m["world_id"])
                if world:
                    label += f"（{world['name']}）"
            member_names.append(label)
        scene = self.db.get_group_scene(g["id"])
        header_text = "群聊成员：" + "、".join(member_names)
        if scene:
            header_text += f"\n当前场景：{scene}"
        rows = [
            ft.Container(
                margin=ft.Margin(top=8, left=12, right=12, bottom=4),
                content=ft.Text(
                    header_text,
                    size=12,
                    color=ft.Colors.BLUE_GREY_600,
                ),
            )
        ]
        messages = self.db.get_group_messages(g["id"])
        for i, msg in enumerate(messages):
            label = "你" if msg["role"] == "user" else (msg.get("role_name") or "AI")
            rows.append(self._ai_message_row(
                msg,
                is_last=(i == len(messages) - 1),
                kind="chat",
                role_label=label,
            ))

        if self._group_input is None:
            self._group_input = ft.TextField(
                hint_text="发到群聊，可用 @角色名 指定",
                expand=True,
                min_lines=1,
                max_lines=3,
                on_submit=self._send_group_message,
                disabled=self._group_busy,
            )
        else:
            self._group_input.disabled = self._group_busy
        rows.append(ft.Container(
            padding=ft.Padding(left=12, right=12, top=8, bottom=8),
            content=ft.Row(
                [
                    self._group_input,
                    ft.IconButton(
                        icon=ft.Icons.HELP_OUTLINE,
                        icon_color=ft.Colors.BLUE_700,
                        tooltip="指令",
                        on_click=self._show_directive_help,
                    ),
                    ft.IconButton(
                        icon=ft.Icons.SEND,
                        icon_color=ft.Colors.WHITE,
                        bgcolor=ft.Colors.BLUE_700,
                        tooltip="发送",
                        disabled=self._group_busy,
                        on_click=self._send_group_message,
                    ),
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.END,
            ),
        ))
        return rows

    async def _send_group_message(self, e):
        if self._ai_group_id is None or self._group_busy:
            return
        text = (self._group_input.value or "").strip() if self._group_input is not None else ""
        if not text:
            return
        group_id = self._ai_group_id
        _, directives = extract_bracket_directives(text)
        auto_rounds = 2 if any("自己聊" in d for d in directives) else 0
        self.db.append_group_message(group_id, "user", text)
        if self._group_input is not None:
            self._group_input.value = ""
        self._group_busy = True
        self._render()
        try:
            replies = await self._group_reply(group_id, text)
            for role_card_id, role_name, clean in replies:
                if clean:
                    self.db.append_group_message(
                        group_id, "assistant", clean,
                        role_name=role_name, role_card_id=role_card_id,
                    )
            for _ in range(auto_rounds):
                extra = await self._group_reply(group_id, "（继续群聊）")
                for role_card_id, role_name, clean in extra:
                    if clean:
                        self.db.append_group_message(
                            group_id, "assistant", clean,
                            role_name=role_name, role_card_id=role_card_id,
                        )
        except Exception as ex:
            self._toast(f"群聊 AI 错误：{ex}")
        finally:
            self._group_busy = False
            self._render()

    async def _group_reply(self, group_id, user_text):
        members = self.db.group_members(group_id)
        if not members:
            return []
        history = self.db.get_group_messages(group_id, limit=80)
        user_clean, user_directives = extract_bracket_directives(user_text)
        remember_directive = has_remember_directive(user_text)
        group_scene = self.db.get_group_scene(group_id)
        scene_change = extract_scene_change(user_directives)
        if scene_change:
            group_scene = scene_change
            self.db.set_group_scene(group_id, group_scene)
        if remember_directive:
            for m in members:
                state = self.db.role_card_state(m["id"])
                directive_text = "\n".join(user_directives)
                state["记忆"] = (
                    state.get("记忆", "") + "\n" + directive_text
                ).strip()
                state["重要记忆"] = (
                    state.get("重要记忆", "") + "\n" + directive_text
                ).strip()
                self.db.save_role_card_state(m["id"], state)

        speakers = select_group_speakers(members, user_clean or user_text, history)
        if any(
            d.startswith("只让") or d.startswith("仅")
            for d in user_directives
        ):
            speakers = [
                m for m in members if f"@{m['name']}" in user_text
            ][:1]
        context = format_group_history(history)
        replies = []
        cfg = self.db.get_ai_config()
        for card in speakers[:3]:
            loaded = set(self._group_loaded_sections.get((group_id, card["id"]), set()))
            final_reply = None
            for _ in range(3):
                loaded.update(match_world_book(card["content"], user_text))
                self._group_loaded_sections[(group_id, card["id"])] = loaded
                state = self.db.role_card_state(card["id"])
                system = build_group_role_system(
                    card["content"], state, loaded, members, context
                )
                if user_directives:
                    system += (
                        "\n\n[导演指令]\n"
                        + "\n".join(f"- {d}" for d in user_directives)
                    )
                if group_scene:
                    system += f"\n\n[当前场景]\n{group_scene}"
                if remember_directive:
                    system += (
                        "\n\n注意：用户本轮明确要求你记住某些内容。"
                        "请把它逐字保留到 ---STATE--- 的 重要记忆= 字段。"
                    )
                post = post_history_instructions(card["content"])
                if post:
                    system += f"\n\n[历史后置指令]\n{post}"
                payload = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "请根据上面的群聊消息继续说话。"},
                ]
                reply = await asyncio.to_thread(
                    chat_completion,
                    cfg["ai_base_url"], cfg["ai_api_key"], cfg["ai_model"],
                    payload,
                )
                clean, loads = extract_load_requests(reply)
                if not loads:
                    final_reply = clean
                    break
                loaded.update(loads)
                self._group_loaded_sections[(group_id, card["id"])] = loaded
            if not final_reply:
                continue
            clean, state = parse_state_block(final_reply)
            if state:
                self._merge_role_state(card["id"], state)
            if clean:
                state = self.db.role_card_state(card["id"])
                group_memory = state.get("群聊记忆", "")
                state["群聊记忆"] = (
                    group_memory + "\n群聊中你提到：" + clean[:120]
                ).strip()
                self.db.save_role_card_state(card["id"], state)
                replies.append((card["id"], card["name"], clean))
                context += f"\n{card['name']}：{clean}"
        return replies

    # ---------- 角色扮演：角色卡 ----------
    def _choose_roleplay(self):
        cards = self.db.list_role_cards()
        dropdown = ft.Dropdown(
            label="选择角色卡",
            options=[ft.dropdown.Option(key=c["id"], text=c["name"]) for c in cards],
            value=cards[0]["id"] if cards else None,
        )
        card_menu = ft.PopupMenuButton(
            icon=ft.Icons.MORE_VERT,
            tooltip="角色卡管理",
            items=[
                ft.PopupMenuItem(
                    content=ft.Text("编辑角色卡"),
                    on_click=lambda e: self._edit_selected_role_card(dropdown, dlg),
                ),
                ft.PopupMenuItem(
                    content=ft.Text("世界观档案"),
                    on_click=lambda e: self._open_world_manager(dlg),
                ),
                ft.PopupMenuItem(
                    content=ft.Text("删除所选角色卡"),
                    on_click=lambda e: self._delete_selected_role_card(dropdown, dlg),
                ),
            ],
        ) if cards else None
        status = ft.Text(
            "还没有角色卡，先导入一份角色卡文件（txt 或 json）",
            size=12, color=ft.Colors.BLUE_GREY_600,
        ) if not cards else ft.Text("", size=12)
        relation_dd = ft.Dropdown(
            label="初始身份",
            value="陌生人",
            options=[
                ft.dropdown.Option(key="陌生人", text="陌生人"),
                ft.dropdown.Option(key="同学", text="同学"),
                ft.dropdown.Option(key="朋友", text="朋友"),
                ft.dropdown.Option(key="同事", text="同事"),
                ft.dropdown.Option(key="师生", text="师生"),
                ft.dropdown.Option(key="家人", text="家人"),
                ft.dropdown.Option(key="恋人", text="恋人"),
                ft.dropdown.Option(key="青梅竹马", text="青梅竹马"),
            ],
        )
        affection_dd = ft.Dropdown(
            label="初始好感度",
            value="0 中立",
            options=[
                ft.dropdown.Option(key="-20 疏远", text="-20 疏远"),
                ft.dropdown.Option(key="0 中立", text="0 中立"),
                ft.dropdown.Option(key="30 友好", text="30 友好"),
                ft.dropdown.Option(key="60 亲近", text="60 亲近"),
                ft.dropdown.Option(key="90 信赖", text="90 信赖"),
                ft.dropdown.Option(key="120 亲密", text="120 亲密"),
                ft.dropdown.Option(key="180 依恋", text="180 依恋"),
            ],
        )
        affection_by_relation = {
            "陌生人": "0 中立",
            "同学": "30 友好",
            "朋友": "60 亲近",
            "同事": "30 友好",
            "师生": "30 友好",
            "家人": "90 信赖",
            "恋人": "120 亲密",
            "青梅竹马": "90 信赖",
        }

        def _sync_affection(e):
            affection_dd.value = affection_by_relation.get(
                relation_dd.value, affection_dd.value
            )
            self.page.update()

        relation_dd.on_change = _sync_affection
        actions = [
            ft.TextButton(
                "导入角色卡",
                on_click=lambda e: self.page.run_task(self._import_role_card, dlg),
            ),
            ft.TextButton(
                "AI 生成角色卡",
                on_click=lambda e: self._open_role_card_generator(dlg),
            ),
            ft.TextButton(
                "Grill-me 拷问生成",
                on_click=lambda e: self._start_role_grill(dlg),
            ),
            ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
            ft.FilledButton(
                content="开始对话",
                on_click=lambda e: self._begin_roleplay(
                    dropdown.value, dlg, relation_dd.value, affection_dd.value
                ),
            ),
        ]
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("开始角色扮演"),
            content=ft.Column(
                [
                    status,
                    ft.Row(
                        [
                            dropdown if cards else ft.Text(
                                "暂无角色卡", size=13, color=ft.Colors.GREY
                            ),
                            card_menu if card_menu else ft.Container(),
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    relation_dd if cards else ft.Text(""),
                    affection_dd if cards else ft.Text(""),
                ],
                tight=True,
                spacing=10,
            ),
            actions=actions,
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _open_role_card_generator(self, dlg):
        if dlg is not None:
            self.page.pop_dialog()
        desc = ft.TextField(
            label="描述你想要的角色",
            hint_text="例如：一个叫阿晴的温柔女生，住在海边，说话很短，有点傲娇",
            multiline=True,
            min_lines=3,
            max_lines=6,
        )
        status = ft.Text("", size=12, color=ft.Colors.BLUE_GREY_600)
        gen_dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("AI 生成角色卡"),
            content=ft.Column([desc, status], tight=True, spacing=10),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(
                    content="生成角色卡",
                    on_click=lambda e: self.page.run_task(
                        self._do_generate_role_card, desc, status, gen_dlg
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(gen_dlg)

    def _start_role_grill(self, dlg=None):
        if dlg is not None:
            self.page.pop_dialog()
        worlds = self.db.list_worlds()
        world_dd = ft.Dropdown(
            label="选择世界观",
            value="",
            options=[
                ft.dropdown.Option(key="", text="暂不绑定"),
                *[
                    ft.dropdown.Option(key=str(w["id"]), text=w["name"])
                    for w in worlds
                ],
            ],
        )
        new_world_field = ft.TextField(
            label="或新建世界观",
            hint_text="填写后自动新建",
        )
        start_dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Grill-me 拷问角色卡"),
            content=ft.Column(
                [
                    ft.Text("可以先选择世界观，也可以直接开始拷问：", size=13,
                            color=ft.Colors.BLUE_GREY_600),
                    world_dd,
                    new_world_field,
                ],
                tight=True,
                spacing=10,
            ),
            actions=[
                ft.TextButton(
                    "取消",
                    on_click=lambda e: self._cancel_role_grill_start(),
                ),
                ft.FilledButton(
                    content="开始拷问",
                    on_click=lambda e: self._begin_role_grill(
                        world_dd, new_world_field, start_dlg
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(start_dlg)

    def _cancel_role_grill_start(self):
        self.page.pop_dialog()
        self._ai_center = True
        self._render()

    def _begin_role_grill(self, world_dd, new_world_field, dlg):
        world_id = int(world_dd.value) if world_dd and world_dd.value else None
        new_world_name = (
            (new_world_field.value or "").strip()
            if new_world_field is not None else ""
        )
        if new_world_name:
            world_id = self.db.create_world(new_world_name, "")
        self.page.pop_dialog()
        self._start_ai_session("role_grill")
        if world_id:
            meta = self.db.get_ai_session_meta(self._ai_session_id)
            meta["world_id"] = world_id
            world = self.db.get_world(world_id)
            if world:
                meta["summary"] = f"世界观：{world['name']}\n{world['content']}"
            self.db.set_ai_session_meta(self._ai_session_id, meta)

    def _attach_world_to_content(self, content, world_id):
        world = self.db.get_world(world_id) if world_id else None
        if world:
            body = (world["name"] + "\n" + world["content"]).strip()
            content = self._replace_role_section(content, "世界观", body)
        return content

    def _replace_role_section(self, content, section, body):
        lines = []
        in_section = False
        replaced = False
        for line in (content or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_section = stripped[1:-1].strip() == section
                if in_section:
                    replaced = True
                    continue
            if in_section:
                continue
            lines.append(line)
        new_content = "\n".join(lines).rstrip()
        if replaced or body:
            new_content += f"\n\n[{section}]\n{body}"
        return new_content.strip()

    def _prompt_import_world(self, name, content):
        self._pending_import = {"name": name, "content": content}
        worlds = self.db.list_worlds()
        world_dd = ft.Dropdown(
            label="绑定世界观",
            value="",
            options=[
                ft.dropdown.Option(key="", text="暂不绑定"),
                *[
                    ft.dropdown.Option(key=str(w["id"]), text=w["name"])
                    for w in worlds
                ],
            ],
        )
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("导入角色卡"),
            content=ft.Column(
                [
                    ft.Text(f"已解析角色：{name}", size=13,
                            color=ft.Colors.BLUE_GREY_600),
                    world_dd,
                ],
                tight=True,
                spacing=10,
            ),
            actions=[
                ft.TextButton(
                    "取消",
                    on_click=lambda e: self._cancel_import_world(),
                ),
                ft.FilledButton(
                    content="导入",
                    on_click=lambda e: self._create_imported_role_card(
                        world_dd, dlg
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _cancel_import_world(self):
        self.page.pop_dialog()
        self._pending_import = None
        self._ai_center = True
        self._render()

    def _create_imported_role_card(self, world_dd, dlg=None):
        if not self._pending_import:
            return
        name = self._pending_import["name"]
        content = self._pending_import["content"]
        world_id = int(world_dd.value) if world_dd and world_dd.value else None
        content = self._attach_world_to_content(content, world_id)
        self.db.create_role_card(name, content, world_id)
        self._pending_import = None
        if dlg is not None:
            self.page.pop_dialog()
        self._toast(f"已导入角色卡：{name}")
        self._ai_center = True
        self._render()

    async def _do_generate_role_card(self, desc_field, status, gen_dlg=None):
        desc = (desc_field.value or "").strip()
        if not desc:
            status.value = "请先描述你想要的角色"
            self.page.update()
            return
        cfg = self.db.get_ai_config()
        if not cfg.get("ai_api_key"):
            status.value = "请先在「设置 → AI 设置」填写 API Key"
            self.page.update()
            return
        status.value = "正在生成角色卡…"
        self.page.update()
        try:
            system = (
                "你是一个角色卡创作者。根据用户的一句话描述，生成一份固定格式的角色卡文本，"
                "只使用这些段名："
                "[核心] [背景] [爱好] [说话风格] [关系] [扩展] [开场白] [示例对话]。"
                "[核心] 第一行写“名字：xxx”，第二行写一句话人设。"
                "[爱好] 写 2~4 个具体爱好，方便角色主动发起话题。"
                "[开场白] 写角色第一次见到用户时会说的话，1~3 句。"
                "[示例对话] 写 2~3 轮最能体现角色语气、反应方式和口头禅的对话，"
                "格式用“用户：”和“角色名：”分行。"
                "内容要具体、有记忆点，语气符合角色设定，不要出现“作为AI”之类的话，"
                "不要输出 Markdown 代码块，也不要输出多余解释。"
            )
            reply = await asyncio.to_thread(
                chat_completion,
                cfg["ai_base_url"], cfg["ai_api_key"], cfg["ai_model"],
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": desc},
                ],
            )
            content = reply.strip()
            if content.startswith("```"):
                content = content.strip("`")
                content = content.split("\n", 1)[-1].strip()
            sections = parse_role_card(content)
            if not sections.get("核心"):
                raise ValueError("AI 生成的格式不完整")
            first_line = sections["核心"].splitlines()[0].strip()
            name = first_line.split("：", 1)[-1].strip() if "：" in first_line else desc[:20]
            name = name or "AI 角色"
            self.db.create_role_card(name, content)
            self._toast(f"已生成角色卡：{name}")
            if gen_dlg is not None:
                self.page.pop_dialog()
            self._ai_center = True
            self._render()
        except Exception as ex:
            status.value = f"生成失败：{ex}"
            self.page.update()

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
                ft.TextButton(
                    "取消",
                    on_click=lambda e: self._cancel_role_card_editor(),
                ),
                ft.FilledButton(
                    content="删除",
                    on_click=lambda e: self._do_delete_role_card(card_id),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(confirm)

    def _do_delete_role_card(self, card_id, reopen_choose=True):
        self.page.pop_dialog()
        for sess in self.db.list_ai_sessions():
            if sess.get("role_card_id") == card_id:
                self.db.delete_ai_session(sess["id"])
        self.db.delete_role_card(card_id)
        self._toast("角色卡已删除")
        if reopen_choose:
            pass
        self._ai_center = True
        self._render()

    def _confirm_delete_role_card(self, card_id):
        card = self.db.get_role_card(card_id)
        if not card:
            return
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("删除角色卡"),
            content=ft.Text(f"将删除「{card['name']}」及其全部聊天记录，确定？"),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(
                    content="删除",
                    on_click=lambda e: self._do_delete_role_card(
                        card_id, reopen_choose=False
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _open_role_details(self, card_id):
        card = self.db.get_role_card(card_id)
        if not card:
            return
        world = self.db.get_world(card.get("world_id")) if card.get("world_id") else None
        state = self.db.role_card_state(card_id)
        parts = [
            f"名字：{card['name']}",
            f"世界观：{world['name'] if world else '未绑定'}",
            f"身份：{state.get('身份', '未设定')}",
            f"好感度：{state.get('好感度', '未建立')}",
            f"情绪：{state.get('当前情绪', '平静')}",
            "",
            card["content"],
        ]
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("角色详情"),
            content=ft.Text("\n".join(parts), size=13, selectable=True),
            actions=[
                ft.TextButton("关闭", on_click=lambda e: self.page.pop_dialog()),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _edit_selected_role_card(self, dropdown, dlg):
        card_id = dropdown.value if dropdown is not None else None
        if not card_id:
            self._toast("请先选择角色卡")
            return
        card = self.db.get_role_card(card_id)
        if not card:
            return
        if dlg is not None:
            self.page.pop_dialog()
        worlds = self.db.list_worlds()
        name_field = ft.TextField(label="角色名", value=card["name"])
        content_field = ft.TextField(
            label="角色卡内容",
            value=card["content"],
            multiline=True,
            min_lines=10,
            max_lines=18,
        )
        world_dd = ft.Dropdown(
            label="世界观",
            value=str(card.get("world_id") or ""),
            options=[
                ft.dropdown.Option(key="", text="暂不绑定"),
                *[
                    ft.dropdown.Option(key=str(w["id"]), text=w["name"])
                    for w in worlds
                ],
            ],
        )
        editor = ft.AlertDialog(
            modal=True,
            title=ft.Text("编辑角色卡"),
            content=ft.Column(
                [name_field, world_dd, content_field],
                tight=True,
                spacing=8,
                scroll=ft.ScrollMode.AUTO,
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(
                    content="保存",
                    on_click=lambda e: self._save_role_card_edit(
                        card_id, name_field, content_field, world_dd, editor
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(editor)

    def _cancel_role_card_editor(self):
        self.page.pop_dialog()
        self._ai_center = True
        self._render()

    def _save_role_card_edit(self, card_id, name_field, content_field,
                             world_dd, dlg):
        world_id = int(world_dd.value) if world_dd.value else None
        content = content_field.value or ""
        if world_id:
            content = self._attach_world_to_content(content, world_id)
        self.db.update_role_card(
            card_id,
            name=name_field.value or "角色卡",
            content=content,
            world_id=world_id,
        )
        self.page.pop_dialog()
        self._toast("角色卡已保存")
        self._ai_center = True
        self._render()

    def _open_world_manager(self, dlg=None):
        if dlg is not None:
            self.page.pop_dialog()
        worlds = self.db.list_worlds()
        world_dd = ft.Dropdown(
            label="选择世界观",
            value=str(worlds[0]["id"]) if worlds else None,
            options=[
                ft.dropdown.Option(
                    key=str(w["id"]),
                    text=f"{w['name']}（{w['card_count']}张卡）",
                )
                for w in worlds
            ],
        )
        status = ft.Text(
            "还没有世界观档案",
            size=12,
            color=ft.Colors.BLUE_GREY_600,
        ) if not worlds else ft.Text("", size=12)
        manager = ft.AlertDialog(
            modal=True,
            title=ft.Text("世界观档案"),
            content=ft.Column(
                [
                    status,
                    world_dd if worlds else ft.Text("暂无世界观", size=13,
                                                   color=ft.Colors.GREY),
                ],
                tight=True,
                spacing=10,
            ),
            actions=[
                ft.TextButton(
                    "返回",
                    on_click=lambda e: self._close_world_manager(),
                ),
                ft.TextButton(
                    "新建",
                    on_click=lambda e: self._open_world_editor(None),
                ),
                ft.TextButton(
                    "编辑",
                    on_click=lambda e: self._open_world_editor(
                        int(world_dd.value) if world_dd.value else None
                    ),
                ),
                ft.TextButton(
                    "删除",
                    on_click=lambda e: self._delete_world(
                        int(world_dd.value) if world_dd.value else None
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(manager)

    def _close_world_manager(self):
        self.page.pop_dialog()
        self._ai_center = True
        self._render()

    def _open_world_editor(self, world_id):
        self.page.pop_dialog()
        world = self.db.get_world(world_id) if world_id else None
        name_field = ft.TextField(label="世界观名称", value=world["name"] if world else "")
        content_field = ft.TextField(
            label="世界观内容",
            value=world["content"] if world else "",
            multiline=True,
            min_lines=6,
            max_lines=14,
        )
        editor = ft.AlertDialog(
            modal=True,
            title=ft.Text("编辑世界观" if world else "新建世界观"),
            content=ft.Column(
                [name_field, content_field],
                tight=True,
                spacing=8,
            ),
            actions=[
                ft.TextButton(
                    "取消",
                    on_click=lambda e: self._cancel_world_editor(),
                ),
                ft.FilledButton(
                    content="保存",
                    on_click=lambda e: self._save_world(
                        world_id, name_field, content_field, editor
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(editor)

    def _cancel_world_editor(self):
        self.page.pop_dialog()
        self._open_world_manager()

    def _save_world(self, world_id, name_field, content_field, dlg):
        name = (name_field.value or "").strip()
        if not name:
            self._toast("世界观名称不能为空")
            return
        if world_id:
            self.db.update_world(world_id, name=name, content=content_field.value)
        else:
            self.db.create_world(name, content_field.value)
        self.page.pop_dialog()
        self._open_world_manager()

    def _delete_world(self, world_id):
        if not world_id:
            self._toast("请先选择世界观")
            return
        ok = self.db.delete_world(world_id)
        self.page.pop_dialog()
        if not ok:
            self._toast("仍有角色卡绑定，请先解除绑定")
            self._open_world_manager()
            return
        self._toast("世界观已删除")
        self._open_world_manager()

    def _begin_roleplay(self, card_id, dlg=None, relation=None, affection=None):
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
            if not self.db.get_ai_messages(sid):
                self._seed_roleplay_greeting(card_id, sid)
            state = self.db.role_card_state(card_id)
            state_changed = (
                (relation and state.get("身份") != relation)
                or (affection and state.get("好感度") != affection)
            )
            if state_changed:
                confirm = ft.AlertDialog(
                    modal=True,
                    title=ft.Text("应用初始设定"),
                    content=ft.Text(
                        f"「{card['name']}」已有长期聊天框。"
                        f"将身份设为「{relation}」、好感度设为「{affection}」，"
                        "聊天记录会保留。确定？"
                    ),
                    actions=[
                        ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                        ft.FilledButton(
                            content="应用并继续",
                            on_click=lambda e: self._apply_role_initial(
                                card_id, sid, relation, affection
                            ),
                        ),
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
                self.page.show_dialog(confirm)
                return
        else:
            sid = self.db.create_ai_session(
                "roleplay", f"角色扮演 · {card['name']}", role_card_id=card_id
            )
            self._seed_roleplay_greeting(card_id, sid)
            if relation or affection:
                self._apply_role_initial(
                    card_id, sid, relation, affection, pop_dialog=False
                )
                return
        self._ai_session_id = sid
        self._ai_center = False
        self._render()

    def _seed_roleplay_greeting(self, card_id, session_id):
        card = self.db.get_role_card(card_id)
        if not card:
            return
        greeting = role_greeting(card["content"])
        if greeting and not self.db.get_ai_messages(session_id):
            self.db.append_ai_message(session_id, "assistant", greeting)

    def _apply_role_initial(self, card_id, sid, relation, affection, pop_dialog=True):
        if pop_dialog:
            self.page.pop_dialog()
        state = self.db.role_card_state(card_id)
        if relation:
            state["身份"] = relation
        if affection:
            state["好感度"] = affection
        self.db.save_role_card_state(card_id, state)
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
                    card_data = data.get("data") if (
                        data.get("spec") == "chara_card_v2"
                        and isinstance(data.get("data"), dict)
                    ) else data
                    if any(k in card_data for k in
                           ("description", "personality", "scenario",
                            "first_mes", "mes_example", "人设", "性格",
                            "alternate_greetings", "creator_notes",
                            "system_prompt", "post_history_instructions",
                            "character_book")):
                        content = tavern_to_role_card(card_data)
                        name = str(
                            card_data.get("name") or card_data.get("角色名") or name
                        )
                    else:
                        name = str(
                            card_data.get("name") or card_data.get("角色名") or name
                        )
                        content = str(
                            card_data.get("content") or card_data.get("system")
                            or card_data.get("设定") or text
                        )
            except Exception:
                pass
            self._prompt_import_world(name, content)
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
        identity = state.get("身份", "未设定")
        affection = state.get("好感度", "未建立")
        emotion = state.get("当前情绪", "平静")
        memory = state.get("记忆", "")
        important = state.get("重要记忆", "")
        group_memory = state.get("群聊记忆", "")
        scene = self.db.get_ai_session_scene(session_id)
        state_menu = ft.PopupMenuButton(
            icon=ft.Icons.MORE_VERT,
            tooltip="状态管理",
            items=[
                ft.PopupMenuItem(
                    content=ft.Text("编辑记忆"),
                    on_click=lambda e: self._open_edit_state(card_id),
                ),
                ft.PopupMenuItem(
                    content=ft.Text("重置关系"),
                    on_click=lambda e: self._reset_role_relation(card_id),
                ),
            ],
        )
        if identity and identity != "未设定":
            lines = [
                ft.Row(
                    [
                        ft.Text("身份", size=11, color=ft.Colors.BLUE_GREY_500),
                        ft.Text(str(identity), size=13, weight=ft.FontWeight.BOLD,
                                color=ft.Colors.BLUE_700),
                        ft.Container(expand=True),
                        state_menu,
                    ],
                    spacing=4,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    [
                        ft.Text("好感度", size=11, color=ft.Colors.BLUE_GREY_500),
                        ft.Text(str(affection), size=13, weight=ft.FontWeight.BOLD,
                                color=ft.Colors.BLUE_700),
                        ft.Container(width=10),
                        ft.Text("情绪", size=11, color=ft.Colors.BLUE_GREY_500),
                        ft.Text(str(emotion), size=13, color=ft.Colors.BLUE_GREY_700),
                        ft.Container(expand=True),
                    ],
                    spacing=4,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ]
        else:
            lines = [
                ft.Row(
                    [
                        ft.Text("好感度", size=11, color=ft.Colors.BLUE_GREY_500),
                        ft.Text(str(affection), size=13, weight=ft.FontWeight.BOLD,
                                color=ft.Colors.BLUE_700),
                        ft.Container(width=10),
                        ft.Text("情绪", size=11, color=ft.Colors.BLUE_GREY_500),
                        ft.Text(str(emotion), size=13, color=ft.Colors.BLUE_GREY_700),
                        ft.Container(expand=True),
                        state_menu,
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
        if group_memory:
            lines.append(ft.Text(f"群聊：{group_memory}", size=12,
                                 color=ft.Colors.INDIGO_700,
                                 max_lines=2, overflow=ft.TextOverflow.ELLIPSIS))
        if scene:
            lines.append(ft.Text(f"当前场景：{scene}", size=12,
                                 color=ft.Colors.TEAL_700,
                                 max_lines=2, overflow=ft.TextOverflow.ELLIPSIS))
        return ft.Container(
            margin=ft.Margin(left=12, right=12, top=8, bottom=4),
            padding=ft.Padding(left=12, right=8, top=8, bottom=8),
            bgcolor=ft.Colors.with_opacity(0.1, ft.Colors.LIGHT_BLUE_200),
            border_radius=10,
            content=ft.Column(lines, tight=True, spacing=4),
        )

    def _open_edit_state(self, card_id):
        state = self.db.role_card_state(card_id)
        identity = ft.TextField(label="身份", value=state.get("身份", ""))
        affection = ft.TextField(label="好感度", value=state.get("好感度", ""))
        emotion = ft.TextField(label="当前情绪", value=state.get("当前情绪", ""))
        memory = ft.TextField(
            label="记忆", value=state.get("记忆", ""),
            multiline=True, min_lines=2, max_lines=5,
        )
        important = ft.TextField(
            label="重要记忆", value=state.get("重要记忆", ""),
            multiline=True, min_lines=2, max_lines=5,
        )
        group_memory = ft.TextField(
            label="群聊记忆", value=state.get("群聊记忆", ""),
            multiline=True, min_lines=2, max_lines=5,
        )
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("编辑记忆"),
            content=ft.Column(
                [identity, affection, emotion, memory, important, group_memory],
                tight=True,
                spacing=8,
                scroll=ft.ScrollMode.AUTO,
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(
                    content="保存",
                    on_click=lambda e: self._save_edit_state(
                        card_id, identity, affection, emotion,
                        memory, important, group_memory, dlg,
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _save_edit_state(self, card_id, identity, affection, emotion,
                         memory, important, group_memory, dlg):
        state = {
            "身份": identity.value,
            "好感度": affection.value,
            "当前情绪": emotion.value,
            "记忆": memory.value,
            "重要记忆": important.value,
            "群聊记忆": group_memory.value,
        }
        self.db.save_role_card_state(card_id, state)
        self.page.pop_dialog()
        self._toast("角色状态已保存")
        self._render()

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
        if kind == "role_grill":
            meta = self.db.get_ai_session_meta(sess["id"])
            covered = set(meta.get("progress", []))
            progress_value = min(1.0, len(covered) / max(1, len(GRILL_PROGRESS_ITEMS)))
            rows.append(ft.Container(
                margin=ft.Margin(left=12, right=12, top=8, bottom=4),
                padding=ft.Padding(left=12, right=12, top=8, bottom=8),
                bgcolor=ft.Colors.with_opacity(0.08, ft.Colors.LIGHT_BLUE_200),
                border_radius=10,
                content=ft.Column(
                    [
                        ft.ProgressBar(value=progress_value),
                        ft.Text(
                            "已覆盖：" + (
                                "暂无"
                                if not covered
                                else "、".join(
                                    x for x in GRILL_PROGRESS_ITEMS if x in covered
                                )
                            ),
                            size=12,
                            color=ft.Colors.BLUE_GREY_600,
                            max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                    ],
                    tight=True,
                    spacing=4,
                ),
            ))
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

        if kind == "role_grill":
            last_assistant = next(
                (m for m in reversed(messages) if m["role"] == "assistant"), None
            )
            if last_assistant and extract_role_card(last_assistant["content"]):
                rows.append(ft.Container(
                    padding=ft.Padding(left=12, right=12, top=8, bottom=4),
                    content=ft.FilledButton(
                        content="预览并生成角色卡",
                        icon=ft.Icons.PERSON_ADD,
                        on_click=lambda e, s=sess["id"]: self._preview_ai_role_card(s),
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
                        icon=ft.Icons.HELP_OUTLINE,
                        icon_color=ft.Colors.BLUE_700,
                        tooltip="指令",
                        on_click=self._show_directive_help,
                    ),
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
            skill_id = sess["skill_id"]
            if skill_id == "role_grill":
                clean, progress, summary = parse_grill_state(reply)
                meta = self.db.get_ai_session_meta(sess["id"])
                covered = set(meta.get("progress", []))
                covered.update(progress)
                if summary:
                    meta["summary"] = summary
                meta["progress"] = list(covered)
                self.db.set_ai_session_meta(sess["id"], meta)
            else:
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
        scene = self.db.get_ai_session_scene(sess["id"])
        grill_meta = (
            self.db.get_ai_session_meta(sess["id"])
            if skill and skill["id"] == "role_grill"
            else {}
        )

        for _ in range(3):
            history = self.db.get_ai_messages(sess["id"], limit=200)
            if not history:
                return None
            last_text = history[-1]["content"] if history[-1]["role"] == "user" else ""
            _, directives = extract_bracket_directives(last_text)
            scene_change = extract_scene_change(directives)
            if scene_change:
                scene = scene_change
                self.db.set_ai_session_scene(sess["id"], scene)
            if card:
                state = self.db.role_card_state(card["id"])
                if history and history[-1]["role"] == "user":
                    loaded.update(
                        match_world_book(card["content"], history[-1]["content"])
                    )
                    self._role_loaded_sections[sess["id"]] = loaded
                system = build_role_system(card["content"], state, loaded)
            else:
                system = skill["system"] if skill else "你是一个简洁的 AI 助手。"
                if skill and skill["id"] == "role_grill":
                    if grill_meta.get("summary"):
                        system += (
                            "\n\n[已确认设定摘要]\n"
                            + str(grill_meta["summary"])
                        )
                    if grill_meta.get("world_id"):
                        system += (
                            "\n\n[世界观约束]\n"
                            "当前角色必须属于这个世界观。"
                            "如果用户提出与世界观冲突的设定，先指出冲突并请用户确认，"
                            "不要直接覆盖世界观内容。"
                        )
                    if grill_meta.get("progress"):
                        system += (
                            "\n\n[已覆盖项]\n"
                            + "、".join(grill_meta["progress"])
                        )
            if scene:
                system += f"\n\n[当前场景]\n{scene}"
            if history and history[-1]["role"] == "user":
                if directives:
                    system += (
                        "\n\n[导演指令]\n"
                        + "\n".join(f"- {d}" for d in directives)
                    )
                if has_remember_directive(last_text):
                    system += (
                        "\n\n注意：用户本轮明确要求你记住某些内容。"
                        "请把它逐字保留到 ---STATE--- 的 重要记忆= 字段，不要压缩丢失。"
                    )
            payload = [{"role": "system", "content": system}]
            payload.extend(
                {"role": m["role"], "content": m["content"]} for m in history
            )
            post = post_history_instructions(card["content"]) if card else ""
            if post:
                payload.append(
                    {"role": "system", "content": f"[历史后置指令]\n{post}"}
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

    def _show_directive_help(self, e=None):
        if self._ai_group_id is not None:
            items = [
                "（场景切换：...）",
                "（只让A回）",
                "（让角色们自己聊一会儿）",
            ]
        else:
            sess = self.db.get_ai_session(self._ai_session_id) if self._ai_session_id else None
            skill = SKILL_BY_ID.get(sess["skill_id"]) if sess else None
            if skill and skill["id"] == "role_grill":
                items = [
                    "（生成角色卡）",
                    "（跳过）",
                    "（回到背景）",
                ]
            else:
                items = [
                    "（场景切换：...）",
                    "（记住：...）",
                ]
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("指令语法"),
            content=ft.Column(
                [ft.Text(f"- {x}", size=13) for x in items],
                tight=True,
                spacing=6,
            ),
            actions=[
                ft.TextButton("关闭", on_click=lambda e: self.page.pop_dialog()),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

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

    # ---------- AI 角色卡落库 ----------
    def _preview_ai_role_card(self, session_id):
        sess = self.db.get_ai_session(session_id)
        if not sess or sess["skill_id"] != "role_grill":
            return
        last = self.db.get_ai_messages(session_id)
        assistant = [m for m in last if m["role"] == "assistant"]
        if not assistant:
            self._toast("AI 还没有生成角色卡")
            return
        content = extract_role_card(assistant[-1]["content"])
        if not content or not parse_role_card(content).get("核心"):
            self._toast("AI 回复里没有可生成的角色卡")
            return
        worlds = self.db.list_worlds()
        world_dd = ft.Dropdown(
            label="加入已有世界观",
            value="",
            options=[
                ft.dropdown.Option(key="", text="暂不绑定"),
                *[
                    ft.dropdown.Option(key=str(w["id"]), text=w["name"])
                    for w in worlds
                ],
            ],
        )
        new_world_field = ft.TextField(
            label="或新建世界观",
            hint_text="留空则使用上面的选择",
        )
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("确认生成角色卡"),
            content=ft.Column(
                [
                    ft.Text("检查角色卡内容，确认后加入角色卡列表：", size=12,
                            color=ft.Colors.BLUE_GREY_600),
                    world_dd,
                    new_world_field,
                    ft.Text(content, size=12, selectable=True),
                ],
                tight=True,
                spacing=8,
                scroll=ft.ScrollMode.AUTO,
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton(
                    content="创建角色卡",
                    on_click=lambda e: self._create_role_from_ai(
                        content, world_dd, new_world_field
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _create_role_from_ai(self, content, world_dd=None, new_world_field=None):
        sections = parse_role_card(content)
        core = sections.get("核心", "")
        if not core:
            self._toast("角色卡格式不完整")
            return
        first_line = core.splitlines()[0].strip()
        name = first_line.split("：", 1)[-1].strip() if "：" in first_line else ""
        name = name or "AI 角色"
        world_id = None
        new_world = (
            (new_world_field.value or "").strip()
            if new_world_field is not None else ""
        )
        if new_world:
            world_id = self.db.create_world(
                new_world, sections.get("世界观", "")
            )
        elif world_dd is not None and world_dd.value:
            world_id = int(world_dd.value)
        content = self._attach_world_to_content(content, world_id)
        self.page.pop_dialog()
        self.db.create_role_card(name, content, world_id)
        self._ai_session_id = None
        self._ai_input = None
        self._ai_center = True
        self._toast(f"已生成角色卡：{name}")
        self._render()

    # ---------- AI 任务树落库 ----------
    def _preview_ai_tasks(self, session_id):
        sess = self.db.get_ai_session(session_id)
        if not sess or sess["skill_id"] not in ("grill_decompose", "quick_decompose"):
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
        project_dd = ft.Dropdown(
            label="挂载到哪个项目",
            value="",
            options=[
                ft.dropdown.Option(key="", text="作为新的顶层项目"),
                *[
                    ft.dropdown.Option(
                        key=str(it["id"]),
                        text=self.db.title_path(it["id"]),
                    )
                    for it in self.db.all_items()
                ],
            ],
        )
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("确认生成任务树"),
            content=ft.Column(
                [
                    ft.Text("选择挂载位置，AI 任务会追加到目标项目下方：", size=12,
                            color=ft.Colors.BLUE_GREY_600),
                    project_dd,
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
                    on_click=lambda e, s=session_id, r=rows, dd=project_dd: (
                        self._apply_ai_tasks(
                            s, r, int(dd.value) if dd.value else None
                        )
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _apply_ai_tasks(self, session_id, rows, project_id):
        self.page.pop_dialog()
        project = self.db.get(project_id) if project_id else None
        # AI 大纲第一行往往是项目名本身；与项目标题一致时跳过，避免重复。
        if project and rows and rows[0][0] == 0 and rows[0][1].strip() == project["title"].strip():
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
                ft.DismissDirection.END_TO_START: 1.5,
                ft.DismissDirection.START_TO_END: 1.5,
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
            on_update=lambda e, i=item_id, d=done: self._on_swipe_update(
                e, i, d
            ),
            on_dismiss=lambda e, i=item_id, d=done: self._on_dismiss(e, i, d),
        )

    def _on_swipe_update(self, e, item_id, done):
        direction = getattr(e, "direction", None)
        key = (item_id, str(direction))
        progress = float(getattr(e, "progress", 0) or 0)
        if progress < 0.05:
            self._swipe_blocked[key] = False
            return
        if progress < 0.2:
            if progress > 0.08:
                for old_key in list(self._swipe_armed):
                    if old_key[0] == item_id and old_key[1] != str(direction):
                        self._swipe_armed.pop(old_key, None)
                        self._swipe_armed_at.pop(old_key, None)
                        self._swipe_blocked[key] = True
                        self._toast("已取消，重新滑动可选择其他操作")
                        return
            return
        if self._swipe_blocked.get(key):
            return
        if self._swipe_armed.get(key):
            if time.monotonic() - self._swipe_armed_at.get(key, 0) < 0.25:
                return
            self._swipe_armed.pop(key, None)
            self._swipe_armed_at.pop(key, None)
            if direction == ft.DismissDirection.END_TO_START:
                self._dismiss_delete(item_id)
            else:
                self._dismiss_complete(item_id, done)
            return
        if not self._swipe_armed.get(key):
            for old_key in list(self._swipe_armed):
                if old_key[0] == item_id:
                    self._swipe_armed.pop(old_key, None)
                    self._swipe_armed_at.pop(old_key, None)
            self._swipe_armed[key] = True
            self._swipe_armed_at[key] = time.monotonic()
            if direction == ft.DismissDirection.END_TO_START:
                label = "删除"
            else:
                label = "恢复" if done else "完成"
            self._toast(f"已显示操作，再滑动一次确认{label}")

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
        elif self._ai_group_id is not None:
            self._close_group_chat()
        elif self._ai_session_id is not None:
            self._close_ai_chat()
        elif self._ai_center:
            self._close_ai_center_or_category()
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
            self._ai_session_id is not None
            or self._ai_group_id is not None
            or self._search_mode
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
        """撤销完成：该子树变未完成，并把祖先链一起放回首页；同层其他项保持完成。"""
        self.db.set_subtree_done(item_id, False)
        for anc in self.db.ancestors(item_id):
            self.db.set_done(anc, False)
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
