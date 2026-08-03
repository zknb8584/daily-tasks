# -*- coding: utf-8 -*-
"""
开发用 UI 冒烟测试（不弹窗口）：
用带桩 Page 驱动 TaskApp 的控件构建与事件处理逻辑，
验证增删改查、勾选/恢复、已完成分区、截止时间、删除确认等代码路径。

运行：python ui_test.py
"""
import base64
import datetime as dt
import json
import os
import tempfile

import flet as ft

import main as appmod
from models import Database


class FakeEvent:
    def __init__(self, control):
        self.control = control


class FakePage:
    def __init__(self):
        self.title = ""
        self.theme_mode = None
        self.theme = None
        self.padding = 0
        self.width = None
        self.height = None
        self.overlay = []
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


CONTAINER_ATTRS = ("controls", "content", "title", "subtitle", "leading", "trailing", "actions")


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

    # ---- 造数据：A 大项目（部分完成）、B 整棵完工、C 过期 ----
    a = db.add(None, "大项目A", (dt.datetime.now() + dt.timedelta(hours=5)).strftime(appmod.DATETIME_FMT))
    c1 = db.add(a, "子任务1", (dt.datetime.now() + dt.timedelta(hours=2)).strftime(appmod.DATETIME_FMT))
    c2 = db.add(a, "子任务2")
    db.set_done(c1, True)
    b = db.add(None, "完成项目B")
    b1 = db.add(b, "b1", (dt.datetime.now() - dt.timedelta(days=1)).strftime(appmod.DATE_FMT))
    b2 = db.add(b, "b2")
    db.set_done(b, True); db.set_done(b1, True); db.set_done(b2, True)
    c = db.add(None, "过期项目C", (dt.datetime.now() - dt.timedelta(hours=3)).strftime(appmod.DATETIME_FMT))
    assert db.fully_done(b) is True
    assert db.fully_done(a) is False

    # ---- 每日一句：先写入句子，App 启动时应随机显示一条 ----
    appmod.save_quotes("第一句测试\n第二句测试")
    assert appmod.get_quotes() == ["第一句测试", "第二句测试"]

    pg = FakePage()
    app = appmod.TaskApp(pg)

    # ---- 冷静蓝配色 ----
    assert pg.theme is not None and pg.theme.color_scheme_seed == ft.Colors.BLUE
    assert pg.appbar.bgcolor == ft.Colors.BLUE_800

    # ---- 渲染：主列表应有 A、C；已完成区应有 B ----
    texts = rendered_texts(app)
    assert any("大项目A" in t for t in texts), texts
    assert any("过期项目C" in t for t in texts), texts
    assert any("已完成 (1)" in t for t in texts), texts
    assert any("进度 1/2" in t for t in texts), texts

    # ---- 每日一句：根界面顶部显示卡片，点一下换一句 ----
    assert any("每日一句" in t for t in texts), texts
    assert any("第一句测试" in t for t in texts) or any("第二句测试" in t for t in texts), texts
    app._shuffle_quote()
    texts = rendered_texts(app)
    assert any("第一句测试" in t for t in texts) or any("第二句测试" in t for t in texts), texts

    # ---- 进入子项目后不显示每日一句，返回根界面恢复 ----
    app._enter_children(a)
    texts = rendered_texts(app)
    assert not any("每日一句" in t for t in texts), texts
    app._back()
    texts = rendered_texts(app)
    assert any("每日一句" in t for t in texts), texts

    # ---- 勾选 c2：A 仍非整棵完工（A 自身未勾）→ 主列表进度变 2/2 ----
    cb = ft.Checkbox(value=False)
    cb.value = True
    app._on_toggle(FakeEvent(cb), c2)
    texts = rendered_texts(app)
    assert any("进度 2/2" in t for t in texts), texts

    # ---- 勾选 A：整棵完工 → 移入已完成 ----
    cb2 = ft.Checkbox(value=False); cb2.value = True
    app._on_toggle(FakeEvent(cb2), a)
    texts = rendered_texts(app)
    assert any("已完成 (2)" in t for t in texts), texts
    assert "大项目A" not in texts or all("已完成" in t for t in texts if "大项目A" in t), texts

    # ---- 恢复 B：从已完成区回到主列表 ----
    app._restore(b)
    assert db.get(b)["done"] == 0
    texts = rendered_texts(app)
    assert any("完成项目B" in t for t in texts), texts
    assert any("已完成 (1)" in t for t in texts), texts
    # 展开已完成区，A 应出现
    app._completed_open = True
    app._render()
    texts = rendered_texts(app)
    assert any("大项目A" in t for t in texts), texts
    app._completed_open = False
    app._render()

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

    print("UI TEST OK")


if __name__ == "__main__":
    main()
