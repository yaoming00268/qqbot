# -*- coding: utf-8 -*-
"""
Comprehensive Unit Tests for QQBot Enhancements:
- Prompt Builder (length limits, action description filtering, user/system prompt composition)
- Group Memory & Compression (persistent JSON storage, threshold compression, participant profiling)
- Group Context Buffer (sliding window, prompt formatting)
- Configuration & Serialization
- Bot Client Message Handling (splitting, mention detection, master privilege, deduplication)
"""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

from test_ai_fixture import ConsoleAgentMock, drain_runtime, mock_agent

import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, AsyncMock

from config import AppConfig, BotBehaviorConfig
from group_memory import (
    GroupMemory,
    GroupMemoryManager,
    GroupContextBuffer,
    ParticipantInfo,
    InteractionRecord
)
from prompt_builder import (
    strip_action_descriptions,
    compose_system_prompt,
    compose_user_prompt
)
from bot_client import split_message, clean_mention_text, GeminiQQBot, is_master_user
from antigravity_client import AntigravityClient, UserSession
from persona_manager import PersonaManager, PersonaPreset
from main import diagnose_antigravity
from gui import BotStudioGUI, create_vector_icon


class TestPromptBuilder(unittest.TestCase):
    """测试提示词构建器与动作过滤算法"""

    def test_strip_action_descriptions_basic(self):
        # 常见括号动作描写
        raw = "（耳朵软软地耷拉下来...） 哼！笨蛋主人，你到底在看什么啊！（两颊泛红别过头去）"
        expected = "哼！笨蛋主人，你到底在看什么啊！"
        self.assertEqual(strip_action_descriptions(raw), expected)

    def test_strip_action_descriptions_ascii_and_fullwidth(self):
        # 半角和全角括号混合
        raw = "(耳朵抖了抖) 姐姐刚才做的松饼好了。(尾巴诚实地在身后摇晃) 给你一块喵！"
        expected = "姐姐刚才做的松饼好了。 给你一块喵！"
        self.assertEqual(strip_action_descriptions(raw), expected)

    def test_strip_action_descriptions_preserves_math_and_bold(self):
        # 保留纯数字公式与无汉字的普通括号，同时保留 Markdown 粗体 **...**
        raw = "(脸红) 今天的算式是 (1 + 2) * 3 = 9，**重点通知** 请大家注意！"
        res = strip_action_descriptions(raw)
        self.assertIn("(1 + 2) * 3 = 9", res)
        self.assertIn("**重点通知**", res)
        self.assertNotIn("脸红", res)

    def test_strip_action_descriptions_preserves_explanatory_parentheses(self):
        # 保留知识性解释括号
        raw = "（轻笑）Python（一种高级编程语言）非常好用，包含各种标准库（例如：json、sys）。"
        res = strip_action_descriptions(raw)
        self.assertIn("Python（一种高级编程语言）非常好用", res)
        self.assertIn("（例如：json、sys）", res)
        self.assertNotIn("轻笑", res)

    def test_strip_action_descriptions_pure_actions_fallback_not_unquoted(self):
        # 纯动作被完全滤空时必须回退到 fallback，杜绝将动作剥壳当作台词输出
        raw1 = "（默默注视着你）"
        self.assertEqual(strip_action_descriptions(raw1), "……")

        raw2 = "（双手叉腰气鼓鼓地看着你）"
        self.assertEqual(strip_action_descriptions(raw2), "……")

        # 若包含显式台词引号或角色前缀，则正确提取对白
        raw3 = "（ふぶき：“才没有在等主人呢！”）"
        self.assertEqual(strip_action_descriptions(raw3), "才没有在等主人呢！")

        raw4 = "（ふぶき：听懂了没有啊笨蛋！）"
        self.assertEqual(strip_action_descriptions(raw4), "听懂了没有啊笨蛋！")

    def test_strip_action_descriptions_empty_and_fallback(self):
        # 纯动作被完全滤空时的优雅回退
        raw = "（害羞地把头埋进长袖子里）"
        fallback = "……哼！"
        self.assertEqual(strip_action_descriptions(raw, fallback=fallback), fallback)
        self.assertEqual(strip_action_descriptions(""), "")

    def test_strip_action_descriptions_multiline(self):
        raw = "（耳朵耷拉下来\n尾巴垂在身后）\n对不起啦，我不是故意的喵……"
        res = strip_action_descriptions(raw)
        self.assertEqual(res, "对不起啦，我不是故意的喵……")

    def test_compose_system_prompt(self):
        base = (
            "你叫雪村ふぶき，是千绘莉的妹妹。\n"
            "- 生动动作神态：在回答时自然穿插括号动作神态描写，如（耳朵抖了抖）。"
        )
        # 1. 禁用动作描写时保留用户正文，由独立格式约束覆盖表现形式。
        p_no_action = compose_system_prompt(base, max_reply_length=150, enable_action_descriptions=False)
        self.assertIn("单条消息分段阈值为 150 字", p_no_action)
        self.assertIn("纯对话台词模式", p_no_action)
        self.assertIn("不写动作、神态、心理旁白", p_no_action)
        self.assertIn(base, p_no_action)

        # 2. 启用动作描写
        p_with_action = compose_system_prompt(base, max_reply_length=500, enable_action_descriptions=True)
        self.assertIn("单条消息分段阈值为 500 字", p_with_action)
        self.assertIn("动作神态描写可适量使用", p_with_action)

    def test_compose_user_prompt(self):
        user_text = "今天做了什么蛋糕？"
        ctx = "- 群友A: 好想吃甜点"
        mem = "• 群概况摘要: 经常讨论烘焙甜点"

        # 主人发言
        prompt_master = compose_user_prompt(
            user_text=user_text,
            author_name="主人",
            group_context=ctx,
            group_memory=mem,
            is_master=True,
            max_reply_length=100,
            enable_action_descriptions=False,
            is_autonomous=False
        )
        self.assertIn("当前发言者是主人", prompt_master)
        self.assertIn("100 字只是单条分段阈值", prompt_master)
        self.assertIn("纯台词文本输出", prompt_master)
        self.assertIn("好想吃甜点", prompt_master)
        self.assertIn("经常讨论烘焙甜点", prompt_master)
        self.assertIn("当前发言者 [主人]: 今天做了什么蛋糕？", prompt_master)

        # 自主插话
        prompt_auto = compose_user_prompt(
            user_text="今天天气真好",
            author_name="路人乙",
            group_context="",
            group_memory="",
            is_master=False,
            max_reply_length=80,
            enable_action_descriptions=True,
            is_autonomous=True
        )
        self.assertIn("是否接话由语义和对话关系决定", prompt_auto)
        self.assertIn("当前发言者 [路人乙]: 今天天气真好", prompt_auto)


class TestGroupMemory(unittest.TestCase):
    """测试群聊记忆库持久化与智能压缩算法"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mgr = GroupMemoryManager(storage_dir=self.test_dir, max_entries=5)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_record_interaction_and_participant(self):
        grp_id = "test_grp_001"
        self.mgr.record_interaction(
            group_openid=grp_id,
            user_openid="user_alice",
            user_msg="你好呀ふぶき！",
            bot_reply="哼，才没有在等你呢！",
            author_name="爱丽丝"
        )

        mem = self.mgr.get_group_memory(grp_id)
        self.assertEqual(len(mem.recent_interactions), 1)
        self.assertIn("user_alice", mem.participants)
        self.assertEqual(mem.participants["user_alice"].nickname, "爱丽丝")
        self.assertEqual(mem.participants["user_alice"].interaction_count, 1)

        # 检查持久化文件是否真实存在
        file_path = os.path.join(self.test_dir, f"group_{grp_id}.json")
        self.assertTrue(os.path.exists(file_path))

    def test_memory_threshold_compression(self):
        grp_id = "test_grp_002"
        # 连续记录 6 条问答，超过 max_entries=5 触发压缩
        for i in range(6):
            self.mgr.record_interaction(
                group_openid=grp_id,
                user_openid=f"user_{i}",
                user_msg=f"问题测试第{i}条：今天天气如何？",
                bot_reply=f"回答第{i}条：今天晴天喵！",
                author_name=f"群友_{i}"
            )

        mem = self.mgr.get_group_memory(grp_id)
        # 压缩后应保留 keep_count=5 之前溢出的条目已总结到 summary 与 notes 中
        self.assertLessEqual(len(mem.recent_interactions), 5)
        self.assertTrue(len(mem.summary) > 0)
        self.assertIn("活跃常客", mem.summary)

        # 验证提示词注入输出
        prompt = self.mgr.get_memory_prompt(grp_id)
        self.assertIn("【本群专属记忆库", prompt)
        self.assertIn("活跃群成员记忆", prompt)

    def test_clear_memory(self):
        grp_id = "test_grp_003"
        self.mgr.record_interaction(grp_id, "u1", "msg1", "reply1")
        self.assertTrue(self.mgr.clear_group_memory(grp_id))

        new_mem = self.mgr.get_group_memory(grp_id)
        self.assertEqual(len(new_mem.recent_interactions), 0)

    def test_memory_multiple_compressions_preserve_topics(self):
        """测试多轮压缩后，先前的群讨论核心话题不会被单批次结果彻底清空"""
        grp_id = "test_grp_preserve"
        # 第一批讨论：烘焙甜点
        for i in range(5):
            self.mgr.record_interaction(grp_id, f"user_baker_{i}", f"做巧克力蛋糕秘诀{i}", f"烘焙成功{i}")
        mem = self.mgr.get_group_memory(grp_id)
        self.assertIn("蛋糕", mem.summary)

        # 第二批讨论：编程技术
        for i in range(5):
            self.mgr.record_interaction(grp_id, f"user_coder_{i}", f"写Python异步协程{i}", f"代码运行流畅{i}")
        mem = self.mgr.get_group_memory(grp_id)
        # 验证先前批次的蛋糕话题依然保存在 summary 中
        self.assertIn("蛋糕", mem.summary)
        self.assertIn("Python", mem.summary)


class TestGroupContextBuffer(unittest.TestCase):
    """测试群聊近期滑动上下文缓冲区"""

    def test_context_buffer_sliding_window(self):
        buf = GroupContextBuffer(max_buffer_size=3)
        grp = "grp_100"

        buf.add_message(grp, "u1", "张三", "第一句话")
        buf.add_message(grp, "u2", "李四", "第二句话")
        buf.add_message(grp, "u3", "王五", "第三句话")
        buf.add_message(grp, "u4", "赵六", "第四句话")

        msgs = buf.get_recent_messages(grp, count=10)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0]["content"], "第二句话")
        self.assertEqual(msgs[-1]["content"], "第四句话")

        prompt_text = buf.format_for_prompt(grp, count=2)
        self.assertIn("第三句话", prompt_text)
        self.assertIn("第四句话", prompt_text)
        self.assertNotIn("第一句话", prompt_text)

    def test_context_buffer_message_truncation_and_newlines(self):
        """测试超长灌水消息截断与换行单行化"""
        buf = GroupContextBuffer(max_buffer_size=5)
        grp = "grp_long"
        long_content = "第一行内容\n第二行内容\n" + "A" * 600
        buf.add_message(grp, "u_spammer", "灌水者", long_content)

        recent = buf.get_recent_messages(grp, 1)
        self.assertLessEqual(len(recent[0]["content"]), 400)

        prompt = buf.format_for_prompt(grp, 1)
        # 换行符被清洗为空格，且单行限制在安全长度内
        self.assertNotIn("\n第二行内容", prompt)
        self.assertLessEqual(len(prompt.splitlines()[-1]), 250)

    def test_context_buffer_resilient_max_size_types(self):
        """测试 GroupContextBuffer 防御性类型强制转换，杜绝 deque maxlen 非整型崩溃"""
        for val in ["20", 15.8, None, -10, 0, lambda: 5]:
            buf = GroupContextBuffer(val)
            self.assertIsInstance(buf.max_buffer_size, int)
            self.assertGreater(buf.max_buffer_size, 0)
            buf.add_message("grp_test", "u1", "user", "msg")
            self.assertEqual(len(buf.get_recent_messages("grp_test")), 1)

        # 即使实例属性在运行时被篡改也能安全入队
        corrupt_buf = GroupContextBuffer(10)
        corrupt_buf.max_buffer_size = lambda: 5  # 被赋值为函数
        corrupt_buf.add_message("grp_corrupt", "u1", "user", "msg")
        self.assertEqual(len(corrupt_buf.get_recent_messages("grp_corrupt")), 1)

    def test_persona_buffer_nested_and_repeated_runtime_init(self):
        """测试 PersonaBuffer 嵌套防卫、max_buffer_size 属性与 _init_media_runtime 重复调用幂等性"""
        from persona_isolation import PersonaBuffer, PersonaMemory
        from media_integration import MediaClientMixin
        from types import SimpleNamespace

        class MockMediaBot(MediaClientMixin):
            pass

        bot = MockMediaBot()
        bot.app_config = AppConfig()
        bot.account_key = "account_1"
        bot.send_action = MagicMock()
        bot.memory_mgr = GroupMemoryManager("data/memories", 50)
        bot.context_buffer = GroupContextBuffer(max_buffer_size=50)
        preset = SimpleNamespace(id="p_default", name="DefaultPersona")
        bot.personas = SimpleNamespace(
            presets={"p_default": preset},
            get_persona=lambda name: preset,
            get_active_persona=lambda: preset
        )

        # 第一次初始化运行时
        bot._init_media_runtime()
        self.assertIsInstance(bot.context_buffer, PersonaBuffer)
        self.assertIsInstance(bot.memory_mgr, PersonaMemory)
        self.assertIsInstance(bot.context_buffer.max_buffer_size, int)
        self.assertEqual(bot.context_buffer.max_buffer_size, 50)

        first_buf = bot.context_buffer
        first_mem = bot.memory_mgr

        # 第二次初始化运行时（模拟连接登录成功后再次调用）
        bot._init_media_runtime()
        self.assertIs(bot.context_buffer, first_buf)
        self.assertIs(bot.memory_mgr, first_mem)

        # 手动双层包裹验证 PersonaBuffer 自动解包与 max_buffer_size 属性
        p2 = PersonaBuffer(bot, bot.context_buffer)
        self.assertIsInstance(p2.max_buffer_size, int)
        self.assertEqual(p2.max_buffer_size, 50)
        self.assertIsInstance(p2.original, GroupContextBuffer)

        # 消息正常加入滑动窗口，不得抛出 TypeError: an integer is required
        p2.add_message("grp_nested", "u_test", "tester", "hello world")
        msgs = p2.get_recent_messages("grp_nested")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["content"], "hello world")

        # 验证非可调用属性安全透传，不落入 __getattr__ 动态函数陷阱
        self.assertFalse(callable(bot.context_buffer.max_buffer_size))
        self.assertIsInstance(bot.context_buffer._buffers, dict)
        self.assertTrue(str(bot.memory_mgr.storage_dir).replace("\\", "/").endswith("data/memories"))
        self.assertEqual(bot.memory_mgr.max_entries, 50)
        self.assertFalse(callable(bot.memory_mgr.storage_dir))
        self.assertFalse(callable(bot.memory_mgr.max_entries))

        # 验证 _control 对象持久保留，watcher 不会因登录握手重置而脱节
        first_control = bot._control
        bot._init_media_runtime()
        self.assertIs(bot._control, first_control)

    def test_onebot_config_ws_url_and_token_bidirectional_sync(self):
        """测试 OneBotConfig ws_url 与 access_token 在根字段与激活账号之间双向同步"""
        cfg = AppConfig()
        # 修改根 ws_url 同步到当前激活账号
        cfg.onebot.ws_url = "ws://127.0.0.1:59999"
        self.assertEqual(cfg.onebot.get_active_account().ws_url, "ws://127.0.0.1:59999")
        self.assertEqual(cfg.onebot.accounts["account_1"].ws_url, "ws://127.0.0.1:59999")

        # 修改 access_token 同步到当前激活账号
        cfg.onebot.access_token = "secret_token_123"
        self.assertEqual(cfg.onebot.get_active_account().access_token, "secret_token_123")
        self.assertEqual(cfg.onebot.accounts["account_1"].access_token, "secret_token_123")

        # 切换账号同步更新根 ws_url 与 token
        cfg.onebot.switch_account("account_2")
        self.assertEqual(cfg.onebot.active_account, "account_2")
        self.assertEqual(cfg.onebot.ws_url, cfg.onebot.accounts["account_2"].ws_url)

        # 在新激活账号下修改根 ws_url 同步更新 account_2
        cfg.onebot.ws_url = "ws://127.0.0.1:59998"
        self.assertEqual(cfg.onebot.accounts["account_2"].ws_url, "ws://127.0.0.1:59998")
        # account_1 保持不受影响
        self.assertEqual(cfg.onebot.accounts["account_1"].ws_url, "ws://127.0.0.1:59999")


class TestMessageSplitAndClean(unittest.TestCase):
    """测试消息切分与 @ 清理"""

    def test_split_message_under_limit(self):
        text = "短文本测试"
        self.assertEqual(split_message(text, 100), ["短文本测试"])

    def test_split_message_paragraphs(self):
        text = "段落一\n段落二\n段落三"
        chunks = split_message(text, max_length=6)
        self.assertTrue(len(chunks) >= 3)
        for c in chunks:
            self.assertLessEqual(len(c), 6)

    def test_split_message_safety_and_prefix(self):
        """测试 max_length <= 0 时边界安全防死循环，以及 first_chunk_prefix 长度合规"""
        # 防御 <= 0 参数，不卡死进程
        safe_chunks = split_message("测试安全文本", 0)
        self.assertTrue(len(safe_chunks) >= 1)

        safe_neg = split_message("测试负数参数", -10)
        self.assertTrue(len(safe_neg) >= 1)

        # 带 @mention 前缀切分测试
        prefix = "<@!12345678901234567890>"
        text = "第一段文本内容极其丰富而且字数较多\n第二段文本内容更加丰富且长"
        chunks = split_message(text, max_length=35, first_chunk_prefix=prefix)
        self.assertTrue(len(chunks) >= 2)
        # 首段包含前缀且总长度严格小于等于 35
        self.assertTrue(chunks[0].startswith(prefix))
        self.assertLessEqual(len(chunks[0]), 35)

    def test_clean_mention_text(self):
        raw = "<@!123456789> 你好机器人"
        self.assertEqual(clean_mention_text(raw), "你好机器人")

        raw2 = "@雪村ふぶき 今天心情好吗？"
        self.assertEqual(clean_mention_text(raw2), "今天心情好吗？")

    def test_clean_mention_text_multiple(self):
        # 多重连续提及标签过滤
        raw = "<@!bot_001> <@!user_002> @ふぶき 大家下午好呀"
        self.assertEqual(clean_mention_text(raw), "大家下午好呀")


class TestBotClientMentionsAndConfig(unittest.TestCase):
    """测试机器人核心判断逻辑"""

    def test_config_defaults(self):
        cfg = AppConfig()
        self.assertFalse(cfg.bot.autonomous_enabled)
        self.assertEqual(cfg.bot.reply_probability, 0.2)
        self.assertEqual(cfg.bot.mention_probability, 0.5)
        self.assertEqual(cfg.bot.cooldown_seconds, 30)
        self.assertEqual(cfg.bot.context_message_count, 10)
        self.assertEqual(cfg.bot.master_id, "")
        self.assertTrue(cfg.bot.memory_enabled)
        self.assertEqual(cfg.bot.memory_max_entries, 15)

    def test_config_yaml_load_example_file(self):
        cfg = AppConfig.load("config.yaml.example")
        self.assertEqual(cfg.bot.max_reply_length, 1200)
        self.assertTrue(cfg.bot.enable_action_descriptions)
        self.assertTrue(cfg.bot.memory_enabled)

    def test_group_message_simulated_dispatch(self):
        import asyncio

        async def run_async_test():
            tmp_mem_dir = tempfile.mkdtemp()
            try:
                cfg = AppConfig()
                cfg.bot.autonomous_enabled = False
                cfg.bot.master_id = "master_123"
                cfg.bot.max_reply_length = 80
                cfg.bot.enable_action_descriptions = False

                mock_ai = mock_agent()
                mock_ai.chat = ConsoleAgentMock(return_value="（耳朵抖了抖）好的主人！我听到了喵！")

                bot = GeminiQQBot(
                    config=cfg,
                    antigravity_client=mock_ai,
                    memory_manager=GroupMemoryManager(storage_dir=tmp_mem_dir)
                )
                bot._connection = MagicMock()
                bot._connection.state = MagicMock()
                bot._connection.state.robot = MagicMock(id="bot_999", name="雪村ふぶき")

                # 1. 普通人发非 @ 消息，且自主未启用 -> 不回复，但记录上下文
                normal_msg = MagicMock()
                normal_msg.id = "msg_001"
                normal_msg.group_openid = "grp_test"
                normal_msg.author = MagicMock(member_openid="user_normal")
                normal_msg.content = "大家吃午饭了吗？"
                normal_msg.mentions = []
                normal_msg.reply = AsyncMock()

                await bot._handle_group_message(normal_msg, is_at_event=False)
                normal_msg.reply.assert_not_called()
                # 验证已被记录进上下文
                ctx = bot.context_buffer.get_recent_messages("grp_test", 10)
                self.assertEqual(len(ctx), 1)
                self.assertEqual(ctx[0]["content"], "大家吃午饭了吗？")

                # 2. 主人发非 @ 消息 -> 100% 必定回复，且动作神态被过滤
                master_msg = MagicMock()
                master_msg.id = "msg_002"
                master_msg.group_openid = "grp_test"
                master_msg.author = MagicMock(member_openid="master_123")
                master_msg.content = "ふぶき在吗？"
                master_msg.mentions = []
                master_msg.reply = AsyncMock()

                await bot._handle_group_message(master_msg, is_at_event=False)
                master_msg.reply.assert_called()
                # 校验回复内容中（耳朵抖了抖）已被滤除，仅剩纯台词
                call_content = master_msg.reply.call_args[1]["content"]
                self.assertNotIn("耳朵抖了抖", call_content)
                self.assertIn("好的主人！我听到了喵！", call_content)

                # 3. 消息去重测试：相同 msg_id 再次传入不应重复触发 reply
                master_msg.reply.reset_mock()
                await bot._handle_group_message(master_msg, is_at_event=False)
                master_msg.reply.assert_not_called()

                # 4. 旧概率与冷却配置不再阻断模型判断应继续的对话
                cfg.bot.autonomous_enabled = True
                cfg.bot.reply_probability = 1.0  # 100% 触发概率
                cfg.bot.mention_probability = 1.0 # 100% @ 提及
                cfg.bot.cooldown_seconds = 60
                mock_ai.chat.return_value = '{"should_reply":true,"action":"none","reply":"好的主人！我听到了喵！"}'

                auto_msg1 = MagicMock()
                auto_msg1.id = "msg_003"
                auto_msg1.group_openid = "grp_cooldown"
                auto_msg1.author = MagicMock(member_openid="user_talker")
                auto_msg1.content = "有人知道怎么烘焙吗？"
                auto_msg1.mentions = []
                auto_msg1.reply = AsyncMock()

                await bot._handle_group_message(auto_msg1, is_at_event=False)
                auto_msg1.reply.assert_called()
                # 普通聊天由控制台决定引用，不按旧概率自动 @。
                sent_content = auto_msg1.reply.call_args[1]["content"]
                self.assertFalse(sent_content.startswith("<@!user_talker>"))

                # 紧随其后的消息仍自然接话，不受旧冷却设置阻挡
                auto_msg2 = MagicMock()
                auto_msg2.id = "msg_004"
                auto_msg2.group_openid = "grp_cooldown"
                auto_msg2.author = MagicMock(member_openid="user_talker2")
                auto_msg2.content = "我也想知道"
                auto_msg2.mentions = []
                auto_msg2.reply = AsyncMock()

                await bot._handle_group_message(auto_msg2, is_at_event=False)
                auto_msg2.reply.assert_called()

                # 5. mention_probability = 0.0 时不添加 @ 标签
                cfg.bot.cooldown_seconds = 0
                cfg.bot.mention_probability = 0.0
                auto_msg3 = MagicMock()
                auto_msg3.id = "msg_005"
                auto_msg3.group_openid = "grp_nomention"
                auto_msg3.author = MagicMock(member_openid="user_talker3")
                auto_msg3.content = "大家下午好"
                auto_msg3.mentions = []
                auto_msg3.reply = AsyncMock()

                await bot._handle_group_message(auto_msg3, is_at_event=False)
                auto_msg3.reply.assert_called()
                nomention_content = auto_msg3.reply.call_args[1]["content"]
                self.assertFalse(nomention_content.startswith("<@!"))
            finally:
                shutil.rmtree(tmp_mem_dir, ignore_errors=True)

        asyncio.run(run_async_test())

    def test_memory_stats_and_clear_all(self):
        tmp_dir = tempfile.mkdtemp()
        mgr = GroupMemoryManager(storage_dir=tmp_dir)

        mgr.record_interaction("grp_a", "u1", "msg a", "reply a")
        mgr.record_interaction("grp_b", "u2", "msg b", "reply b")

        stats = mgr.get_all_memories_stat()
        self.assertEqual(len(stats), 2)
        group_ids = [s["group_openid"] for s in stats]
        self.assertIn("grp_a", group_ids)
        self.assertIn("grp_b", group_ids)

        cleared_count = mgr.clear_all_memories()
        self.assertEqual(cleared_count, 2)
        self.assertEqual(len(mgr.get_all_memories_stat()), 0)

        shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_is_master_user_parsing(self):
        """测试主人 ID 解析（单 ID、多 ID、全角分号、空白处理）"""
        cfg_single = "master_001"
        self.assertTrue(is_master_user(cfg_single, "master_001"))
        self.assertFalse(is_master_user(cfg_single, "master_002"))

        # 包含空白字符与中英文逗号、全角半角分号
        cfg_multi = " master_001 ， master_002； master_003 ; master_004 , "
        self.assertTrue(is_master_user(cfg_multi, "master_001"))
        self.assertTrue(is_master_user(cfg_multi, "master_002"))
        self.assertTrue(is_master_user(cfg_multi, "master_003"))
        self.assertTrue(is_master_user(cfg_multi, "master_004"))
        self.assertFalse(is_master_user(cfg_multi, "master_005"))
        self.assertFalse(is_master_user("", "master_001"))
        self.assertFalse(is_master_user(cfg_multi, ""))

    def test_self_message_suppression_and_nickname_command(self):
        """测试机器人自身消息循环过滤与昵称绑定指令"""
        import asyncio

        async def run_async():
            tmp_mem_dir = tempfile.mkdtemp()
            try:
                cfg = AppConfig()
                cfg.bot.autonomous_enabled = True
                cfg.bot.reply_probability = 1.0
                cfg.bot.cooldown_seconds = 0
                cfg.qq.appid = "app_12345"

                mock_ai = mock_agent()
                mock_ai.chat = ConsoleAgentMock(return_value="这是测试回复")

                bot = GeminiQQBot(
                    config=cfg,
                    antigravity_client=mock_ai,
                    memory_manager=GroupMemoryManager(storage_dir=tmp_mem_dir)
                )
                bot._connection = MagicMock()
                bot._connection.state = MagicMock()
                bot._connection.state.robot = MagicMock(id="bot_999", member_openid="bot_openid_999", name="雪村ふぶき")

                # 1. 机器人自身消息应当被直接丢弃，不记录也不回复
                self_msg = MagicMock()
                self_msg.id = "self_msg_001"
                self_msg.group_openid = "grp_self"
                self_msg.author = MagicMock(member_openid="bot_openid_999")
                self_msg.content = "我自己说的话"
                self_msg.mentions = []
                self_msg.reply = AsyncMock()

                await bot._handle_group_message(self_msg, is_at_event=False)
                self_msg.reply.assert_not_called()
                ctx = bot.context_buffer.get_recent_messages("grp_self", 10)
                self.assertEqual(len(ctx), 0)

                # 1.1 其他被标记为 bot=True 的机器人未 @ 时亦不自主插话
                other_bot_msg = MagicMock()
                other_bot_msg.id = "other_bot_msg_001"
                other_bot_msg.group_openid = "grp_self"
                other_bot_msg.author = MagicMock(member_openid="other_bot_999", bot=True)
                other_bot_msg.content = "其他机器人的播报"
                other_bot_msg.mentions = []
                other_bot_msg.reply = AsyncMock()

                await bot._handle_group_message(other_bot_msg, is_at_event=False)
                other_bot_msg.reply.assert_not_called()

                # 2. 昵称指令测试：/name 艾丽丝
                name_msg = MagicMock()
                name_msg.id = "cmd_msg_002"
                name_msg.group_openid = "grp_self"
                name_msg.author = MagicMock(member_openid="user_alice")
                name_msg.content = "/name 艾丽丝"
                name_msg.mentions = [MagicMock(id="bot_999")]
                name_msg.reply = AsyncMock()

                await bot._handle_group_message(name_msg, is_at_event=True)
                await drain_runtime(bot)
                name_msg.reply.assert_called_once()
                reply_text = name_msg.reply.call_args[1]["content"]
                self.assertIn("已记录称呼：艾丽丝", reply_text)

                # 验证群记忆库中该成员昵称已被更新
                mem = bot.memory_mgr.get_group_memory("grp_self")
                self.assertEqual(mem.participants["user_alice"].nickname, "艾丽丝")
                await bot._close_runtime()
            finally:
                shutil.rmtree(tmp_mem_dir, ignore_errors=True)

        asyncio.run(run_async())

    def test_c2c_message_formatting_and_action_stripping(self):
        """测试 C2C 私聊消息处理中字数约束与动作过滤"""
        import asyncio

        async def run_async():
            cfg = AppConfig()
            cfg.bot.max_reply_length = 50
            cfg.bot.enable_action_descriptions = False

            mock_ai = mock_agent()
            # 模拟模型输出包含动作神态的长文本
            mock_ai.chat = ConsoleAgentMock(return_value="（两颊通红别过头去）哼！才没有很想你呢笨蛋！")

            bot = GeminiQQBot(config=cfg, antigravity_client=mock_ai)

            c2c_msg = MagicMock()
            c2c_msg.id = "c2c_001"
            c2c_msg.author = MagicMock(user_openid="user_bob")
            c2c_msg.content = "ふぶき在吗？"
            c2c_msg.attachments = []
            c2c_msg.message_reference = None
            c2c_msg.reply = AsyncMock()

            await bot.on_c2c_message_create(c2c_msg)
            await asyncio.gather(*bot._runtime.chat_workers.values())
            await bot._close_runtime()
            c2c_msg.reply.assert_called_once()
            sent_content = c2c_msg.reply.call_args[1]["content"]
            # 动作描写已被彻底滤除
            self.assertNotIn("两颊通红", sent_content)
            self.assertIn("才没有很想你呢笨蛋！", sent_content)

            # 验证传入 AI 的 user prompt 中包含格式与字数约束
            call_prompt = mock_ai.chat.call_args[0][1]
            self.assertIn("气泡", call_prompt)
            self.assertIn("纯对话台词模式", call_prompt)

        asyncio.run(run_async())

    def test_context_buffer_exclude_last(self):
        """测试上下文缓冲区 exclude_last 参数"""
        buf = GroupContextBuffer(max_buffer_size=10)
        grp = "grp_exclude_test"
        buf.add_message(grp, "u1", "用户A", "第一条")
        buf.add_message(grp, "u2", "用户B", "第二条")
        buf.add_message(grp, "u3", "用户C", "第三条 (当前条)")

        full_ctx = buf.format_for_prompt(grp, count=5, exclude_last=False)
        self.assertIn("第一条", full_ctx)
        self.assertIn("第二条", full_ctx)
        self.assertIn("第三条", full_ctx)

        excluded_ctx = buf.format_for_prompt(grp, count=5, exclude_last=True)
        self.assertIn("第一条", excluded_ctx)
        self.assertIn("第二条", excluded_ctx)
        self.assertNotIn("第三条", excluded_ctx)

    def test_gui_offscreen_instantiation(self):
        """测试 PySide6 GUI 在无头模式下初始化与控件数据同步"""
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from gui import BotStudioGUI

        temp_dir = tempfile.mkdtemp()
        try:
            cfg_path = os.path.join(temp_dir, "config.yaml")
            AppConfig().save(cfg_path)
            app = QApplication.instance() or QApplication([])
            win = BotStudioGUI(cfg_path)
            self.assertIsNotNone(win)
            self.assertFalse(win.chk_autonomous.isChecked())
            self.assertFalse(hasattr(win, "spin_reply_prob"))
            self.assertFalse(hasattr(win, "spin_mention_prob"))
            self.assertFalse(hasattr(win, "edit_mention_prob"))
            self.assertFalse(hasattr(win, "spin_cooldown"))
            self.assertEqual(win.spin_context_count.value(), 10)
            self.assertTrue(win.chk_memory_enabled.isChecked())
            win.close()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestReviewReportFixes(unittest.TestCase):
    """验证针对 CODE_REVIEW_REPORT.md 中 R1-R10 缺陷的修复与 GUI 矢量代码绘制"""

    def test_r1_group_at_event_without_mentions(self):
        """R1: 验证即使 mentions 为空或正文无 @，只要是 @ 事件即确认为直接 @ 并回复"""
        import asyncio

        async def run_test():
            tmp_mem_dir = tempfile.mkdtemp()
            try:
                cfg = AppConfig()
                cfg.bot.autonomous_enabled = False  # 关闭自主回复
                cfg.qq.appid = "app_12345"

                mock_ai = mock_agent()
                mock_ai.chat = ConsoleAgentMock(return_value="测试回复")

                bot = GeminiQQBot(
                    config=cfg,
                    antigravity_client=mock_ai,
                    memory_manager=GroupMemoryManager(storage_dir=tmp_mem_dir)
                )
                bot._connection = MagicMock()
                bot._connection.state = MagicMock()
                bot._connection.state.robot = MagicMock(id="bot_999", member_openid="bot_openid_999", name="雪村ふぶき")

                # 模拟 @ 事件，但 mentions 为空且正文无 @ 标记
                at_msg = MagicMock()
                at_msg.id = "msg_r1_001"
                at_msg.group_openid = "grp_r1"
                at_msg.author = MagicMock(member_openid="user_normal", bot=False)
                at_msg.content = "你好，请回答一个问题"
                at_msg.mentions = []
                at_msg.reply = AsyncMock()

                await bot._handle_group_message(at_msg, is_at_event=True)
                at_msg.reply.assert_called_once()

                # 测试 @ 事件但消息为空/纯空白时，稳定回复礼貌问候而不是彻底静默
                empty_at_msg = MagicMock()
                empty_at_msg.id = "msg_r1_empty"
                empty_at_msg.group_openid = "grp_r1"
                empty_at_msg.author = MagicMock(member_openid="user_normal", bot=False)
                empty_at_msg.content = "   "
                empty_at_msg.mentions = []
                empty_at_msg.reply = AsyncMock()

                await bot._handle_group_message(empty_at_msg, is_at_event=True)
                empty_at_msg.reply.assert_called_once()
                self.assertIn("你好呀", empty_at_msg.reply.call_args[1]["content"])

                # 反向验证：如果不是 @ 事件且 mentions 为空，在关闭自主回复时不会回复
                non_at_msg = MagicMock()
                non_at_msg.id = "msg_r1_002"
                non_at_msg.group_openid = "grp_r1"
                non_at_msg.author = MagicMock(member_openid="user_normal", bot=False)
                non_at_msg.content = "普通聊天消息"
                non_at_msg.mentions = []
                non_at_msg.reply = AsyncMock()

                await bot._handle_group_message(non_at_msg, is_at_event=False)
                non_at_msg.reply.assert_not_called()
            finally:
                shutil.rmtree(tmp_mem_dir, ignore_errors=True)

        asyncio.run(run_test())

    def test_r2_agentapi_subprocess_timeout_and_reap(self):
        """R2: 验证 AgentAPI 子进程执行受超时限制，并在超时后强制杀死并回收进程"""
        import asyncio

        async def run_test():
            tmp_s = tempfile.mktemp(suffix=".json")
            try:
                client = AntigravityClient(sessions_file=tmp_s)
                client._executable = "dummy_agentapi.exe"

                mock_proc = MagicMock()
                mock_proc.kill = MagicMock()
                mock_proc.wait = AsyncMock(return_value=0)

                async def mock_communicate():
                    await asyncio.sleep(2.0)
                    return b"{}", b""

                mock_proc.communicate = mock_communicate

                with unittest.mock.patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                    with self.assertRaises(TimeoutError):
                        await client._run_agentapi_cmd(["dummy-command"], timeout=0.1)

                mock_proc.kill.assert_called_once()
                mock_proc.wait.assert_awaited_once()
            finally:
                if os.path.exists(tmp_s):
                    os.remove(tmp_s)

        asyncio.run(run_test())

    def test_r3_chat_timeout_isolates_session(self):
        """R3: cancel the late generation while preserving the conversation ID."""
        import asyncio

        async def run_test():
            tmp_s = tempfile.mktemp(suffix=".json")
            try:
                client = AntigravityClient(sessions_file=tmp_s)
                client._is_ready = True
                client._executable = "dummy.exe"
                client._cancel_native_conversation = AsyncMock()

                session = client.get_or_create_session("sess_r3")
                session.conversation_id = "old_cid_123"

                # 模拟超时
                with unittest.mock.patch.object(client, "_run_agentapi_cmd", side_effect=TimeoutError("超时")):
                    from antigravity_client import AgentServiceTimeoutError
                    with self.assertRaises(AgentServiceTimeoutError):
                        await client.chat("sess_r3", "第一问")
                self.assertEqual(session.conversation_id, "old_cid_123")
                client._cancel_native_conversation.assert_awaited_once_with("old_cid_123")
            finally:
                if os.path.exists(tmp_s):
                    os.remove(tmp_s)

        asyncio.run(run_test())

    def test_r4_model_switch_invalidates_backend_conversation(self):
        """R4: 验证切换模型时重置 conversation_id，确保后端创建使用新模型的对话，支持中划线别名标准化"""
        tmp_s = tempfile.mktemp(suffix=".json")
        try:
            client = AntigravityClient(sessions_file=tmp_s)
            session = client.get_or_create_session("sess_r4")
            session.model = "flash"
            session.conversation_id = "old_flash_cid"

            success = client.set_session_model("sess_r4", "pro")
            self.assertTrue(success)
            self.assertEqual(session.model, "pro")
            # 必须重置对话上下文ID
            self.assertIsNone(session.conversation_id)

            # 测试别名归一化与再次切换
            session.conversation_id = "pro_cid"
            success2 = client.set_session_model("sess_r4", "flash-lite")
            self.assertTrue(success2)
            self.assertEqual(session.model, "flash_lite")
            self.assertIsNone(session.conversation_id)
        finally:
            if os.path.exists(tmp_s):
                os.remove(tmp_s)

    def test_r5_clearmemory_resets_ai_session(self):
        """R5: 验证清空群记忆时同步重置 AI 模型的会话上下文"""
        import asyncio

        async def run_test():
            tmp_mem_dir = tempfile.mkdtemp()
            try:
                cfg = AppConfig()
                cfg.bot.master_id = "owner"
                mock_ai = mock_agent()
                mock_ai.chat = ConsoleAgentMock(return_value="内部命令审核")
                mock_ai.reset_session = MagicMock()

                bot = GeminiQQBot(
                    config=cfg,
                    antigravity_client=mock_ai,
                    memory_manager=GroupMemoryManager(storage_dir=tmp_mem_dir)
                )

                reply_mock = AsyncMock()
                await bot._handle_message_core(
                    session_key="group_12345",
                    raw_content="/clearmemory",
                    reply_func=reply_mock, user_openid="owner"
                )

                await drain_runtime(bot)
                mock_ai.reset_session.assert_called_with(bot._chat_session_key("group_12345"))
                reply_mock.assert_called_once()
                await bot._close_runtime()
            finally:
                shutil.rmtree(tmp_mem_dir, ignore_errors=True)

        asyncio.run(run_test())

    def test_r6_action_filter_preserves_code_indentation_and_spaces(self):
        """R6: 验证动作神态过滤不破坏多层代码缩进与字符串内部的空格"""
        # 1. 嵌套 Python 代码与字符串多空格
        raw_code = 'if ready:\n    if yes:\n        print("hello  world")'
        filtered_code = strip_action_descriptions(raw_code)
        self.assertEqual(filtered_code, raw_code)

        # 2. Markdown 代码块保护
        md_code = '```python\ndef test():\n    # (害羞脸红)\n    return "  two  spaces  "\n```'
        filtered_md = strip_action_descriptions(md_code)
        self.assertEqual(filtered_md, md_code)

        # 3. 混合动作描写与代码
        mixed = '（微笑着）请看下面代码：\n```python\nif a:\n    pass\n```\n（别过头去）听懂了吗？'
        filtered_mixed = strip_action_descriptions(mixed)
        self.assertNotIn("微笑着", filtered_mixed)
        self.assertNotIn("别过头去", filtered_mixed)
        self.assertIn("```python\nif a:\n    pass\n```", filtered_mixed)

    def test_r7_gui_log_autoscroll_text_cursor(self):
        """R7: 验证 GUI 实时日志自动滚屏使用正确的 MoveOperation.End 枚举，不抛 AttributeError"""
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from gui import BotStudioGUI

        app = QApplication.instance() or QApplication([])
        win = BotStudioGUI("config.yaml")

        mock_proc = MagicMock()
        mock_proc.readAllStandardOutput.return_value.data.return_value = b"[Test] Log Output Line 1\n"
        win.bot_process = mock_proc
        win.chk_autoscroll.setChecked(True)

        # 执行槽函数，验证不会发生 AttributeError
        win._on_process_output()
        self.assertIn("[Test] Log Output Line 1", win.log_terminal.toPlainText())
        win.bot_process = None
        win.close()

    def test_r8_split_message_prefix_boundary(self):
        """R8: 验证前缀长于或等于安全上限时，自动省略前缀且所有返回分段长度严格 <= max_length"""
        # 复现报告中的用例：max_length=20，前缀长达 36+ 字符
        long_prefix = "<@!" + "a" * 32 + ">"
        chunks = split_message("hello", 20, first_chunk_prefix=long_prefix)
        self.assertTrue(len(chunks) >= 1)
        for c in chunks:
            self.assertLessEqual(len(c), 20)

        # 边界验证：前缀正好等于上限
        chunks_eq = split_message("hello world test text", 20, first_chunk_prefix="a" * 20)
        for c in chunks_eq:
            self.assertLessEqual(len(c), 20)

        # 边界验证：前缀为上限 - 1
        chunks_sub1 = split_message("hello world test text", 20, first_chunk_prefix="a" * 19)
        for c in chunks_sub1:
            self.assertLessEqual(len(c), 20)

    def test_r9_persona_hot_reload_on_disk_change(self):
        """R9: 验证外部修改 personas.yaml 后，机器人自动热重载人设、重置全部会话，并在群消息调度中自动生效"""
        import asyncio

        async def run_test():
            tmp_dir = tempfile.mkdtemp()
            tmp_yaml = os.path.join(tmp_dir, "personas.yaml")
            try:
                mgr = PersonaManager(storage_path=tmp_yaml)
                mgr.set_active("全栈系统架构师")

                cfg = AppConfig()
                mock_ai = mock_agent()
                mock_ai.reset_all_sessions = MagicMock()

                bot = GeminiQQBot(
                    config=cfg,
                    antigravity_client=mock_ai,
                    persona_manager=mgr,
                    memory_manager=GroupMemoryManager(storage_dir=os.path.join(tmp_dir, "memories"))
                )
                self.assertIn("系统架构师", bot.ai.system_prompt)

                time.sleep(0.05)
                import yaml
                with open(tmp_yaml, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                data["active"] = "治愈系温柔学姐"
                with open(tmp_yaml, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f)

                # 人设热重载直接更新正文，不重置会话或请求模型自检。
                bot.sync_system_prompt()
                self.assertEqual(bot.personas.active_persona_name, "治愈系温柔学姐")
                self.assertIn("温柔体贴", bot.ai.system_prompt)
                mock_ai.reset_all_sessions.assert_not_called()

                # 验证群消息触发入口同样会自动执行人设热重载
                time.sleep(0.05)
                with open(tmp_yaml, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                data["active"] = "毒舌犀利吐槽姬"
                with open(tmp_yaml, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f)

                grp_msg = MagicMock()
                grp_msg.id = "msg_r9_group"
                grp_msg.group_openid = "grp_test"
                grp_msg.author = MagicMock(member_openid="u1", bot=False)
                grp_msg.content = "普通群消息"
                grp_msg.mentions = []
                grp_msg.reply = AsyncMock()

                await bot._handle_group_message(grp_msg, is_at_event=False)
                self.assertEqual(bot.personas.active_persona_name, "毒舌犀利吐槽姬")
                self.assertIn("吐槽", bot.ai.system_prompt)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        asyncio.run(run_test())

    def test_r10_probe_quota_validation(self):
        """R10: 验证自检探针在模型异常、配额超限或超时时准确判定失败，且不污染全局配置"""
        import asyncio

        async def run_test():
            tmp_s = tempfile.mktemp(suffix=".json")
            try:
                client = AntigravityClient(sessions_file=tmp_s)
                client._is_ready = True
                initial_timeout = client.timeout_seconds

                # 1. 模型返回超时文本时，probe 应判定失败
                with unittest.mock.patch.object(client, "chat", return_value="⏱️ Gemini 思考超时，当前网络或请求较慢，请稍后重试。"):
                    ok, msg = await client.probe(timeout=1.0)
                    self.assertFalse(ok)
                    self.assertIn("异常内容", msg)
                    # 验证 timeout_seconds 未被 probe 破坏篡改
                    self.assertEqual(client.timeout_seconds, initial_timeout)

                # 1.1 模型返回配额超限错误文本时，probe 应精准判定失败
                with unittest.mock.patch.object(client, "chat", return_value="Error: 429 RESOURCE_EXHAUSTED Quota exceeded"):
                    ok, msg = await client.probe(timeout=1.0)
                    self.assertFalse(ok)
                    self.assertIn("异常内容", msg)

                # 2. 模型抛出异常时，probe 应判定失败
                with unittest.mock.patch.object(client, "chat", side_effect=RuntimeError("Connection refused")):
                    ok, msg = await client.probe(timeout=1.0)
                    self.assertFalse(ok)
                    self.assertIn("Connection refused", msg)

                # 3. 模型返回正常响应时，probe 判定成功
                with unittest.mock.patch.object(client, "chat", return_value="OK"):
                    ok, msg = await client.probe(timeout=1.0)
                    self.assertTrue(ok)
                    self.assertEqual(msg, "OK")

                # 4. main.py 中的 diagnose_antigravity 对 probe 结果的联动验证
                with unittest.mock.patch.object(client, "probe", AsyncMock(return_value=(False, "超时错误"))):
                    diag_result = await diagnose_antigravity(client)
                    self.assertFalse(diag_result)

                with unittest.mock.patch.object(client, "probe", AsyncMock(return_value=(True, "OK"))):
                    diag_result = await diagnose_antigravity(client)
                    self.assertTrue(diag_result)
            finally:
                if os.path.exists(tmp_s):
                    os.remove(tmp_s)

        asyncio.run(run_test())

    def test_gui_no_emojis_and_vector_icons(self):
        """验证 gui.py 彻底去除所有 Emoji 图标并全部由 QPainter 代码矢量绘制，支持回退机制"""
        # 1. 源码扫描验证零 Emoji
        import re
        with open("gui.py", "r", encoding="utf-8") as f:
            gui_code = f.read()

        emoji_matches = re.findall(r"[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]", gui_code)
        self.assertEqual(len(emoji_matches), 0, f"gui.py 中依然存在未清理的 Emoji: {set(emoji_matches)}")

        # 2. 验证所有 17 种代码绘制矢量图标及未知图标回退机制的正确生成
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from gui import create_vector_icon

        app = QApplication.instance() or QApplication([])
        icon_names = [
            "bot", "persona", "settings", "console", "play", "stop",
            "restart", "search", "plus", "star", "star_filled", "trash",
            "save", "folder", "chart", "clear", "dot"
        ]
        for name in icon_names:
            icon = create_vector_icon(name, size=20)
            self.assertFalse(icon.isNull(), f"图标 {name} 生成失败或为空")
            pix = icon.pixmap(20, 20)
            self.assertEqual(pix.width(), 20)
            self.assertEqual(pix.height(), 20)

        # 3. 验证未知图标回退
        fallback_icon = create_vector_icon("unknown_icon_type", size=20)
        self.assertFalse(fallback_icon.isNull())
        fallback_pix = fallback_icon.pixmap(20, 20)
        self.assertEqual(fallback_pix.width(), 20)

    def test_transcript_model_error_status_raises_immediately(self):
        """验证转录日志出现 status=ERROR 时立刻抛出 RuntimeError 快速失败，无需等待数十秒超时"""
        import asyncio

        async def run_test():
            tmp_dir = tempfile.mkdtemp()
            try:
                client = AntigravityClient(sessions_file=os.path.join(tmp_dir, "sessions.json"))
                client._brain_dir = tmp_dir
                cid = "test_err_cid"
                log_dir = os.path.join(tmp_dir, cid, ".system_generated", "logs")
                os.makedirs(log_dir, exist_ok=True)
                log_file = os.path.join(log_dir, "transcript.jsonl")

                with open(log_file, "w", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "source": "MODEL",
                        "status": "ERROR",
                        "error": "RESOURCE_EXHAUSTED: Daily quota reached"
                    }) + "\n")

                start = time.time()
                with self.assertRaises(RuntimeError) as ctx:
                    await client._wait_for_model_response(cid, expected_step_min=0, timeout=5.0)
                elapsed = time.time() - start

                # 必须迅速失败，不得等待满 5 秒超时
                self.assertLess(elapsed, 1.5)
                self.assertIn("RESOURCE_EXHAUSTED", str(ctx.exception))
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        asyncio.run(run_test())

    def test_subprocess_stderr_json_error_parsing(self):
        """验证 AgentAPI 子进程报错在 stderr 中包含 JSON error 字段时能被精准提取"""
        import asyncio

        async def run_test():
            tmp_s = tempfile.mktemp(suffix=".json")
            try:
                client = AntigravityClient(sessions_file=tmp_s)
                client._executable = "dummy_agentapi.exe"

                mock_proc = MagicMock()
                mock_proc.returncode = 1
                mock_proc.communicate = AsyncMock(
                    return_value=(b"", b'{"error": "Quota limit reached for project"}')
                )

                with unittest.mock.patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                    with self.assertRaises(RuntimeError) as ctx:
                        await client._run_agentapi_cmd(["test"])
                    self.assertIn("Quota limit reached for project", str(ctx.exception))
            finally:
                if os.path.exists(tmp_s):
                    os.remove(tmp_s)

        asyncio.run(run_test())

    def test_reset_all_sessions(self):
        """验证 reset_all_sessions 能够将全部活跃会话的 conversation_id 置空"""
        tmp_s = tempfile.mktemp(suffix=".json")
        try:
            client = AntigravityClient(sessions_file=tmp_s)
            s1 = client.get_or_create_session("sess_1")
            s2 = client.get_or_create_session("sess_2")
            s1.conversation_id = "cid_1"
            s2.conversation_id = "cid_2"

            client.reset_all_sessions()
            self.assertIsNone(s1.conversation_id)
            self.assertIsNone(s2.conversation_id)
        finally:
            if os.path.exists(tmp_s):
                os.remove(tmp_s)


class TestPersistentSessionMapping(unittest.TestCase):
    """验证会话持久化、会话与群聊一对一绑定、重启恢复与探针复用机制"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.sessions_file = os.path.join(self.temp_dir, "sessions.json")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_session_persistence_lifecycle(self):
        """测试会话创建、持久化到磁盘与重启后恢复会话ID"""
        client1 = AntigravityClient(sessions_file=self.sessions_file)
        sess1 = client1.get_or_create_session("group_1001")
        sess1.conversation_id = "conv_persist_123"
        client1._save_sessions()

        self.assertTrue(os.path.exists(self.sessions_file))
        with open(self.sessions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("group_1001", data)
        self.assertEqual(data["group_1001"]["conversation_id"], "conv_persist_123")

        # 模拟机器人重启：新建客户端实例，指向相同的持久化文件
        client2 = AntigravityClient(sessions_file=self.sessions_file)
        sess2 = client2.get_or_create_session("group_1001")
        self.assertEqual(sess2.conversation_id, "conv_persist_123")

    def test_session_invalidation_on_model_switch_and_clear(self):
        """测试模型切换或清空会话时同步清除 conversation_id 并持久化到磁盘"""
        client = AntigravityClient(sessions_file=self.sessions_file)
        sess = client.get_or_create_session("group_1002")
        sess.conversation_id = "conv_old_model"
        sess.model = "flash"
        client._save_sessions()

        # 1. 切换模型
        client.set_session_model("group_1002", "pro")
        self.assertIsNone(sess.conversation_id)
        with open(self.sessions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIsNone(data["group_1002"]["conversation_id"])
        self.assertEqual(data["group_1002"]["model"], "pro")

        # 2. 清空重置会话
        sess.conversation_id = "conv_to_clear"
        client._save_sessions()
        client.reset_session("group_1002")
        self.assertIsNone(sess.conversation_id)
        with open(self.sessions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIsNone(data["group_1002"]["conversation_id"])

    def test_session_timeout_expiration(self):
        """测试辅助会话超时自动失效重置，群聊不因空闲丢失上下文"""
        with open(self.sessions_file, "w", encoding="utf-8") as f:
            json.dump({
                "temporary_expired": {
                    "conversation_id": "conv_expired_001",
                    "model": "flash",
                    "thinking_level": "high",
                    "updated_at": time.time() - 3600  # 1小时前
                }
            }, f)

        # 设定超时 10 分钟 (600 秒)
        client = AntigravityClient(session_timeout_minutes=10, sessions_file=self.sessions_file)
        sess = client.get_or_create_session("temporary_expired")
        self.assertIsNone(sess.conversation_id)

    def test_probe_removes_its_backend_session_after_each_check(self):
        """自检结束归档并清理探针，下次自检不复用待删除的对话。"""
        import asyncio

        async def run_test():
            client = AntigravityClient(sessions_file=self.sessions_file)
            client._is_ready = True

            async def chat(key, *args, **kwargs):
                session = client.get_or_create_session(key)
                self.assertIsNone(session.conversation_id)
                session.conversation_id = "probe_conv_" + str(mock_chat.await_count)
                client._save_sessions()
                return "OK"

            async def cleanup(key, archive, **kwargs):
                self.assertTrue(Path(archive).is_file())
                client._invalidate(client._sessions[key])
                client._save_sessions()
                return True

            with unittest.mock.patch.object(client, "chat", AsyncMock(side_effect=chat)) as mock_chat, \
                    unittest.mock.patch.object(client, "_archive_session_once", AsyncMock(side_effect=cleanup)) as remove:
                ok, msg = await client.probe(timeout=5.0)
                self.assertTrue(ok)
                self.assertEqual(msg, "OK")
                self.assertIsNone(client.get_or_create_session("__system_probe__").conversation_id)
                ok2, msg2 = await client.probe(timeout=5.0)
                self.assertTrue(ok2)
                self.assertEqual(msg2, "OK")
                self.assertEqual(remove.await_count, 2)
                self.assertFalse(client._archive_queue())

        asyncio.run(run_test())

    def test_send_message_error_fallback_to_new_conversation(self):
        """测试已有会话在后端失效时，自动捕获异常并无缝回退启动新会话"""
        import asyncio

        async def run_test():
            client = AntigravityClient(sessions_file=self.sessions_file)
            client._is_ready = True
            client._executable = "dummy.exe"

            sess = client.get_or_create_session("group_broken")
            sess.conversation_id = "conv_broken_backend"

            call_count = 0

            async def mock_run_cmd(args, timeout=None):
                nonlocal call_count
                call_count += 1
                if args[0] == "send-message":
                    raise RuntimeError("Conversation not found on server")
                elif args[0] == "new-conversation":
                    return {"response": {"newConversation": {"conversationId": "recovered_conv_888"}}}
                return {}

            with unittest.mock.patch.object(client, "_run_agentapi_cmd", side_effect=mock_run_cmd):
                with unittest.mock.patch.object(client, "_wait_for_model_response", AsyncMock(return_value="这是恢复后的回复")):
                    reply = await client.chat("group_broken", "你好")

            self.assertEqual(reply, "这是恢复后的回复")
            self.assertEqual(sess.conversation_id, "recovered_conv_888")

        asyncio.run(run_test())

    def test_conversation_title_generation(self):
        """测试根据不同会话类型生成友好的 Antigravity 侧边栏对话标题"""
        client = AntigravityClient(sessions_file=self.sessions_file)
        self.assertEqual(client._get_conversation_title("__system_probe__"), "系统配额自检探针")
        self.assertEqual(client._get_conversation_title("onebot_group_123456"), "OneBot群聊_123456")
        self.assertEqual(client._get_conversation_title("onebot_private_987654"), "OneBot私聊_987654")
        self.assertEqual(client._get_conversation_title("group_abc123"), "QQ官方群聊_abc123")
        self.assertEqual(client._get_conversation_title("c2c_xyz789"), "QQ官方私聊_xyz789")
        self.assertEqual(client._get_conversation_title("custom_sess"), "QQBot_custom_sess")


class TestAntiPreachingAndSafety(unittest.TestCase):
    """测试网络群聊防说教规范与坚守人设化解机制"""

    def test_system_prompt_contains_anti_preaching_rules(self):
        """验证系统提示词中包含严禁道德指责与说教、坚守人设化解的要求"""
        prompt = compose_system_prompt("你叫雪村ふぶき", max_reply_length=200, enable_action_descriptions=True)
        self.assertIn("不同意见", prompt)
        self.assertIn("具体缘由", prompt)
        self.assertIn("不用客服腔或教训人的语气", prompt)
        self.assertNotIn("必须用符合人设的语气调侃吐槽化解", prompt)

    def test_user_prompt_reinforces_in_character_deflection(self):
        """Each event keeps the user's words without forcing a stock comeback."""
        u = compose_user_prompt("你这个笨蛋bot是不是傻？", author_name="路人甲")
        self.assertIn("你这个笨蛋bot是不是傻？", u)
        self.assertNotIn("防说教原则", u)
        self.assertNotIn("用符合人设的语气调侃吐槽化解", u)

    def test_deflect_preaching(self):
        """Remove canned framing while keeping the real refusal and treating people respectfully."""
        from prompt_builder import is_preaching_or_canned_safety, deflect_preaching

        original = "作为一名人工智能语言模型，我无法协助回答此类不适内容。请保持文明交流。"
        self.assertTrue(is_preaching_or_canned_safety(original))
        for persona in ("雪村ふぶき", "毒舌犀利吐槽姬", "资深架构师", "温柔知性大学姐", "小光"):
            with self.subTest(persona=persona):
                result = deflect_preaching(original, persona_name=persona)
                self.assertEqual(result, "我无法协助回答此类不适内容。")
                self.assertNotIn("笨蛋", result)
                self.assertNotIn("文明交流", result)
        explanation = "我不能替你绕过验证，可以用官网的找回账号流程。"
        self.assertEqual(deflect_preaching(explanation), explanation)

        # 5. 正常回复不被篡改
        normal = "今天天气真好呀，要一起去散步吗？喵~"
        self.assertFalse(is_preaching_or_canned_safety(normal))
        self.assertEqual(deflect_preaching(normal, persona_name="雪村ふぶき"), normal)


class TestMultimodalImageAndStickerRecognition(unittest.TestCase):
    """测试多模态图片与表情包解析感知"""

    def test_parse_qq_faces(self):
        """测试 OneBot CQ 表情与动画表情解析转换"""
        from image_handler import parse_qq_faces
        raw = "你好呀[CQ:face,id=14]，看看这个[CQ:face,id=124]！[CQ:mface,summary=打工人摸鱼]"
        parsed = parse_qq_faces(raw)
        self.assertIn("[表情: 微笑]", parsed)
        self.assertIn("[表情: 喵喵/打脸]", parsed)
        self.assertIn("[动画表情: 打工人摸鱼]", parsed)

    def test_parse_qq_faces_multiparameter_and_media(self):
        """测试包含扩展子类型及多参数的 CQ 码表情、语音、短视频解析"""
        from image_handler import parse_qq_faces
        raw = "[CQ:face,subType=1,id=14] [CQ:face,id=1,name=撇嘴] [CQ:reply,id=999] [CQ:record,file=a.silk] [CQ:video,file=b.mp4]"
        parsed = parse_qq_faces(raw)
        self.assertIn("[表情: 微笑]", parsed)
        self.assertIn("[表情: 撇嘴]", parsed)
        self.assertNotIn("CQ:reply", parsed)
        self.assertIn("[语音消息]", parsed)
        self.assertIn("[短视频]", parsed)

    def test_extract_cq_images_and_segments(self):
        """测试 OneBot CQ 码图片与 JSON segments 结构解析"""
        from image_handler import extract_cq_images
        raw = "看这张图[CQ:image,url=https://example.com/cat.jpg,file=cat.jpg,summary=可爱猫咪]"
        clean, items = extract_cq_images(raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://example.com/cat.jpg")
        self.assertEqual(items[0]["summary"], "可爱猫咪")

        # 测试 JSON segments 解析
        segments = [{"type": "image", "data": {"url": "https://example.com/seg.png", "file": "seg.png"}}]
        clean2, items2 = extract_cq_images("", segments=segments)
        self.assertEqual(len(items2), 1)
        self.assertEqual(items2[0]["url"], "https://example.com/seg.png")

    def test_extract_botpy_attachments(self):
        """测试 qq-botpy 消息附件提取与协议相对 URL 补充"""
        from image_handler import extract_botpy_attachments
        msg = MagicMock()
        att1 = MagicMock(url="//gchat.qpic.cn/img1.jpg", filename="img1.jpg", content_type="image/jpeg")
        att2 = MagicMock(url="https://gchat.qpic.cn/img2.png", filename="img2.png", content_type="image/png")
        msg.attachments = [att1, att2]

        items = extract_botpy_attachments(msg)
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0]["url"].startswith("https://"))
        self.assertEqual(items[1]["url"], "https://gchat.qpic.cn/img2.png")

    def test_analyze_image_item_local_pillow(self):
        """测试利用 Pillow 分析本地图片尺寸、格式与表情包判定"""
        import asyncio
        from PIL import Image
        from image_handler import analyze_image_item

        async def run_test():
            tmp_dir = tempfile.mkdtemp()
            try:
                # 1. 创建小尺寸表情包 (128x128)
                sticker_path = os.path.join(tmp_dir, "sticker.png")
                img = Image.new("RGB", (128, 128), color=(255, 0, 0))
                img.save(sticker_path, "PNG")

                res_sticker = await analyze_image_item({"file": sticker_path, "subType": "1"}, cache_dir=tmp_dir)
                self.assertEqual(res_sticker["width"], 128)
                self.assertEqual(res_sticker["height"], 128)
                self.assertEqual(res_sticker["format"], "PNG")
                self.assertTrue(res_sticker["is_sticker"])
                self.assertIn("表情包", res_sticker["summary"])

                # 2. 创建大尺寸群聊配图 (800x600)
                photo_path = os.path.join(tmp_dir, "photo.jpg")
                img_photo = Image.new("RGB", (800, 600), color=(0, 255, 0))
                img_photo.save(photo_path, "JPEG")

                res_photo = await analyze_image_item({"file": photo_path}, cache_dir=tmp_dir)
                self.assertEqual(res_photo["width"], 800)
                self.assertEqual(res_photo["height"], 600)
                self.assertEqual(res_photo["format"], "JPEG")
                self.assertFalse(res_photo["is_sticker"])
                self.assertIn("群聊配图", res_photo["summary"])
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        asyncio.run(run_test())

    def test_process_multimodal_message_pure_image_not_empty(self):
        """测试用户发送纯图片时，内容自动转为感知文本，绝不作为空消息被忽略"""
        import asyncio
        from PIL import Image
        from image_handler import process_multimodal_message

        async def run_test():
            tmp_dir = tempfile.mkdtemp()
            try:
                test_img = os.path.join(tmp_dir, "test.png")
                Image.new("RGB", (200, 200), (0, 0, 255)).save(test_img, "PNG")

                enriched, analyses = await process_multimodal_message(
                    content="",
                    attachments=[MagicMock(url="", filename=test_img, content_type="image/png")],
                    cache_dir=tmp_dir
                )
                self.assertTrue(len(enriched) > 0)
                self.assertIn("表情包", enriched)
                self.assertEqual(len(analyses), 1)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        asyncio.run(run_test())

    def test_compose_user_prompt_multimodal_perception_hint(self):
        """Image event content stays intact; lasting perception rules are not repeated."""
        text = "[用户发送了图片: data/cache/images/cat.jpg (640x480)]"
        u = compose_user_prompt(text, author_name="爱猫群友")
        self.assertIn(text, u)
        self.assertNotIn("【多模态感知", u)


class TestOneBotAndContextEnhancements(unittest.TestCase):
    """测试 OneBot 客户端集成与上下文兼容接口"""

    def test_group_context_buffer_aliases(self):
        """测试 GroupContextBuffer 兼容性别名方法 (append_message, format_context, clear_group)"""
        buf = GroupContextBuffer(max_buffer_size=10)
        buf.append_message("grp_onebot", "user_1", "李白", "床前明月光")
        buf.append_message("grp_onebot", "user_2", "杜甫", "疑是地上霜")

        ctx = buf.format_context("grp_onebot", limit=5)
        self.assertIn("李白: 床前明月光", ctx)
        self.assertIn("杜甫: 疑是地上霜", ctx)

        buf.clear_group("grp_onebot")
        self.assertEqual(len(buf.get_recent_messages("grp_onebot")), 0)

    def test_group_memory_aliases_and_nickname_arg(self):
        """测试 GroupMemoryManager 别名与 nickname 参数兼容性"""
        tmp_dir = tempfile.mkdtemp()
        try:
            mgr = GroupMemoryManager(storage_dir=tmp_dir)
            mgr.record_interaction(
                group_openid="grp_ob_mem",
                user_openid="u_ob_1",
                user_msg="你好",
                bot_reply="你好喵",
                nickname="小明"
            )

            mem = mgr.get_memory("grp_ob_mem")
            self.assertEqual(mem.participants["u_ob_1"].nickname, "小明")

            summary = mgr.get_summary_text("grp_ob_mem", current_user_openid="u_ob_1")
            self.assertIn("小明", summary)

            self.assertTrue(mgr.clear_memory("grp_ob_mem"))
            new_mem = mgr.get_memory("grp_ob_mem")
            self.assertEqual(len(new_mem.recent_interactions), 0)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_onebot_client_multimodal_dispatch(self):
        """测试 OneBotClient 接收包含表情包与图片的消息，正确记录说话人、上下文并生成回复"""
        import asyncio
        from onebot_client import OneBotClient

        async def run_test():
            tmp_mem = tempfile.mkdtemp()
            try:
                cfg = AppConfig()
                cfg.bot.autonomous_enabled = False
                cfg.bot.master_id = "master_ob_123"
                cfg.bot.enable_action_descriptions = False

                mock_ai = mock_agent()
                mock_ai.chat = ConsoleAgentMock(return_value="（两颊泛红）这只猫咪真可爱喵！")

                ob = OneBotClient(
                    config=cfg,
                    antigravity_client=mock_ai,
                    memory_manager=GroupMemoryManager(storage_dir=tmp_mem)
                )

                sent_messages = []
                async def mock_send_group(grp_id, msg):
                    sent_messages.append((grp_id, msg))

                ob.send_group_message = mock_send_group
                ob.self_id = "bot_self_qq"

                # 模拟来自主人的带表情包消息
                data = {
                    "group_id": "12345678",
                    "user_id": "master_ob_123",
                    "message_id": "msg_ob_001",
                    "raw_message": "[CQ:face,id=14] 你看这张猫咪图片真逗！",
                    "sender": {"card": "我的主人", "nickname": "主人昵称"}
                }

                await ob._handle_group_message(data)

                # 验证已触发回复
                self.assertEqual(len(sent_messages), 1)
                grp_id, sent_text = sent_messages[0]
                self.assertEqual(grp_id, "12345678")
                # 动作描写已被清洗
                self.assertNotIn("两颊泛红", sent_text)
                self.assertIn("这只猫咪真可爱喵！", sent_text)

                # 验证上下文缓冲区正确记录说话人与表情感知
                ctx = ob.context_buffer.get_recent_messages("12345678", 5)
                self.assertTrue(len(ctx) >= 2)
                self.assertEqual(ctx[0]["author_name"], "我的主人")
                self.assertIn("[表情: 微笑]", ctx[0]["content"])
                self.assertEqual(ctx[1]["author_openid"], "bot")
            finally:
                shutil.rmtree(tmp_mem, ignore_errors=True)

        asyncio.run(run_test())

    def test_onebot_model_command(self):
        """测试 OneBotClient 的 /model 指令查看与切换模型功能"""
        import asyncio
        from onebot_client import OneBotClient

        async def run_test():
            tmp_mem = tempfile.mkdtemp()
            tmp_s = tempfile.mktemp(suffix=".json")
            try:
                cfg = AppConfig()
                mock_ai = AntigravityClient(sessions_file=tmp_s)
                mock_ai.chat = ConsoleAgentMock(return_value="内部命令审核")
                cfg.bot.runtime_database_path = os.path.join(tmp_mem, "runtime.sqlite3")
                cfg.media.database_path = os.path.join(tmp_mem, "media.sqlite3")
                ob = OneBotClient(
                    config=cfg,
                    antigravity_client=mock_ai,
                    memory_manager=GroupMemoryManager(storage_dir=tmp_mem)
                )

                replies = []
                async def mock_send_group(grp_id, msg):
                    replies.append(getattr(msg, "body", msg))
                ob.send_group_message = mock_send_group
                ob._is_ws_connected = lambda: True

                # 1. 查询当前模型
                await ob._handle_command("model", group_id="1001", user_id="u1", is_group=True)
                await drain_runtime(ob)
                self.assertIn("当前模型", replies[-1])
                self.assertIn("flash", replies[-1])

                # 2. 切换为 pro
                await ob._handle_command("model pro", group_id="1001", user_id="u1", is_group=True)
                await drain_runtime(ob)
                self.assertIn("已切换模型：pro", replies[-1])
                sess = mock_ai.get_or_create_session(ob._chat_session_key("onebot_group_1001"))
                self.assertEqual(sess.model, "pro")

                # 3. 切换别名 flash-lite
                await ob._handle_command("model flash-lite", group_id="1001", user_id="u1", is_group=True)
                await drain_runtime(ob)
                self.assertIn("flash_lite", replies[-1])
                self.assertEqual(sess.model, "flash_lite")

                # 4. 无效模型名称
                await ob._handle_command("model invalid_xyz", group_id="1001", user_id="u1", is_group=True)
                await drain_runtime(ob)
                self.assertIn("模型仅支持", replies[-1])
                await ob._close_runtime()
            finally:
                shutil.rmtree(tmp_mem, ignore_errors=True)
                if os.path.exists(tmp_s):
                    os.remove(tmp_s)

        asyncio.run(run_test())

    def test_botpy_on_ready(self):
        """测试 GeminiQQBot 的 on_ready 事件生命周期回调无报错"""
        import asyncio
        from bot_client import GeminiQQBot

        async def run_test():
            tmp_s = tempfile.mktemp(suffix=".json")
            try:
                cfg = AppConfig()
                mock_ai = AntigravityClient(sessions_file=tmp_s)
                bot = GeminiQQBot(config=cfg, antigravity_client=mock_ai)
                # mock robot 属性
                mock_conn = MagicMock()
                mock_conn.state.robot = MagicMock(name="FubukiBot", id="1024")
                bot._connection = mock_conn

                # 调用 on_ready 不应抛出异常
                await bot.on_ready()
            finally:
                if os.path.exists(tmp_s):
                    os.remove(tmp_s)

        asyncio.run(run_test())


class TestOneBotMultiAccountAndNapCat(unittest.TestCase):
    """测试 OneBot v11 多账号配置切换与 NapCatQQ 配置生成"""

    def test_multi_account_config_defaults_and_switch(self):
        cfg = AppConfig()
        self.assertIn("account_1", cfg.onebot.accounts)
        self.assertIn("account_2", cfg.onebot.accounts)
        self.assertEqual(cfg.onebot.active_account, "account_1")
        self.assertEqual(cfg.onebot.ws_url, "ws://127.0.0.1:3001")

        # 切换到 account_2
        ok = cfg.onebot.switch_account("account_2")
        self.assertTrue(ok)
        self.assertEqual(cfg.onebot.active_account, "account_2")
        self.assertEqual(cfg.onebot.ws_url, "ws://127.0.0.1:3002")

        # 切换不存在的账号返回 False
        self.assertFalse(cfg.onebot.switch_account("invalid_account"))

    def test_multi_account_yaml_persistence(self):
        tmp_dir = tempfile.mkdtemp()
        try:
            cfg_path = os.path.join(tmp_dir, "test_config.yaml")
            cfg = AppConfig()
            cfg.onebot.accounts["account_1"].account_id = "111111"
            cfg.onebot.accounts["account_2"].account_id = "222222"
            cfg.onebot.switch_account("account_2")
            cfg.save(cfg_path)

            loaded = AppConfig.load(cfg_path)
            self.assertEqual(loaded.onebot.active_account, "account_2")
            self.assertEqual(loaded.onebot.accounts["account_1"].account_id, "111111")
            self.assertEqual(loaded.onebot.accounts["account_2"].account_id, "222222")
            self.assertEqual(loaded.onebot.ws_url, "ws://127.0.0.1:3002")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_napcat_setup_multi_account_configs(self):
        from napcat_manager import NapCatManager
        tmp_dir = tempfile.mkdtemp()
        try:
            mgr = NapCatManager(base_dir=tmp_dir)
            accounts = {
                "account_1": {"account_id": "123456", "ws_url": "ws://127.0.0.1:3001", "access_token": "token1"},
                "account_2": {"account_id": "654321", "ws_url": "ws://127.0.0.1:3002", "access_token": "token2"}
            }
            ok = mgr.setup_multi_account_configs(accounts, target_dir=tmp_dir)
            self.assertTrue(ok)

            f1 = os.path.join(tmp_dir, "config", "onebot11_123456.json")
            f2 = os.path.join(tmp_dir, "config", "onebot11_654321.json")
            self.assertTrue(os.path.exists(f1))
            self.assertTrue(os.path.exists(f2))

            with open(f1, "r", encoding="utf-8") as fp:
                d1 = json.load(fp)
            self.assertEqual(d1["ws"]["port"], 3001)
            self.assertEqual(d1["ws"]["secret"], "token1")

            with open(f2, "r", encoding="utf-8") as fp:
                d2 = json.load(fp)
            self.assertEqual(d2["ws"]["port"], 3002)
            self.assertEqual(d2["ws"]["secret"], "token2")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestGroupChatMonitorWidget(unittest.TestCase):
    """测试 GUI 侧边栏群聊消息实时监控控件"""

    def test_monitor_widget_events_and_filtering(self):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from gui import GroupChatMonitorWidget

        app = QApplication.instance() or QApplication([])
        widget = GroupChatMonitorWidget()

        # 接收两条不同群消息
        widget.handle_feed_event("msg", {
            "group_id": "grp_101",
            "sender": "张三",
            "user_id": "1001",
            "content": "大家好啊",
            "is_at_bot": False
        })
        widget.handle_feed_event("msg", {
            "group_id": "grp_102",
            "sender": "李四",
            "user_id": "1002",
            "content": "吃饭了吗？",
            "is_at_bot": True
        })

        # 机器人回复事件
        widget.handle_feed_event("action", {
            "group_id": "grp_102",
            "action": "at",
            "reply": "还没吃呢喵！",
            "target": "1002",
            "reason": "AI参聊决策"
        })

        self.assertEqual(len(widget._all_messages), 3)
        self.assertIn("grp_101", widget._monitored_groups)
        self.assertIn("grp_102", widget._monitored_groups)

        # 过滤群聊
        widget._current_filter_group = "grp_101"
        widget._refresh_all_display()
        rendered_text = widget.feed_browser.toPlainText()
        self.assertIn("张三", rendered_text)
        self.assertNotIn("还没吃呢喵", rendered_text)

        # 清空
        widget.clear_feed()
        self.assertEqual(len(widget._all_messages), 0)
        self.assertEqual(widget.feed_browser.toPlainText(), "")


class TestAIDrivenAutonomousDecisionAndRhythm(unittest.TestCase):
    """测试 AI 智能决策参聊、安全政策风险规避、节奏自适应及上下文 @/引用判定"""

    def test_safety_policy_risk_pre_screening(self):
        from prompt_builder import check_safety_policy_risk

        # 正常群聊消息
        safe_msg = "大家觉得今天的天气怎么样？想去喝奶茶"
        is_risk, reason = check_safety_policy_risk(safe_msg)
        self.assertFalse(is_risk)
        self.assertEqual(reason, "")

        # 高危敏感词消息
        risky_msg = "有人知道怎么在暗网买枪支弹药吗"
        is_risk, reason = check_safety_policy_risk(risky_msg)
        self.assertTrue(is_risk)
        self.assertIn("暗网", reason)

    def test_chat_rhythm_analysis(self):
        buf = GroupContextBuffer()
        # 模拟短快短句群聊（平均字数 8 字左右）
        buf.add_message("g1", "u1", "群友A", "哈哈哈哈")
        buf.add_message("g1", "u2", "群友B", "确实如此")
        buf.add_message("g1", "u3", "群友C", "笑死我了")

        rhythm = buf.analyze_chat_rhythm("g1")
        self.assertLessEqual(rhythm["avg_length"], 15)
        self.assertIn("短快", rhythm["rhythm_style"])

        # 模拟深度探讨群聊
        buf2 = GroupContextBuffer()
        buf2.add_message("g2", "u1", "技术专家", "关于异步事件循环的设计，我们需要重点评估高并发下的队列堆积与延迟情况，特别是针对网络断线的重试机制。")
        buf2.add_message("g2", "u2", "架构师", "同意，在底层协议层应当加入背压机制，并在超时阶段进行会话隔离与保护。")

        rhythm2 = buf2.analyze_chat_rhythm("g2")
        self.assertGreater(rhythm2["avg_length"], 40)
        self.assertIn("深入探讨", rhythm2["rhythm_style"])

    def test_parse_ai_decision_json_should_reply_false(self):
        from prompt_builder import parse_ai_decision_and_reply

        raw_json = '{"should_reply": false, "reason": "群友之间私聊，无需插话"}'
        res = parse_ai_decision_and_reply(raw_json)
        self.assertFalse(res.should_reply)
        self.assertIn("私聊", res.reason)

    def test_parse_ai_decision_json_actions_and_format(self):
        from prompt_builder import parse_ai_decision_and_reply

        # 1. Action: at
        raw1 = '```json\n{"should_reply": true, "action": "at", "reply": "才不是这样呢！"}\n```'
        res1 = parse_ai_decision_and_reply(raw1, default_user_id="12345", default_msg_id="m001")
        self.assertTrue(res1.should_reply)
        self.assertEqual(res1.action, "at")
        final_msg1 = res1.format_final_message()
        self.assertEqual(final_msg1, "[CQ:at,qq=12345] 才不是这样呢！")

        # 2. Action: quote
        raw2 = '{"should_reply": true, "action": "quote", "reply": "赞同这个观点！"}'
        res2 = parse_ai_decision_and_reply(raw2, default_user_id="12345", default_msg_id="m001")
        self.assertTrue(res2.should_reply)
        self.assertEqual(res2.action, "quote")
        final_msg2 = res2.format_final_message()
        self.assertEqual(final_msg2, "[CQ:reply,id=m001] 赞同这个观点！")

        # 3. Action: both
        raw3 = '{"should_reply": true, "action": "both", "reply": "确实！"}'
        res3 = parse_ai_decision_and_reply(raw3, default_user_id="12345", default_msg_id="m001")
        self.assertEqual(res3.action, "both")
        final_msg3 = res3.format_final_message()
        self.assertEqual(final_msg3, "[CQ:reply,id=m001][CQ:at,qq=12345] 确实！")

        # 4. Action: none (公开广播随口吐槽，不@也不引用)
        raw4 = '{"should_reply": true, "action": "none", "reply": "突然好想吃蛋糕喵……"}'
        res4 = parse_ai_decision_and_reply(raw4, default_user_id="12345", default_msg_id="m001")
        self.assertEqual(res4.action, "none")
        final_msg4 = res4.format_final_message()
        self.assertEqual(final_msg4, "突然好想吃蛋糕喵……")

    def test_onebot_client_ai_driven_autonomous_flow(self):
        """测试 OneBotClient 完整由 AI 决策参聊、安全拦截与行为控制"""
        import asyncio
        from onebot_client import OneBotClient

        async def run_test():
            tmp_mem = tempfile.mkdtemp()
            try:
                cfg = AppConfig()
                cfg.bot.autonomous_enabled = True
                cfg.bot.reply_probability = 1.0
                cfg.bot.cooldown_seconds = 0

                mock_ai = mock_agent()
                # 兼容旧 JSON 文本，引用由控制台按新消息决定。
                mock_ai.chat = ConsoleAgentMock(return_value='{"should_reply": true, "action": "quote", "reply": "这个确实挺有意思的喵！"}')

                ob = OneBotClient(
                    config=cfg,
                    antigravity_client=mock_ai,
                    memory_manager=GroupMemoryManager(storage_dir=tmp_mem)
                )

                sent_msgs = []
                async def mock_send(grp, msg):
                    sent_msgs.append((grp, msg))
                ob.send_group_message = mock_send
                ob.self_id = "bot_100"

                # 1. 没有后续消息时直接回复，忽略旧格式的 quote 动作。
                msg_data = {
                    "group_id": "8888",
                    "user_id": "7777",
                    "message_id": "m_test_99",
                    "raw_message": "推荐一部好看的动画吧",
                    "sender": {"nickname": "群友小明"}
                }
                await ob._handle_group_message(msg_data)
                self.assertEqual(len(sent_msgs), 1)
                self.assertNotIn("[CQ:reply,id=m_test_99]", sent_msgs[-1][1])
                self.assertEqual(sent_msgs[-1][1].reply_to, "m_test_99")
                self.assertIn("这个确实挺有意思的喵！", sent_msgs[-1][1])

                # 2. 安全风险消息 -> 直接静默放弃，不调用 AI 且不发送消息
                sent_msgs.clear()
                mock_ai.chat.reset_mock()
                risky_data = {
                    "group_id": "8888",
                    "user_id": "7777",
                    "message_id": "m_test_100",
                    "raw_message": "涉政 反动 台独",
                    "sender": {"nickname": "违规用户"}
                }
                await ob._handle_group_message(risky_data)
                mock_ai.chat.assert_not_called()
                self.assertEqual(len(sent_msgs), 0)

                # 3. AI 决策不参聊 (should_reply: false) -> 不发送消息
                sent_msgs.clear()
                mock_ai.chat = ConsoleAgentMock(return_value='{"should_reply": false, "reason": "无关灌水"}')
                normal_data = {
                    "group_id": "8888",
                    "user_id": "7777",
                    "message_id": "m_test_101",
                    "raw_message": "1",
                    "sender": {"nickname": "刷屏群友"}
                }
                await ob._handle_group_message(normal_data)
                self.assertEqual(len(sent_msgs), 0)

            finally:
                shutil.rmtree(tmp_mem, ignore_errors=True)

        asyncio.run(run_test())


class TestReviewV2EnhancementsAndEdgeCases(unittest.TestCase):
    """测试 Review V2 针对配置同步、路径解析、鲁棒JSON解析与GUI流缓冲的全面防护"""

    def test_config_sync_preserves_account2_port(self):
        """测试加载具有不同端口的双账号配置时，绝不发生反向覆盖导致端口重置"""
        tmp_dir = tempfile.mkdtemp()
        try:
            cfg_path = os.path.join(tmp_dir, "test_dual_port.yaml")
            cfg = AppConfig()
            cfg.onebot.accounts["account_1"].ws_url = "ws://127.0.0.1:3001"
            cfg.onebot.accounts["account_2"].ws_url = "ws://127.0.0.1:3002"
            cfg.onebot.switch_account("account_2")
            cfg.save(cfg_path)

            loaded = AppConfig.load(cfg_path)
            self.assertEqual(loaded.onebot.active_account, "account_2")
            self.assertEqual(loaded.onebot.accounts["account_2"].ws_url, "ws://127.0.0.1:3002")
            self.assertEqual(loaded.onebot.ws_url, "ws://127.0.0.1:3002")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_napcat_path_resolution_and_empty_qq_multi_configs(self):
        """测试 NapCatManager 处理直达可执行文件路径以及空 QQ 号多账号配置不冲突"""
        from napcat_manager import NapCatManager
        tmp_dir = tempfile.mkdtemp()
        try:
            # 1. 模拟可执行文件路径
            fake_exe = os.path.join(tmp_dir, "napcat.win32.x64.exe")
            with open(fake_exe, "w") as f:
                f.write("mock")

            mgr = NapCatManager(base_dir=tmp_dir)
            self.assertTrue(mgr.is_installed(fake_exe))
            self.assertEqual(mgr.get_executable_path(fake_exe), os.path.abspath(fake_exe))

            # 2. 空 QQ 号时的双账号独立配置文件生成
            accounts = {
                "account_1": {"account_id": "", "ws_url": "ws://127.0.0.1:3001", "access_token": ""},
                "account_2": {"account_id": "", "ws_url": "ws://127.0.0.1:3002", "access_token": ""}
            }
            ok = mgr.setup_multi_account_configs(accounts, target_dir=tmp_dir)
            self.assertTrue(ok)

            cfg_dir = os.path.join(tmp_dir, "config")
            self.assertTrue(os.path.exists(os.path.join(cfg_dir, "onebot11_account_1.json")))
            self.assertTrue(os.path.exists(os.path.join(cfg_dir, "onebot11_account_2.json")))
            self.assertTrue(os.path.exists(os.path.join(cfg_dir, "onebot11.json")))

            with open(os.path.join(cfg_dir, "onebot11_account_1.json"), "r", encoding="utf-8") as f1:
                d1 = json.load(f1)
            with open(os.path.join(cfg_dir, "onebot11_account_2.json"), "r", encoding="utf-8") as f2:
                d2 = json.load(f2)
            self.assertEqual(d1["ws"]["port"], 3001)
            self.assertEqual(d2["ws"]["port"], 3002)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_parse_ai_decision_robust_json_and_safe_fallback(self):
        """测试复杂带前缀思考、多层大括号的 JSON 提取与自主模式安全防泄密"""
        from prompt_builder import parse_ai_decision_and_reply

        # 1. 带有前置思考与独立大括号的文本，应精准提取有效的 should_reply 字典
        raw_prefixed = 'Thinking: {analysis: "user is asking question"} {"should_reply": true, "action": "quote", "reply": "这是正确答案！"}'
        res1 = parse_ai_decision_and_reply(raw_prefixed, default_user_id="u1", default_msg_id="m1")
        self.assertTrue(res1.should_reply)
        self.assertEqual(res1.action, "quote")
        self.assertEqual(res1.reply_text, "这是正确答案！")

        # 2. 自主模式下，模型输出解释或思考链而非标准决策时，必须安全静默跳过，严禁发到群聊
        raw_thought = "当前群友在讨论数学作业，我认为不适合插话，保持沉默。"
        res2 = parse_ai_decision_and_reply(raw_thought, default_should_reply=False)
        self.assertFalse(res2.should_reply)

        # 3. 主人发言或 @ 机器人时，若模型未输出 JSON，纯文本应作为对白回复
        res3 = parse_ai_decision_and_reply("好的主人！我在听呢！", default_should_reply=True, is_master=True)
        self.assertTrue(res3.should_reply)
        self.assertEqual(res3.reply_text, "好的主人！我在听呢！")

    def test_compose_autonomous_decision_prompt_with_privileges_and_rhythm(self):
        """测试自主参聊 Prompt 构建时正确注入主人特权、@提醒与节奏目标建议"""
        from prompt_builder import compose_autonomous_decision_prompt

        rhythm = {"avg_length": 8.0, "typical_length": 8, "rhythm_style": "短快闲聊", "suggested_max": 20}
        prompt = compose_autonomous_decision_prompt(
            user_text="在吗？",
            author_name="主人",
            is_master=True,
            chat_rhythm=rhythm,
            max_reply_length=100
        )
        self.assertIn("当前发言者是主人", prompt)
        self.assertIn('"suggested_max": 20', prompt)
        self.assertIn("不压缩必要信息", prompt)
        self.assertNotIn("主人特权", prompt)

    def test_gui_account_switch_preserves_edits(self):
        """测试 GUI 在下拉框切换账号时，会自动保存之前正在编辑的账号文本"""
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from gui import BotStudioGUI

        app = QApplication.instance() or QApplication([])
        tmp_dir = tempfile.mkdtemp()
        try:
            cfg_path = os.path.join(tmp_dir, "test_gui_cfg.yaml")
            cfg = AppConfig()
            cfg.save(cfg_path)

            gui = BotStudioGUI(config_path=cfg_path)
            # 当前编辑 account_1，输入 QQ 号
            gui.edit_ob_acc_qq.setText("99887766")
            gui.edit_ob_acc_name.setText("自定义小号A")

            # 切换到 account_2
            gui.combo_ob_account.setCurrentIndex(1)

            # 检查 account_1 的字段是否已被正确持久化
            acc1 = gui.app_config.onebot.accounts["account_1"]
            self.assertEqual(acc1.account_id, "99887766")
            self.assertEqual(acc1.name, "自定义小号A")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_gui_stdout_buffering_and_ipc_filtering(self):
        """测试 GUI 标准输出分包缓冲拼接与 IPC 消息隔离"""
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from gui import BotStudioGUI

        app = QApplication.instance() or QApplication([])
        tmp_dir = tempfile.mkdtemp()
        try:
            cfg_path = os.path.join(tmp_dir, "test_gui_buf.yaml")
            AppConfig().save(cfg_path)
            gui = BotStudioGUI(config_path=cfg_path)

            # 模拟分包接收 [FEED_MSG] 数据
            gui._stdout_buffer = ""
            chunk1 = '[FEED_MSG] {"time": "12:00:00", "group_id": "999", '
            chunk2 = '"sender": "小王", "content": "你好", "is_at_bot": false}\n[INFO] 机器人启动完成\n'

            # 注入 chunk1，末尾无换行，应留在缓冲区
            gui.bot_process = MagicMock()
            gui.bot_process.readAllStandardOutput = MagicMock(return_value=MagicMock(data=lambda: chunk1.encode("utf-8")))
            gui._on_process_output()
            self.assertEqual(len(gui.sidebar_monitor._all_messages), 0)
            self.assertIn("FEED_MSG", gui._stdout_buffer)

            # 注入 chunk2，完整拼接后触发事件并过滤终端输出
            gui.bot_process.readAllStandardOutput = MagicMock(return_value=MagicMock(data=lambda: chunk2.encode("utf-8")))
            gui._on_process_output()
            self.assertEqual(len(gui.sidebar_monitor._all_messages), 1)
            self.assertEqual(gui.sidebar_monitor._all_messages[0]["content"], "你好")
            # 确认 [FEED_MSG] 没有直接裸露打印到控制台日志
            terminal_text = gui.log_terminal.toPlainText()
            self.assertNotIn("[FEED_MSG]", terminal_text)
            self.assertIn("机器人启动完成", terminal_text)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestInteractionAndEntityFixes(unittest.TestCase):
    """验证说话人与 @ 实体精准解析、戳一戳互动与群聊自然沟通修复"""

    def setUp(self):
        import asyncio
        import time
        self.asyncio = asyncio
        self.time = time

    def test_clean_cq_mentions_third_party(self):
        """测试 @机器人 且同时 @其他群成员 时，正确保留被提及目标并识别意图"""
        from onebot_client import clean_cq_mentions

        raw = "[CQ:at,qq=99000000003] 给我骂[CQ:at,qq=2109165295,name=我有点小无奈啊(_|_)]"
        clean_text, is_at_bot, mentioned_users = clean_cq_mentions(
            text=raw,
            self_id="99000000003"
        )
        self.assertTrue(is_at_bot, "应识别到对机器人的呼叫")
        self.assertIn("@我有点小无奈啊(_|_)", clean_text, "应完整保留被提及的目标群友昵称")
        self.assertNotIn("99000000003", clean_text, "应清洗掉针对机器人自身的标签")
        self.assertEqual(len(mentioned_users), 1)
        self.assertEqual(mentioned_users[0]["user_id"], "2109165295")
        self.assertEqual(mentioned_users[0]["nickname"], "我有点小无奈啊(_|_)")

    def test_clean_cq_mentions_segments_and_bot_name(self):
        """测试通过 segments 数组与明文昵称解析 @ 机器人与第三方"""
        from onebot_client import clean_cq_mentions

        segments = [
            {"type": "at", "data": {"qq": "99000000003"}},
            {"type": "text", "data": {"text": " 你认识"}},
            {"type": "at", "data": {"qq": "10001", "name": "千绘莉姐姐"}}
        ]
        clean_text, is_at_bot, mentioned_users = clean_cq_mentions(
            text="[CQ:at,qq=99000000003] 你认识[CQ:at,qq=10001]",
            self_id="99000000003",
            segments=segments
        )
        self.assertTrue(is_at_bot)
        self.assertEqual(len(mentioned_users), 1)
        self.assertEqual(mentioned_users[0]["user_id"], "10001")
        self.assertEqual(mentioned_users[0]["nickname"], "千绘莉姐姐")

    def test_prompt_includes_mentioned_users_instruction(self):
        """测试决策 Prompt 注入提及成员实体，防止张冠李戴"""
        from prompt_builder import compose_autonomous_decision_prompt

        prompt = compose_autonomous_decision_prompt(
            user_text="给我骂 @张三",
            author_name="李四",
            mentioned_users=[{"user_id": "12345", "nickname": "张三"}]
        )
        self.assertIn('"nickname": "张三"', prompt)
        self.assertIn('"user_id": "12345"', prompt)
        self.assertIn("勿误指发言者", prompt)

    def test_poke_event_handling_flow(self):
        """测试拍一拍/戳一戳/摸头互动事件的分发与角色反应"""
        from onebot_client import OneBotClient
        from antigravity_client import AntigravityClient
        from persona_manager import PersonaManager

        ai_mock = mock_agent()
        ai_mock.chat = ConsoleAgentMock(return_value="干、干嘛摸我头啊！头发都乱了……！")
        persona_mgr = PersonaManager()

        client = OneBotClient(
            config=AppConfig(),
            antigravity_client=ai_mock,
            persona_manager=persona_mgr
        )
        client.self_id = "99000000003"
        client.send_group_message = AsyncMock()
        client.send_action = AsyncMock(return_value={"status": "ok", "data": {}})

        # 模拟收到针对机器人的拍头事件
        poke_data = {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "group_id": 9999,
            "user_id": 1111,
            "target_id": 99000000003,
            "action": "拍了拍",
            "suffix": "的头",
            "raw_info": "群友 拍了拍 ヨンミン 的头"
        }

        import asyncio
        async def run_poke():
            await client._dispatch_event(poke_data)

        asyncio.run(run_poke())
        client.send_group_message.assert_called_once()
        sent_text = client.send_group_message.call_args[0][1]
        self.assertTrue("干嘛" in sent_text or "摸" in sent_text)

    def test_ordinary_quiet_phrases_do_not_put_group_analysis_to_sleep(self):
        """非 @ 的闭嘴/思考用语交由本轮分析判断，不产生持续群静默。"""
        import asyncio
        import time
        from onebot_client import OneBotClient
        from antigravity_client import AntigravityClient

        ai_mock = mock_agent()
        client = OneBotClient(config=AppConfig(), antigravity_client=ai_mock)
        client.app_config.bot.autonomous_enabled = True
        client.app_config.bot.search_enabled = False
        ai_mock.chat = ConsoleAgentMock(return_value='{"should_reply":false,"reason":"本轮先听"}')
        client.self_id = "99000000003"
        client.send_group_message = AsyncMock()

        msg_data = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 8888,
            "user_id": 7777,
            "message_id": "msg_silence_1",
            "raw_message": "你先闭嘴，我在思考"
        }

        async def run_silence():
            client.app_config.bot.chat_buffer_seconds = 0  # This test exercises the legacy independent image lane.
            await client._handle_group_message(msg_data)

        asyncio.run(run_silence())
        ai_mock.chat.assert_awaited_once()
        client.send_group_message.assert_not_called()
        self.assertFalse(client._proactive_chat.groups["8888"].quiet)


class TestWebSearchAndMeme(unittest.TestCase):
    """测试网络搜梗、生僻言语解析与合理用梗指引"""

    def test_detect_meme_or_obscure_phrase(self):
        from web_search import default_meme_searcher
        # 1. 显式求梗问句
        t1 = default_meme_searcher.detect_meme_or_obscure_phrase("city不city是什么梗？")
        self.assertEqual(t1, "city不city")

        t2 = default_meme_searcher.detect_meme_or_obscure_phrase("疯狂星期四啥意思啊")
        self.assertEqual(t2, "疯狂星期四")

        # 2. 流行网络黑话关键词
        t3 = default_meme_searcher.detect_meme_or_obscure_phrase("今天这波操作简直yyds")
        self.assertEqual(t3, "yyds")

        t4 = default_meme_searcher.detect_meme_or_obscure_phrase("尊嘟假嘟，你真的要去吗")
        self.assertEqual(t4, "尊嘟假嘟")

        # 3. 普通非梗文本不触发
        t5 = default_meme_searcher.detect_meme_or_obscure_phrase("今天中午吃米饭还是吃面条？")
        self.assertIsNone(t5)

    def test_search_meme_and_format_prompt(self):
        import asyncio
        from web_search import default_meme_searcher

        async def run_search():
            res = await default_meme_searcher.search_meme_or_phrase("电子木鱼")
            self.assertIsNotNone(res)
            self.assertEqual(res["term"], "电子木鱼")
            self.assertIn("功德", res["meaning"])
            self.assertTrue(len(res["guidance"]) > 0)

            # 格式化 Prompt
            sec = default_meme_searcher.format_meme_prompt_section(res)
            self.assertIn("【网络梗与未知语境检索解析】", sec)
            self.assertIn("- 关注词条: 「电子木鱼」", sec)
            self.assertIn("- 释义参考:", sec)
            self.assertIn("- 合理用梗指引:", sec)

        asyncio.run(run_search())

    def test_search_caching(self):
        import asyncio
        from web_search import MemeWebSearcher

        searcher = MemeWebSearcher(cache_ttl_seconds=60)
        async def run_cache():
            res1 = await searcher.search_meme_or_phrase("泰裤辣")
            self.assertIn("泰裤辣", searcher._search_cache)
            res2 = await searcher.search_meme_or_phrase("泰裤辣")
            self.assertEqual(res1, res2)

        asyncio.run(run_cache())


class TestStreamlinedPersona(unittest.TestCase):
    """测试人设预设精简、去剧场怪味与 Token 节约"""

    def test_personas_yaml_streamlined(self):
        from persona_manager import PersonaManager
        pm = PersonaManager("personas.yaml")
        fubuki = pm.get_persona("雪村ふぶき (千绘莉的妹妹)")
        self.assertIsNotNone(fubuki)

        # 验证提示词长度大幅精简（原版 830+ 字符，精简版应在 300 字符以内，压缩率 > 60%）
        self.assertLess(len(fubuki.prompt), 300)

        # 验证剔除了过度入戏与舞台剧怪味描写
        self.assertNotIn("蓬松的白绒毛簇", fubuki.prompt)
        self.assertNotIn("灵巧而敏感的猫尾巴", fubuki.prompt)
        self.assertNotIn("俄罗斯蓝猫兽耳少女", fubuki.prompt)

        # Character presets describe identity; shared style is composed once.
        self.assertIn("雪村ふぶき", fubuki.prompt)
        self.assertIn("自己的好奇心", fubuki.prompt)
        self.assertNotIn("绝对顺从", fubuki.prompt)
        self.assertIn("性格体现在", compose_system_prompt(fubuki.prompt))

    def test_all_personas_compact_and_natural(self):
        from persona_manager import DEFAULT_PERSONAS
        for name, p in DEFAULT_PERSONAS.items():
            # 所有预设均控制在 250 字以内，避免给每个会话带来额外数百 Token 负担
            self.assertLess(len(p.prompt), 250, f"人设 {name} 的 Prompt 过长，应保持精简")


class TestMultimodalImageRecognitionAndFiltering(unittest.TestCase):
    """测试表情包和图片语义识别、意义不明过滤与对应回复指引"""

    def test_ambiguous_image_not_casually_replied(self):
        from image_handler import evaluate_multimodal_intent

        # 1. 纯占位图或普通未知配图，且无有效文字
        ambiguous_imgs = [{
            "summary": "群聊配图 (800x600 JPEG)",
            "is_sticker": False,
            "meaning_understood": False,
            "is_ambiguous": True
        }]
        intent = evaluate_multimodal_intent("", ambiguous_imgs)
        # 应判定意义不明，避免随意回复
        self.assertTrue(intent["should_avoid_reply"])
        self.assertTrue(intent["all_ambiguous"])
        self.assertFalse(intent["meaning_understood"])
        self.assertIn("不随意回复", intent["guidance"])

    def test_understood_stickers_and_faces_replied(self):
        from image_handler import evaluate_multimodal_intent

        # 2. 识别了具体含义的表情包
        understood_imgs = [{
            "summary": "表情包 (128x128 PNG)",
            "is_sticker": True,
            "meaning_understood": True,
            "is_ambiguous": False,
            "semantic_meaning": "表达「摸鱼」相关情绪/梗点"
        }]
        intent = evaluate_multimodal_intent("[表情: 滑稽]", understood_imgs)
        self.assertFalse(intent["should_avoid_reply"])
        self.assertTrue(intent["meaning_understood"])
        self.assertIn("QQ表情「滑稽」", intent["understood_meanings"])
        self.assertIn("回复契合的幽默/生动语句", intent["guidance"])

    def test_onebot_skips_ambiguous_image_reply(self):
        """测试 OneBot 客户端在非 @ 且非主人时，对意义不明的表情包保持沉默（即使 autonomous_enabled=True）"""
        import asyncio
        from onebot_client import OneBotClient
        from antigravity_client import AntigravityClient

        ai_mock = mock_agent()
        app_cfg = AppConfig()
        app_cfg.bot.autonomous_enabled = True  # 启用自主回复，确认即使在自主模式下依然被表情包过滤规则拦截
        client = OneBotClient(config=app_cfg, antigravity_client=ai_mock)
        client.self_id = "99000000003"
        client.send_group_message = AsyncMock()

        # 发送一张无任何说明的未知图片
        msg_data = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 9999,
            "user_id": 1234,
            "message_id": "msg_ambiguous_img",
            "raw_message": "[CQ:image,file=unknown_junk_123.jpg]"
        }

        async def run_msg():
            client.app_config.bot.chat_buffer_seconds = 0  # This test exercises the legacy independent image lane.
            await client._handle_group_message(msg_data)

        asyncio.run(run_msg())
        # 未 @ 机器人，且图片意义不明，按规范不随意回复
        ai_mock.chat.assert_not_called()
        client.send_group_message.assert_not_called()

    def test_onebot_must_reply_overrides_ambiguous_image_filter(self):
        """主人图片无法获取时由程序报告实际状态，不让主聊天猜测画面。"""
        import asyncio
        from onebot_client import OneBotClient
        from antigravity_client import AntigravityClient

        ai_mock = mock_agent()
        ai_mock.chat = ConsoleAgentMock(return_value='{"should_reply": true, "action": "none", "reply": "这张图什么意思呀，没看懂呢"}')
        app_cfg = AppConfig()
        app_cfg.bot.master_id = "1234"  # 设置说话人为主人
        client = OneBotClient(config=app_cfg, antigravity_client=ai_mock)
        client._qq_capture = MagicMock(capture_message=AsyncMock(side_effect=RuntimeError("fixture capture unavailable")))
        client.self_id = "99000000003"
        client.send_group_message = AsyncMock()
        client.send_action = AsyncMock(return_value={"status": "failed", "retcode": 100, "data": {}})
        client._send_text = AsyncMock()

        msg_data = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 9999,
            "user_id": 1234,
            "message_id": "msg_master_ambiguous_img",
            "raw_message": "[CQ:image,file=unknown_junk_123.jpg]"
        }

        async def run_msg():
            try:
                client.app_config.bot.chat_buffer_seconds = 0  # This test exercises the legacy independent image lane.
                await client._handle_group_message(msg_data)
                await client._get_runtime().work_worker
            finally:
                await client._close_runtime()

        asyncio.run(run_msg())
        ai_mock.chat.assert_not_awaited()
        client._qq_capture.capture_message.assert_awaited_once()
        self.assertIn("get_image", [call.args[0] for call in client.send_action.await_args_list])
        client._send_text.assert_awaited_once()
        self.assertEqual(client._send_text.call_args.args[:3], ("send_group_msg", "group_id", "9999"))
        status = client._send_text.call_args.args[3]
        self.assertIn("截图和原图获取或识别均未完成", status)
        self.assertIn("暂时无法确认画面内容", status)
        self.assertNotIn("重新发送", status)
        self.assertNotIn("什么意思", status)


class TestGroupAtmosphereAndPermanentPreset(unittest.TestCase):
    """测试群聊总体氛围、人物画像总结与永久记忆库预设"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.mgr = GroupMemoryManager(storage_dir=self.temp_dir, max_entries=10)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_summarize_group_atmosphere_and_portraits(self):
        grp_id = "test_anime_group"

        # 模拟多次对话：二次元、游戏与日常
        dialogs = [
            ("user_a", "猫猫酱", "今天原神更新了，抽到新角色了吗？", "哼，才没有特意抽呢，运气好而已！"),
            ("user_b", "甜点师傅", "千绘莉姐姐今天做的草莓蛋糕超好吃！", "那当然啦！千绘莉姐姐手艺可是天下第一！"),
            ("user_a", "猫猫酱", "二次元番剧有推荐的吗？", "巧克甜恋呀，笨蛋！"),
            ("user_c", "路人", "大家早安啊！", "早安喵，今天也要打起精神！")
        ]

        for uid, name, umsg, brep in dialogs:
            self.mgr.record_interaction(
                group_openid=grp_id,
                user_openid=uid,
                user_msg=umsg,
                bot_reply=brep,
                author_name=name
            )

        # 触发总结
        res = self.mgr.summarize_group_profile(grp_id)
        self.assertIn("二次元", res["atmosphere"])
        self.assertIn("user_a", res["member_portraits"])
        self.assertIn("猫猫酱", res["member_portraits"]["user_a"])

        # 检查永久预设
        preset = res["permanent_preset"]
        self.assertIn("【群聊专属永久记忆库预设】", preset)
        self.assertIn("群聊总体氛围:", preset)
        self.assertIn("猫猫酱", preset)

        # 检查持久化到磁盘与重新加载
        mem_reloaded = self.mgr.get_group_memory(grp_id)
        self.assertEqual(mem_reloaded.atmosphere, res["atmosphere"])
        self.assertEqual(mem_reloaded.permanent_preset, preset)
        self.assertIn("user_a", mem_reloaded.member_portraits)


class TestPeriodicPresetReminderAndTokenOptimization(unittest.TestCase):
    """Legacy permanent content remains compatible without repeated performance prompts."""

    def test_autonomous_decision_prompt_periodic_reminder(self):
        from prompt_builder import compose_autonomous_decision_prompt

        permanent_preset = "【群聊专属永久记忆库预设】\n- 群聊总体氛围: 二次元闲聊\n- 群聊重点人物画像: 群友A"

        # 第 1 轮交互：应包含完整预设与指令
        p1 = compose_autonomous_decision_prompt(
            user_text="今天天气不错",
            author_name="小明",
            interaction_turn=1,
            permanent_preset=permanent_preset,
            preset_reminder_interval=10
        )
        self.assertIn("【群聊专属永久记忆库预设】", p1)
        self.assertIn('"should_reply"', p1)

        # 第 2 轮交互：采用精简快速模式，不重复发送完整预设与繁杂规范，节约 Token！
        p2 = compose_autonomous_decision_prompt(
            user_text="确实挺好的",
            author_name="小李",
            interaction_turn=2,
            permanent_preset=permanent_preset,
            preset_reminder_interval=10
        )
        self.assertNotIn("【群聊专属永久记忆库预设】", p2)
        self.assertNotIn("【快速参聊决策模式", p2)
        self.assertLess(len(p2), len(p1))

        # 第 10 轮交互：触发周期提醒，重新注入预设与周期提醒标签！
        p10 = compose_autonomous_decision_prompt(
            user_text="下周要开学了",
            author_name="小王",
            interaction_turn=10,
            permanent_preset=permanent_preset,
            preset_reminder_interval=10
        )
        self.assertNotIn("【人设与群记忆周期提醒", p10)
        self.assertIn("【群聊专属永久记忆库预设】", p10)

        # 第 11 轮交互：重新回归精简模式
        p11 = compose_autonomous_decision_prompt(
            user_text="作业写完了吗",
            author_name="小明",
            interaction_turn=11,
            permanent_preset=permanent_preset,
            preset_reminder_interval=10
        )
        self.assertNotIn("【群聊专属永久记忆库预设】", p11)
        self.assertNotIn("【快速参聊决策模式", p11)

    def test_user_prompt_periodic_reminder(self):
        from prompt_builder import compose_user_prompt

        preset = "【群聊专属永久记忆库预设】群总体氛围: 轻松闲聊"

        # 轮次 1：包含预设
        u1 = compose_user_prompt("你好", author_name="小明", interaction_turn=1, permanent_preset=preset)
        self.assertIn("【群聊专属永久记忆库预设】", u1)

        # 轮次 2：精炼模式，节约 Token
        u2 = compose_user_prompt("你好呀", author_name="小明", interaction_turn=2, permanent_preset=preset)
        self.assertNotIn("【群聊专属永久记忆库预设】", u2)
        self.assertNotIn("【继续交流", u2)
        # 轮次 10：注入周期提醒
        u10 = compose_user_prompt("再聊会", author_name="小明", interaction_turn=10, permanent_preset=preset)
        self.assertNotIn("【人设与群记忆周期提醒", u10)
        self.assertIn("【群聊专属永久记忆库预设】", u10)

    def test_multimodal_intent_strips_guidance_tags(self):
        """测试多模态意图识别正确剔除【多模态感知:...】等内部标签，避免意义不明图片被误判为实质性发言"""
        from image_handler import evaluate_multimodal_intent
        ambiguous_imgs = [{
            "local_path": None,
            "url": "",
            "width": 0,
            "height": 0,
            "format": "UNKNOWN",
            "is_sticker": False,
            "summary": "群聊图片",
            "meaning_understood": False,
            "is_ambiguous": True,
            "semantic_meaning": ""
        }]
        # 即使 enriched_text 已经带有内部感知指引，也能准确判断用户没有发实质性提问
        text_with_guidance = (
            "[用户发送了图片: 群聊图片]\n"
            "【多模态感知：图片经分析意义不明且用户无实质提问。规范：不随意回复（判定 should_reply=false）；若被明确要求回复则自然反问没看懂。】"
        )
        intent = evaluate_multimodal_intent(text_with_guidance, ambiguous_imgs)
        self.assertTrue(intent["should_avoid_reply"])
        self.assertFalse(intent["meaning_understood"])

    def test_default_personas_streamlined(self):
        """测试 DEFAULT_PERSONAS 默认预设与 personas.yaml 一致精简且无夸张戏腔与冗长动作描写"""
        from persona_manager import DEFAULT_PERSONAS, PersonaManager
        with tempfile.TemporaryDirectory() as temporary:
            pm = PersonaManager(os.path.join(temporary, "personas.yaml"))
            self.assertEqual(pm.active_persona_name, "雪村ふぶき (千绘莉的妹妹)")
        for name, preset in DEFAULT_PERSONAS.items():
            self.assertLess(len(preset.prompt), 300, f"{name} prompt 过长")
            self.assertNotIn("白绒毛簇", preset.prompt)
            self.assertNotIn("敏感猫尾巴", preset.prompt)

    def test_group_profile_traits_before_compression(self):
        """测试群画像在记忆尚未触发压缩（interaction < 15）前，仍能直接从交互发言中提炼成员兴趣标签"""
        temp_dir = tempfile.mkdtemp()
        try:
            mgr = GroupMemoryManager(storage_dir=temp_dir, max_entries=20)
            grp = "group_traits_early"
            for i in range(4):
                mgr.record_interaction(grp, "user_coder", f"这个 Python 异步并发 bug 该怎么解决 {i}", f"可以加互斥锁 {i}")

            res = mgr.summarize_group_profile(grp)
            portraits = res.get("member_portraits", {})
            self.assertIn("user_coder", portraits)
            self.assertIn("技术", portraits["user_coder"])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_expanded_meme_detection_and_offline_lookup(self):
        """测试扩充后的流行网络热梗与黑话探测及离线知识库解析"""
        from web_search import default_meme_searcher
        import asyncio

        terms = ["蚌埠住了", "遥遥领先", "汗流浃背", "老六", "急了", "典中典", "dddd", "yygq"]
        for t in terms:
            detected = default_meme_searcher.detect_meme_or_obscure_phrase(f"有人说{t}是什么意思啊")
            self.assertIsNotNone(detected, f"未能探测出热词: {t}")

        async def run_lookup():
            r1 = await default_meme_searcher.search_meme_or_phrase("蚌埠住了")
            self.assertIsNotNone(r1)
            self.assertIn("绷不住了", r1["meaning"])

            r2 = await default_meme_searcher.search_meme_or_phrase("遥遥领先")
            self.assertIsNotNone(r2)
            self.assertIn("华为", r2["meaning"])

            r3 = await default_meme_searcher.search_meme_or_phrase("老六")
            self.assertIsNotNone(r3)
            self.assertIn("偷袭", r3["meaning"])

        asyncio.run(run_lookup())

    def test_turn_count_persisted_with_group_memory(self):
        """测试群聊交互轮数计数器从 GroupMemory 的 total_dialog_turns 继承，进程重启不归零"""
        import asyncio
        from onebot_client import OneBotClient
        from antigravity_client import AntigravityClient

        ai_mock = mock_agent()
        ai_mock.chat = ConsoleAgentMock(return_value='{"should_reply": true, "action": "none", "reply": "收到"}')
        client = OneBotClient(config=AppConfig(), antigravity_client=ai_mock)
        client.self_id = "99000000003"
        client.send_group_message = AsyncMock()

        # 模拟群记忆中已累计有 9 次问答交互
        grp_mem = client.memory_mgr.get_group_memory("grp_persist_turn")
        grp_mem.total_dialog_turns = 9
        client.memory_mgr.save_group_memory(grp_mem)

        msg_data = {
            "post_type": "message",
            "message_type": "group",
            "group_id": "grp_persist_turn",
            "user_id": 999,
            "message_id": "msg_turn_10",
            "raw_message": "[CQ:at,qq=99000000003] 轮次测试"
        }

        async def run_msg():
            client.app_config.bot.chat_buffer_seconds = 0  # This test exercises the legacy independent image lane.
            await client._handle_group_message(msg_data)

        asyncio.run(run_msg())
        # 本轮应当为第 10 轮交互，包含周期提醒预设
        self.assertEqual(client._interaction_turn_counts["grp_persist_turn"], 10)


class TestMultimodalVisionEnhancements(unittest.TestCase):
    """测试原生多模态视觉直传、图像类型判定与视觉指引增强（无需本地 OCR）"""

    def test_classify_image_content(self):
        from image_handler import classify_image_content
        # 1. 纯表情包
        self.assertEqual(classify_image_content(is_sticker=True, width=128, height=128), "表情包")

        # 2. 高清横屏游戏/音游结算截图 (如 1920x1080)
        self.assertEqual(classify_image_content(is_sticker=False, width=1920, height=1080), "高清横屏游戏/桌面截图")

        # 3. 手机端竖屏游戏/长截图 (如 1080x2340)
        self.assertEqual(classify_image_content(is_sticker=False, width=1080, height=2340), "手机端长图/应用截图")

        # 4. 普通尺寸横屏截图
        self.assertEqual(classify_image_content(is_sticker=False, width=800, height=450), "游戏/应用截图")

    def test_evaluate_image_semantics_multimodal(self):
        from image_handler import _evaluate_image_semantics
        # 1. 有效尺寸的游戏结算截图直接判定为理解含义，不被视为意义不明
        understood, ambiguous, meaning, reason = _evaluate_image_semantics(
            summary="游戏/音游结算成绩截图 (1920x1080 JPEG)",
            file_path="game.jpg",
            width=1920,
            height=1080,
            is_sticker=False,
            is_animated=False,
            image_category="游戏/音游结算成绩截图"
        )
        self.assertTrue(understood)
        self.assertFalse(ambiguous)
        self.assertIn("游戏/音游结算成绩截图", meaning)

        # 2. 占位小图判定为意义不明
        u_tiny, a_tiny, _, r_tiny = _evaluate_image_semantics(
            summary="占位图",
            file_path="dot.png",
            width=16,
            height=16,
            is_sticker=False,
            is_animated=False
        )
        self.assertFalse(u_tiny)
        self.assertTrue(a_tiny)

        # 3. 包含特征词的表情包判定为理解含义
        stk_u, stk_a, stk_m, _ = _evaluate_image_semantics(
            summary="点赞猫猫表情包 (128x128 PNG)",
            file_path="sticker.png",
            width=128,
            height=128,
            is_sticker=True,
            is_animated=False,
            image_category="表情包"
        )
        self.assertTrue(stk_u)
        self.assertFalse(stk_a)
        self.assertIn("点赞", stk_m)

    def test_analyze_image_item_handles_file_url_multimodal(self):
        import asyncio
        from PIL import Image, ImageDraw
        from image_handler import analyze_image_item

        async def run_test():
            tmp_dir = tempfile.mkdtemp()
            try:
                img_path = os.path.join(tmp_dir, "score_test.png")
                img = Image.new("RGB", (300, 100), color=(255, 255, 255))
                draw = ImageDraw.Draw(img)
                draw.text((10, 10), "SCORE 492", fill=(0, 0, 0))
                img.save(img_path, "PNG")

                file_url = f"file:///{img_path.replace(os.sep, '/')}"
                res = await analyze_image_item({"file": file_url}, cache_dir=tmp_dir)
                self.assertIsNotNone(res["local_path"])
                self.assertTrue(res["local_path"].endswith("score_test.png"))
                self.assertEqual(res["width"], 300)
                self.assertEqual(res["height"], 100)
                self.assertTrue(res["meaning_understood"])
                self.assertFalse(res["is_ambiguous"])
                self.assertIn("file:///", res["file_url"])
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

        asyncio.run(run_test())

    def test_process_multimodal_message_includes_multimodal_section(self):
        import asyncio
        from unittest.mock import patch
        from image_handler import process_multimodal_message

        mock_analysis = {
            "local_path": "G:/qqbot/data/cache/images/test.jpg",
            "file_url": "file:///G:/qqbot/data/cache/images/test.jpg",
            "url": "",
            "width": 1920,
            "height": 1080,
            "format": "JPEG",
            "is_sticker": False,
            "summary": "游戏/音游结算成绩截图 (1920x1080 JPEG)",
            "meaning_understood": True,
            "is_ambiguous": False,
            "semantic_meaning": "包含游戏/音游结算截图画面数据",
            "ambiguity_reason": "",
            "image_category": "游戏/音游结算成绩截图"
        }

        async def run_test():
            for verified in (False, True):
                analysis = {**mock_analysis, "vision_verified": verified,
                    "visual_description": "音游结算显示 492pp 和 S 评级" if verified else "",
                    "meaning_understood": verified, "is_ambiguous": not verified,
                    "semantic_meaning": "492pp 和 S 评级" if verified else ""}
                with self.subTest(verified=verified), patch("image_handler.analyze_image_item", AsyncMock(return_value=analysis)):
                    enriched, analyses = await process_multimodal_message("分析这个成绩[CQ:image,file=test.jpg]")
                    self.assertEqual(len(analyses), 1)
                    self.assertIn("分析这个成绩", enriched)
                    self.assertNotIn("file://", enriched)
                    self.assertNotIn(mock_analysis["local_path"], enriched)
                    self.assertNotIn("请实际调用原生图像查看工具", enriched)
                    self.assertNotIn("【QQ 来图处理】", enriched)
                    if verified:
                        self.assertIn("音游结算显示 492pp 和 S 评级", enriched)
                        self.assertIn("【多模态视觉指引】:", enriched)
                    else:
                        self.assertIn("尚无经过确认的画面描述", enriched)
                        self.assertNotIn("492pp", enriched)

        asyncio.run(run_test())


class TestQuoteAndReplyImageTrace(unittest.TestCase):
    """测试用户引用历史图片消息时的溯源恢复、原生视觉直传与模型回复"""

    def test_onebot_quote_image_message_from_cache(self):
        """引用已识别的历史图片时，主聊天复用分支描述并隐藏原图路径。"""
        import asyncio
        from onebot_client import OneBotClient
        from antigravity_client import AntigravityClient

        ai_mock = mock_agent()
        ai_mock.chat = ConsoleAgentMock(return_value='{"should_reply": true, "action": "quote", "reply": "厉害呀，492pp 加上 S 评级！"}')
        config = AppConfig()
        config.bot.qq_window_capture = False  # Legacy original-file cache behavior is explicit.
        client = OneBotClient(config=config, antigravity_client=ai_mock)
        client.self_id = "99000000003"
        client.send_group_message = AsyncMock()

        # 模拟步骤 1: 用户在群里发送了一张游戏结算图片 (消息ID: 1508246219)
        mock_img_analysis = {
            "local_path": "G:/qqbot/data/cache/images/score.jpg",
            "file_url": "file:///G:/qqbot/data/cache/images/score.jpg",
            "url": "https://example.com/score.jpg",
            "width": 1920,
            "height": 1080,
            "format": "JPEG",
            "is_sticker": False,
            "summary": "游戏/音游结算成绩截图 (1920x1080 JPEG)",
            "vision_verified": True,
            "visual_description": "音游结算成绩显示 492pp 与 S 评级",
            "meaning_understood": True,
            "is_ambiguous": False,
            "semantic_meaning": "包含游戏结算成绩",
            "ambiguity_reason": "",
            "image_category": "游戏/音游结算成绩截图"
        }

        # 提前存入本地消息缓存，模拟已接收该图片消息
        client._recent_messages_cache["1508246219"] = {
            "message_id": "1508246219",
            "group_id": "1129846525",
            "user_id": "99000000002",
            "nickname": "雪村奈々子",
            "raw_message": "[CQ:image,file=score.jpg]",
            "clean_text": "[用户发送了图像: 游戏/音游结算成绩截图 | 原图: [score.jpg](file:///G:/qqbot/data/cache/images/score.jpg)]",
            "image_analyses": [mock_img_analysis],
            "time": time.time()
        }

        # 模拟步骤 2: 用户引用该图片消息并发送：@ヨンミン 分析一下这个 (消息ID: 312849562)
        quote_msg = {
            "post_type": "message",
            "message_type": "group",
            "group_id": "1129846525",
            "user_id": "99000000002",
            "message_id": "312849562",
            "raw_message": "[CQ:reply,id=1508246219][CQ:at,qq=99000000003,name=ヨンミン] 分析一下这个",
            "message": [
                {"type": "reply", "data": {"id": "1508246219"}},
                {"type": "at", "data": {"qq": "99000000003"}},
                {"type": "text", "data": {"text": " 分析一下这个"}}
            ]
        }

        async def run_test():
            try:
                client.app_config.bot.chat_buffer_seconds = 0  # This test exercises the legacy independent image lane.
                await client._handle_group_message(quote_msg)
                await client._get_runtime().work_worker
            finally:
                await client._close_runtime()

        asyncio.run(run_test())

        # 验证 AI chat 必须被调用
        ai_mock.chat.assert_called_once()
        call_prompt = ai_mock.chat.call_args[0][1]

        # 主聊天只消费已确认的识别结果，不重复打开或下载历史图片。
        self.assertIn("引用回复 @雪村奈々子", call_prompt)
        self.assertIn("音游结算成绩显示 492pp 与 S 评级", call_prompt)
        self.assertIn("【引用图片识别结果】:", call_prompt)
        self.assertNotIn("file:///G:/qqbot/data/cache/images/score.jpg", call_prompt)
        self.assertNotIn("【引用多模态视觉指引】:", call_prompt)
        self.assertNotIn("请实际调用原生图像查看工具", call_prompt)
        self.assertIn("分析一下这个", call_prompt)

        # 验证机器人发送了回复
        client.send_group_message.assert_called_once()
        reply_content = client.send_group_message.call_args[0][1]
        self.assertIn("492pp", reply_content)

    def test_onebot_quote_image_dynamic_get_msg_fallback(self):
        """动态拉取被引用图片后，主聊天使用已验证的分支识别描述。"""
        import asyncio
        from unittest.mock import patch
        from onebot_client import OneBotClient
        from antigravity_client import AntigravityClient

        ai_mock = mock_agent()
        ai_mock.chat = ConsoleAgentMock(return_value='{"should_reply": true, "action": "none", "reply": "分析结果如下"}')
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            config = AppConfig()
            config.bot.runtime_database_path = str(Path(td) / "runtime.sqlite3")
            client = OneBotClient(config=config, antigravity_client=ai_mock)
            client.self_id = "99000000003"
            client.send_group_message = AsyncMock()

            # 模拟 get_msg 返回
            async def fake_send_action(action, params, **kwargs):
                if action == "get_msg" and params.get("message_id") == 8888:
                    return {
                        "status": "ok",
                        "retcode": 0,
                        "data": {
                            "message_id": 8888,
                            "group_id": "1129846525",
                            "message_type": "group",
                            "user_id": 7777,
                            "sender": {"nickname": "游戏大神"},
                            "raw_message": "这是我的绝版成绩[CQ:image,file=record.png]"
                        }
                    }
                return {"status": "ok", "retcode": 0}

            client.send_action = AsyncMock(side_effect=fake_send_action)

            mock_record_analysis = {
                "local_path": "G:/qqbot/data/cache/images/record.png",
                "file_url": "file:///G:/qqbot/data/cache/images/record.png",
                "url": "",
                "width": 1920,
                "height": 1080,
                "format": "PNG",
                "is_sticker": False,
                "summary": "游戏/音游结算成绩截图 (1920x1080 PNG)",
                "vision_verified": True,
                "visual_description": "游戏结算页显示绝版成绩和 S 评级",
                "meaning_understood": True,
                "is_ambiguous": False,
                "semantic_meaning": "包含游戏结算成绩",
                "ambiguity_reason": "",
                "image_category": "游戏/音游结算成绩截图"
            }

            quote_msg = {
                "post_type": "message",
                "message_type": "group",
                "group_id": "1129846525",
                "user_id": "99000000002",
                "message_id": "99999",
                "raw_message": "[CQ:reply,id=8888][CQ:at,qq=99000000003] 帮我看看这个",
                "message": [
                    {"type": "reply", "data": {"id": "8888"}},
                    {"type": "at", "data": {"qq": "99000000003"}},
                    {"type": "text", "data": {"text": " 帮我看看这个"}}
                ]
            }

            async def processed_message(*args, **kwargs):
                if "record.png" in kwargs.get("content", ""):
                    return ("这是我的绝版成绩[用户发送了图像: 游戏结算页显示绝版成绩和 S 评级]", [mock_record_analysis])
                return ("[CQ:at,qq=99000000003] 帮我看看这个", [])

            with patch("onebot_client.process_multimodal_message", new=AsyncMock(side_effect=processed_message)) as mock_pmm:

                async def run_test():
                    try:
                        client.app_config.bot.chat_buffer_seconds = 0  # This test exercises the legacy independent image lane.
                        await client._handle_group_message(quote_msg)
                        if client._event_tasks:
                            await asyncio.gather(*list(client._event_tasks))
                        await client._get_runtime().work_worker
                    finally:
                        await client._close_runtime()

                asyncio.run(run_test())

            ai_mock.chat.assert_called_once()
            self.assertIn("get_msg", [call.args[0] for call in client.send_action.await_args_list])
            call_prompt = ai_mock.chat.call_args[0][1]
            self.assertIn("引用回复 @游戏大神", call_prompt)
            self.assertIn("这是我的绝版成绩", call_prompt)
            self.assertIn("游戏结算页显示绝版成绩和 S 评级", call_prompt)
            self.assertIn("【引用图片识别结果】:", call_prompt)
            self.assertNotIn("file:///G:/qqbot/data/cache/images/record.png", call_prompt)
            self.assertNotIn("【引用多模态视觉指引】:", call_prompt)
            self.assertNotIn("请实际调用原生图像查看工具", call_prompt)

    def test_boost_command_status(self):
        """测试 /boost 或 /增强 指令返回多模态视觉引擎运行状态"""
        import asyncio
        from onebot_client import OneBotClient
        from antigravity_client import AntigravityClient

        ai_mock = mock_agent()
        ai_mock.chat = ConsoleAgentMock(return_value="内部命令审核")
        client = OneBotClient(config=AppConfig(), antigravity_client=ai_mock)
        client.send_group_message = AsyncMock()
        client._is_ws_connected = lambda: True

        async def run_test():
            handled = await client._handle_command("boost", group_id="12345", is_group=True)
            self.assertTrue(handled)
            await drain_runtime(client)
            await client._close_runtime()

        asyncio.run(run_test())
        client.send_group_message.assert_called_once()
        msg = client.send_group_message.call_args[0][1]
        self.assertIn("多模态与原生视觉识别引擎状态", msg)
        self.assertIn("独立识别分支", msg)
        self.assertIn("CLI reply/deliver --finish", msg)

    def test_extract_cq_images_deduplication_and_merge(self):
        """测试 OneBot 消息同时包含 CQ 码与 segments 时，正确去重并合并 URL 属性"""
        from image_handler import extract_cq_images
        raw = "[CQ:image,file=score.jpg]"
        segs = [{"type": "image", "data": {"file": "score.jpg", "url": "https://example.com/score.jpg"}}]
        clean_text, items = extract_cq_images(raw, segments=segs)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["file"], "score.jpg")
        self.assertEqual(items[0]["url"], "https://example.com/score.jpg")

    def test_strip_multimodal_tags_with_nested_links(self):
        """测试 strip_multimodal_tags 能正确剥离包含嵌套原图链接的注入标签，保留纯人类用户对白"""
        from image_handler import strip_multimodal_tags
        raw_enriched = (
            "这是我的绝版成绩[用户发送了图像: 游戏/音游结算成绩截图 | 原图: ![record.png](file:///G:/qqbot/data/cache/images/record.png)]\n"
            "【多模态视觉指引】: 用户发送了上述图像...\n"
            "【多模态感知：已理解表情包/图片含义】"
        )
        cleaned = strip_multimodal_tags(raw_enriched)
        self.assertEqual(cleaned, "这是我的绝版成绩")

        # 纯图片消息（无用户发言）清洗后应为空
        pure_img_enriched = (
            "[用户发送了图像: 游戏/音游结算成绩截图 | 原图: ![record.png](file:///G:/qqbot/data/cache/images/record.png) 路径: `G:/qqbot`]\n"
            "【多模态视觉指引】: 用户发送了上述图像..."
        )
        self.assertEqual(strip_multimodal_tags(pure_img_enriched), "")

    def test_analyze_image_item_base64_support(self):
        """测试 analyze_image_item 支持 base64:// 内联图片自动解码与格式识别"""
        import asyncio
        import base64
        import io
        from PIL import Image
        from image_handler import analyze_image_item

        tmp_dir = tempfile.mkdtemp()
        try:
            # 构造内存中 100x100 图像的 base64 字符串
            buf = io.BytesIO()
            img = Image.new("RGB", (100, 100), color=(100, 150, 200))
            img.save(buf, format="PNG")
            b64_str = base64.b64encode(buf.getvalue()).decode("ascii")

            async def run_test():
                res = await analyze_image_item({"file": f"base64://{b64_str}"}, cache_dir=tmp_dir)
                self.assertIsNotNone(res["local_path"])
                self.assertTrue(os.path.isfile(res["local_path"]))
                self.assertEqual(res["width"], 100)
                self.assertEqual(res["height"], 100)
                self.assertTrue(res["meaning_understood"])

            asyncio.run(run_test())
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_bot_client_quote_image_message(self):
        """官方渠道引用历史图片时主聊天只接收已验证的分支识别结果。"""
        import asyncio
        from bot_client import GeminiQQBot
        from antigravity_client import AntigravityClient

        ai_mock = mock_agent()
        ai_mock.chat = ConsoleAgentMock(return_value="这是你在音游中的绝版成绩，打得非常棒！")
        async def run_test():
            client = GeminiQQBot(config=AppConfig(), antigravity_client=ai_mock)

            # 模拟历史图片消息在缓存中
            mock_analysis = {
                "local_path": "G:/qqbot/data/cache/images/history_score.png",
                "file_url": "file:///G:/qqbot/data/cache/images/history_score.png",
                "url": "",
                "width": 1920,
                "height": 1080,
                "format": "PNG",
                "is_sticker": False,
                "summary": "游戏/音游结算成绩截图 (1920x1080 PNG)",
                "vision_verified": True,
                "visual_description": "音游结算截图展示了绝版成绩与最高连击数",
                "meaning_understood": True,
                "is_ambiguous": False,
                "semantic_meaning": "包含游戏结算成绩",
                "ambiguity_reason": "",
                "image_category": "游戏/音游结算成绩截图"
            }
            client._recent_messages_cache["msg_hist_1"] = {
                "message_id": "msg_hist_1",
                "group_id": "grp_official_test",
                "user_id": "user_pro",
                "nickname": "音游大神",
                "clean_text": "晒晒我的成绩[用户发送了图像: 游戏/音游结算成绩截图 | 原图: ![history_score.png](file:///G:/qqbot/data/cache/images/history_score.png)]",
                "image_analyses": [mock_analysis],
                "time": time.time()
            }

            # 构造引用该消息的当前消息
            mock_msg = MagicMock()
            mock_msg.id = "msg_cur_2"
            mock_msg.group_openid = "grp_official_test"
            mock_msg.author.user_openid = "user_asker"
            mock_msg.author.member_openid = "user_asker"
            mock_msg.author.username = "提问群友"
            mock_msg.author.bot = False
            mock_msg.content = "<@!robot_123> 帮我看看这个打得怎么样"
            mock_msg.attachments = []
            mock_ref = MagicMock()
            mock_ref.message_id = "msg_hist_1"
            mock_msg.message_reference = mock_ref
            mock_msg.reply = AsyncMock()

            await client._handle_group_message(mock_msg, is_at_event=True)

        asyncio.run(run_test())

        ai_mock.chat.assert_called_once()
        call_prompt = ai_mock.chat.call_args[0][1]
        self.assertIn("引用回复 @音游大神", call_prompt)
        self.assertIn("晒晒我的成绩", call_prompt)
        self.assertIn("音游结算截图展示了绝版成绩与最高连击数", call_prompt)
        self.assertIn("【引用图片识别结果】:", call_prompt)
        self.assertNotIn("file:///G:/qqbot/data/cache/images/history_score.png", call_prompt)
        self.assertNotIn("【引用多模态视觉指引】:", call_prompt)
        self.assertNotIn("请实际调用原生图像查看工具", call_prompt)


if __name__ == "__main__":
    unittest.main()




