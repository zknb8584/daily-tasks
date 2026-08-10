# -*- coding: utf-8 -*-
"""
开发用 UI 冒烟测试（不弹窗口）：
用带桩 Page 驱动 TaskApp 的控件构建与事件处理逻辑，
验证增删改查、勾选/恢复、已完成分区、截止时间、删除确认等代码路径。

运行：python ui_test.py
"""
import asyncio
import base64
import datetime as dt
import json
import os
import tempfile
import types

import flet as ft

import main as appmod
from ai_client import extract_tasks
from models import Database


class FakeEvent:
    def __init__(self, control):
        self.control = control


class FakeRegistry:
    """模拟 page._services：记录注册的服务。"""

    def __init__(self):
        self.services = []

    def register_service(self, svc):
        self.services.append(svc)


class FakePicker:
    """模拟 async FilePicker：pick_files/save_file 返回固定结果。"""

    def __init__(self, files=None, path=None):
        self.files = files or []
        self.path = path

    async def pick_files(self, **kw):
        return self.files

    async def save_file(self, **kw):
        return self.path


class FakePage:
    def __init__(self):
        self.title = ""
        self.theme_mode = None
        self.theme = None
        self.padding = 0
        self.width = None
        self.height = None
        self.overlay = []
        self._services = FakeRegistry()
        self.appbar = None
        self.floating_action_button = None
        self.on_keyboard_event = None
        self.controls = []
        self.last_dialog = None
        self.pop_count = 0

    def set_allowed_device_orientations(self, x):
        pass

    def add(self, *args):
        self.controls.extend(args)

    def update(self):
        pass

    def show_dialog(self, d):
        self.last_dialog = d

    def pop_dialog(self):
        self.pop_count += 1


CONTAINER_ATTRS = ("controls", "content", "title", "subtitle", "leading", "trailing", "actions", "items")


def collect_text(control, out):
    if isinstance(control, ft.Text):
        out.append(control.value)
    if isinstance(control, str):
        out.append(control)
        return
    for attr in CONTAINER_ATTRS:
        child = getattr(control, attr, None)
        if child is None:
            continue
        if isinstance(child, (list, tuple)):
            for x in child:
                collect_text(x, out)
        else:
            collect_text(child, out)


def rendered_texts(app):
    out = []
    for c in app.scroll.controls:
        collect_text(c, out)
    return out


def main():
    tmp = tempfile.mkdtemp()
    db = Database(os.path.join(tmp, "ui.db"))
    appmod.Database = lambda *a, **k: db   # 让 TaskApp 用测试库

    # ---- 造数据：A 大项目（子任务1已单独完成）、B 整组完成、C 过期 ----
    a = db.add(None, "大项目A", (dt.datetime.now() + dt.timedelta(hours=5)).strftime(appmod.DATETIME_FMT))
    c1 = db.add(a, "子任务1", (dt.datetime.now() + dt.timedelta(hours=2)).strftime(appmod.DATETIME_FMT))
    c2 = db.add(a, "子任务2")
    db.set_done(c1, True)                       # c1 单独完成 → 应归完成区
    b = db.add(None, "完成项目B")
    b1 = db.add(b, "b1", (dt.datetime.now() - dt.timedelta(days=1)).strftime(appmod.DATE_FMT))
    b2 = db.add(b, "b2")
    db.set_subtree_done(b, True)                # B 整组完成 → 完成区
    c = db.add(None, "过期项目C", (dt.datetime.now() - dt.timedelta(hours=3)).strftime(appmod.DATETIME_FMT))

    # ---- 每日一句：先写入句子，App 启动时应随机显示一条 ----
    appmod.save_quotes("第一句测试\n第二句测试")
    assert appmod.get_quotes() == ["第一句测试", "第二句测试"]

    pg = FakePage()
    app = appmod.TaskApp(pg)

    # ---- 冷静蓝配色 ----
    assert pg.theme is not None and pg.theme.color_scheme_seed == ft.Colors.BLUE
    assert pg.appbar.bgcolor == ft.Colors.BLUE_800

    # ---- FilePicker 应注册为服务（非 overlay 渲染，避免红框） ----
    assert len(pg._services.services) == 1, pg._services.services
    assert pg.overlay == [], pg.overlay
    assert isinstance(pg._services.services[0], ft.FilePicker)

    # ---- 首页渲染（两层分组）：A、C 在；完成组 B 与已完成的 c1 不在首页 ----
    texts = rendered_texts(app)
    assert any("大项目A" in t for t in texts), texts
    assert any("过期项目C" in t for t in texts), texts
    assert not any("完成项目B" in t for t in texts), texts          # B 在完成区
    assert not any("子任务1" in t for t in texts), texts            # c1 已完成 → 完成区
    assert any("子任务2" in t for t in texts), texts                # c2 未完成 → 框内
    assert any("添加子项目" in t for t in texts), texts             # 菜单项

    # ---- 每日一句 ----
    assert any("每日一句" in t for t in texts), texts
    app._shuffle_quote()

    # ---- 勾选叶子 c2：直接完成 → 进完成区，首页框内不再显示 ----
    cb = ft.Checkbox(value=False); cb.value = True
    app._on_toggle(FakeEvent(cb), c2)
    assert db.get(c2)["done"] == 1
    texts = rendered_texts(app)
    assert not any("子任务2" in t for t in texts), texts

    # ---- 勾选有子项的大项目 a：弹确认框，取消则回退 ----
    cb2 = ft.Checkbox(value=False); cb2.value = True
    app._on_toggle(FakeEvent(cb2), a)
    assert isinstance(pg.last_dialog, ft.AlertDialog), pg.last_dialog
    assert db.get(a)["done"] == 0                                    # 勾选已回退
    app.page.pop_dialog()                                            # 模拟取消
    assert db.get(a)["done"] == 0

    # ---- 确认完成：整组（含后代）移入完成区 ----
    app._do_complete_group(a)
    assert db.get(a)["done"] == 1
    assert db.get(c1)["done"] == 1 and db.get(c2)["done"] == 1
    texts = rendered_texts(app)
    assert not any("大项目A" in t for t in texts), texts             # 不在首页

    # ---- 完成区：独立界面，含整组 A 与 B；撤销 B 回首页 ----
    app._open_done()
    assert app._show_done is True
    texts = rendered_texts(app)
    assert any("大项目A" in t for t in texts), texts
    assert any("完成项目B" in t for t in texts), texts
    assert any("已完成的大项目" in t for t in texts), texts
    app._undo_completed(b)
    assert db.get(b)["done"] == 0
    assert db.get(b1)["done"] == 0
    app._close_done()
    assert app._show_done is False
    texts = rendered_texts(app)
    assert any("完成项目B" in t for t in texts), texts               # B 回首页

    # ---- 标签：创建 / 分配 / 导出导入 ----
    tag_id = db.get_or_create_tag("工作")
    assert tag_id is not None
    assert db.get_or_create_tag("工作") == tag_id                    # 幂等
    db.set_item_tag(c, "工作")
    t = db.tag_by_id(db.get(c)["tag_id"])
    assert t is not None and t[0] == "工作"
    exported = json.loads(db.export())
    assert "tags" in exported and any(x["name"] == "工作" for x in exported["tags"])
    # 首页按标签筛选
    app._set_tag_filter("工作")
    texts = rendered_texts(app)
    assert any("过期项目C" in t for t in texts), texts
    app._set_tag_filter(None)

    # ---- 排序：按截止时间（最早在上） ----
    assert app._group_deadline_ts(c) < app._group_deadline_ts(a)     # c 过期(-3h) 早于 a(+5h)
    app._sort_by_deadline = True
    app._render()
    app._on_sort_change(FakeEvent(types.SimpleNamespace(selected={"default"})))
    assert app._sort_by_deadline is False

    # ---- 编辑：改标题 + 设置截止时间 ----
    app._open_edit(item_id=c)
    app._title_field.value = "过期项目C改"
    app._dl_state = appmod.DeadlineState((dt.datetime.now() + dt.timedelta(days=2)).strftime(appmod.DATETIME_FMT))
    app._update_dl_label()
    app._save_edit(None)
    it = db.get(c)
    assert it["title"] == "过期项目C改", it
    assert it["deadline"] != "", it

    # ---- 日期/时间选择器构造与回调 ----
    app._open_edit(item_id=c)
    app._pick_date(None)
    assert isinstance(pg.last_dialog, ft.DatePicker)
    dp = pg.last_dialog
    dp.value = dt.datetime(2026, 12, 25, 9, 30)
    app._on_date_picked(FakeEvent(dp))
    assert app._dl_state.date == dt.date(2026, 12, 25)
    app._pick_time(None)
    assert isinstance(pg.last_dialog, ft.TimePicker)
    tp = pg.last_dialog
    tp.value = dt.time(8, 15)
    app._on_time_picked(FakeEvent(tp))
    assert app._dl_state.time == dt.time(8, 15)
    app._clear_deadline(None)
    assert app._dl_state.to_str() == ""
    app.page.pop_dialog()

    # ---- 删除确认 + 级联删除 ----
    app._confirm_delete(c1)
    assert isinstance(pg.last_dialog, ft.AlertDialog)
    app._do_delete(c1)
    assert db.get(c1) is None
    assert len(db.children(a)) == 1

    # ---- 设置对话框构造 ----
    app._open_settings(None)
    assert isinstance(pg.last_dialog, ft.AlertDialog)

    # ---- 通知测试回调 ----
    app._test_notify(None)
    assert pg.pop_count > 0

    # ---- 截止时间蓝色梯度 ----
    now = dt.datetime.now()
    assert app._deadline_color((now - dt.timedelta(hours=2)).strftime(appmod.DATETIME_FMT)) == ft.Colors.BLUE_900
    assert app._deadline_color((now + dt.timedelta(hours=2)).strftime(appmod.DATETIME_FMT)) == ft.Colors.BLUE_600
    assert app._deadline_color((now + dt.timedelta(days=2)).strftime(appmod.DATETIME_FMT)) == ft.Colors.LIGHT_BLUE_400

    # ---- 背景图：模型往返 + 应用到页面 ----
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
        "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    img = os.path.join(tmp, "bg.png")
    with open(img, "wb") as f:
        f.write(png)
    uri = appmod._image_to_data_uri(img)
    assert uri and uri.startswith("data:image/png;base64,"), uri

    db.set_bg_image(img)
    assert db.get_bg_image() is not None
    app._apply_background()
    assert isinstance(app._bg_root.image, ft.DecorationImage)
    db.clear_bg_image()
    app._apply_background()
    assert app._bg_root.image is None

    # ---- 清理每日一句 ----
    appmod.save_quotes("")
    assert appmod.get_quotes() == []

    # ---- 导入计划：追加不覆盖 ----
    plan = json.dumps({
        "app": "daily_tasks", "version": 1,
        "items": [
            {"id": 1, "parent_id": None, "title": "导入项目A", "deadline": "", "done": 0, "created_at": "2026-08-01T00:00:00"},
            {"id": 2, "parent_id": 1, "title": "导入子任务", "deadline": "2026-08-10", "done": 0, "created_at": "2026-08-01T00:00:00"},
        ],
    })
    before_roots = len(db.roots())
    n = db.import_plan(plan)
    assert n == 2, n
    assert len(db.roots()) == before_roots + 1  # 原有顶层项目保留，新增一个
    imp = [r for r in db.roots() if r["title"] == "导入项目A"][0]
    assert len(db.children(imp["id"])) == 1
    assert db.children(imp["id"])[0]["deadline"] == "2026-08-10"

    # ---- 导入计划 UI 回调：走 FakePage 不真正弹选择器，只验证方法存在性路径 ----
    app._render()
    texts = rendered_texts(app)
    assert any("导入项目A" in t for t in texts), texts

    # ---- 搜索 ----
    hits = db.search_items("子任务")
    assert len(hits) >= 1, hits
    assert db.title_path(hits[0]["id"])
    app._open_search()
    assert app._search_mode is True
    app._search_query = "子任务"
    app._render()
    texts = rendered_texts(app)
    assert any("子任务1" in t for t in texts) or any("子任务2" in t for t in texts), texts
    app._close_search()
    assert app._search_mode is False

    # ---- 日历视图 ----
    app._open_calendar()
    assert app._calendar_view is True
    texts = rendered_texts(app)
    assert any("年" in t and "月" in t for t in texts), texts   # 月份导航
    # 选中今天，应列出当天截止任务（C 过期项目是当天? 不一定，先只验证不崩溃）
    app._calendar_prev(None)
    app._calendar_next(None)
    app._close_calendar()
    assert app._calendar_view is False

    # ---- 备注 ----
    db.update(c, note="备注测试内容")
    assert db.get(c)["note"] == "备注测试内容"
    app._open_edit(item_id=c)
    assert app._note_field is not None and app._note_field.value == "备注测试内容"
    app.page.pop_dialog()
    app._render()

    # ---- 统计概览 ----
    s0 = db.stats_overview()
    assert s0["total"] >= 5
    db.log_completion(c)
    s1 = db.stats_overview()
    assert s1["week_done"] >= s0["week_done"] + 1, (s0, s1)

    # ---- 重复任务：完成→滚动截止时间+重新武装+记日志 ----
    db.update(c, repeat_type="daily", repeat_interval=0)
    before_dl = db.get(c)["deadline"]
    app._complete_recurring(c, db.get(c))
    after = db.get(c)
    assert after["done"] == 0
    assert after["repeat_type"] == "daily"
    if before_dl:
        assert after["deadline"] != before_dl

    # ---- async 文件处理器：用 FakePicker 桩 + asyncio.run 覆盖 4 条路径 ----
    # 导出备份
    out = os.path.join(tmp, "out.json")
    app.file_picker = FakePicker(path=out)
    asyncio.run(app._export(None))
    assert os.path.exists(out)
    assert json.load(open(out, encoding="utf-8"))["app"] == "daily_tasks"

    # 导入计划（追加）
    plan_file = os.path.join(tmp, "plan.json")
    with open(plan_file, "w", encoding="utf-8") as f:
        f.write(plan)
    before = len(db.roots())
    app.file_picker = FakePicker(files=[types.SimpleNamespace(path=plan_file, bytes=None)])
    asyncio.run(app._import_plan(None))
    assert len(db.roots()) == before + 1

    # 导入备份（覆盖）
    backup_data = json.load(open(out, encoding="utf-8"))
    backup_roots = {it["title"] for it in backup_data["items"] if it["parent_id"] is None}
    app.file_picker = FakePicker(files=[types.SimpleNamespace(path=out, bytes=None)])
    asyncio.run(app._import(None))
    assert {r["title"] for r in db.roots()} == backup_roots

    # 设置背景图
    app.file_picker = FakePicker(files=[types.SimpleNamespace(path=img, bytes=None)])
    asyncio.run(app._set_bg_image(None))
    assert db.get_bg_image() is not None
    db.clear_bg_image()

    # ================= v1.1.0 独立复现：滑动 + AI 助手 =================

    # ---- AI 配置读写 ----
    cfg0 = db.ai_config()
    assert cfg0["base_url"].startswith("https://"), cfg0
    db.save_ai_config("https://example.com", "test-model", "sk-test")
    cfg = db.ai_config()
    assert cfg["base_url"] == "https://example.com"
    assert cfg["model"] == "test-model"
    assert cfg["api_key"] == "sk-test", cfg
    db.clear_ai_key()
    assert db.ai_config()["api_key"] == ""

    # ---- AI 会话 CRUD + 消息 ----
    sid = db.create_ai_session("quick")
    assert sid is not None
    assert db.get_ai_session(sid)["skill"] == "quick"
    db.set_ai_session_title(sid, "会话标题")
    assert db.get_ai_session(sid)["title"] == "会话标题"
    db.add_ai_message(sid, "user", "你好")
    db.add_ai_message(sid, "assistant", "嗨")
    msgs = db.ai_messages_of(sid)
    assert [m["role"] for m in msgs] == ["user", "assistant"], msgs
    assert db.ai_sessions()[0]["id"] == sid          # 最新更新的排最前
    db.delete_ai_session(sid)
    assert db.get_ai_session(sid) is None
    assert db.ai_messages_of(sid) == []

    # ---- extract_tasks 大纲解析 ----
    outline = "好的，拆解如下：\n---TASKS---\n任务A\n  子A1\n  子A2\n任务B | 2026-08-15\n  子B1 | 2026-08-20 18:00"
    rows = extract_tasks(outline)
    assert rows == [
        (0, "任务A", ""),
        (1, "子A1", ""),
        (1, "子A2", ""),
        (0, "任务B", "2026-08-15"),
        (1, "子B1", "2026-08-20 18:00"),
    ], rows
    assert extract_tasks("没有标记的普通回复") == []

    # ---- AI 中心 / 会话页切换 + 追加任务树 ----
    app._open_ai_center()
    assert app._ai_center is True
    texts = rendered_texts(app)
    assert any("技能" in t for t in texts), texts
    assert any("AI 拷问拆解" in t for t in texts), texts
    app._start_ai_session("quick")
    assert app._ai_session_id is not None and app._ai_center is False
    assert app._ai_title() == "快速拆解", app._ai_title()   # 会话页标题在 AppBar
    # 未配置 Key：发送 → 回复引导文案（不联网）
    app._ai_input.value = "你好"
    asyncio.run(app._send_ai_message(FakeEvent(None)))
    msgs = db.ai_messages_of(app._ai_session_id)
    assert msgs and msgs[-1]["role"] == "assistant", msgs
    assert "API Key" in msgs[-1]["content"], msgs[-1]["content"]
    assert app._ai_busy is False
    # 项目菜单「AI 拆解」：带上下文注入 system 消息
    ai_target = db.add(None, "AI 目标项目")
    app._start_ai_breakdown(ai_target)
    assert app._ai_project == ai_target
    sys_msgs = [m for m in db.ai_messages_of(app._ai_session_id) if m["role"] == "system"]
    assert sys_msgs and "AI 目标项目" in sys_msgs[0]["content"], sys_msgs
    # 预览并追加：不覆盖已有子任务
    app._pending_tasks = (app._ai_session_id, rows)
    app._preview_ai_tasks()
    assert isinstance(pg.last_dialog, ft.AlertDialog)
    app._apply_ai_tasks(app._ai_session_id, rows)
    kids = db.children(ai_target)
    assert [k["title"] for k in kids] == ["任务A", "任务B"], kids
    sub_a = [k for k in kids if k["title"] == "任务A"][0]
    assert [k["title"] for k in db.children(sub_a["id"])] == ["子A1", "子A2"]
    sub_b = [k for k in kids if k["title"] == "任务B"][0]
    assert sub_b["deadline"] == "2026-08-15"
    assert db.children(sub_b["id"])[0]["deadline"] == "2026-08-20 18:00"
    app._close_ai_chat()
    assert app._ai_session_id is None and app._pending_tasks is None
    db.delete(ai_target)

    # ---- AI 设置对话框：保存 / 重开 ----
    app._open_ai_settings(None)
    assert isinstance(pg.last_dialog, ft.AlertDialog)
    app._ai_base.value = "https://api.deepseek.com"
    app._ai_model.value = "deepseek-chat"
    app._ai_key.value = "sk-测试"
    app._ai_save_config(None)
    assert db.ai_config()["base_url"] == "https://api.deepseek.com"
    assert db.ai_config()["model"] == "deepseek-chat"
    assert db.ai_config()["api_key"] == "sk-测试"
    app._open_ai_settings(None)                 # guard 已复位，可再开
    assert app._settings_dialog_open is True
    app._ai_settings_close(None)
    assert app._settings_dialog_open is False
    db.clear_ai_key()

    # ---- Dismissible 构造：主列表行可滑，搜索行只读 ----
    dl = app._dismiss_wrap(1, ft.Container(content=ft.Text("x")))
    assert isinstance(dl, ft.Dismissible)
    assert dl.dismiss_direction == ft.DismissDirection.HORIZONTAL
    assert dl.background is not None and dl.secondary_background is not None
    plain = ft.Container(content=ft.Text("y"))
    assert app._dismiss_wrap(1, plain, swipeable=False) is plain
    sr = app._search_result_row(db.get(c))
    assert not isinstance(sr, ft.Dismissible), type(sr)
    lvl = app._level1_row(db.get(b), [])
    assert isinstance(lvl.controls[0], ft.Dismissible), type(lvl.controls[0])
    assert app._dir_is_left(ft.DismissDirection.END_TO_START) is True
    assert app._dir_is_left("startToEnd") is False
    assert app._dir_is_left(None) is False

    # ---- 滑动删除 → SnackBar 撤销：整棵恢复 + completions 一致性 ----
    p = db.add(None, "滑动删除目标")
    pc = db.add(p, "滑动子")
    db.set_done(pc, True)
    db.log_completion(pc)                            # 记完成日志
    w0 = db.stats_overview()["week_done"]
    snap = db.snapshot_subtree(p)
    assert snap["completions"], snap                 # 快照应含完成日志
    db.delete(p)
    assert db.get(p) is None
    assert db.stats_overview()["week_done"] == w0 - 1  # 删除后日志一并清除（不虚增统计）
    new_id = db.restore_subtree(snap)
    assert db.get(new_id)["title"] == "滑动删除目标"
    child = db.children(new_id)[0]
    assert child["title"] == "滑动子" and child["done"] == 1
    # UI 撤销路径：_swipe_delete 入栈 → _undo_dismissed 整棵恢复
    app._swipe_delete(new_id)
    assert db.get(new_id) is None
    assert app._undo_stack and app._undo_stack[-1]["item_id"] == new_id
    app._undo_dismissed(new_id)
    assert any(r["title"] == "滑动删除目标" for r in db.roots()), db.roots()
    # 清理：找恢复后的根删掉
    for r in db.roots():
        if r["title"] == "滑动删除目标":
            db.delete(r["id"])

    # ---- 右滑完成 / 重复任务滚动（走完成路径与 checkbox 一致） ----
    rc = db.add(None, "重复滑动任务")
    db.update(rc, repeat_type="daily", repeat_interval=0)
    app._swipe_complete(rc, done=False)
    assert db.get(rc)["done"] == 0                   # 重复任务不进入完成区
    assert db.get(rc)["repeat_type"] == "daily"
    db.delete(rc)

    print("UI TEST OK")


if __name__ == "__main__":
    main()
