"""Explicit, scoped CLI message commands and task delivery approvals."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import re
import sqlite3
import time
import uuid

from console_tools.files import ConsoleFileError


class OutgoingCommands:
    @staticmethod
    def command_result_text(kind, payload, intro=""):
        """The main agent introduces a command result; it cannot replace it."""
        detail = payload.get("result") or {}
        if kind not in ("bot_command", "command_error", "media_command") or detail.get("files"):
            return intro
        body = detail.get("answer") or payload.get("error") or ""
        if not isinstance(body, str) or not body.strip():
            return intro
        # Result text is data. Native mention syntax must remain literal;
        # only the separately validated --mention field can notify a member.
        body = re.sub(r"<(?=@|qqbot-at-(?:user|everyone)\b)", "<\u200b", body.strip())
        intro = str(intro or "").strip()
        if not intro or intro in body:
            return body
        return intro if body in intro else intro + "\n\n" + body

    @staticmethod
    def _message_text(text, *, allow_empty=False):
        text = str(text or "").strip()
        if not text and not allow_empty:
            raise ConsoleFileError("MESSAGE_EMPTY", "发送正文不能为空。")
        if re.search(r"\[CQ:(?:at|reply)(?:,|\])|<@[^>]*>|<qqbot-at-(?:user|everyone)\b", text, re.I):
            raise ConsoleFileError("MESSAGE_MARKUP_REJECTED", "引用与 @ 请使用 --reply-to/--mention 参数。")
        return text

    def messages(self, token, limit=20):
        context = self.context(token)
        with self.connect() as db:
            rows = db.execute("SELECT id,sender_id,text,received_at FROM chat_messages "
                "WHERE namespace=? AND scope=? ORDER BY received_at DESC,rowid DESC LIMIT ?",
                (context["namespace"], context["scope"], limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def _chat_context(self, token, *, allow_closed=False, allow_committed=False):
        context = self.context(token)
        if (context.get("sandbox_session_id") or context.get("parent_request_id") or context.get("idle_event")
                or context.get("agent_worker_id") or context.get("agent_event")):
            raise PermissionError("执行分支和内部思考不能发送 QQ 消息；请将成果交给主代理。")
        closed = ("wait", "finished", "sealed", "reported") + (() if allow_committed else ("committed",))
        if not allow_closed and self.reply_control(token).get("mode") in closed:
            raise ConsoleFileError("TURN_CLOSED", "本轮已经结束，不能追加发送。", "使用当前主会话事件的 context_file。")
        return context

    @staticmethod
    def _require_active_turn(db, token):
        """Reply submission leaves work open until explicit finish or sealing."""
        row = db.execute("SELECT mode FROM reply_controls WHERE token=?", (token,)).fetchone()
        if row and row[0] != "committed":
            raise ConsoleFileError("TURN_CLOSED", "本轮已经结束，不能再提交工作。",
                                   "使用当前主会话事件的 context_file。")

    @staticmethod
    def _require_open_turn(db, token):
        # Speech decisions are immutable after the first reply submission.
        if db.execute("SELECT 1 FROM reply_controls WHERE token=?", (token,)).fetchone():
            raise ConsoleFileError("TURN_CLOSED", "本轮已经提交回复或结束，不能重新判断发言。",
                                   "使用当前主会话事件的 context_file。")

    @staticmethod
    def _check_reply_budget(db, token, limit):
        if limit:
            total = sum(max(len(item["text"]), len(item.get("spoken_text", ""))) for row in db.execute(
                "SELECT payload FROM outgoing WHERE token=? AND state NOT IN ('failed','cancelled')", (token,))
                        for item in [json.loads(row[0])])
            if total > limit:
                raise ConsoleFileError("REPLY_LENGTH_EXCEEDED", f"普通回复合计 {total} 字，超过本轮 {limit} 字上限。",
                                       "缩短正文后重新提交；本次新增消息没有暂存或发送。完整命令结果通过 deliver 批准。")

    @staticmethod
    def _commit_key(action, value):
        return hashlib.sha256(json.dumps([action, value], ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _committed_receipt(db, token, key=""):
        row = db.execute("SELECT reason FROM reply_controls WHERE token=?", (token,)).fetchone()
        try:
            saved = json.loads(row[0]) if row else {}
        except (ValueError, TypeError):
            return None
        if key and isinstance(saved, dict) and saved.get("commit"):
            batch = db.execute("SELECT receipt FROM reply_batches WHERE token=? AND commit_key=?", (token, key)).fetchone()
            if batch:
                return json.loads(batch[0])
        if isinstance(saved, dict) and saved.get("commit") and (not key or saved["commit"] == key):
            return saved["receipt"]
        return None

    def _commit_turn(self, db, token, key, message_ids=None):
        self._require_speech_ready(db, token, release=True)
        previous_ids = {identity for row in db.execute("SELECT receipt FROM reply_batches WHERE token=?", (token,))
                        for identity in json.loads(row[0]).get("message_ids", [])}
        receipt = {"state": "committed", "message_ids": [row[0] for row in db.execute(
            "SELECT id FROM outgoing WHERE token=? AND state='staged' ORDER BY created,rowid", (token,))
            if row[0] not in previous_ids and (message_ids is None or row[0] in message_ids)],
            "next_action": "continue_or_finish"}
        db.execute("INSERT OR IGNORE INTO reply_batches VALUES(?,?,?,?)", (token, key, json.dumps(receipt), time.time()))
        db.execute("INSERT OR REPLACE INTO reply_controls VALUES(?,?,?)", (token, "committed",
            json.dumps({"commit": key, "receipt": receipt}, ensure_ascii=False)))
        return receipt

    def committed_messages(self, token):
        """An explicitly committed batch is sendable while its model keeps working."""
        with self.connect() as db:
            ids = {identity for row in db.execute("SELECT receipt FROM reply_batches WHERE token=?", (token,))
                   for identity in json.loads(row[0]).get("message_ids", [])}
            return [{"command_id": row["id"], **json.loads(row["payload"])} for row in db.execute(
                "SELECT * FROM outgoing WHERE token=? AND state='staged' ORDER BY created,rowid", (token,)) if row["id"] in ids]

    def output_pending(self, token):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM outgoing WHERE token=? AND state IN ('staged','sending') LIMIT 1", (token,)).fetchone() is not None

    def _message_targets(self, context, reply_to, mention):
        if context.get("proactive_event") and (reply_to or mention):
            raise ConsoleFileError("MESSAGE_TARGET_INVALID", "主动话题直接发正文，不引用或 @ 成员。")
        if reply_to and (len(reply_to) > 512 or re.search(r"[\x00-\x20]", reply_to)):
            raise ConsoleFileError("MESSAGE_TARGET_INVALID", "消息 ID 格式无效。")
        if mention and not re.fullmatch(r"[A-Za-z0-9_.+=/-]{1,128}", mention):
            raise ConsoleFileError("MESSAGE_TARGET_INVALID", "成员 ID 格式无效。")
        if not context.get("is_group") and (reply_to or mention):
            raise ConsoleFileError("MESSAGE_TARGET_INVALID", "私聊直接发送正文，无需引用或 @。")
        with self.connect() as db:
            if reply_to and not db.execute("SELECT 1 FROM chat_messages WHERE namespace=? AND scope=? AND id=?",
                    (context["namespace"], context["scope"], reply_to)).fetchone():
                raise ConsoleFileError("REPLY_SOURCE_INVALID", "引用消息不在当前会话。", "用 messages 查询真实消息 ID，不能使用 m1/m2 标签。")
            if mention and (mention in ("all", context.get("bot_id")) or not db.execute(
                    "SELECT 1 FROM chat_messages WHERE namespace=? AND scope=? AND sender_id=?",
                    (context["namespace"], context["scope"], mention)).fetchone()):
                raise ConsoleFileError("MENTION_TARGET_INVALID", "只能 @ 当前会话中已确认的成员，不能 @ 全体或机器人。")

    @staticmethod
    def _voice_payload(context, voice, emotion, spoken_text, text=""):
        if type(voice) is not bool:
            raise ConsoleFileError("VOICE_INVALID", "voice 必须为 true 或 false。")
        if not voice:
            if emotion or spoken_text:
                raise ConsoleFileError("VOICE_INVALID", "emotion 和 spoken_text 仅在 voice=true 时使用。")
            return {}
        settings = context.get("voice") or {}
        if not settings.get("enabled"):
            raise ConsoleFileError("VOICE_DISABLED", "控制台尚未启用语音输出。")
        if not emotion or emotion not in settings.get("emotions", []):
            raise ConsoleFileError("VOICE_EMOTION_INVALID", "语音必须选择真实可用的 emotion 标签。", "先用 voice 查询可用标签。")
        if not isinstance(spoken_text, str) or len(spoken_text) > 16000:
            raise ConsoleFileError("VOICE_TEXT_INVALID", "spoken_text 必须为不超过 16000 字的文字。")
        from voice_text import inline_emotions, inline_prosody, VoiceError
        try:
            inline_emotions(spoken_text or text, emotion, settings.get("emotions", []))
        except VoiceError as error:
            raise ConsoleFileError("VOICE_EMOTION_INVALID", str(error), "使用 voice 查询可用标签；句中用 [emotion:标签] 切换。") from None
        try:
            inline_prosody(spoken_text or text)
        except VoiceError as error:
            raise ConsoleFileError("VOICE_PROSODY_INVALID", str(error), "语速标记需成对闭合且不可嵌套。") from None
        return {"voice": True, "emotion": emotion, "spoken_text": spoken_text.strip()}

    @staticmethod
    def _platform_runs(messages):
        # Every voice is one indivisible message. Adjacent text can still merge.
        targets = [(item.get("reply_to", ""), item.get("mention", ""),
                    index if item.get("voice") else -1) for index, item in enumerate(messages)]
        return sum(index == 0 or target != targets[index - 1] for index, target in enumerate(targets))

    def stage_message(self, token, text, reply_to="", mention="", request_key="", *, voice=False, emotion="", spoken_text=""):
        context = self._chat_context(token, allow_committed=True)
        text = self._message_text(text)
        self._message_targets(context, reply_to, mention)
        payload = {"text": text, "reply_to": reply_to, "mention": mention,
                   **self._voice_payload(context, voice, emotion, spoken_text, text)}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        request_key = request_key or hashlib.sha256(encoded.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_active_turn(db, token)
            self._require_speech_ready(db, token)
            existing = db.execute("SELECT id,state,payload FROM outgoing WHERE token=? AND request_key=?", (token, request_key)).fetchone()
            if not existing and context.get("transport") == "official":
                targets = [item for row in db.execute(
                    "SELECT payload FROM outgoing WHERE token=? AND state NOT IN ('failed','cancelled') ORDER BY created,rowid", (token,))
                    for item in [json.loads(row[0])]] + [payload]
                runs = self._platform_runs(targets)
                if runs > context.get("reply_slots", 5):
                    raise ConsoleFileError("MESSAGE_LIMIT", "本轮平台回复额度不足以发送更多不同目标的消息。",
                                           "相邻且引用/@相同的正文可合并；请在下一条新消息继续。")
            db.execute("INSERT OR IGNORE INTO outgoing VALUES(?,?,?,?,?,?,?,?)", (
                uuid.uuid4().hex, token, request_key, encoded, "staged", "{}", time.time(), 0))
            row = db.execute("SELECT id,state,payload FROM outgoing WHERE token=? AND request_key=?", (token, request_key)).fetchone()
            self._check_reply_budget(db, token, context.get("reply_char_limit", 0))
        if row["payload"] != encoded:
            raise ConsoleFileError("REQUEST_KEY_CONFLICT", "幂等键已用于另一条消息。")
        return {"id": row["id"], "state": row["state"]}

    def reply(self, token, texts, reply_to="", mention="", request_key="", *, finish=False, voice=False, emotion="", spoken_text=""):
        """Commit a reply, optionally ending this turn in the same transaction."""
        context = self._chat_context(token, allow_closed=True)
        texts = [texts] if isinstance(texts, str) else texts
        if not isinstance(texts, list) or not texts or any(not isinstance(text, str) for text in texts):
            raise ConsoleFileError("MESSAGE_EMPTY", "reply 需要至少一条文字消息。")
        voice_payload = self._voice_payload(context, voice, emotion, spoken_text, texts[0] if len(texts) == 1 else "")
        if voice and len(texts) != 1:
            raise ConsoleFileError("VOICE_TEXT_INVALID", "一条语音请使用单个 text；前端会切句合成完整语音。")
        messages = [{"text": self._message_text(text), "reply_to": reply_to if index == 0 else "",
                     "mention": mention if index == 0 else "", **voice_payload} for index, text in enumerate(texts)]
        self._message_targets(context, reply_to, mention)
        key = self._commit_key("reply", [messages, request_key])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._committed_receipt(db, token, key)
            if previous:
                return self._finish_turn(db, token) if finish else previous
            self._require_active_turn(db, token)
            message_ids = []
            for index, message in enumerate(messages):
                encoded = json.dumps(message, ensure_ascii=False, sort_keys=True)
                item_key = "reply:" + (request_key or key) + ":" + str(index)
                if db.execute("SELECT 1 FROM outgoing WHERE token=? AND request_key=?", (token, item_key)).fetchone():
                    raise ConsoleFileError("REQUEST_KEY_CONFLICT", "幂等键已用于另一批回复。", "更改正文时使用新的 request_key。")
                identity = uuid.uuid4().hex
                db.execute("INSERT INTO outgoing VALUES(?,?,?,?,?,?,?,?)", (
                    identity, token, item_key, encoded, "staged", "{}", time.time(), 0))
                message_ids.append(identity)
            self._check_reply_budget(db, token, context.get("reply_char_limit", 0))
            if context.get("transport") == "official":
                targets = [item for row in db.execute(
                    "SELECT payload FROM outgoing WHERE token=? AND state NOT IN ('failed','cancelled') ORDER BY created,rowid", (token,))
                    for item in [json.loads(row[0])]]
                runs = self._platform_runs(targets)
                if runs > context.get("reply_slots", 5):
                    raise ConsoleFileError("MESSAGE_LIMIT", "本轮平台回复额度不足，整批回复未提交。")
            receipt = self._commit_turn(db, token, key, message_ids)
            return self._finish_turn(db, token) if finish else receipt

    def commit_reply(self, token, *, finish=False):
        context = self._chat_context(token, allow_closed=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            committed = {identity for row in db.execute("SELECT receipt FROM reply_batches WHERE token=?", (token,))
                         for identity in json.loads(row[0]).get("message_ids", [])}
            drafts = [row[0] for row in db.execute("SELECT id FROM outgoing WHERE token=? AND state='staged' ORDER BY created,rowid", (token,))
                      if row[0] not in committed]
            previous = self._committed_receipt(db, token)
            if previous and not drafts:
                return self._finish_turn(db, token) if finish else previous
            self._require_active_turn(db, token)
            self._require_speech_ready(db, token)
            self._check_reply_budget(db, token, context.get("reply_char_limit", 0))
            if not db.execute("SELECT 1 FROM outgoing WHERE token=? AND state='staged'", (token,)).fetchone():
                raise ConsoleFileError("MESSAGE_EMPTY", "没有待提交的消息；普通回复直接用 reply --text。")
            receipt = self._commit_turn(db, token, self._commit_key("commit", drafts))
            return self._finish_turn(db, token) if finish else receipt

    def finish_turn(self, token):
        """End only this main turn, preserving committed output and queued work."""
        context = self.context(token)
        if any(context.get(key) for key in ("parent_request_id", "sandbox_session_id", "agent_worker_id", "activity_controller")):
            raise PermissionError("执行分支只能完成自己的任务，不能结束主代理回合。")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._finish_turn(db, token)

    def _finish_turn(self, db, token):
        """Share terminal receipts between standalone and combined commands."""
        control = db.execute("SELECT mode,reason FROM reply_controls WHERE token=?", (token,)).fetchone()
        try:
            saved = json.loads(control["reason"]) if control else {}
        except (ValueError, TypeError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        if control and control["mode"] in ("finished", "sealed") and saved.get("finish"):
            return saved["finish"]
        self._require_active_turn(db, token)
        self._require_speech_ready(db, token)
        committed_ids = [identity for row in db.execute("SELECT receipt FROM reply_batches WHERE token=? ORDER BY created", (token,))
                         for identity in json.loads(row[0]).get("message_ids", [])]
        if any(row[0] not in committed_ids for row in db.execute("SELECT id FROM outgoing WHERE token=? AND state='staged'", (token,))):
            raise ConsoleFileError("REPLY_NOT_COMMITTED", "回复尚未提交，请先调用 commit --finish。")
        receipt = {"state": "finished", "message_ids": list(dict.fromkeys(committed_ids)),
                   "next_action": "none"}
        db.execute("INSERT OR REPLACE INTO reply_controls VALUES(?,?,?)", (token, "finished",
            json.dumps({**saved, "finish": receipt}, ensure_ascii=False)))
        return receipt

    def staged_messages(self, token):
        with self.connect() as db:
            return [{"command_id": row["id"], **json.loads(row["payload"])} for row in db.execute(
                "SELECT * FROM outgoing WHERE token=? AND state='staged' ORDER BY created,rowid", (token,))]

    def message_state(self, command_id, state, receipt=None):
        with self.connect() as db:
            db.execute("UPDATE outgoing SET state=?,receipt=?,updated=? WHERE id=?", (
                state, json.dumps(receipt or {}, ensure_ascii=False, default=str), time.time(), command_id))

    def claim_message(self, token, command_id):
        return self.claim_messages(token, [command_id])

    def claim_messages(self, token, command_ids):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" for _ in command_ids)
            count = db.execute("SELECT COUNT(*) FROM outgoing WHERE token=? AND id IN (" + marks + ") AND state='staged'",
                               (token, *command_ids)).fetchone()[0]
            if count != len(command_ids):
                return False
            db.execute("UPDATE outgoing SET state='sending',updated=? WHERE token=? AND id IN (" + marks + ")",
                       (time.time(), token, *command_ids))
            return True

    def message_status(self, token, identity):
        context = self.context(token)
        with self.connect() as db:
            row = db.execute("SELECT o.* FROM outgoing o JOIN contexts c ON c.token=o.token "
                "WHERE o.id=? AND c.namespace=? AND c.scope=?", (identity, context["namespace"], context["scope"])).fetchone()
        if row is None:
            return None
        owner = self.context(row["token"], allow_expired=True)
        if owner.get("sender_id") != context.get("sender_id") or owner.get("session_key") != context.get("session_key"):
            raise PermissionError("不能查询其他发起人或人设的发送记录。")
        receipt, state = json.loads(row["receipt"]), row["state"]
        data = receipt.get("data") or {}
        outbox_ids = data.get("outbox_ids") or ([data["outbox_id"]] if data.get("outbox_id") else [])
        if state == "queued" and outbox_ids and owner.get("runtime_database"):
            from pathlib import Path
            path = Path(owner["runtime_database"])
            if path.is_file():
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                    states = [item[0] for item in db.execute("SELECT state FROM outbox WHERE id IN (" +
                        ",".join("?" for _ in outbox_ids) + ")", outbox_ids)]
                if "unknown" in states:
                    state = "unknown"
                elif "failed" in states or "cancelled" in states:
                    state = "failed" if "failed" in states else "cancelled"
                elif len(states) == len(outbox_ids) and all(item == "accepted" for item in states):
                    state = "accepted"
                if state != row["state"]:
                    self.message_state(identity, state, receipt)
        return {"id": identity, "state": state, "task": "发送消息", "result": receipt}

    def seal_messages(self, token, *, cancel=False, preserve_committed=False):
        """Close and snapshot atomically; None means a failed model had committed."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            control = db.execute("SELECT mode FROM reply_controls WHERE token=?", (token,)).fetchone()
            if cancel and preserve_committed and control and control[0] in ("committed", "finished"):
                return None
            db.execute("INSERT OR IGNORE INTO reply_controls VALUES(?,?,?)", (token, "sealed", "本轮已结束"))
            db.execute("UPDATE reply_controls SET mode='sealed' WHERE token=? AND mode IN ('committed','reported','finished')", (token,))
            if cancel:
                db.execute("UPDATE reply_controls SET mode='sealed',reason='本轮已取消' WHERE token=?", (token,))
                db.execute("UPDATE speech_decisions SET state='cancelled' WHERE token=? AND state IN ('ready','released')", (token,))
                db.execute("UPDATE outgoing SET state='cancelled' WHERE token=? AND state='staged'", (token,))
                db.execute("UPDATE requests SET state='cancel_requested' WHERE token=? AND kind='sticker' "
                           "AND state IN ('submitted','queued','running')", (token,))
            elif not control or control[0] != "wait":
                self._consume_speech(db, token)
                committed = {identity for row in db.execute("SELECT receipt FROM reply_batches WHERE token=?", (token,))
                             for identity in json.loads(row[0]).get("message_ids", [])}
                drafts = [row[0] for row in db.execute("SELECT id FROM outgoing WHERE token=? AND state='staged'", (token,))
                          if row[0] not in committed]
                db.executemany("UPDATE outgoing SET state='cancelled' WHERE id=?", ((identity,) for identity in drafts))
            return [{"command_id": row["id"], **json.loads(row["payload"])} for row in db.execute(
                "SELECT * FROM outgoing WHERE token=? AND state='staged' ORDER BY created,rowid", (token,))]

    def approve_delivery(self, token, request_id, text="", reply_to="", mention="", *, finish=False):
        context = self._chat_context(token, allow_closed=finish, allow_committed=True)
        text = self._message_text(text, allow_empty=True)
        self._message_targets(context, reply_to, mention)
        # Authorization is checked against the task's original owner, persona,
        # namespace and scope. A real ID in another group grants no capability.
        self.status(token, request_id)
        key = self._commit_key("deliver", [request_id, text, reply_to, mention])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if finish:
                previous = self._committed_receipt(db, token, key)
                if previous:
                    return {**previous, "id": request_id, "delivery": "approved"}
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            payload = json.loads(row["result"])
            plan = payload.get("delivery") or {}
            full_text = self.command_result_text(row["kind"], payload, text)
            proposed = {"text": full_text or ("任务完成" if payload.get("success") else "任务未完成，请查看任务记录。"),
                        "reply_to": reply_to, "mention": mention}
            if plan.get("approved_by") and any(plan.get(k) != v for k, v in proposed.items()):
                raise ConsoleFileError("DELIVERY_ALREADY_APPROVED", "该结果已有交付指令，不能修改或重复发送。")
            if (finish and plan.get("approved_by") == token and plan.get("submission_receipt")
                    and self._committed_receipt(db, token)):
                return {**plan["submission_receipt"], "id": request_id, "delivery": "approved"}
            self._require_active_turn(db, token)
            if row["state"] != "ready":
                if row["state"] == "delivered":
                    return {"id": request_id, "state": "delivered"}
                raise ConsoleFileError("RESULT_NOT_READY", "任务尚无可交付的结果。", "用 status 查询真实状态。")
            self._require_speech_ready(db, token, release=True)
            plan.update(proposed, approved_by=token, mode="main_agent")
            if finish:
                receipt = self._committed_receipt(db, token) or self._commit_turn(db, token, key)
                plan["submission_receipt"] = receipt
            payload["delivery"] = plan
            db.execute("UPDATE requests SET result=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), request_id))
            if finish:
                return {**receipt, "id": request_id, "delivery": "approved"}
        return {"id": request_id, "state": "ready", "delivery": "approved"}
