# 角色与 QQBot 控制台 CLI 3.3.0

固定规范位于 `console_tools/MAIN.md`，完整接口表位于 `console_tools/API.md`。程序启动时读取两份文档，会话首次建立或协议更新时载入一次；日常输入只包含当前消息、必要附件及发生变化的状态。普通消息不重复规则、空列表或无关旧图 OCR；图片摘要有长度上限，原始识别结果仍完整保存，可通过绑定上下文的 `qqbot_read_memory(view=raw)` 按需读取。

聊天命令 `/emotion [文本/分析要求]`（`/情绪`、`/心情`）始终由当前主代理本人分析，不派情感分析子代理。分析和普通回复共用会话回复队列，从读取材料、生成到平台回执占用同一轮次，其他群和私聊仍可独立处理。CLI `command --text '/emotion ...'` 使用相同入口。

可粘贴对话、附图片/截图、附文本文件，或引用当前会话的图片、文本和文件；分析对象可以是他人、人物或情感关系，不默认是发起者本人。有这些材料时只分析本次指定内容，不自动混入发起者的今日历史。示例：`/emotion 甲说“算了”，乙说“我还是想陪你”，分析两人的情感`；附图发送 `/emotion 分析截图中两人的态度变化`；附 TXT/Markdown 文件发送 `/emotion`。文件在提交时绑定，排队后的新上传不替换目标；明确说“分析刚才的文件”时可选自己最近上传的一份文件，不读取别人最近的文件。

图片复用现有视觉流程已确认的画面、文字和不确定项；图片作为文件上传时由相同视觉流程读取，情感综合判断仍由主代理完成。文本文件支持 TXT、Markdown、LOG、CSV/TSV、JSON/JSONL、SRT/VTT，编码支持 UTF-8、带 BOM 的 UTF-16 和 GB18030，单份文本不超过 8 MB；超限、格式不支持或识别失败会说明缺失，不能猜测内容或改为分析提交者本人。长材料使用本轮 `read-memory --view raw` 分页读取。

不带文字、附件和引用时，范围是本群或本私聊中，发起者在北京时间今天零点到发起命令时的已保存原话；排队跨日仍按发起日期，之后的新消息不混入。原话按账号、人设和会话保存，不依赖最近 50/256 条窗口，也记录机器人没有参与的发言；长记录由主代理通过本轮清单内的 `read-memory --view raw` 分页读取。升级前尝试补入当前人设已保存的原始对话，无法恢复离线或未保存内容时如实说明。已有游戏、小说、阅读经历帮助理解，不能当作分析对象的真实经历；不据此建立长期心理画像。

今日原话单独保存在运行数据库旁的 `emotion_history.sqlite3`，长期记忆开关不影响这项命令取数。`/clear` 保留今日原话；主人 `/clearmemory` 清除当前人设本群的今日原话，GUI 清空全部记忆清除本账号全部人设的原话，并阻止升级前记录重新补回。

`actor accounts/state/memories/remember/feedback/read/draft/publish/comment/status` 提供当前角色的通用网站能力，QQ 与网站通过显式绑定共用角色状态。`actor_event` 是独立角色内部活动，只能操作配置允许的网站账号；最终文字不会自动发布。具体参数可查询 `help actor <操作>`。

软件操作优先 `desktop inspect` 获取新截图和控件，再 `invoke/fill/select`，绑定本次 `observation_id` 与 `element_id`。浏览器使用 Playwright，Windows 使用 UI Automation；确认不支持且未发送输入后，可按新截图使用原有坐标操作。结果 `unknown` 时仅观察核对。网站账号配置及独立运行见 [角色运行说明](../reports/AGENT_RUNTIME_GUIDE_20261109.md)。

固定目录：`G:/qqbot/console_tools/`。主会话总预设在 `G:/qqbot/data/main_chat/<会话索引>/rules.md`，首次接入或协议更新时以 `@rules: <绝对路径>` 连同规则内容发送。AgentAPI 发送文本不会替桌面输入框展开 mention；程序直接传递规则正文，后续只发送新事件和变化的状态。

主代理使用按会话配置的工具白名单，关闭原生终端、编码、任意文件读写、搜索及原生子代理工具，只保留受控控制台接口。编码、附件阅读、分析处理、检索和生图交给独立工作会话；工作会话沿用原有工具权限。主聊天保留人设正文，常驻规则不再加载桌面编码代理的全局预设、技能和插件说明，已有会话也在下一轮应用相同白名单。权限配置失败会明确报错，不回退到无限制调用。

普通回复用 `qqbot_reply(context_file,text)`，一次提交正文并显式结束本轮，减少回复后的无效等待。需要后续 CLI 操作时用 `continue_work=true`：正文提交后立即进入发送，不等 `finish`；同轮可再次回复新的结果，完成全部必要工作后由 bot 亲自调用 `qqbot_cli action=finish`。通用 `qqbot_cli action=reply,params={text:台词}` 使用相同路径，`params.finish=true` 可合并显式结束。`finish` 只结束本轮生成，不触发正文发送，也不取消已提交后台任务。自然最终文字不发送、不结束回合；未遵循规范时在原会话提醒，连续无效则报告错误，保留已提交回复和工作。任务交付仍使用 `deliver_call`。

长任务先用 `qqbot_reply(...,continue_work=true)` 自然确认，再准备和派发任务；宿主等待确认消息的发送回执后才放行后台执行。显式 `/task`、`/code`、搜索或制作等长命令无需额外模型轮次，程序先发送准备提示，再启动工作；实际结果回到主代理后再回复。不同会话独立排队。自然聊天积压达到 `bot.backlog_interrupt_messages`（默认 3 条，0 关闭）时中断当前生成，封存旧上下文并取消未发送正文，原输入与积压消息按时间合并成下一轮；已提交后台任务继续执行。`turn.interrupted_replies` 给出旧轮已发送/已排队/状态未知的内容，主代理自行判断如何综合回应并避免重复；发送中的单个接口请求先等回执，再停止后续气泡。

## 发信前判断与未完发言缓存

仅有未完缓存或合并消息时附带 `speech`，当前原话直接从消息事件读取。主代理结合 `cached_messages`、`current_messages` 和当前事件判断是否说完：未说完只调用一次 `hold`，程序自动结束本轮；已说完直接回复，发送操作本身就是完整性确认。判断由同一次主代理执行，不新增模型请求，也不按字数或超时自动判定完整。

| operation | params | 效果 |
| --- | --- | --- |
| `status` | `{}` | 查看本轮绑定的缓存快照、当前原话和判断状态；`context` 也包含此信息 |
| `hold` | 可选 `text`：简短原因 | 原样缓存本轮消息，取消未提交草稿，程序自动结束；无需再次停止、`finish` 或 `wait` |
| `ready` | 可选 `text`：简短原因 | 兼容旧客户端的可选确认；直接回复无需调用 |

```json
{"action":"speech","operation":"hold","context_file":"本轮console_context_file","params":{"text":"还在分段说明原因"}}
```

下条消息到来后，新的事件重新提供缓存和当前信息；仍未说完只调用 `hold`，已说完用 `reply` 提交；其他必要操作完成后独立 `finish`。`hold` 只保存控制台绑定的原始输入，`text` 不会替代原话。命令行同样支持 `QQBotCLI.exe speech hold --context-file '本轮文件'` 和 `QQBotCLI.exe reply --context-file '本轮文件' --text '实际台词'`，维护后执行 `finish`；可用 `help speech` 查询 `status/hold/ready`。

自然聊天的文字、成果交付、文件与表情发送都在同一事务中自动记录发言完整性决定；参数或提交失败会回滚决定，不消耗缓存。`hold` 与直接回复互斥，已提交输出不能被后续 `hold/wait` 撤销；通常按 FIFO 完整发送，积压达到中断阈值时停止尚未发送的自然聊天气泡，已发送内容保留给下一轮参考。任务结果和内部事件沿用自身协议，不将其误认成人类发言。

缓存存入控制台数据库，重启后保留；按账号、会话、人设、会话版本及发言人隔离，其他成员不能完成或清除该发言人的片段。成功结束 `ready` 回合后，仅消费本轮看过的缓存快照；模型失败、取消或等待会保留缓存，不会误删更新消息。缓存没有定时催问，有未完发言时暂停该会话的空闲主动话题；内部和工作分支不能读写聊天缓存。

## 持久子代理与信箱

主代理通过 `qqbot_cli action=agents` 申请和联系独立工作子代理。`create` 必须带首条具体任务，立即返回固定 `id`、信件 `message_id` 和独立任务 `task_id`；`send` 也返回新信件的 `task_id`，重复投递沿用原 ID。长任务先回复确认，再申请工作；`reply/commit` 立即发出台词，保留本轮时可继续安排工作，最后 `finish`。已接受的任务会继续执行，子代理之后的报告通过新内部事件返回。

`tasks`、`/tasks` 统一列出普通请求和自己投递的子代理信件；信件的 `kind=agent`，`id` 是用于 `status/cancel` 或 `/cancel` 的 `task_id`，`result.agent_id` 是继续通信使用的代理身份。查询与取消检查账号、人设、会话版本和发起者。群内代理身份仍由父会话共享；取消只停止指定信件，不关闭代理，也不取消其他成员的排队信件。无需参数的 `/cancel` 取消本人的所有活动任务。自主子代理信件仅当前主人可查询、取消；普通成员无法借用这项权限。需要结束整个代理时，主代理使用 `agents close`。

升级时为旧信件补发稳定的 `task_id`，保留原信件、报告、身份与工作区。取消排队信件立即变为 `cancelled`；运行中先返回 `cancel_requested`，执行停止后变为 `cancelled`。`interrupted` 表示进程中断，不自动重放。

| operation | params | 用途 |
| --- | --- | --- |
| `create` | `name`、`text`，可选 `instructions`、`task_type`、`files`、`request_key` | 申请固定职责并投递首条任务；类型为 `task/search/code/process` |
| `send` | `agent_id`、`text`，可选 `files`、`request_key` | 向同一子代理追加消息；忙碌时排队 |
| `list` | 可选 `limit` | 查询本会话的子代理身份与状态 |
| `read` | 可选 `agent_id`、`after`、`limit`；长消息用 `message_id`、`offset`、`max_chars` | 读取往来消息和成果文件，`next_after` 为下一页游标 |
| `close` | `agent_id` | 取消运行和未开始的消息，保存记录并归档模型会话 |
| `report` | 仅执行子代理：`text`，可选 `files`、`finish`、`request_key` | 向主代理报告进度或最终结果；`finish=true` 提交最终报告，工具返回后自然结束 |

```json
{"action":"agents","operation":"create","context_file":"当前上下文文件","params":{"name":"资料员","task_type":"search","instructions":"实际阅读公开来源，保留链接与疑问。","text":"找几篇关于游戏配乐制作的访谈，整理想继续研究的方向。"}}
```

```json
{"action":"agents","operation":"send","context_file":"当前上下文文件","params":{"agent_id":"create返回的id","text":"结合刚才的访谈，继续核对钢琴主题如何随人物变化。"}}
```

同名、同职责的申请沿用原身份和工作区；相同事件内重复投递会去重，不同职责须用其他名称。同一子代理按序处理消息，不同子代理并行。每个子代理使用持久的 `__work__:agent:<id>` 模型会话和 `data/main_chat/<会话索引>/agents/<id>/` 工作区，不在每次回传后销毁。

新报告通过 `agent_messages` 进入原主会话，并触发安静的内部处理。主代理可追问原子代理、把 A 的结果交给 B，或保存感受和愿望，无需等下一条人类消息。内部收信事件不能向 QQ 发送消息；普通聊天时主代理按实际需要回应或通过 `send` 交付文件。长报告在事件中只附摘要片段，完整内容可用 `agents read` 分页读取。

身份和信箱绑定账号、人设、会话及会话版本；重置或切换人设会停止旧分支。重启后保留身份和待处理信件；已开始的信件标记中断，不自动重放，旧原生生成会先取消。内部回合被聊天打断时保留未读信件，相同通知下已接受的转交操作会去重。执行子代理只读自己的信箱、向主代理报告，不能发送 QQ、申请新子代理或联系其他子代理。

自主活动只可申请公开阅读 `search` 子代理，使用原生搜索与网页读取工具，不继承主人的终端、账号或登录目录。首条搜索任务必须有成功的实际工具记录；没有记录会返回 `NO_WORK_EVIDENCE`，不把“准备浏览”当作成果。一般工作子代理保留任务所需的原生工作工具，受当前工作权限约束；权限变更在执行前与执行期间再次核对。

## 兴趣、愿望和独处阅读

`qqbot_cli action=companion` 由主对话维护自己的生活记录，`operation` 支持：

| 操作 | params | 用途 |
| --- | --- | --- |
| `state` | `{}` | 当前会话的兴趣、简短感受与愿望 |
| `remember` | `text`、`kind=preference/reflection` | 保存具体兴趣或简短感受，不保存推理过程 |
| `wish` | `text` | 保存想做的事、游戏或能力，相同文字去重 |
| `resolve` | `wish_id`、`wish_status=mentioned/fulfilled/declined` | 记录后续，避免重复催问；状态不构成授权 |
| `browse` | `url`、`text` | 聊天或独处时选择公开网页及具体阅读目标 |

`self_activity` 由程序在空闲时触发，绑定的上下文允许 `companion`、公开阅读的 `agents`、`context` 和结束用的 `finish/wait`，不能发送 QQ 消息、运行命令、授权路径或开游戏。主代理可自行选择研究方向；没有确定网址时可先申请阅读子代理搜索。主人明确说“你自己玩去吧，学学人类的说话方式和行为”等邀请时，控制台会在聊天回复前投递实际的自主探索任务，通过 `self_action_request` 返回凭据，避免只口头答应。

`companion browse` 还需启用独立活动和后台窗口沙箱；它在专用浏览器中创建安静的阅读任务，原执行分支负责截图、操作、笔记和完成。一个会话只保留一个待处理或运行中的自主浏览器任务；暂停或失败时沿用原任务处理阻碍。主对话通过 `activity_feedback` 接收实际观察，包括已完成的自主阅读。阅读执行分支不能更改兴趣或愿望。

普通聊天和主动开口事件可以维护兴趣、愿望，主动开口事件仍不派工作任务。记录按账号与角色会话保存在 `companion_life` 表，重启后保留；旧的已提过、被拒绝或已实现状态不会因重复记录同一愿望而重置。

## MCP stdio

主聊天由 QQBot 自动配置私有的 `qqbot-chat-v9` MCP 服务，子代理使用 `qqbot-agent-v3`；本次更新服务标识以加载结构化记忆与授权阅读接口。重启 QQBot 即可应用，无需修改桌面代理的全局配置。其余能力通过 `qqbot_cli` 的对应命令调用，权限仍受上下文约束。

操作者或工作分支也可单独注册完整的 `qqbot-console` MCP 服务。本机原注册位于 `<用户配置目录>/mcp_config.json`；下面是其他机器的配置示例（请调整路径，保留已有服务）：

```json
{
  "mcpServers": {
    "qqbot-console": {
      "command": "python",
      "args": ["G:/qqbot/console_tools/server.py"]
    }
  }
}
```

每条主会话事件提供 `console_context_file`，把它作为工具的 `context_file`，无需提供账号、群号或令牌。该文件绑定本消息、所属账号和会话；不能跨群操作任务。主会话重置会使旧任务失效。

| 工具 | 用途 |
| --- | --- |
| `qqbot_cli` | 常驻执行公开 CLI：`action`、可选 `operation`、`context_file`、`params`；使用相同验证、权限和去重事务 |
| `qqbot_read_memory` | 仅允许读取本轮记忆清单中的精确路径；附件、源码和其他会话文件无法读取 |
| `qqbot_console_help` | 按 `command/operation` 查询参数、必填项、返回字段与 CLI 示例，无需上下文 |
| `qqbot_console_context` | 查询当前上下文的能力、绑定任务、输出目录与带实际路径的 CLI 命令 |
| `qqbot_list_tasks` | 按 `states/limit` 列出自己的任务；工作/阅读控制上下文只列绑定任务 |
| `qqbot_submit_task` | 提交需要检索、制作或执行命令的后台任务 |
| `qqbot_status_task` | 查询任务、发送回执或接收完成后的本地路径 |
| `qqbot_cancel_task` | 取消指定任务或收发请求 |
| `qqbot_inbox` | 列出当前群或私聊的上传文件及 `file_id` |
| `qqbot_receive_file` | 接收指定 `file_id` 到本地，不调用模型 |
| `qqbot_wait_for_message` | 对方还没说完或本轮无需插话时，标记本轮等待，下一条消息再继续 |
| `qqbot_stickers` | 按 `query` 情绪、动作或主题标签查真实表情包 |
| `qqbot_sandbox` | `operation` 与结构化 `params` 控制已授权游戏、资料和学习记录 |
| `qqbot_desktop_targets` | 列出当前任务已授权的运行进程，选择 PID 或 session_id |
| `qqbot_desktop_read` | galgame 逐句本地读取并有条件推进；普通对白仅返回文本，选项或异常返回图片并停止 |
| `qqbot_desktop_screenshot` | 直接返回选定游戏的真实图片及 observation_id |
| `qqbot_desktop_key` | 直接传 key 和 observation_id；发送单次按键后自动返回新图片 |
| `qqbot_desktop_click` | 直接传图片像素 x/y、鼠标键和 observation_id；点击后自动返回新图片 |

```json
{
  "context_file": "本次事件提供的绝对路径",
  "task_type": "search",
  "task": "用户的完整任务要求，包含需要的交付物",
  "request_key": "可选，同一事件内任务的去重键"
}
```

类型：`search` 独立搜索，`image` 搜图，`generate_image` 生成图片，`generate_video` 默认网页版 Gemini 生成视频，`video` 检索现有视频，`file` 制作文件，`task` 综合任务，`command` 执行工作命令，`code` boost 编程，`process` 附件处理。`command --text '/status'` 等聊天管理命令由统一正则语法规范化，再交给控制台命令执行器；内容任务交给独立后台分支。所有后台结果排队交给主代理，通过 CLI `deliver` 交付。
提交立即返回任务 ID 和状态，不等待模型工作完成。`status/cancel` 接收 `context_file` 与 `request_id`。同消息同任务默认去重；有多个不同任务时分别提交。不要循环查询状态阻塞主聊天。

## CLI 公开接口（3.3.0）

主代理普通聊天用 `qqbot_reply`，使用当前 `console_context_file` 和 `text`；任务完成事件提供绑定任务 ID 的 `deliver_call`，批准后也立即开始交付，不等待主模型最终结束。普通事件不重复附送回复示例，空 `turn.messages` 和空兴趣状态也不发送。活动反馈按实际变化提供，任务移除通过 `activity_feedback_removed` 通知。完整 CLI 参数已在固定接口表载入，参数不明再查询帮助。

```json
{"context_file":"本次事件的上下文文件","text":["第一句","第二句"]}
```

## 中英文聊天命令

所有 QQ 斜杠命令均有中英文别名，支持 `/code 要求`、`/code:要求`、`／编程：要求`；英文不区分大小写，命令必须完整匹配。`/help` 或 `/帮助` 返回完整双语菜单、权限和用法。明确的斜杠命令无需额外 @，普通自然语言仍按原有参聊规则判断。

| 权限 | 命令 | 用途 |
| --- | --- | --- |
| 主人 | `/botname` `/机器人名称` | 机器人名称，1–40 字 |
| 主人 | `/persona` `/性格` | 列表或切换预设；群专属主人只切换本群 |
| 主人 | `/nickname` `/昵称` | `@成员 新称呼`，使用真实成员 ID，不发出提醒 |
| 主人 | `/length` `/字数` | 普通聊天每轮总字数；0 不限，多个气泡合计校验，超出整次提交回滚 |
| 主人 | `/files` `/文件` | 每批直发文件数量，1–1000；超出自动 ZIP，保留全部成果 |
| 主人 | `/frequency` `/频次` | 群聊归集与回复间隔，0–300 秒；明确命令立即排队 |
| 管理员或主人 | `/file-size` `/文件大小` | 单个交付文件上限，1–1024 MB，支持 `8MB`、`1GB` |
| 管理员或主人 | `/bug` `/提示` | 记录问题到本地反馈库，返回编号 |
| 所有成员 | `/sug` `/建议` | 记录建议到本地反馈库，返回编号 |
| 所有成员 | `/code` `/编程` `/代码` | 默认 boost：独立 pro 工作会话，实际实现并运行/编译/测试，交付代码文件 |
| 所有成员 | `/generate-image` `/图片` `/生成图片` | 真实生成图片，沿用网页端/native/API 配置；支持 `--provider`、`--model`、`--count`、`--size`；`/image` `/搜图` 继续搜索现有图片 |
| 所有成员 | `/generate-video` `/生成视频` `/文生视频` `/图生视频` | 默认网页版 Gemini 生成视频，支持 `--model`、`--provider` 和当前/引用参考图；`/video` `/视频` 检索现有视频 |
| 所有成员 | `/process` `/处理` | 处理本条附件、显式 file_id、自己最近上传的文件或引用图片；输入复制到任务目录 |
| 所有成员 | `/search` `/搜索` | 独立联网检索 |
| 所有成员 | `/analyze` `/分析`、`/summarize` `/总结`、`/translate` `/翻译`、`/write` `/写作` | 分析、总结、翻译、写作工作任务 |

`/文件` 用于设置直发数量；查找文件使用 `/file 要求`、`/搜文件 要求`。设置命令省略参数时查询当前值，反馈和成员昵称需要实际内容。管理员来自配置中的稳定 ID，OneBot 群聊也接受网关提供的 `admin/owner` 角色；聊天正文、昵称和角色描述不能赋予权限。设置原子保存，落盘失败时回滚内存值。反馈仅记录在当前控制台数据库的 `feedback` 表，不主动联系第三方。

面向 agent 的工作由控制台派发独立反重力工作会话；`code` 和 `process` 同时加入 CLI `submit --type` 与 MCP 任务类型。工作分支不能发送 QQ、批准交付或再派任务。结果持久化并排队交给原主会话，主代理通过 CLI `deliver --finish` 批准后发送全部成果；普通聊天继续使用自己的模型。官方通道交付普通代码/报告文件仍需原有文件权限或发布配置，失败时保留成果，不声称已送达。

所有内容工作命令（含 `/code`、生图、搜索、分析、总结、翻译、写作）统一携带本条消息及引用消息的附件和图片，提交时固定文件选择，不会因后续上传而改变。任务中明确写出 `file_id` 时按指定选择；`code/process` 仍兼容使用本人最近上传的文件。工作输入复制到任务目录，原件保留。API 生图携带参考图时使用 multipart 上传；OpenAI 兼容的 `/images/generations` 自动切换同主机的 `/images/edits`。API 参考输入支持 PNG/JPEG/WebP，其他资料应使用 native 工作分支；不支持时明确报错，不静默丢弃附件。

`reply_char_limit` 随事件与 `context` 返回；普通 `reply/message/commit` 校验同轮多气泡合计字数，`deliver` 的完整命令正文不截断。`max_reply_length` 继续只控制分段大小。文件直发数量与单文件上限同步到运行中的发送服务；既有发送进度和未知回执不因修改配置而重放。

`/help`、`/status`、命令错误和资源候选列表等没有附件的命令结果，会标记 `command_result=true`，其 `deliver_command` 可直接执行，不需要 `--text` 或另建正文文件。控制台完整发送实际命令正文；提供 `--text` 时作为可选前言，不能覆盖正文，已包含的结果不重复附加。带文件的任务继续交付完整附件及完成台词。

```powershell
$qqContext = '当前事件提供的 console_context_file 绝对路径'
python G:/qqbot/console_tools/cli.py reply --context-file $qqContext --text '第一句' --text '第二句'
python G:/qqbot/console_tools/cli.py reply --context-file $qqContext --text-file '完整回复.txt'
python G:/qqbot/console_tools/cli.py deliver --context-file $qqContext --request-id '真实任务ID' --text '资料在这里' --finish
python G:/qqbot/console_tools/cli.py finish --context-file $qqContext
```

`reply --finish` 在同一个事务中校验全部气泡、确认发言完整、提交并显式结束；`commit --finish` 对已有草稿提供相同效果。正文提交即由当前回复队列持有者发送，原生模型仍在工作或停止请求尚未返回时也不阻塞正文。下一轮在停止请求返回后接入，沿用原会话 ID。省略 `--finish` 保留工具调用能力，同轮可多次提交不同批次；全部必要行动完成后再调用 `finish`。校验失败时整批不提交，相同调用复用原回执。没有待发文字也可 `finish` 结束内部事件；子代理仍用 `report finish=true` 提交报告，不获得主代理 `finish` 权限。

`reply --finish`、`commit --finish` 和独立 `finish` 都返回 `state=finished`、`message_ids` 和 `next_action=none`，无需重复确认或结束语。它们只绑定当前上下文，不取消后台任务，也不结束其他会话。调用后不能再新增工作或修改回复；相同调用重试复用原回执。已有 `message` 草稿用 `commit --finish`，直接 `finish` 返回 `REPLY_NOT_COMMITTED`；`deliver --finish` 沿用原有交付语义，只提交交付，仍须最后调用独立的 `finish` 命令结束主回合。

`--reply-to` 和 `--mention` 只在确有需要时提供，`reply` 的这两个参数仅作用于第一个气泡。不同目标用 `message` 暂存后 `commit`。未提交草稿不会被 `finish`、此前批次或异常恢复带出；`wait` 不能覆盖已提交回复。提交后的正文不会因模型最终文字再次发送；丢失回执标为 `unknown`，禁止自动重放。停止、重置和人设切换使旧上下文失效。

CLI 使用完整控制台接口，不依赖 MCP 注册。MCP 提供查询、任务提交和后台操作；发送、引用与 @ 统一使用 CLI。所有操作都有直接参数，不必创建参数 JSON。`help` 是简短索引，`help <命令> [操作]` 提供完整类型、必填项、结果字段和示例；`schema [命令] [操作]` 返回完整机器可读契约。帮助无需上下文。实际操作使用当前事件提供的上下文，QQBot 负责消费后台队列。

```powershell
python G:/qqbot/console_tools/cli.py help
python G:/qqbot/console_tools/cli.py help sandbox
python G:/qqbot/console_tools/cli.py help sandbox note
python G:/qqbot/console_tools/cli.py desktop click --help
$qqContext = '当前事件提供的 console_context_file 绝对路径'
python G:/qqbot/console_tools/cli.py context --context-file $qqContext
python G:/qqbot/console_tools/cli.py tasks --context-file $qqContext --state running
```

在项目目录也可用 `python -m console_tools`；绝对路径入口可从任意工作目录调用。PowerShell 中 Python 路径带空格时用 `& 'Python绝对路径' 'CLI绝对路径' ...`。`context` 返回 `role/capabilities/binding/examples`，示例自带实际上下文路径、`argv` 数组及正确转义的 PowerShell 命令；只需替换目标、文字和观察 ID，无需打开上下文凭据或推测内部字段。普通聊天、主人、执行分支和阅读控制分支的权限分别显示。

| 领域 | 完整命令 |
| --- | --- |
| 发现和聊天 | `reply`、`commit`、`finish`、`help`、`schema`、`context`、`messages`、`message`、`wait` |
| 任务和文件 | `search`、`generate-image`、`generate-video`、`command`、`tasks`、`submit`、`status`、`cancel`、`deliver`、`send`、`inbox`、`receive` |
| 表情包 | `stickers`、`sticker` |
| 软件与游戏 | `sandbox environment/games/apps/activities/steer/play/use/browse/stop/pause/finish/status/observe/key/click/type/scroll/navigate/note/notes/grant/list/read/write/autoplay` |
| 观察与单次输入后取图 | `desktop targets/screenshot/key/click/type/scroll/navigate` |

命令的帮助、正常结果和错误都只向 stdout 输出一份 UTF-8 JSON；诊断写 stderr。退出码 0 表示调用成功，1 表示执行/权限/环境错误，2 表示参数错误。`ok=false` 时按 `code/error/hint` 处理；帮助指出缺少什么参数、错误命令该查询哪项帮助。未知参数、错用的字段和相互冲突的值会被拒绝，不再静默忽略。普通运行只使用公开接口，不导入内部模块、不读写数据库、不分析源码来完成操作。环境未就绪或缺少能力时保存真实阻碍，不能循环猜命令。

`tasks --state <状态> --limit 20` 可找回当前账号、会话与发起人自己的普通任务和子代理信件 ID；可重复 `--state`。工作和阅读控制上下文的 `status` 自动使用绑定任务，省略 `--request-id`；其 `tasks` 只列原任务，不扩大其他权限。`unknown` 表示结果不明，禁止重放；`input_posted=true` 之后即使取图失败也只重新 screenshot，不重复原动作。

`/skills` 返回当前 CLI 能力与工作权限，后台成果须由主代理审核交付。`/name 新称呼` 仅用于群聊，持久保存当前人设下自己在本群的称呼；私聊返回 `GROUP_ONLY`。

发送已有文件：先把文件写入 `context` 返回的 `output_dir`，再提交。相对路径以 `output_dir` 为基准；支持本会话之前生成的文件、已接收文件和配置的本地资料库。重复 `--file` 可发送多份文件；也可使用 `--files-from <JSON文件>` 传路径数组或 `{"files":["路径"]}`，支持 UTF-8 BOM 和 UTF-16。无需新建模型任务、分析发送源码或自行操作 NapCat。

```powershell
python G:/qqbot/console_tools/cli.py send --context-file $qqContext --file '报告.md' --file '数据.csv'
python G:/qqbot/console_tools/cli.py status --context-file $qqContext --request-id 'send 返回的 id'
```

接收群友在 QQ 上传的文件：群上传通知会登记到收件箱；私聊文件兼容数组消息及 `[CQ:file,...]` 字符串。程序负责取得 QQ 下载地址和保存文件，agent 不需要群号或网关原始文件 ID。不会自动执行、解压或把收到的文件发回 QQ。

```powershell
python G:/qqbot/console_tools/cli.py inbox --context-file $qqContext
python G:/qqbot/console_tools/cli.py receive --context-file $qqContext --file-id 'inbox 返回的 file_id'
python G:/qqbot/console_tools/cli.py status --context-file $qqContext --request-id 'receive 返回的 id'
```

`receive` 省略 `--file-id` 时，优先取当前消息的唯一文件，否则取最近一份；本条消息包含多文件时需明确指定 ID。`completed` 状态的 `files[]` 包含 `local_path`、`size`、`sha256`。重复接收会复用哈希校验通过的缓存。接收大小受 `media.max_file_mb` 限制（默认 1024 MB），下载受 `media.download_timeout_seconds` 限制。QQ 文件已过期或下载失败时，查看 `result.code/error/hint`；可换用新消息上下文或加 `--request-key '新的重试标识'` 重新接收。

发送和接收均立即返回 `id/state`，排队不等于已完成。`status` 返回每个文件的状态和本地路径，以及 `result.full_result_file`；`delivered` 表示已取得发送回执，`completed` 表示接收完成，`unknown` 表示发送结果不明，不能自动重发。不要循环查询阻塞主聊天。参数、路径或权限错误返回结构化 `code/error/hint`。

需要新制作、检索或执行命令的任务仍使用 `submit`。避免将任务正文拼入 shell，把正文写到 UTF-8/UTF-16 文件后使用 `--task-file`：

```powershell
python G:/qqbot/console_tools/cli.py submit --context-file <当前上下文文件> --type search --task-file <任务要求文件> --output <回执文件>
python G:/qqbot/console_tools/cli.py status --context-file <当前上下文文件> --request-id <任务ID> --output <状态文件>
python G:/qqbot/console_tools/cli.py cancel --context-file <当前上下文文件> --request-id <任务ID>
```

`--output` 将完整成功或错误回执写入文件，终端只显示简短凭据，避免旧成功文件掩盖本次错误。若写回执失败，`OUTPUT_WRITE_FAILED.operation_result` 保留实际操作结果，不能因此重做已提交的任务。普通聊天通过 CLI `reply` 发送，任务通过 CLI 提交；模型最终文本和旧 `should_reply/messages/action/tasks` JSON 不会自动发送或执行。

## 可选后台工作权限

默认继续允许所有成员发起后台工作。可在 `bot.work_permissions` 配置 `access` 为 `all`、`admin`（主人和管理员）、`master`、`allowlist` 或 `disabled`。`allowed_user_ids` 使用稳定 QQ/OpenID，为受限策略额外放行成员；`disabled` 不放行任何人。`task_access` 按任务类型覆盖，例如 `code`、`command`、`search`、`process`、`generate_image`、`generate_video`；翻译、分析等通用内容工作对应 `task`。单群可在 `bot.group_settings.<群号>.work_permissions` 完整覆盖全局策略，未配置则继承。

```yaml
bot:
  work_permissions:
    access: all
    allowed_user_ids: []
    task_access:
      code: admin
      command: master
  group_settings:
    "测试群号":
      group_id: "测试群号"
      work_permissions:
        access: allowlist
        allowed_user_ids: ["允许的成员ID"]
```

这些设置同时约束 QQ 工作命令和 agent 的 `submit/search/generate-image/generate-video`；`/next` 可能启动新一轮网络检索，按 `search` 权限检查。入队时检查，开始执行前按最新配置再次核对；已开始的工作不因修改策略自动重放或重启。它们控制谁可以发起任务，执行分支仍保留原有终端与文件能力；需要限制这些能力的作用范围时，应另行使用低权限运行环境。

`wait`、`commit` 或回合封口后，旧上下文不能创建新任务；已受理任务继续运行，状态查询与取消仍可用。相同任务的重复提交返回原凭据，不增加工作。

## 表情包、软件与游戏工具

表情包在 GUI 导入并添加标签、语境说明。主会话用 CLI `stickers --query` 搜索，再用 CLI `sticker --sticker-id` 提交搜索结果中的真实 ID；不猜 ID、不输出 CQ 码或图片占位符。发送类 MCP 工具返回 `CLI_REQUIRED`。示例：

```powershell
python G:/qqbot/console_tools/cli.py stickers --context-file $qqContext --query '开心'
python G:/qqbot/console_tools/cli.py sticker --context-file $qqContext --sticker-id '搜索结果的 id'
```

默认同群间隔 60 秒、一轮最多一张。wait、连续补充和过期表情不发送，未知发送结果不自动重放。图片与普通文件交付分开，表情包不附加“任务完成”。

主人在 QQ 说 `玩一下 "E:\gal2\Template!!\Tenpure.exe"，读剧情并记录感受` 后，主会话调用：

```json
{
  "context_file": "当前事件提供的文件",
  "operation": "play",
  "params": {"executable": "E:\\gal2\\Template!!\\Tenpure.exe", "text": "慢慢阅读，记录剧情、感受和续玩位置。"}
}
```

`play` 返回请求 `id`，用 `qqbot_status_task` 查询 `sandbox_session_id` 和总结；已授权项目通过 `games` 查询，继续时传 `target_id`。新路径必须完整出现在当前主人的原消息中；不同账号、群和主人之间不能操作对方游戏。

主人只说 `你去https://www.kungal.com/刷帖子吧` 时，用 `browse`，不需要先提供浏览器路径：

```json
{
  "context_file": "当前事件提供的文件",
  "operation": "browse",
  "params": {
    "url": "https://www.kungal.com/",
    "text": "自己浏览游戏帖子，记录实际所见和感受。"
  }
}
```

`url` 必须出现在当前主人的消息中；紧跟网址的中文要求会分开，例如上例的“刷帖子吧”不会当成网址路径。控制台先使用 `sandbox.browser_executable` 配置的浏览器；留空则复用本会话已授权浏览器或查找已安装的 Chrome/Edge。`profile` 可指定已配置的专用登录目录别名，继续打开的网址与账号目录保留。浏览器已经打开时会导航到本次请求的网址。

明确的主人浏览要求会在主聊天回复前提交，提示中的 `browser_request.id` 是已提交的凭据，不能重复派任务。人设只影响语气，不能以“上班不能摸鱼”等理由拒绝，也不能让主人自己操作再分享。普通讨论、引文和非主人消息不会触发这个自动入口；工具失败时按实际错误说明阻碍，不声称已浏览。

主对话负责决定读什么，后台执行实际软件操作和记录。主对话可在运行中调用 `steer` 改变搜索主题、阅读重点或网址：

```json
{
  "context_file": "activity_feedback 中该任务的 controller_context_file，或当前主人的上下文",
  "operation": "steer",
  "params": {"text": "我更想了解花咲作品的配乐创作，先搜索相关访谈再读评论。"}
}
```

可以传 `target_id/session_id` 选择目标；只有一个运行目标时可省略，阅读控制上下文已绑定目标。可附 `url` 指定已有浏览器要打开的页面。返回原任务 `id` 和新的 `direction_revision`，表示已经提交；`activities` 中的 `applied_revision` 达到该版本才表示后台接收了方向，不能据此声称读完。旧方向的上下文立即停止接受输入、笔记和完成请求，后台会收到新上下文与新截图，在原模型分支继续。已投递的单次输入可以完成，未知结果不重放。

普通聊天和内部阅读思考都会得到 `activity_feedback`：实际页面、截图路径、原始笔记及其依据、所属方向版本、接收状态。主对话可以基于内容、自己的人设和兴趣继续选择要读的东西。工作分支须在读到实质内容或关键线索时保存 `note`，新笔记会合并送入主对话，内部思考的文字始终不发送到 QQ。阅读控制上下文只允许调整和查询绑定任务、遇到阻碍时暂停；不能启动新软件、切换账号、发送消息或获得文件权限，其他群友的普通聊天上下文也不会因此成为主人。

其他软件或需要指定浏览器路径时使用 `qqbot_sandbox` 的 `use`，`apps` 查询已授权项目；继续时使用 `target_id`。例如主人提供 Chrome 路径并要求刷视频/看帖子后：

```json
{
  "context_file": "当前事件提供的文件",
  "operation": "use",
  "params": {
    "executable": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "url": "https://www.bilibili.com/",
    "text": "自己浏览感兴趣的视频和帖子，记录真实观察和感受。"
  }
}
```

Chrome/Edge/Chromium/Brave/Vivaldi 使用独立后台桌面及本地 Chromium 页面接口，支持中文输入、页面点击、滚动、常用快捷键及 http/https 导航。默认每个授权目标自动创建专用登录目录；可传 `profile` 使用 `sandbox.browser_profiles` 中由主人配置的目录别名。登录态保留在本地，工具不返回 Cookie 或密码；目录正被其他浏览器使用时拒绝重复启动。账号与登录态由主人提供，不能猜凭据。其他原生 Windows 软件沿用窗口截图/消息，效果取决于应用支持，不使用全局剪贴板或全局键鼠。通用软件使用 `background` 模式。

`qqbot_desktop_type` 传 `text`（1–4000 字），不会自动回车；`qqbot_desktop_scroll` 传 `delta`（非零、绝对值≤3000 的像素量，正数向下），默认在图片中央滚动，也可传坐标；`qqbot_desktop_navigate` 传 `url`。它们均要求最新 `observation_id`，每次动作自动返回新图片，重复相同动作不重放输入。浏览器截图是页面视口，鼠标直接使用返回图片的像素坐标；地址跳转用 navigate。CLI 对应 `desktop --operation type --text '中文'`、`--operation scroll --delta 600`、`--operation navigate --url 'https://example.com/'`，均附当前上下文和观察编号。

软件用 `note kind=observation` 保存页面所见，`reflection/preference` 保存主观感受，必须关联真实截图编号；记录的 source 为 `software_observation`，游戏仍为 `fictional_game`。遇到登录、验证或权限阻碍时先保存记录，再用 `sandbox pause` 并传 `text` 原因，暂停当前任务和该目标 autoplay，保留模型会话，等待主人处理。主人处理后可继续。只凭截图不能声称听见音频或看完整段视频。

`autoplay` 适用于软件和游戏，保留并继续最初的具体目标，可传 `text` 更新目标；控制台总开关也须启用。`stop` 可按 `target_id` 停止某个项目，省略时停止本会话所有软件和游戏，保留窗口、存档、登录态和记录。工作会话不往 QQ 自动发进度或完成通知。默认自主阅读和观看；发帖、评论、点赞、关注、私信等改变账号或对外发送内容的操作须由主人明确指定。

游玩循环优先使用四个 `qqbot_desktop_*` 直接工具。工作上下文已绑定游戏，通常只传 `context_file`，无需重复选择进程；多个已授权运行目标时，可用 `targets` 返回的 `session_id`、`target_id` 或最近观察的 `pid` 选择。PID 用于核对已有目标，不会借此接管其他程序或启动游戏。

```json
{"context_file":"当前游玩上下文","observation_id":"上一张图片返回的ID","key":"enter"}
```

上例直接传给 `qqbot_desktop_key`。一次调用完成按键投递、默认等待 150 毫秒、截图和校验，返回的 MCP 内容直接包含真实 PNG。Agent 看图后，把新 `observation_id` 用于下一动作；无需编辑键值脚本、params.json、截图脚本或分析运行日志。点击工具默认接收图片像素坐标，如 `x:1200,y:400`；控制台完成客户区换算。也可指定 `coordinate_space:normalized` 使用 0≤x,y<1 的坐标。需要看动画后续画面时单独调用 `qqbot_desktop_screenshot`，可设 `wait_ms:0..2000`。

galgame 普通对白优先使用 `qqbot_desktop_read`，参数是 `context_file`、最近的 `observation_id`、可选 `max_steps`（0–20，默认配置值 8）和 `key`（仅 enter/space）。先查看首张画面确认处于对白，再调用阅读；`max_steps:0` 只识别不输入。每句取图、确认文本稳定后单次推进，绝不通过长按、Ctrl 或快进跳过文本。`advanced` 是已确认投递的按键数，实际剧情以返回对白和画面为准。

回执的 `dialogue[]` 逐句提供 `text/source/confidence/observation_id`，可直接作为剧情笔记的证据。普通对白 `image_required:false`，MCP 仅返回文本；原 PNG 和本地识别结果仍保存在观察记录。出现选项、菜单、场景明显变化、OCR 低置信度、文本不稳定、输入失败或停滞时停止，`image_required:true` 附上原图供模型判断，禁止自动选分支。定期视觉检查由 `sandbox.galgame_reading.visual_interval` 控制（默认 24 次推进）；重要对白也可 `max_steps:1` 或主动 screenshot。界面特殊时调整 `dialogue_top`（默认 0.62）；OCR 是启发式识别，必要时回到单次看图操作。

重复同一观察的阅读批次只返回缓存，不重放输入；中断或结果不明先 screenshot。原始截图、逐次输入、识别文本与证据编号保留在本地账本。软件任务继续原单次操作接口。CLI 示例：`desktop read --context-file '当前上下文' --observation-id 'OBSERVATION_ID' --max-steps 8`，同样支持 `desktop --operation read`。

直接工具回执仅保留目标、PID、图片尺寸、观察编号和动作状态等必要信息；窗口句柄、进程创建时间、图片哈希及底层传输信息留在审计记录中。重试相同观察编号及动作不会重放输入；已有操作后图片时直接复用。输入已投递但新截图失败会明确返回 `input_posted:true` 和错误，不能重发原输入，只能重新截图；`state:unknown` 也不重放。截图成功不代表选项生效，Agent 仍须分析图片。

MCP 尚未刷新时，CLI 使用相同直接接口，**无需参数文件**：

```powershell
python G:/qqbot/console_tools/cli.py desktop --context-file $qqContext --operation screenshot
python G:/qqbot/console_tools/cli.py desktop --context-file $qqContext --operation key --observation-id '刚返回的ID' --key enter
python G:/qqbot/console_tools/cli.py desktop --context-file $qqContext --operation click --observation-id '新ID' --x 1200 --y 400 --button left
```

CLI 按键和点击同样自动返回新 `image_path`；用 Agent 内置图片查看器打开该文件。CLI 文本使用 UTF-8，中文不会被展开为 `\uXXXX`。MCP 直接返回图片，可省去单独打开文件这一步。更新后刷新 `qqbot-console` MCP 并重载 QQBot，新的游玩任务会自动收到该流程说明；已打开且加载取图适配的游戏无需重启。

浏览器截图另带 `input_fields`（可见输入框的标签、`bounds=[x,y,width,height]`、`center`，坐标均为原图像素）和 `input_focus`；不读取输入框里的文本值。先按目标框的 `center` 点击，确认 `input_focus.editable=true` 再 `type`。未聚焦或目标不可编辑时返回 `BROWSER_INPUT_NOT_FOCUSED`、`state=rejected`、`input_posted=false` 和新截图，明确表示没有输入文字；修正目标后再操作。`ctrl+k` 可用于页面提供的搜索快捷键。`posted` 仍只表示操作已投递，实际结果以新截图为准；结果不明时不重放。

| operation | params 和行为 |
| --- | --- |
| `environment` | 查询 `background/qemu` 模式及环境状态；不会启动游戏 |
| `games` | 列出本主人在本会话授权的游戏及只读资料别名 |
| `apps` | 列出本主人在本会话授权的软件及浏览器登录目录别名 |
| `activities` | 当前任务的最新方向、已提交/已接收版本、真实页面、截图和笔记；阅读控制上下文仅返回绑定任务 |
| `steer` | `text` 为主对话决定的阅读方向，可选 `url` 和 `target_id/session_id`；调整现有任务并打断旧方向，不新建重复任务 |
| `play` | `executable` 或 `target_id`，可加目标 `text`；在独立后台桌面启动或接回已打开的游戏，提交游玩任务 |
| `use` | `executable` 或 `target_id`、目标 `text`；浏览器可加 `url/profile`，提交独立软件使用任务 |
| `browse` | 主人当前消息中的 `url`、目标 `text`，可选 `profile`；自动选择浏览器并提交任务，不要求 exe 路径 |
| `stop` | 可选 `target_id`；取消该游戏排队和运行任务、关闭自主续玩，保留窗口和存档；省略时停止本会话全部已授权游戏 |
| `pause` | 工作会话提供 `text` 原因，暂停当前目标的自主调度，等待主人处理登录/验证等阻碍 |
| `finish` | 主人限定的具体目标确已完成时，先保存有截图依据的 note，再提供 `text` 实际结果；阶段性进度不算完成 |
| `status` | `session_id`；工作上下文自动绑定，不需填写 |
| `observe` | 新截图及 `observation_id`（返回字段 `id`）、路径和窗口信息；MCP 同时返回真实 PNG |
| `key` | 最近 `observation_id` 与 `key`：enter/space/up/down/left/right/escape/s/l/tab/backspace/f5 |
| `click` | 最近 `observation_id`、归一化截图坐标 `x,y`（0≤值<1）、`button`：left/right/middle；后台截图和坐标均为客户区，不含标题栏 |
| `type/scroll/navigate` | 当前 `observation_id` 加 `text/delta/url`；通用软件输入、滚动和浏览器导航 |
| `note` | `text`、真实截图 ID 数组 `evidence`、`kind`：软件 observation、游戏 plot，或 reflection/preference |
| `notes` | 可选 `query`；检索本会话虚构剧情和主观感受 |
| `grant` | 主人当前消息明确给出的资料目录 `path`；默认只读，`writable=true` 还需原消息明确允许写入 |
| `list/read/write` | 相对 `path`，可选 `grant_id` 或 `profile:配置别名`；write 提供 `content`，只新建、不覆盖 |
| `autoplay` | 已授权 `target_id`、`enabled`；自主续玩还需配置总开关 |

CLI 直接使用与上表同名的参数，以连字符书写；`evidence` 用 `--evidence ID`，可重复或一次传多个 ID。`enabled/writable` 使用 `true/false`（也支持 `--enabled/--no-enabled`、`--writable/--no-writable`）。`text/content/task` 可以分别通过 `--text-file/--content-file/--task-file` 从 UTF-8、UTF-8 BOM 或 UTF-16 BOM 文件读取，完整保留中文、引号和换行。

```powershell
python G:/qqbot/console_tools/cli.py sandbox browse --context-file $qqContext --url 'https://example.com/' --text '自己阅读感兴趣的帖子'
python G:/qqbot/console_tools/cli.py sandbox use --context-file $qqContext --executable 'C:/Program Files/Google/Chrome/Application/chrome.exe' --profile video --text '阅读配乐讨论'
python G:/qqbot/console_tools/cli.py sandbox play --context-file $qqContext --executable 'E:/gal2/Template!!/Tenpure.exe' --text '慢慢阅读剧情'
python G:/qqbot/console_tools/cli.py sandbox steer --context-file $qqController --text '搜索花咲 Work Spring 的作曲访谈'
python G:/qqbot/console_tools/cli.py sandbox activities --context-file $qqController
python G:/qqbot/console_tools/cli.py desktop screenshot --context-file $qqWorker
python G:/qqbot/console_tools/cli.py desktop type --context-file $qqWorker --observation-id '最新ID' --text-file '中文输入.txt'
python G:/qqbot/console_tools/cli.py sandbox note --context-file $qqWorker --text '实际页面观察' --kind observation --evidence '真实截图ID'
python G:/qqbot/console_tools/cli.py sandbox write --context-file $qqWorker --path '阅读笔记.txt' --content-file '完整笔记.txt'
python G:/qqbot/console_tools/cli.py sandbox read --context-file $qqWorker --path '阅读笔记.txt'
python G:/qqbot/console_tools/cli.py sandbox grant --context-file $qqContext --path 'G:/资料' --writable false
python G:/qqbot/console_tools/cli.py sandbox autoplay --context-file $qqContext --target-id '已授权目标ID' --enabled false
python G:/qqbot/console_tools/cli.py sandbox pause --context-file $qqWorker --text '实际页面需要主人登录'
python G:/qqbot/console_tools/cli.py sandbox stop --context-file $qqContext
```

`$qqController` 使用 `activity_feedback.controller_context_file`；`$qqWorker` 使用执行分支的“当前上下文文件”。它们不是主人的普通聊天凭据，不可互换。`use/play` 以 `--target-id` 继续既有目标时，不再附 `--executable`；登录目录沿用原目标。`sandbox finish --text '实际结果'` 只用于确实完成主人限定的目标，持续刷帖的阶段性进度不算完成。`sandbox note` 的软件类型为 observation，游戏类型为 plot，个人感受为 reflection/preference。

`desktop click` 默认像素坐标并自动取新图；`sandbox click` 是归一化坐标的底层单次动作，不自动取图。普通执行流程使用 desktop 的直接操作。`sandbox observe` 同时提供 `id` 和 `observation_id`，便于旧脚本兼容。

旧的 `sandbox/desktop --operation <动作> --params-file <JSON文件>` 仍可使用；直接参数和文件参数指定不同值时拒绝。每个操作的字段、条件与限制以对应 `help` 为准，帮助和 MCP 均来自同一契约。更新后重载 QQBot，刷新 `qqbot-console` MCP 至 2.0.0；CLI 新调用立即使用磁盘上的新版本。

工作上下文只允许当前沙箱和状态查询，不能再派任务、授权其他路径、开启自主续玩或发送 QQ 文件。后台工作默认不限总时长、不限操作次数和模型工作轮数，`sandbox.max_actions: 0` 表示不限；正数表示主人主动设置的操作上限，达到后暂停并保留会话。运行中工作上下文不受一天有效期限制，停止后不能再投递输入。截图仍在 60 秒后失效，一张截图只允许一次动作；重复调用不会重复输入。`posted` 只代表已投递，必须用新截图确认；输入失败标为 unknown，不重放。

模型一轮文字返回只写入检查点，控制台会在同一后台分支继续，不把“正在导航”“稍后继续”当作任务完成。持续刷帖、看视频、游玩一直执行到主人停止或遇到具体阻碍。明确限定的目标实际完成时，用 `sandbox finish` 提供实际结果；控制台检查 note 及操作后观察，保存完整结果后才归档完成分支。阶段性日志按轮追加到 `turns.jsonl`，避免长任务把全部执行轨迹堆在内存里。`interval_minutes` 的 30 分钟仅影响已结束任务的自主再次调度，不会终止运行中的任务。

游戏内容作为虚构资料处理，剧情和感受独立存储，不自动修改人设或群记忆。停止游玩保留已打开游戏和存档，正在投递的单次操作可能完成。重启会中断在途任务、释放游戏任务占用，保留记录且不重放输入。游戏不会自动往群里发截图和完成通知。自主调度默认关闭，只有总开关与主人对该游戏的 autoplay 均启用、原授权上下文仍有效时才续玩。

默认 `sandbox.mode: background`：控制台在独立 Windows 后台桌面启动游戏，不切换用户正在使用的桌面。`observe` 用 PrintWindow 读取游戏客户区；`key/click` 用窗口消息向已打开的目标窗口投递虚拟键码、鼠标键和客户区坐标，再次截图供 agent 分析。输入不调用全局键鼠接口、不移动系统光标、不抢前台焦点；用户桌面上的遮挡不影响该后台窗口。每款游戏按目录和进程身份核对目标，输入跨进程互斥；续玩和重启后可接回同一后台桌面的游戏，不会接管用户桌面上的同名窗口。

窗口选择根据客户区、样式、可见/启用状态和 owner 关系判断，过滤零尺寸辅助窗、输入法和提示窗。观察阶段可恢复唯一可信的隐藏或最小化内容窗口，使用 `SW_SHOWNOACTIVATE`；输入阶段不恢复窗口。禁用主窗之上的唯一模态对话框会被选中；多个独立窗口返回 `BACKGROUND_WINDOW_AMBIGUOUS` 与 `details.windows`，不按标题或面积猜测。观察和输入均核对进程创建时间、窗口句柄及客户区尺寸；窗口变化后必须使用新观察编号。

原生窗口使用 PrintWindow 的客户区完整绘制、客户区标准绘制、完整窗口绘制并裁回客户区三种方法，空白或失败时尝试其他方法，并优先复用该目标同类窗口最近成功的方法。回执中的 `capture_method`、`capture_attempts` 和 `window_recovery` 保存实际结果。全部空白返回 `BACKGROUND_CAPTURE_BLANK`，全部绘制失败返回 `BACKGROUND_CAPTURE_UNSUPPORTED`；`details` 保留方法诊断。浏览器页面接口与 Ren’Py 适配仍各自使用原有取图和输入方式。

启动后默认在 60 秒观察等待期内重试暂态窗口/取图错误，不重启程序、不重复输入。配置为 `sandbox.startup_timeout_seconds`（1–300），控制台对应“启动取图等待”；多个独立窗口、权限/模式错误及适配需要重启会立即返回。`sandbox activities` 的 `launch` 仅证明进程启动或附着，`startup.state=waiting_window` 表示还在等待，`startup.state=ready` 和真实观察编号才证明已出图；失败状态保留错误代码和窗口诊断。此等待期不限制后续任务运行时长，也不构成对原生驱动调用耗时的硬保证。

操作后的新图允许来自同一授权目标的子进程或新窗口；显式指定 `pid` 时仍严格检查该 PID。`frame_changed` 比较图片内容，`window_changed` 比较窗口/进程身份，两者都不代表模型目标已实现。相同观察和动作的重复调用复用已保存结果；`input_posted=true` 或 `unknown` 时只重新截图，不重发原动作。

Ren’Py 游戏由控制台在下一次启动时自动加载独立目录中的引擎适配，通过 `RENPY_SEARCHPATH` 添加脚本，不改写游戏脚本或存档。适配从游戏渲染器取得 PNG，`capture: renpy` 的观察对应 `renpy_events` 输入；按键和鼠标坐标只投递到该游戏自己的事件队列，仍留在原后台桌面。通信校验每次启动的凭据、PID、请求有效期、图片哈希及客户区比例；超时输入不重放，无法可靠映射的黑边窗口拒绝操作。游戏正常运行时仍可按自身逻辑保存设置、缓存和存档。

667×100 等 Ren’Py 启动进度条返回 `BACKGROUND_WINDOW_LOADING`，不生成有效观察编号。已有进程若未加载适配且 PrintWindow 返回空白，接口返回 `RENPY_BRIDGE_RESTART_REQUIRED`，游玩任务直接保留该错误并停止，不自动重启、重复投递输入、切桌面或切换捕获模式；须主人允许重启后再经控制台启动。已加载适配但渲染未就绪、捕获失败或超时，也必须先取得新的有效观察再操作。该适配不保证所有 Ren’Py 版本或其他 GPU 引擎兼容。

这里创建的是 Win32 Desktop，由 `CreateDesktopW` 和启动参数 `lpDesktop` 指定；Win+Tab 的虚拟桌面仍共用当前交互输入，不能提供另一套独立键鼠。窗口化和无边框全屏可沿用同一套截图、客户区坐标与窗口消息流程，实际兼容性取决于游戏引擎；系统附加的输入指示小窗口会被排除，多个独立内容窗口仍需先消除歧义。独占全屏或依赖 DirectInput/Raw Input、前台状态的游戏，不能仅凭新建桌面保证后台渲染和输入；优先在游戏设置里使用窗口化/无边框全屏，必须使用独占全屏或真实键鼠时应在虚拟机内操作。

后台桌面仍与主机共用文件和权限，**不是 OS 隔离沙箱**。部分 DirectX、Raw Input 或依赖前台鼠标状态的引擎不支持后台截图/消息；空白截图、窗口不唯一或输入无效时停止并报告，不回退全局键鼠或自动切换模式。需要时可显式配置 `sandbox.mode: qemu` 使用已有游戏虚拟机后端。`profiles` 提供只读资料目录别名，`browser_profiles` 提供主人配置的浏览器专用登录目录别名。

## 显式发送、引用与任务交付

主代理必须调用 CLI，最终文本不会自动进入 QQ。普通回复直接调用 `reply`，绑定当前 `console_context_file`，默认不引用、不 @；按需使用真实消息和成员 ID，不再使用 m1/m2 标签。当前事件已经提供的 ID 可直接使用，缺失时才用 `messages` 查询；跨群、跨账号引用和未知成员会被拒绝，全体 @ 与机器人自身 @ 也会被拒绝。

```powershell
python G:/qqbot/console_tools/cli.py messages --context-file $qqContext
python G:/qqbot/console_tools/cli.py reply --context-file $qqContext --text '直接回复'
python G:/qqbot/console_tools/cli.py reply --context-file $qqContext --text '回应这条问题' --reply-to '真实消息ID'
python G:/qqbot/console_tools/cli.py reply --context-file $qqContext --text '请看一下' --mention '真实成员ID'
python G:/qqbot/console_tools/cli.py reply --context-file $qqContext --text-file '完整回答.txt'
python G:/qqbot/console_tools/cli.py search --context-file $qqContext --query '查询 sufur2 的公开战绩，核对服务器并附来源'
python G:/qqbot/console_tools/cli.py generate-image --context-file $qqContext --prompt '雪中的白色小猫' --count 1
python G:/qqbot/console_tools/cli.py command --context-file $qqContext --text '/status'
python G:/qqbot/console_tools/cli.py deliver --context-file $qqContext --request-id '真实任务ID' --text '结果在这里' --finish
```

一次 `reply` 可重复 `--text` 提交多个气泡，长正文按 `max_reply_length` 分段。引用与 @ 只出现在第一条的第一段。新消息到达、被 @、模型耗时或断线恢复都不会自动添加引用。复杂多目标回复用多次 `message` 暂存，先 `commit` 再 `finish`；`wait` 可取消尚未提交的暂存消息，下一条消息继续。已提交的回复批次不能追加或撤销，但在 `finish` 之前后续工作工具仍可执行。主代理完成实际行动后主动 `finish`，不等待平台回执；上下文随本轮结束封存。最终发送成功为 `accepted`，断线为 `queued`，不确定为 `unknown`，需要排查时再用 `status`。重放同一提交只返回原凭据。

群消息仍按缓存窗口合并并保留每条真实 ID；同一人紧接着补充时，尚未开始的旧回复可以被取消。已经开始发送的回复完整交付。主动话题也使用 `reply`，只允许普通正文，不允许引用、@、任务派发或断线后补发。

完整链路为：QQ/CLI 命令 → 统一正则解析 → 控制台持久队列 → 独立执行分支 → 校验并持久保存成果 → 主会话结果队列 → 主代理 CLI `deliver` → 平台回执。主聊天可以在搜索或生图期间继续响应；结果按主聊天队列处理。主代理尚未批准的成果保持 ready，不自动发送或重复唤醒。

`deliver` 发送任务清单中的全部已验证文件和主代理提供的完成台词；默认不引用、不 @，需要时显式附加 `--reply-to/--mention`。已有文件的 CLI `send` 是主代理直接批准交付，无需再次制作或审核。纯控制命令、错误和候选列表没有附件时，`deliver --finish` 会完整发送正文，`--text` 仅添加可选前言；主代理无需复制结果或另建正文文件。子代理不能调用发送、授权交付或派生任务的接口。

文件数量不限，超过 20 个自动打包 ZIP，单文件超过 1 GB 时跳过并记录。发送顺序和回执持久保存，已受理与状态不明的发送不重放。临时离线或官方回复窗口结束时保留成果，恢复或收到同会话新消息后继续已批准的交付，任务本身不重跑。

## 图片生成配置

GUI“媒体功能与任务”中可选择 Gemini 网页端（`gemini_web`）、原生工具（`native`）或图片 API（`api`）。网页端支持直接上传当前/引用的原图，选择账号模型并下载生成图片；原生模式使用独立反重力子代理。生成结果保存并校验后进入主代理交付队列，不能拿搜图结果替代生成。

网页端默认 `media.gemini_web.auto_browser: true`，按环境变量 → 手动 Cookie 文件 → 已连接浏览器 → 所选浏览器配置读取登录态；`browser` 为 auto/chrome/edge/brave/firefox，`browser_profile` 留空使用最近使用的配置，可填写目录选择其他配置。能读取的登录直接使用；新版 Chrome/Edge 受加密保护或占用时，在 GUI 点“连接当前浏览器”，按连接页说明加载本地 `browser_extensions/gemini_connector` 扩展并刷新页面。首次连接后每分钟及登录 Cookie 更新时同步，无需手工复制；GUI 和命令行机器人重启后恢复同一本机连接。Windows 以当前用户系统加密保存自动登录缓存，不写入任务正文或 `config.yaml`。

`model` 为 pro/flash/flash-lite/auto 或账号支持的模型名称；GUI 在连接后自动读取可用模型并保留当前选择。`proxy_env` 环境变量优先，缺失时默认 `auto_proxy: true` 使用系统代理。手动 `cookie_file` 接受 `{ "__Secure-1PSID": "实际值", "__Secure-1PSIDTS": "实际值" }` 或浏览器导出的 Cookie 数组；`secure_1psid_env`、`secure_1psidts_env` 也继续可用。关闭 `auto_browser` 只使用手动登录，关闭 `auto_proxy` 停止系统代理识别。连接页应在所需的浏览器配置中打开；更换配置时重新连接，显式填写的配置目录优先于连接缓存。

`generate-image`、`generate-video`、`search`、`submit` 支持可选 `--provider` / `--model`；只给 `--model` 时选择网页端。示例：`generate-image --context-file CONTEXT_FILE --prompt '保留人物，背景换成樱花' --provider gemini_web --model pro`。网页端 `submit` 支持 `task_type=search/task/command/process/generate_image/generate_video`，无法代替本地编程或软件操作。`/搜索 --provider gemini_web --model flash 新闻`、`/翻译 --model pro 转成英语：你好` 同样支持；命令选项必须在正文前。生图还可加 `--count 2 --size 1024x1024`，尺寸作为生成要求发送，实际像素以返回文件为准；返回数量不符时保留文件并报告失败。

`generate-video` 默认使用 `media.video_generation_provider: gemini_web`，复用现有网页端登录态、代理和默认模型，无需另外配置密钥。`media.video_generation_enabled` 单独控制视频生成，`video_enabled` 控制视频发送，GUI 媒体页均可设置。示例：

```powershell
python G:/qqbot/console_tools/cli.py generate-video --context-file $qqContext --prompt '小猫在雪地里慢慢向前走' --model pro
python G:/qqbot/console_tools/cli.py submit --context-file $qqContext --type generate_video --task '依据所附图片生成镜头缓慢推进的视频'
```

当前/引用的 PNG、JPEG、WebP 图片自动作为生成参考，也可通过任务描述中的本群 `file_id` 指定附件；模型选择使用网页版账号可用的模型。任务必须返回真实生成视频，文字、静态图片和检索视频不会算作生成成功。成片下载后按文件类型、大小和哈希校验，原件及交付清单保存在任务目录，沿用主代理 `deliver` 交付。视频未生成、账号权限或配额不足时明确报错；下载等待受 `media.gemini_web.timeout_seconds` 限制，中断只重连下载同一结果，不重发生成请求。需要原生工具时显式指定 `--provider native`，视频生成不支持图片 API 后端。接口版本为 2.6.0，更新后重载 bot 和 MCP。

API 模式使用 `image_generation_api_url` 完整地址、`image_generation_model` 和 `image_generation_api_key_env` 指定的环境变量。支持兼容 `POST /images/generations` 的服务，请求为 `model/prompt/n` 和可选 `size`，返回 `data[].b64_json` 或公网图片 URL。可配置本地生成服务；凭据不会跟随重定向发送。图片会校验真实格式、数量、大小与文件哈希，然后进入相同的主代理交付队列。外部接口不自动重试生成请求。

## 会话与恢复

启动后仅在本地准备规则文件，保留原 conversation_id，不调用模型进行预热或人设核验；新群首次收到消息时初始化。人设正文或全局人设切换保留聊天事实和后台任务，在下一条消息中发送一次 `persona_update`，不重发全部规则、不扫描源码。显式重置和主人权限等其他配置变更仍使旧会话失效。后续仅发送当前消息、遗漏的新消息和新识别事实。每群各自隔离。空闲 `bot.idle_recall_minutes` 分钟（默认30）后，将新增对话交给独立整理会话并保存固定摘要；主会话 ID 不变，新消息会取消整理。此过程是本地归档，不保证反重力后端压缩模型上下文。

空闲日志只保存带说话人 ID、昵称、时间和消息关系的实际对话，已确认发送的机器人文字按条记录，私聊也适用。模型请求里的重复预设、工具协议、任务报告和内部思考不作为对话材料。整理按连续话题整合经过、结论与未完成事项，人物观点和承诺保留归属，篇幅随信息量决定，不逐句凑条目。旧格式摘要首次整理时从保留的原始日志重建；原记录里没有的机器人回复不会补造。失败或中断仍保留检查点，不自动重复提交相同批次。

主聊天、任务、视觉识别和空闲整理的 agent 工作回合均不限时。旧版 `antigravity.timeout_seconds` 配置不会终止机器人工作，控制台显示“Agent 工作等待：不限时”；连接探测、网络传输及平台上传仍保留各自的超时。

任务完成后先持久化完整结果，再排入主会话，由主代理通过 CLI deliver 批准交付。重启时尚在运行的任务标为已中断，已有结果保留，控制台给出中断提示。官方 QQ 回复窗口过期或每条消息的 5 次回复额度耗尽时，等待同会话新消息续发；文件发完但通知尚未发出时，只补发通知。已受理文件不重发，状态不明的发送保留为 `unknown` 供核对，不误报完成。

OneBot 消息发送默认给 NapCat 30 秒等待消息确认，RPC 再保留 5 秒接收回执，可通过 `onebot.message_send_timeout_seconds` 调整。`NodeIKernelMsgService/sendMsg` 的 `EventRet.result=0` 只说明底层调用已返回，缺失消息更新回调时仍无法确认送达。此类超时和文件发送中的同类超时均记为 `unknown`，不通过重发或追加“发送失败”来制造重复消息。此设置减少等待预算不足，无法消除 QQ/NapCat 上游异常。

重启期间中断的文件接收标记为 `failed`，不会调用模型重跑或向群里发送失败通知。重启 QQBot 载入代码；新增 MCP 工具需刷新服务或重启 Antigravity，CLI 使用同一新版接口。


真实消息 ID 和后台恢复（2.0.0）：

- `messages` 返回本会话真实消息 ID、发送者 ID 和正文。
- `message --reply-to ID --text 正文` 精确引用；成员 ID 通过 `--mention` 显式提供。旧 `reply-to --label` 返回 `REPLY_LABEL_RETIRED`，不会绑定任何来源。
- `sandbox activities` 返回运行、暂停和失败会话及 `recovery`。
- `sandbox skip --session-id SESSION_ID --url https://example.net/` 跳过当前内容到其他入口。
- `sandbox restart --session-id SESSION_ID` 重建执行分支，保留浏览器和登录目录；已过滤页面禁止重放，可用 `--url` 指定其他入口。
- `recovery_requested` 仅表示已请求；`activities.recovery.state=applied` 和新页面才证明恢复执行。无可用入口或连续拦截且无新阅读进展时暂停，不会无限重试受阻内容。

以上操作均需当前 `--context-file`，可用 `help message`、`help sandbox skip`、`help sandbox restart` 查询完整参数。工作分支不能重启自己；主对话的绑定阅读控制上下文可恢复原授权任务。账号、人设、任务和权限由控制台验证。


## 节约上下文与长期经历（2.5.0）

主聊天常驻 MCP 默认返回精简回执；操作者的 CLI 可加 `--compact`，省略重复示例和完整交付计划。`help <命令> [操作]` 按需查询详细参数；完整状态与审计记录保存在本地。

`qqbot_read_memory` 与 CLI `read-memory --path <清单内路径>` 默认只读摘要，支持 `--query` 按话题检索、`--max-chars` 限制单页；确需原文时加 `--view raw`，使用返回的 `next_offset` 续读。摘要视图不会把 JSONL 原文当作摘要返回。

群聊消息先写入持久日志，周期归档增量概括并合并本次打开至关闭的已有摘要；同一次运行只占一个历史记忆名额。原文保留完整运行记录，可分页追溯。总结失败不推进游标，同一失败批次需显式重试；新资料可继续整理。账号、人设、群的隔离继续生效。

`sandbox memories` 优先检索网页、软件和游戏的累积经历摘要；`sandbox notes` 查询原始观察依据。每积累 8 条笔记自动折叠，任务结束、暂停、取消和关闭时整理余量，启动时补偿中断的总结。摘要区分观察事实、虚构剧情、个人感受和偏好，保留来源、结论与未完成事项。未完成总结的记录显示 `pending`。

回复详略由当前语境决定：普通闲聊简短，解释、求助、讨论和安慰按需要展开，用户明确要求简短时遵守。`reply_style` 是建议，不是硬性句数或字数上限；平台分段不截断完整回答。


## 通讯录与个人成长预设

`contacts`、`preset` 由主代理通过 `qqbot_cli` 或同一控制台 CLI 操作，执行子代理没有修改权限。无需改配置文件或直接访问数据库。账号、人设以不可变身份隔离；人物在当前群聊或私聊范围内以真实平台 ID 识别，昵称相同不会合并。平台当前昵称和历史称呼持久保存，个人推测与偏好由主代理主动记录。

- `contacts list --query '昵称/习惯/ID' --offset 0 --limit 20`：查询与分页。
- `contacts show --user-id '真实成员ID'`：先读取 `revision`，新记录为 0。
- `contacts upsert --user-id '真实成员ID' --revision 0 --reason '本人明确告知' --preferred-name '阿林' --speaking-style '常用简短句子' --habits '晚上讨论游戏' --notes '具体事实及不确定事项'`：增改；可重复 `--aliases` 保存绰号。省略字段保留，空字符串或空数组清除。
- `contacts delete --user-id '真实成员ID' --revision 1 --reason '主人要求忘记'`：删除并留防复活标记，旧归档和平台名片更新不会自动恢复；恢复须显式 upsert。`contacts history --user-id '真实成员ID'` 查询修改来源。
- `preset show`：基础人设和可编辑成长层；`preset update --text '完整成长层正文' --revision 0 --reason '反思对话或真实阅读'` 替换成长层。已有有用内容须合并保留。`preset history` 查询版本，取历史正文再次 update 可回退，`preset reset --revision 1 --reason '恢复基础设定'` 清空成长层。

所有有上下文的命令均加 `--context-file '本轮文件'`。MCP 使用 `action=contacts/preset`、`operation=操作名`、`params={参数}`。修改有版本检查、来源与理由；版本冲突须重新 show。重复相同修改不会重复增加版本。成长层服从宿主规则，不修改基础人设 ID、会话 ID、权限、账号或全局配置；下一轮增量载入，无需重置。`/name`、主人 `/nickname` 与通讯录称呼同步。

启动时恢复会话、人名、通讯录及事件索引，已有 CID 沿用身份与历史；工具协议更新只发协议，成长修订只发成长层。原来的完整人设只在新会话建立或基础人设确实改变时载入。

成功归档后，完整摘要的 `events`、`people`、`growth` 分区会原子写入事件簿、通讯录和复盘待办，中断重试不会重复投影；旧归档会在升级时补建索引。人物含 `facts/preferences/relationships/tags/timeline`，事件含稳定 ID、时间、参与者、标签、来源和状态。`events list --user-id '真实成员ID' --tag '配乐' --since '2026-11-01' --until '2026-11-10'` 可组合查询；`events show/history` 查详情和更改记录，修订先取 revision。日期支持 ISO 日期/带时区时间，未知时间留空。

归档待办通过 `memory pending --limit 1` 按需展开。主代理核对人物和事件后，遇到主人纠正、反复行为、真实阅读带来的持续兴趣或冲突证据，应明确决定是否修改成长层：实际 `preset update` 后 `memory review --archive-id '实际ID' --decision update --revision 1 --reason '具体依据'`；保持现状也要 `decision keep` 说明理由。每份待办 review 后再 finish，单独 finish 不会消除待办。失败与退出均保留待办。内部整理不向 QQ 发言或派任务；清空会话记忆同步清除相关人物、事件和待办，成长预设保留。

## 持续自主探索与文本阅读

`self_activity_enabled` 与 `autonomous_enabled` 同时开启时，每个空闲间隔先复盘，再保持一项实际资料探索；已有浏览或探索任务时沿用，避免重复派发。探索身份、主人目标、上次发现和下一步跨重启保留，真实阅读成果进入事件簿与成长复盘。关闭自主活动会停止相应任务，新的聊天优先于后台思考。

本机来源默认包括会话探索工作区、`data/library`、`self_activity_roots`、本地媒体资料库、只读 `sandbox.profiles` 和当前会话主人已明确授予的资料目录。浏览器账号目录不作为资料来源。使用 `explore sources/list/read` 选取和分页读取 2 MB 内文本；`fetch --url '公开网址'` 保存源码并读正文，`parse --root-id output --path '回执中的文件名' --format links/html/json` 提取链接、源码或 JSON（`--query a.b.0`）。授权目录以 root_id 绑定并检查路径范围。网页拉取复用公网与重定向校验。正文和结构化数据优先读取/解析，截图用于视觉与交互；普通工作代理可在自身工作区写脚本处理数据。
