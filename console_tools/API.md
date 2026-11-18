# QQBot 固定 CLI 接口表

语音 `spoken_text` 可选 `[fast]短句[/fast]`、`[slow]短句[/slow]`、`[stretch]短词[/stretch]`，分别为当前速度的 1.2、0.8、0.65 倍（限 0.5–2）。模型按语境自行选择，默认不添加。成对闭合且不可嵌套，可组合情绪及语言标记，不朗读标签；QQ 与直播均支持。

本文件随 MAIN.md 在主会话建立、恢复后的协议更新时载入一次；日常事件不重复接口和规范。
MCP 快捷回复：qqbot_reply(context_file,text,continue_work=false,reply_to?,mention?,request_key?,voice=false,emotion?,spoken_text?)。text 支持字符串或字符串数组；语音仅一条字符串且必须选择真实 emotion，spoken_text 为实际朗读台词。普通回复默认显式结束。需继续工具则 continue_work=true，完成后 qqbot_cli action=finish。
通用调用：qqbot_cli(action,context_file,operation?,params?)。operation 只用于下列分组操作，参数放 params。context_file 始终取当前事件的 console_context_file；身份、会话和权限由宿主绑定，表中接口不代表已授权。
命令行：QQBotCLI.exe <命令> [操作] --context-file <当前文件> [--参数 值]。参数名在 CLI 中把下划线改为短横线；task_type 对应 --type。多行或引号正文可用 --text-file/--task-file/--prompt-file/--content-file。
下面参数名后 * 表示必填；未标记的省略时用宿主默认值。成功回执不等于传输成功，以实际状态为准。

## 基础命令

- `sing status`：查询唱歌开关、后端与当前角色；唱歌独立于普通语音朗读。
- `sing cover(song*, lyrics*, emotion, accompaniment, vocals_only, pitch_shift, song_output, request_key)`：歌曲原唱与歌词转为角色歌声，保留原唱旋律、节奏和发音；歌词留档，不换词。song 是本会话文件 ID、授权路径或公网直链，歌词可用 --lyrics-file；默认 emotion=neutral，song_output=file（MP3），可选 voice（腾讯 SILK）或 both。已有干声用 vocals_only=true。
- `sing compose(song*, lyrics*, language, style, emotion, pitch_shift, song_output, seed, request_key)`：song 为纯伴奏；根据歌词创作演唱，再转换角色声音。language 默认 ja，生成正文转假名并保存原歌词。无导唱不能保证原曲唱法。两种任务返回 id/state；后台完成后用 deliver 批准交付，cancel 可停止，不再派普通 task 模拟唱歌。

- `read-memory(path*, view, query, offset, max_chars)`：读取本轮清单内记忆；默认摘要，query 按话题检索，raw 分页读取原始依据。
  返回：text, view, truncated, next_offset
- `voice()`：查询当前启用状态、输出语言、角色、真实情绪标签和说明；选择语音前据此指定情绪。
- `spoken_text` 支持 VoiceStudio 句内标记：`[emotion:标签]` 持续切换，`[emotion:]` 恢复默认 `emotion`；可与 mixed 语言范围组合。标签须全部来自 `voice.emotions`，提交前原子校验。系统按参考分段流式合成、降噪并编码完整 SILK。
- `reply(text*, reply_to, mention, request_key, finish, voice, emotion, spoken_text)`：提交正文即发送并确认对方说完，不等 finish。仅回复时加 --finish；有后续工作时省略，完成后独立 finish。同轮可再 reply 补发，重复内容和幂等键复用回执。重复 --text 分气泡，引用/@仅第一条。--voice 走语音合成和腾讯 SILK 转码，必须 --emotion；日语默认用 --spoken-text 写日语台词，前端转为假名。
  返回：state=committed, next_action=continue_or_finish；--finish 返回 finished；不轮询
- `commit(finish)`：立即提交尚未提交的 message 草稿，不等 finish。--finish 同时显式结束；同轮可多次提交不同批次，重复调用复用回执。
  返回：--finish: state=finished, message_ids, next_action=none；省略: state=committed, next_action=continue_or_finish
- `finish()`：主代理完成回复与工具调用后，最后调用以结束当前回合；保留已提交回复和后台任务，不输出待命文字。无须发言也可直接调用。
  返回：state=finished, message_ids, next_action=none；不再续写、查接口或调用工具
- `message(text*, reply_to, mention, request_key, voice, emotion, spoken_text)`：暂存复杂多目标消息，commit 才发送；普通回复直接 reply。草稿不会被 finish 或其他已提交批次带出。语音参数与 reply 相同。
  返回：id, state=staged；暂存后应 commit
- `messages(limit)`：查询本会话的真实消息 ID、发送者与正文，用于精确引用和 @。
  返回：messages[].id/sender_id/text/received_at
- `deliver(request_id*, text, reply_to, mention, finish)`：批准后立即开始交付全部成果，不等模型最终结束。无附件命令正文完整发送，text 仅前言。--finish 只提交交付，后续必要操作完成后独立 finish；默认不引用、不 @。
  返回：id, state, delivery=approved；命令正文不会被前言替换，无需轮询，实际发送以平台回执为准
- `search(query*, provider, model, request_key)`：提交独立搜索任务，可单次选择网页端 Gemini。
  返回：id, state
- `generate-image(prompt*, count, size, provider, model, request_key)`：生成图片；支持网页端 Gemini、原生工具和图片 API，携带当前/引用原图。
  返回：id, state；不会用搜图替代生成
- `generate-video(prompt*, provider, model, request_key)`：生成视频，默认使用网页端 Gemini；支持模型选择和当前/引用参考图。
  返回：id, state；保存并校验真实生成的视频，不用检索视频替代
- `command(text*, request_key)`：正则解析并规范化聊天命令，由控制台分发执行；未知命令返回明确错误。
  返回：id, state, command
- `reply-to(label*, quote, segment)`：已停用标签引用；请用 message --reply-to 真实消息ID。
- `context(limit)`：查看本上下文可用命令、绑定任务、工作目录、收件箱和可直接执行的示例。
  返回：role, capabilities, binding, output_dir, files, examples
- `wait()`：本轮无需发言时静默结束；对方未说完请改用 speech hold 保存原话。调用后不轮询。
  返回：state=waiting
- `send(files*, request_key)`：主代理排队交付已有成果；不调用制作模型，默认不引用、不 @。
  返回：id, state, next_action=finish；直接结束，不轮询
- `inbox(limit)`：列出当前会话收到的 QQ 文件。
  返回：files[].file_id/name/state/local_path
- `receive(file_id, request_key)`：直接接收 QQ 文件到本地，不调用模型。
  返回：id, state；status 完成后 files[].local_path
- `submit(task_type, task*, provider, model, request_key)`：提交独立后台任务；generate_video 默认网页端，search/task/command/process 可单次选择网页端 Gemini。
  返回：id, state；以 status 结果为准
- `tasks(states, limit)`：列出本发起人的普通任务和持续子代理信件；自主信件仅主人可管理。
  返回：tasks[].id/kind/task/state；agent 信件含 result.agent_id/message_id；绑定上下文只列原任务
- `status(request_id)`：查询任务状态、实际成果和错误。
  返回：id, state, result, files, retryable
- `cancel(request_id)`：停止自己的普通任务或单封子代理信件；不关闭共享代理，不取消其他成员信件。
  返回：id, state=cancel_requested/cancelled 或原终态；自主信件仅主人可管理
- `stickers(query, limit)`：按当前聊天的情绪和主题查询真实表情包。
  返回：stickers[].id/tags
- `sticker(sticker_id*)`：发送搜索结果中的一张真实表情包；每轮最多一张。
  返回：id, state；如需文字继续用 reply，否则直接结束，不轮询

## sandbox

- `sandbox skip(session_id, target_id, url)`：跳过当前受阻内容，撤销旧执行凭据并从其他入口建立干净分支。
  返回：recovery_revision, state；activities.recovery.state=applied 才表示实际恢复
- `sandbox restart(session_id, target_id, url)`：重建当前执行分支，保留同一浏览器和登录态；受阻页面不能重放，可提供其他网址。
  返回：recovery_revision, state；失败/暂停任务沿用原会话恢复
- `sandbox environment()`：只读查询后台环境是否就绪，不启动软件。
  返回：模式和环境状态
- `sandbox games()`：列出当前主人在本会话授权的游戏。
  返回：games, profiles
- `sandbox apps()`：列出已授权软件和可用登录目录别名。
  返回：apps, browser_profiles
- `sandbox activities()`：读取启动阶段、实际画面、笔记和方向接收状态；startup.state=ready 才表示已取得有效画面。
  返回：activities[].request_id/session_id/target_id/goal/launch/startup/page/notes/direction_revision/applied_revision
- `sandbox steer(target_id, session_id, text*, url)`：主对话实时调整既有任务的阅读方向；不会新开浏览器。
  返回：原 id, direction_revision；activities.applied_revision 确认后台接收
- `sandbox play(executable, target_id, text)`：提交游戏目标，或实时调整同一运行目标。
  返回：id, state
- `sandbox use(executable, target_id, text, url, profile)`：提交已授权软件使用目标，或调整同一任务。
  返回：id, state
- `sandbox browse(url*, text, profile)`：按主人当前网址自动选择浏览器并提交浏览任务。
  返回：id, state
- `sandbox stop(target_id)`：停止目标及自动续用；省略 target_id 时停止当前主人全部已授权目标，保留窗口和登录态。
  返回：sessions, requests, state=stopping
- `sandbox pause(session_id, text*)`：记录登录/验证等具体阻碍并暂停当前任务。
  返回：结束请求回执；直接结束本轮
- `sandbox finish(session_id, text*)`：仅在主人明确限定的目标实际完成后提交结束，需先保存有截图依据的笔记。
  返回：结束请求回执；阶段性进度继续执行
- `sandbox status(session_id)`：查询软件/游戏会话及最新阅读方向。
  返回：会话状态和 direction
- `sandbox observe(session_id)`：底层单次截图；日常使用 desktop screenshot。
  返回：id, observation_id, image_path, direction_revision
- `sandbox key(session_id, observation_id*, key*)`：底层单次按键，不自动取图；日常使用 desktop key。
- `sandbox click(session_id, observation_id*, x*, y*, button)`：底层单次点击，x/y 均为 0≤值<1，不自动取图；日常使用 desktop click。
- `sandbox type(session_id, observation_id*, text*)`：底层单次输入；日常使用 desktop type。
- `sandbox scroll(session_id, observation_id*, delta*, x*, y*)`：底层单次滚动，需归一化 x/y；日常使用 desktop scroll 可省略坐标。
- `sandbox navigate(session_id, observation_id*, url*)`：底层单次导航；日常使用 desktop navigate 自动取得新图。
- `sandbox note(session_id, text*, evidence*, kind)`：保存实际阅读内容或感受，关联真实截图 ID。
  返回：id, source, kind
- `sandbox memories(query, limit)`：优先查询软件/网页/游戏经历的累积语义摘要；notes 查询原始依据。
  返回：memories[].summary/conclusions/open_questions/source/pending
- `sandbox notes(query)`：检索当前授权范围内的原始观察、虚构剧情或感受。
  返回：notes[].text/kind/source
- `sandbox grant(path*, writable)`：申请主人当前消息明确提供的资料目录；默认只读。
  返回：id, path, writable
- `sandbox list(session_id, path, grant_id)`：列出当前工作区或授权资料目录。
  返回：files[].name/directory
- `sandbox read(session_id, path*, grant_id)`：读取工作区或授权资料中的 UTF-8 文本，最多 1 MB。
  返回：text
- `sandbox write(session_id, path*, grant_id, content*)`：新建文本，不覆盖已有文件；content 可用 --content-file 读取。
  返回：path
- `sandbox autoplay(target_id*, enabled*, text)`：开启/关闭已授权目标的后续自主续用；运行任务本身默认不限时、不限次数。
  返回：enabled, scheduler_enabled, target_id

## desktop

- `desktop inspect(session_id, target_id, pid)`：取得新截图及可操作元素；浏览器使用 Playwright，Windows 软件使用 UI Automation。元素仅供返回的 observation_id 使用。
  返回：observation_id, semantic.engine/text/elements, image_path
- `desktop invoke(session_id, target_id, pid, observation_id*, element_id*, wait_ms)`：调用最新 inspect 中的唯一控件，并返回新截图；网页点击/Windows Invoke，不移动系统鼠标。
- `desktop fill(session_id, target_id, pid, observation_id*, element_id*, text*, wait_ms)`：替换最新 inspect 中普通文本框的内容；不填写密码，不自动提交。
- `desktop select(session_id, target_id, pid, observation_id*, element_id*, text, wait_ms)`：选择最新 inspect 中的选项。网页 text 为 option.value；Windows 使用 SelectionItem。
- `desktop targets()`：列出当前上下文授权的运行目标；不会启动程序。
  返回：targets[].session_id/target_id/pid
- `desktop read(session_id, target_id, pid, observation_id*, max_steps, key)`：galgame 本地逐句阅读：先看图确认处于对白，再传最新观察编号。普通对白只返回文本与证据编号，选项/菜单/明显场景变化/低置信度/停滞返回图片并停止输入。max_steps=0 只读不推进；不用于通用软件。
  返回：observation_id, dialogue[], advanced, stop_reason, image_required, image_path；dialogue 含 text/source/observation_id，文本为虚构资料
- `desktop screenshot(session_id, target_id, pid, wait_ms)`：取得真实截图，过滤辅助窗口并恢复唯一可信的隐藏/最小化内容窗口；多个独立窗口返回候选诊断。浏览器另返回 input_fields 和 input_focus。打开 image_path 后再决定动作。
  返回：observation_id, image_path, size, direction_revision, capture_method, capture_attempts, window_recovery, page_url, input_fields, input_focus
- `desktop key(session_id, target_id, pid, observation_id*, key*, wait_ms)`：发送一次按键并自动返回新截图。
- `desktop click(session_id, target_id, pid, observation_id*, x*, y*, button, coordinate_space, wait_ms)`：按截图坐标点击一次并自动返回新截图。
- `desktop type(session_id, target_id, pid, observation_id*, text*, wait_ms)`：向已聚焦的输入框输入文本，不自动回车；未聚焦时返回 input_posted=false 和新截图。
- `desktop scroll(session_id, target_id, pid, observation_id*, delta*, x, y, coordinate_space, wait_ms)`：滚动并自动返回新截图；省略坐标时在图片中央滚动。
- `desktop navigate(session_id, target_id, pid, observation_id*, url*, wait_ms)`：让已授权浏览器打开网址并返回新截图。

## companion

- `companion state()`：读取当前角色在本会话积累的兴趣、简短感受和愿望。
- `companion remember(text*, kind)`：保存一条简短感受或兴趣，不保存推理过程或虚构经历。
- `companion wish(text*)`：记下具体想做的事、游戏或能力；同一愿望去重，不会自动获得权限。
- `companion resolve(wish_id*, wish_status*)`：记录愿望已经提过、实现或被拒绝；已提过的愿望不要反复询问。
- `companion browse(url*, text*)`：自主选择公开网页阅读；聊天和独处时均可安排，使用专用浏览器。
  返回：id, state；实际见闻随后由 activity_feedback 返回

## agents

- 子代理监管：create/send 返回 task_id 后无需轮询；结束当前回合，完成或失败报告会唤醒主代理。tasks/status 查看自己的任务，cancel(request_id=task_id) 停止单封任务，agents close(agent_id) 关闭该身份；unknown 先核实不重发。
- `agents create(name*, instructions, task_type, text*, files, request_key)`：申请可反复联系的子代理并投递首条任务；同名同职责沿用原身份和工作区。
  返回：id, name, message_id, task_id, state=queued；task_id 可通过 tasks/status/cancel 管理，结果会唤醒主代理
- `agents send(agent_id*, text*, files, request_key)`：给指定子代理追加消息；忙碌时排队，同一子代理顺序处理，不同子代理并行。
  返回：id, message_id, task_id, state=queued
- `agents list(limit)`：列出当前账号、人设和会话版本的子代理及其状态。
  返回：agents[].id/name/task_type/state
- `agents read(agent_id, after, limit, message_id, offset, max_chars)`：读取子代理往来消息；可按 agent_id/after 过滤，用 message_id 和 offset 读取长消息。
  返回：messages[].seq/agent_id/direction/text/files/state，next_after
- `agents close(agent_id*)`：结束指定子代理，取消未处理的消息；保存工作区和信箱后归档模型会话。
  返回：id, state=closing/closed
- `agents report(text*, files, finish, request_key)`：仅子代理：向主代理传回进度或成果，可附工作区文件；finish=true 提交最终报告，待工具返回后自然结束。
  返回：seq, state=submitted；不能直接发送 QQ 消息

## speech

- `speech status()`：查看当前发言人的未完缓存、当前消息与是否已确认说完。
  返回：required, state, cached_messages, current_messages
- `speech hold(text)`：判断未说完：只调用一次，缓存绑定的原始消息并由程序自动结束本轮，无需再调用 finish/wait/停止回复；下条消息带缓存重新判断。text 仅记简短原因。
  返回：state=waiting, next_action=next_message；本轮已自动结束，不再调用工具、追问或发待命文字
- `speech ready(text)`：兼容旧客户端的可选确认，普通回复不需要调用；直接 reply 即确认；维护完成后 finish。缓存只在本轮成功结束后消费。
  返回：state=ready, next_action=reply_or_finish

## actor

- `actor accounts()`：查询当前跨平台角色的账号及已配置能力。
- `actor state()`：查询角色持续积累的兴趣、感受和愿望。
- `actor memories(query, limit)`：按当前角色及会话范围检索有来源的经历。
- `actor remember(text*)`：保存简短个人反思；聊天记录限当前会话。
- `actor feedback(text*)`：保存当前会话的行为反馈，供后续交流参考。
- `actor read(account_id*, url*, request_key)`：使用账号独立浏览器实际阅读配置允许的网址。
- `actor draft(account_id*, text*, url, draft_kind, request_key)`：创建不可变草稿；评论草稿指定 draft_kind=reply_comment 和目标 url。
- `actor publish(account_id*, draft_id*, request_key)`：发布指定草稿，核验登录身份、成功标志并保存证据；需账号配置启用。
- `actor comment(account_id*, draft_id*, request_key)`：提交指定评论草稿；目标由草稿绑定。需账号启用并配置评论规则。
- `actor status(request_id*)`：读取角色动作回执；unknown 只能观察核对，不得重发。

## contacts

- `contacts list(query, offset, limit)`：查询当前账号、人设、会话的通讯录；按 ID、名字、绰号或习惯检索，可分页。
  返回：contacts, next_offset
- `contacts show(user_id*)`：读取一个人的称呼、历史平台昵称、画像与 revision；删除标记不会被归档复活。
- `contacts upsert(user_id*, revision*, reason*, preferred_name, aliases, speaking_style, habits, notes, facts, preferences, relationships, tags)`：按真实成员 ID 维护称呼、事实、偏好、关系和标签；省略保留，空值清除，先 show。归档自动附加来源时间线。
- `contacts delete(user_id*, revision*, reason*)`：删除人物画像并留下防止旧归档复活的标记；恢复须显式 upsert。
- `contacts history(user_id*, limit)`：查询此成员的修改依据与历史版本。

## preset

- `preset show()`：读取基础人设及可编辑的个人成长预设与 revision；总规则和权限不可修改。
- `preset update(text*, revision*, reason*)`：替换本人的成长预设；依据自身行为、主人命令、真实阅读与反思，下一轮增量生效并保留会话。
- `preset reset(revision*, reason*)`：清空成长预设，保留基础人设；可用 history 内容再次 update 回退。
- `preset history(limit)`：查看预设历次修改正文、来源和理由。

## events

- `events list(query, user_id, tag, since, until, offset, limit)`：按关键词、人物、标签和时间段查事件簿；人物资料另查 contacts。
  返回：events, next_offset
- `events show(event_id*)`：读取一个事件的经过、时间线、参与者、证据和版本。
- `events upsert(event_id, revision*, reason*, title, text, started_at, ended_at, people, tags, event_status, evidence)`：创建或修订事件；更新先 show，传 revision，省略字段保留。
- `events delete(event_id*, revision*, reason*)`：删除事件并保留历史及防止旧归档复活的标记。
- `events history(event_id*, limit)`：查看事件的修改时间线和来源依据。

## memory

- `memory pending(limit)`：读取本会话待复盘的结构化归档与成长候选，可限制条数。
- `memory review(archive_id*, decision*, revision*, reason*)`：确认核对事件和人物，记录本次成长的保留或更新理由；完成后再 finish。

## explore

- `explore sources()`：列出本机工作区与已授权的只读资料目录。
- `explore list(root_id, path, query, offset, limit)`：按目录和关键词选择有兴趣的文本；禁止越过授权范围。
- `explore read(root_id, path*, offset, max_chars)`：直接分页读取本机文本，保留真实来源。
- `explore fetch(url*, format, query, offset, max_chars)`：取得公开网页 HTML/JSON 并保存源码，优先正文；可查询链接和源码，不需要截图。
- `explore parse(root_id, path*, format, query, offset, max_chars)`：对已保存文本做正文/链接提取或 JSON 字段解析；JSON query 使用 a.b.0 路径。

## 参数类型

- `root_id` string {"maxLength":120}：explore sources 返回的已授权资料目录标识。
- `format` string {"enum":["text","html","links","json"]}：正文、HTML 源码、链接或 JSON 字段解析。
- `event_id` string {"maxLength":100}：events list 返回的事件 ID；创建时可省略。
- `archive_id` string {"minLength":1}：memory pending 或 memory_maintenance 返回的归档 ID。
- `decision` string {"enum":["keep","update"]}：成长复盘结论；update 需先实际修改成长预设。
- `title` string {"minLength":1,"maxLength":200}：事件的明确标题。
- `started_at` string {"maxLength":50}：事件开始的 ISO 日期或日期时间；未知时留空。
- `ended_at` string {"maxLength":50}：事件结束的 ISO 日期或日期时间。
- `since` string {"maxLength":50}：时间线起点（ISO 日期或日期时间）。
- `until` string {"maxLength":50}：时间线终点（ISO 日期或日期时间）。
- `event_status` string {"enum":["open","resolved","historical","uncertain"]}：事件状态。
- `tag` string {"maxLength":60}：精确查询标签。
- `tags` array {"items":{"type":"string"},"maxItems":60}：可查询的主题、人物或兴趣标签。
- `people` array {"items":{"type":"string"},"maxItems":30}：事件关联的真实成员 ID。
- `facts` array {"items":{"type":"string"},"maxItems":60}：已确认的人物事实；替换列表，空列表清除。
- `preferences` array {"items":{"type":"string"},"maxItems":60}：本人有依据的偏好。
- `relationships` array {"items":{"type":"string"},"maxItems":60}：已确认的人物关系。
- `user_id` string {"minLength":1,"maxLength":128}：本会话真实平台成员 ID；不以昵称识别或合并人员。
- `revision` integer {"minimum":0}：先 show 取得当前版本；新记录为 0，冲突后重新读取。
- `reason` string {"minLength":1,"maxLength":1000}：本次修改的实际依据、来源或主人要求。
- `preferred_name` string {"maxLength":120}：今后使用的称呼；空串清除自定义称呼。
- `aliases` array {"items":{"type":"string"},"maxItems":30}：绰号及旧称呼；替换该列表，空列表清除。
- `speaking_style` string {"maxLength":2000}：观察到的说话方式，区分事实与推测。
- `habits` string {"maxLength":2000}：经对话确认的习惯及偏好。
- `notes` string {"maxLength":3000}：人物关系、需要记住的事实与不确定事项。
- `account_id` string {"minLength":1}：actor accounts 返回的当前角色账号 ID。
- `draft_id` string {"minLength":1}：actor draft 返回的不可变草稿 ID。
- `draft_kind` string {"enum":["publish_post","reply_comment"],"default":"publish_post"}：草稿用途。
- `element_id` string {"minLength":1}：最新 desktop inspect 返回的元素 ID。
- `agent_id` string {"minLength":1}：agents create/list 返回的固定子代理 ID。
- `name` string {"minLength":1,"maxLength":80}：本会话内唯一的子代理名称；重复申请相同职责会沿用身份。
- `instructions` string {"maxLength":6000}：子代理的固定职责；首条任务和后续消息使用 text。
- `after` integer {"minimum":0,"default":0}：仅读取此信箱序号之后的消息；首屏为 0。
- `message_id` integer {"minimum":1}：agents read 返回的 seq；指定后可用 offset/max_chars 分页读完整消息。
- `wish_id` string {"minLength":1}：companion state 返回的愿望 ID。
- `wish_status` string {"enum":["mentioned","fulfilled","declined"]}：mentioned 已提过，fulfilled 已实现，declined 被拒绝；不构成授权。
- `view` string {"enum":["summary","raw"],"default":"summary"}：summary 返回精简摘要，raw 按字符分页读取原始依据。
- `offset` integer {"minimum":0,"default":0}：上一页返回的 next_offset；首屏为 0。
- `max_chars` integer {"minimum":256,"maximum":16000,"default":4000}：单页最大字符数。
- `provider` string {"enum":["","native","api","gemini_web"],"default":""}：单次任务后端；省略沿用配置。网页端使用 gemini_web。
- `model` string {"maxLength":200,"default":""}：单次网页端/API 模型；只给模型时选择网页端，auto 使用账号默认模型。
- `finish` boolean {"default":false}：reply/commit：同时显式结束回合；deliver：批准交付，仍需独立 finish；agents report：最终报告。
- `reply_to` string：messages 返回的本会话真实消息 ID；省略时不引用。
- `mention` string：需要通知的本会话成员 ID；省略时不 @，禁止 all 或机器人自身。
- `prompt` string {"minLength":1}：要生成的图片或视频描述；多行可使用 --prompt-file。
- `count` integer {"minimum":1,"maximum":10,"default":1}：生成图片数量。
- `size` string {"default":"auto","pattern":"^(auto|[1-9][0-9]{1,4}x[1-9][0-9]{1,4})$"}：生成图片尺寸；auto 使用服务默认值。
- `label` string {"minLength":1}：旧标签参数，仅用于返回停用提示；使用 message --reply-to 真实消息ID。
- `quote` boolean {"default":true}：是否实际引用选中语句。
- `segment` integer {"minimum":0,"maximum":1000,"default":0}：第几条回复；0 为整轮默认来源。
- `session_id` string：已授权会话 ID；工作和阅读控制上下文已绑定，可省略。
- `target_id` string：apps/games/activities 返回的目标 ID。
- `observation_id` string：最近一次截图返回的 observation_id，一张观察只用于一次动作。
- `pid` integer {"minimum":1}：可选，核对已授权的进程；不能借 PID 接管其他进程。
- `executable` string：主人当前消息提供的应用 .exe 绝对路径。
- `text` string {"minLength":1}：目标、输入文本、笔记或实际结果；多行可使用 --text-file。
- `url` string：http/https 网址；不支持脚本、本地文件和带账号密码的网址。
- `profile` string：browser_profiles 中的专用登录目录别名。
- `key` string：支持的按键；详见本命令 enum。
- `x` number {"minimum":0}：图片横坐标；desktop 默认像素，sandbox 底层输入使用归一化坐标。
- `y` number {"minimum":0}：图片纵坐标；desktop 默认像素，sandbox 底层输入使用归一化坐标。
- `button` string {"enum":["left","right","middle"],"default":"left"}：鼠标键。
- `coordinate_space` string {"enum":["pixels","normalized"],"default":"pixels"}：desktop 坐标单位。
- `wait_ms` number {"minimum":0,"maximum":2000}：取图前等待毫秒；截图默认 0，动作默认 150。
- `max_steps` integer {"minimum":0,"maximum":20}：本地逐句阅读最多推进次数；0 只读取当前对白，省略使用配置（默认 8），遇选项/场景变化提前停止。
- `delta` number {"minimum":-3000,"maximum":3000}：非零滚动像素，正数向下、负数向上，绝对值最多 3000。
- `kind` string {"enum":["observation","plot","reflection","preference"],"default":"reflection"}：软件 observation，游戏 plot，或个人 reflection/preference。
- `evidence` array {"items":{"type":"string","minLength":1},"minItems":1,"maxItems":20}：实际来源标识：事件用消息 ID 或归档来源，阅读笔记用 observation_id；可重复 --evidence。
- `query` string：检索关键词；省略时取最近记录。
- `path` string：grant 使用主人明确提供的目录；list/read/write 使用工作区内相对路径。
- `grant_id` string：可选资料授权 ID 或 profile:只读资料别名。
- `content` string：要新建的文本内容；多行可使用 --content-file。
- `writable` boolean {"default":false}：是否申请写入；还需主人当前消息明确允许。
- `enabled` boolean：true 开启，false 关闭；必须明确指定。
- `request_id` string：提交回执的 id；工作/阅读控制上下文可省略，自动使用绑定任务。
- `request_key` string：可选幂等键；省略时控制台按当前消息和内容去重。
- `task_type` string {"enum":["search","image","generate_image","generate_video","video","file","task","command","code","process"],"default":"task"}：独立任务类型。
- `task` string {"minLength":1}：完整任务要求；多行可使用 --task-file。
- `files` array {"items":{"oneOf":[{"type":"string"},{"type":"object","properties":{"path":{"type":"string"},"name":{"type":"string"}},"required":["path"],"additionalProperties":false}]},"minItems":1}：本地成果路径；重复 --file，或 --files-from JSON 文件（也支持含 path/name 的对象）。
- `file_id` string {"default":"latest"}：inbox 返回的 file_id；默认接收本条消息唯一文件或最近文件。
- `sticker_id` string：stickers 返回的真实表情包 ID。
- `limit` integer {"minimum":1,"maximum":100,"default":20}：最多返回条数。
- `states` array {"items":{"type":"string","enum":["submitted","queued","running","ready","delivered","completed","paused","unknown","cancel_requested","cancelled","failed","interrupted","waiting","staged","committed","finished","accepted"]}}：按任务状态过滤；重复 --state，省略时返回最近任务。

## 状态

- `submitted`：已提交，尚未执行
- `queued`：等待执行
- `running`：执行中
- `ready`：成果待发送
- `delivered`：已取得发送回执
- `completed`：已完成
- `paused`：遇到具体阻碍，进度已保留
- `unknown`：结果不明，禁止重放操作
- `cancel_requested`：正在停止
- `cancelled`：已取消
- `failed`：查看 code/error/hint
- `interrupted`：执行中断；保留历史，不自动重放
- `waiting`：本轮不发言，等待下一条消息
- `staged`：草稿尚未提交；commit 后即可发送
- `committed`：正文已提交立即发送；可继续工具，finish 只结束回合
- `finished`：主代理已主动结束本轮，保留已提交回复和后台任务
- `accepted`：平台已接受消息

`broadcast` 提供直播和运营接口：`status/account/room/listener/events` 查询及弹幕连接，`speak/perform/avatar` 直播台词、歌曲与角色/OBS控制，`draft/drafts/draft_show/draft_delete` 本机草稿，`publish/receipt/live_start/live_stop/danmaku` 显式外发及回执。只对绑定的主人主代理开放运营操作；直播角色事件仅可使用台词、表情、表演和事件查询。参数与示例使用 `help broadcast <操作>`，发布需要最新草稿版本和唯一 request_key。详情见 BROADCAST_README.md。
