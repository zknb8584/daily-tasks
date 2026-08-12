# 每日任务（Daily Tasks）

基于 **Python + Flet** 的安卓待办 App，数据全部保存在手机本地，离线可用。

## 功能

- **无限嵌套项目树**：大项目下面可以挂子项目，子项目还能再挂，任意层级。
- **新建 / 编辑 / 删除 / 勾选完成 / 恢复**，随时可改标题和截止时间。
- **截止时间**：日期 + 可选时分（时间留空按「整天」处理，即当天 23:59 截止）。
- **显示规则（每个层级一致）**：

  | 状态 | 显示 |
  |---|---|
  | 未完成 | 主列表（按截止时间升序，紧急靠前） |
  | 已完成，但下面还有未完成的子项 | 主列表划线沉底 |
  | 已完成 **且** 整棵子树完工 | 移入本层底部「已完成 (N)」折叠区，可一键恢复 / 批量清除 |
  | 任意勾选完成 | 弹出「已标记完成 · 撤销」提示，防止误触 |

- **进度显示**：带子项的项目自动显示「进度 x/y」。
- **本地通知**（应用存活时）：后台每分钟扫描，对「即将到期 / 已过期」任务弹系统通知；每次打开 App 也补发。
- **每日一句**：在「设置 → 编辑每日一句」里写句子（每行一句），每次打开 App 在首页顶部随机显示一条，点击可换一句。
- **滑动手势**：左滑删除、右滑完成/恢复（完成区右滑恢复）；删除后弹「撤销」防误触。
- **AI 助手（可选用）**：顶栏 AI 图标进入 AI 中心，内置「AI 拷问拆解 / 快速拆解 / 通用问答」三个技能；多轮对话、会话历史持久化；任务拆解确认后追加到项目；通用问答可复制、分享、存为项目备注。API 走 OpenAI 兼容接口（默认 DeepSeek），在「设置 → AI 设置」填 Base URL / API Key / 模型即可，Key 只存本机。
- **AI 中心三板块**：AI 中心按「生成任务树 / 回答与解惑 / 角色扮演」归类，聊天记录也按板块展示。
- **角色聊天列表**：生成角色卡后，角色扮演板块会显示以角色命名的聊天条，点击直接进入私聊；聊天页右上角可查看角色详情。
- **任务树任意挂载**：AI 问答结束后生成任务树，可在弹窗里选择挂载到任意现有项目，或作为新的顶层项目。
- **两段式滑动**：滑动任务先提示，再滑一次才执行删除/完成，减少误触。
- **课堂速解**：上课遇到没听过的词或概念，直接输入一段话/术语，AI 给大白话解释 + 生活例子 + 记忆点。
- **角色扮演（长期聊天框）**：导入 `.txt`/JSON 角色卡（固定 `[核心] [背景] [爱好] [说话风格] [关系] [开场白] [示例对话] [替代开场] [作者备注] [世界观] [扩展] [记忆] [当前情绪] [好感度] [系统提示] [历史后置指令] [世界书]` 段名），兼容 Character Card V2/TavernAI 的 `first_mes`、`mes_example`、`alternate_greetings`、`creator_notes`、`system_prompt`、`post_history_instructions`、`character_book`；新角色会自动使用 `first_mes` 作为开场白，`mes_example` 会作为示例对话注入系统提示；开始对话前可选初始身份和好感度；普通输入默认全部当角色对话，只有括号里的内容（如 `（场景切换：…）`）才作为导演指令；每个角色一个永久聊天框；可手动编辑角色卡、编辑记忆、绑定世界观；当前场景会持久保存；按需加载角色卡段省 token；世界书会按用户消息里的关键词自动加载；角色会从 `[爱好]` 等设定里主动发起话题；好感度/情绪/记忆自动更新；括号内的「记住」指令会写入重要记忆；可重置关系、删除角色卡。
- **Grill-me 拷问角色卡**：AI 中心和「开始角色扮演」弹窗都可进入；可选择已有世界观或新建；AI 按模板一次只问一个问题，薄弱处会反复追问，并维护进度条和已确认设定摘要；最后输出 `---ROLE_CARD---`，可预览并一键加入角色卡列表。
- **AI 群聊**：AI 中心新增「我的群聊」，可选 2~4 个角色创建群聊；群聊消息持久保存，`@角色名` 时随机让 1~3 个总人数接话，`（只让A回）` 可强制单人；支持 `（场景切换：...）`、`（记住：...）`、`（让角色们自己聊一会儿）`；普通输入同样按角色对话处理；群聊共享当前场景；角色在群聊中产生的「群聊记忆」会带回私聊，角色独立的好感度/情绪/记忆仍然全局保留。
- **自定义背景图**：「设置 → 设置背景图」从相册选一张作为背景（本地保存，离线可用），可随时清除；有 Pillow 时会自动压缩，没有则用原图。
- **配色**：冷静蓝主题（深蓝 / 浅蓝），弃用红橙等跳脱色；截止时间用蓝梯度表达紧急程度（浅蓝=常态、中蓝=临近、深蓝加粗=已过期）。
- **备份**：设置页一键导出 / 导入 JSON。
- **界面**：中文、竖屏锁定、大点击区域，为手指操作优化。

## 项目结构

```
task_app/
├── main.py            # 入口 + 全部 UI（flet 0.86 API）
├── models.py          # SQLite 数据层（递归树、截止时间、备份序列化）
├── notifications.py   # plyer 本地通知 + 到期扫描去重
├── ui_test.py         # 开发用 UI 冒烟测试（不弹窗口）
├── requirements.txt
├── assets/
│   ├── icon.png               # 启动图标（旧式）
│   └── icon_foreground.png    # 自适应图标前景
└── .github/workflows/build-apk.yml   # GitHub Actions 自动打包
```

## 开发环境（推荐：venv）

依赖已锁版本（`requirements.txt`：flet 0.86.5 + plyer），推荐用虚拟环境隔离，**不污染全局 Python**：

```bash
python -m venv .venv                                                       # 首次：创建
# 之后每次开始开发先激活（Windows）：
#   PowerShell:  .\.venv\Scripts\Activate.ps1
#   cmd:         .\.venv\Scripts\activate.bat
#   Git Bash:    source .venv/Scripts/activate
\.venv\Scripts\python -m pip install -r requirements.txt                   # 首次：装依赖
```

激活后 `python` / `pip` 即指向 venv。不激活也能用绝对路径跑：`\.venv\Scripts\python ui_test.py`。

> venv 仅用于本地开发测试；**APK 打包在 CI 里做**（干净环境装同一份 requirements），本地 venv 不影响打包。全局 Python 里的包保持原样，不会被本项目改动。
> 桌面弹窗调试可额外装 `flet-desktop==0.86.5`；背景图压缩可额外装 `pillow`。这些都不进 requirements，避免被 Android 打包阶段当作目标运行依赖打包（flet-cli 只在 CI 构建机单独安装）。

## 本地运行（桌面调试）

```bash
\.venv\Scripts\python main.py            # 弹出手机竖屏比例窗口
\.venv\Scripts\python main.py --selftest # 无界面自检（数据层 + 通知 + 备份）
\.venv\Scripts\python ui_test.py         # UI 逻辑冒烟测试
```

> 桌面端弹窗需要联网下载 Flet 桌面客户端（GitHub Releases）；若网络不通，
> 程序逻辑不受影响，可跳过弹窗，直接用 `--selftest` / `ui_test.py` 验证。

## 打包 Android APK

Flet 0.86 的 Android 打包基于 **Flutter 工具链**（不是老 buildozer），**Windows / macOS / Linux 原生均可构建**，无需 WSL。

### 方案一：Windows 本地打包（推荐）

**1. 安装依赖（一次性的）**

| 软件 | 说明 |
|---|---|
| Python 3.10+ | 已有则跳过 |
| Flutter SDK | 官网 https://flutter.dev（国内用镜像 https://flutter.cn 或环境变量 `FLUTTER_STORAGE_BASE_URL=https://storage.flutter-io.cn`、`PUB_HOSTED_URL=https://pub.flutter-io.cn`） |
| Android SDK | 装 Android Studio 即可自带；或只装 command-line tools + platform-tools + build-tools + platform |
| JDK 17 | Android Studio 自带 JBR；或 Temurin 17 |

**2. 配置环境变量**

```
ANDROID_HOME = C:\Users\你的用户名\AppData\Local\Android\Sdk
PATH 追加    = %ANDROID_HOME%\platform-tools; Flutter SDK 的 bin 目录
```

**3. 验证环境**

```bash
flutter doctor          # 确认 Android toolchain 一栏为绿色
yes | flutter doctor --android-licenses
flet doctor
```

**4. 构建 APK**（在项目目录 `task_app/` 下）

```bash
pip install flet

flet build apk ^
  --project daily_tasks ^
  --product "每日任务" ^
  --org com.example ^
  --bundle-id com.example.daily_tasks ^
  --build-version 1.0.0 ^
  --build-number 1 ^
  --android-permissions android.permission.POST_NOTIFICATIONS=true android.permission.VIBRATE=true ^
  --android-adaptive-icon-background "#3F51B5" ^
  --arch arm64-v8a
```

> `^` 是 Windows cmd 续行符；Linux/macOS 用 `\`。去掉 `--arch arm64-v8a` 则打全 ABI（APK 更大但兼容所有机型）。

> **签名说明**：仓库里已含 `android/daily_tasks.keystore`（CI 用它固定签名，密码 `DT-apk-2026-key`）。这样每次构建出的 APK 签名一致，手机**直接覆盖安装即可升级，无需卸载、不丢数据**。仓库是私有的，请勿公开。

首次构建会联网拉取 Flet 的 Flutter 模板、Flutter/Gradle 依赖、并打包 Python 运行时，**需要 10~30 分钟**；成功后：

- 输出目录：`task_app/build/apk/`
- APK 文件：`task_app/build/apk/app-release.apk`

把 `app-release.apk` 传到手机（微信/QQ/数据线）即可安装。手机需允许「安装未知来源应用」。

### 方案二：GitHub Actions 云打包（零本地安装）

仓库里已带 `.github/workflows/build-apk.yml`。把项目推到 GitHub 后：

1. 在仓库 **Actions** 页找到 "Build Android APK"，点 **Run workflow**；
2. 等构建完成（约 20~40 分钟）；
3. 在本次运行结果下方 **Artifacts** 里下载 `daily-tasks-apk`，解压得到 APK。

> 国内访问 GitHub 可能较慢或偶尔失败；网络不稳时建议用方案一。

### 方案三：Flet 云构建

Flet 官方提供云端打包服务，详见 https://flet.dev/docs/publish —— 需注册 Flet 账号，个别功能收费。

## 关于本地通知

- 机制：App 存活时后台线程每 10 分钟扫描一次 + 打开时补发；任务被系统杀掉后不再提醒（这是「简单本地通知」方案的已知边界）。
- 打包时已通过 `--android-permissions` 声明 `POST_NOTIFICATIONS`（Android 13+ 需要）。
- **真机验证**：装好 APK 后，进「设置 → 测试通知」。若通知不弹：
  1. 在系统设置里给「每日任务」开启「通知」权限；
  2. 若仍不行，说明 plyer 在当前嵌入的 Android Python 环境下不可用 —— 通知会自动降级为静默（不影响其他任何功能），可在 issues 反馈。
- 桌面调试时通知会打印到控制台（`[通知] ...`）。

## 数据存储与备份

- SQLite 数据库存于应用私有目录（`FLET_APP_STORAGE_DATA`），**无需任何权限、离线可用**。
- 卸载 App 前请先「设置 → 导出备份」；换手机后在「设置 → 导入备份」恢复。
- 桌面调试时数据在项目 `data/` 目录，方便直接查看。

### 用大纲一键生成任务备份（推荐工作流）

把一个大项目拆成任务树，让 AI 或自己写一个**缩进大纲**，再转成备份文件导入手机：

```
毕业论文
  文献综述
  方法论
    实验设计
    数据采集 | 2026-08-15
  论文撰写 | 2026-08-31 18:00
```

```bash
python gen_backup.py 示例大纲.txt -o 任务备份.json   # 生成计划文件
```

然后把这个 JSON 传到手机，在 App 里「设置 → **导入计划**」选择它 —— 任务树会**追加**为新的顶层项目，**不会覆盖**你手机里已有的任务。

> 规则：缩进 = 层级（空格/Tab 皆可）；`标题 | 截止时间` 可带截止时间（`YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM`）；空行和 `#` 开头的行忽略。
>
> 两个导入入口的区别：**「导入计划」= 追加**（外部计划常用，安全）；**「导入备份」= 整体覆盖还原**（换机/恢复用，会替换全部任务）。

## 常见问题

**Q：构建时下载很慢/失败？**
设置镜像（国内）：Flutter 用 `https://flutter.cn` 的 SDK，Gradle 可在 `~/.gradle/init.gradle` 配置阿里云 maven 镜像。构建还需联网拉取 Flet 的 Flutter 模板（默认锁定的版本号与 flet 一致），网络不通会卡在 `Creating Flutter project...`，重试或换网络环境。

**Q：`flet doctor` 提示 Android toolchain 缺失？**
装 Android Studio，打开一次让它下载 SDK；在 `flutter config --android-sdk <SDK路径>` 里指定 SDK 位置。

**Q：APK 装到手机提示「未安装」？**
可能是 ABI 不匹配。去掉 `--arch arm64-v8a` 重打全 ABI 版本，或确认手机是 64 位。

**Q：勾错了怎么办？**
每次勾选完成都有 3 秒「撤销」；已完成的在底部「已完成」区点「恢复」即可。

## 开发说明

- 本项目针对 **flet 0.86.x** API（`page.show_dialog` / `ft.run` / `StoragePaths` 等），`requirements.txt` 已锁定 `flet==0.86.5`。若升级 Flet 大版本，需同步适配 API 变化。
- 主要功能集中在 `main.py`，`models.py` 纯数据层、不依赖 UI，便于单测。
