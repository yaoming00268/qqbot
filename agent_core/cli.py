"""Standalone actor service and local administration, independent of QQ."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import uuid

from agent_core.runtime import ActorRuntime
from agent_core.store import digest


def database_path(config):
    return Path(config.agent.database_path or Path(config.bot.runtime_database_path).resolve().parent / "actors.sqlite3").resolve()


async def serve(config, *, ai=None, config_file=""):
    from vm_protocol import file_lock
    if ai is None and config.agent.autonomous_model_enabled:
        from model_backend import create_ai_client
        ai = create_ai_client(config, system_prompt="",
            sessions_file=str(database_path(config).parent / "actor_model_sessions.json"))
    with file_lock(database_path(config).with_suffix(".service.lock"), timeout=1):
        runtime = ActorRuntime(config.agent, database_path=database_path(config), ai=ai)
        print(json.dumps({"state": "running", "mode": "agent", "database": str(runtime.store.path),
                          "actors": list(config.agent.actors)}, ensure_ascii=False), flush=True)
        stamp = Path(config_file).stat().st_mtime_ns if config_file else None
        try:
            while True:
                if config_file and Path(config_file).stat().st_mtime_ns != stamp:
                    from config import AppConfig
                    try:
                        changed = AppConfig.load(config_file)
                        if database_path(changed) != runtime.store.path:
                            raise ValueError("运行中修改角色数据库路径需要重启。")
                        runtime.configure(changed.agent)
                        stamp = Path(config_file).stat().st_mtime_ns
                    except (ValueError, OSError):
                        import logging
                        logging.getLogger("ActorRuntime").exception("新配置无效，保留当前配置")
                        stamp = Path(config_file).stat().st_mtime_ns
                await runtime.tick()
                await asyncio.sleep(1)
        finally:
            await runtime.close()
            if ai is not None and hasattr(ai, "aclose"):
                await ai.aclose()


def main(argv=None):
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    from config import AppConfig
    parser = argparse.ArgumentParser(description="持续角色与通用网站账号；平台由配置决定。")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="运行订阅调度与角色自主活动，无需 QQ")
    sub.add_parser("status", help="列出角色与账号")
    for name in ("read", "draft", "publish", "comment", "login"):
        item = sub.add_parser(name)
        item.add_argument("--account", required=True, help="配置键或 web:配置键")
        if name == "read":
            item.add_argument("--url", required=True)
        if name == "draft":
            group = item.add_mutually_exclusive_group(required=True)
            group.add_argument("--text")
            group.add_argument("--text-file", type=Path)
            item.add_argument("--url", default="")
            item.add_argument("--kind", choices=("publish_post", "reply_comment"), default="publish_post")
        if name in ("publish", "comment"):
            item.add_argument("--draft", required=True)
        if name != "login":
            item.add_argument("--request-key", default="", help="重试同一请求时沿用，不能更换键重发未知结果")
    for name in ("events", "memories", "action", "link-person"):
        item = sub.add_parser(name)
        item.add_argument("--actor", required=True)
        if name == "action":
            item.add_argument("--id", required=True)
        if name == "memories":
            item.add_argument("--query", default="")
        if name == "link-person":
            item.add_argument("--platform", required=True)
            item.add_argument("--remote-id", required=True)
            item.add_argument("--person-id", default="")
            item.add_argument("--name", default="")
    args = parser.parse_args(argv)
    config = AppConfig.load(args.config)
    if args.command == "run":
        try:
            asyncio.run(serve(config, config_file=args.config))
        except KeyboardInterrupt:
            pass
        return 0
    runtime = ActorRuntime(config.agent, database_path=database_path(config))
    store = runtime.store
    store.recover_expired_actions()
    try:
        if args.command == "status":
            value = {actor: {"name": store.actor(actor)["name"], "accounts": store.accounts(actor)}
                     for actor in config.agent.actors}
        elif args.command == "events":
            value = store.events(args.actor)
        elif args.command == "memories":
            value = store.recall(args.actor, query=args.query, shared_only=True)
        elif args.command == "action":
            value = store.action(args.id, args.actor)
        elif args.command == "link-person":
            value = {"person_id": store.link_person(args.actor, args.platform, args.remote_id, args.name, args.person_id)}
        else:
            account_id = args.account if args.account.startswith("web:") else "web:" + args.account
            account = store.account(account_id)
            actor_id = account["actor_id"]
            if args.command == "login":
                from agent_core.configuration import WebAccountConfig
                from agent_core.web import managed_browser
                cfg = WebAccountConfig.model_validate(account["config"]).model_copy(update={"headless": False})
                if not account["enabled"] or not store.actor(actor_id)["enabled"]:
                    raise ValueError("请先启用该角色和账号。")
                profile = cfg.profile or str(store.path.parent / "actor_browser_profiles" / digest(account_id)[:24])
                with managed_browser(cfg, profile, config.agent.action_timeout_ms) as browser:
                    page = browser.pages[0] if browser.pages else browser.new_page()
                    page.goto(cfg.home_url)
                    input("请在专用浏览器完成登录，完成后按 Enter 保存登录状态并关闭：")
                value = {"account_id": account_id, "state": "profile_saved"}
            else:
                kind = {"read": "read_content", "draft": "create_draft", "publish": "publish_post", "comment": "reply_comment"}[args.command]
                if args.command == "read":
                    payload = {"url": args.url}
                elif args.command == "draft":
                    payload = {"text": args.text_file.read_text(encoding="utf-8-sig") if args.text_file else args.text,
                               "target_url": args.url, "draft_kind": args.kind}
                else:
                    payload = {"draft_id": args.draft}
                # Draft identity makes publishing idempotent across CLI invocations too.
                key = args.request_key or (digest([kind, payload]) if args.command in ("publish", "comment") else uuid.uuid4().hex)
                value = runtime.gateway.execute(actor_id, account_id, kind, payload, key)
        print(json.dumps({"ok": True, "result": value}, ensure_ascii=False, indent=2))
        return 0 if not isinstance(value, dict) or value.get("state") not in ("failed", "unknown") else 1
    except (ValueError, PermissionError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error), "code": getattr(error, "code", type(error).__name__)}, ensure_ascii=False))
        return 1
