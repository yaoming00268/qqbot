"""The main conversation decides what to read; the worker applies it live."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import asyncio
import json
from pathlib import Path
import re
import unittest
from unittest.mock import AsyncMock, patch

from console_tools.cli import execute
from sandbox_runtime import ActivityDirectionChanged, SandboxRuntime
from test_browser_requests import BrowserFrameBackend
import test_unlimited_activity as limit_fixtures
import test_startup_participation as fixtures


class ActivityDirectionCapabilityTests(unittest.TestCase):
    setUp = limit_fixtures.UnlimitedDesktopCapabilityTests.setUp

    def test_redirect_invalidates_old_inputs_and_notes_without_starting_another_task(self):
        frame = execute("desktop", self.worker, operation="screenshot")
        before = self.runtime.game(self.metadata, self.runtime.session(self.metadata, self.sid)["target"])
        changed = execute("sandbox", self.owner, operation="steer", params={"text": "改读花咲作品的配乐讨论"})
        repeated = execute("sandbox", self.owner, operation="steer", params={"text": "改读花咲作品的配乐讨论"})
        self.assertEqual(changed["id"], self.request["id"])
        self.assertEqual(changed["direction_revision"], repeated["direction_revision"])
        self.assertEqual(changed["direction_revision"], 1)
        for operation, params in (("key", {"observation_id": frame["observation_id"], "key": "enter"}),
                ("note", {"text": "旧内容", "evidence": [frame["observation_id"]]}),
                ("finish", {"text": "旧目标完成"})):
            with self.subTest(operation=operation), self.assertRaises(ActivityDirectionChanged):
                execute("sandbox", self.worker, operation=operation, params=params)
        self.assertFalse(self.backend.inputs)
        fresh = self.broker.issue_context({**self.metadata, "sandbox_session_id": self.sid, "activity_revision": 1}, self.root / "new")
        observation = execute("desktop", fresh, operation="screenshot")
        self.assertEqual(observation["direction_revision"], 1)
        result = execute("desktop", fresh, operation="key", params={"observation_id": observation["observation_id"], "key": "enter", "wait_ms": 0})
        self.assertTrue(result["input_posted"])
        self.runtime.acknowledge_direction({**self.metadata,"sandbox_session_id":self.sid,"activity_revision":1}, self.sid, 1)
        feedback = execute("sandbox", self.owner, operation="activities")["activities"][0]
        self.assertEqual(feedback["goal"], "改读花咲作品的配乐讨论")
        self.assertEqual(feedback["applied_revision"], 1)
        after = self.runtime.game(self.metadata, before["id"])
        self.assertEqual(before["profile_path"], after["profile_path"])

    def test_existing_use_call_retargets_the_running_worker_instead_of_enqueuing(self):
        result = execute("sandbox", self.owner, operation="use", params={
            "target_id": self.runtime.session(self.metadata,self.sid)["target"], "text": "搜索花咲作品相关帖子"})
        self.assertEqual(result["id"], self.request["id"])
        self.assertTrue(result["updated"])
        with self.broker.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)
        self.assertEqual(self.runtime.direction(self.metadata,self.sid)["text"], "搜索花咲作品相关帖子")

    def test_controller_is_bound_to_its_existing_task_without_owner_privilege_for_other_actions(self):
        target = self.runtime.session(self.metadata,self.sid)["target"]
        owner_token = self.runtime.game(self.metadata,target)["source_token"]
        controller = self.broker.issue_context({**self.metadata,"sandbox_session_id":self.sid,
            "activity_controller":True,"activity_target_id":target,"parent_request_id":self.request["id"]}, self.root / "controller")
        for operation, params in (("use", {"target_id": target}), ("grant", {"path":str(self.root)}),
                ("autoplay", {"target_id":target,"enabled":True}), ("steer", {"target_id":"other","text":"wrong"})):
            with self.subTest(operation=operation), self.assertRaises(PermissionError):
                execute("sandbox", controller, operation=operation, params=params)
        with self.assertRaises(PermissionError):
            execute("desktop", controller, operation="screenshot")
        with self.assertRaises(PermissionError):
            execute("send", controller, files=[])
        untrusted = self.broker.issue_context({**self.metadata,"is_master":False}, self.root / "nonowner")
        with self.assertRaises(PermissionError):
            execute("sandbox", untrusted, operation="steer", params={"text":"wrong"})
        result = execute("sandbox", controller, operation="steer", params={"text":"我更想看配乐创作经历"})
        self.assertEqual(result["id"],self.request["id"])
        self.assertEqual(self.runtime.game(self.metadata,target)["source_token"],owner_token)

    def test_queued_direction_is_saved_before_launch_and_other_scopes_cannot_change_it(self):
        self.runtime.finish(self.metadata,self.sid,"cancelled")
        self.broker.update(self.request["id"],"submitted")
        result=execute("sandbox",self.owner,operation="steer",params={"text":"先看指定访谈","url":"https://example.com/interview"})
        self.assertEqual(result["id"],self.request["id"])
        target=self.runtime.session(self.metadata,self.sid)["target"]
        sid,app=self.runtime.begin(self.metadata,target,self.request["id"])
        self.assertEqual(app["goal"],"先看指定访谈")
        self.assertEqual(app["launch_url"],"https://example.com/interview")
        other=self.broker.issue_context({**self.metadata,"scope":"another-group"},self.root/"another")
        with self.assertRaises(PermissionError):
            execute("sandbox",other,operation="steer",params={"session_id":sid,"text":"不可跨群"})


class MainActivityFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture=fixtures.StartupParticipationTests()
        self.fixture.setUp()

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_direction_change_during_checkpoint_capture_continues_with_new_goal(self):
        from test_ai_fixture import drain_runtime
        bot,key=self.fixture.bot(stored_cid="existing")
        bot.app_config.bot.master_id="456"
        bot.ai.archive_session=AsyncMock(return_value=True)
        exe=Path("browser/chrome.exe").resolve()
        exe.parent.mkdir()
        exe.touch()
        backend=BrowserFrameBackend()
        backend.launch=lambda target:{"pid":77}
        original_observe=backend.observe
        request_id=""
        worker_calls=0

        def capture(target,path):
            frame=original_observe(target,path)
            if backend.captures==3:
                console=bot._get_main_coordinator()
                owner=console.issue_chat_context(console.contexts[key],key)[0]
                execute("sandbox",owner,operation="steer",params={"text":"改看人物主题配乐"})
            return frame

        backend.observe=capture

        async def agent(session,prompt,**_options):
            nonlocal request_id,worker_calls
            if not session.startswith("__work__:"):
                owner=next(iter(bot._get_main_coordinator().requests))
                request_id=execute("sandbox",owner,operation="use",params={"executable":str(exe)})["id"]
                return "好。"
            worker_calls+=1
            context=re.search(r"当前上下文文件：([^\r\n]+)",prompt)[1]
            if worker_calls==1:
                frame=execute("desktop",context,operation="screenshot")
                execute("sandbox",context,operation="note",params={"kind":"observation","text":"观察首页。","evidence":[frame["observation_id"]]})
                return "继续浏览。"
            self.assertIn("改看人物主题配乐",prompt)
            execute("sandbox",context,operation="pause",params={"text":"需要登录后才能阅读主题配乐页面。"})
            return "保留实际阻碍。"

        bot.ai.chat.side_effect=agent
        with patch("sandbox_runtime.SandboxRuntime.device",return_value=backend):
            await self.fixture.receive(bot,False,"start","使用 "+str(exe))
            await asyncio.wait_for(drain_runtime(bot),5)
        self.assertEqual(worker_calls,2)
        self.assertEqual(bot._get_main_coordinator().broker.record(request_id)["state"],"paused")

    async def test_normal_chat_interrupts_slow_internal_reading_thought_without_stopping_browser(self):
        from test_ai_fixture import drain_runtime
        bot,key=self.fixture.bot(stored_cid="existing")
        bot.app_config.bot.master_id="456"
        bot.ai.archive_session=AsyncMock(return_value=True)
        exe=Path("browser/chrome.exe").resolve()
        exe.parent.mkdir()
        exe.touch()
        backend=BrowserFrameBackend()
        backend.launch=lambda target:{"pid":77}
        note_ready,thinking=asyncio.Event(),asyncio.Event()
        request_id=""

        async def agent(session,prompt,**options):
            nonlocal request_id
            if session.startswith("__work__:"):
                context=re.search(r"当前上下文文件：([^\r\n]+)",prompt)[1]
                frame=execute("desktop",context,operation="screenshot")
                execute("sandbox",context,operation="note",params={"kind":"observation","text":"实际页面有配乐讨论。","evidence":[frame["observation_id"]]})
                note_ready.set()
                while not options["stop_requested"]():
                    await asyncio.sleep(.01)
                return "停止当前操作。"
            if '"type": "activity_observation"' in prompt:
                thinking.set()
                await asyncio.Event().wait()
            if not request_id:
                owner=list(bot._get_main_coordinator().requests)[-1]
                request_id=execute("sandbox",owner,operation="use",params={"executable":str(exe)})["id"]
            return "好。"

        bot.ai.chat.side_effect=agent
        with patch("sandbox_runtime.SandboxRuntime.device",return_value=backend):
            try:
                await self.fixture.receive(bot,False,"start","使用 "+str(exe))
                await asyncio.wait_for(note_ready.wait(),4)
                console=bot._get_main_coordinator()
                console.activity_review_due[key]=0
                await console.schedule_activity_reviews()
                await asyncio.wait_for(thinking.wait(),4)
                review=console.activity_review_tasks[key]
                await asyncio.wait_for(self.fixture.receive(bot,False,"new-chat","现在跟我聊一下。",elapsed=1),4)
                self.assertTrue(review.cancelled())
                self.assertEqual(console.broker.record(request_id)["state"],"running")
            finally:
                console=bot._get_main_coordinator()
                owner=console.issue_chat_context(console.contexts[key],key)[0]
                execute("sandbox",owner,operation="stop")
                await console.poll()
                await asyncio.wait_for(drain_runtime(bot),5)

    async def test_main_thoughts_retarget_live_reading_and_new_notes_return_silently(self):
        await self.exercise_reading_control(False)

    async def test_official_main_conversation_controls_its_existing_reading_worker(self):
        await self.exercise_reading_control(True)

    async def exercise_reading_control(self, official):
        from test_ai_fixture import drain_runtime
        bot,key=self.fixture.bot(official,stored_cid="existing")
        bot.app_config.bot.master_id="456"
        bot.ai.archive_session=AsyncMock(return_value=True)
        exe=Path("browser/chrome.exe").resolve()
        exe.parent.mkdir()
        exe.touch()
        backend=BrowserFrameBackend()
        launches=[]
        backend.launch=lambda target: launches.append(target["id"]) or {"pid":77}
        first_note, second_note, third_turn = asyncio.Event(),asyncio.Event(),asyncio.Event()
        request_id=""
        worker_calls=0
        main_calls=0
        old_inputs_rejected=[]
        new_goal="我更想理解花咲作品的配乐创作，先找相关访谈"
        third_goal="刚读到钢琴主题的线索，继续阅读这位作曲者的访谈"

        async def agent(session,prompt,**options):
            nonlocal request_id,worker_calls,main_calls
            if session.startswith("activity_summary_"):
                self.assertTrue(options["cascade_config"]["plannerConfig"]["toolConfig"]["forceDisableToolCalling"])
                return json.dumps({"summary": "阅读访谈，了解到钢琴主题用于表现人物变化。", "conclusions": ["钢琴主题呼应人物变化"]}, ensure_ascii=False)
            if not session.startswith("__work__:"):
                main_calls+=1
                feedback=[json.loads(part)["activity_feedback"] for part in prompt.split("\n\n") if part.startswith('{"activity_feedback"')]
                if not request_id:
                    owner=list(bot._get_main_coordinator().requests)[-1]
                    request_id=execute("sandbox",owner,operation="use",params={"executable":str(exe),"text":"随意看看论坛帖子"})["id"]
                    return "我去看看。"
                self.assertTrue(feedback)
                item=next(item for item in feedback[0] if item["request_id"]==request_id)
                controller=item["controller_context_file"]
                if '"type": "activity_observation"' in prompt:
                    self.assertTrue(any("钢琴主题" in note["text"] for note in item["notes"]))
                    receipt=execute("sandbox",controller,operation="steer",params={"text":third_goal})
                    self.assertEqual(receipt["direction_revision"],2)
                    idle=[json.loads(part) for part in prompt.split("\n\n") if part.startswith('{"console_context_file"')][0]
                    execute("wait",idle["console_context_file"])
                    return "后台任务已经切换目标了。"
                self.assertTrue(any("新作配乐访谈" in note["text"] for note in item["notes"]))
                current=list(bot._get_main_coordinator().requests)[-1]
                # This chat is from a nonowner; its own tools gain no owner power.
                with self.assertRaises(PermissionError):
                    execute("sandbox",current,operation="use",params={"executable":str(exe)})
                receipt=execute("sandbox",controller,operation="steer",params={"text":new_goal,"url":"https://example.com/interview"})
                self.assertEqual(receipt["id"],request_id)
                self.assertEqual(receipt["direction_revision"],1)
                return "我对配乐访谈更感兴趣。"
            worker_calls+=1
            context=re.search(r"当前上下文文件：([^\r\n]+)",prompt)[1]
            frame=execute("desktop",context,operation="screenshot")
            if worker_calls==1:
                execute("sandbox",context,operation="note",params={"kind":"observation","text":"页面有一篇新作配乐访谈。","evidence":[frame["observation_id"]]})
                first_note.set()
            elif worker_calls==2:
                self.assertIn(new_goal,prompt)
                self.assertEqual(backend.page_url,"https://example.com/interview")
                execute("sandbox",context,operation="note",params={"kind":"observation","text":"访谈提到钢琴主题如何表现人物变化。","evidence":[frame["observation_id"]]})
                second_note.set()
            else:
                self.assertIn(third_goal,prompt)
                execute("sandbox",context,operation="pause",params={"text":"访谈全文需要主人完成登录。"})
                third_turn.set()
                return "等待登录。"
            while not options["stop_requested"]():
                await asyncio.sleep(.01)
            with self.assertRaises(ActivityDirectionChanged):
                execute("desktop",context,operation="key",params={"observation_id":frame["observation_id"],"key":"enter","wait_ms":0})
            old_inputs_rejected.append(worker_calls)
            return "旧轮次停止，改用主对话的新方向。"

        bot.ai.chat.side_effect=agent
        with patch("sandbox_runtime.SandboxRuntime.device",return_value=backend):
            try:
                initial_message=await self.fixture.receive(bot,official,"start","使用 "+str(exe))
                await asyncio.wait_for(first_note.wait(),4)
                thought_message=await self.fixture.receive(bot,official,"thoughts","你现在对什么感兴趣？",sender="777",elapsed=1)
                await asyncio.wait_for(second_note.wait(),4)
                console=bot._get_main_coordinator()
                sent_count=(lambda: initial_message.reply.await_count+thought_message.reply.await_count) if official else (lambda: bot.send_group_message.await_count)
                sent=sent_count()
                console.activity_review_due[key]=0
                await console.schedule_activity_reviews()
                await asyncio.wait_for(console.activity_review_tasks[key],4)
                await asyncio.wait_for(third_turn.wait(),4)
                await asyncio.wait_for(drain_runtime(bot),5)
                self.assertEqual(sent_count(),sent)
                self.assertEqual(console.broker.record(request_id)["state"],"paused")
                self.assertEqual(worker_calls,3)
                self.assertEqual(main_calls,3)
                self.assertEqual(len(launches),1)
                self.assertEqual(old_inputs_rejected,[1,2])
                feedback=console.activity_feedback(key,console.contexts[key].scope)
                self.assertEqual(feedback[0]["applied_revision"],2)
                self.assertEqual(feedback[0]["goal"],third_goal)
                bot.ai.archive_session.assert_not_awaited()
            finally:
                console=bot._get_main_coordinator()
                if request_id and console.broker.record(request_id)["state"]=="running":
                    owner=console.issue_chat_context(console.contexts[key],key)[0]
                    execute("sandbox",owner,operation="stop")
                    await console.poll()


if __name__=="__main__":
    unittest.main()
