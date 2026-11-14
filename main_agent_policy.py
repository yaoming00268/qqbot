"""Native, per-conversation capabilities for the conversational agent."""
from __future__ import annotations

from pathlib import Path
import sys

from runtime_paths import module_command


def subagent_cascade_config(*, public_only=False):
    """Workers keep native work tools, with only their mailbox MCP exposed."""
    executable, *arguments = module_command("mcp")
    console = {"serverName": "qqbot-agent-v3", "command": executable,
        "args": arguments,
        "enabledTools": ["qqbot_cli", "qqbot_console_help"],
        "forceAllToolsEager": True, "skipToolNamePrefix": True}
    discovery = {name: {"inheritUser": False} for name in ("skills", "agents", "plugins", "rules", "hooks")}
    discovery["mcp"] = {"inheritUser": False, "servers": [console]}
    discovery["kindEnablement"] = {name: False for name in ("agent", "flow", "hooks", "plugin", "rule", "skill", "workflow")}
    discovery["kindEnablement"]["mcp"] = True
    tools = {"invokeSubagent": {"enabled": False}, "schedule": {"enabled": False},
             "skillSearch": {"enabled": False}, "memory": {"forceDisable": True},
             "mcp": {"enableLazyLoad": False, "nonBlockingTurnLoad": False}}
    if public_only:
        # Public reading has no shell, local account access or native browser
        # profile. Interactive browsing goes through companion's own profile.
        for name in ("code", "writeToFile", "viewFile", "viewCodeItem", "listDir", "grep", "find",
                     "mquery", "codeSearch", "runCommand", "commandStatus", "generateImage", "internalSearch",
                     "knowledgeBaseSearch", "trajectorySearch"):
            tools[name] = {"forceDisable": True}
        tools.update(antigravityBrowser={"enabled": False}, notebookEdit={"enabled": False},
                     readKnowledgeBaseItem={"enabled": False})
    planner = {"toolConfig": tools, "customizationDiscoveryConfig": discovery,
        "customizationConfig": {"mcpServers": [console], "customizationDiscoveryConfig": discovery}}
    if public_only:
        # The installed server also selects tools through declarative components.
        planner["declarativeMixinConfig"] = {
            "tools": [{"name": name, "builtin": True} for name in
                      ("search_web", "read_url_content", "view_content_chunk", "call_mcp_tool")],
            "promptSections": [{"builtinName": "mcp_servers"}], "forceDisableFundamentalComponents": True,
            "resolutionPolicy": {"unknownComponentPolicy": "RESOLUTION_POLICY_IGNORE",
                                 "emptyComponentPolicy": "RESOLUTION_POLICY_IGNORE"}}
    return {"plannerConfig": planner}


def main_cascade_config(*, idle=False):
    # These are LanguageServerService CascadeToolConfig fields. Workers never
    # receive this config: they retain their normal coding and file tools.
    disabled = ("code", "writeToFile", "viewFile", "viewCodeItem", "listDir",
                "grep", "find", "mquery", "codeSearch", "runCommand", "commandStatus",
                "searchWeb", "readUrlContent", "generateImage", "internalSearch",
                "knowledgeBaseSearch", "memory", "trajectorySearch")
    tools = {name: {"forceDisable": True} for name in disabled}
    tools.update(antigravityBrowser={"enabled": False}, invokeSubagent={"enabled": False},
                 notebookEdit={"enabled": False}, skillSearch={"enabled": False},
                 schedule={"enabled": False}, readKnowledgeBaseItem={"enabled": False},
                 forceDisableToolCalling=idle)
    tools["mcp"] = {"enableLazyLoad": False, "nonBlockingTurnLoad": False}
    executable, *arguments = module_command("mcp")
    console = {"serverName": "qqbot-chat-v9", "command": executable,
               "args": arguments,
               "enabledTools": ["qqbot_reply", "qqbot_cli", "qqbot_read_memory", "qqbot_console_help"],
               "forceAllToolsEager": True, "skipToolNamePrefix": True}
    discovery = {name: {"inheritUser": False} for name in ("skills", "agents", "plugins", "rules", "hooks")}
    discovery["mcp"] = {"inheritUser": False, "servers": [] if idle else [console]}
    discovery["kindEnablement"] = {name: False for name in
        ("agent", "flow", "hooks", "plugin", "rule", "skill", "workflow")}
    discovery["kindEnablement"]["mcp"] = not idle
    # Do not inherit the desktop coding agent's global skills, rules, plugins or
    # MCP servers into a QQ persona. Both discovery entry points are supported
    # by the installed server and are scoped to this invocation only.
    return {"plannerConfig": {"toolConfig": tools,
        # Current servers select builtins through this explicit component list;
        # legacy forceDisable switches alone do not constrain those tools.
        "declarativeMixinConfig": {"tools": [{"name": "call_mcp_tool", "builtin": True}],
            "promptSections": [{"builtinName": "mcp_servers"}], "forceDisableFundamentalComponents": True,
            "resolutionPolicy": {"unknownComponentPolicy": "RESOLUTION_POLICY_IGNORE",
                                 "emptyComponentPolicy": "RESOLUTION_POLICY_IGNORE"}},
        "customizationDiscoveryConfig": discovery,
        "customizationConfig": {"mcpServers": [] if idle else [console],
                                "customizationDiscoveryConfig": discovery}}}
