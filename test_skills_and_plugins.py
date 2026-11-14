# -*- coding: utf-8 -*-
"""
Tests for Extensible Skills and Plugin Architecture.
Verifies BaseSkill, SkillManager, SendImageSkill, SendFileSkill, WebSearchSkill,
and AI decision integration.
"""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)

import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from skills.base import BaseSkill, SkillParameter, SkillContext, SkillResult
from skills.manager import SkillManager, default_skill_manager
from skills.media_skills import SendImageSkill, SendVideoSkill
from skills.file_skills import SendFileSkill
from skills.web_skills import WebSearchSkill
from prompt_builder import (
    compose_system_prompt,
    compose_autonomous_decision_prompt,
    parse_ai_decision_and_reply,
    AIDecisionResult
)


class TestSkillArchitecture(unittest.TestCase):
    """测试技能与插件系统基础架构"""

    def setUp(self):
        self.manager = SkillManager()

    def test_custom_skill_registration_and_schema(self):
        class EchoSkill(BaseSkill):
            name = "echo_skill"
            display_name = "回显技能"
            description = "测试用回显技能"
            parameters = [
                SkillParameter(name="msg", param_type="string", description="消息文本", required=True),
                SkillParameter(name="times", param_type="integer", description="重复次数", required=False, default=1)
            ]

            async def execute(self, params, context):
                msg = params.get("msg", "")
                times = params.get("times", 1)
                return SkillResult(success=True, message=msg * times, action_type="text")

        skill = EchoSkill()
        self.manager.register(skill)

        retrieved = self.manager.get_skill("echo_skill")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.name, "echo_skill")

        schema = retrieved.to_schema()
        self.assertIn("echo_skill", schema["name"])
        self.assertIn("msg", schema["parameters"]["properties"])
        self.assertIn("msg", schema["parameters"]["required"])

        doc = retrieved.to_prompt_doc()
        self.assertIn("echo_skill", doc)
        self.assertIn("msg: string (必填)", doc)

    def test_skill_execution(self):
        async def run_test():
            class MathSkill(BaseSkill):
                name = "calc_add"
                display_name = "加法计算器"
                description = "计算两数相加"
                parameters = [
                    SkillParameter(name="a", param_type="integer", required=True),
                    SkillParameter(name="b", param_type="integer", required=True)
                ]

                async def execute(self, params, context):
                    return SkillResult(success=True, message=str(params["a"] + params["b"]), action_type="text", data=params["a"] + params["b"])

            mgr = SkillManager()
            mgr.register(MathSkill())

            ctx = SkillContext(session_key="test", conversation_id="101", user_id="u1")
            res = await mgr.execute_skill("calc_add", {"a": 3, "b": 7}, ctx)
            self.assertTrue(res.success)
            self.assertEqual(res.data, 10)
            self.assertEqual(res.message, "10")

        asyncio.run(run_test())

    def test_parse_skill_invocation_formats(self):
        mgr = self.manager

        # 格式 1: action: "skill", skill: "send_image", params: {...}
        d1 = {
            "should_reply": True,
            "action": "skill",
            "skill": "send_image",
            "params": {"query": "初雪樱 玉树樱 立绘", "count": 1},
            "reply": "立绘这就发出来啦~"
        }
        res1 = mgr.parse_skill_invocation(d1)
        self.assertIsNotNone(res1)
        self.assertEqual(res1[0], "send_image")
        self.assertEqual(res1[1]["query"], "初雪樱 玉树樱 立绘")

        # 格式 2: 常规回复附带 skill
        d2 = {
            "should_reply": True,
            "action": "quote",
            "reply": "好的，正在搜图",
            "skill": "send_image",
            "params": {"query": "柴犬 壁纸"}
        }
        res2 = mgr.parse_skill_invocation(d2)
        self.assertIsNotNone(res2)
        self.assertEqual(res2[0], "send_image")
        self.assertEqual(res2[1]["query"], "柴犬 壁纸")

        # 格式 3: media 字段声明
        d3 = {
            "should_reply": True,
            "action": "none",
            "reply": "正在准备视频",
            "media": {"kind": "video", "query": "猫咪短视频"}
        }
        res3 = mgr.parse_skill_invocation(d3)
        self.assertIsNotNone(res3)
        self.assertEqual(res3[0], "send_video")
        self.assertEqual(res3[1]["query"], "猫咪短视频")

        # 格式 4: 文本内嵌 [skill:name,query="..."]
        d4 = "这是为你找到的立绘！[skill:send_image,query=\"初雪樱\"]"
        res4 = mgr.parse_skill_invocation(d4)
        self.assertIsNotNone(res4)
        self.assertEqual(res4[0], "send_image")
        self.assertEqual(res4[1]["query"], "初雪樱")

    def test_prompt_injection_includes_skills(self):
        sys_prompt = compose_system_prompt("你是一个测试AI")
        self.assertIn("【可用工具与扩展技能 (Skill Tools)】", sys_prompt)
        self.assertIn("send_image", sys_prompt)
        self.assertIn("send_file", sys_prompt)
        self.assertIn("web_search", sys_prompt)
        self.assertIn("严禁以'无法发送图片'", sys_prompt)

        dec_prompt = compose_autonomous_decision_prompt("帮我找初雪樱立绘")
        self.assertNotIn("【可用工具与扩展技能 (Skill Tools)】", dec_prompt)
        self.assertIn("send_image", dec_prompt)
        self.assertLess(len(dec_prompt), len(sys_prompt))

    def test_ai_decision_parsing_with_skill(self):
        raw_json = (
            '{\n'
            '  "should_reply": true,\n'
            '  "action": "skill",\n'
            '  "skill": "send_image",\n'
            '  "params": {"query": "初雪樱 玉树樱 立绘"},\n'
            '  "reply": "初雪樱的女主玉树樱立绘来啦~"\n'
            '}'
        )
        dec = parse_ai_decision_and_reply(raw_json)
        self.assertTrue(dec.should_reply)
        self.assertEqual(dec.action, "skill")
        self.assertEqual(dec.skill_name, "send_image")
        self.assertIn("query", dec.skill_params)
        self.assertEqual(dec.skill_params["query"], "初雪樱 玉树樱 立绘")
        self.assertIn("玉树樱立绘来啦", dec.reply_text)

    def test_send_file_skill_execution(self):
        """测试 SendFileSkill 文本导出与安全路径暂存"""
        async def run_test():
            import tempfile
            from pathlib import Path
            from config import AppConfig, MediaConfig
            from media_models import MessageEnvelope
            from messaging import SendReceipt

            skill = SendFileSkill()
            tmp = tempfile.mkdtemp()
            try:
                cfg = AppConfig()
                cfg.media.cache_dir = tmp
                m_ctx = MessageEnvelope(
                    transport="onebot",
                    account_id="acc1",
                    conversation_id="grp101",
                    sender_id="user1",
                    message_id="msg1",
                    text="导出文档",
                    send_text=AsyncMock(),
                    is_group=True
                )
                ctx = SkillContext(
                    session_key="test_file",
                    conversation_id="grp101",
                    user_id="user1",
                    app_config=cfg,
                    media_context=m_ctx
                )

                mock_sender = MagicMock()
                mock_sender.send = AsyncMock(return_value=SendReceipt(status="ok"))

                with patch("media_sender.MediaSender", return_value=mock_sender):
                    res = await skill.execute({
                        "file_name": "summary_report.txt",
                        "content": "测试文档导出内容：这是第一行汇总数据。"
                    }, ctx)

                self.assertTrue(res.success)
                self.assertIn("summary_report.txt", res.message)

                # 验证导出的文件实际落入 cache_dir / exports
                exported_file = Path(tmp) / "exports" / "summary_report.txt"
                self.assertTrue(exported_file.exists())
                self.assertEqual(exported_file.read_text(encoding="utf-8"), "测试文档导出内容：这是第一行汇总数据。")

                # 验证 mock_sender.send 被调用且参数正确
                mock_sender.send.assert_awaited_once()
                call_args = mock_sender.send.call_args
                self.assertTrue(call_args.kwargs.get("as_file") or (len(call_args.args) > 2 and call_args.args[2]))
            finally:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)

        asyncio.run(run_test())

    def test_web_search_general_query_no_forced_meme_keywords(self):
        """测试通用搜索不强制追加'是什么梗 含义'"""
        async def run_test():
            from web_search import MemeWebSearcher

            searcher = MemeWebSearcher()
            mock_snippet = "《初雪樱》（Hatsuyuki Sakura）是SAGA PLANETS出品的恋爱冒险游戏，女主角为玉树樱。"

            with patch.object(searcher, "_sync_http_search", return_value=mock_snippet) as mock_http:
                res = await searcher.search_general("初雪樱 女主 玉树樱")
                self.assertIsNotNone(res)
                self.assertIn("玉树樱", res.get("snippet", ""))
                # 确认调用的 query 没有被强行追加 meme 后缀
                mock_http.assert_called_once()
                args, kwargs = mock_http.call_args
                called_query = args[0]
                self.assertEqual(called_query, "初雪樱 女主 玉树樱")
                self.assertNotIn("是什么梗", called_query)

        asyncio.run(run_test())

    def test_ai_decision_reply_strips_raw_json_leak(self):
        """测试当模型输出 Markdown JSON 块时，正确解析回复且不泄漏原始代码块"""
        raw_output = (
            "```json\n"
            "{\n"
            '  "should_reply": true,\n'
            '  "action": "reply",\n'
            '  "reply": "初雪樱的女主角是玉树樱（玉树 樱）。"\n'
            "}\n"
            "```"
        )
        dec = parse_ai_decision_and_reply(raw_output)
        self.assertTrue(dec.should_reply)
        self.assertEqual(dec.reply_text, "初雪樱的女主角是玉树樱（玉树 樱）。")
        self.assertNotIn("```json", dec.reply_text)
        self.assertNotIn('"should_reply"', dec.reply_text)


if __name__ == "__main__":
    unittest.main()
