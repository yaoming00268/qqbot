"""Public QQBot CLI: one UTF-8 JSON receipt per call, including help/errors."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import sqlite3
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from console_tools.broker import ConsoleBroker
from console_tools.contracts import (ArgumentError, COMMANDS, FIELDS, GROUPS, STATES,
    capabilities, command_spec, flag, help_schema, invocation, schema, validate)
from console_tools.files import file_error, read_json


def _context_view(broker, token, context_file, limit):
    context = broker.context(token)
    allowed = capabilities(context)
    binding = {key: context[source] for key, source in (
        ("request_id", "parent_request_id"), ("session_id", "sandbox_session_id"),
        ("target_id", "activity_target_id"), ("direction_revision", "activity_revision"),
        ("agent_id", "agent_worker_id"), ("message_id", "agent_input_id")) if source in context}
    target_kind = ""
    if context.get("sandbox_session_id"):
        from sandbox_runtime import SandboxRuntime
        runtime = SandboxRuntime(context["sandbox_config"]["directory"])
        session = runtime.session(context, context["sandbox_session_id"])
        binding.update(request_id=session["request_id"], target_id=session["target"])
        target_kind = runtime.game(context, session["target"])["kind"]
    examples = {}
    for action, operations in allowed.items():
        for operation in operations if action in GROUPS else [""]:
            definition = command_spec(action, operation)
            values = {**definition["example"]}
            if definition.get("needs_session") and not context.get("sandbox_session_id"):
                values["session_id"] = "SESSION_ID"
            if action == "status" and binding.get("request_id"):
                values["request_id"] = binding["request_id"]
            if operation == "note" and target_kind:
                values["kind"] = "observation" if target_kind == "software" else "plot"
            examples[" ".join(filter(None, (action, operation)))] = invocation(action, operation,
                context_file=context_file, **values)
    return {"output_dir": context.get("output_dir", ""), "artifact_roots": context.get("artifact_roots", []),
            "work_permissions": context.get("work_permissions", {"access": "all"}),
            "reply_char_limit": context.get("reply_char_limit", 0), "memory_files": context.get("memory_files", []),
            "messages": [] if context.get("agent_worker_id") else broker.messages(token, limit),
            "scope": context["scope"], "role": "reading_controller" if context.get("activity_controller") else
                "agent_worker" if context.get("agent_worker_id") else
                "worker" if context.get("sandbox_session_id") else "owner" if context.get("is_master") else "chat",
            "binding": binding, "capabilities": allowed, "examples": examples,
            **({"speech": broker.speech_status(token)} if "speech" in allowed else {}),
            "files": [] if context.get("sandbox_session_id") or context.get("agent_worker_id") else broker.inbox(token, limit),
            "help": schema()["actions"],
            "instruction": "示例中的 ID、目标和文字占位符须用实际值替换；查询 help <命令> [操作] 获取完整契约，无需源码或数据库。"}


def _status(broker, token, request_id):
    try:
        outgoing = broker.message_status(token, request_id)
        if outgoing is not None:
            return {**outgoing, "files": [], "retryable": False}
        from console_tools.task_control import task_status
        row = task_status(broker, token, request_id)
    except PermissionError as error:
        return {**file_error(error), "id": request_id, "task": "", "result": {}}
    result = row["result"]
    delivered, detail = result.get("delivery") or {}, result.get("result") or {}
    files = delivered.get("files") or result.get("received_files") or detail.get("files") or []
    files = [{key: item[key] for key in ("file_id", "asset_id", "name", "title", "local_path", "size", "sha256", "state")
              if key in item} for item in files]
    return {**row, "task": row["task"][:500], "files": files,
            "retryable": row["state"] not in ("unknown", "delivered", "completed", "cancelled", "interrupted"),
            "result": {key: result[key] for key in ("full_result_file", "success", "source_message_id", "code", "error", "hint",
                "delivery_error", "sandbox_session_id", "summary", "target_id", "direction_revision", "turns", "checkpoint", "recovery",
                "agent_id", "message_id", "name", "task_type", "autonomous") if key in result}}


def execute(action, context_file="", *, task_type="task", task="", request_id="", request_key="",
            files=None, file_id="latest", limit=20, query="", sticker_id="", operation="", params=None,
            states=None, help_action="", help_operation="", label="", quote=True, segment=0,
            text="", reply_to="", mention="", prompt="", count=1, size="auto", finish=False, provider="", model="",
            path="", view="summary", offset=0, max_chars=4000):
    """Shared programmatic entry for MCP and CLI; authorization stays in broker."""
    if action in ("schema", "help"):
        return (help_schema if action == "help" else schema)(help_action, help_operation)
    command_spec(action, operation)
    if not context_file:
        raise ArgumentError("缺少 --context-file，请使用当前事件提供的文件。", action, operation)
    broker, token = ConsoleBroker.from_context(context_file)
    context = broker.context(token)
    if context.get("emotion_analysis") and (action not in capabilities(context)
            or action in GROUPS and operation not in capabilities(context)[action]):
        raise PermissionError("/emotion 由当前主代理本人分析；本轮只允许读取依据和回复，不能委派或创建任务。")
    if (context.get("agent_worker_id") or context.get("agent_event") or context.get("actor_event")) and (action not in capabilities(context)
            or action in GROUPS and operation not in capabilities(context)[action]):
        raise PermissionError("子代理及内部收信事件只通过信箱传递内容，不能发送 QQ 消息或调用其他任务接口。")
    if context.get("self_activity") and (action not in capabilities(context)
            or action in GROUPS and operation not in capabilities(context)[action]):
        raise PermissionError("独立活动只维护兴趣、愿望和阅读，不发送 QQ 消息或执行其他任务。")
    if context.get("proactive_event") and (action not in capabilities(context)
            or action in GROUPS and operation not in capabilities(context)[action]):
        raise PermissionError("主动话题只允许发送普通消息或等待，不能派任务或操作软件。")
    if context.get("memory_maintenance") and (action not in capabilities(context)
            or action in GROUPS and operation not in capabilities(context)[action]):
        raise PermissionError("归档整理只允许维护通讯录和自己的预设，不发送 QQ 消息或派任务。")
    if context.get("idle_event") and not (context.get("self_activity") or context.get("agent_event") or context.get("actor_event") or context.get("memory_maintenance")) and action not in capabilities(context):
        raise PermissionError("空闲事件只允许 wait/finish，不派任务或操作软件。")
    if (context.get("sandbox_session_id") or context.get("parent_request_id")) and action not in capabilities(context):
        raise PermissionError("工作上下文只允许绑定任务的沙箱操作和查询，不发送 QQ 消息或创建新任务。")
    if context.get("activity_controller") and (action not in capabilities(context)
            or action == "sandbox" and operation not in capabilities(context)["sandbox"]):
        raise PermissionError("阅读控制上下文只允许查询或调整绑定任务。")
    if action in GROUPS:
        if (action == "sandbox" and context.get("sandbox_session_id") and not context.get("activity_controller")
                and operation in ("games", "apps", "play", "use", "browse", "autoplay", "grant", "stop", "steer", "skip", "restart")):
            raise PermissionError("执行分支不能授权新路径、派生任务或更改主对话方向。")
        values = {} if params is None else params
        validate(action, operation, values, context)
        if action == "explore":
            from exploration import operate
            value = operate(broker, token, operation, **values)
        elif action in ("events", "memory"):
            from structured_memory import operate
            value = operate(broker, token, action, operation, **values)
        elif action in ("contacts", "preset"):
            from bot_identity import operate
            value = operate(broker, token, action, operation, **values)
        elif action == "actor":
            from agent_core.tools import operate
            value = operate(broker, token, operation, **values)
        elif action == "companion":
            from companion_life import operate
            value = operate(broker, token, operation, **values)
        elif action == "agents":
            from agent_mailbox import AgentMailbox
            value = AgentMailbox(broker).operate(token, operation, **values)
        elif action == "speech":
            value = broker.speech(token, operation, **values)
        else:
            value = broker.sandbox(token, operation, **values) if action == "sandbox" else broker.desktop(token, operation, **values)
        if action == "sandbox" and operation == "observe" and value.get("id"):
            value = {**value, "observation_id": value["id"]}
        return {"ok": True, **value}
    if action == "reply" and isinstance(text, str):
        text = [text] if text else []
    values = {"task_type": task_type, "task": task, "request_id": request_id or context.get("parent_request_id", ""),
              "request_key": request_key, "files": files, "file_id": file_id, "limit": limit, "query": query,
              "sticker_id": sticker_id, "states": states or [], "label": label, "quote": quote, "segment": segment,
              "text": text, "reply_to": reply_to, "mention": mention, "prompt": prompt, "count": count, "size": size,
              "finish": finish, "provider": provider, "model": model,
              "path": path, "view": view, "offset": offset, "max_chars": max_chars}
    selected = {key: values[key] for key in COMMANDS[action]["params"] if values[key] is not None}
    if action == "deliver" and not text:
        selected.pop("text", None)
    validate(action, "", selected, context)
    request_id = values["request_id"]
    if action in ("status", "cancel") and not request_id:
        raise ArgumentError("缺少 --request-id；先用 tasks 查询，绑定任务上下文可省略。", action)
    if action == "read-memory":
        value = broker.read_memory(token, path, view=view, query=query, offset=offset, max_chars=max_chars)
    elif action == "reply":
        value = broker.reply(token, text, reply_to, mention, request_key, finish=finish)
    elif action == "commit":
        value = broker.commit_reply(token, finish=finish)
    elif action == "finish":
        value = broker.finish_turn(token)
    elif action == "message":
        value = broker.stage_message(token, text, reply_to, mention, request_key)
        value.update(next_action="commit")
    elif action == "messages":
        value = {"messages": broker.messages(token, limit)}
    elif action == "deliver":
        value = broker.approve_delivery(token, request_id, text, reply_to, mention, finish=finish)
        value.setdefault("next_action", "finish")
    elif action == "search":
        value = broker.submit(token, "search", query, request_key, provider=provider, model=model)
    elif action in ("generate-image", "generate-video"):
        description = json.dumps({"prompt": prompt, **({"count": count, "size": size} if action == "generate-image" else {})}, ensure_ascii=False)
        value = broker.submit(token, action.replace("-", "_"), description, request_key, provider=provider, model=model)
    elif action == "command":
        from console_tools.chat_commands import submit_command
        value = submit_command(broker, token, text, request_key)
    elif action == "context":
        value = _context_view(broker, token, context_file, limit)
    elif action == "reply-to":
        value = broker.select_reply(token, label, quote, segment)
    elif action == "wait":
        value = broker.wait_for_message(token)
    elif action == "tasks":
        from console_tools.task_control import list_tasks
        value = {"tasks": [_status(broker, token, row["id"]) for row in list_tasks(broker, token, states or [], limit)]}
    elif action == "status":
        value = _status(broker, token, request_id)
    elif action == "cancel":
        from console_tools.task_control import cancel_task
        try:
            row = cancel_task(broker, token, request_id)
        except PermissionError as error:
            return {**file_error(error), "id": request_id, "state": "forbidden"}
        value = {"id": request_id, "state": row["state"]}
    elif action == "send":
        value = {**broker.send_files(token, files, request_key), "next_action": "finish"}
    elif action == "inbox":
        value = {"files": broker.inbox(token, limit)}
    elif action == "receive":
        value = {**broker.receive_file(token, file_id, request_key), "next_action": "status"}
    elif action == "submit":
        value = broker.submit(token, task_type, task, request_key, provider=provider, model=model)
    elif action == "stickers":
        value = {"stickers": broker.stickers(token, query, limit)}
    else:
        value = {**broker.send_sticker(token, sticker_id), "next_action": "reply_or_finish"}
    return {"ok": True, **value}


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ArgumentError(message, getattr(self, "command_name", ""))


def _boolean(text):
    if text.casefold() not in ("true", "false"):
        raise argparse.ArgumentTypeError("请使用 true 或 false。")
    return text.casefold() == "true"


def _common(parser):
    parser.add_argument("-h", "--help", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--context-file", default=argparse.SUPPRESS)
    parser.add_argument("--output", default=argparse.SUPPRESS)
    parser.add_argument("--compact", action="store_true", default=argparse.SUPPRESS)


def _add_field(parser, name, *, repeated_text=False):
    definition = FIELDS[name]
    target = parser
    if name in ("text", "content", "task", "prompt"):
        target = parser.add_mutually_exclusive_group()
        target.add_argument("--" + name + "-file", default=argparse.SUPPRESS)
    options = {"dest": name, "default": argparse.SUPPRESS, "help": definition["description"]}
    if repeated_text and name == "text":
        options["action"] = "append"
    if definition["type"] == "boolean":
        group = parser.add_mutually_exclusive_group()
        group.add_argument(flag(name), nargs="?", const=True, type=_boolean, **options)
        group.add_argument("--no-" + name, action="store_false", **options)
        return
    if definition["type"] == "array":
        options.update(action="append")
        if name == "evidence":
            options["nargs"] = "+"
        if name == "states":
            options["choices"] = list(STATES)
    elif definition["type"] in ("number", "integer"):
        options["type"] = float if definition["type"] == "number" else int
    if "enum" in definition:
        options["choices"] = definition["enum"]
    target.add_argument(flag(name), **options)


def build_parser(actions=None):
    parser = JsonArgumentParser(add_help=False, allow_abbrev=False)
    _common(parser)
    subcommands = parser.add_subparsers(dest="action", parser_class=JsonArgumentParser)
    for action in actions if actions is not None else ["help", "schema", *COMMANDS, *GROUPS]:
        command = subcommands.add_parser(action, add_help=False, allow_abbrev=False)
        command.command_name = action
        _common(command)
        if action in ("help", "schema"):
            command.add_argument("topic", nargs="?", default="", choices=["", *COMMANDS, *GROUPS])
            command.add_argument("topic_operation", nargs="?", default="")
            continue
        if action in GROUPS:
            command.add_argument("operation_name", nargs="?", default="")
            command.add_argument("--operation", default="")
            command.add_argument("--params-file", default="", help="兼容旧调用；所有参数也可直接使用命令行。")
            fields = sorted({key for item in GROUPS[action].values() for key in item["params"]})
        else:
            fields = COMMANDS[action]["params"]
        for name in fields:
            _add_field(command, name, repeated_text=action == "reply")
        if "files" in fields:
            command.add_argument("--files-from", default=argparse.SUPPRESS)
    return parser


def _read_text(path):
    raw = Path(path).read_bytes()
    text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _run(args):
    action = args.get("action") or "help"
    operation = args.get("operation_name") or args.get("operation", "")
    if args.get("help") or action in ("schema", "help"):
        topic = action if args.get("help") and action not in ("schema", "help") else args.get("topic", "")
        return (schema if action == "schema" and not args.get("help") else help_schema)(topic, operation or args.get("topic_operation", ""))
    if args.get("operation_name") and args.get("operation") and args["operation_name"] != args["operation"]:
        raise ArgumentError("操作名与 --operation 不一致。", action)
    command_spec(action, operation)
    values = {name: value for name, value in args.items() if name in FIELDS}
    for name in ("task", "text", "content", "prompt"):
        if name + "_file" in args:
            values[name] = _read_text(args[name + "_file"])
    if "evidence" in values:
        values["evidence"] = [item for group in values["evidence"] for item in group]
    if "files_from" in args:
        if "files" in values:
            raise ArgumentError("--file 和 --files-from 只能使用一种。", action)
        loaded = read_json(args["files_from"])
        values["files"] = loaded.get("files") if isinstance(loaded, dict) else loaded
    if action in GROUPS:
        params = read_json(args["params_file"]) if args.get("params_file") else {}
        if not isinstance(params, dict):
            raise ArgumentError("--params-file 必须包含 JSON 对象。", action, operation)
        for name, value in values.items():
            if name in params and params[name] != value:
                raise ArgumentError("命令行和参数文件指定了不同的 " + flag(name), action, operation)
            params[name] = value
        return execute(action, args.get("context_file", ""), operation=operation, params=params)
    return execute(action, args.get("context_file", ""), **values)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args, status = {}, 0
    try:
        argv = list(sys.argv[1:] if argv is None else argv)
        # Normal calls only need one parser. Discovery still exposes the complete contract.
        selected = argv[:1] if argv and argv[0] in {"help", "schema", *COMMANDS, *GROUPS} else None
        args = vars(build_parser(selected).parse_args(argv))
        with redirect_stdout(sys.stderr):
            result = _run(args)
        status = 1 if result.get("ok") is False or result.get("error") else 0
        result = {"ok": status == 0, **result}
        if (result.get("id") and result.get("state") and args.get("action") not in (
                "reply", "commit", "message", "deliver", "status", "cancel", "sticker", "send", "agents")
                and result.get("ok")):
            operation = args.get("operation_name") or args.get("operation", "")
            next_command = invocation("sandbox", "activities", context_file=args.get("context_file", "")) if (
                args.get("action") == "sandbox" and operation == "steer") else invocation(
                    "status", context_file=args.get("context_file", ""), request_id=result["id"])
            result["next_commands"] = [next_command]
    except ArgumentError as error:
        result, status = file_error(error), 2
    except sqlite3.Error as error:
        result, status = {"ok": False, "code": "CONSOLE_DATABASE_ERROR", "error": str(error),
            "hint": "确认 QQBot 与当前 context_file 可用；保留原任务，不重复提交结果不明的操作。"}, 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        result, status = file_error(error), 1
    except Exception as error:
        result, status = {"ok": False, "code": "CONSOLE_INTERNAL_ERROR", "error": type(error).__name__ + ": " + str(error),
            "hint": "保留当前进度和错误回执，交由主人处理；不要分析源码或重放未知输入。"}, 1
    if args.get("compact"):
        from console_tools.compact import compact_receipt
        result = compact_receipt(args.get("action", ""), result, args.get("operation_name") or args.get("operation", ""))
    if args.get("output"):
        try:
            path = Path(args["output"]).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            result = {"ok": status == 0, "output_file": str(path),
                **{key: result[key] for key in ("id", "state", "code", "error", "hint") if key in result}}
        except (OSError, ValueError) as error:
            result, status = {"ok": False, "code": "OUTPUT_WRITE_FAILED", "error": str(error),
                "operation_result": result, "hint": "本次操作回执保留在 operation_result；结果不明时不要重放操作。"}, 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    if status:
        raise SystemExit(status)


if __name__ == "__main__":
    main()
