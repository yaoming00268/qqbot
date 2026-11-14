"""Execute all regression tests in an isolated, network-disabled workspace."""
from pathlib import Path
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent
RUNNER = r'''
import asyncio, inspect, json, os, socket, subprocess, unittest
from pathlib import Path
from unittest.mock import patch

original_connect = socket.socket.connect
original_popen = subprocess.Popen
original_spawn = asyncio.create_subprocess_exec
validation_loop = asyncio.new_event_loop()
validation_loop_class = type(validation_loop)
validation_loop.close()

async def blocked_connection(*args, **kwargs):
    raise OSError("Validation: external network disabled")

def connect(sock, address):
    if any(f.function == "_fallback_socketpair" and f.filename.endswith("socket.py") for f in inspect.stack()):
        return original_connect(sock, address)
    raise OSError("Validation: external network disabled")

def popen(args, *a, **kw):
    if isinstance(args, (list, tuple)) and Path(args[0]).name.lower() in ("ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe") and not kw.get("shell"):
        return original_popen(args, *a, **kw)
    raise RuntimeError("Validation: external processes disabled except local ffmpeg fixtures")

async def spawn(*args, **kwargs):
    if Path(args[0]).name.lower() in ("ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe"):
        return await original_spawn(*args, **kwargs)
    raise RuntimeError("Validation: external process disabled")

with patch.object(socket.socket, "connect", new=connect), patch.object(socket.socket, "connect_ex", return_value=10061), patch.object(validation_loop_class, "sock_connect", new=blocked_connection), patch.object(asyncio.BaseEventLoop, "create_connection", new=blocked_connection), patch.object(subprocess, "Popen", new=popen), patch.object(asyncio, "create_subprocess_exec", new=spawn):
    import importlib
    suite = unittest.defaultTestLoader.discover(".", pattern="test_*.py")
    test_ids = set()
    def get_ids(s):
        for item in s:
            if isinstance(item, unittest.TestSuite):
                get_ids(item)
            else:
                test_ids.add(item.id().split(".")[-1])
    get_ids(suite)
    for f in Path(".").glob("test_*.py"):
        try:
            mod = importlib.import_module(f.stem)
            for attr in dir(mod):
                if attr.startswith("test_") and callable(getattr(mod, attr)) and attr not in test_ids and not isinstance(getattr(mod, attr), type):
                    suite.addTest(unittest.FunctionTestCase(getattr(mod, attr)))
                    test_ids.add(attr)
        except Exception:
            pass
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    Path("result.json").write_text(json.dumps({"tests_run":result.testsRun, "collected":suite.countTestCases(), "successful":result.wasSuccessful(),
        "failures":[{"test":str(t),"traceback":tb} for t,tb in result.failures],
        "errors":[{"test":str(t),"traceback":tb} for t,tb in result.errors],
        "skipped":[{"test":str(t),"reason":r} for t,r in result.skipped]}, ensure_ascii=False, indent=2), encoding="utf-8")
'''

PACKAGES = ("skills", "master", "ui", "console_tools")

def main():
    for path in ROOT.glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8-sig"), filename=path.name)
    for name in PACKAGES:
        pkg = ROOT / name
        if pkg.exists():
            for p in pkg.rglob("*.py"):
                ast.parse(p.read_text(encoding="utf-8-sig"), filename=str(p))
    with tempfile.TemporaryDirectory(prefix="qqbot_validation_") as tmp:
        stage = Path(tmp)
        for path in ROOT.glob("*.py"):
            shutil.copy2(path, stage / path.name)
        for name in PACKAGES:
            pkg = ROOT / name
            if pkg.exists():
                shutil.copytree(pkg, stage / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("personas.yaml", "requirements.txt", "README.md", "pytest.ini"):
            src = ROOT / name
            if src.exists():
                shutil.copy2(src, stage / name)
        if (ROOT / "data" / "slang_glossary.md").exists():
            (stage / "data").mkdir(exist_ok=True)
            shutil.copy2(ROOT / "data" / "slang_glossary.md", stage / "data" / "slang_glossary.md")
        (stage / "config.yaml").write_text("qq:\n  appid: review-app\n  secret: review-secret\nbot:\n  max_reply_length: 100\n  enable_action_descriptions: false\n  memory_enabled: true\n  search_enabled: false\nantigravity:\n  ls_address: 127.0.0.1:1\n  csrf_token: fixture-token\n", encoding="utf-8")
        (stage / "validate_runner.py").write_text(RUNNER, encoding="utf-8")
        env = os.environ.copy()
        for key in list(env.keys()):
            if key.startswith("ANTIGRAVITY_"):
                env.pop(key, None)
        env.update(QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        log_path = OUT / "tests.log"
        with open(log_path, "w", encoding="utf-8") as log_file:
            try:
                result = subprocess.run([sys.executable, "validate_runner.py"], cwd=stage, env=env,
                    stdout=log_file, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=180)
            except subprocess.TimeoutExpired as te:
                log_file.flush()
                print("validate_runner TIMED OUT after 180s!")
                if te.stdout:
                    print("STDOUT:", te.stdout[-2000:])
                raise
        result_file = stage / "result.json"
        if not result_file.exists():
            print("validate_runner failed! Return code:", result.returncode)
            if log_path.exists():
                print(log_path.read_text(encoding="utf-8", errors="replace")[-4000:])
            raise SystemExit(1)
        output = json.loads(result_file.read_text(encoding="utf-8"))
        (OUT / "test_results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"collected": output.get("collected", output["tests_run"]), "executed": output["tests_run"] - len(output["skipped"]),
            "tests_run": output["tests_run"], "successful": output["successful"],
            "failures":len(output["failures"]),"errors":len(output["errors"]),"skipped":len(output["skipped"])}, ensure_ascii=False))
        for failure in output["failures"] + output["errors"]:
            print(failure["test"])
            print(failure["traceback"][-4000:])
        if result.returncode:
            raise SystemExit(f"validate_runner exited abnormally: {result.returncode}; see {log_path}")
        if not output["successful"]:
            raise SystemExit(1)

if __name__ == "__main__":
    main()
