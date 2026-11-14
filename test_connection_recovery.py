"""Offline regressions for restarted language servers and service notices."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

from antigravity_client import (AgentAPIConnectionRefusedError, AgentServiceError,
    AgentServiceTimeoutError, AntigravityClient, SessionInvalidatedError)


REFUSED = ('rpc error: code = Unavailable desc = connection error: desc = '
    '"transport: Error while dialing: dial tcp 127.0.0.1:11278: connectex: '
    'No connection could be made because the target machine actively refused it."')
OLD_CID = "b1157736-eadc-4935-bc03-d9e4faf335ca"
KEY = "onebot_group_1129846525"


class ConnectionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot_connection_recovery_")
        self.root = Path(self.temp.name)
        self.ai = AntigravityClient(sessions_file=str(self.root / "sessions.json"),
            ls_address="127.0.0.1:11278", csrf_token="old-fixture-token")
        self.ai._is_ready = True
        self.ai._executable = "fixture_agentapi.exe"
        self.ai._brain_dir = str(self.root / "brain")
        self.ai._wait_for_model_response = AsyncMock(return_value="继续刚才的话题")
        self.ai._cancel_native_conversation = AsyncMock()
        self.discover = MagicMock(return_value=("127.0.0.1:5053", "new-fixture-token"))
        self.ai._discover_ls_credentials = self.discover

    def tearDown(self):
        self.temp.cleanup()

    def session(self):
        session = self.ai.get_or_create_session(KEY)
        session.conversation_id = OLD_CID
        session.memory_context = {"conversation_id": OLD_CID, "loaded_files": {"fixture": "read"}}
        self.ai._save_sessions()
        return session

    @staticmethod
    def process(*, error=None, result=None):
        proc = MagicMock(returncode=1 if error else 0)
        proc.communicate = AsyncMock(return_value=(
            json.dumps(result or {}).encode(), (error or "").encode()))
        proc.wait = AsyncMock(return_value=0)
        return proc

    async def test_refused_port_rediscovers_and_resends_same_message_and_cid(self):
        session = self.session()
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=[
                self.process(error=REFUSED), self.process()])) as spawn:
            reply = await self.ai.chat(KEY, "继续聊", raise_on_error=True)
        self.assertEqual(reply, "继续刚才的话题")
        self.assertEqual(spawn.await_count, 2)
        first, second = spawn.await_args_list
        self.assertEqual(first.args, second.args)
        self.assertEqual(first.args[1:], ("send-message", OLD_CID, "继续聊"))
        self.assertEqual(first.kwargs["env"]["ANTIGRAVITY_LS_ADDRESS"], "127.0.0.1:11278")
        self.assertEqual(second.kwargs["env"]["ANTIGRAVITY_LS_ADDRESS"], "127.0.0.1:5053")
        self.assertEqual(second.kwargs["env"]["ANTIGRAVITY_CSRF_TOKEN"], "new-fixture-token")
        self.discover.assert_called_once()
        self.assertTrue(self.discover.call_args.kwargs["refresh"])
        self.assertEqual(session.conversation_id, OLD_CID)
        self.assertEqual(session.memory_context["loaded_files"], {"fixture": "read"})
        saved = json.loads(Path(self.ai.sessions_file).read_text(encoding="utf-8"))
        self.assertEqual(saved[KEY]["conversation_id"], OLD_CID)
        self.ai._wait_for_model_response.assert_awaited_once()

    async def test_new_conversation_can_recover_before_any_request_is_accepted(self):
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=[
                self.process(error=REFUSED), self.process(result={"response": {
                    "newConversation": {"conversationId": "new-fixture-cid"}}})])) as spawn:
            await self.ai.chat(KEY, "开始聊", raise_on_error=True)
        self.assertEqual(spawn.await_args_list[0].args, spawn.await_args_list[1].args)
        self.assertEqual(spawn.await_args_list[0].args[1], "new-conversation")
        self.assertEqual(self.ai.get_or_create_session(KEY).conversation_id, "new-fixture-cid")

    async def test_loopback_refusal_formats_and_json_errors_are_recognized(self):
        messages = (REFUSED.replace("connectex:", "connect:").replace(
            "No connection could be made because the target machine actively refused it.", "connection refused"),
            REFUSED.replace("127.0.0.1", "[::1]"), REFUSED.replace("127.0.0.1", "localhost"))
        for message in messages:
            with self.subTest(message=message):
                self.ai.ls_address = "127.0.0.1:11278"
                self.discover.reset_mock()
                with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=[
                        self.process(error="command failed", result={"error": message}), self.process()])) as spawn:
                    await self.ai._run_agentapi_cmd(["send-message", OLD_CID, "继续"])
                self.assertEqual(spawn.await_count, 2)
                self.discover.assert_called_once()

    async def test_concurrent_groups_share_one_discovery(self):
        entered = 0
        both_failed = asyncio.Event()
        attempts = []

        async def command(args, *, timeout, env):
            nonlocal entered
            attempts.append((list(args), env["ANTIGRAVITY_LS_ADDRESS"]))
            if env["ANTIGRAVITY_LS_ADDRESS"].endswith(":11278"):
                entered += 1
                if entered == 2:
                    both_failed.set()
                await both_failed.wait()
                raise AgentAPIConnectionRefusedError(REFUSED)
            return {}

        self.ai._run_agentapi_cmd_once = AsyncMock(side_effect=command)
        await asyncio.wait_for(asyncio.gather(
            self.ai._run_agentapi_cmd(["send-message", "group-a", "first"]),
            self.ai._run_agentapi_cmd(["send-message", "group-b", "second"])), 2)
        self.discover.assert_called_once()
        self.assertEqual(len(attempts), 4)
        for cid in ("group-a", "group-b"):
            self.assertEqual([address for args, address in attempts if args[1] == cid],
                ["127.0.0.1:11278", "127.0.0.1:5053"])

    async def test_unavailable_without_definitive_refusal_is_never_replayed(self):
        messages = ("rpc error: code = Unavailable desc = transport is closing",
            "rpc error: code = Unavailable desc = EOF", "429 quota exhausted",
            "dial tcp 127.0.0.1:11278: i/o timeout", "connection reset by peer",
            REFUSED.replace(":11278:", ":9999:"), REFUSED.replace("127.0.0.1", "remote.example"),
            "rpc error: code = Internal desc = downstream failed: " + REFUSED)
        for message in messages:
            with self.subTest(message=message), patch("asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=self.process(error=message))) as spawn:
                with self.assertRaises(RuntimeError):
                    await self.ai._run_agentapi_cmd(["send-message", OLD_CID, "test"])
                spawn.assert_awaited_once()
        self.discover.assert_not_called()

    async def test_missing_service_is_bounded_and_later_input_can_recover(self):
        session = self.session()
        self.discover.return_value = (None, None)

        async def command(args, *, timeout, env):
            if env["ANTIGRAVITY_LS_ADDRESS"].endswith(":11278"):
                raise AgentAPIConnectionRefusedError(REFUSED)
            return {}

        self.ai._run_agentapi_cmd_once = AsyncMock(side_effect=command)
        for _ in range(2):
            with self.assertRaises(AgentServiceError) as caught:
                await self.ai.chat(KEY, "重试")
            self.assertNotIn("11278", str(caught.exception))
            self.assertIsInstance(caught.exception.__cause__, AgentAPIConnectionRefusedError)
        self.assertEqual(self.ai._run_agentapi_cmd_once.await_count, 2)
        self.discover.assert_called_once()
        self.assertEqual(session.conversation_id, OLD_CID)
        self.discover.return_value = ("127.0.0.1:5053", "new-fixture-token")
        self.ai._last_discovery_failure -= 3
        self.assertEqual(await self.ai.chat(KEY, "继续"), "继续刚才的话题")
        self.assertEqual(self.discover.call_count, 2)
        self.assertEqual(session.conversation_id, OLD_CID)

    async def test_retry_is_attempted_only_once(self):
        self.session()
        self.ai._run_agentapi_cmd_once = AsyncMock(side_effect=AgentAPIConnectionRefusedError(REFUSED))
        with self.assertRaises(AgentAPIConnectionRefusedError):
            await self.ai.chat(KEY, "继续", raise_on_error=True)
        self.assertEqual(self.ai._run_agentapi_cmd_once.await_count, 2)
        self.discover.assert_called_once()
        self.assertEqual(self.ai.get_or_create_session(KEY).conversation_id, OLD_CID)

    async def test_remote_configuration_is_never_replaced_with_local_server(self):
        self.ai.ls_address = "remote.example:5000"
        self.ai._run_agentapi_cmd_once = AsyncMock(side_effect=AgentAPIConnectionRefusedError(REFUSED))
        with self.assertRaises(AgentAPIConnectionRefusedError):
            await self.ai._run_agentapi_cmd(["send-message", OLD_CID, "test"])
        self.ai._run_agentapi_cmd_once.assert_awaited_once()
        self.discover.assert_not_called()

    async def test_reset_or_cancel_during_discovery_cannot_send_late_request(self):
        for reset in (True, False):
            with self.subTest(reset=reset):
                session = self.session()
                loop = asyncio.get_running_loop()
                entered = asyncio.Event()
                released = threading.Event()
                finished = threading.Event()

                def discover(**kwargs):
                    loop.call_soon_threadsafe(entered.set)
                    try:
                        released.wait(2)
                        return "127.0.0.1:5053", "new-fixture-token"
                    finally:
                        finished.set()

                self.discover.side_effect = discover
                self.ai._run_agentapi_cmd_once = AsyncMock(side_effect=AgentAPIConnectionRefusedError(REFUSED))
                request = asyncio.create_task(self.ai.chat(KEY, "继续", raise_on_error=True))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    if reset:
                        self.ai.reset_session(KEY)
                    else:
                        request.cancel()
                    with self.assertRaises(SessionInvalidatedError if reset else asyncio.CancelledError):
                        await asyncio.wait_for(request, 1)
                finally:
                    released.set()
                    await asyncio.to_thread(finished.wait, 1)
                self.ai._run_agentapi_cmd_once.assert_awaited_once()
                self.assertEqual(self.ai.ls_address, "127.0.0.1:11278")
                self.assertEqual(self.ai._connection_generation, 0)
                self.assertEqual(session.conversation_id, None if reset else OLD_CID)

    async def test_waiting_for_discovery_lock_obeys_original_deadline(self):
        self.ai._run_agentapi_cmd_once = AsyncMock(side_effect=AgentAPIConnectionRefusedError(REFUSED))
        await self.ai._reconnect_lock.acquire()
        try:
            with self.assertRaises(TimeoutError):
                await self.ai._run_agentapi_cmd(["send-message", OLD_CID, "test"], timeout=0.03)
        finally:
            self.ai._reconnect_lock.release()
        self.discover.assert_not_called()
        self.ai._run_agentapi_cmd_once.assert_awaited_once()

    async def test_discovery_and_retry_share_the_command_budget(self):
        budgets = []

        async def command(args, *, timeout, env):
            budgets.append(timeout)
            if len(budgets) == 1:
                await asyncio.sleep(0.02)
                raise AgentAPIConnectionRefusedError(REFUSED)
            return {}

        self.ai._run_agentapi_cmd_once = AsyncMock(side_effect=command)
        await self.ai._run_agentapi_cmd(["send-message", OLD_CID, "test"], timeout=1)
        self.assertEqual(len(budgets), 2)
        self.assertLess(budgets[1], budgets[0] - 0.01)
        self.assertLessEqual(self.discover.call_args.kwargs["timeout"], budgets[1] + 0.1)

    async def test_background_unlimited_timeout_survives_reconnection(self):
        self.ai._run_agentapi_cmd_once = AsyncMock(side_effect=[AgentAPIConnectionRefusedError(REFUSED), {}])
        await self.ai._run_agentapi_cmd(["send-message", OLD_CID, "work"], timeout=0)
        self.assertEqual([c.kwargs["timeout"] for c in self.ai._run_agentapi_cmd_once.await_args_list], [0, 0])
        self.assertLessEqual(self.discover.call_args.kwargs["timeout"], 12)

    async def test_errors_are_exceptions_never_model_reply_strings(self):
        self.session()
        for error, expected in ((RuntimeError("backend detail:11278"), AgentServiceError),
                (TimeoutError("internal details"), AgentServiceTimeoutError)):
            with self.subTest(expected=expected):
                self.ai._run_agentapi_cmd_once = AsyncMock(side_effect=error)
                with self.assertRaises(expected) as caught:
                    await self.ai.chat(KEY, "test")
                self.assertIs(caught.exception.__cause__, error)
                self.assertNotIn("11278", str(caught.exception))
        self.assertEqual(self.ai.get_or_create_session(KEY).conversation_id, OLD_CID)

    async def test_unavailable_initialization_also_raises_service_exception(self):
        self.session()
        self.ai._is_ready = False
        self.ai.initialize = MagicMock(return_value=False)
        with self.assertRaises(AgentServiceError):
            await self.ai.chat(KEY, "继续")
        self.assertEqual(self.ai.get_or_create_session(KEY).conversation_id, OLD_CID)


class CredentialDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot_discovery_")
        self.ai = AntigravityClient(sessions_file=str(Path(self.temp.name) / "sessions.json"),
            ls_address="127.0.0.1:11278", csrf_token="stale-fixture-token")
        self.probe = MagicMock(side_effect=lambda port, **kwargs: (True, "current-fixture-token")
            if int(port) == 5053 else (False, None))
        self.ai._probe_antigravity_http = self.probe
        self.log = "listening on random port at 11278 for HTTP\n" * 4 + (
            "listening on random port at 5053 for HTTP\nlistening on random port at 5052 for HTTPS\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_initialize_validates_cached_endpoint_and_adopts_live_credentials(self):
        self.ai._discover_executable = MagicMock(return_value="fixture.exe")
        self.ai._discover_project_id = MagicMock(return_value="fixture-project")
        with patch("os.path.isfile", return_value=True), patch("builtins.open", mock_open(read_data=self.log)), \
                patch("subprocess.run", side_effect=AssertionError("log discovery should suffice")):
            self.assertTrue(self.ai.initialize())
        self.assertEqual(self.ai.ls_address, "127.0.0.1:5053")
        self.assertEqual(self.ai.csrf_token, "current-fixture-token")
        self.assertEqual([c.args[0] for c in self.probe.call_args_list], [11278, 5053])

    def test_refresh_bypasses_stale_config_and_environment(self):
        with patch.dict("os.environ", {"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:11278",
                "ANTIGRAVITY_CSRF_TOKEN": "stale-env-token"}), patch("os.path.isfile", return_value=True), \
                patch("builtins.open", mock_open(read_data=self.log)):
            self.assertEqual(self.ai._discover_ls_credentials(refresh=True),
                ("127.0.0.1:5053", "current-fixture-token"))
        self.assertEqual([c.args[0] for c in self.probe.call_args_list], [5053])

    def test_process_fallback_checks_all_servers_and_reuses_tokenless_probe(self):
        self.probe.side_effect = lambda port, **kwargs: (int(port) == 5053, None)
        servers = [{"Cmd": "--csrf_token abc-def", "Ports": [5052]},
                   {"Cmd": "--csrf_token aabb-ccdd", "Ports": [5053]}]
        result = MagicMock(stdout=json.dumps(servers))
        with patch("os.path.isfile", return_value=True), patch("builtins.open", mock_open(read_data=self.log)), \
                patch("subprocess.run", return_value=result):
            self.assertEqual(self.ai._discover_ls_credentials(refresh=True), ("127.0.0.1:5053", "aabb-ccdd"))
        ports = [c.args[0] for c in self.probe.call_args_list]
        self.assertEqual(len(ports), len(set(ports)))

    def test_explicit_remote_config_is_preserved(self):
        self.ai.ls_address = "remote.example:5000"
        self.assertEqual(self.ai._discover_ls_credentials(), ("remote.example:5000", "stale-fixture-token"))
        self.probe.assert_not_called()


class ServiceFailureIngressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Compose the existing isolated ingress fixture without inheriting and
        # rediscovering its unrelated tests a second time.
        import test_startup_participation
        self.fixture = test_startup_participation.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def fail_ai(self, bot):
        bot.ai.chat.side_effect = AgentServiceError()
        bot._remember_chat_reply = MagicMock()
        bot._ack_image_context = MagicMock()
        bot.memory_mgr.record_interaction = MagicMock()
        bot.context_buffer.add_message = MagicMock(wraps=bot.context_buffer.add_message)

    def assert_no_false_reply(self, bot):
        bot._remember_chat_reply.assert_not_called()
        bot._ack_image_context.assert_not_called()
        bot.memory_mgr.record_interaction.assert_not_called()
        self.assertFalse(any(c.kwargs.get("is_bot") for c in bot.context_buffer.add_message.call_args_list))

    async def test_autonomous_service_failure_sends_nothing_and_next_message_is_analyzed(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, key = self.fixture.bot(official, stored_cid=OLD_CID)
                self.fail_ai(bot)
                message = await self.fixture.receive(bot, official, "offline", "今天聊点什么")
                self.fixture.assert_not_sent(bot, official, message)
                self.assert_no_false_reply(bot)
                self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, OLD_CID)
                bot.ai.chat.side_effect = None
                bot.ai.chat.return_value = '{"should_reply":true,"reply":"接着聊吧"}'
                next_message = await self.fixture.receive(bot, official, "online", "刚才说到哪里", elapsed=1)
                self.assertEqual(bot.ai.chat.await_count, 2)
                (next_message.reply if official else bot.send_group_message).assert_awaited_once()

    async def test_directed_group_failure_only_sends_safe_notice_without_memories(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, _ = self.fixture.bot(official, stored_cid=OLD_CID)
                self.fail_ai(bot)
                # Configured master is a directed reply in both transports.
                bot.app_config.bot.master_id = "456"
                message = await self.fixture.receive(bot, official, "directed", "继续刚才的话题")
                sender = message.reply if official else bot.send_group_message
                sender.assert_awaited_once()
                notice = sender.await_args.kwargs["content"] if official else sender.await_args.args[1]
                self.assertIn(AgentServiceError.user_message, notice)
                self.assertNotIn("11278", notice)
                self.assertNotIn("rpc error", notice)
                self.assert_no_false_reply(bot)

    async def test_private_failures_only_send_notice_without_saving_ai_output(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, _ = self.fixture.bot(official)
                self.fail_ai(bot)
                if official:
                    sender = AsyncMock()
                    await bot._handle_message_core("c2c_456", "接着聊聊", sender, user_openid="456")
                    notice = sender.await_args.args[0]
                else:
                    bot.send_private_message = AsyncMock()
                    await bot._handle_private_message({"user_id": 456, "message_id": "private",
                        "raw_message": "接着聊聊", "message": [{"type": "text", "data": {"text": "接着聊聊"}}],
                        "sender": {"nickname": "Fixture user"}})
                    bot.send_private_message.assert_awaited_once()
                    notice = bot.send_private_message.await_args.args[1]
                self.assertEqual(notice, AgentServiceError.user_message)
                self.assert_no_false_reply(bot)


if __name__ == "__main__":
    unittest.main()
