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

## 本地运行（桌面调试）

```bash
pip install -r requirements.txt
python main.py            # 弹出手机竖屏比例窗口
python main.py --selftest # 无界面自检（数据层 + 通知 + 备份）
python ui_test.py         # UI 逻辑冒烟测试
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
  --project "每日任务" ^
  --product daily_tasks ^
  --org com.example ^
  --bundle-id com.example.daily_tasks ^
  --build-version 1.0.0 ^
  --build-number 1 ^
  --android-permissions android.permission.POST_NOTIFICATIONS android.permission.VIBRATE ^
  --android-adaptive-icon-background "#3F51B5" ^
  --arch arm64-v8a
```

> `^` 是 Windows cmd 续行符；Linux/macOS 用 `\`。去掉 `--arch arm64-v8a` 则打全 ABI（APK 更大但兼容所有机型）。

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

- 本项目针对 **flet 0.86.x** API（`page.show_dialog` / `ft.run` / `StoragePaths` 等）。若升级 Flet 大版本，需同步适配 API 变化。
- 主要功能集中在 `main.py`，`models.py` 纯数据层、不依赖 UI，便于单测。
