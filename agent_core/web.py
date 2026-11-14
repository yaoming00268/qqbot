"""Configurable website accounts with native Playwright and verified publishing recipes."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import time

from agent_core.configuration import WebAccountConfig, origin
from computer_control import ControlError, locator, page_snapshot, unique_locator


def allowed_url(config, url):
    if origin(url) not in config.allowed_origins:
        raise PermissionError("目标网址不属于当前账号的 allowed_origins。")
    return url


@contextmanager
def managed_browser(config, profile, timeout_ms):
    from playwright.sync_api import sync_playwright
    from browser_control import installed_browser
    from vm_protocol import file_lock
    profile = Path(profile).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    with file_lock(profile.with_suffix(".lock"), timeout=2):
        with sync_playwright() as driver:
            context = driver.chromium.launch_persistent_context(str(profile),
                executable_path=str(installed_browser(config.executable)), headless=config.headless,
                viewport={"width": 1280, "height": 800}, accept_downloads=False)
            context.set_default_timeout(timeout_ms)
            try:
                def route_request(route):
                    request = route.request
                    if request.is_navigation_request():
                        try:
                            allowed_url(config, request.url)
                        except (ValueError, PermissionError):
                            route.abort("blockedbyclient")
                            return
                    route.continue_()
                context.route("**/*", route_request)
                yield context
            finally:
                context.close()


class WebAccountAdapter:
    capabilities = frozenset(("read_content", "read_feed", "create_draft", "publish_post", "reply_comment"))

    def __init__(self, store, *, browser_factory=managed_browser, timeout_ms=10000, max_chars=16000):
        self.store, self.browser_factory = store, browser_factory
        self.timeout_ms, self.max_chars = timeout_ms, max_chars

    def _evidence(self, page, action):
        root = self.store.path.parent / "actor_artifacts" / action["id"]
        root.mkdir(parents=True, exist_ok=True)
        snapshot = page_snapshot(page, max_chars=self.max_chars)
        (root / "observation.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        page.screenshot(path=str(root / "observation.png"))
        return snapshot, [str(root / "observation.json"), str(root / "observation.png")]

    def execute(self, action, account, *, guard=lambda: None):
        config = WebAccountConfig.model_validate(account["config"])
        payload, kind = action["payload"], action["kind"]
        if kind == "create_draft":
            if payload.get("target_url"):
                allowed_url(config, payload["target_url"])
            return {"verified": True, "draft": self.store.create_draft(action["actor_id"], account["id"],
                payload["text"], kind=payload.get("draft_kind", "publish_post"),
                target_url=payload.get("target_url", ""), draft_id=action["id"])}
        draft = None
        if kind in ("publish_post", "reply_comment"):
            draft = self.store.draft(payload["draft_id"], action["actor_id"], account["id"])
            if draft["kind"] != kind or draft["state"] != "draft":
                raise ControlError("DRAFT_NOT_READY", "草稿已提交、结果不明或类型不同，不能重复发布。")
            with self.store.connect() as db:
                duplicate = db.execute("SELECT 1 FROM actor_drafts WHERE account_id=? AND kind=? AND text=? "
                    "AND target_url=? AND id<>? AND state IN ('submitting','published')",
                    (account["id"], kind, draft["text"], draft["target_url"], draft["id"])).fetchone()
            if duplicate:
                raise ControlError("DUPLICATE_CONTENT", "同一账号和目标已有相同内容被提交，先核对原动作，未重复发布。")
            recipe = config.publish if kind == "publish_post" else config.comment
            if not recipe or not config.identity or not config.expected_identity:
                raise ControlError("ACCOUNT_RECIPE_MISSING", "该账号尚未配置完整的发布与身份核验规则。")
            if kind == "reply_comment" and not draft["target_url"]:
                raise ControlError("COMMENT_TARGET_MISSING", "评论草稿需要具体目标网址。")
            url = draft["target_url"] or recipe.composer_url or config.home_url
        else:
            url = payload.get("url") or config.home_url
        url = allowed_url(config, url)
        from agent_core.store import digest
        profile = config.profile or str(self.store.path.parent / "actor_browser_profiles" / digest(account["id"])[:24])
        submitted = False
        try:
            guard()
            with self.browser_factory(config, profile, self.timeout_ms) as context:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                allowed_url(config, page.url)
                if draft:
                    observed_identity = unique_locator(page, config.identity).inner_text().strip()
                    if observed_identity != config.expected_identity.strip():
                        raise ControlError("ACCOUNT_IDENTITY_MISMATCH", "页面登录身份与配置账号不一致，未发布。")
                    editor = unique_locator(page, recipe.editor)
                    submit = unique_locator(page, recipe.submit)
                    confirmation = locator(page, recipe.confirmation)
                    if confirmation.count() and confirmation.is_visible():
                        raise ControlError("CONFIRMATION_ALREADY_VISIBLE", "成功标志在提交前已经可见，无法核验新发布。")
                    editor.fill(draft["text"])
                    submit.click(trial=True)
                    guard()
                    allowed_url(config, page.url)
                    if unique_locator(page, config.identity).inner_text().strip() != config.expected_identity.strip():
                        raise ControlError("ACCOUNT_IDENTITY_MISMATCH", "提交前账号身份已改变，未发布。")
                    with self.store.connect() as db:
                        changed = db.execute("UPDATE actor_drafts SET state='submitting' WHERE id=? AND state='draft'",
                                             (draft["id"],)).rowcount
                        if changed != 1:
                            raise ControlError("DRAFT_NOT_READY", "草稿状态已变化，未重复提交。")
                    submitted = True
                    submit.click()
                    confirmation.wait_for(state="visible", timeout=self.timeout_ms)
                    if confirmation.count() != 1:
                        raise ControlError("CONFIRMATION_NOT_UNIQUE", "提交后的成功标志不唯一。", input_posted=None)
                    allowed_url(config, page.url)
                    snapshot, evidence = self._evidence(page, action)
                    remote_url = page.url
                    if recipe.result_link:
                        link = unique_locator(page, recipe.result_link).get_attribute("href")
                        if link:
                            from urllib.parse import urljoin
                            remote_url = allowed_url(config, urljoin(page.url, link))
                    with self.store.connect() as db:
                        db.execute("UPDATE actor_drafts SET state='published' WHERE id=?", (draft["id"],))
                    return {"verified": True, "remote_url": remote_url, "draft_id": draft["id"],
                            "confirmation": confirmation.inner_text()[:500], "evidence": evidence,
                            "title": snapshot["title"], "observed_at": time.time()}
                if kind not in ("read_content", "read_feed"):
                    raise ValueError("不支持的网页账号动作。")
                snapshot, evidence = self._evidence(page, action)
                links = []
                for element in snapshot["elements"]:
                    href = element.get("href")
                    try:
                        if href and origin(href) in config.allowed_origins and not any(row["url"] == href for row in links):
                            links.append({"url": href, "title": element["label"]})
                    except ValueError:
                        continue
                return {"verified": True, "url": page.url, "title": snapshot["title"], "text": snapshot["text"],
                        "links": links[:40], "evidence": evidence, "observed_at": time.time(),
                        "source": "web_observation", "read_scope": "visible_text_and_frames"}
        except ControlError as error:
            if submitted:
                error.input_posted = None
            raise
        except Exception as error:
            raise ControlError("WEB_RESULT_UNKNOWN" if submitted else "WEB_OPERATION_FAILED",
                               "网站操作未完成核验。" + ("发布可能已提交，禁止自动重发。" if submitted else "请查看配置、登录或页面状态。"),
                               input_posted=None if submitted else False) from error


class ActionGateway:
    def __init__(self, store, adapters=None):
        self.store = store
        self.adapters = adapters or {"web": WebAccountAdapter(store)}

    def execute(self, actor_id, account_id, kind, payload, request_key, *, guard=lambda: None):
        if not request_key:
            raise ValueError("动作需要稳定的幂等键。")
        action = self.store.prepare_action(actor_id, account_id, kind, payload, request_key)
        if not self.store.claim_action(action["id"]):
            return self.store.action(action["id"], actor_id)
        action = self.store.action(action["id"], actor_id)
        def check():
            with self.store.connect() as db:
                lease = db.execute("SELECT owner,expires FROM actor_leases WHERE account_id=?", (account_id,)).fetchone()
            if not lease or lease["owner"] != action["id"] or lease["expires"] <= time.time():
                raise ControlError("ACTION_LEASE_EXPIRED", "账号执行租约已失效，未继续操作。")
            current = self.store.account(account_id, actor_id)
            if (not current["enabled"] or current["revision"] != action["config_revision"]
                    or kind not in current["config"].get("actions", []) or not self.store.actor(actor_id)["enabled"]):
                raise ControlError("ACTION_PERMISSION_CHANGED", "账号能力或角色状态已变化，未继续操作。")
            guard()
        try:
            check()
            account = self.store.account(account_id, actor_id)
            adapter_name = account["config"].get("adapter", account["platform"])
            if adapter_name not in self.adapters:
                raise ControlError("ADAPTER_UNAVAILABLE", "当前进程未提供该平台的执行适配器。")
            result = self.adapters[adapter_name].execute(action, account, guard=check)
            if result.get("verified") is not True:
                return self.store.finish_action(action["id"], "unknown", result)
            if kind in ("read_content", "read_feed") and result.get("text"):
                self.store.remember(actor_id, result["title"] + "\n" + result["text"], account_id=account_id,
                    visibility="actor", source_id=result.get("url", ""), evidence=result.get("evidence", []), kind="web_observation")
            return self.store.finish_action(action["id"], "confirmed", result)
        except Exception as error:
            posted = getattr(error, "input_posted", False if isinstance(error, (ValueError, PermissionError, KeyError, TypeError)) else None)
            state = "failed" if posted is False else "unknown"
            return self.store.finish_action(action["id"], state,
                {"error": str(error), "code": getattr(error, "code", type(error).__name__),
                 "input_posted": posted})
