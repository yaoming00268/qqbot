"""Configured AgentAPI requests over the local language server's JSON RPC."""
from __future__ import annotations

from copy import deepcopy
import json
import time

import aiohttp


async def configured_command(client, args, config, *, timeout, env, on_created=None):
    """Never fall back to an unrestricted invocation if policy setup fails."""
    from antigravity_client import AgentAPIConnectionRefusedError, ContentBlockedError, InvalidMediaError
    address, token = env.get("ANTIGRAVITY_LS_ADDRESS"), env.get("ANTIGRAVITY_CSRF_TOKEN")
    if not address or not token:
        raise RuntimeError("受限主会话缺少本地 Language Server 连接。")
    endpoint = f"http://{address}/exa.language_server_pb.LanguageServerService/"
    budget = aiohttp.ClientTimeout(total=timeout if timeout and timeout > 0 else None)
    async with aiohttp.ClientSession(trust_env=False, timeout=budget,
            headers={"X-Codeium-Csrf-Token": token}) as http:
        async def rpc(method, body):
            try:
                async with http.post(endpoint + method, json=body, allow_redirects=False) as response:
                    raw = await response.text()
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        raise RuntimeError(f"{method} 返回无效 JSON（HTTP {response.status}）。") from None
                    if response.status >= 300 or isinstance(data, dict) and data.get("error"):
                        detail = str(data.get("message") or data.get("error") or data)
                        if client._content_is_blocked(detail):
                            raise ContentBlockedError(detail)
                        if client._invalid_media(detail):
                            raise InvalidMediaError(detail)
                        raise RuntimeError(f"{method} HTTP {response.status}: {detail}")
                    return data
            except aiohttp.ClientConnectorError as error:
                if getattr(error.os_error, "errno", None) in (61, 111, 10061) or getattr(error.os_error, "winerror", None) == 10061:
                    raise AgentAPIConnectionRefusedError("Language Server 初始连接被拒绝。") from error
                raise

        policy = deepcopy(config)
        if args[0] == "new-conversation":
            tier = next((arg.partition("=")[2] for arg in args[1:-1] if arg.startswith("--model=")), "flash")
            cache = getattr(client, "_configured_models", {})
            key = (address, client._connection_generation, tier)
            entry = cache.get(key)
            if not entry or time.monotonic() - entry[0] > 300:
                models = await rpc("GetCascadeModelConfigData", {})
                selected = resolve_model(models, tier)
                cache[key] = (time.monotonic(), selected)
                client._configured_models = cache
            else:
                selected = entry[1]
            policy["plannerConfig"]["requestedModel"] = selected
            started = await rpc("StartCascade", {"source": "CORTEX_TRAJECTORY_SOURCE_AGENT_API",
                "projectEnvConfig": {"projectId": env.get("ANTIGRAVITY_PROJECT_ID", "")}})
            cid = started.get("cascadeId")
            if not cid:
                raise RuntimeError("受限主会话未返回 cascadeId。")
            try:
                if on_created is not None:
                    on_created(cid)
                await rpc("SendUserCascadeMessage", {"cascadeId": cid, "items": [{"text": args[-1]}],
                    "cascadeConfig": policy, "blocking": False})
            except BaseException:
                await client._cancel_native_conversation(cid)
                raise
            return {"response": {"newConversation": {"conversationId": cid}}}
        if args[0] == "send-message":
            await rpc("SendUserCascadeMessage", {"cascadeId": args[1], "items": [{"text": args[-1]}],
                "cascadeConfig": policy, "blocking": False})
            return {"response": {}}
        raise ValueError("受限会话只支持新建与追加消息。")


def resolve_model(data, tier):
    """Use the server's advertised model IDs; never pin opaque enum numbers."""
    available = [row for row in data.get("clientModelConfigs", []) if not row.get("disabled")]
    def matches(row):
        label = (str(row.get("modelId", "")) + " " + str(row.get("label", ""))).lower()
        if tier == "flash":
            return "gemini" in label and "flash" in label and "lite" not in label
        if tier == "flash_lite":
            return "gemini" in label and "flash" in label and "lite" in label
        if tier == "pro":
            return "gemini" in label and "pro" in label
        return row.get("modelId") == tier or (row.get("modelOrAlias") or {}).get("model") == tier
    rows = [row for row in available if matches(row) and row.get("modelOrAlias")]
    preferred = (data.get("defaultOverrideModelConfig") or {}).get("modelOrAlias")
    if not rows:
        raise RuntimeError(f"本地服务未提供所选模型 {tier}；未改用其他模型或放宽主会话权限。")
    return next((row["modelOrAlias"] for row in rows if row["modelOrAlias"] == preferred), rows[0]["modelOrAlias"])
