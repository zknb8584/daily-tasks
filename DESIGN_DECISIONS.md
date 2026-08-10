# DESIGN_DECISIONS — Claude 独立复现 v1.1.0（滑动 + AI）

> 平行于 Codex 的独立实现，基线 v1.0.12（`2dc19fd`），分支 `claude/v1.1-swipe-ai`。
> 铁律：**全程未查看 `codex/ai-swipe` 分支的代码改动**，全部实现基于需求描述 + 本人在 flet 0.86 上的设计。
> 本文记录「为什么这样做」以及验证结果，供与 Codex 版本对比。

---

## 1. 滑动手势

### 需求
首页第一层、第二层、子视图、完成区行都支持滑动；左滑删除（级联删整棵 + SnackBar 撤销、撤销整棵恢复）；右滑完成/恢复（带子项目右滑走确认框、重复任务滚动下一次截止）；搜索/日历当天列表不启用滑动。

### 设计决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | 用 `ft.Dismissible`（flet 0.86 原生）而非自绘手势 | 动画/回弹/确认协议都是现成的，代码量最小 |
| D2 | `dismiss_direction=HORIZONTAL`，**左滑=删除、右滑=完成/恢复** | 与需求一致；左滑配 `secondary_background`（flet 文档：向左拖露出），右滑配 `background`（向右拖露出） |
| D3 | 确认协议用 `on_confirm_dismiss` + `e.control.confirm_dismiss(bool)` | 这是 flet 0.86 的「可撤销滑动」机制——未确认前 Dismissible 停在待确认态，`False` 自动回弹。比 `on_dismiss`（已消失）更适合「带子项右滑确认」 |
| D4 | 左滑删除**不弹确认框**，直接删除 + SnackBar「撤销」 | 需求明确「左滑删除 → SnackBar 撤销」；误删有 3 秒反悔窗口，比确认框更顺畅 |
| D5 | 右滑带子项才走确认框；无子项 / 重复任务 / 完成区恢复直接放行 | 确认框只出现在「会导致整棵子树状态变化」的场合，减少打扰 |
| D6 | 撤销用**快照栈**（`snapshot_subtree` / `restore_subtree`），上限 20 条 | 整棵恢复必须保留结构与完成日志；`restore_subtree` 重新分配 id，根挂回原父节点 |
| D7 | 删除时**同步清理子树的 completions 日志** | 防止完成日志残留虚增「本周完成/连续打卡」统计（这是已知数据一致性坑） |
| D8 | 滑动完成走与勾选一致的数据路径（`set_subtree_done` + `log_completions`；重复任务走 `next_deadline` 滚动） | 两条入口行为必须完全一致，避免「勾选完成的统计方式 ≠ 滑动完成」 |
| D9 | 删除背景用 `BLUE_GREY_900`（深灰蓝），**不用红色** | 保持 v1.0.12「冷静蓝、弃用红橙跳脱色」的设计基调；破坏性靠「删除」文字 + 垃圾桶图标表达 |
| D10 | 搜索 / 日历行不包 Dismissible（`swipeable=False` 直接返回原行） | 需求明确只读；也避免搜索结果滑动删除造成歧义 |
| D11 | 首页第一层行和其内联第二层行**各自独立可滑** | 需求说「第一层、第二层都支持滑动」；滑根项目=删整棵（级联），滑子项目=只删该子 |
| D12 | 背景容器加与卡片一致的 margin/border-radius | 让滑出的色块与卡片对位，视觉干净 |

### 关键实现细节
- `_dismiss_wrap(item_id, child, done, swipeable, bg_margin)`：统一的包裹器；`done` 决定右滑语义（未完成=完成/绿，已完成=恢复/青）。
- `_confirm_swipe` 是 async handler（flet 用 `inspect.iscoroutinefunction` 判断并 await）；用 `functools.partial` 绑定 `item_id/done`（partial 包 async fn 仍会被正确识别为协程函数）。
- 右滑带子项的确认框用 `asyncio.Future` + `wait_for(15s)` 阻塞等按钮结果，超时按取消回弹；避免复杂的状态机。

### 验证（ui_test.py）
- `_dismiss_wrap` 返回 `ft.Dismissible`、`dismiss_direction=HORIZONTAL`、双背景齐全；`swipeable=False` 原样返回。
- 首页第一层行确为 Dismissible；搜索行不是。
- 滑动删除 → 撤销往返：整棵恢复，子完成状态保留。
- completions 一致性：子树完成后记日志 → 删除整棵 → `week_done` 减 1（日志不虚增）→ 恢复后可再删。
- 重复任务右滑完成：不进入完成区、截止时间滚动。

---

## 2. AI 助手

### 需求
顶栏 AI 图标 → AI 中心（技能列表 + 会话历史持久化）；技能「AI 拷问拆解 / 快速拆解 / 通用问答」；项目菜单「AI 拆解」带项目上下文；回复含 `---TASKS---` → 「预览并生成任务树」追加到项目不覆盖；通用问答可复制/分享/存为项目备注；「设置 → AI 设置」配 Base URL/Key/模型/测试连接/清除 Key，Key 只存本机。

### 设计决策

| # | 决策 | 理由 |
|---|---|---|
| A1 | AI 客户端**只用标准库 urllib**，`chat_completion()` 返回 `(ok, 文本|中文错误)` | 不引入 requests/openai SDK 依赖，APK 体积与 CI 依赖更稳；错误信息中文友好（无 Key/鉴权失败/429/超时/格式异常） |
| A2 | 三个技能用**自定义中文 system prompt**，输出以 `---TASKS---` 标记引导 | prompt 直接教模型输出大纲格式，`extract_tasks()` 解析标记后按「2 空格=1 层缩进」分层 |
| A3 | 会话持久化：`ai_sessions` + `ai_messages` 两张表 | 需求「会话历史持久化」；消息按会话级联删除；「我的会话」按 `updated_at` 倒序 |
| A4 | 每次发送 = 该会话**全量消息重发**（含 system 上下文 + 技能 prompt） | 无状态服务端，最简单可靠的上下文方案；会话内消息量不大 |
| A5 | **Key 只存在本地 SQLite**，导出备份不含；设置页可清除 | 隐私边界：备份/迁移不带走 Key |
| A6 | 顶栏新增 AI 图标；会话页/中心用 AppBar 返回，`_back()` 优先关 AI | 与完成区/搜索/日历的导航一致 |
| A7 | 「AI 拆解」用 `quick` 技能 + 注入 system 上下文（项目标题/截止/备注/已有子任务） | 项目上下文足够时直接出大纲比拷问式更贴合「拆解」语义 |
| A8 | `---TASKS---` 结果不直接入库，先「预览并生成任务树」对话框 → 确认追加；有项目上下文直接追加，否则弹项目选择 | 需求「不覆盖已有子任务」——`append_tasks` 只追加，逐条保留原 id 语义 |
| A9 | 追加用 `append_tasks(parent_id, [(level,title,deadline)])`：level 0 → 目标项目，level N → 最近一个更浅层节点 | 与导入计划的层级逻辑统一 |
| A10 | 通用问答气泡带复制/分享/存为备注；存备注落到项目 note 字段并加 `[问 AI]` 来源标记 | 需求明确；来源标记便于用户辨识 AI 生成内容 |
| A11 | 网络请求放 `asyncio.to_thread` 防卡 UI；回复期间发送按钮禁用 + 转圈「正在思考…」 | 不阻塞主线程；避免连点重复请求 |
| A12 | AI 设置对话框支持「测试连接」（真实调一次 `/chat/completions`）与「清除 Key」 | 需求要求；测试用 `timeout=20` 的轻量请求 |

### 关键实现细节
- `extract_tasks`：只收集 `---TASKS---` 之后的行；行尾 ` | YYYY-MM-DD[ HH:MM]`（用 `parse_deadline` 校验）拆成截止时间；空行忽略。
- 对话页输入框引用存 `self._ai_input`，避免每次 `_render` 重建导致失焦/丢字。
- 会话标题自动取首条用户消息前 12 字。

### 验证（ui_test.py）
- `ai_config`/`save_ai_config`/`clear_ai_key` 读写往返。
- AI 会话 CRUD：创建/读标题/改标题/加消息/`ai_sessions` 排序/级联删除消息。
- `extract_tasks`：多级缩进 + 截止时间解析、无标记返回空。
- AI 中心渲染（技能列表）、`_start_ai_session` 切会话页、未配 Key 时发送返回引导文案（不联网）。
- 「AI 拆解」注入 system 上下文（含项目标题）。
- 预览 → 追加：任务树追加到目标项目、不覆盖已有子任务、截止时间保留。
- AI 设置对话框：保存配置 / guard 复位可重开。

---

## 3. 其他

- **铁律遵守情况**：分支从 `2dc19fd`（v1.0.12）创建，新增/修改仅 `main.py`、`models.py`、`ai_client.py`、`ui_test.py`、本文件；未查看过 `codex/ai-swipe` 任何代码。若实现与 Codex 有相似之处，来自相同需求描述与 flet 0.86 的同一套 API 约束，属正常趋同。
- **自检**：`ui_test.py` → `UI TEST OK`；`main.py --selftest` → `SELFTEST OK`。
- **未做**：本分支只做实现与测试，**不出 APK、不合 main**；供用户与 Codex 分支对比后决定。
