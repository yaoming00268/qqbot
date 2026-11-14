# -*- coding: utf-8 -*-
"""
Web Search & Knowledge Retrieval Skills.
Uses the connected Antigravity native search and page/browser tools.
"""

from __future__ import annotations
import logging
from typing import Any, Dict

from skills.base import BaseSkill, SkillParameter, SkillContext, SkillResult

logger = logging.getLogger("WebSkills")


class WebSearchSkill(BaseSkill):
    """网络搜索与实时信息检索技能"""
    name = "web_search"
    display_name = "网络搜索"
    description = "执行实时网络检索，获取关于动漫、人物资料、新闻、网络热梗、攻略等客观准确信息。"
    parameters = [
        SkillParameter(name="query", param_type="string", description="搜索关键词，如'初雪樱 玉树樱'或'最新游戏攻略'", required=True),
        SkillParameter(name="max_results", param_type="integer", description="仅用户明确要求时设置引用来源的数量，不限制检索网页数", required=False)
    ]

    async def execute(self, params: Dict[str, Any], context: SkillContext) -> SkillResult:
        query = str(params.get("query") or "").strip()
        if not query:
            return SkillResult(success=False, message="缺少搜索关键词", action_type="error")

        logger.info(f"🌐 [WebSearchSkill] 正在检索: query='{query}'")
        try:
            import json
            from native_retrieval import json_result
            if context.ai_client is None:
                return SkillResult(success=False, message="网络检索缺少反重力会话连接。", action_type="error")
            limit = max(1, int(params["max_results"])) if params.get("max_results") else None
            if context.media_context is not None and context.media_service is not None:
                from skills.task_skills import RunTaskSkill
                from media_router import parse_media_intent
                request = context.media_context.text or query
                intent = parse_media_intent(request)
                task = ("自主执行完整网络检索任务，按需要搜索、浏览、抓取、核实和下载，没有工作时限。\n"
                        "用户要求图片、CG、视频、资料等资源时，下载并实际查看核对，再列入交付文件；不要只返回链接或资料介绍。"
                        "用户只问知识或信息时，在 answer 中提供完整实质答案并在 sources 中记录来源；结果将保存为文件交付。\n"
                        f"完整用户请求：{request}\n检索主体和补充上下文：{query}")
                if limit is not None:
                    task += f"\n用户要求最终引用 {limit} 项来源；检索和访问的网页数量不受此数量限制。"
                options = {"task": task, "_requires_network": True}
                if intent and intent.operation in ("search", "search_send", "send_url"):
                    options.update(output_kind=intent.kind, _intent=intent, _as_file=intent.as_file)
                return await RunTaskSkill().execute(options, context)
            task = (
                "自主检索、浏览和抓取足够的网络资料，核对后作答并附来源链接。"
                "原生搜索、网页读取、浏览器、终端和脚本均可按需要使用，没有固定网页数量或操作次数。"
                '返回 {"answer":"检索后的中文回答","sources":[{"title":"来源标题","url":"实际来源URL"}]}。'
                "来源 URL 必须出现在本轮网络工具结果中。请求数据："
                + json.dumps({"query": query, "max_results": limit}, ensure_ascii=False)
            )
            evidence = await context.ai_client.native_retrieve(context.session_key, task, timeout=0, require_page=False)
            evidence.verify(require_page=False)
            data = json_result(evidence.text)
            answer = str(data.get("answer") or "").strip()
            sources = data.get("sources")
            if not answer or not isinstance(sources, list) or not sources:
                return SkillResult(success=False, message="原生检索未返回有来源的回答。", action_type="error")
            verified = []
            for source in sources[:limit]:
                url = evidence.require_url(source["url"])
                verified.append({"title": str(source.get("title") or "来源"), "url": url})
            message = answer + "\n\n来源：\n" + "\n".join(s["title"] + "：" + s["url"] for s in verified)
            return SkillResult(success=True, message=message,
                               data={"answer": answer, "sources": verified}, action_type="text")
        except Exception as e:
            logger.error(f"[WebSearchSkill] 搜索异常: {e}")
            return SkillResult(success=False, message=str(e), action_type="error")
