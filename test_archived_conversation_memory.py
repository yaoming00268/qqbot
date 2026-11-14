"""Offline restoration of deleted main conversations into scoped file memory."""
from __future__ import annotations

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import gzip
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from group_memory import GroupMemoryManager


CID = "1a831595-4664-4dbb-96f0-c75e163b8a01"
STAMP = "20261029_172829_672422d2"
BODY = '{"type":"USER_INPUT","content":"PRIVATE_TRANSCRIPT_SENTINEL"}\n'


class ArchivedConversationMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_restore_memory_")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name).resolve() / "memories"
        self.manager = GroupMemoryManager(str(self.directory))

    def archive(self, *, group="123", stamp=STAMP, **extra):
        path = self.directory / f"group_{group}_{stamp}.json"
        path.write_text(json.dumps({"group_openid": group, "summary": "saved summary", **extra}), encoding="utf-8")
        return path

    def conversation(self, archive, *, cid=CID, stem=None, session="onebot_group_123", body=BODY):
        directory = self.directory / "conversations"
        directory.mkdir(exist_ok=True)
        stem = stem or cid
        metadata = directory / f"{stem}.json"
        metadata.write_text(json.dumps({"conversation_id": cid, "session_key": session,
                                        "archive": str(archive.resolve()), "deleted_at": 123}), encoding="utf-8")
        compressed = directory / f"{stem}.jsonl.gz"
        with gzip.open(compressed, "wb") as stream:
            stream.write(body.encode("utf-8"))
        return metadata, compressed

    def output(self, *, group="123", stamp=STAMP, cid=CID):
        return self.directory / "packages" / f"group_{group}" / stamp / f"conversation_{cid}.jsonl"

    def raw_paths(self, manifest):
        return [Path(item["path"]) for item in manifest["files"] if item["role"] == "archive_messages"]

    def test_exact_archived_main_conversation_is_offered_as_optional_file_only(self):
        archive = self.archive()
        self.conversation(archive)
        manifest = self.manager.get_memory_file_manifest("123")
        output = self.output()
        self.assertEqual(self.raw_paths(manifest), [output])
        self.assertEqual(output.read_text(encoding="utf-8"), BODY)
        context = self.manager.get_memory_file_context("123")
        self.assertIn(output.as_posix(), context)
        self.assertNotIn("PRIVATE_TRANSCRIPT_SENTINEL", context)
        self.assertNotIn(BODY, json.dumps(manifest))
        self.assertEqual(manifest["files"][-1]["size"], len(BODY.encode("utf-8")))

    @unittest.skipUnless(os.name == "nt", "Windows archive paths are case insensitive")
    def test_windows_metadata_archive_path_can_use_different_letter_case(self):
        archive = self.archive()
        metadata, _ = self.conversation(archive)
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        payload["archive"] = str(archive).swapcase()
        self.assertNotEqual(payload["archive"], str(archive))
        self.assertTrue(Path(payload["archive"]).is_file())
        metadata.write_text(json.dumps(payload), encoding="utf-8")
        manifest = self.manager.get_memory_file_manifest("123")
        self.assertEqual(self.raw_paths(manifest), [self.output()])
        self.assertEqual(self.output().read_text(encoding="utf-8"), BODY)

    def test_group_archive_cid_and_history_must_all_match_before_decompression(self):
        selected = self.archive()
        older = self.archive(stamp="20261028_120000")
        other = self.archive(group="999")
        self.conversation(older, cid="older-fixture-123")
        self.conversation(other, cid="other-fixture-999", session="onebot_group_999")
        self.conversation(selected, cid="prefix-fixture-123", session="onebot_group_1234")
        self.conversation(selected, cid="summary-fixture-123", session="group_summary_123")
        self.conversation(selected, cid="mismatch-fixture-123", stem="different-fixture-123")
        self.conversation(selected, cid="short")
        with patch("group_memory.gzip.open", side_effect=AssertionError("unrelated transcript read")):
            self.assertEqual(self.raw_paths(self.manager.get_memory_file_manifest("123", history_count=1)), [])
            self.assertEqual(self.manager.get_memory_file_manifest("123", history_count=0)["files"], [])
        # The official QQ key is equally valid, without a OneBot-only rule.
        self.conversation(selected, cid="official-fixture-123", session="group_123")
        result = self.raw_paths(self.manager.get_memory_file_manifest("123", history_count=1))
        self.assertEqual(result, [self.output(cid="official-fixture-123")])
        self.assertFalse(self.output(group="999", cid="other-fixture-999").exists())

    def test_restoration_is_cached_across_calls_and_manager_restart(self):
        archive = self.archive()
        self.conversation(archive)
        first = self.manager.get_memory_file_manifest("123")
        before = self.output().stat()
        with patch("group_memory.gzip.open", side_effect=AssertionError("decompressed twice")):
            self.assertEqual(self.manager.get_memory_file_manifest("123"), first)
            restarted = GroupMemoryManager(str(self.directory))
            self.assertEqual(restarted.get_memory_file_manifest("123"), first)
        after = self.output().stat()
        self.assertEqual((before.st_mtime_ns, before.st_size), (after.st_mtime_ns, after.st_size))

    def test_changed_compressed_source_replaces_cache_atomically(self):
        archive = self.archive()
        _, compressed = self.conversation(archive)
        first = self.manager.get_memory_file_manifest("123")
        updated = BODY + '{"content":"another later turn"}\n'
        with gzip.open(compressed, "wb") as stream:
            stream.write(updated.encode("utf-8"))
        second = self.manager.get_memory_file_manifest("123")
        self.assertEqual(self.output().read_text(encoding="utf-8"), updated)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        # A damaged replacement cannot destroy the previously restored copy.
        compressed.write_bytes(b"not a valid gzip")
        invalid = self.manager.get_memory_file_manifest("123")
        self.assertEqual(self.raw_paths(invalid), [])
        self.assertEqual(self.output().read_text(encoding="utf-8"), updated)
        self.assertFalse(list(self.output().parent.glob("*.tmp")))

    def test_oversized_or_truncated_gzip_never_leaves_partial_raw_file(self):
        archive = self.archive()
        _, compressed = self.conversation(archive, body="x" * 1024)
        with patch.object(GroupMemoryManager, "MAX_ARCHIVED_CONVERSATION_BYTES", 128):
            self.assertEqual(self.raw_paths(self.manager.get_memory_file_manifest("123")), [])
        self.assertFalse(self.output().exists())
        compressed.write_bytes(gzip.compress(BODY.encode("utf-8"))[:-7])
        self.assertEqual(self.raw_paths(self.manager.get_memory_file_manifest("123")), [])
        self.assertFalse(self.output().exists())
        self.assertFalse(list(self.directory.rglob("*.tmp")))

    def test_untrusted_package_strings_and_metadata_archive_paths_cannot_escape(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        victim = outside / "victim.jsonl"
        victim.write_text("untouched", encoding="utf-8")
        archive = self.archive(package_dir=str(outside), messages_file=str(victim))
        metadata, _ = self.conversation(archive)
        self.manager.get_memory_file_manifest("123")
        self.assertEqual(victim.read_text(encoding="utf-8"), "untouched")
        self.assertEqual(self.output().read_text(encoding="utf-8"), BODY)
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        payload["archive"] = str(outside / archive.name)
        metadata.write_text(json.dumps(payload), encoding="utf-8")
        with patch("group_memory.gzip.open", side_effect=AssertionError("malicious metadata decompressed")):
            self.assertEqual(self.raw_paths(self.manager.get_memory_file_manifest("123")), [])
        self.assertEqual(victim.read_text(encoding="utf-8"), "untouched")

    def test_symlinked_output_cannot_overwrite_or_expose_another_path(self):
        archive = self.archive()
        self.conversation(archive)
        output = self.output()
        output.parent.mkdir(parents=True)
        victim = Path(self.tmp.name) / "unrelated.jsonl"
        victim.write_text("never overwrite", encoding="utf-8")
        try:
            output.symlink_to(victim)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with patch("group_memory.gzip.open", side_effect=AssertionError("unsafe target decompressed")):
            self.assertEqual(self.raw_paths(self.manager.get_memory_file_manifest("123")), [])
        self.assertTrue(output.is_symlink())
        self.assertEqual(victim.read_text(encoding="utf-8"), "never overwrite")


if __name__ == "__main__":
    unittest.main()
