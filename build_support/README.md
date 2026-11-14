# QQBot 多模块运行包

`QQBot.exe`（核心）、`QQBotStudio.exe`（GUI）、`QQBotCLI.exe`（命令行）、`QQBotMCP.exe`（stdio 服务）分别内置预编译 Python 模块，共享 `_internal` 中的 Python 运行时和 DLL。整体复制运行目录，不要拆开移动 EXE，也不要删除 `_internal`。适用于具有系统 ICU 的 Windows 10/11 x64；Studio 需要 Microsoft Edge WebView2 Runtime。不采用单文件临时解压，不在启动时编译项目源码；仍需正常导入模块，模型和网络等待时间不由封装消除。

现有项目直接双击根目录 `run_gui.bat` 或 `run.bat`，优先使用编译运行包，并通过 `--home` 复用原账号配置、人设、记忆、缓存和已安装的 NapCat/QEMU。设置环境变量 `QQBOT_USE_SOURCE=1` 可回到源码入口。不要同时启动两个操作同一账号的核心服务。

根目录启动脚本先读取 `runtime/active-runtime.txt` 中相对于 `runtime` 的运行包目录；不存在或所选 EXE 缺失时，回退到 `runtime/QQBot`。更新时可以把验证后的四模块包放在独立版本目录，再修改这一行路径。正在运行的旧进程继续使用原文件，退出控制台并从 `run_gui.bat` 重新打开后才使用新版。

2026-11-10 结构化记忆版保留原会话 CID 与身份；协议变动和个人成长分别增量送入。启动时自动关联旧自主浏览任务的回复确认、补建旧归档的事件/人物索引。每次归档原子维护事件簿、通讯录与成长复盘待办，支持时间、人物与标签查询。自主探索默认只读工作区和已授权资料，持续保存上次发现及下一步；自主活动仍由现有开关和间隔控制。

独立部署时，把整个运行目录复制到目标 Windows x64 电脑，为它准备一个数据目录；复制 `config.yaml.example` 为 `config.yaml`，填写账号配置，再按需复制已有的 `personas.yaml`、`data` 和外部工具。运行包不包含个人配置、Cookie、记忆和测试资料。NapCat、Antigravity、浏览器、ffmpeg、QEMU 仍按功能需要单独配置。

```powershell
.\QQBotStudio.exe --home 'D:\QQBotData'
.\QQBot.exe --home 'D:\QQBotData'
.\QQBotCLI.exe --home 'D:\QQBotData' help
.\QQBotMCP.exe --home 'D:\QQBotData'
.\QQBot.exe --home 'D:\QQBotData' --module actor status
.\QQBot.exe --home 'D:\QQBotData' --module actor run
```

MCP 的 stdout 只输出 JSON-RPC。主代理、子代理和 CLI 示例自动使用对应 EXE，并携带同一个数据目录；GUI 启动核心时也会选择核心 EXE。

源码更新后需重新构建，运行包不会动态加载根目录 `.py`。在具备 `requirements.txt` 依赖和 PyInstaller 6 的 Python 环境执行 `python maintenance/build_runtime.py --output runtime-new`，验证后再替换旧目录。构建器拒绝覆盖已有包，账号数据不在替换范围。包内 `build-manifest.json` 记录每个文件的哈希，便于检查版本。

2026-11-12 新增聊天积压中断合并：同一会话默认积压 3 条原始新消息后，停止当前自然回复及未发送的后续气泡，将原问题、补充消息和已发送内容一起交给 AI 判断如何回复。发送中的单个接口请求先等待回执，已提交的后台任务继续执行；保留原聊天 CID 和记忆。GUI“设置 → 消息排队”可调整阈值，`bot.backlog_interrupt_messages: 0` 关闭。

启动检查：`QQBot.exe --home <临时数据目录> --check` 只导入核心，不连接 QQ 或模型；`--check-dependencies` 额外检查 OCR、网页端等依赖。GUI 离屏检查用 `QQBot.exe --module gui --home <临时数据目录> --check-gui`，只在该临时目录生成默认配置。真实验收需由用户正常启动。

2026-11-13 Studio 改为 pywebview + WebView2，使用本地网页资源和 SVG 图标。左下“外观与个性化”可设置浅色/深色/系统主题、玻璃浓度 35–100%、模糊 0–48px、整窗不透明度 65–100%、字号 12–17px、原生 Acrylic 和减少动画；外观自动保存到数据目录 `data/studio/appearance.json`，立即生效。已有配置、人设和记忆沿用原目录。账号、排队、记忆和模型选项在“设置”；网站账号在“资源与任务 → 角色与网站”。正常关闭会请求核心保存并停止。

`--check-gui` 在独立数据目录打开隐藏的真实 WebView2 窗口，检查页面渲染、JS bridge、原生不透明度，并生成 `studio-check.json`。不会启动 QQ 核心或连接模型。Windows 原生 Acrylic 不可用时保留 CSS 玻璃。
