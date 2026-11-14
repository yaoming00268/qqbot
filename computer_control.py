"""Semantic computer control: Playwright locators and scoped Windows UIA patterns.

Coordinates remain an explicit fallback. No global mouse, keyboard or clipboard.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json

from agent_core.configuration import LocatorSpec


class ControlError(ValueError):
    def __init__(self, code, message, *, input_posted=False):
        super().__init__(message)
        self.code, self.input_posted = code, input_posted
        self.hint = "先重新 inspect；结果不明时只观察，不重放原动作。"


def locator(page, spec):
    spec = LocatorSpec.model_validate(spec)
    if spec.role:
        return page.get_by_role(spec.role, **({"name": spec.name, "exact": True} if spec.name else {}))
    for key, method in (("label", "get_by_label"), ("placeholder", "get_by_placeholder"), ("test_id", "get_by_test_id")):
        value = getattr(spec, key)
        if value:
            return getattr(page, method)(value, **({} if key == "test_id" else {"exact": True}))
    return page.locator(spec.css)


def unique_locator(page, spec):
    result = locator(page, spec)
    if result.count() != 1:
        raise ControlError("CONTROL_NOT_UNIQUE", "定位器未找到唯一元素；请重新观察或缩小定位范围。")
    return result


PAGE_SNAPSHOT = r"""({maxChars, maxElements}) => {
    const visible = el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return !!r.width && !!r.height && s.display !== 'none' && s.visibility !== 'hidden';
    };
    const path = el => {
        const pieces = [];
        for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
            if (n.id && document.querySelectorAll('#' + CSS.escape(n.id)).length === 1) {
                pieces.unshift('#' + CSS.escape(n.id)); break;
            }
            let index = 1;
            for (let p = n.previousElementSibling; p; p = p.previousElementSibling)
                if (p.tagName === n.tagName) index++;
            pieces.unshift(n.tagName.toLowerCase() + ':nth-of-type(' + index + ')');
        }
        return pieces.join(' > ');
    };
    const elements = [];
    for (const el of document.querySelectorAll('a[href],button,input,textarea,select,[role],[contenteditable=true]')) {
        if (elements.length >= maxElements) break;
        if (!visible(el) || el.type === 'password' || el.type === 'hidden') continue;
        const label = el.getAttribute('aria-label') ||
            (el.labels && el.labels.length ? el.labels[0].innerText : '') ||
            el.getAttribute('placeholder') || (el.tagName === 'INPUT' ? '' : el.innerText) || '';
        elements.push({tag:el.tagName.toLowerCase(), role:el.getAttribute('role') || '',
            label:label.trim().slice(0,200), type:el.type || '', disabled:!!el.disabled,
            href:el.tagName === 'A' ? el.href : '', selector:{css:path(el)}});
    }
    const parts = [];
    let size = 0;
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node && size < maxChars; node = walker.nextNode()) {
        const el = node.parentElement;
        if (!el || el.closest('script,style,noscript,textarea,input,[contenteditable=true],[aria-hidden=true]') || !visible(el)) continue;
        const text = node.textContent.trim().replace(/\s+/g,' ');
        if (text) {parts.push(text); size += text.length + 1;}
    }
    return {title:document.title, text:parts.join('\n').slice(0,maxChars), elements};
}"""


def page_snapshot(page, *, max_chars=16000, max_elements=80):
    result = {"engine": "playwright", "url": page.url, "title": page.title(), "text": "", "elements": []}
    texts = []
    remaining = max_chars
    for index, frame in enumerate(page.frames[:12]):
        if remaining <= 0:
            break
        data = frame.evaluate(PAGE_SNAPSHOT, {"maxChars": remaining, "maxElements": max_elements - len(result["elements"])})
        texts.append(data["text"])
        remaining -= len(data["text"])
        for element in data["elements"]:
            element.update(id="e" + str(len(result["elements"]) + 1), frame=index, frame_url=frame.url)
            result["elements"].append(element)
    result["text"] = "\n".join(texts)[:max_chars]
    result["fingerprint"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


class PlaywrightBridge:
    """Attach only to the already-authorized browser/profile and observed tab."""
    def __init__(self, target, *, timeout_ms=10000):
        self.target, self.timeout_ms = target, timeout_ms

    @contextmanager
    def page(self, observation):
        from browser_control import BrowserBridge
        from playwright.sync_api import sync_playwright
        bridge = BrowserBridge(self.target)
        tab = bridge.page(observation["browser_page"])
        if tab["url"] != observation["page_url"]:
            raise ControlError("CONTROL_PAGE_CHANGED", "页面已经跳转，请重新截图和 inspect。")
        with sync_playwright() as driver:
            browser = driver.chromium.connect_over_cdp(bridge.endpoint(), timeout=self.timeout_ms)
            pages = []
            for context in browser.contexts:
                for page in context.pages:
                    session = context.new_cdp_session(page)
                    try:
                        info = session.send("Target.getTargetInfo")["targetInfo"]
                        if info["targetId"] == tab["id"]:
                            pages.append(page)
                    finally:
                        session.detach()
            if len(pages) != 1 or pages[0].url != observation["page_url"]:
                raise ControlError("CONTROL_PAGE_CHANGED", "观察的标签页已改变。")
            pages[0].set_default_timeout(self.timeout_ms)
            yield pages[0]
            # Stopping the driver disconnects CDP; never close the user's task browser.

    def inspect(self, observation):
        with self.page(observation) as page:
            return page_snapshot(page)

    def act(self, observation, action, element_id, *, text="", guard=None):
        element = next((row for row in observation.get("semantic", {}).get("elements", []) if row["id"] == element_id), None)
        if not element:
            raise ControlError("CONTROL_ELEMENT_MISSING", "元素不属于本次 inspect 观察。")
        posted = False
        try:
            with self.page(observation) as page:
                if page_snapshot(page)["fingerprint"] != observation.get("semantic", {}).get("fingerprint"):
                    raise ControlError("CONTROL_CONTENT_CHANGED", "页面内容已改变，请重新 inspect。")
                frames = page.frames
                if element["frame"] >= len(frames) or frames[element["frame"]].url != element["frame_url"]:
                    raise ControlError("CONTROL_FRAME_CHANGED", "元素所属框架已改变。")
                target = unique_locator(frames[element["frame"]], element["selector"])
                if action == "invoke":
                    target.click(trial=True)
                elif action == "fill":
                    if not target.is_editable() or target.get_attribute("type") == "password":
                        raise ControlError("CONTROL_NOT_EDITABLE", "目标不是可编辑的普通文本框。")
                elif action == "select":
                    if target.evaluate("el => el.tagName") != "SELECT":
                        raise ControlError("CONTROL_NOT_SELECT", "目标不是标准选择框。")
                else:
                    raise ControlError("CONTROL_OPERATION_INVALID", "不支持的语义动作。")
                if guard:
                    guard()
                posted = True
                if action == "invoke":
                    target.click()
                elif action == "fill":
                    target.fill(text)
                else:
                    target.select_option(value=text)
                value = (target.inner_text() if target.evaluate("el => el.isContentEditable") else target.input_value()) if action in ("fill", "select") else None
                return {"posted": True, "verified": action in ("fill", "select") and value == text,
                        "host_input": False, "input_transport": "playwright", "element_id": element_id}
        except ControlError:
            raise
        except Exception as error:
            raise ControlError("CONTROL_ACTION_FAILED", "语义操作未能确认；请重新观察。",
                               input_posted=None if posted else False) from error


class WindowsAutomation:
    """UIA pattern operations scoped to one validated window and process."""
    def __init__(self, window, *, automation=None):
        self.window, self.automation = window, automation

    @contextmanager
    def controls(self):
        if self.automation is None:
            import uiautomation as automation
        else:
            automation = self.automation
        with automation.UIAutomationInitializerInThread():
            root = automation.ControlFromHandle(self.window["hwnd"])
            if root is None or root.ProcessId != self.window["pid"]:
                raise ControlError("UIA_PROCESS_CHANGED", "UI Automation 根窗口不属于当前进程。")
            yield automation, root

    @staticmethod
    def walk(root, limit=128):
        pending, count = [(root, 0)], 0
        while pending and count < limit:
            control, depth = pending.pop(0)
            count += 1
            yield control
            if depth < 8:
                pending.extend((child, depth + 1) for child in control.GetChildren()[:limit - count])

    def inspect(self):
        result = {"engine": "windows_uia", "text": "", "elements": []}
        with self.controls() as (_, root):
            for control in self.walk(root):
                if control.ProcessId != self.window["pid"] or control.IsPassword:
                    continue
                result["elements"].append({"id": "e" + str(len(result["elements"]) + 1),
                    "runtime_id": list(control.GetRuntimeId()), "label": control.Name[:300],
                    "automation_id": control.AutomationId, "control_type": control.ControlTypeName,
                    "disabled": not control.IsEnabled})
        result["text"] = "\n".join(row["label"] for row in result["elements"] if row["label"])[:16000]
        return result

    def act(self, observation, action, element_id, *, text="", guard=None):
        element = next((row for row in observation.get("semantic", {}).get("elements", []) if row["id"] == element_id), None)
        if not element:
            raise ControlError("CONTROL_ELEMENT_MISSING", "元素不属于本次 inspect 观察。")
        with self.controls() as (automation, root):
            matches = [row for row in self.walk(root) if list(row.GetRuntimeId()) == element["runtime_id"]]
            if len(matches) != 1:
                raise ControlError("UIA_ELEMENT_CHANGED", "原控件已改变，请重新 inspect。")
            control = matches[0]
            if control.ProcessId != self.window["pid"] or control.IsPassword or not control.IsEnabled:
                raise ControlError("UIA_ELEMENT_UNAVAILABLE", "目标控件不可用。")
            pattern_name = {"invoke": "InvokePattern", "fill": "ValuePattern", "select": "SelectionItemPattern"}.get(action)
            if not pattern_name:
                raise ControlError("CONTROL_OPERATION_INVALID", "不支持的 UIA 动作。")
            try:
                pattern = control.GetPattern(getattr(automation.PatternId, pattern_name))
            except Exception as error:
                raise ControlError("UIA_PATTERN_UNAVAILABLE", "该控件没有对应的 UIA 能力；可重新截图选择坐标操作。") from error
            if pattern is None or action == "fill" and pattern.IsReadOnly:
                raise ControlError("UIA_PATTERN_UNAVAILABLE", "目标控件不支持此语义动作。")
            if guard:
                guard()
            try:
                if action == "invoke":
                    pattern.Invoke()
                elif action == "fill":
                    pattern.SetValue(text)
                else:
                    pattern.Select()
                return {"posted": True, "verified": action == "fill" and pattern.Value == text,
                        "host_input": False, "input_transport": "windows_uia", "element_id": element_id}
            except Exception as error:
                raise ControlError("UIA_RESULT_UNKNOWN", "控件操作结果未确认；先重新观察。", input_posted=None) from error
