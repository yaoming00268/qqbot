"""Offline checks for durable, agent-controlled unfinished speech."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest

from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.files import ConsoleFileError


class SpeechBufferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-speech-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.broker = ConsoleBroker(self.root / "console.sqlite3")
        self.payload = dict(namespace="account", scope="group", session_key="persona:group",
            generation=0, sender_id="alice", is_group=True, main_agent=True,
            speech_check_required=True, source_message_id="one", source_text="因为",
            received_at=1, reply_sources=[], output_dir=str(self.root), artifact_roots=[str(self.root)])

    def context(self, **values):
        path = self.broker.issue_context({**self.payload, **values}, self.root / "contexts")
        _, token = ConsoleBroker.from_context(path)
        return path, token

    def speech(self, path, operation, **params):
        return execute("speech", path, operation=operation, params=params)

    def test_output_confirms_speech_without_ready_and_hold_cancels_drafts(self):
        path, token = self.context()
        execute("message", path, text="未提交草稿")
        self.assertEqual(self.speech(path, "status")["state"], "ready")
        receipt = self.speech(path, "hold", text="还在说明原因")
        self.assertEqual(receipt["state"], "waiting")
        self.assertEqual(self.broker.staged_messages(token), [])
        self.assertEqual(self.broker.reply_control(token)["mode"], "wait")
        self.assertEqual(self.speech(path, "hold"), receipt)
        with self.assertRaises(ConsoleFileError):
            self.speech(path, "ready")
        with self.assertRaises(ConsoleFileError):
            execute("reply", path, text="迟到答复", finish=True)
        other, other_token = self.context(source_message_id="complete", source_text="完整问题")
        result = execute("reply", other, text="直接答复", finish=True)
        self.assertEqual(result["state"], "finished")
        self.assertEqual(self.broker.reply_control(other_token)["mode"], "finished")

    def test_repeated_fragments_survive_restart_until_ready_turn_is_sealed(self):
        first, _ = self.context()
        self.speech(first, "hold")
        self.broker = ConsoleBroker(self.broker.path)
        second, _ = self.context(source_message_id="two", source_text="我喜欢猫", received_at=2)
        status = self.speech(second, "status")
        self.assertEqual([row["text"] for row in status["cached_messages"]], ["因为"])
        self.assertEqual([row["text"] for row in status["current_messages"]], ["我喜欢猫"])
        self.speech(second, "hold")
        third, token = self.context(source_message_id="three", source_text="所以想养一只，你觉得呢？", received_at=3)
        self.assertEqual([row["id"] for row in self.speech(third, "status")["cached_messages"]], ["one", "two"])
        execute("reply", third, text="那我们先了解养猫需要哪些准备。", finish=True)
        with self.assertRaises(ConsoleFileError):
            self.speech(third, "hold")
        self.broker.seal_messages(token)
        fourth, _ = self.context(source_message_id="four")
        self.assertEqual(self.speech(fourth, "status")["cached_messages"], [])

    def test_ready_without_successful_seal_keeps_cache_and_never_clears_future_hold(self):
        first, _ = self.context()
        self.speech(first, "hold")
        second, token = self.context(source_message_id="two", source_text="更多信息")
        self.speech(second, "ready")
        self.broker.seal_messages(token, cancel=True)
        self.broker.seal_messages(token)  # A later cleanup must not consume a failed snapshot.
        failed, _ = self.context(source_message_id="failure")
        self.assertEqual(len(self.speech(failed, "status")["cached_messages"]), 1)
        older, old_token = self.context(source_message_id="old")
        newer, _ = self.context(source_message_id="new", source_text="新片段")
        self.speech(newer, "hold")
        self.speech(older, "ready")
        self.broker.seal_messages(old_token)
        after, _ = self.context(source_message_id="after")
        self.assertEqual([r["id"] for r in self.speech(after, "status")["cached_messages"]], ["new"])

    def test_account_scope_persona_generation_and_speaker_are_isolated(self):
        path, _ = self.context()
        self.speech(path, "hold")
        for field, value in (("namespace", "other"), ("scope", "other"), ("session_key", "other"),
                             ("generation", 1), ("sender_id", "bob")):
            with self.subTest(field=field):
                other, _ = self.context(**{field: value})
                self.assertEqual(self.speech(other, "status")["cached_messages"], [])
        same, _ = self.context()
        self.assertEqual(len(self.speech(same, "status")["cached_messages"]), 1)

    def test_cache_uses_bound_current_sources_only_and_preserves_exact_text(self):
        rows = [{"id": "history", "sender_id": "alice", "text": "旧聊天", "received_at": 0},
                {"id": "a1", "sender_id": "alice", "text": "原文\n  空格", "received_at": 1},
                {"id": "b1", "sender_id": "bob", "text": "他人的话", "received_at": 2}]
        path, _ = self.context(source_message_id="a2", source_text="  因为\n", reply_sources=rows,
                               speech_current_messages=rows[1:])
        with self.assertRaises(ValueError):
            self.speech(path, "hold", content="伪造缓存")
        self.speech(path, "hold")
        next_path, _ = self.context(source_message_id="next")
        cached = self.speech(next_path, "status")["cached_messages"]
        self.assertEqual([(r["id"], r["text"]) for r in cached], [("a1", "原文\n  空格"), ("a2", "  因为\n")])

    def test_workers_and_internal_contexts_cannot_manage_chat_cache(self):
        for role in ("parent_request_id", "agent_worker_id", "sandbox_session_id", "agent_event", "idle_event"):
            path, _ = self.context(**{role: "worker"})
            with self.subTest(role=role), self.assertRaises(PermissionError):
                self.speech(path, "hold")

    def test_internal_grants_strip_inherited_human_speech_decision(self):
        path, token = self.context()
        self.speech(path, "hold")
        original = self.broker.context(token)
        for role in ("agent_event", "self_directed", "agent_worker_id", "sandbox_session_id", "activity_controller"):
            child = self.broker.issue_context({**original, role: "internal"}, self.root / "internal")
            _, child_token = ConsoleBroker.from_context(child)
            bound = self.broker.context(child_token)
            self.assertFalse(bound.get("speech_check_required"))
            self.assertNotIn("speech", bound)
            if role == "agent_event":
                self.assertEqual(execute("finish", child)["state"], "finished")
        self.assertTrue(self.broker.has_pending_speech("account", "group", "persona:group", 0))

    def test_send_and_delivery_confirm_speech_in_the_output_transaction(self):
        path, token = self.context()
        artifact = self.root / "file.txt"
        artifact.write_text("实际内容", encoding="utf-8")
        task = execute("submit", path, task="资料")
        self.broker.update(task["id"], "ready", {"success": True, "result": {"files": []}})
        for action, params in (("send", {"files": [str(artifact)]}),
                               ("deliver", {"request_id": task["id"], "text": "成果"})):
            with self.subTest(action=action):
                execute(action, path, **params)
                self.assertEqual(self.speech(path, "status")["state"], "released")
        with self.assertRaises(ConsoleFileError):
            self.speech(path, "hold")

    def test_sticker_confirms_speech_and_hold_cancels_its_unsealed_request(self):
        from PIL import Image
        from sticker_library import StickerLibrary
        picture = self.root / "sticker.png"
        Image.new("RGB", (4, 4), "blue").save(picture)
        library = StickerLibrary(self.root / "stickers")
        identity = library.import_file(picture, ["开心"])
        path, _ = self.context(sticker_config={"enabled": True, "directory": str(library.root)})
        receipt = execute("sticker", path, sticker_id=identity)
        self.speech(path, "hold")
        self.assertEqual(self.broker.record(receipt["id"])["state"], "cancel_requested")

    def test_concurrent_hold_and_reply_have_exactly_one_outcome(self):
        for index in range(6):
            path, token = self.context(source_message_id=str(index))
            barrier = threading.Barrier(2)
            def perform(hold):
                barrier.wait()
                try:
                    return self.speech(path, "hold") if hold else execute("reply", path, text="完成的答复", finish=True)
                except ConsoleFileError:
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                hold, reply = pool.submit(perform, True), pool.submit(perform, False)
                held, replied = hold.result(), reply.result()
            self.assertNotEqual(held is None, replied is None)
            self.assertEqual(self.broker.reply_control(token)["mode"], "wait" if held else "finished")
            self.assertEqual(len(self.broker.staged_messages(token)), 0 if held else 1)

    def test_public_child_cli_emits_json_and_retains_buffer_in_new_process(self):
        path, _ = self.context()
        cli = str(Path(__file__).with_name("console_tools") / "cli.py")
        def run(*args, expected=0):
            result = subprocess.run([sys.executable, cli, *args, "--context-file", path],
                capture_output=True, encoding="utf-8", timeout=10)
            self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
            return json.loads(result.stdout)
        self.assertEqual(run("speech", "hold")["state"], "waiting")
        path, _ = self.context(source_message_id="next", source_text="补充")
        self.assertEqual(run("speech", "status")["cached_messages"][0]["text"], "因为")
        self.assertIn("speech", run("context")["capabilities"])
        self.assertEqual(run("reply", "--text", "直接回复", "--finish")["state"], "finished")


if __name__ == "__main__":
    unittest.main()
