"""Offline protocol fixtures plus a real local WebGL/PCM browser smoke test."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
import base64
import json
import math
from pathlib import Path
import struct
import tempfile
import unittest

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from vtuber_clients import OBSClient
from vtuber_config import VTuberConfig
from vtuber_service import VTuberRuntime, create_app, pcm_rms


class VTuberValidationTests(unittest.TestCase):
    def test_config_is_secret_free_and_rejects_unsafe_endpoints(self):
        data = VTuberConfig().model_dump()
        self.assertNotIn("obs_password", data)
        self.assertNotIn("api_token", data)
        for values in ({"host": "0.0.0.0"}, {"obs_url": "ws://example.com:4455"},
                       {"obs_url": "ws://user:password@localhost:4455"},
                       {"allow_remote": True, "vts_url": "ws://example.com:8001"}, {"port": 2}):
            with self.assertRaises(ValueError): VTuberConfig(**values)

    def test_pcm_rms_all_integer_widths_and_invalid_samples(self):
        self.assertEqual(pcm_rms(bytes([128] * 16), 1), 0)
        self.assertAlmostEqual(pcm_rms(struct.pack("<hh", 16384, -16384), 2), 0.5)
        self.assertAlmostEqual(pcm_rms((4194304).to_bytes(3, "little", signed=True), 3), 0.5)
        self.assertAlmostEqual(pcm_rms(struct.pack("<i", 1073741824), 4), 0.5)
        for value, width in ((b"", 2), (b"1", 2), (b"12345", 5)):
            with self.assertRaises(ValueError): pcm_rms(value, width)


class VTuberRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qqbot-vtuber-test-")
        root = Path(self.temp.name)
        self.runtime = VTuberRuntime(VTuberConfig(credentials_file=str(root / "credentials.json"), vts_token_file=str(root / "vts.json")))
        self.server = TestServer(create_app(self.runtime))
        await self.server.start_server()
        self.session = ClientSession()
        self.headers = {"Authorization": "Bearer " + self.runtime.api_token}

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()
        self.temp.cleanup()

    async def test_http_auth_origin_validation_and_no_secret_status(self):
        async with self.session.get(self.server.make_url("/api/status")) as result:
            self.assertEqual(result.status, 401)
        async with self.session.get(self.server.make_url("/api/status"), headers={"Authorization": "Bearer 非法凭据"}) as result:
            self.assertEqual(result.status, 401)
        async with self.session.get(self.server.make_url("/api/status"), headers={**self.headers, "Origin": "https://untrusted.invalid"}) as result:
            self.assertEqual(result.status, 403)
        async with self.session.get(self.server.make_url("/api/status"), headers={**self.headers, "Host": "attacker.invalid"}) as result:
            self.assertEqual(result.status, 403)
        async with self.session.get(self.server.make_url("/api/status"), headers=self.headers) as result:
            body = await result.json()
            self.assertTrue(body["ok"])
            self.assertNotIn(self.runtime.api_token, json.dumps(body))
            self.assertFalse(body["result"]["vts"]["connected"])
            self.assertFalse(body["result"]["obs"]["connected"])
        for event in ({"type": "mouth", "value": 1.2}, {"type": "mouth", "value": float("nan")},
                      {"type": "emotion", "emotion": "unknown"}, {"type": "text", "text": "x" * 4001},
                      {"type": "motion", "index": -1}, {"type": "unknown"}):
            async with self.session.post(self.server.make_url("/api/event"), headers=self.headers, json=event) as result:
                self.assertEqual(result.status, 400, event)

    async def test_websocket_event_pcm_audio_and_reconnect_does_not_replay_speech(self):
        ws = await self.session.ws_connect(self.server.make_url("/ws"))
        await ws.send_json({"type": "auth", "token": self.runtime.api_token})
        self.assertEqual((await ws.receive_json())["type"], "snapshot")
        await self.runtime.publish_event({"type": "speech", "text": "测试字幕", "emotion": "happy"})
        self.assertEqual((await ws.receive_json())["text"], "测试字幕")
        pcm = struct.pack("<" + "h" * 2400, *[int(10000 * math.sin(i)) for i in range(2400)])
        await self.runtime.push_pcm(pcm)
        event = await ws.receive_json()
        self.assertEqual(base64.b64decode(event["pcm_base64"]), pcm)
        self.assertGreater(event["rms"], 0)
        audio = Path(self.temp.name) / "song.wav"
        audio.write_bytes(b"RIFF fixture")
        await self.runtime.sync_audio(audio, text="歌声", emotion="excited")
        event = await ws.receive_json()
        async with self.session.get(self.server.make_url(event["url"])) as response:
            self.assertEqual(response.status, 401)
        async with self.session.get(self.server.make_url(event["url"]), headers=self.headers) as response:
            self.assertEqual(await response.read(), audio.read_bytes())
        await ws.close()
        ws2 = await self.session.ws_connect(self.server.make_url("/ws"))
        await ws2.send_json({"type": "auth", "token": self.runtime.api_token})
        snapshot = await ws2.receive_json()
        self.assertEqual(snapshot["type"], "snapshot")
        self.assertEqual(snapshot["state"]["text"], "歌声")
        self.assertNotIn("url", snapshot)
        await ws2.close()

    async def test_vts_fixture_auth_persist_hotkey_expression_and_lip_sync(self):
        calls = []
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                body = json.loads(msg.data)
                kind = body["messageType"]
                calls.append(body)
                data = {"authenticationToken": "fixture-token"} if kind == "AuthenticationTokenRequest" else {"authenticated": True} if kind == "AuthenticationRequest" else {"ok": True}
                await ws.send_json({"requestID": body["requestID"], "messageType": kind.replace("Request", "Response"), "data": data})
            return ws
        app = web.Application(); app.router.add_get("/", handler)
        server = TestServer(app); await server.start_server()
        self.runtime.vts.url = str(server.make_url("/")).replace("http:", "ws:")
        try:
            self.assertTrue((await self.runtime.execute("vts.connect"))["authenticated"])
            token = json.loads(self.runtime.vts.token_file.read_text())
            self.assertEqual(token["authenticationToken"], "fixture-token")
            await self.runtime.execute("vts.hotkey", {"hotkey_id": "wave"})
            await self.runtime.execute("vts.expression", {"expression": "happy.exp3.json", "active": True})
            await self.runtime.execute("vts.mouth", {"value": 0.6})
            await self.runtime.execute("vts.parameter", {"id": "FaceAngleX", "value": 10})
            self.assertTrue(any(item["messageType"] == "HotkeyTriggerRequest" for item in calls))
            mouth = next(item for item in calls if item["messageType"] == "InjectParameterDataRequest")
            self.assertEqual(mouth["data"]["parameterValues"][0], {"id": "MouthOpen", "value": 0.6, "weight": 1.0})
            self.assertNotIn("fixture-token", json.dumps(self.runtime.status()))
        finally:
            await self.runtime.vts.close(); await server.close()

    async def test_obs_v5_fixture_auth_browser_scene_monitor_and_explicit_stream(self):
        calls, scenes, inputs = [], [], []
        async def handler(request):
            ws = web.WebSocketResponse(); await ws.prepare(request)
            await ws.send_json({"op": 0, "d": {"rpcVersion": 1, "authentication": {"salt": "salt", "challenge": "challenge"}}})
            identify = await ws.receive_json()
            self.assertEqual(identify["d"]["authentication"], OBSClient.authentication("fixture-password", "salt", "challenge"))
            await ws.send_json({"op": 2, "d": {"negotiatedRpcVersion": 1}})
            async for message in ws:
                body = json.loads(message.data)["d"]; kind = body["requestType"]; calls.append(body)
                if kind == "CreateScene": scenes.append({"sceneName": body["requestData"]["sceneName"]})
                if kind == "CreateInput": inputs.append({"inputName": body["requestData"]["inputName"]})
                response = {"scenes": scenes} if kind == "GetSceneList" else {"inputs": inputs} if kind == "GetInputList" else {"sceneItems": []} if kind == "GetSceneItemList" else {}
                await ws.send_json({"op": 7, "d": {"requestId": body["requestId"], "requestStatus": {"result": True, "code": 100}, "responseData": response}})
            return ws
        app = web.Application(); app.router.add_get("/", handler)
        server = TestServer(app); await server.start_server()
        self.runtime.obs.url = str(server.make_url("/")).replace("http:", "ws:")
        self.runtime.obs.password = "fixture-password"
        try:
            result = await self.runtime.execute("obs.browser_source")
            self.assertTrue(result["configured"])
            created = next(body for body in calls if body["requestType"] == "CreateInput")
            self.assertTrue(created["requestData"]["inputSettings"]["reroute_audio"])
            self.assertIn("#token=", created["requestData"]["inputSettings"]["url"])
            self.assertNotIn("StartStream", [body["requestType"] for body in calls])
            await self.runtime.execute("obs.audio_monitor")
            await self.runtime.execute("obs.start_stream")
            await self.runtime.execute("obs.stop_stream")
            self.assertEqual([body["requestType"] for body in calls][-3:], ["SetInputAudioMonitorType", "StartStream", "StopStream"])
            self.assertNotIn("fixture-password", json.dumps(self.runtime.status()))
        finally:
            await self.runtime.obs.close(); await server.close()

    async def test_bad_pcm_and_paths_do_not_execute_external_programs(self):
        for payload in ({"pcm_base64": "invalid%"}, {"pcm_base64": ""},
                        {"pcm_base64": "AAAA", "channels": 3}, {"pcm_base64": "AAAA", "sample_rate": 1}):
            with self.assertRaises(ValueError): await self.runtime.execute("pcm", payload)
        with self.assertRaises(ValueError): await self.runtime.sync_audio("missing.exe")
        with self.assertRaises(ValueError): await self.runtime.execute("obs.arbitrary")
        with self.assertRaises(ValueError): await self.runtime.configure({"port": 9000})


class VTuberBrowserTests(unittest.TestCase):
    def test_actual_live2d_transparency_audio_and_pcm_render(self):
        from maintenance.vtuber_preview_verify import verify
        with tempfile.TemporaryDirectory(prefix="qqbot-vtuber-browser-report-") as folder:
            result = verify(folder)
            self.assertTrue(result["ok"])
            self.assertGreater(result["audio_peak_mouth"], 0.2)
            self.assertGreater(result["opaque_model_pixels"], 10000)
            self.assertTrue(result["transparent_corner"])
            self.assertTrue(result["pcm_stream"])
            self.assertEqual(result["browser_errors"], [])


if __name__ == "__main__": unittest.main()
