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

    print("UI TEST OK")


if __name__ == "__main__":
    main()
