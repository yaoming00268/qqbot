"""Offline checks for local acquisition, identity binding and download fallback."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from PIL import Image

from media_downloader import MediaError
from qq_window_capture import AccessibleNode as Node, QQCaptureService, QQCaptureTarget, QQWindow, resolve_accessible_target


class AlgorithmicCaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="qqbot_algorithm_capture_")
        self.previous = os.getcwd()
        os.chdir(self.temporary.name)
        self.window = QQWindow(1, 7, "测试群 (123)", (100, 200, 900, 800))
        self.request = {"scope": "onebot:default:group:123", "account_id": "999", "conversation_id": "123",
            "conversation_name": "测试群", "message_id": "m1", "sender_name": "Alice", "text": "看看这个",
            "timestamp": time.time(), "time_display": time.strftime("%Y-%m-%d %H:%M:%S")}

    def tearDown(self):
        os.chdir(self.previous)
        self.temporary.cleanup()

    def tree(self, *, account="999", group="123", message_id="m1", repeated=False):
        message = Node(automation_id="message_id:" + message_id if message_id else "", control_type="ListItemControl",
            rect=(300, 300, 800, 600), children=(Node(name="Alice"),
            Node(name=self.request["time_display"][-8:]), Node(name="看看这个"),
            Node(name="图片", control_type="ImageControl", rect=(350, 340, 750, 550))))
        return Node(children=(Node(name="当前账号：" + account), Node(name="群号：" + group), message) + ((message,) if repeated else ()))

    def resolve(self, tree=None, *, item=None, windows=None, snapshots=None, request=None):
        return resolve_accessible_target(windows or [self.window], snapshots or {1: tree or self.tree()},
            request or self.request, item if item is not None else {"subType": "1"})

    def test_accessibility_account_group_and_message_produce_precise_crop(self):
        target = self.resolve()
        self.assertEqual(target.box, (200, 100, 700, 400))
        self.assertEqual(target.account_id, "999")
        self.assertEqual(target.conversation_id, "123")
        self.assertEqual(target.message_id, "m1")
        self.assertEqual(target.mode, "qq_chat_screenshot")

    def test_visible_sender_time_body_can_bind_without_internal_message_id(self):
        target = self.resolve(self.tree(message_id=""))
        self.assertEqual(target.evidence, "unique_recent_sender_time_text")
        self.assertEqual(target.message_id, "m1")

    def test_wrong_account_wrong_group_wrong_message_and_duplicates_fail(self):
        for tree in (self.tree(account="888"), self.tree(group="456"), self.tree(message_id="m2"),
                     self.tree(repeated=True), self.tree(message_id="", repeated=True)):
            # A different group title must not rescue a mismatched group ID.
            window = QQWindow(1, 7, "QQ", self.window.rect)
            with self.assertRaises(MediaError):
                self.resolve(tree, windows=[window])

    def test_conflicting_visible_group_identity_overrides_stale_window_title(self):
        with self.assertRaises(MediaError):
            self.resolve(self.tree(group="456"))
        tree = self.tree()
        ambiguous = Node(children=tree.children + (Node(name="当前账号：888"),))
        with self.assertRaises(MediaError):
            self.resolve(ambiguous)

    def test_stale_quote_needs_real_message_id(self):
        request = {**self.request, "timestamp": time.time() - 600}
        with self.assertRaises(MediaError):
            self.resolve(self.tree(message_id=""), request=request)
        self.assertEqual(self.resolve(request=request).message_id, "m1")

    def test_message_text_cannot_impersonate_login_metadata(self):
        content = self.tree().children[-1]
        spoof = Node(children=(Node(control_type="ListItemControl", rect=content.rect,
            children=(Node(name="当前账号：999"), Node(name="群号：123"), content)),))
        with self.assertRaises(MediaError):
            self.resolve(spoof)

    def test_group_name_alone_or_console_process_never_qualifies(self):
        tree = self.tree()
        no_id = Node(children=(tree.children[0], tree.children[-1]))
        window = QQWindow(1, 7, "测试群", self.window.rect)
        with self.assertRaises(MediaError):
            self.resolve(no_id, windows=[window])
        console = QQWindow(1, 7, "测试群 (123)", self.window.rect, executable="python.exe")
        with self.assertRaises(MediaError):
            self.resolve(windows=[console])

    def test_ordinary_image_needs_owned_viewer_linked_to_original_source(self):
        with self.assertRaises(MediaError):
            self.resolve(item={"file": "original.png"})
        viewer = QQWindow(2, 7, "QQ 图片查看器", (150, 250, 950, 850), owner=1)
        image = Node(name="original.png", control_type="ImageControl", rect=(200, 300, 900, 800))
        snapshots = {1: self.tree(), 2: Node(children=(image,))}
        target = self.resolve(item={"file": "original.png"}, windows=[self.window, viewer], snapshots=snapshots)
        self.assertEqual(target.window.hwnd, 2)
        self.assertEqual(target.mode, "qq_image_viewer_screenshot")
        for other in (QQWindow(2, 8, viewer.title, viewer.rect, owner=1),
                      QQWindow(2, 7, viewer.title, viewer.rect, owner=99)):
            with self.assertRaises(MediaError):
                self.resolve(item={"file": "original.png"}, windows=[self.window, other], snapshots=snapshots)
        with self.assertRaises(MediaError):
            self.resolve(item={"file": "other.png"}, windows=[self.window, viewer], snapshots=snapshots)

    async def test_local_capture_finishes_file_before_agent_is_involved(self):
        target = self.resolve()
        backend = MagicMock()
        backend.locate_target.return_value = target
        backend.capture.return_value = Image.new("RGB", (800, 600), "orange")
        ai = MagicMock(chat=AsyncMock(side_effect=AssertionError("acquisition cannot invoke model")))
        result = await QQCaptureService(ai, backend=backend).capture_message(self.request, {"subType": "1"})
        path = Path(result["file"])
        self.assertTrue(path.is_file())
        with Image.open(path) as image:
            self.assertEqual(image.size, (500, 300))
        metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["account_id"], "999")
        self.assertEqual(metadata["acquisition"], "qqbot_local_algorithm")
        ai.chat.assert_not_awaited()
        backend.click.assert_not_called()
        backend.search_digits.assert_not_called()

    async def test_chat_switch_during_capture_discards_image(self):
        target = self.resolve()
        changed = QQCaptureTarget(target.window, target.box, "999", "456", "group", "m1", 0,
                                  "qq_chat_screenshot", target.evidence)
        backend = MagicMock()
        backend.locate_target.side_effect = [target, changed]
        backend.capture.return_value = Image.new("RGB", (800, 600), "orange")
        with self.assertRaises(MediaError):
            await QQCaptureService(backend=backend).capture_message(self.request, {"subType": "1"})
        self.assertFalse(Path("data/cache/images/qq_capture").exists())


if __name__ == "__main__":
    unittest.main()
