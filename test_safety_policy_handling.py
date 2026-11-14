"""Offline regressions for provider policy notices and subsequent chat turns."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from antigravity_client import AntigravityClient, ContentBlockedError


POLICY_NOTICE = (
    "The prompt could not be submitted. The prompt contains sensitive words that violate Google's "
    "[Generative AI Prohibited Use policy](https://policies.google.com/terms/generative-ai/use-policy). "
    "Try rephrasing the prompt. If you think this was an error, "
    "[send feedback](https://ai.google.dev/gemini-api/docs/troubleshooting)."
)
OUTPUT_NOTICE = (
    "Your previous response was blocked by content safety filters: "
    "The model output could not be generated. This output contains sensitive words that violate Google's "
    "Generative AI Prohibited Use policy."
)


def inbound(step, marker):
    return {"step_index": step, "type": "USER_INPUT", "source": "USER", "content": marker}


def final(step, content, **metadata):
    return {"step_index": step, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE",
            "content": content, **metadata}


class PolicySignalTests(unittest.TestCase):
    def test_google_notices_and_provider_metadata_are_recognized(self):
        for detail in (POLICY_NOTICE, OUTPUT_NOTICE, POLICY_NOTICE.replace("violate Google's", "violate\nGoogle’s"),
                       {"code": "SAFETY"}, {"finishReason": "IMAGE_SAFETY"},
                       "请求不符合内容政策", "Content filtered", "finish_reason: PROHIBITED_CONTENT"):
            with self.subTest(detail=detail):
                self.assertTrue(AntigravityClient._content_is_blocked(detail))
        for metadata in ({"promptFeedback": {"blockReason": "SAFETY"}},
                         {"candidates": [{"finishReason": "PROHIBITED_CONTENT"}]},
                         {"finish_reason": "BLOCKLIST"}):
            with self.subTest(metadata=metadata), self.assertRaises(ContentBlockedError):
                AntigravityClient._check_agentapi_response(["send-message", "cid"], metadata)

    def test_errors_are_classified_without_scanning_ordinary_reply_content(self):
        with self.assertRaises(ContentBlockedError):
            AntigravityClient._check_agentapi_response(["send-message", "cid"],
                {"response": {"error": {"code": 400, "message": POLICY_NOTICE}}})
        AntigravityClient._check_agentapi_response(["send-message", "cid"], {"content": POLICY_NOTICE})
        for text in ("The prompt could not be submitted: connection refused", "Safety tips",
                     "How should we handle safety policies?", "ordinary content", "quota exhausted"):
            self.assertFalse(AntigravityClient._content_is_blocked(text), text)
        self.assertFalse(AntigravityClient._is_policy_notice("服务提示的含义是：" + POLICY_NOTICE))


class PolicyTranscriptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot_policy_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ai = AntigravityClient(sessions_file=str(self.root / "sessions.json"))
        self.ai._is_ready = True
        self.ai._brain_dir = str(self.root / "brain")
        self.ai._cancel_native_conversation = AsyncMock()
        self.ai._ensure_conversation_present = AsyncMock()
        self.key = "onebot_group_policy"
        self.session = self.ai.get_or_create_session(self.key)
        self.session.conversation_id = "fixture"
        self.path = self.root / "brain/fixture/.system_generated/logs/transcript.jsonl"
        self.path.parent.mkdir(parents=True)

    def write(self, *rows):
        self.path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")

    async def test_google_notice_logged_as_completed_model_reply_is_an_error(self):
        for notice in (POLICY_NOTICE, OUTPUT_NOTICE):
            with self.subTest(notice=notice):
                self.write(inbound(0, "current"), final(1, notice))
                with self.assertRaises(ContentBlockedError):
                    await self.ai._wait_for_model_response("fixture", expected_step_min=0,
                        request_marker="current", allow_empty_response=True, timeout=.3)

    async def test_rejection_before_input_is_logged_does_not_wait_forever(self):
        for metadata in ({"content": POLICY_NOTICE}, {"promptFeedback": {"blockReason": "SAFETY"}}):
            self.write({"step_index": 0, "type": "ERROR_MESSAGE", "source": "SYSTEM", "status": "DONE", **metadata})
            with self.subTest(metadata=metadata), self.assertRaises(ContentBlockedError):
                await self.ai._wait_for_model_response("fixture", expected_step_min=0,
                    request_marker="not-logged", timeout=.3)

    async def test_nested_candidate_block_reason_ends_empty_generation(self):
        self.write(inbound(0, "current"), final(1, "", candidates=[{"finishReason": "SAFETY"}]))
        with self.assertRaises(ContentBlockedError):
            await self.ai._wait_for_model_response("fixture", expected_step_min=0,
                request_marker="current", allow_empty_response=True, timeout=.3)

    async def test_old_notices_and_tool_text_do_not_block_the_current_reply(self):
        discussion = "这个服务提示说明本轮被拦截：" + POLICY_NOTICE
        self.write(final(0, POLICY_NOTICE), inbound(1, "current"),
            {"step_index": 2, "type": "TOOL_RESULT", "source": "TOOL", "status": "ERROR", "content": POLICY_NOTICE},
            final(3, discussion))
        reply = await self.ai._wait_for_model_response("fixture", expected_step_min=0,
            request_marker="current", timeout=.3)
        self.assertEqual(reply, discussion)

    async def test_block_is_not_replayed_and_the_next_input_uses_the_same_conversation(self):
        attempts = []

        async def command(args, **options):
            marker = args[-1].splitlines()[0]
            attempts.append(args)
            if len(attempts) == 1:
                self.write(inbound(0, marker), final(1, POLICY_NOTICE))
            else:
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(inbound(2, marker)) + "\n" + json.dumps(final(3, "可以继续聊天")) + "\n")
            return {}

        self.ai._run_agentapi_cmd = AsyncMock(side_effect=command)
        with self.assertRaises(ContentBlockedError):
            await self.ai.chat(self.key, "first input", bind_response=True, allow_empty_response=True, timeout=1)
        self.assertEqual(len(attempts), 1)
        self.ai._cancel_native_conversation.assert_awaited_once_with("fixture")
        self.assertFalse(self.session.lock.locked())
        self.assertEqual(self.session.conversation_id, "fixture")
        self.assertEqual(await self.ai.chat(self.key, "new input", bind_response=True, timeout=1), "可以继续聊天")
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(args[:2] == ["send-message", "fixture"] for args in attempts))
        self.assertNotIn("first input", attempts[1][-1])

    async def test_nonzero_rpc_exit_preserves_the_policy_error_type(self):
        process = MagicMock(returncode=1)
        process.communicate = AsyncMock(return_value=(b"", POLICY_NOTICE.encode("utf-8")))
        self.ai._executable = "fixture.exe"
        from unittest.mock import patch
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn:
            with self.assertRaises(ContentBlockedError):
                await self.ai._run_agentapi_cmd(["send-message", "fixture", "input"])
        spawn.assert_awaited_once()


class PolicyIngressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import test_startup_participation
        self.fixture = test_startup_participation.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_group_block_is_skipped_without_replaying_messages_or_recording_a_reply(self):
        for official in (False, True):
            for directed in (False, True):
                with self.subTest(official=official, directed=directed):
                    bot, key = self.fixture.bot(official, stored_cid="same-cid")
                    if directed:
                        bot.app_config.bot.master_id = "456"
                    bot.ai.chat.side_effect = ContentBlockedError(POLICY_NOTICE)
                    bot._remember_chat_reply = MagicMock()
                    bot._ack_image_context = MagicMock()
                    bot.memory_mgr.record_interaction = MagicMock()
                    first = await self.fixture.receive(bot, official, "blocked", "本轮被拒绝的旧输入")
                    sends = first.reply if official else bot.send_group_message
                    self.assertEqual(sends.await_count, int(directed))
                    if directed:
                        notice = sends.await_args.kwargs["content"] if official else sends.await_args.args[1]
                        self.assertIn(ContentBlockedError.user_message, notice)
                        self.assertNotIn("Google", notice)
                    bot._remember_chat_reply.assert_not_called()
                    bot._ack_image_context.assert_not_called()
                    bot.memory_mgr.record_interaction.assert_not_called()
                    state = bot.ai.get_or_create_session(key).chat_state
                    self.assertEqual(state["last_delivery"]["state"], "blocked")
                    self.assertEqual(state["last_delivery"]["code"], "CONTENT_BLOCKED")
                    bot.ai.chat.side_effect = None
                    bot.ai.chat.return_value = "继续聊新的话题"
                    await self.fixture.receive(bot, official, "next", "今天天气如何", elapsed=1)
                    self.assertNotIn("本轮被拒绝的旧输入", bot.ai.chat.await_args.args[1])
                    self.assertEqual(bot.ai.chat.await_count, 2)
                    self.assertEqual(bot.ai.get_or_create_session(key).conversation_id, "same-cid")

    async def test_private_block_only_sends_a_policy_notice(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, _ = self.fixture.bot(official)
                bot.ai.chat.side_effect = ContentBlockedError(POLICY_NOTICE)
                if official:
                    sender = AsyncMock()
                    await bot._handle_message_core("c2c_456", "继续聊聊", sender, user_openid="456")
                    notice = sender.await_args.args[0]
                else:
                    bot.send_private_message = AsyncMock()
                    await bot._handle_private_message({"user_id": 456, "message_id": "private",
                        "raw_message": "继续聊聊", "sender": {"nickname": "Fixture user"}})
                    notice = bot.send_private_message.await_args.args[1]
                self.assertEqual(notice, ContentBlockedError.user_message)

    async def test_blocked_image_context_stays_archived_and_is_not_automatically_reinjected(self):
        for official in (False, True):
            with self.subTest(official=official):
                bot, key = self.fixture.bot(official, stored_cid="same-cid")
                await bot._save_image_context("12345678", "old-image", [{"vision_verified": True,
                    "visual_description": "旧图片的观察内容", "image_index": 0}], timestamp=self.fixture.baseline)
                bot.ai.chat.side_effect = ContentBlockedError(POLICY_NOTICE)
                await self.fixture.receive(bot, official, "blocked-image", "看看图片")
                self.assertIn("旧图片的观察内容", bot.ai.chat.await_args.args[1])
                state = bot.ai.get_or_create_session(key).chat_state
                self.assertIn("old-image:0", state["blocked_image_versions"])
                self.assertNotIn("old-image:0", state.get("image_versions", {}))
                bot.ai.chat.side_effect = None
                bot.ai.chat.return_value = "正常聊天"
                await self.fixture.receive(bot, official, "next", "换个话题", elapsed=1)
                self.assertNotIn("旧图片的观察内容", bot.ai.chat.await_args.args[1])
                self.assertTrue(bot._get_image_context_store().get(bot._image_scope("12345678"), "old-image"))
                # Skips survive a process restart but belong only to this persona.
                restored = AntigravityClient(sessions_file=bot.ai.sessions_file)
                self.assertEqual(restored.get_or_create_session(key).chat_state["blocked_image_versions"],
                                 state["blocked_image_versions"])
                await bot._save_image_context("12345678", "new-image", [{"vision_verified": True,
                    "visual_description": "新图片的观察内容", "image_index": 0}], timestamp=self.fixture.baseline + 2)
                await self.fixture.receive(bot, official, "new-image-message", "看新图片", elapsed=2)
                self.assertIn("新图片的观察内容", bot.ai.chat.await_args.args[1])
                self.assertNotIn("旧图片的观察内容", bot.ai.chat.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
