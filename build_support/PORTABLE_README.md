# QQBot Studio：pywebview 便携运行包

2026-11-16 版接入 VoiceStudio 轻量流式服务，支持同一句的多个 `[emotion:标签]`、实时本机试听、降噪和语音缓存。唱歌支持原唱翻唱、伴奏创唱、人声降噪与分轨缓存。主代理语音经过假名化、合成、腾讯 SILK 转码与正常队列发送。`voice.bat` 提供安装、诊断和试听入口，详见 `_internal/VOICE_README.md` 和 `_internal/SINGING_README.md`。

语音工作室切换角色会立即保存并显示 Bot 应用回执。“声音训练”提供音频切分、转写、标签复核、GPT 与 SoVITS 双阶段微调、恢复检查点、留出文本验证和新角色注册。支持 CSV、JSONL 和 GPT-SoVITS .list，默认 v2ProPlus；小语料和未复核标签会标记为实验模型。训练环境、素材和角色权重保存在数据目录，不随干净运行包分发。详见 `_internal/VOICE_TRAINING_README.md`，CLI 入口为 `voice.bat train status`。

主程序带假名词典、SILK 编码器和 DSH 独立运行时。先用 `voice.bat setup --python C:\Python312\python.exe --device cuda` 安装 GPT-SoVITS 独立环境与模型，再用 `voice.bat studio-setup` 安装固定版本 VoiceStudio 组件（需 Git、Python 3.10–3.12、ffmpeg）。默认本机流式地址 9881，原生后端 9880。ATRI 样板为非商业评估用途，保留模型来源、作者署名与许可；可用情绪取决于真实参考切片。初次安装后在语音工作室开启语音和自动启动；全新解压包默认不启用语音。QQ record 在完整 SILK 编码后发送，本机控制台支持流式试听。

DSH 默认不切换；模型设置里可选择 `deepseek_harness`，在启动环境设置 `DEEPSEEK_API_KEY`。程序使用官方 SDK 0.2.2rc2，沿用原生会话历史，AG 与 DSH 的映射分别保存。界面“检查”只验证 SDK 握手，不消耗模型请求。

适用于 Windows 10/11 x64，保留系统 ICU。无需安装 Python；图形界面需要 Microsoft Edge WebView2 Runtime。解压整个目录后使用，四个 EXE 与 `_internal` 必须放在一起。解压目录及数据目录应可写。

此版本修复自主浏览任务的回复确认关联，并加入事件簿、人物结构化资料和逐次成长复盘。QQBot 已有用户可继续双击原项目的 `run_gui.bat` / `run.bat` 使用选定运行包，完整退出旧核心并重开后生效；保留账号配置、人设、聊天 CID 和记忆。独立解压包复用旧项目时，按下文设置 `QQBOT_HOME` 指向旧项目根目录。

空闲探索沿用上次发现、兴趣与下一步，范围为 bot 工作区和已授权资料目录及公开网络。`self_activity_enabled`、`autonomous_enabled` 需同时开启，默认间隔 20 分钟；不自动给新用户开启。归档时人物与事件分区，带时间线、标签和查询，成长层更改会增量生效。控制台 `help events`、`help memory`、`help explore` 可查看接口；主代理使用同一 MCP 接口。

聊天期间同一会话默认积压 3 条原始新消息时，中断当前自然回复，将原问题、积压消息与旧轮发送记录一起交给 AI 综合判断。已发出的内容保留，未发送的后续气泡停止，后台任务继续；同一发言人已预合并的消息仍按原始条数计数。可在消息排队设置调整阈值，`bot.backlog_interrupt_messages: 0` 关闭。完整退出旧核心并重开后使用新版。

## 图形界面与外观

Studio 使用 pywebview 和本地网页资源，导航使用 SVG。左下“外观与个性化”提供浅色、深色、系统主题，以及玻璃浓度、模糊、窗口不透明度、字号和原生 Acrylic 开关。外观立即生效并自动保存到数据目录的 `data/studio/appearance.json`。账号、队列和模型参数在“设置”，保存后重启核心使连接参数生效。

记忆按事件簿、通讯录和成长分区；话术表、活动记录、Gemini 浏览器连接、本地情感分析与训练沿用原有数据。独立 BERT 工作台不包含在运行包中，可填写已有推理服务描述路径。关闭控制台会先请求核心保存并停止。

## 首次使用

1. 双击 `run_gui.bat` 打开控制台。脚本首次运行会建立 `data/config.yaml`，默认采用独立 `agent` 模式，无需 QQ 凭据。
2. 在“资源与任务 → 角色与网站”页配置角色和通用网站账号。该页编辑 `agent` 下的内容，不要再包一层 `agent:`。具体平台后续通过网址、身份和页面控件配置。
3. 保存后从 GUI 启动核心，或运行 `run.bat`。初始角色和账号为空，不会自动操作网站。自主理解与决策需要本机已配置的 Antigravity；账号浏览需要已安装的 Chrome/Edge/Chromium。

双击 EXE 时不会经过首次配置脚本；建议使用以上 BAT。`README.md` 是底层运行时说明；本文件说明便携包入口。

## 命令入口

在解压目录打开 PowerShell：

```powershell
.\actor.bat status
.\actor.bat login --account my_site
.\actor.bat read --account my_site --url https://example.com/article
.\actor.bat draft --account my_site --text-file D:\draft.txt --request-key draft-001
.\actor.bat publish --account my_site --draft <草稿ID> --request-key publish-001
.\cli.bat help actor publish
.\mcp.bat
.\run.bat --check
.\run.bat --check-dependencies
.\run.bat --module gui --check-gui
```

`mcp.bat` 提供 stdio JSON-RPC，stdout 只输出协议内容。首次配置不会额外输出提示。

默认所有入口共用包内 `data` 作为数据根目录；配置、记忆、账号浏览器目录和日志都写在里面。需要复用已有数据时，在 PowerShell 中设置环境变量后再启动：

```powershell
$env:QQBOT_HOME = 'D:\MyAgentData'
.\run_gui.bat
```

外部数据目录存在 `config.yaml` 时完整保留；没有时创建默认配置。将旧项目根目录作为 `QQBOT_HOME` 可以复用旧配置，不要同时启动两个操作相同账号或数据目录的核心服务。QQ 仍可通过原有 `official / onebot / dual` 模式接入；包内另保留未修改的 `config.yaml.example`。

## 通用网站最小示例

在 GUI 的“资源与任务 → 角色与网站”页填写以下内容，替换网址与订阅链接：

```yaml
enabled: true
autonomous_model_enabled: true
actors:
  alice:
    name: 小艾
    prompt: |
      你喜欢音乐、游戏和日常观察，有稳定的性格与自己的判断。
      根据实际阅读形成观点，区分来源作者的经历与自己的观察。
      自然交流，记住上下文，不为表现活跃而强行开口。
bindings: {}
accounts:
  my_site:
    actor_id: alice
    platform: website
    enabled: true
    home_url: https://example.com/
    allowed_origins: [https://example.com]
    headless: true
    feeds: [https://example.com/feed]
    interval_seconds: 1800
    actions: [read_content, read_feed, create_draft]
```

登录运行 `actor.bat login --account my_site`，在打开的专用浏览器完成登录后，回终端按 Enter 保存。后续活动复用该账号独立的持久目录。

需要发布或评论时，给对应账号增加 `identity`、`expected_identity` 和实际页面定位规则，再将 `publish_post` 或 `reply_comment` 加入 `actions`。例如发布：

```yaml
    identity: {test_id: account-name}
    expected_identity: my-account-name
    publish:
      composer_url: https://example.com/compose
      editor: {role: textbox, name: 正文}
      submit: {role: button, name: 发布}
      confirmation: {test_id: publish-success}
      result_link: {test_id: published-link}
```

以上是网站结构示例，必须按目标页面配置。定位器只选择 `role`（可附 `name`）、`label`、`placeholder`、`test_id`、`css` 中的一种主要方式。身份必须精确匹配；成功提示须在提交前不可见、提交后出现。评论规则放在 `comment` 下，草稿用 `draft --kind reply_comment --url <帖子地址>`，再用 `comment --account my_site --draft <草稿ID>` 提交。

每次动作返回 JSON 回执。`confirmed` 表示取得已配置的成功证据，`unknown` 表示提交结果不明。重复请求键不会重复执行；出现 `unknown` 时先看证据和实际页面，不换键自动重发。默认只启用阅读与本地草稿；具体平台配置和登录后才能进行对应网站操作。

## 文件校验与更新

ZIP 旁的 `.sha256` 是完整压缩包的 SHA-256。在 ZIP 所在目录可用 `Get-FileHash .\QQBot-*.zip -Algorithm SHA256` 核对。包内 `package-manifest.json` 记录运行文件、脚本和模板哈希，`build-manifest.json` 保留原构建记录。

备份数据目录后，将新版解压到独立目录，退出旧核心再启动新版。外部 `QQBOT_HOME` 可继续使用；数据放在包内时需自行迁移整个 `data`。Python 已内置；NapCat、Antigravity、Chrome/Edge/Chromium、ffmpeg 和 QEMU 按所用功能单独安装或配置。

该版本的浏览器验收使用本地测试网站及独立浏览器目录。真实平台登录、页面定位和在线模型效果，需要配置具体平台后验证。

## 直播与运营

新版控制台提供“直播与运营”：透明Live2D预览、OBS场景/浏览器来源、当前角色TTS流式口型、已有歌曲表演、弹幕批量事件、B站动态和视频投稿草稿。浏览器Live2D可以独立工作，VTube Studio为可选控制后端。官方Hiyori示例及运行库带有各自许可文件。

先打开本机预览并配置OBS，再绑定当前角色和直播间。Cookie由 `BILIBILI_*` 环境变量或 `data/broadcast/bilibili-credentials.json` 提供，默认关闭实际发布；真实账号开播和发布接口需之后联调。详细配置见 `_internal/BROADCAST_README.md`、`_internal/VTUBER_README.md`。本轮便携包只制作并验证本机功能，不会自动替换已运行版本。
