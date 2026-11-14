"""File-only group memory manifests, versioning and strict path isolation."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from group_memory import GroupMemoryManager


class MemoryFileContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_memory_manifest_")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "memories"
        self.manager = GroupMemoryManager(str(self.directory))

    def write(self, filename, group="123", **extra):
        path = self.directory / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"group_openid": group, **extra}, ensure_ascii=False), encoding="utf-8")
        return path

    def test_context_contains_absolute_paths_instead_of_memory_contents(self):
        current = self.write("group_123.json", summary="禁止拼进上下文的摘要秘密", permanent_preset="另一个永久预设秘密")
        archive = self.write("group_123_20261029_120000_abcdef01.json", summary="不应拼进上下文的过往记忆")
        with patch.object(self.manager, "get_group_memory", side_effect=AssertionError("loaded whole memory")), \
             patch.object(self.manager, "get_permanent_preset", side_effect=AssertionError("generated profile")), \
             patch.object(self.manager, "load_recent_memories", side_effect=AssertionError("loaded archives")), \
             patch("pathlib.Path.read_text", side_effect=AssertionError("read whole file")), \
             patch("group_memory.json.load", side_effect=AssertionError("parsed whole file")):
            context = self.manager.get_memory_file_context("123", current_user_openid="456")
        self.assertIn(current.resolve().as_posix(), context)
        self.assertIn(archive.resolve().as_posix(), context)
        self.assertNotIn("摘要秘密", context)
        self.assertNotIn("预设秘密", context)
        self.assertNotIn("过往记忆", context)
        self.assertIn('"current_user_openid":"456"', context)
        self.assertEqual(self.manager._cache, {})
        self.assertEqual(self.manager._recent_memories_cache, {})

    def test_only_the_requested_group_and_exact_archive_names_are_listed(self):
        self.write("group_123.json")
        self.write("group_123_20261029_120000.json")
        self.write("group_1234.json", group="1234")
        self.write("group_123_456_20261029_120000.json", group="123_456")
        self.write("group_123_20261029_120001.json", group="other")
        # A date-shaped group ID's current file collides with an archive name.
        self.write("group_123_20261029_120002.json", group="123_20261029_120002")
        self.write("group_123_20261029_120003.json.tmp", summary="not committed")
        paths = [Path(item["path"]).name for item in self.manager.get_memory_file_manifest("123")["files"]]
        self.assertEqual(paths, ["group_123.json", "group_123_20261029_120000.json"])

    def test_history_count_limits_archives_and_does_not_read_older_identities(self):
        for index in range(1, 8):
            self.write(f"group_123_20261029_12000{index}.json", summary="X" * 100000)
        with patch.object(self.manager, "_memory_file_owner", wraps=self.manager._memory_file_owner) as identity:
            manifest = self.manager.get_memory_file_manifest("123", history_count=2)
        self.assertEqual(identity.call_count, 2)
        self.assertEqual([Path(item["path"]).name for item in manifest["files"]],
                         ["group_123_20261029_120007.json", "group_123_20261029_120006.json"])
        self.assertEqual(self.manager.get_memory_file_manifest("123", history_count=0)["files"], [])

    def test_existing_package_paths_are_derived_and_untrusted_json_paths_are_ignored(self):
        stamp = "20261029_120000_abcdef01"
        self.write(f"group_123_{stamp}.json", package_dir=str(self.directory / "packages/group_999"),
                   messages_file=str(self.directory / "packages/group_999/private.jsonl"))
        package = self.directory / "packages" / "group_123" / stamp
        package.mkdir(parents=True)
        summary = package / "agent_summary.json"
        messages = package / "messages.jsonl"
        summary.write_text('{"summary":"agent should read directly"}', encoding="utf-8")
        messages.write_text('{"content":"raw message"}\n', encoding="utf-8")
        manifest = self.manager.get_memory_file_manifest("123")
        self.assertEqual([item["role"] for item in manifest["files"]], ["archive", "archive_summary", "archive_messages"])
        self.assertEqual([item["path"] for item in manifest["files"]][1:], [summary.resolve().as_posix(), messages.resolve().as_posix()])
        self.assertNotIn("group_999", json.dumps(manifest))

    def test_stat_versions_change_on_file_updates_addition_and_deletion(self):
        path = self.write("group_123.json", summary="first")
        first = self.manager.get_memory_file_manifest("123")
        self.assertEqual(first, self.manager.get_memory_file_manifest("123"))
        path.write_text('{"group_openid":"123","summary":"changed and longer"}', encoding="utf-8")
        second = self.manager.get_memory_file_manifest("123")
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["files"][0]["version"], second["files"][0]["version"])
        archive = self.write("group_123_20261029_120000.json")
        third = self.manager.get_memory_file_manifest("123")
        self.assertNotEqual(second["fingerprint"], third["fingerprint"])
        archive.unlink()
        self.assertEqual(second, self.manager.get_memory_file_manifest("123"))

    def test_group_ids_reject_traversal_and_lossy_sanitization(self):
        for group_id in ("", "../123", "..\\123", "123/456", "123\\456", "12:3", " 123", "123 ", "123*"):
            with self.subTest(group_id=group_id), self.assertRaises(ValueError):
                self.manager.get_memory_file_context(group_id)
        self.write("group_grp_official_test.json", group="grp_official_test")
        self.assertTrue(self.manager.get_memory_file_context("grp_official_test"))

    def test_symlinked_current_or_package_files_cannot_cross_groups(self):
        other = self.write("group_999.json", group="999")
        current = self.directory / "group_123.json"
        try:
            current.symlink_to(other)
        except OSError:
            self.skipTest("symlink creation unavailable")
        self.assertEqual(self.manager.get_memory_file_manifest("123")["files"], [])
        current.unlink()
        stamp = "20261029_120000"
        self.write(f"group_123_{stamp}.json")
        package = self.directory / "packages" / "group_123" / stamp
        package.mkdir(parents=True)
        (package / "messages.jsonl").symlink_to(other)
        manifest = self.manager.get_memory_file_manifest("123")
        self.assertEqual([item["role"] for item in manifest["files"]], ["archive"])

    def test_missing_files_do_not_create_placeholder_memories(self):
        self.assertEqual(self.manager.get_memory_file_context("123"), "")
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.manager._cache, {})

    def test_explicit_memory_reader_keeps_legacy_output(self):
        self.write("group_123.json", summary="用户主动查看的记忆", permanent_preset="用户保存的预设")
        self.assertIn("用户主动查看的记忆", self.manager.get_memory_prompt("123"))
        self.assertEqual(self.manager.get_permanent_preset("123"), "用户保存的预设")

    def test_unverified_and_truncated_files_are_not_exposed(self):
        for name, content in (("group_123.json", "corrupt"),
                              ("group_123_20261029_120000.json", '{"summary":"unknown owner"}'),
                              ("group_123_20261029_120001.json", '{"group_openid":"wrong"}')):
            (self.directory / name).write_text(content, encoding="utf-8")
        self.assertEqual(self.manager.get_memory_file_manifest("123")["files"], [])


if __name__ == "__main__":
    unittest.main()
