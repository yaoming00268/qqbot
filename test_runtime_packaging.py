"""Packaging boundaries, live glossary reload and historical archive safety."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from maintenance.history_cleanup import compact, checked_target, restore_archive, verify_archive
from runtime_paths import application_home, module_command, resource_path
from slang_glossary import SlangGlossary


class RuntimeModulesTests(unittest.TestCase):
    def test_source_commands_keep_python_and_module_paths(self):
        import sys
        self.assertEqual(module_command("cli"), [sys.executable, str(resource_path("console_tools/cli.py"))])
        self.assertEqual(module_command("core")[1:4], ["-X", "utf8", "-u"])

    def test_frozen_gui_agent_and_cli_all_use_sibling_exes_and_the_same_home(self):
        import sys
        from main_agent_policy import main_cascade_config, subagent_cascade_config
        from console_tools.contracts import invocation
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for name in ("QQBot.exe", "QQBotStudio.exe", "QQBotCLI.exe", "QQBotMCP.exe"):
                (root / name).touch()
            data = root / "data directory"
            data.mkdir()
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(root / "QQBotStudio.exe")), \
                    patch.object(sys, "_MEIPASS", str(root / "_internal"), create=True), patch.dict(os.environ, {"QQBOT_HOME": str(data)}):
                self.assertEqual(application_home(), data)
                self.assertEqual(resource_path("renpy_bridge_agent.py"), root / "_internal/renpy_bridge_agent.py")
                self.assertEqual(module_command("core"), [str(root / "QQBot.exe"), "--home", str(data)])
                for config in (main_cascade_config(), subagent_cascade_config()):
                    server = config["plannerConfig"]["customizationConfig"]["mcpServers"][0]
                    self.assertEqual(server["command"], str(root / "QQBotMCP.exe"))
                    self.assertEqual(server["args"], ["--home", str(data)])
                argv = invocation("tasks", context_file="current.json")["argv"]
                self.assertEqual(argv[:4], [str(root / "QQBotCLI.exe"), "--home", str(data), "tasks"])
                (root / "QQBotMCP.exe").unlink()
                with self.assertRaises(FileNotFoundError):
                    module_command("mcp")


class GlossaryCacheTests(unittest.TestCase):
    def test_cached_results_cannot_be_mutated_and_external_replace_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.md"
            glossary = SlangGlossary(path)
            glossary.record("OP", meaning="原解释")
            with patch.object(glossary, "_parse", wraps=glossary._parse) as parser:
                glossary.entries().clear()
                self.assertEqual(glossary.match("op!")[0].meaning, "原解释")
                self.assertFalse(glossary.match("open"))
                self.assertEqual(parser.call_count, 1)
                replacement = path.with_suffix(".new")
                replacement.write_text(path.read_text(encoding="utf-8").replace("原解释", "新解释"), encoding="utf-8")
                original_mtime = path.stat().st_mtime_ns
                replacement.replace(path)
                os.utime(path, ns=(original_mtime, original_mtime))
                self.assertEqual(glossary.match("op!")[0].meaning, "新解释")
                self.assertEqual(parser.call_count, 2)
            path.write_text("invalid markdown", encoding="utf-8")
            with self.assertRaises(ValueError):
                glossary.entries()
            path.unlink()
            self.assertEqual(glossary.entries(), [])

    def test_other_instance_updates_are_seen_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "words.md"
            reader, writer = SlangGlossary(path), SlangGlossary(path)
            self.assertEqual(reader.entries(), [])
            writer.record("测试黑话", meaning="已确认")
            self.assertEqual(reader.match("这是测试黑话")[0].meaning, "已确认")


class HistoryCleanupTests(unittest.TestCase):
    def test_deduplicates_then_removes_only_archived_generated_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("review_artifacts/case/validation_01", "implementation_artifacts/case/live_012abc", "review_artifacts/case/before"):
                path = root / name
                path.mkdir(parents=True)
                (path / "evidence.txt").write_bytes(b"same content")
            (root / "config.yaml").write_bytes(b"keep configuration")
            archive = root / "snapshots.zip"
            preview = compact(root, archive)
            self.assertEqual(preview["bytes"], 24)
            result = compact(root, archive, apply=True)
            self.assertTrue(result["verified"])
            self.assertEqual(result["files"], 2)
            self.assertEqual(result["unique_objects"], 1)
            self.assertEqual((root / "config.yaml").read_bytes(), b"keep configuration")
            self.assertTrue((root / "review_artifacts/case/before/evidence.txt").exists())
            manifest = verify_archive(archive)
            with zipfile.ZipFile(archive) as saved:
                self.assertEqual(saved.read("objects/" + manifest["files"][0]["sha256"]), b"same content")
            restored = root / "restore"
            receipt = restore_archive(archive, restored, [manifest["targets"][0]])
            self.assertEqual(receipt["files"], 1)
            self.assertEqual((restored / manifest["files"][0]["path"]).read_bytes(), b"same content")
            with self.assertRaises(FileExistsError):
                restore_archive(archive, restored)

    def test_refuses_unsafe_targets_and_an_existing_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for path in (root, root / "data", root / "review_artifacts/case/before", root / "../outside/validation"):
                with self.assertRaises(ValueError):
                    checked_target(root, path)
            (root / "review_artifacts/case").mkdir(parents=True)
            (root / "implementation_artifacts").mkdir()
            archive = root / "archive.zip"
            archive.write_bytes(b"existing evidence")
            with self.assertRaises(FileExistsError):
                compact(root, archive, apply=True)
            self.assertEqual(archive.read_bytes(), b"existing evidence")


if __name__ == "__main__":
    unittest.main()
