"""QQ source recovery uses actual OneBot responses and exact message scope."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import base64
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from qq_image_source import QQImageSourceResolver


def response(data):
    return {"status": "ok", "retcode": 0, "data": data}


class QQImageSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_image_source_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cache = self.root / "cache"
        self.original = self.root / "napcat" / "source.png"
        self.original.parent.mkdir()
        Image.new("RGB", (200, 100), "orange").save(self.original)
        self.action = AsyncMock(return_value=response({}))
        self.event = {"message_id": "101", "group_id": "123", "user_id": "456"}
        self.resolver = QQImageSourceResolver(self.action, self.event, self.cache)

    def assert_cached(self, result):
        path = Path(result["file"])
        self.assertTrue(path.is_relative_to(self.cache))
        self.assertTrue(path.is_file())
        with Image.open(path) as image:
            self.assertEqual(image.size, (200, 100))
        self.assertEqual(result["file"], result["local_path"])

    async def test_api_absolute_file_is_verified_and_copied_to_cache(self):
        self.action.return_value = response({"file": str(self.original), "url": "https://example.com/original.png"})
        result = await self.resolver.resolve({"file": "QQ-token", "summary": "original"})
        self.assert_cached(result)
        self.assertNotEqual(Path(result["file"]), self.original)
        self.assertEqual(result["source_url"], "https://example.com/original.png")
        self.action.assert_awaited_once_with("get_image", {"file": "QQ-token"}, timeout=8)

    async def test_api_local_path_in_url_is_also_imported(self):
        self.action.return_value = response({"url": str(self.original)})
        self.assert_cached(await self.resolver.resolve({"file": "token"}))

    async def test_http_file_tokens_keep_their_real_source_when_api_returns_local_file(self):
        self.action.return_value = response({"file": str(self.original)})
        result = await self.resolver.resolve({"file": "https://example.com/source.png"})
        self.assert_cached(result)
        self.assertEqual(result["source_url"], "https://example.com/source.png")

    async def test_api_bare_base64_and_base64_protocol_are_validated(self):
        encoded = base64.b64encode(self.original.read_bytes()).decode()
        for value in (encoded, "base64://" + encoded, "data:image/png;base64," + encoded):
            with self.subTest(prefix=value[:15]):
                self.action.return_value = response({"file": value})
                self.assert_cached(await self.resolver.resolve({"file": "token"}))
        self.action.return_value = response({"url": encoded})
        self.assert_cached(await self.resolver.resolve({"file": "token"}))

    async def test_raw_jpeg_payload_that_starts_with_a_slash_is_not_a_path(self):
        stream = BytesIO()
        Image.new("RGB", (200, 100), "orange").save(stream, format="JPEG")
        self.action.return_value = response({"url": base64.b64encode(stream.getvalue()).decode()})
        self.assert_cached(await self.resolver.resolve({"file": "token"}))

    async def test_api_http_url_is_returned_for_the_existing_downloader(self):
        self.action.return_value = response({"url": "https://example.com/fresh.png"})
        result = await self.resolver.resolve({"file": "token", "url": "https://example.com/expired.png"})
        self.assertEqual(result["url"], "https://example.com/fresh.png")
        self.assertEqual(result["source_url"], result["url"])
        self.assertFalse(result["file"])
        self.assertFalse(self.cache.exists())

    async def test_cached_files_are_reused_but_direct_external_paths_are_not_imported(self):
        result = await self.resolver.resolve({"file": str(self.original)})
        self.assertFalse(result["file"])
        self.assertFalse(self.cache.exists())
        self.action.return_value = response({"file": str(self.original)})
        imported = await self.resolver.resolve({"file": "token"})
        self.action.reset_mock()
        result = await self.resolver.resolve(imported)
        self.assert_cached(result)
        self.action.assert_not_awaited()

    async def test_group_message_refresh_reacquires_new_image_token(self):
        async def action(name, params, **options):
            if name == "get_msg":
                return response({"message_id": 101, "group_id": 123, "message_type": "group", "message": [
                    {"type": "image", "data": {"file": "fresh-token", "url": "https://example.com/new.png"}}]})
            return response({"file": str(self.original)}) if params["file"] == "fresh-token" else response({})
        self.action.side_effect = action
        result = await self.resolver.resolve({"file": "expired-token"})
        self.assert_cached(result)
        self.assertEqual([call.args[0] for call in self.action.await_args_list], ["get_image", "get_msg", "get_image"])
        self.assertEqual(self.action.await_args_list[-1].args[1], {"file": "fresh-token"})

    async def test_refresh_true_uses_get_msg_even_when_initial_api_url_is_valid(self):
        async def action(name, params, **options):
            if name == "get_msg":
                return response({"group_id": 123, "message": [{"type": "image", "data": {"file": "new"}}]})
            return response({"url": "https://example.com/" + params["file"] + ".png"})
        self.action.side_effect = action
        result = await self.resolver.resolve({"file": "old"}, refresh=True)
        self.assertEqual(result["url"], "https://example.com/new.png")
        self.assertEqual(self.action.await_count, 3)

    async def test_refresh_rejects_foreign_group_private_peer_and_message_id(self):
        cases = [(self.event, {"message_id": 101, "group_id": 999}),
                 (self.event, {"message_id": 102, "group_id": 123}),
                 ({"message_id": "101", "user_id": "456"}, {"message_id": 101, "user_id": 999}),
                 ({"message_id": "101", "user_id": "456"}, {"message_id": 101, "user_id": 456, "group_id": 123})]
        for event, fetched in cases:
            with self.subTest(event=event, fetched=fetched):
                fetched["message"] = [{"type": "image", "data": {"file": "foreign-image"}}]
                self.action.side_effect = [response({}), response(fetched)]
                self.action.reset_mock()
                result = await QQImageSourceResolver(self.action, event, self.cache).resolve({"file": "old"})
                self.assertFalse(result["file"])
                self.assertEqual(self.action.await_count, 2)

    async def test_private_message_can_identify_peer_in_standard_sender_field(self):
        self.action.side_effect = [response({}), response({"message_type": "private", "sender": {"user_id": 456},
            "message": [{"type": "image", "data": {"file": "new"}}]}), response({"file": str(self.original)})]
        result = await QQImageSourceResolver(self.action, {"message_id": 101, "user_id": 456}, self.cache).resolve({"file": "old"})
        self.assert_cached(result)

    async def test_matching_original_token_can_be_found_after_other_images(self):
        self.action.side_effect = [response({}), response({"group_id": 123, "message": [
            {"type": "image", "data": {"file": "other"}},
            {"type": "image", "data": {"file": "original", "url": "https://example.com/correct.png"}}]}), response({})]
        result = await self.resolver.resolve({"file": "original"}, index=0)
        self.assertEqual(result["url"], "https://example.com/correct.png")
        self.assertEqual(self.action.await_args_list[-1].args[1], {"file": "original"})

    async def test_out_of_bounds_index_never_uses_an_unrelated_first_image(self):
        self.action.side_effect = [response({}), response({"group_id": 123, "message": [
            {"type": "image", "data": {"file": "unrelated", "url": "https://example.com/wrong.png"}}]})]
        result = await self.resolver.resolve({"file": "missing"}, index=3)
        self.assertFalse(result["file"])
        self.assertFalse(result["url"])
        self.assertEqual(self.action.await_count, 2)

    async def test_mface_urls_and_raw_cq_refresh_keep_actual_source(self):
        self.action.side_effect = [response({"group_id": 123, "message": [], "raw_message":
            "[CQ:mface,summary=face,url=https://example.com/sticker.gif]"})]
        result = await self.resolver.resolve({"type": "mface", "summary": "face"})
        self.assertEqual(result["source_url"], "https://example.com/sticker.gif")
        self.assertEqual(result["subType"], "1")
        self.action.assert_awaited_once_with("get_msg", {"message_id": 101}, timeout=8)

    async def test_original_public_url_is_a_fallback_but_local_and_invalid_urls_are_not(self):
        result = await self.resolver.resolve({"url": "https://example.com/original.png"})
        self.assertEqual(result["source_url"], result["url"])
        for invalid in ("http://127.0.0.1/image.png", "file:///C:/private.png", "not-a-url"):
            self.assertFalse((await self.resolver.resolve({"url": invalid}))["url"])

    async def test_corrupt_relative_and_oversized_api_files_are_not_imported(self):
        corrupt = self.root / "corrupt.png"
        corrupt.write_text("not an image", encoding="utf-8")
        large = self.root / "large.png"
        large.write_bytes(self.original.read_bytes() + b"\0" * (20 * 1024 * 1024))
        for value in (str(corrupt), str(large), "relative.png"):
            self.action.return_value = response({"file": value})
            result = await self.resolver.resolve({"file": "token"})
            self.assertFalse(result["file"])
        self.action.return_value = response({"file": "base64://" + base64.b64encode(b"not an image").decode()})
        self.assertFalse((await self.resolver.resolve({"file": "token"}))["file"])
        self.assertFalse(list(self.cache.glob("*.part")))


if __name__ == "__main__":
    unittest.main()
