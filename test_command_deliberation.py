# -*- coding: utf-8 -*-
"""
Unit and integration tests for Command Deliberation & Generalization Engine.
Verifies:
1. Intent deliberation before responding: distinguishing actionable commands from casual chat.
2. Natural language media generalization and execution via Antigravity search & download engines.
3. Natural language bot control generalization (clear, persona, model, thinking, boost, memory, help).
4. Full integration with OneBotClient and BotClient without rigid slash prefixes.
5. Negative cases (casual chat, evaluative comments, discussions are never executed as commands).
"""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

from test_ai_fixture import ConsoleAgentMock, mock_agent, drain_runtime, orchestrated_response

import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from antigravity_client import AntigravityClient
from bot_client import GeminiQQBot
from command_deliberation import CommandDeliberator, DeliberationResult, default_command_deliberator
from config import AppConfig
from group_memory import GroupMemoryManager
from messaging import SendReceipt
from media_models import MediaAsset, MediaIntent, MessageEnvelope
from media_router import parse_media_intent
from onebot_client import OneBotClient
from persona_manager import PersonaManager
from skills.base import SkillResult


async def accepted_media_send(*args, before_submit=None, **kwargs):
    if before_submit:
        before_submit()
    return SendReceipt(status="accepted", message_id="fixture-sent")


class TestCommandDeliberationEngine(unittest.TestCase):
    """测试意图前置思考引擎对命令与日常聊天的判别及泛化能力"""

    def setUp(self):
        self.deliberator = CommandDeliberator()

    def test_distinguish_commands_from_casual_chat(self):
        """测试精准区分操作命令与普通闲聊"""
        # 日常闲聊（必须判定为非命令）
        chat_cases = [
            "今天中午吃什么",
            "你看这只猫咪图片真逗",
            "为什么天空是蓝色的",
            "你觉得猫和狗谁更可爱",
            "给本喵讲个笑话",
            "我昨天看了一个做饭视频，太搞笑了",
            "请解释字符串 [CQ:image,file=x]",
            "你平时喜欢看动漫吗",
            "我今天工作好累啊，求安慰",
            "你好呀，请问你叫什么名字",
            "这张照片真好看",
            "代码片段中为何报错了",
            # 外部角色与游戏讨论
            "当前游戏中哪个角色最强？",
            "王者荣耀当前哪个角色好玩",
            "原神角色有哪些",
            "崩铁新角色有什么技能",
            # 否定请求
            "不要发这个表情",
            "别发表情包了",
            "我不要看图片",
            # 陈述或情态讨论
            "明天要交文件",
            "我们要拍一个短视频",
            "重要的文件要保存好",
            # 选项选择与集/章
            "选第1个选项",
            "选第2个方案",
            "我要看第2集",
            "我看第3章了",
            # 假设性与疑问提问
            "切换人设为傲娇猫娘女仆会怎么样？",
            "换成人设 傲娇猫娘女仆 之后你会变可爱吗？",
            "人设 傲娇猫娘女仆 到底是什么样的？",
            "切换模型为pro会消耗更多token吗？",
            "开启深度思考会变慢吗？",
            "你看看这个网站: https://example.com/docs",
        ]
        for text in chat_cases:
            res = self.deliberator.deliberate(text)
            self.assertFalse(res.is_command, f"错误将闲聊识别为命令: {text}")
            self.assertEqual(res.command_type, "chat")

        # 操作控制命令（必须判定为命令，无须斜杠即可识别的系统控制命令）
        command_cases = [
            ("清空上下文", "clear"),
            ("重置对话", "clear"),
            ("把刚才聊的都忘掉", "clear"),
            ("切换人设为傲娇猫娘女仆", "persona"),
            ("换成人设 全栈系统架构师", "persona"),
            ("查看可用人设列表", "persona"),
            ("切换模型为pro", "model"),
            ("把模型换成flash", "model"),
            ("查看当前模型", "model"),
            ("查看增强状态", "boost"),
            ("多模态引擎运行状态", "boost"),
            ("查看本群记忆", "memory"),
            ("清空本群专属长期记忆", "clearmemory"),
            ("查看使用帮助", "help"),
        ]
        for text, expected_type in command_cases:
            res = self.deliberator.deliberate(text, is_group=True)
            self.assertTrue(res.is_command, f"未能识别自然语言命令: {text}")
            self.assertEqual(res.command_type, expected_type, f"命令类型不匹配: {text} -> {res.command_type}")

    def test_ai_agent_entity_search_not_intercepted_by_media_algorithm(self):
        """测试所有自然语言媒体检索与实体查询需求绝不被算法死板拦截，全部交给Agent大模型推理与技能调度"""
        agent_cases = [
            "帮我网络搜索初雪樱女主的立绘发到群里",
            "帮我搜初雪樱女主立绘",
            "在网上搜初雪樱女主立绘发出来",
            "网络检索初雪樱女主的立绘",
            "帮我把互联网上的雪村奈々子的信息找出来",
            "帮我网络搜索男主的立绘",
            "来两张橘猫图片发我",
            "整点星空手机壁纸",
            "我想看可爱柴犬的照片",
            "搜一个两分钟以内的做咖啡短视频",
            "把刚才的回答整理成文本发我",
            "搜一张初音未来的壁纸发群里",
            "发几张原神甘雨的图",
            "换一张壁纸",
            "换个头像",
        ]
        for text in agent_cases:
            res = self.deliberator.deliberate(text, is_group=True)
            self.assertFalse(res.is_command, f"错误将网络检索实体请求拦截为算法命令: {text}")
            self.assertEqual(res.command_type, "chat")

    def test_slash_commands_backward_compatibility(self):
        """测试传统斜杠指令保持100%兼容"""
        cases = [
            ("/clear", "clear"),
            ("/persona 傲娇猫娘女仆", "persona"),
            ("/model pro", "model"),
            ("/boost", "boost"),
            ("/memory", "memory"),
            ("/clearmemory", "clearmemory"),
            ("/help", "help"),
            ("/image 橘猫", "media"),
            ("/video 猫咪", "media"),
            ("/next", "media"),
            ("/pick 1", "media"),
        ]
        for raw, expected_type in cases:
            res = self.deliberator.deliberate(raw)
            self.assertTrue(res.is_command)
            self.assertEqual(res.command_type, expected_type)

    def test_media_natural_language_parameter_extraction(self):
        """测试自然语言多模态请求中数量、比例、时长与关键词的精准提取"""
        intent1 = parse_media_intent("来两张橘猫的图片发我")
        self.assertIsNotNone(intent1)
        self.assertEqual(intent1.count, 2)
        self.assertEqual(intent1.kind, "image")
        self.assertIn("橘猫", intent1.query)

        intent2 = parse_media_intent("搜一个两分钟以内的做咖啡短视频")
        self.assertIsNotNone(intent2)
        self.assertEqual(intent2.kind, "video")
        self.assertEqual(intent2.max_duration, 120)
        self.assertIn("做咖啡", intent2.query)

        intent3 = parse_media_intent("整张9:16的星空壁纸")
        self.assertIsNotNone(intent3)
        self.assertEqual(intent3.aspect_ratio, "9:16")
        self.assertIn("星空", intent3.query)

        intent4 = parse_media_intent("把上一条回答存成文本发我")
        self.assertIsNotNone(intent4)
        self.assertEqual(intent4.operation, "export")
        self.assertEqual(intent4.extension, "txt")

    def test_private_vs_group_memory_deliberation(self):
        """测试私聊与群聊下清空记忆的智能归属（私聊对应会话clear，群聊对应clearmemory）"""
        res_private = self.deliberator.deliberate("清空记忆", is_group=False)
        self.assertTrue(res_private.is_command)
        self.assertEqual(res_private.command_type, "clear")

        res_group = self.deliberator.deliberate("清空本群专属长期记忆", is_group=True)
        self.assertTrue(res_group.is_command)
        self.assertEqual(res_group.command_type, "clearmemory")

    def test_ai_deliberation_fallback_and_thinking(self):
        """测试 AI 深度意图思考分析复杂自然语言指令（包含 Markdown 代码块与前导文本的稳健解析）"""
        mock_ai = mock_agent()
        mock_ai.chat = ConsoleAgentMock(return_value='分析如下：\n```json\n{"is_command": true, "command_type": "clear", "command_name": "clear"}\n```')

        async def run_async():
            res = await self.deliberator.deliberate_async(
                "请把我们刚才的所有对话全部清空重来",
                ai_client=mock_ai,
                use_ai_thinking=True
            )
            self.assertTrue(res.is_command)
            self.assertEqual(res.command_type, "clear")

        asyncio.run(run_async())


class TestOneBotNaturalLanguageCommandExecution(unittest.TestCase):
    """测试 OneBotClient 下自然语言命令泛化与搜索下载引擎闭环执行"""

    def setUp(self):
        vision = patch("visual_verification.verify_media_image", new=AsyncMock(return_value={"matched": True}))
        vision.start()
        self.addCleanup(vision.stop)
        self.tmp_dir = tempfile.mkdtemp()
        self.cfg = AppConfig()
        self.cfg.bot.master_id = "master_123"
        self.cfg.media.cache_dir = os.path.join(self.tmp_dir, "media_cache")
        self.cfg.media.database_path = os.path.join(self.tmp_dir, "media.sqlite3")
        self.mock_ai = mock_agent()
        self.mock_ai.chat = ConsoleAgentMock(return_value="普通闲聊回复")
        self.memory = GroupMemoryManager(storage_dir=self.tmp_dir)
        self.client = OneBotClient(
            config=self.cfg,
            antigravity_client=self.mock_ai,
            memory_manager=self.memory
        )
        self.client._is_ws_connected = lambda: True
        self.client.send_group_message = AsyncMock()
        self.client.send_private_message = AsyncMock()

    def tearDown(self):
        import shutil
        asyncio.run(self.client._close_runtime())
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_boost_status_contains_generalization_and_engines(self):
        """测试 /boost 或 自然语言查询增强状态 返回包含命令泛化与自带引擎就绪说明"""
        async def run_test():
            handled = await self.client._handle_command("查看增强状态", group_id="1001", is_group=True)
            self.assertTrue(handled)
            await drain_runtime(self.client)

        asyncio.run(run_test())
        self.client.send_group_message.assert_called_once()
        msg = self.client.send_group_message.call_args[0][1]
        self.assertIn("多模态与原生视觉识别引擎状态", msg)
        self.assertIn("独立识别分支", msg)
        self.assertIn("CLI reply/deliver --finish", msg)
        self.assertIn("意图思考与命令泛化引擎", msg)
        self.assertIn("自带搜索与多模态下载引擎", msg)

    def test_natural_language_clear_context(self):
        """测试无需斜杠前缀，自然语言'清空上下文'能够重置会话"""
        async def run_test():
            handled = await self.client._handle_command("清空上下文", group_id="1001", is_group=True)
            self.assertTrue(handled)
            await drain_runtime(self.client)

        asyncio.run(run_test())
        self.client.send_group_message.assert_called_once()
        msg = self.client.send_group_message.call_args[0][1]
        self.assertIn("清空当前对话上下文", msg)

    def test_natural_language_switch_model_and_removed_speed_control(self):
        """测试自然语言切换模型与调整思考深度"""
        async def run_test():
            # 切换模型为 pro
            handled_model = await self.client._handle_command("切换模型为pro", group_id="1001", is_group=True)
            self.assertTrue(handled_model)
            await drain_runtime(self.client)

            # 调整思考为 high
            handled_thinking = await self.client._handle_command("开启深度思考", group_id="1001", is_group=True)
            self.assertTrue(handled_thinking)
            await drain_runtime(self.client)

        asyncio.run(run_test())
        calls = self.client.send_group_message.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn("pro", calls[0][0][1])
        self.assertIn("未知命令", calls[1][0][1])

    def test_natural_language_media_drives_search_and_download_engines(self):
        """测试自然语言请求图片时，消息流转至Agent大模型理解，并由Agent调度send_image技能闭环执行"""
        async def run_test():
            # 模拟群内发言：艾特机器人“来两张橘猫的图片发我”
            decision = (
                '{"should_reply": true, "action": "skill", "skill": "send_image", '
                '"params": {"query": "可爱橘猫", "count": 2}, "reply": "可爱的橘猫图片来啦~"}'
            )
            from test_agent_artifacts import task_result
            self.mock_ai.chat = ConsoleAgentMock(side_effect=orchestrated_response(decision))

            fake_asset = MediaAsset(kind="image", title="可爱橘猫", url="https://example.com/cat.jpg", provider="wikimedia")
            self.client.send_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {}})
            with patch.object(self.client._get_media_service().searcher, "search", AsyncMock(return_value=[fake_asset])):
                with patch.object(self.client._get_media_service().processor, "prepare", AsyncMock(return_value=fake_asset)):
                    with patch.object(self.client._get_media_service().sender, "send", AsyncMock(side_effect=accepted_media_send)):
                        data = {
                            "group_id": "1001",
                            "user_id": "user_456",
                            "message_id": "msg_001",
                            "raw_message": "[CQ:at,qq=123456] 来两张橘猫的图片发我",
                            "sender": {"nickname": "群友小明"}
                        }
                        self.client.self_id = "123456"
                        await self.client._handle_group_message(data)
                        await drain_runtime(self.client)

            # 验证自然语言请求正确发送给 Agent 大模型，而不是被算法私自截胡
            self.assertEqual(self.mock_ai.chat.await_count, 3)
            # 完成提示由交付层发送一次，不追加模型预先生成的台词。
            sent_texts = [getattr(call.args[1], "body", call.args[1]) for call in self.client.send_group_message.call_args_list]
            self.assertEqual(sent_texts, ["任务完成"])

        asyncio.run(run_test())

    def test_slash_media_command_executes_directly(self):
        """测试显式斜杠指令 /image 保持传统快捷直接执行链路，直接交给反重力下载，不经过闲聊决策"""
        async def run_test():
            from test_agent_artifacts import task_result
            self.mock_ai.chat = ConsoleAgentMock(side_effect=orchestrated_response(""))
            fake_asset = MediaAsset(kind="image", title="可爱橘猫", url="https://example.com/cat.jpg", provider="wikimedia")
            self.client.send_action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {}})
            from messaging import SendReceipt
            with patch.object(self.client._get_media_service().searcher, "search", AsyncMock(return_value=[fake_asset])):
                with patch.object(self.client._get_media_service().processor, "prepare", AsyncMock(return_value=fake_asset)):
                    with patch.object(self.client._get_media_service().sender, "send", AsyncMock(side_effect=accepted_media_send)):
                        data = {
                            "group_id": "1001",
                            "user_id": "user_456",
                            "message_id": "msg_slash_01",
                            "raw_message": "[CQ:at,qq=123456] /image 橘猫",
                            "sender": {"nickname": "群友小明"}
                        }
                        self.client.self_id = "123456"
                        await self.client._handle_group_message(data)
                        await drain_runtime(self.client)

            # 验证斜杠指令直接进入媒体引擎，仅调用后台任务大模型
            self.assertEqual(self.mock_ai.chat.await_count, 2)
            self.assertIn("本任务输出目录", self.mock_ai.chat.await_args_list[0].args[1])
            self.assertIn("task_result", self.mock_ai.chat.await_args_list[1].args[1])

        asyncio.run(run_test())

    def test_ordinary_chat_not_intercepted_by_command_engine(self):
        """测试普通闲聊正常流转至大模型聊天，未被误判为命令"""
        async def run_test():
            data = {
                "group_id": "1001",
                "user_id": "user_456",
                "message_id": "msg_002",
                "raw_message": "[CQ:at,qq=123456] 今天中午吃什么好呀？",
                "sender": {"nickname": "群友小明"}
            }
            self.client.self_id = "123456"
            await self.client._handle_group_message(data)
            # 普通聊天应触发大模型生成
            self.mock_ai.chat.assert_called_once()

        asyncio.run(run_test())

    def test_agent_media_search_drives_ai_chat_and_skill_execution(self):
        """测试用户请求@雪村フブキ 帮我网络搜索初雪樱女主的立绘发到群里 时，消息流转至Agent并通过Skill执行，无机械摘要刷屏"""
        async def run_test():
            self.client.self_id = "123456"
            decision = (
                '{"should_reply": true, "action": "skill", "skill": "send_image", '
                '"params": {"query": "初雪樱 玉树樱 立绘"}, "reply": "初雪樱的女主玉树樱的立绘这就发出来啦~"}'
            )
            from test_agent_artifacts import task_result
            self.mock_ai.chat = ConsoleAgentMock(side_effect=orchestrated_response(decision))

            fake_asset = MediaAsset(kind="image", title="玉树樱立绘", url="https://example.com/sakura.png", provider="bing")
            media_svc = self.client._get_media_service()

            with patch.object(media_svc.searcher, "search", AsyncMock(return_value=[fake_asset])):
                with patch.object(media_svc.processor, "prepare", AsyncMock(return_value=fake_asset)):
                    with patch.object(media_svc.sender, "send", AsyncMock(side_effect=accepted_media_send)):
                        data = {
                            "group_id": "1129846525",
                            "user_id": "user_789",
                            "message_id": "msg_boost_01",
                            "raw_message": "[CQ:at,qq=123456] 帮我网络搜索初雪樱女主的立绘发到群里",
                            "sender": {"nickname": "群友提问"}
                        }
                        await self.client._handle_group_message(data)
                        await drain_runtime(self.client)

            # 主会话派发，任务分支执行，控制台直接交付。
            self.assertEqual(self.mock_ai.chat.await_count, 3)
            self.assertIn("本任务输出目录", self.mock_ai.chat.await_args_list[1].args[1])
            # 最终交付仅一条提示，不发送内部总结或第二条人设台词。
            sent_texts = [getattr(call.args[1], "body", call.args[1]) for call in self.client.send_group_message.call_args_list]
            self.assertEqual(sent_texts, ["任务完成"])
            # 验证机械摘要没有发送到群里（silent_summary 生效）
            self.assertFalse(any("未能获取" in t for t in sent_texts))
            self.assertFalse(any("准备发送" in t for t in sent_texts))

        asyncio.run(run_test())


class TestBotClientNaturalLanguageCommandExecution(unittest.TestCase):
    """测试官方通道 BotClient 下自然语言命令泛化与执行"""

    def setUp(self):
        try:
            self.loop = asyncio.get_event_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
        self.tmp_dir = tempfile.mkdtemp()
        import shutil
        tmp_persona = os.path.join(self.tmp_dir, "personas.yaml")
        shutil.copy("personas.yaml", tmp_persona)
        self.cfg = AppConfig()
        self.cfg.bot.master_id = "master_openid_999"
        self.mock_ai = mock_agent()
        self.mock_ai.chat = ConsoleAgentMock(return_value="官方机器人闲聊回复")
        self.memory = GroupMemoryManager(storage_dir=self.tmp_dir)
        self.personas = PersonaManager(storage_path=tmp_persona)
        self.client = GeminiQQBot(
            config=self.cfg,
            antigravity_client=self.mock_ai,
            persona_manager=self.personas,
            memory_manager=self.memory
        )

    def tearDown(self):
        import shutil
        asyncio.run(self.client._close_runtime())
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_bot_client_natural_language_clear_and_boost(self):
        """测试官方客户端无需斜杠前缀响应重置与增强状态"""
        replies = []
        async def reply_func(text, **options):
            replies.append(text)

        async def run_test():
            # 1. 自然语言清空会话
            await self.client._handle_message_core(
                session_key="group_room123",
                raw_content="帮我重置一下上下文",
                user_openid="user_abc",
                reply_func=reply_func
            )
            await drain_runtime(self.client)
            # 2. 自然语言查看多模态增强状态
            await self.client._handle_message_core(
                session_key="group_room123",
                raw_content="查看多模态增强状态",
                user_openid="user_abc",
                reply_func=reply_func
            )
            await drain_runtime(self.client)

        asyncio.run(run_test())
        self.assertEqual(len(replies), 2)
        self.assertIn("清空当前对话上下文", replies[0])
        self.assertIn("多模态与原生视觉识别引擎状态", replies[1])
        self.assertIn("意图思考与命令泛化引擎", replies[1])
        self.assertIn("自带搜索与多模态下载引擎", replies[1])

    def test_bot_client_natural_language_persona_switch(self):
        """测试主人账号使用自然语言切换全局人设"""
        replies = []
        async def reply_func(text, **options):
            replies.append(text)

        async def run_test():
            await self.client._handle_message_core(
                session_key="c2c_master",
                raw_content="切换人设为傲娇猫娘女仆",
                user_openid="master_openid_999",
                reply_func=reply_func
            )
            await drain_runtime(self.client)

        asyncio.run(run_test())
        self.assertEqual(len(replies), 1)
        self.assertIn("已切换人设：傲娇猫娘女仆", replies[0])
        self.assertEqual(self.client.personas.active_persona_name, "傲娇猫娘女仆")

    def test_bot_client_private_message_skill_execution(self):
        async def run_test():
            decision = '{"should_reply":false,"tasks":[{"type":"image","task":"初雪樱 玉树樱 立绘"}]}'
            self.mock_ai.chat = ConsoleAgentMock(side_effect=orchestrated_response(decision))
            replies = AsyncMock()
            prepared = SkillResult(True, "已核对", action_type="prepared", data={"files":[], "answer":"已核对的立绘"})
            with patch("skills.task_skills.RunTaskSkill.execute", AsyncMock(return_value=prepared)) as worker:
                await self.client._handle_message_core("c2c_user123", "初雪樱女主的立绘可以看看吗", replies, user_openid="user123")
                await drain_runtime(self.client)
                worker.assert_awaited_once()
                self.assertIn("初雪樱", worker.await_args.args[0]["task"])
                replies.assert_awaited_once()
            self.assertEqual(replies.await_args.args[0], "任务完成")
            await self.client._close_runtime()
        asyncio.run(run_test())



if __name__ == "__main__":
    unittest.main()
