# -*- coding: utf-8 -*-
"""
Tests for Group Memory Summarization, History Preloading,
Command Deliberation Gating, and Per-Group Master/Persona Isolation.
"""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

from test_ai_fixture import mock_agent

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from antigravity_client import AntigravityClient
from config import AppConfig, GroupCustomConfig
from group_memory import GroupMemoryManager, SessionMessage
from command_deliberation import CommandDeliberator, default_command_deliberator
from persona_manager import PersonaManager
from prompt_builder import compose_system_prompt


class TestGroupMemoryAndIsolation(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mem_dir = os.path.join(self.test_dir, "memories")
        os.makedirs(self.mem_dir, exist_ok=True)
        self.memory_mgr = GroupMemoryManager(storage_dir=self.mem_dir, max_entries=5)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_record_session_message_and_summarize_on_shutdown(self):
        """测试每次打开到关闭期间的全部群消息被记录，并在关闭时由大模型生成人物刻写与总结概括，保存为带日期时间后缀的 JSON 文件"""
        group_id = "test_group_1001"
        self.memory_mgr.record_session_message(group_id, "u_001", "小华", "大家好，我是小华，擅长前端开发", is_bot=False)
        self.memory_mgr.record_session_message(group_id, "bot_0", "吹雪", "你好呀小华！", is_bot=True)
        self.memory_mgr.record_session_message(group_id, "u_002", "老李", "小华你好，我是老李，负责后端架构", is_bot=False)
        self.memory_mgr.record_session_message(group_id, "u_001", "小华", "今天我们讨论一下微服务接口标准", is_bot=False)

        mock_ai = mock_agent()
        mock_summary_response = {
            "summary": "小华与老李讨论了前端开发与微服务架构设计标准。",
            "atmosphere": "专业务实的技术研讨氛围",
            "member_portraits": {
                "小华": "前端开发者，关注接口规范与组件设计",
                "老李": "后端架构师，注重高可用性与系统架构"
            },
            "key_topics": ["前端开发", "微服务接口标准"],
            "notable_events": ["小华与老李完成架构对齐"]
        }
        mock_ai.chat = AsyncMock(return_value=f"```json\n{json.dumps(mock_summary_response, ensure_ascii=False)}\n```")

        async def run_summary():
            return await self.memory_mgr.summarize_and_save_session(mock_ai)

        saved_files = asyncio.run(run_summary())
        self.assertIn(group_id, saved_files)
        saved_file = saved_files[group_id]
        self.assertTrue(os.path.exists(saved_file))
        self.assertTrue(os.path.basename(saved_file).startswith("group_test_group_1001_"))
        self.assertTrue(saved_file.endswith(".json"))

        with open(saved_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["group_openid"], group_id)
        self.assertEqual(data["message_count"], 4)
        self.assertEqual(data["summary"], mock_summary_response["summary"])
        self.assertIn("小华", data["member_portraits"])
        self.assertIn("老李", data["member_portraits"])

    def test_load_recent_n_memories_on_startup(self):
        """测试启动时按时间倒序读取前 N 次群记忆并注入 Prompt"""
        group_id = "group_startup_test"
        # 模拟生成 5 个不同时间戳的会话记忆文件
        for i in range(1, 6):
            ts_str = f"20261028_12000{i}"
            file_name = f"group_{group_id}_{ts_str}.json"
            file_path = os.path.join(self.mem_dir, file_name)
            payload = {
                "group_openid": group_id,
                "session_start": f"2026-10-28 12:00:0{i}",
                "session_end": f"2026-10-28 12:10:0{i}",
                "created_at": 1000 + i * 100,
                "message_count": 10,
                "summary": f"第{i}次会话讨论了第{i}阶段技术路线",
                "atmosphere": f"第{i}期研讨氛围",
                "member_portraits": {f"成员_{i}": f"活跃成员{i}"},
                "key_topics": [f"主题_{i}"]
            }
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)

        # 读取前 3 次记忆
        memories = self.memory_mgr.load_recent_memories(group_id, count=3)
        self.assertEqual(len(memories), 3)
        summaries = [m["summary"] for m in memories]
        self.assertIn("第5次会话讨论了第5阶段技术路线", summaries)
        self.assertIn("第4次会话讨论了第4阶段技术路线", summaries)
        self.assertIn("第3次会话讨论了第3阶段技术路线", summaries)
        self.assertNotIn("第1次会话讨论了第1阶段技术路线", summaries)

        # 测试 Prompt 格式化
        prompt = self.memory_mgr.get_memory_prompt(group_id, history_count=3)
        self.assertIn("【本群过往专属历史记忆", prompt)
        self.assertIn("第5阶段技术路线", prompt)
        self.assertIn("成员_5", prompt)

    def test_command_deliberation_only_at_bot_triggers_commands(self):
        """测试群聊中只有 @bot 才会触发命令，如果不是 @bot 则绝不作为对 bot 的命令"""
        deliberator = CommandDeliberator()

        # 1. 在群聊中，若未 @bot (is_at_bot=False)，即使包含明确指令关键词也判定为普通闲聊
        cmd_text = "/clear"
        res_no_at = deliberator.deliberate(cmd_text, is_group=True, is_at_bot=False)
        self.assertFalse(res_no_at.is_command)
        self.assertEqual(res_no_at.command_type, "chat")

        res_natural_no_at = deliberator.deliberate("清空本群专属长期记忆", is_group=True, is_at_bot=False)
        self.assertFalse(res_natural_no_at.is_command)
        self.assertEqual(res_natural_no_at.command_type, "chat")

        # 2. 在群聊中，若 @bot (is_at_bot=True)，则正常识别执行命令
        res_at = deliberator.deliberate(cmd_text, is_group=True, is_at_bot=True)
        self.assertTrue(res_at.is_command)
        self.assertEqual(res_at.command_type, "clear")

        res_natural_at = deliberator.deliberate("清空本群专属长期记忆", is_group=True, is_at_bot=True)
        self.assertTrue(res_natural_at.is_command)
        self.assertEqual(res_natural_at.command_type, "clearmemory")

        # 3. 私聊模式下无需 @bot 即可触发命令
        res_private = deliberator.deliberate("清空上下文", is_group=False)
        self.assertTrue(res_private.is_command)
        self.assertEqual(res_private.command_type, "clear")

    def test_native_antigravity_query_not_hijacked_as_media(self):
        """测试互联网信息查询等自然语言不被算法误拦截为媒体命令，交由 Antigravity 原生处理"""
        deliberator = CommandDeliberator()
        queries = [
            "帮我把互联网上的雪村奈々子的信息找出来",
            "在网上查一查今天有什么科技新闻",
            "搜索一下雪村奈々子的声优是谁",
            "请问量子计算的基本原理是什么"
        ]
        for q in queries:
            res = deliberator.deliberate(q, is_group=True, is_at_bot=True)
            self.assertFalse(res.is_command, f"查询被误拦截为命令: {q}")
            self.assertEqual(res.command_type, "chat")

    def test_group_custom_config_and_master_isolation(self):
        """测试群聊隔离：不同群可独立配置主人称谓、主人ID和独立人设预设"""
        cfg = AppConfig()
        cfg.bot.master_id = "global_master_000"

        # 群 1: 主人称谓为“司令”，主人ID为“admiral_101”，独立人设为“傲娇猫娘女仆”
        cfg.bot.group_settings["grp_1"] = GroupCustomConfig(
            group_id="grp_1",
            group_name="舰队一号群",
            master_name="司令",
            master_id="admiral_101",
            persona="傲娇猫娘女仆"
        )

        # 群 2: 主人称谓为“首席架构师”，主人昵称为“张总”，独立人设为“全栈系统架构师”
        cfg.bot.group_settings["grp_2"] = GroupCustomConfig(
            group_id="grp_2",
            group_name="研发二部",
            master_name="首席架构师",
            master_id="",
            persona="全栈系统架构师"
        )

        # 验证主人判定 (仅稳定 ID 鉴权，昵称不可鉴权)
        self.assertTrue(cfg.bot.is_master("grp_1", "admiral_101", "任何昵称"))
        self.assertFalse(cfg.bot.is_master("grp_1", "other_user", "司令"))
        self.assertFalse(cfg.bot.is_master("grp_2", "unknown_user", "首席架构师"))
        self.assertTrue(cfg.bot.is_master("grp_2", "global_master_000", "首席架构师"))
        self.assertTrue(cfg.bot.is_master("grp_default", "global_master_000", "普通用户"))

        # 验证各群的主人称谓与独立人设获取
        self.assertEqual(cfg.bot.get_master_name("grp_1"), "司令")
        self.assertEqual(cfg.bot.get_persona_name("grp_1"), "傲娇猫娘女仆")

        self.assertEqual(cfg.bot.get_master_name("grp_2"), "首席架构师")
        self.assertEqual(cfg.bot.get_persona_name("grp_2"), "全栈系统架构师")

        self.assertEqual(cfg.bot.get_master_name("grp_default"), "")
        self.assertEqual(cfg.bot.get_persona_name("grp_default"), "")

        # 验证 System Prompt 中独立主人称谓与独立人设注入
        prompt_grp1 = compose_system_prompt(
            base_prompt="你现在扮演傲娇猫娘。",
            master_name=cfg.bot.get_master_name("grp_1"),
            bot_names=["猫娘"]
        )
        self.assertIn("司令", prompt_grp1)
        self.assertIn("猫娘", prompt_grp1)

        prompt_grp2 = compose_system_prompt(
            base_prompt="你现在扮演全栈架构师。",
            master_name=cfg.bot.get_master_name("grp_2"),
            bot_names=["架构师"]
        )
        self.assertIn("首席架构师", prompt_grp2)
        self.assertIn("架构师", prompt_grp2)
        self.assertNotIn("司令", prompt_grp2)

    def test_gui_group_settings_and_memory_history_count(self):
        """测试 GUI 面板上能够正确读取、展示、新增/编辑、保存及删除群隔离独立配置与记忆历史数"""
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication
        from gui import BotStudioGUI

        tmp_cfg_path = os.path.join(self.test_dir, "config.yaml")
        shutil.copy("config.yaml", tmp_cfg_path)

        app = QApplication.instance() or QApplication([])
        win = BotStudioGUI(tmp_cfg_path)

        # 1. 验证开机记忆历史数读取
        self.assertEqual(win.spin_memory_history_count.value(), win.app_config.bot.memory_history_count)
        win.spin_memory_history_count.setValue(5)

        # 2. 模拟在 UI 中添加群专属隔离设置
        win.edit_grp_id.setText("998877")
        win.edit_grp_name.setText("专属测试群")
        win.edit_grp_master_name.setText("大队长")
        win.edit_grp_master_id.setText("leader_001")
        win._on_add_or_update_group_setting()

        # 验证表格与数据模型已添加
        self.assertIn("998877", win.app_config.bot.group_settings)
        self.assertEqual(win.app_config.bot.group_settings["998877"].master_name, "大队长")
        self.assertEqual(win.table_group_settings.rowCount(), len(win.app_config.bot.group_settings))

        # 3. 模拟保存配置
        win._on_save_config(show_msg=False)
        reloaded = AppConfig.load(tmp_cfg_path)
        self.assertEqual(reloaded.bot.memory_history_count, 5)
        self.assertIn("998877", reloaded.bot.group_settings)
        self.assertEqual(reloaded.bot.group_settings["998877"].master_name, "大队长")

        # 4. 模拟删除群配置
        win._on_delete_group_setting("998877")
        self.assertNotIn("998877", win.app_config.bot.group_settings)

        win.close()

    def test_preload_recent_memories_caches_correctly_with_underscores(self):
        """测试预加载群记忆正确缓存进内存，并支持包含下划线的群号"""
        group_id = "grp_team_alpha_01"
        for i in range(1, 4):
            ts_str = f"20261028_10000{i}"
            fname = f"group_{group_id}_{ts_str}.json"
            fpath = os.path.join(self.mem_dir, fname)
            payload = {
                "group_openid": group_id,
                "session_start": f"2026-10-28 10:00:0{i}",
                "session_end": f"2026-10-28 10:10:0{i}",
                "created_at": 2000 + i * 10,
                "message_count": 5,
                "summary": f"Alpha团队第{i}阶段讨论",
                "atmosphere": "严谨研发氛围",
                "member_portraits": {f"成员A{i}": "核心技术干将"},
                "key_topics": [f"Alpha主题{i}"]
            }
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)

        mgr = GroupMemoryManager(storage_dir=self.mem_dir)
        mgr.preload_recent_memories(history_count=2)
        # 验证已加载进内存缓存中，且群ID未被下划线错误截断
        self.assertIn(group_id, mgr._recent_memories_cache)
        cached = mgr._recent_memories_cache[group_id]
        self.assertEqual(len(cached), 2)
        self.assertEqual(cached[0]["summary"], "Alpha团队第3阶段讨论")
        self.assertEqual(cached[1]["summary"], "Alpha团队第2阶段讨论")

    def test_antigravity_group_sessions_persistence(self):
        """测试群聊与私聊在空闲超时或重启加载时不会被重置"""
        sessions_file = os.path.join(self.test_dir, "test_sessions.json")
        client = AntigravityClient(sessions_file=sessions_file)

        # 模拟群聊会话与私聊会话
        grp_sess = client.get_or_create_session("onebot_group_8888")
        grp_sess.conversation_id = "conv_group_keep_123"
        grp_sess.last_active = time.time() - 3600  # 1小时前 (超过40分钟超时)

        c2c_sess = client.get_or_create_session("c2c_user_9999")
        c2c_sess.conversation_id = "conv_c2c_keep_456"
        c2c_sess.last_active = time.time() - 3600  # 1小时前

        client._save_sessions()

        # 重新创建 client 模拟重启加载
        client2 = AntigravityClient(sessions_file=sessions_file)
        # 群聊会话应当保留 conversation_id
        loaded_grp = client2.get_or_create_session("onebot_group_8888")
        self.assertEqual(loaded_grp.conversation_id, "conv_group_keep_123")

        # 私聊会话同样跨空闲与重启保留原 ID
        loaded_c2c = client2.get_or_create_session("c2c_user_9999")
        self.assertEqual(loaded_c2c.conversation_id, "conv_c2c_keep_456")

    def test_natural_language_search_not_hijacked_as_media(self):
        """测试自然语言搜索与信息检索不被算法粗暴拦截为媒体下载"""
        deliberator = CommandDeliberator()
        test_phrases = [
            "在网上查一下橘猫的品种与生活习性",
            "帮我把互联网上的雪村奈々子的信息找出来",
            "在网上帮我找一下Python异步编程教程",
            "百度搜一下最新的显卡驱动"
        ]
        for phrase in test_phrases:
            res = deliberator.deliberate(phrase, is_group=True, is_at_bot=True)
            self.assertFalse(res.is_command, f"自然语言语句被误判定为命令: {phrase}")
            self.assertEqual(res.command_type, "chat")

    def test_group_slash_command_without_at_bot_is_not_command(self):
        """测试群聊中未@bot时，无论是自然语言还是斜杠指令均不作为对bot的命令"""
        deliberator = CommandDeliberator()
        res_slash = deliberator.deliberate("/image 橘猫", is_group=True, is_at_bot=False)
        self.assertFalse(res_slash.is_command)
        self.assertEqual(res_slash.command_type, "chat")

        res_none_at = deliberator.deliberate("/clear", is_group=True, is_at_bot=False)
        self.assertFalse(res_none_at.is_command)
        self.assertEqual(res_none_at.command_type, "chat")

    def test_runtime_control_stop_triggers_graceful_stop(self):
        """测试 runtime_control.request_stop() 触发 client.graceful_stop()"""
        from runtime_control import RuntimeControl, request_stop, CONTROL_PATH
        rc = RuntimeControl(namespace="test_stop")
        mock_client = MagicMock()
        mock_client.graceful_stop = AsyncMock()

        req_id = request_stop()
        self.assertTrue(bool(req_id))

        try:
            rc.poll(mock_client)
            self.assertEqual(rc.seen, req_id)
            mock_client.graceful_stop.assert_called_once()
        finally:
            if rc.ack_path.exists():
                rc.ack_path.unlink(missing_ok=True)
            if CONTROL_PATH.exists():
                CONTROL_PATH.unlink(missing_ok=True)

    def test_runtime_control_ignores_stale_stop_action(self):
        """测试早于实例启动的过期 stop 控制指令会被忽略，不会触发 client.graceful_stop()"""
        from runtime_control import RuntimeControl, request_stop, CONTROL_PATH
        mock_client = MagicMock()
        mock_client.graceful_stop = AsyncMock()

        req_id = request_stop()
        self.assertTrue(bool(req_id))

        # 模拟实例在 stop 指令发出之后才启动
        time.sleep(0.02)
        rc = RuntimeControl(namespace="test_stale_stop")
        try:
            rc.poll(mock_client)
            mock_client.graceful_stop.assert_not_called()
        finally:
            if rc.ack_path.exists():
                rc.ack_path.unlink(missing_ok=True)
            if CONTROL_PATH.exists():
                CONTROL_PATH.unlink(missing_ok=True)

    def test_onebot_graceful_stop_does_not_stop_loop(self):
        """测试 OneBotClient.graceful_stop() 具备幂等性且不会调用 loop.stop() 导致事件循环异常中断"""
        from onebot_client import OneBotClient
        ai_mock = mock_agent()
        client = OneBotClient(config=AppConfig(), antigravity_client=ai_mock)

        loop = asyncio.new_event_loop()
        try:
            async def _run_test():
                await client.graceful_stop()
                self.assertTrue(client._graceful_stopped)
                # 再次调用验证幂等性
                await client.graceful_stop()

            loop.run_until_complete(_run_test())
            # 确认事件循环没有被直接 loop.stop() 破坏
            self.assertFalse(loop.is_closed())
        finally:
            loop.close()

    def test_onebot_start_clean_exit_on_runtime_stop(self):
        """测试 OneBotClient.start() 运行中收到 request_stop() 能够平滑终止且不抛出 Event loop stopped 异常"""
        from onebot_client import OneBotClient
        from runtime_control import request_stop, CONTROL_PATH

        cfg = AppConfig()
        cfg.onebot.reconnect_interval = 1
        cfg.onebot.ws_url = "ws://127.0.0.1:59998"
        client = OneBotClient(config=cfg, antigravity_client=MagicMock())
        connector = patch("onebot_client.websockets.connect", side_effect=ConnectionRefusedError("offline fixture"))
        connector.start()
        self.addCleanup(connector.stop)

        loop = asyncio.new_event_loop()
        try:
            async def stopper():
                await asyncio.sleep(0.05)
                request_stop()

            async def main():
                asyncio.create_task(stopper())
                await client.start()

            # 运行直到完成，不得抛出 RuntimeError('Event loop stopped before Future completed.')
            loop.run_until_complete(main())
            self.assertFalse(client._is_running)
        finally:
            loop.run_until_complete(client.graceful_stop())
            loop.close()
            CONTROL_PATH.unlink(missing_ok=True)
            if hasattr(client, "_control") and client._control.ack_path.exists():
                client._control.ack_path.unlink(missing_ok=True)

    def test_onebot_start_ignores_stale_stop(self):
        """测试启动前残留的 stop 信号不会导致刚启动的 OneBotClient 立即异常终止"""
        from onebot_client import OneBotClient
        from runtime_control import request_stop, CONTROL_PATH

        request_stop()
        time.sleep(0.02)

        cfg = AppConfig()
        cfg.onebot.reconnect_interval = 1
        cfg.onebot.ws_url = "ws://127.0.0.1:59997"
        client = OneBotClient(config=cfg, antigravity_client=MagicMock())
        connector = patch("onebot_client.websockets.connect", side_effect=ConnectionRefusedError("offline fixture"))
        connector.start()
        self.addCleanup(connector.stop)

        loop = asyncio.new_event_loop()
        try:
            async def stopper_later():
                await asyncio.sleep(0.1)
                self.assertTrue(client._is_running)
                client.stop()

            async def main():
                asyncio.create_task(stopper_later())
                await client.start()

            loop.run_until_complete(main())
        finally:
            loop.run_until_complete(client.graceful_stop())
            loop.close()
            CONTROL_PATH.unlink(missing_ok=True)
            if hasattr(client, "_control") and client._control.ack_path.exists():
                client._control.ack_path.unlink(missing_ok=True)


    def test_runtime_control_ignores_stop_with_zero_or_missing_time(self):
        """测试 time 为 0 或缺失时间戳的旧格式 stop 控制指令会被视为过期指令忽略"""
        from runtime_control import RuntimeControl, CONTROL_PATH
        CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTROL_PATH.write_text(json.dumps({"id": "old_stop_001", "action": "stop", "time": 0}), encoding="utf-8")

        mock_client = MagicMock()
        mock_client.graceful_stop = AsyncMock()

        rc = RuntimeControl(namespace="test_zero_time_stop")
        try:
            rc.poll(mock_client)
            mock_client.graceful_stop.assert_not_called()
        finally:
            if rc.ack_path.exists():
                rc.ack_path.unlink(missing_ok=True)
            if CONTROL_PATH.exists():
                CONTROL_PATH.unlink(missing_ok=True)

    def test_onebot_runtime_stop_summarizes_exactly_once(self):
        """测试 OneBotClient 运行中收到 request_stop() 期间，群记忆总结归档仅执行一次，杜绝并发双重归档"""
        from onebot_client import OneBotClient
        from runtime_control import request_stop, CONTROL_PATH

        cfg = AppConfig()
        cfg.onebot.reconnect_interval = 1
        cfg.onebot.ws_url = "ws://127.0.0.1:59996"
        client = OneBotClient(config=cfg, antigravity_client=MagicMock())
        connector = patch("onebot_client.websockets.connect", side_effect=ConnectionRefusedError("offline fixture"))
        connector.start()
        self.addCleanup(connector.stop)

        summarize_calls = []
        async def mock_summarize(*args, **kwargs):
            summarize_calls.append("start")
            await asyncio.sleep(0.05)
            summarize_calls.append("end")
            return {}
        client.memory_mgr.summarize_and_save_session = mock_summarize

        loop = asyncio.new_event_loop()
        try:
            async def stopper():
                await asyncio.sleep(0.02)
                request_stop()

            async def main():
                asyncio.create_task(stopper())
                await client.start()

            loop.run_until_complete(main())
            self.assertEqual(summarize_calls, ["start", "end"])
        finally:
            loop.run_until_complete(client.graceful_stop())
            loop.close()
            CONTROL_PATH.unlink(missing_ok=True)
            if hasattr(client, "_control") and client._control.ack_path.exists():
                client._control.ack_path.unlink(missing_ok=True)

    def test_summarize_and_save_session_atomic_drain(self):
        """测试 GroupMemoryManager.summarize_and_save_session 原子清空暂存，防重复生成"""
        group_id = "test_atomic_drain"
        self.memory_mgr.record_session_message(group_id, "u_001", "张三", "消息1", is_bot=False)

        mock_ai = mock_agent()
        mock_ai.chat = AsyncMock(return_value='{"summary": "概要", "atmosphere": "好", "member_portraits": {}}')

        loop = asyncio.new_event_loop()
        try:
            async def _run():
                # 第一次调用成功归档
                res1 = await self.memory_mgr.summarize_and_save_session(mock_ai)
                self.assertIn(group_id, res1)
                # 第二次调用因暂存已原子清空，直接返回空字典且不再调用 AI
                res2 = await self.memory_mgr.summarize_and_save_session(mock_ai)
                self.assertEqual(res2, {})
                self.assertEqual(mock_ai.chat.call_count, 1)

            loop.run_until_complete(_run())
        finally:
            loop.close()

    def test_official_bot_no_duplicate_summarization(self):
        """测试 GeminiQQBot.graceful_stop() 具备幂等并正确同步已完成状态"""
        from bot_client import GeminiQQBot

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            bot = GeminiQQBot(config=AppConfig(), antigravity_client=MagicMock())

            calls = []
            async def mock_summarize(*args, **kwargs):
                calls.append(1)
                return {}
            bot.memory_mgr.summarize_and_save_session = mock_summarize

            async def _run():
                await bot.graceful_stop()
                self.assertTrue(bot._graceful_stopped)
                # 再次调用 graceful_stop 不会重复执行归档
                await bot.graceful_stop()
                self.assertEqual(len(calls), 1)

            loop.run_until_complete(_run())
        finally:
            loop.close()
            asyncio.set_event_loop(None)


if __name__ == "__main__":
    unittest.main()


