"""Disposable pytest checkout, live-data protection and test-owned networking."""
from __future__ import annotations

import atexit
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import weakref
from contextlib import contextmanager


class TestIsolationError(PermissionError):
    """An offline test tried to access a live runtime resource."""
    __test__ = False


_state = None
_browser_services = []


@contextmanager
def playwright_fixture_driver():
    """Enable only CDP connections to already registered disposable browsers."""
    from unittest.mock import patch
    if not _state:
        raise TestIsolationError("Playwright fixture driver requires offline test guards")
    ports = []
    for reference, profile in _browser_services:
        process = reference()
        if process and process.poll() is None and (profile / "DevToolsActivePort").is_file():
            ports.append(int((profile / "DevToolsActivePort").read_text().splitlines()[0]))
    if not ports:
        raise TestIsolationError("Register a disposable headless browser before using Playwright")
    guard = Path(__file__).resolve().parent / "playwright_fixture_guard.cjs"
    with patch.dict(os.environ, {"NODE_OPTIONS": '--require="' + guard.as_posix() + '"',
            "QQBOT_PLAYWRIGHT_TEST_PORTS": json.dumps(sorted(ports))}):
        yield


def bootstrap_test_module(name, path):
    """Direct test files use pytest's sandbox; bare unittest fails before fixtures."""
    root = Path(__file__).resolve().parent
    if is_sandbox(root):
        return
    if name == "__main__":
        raise SystemExit(run_isolated([str(Path(path).resolve()), *sys.argv[1:]], Path.cwd()))
    # unittest changes argv[0] to the display label 'python -m unittest'.
    # Reject every unguarded test import instead of guessing the runner's path;
    # this also covers programmatic unittest discovery from a Python snippet.
    raise TestIsolationError("Use python -m pytest: bare unittest does not install the offline test sandbox; "
                             "test modules cannot be imported outside the disposable checkout")


def command_argv(command):
    """Decode audit's Windows command line without interpreting shell syntax."""
    if isinstance(command, (tuple, list)):
        return [os.fsdecode(part) for part in command]
    if os.name != "nt":
        return shlex.split(command)
    import ctypes
    from ctypes import wintypes
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not pointer:
        raise TestIsolationError("invalid child process command line")
    try:
        return [pointer[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(pointer)


def is_sandbox(root):
    marker = os.environ.get("QQBOT_TEST_SANDBOX", "")
    return bool(marker and Path(marker).resolve() == Path(root).resolve()
                and (Path(root) / ".qqbot-test-sandbox").is_file())


def own_browser_service(process, profile):
    """Trust only a newly launched headless test browser's ephemeral CDP port."""
    root = Path(__file__).resolve().parent
    if not is_sandbox(root) or process.poll() is not None:
        raise TestIsolationError("test browser must be an owned live subprocess")
    profile = Path(profile).resolve()
    args = [str(arg) for arg in process.args]
    supplied_profile = next((Path(arg.split("=", 1)[1]).resolve()
        for arg in args if arg.startswith("--user-data-dir=")), None)
    if (Path(args[0]).name.lower() not in {"chrome.exe", "msedge.exe", "chrome", "chromium", "chromium.exe"}
            or not any(arg.startswith("--headless") for arg in args)
            or "--remote-debugging-port=0" not in args
            or "--remote-debugging-address=127.0.0.1" not in args
            or supplied_profile != profile
            or not profile.is_relative_to(Path(tempfile.gettempdir()).resolve())
            or profile.is_relative_to(Path(os.environ["QQBOT_TEST_SOURCE_ROOT"]).resolve())):
        raise TestIsolationError("only disposable headless browser profiles may expose test ports")
    _browser_services.append((weakref.ref(process), profile))


def _copy_checkout(source, target):
    # Copy code and static test inputs, never live accounts, cookies, databases,
    # cached images, model transcripts or earlier test outputs.
    for path in source.iterdir():
        if path.is_file() and (path.suffix in {".py", ".md"} or path.name in
                {"pytest.ini", "requirements.txt", "config.yaml.example", "pyproject.toml", "setup.cfg"}):
            shutil.copy2(path, target / path.name)
    for name in ("agent_core", "console_tools", "skills", "master", "ui", "browser_extensions", "maintenance",
                 "emotion_studio/emotionlab"):
        if (source / name).is_dir():
            shutil.copytree(source / name, target / name, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "node_modules", ".git", "data", "*.sqlite3", "*.log"))
    for name in ("implementation_artifacts/validate.py",
                 "playwright_fixture_guard.cjs",
                 "implementation_artifacts/gemini_browser_setup/extension_smoke.cjs",
                 "data/slang_glossary.md"):
        path = source / name
        if path.is_file():
            copy = target / name
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copy)
    (target / ".qqbot-test-sandbox").write_text("offline pytest checkout\n", encoding="utf-8")
    (target / "sitecustomize.py").write_text(
        "import importlib.machinery, pathlib, runpy, sys\n"
        "_here = pathlib.Path(__file__).resolve().parent\n"
        "_paths = [p for p in sys.path if pathlib.Path(p or '.').resolve() != _here]\n"
        "_original = importlib.machinery.PathFinder.find_spec('sitecustomize', _paths)\n"
        "if _original and _original.origin:\n    runpy.run_path(_original.origin)\n"
        "from testing_sandbox import install_guards\ninstall_guards()\n", encoding="utf-8")


def _arguments(args, source, target, invocation):
    output, allowed = [], []
    index = 0
    while index < len(args):
        arg = str(args[index])
        if arg in ("--junitxml", "--junit-xml", "--log-file"):
            path = (invocation / str(args[index + 1])).resolve()
            output.extend([arg, str(path)])
            allowed.append(str(path))
            index += 2
            continue
        if any(arg.startswith(prefix) for prefix in ("--junitxml=", "--junit-xml=", "--log-file=")):
            flag, value = arg.split("=", 1)
            path = (invocation / value).resolve()
            output.append(flag + "=" + str(path))
            allowed.append(str(path))
        elif not arg.startswith("-"):
            name, sep, selector = arg.partition("::")
            path = (invocation / name).resolve()
            if path.is_relative_to(source) and path.exists():
                arg = str(target / path.relative_to(source)) + (sep + selector if sep else "")
            output.append(arg)
        else:
            output.append(arg)
        index += 1
    return output, allowed


def run_isolated(args, invocation):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    source = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="qqbot-pytest-") as folder:
        target = Path(folder).resolve()
        _copy_checkout(source, target)
        rewritten, allowed = _arguments(list(args), source, target, Path(invocation).resolve())
        env = dict(os.environ)
        for name in list(env):
            if name.startswith("ANTIGRAVITY_") or name.endswith(("_API_KEY", "_ACCESS_TOKEN")):
                env.pop(name, None)
        env.pop("PYTEST_CURRENT_TEST", None)
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(name, None)
        env.update(QQBOT_TEST_SANDBOX=str(target), QQBOT_TEST_SOURCE_ROOT=str(source),
                   QQBOT_TEST_OUTPUT_FILES=json.dumps(allowed), PYTHONIOENCODING="utf-8",
                   QT_QPA_PLATFORM="offscreen")
        # Keep the disposable modules ahead of any user-set workspace PYTHONPATH.
        env["PYTHONPATH"] = str(target)
        with subprocess.Popen([sys.executable, "-m", "pytest", *rewritten], cwd=target, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
            returncode = process.wait()
        audit_dir = os.environ.get("QQBOT_TEST_AUDIT_DIR")
        audit = target / ".qqbot-test-audit.json"
        if audit_dir and audit.is_file():
            directory = Path(audit_dir).resolve()
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(audit, directory / (target.name + ".json"))
        return returncode


def install_guards():
    global _state
    if _state is not None:
        return _state
    root = Path(__file__).resolve().parent
    if not is_sandbox(root):
        raise TestIsolationError("pytest must run in a disposable checkout")
    source = Path(os.environ["QQBOT_TEST_SOURCE_ROOT"]).resolve()
    allowed_outputs = {Path(name).resolve() for name in json.loads(os.environ.get("QQBOT_TEST_OUTPUT_FILES", "[]"))}
    state = {"sandbox": str(root), "source": str(source), "blocked": [], "blocked_count": 0,
             "owned_connections": 0}
    _state = state
    owners, lock = weakref.WeakKeyDictionary(), threading.RLock()

    def reject(reason):
        state["blocked_count"] += 1
        if len(state["blocked"]) < 100:
            state["blocked"].append({"reason": reason, "test": os.environ.get("PYTEST_CURRENT_TEST", "")})
        raise TestIsolationError("offline test isolation: " + reason)

    def path_value(value):
        if isinstance(value, (str, bytes, os.PathLike)):
            return Path(os.fsdecode(value)).resolve()
        return None

    def check_file(value, write=False, mkdir=False):
        path = path_value(value)
        if path is None:
            return
        # The child has its own __file__ paths as well as its own working dir.
        # This also catches an old fixture with a hard-coded production path.
        if path.is_relative_to(source):
            sensitive = path.is_relative_to(source / "data") or path.name in {"config.yaml", "config.yaml.backup"}
            if path in allowed_outputs and write:
                return
            if mkdir and any(output.is_relative_to(path) for output in allowed_outputs):
                return
            if write or sensitive:
                reject("live workspace files are unavailable to tests")
        if path.is_relative_to(Path.home() / ".gemini" / "antigravity"):
            reject("live Antigravity account files are unavailable to tests")

    original_bind = socket.socket.bind

    def bind(sock, address):
        result = original_bind(sock, address)
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            with lock:
                owners[sock] = sock.getsockname()[1]
        return result

    socket.socket.bind = bind

    def loopback(host):
        if host in ("localhost", b"localhost", None, ""):
            return True
        try:
            return ipaddress.ip_address(os.fsdecode(host).split("%", 1)[0]).is_loopback
        except ValueError:
            return False

    def owned_ports():
        with lock:
            ports = {port for listener, port in list(owners.items()) if listener.fileno() >= 0}
        return ports

    def check_connection(sock, address):
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return
        owned = owned_ports()
        for ref, profile in list(_browser_services):
            process = ref()
            if process is None or process.poll() is not None:
                continue
            try:
                port = int((profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0])
                owned.add(port)
            except (OSError, ValueError, IndexError):
                pass
        if not loopback(address[0]) or address[1] not in owned:
            reject("connections must target a local server owned by this test process")
        state["owned_connections"] += 1

    if sys.platform == "win32":
        # ConnectEx bypasses socket.connect's audit event on the default loop.
        # Check before submitting overlapped IO, keeping Proactor subprocesses.
        from asyncio.windows_events import IocpProactor
        original_connect = IocpProactor.connect

        def proactor_connect(proactor, sock, address):
            check_connection(sock, address)
            return original_connect(proactor, sock, address)

        IocpProactor.connect = proactor_connect

    trusted_programs = {Path(found).resolve(): name for name in
        ("ffmpeg", "ffprobe", "node", "pwsh", "powershell") if (found := shutil.which(name))}
    interpreter = Path(sys.executable).resolve()

    def child_environment(env):
        values = {key.upper(): value for key, value in (os.environ if env is None else env).items()}
        if (values.get("PYTHONPATH") != str(root) or values.get("QQBOT_TEST_SANDBOX") != str(root)
                or values.get("QQBOT_TEST_SOURCE_ROOT") != str(source) or values.get("PYTHONHOME")):
            reject("child Python must inherit the disposable checkout and guards")
        try:
            outputs = {Path(value).resolve() for value in json.loads(values.get("QQBOT_TEST_OUTPUT_FILES", "[]"))}
        except (ValueError, TypeError):
            reject("invalid child test output paths")
        if not outputs <= allowed_outputs:
            reject("child processes cannot widen test output permissions")

    def python_arguments(parts, cwd, env):
        child_environment(env)
        arguments = list(parts[1:])
        while arguments and arguments[0] in ("-u", "-B"):
            arguments.pop(0)
        if not arguments:
            reject("interactive child Python is disabled")
        if arguments[0] == "-c" and len(arguments) >= 2:
            return
        if arguments[0] == "-m" and len(arguments) >= 2 and arguments[1] in ("console_tools", "pytest", "unittest"):
            return
        if arguments[0].startswith("-"):
            reject("child Python flags or modules may not bypass test startup guards")
        script = (Path(cwd or Path.cwd()) / arguments[0]).resolve()
        if not script.is_relative_to(root) or script.name.lower() in ("main.py", "gui.py") or script.suffix != ".py":
            reject("child Python scripts must be test checkout entry points")

    def check_process(event_args):
        executable, command, cwd, env = event_args
        parts = command_argv(command)
        if not parts:
            reject("empty subprocess command")
        actual = os.fsdecode(executable) if executable is not None else parts[0]
        found = shutil.which(actual) if not Path(actual).is_absolute() else actual
        program = Path(found or actual).resolve()
        if program == interpreter:
            python_arguments(parts, cwd, env)
            return
        if program.name.lower() == "node.exe" and program.parent.name == "driver" and program.parent.parent.name == "playwright":
            values = {key.upper(): value for key, value in (env or os.environ).items()}
            guard = root / "playwright_fixture_guard.cjs"
            active_ports = []
            for reference, profile in _browser_services:
                process = reference()
                if process and process.poll() is None and (profile / "DevToolsActivePort").is_file():
                    active_ports.append(int((profile / "DevToolsActivePort").read_text().splitlines()[0]))
            if (len(parts) != 3 or Path(parts[1]).resolve() != program.parent / "package/cli.js" or parts[2] != "run-driver"
                    or values.get("NODE_OPTIONS") != '--require="' + guard.as_posix() + '"'
                    or not active_ports or values.get("QQBOT_PLAYWRIGHT_TEST_PORTS") != json.dumps(sorted(active_ports))):
                reject("Playwright is limited to the guarded disposable CDP fixture driver")
            return
        kind = trusted_programs.get(program)
        if kind in ("pwsh", "powershell"):
            # Only the generated literal CLI invocation is a permitted shell.
            if len(parts) != 5 or [part.lower() for part in parts[1:4]] != ["-noprofile", "-noninteractive", "-command"]:
                reject("only literal generated CLI PowerShell calls are permitted")
            text = parts[4]
            if not re.fullmatch(r"& (?:'(?:[^']|'')*')(?: '(?:[^']|'')*')*", text):
                reject("arbitrary shell programs are disabled")
            argv = [match[1:-1].replace("''", "'") for match in re.findall(r"'(?:[^']|'')*'", text[2:])]
            if len(argv) < 2 or Path(argv[0]).resolve() != interpreter or Path(argv[1]).resolve() != root / "console_tools/cli.py":
                reject("PowerShell can only invoke the guarded test CLI")
            python_arguments(argv, cwd, env)
            return
        if kind == "node":
            harness = root / "implementation_artifacts/gemini_browser_setup/extension_smoke.cjs"
            if len(parts) == 2 and (Path(cwd or Path.cwd()) / parts[1]).resolve() == harness:
                return
            reject("Node is limited to the copied extension fixture harness")
        if kind in ("ffmpeg", "ffprobe"):
            for argument in parts[1:]:
                if re.search(r"(?:https?|tcp|udp|ftp|concat|subfile|file):|://", argument, re.I):
                    reject("media fixtures may not open network or indirect file inputs")
                if Path(argument).is_absolute():
                    check_file(argument, True)
                elif not argument.startswith("-") and (Path(cwd or Path.cwd()) / argument).exists():
                    check_file(Path(cwd or Path.cwd()) / argument, True)
            return
        if program.name.lower() in {"chrome.exe", "msedge.exe", "chrome", "chromium", "chromium.exe"}:
            required = {"--disable-background-networking", "--disable-component-update", "--disable-sync", "--no-pings",
                        "--no-first-run", "--no-default-browser-check", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0"}
            profile = next((Path(part.split("=", 1)[1]).resolve() for part in parts if part.startswith("--user-data-dir=")), None)
            urls = [part for part in parts if part.startswith(("http:", "https:"))]
            if (not program.is_file() or program.is_relative_to(source) or program.is_relative_to(Path(tempfile.gettempdir()).resolve())
                    or not required <= set(parts) or not any(part.startswith("--headless") for part in parts)
                    or profile is None or not profile.is_relative_to(Path(tempfile.gettempdir()).resolve()) or profile.is_relative_to(source)
                    or len(urls) != 1):
                reject("only disposable headless test browsers may be launched")
            url = urllib.parse.urlsplit(urls[0])
            if not loopback(url.hostname) or url.port not in owned_ports():
                reject("test browsers must open a test-owned local server")
            allowed_flags = required | {"--headless=new", "--headless", "--disable-gpu", "--window-size=1000,700", "--force-device-scale-factor=1"}
            if any(part not in allowed_flags and part not in urls and not part.startswith("--user-data-dir=") for part in parts[1:]):
                reject("unrecognized test browser arguments")
            return
        reject("external programs are disabled except guarded Python and explicit test tools")

    def audit(event, args):
        if event == "open":
            mode, flags = args[1], args[2]
            write = bool(isinstance(mode, str) and any(c in mode for c in "wax+")) or bool(
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            check_file(args[0], write)
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"}:
            check_file(args[0], True, mkdir=event == "os.mkdir")
        elif event in {"os.rename", "os.replace", "os.link", "os.symlink"}:
            check_file(args[0], True)
            check_file(args[1], True)
        elif event in {"os.listdir", "os.scandir"}:
            check_file(args[0])
        elif event == "sqlite3.connect":
            database = args[0]
            if isinstance(database, str) and database.startswith("file:"):
                database = urllib.parse.unquote(database[5:].split("?", 1)[0])
            if database != ":memory:":
                check_file(database, True)
        elif event == "socket.getaddrinfo":
            if not loopback(args[0]):
                reject("external DNS/network access is disabled")
        elif event == "socket.connect":
            check_connection(*args)
        elif event == "subprocess.Popen":
            check_process(args)

    sys.addaudithook(audit)

    # Seed only defaults, never the user's saved accounts or existing memories.
    from config import AppConfig
    from persona_manager import PersonaManager
    if not (root / "config.yaml").exists():
        AppConfig().save(str(root / "config.yaml"))
    if not (root / "personas.yaml").exists():
        PersonaManager(str(root / "personas.yaml"))

    def save_audit():
        (root / ".qqbot-test-audit.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    atexit.register(save_audit)
    return state

