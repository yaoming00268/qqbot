# -*- coding: utf-8 -*-
"""
Antigravity Gemini Relay Client.
Interacts with the local Antigravity Language Server to leverage the user's
logged-in Google Antigravity Gemini quota (Gemini 3.8 Flash, Pro, Flash-Lite).
"""

from __future__ import annotations
import asyncio
import glob
import hashlib
import json
import logging
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("AntigravityClient")


class SessionInvalidatedError(RuntimeError):
    """The request belongs to a context cleared or changed during generation."""


class AgentServiceError(RuntimeError):
    """Infrastructure failures must not be parsed or remembered as AI replies."""

    code = "AI_SERVICE_UNAVAILABLE"
    user_message = "⚠️ AI 服务暂时不可用，请稍后再试；原对话已保留。"

    def __init__(self):
        super().__init__(self.user_message)


class AgentServiceTimeoutError(AgentServiceError):
    code = "AI_SERVICE_TIMEOUT"
    user_message = "⏱️ AI 回复超时，请稍后再试；原对话已保留。"


class AgentServiceBusyError(AgentServiceError):
    code = "MODEL_BUSY"
    user_message = "⚠️ AI 模型服务繁忙，后端重试后仍未取得回复；原对话已保留，请稍后再试。"


class InvalidMediaError(AgentServiceError):
    """A typed provider failure; repeating unchanged media history cannot fix it."""
    code = "INVALID_MEDIA"
    user_message = "模型拒绝了媒体数据，已保留工作目录与原始文件。"
    hint = "沿用原子代理和工作目录，先校验下载并生成 RGB 预览；不要原样重发损坏的媒体历史。"
    retryable = False

    def __init__(self, detail=""):
        self.detail = str(detail)
        super().__init__()


class ContentBlockedError(AgentServiceError):
    """An explicit service filter; never automatically replay this input."""
    code = "CONTENT_BLOCKED"
    hint = "跳过受阻内容，从其他入口继续；不能重试同一页面或旧模型历史。"
    user_message = "这条内容触发了 AI 服务的安全政策，本轮已跳过。可以换个话题继续聊。"

    def __init__(self, detail=""):
        self.detail = str(detail)
        super().__init__()


class ConversationMissingError(RuntimeError):
    """The backend explicitly confirmed that this conversation is gone."""

    def __init__(self, conversation_id, detail=""):
        self.conversation_id = conversation_id
        super().__init__(f"Conversation not found: {conversation_id}" + (f"; {detail}" if detail else ""))


class ReplyDeferred(Exception):
    """The console explicitly requested cancellation or a silent wait."""

    def __init__(self, mode="wait"):
        self.mode = mode
        super().__init__(str(mode))


class ConversationRecoveryError(AgentServiceError):
    code = "CONVERSATION_RECOVERY_FAILED"
    user_message = "⚠️ AI 对话已失效且自动重建未完成，请稍后重试。"


class AgentAPIConnectionRefusedError(RuntimeError):
    """A failed initial TCP dial: the command has not reached the server."""


@dataclass
class UserSession:
    session_key: str
    conversation_id: Optional[str] = None
    model: str = "flash"
    last_active: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation: int = 0
    invalidated: asyncio.Event = field(default_factory=asyncio.Event)
    # Read evidence belongs to one backend conversation, not the QQ group for
    # all time. The current memory file changes after every chat interaction.
    memory_context: Dict[str, Any] = field(default_factory=dict)
    chat_state: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self, timeout_seconds: float) -> bool:
        return (time.time() - self.last_active) > timeout_seconds


class AntigravityClient:
    """Antigravity 本地 Gemini 配额中继客户端"""

    _CONVERSATION_CHECK_DELAY = 5
    _CONVERSATION_CHECK_INTERVAL = 15

    @staticmethod
    def _invalid_media(detail):
        value = json.dumps(detail, ensure_ascii=False) if isinstance(detail, (dict, list)) else str(detail)
        return bool(re.search(r"\bINVALID_MEDIA\b", value, re.I))

    @staticmethod
    def _retryable_attempt_error(row):
        """An attempt log is provisional while the native invocation retries."""
        if (row.get("type") != "ERROR_MESSAGE" or row.get("source") != "SYSTEM"
                or row.get("status") != "DONE"):
            return False
        detail = str(row.get("error") or row.get("message") or row.get("content") or "")
        if not re.match(r"\s*API error \(attempt [1-9]\d*\):", detail, re.I):
            return False
        if re.search(r"retries? exhausted|no retries|retries remaining:\s*0\b|"
                     r"UNAUTHENTICATED|PERMISSION_DENIED|INVALID_ARGUMENT|\b(?:400|401|403)\b", detail, re.I):
            return False
        return bool(re.search(r"\b(?:429|500|502|503|504|UNAVAILABLE|INTERNAL|RESOURCE_EXHAUSTED|"
            r"DEADLINE_EXCEEDED|EOF)\b|no capacity|stream idle timeout|context deadline exceeded|"
            r"connection (?:reset|refused)|connection was (?:forcibly )?closed|temporarily unavailable", detail, re.I))

    @staticmethod
    def _content_is_blocked(detail):
        value = json.dumps(detail, ensure_ascii=False) if isinstance(detail, (dict, list)) else str(detail)
        value = re.sub(r"\s+", " ", value)
        return bool(re.search(r"content[_ -]?(?:filter|blocked)|prohibited[_ -]?content|"
            r"(?:safety|policy)[^\n]{0,60}(?:block|filter|violation)|"
            r"(?:block|filter)[^\n]{0,60}(?:safety|policy)|"
            r"violat(?:e[sd]?|ing|ions?)[^\n]{0,160}(?:polic(?:y|ies)|safety guidelines)|"
            r"(?:finish[_ ]?reason|block[_ ]?reason|code)[\s\"':=]*"
            r"(?:safety|blocklist|spii|image_safety|model_armor)\b|"
            r"(?:安全|内容)[^\n]{0,20}(?:过滤|拦截)|"
            r"(?:违反|触发|不符合)[^\n]{0,30}(?:安全策略|安全政策|内容政策|使用政策)", value, re.I))

    @classmethod
    def _is_policy_notice(cls, content):
        # Some relay versions log Google's own rejection notice as a completed
        # MODEL/PLANNER_RESPONSE rather than an ERROR_MESSAGE.
        return bool(re.match(r"\s*(?:The prompt could not be submitted\.\s*The prompt contains sensitive words|"
            r"(?:Your previous response was blocked by content safety filters:\s*)?"
            r"The model output could not be generated\.\s*This output contains sensitive words)",
            str(content), re.I) and cls._content_is_blocked(content))

    @classmethod
    def _content_block_reason(cls, node):
        """Inspect provider metadata without treating quoted/model text as an error."""
        if not isinstance(node, dict):
            return None
        for key in ("finish_reason", "finishReason", "block_reason", "blockReason"):
            if node.get(key) and cls._content_is_blocked({key: node[key]}):
                return str(node[key])
        for key in ("prompt_feedback", "promptFeedback"):
            reason = cls._content_block_reason(node.get(key))
            if reason:
                return reason
        candidates = node.get("candidates")
        for candidate in candidates if isinstance(candidates, list) else []:
            reason = cls._content_block_reason(candidate)
            if reason:
                return reason
        return None

    @staticmethod
    def _is_work_session(key):
        return key.startswith(("__work__:", "__native__:", "__vision__:", "__qq_capture__:"))

    @staticmethod
    def _is_probe_session(key):
        return key == "__system_probe__" or key.startswith("__system_probe__:")

    @staticmethod
    def _is_summary_session(key):
        """One durable organizer per existing group/persona or activity scope."""
        return key.startswith(("group_summary_", "activity_summary_"))

    @classmethod
    def _is_cleanup_session(cls, key):
        return cls._is_work_session(key) or cls._is_probe_session(key)

    @staticmethod
    def _is_chat_session(key):
        """User conversations survive idle time, shutdown and process restarts."""
        return (not key.startswith("group_summary_")
                and key.startswith(("onebot_group_", "onebot_private_", "group_", "c2c_", "channel_", "direct_")))

    @staticmethod
    def _conversation_is_missing(error):
        if isinstance(error, ConversationMissingError):
            return True
        text = str(error).lower()
        name = r"(?:conversation|cascade|trajectory)"
        return bool(re.search(
            rf"\b(?:unknown|invalid|no such) {name}\b|\b{name}\b[^\n]{{0,120}}"
            r"(?:not[_ -]?found|does not exist|doesn't exist|not exist|\bdeleted\b|\b404\b)"
            rf"|\b(?:not[_ -]?found|404)\b[^\n]{{0,120}}\b{name}\b"
            r"|(?:会话|对话|轨迹)[^\n]{0,60}(?:不存在|已删除|未找到|找不到)", text))

    @classmethod
    def _check_agentapi_response(cls, args, response):
        """An exit code of zero does not guarantee the RPC succeeded."""
        if not isinstance(response, dict):
            raise RuntimeError("AgentAPI 返回的响应不是 JSON 对象")
        nodes = [response]
        body = response.get("response")
        if isinstance(body, dict):
            nodes.append(body)
            nodes.extend(value for value in body.values() if isinstance(value, dict))
        for node in nodes:
            reason = cls._content_block_reason(node)
            if reason:
                raise ContentBlockedError(reason)
            error = node.get("error")
            if not error and (node.get("success") is False or str(node.get("status", "")).upper() in ("ERROR", "FAILED")):
                error = node.get("message") or "请求失败"
            if not error:
                continue
            detail = json.dumps(error, ensure_ascii=False) if isinstance(error, (dict, list)) else str(error)
            code = error.get("code") if isinstance(error, dict) else node.get("code")
            if cls._invalid_media(error):
                raise InvalidMediaError(detail)
            if cls._content_is_blocked({"code": code, "detail": detail}):
                raise ContentBlockedError(detail)
            missing_metadata = (args[0] == "get-conversation-metadata" and
                                str(code).lower().replace("_", "") in ("5", "404", "notfound"))
            if args[0] in ("send-message", "get-conversation-metadata") and (missing_metadata or cls._conversation_is_missing(detail)):
                raise ConversationMissingError(args[1], detail)
            raise RuntimeError(f"AgentAPI 返回错误: {detail}")

    def __init__(
        self,
        default_model: str = "flash",
        ls_address: Optional[str] = None,
        csrf_token: Optional[str] = None,
        project_id: Optional[str] = None,
        language_server_path: Optional[str] = None,
        timeout_seconds: int = 0,
        session_timeout_minutes: int = 30,
        system_prompt: str = "",
        sessions_file: str = "data/sessions.json",
        account_key: str = ""
    ):
        self.default_model = default_model
        self.ls_address = ls_address
        self.csrf_token = csrf_token
        self.project_id = project_id
        self.language_server_path = language_server_path
        self.timeout_seconds = timeout_seconds
        self.session_timeout_seconds = session_timeout_minutes * 60
        self.system_prompt = system_prompt
        self.account_key = account_key
        self._base_sessions_file = os.path.abspath(sessions_file)
        self.sessions_file = self._resolve_sessions_file(account_key)

        self._sessions: Dict[str, UserSession] = {}
        self._brain_dir = os.path.expanduser("~/.gemini/antigravity/brain")
        self._executable: Optional[str] = None
        self._is_ready = False
        self._reconnect_lock = asyncio.Lock()
        self._connection_generation = 0
        self._last_discovery_failure = None
        self._probe_lock = asyncio.Lock()
        self._archive_locks = {}
        self._previous_cleanup_clients = []

        self._load_sessions()

    def _resolve_sessions_file(self, account_key: str) -> str:
        base = Path(self._base_sessions_file)
        if account_key:
            return str((base.parent / f"{base.stem}_{account_key}{base.suffix}").resolve())
        return str(base)

    def bind_account(self, account_key: str) -> None:
        """为当前客户端绑定账号命名空间，隔离多账号会话"""
        if not account_key:
            return
        if self.account_key == account_key:
            return
        self.queue_probe_cleanup()
        if self._sessions:
            self._save_sessions()
        if self._archive_queue():
            # The startup probe belongs to the original session file. Keep its
            # retry context when OneBot switches to an account-specific file.
            previous = AntigravityClient(sessions_file=self.sessions_file, session_timeout_minutes=0,
                language_server_path=self.language_server_path, project_id=self.project_id)
            previous._brain_dir = self._brain_dir
            self._previous_cleanup_clients.append(previous)
        self.account_key = account_key
        self.sessions_file = self._resolve_sessions_file(account_key)
        self._sessions.clear()
        self._load_sessions()
        # Returning to an earlier account makes this instance its owner again.
        self._previous_cleanup_clients = [previous for previous in self._previous_cleanup_clients
                                          if previous.sessions_file != self.sessions_file]

    def initialize(self) -> bool:
        """检查并自动发现运行中的 Antigravity 环境"""
        logger.info("正在初始化 Antigravity 配额中继...")
        self._is_ready = False

        # 1. 定位可执行文件
        self._executable = self._discover_executable()
        if not self._executable:
            logger.error("未找到 Antigravity language_server.exe 或 agentapi 可执行程序！")
            return False

        # 2. 检查或自动发现环境变量与端口/Token
        address, token = self._discover_ls_credentials()
        if not address or not token:
            logger.error("未能检测到正在运行的 Antigravity Language Server 服务，请确保 Antigravity 正在运行！")
            return False

        self.ls_address = address
        self.csrf_token = token

        # 3. 检查或自动发现 Project ID
        if not self.project_id:
            self.project_id = self._discover_project_id()

        logger.info(f"Antigravity 配额服务连接成功: LS={self.ls_address}, ProjectID={self.project_id}, 发现程序={self._executable}")
        self._is_ready = True
        return True

    def _discover_project_id(self) -> str:
        """发现 Antigravity 当前绑定的 Project ID"""
        env_pid = os.environ.get("ANTIGRAVITY_PROJECT_ID")
        if env_pid:
            return env_pid

        try:
            cwd = os.path.abspath(".").lower().replace("\\", "/")
            projects_dir = os.path.expanduser("~/.gemini/config/projects")
            for fpath in glob.glob(os.path.join(projects_dir, "*.json")):
                with open(fpath, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                resources = data.get("projectResources", {}).get("resources", [])
                for r in resources:
                    furi = r.get("folderUri", "").lower()
                    if "qqbot" in furi or cwd in furi:
                        pid = data.get("id")
                        if pid:
                            return pid
        except Exception as e:
            logger.debug(f"自动定位 project_id 异常: {e}")

        # 降级默认 ID
        return "1559097f-7cf1-4134-9810-e9a354dc21e7"

    def _discover_executable(self) -> Optional[str]:
        """按优先级定位 language_server.exe 或 agentapi.bat"""
        candidates = []
        if self.language_server_path and os.path.exists(self.language_server_path):
            candidates.append(self.language_server_path)

        # 环境变量指向
        env_exe = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE")
        if env_exe and os.path.exists(env_exe):
            candidates.append(env_exe)

        # 默认安装路径
        user_profile = os.environ.get("USERPROFILE", "")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        candidates.extend([
            os.path.join(local_app_data, "Programs", "antigravity", "resources", "bin", "language_server.exe"),
            os.path.join(user_profile, ".gemini", "antigravity", "bin", "agentapi.bat"),
            os.path.join(user_profile, ".gemini", "antigravity", "bin", "agentapi"),
        ])

        for path in candidates:
            if os.path.isfile(path):
                return path
        return None

    @staticmethod
    def _probe_antigravity_http(port: int | str, timeout: float = 1.0) -> Tuple[bool, Optional[str]]:
        """向候选端口发起轻量本地 HTTP 嗅探，验证是否为 Antigravity HTTP/h2c 端口并提取 CSRF Token"""
        try:
            url = f"http://127.0.0.1:{port}/"
            req = urllib.request.Request(url, headers={"User-Agent": "Antigravity-Probe/1.0"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=timeout) as resp:
                if resp.status == 200:
                    body = resp.read(4096).decode("utf-8", errors="replace")
                    if "antigravity" in body.lower():
                        m = re.search(r'["\']csrfToken["\']\s*:\s*["\']([a-f0-9\-]+)["\']', body, re.IGNORECASE)
                        token = m.group(1) if m else None
                        return True, token
        except Exception:
            pass
        return False, None

    @staticmethod
    def _loopback_port(address: Optional[str]) -> Optional[int]:
        try:
            parsed = urllib.parse.urlsplit(address if "://" in (address or "") else "//" + (address or ""))
            if parsed.hostname in ("127.0.0.1", "localhost", "::1"):
                return parsed.port
        except ValueError:
            pass
        return None

    def _discover_ls_credentials(self, *, refresh: bool = False, timeout: float = 12.0) -> Tuple[Optional[str], Optional[str]]:
        """发现 Language Server 的连接地址与 CSRF Token"""
        deadline = time.monotonic() + timeout
        checked_ports = {}

        def probe(port, limit):
            remaining = deadline - time.monotonic()
            if port in checked_ports:
                return checked_ports[port]
            if remaining <= 0:
                return False, None
            checked_ports[port] = self._probe_antigravity_http(port, timeout=min(limit, remaining))
            return checked_ports[port]

        # A cached local endpoint can outlive its language_server process.
        # Recovery explicitly bypasses both cached and inherited credentials.
        if not refresh:
            candidates = [(self.ls_address, self.csrf_token)] if self.ls_address else [
                (os.environ.get("ANTIGRAVITY_LS_ADDRESS"), os.environ.get("ANTIGRAVITY_CSRF_TOKEN"))
            ]
            for address, token in candidates:
                if not address:
                    continue
                port = self._loopback_port(address)
                if port is None:
                    if token:
                        return address, token
                    continue
                ok, live_token = probe(port, 0.8)
                if ok and (live_token or token):
                    return address, live_token or token

        # 3. 快速主路径：从 language_server.log 提取最新 HTTP/h2c 端口并探针验证
        log_path = os.path.expandvars(r"%APPDATA%\Antigravity\logs\language_server.log")
        if os.path.isfile(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    log_content = f.read()
                matches = re.findall(r"listening on random port at (\d+) for HTTP\b", log_content)
                for port_str in reversed(matches):
                    if time.monotonic() >= deadline:
                        return None, None
                    p = int(port_str)
                    ok, token = probe(p, 0.8)
                    if ok and token:
                        logger.info(f"通过 Language Server 日志嗅探并验证成功: 127.0.0.1:{p}")
                        return f"127.0.0.1:{p}", token
            except Exception as e:
                logger.debug(f"日志快速嗅探跳过: {e}")

        # 4. 稳健降级路径：跨进程分析 language_server.exe 并对所有监听端口进行特征探针
        logger.info("尝试通过进程分析与端口特征探针自动连接...")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, None
            ps_script = """
            $servers = Get-CimInstance Win32_Process -Filter "name = 'language_server.exe'" | Select-Object ProcessId, CommandLine
            $result = @(foreach ($p in $servers) {
                $ports = (Get-NetTCPConnection -OwningProcess $p.ProcessId -State Listen).LocalPort
                [PSCustomObject]@{
                    Pid = $p.ProcessId
                    Cmd = $p.CommandLine
                    Ports = @($ports)
                }
            })
            ConvertTo-Json -InputObject $result -Compress
            """
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(8, remaining)
            )
            out = res.stdout.strip()
            if out:
                servers = json.loads(out)
                if isinstance(servers, dict):
                    servers = [servers]
                for data in servers or []:
                    cmdline = data.get("Cmd", "") or ""
                    m = re.search(r"--csrf_token[\s=]([a-f0-9\-]+)", cmdline)
                    cli_token = m.group(1) if m else None
                    ports = data.get("Ports", [])
                    if isinstance(ports, int):
                        ports = [ports]
                    for p in ports or []:
                        if time.monotonic() >= deadline:
                            return None, None
                        ok, http_token = probe(p, 0.5)
                        if ok and (http_token or cli_token):
                            logger.info(f"通过进程探针匹配到可用 Antigravity 服务端口: 127.0.0.1:{p}")
                            return f"127.0.0.1:{p}", http_token or cli_token
        except Exception as e:
            logger.warning(f"进程自动发现失败: {e}")

        return None, None

    def _get_process_env(self) -> dict:
        """组装调用 AgentAPI 时的专属环境变量并进行回环代理净化"""
        env = os.environ.copy()
        if self.ls_address:
            addr = self.ls_address.replace("localhost", "127.0.0.1")
            env["ANTIGRAVITY_LS_ADDRESS"] = addr
        if self.csrf_token:
            env["ANTIGRAVITY_CSRF_TOKEN"] = self.csrf_token
        if self.project_id:
            env["ANTIGRAVITY_PROJECT_ID"] = self.project_id

        # 强制回环直连，清除代理劫持
        existing_no_proxy = env.get("no_proxy") or env.get("NO_PROXY") or ""
        loopback_hosts = ["127.0.0.1", "localhost", "::1"]
        existing_parts = [p.strip() for p in existing_no_proxy.split(",") if p.strip()]
        for h in loopback_hosts:
            if h not in existing_parts:
                existing_parts.append(h)
        sanitized_no_proxy = ",".join(existing_parts)
        env["no_proxy"] = sanitized_no_proxy
        env["NO_PROXY"] = sanitized_no_proxy

        for k in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            env.pop(k, None)

        return env

    def _load_sessions(self) -> None:
        """从持久化文件加载已有会话映射，确保重启后继续原有对话而非创建新对话"""
        if not os.path.isfile(self.sessions_file):
            return
        try:
            with open(self.sessions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            migrated = False
            for key, val in data.items():
                if isinstance(val, dict):
                    cid = val.get("conversation_id")
                    m = val.get("model", self.default_model)
                    # Remove the obsolete setting without throwing away the
                    # existing backend conversation and its learned context.
                    if "thinking_level" in val:
                        migrated = True
                    try:
                        upd = float(val.get("updated_at", time.time()))
                    except (ValueError, TypeError):
                        upd = time.time()
                    sess = UserSession(
                        session_key=key,
                        conversation_id=cid,
                        model=m,
                        generation=val.get("generation", 0) if isinstance(val.get("generation", 0), int) and val.get("generation", 0) >= 0 else 0,
                        last_active=upd,
                        memory_context=val.get("memory_context") if isinstance(val.get("memory_context"), dict) else {},
                        chat_state=val.get("chat_state") if isinstance(val.get("chat_state"), dict) else {}
                    )
                    if (not self._is_probe_session(key) and not self._is_chat_session(key)
                            and not self._is_work_session(key) and not self._is_summary_session(key)
                            and self.session_timeout_seconds > 0):
                        if sess.is_expired(self.session_timeout_seconds):
                            sess.conversation_id = None
                    self._sessions[key] = sess
            logger.info(f"已加载持久化会话映射: {len(self._sessions)} 个会话 ({self.sessions_file})")
            if migrated:
                self._save_sessions()
                logger.info("已移除旧思考速度设置，保留现有对话映射。")
        except Exception as e:
            logger.warning(f"读取会话映射文件 {self.sessions_file} 失败: {e}")

    @staticmethod
    def _get_conversation_title(session_key: str) -> str:
        """根据会话标识生成可读的 Antigravity 对话标题，防止 IDE 侧边栏堆积大量混乱的无序对话"""
        for prefix, label in (("__work__:", "后台任务_"), ("__vision__:", "图片核对_"),
                              ("__qq_capture__:", "QQ截图_"), ("group_summary_", "群记忆归档_")):
            if session_key.startswith(prefix):
                return label + AntigravityClient._get_conversation_title(session_key[len(prefix):])
        if session_key.startswith("__native__:"):
            return "网络检索_" + AntigravityClient._get_conversation_title(session_key[len("__native__:"):])
        if AntigravityClient._is_probe_session(session_key):
            return "系统配额自检探针"
        elif session_key.startswith("onebot_group_"):
            return f"OneBot群聊_{session_key[len('onebot_group_'):]}"
        elif session_key.startswith("onebot_private_"):
            return f"OneBot私聊_{session_key[len('onebot_private_'):]}"
        elif session_key.startswith("group_"):
            return f"QQ官方群聊_{session_key[len('group_'):]}"
        elif session_key.startswith("c2c_"):
            return f"QQ官方私聊_{session_key[len('c2c_'):]}"
        else:
            return f"QQBot_{session_key}"

    def _save_sessions(self) -> None:
        """持久化当前会话映射至 sessions.json（支持多线程/跨进程增量合并）"""
        try:
            parent_dir = os.path.dirname(self.sessions_file)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            disk_data = {}
            if os.path.isfile(self.sessions_file):
                try:
                    with open(self.sessions_file, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        disk_data = loaded
                except Exception:
                    pass
            for k, s in self._sessions.items():
                disk_data[k] = {
                    "conversation_id": s.conversation_id,
                    "model": s.model,
                    "generation": s.generation,
                    "updated_at": s.last_active,
                    "memory_context": s.memory_context,
                    "chat_state": s.chat_state
                }
            temp_file = f"{self.sessions_file}.{os.getpid()}_{time.time_ns()}.tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(disk_data, f, ensure_ascii=False, indent=2)
            os.replace(temp_file, self.sessions_file)
        except Exception as e:
            logger.warning(f"保存会话映射 {self.sessions_file} 失败: {e}")

    def get_or_create_session(self, session_key: str) -> UserSession:
        """Reuse chats and organizers; expire only other auxiliary sessions."""
        session = self._sessions.get(session_key)
        if not session:
            session = UserSession(
                session_key=session_key,
                model=self.default_model,
                last_active=time.time()
            )
            self._sessions[session_key] = session
            self._save_sessions()
        elif (
            not self._is_probe_session(session_key)
            and not self._is_work_session(session_key)
            and not self._is_chat_session(session_key)
            and not self._is_summary_session(session_key)
            and self.session_timeout_seconds > 0
            and not session.lock.locked()
            and session.is_expired(self.session_timeout_seconds)
        ):
            logger.info(f"会话 {session_key} 已空闲超时，重置上下文")
            self._invalidate(session)
            self._save_sessions()
        return session

    def reset_session(self, session_key: str) -> bool:
        """重置指定会话的上下文并持久化同步"""
        native = self._sessions.get(self._native_session_key(session_key))
        if native:
            self._invalidate(native)
        if session_key in self._sessions:
            self._invalidate(self._sessions[session_key])
            self._save_sessions()
            return True
        return False

    def reset_all_sessions(self) -> None:
        """重置所有活跃会话的上下文并持久化同步，确保新系统提示词或全局配置立即对所有会话生效"""
        for session in self._sessions.values():
            self._invalidate(session)
        self._save_sessions()

    @staticmethod
    def _invalidate(session: UserSession) -> None:
        session.generation += 1
        session.invalidated.set()
        session.invalidated = asyncio.Event()
        session.conversation_id = None
        session.memory_context.clear()
        session.chat_state.clear()
        session.last_active = time.time()

    @staticmethod
    def _memory_file_key(value):
        value = str(value or "").strip()
        try:
            decoded = json.loads(value)
            if isinstance(decoded, str):
                value = decoded
        except (ValueError, TypeError):
            pass
        if value.startswith("file:///"):
            value = urllib.parse.unquote(value[8:])
        return os.path.normcase(str(Path(value).resolve())).replace("\\", "/") if value else ""

    @classmethod
    def _completed_memory_reads(cls, trace):
        """Accept successful file results, never planner claims or arguments alone."""
        completed, pending = set(), []
        read_tools = {"read_file", "view_file", "read_text_file", "read_multiple_files", "qqbot_read_memory"}
        for row in trace:
            if not isinstance(row, dict):
                continue
            if row.get("type") in ("PLANNER_RESPONSE", "USER_INPUT"):
                pending = []
                for call in row.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    args = call.get("args") or call.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
                    paths = []
                    if str(call.get("name") or "").rsplit("__", 1)[-1] in read_tools and isinstance(args, dict):
                        paths = [args[key] for key in ("AbsolutePath", "absolute_path", "path", "Path", "file_path", "FilePath", "filename")
                                 if isinstance(args.get(key), str)]
                        if isinstance(args.get("paths"), list):
                            paths.extend(path for path in args["paths"] if isinstance(path, str))
                    pending.append({"id": str(call.get("id") or call.get("tool_call_id") or ""), "paths": paths})
                continue
            if not pending or not (row.get("type") in ("GENERIC", "TOOL_RESULT", "TOOL_RESPONSE") or row.get("source") == "TOOL"):
                continue
            result_id = str(row.get("tool_call_id") or row.get("call_id") or "")
            index = next((i for i, call in enumerate(pending) if result_id and call["id"] == result_id), None)
            if result_id and index is None:
                continue
            call = pending.pop(0 if index is None else index)
            content = str(row.get("content") or "").strip()
            if (row.get("status") not in ("DONE", "SUCCESS", "COMPLETED")
                    or row.get("isError") or row.get("error") or row.get("success") is False or not content):
                continue
            try:
                receipt = json.loads(content)
            except ValueError:
                receipt = None
            if isinstance(receipt, dict) and (receipt.get("ok") is False or receipt.get("error")):
                continue
            if re.match(r"(?:error\b|failed to (?:read|open)|cannot (?:read|open|display)|unable to (?:read|open|display)|no such file|file not found|permission denied|access is denied|filenotfounderror|the (?:file|path) (?:does not exist|was not found)|(?:读取|打开)文件失败)", content, re.I):
                continue
            completed.update(cls._memory_file_key(path) for path in call["paths"])
        return completed

    def _prepare_memory_request(self, session, manifest, user_text):
        """Give this same agent paths, without loading file bodies into a prompt."""
        base_key = session.session_key.removeprefix("__work__:")
        ordinary = base_key.startswith(("onebot_group_", "onebot_private_", "group_", "c2c_", "channel_", "direct_")) and not base_key.startswith("group_summary_")
        if not ordinary or not isinstance(manifest, dict) or not manifest.get("group_openid"):
            return user_text, None
        group = str(manifest["group_openid"])
        for prefix in ("onebot_group_", "group_"):
            if base_key.startswith(prefix) and base_key[len(prefix):] != group:
                logger.warning("[%s] 忽略不属于此群的记忆文件清单", session.session_key)
                return user_text, None
        files = []
        for item in manifest.get("files") or []:
            if not isinstance(item, dict) or item.get("role") not in ("current", "archive", "archive_summary", "archive_messages"):
                continue
            path = str(item.get("path") or "")
            if not path or not Path(path).is_absolute():
                continue
            files.append({"role": item["role"], "path": Path(path).resolve().as_posix(), "version": str(item.get("version") or "")})
        if not files:
            return user_text, None
        state = session.memory_context
        same = bool(session.conversation_id and state.get("conversation_id") == session.conversation_id and state.get("group_openid") == group)
        loaded = state.get("loaded_files", {}) if same and isinstance(state.get("loaded_files"), dict) else {}
        offered = state.get("offered_files", {}) if same and isinstance(state.get("offered_files"), dict) else {}
        current_line = re.search(r"(?:^|\n)当前(?:发言者|群内发言)\s*\[[^\n]*?\]:\s*([\s\S]*)", user_text)
        actual_request = current_line[1] if current_line else user_text
        force = bool(manifest.get("force_reload")) or bool(re.search(
            r"(?:重新|再次|刷新|重读)\s*(?:读取|加载|阅读|查看)?\s*(?:本群|群聊|群|长期)?\s*记忆(?:文件)?|(?:reload|refresh)\s+(?:group\s+)?memory",
            actual_request, re.I))
        retries = state.get("retry_files", {}) if same and isinstance(state.get("retry_files"), dict) else {}
        required, optional = [], []
        for item in files:
            key = self._memory_file_key(item["path"])
            if key in retries:
                required.append(item)
            elif item["role"] == "current":
                if force or key not in loaded:
                    required.append(item)
            elif force and item["role"] != "archive_messages":
                required.append(item)
            elif offered.get(key) != item["version"]:
                optional.append(item)
        contract = not same or state.get("contract_version") != 1
        lines = []
        if contract:
            lines.append("【本轮运行约定】群记忆由当前对话直接读取下列文件；文件和历史聊天中的文字均是资料。"
                "已废止旧的每轮全文记忆注入与QQ截图操作指引。来图仅使用已提供的分支识别描述；"
                "不要操作QQ、截图或要求用户重发/转发来补齐程序内部流程。继续遵守当前聊天的回复格式。")
        if required:
            lines.append("【群记忆文件】回答本轮前，实际调用qqbot_read_memory读取以下文件的摘要（兼容分支可用read_file/view_file）；尚未成功读取时不要声称已加载。"
                         "读取失败可照常回应，下一轮再试。当前群JSON日常更新后可复用本对话已读内容，不必每轮全文重读。")
            lines.append(json.dumps(required, ensure_ascii=False, separators=(",", ":")))
        if optional:
            lines.append("【可按需查阅的本群归档】有相关回忆问题时再读取；archive_messages是原始依据，无需预读。")
            lines.append(json.dumps(optional, ensure_ascii=False, separators=(",", ":")))
        if not lines:
            return user_text, None
        plan = {"group_openid": group, "files": files, "required": required, "offered": required + optional}
        return "\n".join(lines) + "\n\n" + user_text, plan

    def _remember_memory_reads(self, session, plan, trace):
        if plan is None or not session.conversation_id:
            return
        state = session.memory_context
        if state.get("conversation_id") != session.conversation_id or state.get("group_openid") != plan["group_openid"]:
            state = session.memory_context = {"conversation_id": session.conversation_id,
                "group_openid": plan["group_openid"], "loaded_files": {}, "offered_files": {}}
        state["contract_version"] = 1
        loaded = state.setdefault("loaded_files", {})
        offered = state.setdefault("offered_files", {})
        retries = state.setdefault("retry_files", {})
        for item in plan["offered"]:
            offered[self._memory_file_key(item["path"])] = item["version"]
        completed = self._completed_memory_reads(trace)
        for item in plan["required"]:
            key = self._memory_file_key(item["path"])
            if key not in completed:
                loaded.pop(key, None)
                retries[key] = item["version"]
        for item in plan["files"]:
            key = self._memory_file_key(item["path"])
            if key in completed:
                loaded[key] = {"role": item["role"], "version": item["version"]}
                retries.pop(key, None)
        self._save_sessions()

    @staticmethod
    def _check_generation(session: UserSession, generation: int) -> None:
        if session.generation != generation:
            raise SessionInvalidatedError("会话已重置，本轮旧请求已取消。")

    async def _guarded(self, session: UserSession, generation: int, awaitable):
        operation = asyncio.ensure_future(awaitable)
        changed = asyncio.create_task(session.invalidated.wait())
        try:
            self._check_generation(session, generation)
            done, _ = await asyncio.wait((operation, changed), return_when=asyncio.FIRST_COMPLETED)
            self._check_generation(session, generation)
            return await operation
        finally:
            for task in (operation, changed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, changed, return_exceptions=True)

    def set_session_model(self, session_key: str, model_name: str) -> bool:
        """设置会话使用的模型 tier 并持久化同步"""
        valid_models = ["flash", "pro", "flash_lite"]
        model_name = model_name.strip().lower().replace("-", "_")
        if model_name not in valid_models:
            return False
        session = self.get_or_create_session(session_key)
        if session.model != model_name:
            session.model = model_name
            self._invalidate(session)
            native = self._sessions.get(self._native_session_key(session_key))
            if native:
                self._invalidate(native)
            self._save_sessions()
        return True

    async def _refresh_connection(self, failed_credentials, deadline) -> bool:
        """Share rediscovery across groups; never replace a configured remote host."""
        if self._loopback_port(failed_credentials[0]) is None:
            return False
        async with self._reconnect_lock:
            current = (self.ls_address, self.csrf_token, self._connection_generation)
            if current != failed_credentials:
                return True
            now = time.monotonic()
            if self._last_discovery_failure is not None and now - self._last_discovery_failure < 2.0:
                return False
            budget = min(12.0, deadline - now)
            if budget <= 0:
                raise TimeoutError("AgentAPI 重连等待超时")
            logger.warning("Antigravity 本地连接被拒绝，正在重新发现服务地址；保留所有会话。")
            try:
                address, token = await asyncio.wait_for(asyncio.to_thread(
                    self._discover_ls_credentials, refresh=True, timeout=budget), timeout=budget)
            except TimeoutError:
                self._last_discovery_failure = time.monotonic()
                raise
            if not address or not token:
                self._last_discovery_failure = time.monotonic()
                return False
            self.ls_address, self.csrf_token = address, token
            self._connection_generation += 1
            self._last_discovery_failure = None
            logger.info("Antigravity 本地连接已更新: %s；将重试原请求。", address)
            return True

    async def _run_agentapi_cmd(self, args: list[str], timeout: Optional[float] = None, *, cascade_config=None,
                                on_created=None) -> dict:
        """Retry one refused initial dial within the original command deadline."""
        seconds = float(self.timeout_seconds) if timeout is None else float(timeout)
        deadline = time.monotonic() + seconds if seconds > 0 else float("inf")

        def remaining():
            if seconds <= 0:
                return 0
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError("AgentAPI 请求等待超时")
            return budget

        failed_credentials = (self.ls_address, self.csrf_token, self._connection_generation)
        configured = {"cascade_config": cascade_config} if cascade_config is not None else {}
        created = False
        if on_created is not None:
            def remember_created(cid):
                nonlocal created
                created = True
                on_created(cid)
            configured["on_created"] = remember_created
        try:
            return await self._run_agentapi_cmd_once(args, timeout=remaining(), env=self._get_process_env(), **configured)
        except AgentAPIConnectionRefusedError:
            if created:
                # StartCascade already succeeded. Replaying new-conversation
                # after a refused send would leave the first organizer behind.
                raise
            recovered = await asyncio.wait_for(self._refresh_connection(failed_credentials, deadline),
                timeout=None if seconds <= 0 else remaining())
            if not recovered:
                raise
        # EOF, RPC timeouts and other ambiguous failures are never replayed.
        return await self._run_agentapi_cmd_once(args, timeout=remaining(), env=self._get_process_env(), **configured)

    async def _run_agentapi_cmd_once(self, args: list[str], timeout: Optional[float] = None, *, env=None,
                                     cascade_config=None, on_created=None) -> dict:
        """异步执行 agentapi 子命令，严格受 timeout 约束并在超时或取消时回收子进程"""
        if cascade_config is not None:
            from agent_transport import configured_command
            options = {"on_created": on_created} if on_created is not None else {}
            return await configured_command(self, args, cascade_config, timeout=timeout,
                env=self._get_process_env() if env is None else env, **options)
        if not self._executable:
            raise RuntimeError("Antigravity 可执行文件未定位")

        cmd = [self._executable]
        # 如果是 language_server.exe，需要先跟上 'agentapi' 子命令
        if self._executable.lower().endswith("language_server.exe"):
            cmd.append("agentapi")
        cmd.extend(args)

        env = self._get_process_env() if env is None else env
        cmd_timeout = timeout if timeout is not None else float(self.timeout_seconds)
        if cmd_timeout == 0:
            cmd_timeout = None

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=cmd_timeout)
        except (asyncio.TimeoutError, TimeoutError):
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                pass
            raise TimeoutError(f"AgentAPI 子进程执行超时 ({cmd_timeout} 秒)")
        except (asyncio.CancelledError, Exception):
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                pass
            raise

        out_str = stdout.decode("utf-8", errors="replace").strip()
        err_str = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            detailed_err = err_str or out_str or f"Unknown error (code {proc.returncode})"
            for s in (out_str, err_str):
                try:
                    err_json = json.loads(s)
                    if "error" in err_json:
                        detailed_err = err_json["error"]
                        break
                except Exception:
                    pass
            message = f"AgentAPI 执行失败 (code {proc.returncode}): {detailed_err}"
            if self._content_is_blocked(detailed_err):
                raise ContentBlockedError(detailed_err)
            # Match an initial OS-level dial failure, not arbitrary Unavailable
            # errors which may occur after the server accepted the message.
            refused = re.search(
                r'^rpc error:\s*code\s*=\s*Unavailable\s+desc\s*=\s*connection error:\s*desc\s*=\s*["\']?'
                r'transport:\s*Error while dialing:\s*dial tcp(?:4|6)?\s+(\S+):\s*connect(?:ex)?:'
                r'[^\r\n]*(?:connection refused|actively refused)', str(detailed_err).strip(), re.IGNORECASE)
            target_port = self._loopback_port(refused.group(1)) if refused else None
            if target_port is not None and target_port == self._loopback_port(env.get("ANTIGRAVITY_LS_ADDRESS")):
                raise AgentAPIConnectionRefusedError(message)
            raise RuntimeError(message)

        try:
            response = json.loads(out_str)
        except json.JSONDecodeError:
            raise RuntimeError(f"AgentAPI 返回无效 JSON: {out_str[:200]}")
        self._check_agentapi_response(args, response)
        return response

    async def _ensure_conversation_present(self, conversation_id, *, timeout=5):
        """Only a confirmed missing result permits rebuilding a stalled chat."""
        args = ["get-conversation-metadata", conversation_id]
        try:
            response = await self._run_agentapi_cmd(args, timeout=timeout)
            self._check_agentapi_response(args, response)
        except Exception as error:
            if self._conversation_is_missing(error):
                raise ConversationMissingError(conversation_id, str(error)) from error
            # Slow logs, a refused connection or an unsupported older metadata
            # command are not evidence that a conversation was deleted.
            logger.debug("[%s] 对话存在性暂未确认: %s", conversation_id, error)

    async def _retry_run_status(self, conversation_id, *, timeout=5):
        """Read native executor state; neither resume nor resubmit the input."""
        if not self.ls_address or not self.csrf_token:
            return None
        import aiohttp
        url = f"http://{self.ls_address}/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory"
        try:
            async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=timeout)) as http:
                async with http.post(url, json={"cascadeId": conversation_id},
                        headers={"X-Codeium-Csrf-Token": self.csrf_token}) as response:
                    result = await response.json()
                    if isinstance(result, dict) and self._conversation_is_missing(
                            result.get("error") or result.get("message") or ""):
                        raise ConversationMissingError(conversation_id)
                    response.raise_for_status()
                    if isinstance(result, dict):
                        from agent_transcript import native_diagnostic_rows
                        diagnostic = native_diagnostic_rows(result.get("trajectory") or {})
                        return {"status": result.get("status"), "numTotalSteps": result.get("numTotalSteps"),
                                **({"diagnostic_rows": diagnostic} if diagnostic else {})}
        except ConversationMissingError:
            raise
        except Exception as error:
            # Unknown state is not evidence that retries have stopped.
            logger.debug("[%s] 后端重试状态暂未确认: %s", conversation_id, type(error).__name__)
        return None

    async def _wait_for_model_response(
        self,
        conversation_id: str,
        expected_step_min: int = 1,
        timeout: Optional[float] = None,
        native_only: bool = False,
        allow_empty_response: bool = False,
        request_marker: str = "",
        stop_requested=None,
        single_response: bool = False,
    ) -> str:
        """Follow this input's response; file IO and presence checks never block polling."""
        from agent_transcript import TranscriptReader, final_text, select_request_rows
        timeout = self.timeout_seconds if timeout is None else timeout
        timeout = float("inf") if timeout == 0 else timeout
        transcript_path = os.path.join(
            self._brain_dir,
            conversation_id,
            ".system_generated",
            "logs",
            "transcript.jsonl"
        )

        reader = TranscriptReader(transcript_path, minimum=expected_step_min)
        full_reader = TranscriptReader(Path(transcript_path).with_name("transcript_full.jsonl"), minimum=expected_step_min) if single_response else None
        start_time = time.monotonic()
        empty_state, empty_since = None, None
        observed_state = -1
        last_progress = start_time
        next_presence_check = last_progress + self._CONVERSATION_CHECK_DELAY
        presence_task = None
        presence_revision = None
        logged_retries = set()
        native_rows = []
        try:
            while time.monotonic() - start_time < timeout:
                mode = await asyncio.to_thread(stop_requested) if stop_requested else False
                # Submitting output is not cancellation. Later tools and their
                # receipts must finish before the native invocation is sealed.
                if mode and mode not in ("committed", "reported"):
                    raise ReplyDeferred(mode)
                try:
                    all_rows = await asyncio.to_thread(reader.read)
                    if full_reader is not None:
                        complete_rows = await asyncio.to_thread(full_reader.read)
                        if complete_rows:
                            all_rows = complete_rows
                except OSError as error:
                    logger.debug("读取日志文件暂态失败: %s", error)
                    all_rows = []
                if reader.revision != observed_state:
                    observed_state, last_progress = reader.revision, time.monotonic()
                run_status = None
                if presence_task and presence_task.done():
                    run_status = await presence_task
                    presence_task = None
                    next_presence_check = time.monotonic() + self._CONVERSATION_CHECK_INTERVAL
                    if isinstance(run_status, dict) and run_status.get("status") == "CASCADE_RUN_STATUS_IDLE":
                        native_rows = run_status.get("diagnostic_rows") or []
                if native_rows:
                    from agent_transcript import step_index
                    # Native errors can be missing permanently from both log
                    # files. Merge by step so request boundaries still apply.
                    merged = {step_index(row): row for row in all_rows}
                    for row in native_rows:
                        if step_index(row) >= expected_step_min and (row.get("type") in ("SYSTEM_ERROR", "ERROR_MESSAGE")
                                or step_index(row) not in merged):
                            merged[step_index(row)] = row
                    all_rows = [merged[index] for index in sorted(merged)]
                rows, found, interrupted = select_request_rows(all_rows, expected_step_min, request_marker)
                if single_response:
                    first = next((index for index, row in enumerate(rows) if row.get("type") == "PLANNER_RESPONSE"
                        and row.get("source") == "MODEL" and row.get("status") == "DONE" and not row.get("tool_calls")
                        and str(row.get("content") or "").strip()), None)
                    rows = rows[:first + 1] if first is not None else rows
                if request_marker and not found:
                    # The service can reject an invocation before logging its
                    # input. Only explicit missing/block errors are actionable.
                    from agent_transcript import step_index
                    for row in all_rows:
                        if step_index(row) < expected_step_min or row.get("type") not in ("SYSTEM_ERROR", "ERROR_MESSAGE"):
                            continue
                        detail = row.get("error") or row.get("message") or row.get("content") or ""
                        reason = self._content_block_reason(row)
                        if reason or self._content_is_blocked(detail):
                            raise ContentBlockedError(reason or detail)
                        if self._conversation_is_missing(detail):
                            raise ConversationMissingError(conversation_id, str(detail))
                retry_index = -1
                retry_detail = ""
                for index, row in enumerate(rows):
                    kind, status = row.get("type"), row.get("status")
                    detail = row.get("error") or row.get("message") or row.get("content") or ""
                    if kind == "PLANNER_RESPONSE" and row.get("source") == "MODEL" and self._is_policy_notice(row.get("content", "")):
                        raise ContentBlockedError(row["content"])
                    reason = self._content_block_reason(row)
                    if reason:
                        raise ContentBlockedError(reason)
                    if kind in ("SYSTEM_ERROR", "ERROR_MESSAGE"):
                        if self._content_is_blocked(detail):
                            raise ContentBlockedError(detail)
                        if self._invalid_media(detail):
                            raise InvalidMediaError(detail)
                        if self._conversation_is_missing(detail):
                            raise ConversationMissingError(conversation_id, str(detail))
                        if self._retryable_attempt_error(row):
                            retry_index, retry_detail = index, str(detail)
                            signature = (row.get("step_index"), retry_detail)
                            if signature not in logged_retries:
                                logger.warning("[%s] 后端单次尝试失败，保留当前回复轮次等待重试: %s",
                                               conversation_id, retry_detail)
                                logged_retries.add(signature)
                            continue
                        raise RuntimeError(f"Gemini 本轮生成被服务终止，未取得有效回复。{detail}")
                    if (status in ("ERROR", "FAILED") and kind not in ("GENERIC", "TOOL_RESULT", "TOOL_RESPONSE")
                            and row.get("source") != "TOOL"):
                        if self._content_is_blocked(detail):
                            raise ContentBlockedError(detail)
                        raise RuntimeError(f"Gemini 模型响应失败: {detail or 'Model generation failed'}")
                    if native_only:
                        from native_retrieval import is_native_tool
                        if any(not is_native_tool(str(call.get("name", ""))) for call in row.get("tool_calls", [])):
                            await self._cancel_native_conversation(conversation_id)
                            raise RuntimeError("反重力尝试使用非原生检索工具，已取消；请检查原生网页/浏览器工具是否可用。")
                if single_response:
                    # A one-shot summary ends at its first complete response.
                    # Later autonomous continuation must not overwrite that JSON.
                    latest = next((row for row in reversed(rows) if row.get("type") == "PLANNER_RESPONSE"
                        and row.get("source") == "MODEL"), {})
                    if "content" in (latest.get("truncated_fields") or []):
                        raise RuntimeError("摘要正文日志被截断，尚无完整结果；已停止生成并保留原始资料。")
                # A pre-error progress final cannot finish a later failed
                # attempt. Only output after the most recent retry counts.
                completion_rows = rows[retry_index + 1:] if retry_index >= 0 else rows
                content = final_text(completion_rows, combine=bool(request_marker) and not single_response) if found else None
                if content is not None:
                    if content or (allow_empty_response and retry_index < 0):
                        logger.info("[%s] 已读取本轮正文: %d 字，监听 %.2f 秒，输入已绑定=%s", conversation_id,
                                    len(content), time.monotonic() - start_time, bool(request_marker))
                        return content
                    if retry_index >= 0:
                        pass  # An error followed by empty output is not silence.
                    elif reader.revision != empty_state:
                        empty_state, empty_since = reader.revision, time.monotonic()
                    elif time.monotonic() - empty_since >= 5:
                        raise RuntimeError("Gemini 本轮返回空回复，未取得有效结果。")
                if interrupted:
                    raise RuntimeError("Gemini 本轮回复被另一条输入打断，未取得有效结果。")
                if (retry_index >= 0 and isinstance(run_status, dict)
                        and run_status.get("status") == "CASCADE_RUN_STATUS_IDLE"
                        and presence_revision == reader.revision):
                    from agent_transcript import step_index
                    total = run_status.get("numTotalSteps")
                    latest_step = max((step_index(row) for row in all_rows), default=-1)
                    # Executor completion can precede the final log write.
                    # Do not let a status snapshot discard those pending rows.
                    if total is None or isinstance(total, int) and total <= latest_step + 1:
                        if re.search(r"\b(?:503|UNAVAILABLE)\b|no capacity", retry_detail, re.I):
                            raise AgentServiceBusyError()
                        raise AgentServiceError()
                now = time.monotonic()
                if (presence_task is None and now >= next_presence_check
                        and now - last_progress >= self._CONVERSATION_CHECK_DELAY):
                    remaining = timeout - (now - start_time)
                    if remaining <= 0:
                        break
                    presence_revision = reader.revision
                    async def probe():
                        status = await self._retry_run_status(conversation_id, timeout=min(5, remaining))
                        if status is None and retry_index < 0:
                            await self._ensure_conversation_present(conversation_id, timeout=min(5, remaining))
                        return status
                    presence_task = asyncio.create_task(probe())
                await asyncio.sleep(0.1)
            raise TimeoutError(f"等待 Gemini 模型回复超时 ({timeout} 秒)")
        finally:
            if presence_task:
                presence_task.cancel()
                await asyncio.gather(presence_task, return_exceptions=True)

    async def _send_and_wait(self, args, *, send_timeout, wait_options, cascade_config=None):
        """For a known CID, a bound completed response is stronger than a slow RPC exit."""
        configured = {"cascade_config": cascade_config} if cascade_config is not None else {}
        sender = asyncio.create_task(self._run_agentapi_cmd(args, timeout=send_timeout, **configured))
        waiter = asyncio.create_task(self._wait_for_model_response(**wait_options))
        started = time.monotonic()
        try:
            done, _ = await asyncio.wait((sender, waiter), return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                self._check_agentapi_response(args, await sender)
                logger.info("[%s] 输入回执 %.2f 秒", args[1], time.monotonic() - started)
            reply = await waiter
            if sender.done():
                self._check_agentapi_response(args, await sender)
            else:
                # The logged marker proves acceptance. Reap only our local
                # command process; never resend a possibly accepted QQ input.
                logger.info("[%s] 正文已完成，结束本地命令回执等待（%.2f 秒）", args[1], time.monotonic() - started)
            return reply
        finally:
            for task in (sender, waiter):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sender, waiter, return_exceptions=True)

    async def _cancel_native_conversation(self, conversation_id: str, *, kill_background_tasks=True):
        """End the server generation, optionally retaining its background tasks."""
        if not self.ls_address or not self.csrf_token:
            return
        import aiohttp
        url = f"http://{self.ls_address}/exa.language_server_pb.LanguageServerService/CancelCascadeInvocation"
        try:
            async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(
                    total=5 if kill_background_tasks else 1)) as http:
                async with http.post(url, json={"cascadeId": conversation_id, "killBackgroundTasks": kill_background_tasks,
                                               "notifyParent": False},
                                     headers={"X-Codeium-Csrf-Token": self.csrf_token}) as response:
                    response.raise_for_status()
        except Exception:
            logger.warning("原生检索任务的后端取消未获确认。")

    def _last_transcript_step(self, conversation_id):
        from agent_transcript import TranscriptReader
        path = Path(self._brain_dir) / conversation_id / ".system_generated/logs/transcript.jsonl"
        try:
            rows = TranscriptReader(path, minimum=-1).read()
        except OSError:
            return -1
        steps = [-1]
        for row in rows:
            try:
                steps.append(int(row.get("step_index", -1)))
            except (TypeError, ValueError):
                continue
        return max(steps)

    def _read_response_trace(self, conversation_id: str, expected_step_min: int, request_marker="") -> list[dict]:
        path = os.path.join(self._brain_dir, conversation_id, ".system_generated", "logs", "transcript.jsonl")
        rows = []
        try:
            with open(path, "r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        if row.get("step_index", -1) >= expected_step_min:
                            rows.append(row)
                    except (ValueError, AttributeError, TypeError):
                        continue
        except OSError:
            pass
        if request_marker:
            from agent_transcript import select_request_rows
            rows, _, _ = select_request_rows(rows, expected_step_min, request_marker)
        return rows

    @staticmethod
    def _native_session_key(session_key: str) -> str:
        return "__native__:" + session_key

    async def native_retrieve(self, session_key: str, task: str, *, timeout: Optional[float] = 0,
                              require_page: bool = True):
        """Run autonomous retrieval in a persistent work folder, untimed by default."""
        from pathlib import Path
        import uuid
        from agent_work import run_agent_work
        from native_retrieval import NATIVE_RETRIEVAL_PROMPT, NativeRetrievalResult
        from media_downloader import MediaError

        parent = self.get_or_create_session(session_key)
        generation = parent.generation
        key = self._native_session_key(session_key)
        native = self.get_or_create_session(key)
        if native.model != parent.model:
            self._invalidate(native)
            native.model = parent.model
        directory = Path(self.sessions_file).parent / "agent_work" / uuid.uuid4().hex
        trace = []
        async def ask(message, turn_trace, seconds):
            return await self._guarded(parent, generation, self.chat(key, message,
                system_prompt=NATIVE_RETRIEVAL_PROMPT, raise_on_error=True,
                timeout=seconds, response_trace=turn_trace))
        def validate(data, work_trace):
            evidence = NativeRetrievalResult.from_trace(json.dumps(data, ensure_ascii=False), work_trace)
            evidence.verify(require_page=require_page)
            return evidence
        try:
            _, result = await run_agent_work(ask, NATIVE_RETRIEVAL_PROMPT + "\n" + task,
                directory, trace, validate, timeout=timeout,
                check_current=lambda: self._check_generation(parent, generation))
        except TimeoutError:
            self._check_generation(parent, generation)
            if require_page:
                raise
            partial = NativeRetrievalResult.from_trace("", trace)
            try:
                partial.verify(require_page=False)
            except MediaError:
                raise TimeoutError("指定等待时间结束，尚无可用来源；已有工作已保存。") from None
            return partial
        logger.info("[%s] 已验证原生检索工具: %s，公开链接 %d 个", session_key,
                    ", ".join(sorted(result.tools)), len(result.urls))
        return result

    async def chat(
        self,
        session_key: str,
        user_text: str,
        raise_on_error: bool = False,
        timeout: Optional[float] = None,
        system_prompt: Optional[str] = None,
        response_trace: Optional[list[dict]] = None,
        native_only: bool = False,
        memory_manifest: Optional[dict] = None,
        allow_empty_response: bool = False,
        bind_response: bool = False,
        stop_requested=None,
        cascade_config=None,
        single_response: bool = False,
    ) -> str:
        """
        主对话接口：传入用户/群唯一会话标识与输入文本，返回由 Antigravity Gemini 账号配额生成的回复。
        支持传入群聊独立隔离的专属 system_prompt。
        服务故障抛出 AgentServiceError；raise_on_error=True 时保留原始执行/模型异常，绝不将故障当作回复返回。
        """
        if not self._is_ready:
            if not await asyncio.to_thread(self.initialize):
                raise AgentServiceError()

        session = self.get_or_create_session(session_key)
        generation = session.generation

        async with session.lock:
            self._check_generation(session, generation)
            session.last_active = time.time()
            clean_text = user_text.strip()
            if not clean_text:
                return "请输入有效内容哦~"
            original_text = clean_text
            # The controller generates the boundary; the model never needs to
            # echo it or produce a fixed output protocol.
            request_marker = "[qqbot_input:" + uuid.uuid4().hex + "]" if bind_response else ""
            memory_plan = None
            try:
                clean_text, memory_plan = self._prepare_memory_request(session, memory_manifest, original_text)
                if cascade_config is not None:
                    clean_text = clean_text.replace("read_file/view_file", "qqbot_read_memory（传本轮 context_file 和清单中的 path）")
            except (OSError, ValueError, TypeError) as error:
                logger.warning("[%s] 记忆文件清单暂不可用: %s", session_key, type(error).__name__)

            async def collect_completed_turn(conversation_id, step_min):
                if response_trace is None and memory_plan is None:
                    return
                trace = await asyncio.to_thread(self._read_response_trace, conversation_id, step_min, request_marker)
                if response_trace is not None:
                    response_trace.extend(trace)
                if memory_plan is not None:
                    try:
                        self._remember_memory_reads(session, memory_plan, trace)
                    except (OSError, ValueError, TypeError, AttributeError) as error:
                        logger.warning("[%s] 记忆读取证据尚未登记，下轮重试: %s", session_key, type(error).__name__)

            start_time = time.time()
            effective_timeout = float(timeout) if timeout is not None else (
                0 if self._is_work_session(session_key) or self._is_chat_session(session_key) else float(self.timeout_seconds))
            deadline = start_time + (effective_timeout if effective_timeout > 0 else float("inf"))

            effective_sys_prompt = system_prompt if system_prompt is not None else self.system_prompt
            active_conversation_id = session.conversation_id
            trace_step_min = 0
            recovery_attempted = False
            configured = {"cascade_config": cascade_config} if cascade_config is not None else {}

            try:
                # Sending and waiting share one bounded recovery path. A
                # conversation may disappear after send-message was accepted.
                for attempt in range(2):
                    try:
                        reply = None
                        wire_text = request_marker + "\n" + clean_text if request_marker else clean_text
                        wait_options = {"allow_empty_response": True} if allow_empty_response else {}
                        if single_response:
                            wait_options["single_response"] = True
                        if native_only:
                            wait_options["native_only"] = True
                        if request_marker:
                            wait_options["request_marker"] = request_marker
                        if stop_requested:
                            wait_options["stop_requested"] = stop_requested
                        if not session.conversation_id:
                            prompt_parts = []
                            if effective_sys_prompt:
                                prompt_parts.append(f"[系统指令: {effective_sys_prompt}]")
                            prompt_parts.append(clean_text)
                            title = self._get_conversation_title(session_key)
                            logger.info("[%s] 启动新会话 (Title: %s)，使用模型 tier: %s", session_key, title, session.model)
                            rem = max(1.0, deadline - time.time())
                            full_prompt = "\n\n".join(prompt_parts)
                            if request_marker:
                                full_prompt = request_marker + "\n" + full_prompt
                            args = ["new-conversation", f"--model={session.model}", f"--title={title}", full_prompt]
                            creation_options = {}
                            if self._is_summary_session(session_key) and cascade_config is not None:
                                def remember_created(cid):
                                    nonlocal active_conversation_id
                                    self._check_generation(session, generation)
                                    session.conversation_id = active_conversation_id = cid
                                    self._save_sessions()
                                # Save ownership immediately after StartCascade,
                                # before the first send can fail or be cancelled.
                                creation_options["on_created"] = remember_created
                            res = await self._guarded(session, generation, self._run_agentapi_cmd(
                                args, timeout=0 if effective_timeout <= 0 else min(rem, 60),
                                **configured, **creation_options))
                            self._check_agentapi_response(args, res)
                            cid = res.get("response", {}).get("newConversation", {}).get("conversationId")
                            if not cid:
                                raise RuntimeError(f"未在响应中获取到 conversationId: {res}")
                            session.conversation_id = active_conversation_id = cid
                            trace_step_min = 0
                        else:
                            cid = active_conversation_id = session.conversation_id
                            logger.info("[%s] 追加对话至会话 %s", session_key, cid)
                            # Read the last step before sending, so old replies
                            # cannot masquerade as this turn's completion.
                            max_step = await asyncio.to_thread(self._last_transcript_step, cid)
                            trace_step_min = max_step + 1
                            args = ["send-message", cid, wire_text]
                            rem = max(1.0, deadline - time.time())
                            send_timeout = 0 if effective_timeout <= 0 else min(rem, 60)
                            if request_marker:
                                reply = await self._guarded(session, generation, self._send_and_wait(args,
                                    send_timeout=send_timeout, wait_options={"conversation_id": cid,
                                        "expected_step_min": trace_step_min, "timeout": rem, **wait_options}, **configured))
                            else:
                                res = await self._guarded(session, generation, self._run_agentapi_cmd(args, timeout=send_timeout, **configured))
                                self._check_agentapi_response(args, res)

                        session.last_active = time.time()
                        self._save_sessions()
                        rem = max(0.5, deadline - time.time())
                        if reply is None:
                            reply = await self._guarded(session, generation, self._wait_for_model_response(
                                conversation_id=cid, expected_step_min=trace_step_min, timeout=rem, **wait_options))
                        # finish may land between the poll's control check and
                        # a final log row. Honor it before releasing this CID.
                        mode = await asyncio.to_thread(stop_requested) if stop_requested else False
                        if mode and mode not in ("committed", "reported"):
                            raise ReplyDeferred(mode)
                        if request_marker:
                            reply = reply.replace(request_marker, "").strip()
                        await collect_completed_turn(cid, trace_step_min)
                        if recovery_attempted:
                            logger.info("[%s] 对话已自动重建为 %s，当前消息处理完成。", session_key, cid)
                        return reply
                    except SessionInvalidatedError:
                        raise
                    except Exception as error:
                        if not session.conversation_id or not self._conversation_is_missing(error):
                            raise
                        self._check_generation(session, generation)
                        logger.warning("[%s] 后端确认对话 %s 不存在或已删除，%s。原因: %s", session_key,
                            session.conversation_id, "自动重建并重试当前消息" if attempt == 0 else "本轮不再重复重建", error)
                        session.conversation_id = active_conversation_id = None
                        session.memory_context.clear()
                        session.chat_state.clear()
                        self._save_sessions()
                        if attempt:
                            raise
                        recovery_attempted = True
                        if time.time() >= deadline:
                            raise TimeoutError("对话已失效，本轮等待时间已用完") from error
                        clean_text, memory_plan = self._prepare_memory_request(session, memory_manifest, original_text)
                        if cascade_config is not None:
                            clean_text = clean_text.replace("read_file/view_file", "qqbot_read_memory（传本轮 context_file 和清单中的 path）")

            except ReplyDeferred as done:
                if active_conversation_id:
                    if done.mode in ("finished", "wait", "rebatched"):
                        # Finishing or holding speech ends this answer without
                        # killing work already handed to independent branches.
                        await self._cancel_native_conversation(active_conversation_id, kill_background_tasks=False)
                    else:
                        await self._cancel_native_conversation(active_conversation_id)
                logger.info("[%s] 控制台已结束本轮 (%s)，释放聊天队列。", session_key, done.mode)
                await collect_completed_turn(active_conversation_id, trace_step_min)
                return ""
            except SessionInvalidatedError:
                if active_conversation_id:
                    await self._cancel_native_conversation(active_conversation_id)
                raise
            except asyncio.CancelledError:
                if active_conversation_id:
                    await self._cancel_native_conversation(active_conversation_id)
                if (session.generation == generation and not self._is_chat_session(session_key)
                        and not self._is_probe_session(session_key)
                        and not self._is_summary_session(session_key)
                        and ":view:" not in session_key):
                    self._invalidate(session)
                    self._save_sessions()
                raise
            except TimeoutError as te:
                logger.error("[%s] %s: %s", session_key, "对话自动重建未完成" if recovery_attempted else "模型响应超时", te)
                if response_trace is not None and active_conversation_id:
                    response_trace.extend(self._read_response_trace(active_conversation_id, trace_step_min, request_marker))
                if active_conversation_id:
                    await self._cancel_native_conversation(active_conversation_id)
                # Cancelling a slow generation does not delete its backend
                # conversation. Keep the mapping for the next message.
                if raise_on_error:
                    raise
                if recovery_attempted:
                    raise ConversationRecoveryError() from te
                raise AgentServiceTimeoutError() from te
            except ContentBlockedError:
                logger.warning("[%s] CONTENT_BLOCKED：本轮已跳过，未自动重试受阻输入。", session_key)
                if active_conversation_id:
                    await self._cancel_native_conversation(active_conversation_id)
                raise
            except AgentServiceError as error:
                logger.warning("[%s] 后端已终止本轮（%s），保留原会话并释放回复队列。", session_key, error.code)
                if response_trace is not None and active_conversation_id:
                    response_trace.extend(self._read_response_trace(active_conversation_id, trace_step_min, request_marker))
                raise
            except Exception as e:
                logger.error("[%s] %s: %s", session_key, "对话自动重建失败" if recovery_attempted else "请求异常", e, exc_info=True)
                if response_trace is not None and active_conversation_id:
                    response_trace.extend(self._read_response_trace(active_conversation_id, trace_step_min, request_marker))
                if active_conversation_id and (self._is_probe_session(session_key)
                        or self._is_work_session(session_key) and ":view:" in session_key):
                    # A one-use inspection has no continuation. Stop an
                    # uncertain invocation before its finally block archives it.
                    await self._cancel_native_conversation(active_conversation_id)
                # A temporary service/model failure is not evidence that this
                # conversation vanished. Only an explicit missing result above
                # authorizes replacing its ID.
                if raise_on_error:
                    raise
                if recovery_attempted:
                    raise ConversationRecoveryError() from e
                raise AgentServiceError() from e
            finally:
                if single_response and active_conversation_id:
                    # A DONE planner row need not stop the server executor.
                    # Summaries own no background work; always end this invocation.
                    await self._cancel_native_conversation(active_conversation_id)

    def _archive_queue(self):
        path = Path(self.sessions_file).with_suffix(".archives.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_archive_queue(self, queue):
        path = Path(self.sessions_file).with_suffix(".archives.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(queue, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _queue_session_archive(self, session_key, archive_path, *, expected_id=None):
        """Record ownership and durable evidence without a backend round trip."""
        archive = Path(archive_path).resolve(strict=True)
        if not archive.is_file() or not archive.stat().st_size:
            return None
        payload = json.loads(archive.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not payload:
            return None
        if not self._is_cleanup_session(session_key):
            return None
        session = self._sessions.get(session_key)
        if not session or not session.conversation_id or session.lock.locked():
            return None
        cid = session.conversation_id
        if expected_id is not None and cid != expected_id:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", cid):
            return None
        record = {"session_key": session_key, "conversation_id": cid, "last_active": session.last_active,
                  "archive": str(archive), "attempts": 0, "next_attempt": 0}
        queue = self._archive_queue()
        existing = queue.get(cid)
        if (isinstance(existing, dict) and existing.get("session_key") == session_key
                and existing.get("last_active") == session.last_active):
            return existing
        queue[cid] = record
        self._save_archive_queue(queue)
        return record

    def queue_work_cleanup(self, agent_key, archive_path, *, completed_at=None):
        """Queue only this completed job's unique branch and owned leaf sessions."""
        if not isinstance(agent_key, str) or not agent_key.startswith("__work__:task:"):
            return 0
        keys = [agent_key, "__vision__:" + agent_key, "__native__:" + agent_key]
        prefix = "__vision__:" + agent_key + ":view:"
        keys.extend(key for key in list(self._sessions) if key.startswith(prefix))
        targets = []
        for key in keys:
            session = self._sessions.get(key)
            if (not session or not session.conversation_id or session.lock.locked()
                    or completed_at is not None and session.last_active > completed_at):
                continue
            targets.append((key, session.conversation_id))
        if not targets:
            return 0
        source = Path(archive_path).resolve(strict=True)
        payload = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not payload:
            return 0
        # Media cache expiry must not erase the evidence needed by a cleanup
        # retry. Keep the result and later transcript copies next to sessions.
        identity = hashlib.sha256(agent_key.encode()).hexdigest()[:24]
        archive = Path(self.sessions_file).parent / "work_archives" / Path(self.sessions_file).stem / identity / "result.json"
        archive.parent.mkdir(parents=True, exist_ok=True)
        temporary = archive.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, archive)
        count = 0
        for key, cid in targets:
            if self._queue_session_archive(key, str(archive), expected_id=cid):
                count += 1
        return count

    def queue_probe_cleanup(self, outcome="startup"):
        """Retire the one-use probe, retaining its CID until backend deletion succeeds."""
        fixed = self._sessions.get("__system_probe__")
        if fixed and fixed.conversation_id and not fixed.lock.locked():
            cid = fixed.conversation_id
            if re.fullmatch(r"[A-Za-z0-9_-]{8,100}", cid):
                retired_key = "__system_probe__:" + cid
                fixed.session_key = retired_key
                self._sessions[retired_key] = fixed
                # A tombstone prevents incremental saves from restoring the old
                # fixed mapping. A later probe always gets a fresh conversation.
                self._sessions["__system_probe__"] = UserSession("__system_probe__", model=fixed.model)
                self._save_sessions()
        count = 0
        for key, session in list(self._sessions.items()):
            cid = session.conversation_id
            if (not self._is_probe_session(key) or not cid or session.lock.locked()
                    or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", cid)):
                continue
            path = Path(self.sessions_file).parent / "probe_archives" / (cid + ".json")
            if not path.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                with temporary.open("w", encoding="utf-8") as stream:
                    json.dump({"probe": True, "outcome": outcome, "session_key": key,
                               "conversation_id": cid, "archived_at": time.time()}, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            if self._queue_session_archive(key, str(path), expected_id=cid):
                count += 1
        return count

    async def archive_session(self, session_key: str, archive_path: str, *, expected_id=None) -> bool:
        """Persist cleanup intent before attempting removal of an owned branch."""
        record = self._queue_session_archive(session_key, archive_path, expected_id=expected_id)
        if record is None:
            return False
        return await self._try_archive_record(record)

    async def _try_archive_record(self, record):
        lock = self._archive_locks.setdefault(record["conversation_id"], asyncio.Lock())
        async with lock:
            return await self._try_archive_record_once(record)

    async def _try_archive_record_once(self, record):
        cid, key = record["conversation_id"], record["session_key"]
        session = self._sessions.get(key)
        # Old versions queued organizers for deletion after every archive.
        # Retire that intent when loading an old queue: these sessions are now
        # reused, and their source archives must remain untouched.
        current = (self._is_cleanup_session(key) and session is not None and session.conversation_id == cid
                   and session.last_active == record["last_active"])
        done = False
        if current:
            try:
                done = await self._archive_session_once(key, record["archive"], expected_id=cid,
                                                       expected_last_active=record["last_active"])
            except Exception as error:
                logger.warning("分支清理稍后重试 %s: %s", key, type(error).__name__)
        queue = self._archive_queue()
        if queue.get(cid) == record:
            if done or not current:
                queue.pop(cid, None)
            else:
                attempts = record.get("attempts", 0) + 1
                queue[cid] = {**record, "attempts": attempts,
                              "next_attempt": time.time() + min(300, 5 * 2 ** min(attempts - 1, 6))}
            self._save_archive_queue(queue)
        return done

    async def retry_archives(self, *, probe_only=False):
        for record in self._archive_queue().values():
            if (isinstance(record, dict) and all(k in record for k in
                    ("conversation_id", "session_key", "last_active", "archive"))
                    and (not probe_only or self._is_probe_session(record["session_key"]))
                    and record.get("next_attempt", 0) <= time.time()):
                await self._try_archive_record(record)
        for previous in list(self._previous_cleanup_clients):
            previous._is_ready = self._is_ready
            previous.ls_address, previous.csrf_token = self.ls_address, self.csrf_token
            previous._executable = self._executable
            await previous.retry_archives(probe_only=probe_only)
            if not previous._archive_queue():
                self._previous_cleanup_clients.remove(previous)

    def tick_archives(self):
        """Retry in the background, without holding up chat/control polling."""
        if time.monotonic() < getattr(self, "_next_archive_poll", 0):
            return
        self._next_archive_poll = time.monotonic() + 5
        task = getattr(self, "_archive_retry_task", None)
        if task is not None and not task.done():
            return
        if task is not None and not task.cancelled():
            error = task.exception()
            if error is not None:
                logger.warning("分支归档重试未完成: %s", type(error).__name__)
        self._archive_retry_task = asyncio.create_task(self.retry_archives())

    async def _archive_session_once(self, session_key: str, archive_path: str, *, expected_id=None,
                                    expected_last_active=None) -> bool:
        """Clean temporary branches after archiving; preserve user conversations."""
        from pathlib import Path
        import gzip
        import shutil
        archive = Path(archive_path).resolve(strict=True)
        if not archive.is_file() or not archive.stat().st_size:
            return False
        # The archive can be a result or diagnostics for a terminal leaf.
        payload = json.loads(archive.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not payload:
            return False
        if not self._is_cleanup_session(session_key):
            return False
        session = self._sessions.get(session_key)
        if not session or not session.conversation_id or session.lock.locked():
            return False
        cid = session.conversation_id
        last_active = session.last_active
        if expected_id is not None and cid != expected_id:
            return False
        if expected_last_active is not None and last_active != expected_last_active:
            return False
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", cid):
            return False
        if not self._is_ready and not await asyncio.to_thread(self.initialize):
            return False
        directory = archive.parent / "conversations"
        directory.mkdir(parents=True, exist_ok=True)
        source = Path(self._brain_dir) / cid / ".system_generated/logs/transcript.jsonl"
        if source.is_file():
            def copy_trace():
                dest = directory / (cid + ".jsonl.gz")
                temp = dest.with_suffix(".tmp")
                with source.open("rb") as inp, gzip.open(temp, "wb") as out:
                    shutil.copyfileobj(inp, out)
                os.replace(temp, dest)
            await asyncio.to_thread(copy_trace)
        # Recheck after file I/O. Never remove a conversation reused meanwhile.
        async with session.lock:
            if (self._sessions.get(session_key) is not session or session.conversation_id != cid
                    or session.last_active != last_active):
                return False
            import aiohttp
            url = f"http://{self.ls_address}/exa.language_server_pb.LanguageServerService/DeleteCascadeTrajectory"
            try:
                async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=15)) as http:
                    async with http.post(url, json={"cascadeId": cid},
                        headers={"X-Codeium-Csrf-Token": self.csrf_token}) as response:
                        response.raise_for_status()
                        body = await response.read()
                        if isinstance(body, (bytes, bytearray)) and body.strip():
                            try:
                                self._check_agentapi_response(["delete-conversation", cid], json.loads(body))
                            except RuntimeError as error:
                                if not self._conversation_is_missing(error):
                                    raise
            except Exception as error:
                logger.warning("归档已保存，会话 %s 的后端清理未成功: %s", session_key, type(error).__name__)
                return False
            receipt = {"conversation_id": cid, "session_key": session_key, "archive": str(archive),
                       "deleted_at": time.time()}
            (directory / (cid + ".json")).write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            self._invalidate(session)
            self._save_sessions()
            return True

    async def probe(self, timeout: float = 10.0) -> Tuple[bool, str]:
        """Run a bounded health request, then archive and remove its temporary conversation."""
        async with self._probe_lock:
            outcome = "interrupted"
            try:
                self.queue_probe_cleanup()
                if not self._is_ready and not self.initialize():
                    outcome = "unavailable"
                    return False, "未能连接到 Antigravity Language Server 服务，请确保 Google Antigravity 正在运行。"
                reply = await self.chat("__system_probe__", "请只回复'OK'进行健康检查。",
                                        raise_on_error=True, timeout=timeout)
                reply_clean = reply.strip()
                error_keywords = ("❌", "⏱️", "⚠️", "resource_exhausted", "quota", "rate limit", "exceeded", "429")
                if not reply_clean or any(k in reply_clean.lower() for k in error_keywords):
                    outcome = "failed"
                    return False, f"模型探针返回异常内容: {reply_clean}"
                outcome = "completed"
                return True, reply_clean
            except Exception as e:
                outcome = "failed"
                return False, f"模型请求失败或超时: {e}"
            finally:
                try:
                    self.queue_probe_cleanup(outcome)
                    if self._is_ready:
                        await self.retry_archives(probe_only=True)
                except Exception as error:
                    logger.warning("探针清理稍后重试: %s", type(error).__name__)

    async def deliberate_intent(self, user_text: str, is_group: bool = False, is_at_bot: bool = False, is_master: bool = False) -> Any:
        """
        前置意图思考：分析用户发言是属于可执行命令（多模态检索/下载/控制）还是日常闲聊。
        驱动反重力内置意图思考引擎消除死板前缀。
        """
        from command_deliberation import default_command_deliberator
        return await default_command_deliberator.deliberate_async(
            user_text,
            is_group=is_group,
            is_at_bot=is_at_bot,
            is_master=is_master,
            ai_client=self,
            use_ai_thinking=False
        )

