"""MCP stdio server for local QQBot console tasks (JSON-RPC, no network)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from console_tools.cli import execute
from console_tools.broker import ConsoleBroker, TASK_TYPES
from console_tools.contracts import COMMANDS, GROUPS, DESKTOP, SANDBOX, VERSION, command_spec, input_schema
from console_tools.files import file_error


DESKTOP_TOOLS = {"qqbot_desktop_" + name: name for name in DESKTOP}


def desktop_schemas():
    result = []
    for operation, definition in DESKTOP.items():
        contract = input_schema("desktop", operation)
        result.append({"name": "qqbot_desktop_" + operation, "description": definition["description"],
            "inputSchema": {**contract, "properties": {"context_file": {"type": "string"}, **contract["properties"]},
                "required": ["context_file", *contract["required"]]}})
    return result


def _all_tools_schema():
    definitions = [{"name": "qqbot_reply", "description": "立即发送台词并结束本轮。需要继续工具时 continue_work=true；完成后用 CLI finish。固定规范见已加载 MAIN.md。",
        "inputSchema": {"type": "object", "properties": {
            "context_file": {"type": "string"}, "text": {"oneOf": [{"type": "string", "minLength": 1},
                {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1}]},
            "continue_work": {"type": "boolean", "default": False}, "reply_to": {"type": "string"},
            "mention": {"type": "string"}, "request_key": {"type": "string"},
            **{key: value for key, value in input_schema("reply")["properties"].items()
               if key in ("voice", "emotion", "spoken_text")}},
            "required": ["context_file", "text"], "additionalProperties": False}},
        {"name": "qqbot_cli", "description": "控制台 CLI。reply/commit 立即提交发送，action=finish 仅结束回合；接口与规范已载入 MAIN.md，参数不明再查 help。",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": [*COMMANDS, *GROUPS]}, "context_file": {"type": "string"},
            "operation": {"type": "string"}, "params": {"type": "object"}},
            "required": ["action", "context_file"], "additionalProperties": False}},
        {"name": "qqbot_read_memory", "description": "读取本轮清单内记忆：默认精简摘要，可query检索；原始依据用view=raw分页，next_offset续读。禁止读取清单外文件。",
        "inputSchema": {"type": "object", "properties": {"context_file": {"type": "string"}, "path": {"type": "string"},
            "view": {"type": "string", "enum": ["summary", "raw"], "default": "summary"},
            "query": {"type": "string", "maxLength": 200}, "offset": {"type": "integer", "minimum": 0},
            "max_chars": {"type": "integer", "minimum": 256, "maximum": 16000, "default": 4000}},
            "required": ["context_file", "path"], "additionalProperties": False}},
        {"name": "qqbot_console_help", "description": "按命令和操作查询控制台参数；已知用法直接调用，无需每轮查帮助。",
        "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}, "operation": {"type": "string"}}, "additionalProperties": False}},
        {"name": "qqbot_console_context", "description": "查询当前凭据允许的操作、绑定任务、输出目录和可直接执行的 CLI 命令。",
         "inputSchema": {"type": "object", "properties": {"context_file": {"type": "string"}}, "required": ["context_file"], "additionalProperties": False}},
        {"name": "qqbot_list_tasks", "description": "列出当前发起人在本账号和会话中的任务；工作/阅读控制凭据只查询绑定任务。",
         "inputSchema": {"type": "object", "properties": {"context_file": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
             "states": {"type": "array", "items": {"type": "string"}}}, "required": ["context_file"], "additionalProperties": False}},
        {"name": "qqbot_submit_task", "description": "提交独立后台任务，结果排队交给主代理通过 CLI deliver 交付；search 为搜索，generate_image 为生图，generate_video 默认网页版生成视频。",
        "inputSchema": {"type": "object", "properties": {
            "context_file": {"type": "string"}, "task_type": {"type": "string", "enum": list(TASK_TYPES)},
            "task": {"type": "string"}, "request_key": {"type": "string"},
            **{key: value for key, value in input_schema("submit")["properties"].items() if key in ("provider", "model")}},
            "required": ["context_file", "task_type", "task"], "additionalProperties": False}},
        *[{"name": "qqbot_" + action + "_task", "description": description,
           "inputSchema": {"type": "object", "properties": {"context_file": {"type": "string"},
               "request_id": {"type": "string"}}, "required": ["context_file", "request_id"], "additionalProperties": False}}
          for action, description in (("status", "查询本会话任务状态及结果文件路径"), ("cancel", "取消本会话指定后台任务"))],
        {"name": "qqbot_wait_for_message", "description": "对方还没说完或当前无需插话时调用；控制台据此结束本轮，下一条消息继续。无需强行输出空文本；本轮任何结束文字均不会发送。",
         "inputSchema": {"type": "object", "properties": {"context_file": {"type": "string"}},
             "required": ["context_file"], "additionalProperties": False}},
        {"name": "qqbot_inbox", "description": "列出当前 QQ 群或私聊收到的文件，返回 file_id、文件名和大小。",
         "inputSchema": {"type": "object", "properties": {"context_file": {"type": "string"}},
             "required": ["context_file"], "additionalProperties": False}},
        {"name": "qqbot_stickers", "description": "按情绪、动作或聊天主题标签搜索本地表情包。结合本群聊天语境选择，不猜测ID。",
         "inputSchema": {"type": "object", "properties": {"context_file": {"type": "string"}, "query": {"type": "string"}},
             "required": ["context_file"], "additionalProperties": False}},
        {"name": "qqbot_sandbox", "description": "主对话控制软件/游戏阅读。steer 实时调整既有任务的阅读目标或网址，activities 取得方向接收版本、真实页面和笔记。browse/use/play 创建或调整同一目标；默认不限时、不限次数。finish 完成、pause 受阻、stop 停止。",
         "inputSchema": {"type": "object", "properties": {"context_file": {"type": "string"},
             "operation": {"type": "string", "enum": list(SANDBOX)},
             "params": {"type": "object", "properties": {key: value for operation in SANDBOX
                 for key, value in input_schema("sandbox", operation)["properties"].items()}, "additionalProperties": False}},
             "required": ["context_file", "operation"], "additionalProperties": False}},
        *desktop_schemas(),
        {"name": "qqbot_receive_file", "description": "接收 inbox 中指定的 QQ 文件到本地，不调用模型。用 status 查询完成后的 local_path。",
         "inputSchema": {"type": "object", "properties": {"context_file": {"type": "string"}, "file_id": {"type": "string"}},
             "required": ["context_file", "file_id"], "additionalProperties": False}}]
    # Preserve stable discovery ordering for clients caching the generic tool.
    return [definitions[1], definitions[0], *definitions[2:]]


def tools_schema():
    role = os.environ.get("QQBOT_MCP_ROLE", "")
    definitions = _all_tools_schema()
    if not role:
        return definitions
    allowed = {"qqbot_cli", "qqbot_console_help"}
    if role == "main":
        allowed.update({"qqbot_reply", "qqbot_read_memory"})
    elif role != "worker":
        return []
    return [tool for tool in definitions if tool["name"] in allowed]


def handle(message):
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in message:
        return None
    base = {"jsonrpc": "2.0", "id": message["id"]}
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "qqbot-console", "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools_schema()}
    elif method == "tools/call":
        try:
            name, args = params["name"], params.get("arguments") or {}
            if os.environ.get("QQBOT_MCP_ROLE") and name not in {tool["name"] for tool in tools_schema()}:
                raise PermissionError("当前代理的 MCP 角色没有此工具；请使用本角色公开的 CLI 接口。")
            if name in ("qqbot_send_files", "qqbot_send_sticker", "qqbot_reply_to"):
                from console_tools.files import ConsoleFileError
                raise ConsoleFileError("CLI_REQUIRED", "发送、引用和 @ 已统一使用 CLI。",
                                       "使用 qqbot_cli 的 reply/deliver，完成后调用 finish；其他发送用 CLI send/sticker，引用使用 --reply-to 真实消息ID。")
            operations = {"qqbot_submit_task": "submit", "qqbot_status_task": "status", "qqbot_cancel_task": "cancel",
                          "qqbot_inbox": "inbox", "qqbot_receive_file": "receive",
                          "qqbot_wait_for_message": "wait", "qqbot_stickers": "stickers",
                          "qqbot_sandbox": "sandbox",
                          "qqbot_console_context": "context", "qqbot_list_tasks": "tasks"}
            if name == "qqbot_reply":
                if set(args) - {"context_file", "text", "continue_work", "reply_to", "mention", "request_key", "voice", "emotion", "spoken_text"}:
                    raise ValueError("快捷回复包含未知参数。")
                if type(args.get("continue_work", False)) is not bool:
                    raise ValueError("continue_work 必须是布尔值。")
                value = execute("reply", args["context_file"], text=args["text"], finish=not args.get("continue_work", False),
                    reply_to=args.get("reply_to", ""), mention=args.get("mention", ""), request_key=args.get("request_key", ""),
                    voice=args.get("voice", False), emotion=args.get("emotion", ""), spoken_text=args.get("spoken_text", ""))
            elif name == "qqbot_cli":
                if set(args) - {"action", "context_file", "operation", "params"}:
                    raise ValueError("控制台调用包含未知字段。")
                action, operation, values = args["action"], args.get("operation", ""), args.get("params", {})
                definition = command_spec(action, operation)
                if not isinstance(values, dict) or set(values) - set(definition["params"]):
                    raise ValueError("params 包含未知字段；使用 help 查询当前命令参数。")
                broker, token = ConsoleBroker.from_context(args["context_file"])
                context = broker.context(token)
                if context.get("main_agent") and not context.get("sandbox_session_id"):
                    if action == "desktop" or action == "sandbox" and operation in (
                            "read", "write", "list", "grant", "observe", "key", "click", "type", "scroll", "navigate", "finish"):
                        raise PermissionError("主代理负责聊天与调度，软件操作和文件工作由绑定的执行子代理完成。")
                value = (execute(action, args["context_file"], operation=operation, params=values) if action in GROUPS
                         else execute(action, args["context_file"], **values))
            elif name == "qqbot_read_memory":
                if set(args) - {"context_file", "path", "view", "query", "offset", "max_chars"} or not {"context_file", "path"} <= set(args):
                    raise ValueError("记忆读取需要 context_file、path 和可选分页/检索参数。")
                broker, token = ConsoleBroker.from_context(args["context_file"])
                value = execute("read-memory", args["context_file"], **{key: value for key, value in args.items() if key != "context_file"})
            elif name == "qqbot_console_help":
                if set(args) - {"command", "operation"}:
                    raise ValueError("帮助接口只接受 command 和 operation。")
                value = execute("help", help_action=args.get("command", ""), help_operation=args.get("operation", ""))
            elif name in DESKTOP_TOOLS:
                allowed = next(tool["inputSchema"] for tool in desktop_schemas() if tool["name"] == name)
                if set(args) - set(allowed["properties"]) or set(allowed["required"]) - set(args):
                    raise ValueError("桌面工具参数不完整或包含未知字段。")
                value = execute("desktop", args["context_file"], operation=DESKTOP_TOOLS[name],
                                params={key: value for key, value in args.items() if key != "context_file"})
            elif name in operations:
                value = execute(operations[name], **args)
            else:
                raise ValueError("未知工具。")
            summary = {key: item for key, item in value.items() if key != "image_path"} if name in DESKTOP_TOOLS else value
            if name in ("qqbot_cli", "qqbot_reply"):
                from console_tools.compact import compact_receipt
                summary = compact_receipt(args.get("action", "reply"), summary, args.get("operation", ""))
            result = {"content": [{"type": "text", "text": json.dumps(summary, ensure_ascii=False, separators=(",", ":"))}], "isError": value.get("ok") is False or bool(value.get("error"))}
            if value.get("image_path") and value.get("image_required", True) and (name in DESKTOP_TOOLS or name == "qqbot_sandbox" and args.get("operation") == "observe"
                    or name == "qqbot_cli" and args.get("action") in ("desktop", "sandbox")):
                import base64
                # The path came from the authenticated capability runtime, not
                # an arbitrary caller argument. Deliver the actual observation.
                result["content"].append({"type": "image", "mimeType": "image/png",
                    "data": base64.b64encode(Path(value["image_path"]).read_bytes()).decode("ascii")})
        except (OSError, ValueError, KeyError, TypeError) as error:
            result = {"content": [{"type": "text", "text": json.dumps(file_error(error), ensure_ascii=False)}], "isError": True}
    else:
        return {**base, "error": {"code": -32601, "message": "Method not found"}}
    return {**base, "result": result}


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            response = handle(json.loads(line))
        except (ValueError, TypeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
