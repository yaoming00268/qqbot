# 插件与技能系统 (Skills & Plugin Architecture)

## 模块定位
为大语言模型（Gemini 3.8 等）提供**动作执行能力与外部工具调用接口**（Function Calling / Skill Tools）。彻底解决大模型“误以为自己无法在群内发图、发文件”或“仅能输出纯文本却无法调用物理接口”的问题。支持多模态媒体检索发送、文档导出、网络事实查询等扩展能力。

---

## 核心设计与执行流

```
群聊/私聊发言 ──> 大模型 (Gemini 3.8) ──[决策输出 JSON 包含 skill 字段]
                         │
                         ▼
                  SkillManager.parse_skill_invocation()
                         │
                         ▼
                  SkillManager.execute_skill(name, params, context)
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
  SendImageSkill   SendFileSkill   WebSearchSkill
  (Bing/Commons    (本地/缓存文档   (实时网络检索
   搜索与发送)      导出与直传)     与事实汇总)
        │                │                │
        └────────────────┼────────────────┘
                         ▼
                MediaSender / Adapter
                         │
                         ▼
              向 QQ 群 / 私聊 真实发送图片或文件
```

---

## 内置技能清单

| 技能名称 (`name`) | 显示名称 | 核心参数 | 作用说明 |
| :--- | :--- | :--- | :--- |
| `send_image` | 发送图片/立绘 | `query` (string, 必填), `count` (int, 选填), `orientation` (string, 选填) | 搜索并下载发送动漫立绘、插画、壁纸或表情包至群聊/私聊。严禁大模型推脱称无法发图。 |
| `send_video` | 发送视频 | `query` (string, 必填), `max_duration` (int, 选填) | 由反重力检索并下载视频后发送；只有用户明确要求时才设置时长。 |
| `send_file` | 发送文件/文档 | `file_name` (string, 必填), `content` (string, 选填), `file_path` (string, 选填) | 将聊天总结、表格、代码导出为文档（txt/md/csv）或直接发送已有文件。 |
| `web_search` | 网络搜索 | `query` (string, 必填), `max_results` (int, 选填) | 检索网络最新资讯、游戏攻略、角色立绘设定或事实。 |
| `run_task` | 后台任务 | `task` (string, 必填), `output_kind` (string, 选填) | 独立完成检索、分析或制作；纯文本答案也保存为 Markdown 文件交付。 |
| `group_slang` | 群聊话术表 | `query` (string, 必填), `operation` (`lookup` / `resolve` / `record`) | 查询固定路径 `data/slang_glossary.md` 的黑话、别名、标签与索引；空解释在后台核实并回写。 |

技能上下文使用客户端传入的 `ai_client` 和共享 `media_service`。`web_search` 的结果会实际发送到聊天中；媒体技能的 `success` 依据平台回执判断，不能把 `MediaService.handle()` 的“已处理”布尔值当作发送成功。

所有内容技能的文字结果以文件发送，仅保留简短完成台词。自然语言明确检索和资料制作请求直接进入工作队列，避免普通聊天规划阶段耗时。话术表的本地命中始终先注入两种聊天提示；仅已记录但缺少解释的词条会触发后台补全，不由普通聊天同步联网。技能维护流程见 [group-slang/SKILL.md](group-slang/SKILL.md)。

默认 `native` 媒体请求直接交给反重力完成整个任务：自主检索、浏览、脚本抓取、下载到 `cache_dir/<账号命名空间>/tasks/<任务目录>/`，查看并核对本地成果后返回交付清单。QQBot 直接登记和发送这些本地文件，不经过 `native_retrieve → MediaCrawler → download_url`，不套用公共下载器的网页数量、大小、像素、时长或下载总时限，不自动推导立绘/手机图片的方向。用户完整原文和明确要求传入任务。来源可由实际浏览器、网络或终端工具获得，不要求出现在初始搜索结果白名单。每张图片复用本轮真实查看记录及画面核对；缺少查看记录时再进行视觉核对。`delivery.json` 保存交付清单。公共搜索源仍使用独立下载器配置。

---

## 文件结构
```
skills/
├── __init__.py       # 统一导出所有 Skill 类与 default_skill_manager
├── base.py           # BaseSkill, SkillParameter, SkillContext, SkillResult 基类定义
├── manager.py        # SkillManager 注册、Prompt 格式化注入与异步执行器
├── media_skills.py   # 图片、立绘、壁纸、视频发送技能实现
├── file_skills.py    # 文件生成、文档导出与文件发送技能实现
├── web_skills.py     # 网络搜索与知识检索技能实现
└── README.md         # 模块架构说明、扩展技能指南与维护参考
```

---

## 如何编写并注册一个新技能 (5 分钟上手)

### 1. 继承 `BaseSkill` 并声明参数
创建 `skills/my_custom_skill.py`:
```python
from skills.base import BaseSkill, SkillParameter, SkillContext, SkillResult

class RollDiceSkill(BaseSkill):
    name = "roll_dice"
    display_name = "掷骰子"
    description = "随机掷出指定面数的骰子，用于群内抽奖或游戏判定。"
    parameters = [
        SkillParameter(name="sides", param_type="integer", description="骰子面数，默认6", required=False, default=6)
    ]

    async def execute(self, params: dict, context: SkillContext) -> SkillResult:
        import random
        sides = int(params.get("sides") or 6)
        val = random.randint(1, sides)
        return SkillResult(
            success=True,
            message=f"掷出了 {val} 点（1~{sides}）",
            action_type="text",
            data={"val": val}
        )
```

### 2. 注册到 `SkillManager`
在启动初始化或 `skills/__init__.py` 中：
```python
from skills.manager import default_skill_manager
from skills.my_custom_skill import RollDiceSkill

default_skill_manager.register(RollDiceSkill())
```
注册后，系统提示词将**自动生成**该技能说明给大模型，模型在决策输出中即可直接声明调用：
```json
{"should_reply": true, "action": "skill", "skill": "roll_dice", "params": {"sides": 20}, "reply": "看我的二十面骰！"}
```

---

## 维护与调试要点
1. **提示词防谎称规则**：
   在 `format_prompt_skills()` 中明确规定：严禁 AI 以“无法发图”或“不支持发送文件”拒绝。如果发现模型再次出现这种说辞，优先检查决策提示词中 `SkillManager.format_prompt_skills()` 是否注入成功。
2. **异步非阻塞**：
   所有 `execute` 方法必须为 `async def`，严禁在执行体中执行同步阻塞的网络请求或耗时 IO（请使用 `asyncio`、`aiohttp` 或 `asyncio.to_thread`）。
3. **安全边界**：
   文件发送类技能必须通过 `contained_path` 或安全文件名校验（`os.path.basename`），绝不向客户端发送系统根目录或受保护配置文件。
