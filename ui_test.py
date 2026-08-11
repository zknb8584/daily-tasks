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

import ai_client
import main as appmod
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
    assert len(pg._services.services) >= 2, pg._services.services
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

    # ---- AI：会话 CRUD + 配置 + 技能中心 + 对话页 ----
    ai_root = db.add(None, "AI 测试项目")
    db.set_ai_config(base_url="https://api.deepseek.com/v1", model="deepseek-chat",
                     api_key="sk-test")
    cfg = db.get_ai_config()
    assert cfg["ai_base_url"] == "https://api.deepseek.com/v1"
    assert cfg["ai_model"] == "deepseek-chat"
    assert cfg["ai_api_key"] == "sk-test"
    sid = db.create_ai_session("grill_decompose", "AI 测试会话", project_id=ai_root)
    db.append_ai_message(sid, "user", "项目：AI 测试项目")
    db.append_ai_message(sid, "assistant", "---TASKS---\nAI 测试项目\n  子步骤1\n  子步骤2")
    assert len(db.get_ai_messages(sid)) == 2
    assert len(db.list_ai_sessions()) >= 1

    app._open_ai_center()
    texts = rendered_texts(app)
    assert any("AI 拷问拆解" in t for t in texts), texts
    assert any("AI 测试会话" in t for t in texts), texts

    app._open_ai_session(sid)
    texts = rendered_texts(app)
    assert any("子步骤1" in t for t in texts), texts
    assert any("预览并生成任务树" in t for t in texts), texts

    # ---- AI：任务大纲解析 + 追加落库 ----
    rows = ai_client.extract_tasks(
        "请拆解。\n---TASKS---\nAI 测试项目\n  子步骤1\n    子子步骤1\n  子步骤2"
    )
    assert rows == [(0, "AI 测试项目"), (1, "子步骤1"), (2, "子子步骤1"), (1, "子步骤2")], rows
    before_kids = len(db.children(ai_root))
    before_subtree = len(db._subtree_ids(ai_root))
    app._apply_ai_tasks(sid, rows, ai_root)
    assert len(db._subtree_ids(ai_root)) == before_subtree + 3  # 根标题与项目同名，跳过

    # ---- 滑动：子树快照删除 -> 撤销恢复 ----
    snap = db.snapshot_subtree(ai_root)
    db.delete(ai_root)
    assert all(r["id"] != ai_root for r in db.roots())
    restored_root = db.restore_subtree(snap)
    assert restored_root is not None
    assert any(r["title"] == "AI 测试项目" for r in db.roots())

    # ---- 滑动：Dismissible 包装构造不崩 ----
    wrapper = app._dismiss_wrap(ft.ListTile(title=ft.Text("x")), 1)
    assert isinstance(wrapper, ft.Dismissible)

    # ---- 预览按钮：最后一条无大纲时隐藏，有大纲时显示 ----
    db.append_ai_message(sid, "assistant", "好的，明白了。")
    app._ai_input = None
    app._render()
    texts = rendered_texts(app)
    assert not any("预览并生成任务树" in t for t in texts), texts
    db.append_ai_message(sid, "assistant", "---TASKS---\nAI 测试项目\n  补充任务")
    app._render()
    texts = rendered_texts(app)
    assert any("预览并生成任务树" in t for t in texts), texts

    # ---- 滑动撤销栈：连续删两个，逐个撤销 ----
    app._dismissed_stack = []
    t1 = db.add(None, "滑删A")
    t2 = db.add(None, "滑删B")
    app._dismiss_delete(t1)
    app._dismiss_delete(t2)
    assert db.get(t1) is None and db.get(t2) is None
    assert len(app._dismissed_stack) == 2
    app._undo_dismissed()
    assert any(r["title"] == "滑删B" for r in db.roots()), db.roots()  # 后删的先撤销
    assert len(app._dismissed_stack) == 1
    app._undo_dismissed()
    assert any(r["title"] == "滑删A" for r in db.roots()), db.roots()
    assert app._dismissed_stack == []

    # ---- 快照/恢复保留完成日志；删除清理孤儿日志（统计不虚增） ----
    w0 = db.stats_overview()["week_done"]
    rc = db.add(None, "恢复C")
    db.log_completion(rc)
    assert db.stats_overview()["week_done"] == w0 + 1
    snap2 = db.snapshot_subtree(rc)
    assert "completions" in snap2 and len(snap2["completions"]) == 1, snap2
    db.delete(rc)
    assert db.get(rc) is None
    assert db.stats_overview()["week_done"] == w0   # 删除后孤儿日志已清理
    restored = db.restore_subtree(snap2)
    assert restored is not None
    assert db.stats_overview()["week_done"] == w0 + 1  # 完成日志随恢复带回来

    # ---- 角色卡解析工具 ----
    card_text = """[核心]
名字：阿晴
[背景]
住在海边
[说话风格]
短句
"""
    sections = ai_client.parse_role_card(card_text)
    assert sections["核心"] == "名字：阿晴"
    assert sections["背景"] == "住在海边"

    clean, state = ai_client.parse_state_block(
        "好的。\n---STATE---\n好感度=45\n当前情绪=开心"
    )
    assert clean == "好的。"
    assert state["好感度"] == "45"
    assert state["当前情绪"] == "开心"

    clean2, loads = ai_client.extract_load_requests("先回答\n@load:背景\n继续")
    assert loads == ["背景"]
    assert "@load" not in clean2

    system = ai_client.build_role_system(
        card_text, {"好感度": "45", "记忆": "今天聊了海"}
    )
    assert "名字：阿晴" in system
    assert "摘要" in system
    assert "@load:背景" in system
    assert "45" in system

    # ---- 角色卡 DB 状态 + 每个角色一个永久聊天框 + 重置关系 ----
    card_id = db.create_role_card("测试角色", card_text)
    db.save_role_card_state(card_id, {"好感度": "45", "记忆": "x"})
    assert db.role_card_state(card_id)["好感度"] == "45"

    app._begin_roleplay(card_id)
    sid1 = app._ai_session_id
    assert sid1 is not None
    app._begin_roleplay(card_id)
    assert app._ai_session_id == sid1  # 复用同一长期聊天框

    app._do_reset_role_relation(card_id)
    assert db.role_card_state(card_id) == {}
    assert all(s["role_card_id"] != card_id for s in db.list_ai_sessions())

    # ---- AI 生成角色卡 ----
    gen_field = ft.TextField(value="一个叫小星的机器人")
    gen_status = ft.Text("")
    orig_chat = appmod.chat_completion
    appmod.chat_completion = lambda *a, **k: (
        "[核心]\n名字：小星\n人设：机器人\n[说话风格]\n简短"
    )
    asyncio.run(app._do_generate_role_card(gen_field, gen_status, None))
    appmod.chat_completion = orig_chat
    assert any(c["name"] == "小星" for c in db.list_role_cards())

    print("UI TEST OK")


if __name__ == "__main__":
    main()
