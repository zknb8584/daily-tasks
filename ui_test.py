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

    app._begin_roleplay(card_id, None, "同学", "30 友好")
    assert db.role_card_state(card_id)["身份"] == "同学"
    assert db.role_card_state(card_id)["好感度"] == "30 友好"

    # ---- 导入 V2 角色卡 JSON：system_prompt / post_history / character_book ----
    v2_file = os.path.join(tmp, "v2_role.json")
    with open(v2_file, "w", encoding="utf-8") as f:
        json.dump({
            "spec": "chara_card_v2",
            "data": {
                "name": "V2导入",
                "description": "测试导入",
                "system_prompt": "你是旧书店老板。",
                "post_history_instructions": "回复前先想动作。",
                "character_book": {"entries": [{
                    "keys": ["暗门"],
                    "content": "书店角落里有一扇暗门。",
                }]},
            },
        }, f, ensure_ascii=False)
    app.file_picker = FakePicker(files=[types.SimpleNamespace(path=v2_file, bytes=None)])
    asyncio.run(app._import_role_card(None))
    app._create_imported_role_card(types.SimpleNamespace(value=""), None)
    imported = [c for c in db.list_role_cards() if c["name"] == "V2导入"]
    assert len(imported) == 1
    assert "旧书店老板" in imported[0]["content"]
    assert "回复前先想动作" in imported[0]["content"]
    assert "暗门" in imported[0]["content"]

    # ---- 角色扮演实际请求：V2 指令 + 世界书关键词自动加载 ----
    wb_card = "[核心]\n名字：世界书测试\n[系统提示]\n只按旧书店规则回复\n"
    wb_card += "[历史后置指令]\n回复前先想角色动作。\n"
    wb_card += "[世界书]\n条目：暗门\n关键词：旧书店，雨夜\n内容：角落里有扇暗门。\n"
    wb_card_id = db.create_role_card("世界书测试", wb_card)
    wb_sid = db.create_ai_session(
        "roleplay", "世界书测试会话", role_card_id=wb_card_id
    )
    db.append_ai_message(wb_sid, "user", "（场景切换：夜晚）我在旧书店门口。")
    captured_payloads = []
    orig_chat_completion = appmod.chat_completion
    appmod.chat_completion = lambda *a, **k: (
        captured_payloads.append(a[3]) or "好的。"
    )
    app._role_loaded_sections[wb_sid] = set()
    asyncio.run(app._chat_with_role_card(db.get_ai_session(wb_sid)))
    appmod.chat_completion = orig_chat_completion
    assert captured_payloads, "应该已经调用 chat_completion"
    payload = captured_payloads[0]
    assert "只按旧书店规则回复" in payload[0]["content"]
    assert "角落里有扇暗门" in payload[0]["content"]
    assert "导演指令" in payload[0]["content"]
    assert "场景切换：夜晚" in payload[0]["content"]
    assert payload[-1]["role"] == "system"
    assert payload[-1]["content"].startswith("[历史后置指令]")

    # ---- AI 群聊：DB + 选角 + 实际发送 ----
    group_a_id = db.create_role_card("群A", "[核心]\n名字：群A\n[爱好]\n主机游戏")
    group_b_id = db.create_role_card("群B", "[核心]\n名字：群B\n[爱好]\n音乐")
    group_id = db.create_group_chat("测试群")
    db.add_group_member(group_id, group_a_id)
    db.add_group_member(group_id, group_b_id)
    assert len(db.group_members(group_id)) == 2
    db.append_group_message(group_id, "user", "大家聊聊游戏")
    db.append_group_message(
        group_id, "assistant", "我喜欢主机游戏",
        role_name="群A", role_card_id=group_a_id,
    )
    assert len(db.get_group_messages(group_id)) == 2
    group_history = ai_client.format_group_history(db.get_group_messages(group_id))
    assert "群A" in group_history
    clean_text, directives = ai_client.extract_bracket_directives(
        "（场景切换：晚上）我们在门口见"
    )
    assert clean_text == "我们在门口见"
    assert directives == ["场景切换：晚上"]
    assert ai_client.has_remember_directive("记住我穿蓝色") is False
    assert ai_client.has_remember_directive("（记住：我穿蓝色）") is True
    forced = ai_client.select_group_speakers(
        [{"id": group_a_id, "name": "群A", "content": "[爱好]\n主机游戏"}],
        "（场景切换：晚上）@群A 一起打游戏？",
    )
    assert forced[0]["id"] == group_a_id
    three_members = [
        {"id": group_a_id, "name": "群A", "content": "[爱好]\n主机游戏"},
        {"id": group_b_id, "name": "群B", "content": "[爱好]\n音乐"},
        {"id": 999, "name": "群C", "content": "[爱好]\n阅读"},
    ]
    for _ in range(20):
        picked = ai_client.select_group_speakers(three_members, "@群A 一起聊？")
        assert 1 <= len(picked) <= 3
        assert any(m["id"] == group_a_id for m in picked)
    group_system = ai_client.build_group_role_system(
        "[核心]\n名字：群A", {}, set(),
        [{"name": "群A"}, {"name": "群B"}],
        group_history,
    )
    assert "群聊设定" in group_system
    assert "主机游戏" in group_system
    assert "用户普通输入" in group_system
    assert "导演指令" in group_system

    app._open_group_chat(group_id)
    app._group_input = ft.TextField(
        value="（场景切换：晚上）（只让A回）@群A 一起打游戏？"
    )
    app._on_key(types.SimpleNamespace(key="Delete", ctrl=False))
    assert app._ai_group_id == group_id
    orig_group_chat = appmod.chat_completion
    appmod.chat_completion = lambda *a, **k: "好啊，我带你打。"
    asyncio.run(app._send_group_message(None))
    appmod.chat_completion = orig_group_chat
    group_msgs = db.get_group_messages(group_id)
    assert group_msgs[-1]["role"] == "assistant"
    assert group_msgs[-1]["role_name"] == "群A"
    assert group_msgs[-1]["role_card_id"] == group_a_id
    assert "群聊记忆" in db.role_card_state(group_a_id)

    # ---- 世界观档案 + 当前场景 + Grill 状态解析 ----
    wid = db.create_world("测试世界", "旧城")
    wcard_id = db.create_role_card(
        "世界角色", "[核心]\n名字：世界角色", world_id=wid
    )
    db.update_world(wid, content="新城")
    assert "[世界观]" in db.get_role_card(wcard_id)["content"]
    assert "新城" in db.get_role_card(wcard_id)["content"]
    assert db.delete_world(wid) is False
    db.update_role_card(wcard_id, world_id=None)
    assert db.delete_world(wid) is True
    scene_sid = db.create_ai_session("roleplay", "场景测试")
    db.set_ai_session_scene(scene_sid, "夜晚")
    assert db.get_ai_session_scene(scene_sid) == "夜晚"
    scene_gid = db.create_group_chat("场景群")
    db.set_group_scene(scene_gid, "雨天")
    assert db.get_group_scene(scene_gid) == "雨天"
    clean_prog, progress, summary = ai_client.parse_grill_state(
        "先问背景。\n---PROGRESS---\n核心:done\n背景:done\n"
        "---SUMMARY---\n已确认核心设定"
    )
    assert progress == ["核心", "背景"]
    assert summary == "已确认核心设定"
    assert "---PROGRESS---" not in clean_prog
    assert ai_client.extract_scene_change(["场景切换：夜晚"]) == "夜晚"

    # ---- 修复回归：删除角色卡清群成员 / 世界观替换 / 新建拷问世界观 ----
    del_card_id = db.create_role_card("删除测试", "[核心]\n名字：删除测试")
    del_group_id = db.create_group_chat("删除群")
    db.add_group_member(del_group_id, del_card_id)
    db.delete_role_card(del_card_id)
    assert db.group_members(del_group_id) == []

    attach_world_id = db.create_world("新世界", "新城")
    attached = app._attach_world_to_content(
        "[核心]\n名字：A\n[世界观]\n旧世界\n旧城",
        attach_world_id,
    )
    assert "新世界" in attached
    assert "旧城" not in attached

    app._begin_role_grill(
        types.SimpleNamespace(value=""),
        types.SimpleNamespace(value="新拷问世界"),
        None,
    )
    assert any(w["name"] == "新拷问世界" for w in db.list_worlds())
    assert db.get_ai_session_meta(app._ai_session_id).get("world_id") is not None

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

    # ---- 删除子项目撤销：恢复完整结构并挂回原父 ----
    parent = db.add(None, "结构父")
    child = db.add(parent, "结构子")
    grand = db.add(child, "结构孙")
    snap3 = db.snapshot_subtree(child)
    db.delete(child)
    restored_child = db.restore_subtree(snap3)
    assert db.get(restored_child)["parent_id"] == parent
    assert any(x["title"] == "结构孙" for x in db.children(restored_child))

    # ---- 完成撤销：祖先链一起返回，同层兄弟保持已完成 ----
    p2 = db.add(None, "链父")
    c2a = db.add(p2, "链子A")
    c2b = db.add(p2, "链子B")
    db.set_subtree_done(p2, True)
    app._undo_completed(c2a)
    assert db.get(p2)["done"] == 0
    assert db.get(c2a)["done"] == 0
    assert db.get(c2b)["done"] == 1

    # ---- 角色卡 JSON 兼容转换 + 角色化输出规则 ----
    tavern = ai_client.tavern_to_role_card({
        "name": "娜娜",
        "description": "猫耳少女",
        "personality": "说话简短，喜欢用喵",
        "hobbies": "主机游戏，音乐",
        "scenario": "住在一间旧书店",
    })
    parsed = ai_client.parse_role_card(tavern)
    assert parsed["核心"].startswith("名字：娜娜")
    assert "喵" in parsed["说话风格"]
    assert "主机游戏" in parsed["爱好"]
    assert "旧书店" in parsed["背景"]

    card_v2 = ai_client.tavern_to_role_card({
        "spec": "chara_card_v2",
        "data": {
            "name": "V2角色",
            "description": "测试",
            "first_mes": "你来了？这家店今晚只为你开门。",
            "mes_example": "用户：你这里有什么书？\n角色：想找什么，我帮你翻。",
            "alternate_greetings": ["雨夜好，先进来避雨吧。", "今晚只有我在这里。"],
            "creator_notes": "语气克制，动作少，话里带着一点旧书店的气味。",
            "system_prompt": "你是旧书店老板，绝不主动提起未来。",
            "post_history_instructions": "回复前先想角色当下的动作。",
            "character_book": {
                "entries": [
                    {
                        "keys": ["旧书店", "雨夜"],
                        "content": "书店角落里有一扇暗门。",
                    }
                ]
            },
        },
    })
    assert "V2角色" in card_v2
    parsed_v2 = ai_client.parse_role_card(card_v2)
    assert parsed_v2["开场白"] == "你来了？这家店今晚只为你开门。"
    assert "你这里有什么书" in parsed_v2["示例对话"]
    assert "雨夜好" in parsed_v2["替代开场"]
    assert "旧书店的气味" in parsed_v2["作者备注"]
    assert "旧书店老板" in card_v2
    assert "回复前先想角色当下的动作" in card_v2
    assert "暗门" in card_v2
    world_entries = ai_client.parse_world_book(card_v2)
    assert world_entries[0]["keys"] == ["旧书店", "雨夜"]
    assert ai_client.match_world_book(card_v2, "你记得那间旧书店吗？") == ["世界书"]
    assert ai_client.post_history_instructions(card_v2) == "回复前先想角色当下的动作。"
    assert ai_client.role_greeting(card_v2) == "你来了？这家店今晚只为你开门。"

    role_system = ai_client.build_role_system(
        "[核心]\n名字：测试角色", {}
    )
    assert "像真人一样说话" in role_system
    assert "AI 腔" in role_system
    assert "万能安慰" in role_system
    assert "具体的行动、反应或新情境" in role_system

    role_system_identity = ai_client.build_role_system(
        "[核心]\n名字：测试角色\n[爱好]\n主机游戏\n音乐",
        {"身份": "同学", "好感度": "30 友好"},
    )
    assert "[当前身份]" in role_system_identity
    assert "同学" in role_system_identity
    assert "主机游戏" in role_system_identity
    assert "主动问一句或抛出一个新话题" in role_system_identity

    role_system_v2 = ai_client.build_role_system(card_v2, {})
    assert "优先级最高" in role_system_v2
    assert "旧书店老板" in role_system_v2
    assert "世界书" in role_system_v2
    assert "示例对话" in role_system_v2
    assert "你这里有什么书" in role_system_v2
    role_system_v2_loaded = ai_client.build_role_system(card_v2, {}, loaded={"世界书"})
    assert "暗门" in role_system_v2_loaded

    greet_card_id = db.create_role_card("开场角色", card_v2)
    app._begin_roleplay(greet_card_id)
    greet_sid = app._ai_session_id
    greet_msgs = db.get_ai_messages(greet_sid)
    assert greet_msgs[0]["role"] == "assistant"
    assert greet_msgs[0]["content"] == "你来了？这家店今晚只为你开门。"
    app._begin_roleplay(greet_card_id)
    assert len(db.get_ai_messages(greet_sid)) == 1

    # ---- Grill-me 拷问角色卡：技能、提取、落库 ----
    assert any(s["id"] == "role_grill" for s in ai_client.AI_SKILLS)
    app._begin_role_grill(
        types.SimpleNamespace(value=""),
        types.SimpleNamespace(value=""),
        None,
    )
    assert db.get_ai_session(app._ai_session_id)["skill_id"] == "role_grill"
    grilled_card = (
        "总结：角色已经很具体。\n"
        "---ROLE_CARD---\n"
        "[核心]\n名字：拷问角色\n人设：一位旧书店的守夜人\n"
        "[说话风格]\n简短、克制\n[开场白]\n你来早了，书还没醒。\n"
    )
    extracted = ai_client.extract_role_card(grilled_card)
    assert "名字：拷问角色" in extracted
    app._create_role_from_ai(
        extracted,
        types.SimpleNamespace(value=""),
        types.SimpleNamespace(value=""),
    )
    assert any(c["name"] == "拷问角色" for c in db.list_role_cards())

    print("UI TEST OK")


if __name__ == "__main__":
    main()
