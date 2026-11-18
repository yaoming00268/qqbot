from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import asyncio,json,unittest
from unittest.mock import AsyncMock
from console_tools.cli import execute
from live_events import enqueue,ROOT

class MainLiveFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from test_startup_participation import StartupParticipationTests
        self.fixture=StartupParticipationTests();self.fixture.setUp()
    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()
    def setup_live(self):
        bot,_=self.fixture.bot(stored_cid='fixture-only')
        bot.app_config.bilibili.enabled=True
        coord=bot._get_main_coordinator();coord.start=lambda:None
        coord.live_available=AsyncMock(return_value=True)
        coord.live_run_current=lambda run:True
        event=enqueue(coord.broker,namespace=coord.namespace,run='fixture',worker='fixture',attempt='1',summary={'type':'map_finished','metric_source':'last_play'})
        return bot,coord,event
    async def test_actual_main_chat_explicit_say_no_qq(self):
        bot,coord,event=self.setup_live();key=bot._chat_session_key(ROOT);seen=[]
        async def model(actual,prompt,**kwargs):
            self.assertEqual(actual,key)
            for line in prompt.splitlines():
                try: item=json.loads(line)
                except ValueError: continue
                if isinstance(item,dict) and item.get('console_context_file'):
                    path=item['console_context_file']
                    receipt=execute('live-say',path,text='这张谱节奏很密',request_key='fixture')
                    seen.append(receipt['state']);execute('finish',path);return 'ignored final text'
            self.fail('missing live context')
        bot.ai.chat=AsyncMock(side_effect=model)
        await coord.schedule_live_events();await asyncio.wait_for(coord.live_task,5)
        self.assertEqual(seen,['queued']);bot.send_group_message.assert_not_awaited();bot.send_action.assert_not_awaited()
        with coord.broker.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM live_events WHERE id=?',(event,)).fetchone()[0],'done')
            self.assertEqual(db.execute('SELECT count(*) FROM outgoing').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM live_outgoing').fetchone()[0],1)
    async def test_room_closes_while_waiting_for_shared_lock(self):
        bot,coord,event=self.setup_live();key=bot._chat_session_key(ROOT)
        bot.ai.chat=AsyncMock(return_value='ignored')
        lock=coord.locks.setdefault(key,asyncio.Lock());await lock.acquire()
        try:
            await coord.schedule_live_events();await asyncio.sleep(.05)
            coord.live_available.return_value=False
        finally:lock.release()
        await asyncio.wait_for(coord.live_task,5)
        bot.ai.chat.assert_not_awaited()
        with coord.broker.connect() as db:self.assertEqual(db.execute('SELECT state FROM live_events WHERE id=?',(event,)).fetchone()[0],'cancelled')
    async def test_closed_cancels_pending_without_model(self):
        bot,coord,event=self.setup_live();bot.ai.chat=AsyncMock();coord.live_available.return_value=False
        await coord.schedule_live_events();bot.ai.chat.assert_not_awaited()
        with coord.broker.connect() as db:self.assertEqual(db.execute('SELECT state FROM live_events WHERE id=?',(event,)).fetchone()[0],'cancelled')

    async def test_shared_root_persona_cid_and_memory_manifest(self):
        from types import SimpleNamespace
        from pathlib import Path
        from conversation_routing import ConversationRoute
        from persona_isolation import namespace, identity
        from unittest.mock import patch
        bot,coord,event=self.setup_live()
        bot.app_config.bot.memory_enabled=True
        bot.app_config.bot.conversations.setdefault(namespace(bot),{})['onebot_group_222'] = ConversationRoute(mode='follow',target=ROOT)
        persona=bot.personas.get_active_persona();persona.prompt='测试人设：喜欢节奏游戏。'
        key=bot._chat_session_key(ROOT);session=bot.ai.get_or_create_session(key);session.conversation_id='fixture-shared-cid'
        memory=Path('fixture-memory.json').resolve();memory.write_text('{"summary":"喜欢节奏游戏"}',encoding='utf8')
        profile=Path(bot.memory_mgr.storage_dir)/'profiles'/identity(bot,persona);profile.mkdir(parents=True,exist_ok=True)
        manager=SimpleNamespace(get_memory_file_manifest=lambda group,history_count: {'files':[{'role':'summary','path':str(memory),'version':'1'}]})
        captured={}
        async def model(actual,prompt,**options):
            captured['key']=actual;captured['cid']=bot.ai.get_or_create_session(actual).conversation_id
            captured['rules']=options.get('system_prompt','');captured['manifest']=options.get('memory_manifest')
            for line in prompt.splitlines():
                try:item=json.loads(line)
                except ValueError:continue
                if isinstance(item,dict) and item.get('console_context_file'):
                    ctx=item['console_context_file']
                    for action in ('reply','message','commit','deliver'):
                        with self.assertRaises((PermissionError,ValueError)):execute(action,ctx,text='禁止外发')
                    with self.assertRaises(PermissionError):execute('read-memory',ctx,path=str(memory.parent/'outside.json'))
                    execute('live-say',ctx,text='测试台词',request_key='one');execute('finish',ctx);return ''
            self.fail('missing live context')
        bot.ai.chat=AsyncMock(side_effect=model)
        with patch.object(bot.memory_mgr,'manager',return_value=manager):
            await coord.schedule_live_events();await asyncio.wait_for(coord.live_task,5)
        self.assertEqual(captured['key'],key);self.assertEqual(captured['cid'],'fixture-shared-cid')
        self.assertIn('测试人设：喜欢节奏游戏',captured['rules'])
        self.assertIn(str(memory),[item['path'] for item in captured['manifest']['files']])
        self.assertIn(ROOT,captured['manifest']['shared_conversations'])
        bot.send_group_message.assert_not_awaited()
