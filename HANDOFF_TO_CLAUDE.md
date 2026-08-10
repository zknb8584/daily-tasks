# 天野陽菜 · 给 Claude 的交接说明

## 我们在做什么

这是一个 **Python + Flet 的安卓本地任务 App**，当前正式名「天野陽菜」（包名 `daily_tasks`）。
你看到这份文档时，Codex 已经把第一版滑动 + AI 功能实现并测试通过；这份文档的作用是让你
独立地了解项目全貌、本轮设计共识、已实现内容和需要你继续推进的部分。

## 项目现状

- 代码目录：`D:\sicence practice\task_app`
- 当前稳定版本：v1.0.12（上一轮交付的 APK 已装到小米 15）
- 本轮 Codex 已完成代码：v1.1.0（尚未构建 APK）
- 仓库：GitHub 私有仓库 `zknb8584/daily-tasks`
- 本地开发环境：`task_app/.venv`（Python 3.12，flet==0.86.5）
- APK 构建：GitHub Actions（固定签名 keystore 已入库，覆盖安装不丢数据）

## 已完成的核心功能（v1.0.12 起）

1. 无限嵌套项目树（parent_id 递归），第一层 + 第二层首页树形展示（大字号 + 缩进小字号）
2. 截止时间（日期 + 可选时分）、按截止时间排序、标签 + 标签筛选
3. 全局完成区（整棵子树完成后移入，可撤销恢复、清除全部）
4. 勾选大项目完成有「整个项目都完成了吗？」确认框防误触
5. 搜索（全树）、任务备注、统计概览、重复任务（每天/每周/每 N 天）
6. 日历视图（按月看截止任务、点选日期看当天任务）
7. 每日一句、自定义背景图、JSON 导出/导入、导入计划（追加不覆盖）
8. 本地通知（App 存活时扫描到期/过期）
9. 自定义应用名「天野陽菜」+ 自定义图标

## 本轮 Codex 已实现（v1.1.0，代码已写、测试已过）

### 滑动手势
- 首页第一层、第二层、子视图、完成区行都支持滑动
- 左滑删除（级联删除整棵子树），删除后弹 SnackBar「撤销」，撤销会整棵恢复
- 右滑完成/恢复；带子项目右滑完成仍走确认框；重复任务右滑完成会滚动截止时间
- 搜索列表、日历当天列表不启用滑动（查询视图保持只读）

### AI 助手（OpenAI 兼容接口，默认 DeepSeek）
- 顶栏新增 AI 图标 → AI 中心（技能列表 + 我的会话，会话持久化到 SQLite）
- 内置技能：
  - **AI 拷问拆解**：grill-me 风格，一次一问、根据回答追问，最后输出任务大纲
  - **快速拆解**：直接根据项目信息生成任务大纲
  - **通用问答**：普通对话，回答可复制 / 分享 / 存为项目备注
- 项目菜单新增「AI 拆解」入口，自动把项目标题、截止、已有子任务作为上下文
- AI 回复出现 `---TASKS---` 标记时，对话页出现「预览并生成任务树」按钮；
  确认后把大纲追加到项目下，不覆盖现有子任务
- 「设置 → AI 设置」：Base URL / API Key / 模型 / 测试连接 / 清除 Key
- Key 只存本地 SQLite；不配置 Key 时 AI 功能不可用，不影响其余功能

## 关键技术点（Claude 务必注意）

- Flet 版本锁定 `flet==0.86.5`，代码按 0.86 API 编写：
  `ft.run(main)`、`page.show_dialog/pop_dialog`、`page._services.register_service(FilePicker/Share)`、
  `DatePicker/TimePicker` 都是 DialogControl、`FilePicker.pick_files/save_file` 是 async 必须 await。
- 手机端出现过的「灰色大块」问题：避免自绘 `Container(expand=True)` 行，列表行统一用原生 `ListTile`。
- 滑动手势用 `ft.Dismissible`：
  `dismiss_direction=ft.DismissDirection.HORIZONTAL`，`on_dismiss` 事件带 `direction`，
  `END_TO_START` 左滑、`START_TO_END` 右滑；`background` 是右滑背景，`secondary_background` 是左滑背景。
- AI 请求在 `ai_client.py`：只用 Python 标准库 `urllib`，OpenAI 兼容
  `POST {base}/chat/completions`；在事件处理器里用 `asyncio.to_thread` 包阻塞请求，避免卡 UI。
- AI 会话表：`ai_sessions` / `ai_messages`；设置存在 `settings` 表（`ai_base_url` / `ai_model` / `ai_api_key`）。
- 导出备份**不包含** AI 会话和 API Key（隐私设计）。

## Codex 已写的文件

- `ai_client.py`：技能定义、OpenAI 兼容请求、任务大纲解析（`extract_tasks`）
- `models.py`：AI 会话 CRUD、AI 配置、子树快照/恢复
- `main.py`：AI 中心、对话页、AI 设置、滑动包装、任务树落库
- `ui_test.py`：已覆盖 AI 会话、解析、追加、滑动快照恢复、Dismissible 构造

## 建议 Claude 的推进方式

1. 先读 `README.md`、`HANDOFF_TO_CLAUDE.md`、`main.py`、`models.py`、`ai_client.py`。
2. 本地验证：`task_app/.venv/Scripts/python.exe ui_test.py`，应输出 `UI TEST OK`。
3. 在独立工作目录或独立分支上继续，避免与 Codex 的改动互相覆盖。
4. 重点可继续的方向（按价值排序）：
   - AI 会话页真正发网络请求后的真机验证与错误提示打磨
   - 通用问答「存为备注」时让用户自由选择挂到哪个项目
   - 滑动删除的「撤销」在完成区/搜索里的细节一致性
   - AI 技能支持用户自定义提示词模板
5. 完成后再做 CI 构建：push 到 GitHub 私有仓库 `zknb8584/daily-tasks` 的独立分支，
   Actions 会自动出 APK（已配好固定签名）。

## 关于对比

你与 Codex 是同一需求的两份独立实现，最后用户会对比完成度。请保留你的关键设计决策和
验证结果（尤其是真机验证），提交到独立分支，方便用户比较。
