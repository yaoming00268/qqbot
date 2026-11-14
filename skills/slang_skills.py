"""Look up confirmed slang and enrich empty entries in the background lane."""
from __future__ import annotations

from dataclasses import asdict
import re

from skills.base import BaseSkill, SkillContext, SkillParameter, SkillResult
from slang_glossary import default_slang_glossary


class GroupSlangSkill(BaseSkill):
    name = "group_slang"
    display_name = "群聊话术表"
    description = ("按词语、句子、别名、标签或索引查询已确认的群聊黑话；已有解释直接复用。"
                   "空解释在后台联网概括后写回固定 Markdown 话术表。只有主人明确要求时才收录新黑话。")
    parameters = [
        SkillParameter("query", description="要查询或明确收录的黑话词语、句子、标签或词条索引"),
        SkillParameter("operation", description="lookup/resolve/record，默认 lookup", required=False, default="lookup"),
        SkillParameter("meaning", description="主人明确提供的解释；留空则由后台检索", required=False),
        SkillParameter("tags", description="以空格分隔的标签", required=False),
        SkillParameter("aliases", description="以英文分号分隔的别名", required=False),
    ]

    async def execute(self, params: dict, context: SkillContext) -> SkillResult:
        glossary = context.extra.get("slang_glossary") or default_slang_glossary
        query = str(params.get("query") or "").strip()
        operation = str(params.get("operation") or "lookup")
        if not query:
            return SkillResult(False, "请指定话术表词语、句子、标签或索引。", action_type="error")
        if operation not in ("lookup", "resolve", "record"):
            return SkillResult(False, "话术表操作无效。", action_type="error")
        if operation == "record":
            request = context.media_context.text if context.media_context else str(context.extra.get("user_text") or "")
            if not context.is_master or query not in request or not re.search(r"黑话|话术表|词条|收录", request):
                return SkillResult(False, "新增黑话需要主人明确说明要收录的词语或句子。", action_type="error")
            entry = glossary.record(query, meaning=str(params.get("meaning") or ""),
                tags=params.get("tags") or (), aliases=params.get("aliases") or ())
            entries = [entry]
        else:
            entries = glossary.search(query)
        if not entries:
            return SkillResult(True, "话术表中没有该词条，尚未将其判定为黑话。", data={"entries": []})
        missing = [entry.term for entry in entries if entry.needs_explanation]
        if missing and context.ai_client is not None:
            from conversation_runtime import current_event
            event = current_event.get()
            # The ordinary client dispatcher queues skills before execution.
            # Standalone callers must not accidentally block the chat lane.
            if event is not None and event.lane == "chat":
                return SkillResult(False, "黑话解释需加入后台任务队列。", action_type="error")
            check = context.media_context.check_current if context.media_context else lambda: None
            await glossary.resolve_missing(missing, context.ai_client, context.session_key, check_current=check)
            entries = [entry for entry in glossary.search(query)]
        message = "\n".join(entry.term + "：" + ("已确认是黑话，解释待补充。" if entry.needs_explanation else entry.meaning)
                            for entry in entries)
        return SkillResult(True, message, data={"entries": [asdict(entry) for entry in entries]})
