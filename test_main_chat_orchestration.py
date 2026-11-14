"""Real offline main/branch/MCP handoffs with isolated transport fixtures."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.server import handle
from image_handler import analyze_image_item
from prompt_builder import compose_autonomous_decision_prompt, parse_ai_decision_and_reply


class MainChatOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import test_startup_participation
        self.fixture = test_startup_participation.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def drain(self, bot):
        runtime = bot._get_runtime()
        for _ in range(20):
            tasks = [task for task in runtime.chat_workers.values() if not task.done()]
            if runtime.work_worker and not runtime.work_worker.done():
                tasks.append(runtime.work_worker)
            coordinator = getattr(bot, "_main_coordinator", None)
            if coordinator:
                tasks.extend(task for task in coordinator.delivery_tasks.values() if not task.done())
            if not tasks:
                return
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.fail("console work did not drain")

    def agent(self, bot, *, messages=False):
        async def chat(key, prompt, **options):
            session = bot.ai.get_or_create_session(key)
            session.conversation_id = session.conversation_id or "cid-main-fixture"
            return '{"should_reply":true,"messages":[{"text":"第一句","action":"none"},{"text":"接着说第二句","action":"none"}]}' if messages else '{"should_reply":false}'
        bot.ai.chat.side_effect = chat

    async def test_first_rules_once_then_only_new_messages_same_cid(self):
        bot, key = self.fixture.bot(stored_cid="same-main-cid")
        self.agent(bot)
        await self.fixture.receive(bot, False, "one", "今天讨论第一个话题")
        await self.fixture.receive(bot, False, "two", "接着讨论第二个话题", elapsed=1)
        first, second = bot.ai.chat.await_args_list
        self.assertEqual(first.args[0], key)
        self.assertNotIn("@rules:", first.args[1])
        self.assertIn('"type": "continuity_resume"', first.args[1])
        self.assertIn('"type": "protocol_update"', first.args[1])
        self.assertNotIn("@rules:", second.args[1])
        self.assertNotIn("今天讨论第一个话题", second.args[1])
        self.assertNotIn("自主参聊决策规范", second.args[1])
        self.assertIn('"console_context_file":', second.args[1])
        self.assertNotIn('"reply_call":', second.args[1])
        self.assertNotIn('"reply_command":', second.args[1])
        # One bound command replaces a help/context discovery round trip.
        self.assertLess(len(second.args[1]), 1300)
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "same-main-cid")
        rule = Path(bot.ai.get_or_create_session(key).chat_state["rules_file"])
        self.assertTrue(rule.is_file())
        self.assertIn("console_tools", rule.read_text(encoding="utf-8"))

    async def test_continuous_messages_do_not_trigger_periodic_rules_on_tenth_turn(self):
        bot, _ = self.fixture.bot(stored_cid="same-main-cid")
        self.agent(bot)
        for index in range(12):
            await self.fixture.receive(bot, False, str(index), f"继续第{index}条", elapsed=index)
        self.assertEqual(sum('"type": "protocol_update"' in c.args[1] for c in bot.ai.chat.await_args_list), 1)
        self.assertFalse(any("@rules:" in c.args[1] for c in bot.ai.chat.await_args_list))

    async def test_multi_message_decision_sends_separate_bubbles_without_quotes_both_transports(self):
        for official in (False, True):
            bot, _ = self.fixture.bot(official, stored_cid="main-fixture")
            self.agent(bot, messages=True)
            event = await self.fixture.receive(bot, official, "multi", "聊聊刚才的事")
            sends = event.reply if official else bot.send_group_message
            self.assertEqual(sends.await_count, 2)
            if not official:
                self.assertTrue(all(not getattr(c.args[1], "quote", "") for c in sends.await_args_list))

    async def test_console_returns_completed_files_to_main_before_delivery(self):
        bot, key = self.fixture.bot(stored_cid="same-main-cid")
        bot._is_ws_connected = lambda: True
        sequence = []
        service = bot._get_media_service()
        actual_send = service.send_assets
        async def send_assets(ctx, job, assets, *, check_current=None):
            if check_current:
                check_current()
            sequence.append("files-sent")
            self.assertTrue(Path(assets[0].local_path).is_file())
            self.assertTrue(list(bot._get_main_coordinator().directory(key).glob("result-*.json")))
            await actual_send(ctx, job, assets, check_current=check_current)
        service.send_assets = AsyncMock(side_effect=send_assets)
        async def chat(session_key, prompt, **options):
            if session_key.startswith("__work__:task:"):
                sequence.append("task-branch")
                service.send_assets.assert_not_awaited()
                return '{"answer":"完整任务内容与真实成果","files":[],"sources":[]}'
            self.assertEqual(session_key, key)
            if '"type": "task_result"' in prompt:
                sequence.append("main-result")
                service.send_assets.assert_not_awaited()
                return "审核完成"
            return '{"should_reply":false,"tasks":[{"type":"task","task":"整理一份简短资料"}]}'
        bot.ai.chat.side_effect = chat
        await self.fixture.receive(bot, False, "task-request", "帮我整理一下资料")
        await self.drain(bot)
        self.assertEqual(sequence, ["task-branch", "main-result", "files-sent"])
        self.assertEqual(bot.ai.chat.await_count, 3)
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "same-main-cid")
        service.send_assets.assert_awaited_once()
        bot.send_group_message.assert_awaited_once()
        self.assertEqual(bot.send_group_message.await_args.args[1].body, "任务完成")

    async def test_slow_task_does_not_block_new_main_chat(self):
        bot, key = self.fixture.bot(stored_cid="main-cid")
        entered, release = asyncio.Event(), asyncio.Event()
        async def chat(session_key, prompt, **options):
            if session_key.startswith("__work__:task:"):
                entered.set()
                await release.wait()
                return '{"answer":"done","files":[]}'
            if '"type": "task_result"' in prompt:
                return '{"should_reply":false}'
            if "耗时资料" in prompt:
                return '{"should_reply":false,"tasks":[{"type":"task","task":"整理耗时资料"}]}'
            return '{"should_reply":true,"reply":"可以继续聊天"}'
        bot.ai.chat.side_effect = chat
        await self.fixture.receive(bot, False, "slow", "整理耗时资料")
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await self.fixture.receive(bot, False, "normal", "还在吗", elapsed=1)
            bot.send_group_message.assert_awaited_once()
            self.assertFalse(bot._runtime.work_worker.done())
        finally:
            release.set()
            await self.drain(bot)

    async def test_mcp_and_cli_share_receipt_dedup_and_scope_restrictions(self):
        bot, key = self.fixture.bot(stored_cid="main-cid")
        self.agent(bot)
        await self.fixture.receive(bot, False, "message", "继续聊")
        coordinator = bot._get_main_coordinator()
        ctx = next(iter(coordinator.requests.values()))
        context_file, _, _ = coordinator.issue_chat_context(ctx, key)
        args = {"context_file": context_file, "task_type": "task", "task": "整理数据"}
        response = handle({"id": 1, "method": "tools/call", "params": {"name": "qqbot_submit_task", "arguments": args}})
        self.assertFalse(response["result"]["isError"])
        receipt = json.loads(response["result"]["content"][0]["text"])
        repeated = execute("submit", **args)
        self.assertEqual(receipt["id"], repeated["id"])
        other_context = coordinator.broker.issue_context({"namespace": coordinator.namespace,
            "scope": "onebot:account_1:group:other"}, Path("other"))
        with self.assertRaises(ValueError):
            execute("status", other_context, request_id=receipt["id"])
        names = {tool["name"] for tool in handle({"id": 2, "method": "tools/list"})["result"]["tools"]}
        self.assertTrue({"qqbot_submit_task", "qqbot_status_task", "qqbot_cancel_task", "qqbot_inbox",
            "qqbot_receive_file", "qqbot_wait_for_message", "qqbot_stickers", "qqbot_sandbox"} <= names)
        self.assertFalse({"qqbot_send_files", "qqbot_send_sticker", "qqbot_reply_to"} & names)
        execute("cancel", context_file, request_id=receipt["id"])
        await coordinator.poll()
        self.assertEqual(execute("status", context_file, request_id=receipt["id"])["state"], "cancelled")

    async def test_explicit_no_reply_tasks_never_expose_json_even_for_master(self):
        response = '{"should_reply":false,"tasks":[{"type":"search","task":"查资料"}]}'
        decision = parse_ai_decision_and_reply(response, is_master=True, is_at_bot=True)
        self.assertFalse(decision.should_reply)
        self.assertEqual(len(decision.tasks), 1)

    async def test_sticker_result_persists_before_branch_archive_and_reaches_main(self):
        bot, key = self.fixture.bot(stored_cid="main-cid")
        bot._is_ws_connected = lambda: True
        picture = Path("data/cache/images/sticker.png").resolve()
        picture.parent.mkdir(parents=True)
        Image.new("RGB", (500, 500), "red").save(picture)
        bot._qq_capture = SimpleNamespace(capture_message=AsyncMock(return_value={"file": str(picture), "capture_mode": "qq_chat_screenshot"}))
        async def chat(session_key, prompt, **options):
            if ":view:" in session_key:
                self.assertIn("这是一张 QQ 表情包", prompt)
                self.assertNotIn("表格各行", prompt)
                viewed = prompt.split("指定图片绝对路径：", 1)[1].splitlines()[0]
                options["response_trace"].extend([
                    {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "view_file", "args": {"AbsolutePath": viewed}}]},
                    {"type": "TOOL_RESULT", "status": "DONE", "content": "Displayed image"}])
                return '{"media_type":"sticker","description":"猫猫委屈地抹眼泪","emotion":"委屈","gesture":"抹眼泪","meaning":"表达委屈或撒娇"}'
            self.assertEqual(session_key, key)
            self.assertIn("委屈", prompt)
            self.assertIn('"media_type": "sticker"', prompt)
            return '{"should_reply":true,"reply":"摸摸头"}'
        async def archive(session_key, path, **kwargs):
            facts = bot._get_image_context_store().get(bot._image_scope("12345678"), "sticker-message")
            self.assertEqual(facts[0].media_type, "sticker")
            self.assertEqual(facts[0].emotion, "委屈")
            return True
        bot.ai.chat.side_effect = chat
        bot.ai.archive_session = AsyncMock(side_effect=archive)
        bot._queue_event({"post_type": "message", "message_type": "group", "group_id": 12345678,
            "user_id": 456, "message_id": "sticker-message", "raw_message": "[CQ:image,file=sticker,subType=1]",
            "message": [{"type": "image", "data": {"file": "sticker", "subType": "1"}}], "sender": {"nickname": "群友"}})
        await self.drain(bot)
        bot.ai.archive_session.assert_awaited_once()
        bot.send_group_message.assert_awaited_once()
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "main-cid")

    async def test_small_ordinary_image_is_not_forced_to_be_sticker(self):
        picture = Path("images/photo.png").resolve()
        picture.parent.mkdir(parents=True)
        Image.new("RGB", (128, 128), "blue").save(picture)
        result = await analyze_image_item({"file": str(picture), "subType": "0"}, cache_dir=str(picture.parent))
        self.assertFalse(result["is_sticker"])

    async def test_idle_recall_uses_same_cid_and_new_input_cancels_it(self):
        bot, key = self.fixture.bot(stored_cid="main-cid")
        self.agent(bot)
        await self.fixture.receive(bot, False, "initial", "先聊到这里")
        entered = asyncio.Event()
        async def slow(key, prompt, **options):
            if "空闲整理事件" in prompt:
                entered.set()
                await asyncio.Event().wait()
            return '{"should_reply":false}'
        bot.ai.chat.side_effect = slow
        session = bot.ai.get_or_create_session(key)
        session.chat_state["last_event"] -= 3600
        coordinator = bot._get_main_coordinator()
        await coordinator.recall_idle()
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(self.fixture.receive(bot, False, "new-input", "继续刚才的话题", elapsed=1), 1)
        self.assertTrue(coordinator.recall_tasks[key].cancelled())
        self.assertEqual(session.conversation_id, "main-cid")
        bot.send_group_message.assert_not_awaited()

    async def test_startup_prepares_rules_locally_without_model_tokens(self):
        for official in (False, True):
            bot, key = self.fixture.bot(official, stored_cid="preserved-cid")
            self.agent(bot)
            coordinator = bot._get_main_coordinator()
            coordinator.prepare_startup()
            await asyncio.gather(*coordinator.startup_tasks.values())
            bot.ai.chat.assert_not_awaited()
            await self.fixture.receive(bot, official, "new", "接着刚才的话题")
            self.assertEqual(bot.ai.chat.await_count, 1)
            self.assertIn('"type": "protocol_update"', bot.ai.chat.await_args.args[1])
            self.assertNotIn("@rules:", bot.ai.chat.await_args.args[1])
            await self.fixture.receive(bot, official, "second", "再说一句", elapsed=1)
            self.assertNotIn("@rules:", bot.ai.chat.await_args.args[1])
            self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "preserved-cid")
            if not official:
                bot.send_group_message.assert_not_awaited()

    async def test_first_message_does_not_wait_for_a_model_startup_round(self):
        bot, key = self.fixture.bot(stored_cid="preserved-cid")
        bot.ai.chat.return_value = '{"should_reply":false}'
        coordinator = bot._get_main_coordinator()
        coordinator.prepare_startup()
        await asyncio.wait_for(self.fixture.receive(bot, False, "new", "还在吗"), 1)
        bot.ai.chat.assert_awaited_once()
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "preserved-cid")

    async def test_two_tasks_from_one_message_both_return_without_reexecution(self):
        bot, key = self.fixture.bot(stored_cid="main-cid")
        bot._is_ws_connected = lambda: True
        async def chat(key, prompt, **options):
            if key.startswith("__work__:task:"):
                return '{"answer":"完整资料","files":[]}'
            if '"type": "task_result"' in prompt:
                return '{"should_reply":true,"reply":"文件已整理"}'
            return '{"should_reply":false,"tasks":[{"type":"task","task":"整理资料甲"},{"type":"task","task":"整理资料乙"}]}'
        bot.ai.chat.side_effect = chat
        await self.fixture.receive(bot, False, "two-tasks", "整理两份资料")
        await self.drain(bot)
        calls = [c for c in bot.ai.chat.await_args_list if c.args[0].startswith("__work__:task:")]
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0].args[0], calls[1].args[0])
        self.assertEqual(bot.send_group_message.await_count, 2)
        await bot._get_main_coordinator().poll()
        await self.drain(bot)
        self.assertEqual(bot.send_group_message.await_count, 2)

    async def test_console_retries_disconnected_delivery_without_repeating_work(self):
        bot, key = self.fixture.bot(stored_cid="main-cid")
        async def chat(key, prompt, **options):
            if key.startswith("__work__:task:"):
                return '{"answer":"已完成资料","files":[]}'
            if '"type": "task_result"' in prompt:
                return "审核完成"
            return '{"should_reply":false,"tasks":[{"type":"task","task":"整理资料"}]}'
        bot.ai.chat.side_effect = chat
        await self.fixture.receive(bot, False, "task", "整理资料")
        await self.drain(bot)
        coordinator = bot._get_main_coordinator()
        ready = coordinator.broker.completed(coordinator.namespace, bot._image_scope("12345678"))
        self.assertEqual(len(ready), 1)
        coordinator.retry_after.clear()
        bot._is_ws_connected = lambda: True
        await coordinator.poll()
        await self.drain(bot)
        self.assertEqual(len([c for c in bot.ai.chat.await_args_list if c.args[0].startswith("__work__:task:")]), 1)
        self.assertEqual(coordinator.broker.active(coordinator.namespace), [])

    async def test_persisted_result_waits_for_fresh_official_reply_window(self):
        bot, key = self.fixture.bot(True, stored_cid="main-cid")
        self.agent(bot)
        await self.fixture.receive(bot, True, "old", "先聊聊")
        coordinator = bot._get_main_coordinator()
        context_file = coordinator.issue_chat_context(coordinator.contexts[key], key)[0]
        broker, token = ConsoleBroker.from_context(context_file)
        receipt = broker.submit(token, "task", "已有成果")
        broker.update(receipt["id"], "ready", {"type":"task_result", "success":True, "result":{"answer":"已确认成果"}})
        coordinator.contexts[key].received_at -= 400
        bot.ai.chat.reset_mock()
        await coordinator.poll()
        bot.ai.chat.assert_not_awaited()
        bot.ai.chat.return_value = '{"should_reply":false}'
        await self.fixture.receive(bot, True, "fresh", "继续")
        await self.drain(bot)
        self.assertEqual(bot.ai.chat.await_count, 2)
        self.assertIn('"type": "task_result"', bot.ai.chat.await_args.args[1])
        self.assertEqual(broker.record(receipt["id"])["state"], "delivered")

    async def test_idle_archive_uses_fixed_file_and_durable_cursor_and_keeps_main_cid(self):
        bot, key = self.fixture.bot(stored_cid="main-cid")
        self.agent(bot)
        await self.fixture.receive(bot, False, "initial", "聊到这里")
        coordinator = bot._get_main_coordinator()
        original = bot.ai.chat.side_effect
        async def summarize(session_key, prompt, **options):
            if options.get("single_response"):
                return '{"summary":"讨论了当前话题","conclusions":["稍后继续"]}'
            if '"type": "message"' in prompt and "继续聊" in prompt:
                context = next(json.loads(line)["console_context_file"] for line in prompt.splitlines()
                    if line.startswith('{"console_context_file":'))
                state = bot.ai.get_or_create_session(key).chat_state
                self.assertIn("讨论了当前话题", execute("read-memory", context, path=state["idle_archive"])["text"])
            return await original(session_key, prompt, **options)
        bot.ai.chat.side_effect = summarize
        await coordinator._recall(key, bot.ai.get_or_create_session(key).generation)
        state = bot.ai.get_or_create_session(key).chat_state
        archive = json.loads(Path(state["idle_archive"]).read_text(encoding="utf-8"))
        self.assertIn("聊到这里", Path(archive["journal"]).read_text(encoding="utf-8"))
        self.assertEqual(Path(state["idle_archive"]), coordinator.directory(key) / "memory.json")
        calls = bot.ai.chat.await_count
        for _ in range(5):
            await coordinator._recall(key, bot.ai.get_or_create_session(key).generation)
        self.assertEqual(bot.ai.chat.await_count, calls)
        self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "main-cid")
        await self.fixture.receive(bot, False, "next", "继续聊", elapsed=1)
        journal = (coordinator.directory(key)/"events.jsonl").read_bytes()
        self.assertIn("聊到这里", journal.decode("utf-8"))
        self.assertNotIn("聊到这里", journal[archive["cursor_bytes"]:].decode("utf-8"))
        context_file = list(coordinator.requests)[-1]
        manifest = bot.ai.chat.await_args.kwargs["memory_manifest"]
        self.assertIn(str(Path(state["idle_archive"]).as_posix()), [row["path"] for row in manifest["files"]])

    async def test_reset_generation_remains_invalid_after_agent_restart(self):
        from antigravity_client import AntigravityClient
        bot, key = self.fixture.bot(stored_cid="old-main")
        before = bot.ai.get_or_create_session(key).generation
        bot.ai.reset_session(key)
        restarted = AntigravityClient(sessions_file=bot.ai.sessions_file)
        self.assertGreater(restarted.get_or_create_session(key).generation, before)
        self.assertIsNone(restarted.get_or_create_session(key).conversation_id)

    async def test_cancel_ready_result_also_cancels_disconnected_text_outbox(self):
        from onebot_client import OneBotClient
        bot, key = self.fixture.bot(stored_cid="main-cid")
        self.agent(bot)
        await self.fixture.receive(bot, False, "initial", "先聊聊")
        coordinator = bot._get_main_coordinator()
        context_file = coordinator.issue_chat_context(coordinator.contexts[key], key)[0]
        broker, token = ConsoleBroker.from_context(context_file)
        request = broker.submit(token, "task", "已有成果")
        broker.update(request["id"], "ready", {"type":"task_result", "success":True, "result":{"answer":"资料完整"}})
        self.agent(bot, messages=True)
        bot.send_group_message = OneBotClient.send_group_message.__get__(bot)
        bot._is_ws_connected = lambda: False
        await coordinator.poll()
        await self.drain(bot)
        payload = json.loads(broker.record(request["id"])["result"])
        outbox = payload["delivery"]["outbox_id"]
        execute("cancel", context_file, request_id=request["id"])
        await coordinator.poll()
        self.assertEqual(bot._runtime.store.db.execute("SELECT state FROM outbox WHERE id=?", (outbox,)).fetchone()[0], "cancelled")
        bot.send_action.assert_not_awaited()

    async def test_restart_reports_interrupted_work_without_automatically_reexecuting(self):
        from main_chat import MainChat
        bot, key = self.fixture.bot(stored_cid="main-cid")
        self.agent(bot)
        await self.fixture.receive(bot, False, "initial", "先聊聊")
        previous = bot._get_main_coordinator()
        context_file = previous.issue_chat_context(previous.contexts[key], key)[0]
        broker, token = ConsoleBroker.from_context(context_file)
        request = broker.submit(token, "task", "未完成的任务")
        broker.update(request["id"], "running", {"runtime_job_id":"interrupted-fixture"})
        await previous.close()
        bot._main_coordinator = MainChat(bot)
        bot.ai.chat.reset_mock()
        await bot._main_coordinator.poll()
        await self.drain(bot)
        bot.ai.chat.assert_awaited_once()
        self.assertFalse(any(c.args[0].startswith("__work__:") for c in bot.ai.chat.await_args_list))
        self.assertIn("重启中断", bot.ai.chat.await_args.args[1])
        self.assertEqual(bot.send_group_message.await_args.args[1].body, "任务未完成")
        self.assertEqual(broker.record(request["id"])["state"], "delivered")


if __name__ == "__main__":
    unittest.main()
