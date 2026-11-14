# QQBot Studio（pywebview）

默认桌面界面使用 pywebview 6 和 Edge WebView2。本地 HTML、CSS、JavaScript 通过 Python bridge 访问现有配置、核心与数据，界面不加载 CDN。

## 启动

双击项目根目录 `run_gui.bat`。该启动器优先使用 `runtime/active-runtime.txt` 选定的冻结包。更新后退出旧控制台，再打开启动器。源码方式为 `python gui.py --home <数据目录>`，需要 `requirements.txt` 中的依赖。Windows 10/11 x64 需要 Microsoft Edge WebView2 Runtime；通常已随 Edge 安装。

界面不会在打开时自动启动 Bot。顶部可启动、请求停止、保存；运行日志可重启或显式强制结束。正常停止会等待核心保存记忆和任务退出。

## 页面

- 概览：真实运行状态、任务数量、近期消息与快捷入口。
- 对话监控：消息搜索、群号筛选、复制消息、设置主人与称谓。
- 角色预设：创建、编辑、切换与删除。重命名保留身份 ID，并同步群聊绑定。
- 记忆与通讯录：事件时间线、人物资料、自主成长、Markdown 话术表。结构化资料读取原数据库，话术表支持校验与版本冲突检测。
- 自主活动：参与、探索、阅读和应用配置；活动历史、观察记录及现有任务恢复。
- 资源与任务：后台任务、媒体能力、Gemini 浏览器连接、网站角色和表情素材。
- 情感工作室：读取 LM Studio 已有模型、BERT / LM Studio / 本地分类器分析、样本标注、训练与真实曲线。BERT 工作台沿用独立 `emotion_studio`。
- 设置：分区表单与完整配置；OneBot 每个账号单独保存连接地址及访问令牌。`Ctrl+S` 保存所有当前编辑。

## 外观

左下角“外观与个性化”可调整：

| 项目 | 范围 |
| --- | --- |
| 主题 | 雾白、墨色、跟随系统 |
| 强调色 | 钛蓝、松绿、雾紫、石板 |
| 玻璃浓度 | 35–100%，只改变界面底色 |
| 背景模糊 | 0–48px |
| 窗口不透明度 | 65–100%，作用于整个原生窗口 |
| 字号 | 12–17px，按比例缩放界面文字 |
| 开关 | Windows 原生 Acrylic、减少动画 |

偏好实时预览并自动保存在数据目录的 `data/studio/appearance.json`，与 `config.yaml` 分开。Windows 支持时使用 DWM Acrylic；不支持原生材质时使用 CSS 背景模糊。所有导航和按钮使用 SVG 图标，不使用装饰 emoji；聊天原文保留原始内容。

## 代码与验证

- `webview_app.py`：桌面窗口、生命周期与原生 smoke。
- `studio_api.py`：配置、记录、文件选择、任务回调和进程管理。
- `window_effects.py`：Acrylic、圆角和整窗不透明度。
- `web/`：前端页面、交互与主题。
- `main_window.py`、`theme.py`、`components/`：旧 Qt 组件兼容入口，默认启动不导入 Qt。

配置保存采用局部合并及文件版本校验。记忆、账号数据仍位于原数据目录，不随界面迁移。连接方式及大部分行为设置保存后需重启核心；外观立即生效，话术表沿用原有文件刷新机制。

测试：`python -m pytest -q test_webview_studio.py`；交互验收：`python maintenance/smoke_studio_ui.py --report <报告目录>`。测试使用临时数据目录，不连接真实 QQ 或模型账号。
