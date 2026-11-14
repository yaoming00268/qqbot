"""Regression tests for local slang routing, verified enrichment and merge safety."""

from testing_sandbox import bootstrap_test_module
bootstrap_test_module(__name__, __file__)
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from conversation_runtime import EventContext, current_event
from native_retrieval import NativeRetrievalResult
from skills.base import SkillContext
from skills.slang_skills import GroupSlangSkill
from slang_glossary import SlangGlossary, SLANG_GLOSSARY_PATH


SOURCE = "https://example.com/slang-explanation"


def evidence(*, answer="形容群聊中某种约定俗成的行为。", tags=None, source=SOURCE):
    return NativeRetrievalResult(json.dumps({"answer": answer, "tags": tags or ["用语", "群聊"],
        "sources": [{"title": "网络用语解释", "url": source}]}, ensure_ascii=False),
        frozenset({"search_web"}), frozenset({SOURCE}))


class GlossaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qqbot_slang_")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "slang_glossary.md"
        self.glossary = SlangGlossary(self.path)
        self.ai = MagicMock(native_retrieve=AsyncMock(return_value=evidence()), archive_session=AsyncMock())

    def test_default_path_is_independent_of_working_directory(self):
        self.assertEqual(SLANG_GLOSSARY_PATH, Path(__file__).resolve().parent / "data/slang_glossary.md")

    def test_unknown_messages_are_not_admitted_and_local_meanings_are_reused(self):
        self.assertEqual(self.glossary.match("看不懂的普通聊天"), [])
        self.assertFalse(self.path.exists())
        entry = self.glossary.record("测试黑话", meaning="群里已确认的解释", aliases="已知别名", tags="群聊 测试")
        self.assertEqual(self.glossary.match("有人说已知别名")[0], entry)
        self.assertEqual(self.glossary.pending_terms("测试黑话"), [])
        section = self.glossary.prompt_section("测试黑话")
        self.assertIn("群里已确认的解释", section)
        self.assertEqual(json.loads(section.splitlines()[1]), [{"term": "测试黑话", "meaning": "群里已确认的解释"}])
        self.assertEqual(self.glossary.search("#测试"), [entry])
        self.assertEqual(self.glossary.search(entry.index), [entry])
        self.ai.native_retrieve.assert_not_awaited()

    def test_url_aliases_do_not_inject_unrelated_slang_into_browsing_requests(self):
        self.glossary.record("草", meaning="笑意", aliases="www")
        self.assertEqual(self.glossary.prompt_section("去看看 https://www.linovelib.com/"), "")
        self.assertEqual(self.glossary.match("https://example.com/www?q=www"), [])
        self.assertEqual(self.glossary.match("https://www.example.com/，www！")[0].term, "草")

    def test_ascii_words_match_boundaries_instead_of_unrelated_substrings(self):
        self.glossary.record("OP", meaning="测试用语")
        self.assertFalse(self.glossary.match("open the file"))
        self.assertEqual(self.glossary.match("这是 op？")[0].term, "OP")

    def test_single_cjk_character_matches_isolated_words_instead_of_unrelated_substrings(self):
        self.glossary.record("急", meaning="嘲讽用语", aliases="他急了")
        self.assertFalse(self.glossary.match("别着急慢慢来"))
        self.assertFalse(self.glossary.match("紧急情况"))
        self.assertEqual(self.glossary.match("急！")[0].term, "急")
        self.assertEqual(self.glossary.match("急急急")[0].term, "急")
        self.assertEqual(self.glossary.match("他急了开始胡言乱语")[0].term, "急")

        self.glossary.record("乐", meaning="看乐子", aliases="找乐子")
        self.assertFalse(self.glossary.match("大家推荐一些好听的音乐"))
        self.assertFalse(self.glossary.match("音乐游戏"))
        self.assertEqual(self.glossary.match("笑死，乐")[0].term, "乐")
        self.assertEqual(self.glossary.match("找乐子")[0].term, "乐")

    def test_hand_edited_rows_take_effect_and_escaped_content_roundtrips(self):
        self.glossary.record("测试黑话", meaning="a|b\nsecond line", aliases="另一个名字", tags="测试")
        original = self.path.read_text(encoding="utf-8")
        original = original.replace("<!-- entries:end -->", "| | 手工添加的句子 | | | #手工 | |\n<!-- entries:end -->")
        original += "\n人工补充说明保留在这里。\n"
        self.path.write_text(original, encoding="utf-8")
        self.assertEqual(self.glossary.pending_terms("有人说手工添加的句子"), ["手工添加的句子"])
        self.glossary.record("第三个词", meaning="说明")
        self.assertEqual(self.glossary.search("测试黑话")[0].meaning, "a|b\nsecond line")
        self.assertIn("人工补充说明保留在这里", self.path.read_text(encoding="utf-8"))
        self.assertTrue(self.glossary.search("手工添加的句子")[0].index.startswith("slang-"))

    def test_parallel_writers_merge_without_losing_other_terms(self):
        def write(index):
            return SlangGlossary(self.path).record("词条" + str(index), meaning="解释" + str(index))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(20)))
        self.assertEqual(len(self.glossary.entries()), 20)

    def test_console_save_replaces_fields_and_renames_without_losing_other_entries_or_notes(self):
        old = self.glossary.save_entry("旧词", meaning="旧解释", aliases="旧别名", tags="旧标签", sources=SOURCE)
        other = self.glossary.record("其他词", meaning="保留这条")
        self.path.write_text(self.path.read_text(encoding="utf-8") + "\n人工注释应保留。\n", encoding="utf-8")
        updated = self.glossary.save_entry("新词", index=old.index, meaning="新解释|第二行\n换行",
            aliases="新别名", tags="新标签", sources="", expected=old)
        self.assertEqual(updated.index, old.index)
        self.assertEqual(self.glossary.entries(), [updated, other])
        self.assertEqual(updated.aliases, ("新别名",))
        self.assertEqual(updated.tags, ("新标签",))
        self.assertEqual(updated.sources, ())
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("人工注释应保留", text)
        self.assertNotIn("#旧标签", text)
        self.assertIn("#新标签", text)
        self.assertFalse(self.glossary.match("旧别名"))
        self.assertEqual(self.glossary.match("新别名"), [updated])

    def test_console_conflicting_save_or_delete_preserves_background_explanation_and_draft(self):
        old = self.glossary.save_entry("待补词")
        filled = SlangGlossary(self.path)._fill_existing("待补词", "后台新解释", ["后台标签"], [SOURCE])
        for operation in (lambda: self.glossary.save_entry("待补词", index=old.index, meaning="旧窗口内容", expected=old),
                          lambda: self.glossary.delete_entry(old.index, expected=old)):
            with self.assertRaisesRegex(ValueError, "已被后台或其他窗口更新"):
                operation()
            self.assertEqual(self.glossary.entries(), [filled])

    def test_console_saving_one_entry_keeps_unrelated_concurrent_changes(self):
        old = self.glossary.save_entry("编辑中的词", meaning="原解释")
        other = SlangGlossary(self.path).record("新加的其他词", meaning="来自后台")
        updated = self.glossary.save_entry(old.term, index=old.index, meaning="手动更新", expected=old)
        self.assertEqual(self.glossary.entries(), [updated, other])

    def test_console_rename_keeps_unique_indices_when_original_name_is_added_again(self):
        old = self.glossary.save_entry("原词")
        renamed = self.glossary.save_entry("改名后的词", index=old.index, expected=old)
        recreated = self.glossary.record("原词")
        self.assertNotEqual(renamed.index, recreated.index)
        self.assertEqual(self.glossary.search(old.index), [renamed])
        self.assertEqual(len(self.glossary.entries()), 2)

    def test_console_duplicate_or_invalid_terms_do_not_overwrite_existing_entries(self):
        old = self.glossary.save_entry("Ｎｅｗ", meaning="解释")
        other = self.glossary.save_entry("其他词")
        for operation in (lambda: self.glossary.save_entry("new", meaning="重复"),
                          lambda: self.glossary.save_entry("new", index=other.index, expected=other),
                          lambda: self.glossary.save_entry(""), lambda: self.glossary.save_entry("多行\n词条")):
            before = self.path.read_bytes()
            with self.assertRaises(ValueError):
                operation()
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.glossary.entries(), [old, other])

    async def test_console_deletion_during_search_cannot_be_recreated_by_stale_result(self):
        old = self.glossary.save_entry("测试黑话", tags="待删标签")
        async def retrieve(*args, **kwargs):
            self.glossary.delete_entry(old.index, expected=old)
            return evidence()
        self.ai.native_retrieve.side_effect = retrieve
        self.assertEqual(await self.glossary.resolve_missing([old.term], self.ai, "g"), [])
        self.assertEqual(self.glossary.entries(), [])
        self.assertNotIn("待删标签", self.path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "已被删除"):
            self.glossary.save_entry(old.term, index=old.index, expected=old)

    async def test_verified_enrichment_uses_work_branch_and_archives_it(self):
        old = self.glossary.record("测试黑话", tags="人工标签")
        result = await self.glossary.resolve_missing([old.term], self.ai, "onebot_group_1")
        key, prompt = self.ai.native_retrieve.await_args.args
        self.assertTrue(key.startswith("__work__:slang:"))
        self.assertNotEqual(key, "onebot_group_1")
        self.assertEqual(self.ai.native_retrieve.await_args.kwargs["timeout"], 0)
        self.assertIn("资料", prompt)
        self.assertEqual(result[0].sources, (SOURCE,))
        self.assertIn("人工标签", result[0].tags)
        self.assertIn("用语", result[0].tags)
        self.assertEqual(self.glossary.pending_terms(old.term), [])
        self.assertEqual(self.ai.archive_session.await_count, 2)
        for call in self.ai.archive_session.await_args_list:
            payload = json.loads(Path(call.args[1]).read_text(encoding="utf-8"))
            self.assertEqual(payload["entry"]["index"], old.index)
        await self.glossary.resolve_missing([old.term], self.ai, "onebot_group_1")
        self.ai.native_retrieve.assert_awaited_once()

    async def test_concurrent_empty_term_requests_do_not_duplicate_network_search(self):
        self.glossary.record("测试黑话")
        entered, release = asyncio.Event(), asyncio.Event()
        async def retrieve(*args, **kwargs):
            entered.set()
            await release.wait()
            return evidence()
        self.ai.native_retrieve.side_effect = retrieve
        first = asyncio.create_task(self.glossary.resolve_missing(["测试黑话"], self.ai, "g1"))
        await entered.wait()
        second = asyncio.create_task(self.glossary.resolve_missing(["测试黑话"], self.ai, "g2"))
        release.set()
        first_result, second_result = await asyncio.gather(first, second)
        self.assertEqual(first_result, second_result)
        self.ai.native_retrieve.assert_awaited_once()

    async def test_unverified_sources_and_ambiguous_definitions_do_not_pollute_table(self):
        self.glossary.record("测试黑话")
        self.ai.native_retrieve.return_value = evidence(source="https://example.com/invented")
        with self.assertRaises(Exception):
            await self.glossary.resolve_missing(["测试黑话"], self.ai, "g")
        self.assertEqual(self.glossary.pending_terms("测试黑话"), ["测试黑话"])
        self.ai.archive_session.assert_not_awaited()
        self.ai.native_retrieve.return_value = evidence(answer="")
        with self.assertRaises(ValueError):
            await self.glossary.resolve_missing(["测试黑话"], self.ai, "g")
        self.assertEqual(self.glossary.pending_terms("测试黑话"), ["测试黑话"])

    async def test_manual_explanation_written_during_search_wins(self):
        self.glossary.record("测试黑话")
        async def retrieve(*args, **kwargs):
            self.glossary.record("测试黑话", meaning="人工纠正的解释", tags="主人解释")
            self.glossary.record("其他词条", meaning="不能被并发回写覆盖")
            return evidence()
        self.ai.native_retrieve.side_effect = retrieve
        result = await self.glossary.resolve_missing(["测试黑话"], self.ai, "g")
        self.assertEqual(result[0].meaning, "人工纠正的解释")
        self.assertEqual(self.glossary.search("其他词条")[0].meaning, "不能被并发回写覆盖")

    async def test_deleted_term_is_not_recreated_by_a_stale_network_result(self):
        self.glossary.record("测试黑话")
        async def retrieve(*args, **kwargs):
            lines = self.path.read_text(encoding="utf-8").splitlines()
            self.path.write_text("\n".join(line for line in lines if not line.startswith("| slang-")), encoding="utf-8")
            return evidence()
        self.ai.native_retrieve.side_effect = retrieve
        self.assertEqual(await self.glossary.resolve_missing(["测试黑话"], self.ai, "g"), [])
        self.assertEqual(self.glossary.entries(), [])

    async def test_unknown_term_is_never_resolved_or_admitted(self):
        self.assertEqual(await self.glossary.resolve_missing(["未经确认的词"], self.ai, "g"), [])
        self.ai.native_retrieve.assert_not_awaited()
        self.assertFalse(self.path.exists())

    async def test_skill_reuses_meaning_and_requires_explicit_master_record_request(self):
        skill = GroupSlangSkill()
        context = SkillContext("g", "1", "2", ai_client=self.ai, extra={"slang_glossary": self.glossary})
        denied = await skill.execute({"operation": "record", "query": "测试黑话"}, context)
        self.assertFalse(denied.success)
        context.is_master = True
        context.extra["user_text"] = "把测试黑话收录到话术表，解释是群内约定用语"
        result = await skill.execute({"operation": "record", "query": "测试黑话", "meaning": "群内约定用语"}, context)
        self.assertTrue(result.success)
        looked_up = await skill.execute({"query": "测试黑话"}, context)
        self.assertIn("群内约定用语", looked_up.message)
        self.ai.native_retrieve.assert_not_awaited()

    async def test_skill_will_not_start_search_from_the_chat_lane(self):
        self.glossary.record("测试黑话")
        context = SkillContext("g", "1", "2", ai_client=self.ai, extra={"slang_glossary": self.glossary})
        token = current_event.set(EventContext("g", "m", 0, "chat", "owner"))
        try:
            result = await GroupSlangSkill().execute({"query": "测试黑话"}, context)
        finally:
            current_event.reset(token)
        self.assertFalse(result.success)
        self.ai.native_retrieve.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
