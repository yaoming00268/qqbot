"""A portable release must be complete, unchanged, and free of home data."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from maintenance.package_runtime import EXECUTABLES, package, verify_package


class PortableDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        payloads = {name: b"frozen-fixture" for name in EXECUTABLES}
        payloads.update({"config.yaml.example": b'bot:\n  mode: "official"\nagent:\n  actors: {}\n',
                         "README.md": b"original runtime instructions",
                         "_internal/runtime.dll": b"dependency",
                         "_internal/rapidocr_onnxruntime/config.yaml": b"static OCR model settings"})
        rows = []
        for name, data in payloads.items():
            target = self.runtime / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            rows.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.manifest_path = self.runtime / "build-manifest.json"
        self.manifest = {"home_data_bundled": False, "files": rows}
        self.save_manifest()
        self.output = self.root / "portable.zip"

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def build(self):
        return package(self.runtime, self.output, "QQBot-test", b"portable instructions")

    def test_round_trip_preserves_binaries_and_separates_the_agent_template(self):
        result = self.build()
        manifest = verify_package(self.output)
        self.assertFalse(manifest["home_data_bundled"])
        self.assertEqual(result["sha256"], hashlib.sha256(self.output.read_bytes()).hexdigest())
        self.assertTrue(self.output.with_suffix(".zip.sha256").is_file())
        with zipfile.ZipFile(self.output) as saved:
            for name in EXECUTABLES:
                self.assertEqual(saved.read("QQBot-test/" + name), b"frozen-fixture")
            self.assertIn(b'"official"', saved.read("QQBot-test/config.yaml.example"))
            self.assertIn(b'"agent"', saved.read("QQBot-test/config.agent.yaml.example"))
            self.assertNotIn("QQBot-test/data/config.yaml", saved.namelist())

    def test_refuses_modified_binary(self):
        (self.runtime / "QQBot.exe").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_refuses_even_an_unlisted_home_file(self):
        (self.runtime / "config.yaml").write_text("secret: private", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unlisted"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_refuses_home_file_even_if_added_to_build_manifest(self):
        data = b"secret: private"
        (self.runtime / "config.yaml").write_bytes(data)
        self.manifest["files"].append({"path": "config.yaml", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "Not a runtime resource"):
            self.build()

    def test_refuses_manifest_path_escape_and_windows_case_collision(self):
        for name in ("../outside", "_internal/../outside", "_internal\\outside", "qqbot.exe"):
            with self.subTest(path=name):
                self.manifest["files"].append({**self.manifest["files"][0], "path": name})
                self.save_manifest()
                with self.assertRaises(ValueError):
                    self.build()
                self.manifest["files"].pop()

    def test_refuses_missing_role_and_existing_release(self):
        self.build()
        previous = self.output.read_bytes()
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(previous, self.output.read_bytes())
        self.output.unlink()
        (self.runtime / "QQBotMCP.exe").unlink()
        self.manifest["files"] = [row for row in self.manifest["files"] if row["path"] != "QQBotMCP.exe"]
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.build()

    def test_verifier_detects_tampering_unlisted_files_and_traversal(self):
        self.build()
        with zipfile.ZipFile(self.output) as source:
            payloads = {info.filename: source.read(info) for info in source.infolist()}
        for bad_name in ("QQBot-test/QQBot.exe", "QQBot-test/data/cookies.json", "QQBot-test/../escape"):
            with self.subTest(path=bad_name):
                changed = {**payloads, bad_name: b"tampered"}
                altered = self.root / "altered.zip"
                with zipfile.ZipFile(altered, "w") as saved:
                    for name, data in changed.items():
                        saved.writestr(name, data)
                with self.assertRaises(ValueError):
                    verify_package(altered)


if __name__ == "__main__":
    unittest.main()
