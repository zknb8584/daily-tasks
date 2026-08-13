# 天野陽菜 · Claude Code 交接文档

> 更新时间：2026-08-13  
> 当前版本：v1.8.15  
> 仓库：`zknb8584/daily-tasks`（私有仓库）  
> 分支：`main`，当前工作区干净  
> 最近提交：`3a5f079 ci: retry flet apk build on gradle download flake`

## 1. 这是什么

这是一个用 **Python + Flet** 开发的安卓本地任务 App，正式名称「天野陽菜」。
数据全部保存在手机本地 SQLite，默认离线可用；AI 功能是可选项，需要用户自行配置
OpenAI 兼容 API（默认 DeepSeek）。目标设备是小米 15，所以 CI 只打 `arm64-v8a` APK。

代码目录：`D:\sicence practice\task_app`

本地开发环境：`task_app\.venv`（Python 3.12，`flet==0.86.5`）

## 2. 核心设计思路

### 2.1 任务树

任务不是扁平清单，而是一棵无限层级的项目树：

- `items.parent_id` 指向父项目，顶层项目为 `NULL`。
- 每个节点都能独立完成，但显示规则区分“单节点完成”和“整棵子树完成”。
- 单节点完成但子项目没完成：留在主列表，划线沉底。
- 单节点完成且整棵子树都完成：整棵子树移入完成区，可恢复、可清除。
- 首页按用户要求做成“扁平树”：第一层大字号，第二层小字号并缩进，第三层不直接展开。

### 2.2 数据层

`models.py` 是唯一数据入口，所有 UI 都通过 `Database` 操作 SQLite。
`_init()` 里既有建表也有 `PRAGMA table_info` 增量迁移，老手机升级不会丢数据。

主要表：

- `items` / `tags` / `completions`：任务、标签、完成日志。
- `ai_sessions` / `ai_messages`：AI 会话历史。
- `ai_role_cards` / `ai_user_cards` / `ai_drafts`：AI 角色卡、用户人设卡、草稿。
- `ai_role_relations` / `ai_character_relations`：用户↔角色、角色↔角色关系。
- `ai_worlds`：世界观档案。
- `ai_group_chats` / `ai_group_members` / `ai_group_messages`：群聊。

导出备份**不包含** AI 会话、API Key 等隐私数据，只导出任务树、标签、设置和角色卡体系。

### 2.3 UI

`main.py` 目前是单体 UI 文件，`TaskApp` 类内部用一个 `_render()` 分发：

- 任务首页 / 子项目页 / 完成区 / 搜索 / 日历。
- AI 中心 / AI 会话页 / AI 群聊页。
- 角色扮演首页 / 聊天页 / 角色卡页 / 关系地图页。

手机端必须坚持这些原则：

- 竖屏锁定。
- 按钮和点击区域足够大。
- 列表行尽量用原生 `ListTile`，不要自绘 `Container(expand=True)` 行。
- 不要引入电脑桌面专用组件。

### 2.4 通知

通知采用“应用存活时扫描”的简单方案：

- App 打开时扫一次。
- 后台线程每分钟扫一次到期/过期任务。
- 同一任务同一阶段只通知一次，去重集合存在 SQLite。

已知边界：App 被系统杀掉后不会继续通知。这是方案 A 的约定，不是 bug。

### 2.5 AI

AI 不是核心依赖。用户不填 API Key 时，任务功能完全可用。

- `ai_client.py` 只用 Python 标准库 `urllib`，不引入额外网络依赖。
- 所有 AI 请求是 OpenAI 兼容的 `POST {base}/chat/completions`。
- UI 事件里用 `asyncio.to_thread` 包住阻塞网络请求。
- API Key 只存本地 SQLite。

## 3. 功能清单

### 3.1 任务核心功能

- 无限嵌套项目树。
- 新建、编辑、删除、勾选完成、恢复、批量清除完成。
- 截止时间：日期 + 可选时分，时间留空按整天 23:59 处理。
- 标签、按标签筛选、按截止时间排序。
- 全树搜索、任务备注。
- 统计概览、连续打卡/本周完成数。
- 重复任务：每天、每周、每 N 天。
- 日历视图。
- JSON 备份导出/导入，以及“导入计划”追加不覆盖。
- 每日一句、自定义背景图。
- 本地通知。
- 两段式滑动：第一段提示，第二段确认；反向滑动取消。

### 3.2 AI 助手

- AI 中心三级入口：生成任务树 / 回答与解惑 / 角色扮演。
- AI 拷问拆解：grill-me 风格，一次一问，最后生成任务树。
- 快速拆解：直接根据项目信息生成任务树。
- 通用问答 / 课堂速解。
- 任务树可以挂到任意现有项目，也可以新建顶层项目。
- AI 设置：Base URL、API Key、模型、测试连接、清除 Key。

### 3.3 角色扮演

- 角色聊天列表按聊天软件样式展示，每个角色一个永久聊天框。
- AI 角色卡、用户人设卡、草稿分区。
- 导入 `.txt` / JSON 角色卡，兼容 Character Card V2 / TavernAI 字段。
- 用户人设卡可以创建多张，同一张人设卡可以分别记录与不同角色的关系和好感度。
- 世界观档案：新建、编辑、导入、导出、绑定角色。
- Grill-me 拷问生成角色卡，可保存草稿。
- 建议回复模式。
- 自主性设置和主动发言频率。
- 私聊、群聊、群聊记忆带回私聊。
- 场景切换、记忆、重要记忆、情绪、好感度自动更新。
- 按需加载角色卡段和世界书，降低 token 消耗。
- 关系管理和关系地图。

### 3.4 最近新增/修复

- **关系地图独立入口**：放在“角色扮演 → 角色卡”页面，不再和关系管理弹窗混在一起。
- **关系网络图重做**：用 `InteractiveViewer + Stack + 力导向布局` 做成可缩放、可拖动的网络图。
- 节点显示完整名字，连线显示关系和好感度，点击角色节点可查看详情。
- **修复“导入用户人设”变成 AI 角色卡**：导入用户人设时默认选中“我的人设卡”，标题和弹窗内容也对应调整。
- CI 增加 Flutter SDK 自动确认和构建重试，解决 Flet 提示安装 SDK 和 Gradle 下载断流。

## 4. 代码地图

```text
task_app/
├── main.py             # 入口 + 全部 UI，约 6200 行
├── models.py           # SQLite 数据层，约 1500 行
├── ai_client.py        # AI 技能、OpenAI 请求、角色卡/任务树解析
├── notifications.py    # plyer 本地通知 + 到期扫描
├── ui_test.py          # FakePage 驱动的 UI 冒烟测试
├── ROLE_CARD_GUIDE.md  # 生成可导入角色卡的交接指南
├── HANDOFF_TO_CLAUDE.md
├── requirements.txt    # flet==0.86.5, plyer==2.1.0
├── assets/             # 应用图标
├── android/            # 固定签名 keystore
└── .github/workflows/build-apk.yml
```

### main.py 中值得先看的方法

- `_render()`：全局视图分发。
- `_render_home()` / `_render_level()` / `_render_done()`：任务树和完成区。
- `_render_ai_center()` / `_render_ai_chat()` / `_render_group_chat()`：AI 页面。
- `_render_roleplay_home()` / `_render_roleplay_chat_page()` / `_render_roleplay_cards_page()`：角色扮演 UI。
- `_render_relation_map_page()` / `_relation_network_data()` / `_user_role_relation()`：关系地图（纯数据库辐条视图）。
- `_import_role_card()` / `_prompt_import_world()` / `_create_imported_role_card()`：角色卡和用户人设卡导入。
- `_open_relation_manager()`：关系管理弹窗。

### ai_client.py 中值得先看的内容

- `AI_SKILLS` / `SKILL_BY_ID`：内置技能和提示词。
- `chat_completion()`：OpenAI 兼容请求。
- `extract_tasks()`：解析 `---TASKS---` 大纲。
- `extract_role_card()`：解析 `---ROLE_CARD---`。
- `parse_state_block()`：解析 `---STATE---`。
- `extract_bracket_directives()` / `extract_scene_change()` / `has_remember_directive()`：括号指令。
- `match_world_book()` / `extract_load_requests()`：世界书和按需加载。
- `build_role_system()` / `build_autonomy_rule()`：角色系统提示词。
- `tavern_to_role_card()`：兼容 TavernAI/Character Card V2 JSON。

## 5. AI 内部协议

这些标记是代码和提示词之间的约定，不要轻易改：

- `---TASKS---`：任务树，后面的缩进行表示父子层级，两空格一层。
- `---ROLE_CARD---`：生成的固定格式角色卡。
- `---STATE---`：角色状态块，格式是 `键=值`，每行一个。
- `@load:段名`：AI 请求加载角色卡某个完整段落。
- `（场景切换：...）`：导演指令，不当作角色台词。
- `（记住：...）`：写入重要记忆。
- `（只让A回）`：群聊里强制单人回复。
- `（让角色们自己聊一会儿）`：群聊自动接龙。

普通聊天内容默认全部当作角色对话，只有括号里的内容才当作指令。

## 6. Flet 0.86 关键约束

版本锁死 `flet==0.86.5`，不要直接升级大版本。

- 入口用 `ft.run(main)`，不是 `ft.app`。
- `main()` 是 async，`set_allowed_device_orientations()` 需要 `await`。
- 弹窗用 `page.show_dialog()` / `page.pop_dialog()`，不是 `page.open()`。
- `DatePicker` / `TimePicker` / `AlertDialog` 都是 DialogControl。
- `FilePicker` 是 Service，要用 `page._services.register_service()` 注册，不能放进 `page.overlay`。
- `FilePicker.pick_files()` / `save_file()` 是 async，必须 `await`。
- `SegmentedButton.selected` 必须是 list，不是 set。
- 部分控件（例如 `Dropdown`）不要在构造函数里传 `on_change`，先创建再赋值。
- 列表行用原生 `ListTile`，避免自绘大 Container 导致手机端灰色大块。

## 7. 关系地图

当前实现思路：

- **关系是数据库里的唯一事实源**（`ai_role_relations` / `ai_character_relations`），聊天、群聊、关系管理、关系图都读写同一份；关系图只是它的可视化映射（纯视图，每次渲染从库重读）。
- `_relation_network_data()` 收集中心的一级关系。
- `_render_relation_map_page()` 渲染「关系中心辐条视图」：中心一张卡 + 每条直接关系一张卡（点击进详情 / 编辑 / 删除）；改关系、切中心、导入关系清单后任意一次 `_render()` 即刷新。
- 统一读法 `_user_role_relation()`（人设卡无关系时回退共享「我」卡）：聊天进入（`_open_roleplay_start`）与群聊（`_group_reply`）不再重复要求设置关系。
- 连线颜色按好感度分级：疏远灰、普通蓝、友好绿、亲密蓝绿。
- 节点显示完整名字，中心节点高亮。
- 点击非中心角色节点会打开角色详情。

后续如果要把关系图做成完整图数据库级别，可以扩展为多跳关系、按世界观过滤、节点拖动后保存坐标。

## 8. 测试

当前不依赖真机即可跑通：

```powershell
\.venv\Scripts\python.exe -m py_compile main.py models.py ai_client.py ui_test.py
\.venv\Scripts\python.exe ui_test.py
```

`ui_test.py` 应输出 `UI TEST OK`。

测试用 `FakePage` / `FakeEvent` / `FakePicker` 驱动 `TaskApp`，覆盖：

- 任务增删改查、完成、恢复、完成区。
- 标签、搜索、统计、重复任务。
- AI 会话、任务树解析、追加落库。
- 角色卡、用户人设、关系、关系地图。
- 滑动快照删除与撤销。
- 文件导出/导入、背景图。

注意：测试只能保证控件能构造、事件逻辑能跑，不能替代真机视觉验证。

## 9. 打包与发布

### GitHub Actions

`.github/workflows/build-apk.yml` 会自动：

1. 安装 Python、JDK、Flutter。
2. 安装 `requirements.txt` 和 `flet-cli==0.86.5`。
3. 下载 Flet Python manifest（带重试）。
4. 跑 `ui_test.py`。
5. 自动接受 Android licenses。
6. 自动接受 Flet 安装 Flutter SDK 的提示。
7. 构建 APK，失败自动重试。
8. 上传 `daily-tasks-apk` artifact。

构建参数：

```text
--project daily_tasks
--product "天野陽菜"
--arch arm64-v8a
--android-permissions android.permission.POST_NOTIFICATIONS=true android.permission.VIBRATE=true
--android-signing-key-store android/daily_tasks.keystore
```

固定签名 keystore 已入库，所以每次 APK 都可以覆盖安装，不丢数据。

### 推送命令

本机 HTTPS 直连 GitHub 不稳，目前用代理 + OpenSSL：

```bash
git -c credential.helper= \
  -c http.sslBackend=openssl \
  -c http.proxy=http://127.0.0.1:7890 \
  push https://zknb8584:<PAT>@github.com/zknb8584/daily-tasks.git main
```

也可以让用户运行仓库里的 `_push.bat`，按提示粘贴 PAT。

不要把真实 PAT 写进代码、文档或提交记录。

## 10. 已知限制和风险

- 通知只在 App 存活时工作，被系统杀掉后不会提醒。
- 主动发言只在 App 运行期间触发，频率和随机阈值需要真机调优。
- `main.py` 已经很大，后续大功能建议逐步拆分 UI 文件。
- 关系地图的布局算法简单，节点很多时仍可能重叠，需要真机检查。
- CI 只验证代码能构建，不代表手机端视觉 100% 正常。
- 当前 APK 只打 `arm64-v8a`，适用于小米 15 等现代 ARM 手机；需要兼容更多机型时去掉该参数。
- 导出备份不包含 AI API Key，这是隐私设计。

## 11. 给 Claude Code 的开工步骤

1. 先读 `README.md`、`HANDOFF_TO_CLAUDE.md`、`ROLE_CARD_GUIDE.md`。
2. 再读 `main.py`、`models.py`、`ai_client.py`、`ui_test.py`、`build-apk.yml`。
3. 本地跑：

```powershell
\.venv\Scripts\python.exe ui_test.py
```

4. 建议在独立分支 `claude-code` 上继续，避免和 Codex 当前 `main` 的工作互相覆盖。
5. 每次改动后跑 `py_compile` 和 `ui_test.py`，再推送触发 CI。
6. CI 成功后下载 artifact，在小米 15 上真机验证，尤其检查：
   - 关系地图缩放、拖动、节点点击。
   - 用户人设卡导入后出现在“我的角色卡”。
   - 角色扮演私聊/群聊的长期记忆和主动发言。
   - 两段式滑动是否误触。
7. 保留你的关键设计决策和真机验证记录，方便用户对比完成度。

