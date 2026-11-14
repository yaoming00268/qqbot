# QQBot Studio UI 组件库 (ui/components)

本目录包含 QQBot Studio 现代化毛玻璃控制界面的各个独立功能组件。每个组件均为独立的 `QWidget` 派生类，具有高内聚、低耦合、职责单一的特点，极大降低维护复杂度与阅读负担。

## 组件清单

| 组件模块 | 主类名 | 职责与功能 |
| --- | --- | --- |
| `monitor_sidebar.py` | `GroupChatMonitorWidget`, `FrostedChatBrowser` | 实时群聊消息监控侧边栏、毛玻璃半透明气泡、右键设置/修改/删除主人与专属称谓菜单、文本字体缩放 |
| `persona_tab.py` | `PersonaTabWidget` | 人设预设工坊、人设列表、新建/编辑/保存/删除预设、系统提示词即时预览与试运行演练 |
| `config_tab.py` | `ConfigTabWidget` | 全局运行模式、QQ 官方凭证、OneBot 双小号管理、Gemini 参数、群聊行为与各群隔离表格 |
| `console_tab.py` | `ConsoleTabWidget` | 机器人子进程生命周期管理（启动/停止/重启）、实时日志与话术表编辑页、过滤高亮与重置群记忆 |
| `slang_editor.py` | `SlangGlossaryEditor` | 固定路径 Markdown 话术表的搜索、新增、修改、重命名和删除；即时生效、索引更新与并发编辑冲突提示 |
| `emotion_tab.py`, `emotion_charts.py` | `EmotionTabWidget`, `MetricChart`, `ConfusionChart` | LM Studio 模型读取与分析、逐句固定标签标注、后台本地分类器训练、损失/准确率曲线与混淆矩阵、训练历史回读与导出 |
| `media_tab.py` | `MediaTabWidget` | 媒体发送总控、图片/视频/文件容量上限配置、本地资料库、ffmpeg 诊断、任务进度与图片预览 |
| `companion_tab.py` | `CompanionTabWidget` | 表情包标签导入与冷却、软件/游戏操作上限（默认不限）及自主续用开关、只读资料和浏览器登录目录别名、游玩和学习记录 |

## 关键技术特性

1. **确定性右键选中 (`MessageBlockData`)**
   - 使用 Qt 原生 `QTextBlockUserData` 将消息元数据（`group_id`, `user_id`, `sender` 等）直接锚定在 `QTextBlock` 上。
   - 彻底杜绝了使用字符偏移或单纯 blockCount 导致的错位、越界或仅能选中最新消息的缺陷。

2. **现代化毛玻璃质感 (Frosted Acrylic)**
   - 采用深色极简 Slate 基底配合动态透明度（0.55~1.0），无廉价霓虹渐变。
   - 彻底移除 Emoji 依赖，全线使用矢量 `create_vector_icon`（SVG 几何算法纯代码绘制）。

3. **双向事件驱动**
   - 侧边栏修改主人身份后，自动触发 `master_updated_signal` 信号，实时同步到 `config_tab.py` 中的群聊配置表格并持久化至 `config.yaml`。
