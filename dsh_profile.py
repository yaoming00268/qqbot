"""Version-pinned overlays for the official SDK profile and QQBot MCP bridge."""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from runtime_paths import module_command


# These are ids from the bundled 0.2.2rc2 SDK profile. Never inherit the desktop
# coding agent's tools into the QQ main persona, or enable native recursive agents.
TOOL_IDS = ("tool-plugin-manager", "tool-working-directory", "tool-bash", "tool-pwsh", "tool-jobs",
    "tool-fs", "tool-fs-search", "tool-skill", "tool-agent-team", "tool-subagent-control",
    "tool-subagent", "tool-subagent-fork", "tool-workflow", "tool-todo", "tool-goal", "tool-web",
    "tool-office", "workspace-dependencies", "tool-schedule", "tool-ask-user", "present")
ALWAYS_DISABLED = ("agent-instructions", "skill-filesystem", "skill-office", "goal-round-driver",
    "schedule", "file-upload", "file-reference-local", "deliverable-file-references",
    "plugin-manager", "config-editor", "settings", "ptc-runtime", "workflow-ptc")


def build_patch(home, *, scope="main", system_prompt=""):
    if scope not in ("main", "work", "public", "probe"):
        raise ValueError("Unknown DSH tool scope")
    rows = [{"id": name, "disabled": True} for name in ALWAYS_DISABLED]
    allowed = {"tool-bash", "tool-pwsh", "tool-jobs", "tool-fs", "tool-fs-search", "tool-working-directory", "tool-web"} if scope == "work" else {"tool-web"} if scope == "public" else set()
    rows += [{"id": name, "disabled": name not in allowed} for name in TOOL_IDS]
    rows += [{"id": "system-prompt", "config": {"includeHarnessIdentity": False,
        "includeRuntimeContext": False, "personaPrefix": system_prompt.replace("{{", "{ {")}},
        {"id": "session-persistence-jsonl", "config": {"root": str(Path(home).resolve() / "sessions"), "compression": "none"}},
        {"id": "approval", "config": {"policy": "never"}},
        {"id": "permission", "config": {"presets": {"qqbot": {"sandbox": "workspace-write", "approval": "never"}}, "defaultPreset": "qqbot"}}]
    if scope != "probe":
        executable, *args = module_command("mcp")
        rows.append({"insert": [{"id": "qqbot-console", "name": "@deepseek-ai/dsh-mcp-client", "config": {
            "serverName": "qqbot", "transport": "stdio", "command": executable, "args": args,
            "cwd": str(Path.cwd().resolve()), "env": {"QQBOT_MCP_ROLE": "main" if scope == "main" else "worker"}, "toolCallTimeoutMs": 180000,
            "startupTimeoutMs": 30000, "failOnStartupError": True}}]})
    text = yaml.safe_dump(rows, allow_unicode=True, sort_keys=False)
    digest = hashlib.sha256(text.encode()).hexdigest()
    path = Path(home).resolve() / "qqbot-overlays" / (scope + "-" + digest[:20] + ".yml")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(text, encoding="utf-8")
    return path


def export_mcp_config(home):
    """Ready-to-use official patch, also usable from an external DSH CLI."""
    return build_patch(home, scope="main", system_prompt="使用 QQBot CLI 完成当前事件；保持同一个持久身份、记忆与关系。")
