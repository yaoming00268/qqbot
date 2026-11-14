"""A direct URL instruction must produce a real browser task before chat."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from browser_control import BrowserError, installed_browser
from browser_requests import browser_urls, direct_browser_request
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from console_tools.server import tools_schema
from sandbox_runtime import SandboxRuntime
from test_desktop_control import FrameBackend
import test_startup_participation as fixtures


class BrowserFrameBackend(FrameBackend):
    """An already open page must change before the worker receives it."""
    def __init__(self):
        super().__init__()
        self.page_url = "https://example.com/previous-page"

    def observe(self, target, path):
        return {**super().observe(target, path), "page_url": self.page_url}

    def act(self, target, observed, action, **params):
        result = super().act(target, observed, action, **params)
        if action == "navigate":
            self.page_url = params["url"]
        return result


class BrowserRequestParsingTests(unittest.TestCase):
    def test_screenshot_instruction_separates_url_from_chinese_suffix(self):
        text = "你去https://www.kungal.com/刷帖子吧"
        self.assertEqual(browser_urls(text), ["https://www.kungal.com/"])
        self.assertEqual(direct_browser_request(text, is_master=True, directed=True),
                         {"url": "https://www.kungal.com/", "text": text})

    def test_direct_navigation_and_reading_requests(self):
        for text in ("去 https://example.com/ 看看帖子", "帮我打开https://example.com/", "请你去https://example.com/",
                     "你能不能去 https://example.com/ 看帖子", "https://example.com/ 你自己去看看", "访问 https://example.com/"):
            with self.subTest(text=text):
                self.assertIsNotNone(direct_browser_request(text, is_master=True, directed=True))
        self.assertEqual(browser_urls("你去https://example.com/帖子/123 看看"), ["https://example.com/帖子/123"])
        self.assertEqual(browser_urls("打开https://example.com/?q=看视频"), ["https://example.com/?q=看视频"])
        self.assertEqual(browser_urls("打开https://example.com/#看视频"), ["https://example.com/#看视频"])

    def test_discussion_quotes_negation_downloads_and_nonowners_do_not_auto_launch(self):
        for text in ("你觉得 https://example.com/ 怎么样", "别去 https://example.com/ 刷帖", "不要打开 https://example.com/",
                     "他说你去https://example.com/刷帖子吧", "“你去https://example.com/刷帖子吧”",
                     "示例：打开 https://example.com/", "如果让你去 https://example.com/ 看帖子呢",
                     "你去过 https://example.com/ 吗", "你打开了 https://example.com/ 吗", "你之前去 https://example.com/ 看过帖子吗",
                     "去 https://example.com/ 下载视频", "看看这两个网站 https://example.com/ https://example.net/"):
            with self.subTest(text=text):
                self.assertIsNone(direct_browser_request(text, is_master=True, directed=True))
        text = "你去https://example.com/刷帖子吧"
        self.assertIsNone(direct_browser_request(text, is_master=False, directed=True))
        self.assertIsNone(direct_browser_request(text, is_master=True, directed=False))


class BrowserURLCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.exe = self.root / "browser/chrome.exe"
        self.exe.parent.mkdir()
        self.exe.touch()
        self.broker = ConsoleBroker(self.root / "console.db")
        self.metadata = {"namespace": "account", "scope": "group", "sender_id": "owner", "is_master": True,
                         "source_text": "你去https://www.kungal.com/刷帖子吧", "sandbox_config": {"enabled": True,
                         "directory": str(self.root / "sandbox"), "mode": "background", "browser_executable": str(self.exe)}}
        self.context = self.broker.issue_context(self.metadata, self.root / "contexts")
        self.runtime = SandboxRuntime(self.root / "sandbox")

    def test_url_only_registers_configured_browser_without_forging_source_text(self):
        receipt = execute("sandbox", self.context, operation="browse", params={"url": "https://www.kungal.com/", "text": "自己刷帖子"})
        duplicate = execute("sandbox", self.context, operation="browse", params={"url": "https://www.kungal.com/", "text": "自己刷帖子"})
        self.assertEqual(receipt["id"], duplicate["id"])
        target_id = json.loads(self.broker.record(receipt["id"])["result"])["target_id"]
        target = self.runtime.game(self.metadata, target_id)
        self.assertEqual(target["executable"], str(self.exe))
        self.assertEqual(target["launch_url"], "https://www.kungal.com/")
        self.assertEqual(target["goal"], "自己刷帖子")
        _, token = ConsoleBroker.from_context(self.context)
        self.assertEqual(self.broker.context(token)["source_text"], "你去https://www.kungal.com/刷帖子吧")
        self.assertNotIn(str(self.exe), self.broker.context(token)["source_text"])
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools_schema()}
        self.assertIn("browse", schemas["qqbot_sandbox"]["properties"]["operation"]["enum"])

    def test_url_authority_and_worker_scope_are_preserved(self):
        with self.assertRaises(PermissionError):
            execute("sandbox", self.context, operation="browse", params={"url": "https://not-authorized.example/"})
        for metadata in ({**self.metadata, "is_master": False}, {**self.metadata, "sandbox_session_id": "work"}):
            context = self.broker.issue_context(metadata, self.root / "other")
            with self.assertRaises(PermissionError):
                execute("sandbox", context, operation="browse", params={"url": "https://www.kungal.com/"})
        with self.assertRaises(PermissionError):
            self.runtime.register(self.metadata, str(self.exe), kind="software")

    def test_discovery_uses_known_installations_and_rejects_missing_or_wrong_program(self):
        install = self.root / "Program Files/Google/Chrome/Application/chrome.exe"
        install.parent.mkdir(parents=True)
        install.touch()
        with patch.dict(os.environ, {"ProgramFiles": str(self.root / "Program Files"), "ProgramFiles(x86)": "", "LOCALAPPDATA": ""}):
            self.assertEqual(installed_browser(), install.resolve())
        wrong = self.root / "cmd.exe"
        wrong.touch()
        for configured in (str(wrong), str(self.root / "missing/chrome.exe")):
            with self.assertRaises(BrowserError):
                installed_browser(configured)

    def test_same_browser_and_login_directory_are_reused_for_a_new_requested_url(self):
        first = self.runtime.register_browser(self.metadata, "https://www.kungal.com/")
        next_context = {**self.metadata, "source_text": "打开 https://example.com/post/123"}
        second = self.runtime.register_browser(next_context, "https://example.com/post/123")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["profile_path"], second["profile_path"])
        self.assertEqual(second["launch_url"], "https://example.com/post/123")


class BrowserRequestIngressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_url_instruction_starts_worker_after_ack_while_main_is_still_active(self):
        from test_ai_fixture import drain_runtime
        for official in (False, True):
            bot, key = self.fixture.bot(official, stored_cid="existing")
            bot.app_config.bot.master_id = "456"
            exe = Path(f"browser-{official}/chrome.exe").resolve()
            exe.parent.mkdir()
            exe.touch()
            bot.app_config.sandbox.directory = f"sandbox-{official}"
            bot.app_config.sandbox.browser_executable = str(exe)
            bot.ai.archive_session = AsyncMock(return_value=True)
            backend = BrowserFrameBackend()
            backend.launch = lambda _target: {"pid": 77}
            worker_entered, release = asyncio.Event(), asyncio.Event()
            main_count = 0

            async def agent(session, prompt, **_options):
                nonlocal main_count
                if session.startswith("activity_summary_"):
                    return json.dumps({"summary": "浏览页面显示游戏帖子，继续查看需要主人登录。"}, ensure_ascii=False)
                if session.startswith("__work__:"):
                    self.assertEqual(backend.page_url, "https://www.kungal.com/")
                    worker_entered.set()
                    context = str(list(Path(bot.app_config.sandbox.directory).glob("sessions/*/contexts/*.json"))[-1])
                    first = execute("desktop", context, operation="screenshot")
                    execute("sandbox", context, operation="note", params={"kind": "observation", "text": "页面显示游戏帖子。", "evidence": [first["observation_id"]]})
                    execute("sandbox", context, operation="pause", params={"text": "继续查看需要主人登录。"})
                    return "本轮已阅读页面展示的帖子。"
                main_count += 1
                parts = [json.loads(part) for part in prompt.split("\n\n") if part.startswith('{"browser_request"')]
                self.assertEqual(len(parts), 1)
                receipt = parts[0]["browser_request"]
                self.assertEqual(receipt["url"], "https://www.kungal.com/")
                self.assertIn("id", receipt)
                self.assertNotIn("instruction", parts[0])
                self.assertIn("browser_request", _options["system_prompt"])
                from test_ai_fixture import prompt_object
                context = prompt_object(prompt, "console_context_file")["console_context_file"]
                execute("reply", context, text="我去逛逛。")
                await asyncio.wait_for(worker_entered.wait(), 2)
                await release.wait()
                execute("finish", context)
                return ""

            bot.ai.chat.side_effect = agent
            with patch("sandbox_runtime.SandboxRuntime.device", return_value=backend):
                received = asyncio.create_task(self.fixture.receive(bot, official, f"browse-{official}", "你去https://www.kungal.com/刷帖子吧"))
                await asyncio.wait_for(worker_entered.wait(), 3)
                self.assertFalse(received.done())
                release.set()
                await asyncio.wait_for(received, 3)
                await drain_runtime(bot)
            self.assertEqual(main_count, 1)
            self.assertEqual([params["url"] for action, params in backend.inputs if action == "navigate"], ["https://www.kungal.com/"])
            coordinator = bot._get_main_coordinator()
            with coordinator.broker.connect() as db:
                requests = db.execute("SELECT kind FROM requests WHERE namespace=?", (coordinator.namespace,)).fetchall()
            self.assertEqual(len([row for row in requests if row["kind"] == "sandbox_play"]), 1)
            with SandboxRuntime(bot.app_config.sandbox.directory).connect() as db:
                self.assertEqual(db.execute("SELECT state FROM sessions").fetchone()[0], "paused")
            # Both the paused reader and its reusable organizer stay available.
            bot.ai.archive_session.assert_not_awaited()

    async def test_browser_setup_failure_is_reported_as_a_concrete_obstacle(self):
        bot, _ = self.fixture.bot(stored_cid="existing")
        bot.app_config.bot.master_id = "456"
        bot.app_config.sandbox.browser_executable = str(Path("missing/chrome.exe").resolve())
        async def agent(_session, prompt, **_options):
            parts = [json.loads(part) for part in prompt.split("\n\n") if part.startswith('{"browser_request"')]
            self.assertEqual(parts[0]["browser_request"]["code"], "BROWSER_EXECUTABLE_INVALID")
            self.assertNotIn("instruction", parts[0])
            self.assertIn("含 error 则如实说明具体阻碍", _options["system_prompt"])
            return "默认浏览器路径不可用，需要改一下配置。"
        bot.ai.chat.side_effect = agent
        await self.fixture.receive(bot, False, "missing-browser", "你去https://www.kungal.com/刷帖子吧")


if __name__ == "__main__":
    unittest.main()
