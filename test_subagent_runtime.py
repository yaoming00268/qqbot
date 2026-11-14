"""Real offline parent/console/worker handoffs, without native services or QQ egress."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from companion_life import state_for
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from main_agent_policy import main_cascade_config, subagent_cascade_config
from test_ai_fixture import prompt_object


def native_read_trace(options):
    options["response_trace"].extend([
        {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "read_url_content", "id": "read-1"}]},
        {"type": "TOOL_RESULT", "tool_call_id": "read-1", "status": "DONE"}])


class SubagentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture = StartupParticipationTests()
        self.fixture.setUp()
        self.patches = []

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()
        for item in self.patches:
            item.stop()

    async def prepare(self, official=False):
        bot, key = self.fixture.bot(official, stored_cid="persistent-main")
        bot.app_config.bot.master_id = "456"
        bot.app_config.bot.self_activity_enabled = True
        bot.app_config.bot.proactive_chat_enabled = False
        coordinator = bot._get_main_coordinator()
        prevent_watch = patch.object(coordinator, "start")
        prevent_watch.start()
        self.patches.append(prevent_watch)
        from test_ai_fixture import ScriptedConsoleAgentMock
        bot.ai.chat = ScriptedConsoleAgentMock(return_value="")
        message = await self.fixture.receive(bot, official, "setup", "先聊几句")
        bot.ai.chat.reset_mock()
        return bot, key, coordinator, message

    def context(self, coordinator, key):
        return coordinator.issue_chat_context(coordinator.contexts[key], key)[0]

    async def test_plain_chat_without_agent_mail_does_not_initialize_work_runtime_or_outbox(self):
        bot, _, coordinator, _ = await self.prepare()
        self.assertIsNone(bot._runtime)
        for _ in range(3):
            await coordinator.poll()
        self.assertIsNone(bot._runtime)
        self.assertIsNone(bot._text_outbox)

    async def test_chat_tasks_and_cancel_stop_running_mail_and_keep_other_members_queue(self):
        from console_tools.chat_commands import execute_control
        bot, key, coordinator, _ = await self.prepare()
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def chat(session_key, prompt, **options):
            if not session_key.startswith("__work__:agent:"):
                return ""
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        bot.ai.chat.side_effect = chat
        context = self.context(coordinator, key)
        broker, token = ConsoleBroker.from_context(context)
        first = execute("agents", context, operation="create", params={"name": "可取消", "text": "等待取消"})
        guest_context = broker.issue_context({**broker.context(token), "sender_id": "guest"}, Path(context).parent)
        guest = execute("agents", guest_context, operation="send", params={"agent_id": first["id"], "text": "其他成员尚未开始的任务"})
        await coordinator.poll()
        await asyncio.wait_for(entered.wait(), 2)
        listing = execute("command", context, text="/tasks")
        shown = json.loads(execute_control(coordinator, broker.record(listing["id"]), coordinator.contexts[key], broker.context(token)))
        self.assertIn(first["task_id"], [row["id"] for row in shown])
        self.assertNotIn(guest["task_id"], [row["id"] for row in shown])
        stop = execute("command", context, text="/cancel " + first["task_id"])
        result = execute_control(coordinator, broker.record(stop["id"]), coordinator.contexts[key], broker.context(token))
        self.assertIn(first["task_id"], result)
        self.assertEqual(execute("status", context, request_id=first["task_id"])["state"], "cancel_requested")
        with patch.object(coordinator.agents, "schedule_reviews"):
            await coordinator.agents.poll()
            await asyncio.wait_for(cancelled.wait(), 2)
            await asyncio.gather(coordinator.agents.tasks[first["id"]], return_exceptions=True)
        self.assertEqual(execute("status", context, request_id=first["task_id"])["state"], "cancelled")
        self.assertEqual(execute("status", guest_context, request_id=guest["task_id"])["state"], "queued")
        mail = execute("agents", context, operation="read", params={"agent_id": first["id"]})["messages"]
        self.assertEqual([row["state"] for row in mail if row["direction"] == "to_agent"], ["cancelled", "queued"])

    async def settle(self, coordinator):
        for _ in range(20):
            await coordinator.poll()
            await asyncio.sleep(0)
            tasks = [task for task in [*coordinator.agents.tasks.values(), *coordinator.agents.review_tasks.values()] if not task.done()]
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), 3)
            await asyncio.sleep(0)
            if not any(agent["state"] in ("queued", "running") for agent in coordinator.agents.mailbox.all(coordinator.namespace)):
                coordinator.agents.schedule_reviews()
                reviews = [task for task in coordinator.agents.review_tasks.values() if not task.done()]
                if reviews:
                    await asyncio.wait_for(asyncio.gather(*reviews), 3)
                if not any(agent["state"] in ("queued", "running") for agent in coordinator.agents.mailbox.all(coordinator.namespace)):
                    return
        self.fail("agent mailbox did not settle")

    async def test_a_result_can_reach_b_and_return_to_main_without_a_new_human_message_both_transports(self):
        for official in (False, True):
            bot, key, coordinator, message = await self.prepare(official)
            visits, agents = [], {}
            bot.ai.get_or_create_session(key).chat_state["buffer_seen"] = ["already-seen"]
            async def chat(session_key, prompt, **options):
                context = prompt_object(prompt, "console_context_file")["console_context_file"]
                if session_key.startswith("__work__:agent:"):
                    data = prompt_object(prompt, "agent_input")["agent_input"]
                    visits.append(data["text"])
                    execute("agents", context, operation="report", params={"text": "A 的实际分析" if data["text"] == "分析 A" else "B 的综合结论", "finish": True})
                    return ""
                self.assertEqual(session_key, key)
                reports = prompt_object(prompt, "agent_messages")["agent_messages"]
                broker, token = ConsoleBroker.from_context(context)
                self.assertTrue(broker.context(token)["agent_event"])
                self.assertNotIn('"reply_call":', prompt)
                with self.assertRaises(PermissionError):
                    execute("reply", context, text="内部消息不得外发")
                if reports[0]["name"] == "A":
                    agents["B"] = execute("agents", context, operation="create", params={"name": "B", "text": reports[0]["text"]})["id"]
                else:
                    execute("companion", context, operation="remember", params={"text": "从公开交流中更能体会语气与情境的关系"})
                execute("wait", context)
                return "最终文字不会发到QQ"
            bot.ai.chat.side_effect = chat
            ctx = self.context(coordinator, key)
            agents["A"] = execute("agents", ctx, operation="create", params={"name": "A", "text": "分析 A"})["id"]
            await self.settle(coordinator)
            self.assertEqual(visits, ["分析 A", "A 的实际分析"])
            self.assertFalse(coordinator.agents.pending(key))
            self.assertEqual(bot.ai.get_or_create_session(key).chat_state["buffer_seen"], ["already-seen"])
            self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "persistent-main")
            self.assertIn("语气", state_for(coordinator.broker, coordinator.namespace, key)["reflection"])
            self.fixture.assert_not_sent(bot, official, message)

    async def test_children_are_parallel_and_followups_use_the_same_key_and_workspace_in_order(self):
        bot, key, coordinator, _ = await self.prepare()
        entered, release, fast = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen = []
        async def chat(session_key, prompt, **options):
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            if not session_key.startswith("__work__:agent:"):
                execute("wait", context)
                return ""
            data = prompt_object(prompt, "agent_input")["agent_input"]
            workspace = prompt_object(prompt, "output_dir")["output_dir"]
            session = bot.ai.get_or_create_session(session_key)
            session.conversation_id = session.conversation_id or "cid-" + session_key
            seen.append((session_key, data["text"], workspace, session.conversation_id))
            if data["text"] == "慢任务":
                entered.set()
                await release.wait()
            elif data["text"] == "快任务":
                fast.set()
            return "真实分析结果"
        bot.ai.chat.side_effect = chat
        ctx = self.context(coordinator, key)
        a = execute("agents", ctx, operation="create", params={"name": "慢", "text": "慢任务"})
        execute("agents", ctx, operation="send", params={"agent_id": a["id"], "text": "后续一"})
        execute("agents", ctx, operation="create", params={"name": "快", "text": "快任务"})
        await coordinator.poll()
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(fast.wait(), 1)
        self.assertNotIn("后续一", [item[1] for item in seen])
        release.set()
        await self.settle(coordinator)
        execute("agents", self.context(coordinator, key), operation="send", params={"agent_id": a["id"], "text": "后续二"})
        await self.settle(coordinator)
        same = [item for item in seen if item[0] == "__work__:agent:" + a["id"]]
        self.assertEqual([item[1] for item in same], ["慢任务", "后续一", "后续二"])
        self.assertEqual(len({item[2] for item in same}), 1)
        self.assertEqual(len({item[3] for item in same}), 1)
        owner = bot._get_runtime().store.db.execute("SELECT owner FROM work WHERE label='子代理 慢' LIMIT 1").fetchone()[0]
        self.assertEqual(owner, coordinator.contexts[key].owner)
        parent_token = ConsoleBroker.from_context(ctx)[1]
        for record in Path(same[0][2]).glob("run-*.json"):
            self.assertNotIn(parent_token, record.read_text(encoding="utf-8"))
        bot.ai.archive_session = AsyncMock(return_value=True)
        execute("agents", self.context(coordinator, key), operation="close", params={"agent_id": a["id"]})
        await coordinator.poll()
        bot.ai.archive_session.assert_awaited_once()
        self.assertTrue(Path(bot.ai.archive_session.await_args.args[1]).is_file())
        self.assertNotIn(parent_token, Path(bot.ai.archive_session.await_args.args[1]).read_text(encoding="utf-8"))
        self.assertEqual(coordinator.agents.mailbox.agent(a["id"])["state"], "closed")

    async def test_explicit_self_invitation_dispatches_before_reply_and_work_survives_commit(self):
        bot, key, coordinator, _ = await self.prepare()
        seen = []
        async def chat(session_key, prompt, **options):
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            if session_key.startswith("__work__:agent:"):
                seen.append("work")
                self.assertTrue(options["bind_response"])
                native_read_trace(options)
                return "实际阅读 https://example.com/posts 后发现，熟人之间常用共同话题省略背景。"
            if '"agent_messages":' in prompt:
                execute("companion", context, operation="remember", params={"text": "熟人聊天会依赖共同语境，不必句句说明。"})
                execute("wait", context)
                return ""
            receipt = prompt_object(prompt, "self_action_request")["self_action_request"]
            self.assertIn("id", receipt)
            self.assertTrue(coordinator.agents.mailbox.agent(receipt["id"])["autonomous"])
            execute("speech", context, operation="ready")
            execute("reply", context, text="好，我去看看感兴趣的东西。")
            self.assertFalse(options["stop_requested"]())
            seen.append("reply")
            return ""
        bot.ai.chat.side_effect = chat
        await self.fixture.receive(bot, False, "go-play", "你自己玩去吧，学学人类的说话方式和行为", elapsed=1)
        await self.settle(coordinator)
        self.assertIn("work", seen)
        self.assertIn("reply", seen)
        self.assertEqual(bot.send_group_message.await_count, 1)
        self.assertEqual(state_for(coordinator.broker, coordinator.namespace, key)["reflection"], "熟人聊天会依赖共同语境，不必句句说明。")

    async def test_claimed_search_without_real_tool_evidence_returns_honest_failure(self):
        bot, key, coordinator, _ = await self.prepare()
        async def chat(session_key, prompt, **options):
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            if session_key.startswith("__work__:agent:"):
                execute("agents", context, operation="report", params={"text": "已经浏览很多网站了", "finish": True})
            else:
                reports = prompt_object(prompt, "agent_messages")["agent_messages"]
                self.assertEqual(reports[0]["code"], "NO_WORK_EVIDENCE")
                self.assertFalse(reports[0]["success"])
                self.assertNotIn("已经浏览", reports[0]["text"])
                execute("wait", context)
            return ""
        bot.ai.chat.side_effect = chat
        result = execute("agents", self.context(coordinator, key), operation="create",
            params={"name": "检索", "task_type": "search", "text": "实际阅读"})
        await self.settle(coordinator)
        mail = execute("agents", self.context(coordinator, key), operation="read", params={"agent_id": result["id"]})["messages"]
        self.assertEqual(mail[0]["state"], "failed")
        self.assertTrue(any(row.get("code") == "NO_WORK_EVIDENCE" for row in mail))
        runtime_state = bot._get_runtime().store.db.execute("SELECT state FROM work WHERE label='子代理 检索'").fetchone()[0]
        self.assertEqual(runtime_state, "failed")

    async def test_result_interruption_keeps_mail_for_next_real_chat(self):
        bot, key, coordinator, _ = await self.prepare()
        entered = asyncio.Event()
        async def chat(session_key, prompt, **options):
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            if session_key.startswith("__work__:agent:"):
                return "可保留的结果"
            broker, token = ConsoleBroker.from_context(context)
            if broker.context(token).get("agent_event"):
                entered.set()
                await asyncio.Future()
            self.assertIn("可保留的结果", prompt)
            execute("wait", context)
            return ""
        bot.ai.chat.side_effect = chat
        execute("agents", self.context(coordinator, key), operation="create", params={"name": "资料", "text": "任务"})
        await coordinator.poll()
        await asyncio.gather(*coordinator.agents.tasks.values())
        coordinator.agents.schedule_reviews()
        await asyncio.wait_for(entered.wait(), 1)
        self.assertTrue(coordinator.agents.pending(key))
        await self.fixture.receive(bot, False, "interrupt-review", "先回来聊聊", elapsed=1)
        self.assertFalse(coordinator.agents.pending(key))

    async def test_close_cancels_busy_child_and_queued_followups_without_replay(self):
        bot, key, coordinator, _ = await self.prepare()
        entered = asyncio.Event()
        calls = []
        async def chat(session_key, prompt, **options):
            if session_key.startswith("__work__:agent:"):
                calls.append(prompt_object(prompt, "agent_input")["agent_input"]["text"])
                entered.set()
                await asyncio.Future()
            execute("wait", prompt_object(prompt, "console_context_file")["console_context_file"])
            return ""
        bot.ai.chat.side_effect = chat
        bot.ai.archive_session = AsyncMock(return_value=True)
        context = self.context(coordinator, key)
        agent = execute("agents", context, operation="create", params={"name": "忙", "text": "执行中"})
        execute("agents", context, operation="send", params={"agent_id": agent["id"], "text": "还未执行"})
        await coordinator.poll()
        await asyncio.wait_for(entered.wait(), 1)
        execute("agents", context, operation="close", params={"agent_id": agent["id"]})
        await coordinator.poll()
        await asyncio.gather(*coordinator.agents.tasks.values(), return_exceptions=True)
        await coordinator.poll()
        await self.settle(coordinator)
        self.assertEqual(calls, ["执行中"])
        self.assertEqual(coordinator.agents.mailbox.agent(agent["id"])["state"], "closed")
        mail = execute("agents", context, operation="read", params={"agent_id": agent["id"]})["messages"]
        self.assertEqual([row["state"] for row in mail if row["direction"] == "to_agent"], ["interrupted", "cancelled"])

    async def test_permission_changes_and_disabled_autonomy_are_checked_again_before_execution(self):
        bot, key, coordinator, _ = await self.prepare()
        context = self.context(coordinator, key)
        execute("agents", context, operation="create", params={"name": "权限变更", "text": "任务"})
        bot.app_config.bot.work_permissions.access = "disabled"
        from test_ai_fixture import ScriptedConsoleAgentMock
        bot.ai.chat = ScriptedConsoleAgentMock(return_value="")
        await coordinator.poll()
        await asyncio.gather(*coordinator.agents.tasks.values())
        self.assertFalse(any(call.args[0].startswith("__work__:agent:") for call in bot.ai.chat.await_args_list))
        bot.app_config.bot.work_permissions.access = "all"
        broker, token = ConsoleBroker.from_context(self.context(coordinator, key))
        idle = broker.issue_context({**broker.context(token), "self_activity": True, "idle_event": True}, coordinator.directory(key) / "contexts")
        execute("agents", idle, operation="create", params={"name": "自主", "task_type": "search", "text": "公开阅读"})
        bot.app_config.bot.self_activity_enabled = False
        await coordinator.poll()
        await asyncio.gather(*coordinator.agents.tasks.values())
        self.assertFalse(any(call.args[0].startswith("__work__:agent:") for call in bot.ai.chat.await_args_list))

    async def test_restart_keeps_idle_identity_and_delivers_mail_without_fresh_transport_context(self):
        from subagent_runtime import SubagentRuntime
        bot, key, coordinator, _ = await self.prepare()
        seen = []
        async def chat(session_key, prompt, **options):
            context = prompt_object(prompt, "console_context_file")["console_context_file"]
            if session_key.startswith("__work__:agent:"):
                seen.append((session_key, prompt_object(prompt, "agent_input")["agent_input"]["text"]))
                session = bot.ai.get_or_create_session(session_key)
                session.conversation_id = session.conversation_id or "retained-child-cid"
                return "已保存的结果"
            execute("wait", context)
            return ""
        bot.ai.chat.side_effect = chat
        context = self.context(coordinator, key)
        agent = execute("agents", context, operation="create", params={"name": "持久", "text": "第一条"})
        await self.settle(coordinator)
        await coordinator.agents.close()
        coordinator.agents = SubagentRuntime(coordinator)
        execute("agents", self.context(coordinator, key), operation="send", params={"agent_id": agent["id"], "text": "重启后"})
        coordinator.contexts.clear()
        await self.settle(coordinator)
        self.assertEqual([item[1] for item in seen], ["第一条", "重启后"])
        self.assertEqual(len({item[0] for item in seen}), 1)
        self.assertFalse(coordinator.agents.pending(key))
        self.assertEqual(bot.ai.get_or_create_session("__work__:agent:" + agent["id"]).conversation_id, "retained-child-cid")

    async def test_parent_reset_stops_child_and_old_mail_cannot_wake_the_new_generation(self):
        bot, key, coordinator, _ = await self.prepare()
        entered = asyncio.Event()
        async def chat(session_key, prompt, **options):
            self.assertTrue(session_key.startswith("__work__:agent:"))
            entered.set()
            await asyncio.Future()
        bot.ai.chat.side_effect = chat
        bot.ai.archive_session = AsyncMock(return_value=True)
        context = self.context(coordinator, key)
        agent = execute("agents", context, operation="create", params={"name": "旧任务", "text": "执行中"})
        await coordinator.poll()
        await asyncio.wait_for(entered.wait(), 1)
        bot.ai.get_or_create_session(key).generation += 1
        await coordinator.poll()
        await asyncio.gather(*coordinator.agents.tasks.values(), return_exceptions=True)
        await coordinator.poll()
        self.assertEqual(coordinator.agents.mailbox.agent(agent["id"])["state"], "closed")
        self.assertFalse(coordinator.agents.pending(key))
        self.assertEqual(bot.ai.chat.await_count, 1)

    async def test_restart_interrupts_an_inflight_native_turn_and_only_runs_unstarted_mail(self):
        from subagent_runtime import SubagentRuntime
        bot, key, coordinator, _ = await self.prepare()
        context = self.context(coordinator, key)
        agent = execute("agents", context, operation="create", params={"name": "恢复", "text": "不可重放"})
        execute("agents", context, operation="send", params={"agent_id": agent["id"], "text": "尚未开始"})
        coordinator.agents.mailbox.claim(agent["id"])
        session = bot.ai.get_or_create_session("__work__:agent:" + agent["id"])
        session.conversation_id = "orphan-native-cid"
        await coordinator.agents.close()
        coordinator.agents = SubagentRuntime(coordinator)
        bot.ai._cancel_native_conversation = AsyncMock()
        inputs = []
        async def chat(session_key, prompt, **options):
            context_file = prompt_object(prompt, "console_context_file")["console_context_file"]
            if session_key.startswith("__work__:agent:"):
                inputs.append(prompt_object(prompt, "agent_input")["agent_input"]["text"])
                return "恢复后结果"
            execute("wait", context_file)
            return ""
        bot.ai.chat.side_effect = chat
        await self.settle(coordinator)
        bot.ai._cancel_native_conversation.assert_awaited_once_with("orphan-native-cid")
        self.assertEqual(inputs, ["尚未开始"])

    def test_public_workers_have_native_read_tools_and_no_main_or_desktop_customizations(self):
        public = subagent_cascade_config(public_only=True)["plannerConfig"]
        allowed = [tool["name"] for tool in public["declarativeMixinConfig"]["tools"]]
        self.assertIn("search_web", allowed)
        self.assertIn("read_url_content", allowed)
        self.assertNotIn("run_command", allowed)
        self.assertFalse(public["customizationDiscoveryConfig"]["rules"]["inheritUser"])
        self.assertTrue(public["toolConfig"]["runCommand"]["forceDisable"])
        normal = subagent_cascade_config()["plannerConfig"]
        self.assertNotIn("declarativeMixinConfig", normal)
        self.assertNotIn("runCommand", normal["toolConfig"])
        self.assertNotIn("searchWeb", normal["toolConfig"])
        self.assertTrue(main_cascade_config()["plannerConfig"]["toolConfig"]["searchWeb"]["forceDisable"])
