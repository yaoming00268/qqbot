"""Real isolated QQ handlers share model state while keeping source grants and delivery."""
from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import pytest

from config import AppConfig, GroupCustomConfig, WorkPermissions
from conversation_records import dialogue_records
from conversation_routing import ConversationRoute, binding, shared_memory_manifest
from persona_isolation import effective_persona, identity, namespace, persona_for, scoped_session
from test_ai_fixture import ScriptedConsoleAgentMock, prompt_object
import test_startup_participation as fixtures
from console_tools.broker import ConsoleBroker
from console_tools.cli import execute
from ui.studio_api import StudioAPI


@pytest.mark.parametrize("mapping", [
    {"onebot_group_1": {"mode":"follow","target":"onebot_group_1"}},
    {"onebot_group_1": {"mode":"merge","target":"onebot_group_2"}, "onebot_group_2": {"mode":"follow","target":"onebot_group_1"}},
    {"../escape": {}},
    {"onebot_group_1": {"mode":"follow","target":"group_2"}},
    {"onebot_group_1": {"mode":"follow","target":"onebot:other::onebot_group_2"}},
    {"onebot_group_1": {"mode":"independent","target":"onebot_group_2"}},
    {"onebot_group_1": {"mode":"merge","target":"onebot_group_2","persona_id":"p_maid"}},
])
def test_invalid_cycles_cross_account_targets_and_ambiguous_personas_are_rejected(mapping):
    with pytest.raises(ValueError):
        AppConfig.model_validate({"bot":{"conversations":{"onebot:account_1":mapping}}})


class ConversationRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.StartupParticipationTests()
        self.fixture.setUp()
        self.prompts, self.contexts = [], []

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def prepare(self, official=False):
        bot, key = self.fixture.bot(official, stored_cid="primary-cid")
        coordinator = bot._get_main_coordinator()
        self.enterContext(patch.object(coordinator,"start"))
        self.enterContext(patch.object(coordinator,"poll",new_callable=AsyncMock))
        async def model(session_key,prompt,**options):
            session = bot.ai.get_or_create_session(session_key)
            session.conversation_id = session.conversation_id or "cid-" + hashlib.sha256(session_key.encode()).hexdigest()[:12]
            context = prompt_object(prompt,"console_context_file")["console_context_file"]
            broker, token = ConsoleBroker.from_context(context)
            payload = broker.context(token)
            self.prompts.append((session_key,prompt,options))
            self.contexts.append(payload)
            if not payload.get("idle_event"):
                execute("speech",context,operation="ready")
                execute("reply",context,text="收到：" + str(payload["conversation_id"]))
            return ""
        bot.ai.chat = ScriptedConsoleAgentMock(side_effect=model)
        if not official:
            self.enterContext(patch.object(bot, "send_private_message", wraps=bot.send_private_message))
        return bot,key,coordinator

    def set_rule(self,bot,source,**values):
        bot.app_config.bot.conversations.setdefault(namespace(bot),{})[source] = ConversationRoute(**values)

    async def receive(self,bot,official,target,text="继续聊",*,private=False,message_id="m",elapsed=0,sender="456"):
        now = self.fixture.baseline + elapsed
        if official:
            message = SimpleNamespace(id=message_id, content=text, attachments=None, mentions=[],message_reference=None,
                author=SimpleNamespace(member_openid=sender,user_openid=target,username="Fixture",bot=False),
                reply=AsyncMock(return_value={"id":"sent-"+message_id}))
            if not private:
                message.group_openid = target
            with patch("time.time",return_value=now),patch("random.random",return_value=.99):
                if private:
                    await bot._process_c2c_message(message)
                else:
                    await bot._handle_group_message(message,is_at_event=False)
            return message
        event = {"post_type":"message","message_type":"private" if private else "group","self_id":999,
            "user_id":int(target) if private else int(sender),"message_id":message_id,
            "raw_message":text,"message":[{"type":"text","data":{"text":text}}],"sender":{"nickname":"Fixture"},"time":now}
        if not private:
            event["group_id"] = int(target)
        with patch("time.time",return_value=now),patch("random.random",return_value=.99):
            await (bot._handle_private_message(event) if private else bot._handle_group_message(event))
        return event

    async def test_group_and_private_can_select_different_personas_on_both_transports(self):
        for official in (False,True):
            with self.subTest(official=official):
                bot,_,_ = await self.prepare(official)
                prefix = "" if official else "onebot_"
                private = ("c2c_" if official else "onebot_private_") + "7788"
                selected = list(bot.personas.presets.values())[1]
                self.set_rule(bot,private,persona_id=selected.id)
                await self.receive(bot,official,"12345678",message_id="group")
                group_owner = self.contexts[-1]["identity_owner"]
                await self.receive(bot,official,"7788",private=True,message_id="private",elapsed=1)
                payload = self.contexts[-1]
                self.assertEqual(payload["identity_persona_name"],selected.name)
                self.assertEqual(payload["identity_owner"],identity(bot,selected))
                self.assertNotEqual(payload["identity_owner"],group_owner)
                self.assertTrue(payload["session_key"].startswith(private+"::persona:"))

    async def test_merge_reuses_primary_cid_and_presets_but_sends_to_each_actual_group(self):
        for official in (False,True):
            with self.subTest(official=official):
                bot,key,coordinator = await self.prepare(official)
                prefix = "" if official else "onebot_"
                first = await self.receive(bot,official,"12345678",message_id="first")
                self.set_rule(bot,prefix+"group_87654321",mode="merge",target=prefix+"group_12345678")
                second = await self.receive(bot,official,"87654321",message_id="second",elapsed=1)
                await self.receive(bot,official,"12345678",message_id="third",elapsed=2)
                last = self.prompts[-3:]
                self.assertEqual({row[0] for row in last},{key})
                self.assertEqual(bot.ai.get_or_create_session(key).conversation_id,"primary-cid")
                self.assertNotIn('"type": "persona_update"',last[1][1])
                self.assertNotIn('"type": "protocol_update"',last[1][1])
                self.assertEqual(self.contexts[-2]["conversation_id"],"87654321")
                self.assertEqual(self.contexts[-2]["conversation_binding"]["primary"],prefix+"group_12345678")
                for source in ("12345678","87654321"):
                    scope=next(item["scope"] for item in self.contexts[-3:] if item["conversation_id"]==source)
                    self.assertEqual(coordinator.context_for(key,scope).conversation_id,source)
                if official:
                    first.reply.assert_awaited_once()
                    second.reply.assert_awaited_once()
                    self.assertEqual(second.reply.await_args.kwargs["content"],"收到：87654321")
                else:
                    self.assertEqual([call.args[0] for call in bot.send_group_message.await_args_list[-3:]],
                                     ["12345678","87654321","12345678"])

    async def test_private_follower_uses_primary_persona_and_keeps_private_delivery(self):
        for official in (False,True):
            with self.subTest(official=official):
                bot,_,coordinator=await self.prepare(official)
                primary=("" if official else "onebot_")+"group_12345678"
                private=("c2c_" if official else "onebot_private_")+"7788"
                role=list(bot.personas.presets.values())[1]
                self.set_rule(bot,primary,persona_id=role.id)
                self.set_rule(bot,private,mode="follow",target=primary)
                await self.receive(bot,official,"12345678",message_id="primary")
                root=self.prompts[-1][0]
                message=await self.receive(bot,official,"7788",private=True,message_id="follower",elapsed=1)
                self.assertEqual(self.prompts[-1][0],root)
                self.assertEqual(self.contexts[-1]["identity_persona_name"],role.name)
                self.assertFalse(self.contexts[-1]["is_group"])
                self.assertEqual(coordinator.contexts[root].conversation_id,"12345678")
                self.assertEqual(coordinator.context_for(root,self.contexts[-1]["scope"]).conversation_id,"7788")
                if official:
                    message.reply.assert_awaited_once()
                else:
                    bot.send_private_message.assert_awaited_once()
                    self.assertEqual(bot.send_private_message.await_args.args[0],"7788")

    async def test_source_permissions_do_not_inherit_primary_master_or_work_grants(self):
        bot,_,_=await self.prepare()
        bot.app_config.bot.group_settings["12345678"]=GroupCustomConfig(group_id="12345678",master_id="456")
        bot.app_config.bot.group_settings["87654321"]=GroupCustomConfig(group_id="87654321",work_permissions=WorkPermissions(access="disabled"))
        self.set_rule(bot,"onebot_group_87654321",mode="follow",target="onebot_group_12345678")
        await self.receive(bot,False,"12345678",message_id="primary")
        self.assertTrue(self.contexts[-1]["is_master"])
        await self.receive(bot,False,"87654321",message_id="ordinary",elapsed=1)
        payload=self.contexts[-1]
        self.assertFalse(payload["is_master"])
        self.assertEqual(payload["work_permissions"]["access"],"disabled")
        self.assertEqual(payload["conversation_id"],"87654321")

    async def test_unlink_invalidates_old_follower_envelope_and_restores_separate_session(self):
        bot,_,coordinator=await self.prepare()
        self.set_rule(bot,"onebot_group_87654321",mode="follow",target="onebot_group_12345678")
        await self.receive(bot,False,"87654321",message_id="linked")
        root=self.prompts[-1][0]
        old=coordinator.context_for(root,bot._image_scope("87654321"))
        self.assertTrue(old.guard())
        self.set_rule(bot,"onebot_group_87654321")
        self.assertFalse(old.guard())
        restored = bot._make_media_context(session_key=root,user_id="456",conversation_id="87654321",is_group=True)
        self.assertFalse(restored.guard(), "Persisted tasks must not regain the old shared capability after unlink")
        await self.receive(bot,False,"87654321",message_id="separate",elapsed=1)
        self.assertNotEqual(self.prompts[-1][0],root)

    async def test_unlink_cancels_remaining_chunks_and_persisted_outbox_guards(self):
        bot,_,_=await self.prepare()
        self.set_rule(bot,"onebot_group_87654321",mode="follow",target="onebot_group_12345678")
        captured=[]
        async def enqueue(*args,**options):
            self.assertTrue(options["guard"]())
            captured.append(options["guard_meta"])
            self.set_rule(bot,"onebot_group_87654321")
            return {"status":"queued","data":{"outbox_id":"fixture-outbox"}}
        bot._text_outbox=SimpleNamespace(enqueue=AsyncMock(side_effect=enqueue))
        result=await bot._send_text_parts("send_group_msg","group_id","87654321","第一段。" * 2000)
        self.assertEqual(result["status"],"cancelled")
        bot._text_outbox.enqueue.assert_awaited_once()
        self.assertFalse(bot._validate_outbox_guard(captured[0],{},{}))
        bot._text_outbox=None

    async def test_private_persona_outbox_guard_tracks_private_binding(self):
        bot,_,_=await self.prepare()
        role=list(bot.personas.presets.values())[1]
        self.set_rule(bot,"onebot_private_7788",persona_id=role.id)
        async def enqueue(*args,**options):
            self.assertTrue(bot._validate_outbox_guard(options["guard_meta"],{},{}))
            return {"status":"queued","data":{"outbox_id":"fixture-private"}}
        bot._text_outbox=SimpleNamespace(enqueue=AsyncMock(side_effect=enqueue))
        result=await bot._send_text_parts("send_private_msg","user_id","7788","私聊发言")
        self.assertEqual(result["status"],"queued")
        bot._text_outbox=None

    async def test_unlink_while_waiting_for_reply_slot_does_not_send_old_text(self):
        bot,_,coordinator=await self.prepare()
        source="onebot_group_87654321"
        self.set_rule(bot,source,mode="follow",target="onebot_group_12345678")
        key=bot._chat_session_key(source)
        async with coordinator.replies.turn(key):
            pending=asyncio.create_task(bot._send_text("send_group_msg","group_id","87654321","旧回复"))
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            self.set_rule(bot,source)
        result=await asyncio.wait_for(pending,1)
        self.assertEqual(result["status"],"cancelled")
        bot.send_action.assert_not_awaited()

    async def test_exploration_missing_source_never_uses_primary_directory_grants(self):
        bot,key,coordinator=await self.prepare()
        bot.app_config.bot.master_id="456"
        self.set_rule(bot,"onebot_group_87654321",mode="follow",target="onebot_group_12345678")
        await self.receive(bot,False,"12345678",message_id="primary")
        primary=coordinator.contexts[key]
        await self.receive(bot,False,"87654321",message_id="follower",elapsed=1)
        follower=coordinator.context_for(key,bot._image_scope("87654321"))
        database=Path(bot.app_config.sandbox.directory)/"sandbox.sqlite3"
        database.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE grants(id TEXT,owner TEXT,path TEXT,namespace TEXT,scope TEXT)")
            db.execute("INSERT INTO grants VALUES(?,?,?,?,?)",("fixture","456",str(Path("primary-grant").resolve()),coordinator.namespace,primary.scope))
            db.commit()
        from exploration import configuration
        self.assertIn("grant-fixture",configuration(coordinator,key)["roots"])
        self.assertNotIn("grant-fixture",configuration(coordinator,key,source=None)["roots"])
        self.assertNotIn("grant-fixture",configuration(coordinator,key,source=follower)["roots"])

    async def test_shared_failure_evidence_stays_with_source_and_blocked_input_is_not_replayed(self):
        from antigravity_client import ContentBlockedError
        bot,key,_=await self.prepare()
        self.set_rule(bot,"onebot_group_87654321",mode="merge",target="onebot_group_12345678")
        await self.receive(bot,False,"12345678",message_id="initial")
        working_model=bot.ai.chat.side_effect
        bot.ai.chat.side_effect=ContentBlockedError("fixture-policy-block")
        await self.receive(bot,False,"12345678",text="拒绝过的输入",message_id="blocked",elapsed=1)
        self.assertEqual(bot.ai.get_or_create_session(key).chat_state["last_delivery"]["state"],"blocked")
        bot.ai.chat.side_effect=working_model
        await self.receive(bot,False,"87654321",message_id="other-source",elapsed=2)
        self.assertNotIn('"previous_turn_failure"',self.prompts[-1][1])
        self.assertNotIn('"previous_delivery"',self.prompts[-1][1])
        await self.receive(bot,False,"12345678",message_id="resume",elapsed=3)
        evidence=prompt_object(self.prompts[-1][1],"previous_turn_failure")["previous_turn_failure"]
        self.assertEqual(evidence["source_message_id"],"blocked")
        self.assertNotIn("拒绝过的输入",self.prompts[-1][1])

    async def test_followers_do_not_create_separate_idle_invitations(self):
        bot,_,coordinator=await self.prepare()
        self.set_rule(bot,"onebot_group_87654321",mode="follow",target="onebot_group_12345678")
        await self.receive(bot,False,"87654321",message_id="follow-only")
        bot.app_config.bot.self_activity_enabled=True
        bot.app_config.bot.self_activity_interval_minutes=1
        with patch("time.time",return_value=self.fixture.baseline+61):
            await coordinator.schedule_self_activity()
        self.assertFalse(coordinator.self_activity_tasks)
        await self.receive(bot,False,"12345678",message_id="main-active",elapsed=62)
        with patch("time.time",return_value=self.fixture.baseline+123),patch("exploration.ensure_exploration",return_value=None):
            await coordinator.schedule_self_activity()
            await asyncio.gather(*coordinator.self_activity_tasks.values())
        self.assertEqual(len(coordinator.self_activity_tasks),1)

    async def test_shared_history_has_source_and_does_not_drop_same_ids_in_different_groups(self):
        bot,_,coordinator=await self.prepare()
        bot.app_config.bot.memory_enabled=True
        self.set_rule(bot,"onebot_group_87654321",mode="merge",target="onebot_group_12345678")
        await self.receive(bot,False,"12345678",text="一样的消息",message_id="same")
        key=self.prompts[-1][0]
        await self.receive(bot,False,"87654321",text="一样的消息",message_id="same",elapsed=1)
        from idle_memory import IdleMemory
        journal=IdleMemory(coordinator.directory(key),0).journal
        records=dialogue_records([json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()])
        matching=[item for item in records if item["content"]=="一样的消息"]
        self.assertEqual({row["source_scope"] for row in matching},{bot._image_scope("12345678"),bot._image_scope("87654321")})
        self.assertEqual(len(matching),2)

    async def test_linked_memory_files_are_deduplicated_and_reuse_one_cache_identity(self):
        bot,key,coordinator=await self.prepare()
        bot.app_config.bot.memory_enabled=True
        for group in ("12345678","87654321","55555"):
            memory=bot.memory_mgr.get_group_memory(group)
            memory.summary="历史摘要："+group
            bot.memory_mgr.save_group_memory(memory)
        self.set_rule(bot,"onebot_group_87654321",mode="merge",target="onebot_group_12345678")
        manifest=shared_memory_manifest(coordinator,key)
        paths=[item["path"] for item in manifest["files"]]
        self.assertEqual(len(paths),len(set(paths)))
        self.assertTrue(any("87654321" in path for path in paths))
        self.assertTrue(any("12345678" in path for path in paths))
        self.assertFalse(any("55555" in path for path in paths))
        self.assertEqual(manifest["group_openid"],"12345678")
        session=bot.ai.get_or_create_session(key)
        session.memory_context={"conversation_id":session.conversation_id,"group_openid":"12345678","contract_version":1,
            "loaded_files":{bot.ai._memory_file_key(item["path"]):item["version"] for item in manifest["files"]},
            "offered_files":{bot.ai._memory_file_key(item["path"]):item["version"] for item in manifest["files"]}}
        prompt,plan=bot.ai._prepare_memory_request(session,manifest,"继续聊")
        self.assertEqual(prompt,"继续聊")
        self.assertIsNone(plan)

    async def test_old_persona_history_can_merge_into_private_primary_and_resume_after_unlink(self):
        from conversation_studio import update
        bot,_,coordinator=await self.prepare()
        bot.app_config.bot.memory_enabled=True
        source="onebot_group_12345678"
        old_role=list(bot.personas.presets.values())[1]
        self.set_rule(bot,source,persona_id=old_role.id)
        await self.receive(bot,False,"12345678",text="合并前的群聊历史",message_id="history")
        old_key=self.prompts[-1][0]
        old_cid=bot.ai.get_or_create_session(old_key).conversation_id
        old_journal=coordinator.directory(old_key)/"events.jsonl"
        update(bot.app_config,bot.personas,{"account":namespace(bot),"source":source,"mode":"merge","target":"onebot_private_7788"})
        self.assertEqual(bot.app_config.bot.conversations[namespace(bot)][source].history_persona_id,old_role.id)
        await self.receive(bot,False,"7788",private=True,message_id="private-root",elapsed=1)
        primary_key=self.prompts[-1][0]
        manifest=shared_memory_manifest(coordinator,primary_key)
        self.assertEqual(manifest["group_openid"],"onebot_private_7788")
        self.assertTrue(any(Path(item["path"])==old_journal for item in manifest["files"]))
        self.assertEqual(bot.ai.get_or_create_session(old_key).conversation_id,old_cid)
        update(bot.app_config,bot.personas,{"account":namespace(bot),"source":source,"persona_id":old_role.id})
        await self.receive(bot,False,"12345678",message_id="separate-again",elapsed=2)
        self.assertEqual(self.prompts[-1][0],old_key)
        self.assertEqual(bot.ai.get_or_create_session(old_key).conversation_id,old_cid)

    async def test_route_settings_do_not_apply_to_another_account(self):
        bot,_,_=await self.prepare()
        self.set_rule(bot,"onebot_group_87654321",mode="follow",target="onebot_group_12345678")
        other=SimpleNamespace(app_config=bot.app_config,personas=bot.personas,account_key="account_2",send_action=None)
        self.assertEqual(binding(other,"onebot_group_87654321")["primary"],"onebot_group_87654321")
        self.assertNotEqual(scoped_session(bot,"onebot_group_87654321"),scoped_session(other,"onebot_group_87654321"))

    async def test_bot_mention_names_use_selected_group_private_and_primary_persona(self):
        bot,_,_=await self.prepare()
        role=list(bot.personas.presets.values())[1]
        self.set_rule(bot,"onebot_group_12345678",persona_id=role.id)
        self.set_rule(bot,"onebot_private_7788",persona_id=role.id)
        self.set_rule(bot,"onebot_group_87654321",mode="follow",target="onebot_group_12345678")
        for names in (bot._bot_names("12345678"),bot._bot_names("87654321"),bot._bot_names(key="onebot_private_7788")):
            self.assertIn(role.name,names)
            self.assertTrue(set(role.aliases).issubset(names))

    async def test_command_result_keeps_the_selected_private_session(self):
        bot,_,coordinator=await self.prepare()
        role=list(bot.personas.presets.values())[1]
        self.set_rule(bot,"onebot_private_7788",persona_id=role.id)
        await self.receive(bot,False,"7788",private=True,message_id="selected-private")
        key=self.prompts[-1][0]
        ctx=coordinator.context_for(key,bot._image_scope("7788",False))
        context,token,metadata=coordinator.issue_chat_context(ctx,key)
        receipt=execute("command",context,text="/status")
        request=coordinator.broker.record(receipt["id"])
        with patch.object(coordinator,"_queue_result",return_value=None):
            result=await coordinator._execute(request,ctx,metadata)
        self.assertTrue(result.success,result.message)
        resumed=coordinator.broker.context(token,allow_expired=True)
        self.assertEqual(resumed["session_key"],key)
        self.assertTrue(coordinator.context_for(key,ctx.scope).guard())


class ConversationStudioTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)
        self.api=StudioAPI(self.home)
        self.initial=self.ok(self.api.bootstrap())

    def ok(self,result):
        self.assertTrue(result["ok"],result)
        return result["data"]

    def save(self,source,**values):
        current=self.ok(self.api.conversations())
        return self.api.conversations("save",{"account":"onebot:account_1","source":source,"revision":current["revision"],**values})

    def test_save_independent_and_follow_preserves_config_and_uses_stable_persona_id(self):
        config=AppConfig.load(str(self.home/"config.yaml"))
        config.qq.secret="fixture-secret"
        config.save(str(self.home/"config.yaml"))
        role=self.initial["personas"]["items"][1]
        self.ok(self.save("onebot_group_123",persona_id=role["id"]))
        result=self.ok(self.save("onebot_private_456",mode="follow",target="onebot_group_123"))
        self.assertEqual(result["conversations"],2)
        self.assertEqual(result["contexts"],1)
        follower=next(item for item in result["items"] if item["source"]=="onebot_private_456")
        self.assertEqual(follower["effective_persona_id"],role["id"])
        self.assertNotIn("fixture-secret",json.dumps(result))
        self.assertEqual(AppConfig.load(str(self.home/"config.yaml")).qq.secret,"fixture-secret")

    def test_stale_revision_cycle_and_missing_persona_cannot_change_disk(self):
        self.ok(self.save("onebot_group_123"))
        self.ok(self.save("onebot_group_456",mode="merge",target="onebot_group_123"))
        path=self.home/"config.yaml"
        before=path.read_bytes()
        self.assertFalse(self.save("onebot_group_123",mode="follow",target="onebot_group_456")["ok"])
        self.assertFalse(self.save("onebot_private_999",persona_id="deleted-role")["ok"])
        stale=self.api.conversations("save",{"account":"onebot:account_1","source":"onebot_group_888","revision":self.initial["revision"]})
        self.assertFalse(stale["ok"])
        self.assertEqual(path.read_bytes(),before)

    def test_discovery_returns_identifiers_without_model_cids_or_message_contents(self):
        data=self.home/"data"
        data.mkdir(exist_ok=True)
        (data/"sessions_account_1.json").write_text(json.dumps({"onebot_private_789::persona:abc":
            {"conversation_id":"secret-model-cid","chat_state":{"text":"private-dialogue"}}}),encoding="utf-8")
        result=self.ok(self.api.conversations())
        self.assertTrue(any(item["source"]=="onebot_private_789" for item in result["items"]))
        self.assertNotIn("secret-model-cid",json.dumps(result))
        self.assertNotIn("private-dialogue",json.dumps(result))

    def test_shared_persona_binding_survives_rename(self):
        role=dict(self.initial["personas"]["items"][1])
        self.ok(self.save("onebot_group_123",persona_id=role["id"]))
        old=role["name"]
        role["name"]="重命名后的对话角色"
        self.ok(self.api.save_persona(old,role,self.initial["personas"]["revision"]))
        row=next(item for item in self.ok(self.api.conversations())["items"] if item["source"]=="onebot_group_123")
        self.assertEqual(row["effective_persona"],role["name"])
        self.assertEqual(row["effective_persona_id"],role["id"])

    def test_bound_persona_cannot_be_deleted_until_its_conversation_rule_is_removed(self):
        role=self.initial["personas"]["items"][1]
        self.ok(self.save("onebot_group_123",persona_id=role["id"]))
        before=(self.home/"personas.yaml").read_bytes()
        self.assertFalse(self.api.persona_action("delete",role["name"],self.initial["personas"]["revision"])["ok"])
        self.assertEqual((self.home/"personas.yaml").read_bytes(),before)
        self.ok(self.api.conversations("remove",{"account":"onebot:account_1","source":"onebot_group_123",
            "revision":self.ok(self.api.conversations())["revision"]}))
        self.ok(self.api.persona_action("delete",role["name"],self.initial["personas"]["revision"]))
