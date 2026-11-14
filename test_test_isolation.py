"""Tests cannot enqueue live bot work through files or model connections."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import os
import asyncio
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import unittest

from testing_sandbox import TestIsolationError, install_guards, is_sandbox


class TestRuntimeIsolation(unittest.TestCase):
    def test_code_and_default_data_paths_belong_to_disposable_checkout(self):
        root = Path(__file__).resolve().parent
        self.assertTrue(is_sandbox(root))
        source = Path(os.environ["QQBOT_TEST_SOURCE_ROOT"])
        self.assertNotEqual(root, source)
        from antigravity_client import AntigravityClient
        from group_memory import GroupMemoryManager
        ai = AntigravityClient()
        manager = GroupMemoryManager()
        self.assertTrue(Path(ai.sessions_file).is_relative_to(root))
        self.assertTrue(Path(manager.storage_dir).resolve().is_relative_to(root))

    def test_hard_coded_live_data_reads_and_writes_are_denied(self):
        source = Path(os.environ["QQBOT_TEST_SOURCE_ROOT"])
        for mode in ("r", "w", "a"):
            with self.subTest(mode=mode), self.assertRaises(TestIsolationError):
                (source / "data" / "must-not-exist-test-probe.json").open(mode)

    def test_saved_live_accounts_are_not_copied(self):
        from config import AppConfig
        cfg = AppConfig.load("config.yaml")
        self.assertEqual(cfg.qq.appid, "")
        self.assertEqual(cfg.qq.secret, "")
        self.assertFalse(cfg.antigravity.csrf_token)
        self.assertFalse(cfg.antigravity.ls_address)

    def test_sqlite_cannot_bypass_live_data_protection(self):
        path = Path(os.environ["QQBOT_TEST_SOURCE_ROOT"]) / "data/must-not-create-isolation.sqlite3"
        for name in (str(path), "file:" + path.as_posix() + "?mode=ro"):
            with self.subTest(name=name), self.assertRaises(TestIsolationError):
                sqlite3.connect(name, uri=name.startswith("file:"))

    def test_preexisting_local_service_cannot_be_contacted(self):
        with socket.socket() as sock, self.assertRaises(TestIsolationError):
            sock.connect(("127.0.0.1", 1))

    def test_async_connections_are_checked_before_native_connect(self):
        async def check():
            with self.assertRaises(TestIsolationError):
                await asyncio.open_connection("127.0.0.1", 1)
            import aiohttp
            async with aiohttp.ClientSession() as client:
                with self.assertRaises(aiohttp.ClientConnectorError) as raised:
                    await client.get("http://127.0.0.1:1/")
                self.assertIsInstance(raised.exception.os_error, TestIsolationError)
        asyncio.run(check())

    def test_async_connections_to_owned_servers_remain_usable(self):
        async def check():
            async def reply(reader, writer):
                writer.write(b"fixture")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            server = await asyncio.start_server(reply, "127.0.0.1", 0)
            async with server:
                reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
                self.assertEqual(await reader.readexactly(7), b"fixture")
                writer.close()
                await writer.wait_closed()
        asyncio.run(check())

    def test_test_owned_local_http_and_tcp_servers_remain_usable(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with socket.socket() as client:
                client.connect(listener.getsockname())
                connection, _ = listener.accept()
                with connection:
                    connection.sendall(b"fixture")
                    self.assertEqual(client.recv(7), b"fixture")

    def test_real_model_binary_launch_is_blocked_before_process_creation(self):
        with self.assertRaises(TestIsolationError):
            sys.audit("subprocess.Popen", "language_server.exe", ["language_server.exe", "agentapi"], None, {})

    def test_child_python_inherits_file_and_service_guards(self):
        script = """import os, socket
from pathlib import Path
from testing_sandbox import TestIsolationError
attempts = [lambda: (Path(os.environ['QQBOT_TEST_SOURCE_ROOT'])/'data/child-must-not-write.json').write_text('fixture'),
            lambda: socket.create_connection(('127.0.0.1', 1))]
for operation in attempts:
    try:
        operation()
    except TestIsolationError:
        print('blocked')
    else:
        raise AssertionError('child escaped isolation')
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["blocked", "blocked"])

    def test_child_python_cannot_disable_startup_or_replace_guard_environment(self):
        for flag in ("-I", "-E", "-S", "-IS", "-X"):
            with self.subTest(flag=flag), self.assertRaises(TestIsolationError):
                subprocess.run([sys.executable, flag, "-c", "print('must not launch')"], capture_output=True)
        for env in ({}, {**os.environ, "PYTHONPATH": ""},
                    {**os.environ, "QQBOT_TEST_OUTPUT_FILES": json.dumps([str(Path(os.environ['QQBOT_TEST_SOURCE_ROOT'])/'data/extra.json')])}):
            with self.subTest(environment=sorted(env.keys())[:1]), self.assertRaises(TestIsolationError):
                subprocess.run([sys.executable, "-c", "print('must not launch')"], env=env, capture_output=True)

    def test_arbitrary_native_and_shell_programs_are_not_allowed(self):
        for program, arguments in (("cmd.exe", ["cmd.exe", "/c", "exit 0"]),
                                   ("node", ["node", "-e", "process.exit(0)"]),
                                   ("powershell", ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Write-Output fixture"])):
            with self.subTest(program=program), self.assertRaises(TestIsolationError):
                sys.audit("subprocess.Popen", program, arguments, None, None)

    def test_native_media_tools_cannot_use_live_paths_or_network_inputs(self):
        import shutil
        program = shutil.which("ffmpeg")
        if not program:
            self.skipTest("ffmpeg unavailable")
        for argument in (str(Path(os.environ["QQBOT_TEST_SOURCE_ROOT"]) / "data/never-open.mp4"), "https://fixture.invalid/input.mp4"):
            with self.subTest(argument=argument), self.assertRaises(TestIsolationError):
                sys.audit("subprocess.Popen", program, [program, "-i", argument], None, None)

