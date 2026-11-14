"""One anchored command grammar for QQ ingress and the public CLI."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from console_tools.files import ConsoleFileError


ALIASES = {
    "help": "help|start|帮助", "status": "status|状态", "skills": "skills|技能",
    "clear": "clear|reset|重置|清空|新对话", "persona": "persona|personality|性格|人设|角色",
    "model": "model|模型", "memory": "memory|群记忆", "clearmemory": "clearmemory|清空群记忆|重置记忆",
    "name": "name|setname|改名", "boost": "boost|增强", "media": "media|媒体",
    "search": "search|web|搜索|查询", "generate-image": "generate-image|generate_image|imagegen|draw|图片|生成|生成图片|生图|画图",
    "generate-video": "generate-video|generate_video|videogen|生成视频|生视频|文生视频|图生视频",
    "image": "image|搜图", "video": "video|视频", "file": "file|搜文件|找文件",
    "task": "task|任务", "command": "command|exec|执行", "tasks": "tasks|任务进度",
    "cancel": "cancel|取消任务|取消下载", "library": "library|资料库", "groupfile": "groupfile|群文件",
    "pick": "pick|选择", "next": "next|下一页",
    "botname": "botname|bot-name|机器人名称", "nickname": "nickname|昵称",
    "length": "length|reply-length|字数", "files": "files|file-count|文件|文件数",
    "frequency": "frequency|interval|频次|频率", "file-size": "file-size|filesize|文件大小",
    "bug": "bug|report|提示|问题反馈", "sug": "sug|suggest|suggestion|建议",
    "code": "code|coding|编程|代码", "process": "process|处理",
    "analyze": "analyze|analyse|分析", "summarize": "summarize|summary|总结",
    "translate": "translate|翻译", "write": "write|写作",
    "emotion": "emotion|情绪|心情",
}
GRAMMAR = {name: re.compile(r"[/／](?:" + aliases + r")(?:(?:\s*[:：]\s*|\s+)(?P<arg>[^\x00]*))?", re.I)
           for name, aliases in ALIASES.items()}
NO_ARGS = {"help", "status", "skills", "clear", "memory", "clearmemory", "boost", "media", "tasks", "next"}
MEDIA_CONTROLS = {"library", "groupfile", "pick", "next"}
WORK = {"search", "generate-image", "generate-video", "image", "video", "file", "task", "command", "code", "process",
        "analyze", "summarize", "translate", "write", *MEDIA_CONTROLS}
TASK_PREFIX = {"analyze": "分析以下内容：", "summarize": "总结以下内容：", "translate": "按要求翻译以下内容：",
               "write": "按要求完成写作："}
USAGE = {
    "help": "使用说明", "status": "查看模型、队列与设置", "skills": "查看可用技能",
    "clear": "重置当前对话", "persona": "[预设名]；切换仅主人可用", "model": "[flash/pro/flash_lite]",
    "memory": "查看群记忆", "clearmemory": "清空群记忆（仅主人）", "name": "新称呼；设置自己在本群的称呼（仅群聊）",
    "boost": "查看增强执行能力", "media": "媒体命令帮助", "search": "搜索要求；联网检索",
    "generate-image": "图片描述；生成图片", "generate-video": "视频描述；默认网页版生成，可附图", "image": "关键词；搜索现有图片", "video": "检索现有视频的要求",
    "file": "文件要求；查找文件", "task": "完整任务要求", "command": "完整执行要求",
    "tasks": "查看自己的任务进度", "cancel": "[任务ID]；取消自己的任务", "library": "[关键词]",
    "groupfile": "[关键词]", "pick": "编号 [结果ID]", "next": "候选列表下一页",
    "botname": "[名称]；修改机器人名称", "nickname": "@成员 新称呼",
    "length": "[总字数]；0 为不限，普通聊天生效", "files": "[数量]；超出直发数量自动打包",
    "frequency": "[秒数]；群聊归集与回复间隔", "file-size": "[大小MB/GB]；单个交付文件上限",
    "bug": "问题描述；记录到本地反馈库", "sug": "建议内容；记录到本地反馈库",
    "code": "编程要求；默认 boost，独立 pro 会话执行与验证",
    "process": "[处理要求]；处理当前附件、引用图片或自己最近的文件",
    "analyze": "分析内容", "summarize": "待总结内容", "translate": "目标语言及待翻译内容", "write": "写作要求",
    "emotion": "[文本/分析要求]；可附图片或文本文件，由主代理分析材料中的情感；无内容时分析自己今日心情",
}


def format_help():
    from console_tools.control_commands import ADMIN_COMMANDS, MASTER_COMMANDS
    lines = ["支持命令（中英文同义；参数支持空格、: 或 ：）："]
    for title, names in (("所有成员", [name for name in ALIASES if name not in MASTER_COMMANDS | ADMIN_COMMANDS]),
                         ("仅主人", [name for name in ALIASES if name in MASTER_COMMANDS]),
                         ("管理员或主人", [name for name in ALIASES if name in ADMIN_COMMANDS])):
        lines.append("\n" + title + "：")
        for name in names:
            chinese = next(alias for alias in ALIASES[name].split("|") if re.search(r"[\u4e00-\u9fff]", alias))
            lines.append(f"/{name} /{chinese}：{USAGE[name]}")
    lines.append("\n例：/搜索 今日科技新闻；/code 写一个带测试的计算器；/图片 雪中的白猫。")
    lines.append("生图使用媒体页配置的后端和模型；生成视频默认网页版 Gemini。可用 /图片 --model pro 修改所附原图、/生成视频 --model pro 让所附图片动起来，或 /搜索 --provider gemini_web --model flash 检索要求。选项放在正文前。")
    lines.append("默认工作命令使用反重力独立子代理；可按上述选项使用网页端 Gemini。完成后进入结果队列，由主代理通过 CLI 批准发送。")
    lines.append("/emotion（/情绪、/心情）由当前主代理本人排队分析，不委派子代理。可附图片、文本文件或粘贴对话，分析他人、人物或材料中的情感；不带内容时分析自己在本会话北京时间今日的已保存发言。例：/emotion 分析截图中两人的感情变化（附图）。")
    return "\n".join(lines)


def format_skill_help(context):
    from console_tools.contracts import capabilities
    from console_tools.work_permissions import authorize_work
    lines = ["当前能力与权限（完整命令用 /help）：",
        "普通聊天由主代理通过 CLI reply 提交，全部行动完成后用 finish 结束本轮。",
        "/emotion [文本/要求]：可附图片、文本文件，主代理本人排队分析材料中的情感；无内容时分析自己今日心情。",
        "可发起的后台工作："]
    examples = []
    for kind, command, description in (
        ("search", "/search 今日科技新闻并附来源", "联网检索"),
        ("image", "/image 雪中的白猫", "检索现有图片"),
        ("video", "/video 猫咪短片", "检索现有视频"),
        ("file", "/file Python 入门资料", "检索文件"),
        ("generate_image", "/generate-image 雪中的白猫", "生成图片"),
        ("generate_video", "/generate-video 白猫缓缓向前走", "生成视频"),
        ("code", "/code 写一个带测试的计算器", "boost 编程分支"),
        ("process", "/process 总结所附文件", "处理本次附件"),
        ("task", "/task 分析下述方案并保存报告", "分析、总结、翻译与写作"),
        ("command", "/command 完整执行要求", "独立执行任务"),
    ):
        try:
            authorize_work(kind, context)
        except PermissionError:
            continue
        lines.append(f"• {description}：{command}")
        examples.append(command)
    if not examples:
        lines.append("当前成员没有后台工作权限，请联系主人调整工作权限。")
    allowed = capabilities(context)
    if "agents" in allowed:
        lines.append("持续子代理：CLI agents create/send/list/read/close；同一身份可继续联系，结果先回到主代理信箱。")
    lines.extend(["任务提交后以 CLI tasks/status 的实际状态和成果为准；/tasks 查看自己的普通任务与子代理信件，/cancel [任务ID] 取消自己的任务。自主子代理信件仅主人可管理。",
        "后台成果进入结果队列，主代理核对后通过 CLI deliver 批准交付；信箱报告通过主代理决定后续行动。最终发送状态以平台回执为准。",
        "CLI help <命令> [操作] 查询参数与示例，CLI context 查看本轮权限、目录和真实 ID；使用本轮 console_context_file。"])
    if examples:
        lines.append("MCP 调用示例：" + json.dumps({"action": "command", "context_file": "本轮 console_context_file",
            "params": {"text": examples[0]}}, ensure_ascii=False))
    return "\n".join(lines)


@dataclass(frozen=True)
class ChatCommand:
    name: str
    argument: str = ""

    @property
    def canonical(self):
        return "/" + self.name + (" " + self.argument if self.argument else "")


def parse_command(text, *, natural=False, is_group=False):
    raw = str(text or "").strip()
    if not raw.startswith(("/", "／")):
        if not natural:
            return None
        # Existing natural control expressions are anchored regular expressions;
        # normal questions and quoted commands continue to the main agent.
        from command_deliberation import default_command_deliberator
        result = default_command_deliberator.deliberate(raw, is_group=is_group, is_at_bot=True)
        if result.is_command and result.command_type not in ("media", "chat"):
            raw = result.as_command_str()
        elif result.media_intent and result.media_intent.operation in ("cancel", "status", "next", "pick", "help"):
            intent = result.media_intent
            raw = {"status": "/tasks", "cancel": "/cancel", "help": "/media",
                   "pick": "/pick", "next": "/next"}[intent.operation]
            if intent.operation == "pick":
                raw += " " + str(intent.index) + (" " + intent.result_id if intent.result_id else "")
            elif intent.job_id:
                raw += " " + intent.job_id
        elif re.fullmatch(r"(?:状态|status|任务进度)", raw, re.I):
            raw = "/tasks" if raw == "任务进度" else "/status"
        elif match := re.fullmatch(r"(?:叫我|我叫)\s*[:：]?\s*(.{1,20})", raw):
            raw = "/name " + match[1].strip()
        else:
            return None
    for name, pattern in GRAMMAR.items():
        match = pattern.fullmatch(raw)
        if match:
            argument = (match["arg"] or "").strip()
            if name == "model":
                argument = argument.lower().replace("-", "_")
            if name in NO_ARGS and argument:
                raise ConsoleFileError("COMMAND_ARGUMENT_INVALID", f"/{name} 不接受附加参数。")
            if name in WORK - {"next", "library", "groupfile", "process"} and not argument:
                raise ConsoleFileError("COMMAND_ARGUMENT_REQUIRED", f"/{name} 需要完整任务要求。")
            if name == "pick" and not re.fullmatch(r"(?:[1-9]|[1-9][0-9]|100)(?:\s+[a-f0-9]{12})?", argument):
                raise ConsoleFileError("COMMAND_ARGUMENT_INVALID", "/pick 需要 1–100 的编号，可附搜索结果 ID。")
            if name == "cancel" and argument and not re.fullmatch(r"[a-f0-9]{12,32}", argument):
                raise ConsoleFileError("COMMAND_ARGUMENT_INVALID", "/cancel 的参数必须是实际任务 ID；省略时取消自己的活动任务。")
            return ChatCommand(name, argument)
    raise ConsoleFileError("COMMAND_UNKNOWN", "未知命令。", "发送 /help 查看支持的命令；命令必须完整匹配，不能省略或拼接。")


def submit_command(broker, token, text, request_key=""):
    context = broker._chat_context(token)
    command = parse_command(text, natural=True, is_group=context.get("is_group", False))
    if command is None:
        raise ConsoleFileError("COMMAND_UNKNOWN", "没有匹配到有效命令。", "使用 /help 查看命令。")
    name, argument = command.name, command.argument
    from console_tools.control_commands import authorize
    authorize(name, context)
    if name in MEDIA_CONTROLS:
        receipt = broker._enqueue(token, context, "media_command", command.canonical,
                                  {"command": name, "argument": argument}, request_key)
    elif name in WORK:
        kind = {"generate-image": "generate_image", "generate-video": "generate_video", **{name: "task" for name in TASK_PREFIX}}.get(name, name)
        from command_backends import GENERATION_TASKS, WEB_TASKS, parse_backend_options
        route = {}
        if argument and kind in WEB_TASKS | GENERATION_TASKS:
            try:
                argument, route = parse_backend_options(argument, kind)
            except ValueError as error:
                raise ConsoleFileError("COMMAND_ARGUMENT_INVALID", str(error)) from None
        task = TASK_PREFIX[name] + "\n" + argument if name in TASK_PREFIX else (
            argument if name in ("search", "generate-image", "generate-video", "task", "command", "code", "process") else command.canonical)
        inputs = broker.task_inputs(token, argument, kind)
        if name == "process" and not argument and not inputs["input_file_ids"] and not context.get("input_images"):
            raise ConsoleFileError("NO_FILES", "没有可处理的附件。", "附上文件/图片/视频，或引用图片后发送 /处理 具体要求。")
        if kind in GENERATION_TASKS and route:
            task = json.dumps({"prompt": task, **route}, ensure_ascii=False)
        receipt = broker._enqueue(token, context, kind,
            task or "检查所附文件并完成处理，未提供明确处理目标时说明内容与可执行操作。",
            {**inputs, "boost": name == "code", **{key: value for key, value in route.items() if key in ("provider", "model")}}, request_key)
    else:
        file_reference = "最近的文件" if re.search(r"(?:刚才|最近|刚上传).{0,10}(?:文件|附件)|(?:分析|看看|阅读).{0,4}(?:文件|附件)", argument) else ""
        inputs = broker.task_inputs(token, file_reference, "emotion") if name == "emotion" else {}
        if name == "emotion" and context.get("quoted_message_id"):
            quote = next((row for row in context.get("reply_sources", [])
                          if str(row.get("id")) == context["quoted_message_id"]), None)
            inputs["quoted_text"] = str(quote.get("text") or "") if quote else ""
            inputs["quoted_sender_id"] = str(quote.get("sender_id") or "") if quote else ""
        receipt = broker._enqueue(token, context, "bot_command", command.canonical,
                                  {"command": name, "argument": argument, **inputs}, request_key)
    return {**receipt, "command": command.canonical}


def execute_control(coordinator, request, ctx, metadata):
    """A deterministic command worker; never sends messages itself."""
    client = coordinator.client
    data = json.loads(request["result"])
    command, argument = data["command"], data.get("argument", "")
    cfg = client.app_config.bot
    session_key = metadata["session_key"]
    from console_tools.control_commands import ADMIN_COMMANDS, MASTER_COMMANDS, execute_setting
    if command in MASTER_COMMANDS | ADMIN_COMMANDS | {"sug"}:
        return execute_setting(coordinator, command, argument, ctx, {**metadata, "command_token": request["token"]})
    if command in ("help", "media"):
        return format_help()
    if command == "status":
        session = client.ai.get_or_create_session(session_key)
        generation = client.app_config.media.image_generation_provider
        if generation == "gemini_web":
            generation += "；网页端模型：" + client.app_config.media.gemini_web.model
        return f"当前模型：{session.model}\n机器人名称：{cfg.bot_name or client.personas.get_active_persona().name}\n自动补充话术释义：{'开启' if cfg.search_enabled else '关闭'}\n搜索：独立子代理\n/code：boost 编程子代理（pro）\n图片生成：{generation}\n普通回复总字数：{cfg.reply_char_limit or '不限'}\n文件直发数量：{client.app_config.media.delivery_batch_size}\n单文件上限：{client.app_config.media.delivery_max_file_mb} MB\n回复间隔：{cfg.chat_buffer_seconds:g} 秒\n主聊天与任务队列分别运行。"
    if command == "boost":
        return ("多模态与原生视觉识别引擎状态\n"
                "• 图片与表情识别：独立识别分支，复用已确认的画面与来源。\n"
                "• 意图思考与命令泛化引擎：命令完整正则匹配，控制台统一分发。\n"
                "• 自带搜索与多模态下载引擎：独立子代理执行，成果进入主代理结果队列。\n"
                f"• 图片生成：{'开启' if client.app_config.media.image_generation_enabled else '关闭'}，"
                f"后端 {client.app_config.media.image_generation_provider}。\n"
                "• 消息发送：CLI reply/deliver --finish；引用和 @ 由主代理显式指定真实 ID。")
    if command == "skills":
        from console_tools.work_permissions import current_work_context
        return format_skill_help(current_work_context(cfg, metadata))
    if command == "tasks":
        from console_tools.task_control import list_tasks
        return json.dumps(list_tasks(coordinator.broker, request["token"], limit=20, exclude=request["id"]), ensure_ascii=False)
    if command == "cancel":
        from console_tools.task_control import ACTIVE_STATES, list_tasks, cancel_task
        cancelled = []
        while True:
            ids = [argument] if argument else [row["id"] for row in list_tasks(coordinator.broker, request["token"], states=ACTIVE_STATES, limit=100, exclude=request["id"])]
            if not ids:
                break
            for identity in ids:
                if identity == request["id"]:
                    continue
                row = cancel_task(coordinator.broker, request["token"], identity)
                if row["state"] in ("cancel_requested", "cancelled"):
                    cancelled.append(identity)
            if argument:
                break
        return "已请求取消：" + (", ".join(cancelled) or "当前没有可取消的任务")
    if command == "name":
        if not ctx.is_group:
            raise ConsoleFileError("GROUP_ONLY", "/name 仅支持群聊称呼；私聊暂不支持设置称呼。")
        if not 1 <= len(argument) <= 20:
            raise ValueError("昵称需要 1–20 个字符，例如 /name 晓明。")
        client.memory_mgr.set_participant_nickname(ctx.conversation_id, ctx.sender_id, argument)
        from bot_identity import set_name
        set_name(client, metadata["session_key"], ctx.scope, ctx.sender_id, argument, ctx.message_id)
        if hasattr(client, "_cache_member_name"):
            client._cache_member_name(ctx.conversation_id, ctx.sender_id, argument)
        return "已记录称呼：" + argument
    if command == "memory":
        return client.memory_mgr.get_memory_prompt(ctx.conversation_id) or "本群记忆库为空。" if ctx.is_group else "群记忆仅在群聊中启用。"
    if command == "persona":
        if not argument:
            return "当前人设：" + client.personas.get_active_persona().name + "\n可用人设：" + "、".join(client.personas.list_personas())
        from messaging import is_master_user
        if not is_master_user(cfg.master_id, ctx.sender_id):
            if ctx.is_group and cfg.is_master(ctx.conversation_id, ctx.sender_id):
                if argument not in client.personas.list_personas():
                    raise ValueError("未找到指定人设。")
                from console_tools.control_commands import _save
                _save(client, cfg.get_group_config(ctx.conversation_id), "persona", argument)
                client.sync_system_prompt()
                return "已切换本群人设：" + argument
            raise PermissionError("切换人设仅限配置的主人账号。")
        if not client.personas.set_active(argument):
            raise ValueError("未找到指定人设。")
        client.sync_system_prompt()
        return "已切换人设：" + argument
    if command == "model":
        if not argument:
            return "当前模型：" + client.ai.get_or_create_session(session_key).model + "；可选 flash、pro、flash_lite。"
        if argument not in ("flash", "pro", "flash_lite"):
            raise ValueError("模型仅支持 flash、pro、flash_lite。")
        if not client.ai.set_session_model(session_key, argument):
            raise ValueError("模型切换失败。")
        return "已切换模型：" + argument
    if command in ("clear", "clearmemory"):
        if command == "clearmemory":
            if not ctx.is_group:
                raise ValueError("群记忆仅在群聊中启用。")
            if not cfg.is_master(ctx.conversation_id, ctx.sender_id):
                raise PermissionError("清空群长期记忆库仅限配置的主人账号；普通成员无权清空群共享记忆。")
            client.memory_mgr.clear_memory(ctx.conversation_id)
            client._clear_image_context(ctx.conversation_id)
            client._get_emotion_history().clear(coordinator.namespace, session_key)
        client.ai.reset_session(session_key)
        if ctx.is_group:
            client.context_buffer.clear_group(ctx.conversation_id)
        for row in coordinator.broker.active(coordinator.namespace):
            if row["scope"] == ctx.scope and row["id"] != request["id"]:
                coordinator.broker.update(row["id"], "cancel_requested")
        if client._text_outbox:
            client._text_outbox.cancel(ctx.scope)
        return "已清空本群记忆与对话。" if command == "clearmemory" else "已清空当前对话上下文。"
    raise ValueError("命令尚未实现。")
