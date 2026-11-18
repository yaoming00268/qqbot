"""Gemini page transport using a dedicated Chromium profile."""
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

from gemini_browser_auth import browser_roots
from media_downloader import MediaError, file_signature, public_url

URL = "https://gemini.google.com/app"
HOST = "gemini.google.com"
EDITOR = 'rich-textarea [contenteditable="true"], [role="textbox"][contenteditable="true"], textarea'
RESPONSE = 'model-response, [data-message-author-role="model"]'
USER_TURN = 'user-query, [data-message-author-role="user"]'
TURN = f"{RESPONSE}, {USER_TURN}"
ANSWER = '.markdown, message-content, [data-testid="response-content"], [data-message-content]'
SITE_CHROME = f'body *:not(:is({TURN}, {EDITOR})):not(:is({TURN}, {EDITOR}) *)'
COMPOSER = 'form, input-area-v2, input-area, chat-input, .input-area-container, [data-testid="composer"]'
ATTACHMENT = (
    'uploader-file-preview, file-preview, uploaded-file, attachment-chip, file-chip, '
    '.file-preview, .attachment-chip, [data-file-name], [data-testid="attachment-chip"]'
)
OVERLAY = '[role="menu"], [role="listbox"], [role="dialog"], .cdk-overlay-pane'
POLL = 0.2
UI_TIMEOUT = 30

def _ci(pattern):
    return re.compile(pattern, re.IGNORECASE)


SIGN_IN = _ci(r"^(?:sign in|log in|登录|登入|登入帳戶|登錄)$")
VERIFY = _ci(
    r"verify (?:it['’]s|it is|you are|your identity)|unusual activity|captcha|"
    r"人机验证|人機驗證|验证您|验证你|验证身份|驗證身分|異常活動|异常活动|验证码|驗證碼"
)
LIMIT = _ci(
    r"\busage limit\b|\brate limit\b|\bquota (?:exceeded|exhausted)\b|\blimit reached\b|"
    r"\breached (?:your|the|a) [^.!?\n]{0,45}\blimit\b|\bout of (?:requests|credits)\b|"
    r"使用[上限额額]|已達到.{0,12}(?:限額|上限)|已达到.{0,12}(?:限额|上限)|"
    r"配额.{0,8}(?:用尽|耗尽|达到)|配額.{0,8}(?:用盡|耗盡|達到)"
)
UPLOAD_ERROR = _ci(
    r"(?:upload|attach)(?:ing|ment)? (?:failed|error)|"
    r"(?:unable to|couldn['’]t|can['’]t|failed to) (?:upload|attach)|"
    r"unsupported file|file (?:type )?(?:is )?too large|上传失败|上傳失敗|无法上传|無法上傳|文件过大|檔案過大"
)
GENERATION_ERROR = _ci(
    r"something went wrong|(?:generation|response) failed|couldn['’]t generate|"
    r"try again later|network error|出错了|發生錯誤|发生错误|生成失败|生成失敗|网络错误|網路錯誤|请稍后重试"
)
STATUS = '[role="alert"], [role="status"], [role="alertdialog"], [role="dialog"], mat-snack-bar-container, [aria-live="assertive"]'
CHECKPOINT = (
    'iframe[src*="/recaptcha/"], iframe[src*="hcaptcha.com/"], iframe[src*="challenges.cloudflare.com/"], '
    'iframe[title*="captcha" i], [data-testid="captcha"], .g-recaptcha, input[name="captcha"]'
)
SUBMIT = _ci(r"^(?:submit|send(?: message| prompt)?|发送(?:消息)?|提交|傳送|送出)$")
STOP = _ci(r"^(?:stop(?: (?:generating|generation|response))?|停止(?:生成|回答)?|停止回覆)$")
MODEL_PICKER = _ci(
    r"^(?:(?:open |select |choose |switch )?(?:the )?(?:gemini )?(?:model|mode)(?: picker| selector| menu)*"
    r"|.*(?:open mode picker|model picker).*|[a-z\d. \u2010-\u2015-]*(?:flash(?:[\s\u2010-\u2015-]?lite)?|pro|thinking|fast)|"
    r"auto|hydrogen|argon|carbon|"
    r"(?:选择|選擇|切换|切換)?(?:模型|模式)|快速|思考|专业版|專業版)$"
)
MODELS = {
    "flash-lite": _ci(r"\bflash[\s\u2010-\u2015-]*lite\b"),
    "flash": _ci(r"\bflash\b(?![\s\u2010-\u2015-]*lite\b)(?!.*\blite\b)|\bfast\b|快速"),
    "thinking": _ci(r"thinking|思考"),
    "pro": _ci(r"\bpro\b|专业版|專業版"),
}
ADD_FILES = _ci(r"^(?:add files|add (?:an? )?image|upload(?: files)?|attach(?: files)?|添加(?:文件|附件|图片)|新增(?:檔案|附件|圖片)|上传(?:文件|图片)?|上傳(?:檔案|圖片)?)$")
UPLOAD_FILES = _ci(r"^(?:upload(?: files)?|files|上传(?:文件)?|上傳(?:檔案)?)$")
REMOVE_FILE = _ci(r"^remove (?:file|attachment|image|upload|[^:]+\.[a-z\d]+)\b|^(?:移除|删除|刪除)(?:文件|檔案|附件|图片|圖片|[ :：]).*")
PENDING_UPLOAD = _ci(r"\b(?:uploading|attaching|processing (?:your )?(?:file|attachment))\b|(?:正在|正在处理|正在處理)(?:上传|上傳|处理|處理|附件|文件|檔案)")
PENDING_GENERATION = _ci(r"\b(?:generating|creating|working on|processing)\b|正在(?:生成|创建|建立|处理|處理)")
DOWNLOAD_IMAGE = _ci(r"^(?:download full[- ]size(?: image| picture)?|下载原图|下载完整尺寸(?:的)?(?:图片|图像)|下載原圖|下載完整尺寸(?:的)?(?:圖片|影像))$")
DOWNLOAD_VIDEO = _ci(r"^(?:download video|下载视频|下載影片|下載視訊)$")
SHARE_VIDEO = _ci(r"^(?:share video|分享(?:视频|影片|視訊))$")
SHARE = _ci(r"^(?:share|分享)$")
RESPONSE_ACTIONS = 'message-actions, .response-actions, [aria-label="Response actions"], [data-testid="response-actions"]'
VIDEO_CONTAINER = 'generated-video, video-player, video-generation-result, [data-generated-video], [data-testid="generated-video"]'
IMAGE_CONTAINER = 'generated-image, image-viewer, image-generation-result, [data-generated-image], [data-testid="generated-image"]'
VIDEO_RESULT = 'generated-video, video-generation-result, [data-testid="video-generation-result"], [data-testid="generated-video"]'
VIDEO_COMPOSER = 'video-generation-app, video-generation-page, [data-testid="video-composer"]'
SIDEBAR = 'nav, aside, [role="navigation"], side-nav, side-navigation'
ADD_IMAGE = _ci(r"^(?:add (?:an? )?image|添加图片|新增圖片|上传图片|上傳圖片)$")
SOURCES = _ci(r"^(?:sources(?: and related links)?|show sources|来源|查看来源|資料來源|參考來源)$")
SIGNATURES = {
    "image": {
        "image/png": (".png",), "image/jpeg": (".jpg", ".jpeg"), "image/webp": (".webp",),
        "image/gif": (".gif",), "image/bmp": (".bmp",), "image/avif": (".avif",),
        "image/tiff": (".tif", ".tiff"),
    },
    "video": {"video/mp4": (".mp4", ".mov"), "video/webm": (".webm",)},
}

@dataclass
class PageResult:
    text: str
    images: list[str]
    videos: list[str]

def _browser(config):
    if config.browser not in ("auto", "chrome", "edge"):
        raise MediaError("网页浏览器模式仅支持 Chrome 或 Edge。")
    names = ("chrome", "edge") if config.browser == "auto" else (config.browser,)
    paths = {"chrome": "Google/Chrome/Application/chrome.exe", "edge": "Microsoft/Edge/Application/msedge.exe"}
    for name in names:
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            root = os.environ.get(variable)
            if root and (Path(root) / paths[name]).is_file():
                return (Path(root) / paths[name]).resolve()
    raise MediaError("未找到 Chrome 或 Edge，请安装所选浏览器。")

def _profile(config):
    profile = Path(os.path.expandvars(config.page_profile_dir)).expanduser().resolve()
    roots = browser_roots()
    if any(profile.is_relative_to(roots[name].resolve()) for name in ("chrome", "edge") if name in roots):
        raise MediaError("网页端 Gemini 需要专用资料目录，请选用 Chrome 或 Edge 默认资料目录之外的文件夹。")
    return profile

def _lock(profile):
    profile.mkdir(parents=True, exist_ok=True)
    stream = (profile.parent / (profile.name + ".qqbot.lock")).open("a+b")
    stream.seek(0, os.SEEK_END)
    if not stream.tell():
        stream.write(b"0")
        stream.flush()
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise MediaError("专用浏览器正在使用，请关闭登录窗口或等待当前任务完成。") from None
    return stream

def _unlock(stream):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    stream.close()

def launch_page_login(config):
    profile = _profile(config)
    stream = _lock(profile)
    try:
        subprocess.Popen([str(_browser(config)), f"--user-data-dir={profile}", "--new-window", URL])
    except OSError:
        raise MediaError("无法启动网页端 Gemini 浏览器。") from None
    finally:
        _unlock(stream)

@asynccontextmanager
async def _page(config):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise MediaError("缺少 Playwright 依赖，请安装 requirements.txt。") from None
    profile = _profile(config)
    stream = _lock(profile)
    try:
        async with async_playwright() as playwright:
            context = None
            try:
                context = await playwright.chromium.launch_persistent_context(str(profile), executable_path=str(_browser(config)), headless=False, accept_downloads=True)
                blank = next((tab for tab in context.pages if tab.url == "about:blank"), None)
                page = blank if blank is not None else await context.new_page()
                page.set_default_timeout(min(config.timeout_seconds * 1000, 30000))
                await page.goto(URL, wait_until="domcontentloaded")
                yield page
            finally:
                if context is not None:
                    await context.close()
    finally:
        _unlock(stream)

async def _visible(locator):
    items = await _visible_items(locator)
    return items[0] if items else None


async def _visible_items(locator):
    indices = await locator.evaluate_all(
        """(elements) => elements.flatMap((element, index) => {
            if (typeof element.checkVisibility === 'function') {
                return element.checkVisibility({opacityProperty: true, visibilityProperty: true}) ? [index] : [];
            }
            const bounds = element.getBoundingClientRect();
            if (!bounds.width || !bounds.height || getComputedStyle(element).visibility !== 'visible') return [];
            for (let node = element; node; node = node.parentElement) {
                if (getComputedStyle(node).opacity === '0') return [];
            }
            return [index];
        })"""
    )
    return [locator.nth(index) for index in indices]


def _buttons(scope, name):
    return scope.get_by_role("button", name=name)


def _site(page, locator):
    return locator.and_(page.locator(SITE_CHROME))


def _https_url(value):
    try:
        url = urlsplit(value)
        if (url.scheme == "https" and url.hostname and url.port in (None, 443)
                and not (url.username or url.password) and not any(char in value for char in ("\r", "\n", "\\"))):
            return url
    except ValueError:
        pass
    return None


def _login_error():
    return MediaError("网页端 Gemini 需要登录，请打开专用浏览器完成登录。")


def _verification_error():
    return MediaError("网页端 Gemini 需要人工验证，请打开专用浏览器完成验证。")

async def _check_state(page):
    url = _https_url(page.url)
    if url is not None and (url.hostname == "accounts.google.com" or
                            (url.hostname == "www.google.com" and url.path.startswith("/sorry/"))):
        if "/challenge" in url.path or "/sorry/" in url.path or await _visible(page.locator(CHECKPOINT)):
            raise _verification_error()
        raise _login_error()
    if url is None or url.hostname != HOST or url.netloc.lower() not in (HOST, f"{HOST}:443"):
        raise MediaError("网页端 Gemini 跳转到其他页面，请检查登录状态。")
    if await _visible(_site(page, page.locator(CHECKPOINT))):
        raise _verification_error()
    notices = _site(page, page.locator(f':is({STATUS}):not(:has({TURN}, {EDITOR}))'))
    for notice in await _visible_items(notices):
        text = (await notice.inner_text()).strip()
        if VERIFY.search(text):
            raise _verification_error()
        if LIMIT.search(text):
            raise MediaError("网页端 Gemini 已达到使用限额或配额，请在限额恢复后再试。")
        if UPLOAD_ERROR.search(text):
            raise MediaError("网页端 Gemini 附件上传失败，请检查文件类型及大小。")
        if GENERATION_ERROR.search(text):
            raise MediaError("网页端 Gemini 显示操作失败，请检查连接或稍后重试。")
    login = _buttons(page, SIGN_IN).or_(page.get_by_role("link", name=SIGN_IN))
    if await _visible(_site(page, login)):
        raise _login_error()


async def _wait_control(page, locator, *, seconds=UI_TIMEOUT):
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        await _check_state(page)
        item = await _visible(locator)
        if item is not None or asyncio.get_running_loop().time() >= deadline:
            return item
        await asyncio.sleep(POLL)


async def _editor(page):
    deadline = asyncio.get_running_loop().time() + UI_TIMEOUT
    editors = page.locator(EDITOR).and_(page.locator(f':not(:is({TURN}) *)'))
    while asyncio.get_running_loop().time() < deadline:
        await _check_state(page)
        editor = await _visible(editors)
        if editor is not None and await editor.is_editable():
            return editor
        await asyncio.sleep(POLL)
    raise MediaError("未找到可编辑的 Gemini 输入框，请检查登录状态及网页界面。")


async def _edit_value(editor):
    return await editor.evaluate("(el) => el.isContentEditable ? el.innerText : el.value")


def _chat_action(page, name):
    return _site(page, _buttons(page, name).or_(page.get_by_role("link", name=name)))


async def _fresh_chat(page):
    await _editor(page)
    temporary = _chat_action(page, _ci(r"^(?:(?:start|turn on|open) )?temporary chat$|^临时(?:对话|聊天)$|^暫時(?:對話|聊天)$"))
    new = _chat_action(page, _ci(r"^(?:start )?new chat$|^新(?:建)?(?:对话|聊天)$|^(?:新(?:增)?|建立)(?:對話|聊天)$"))
    await _wait_control(page, temporary.or_(new), seconds=1)
    temporary_action = await _visible(temporary)
    if temporary_action is not None and await temporary_action.is_enabled():
        action = temporary_action
    else:
        action = await _visible(new)
    if action is not None and await action.is_enabled():
        if await action.get_attribute("aria-pressed") != "true":
            await action.click()
    else:
        action = None

    deadline = asyncio.get_running_loop().time() + UI_TIMEOUT
    ready_since = None
    while asyncio.get_running_loop().time() < deadline:
        editor = await _editor(page)
        url = _https_url(page.url)
        same_start = url is not None and url.path.rstrip("/") == "/app"
        clean = (
            (action is not None or same_start)
            and not await page.locator(TURN).count()
            and not (await _edit_value(editor)).strip()
            and await _visible(_site(page, page.locator(ATTACHMENT))) is None
            and await _visible(_site(page, _buttons(page, REMOVE_FILE))) is None
        )
        now = asyncio.get_running_loop().time()
        if clean:
            ready_since = now if ready_since is None else ready_since
            if now - ready_since >= 0.4:
                return editor
        else:
            ready_since = None
        await asyncio.sleep(POLL)
    raise MediaError("无法确认 Gemini 已打开空白对话，未提交任务。")


async def _composer_button(page, editor, name, *, seconds=UI_TIMEOUT):
    candidates = _site(page, _buttons(page, name))
    nearest = editor.locator("xpath=ancestor::*[self::form or self::input-area-v2 or self::input-area or self::chat-input or @data-testid='composer'][1]")
    if await nearest.count():
        button = await _visible(candidates.and_(nearest.locator("button, [role=button]")))
        if button is not None:
            return button
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        await _check_state(page)
        visible = await _visible_items(candidates)
        if len(visible) == 1:
            return visible[0]
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(POLL)

async def _model_options(page):
    selectors = page.locator('model-selector button, mode-picker button, button.model-selector, button.model-picker')
    button = await _wait_control(page, _site(page, _buttons(page, MODEL_PICKER).or_(selectors)), seconds=5)
    if button is None:
        return []
    await button.click()
    options = page.get_by_role("menuitem").or_(page.get_by_role("option")).or_(page.get_by_role("menuitemradio"))
    options = _site(page, options)
    await _wait_control(page, options, seconds=3)
    result = []
    for option in await _visible_items(options):
        label = (await option.inner_text()).strip()
        if label:
            # The visible label normally precedes secondary model descriptions.
            result.append((label.splitlines()[0].strip(), option))
    return result

async def _choose_model(page, model):
    desired = model.strip().casefold()
    if desired == "auto":
        return
    options = await _model_options(page)
    matches = [option for label, option in options if label.casefold() == desired]
    pattern = MODELS.get(desired)
    if not matches and pattern is not None:
        matches = [option for label, option in options if pattern.search(label)]
        if desired == "flash" and len(matches) > 1:
            matches = [option for label, option in options if pattern.search(label) and "flash" in label.casefold()]
    if len(matches) != 1 or not await matches[0].is_enabled():
        raise MediaError("网页端未显示所选模型或模型不可用。")
    await _check_state(page)
    await matches[0].click()

async def inspect_page_account(config):
    try:
        async with _page(config) as page:
            await _editor(page)
            models = []
            for label, option in await _model_options(page):
                name = next((alias for alias, pattern in MODELS.items() if pattern.search(label)), label)
                if any(item["name"] == name for item in models):
                    name = label
                models.append({"name": name, "display_name": label, "available": await option.is_enabled()})
            return {"source": "browser", "status": "connected", "models": models}
    except MediaError:
        raise
    except Exception as error:
        raise MediaError("无法检查网页端 Gemini，请关闭登录窗口并检查网页连接。") from error

async def _upload(page, editor, paths):
    if not paths:
        return
    button = await _composer_button(page, editor, ADD_FILES)
    if button is None:
        raise MediaError("未找到 Gemini 附件上传按钮。")
    chooser = asyncio.get_running_loop().create_future()

    def receive(file_chooser):
        if not chooser.done():
            chooser.set_result(file_chooser)

    page.on("filechooser", receive)
    try:
        await _check_state(page)
        await button.click()
        upload = page.get_by_role("menuitem", name=UPLOAD_FILES).or_(
            _buttons(page, UPLOAD_FILES).and_(page.locator(OVERLAY).locator("*"))
        )
        deadline = asyncio.get_running_loop().time() + 3
        picked = False
        while not chooser.done() and asyncio.get_running_loop().time() < deadline:
            await _check_state(page)
            option = await _visible(_site(page, upload))
            if not picked and option is not None and await option.is_enabled():
                await option.click()
                picked = True
                deadline = asyncio.get_running_loop().time() + 3
            elif not chooser.done():
                await asyncio.sleep(POLL)
        await _check_state(page)
        if chooser.done():
            await chooser.result().set_files([str(path) for path in paths])
            return
        inputs = _site(page, page.locator('input[type="file"]'))
        if picked or await inputs.count() != 1:
            raise MediaError("未找到 Gemini 文件选择控件。")
        await inputs.first.set_input_files([str(path) for path in paths])
    finally:
        page.remove_listener("filechooser", receive)


async def _attachments_ready(page, paths):
    if not paths:
        return True
    pending = page.get_by_text(PENDING_UPLOAD).and_(page.locator(f"{STATUS}, {ATTACHMENT}, :is({ATTACHMENT}) *"))
    pending = pending.or_(page.locator(
        f':is({COMPOSER}, {ATTACHMENT}) [role="progressbar"], '
        f':is({ATTACHMENT})[aria-busy="true"], :is({ATTACHMENT}) [aria-busy="true"], '
        f':is({ATTACHMENT})[data-status="uploading"], :is({ATTACHMENT}) .uploading'
    )).or_(page.get_by_role("progressbar", name=PENDING_UPLOAD))
    if await _visible(_site(page, pending)) is not None:
        return False
    remove_buttons = await _visible_items(_site(page, _buttons(page, REMOVE_FILE)))
    known = Counter(path.name for path in paths)
    if len(remove_buttons) >= len(paths):
        return True
    for name, count in known.items():
        named = page.get_by_text(name, exact=True).or_(page.get_by_title(name, exact=True)).or_(
            page.locator("[data-file-name]").filter(has_text=name)
        )
        if len(await _visible_items(_site(page, named))) < count:
            return False
    return True


async def _choose_tool(page, editor, kind):
    if kind == "text":
        return False
    pattern = _ci(
        r"^(?:create|generate) images?$|^(?:生成|创建|建立)(?:图片|圖片|圖像)$" if kind == "image"
        else r"^(?:create|generate) videos?$|^(?:生成|创建|建立)(?:视频|影片|視訊)$"
    )
    button = await _composer_button(page, editor, _ci(r"^(?:tools|open tools menu|工具)$"), seconds=1)
    if button is not None:
        await button.click()
        option = page.get_by_role("menuitem", name=pattern).or_(
            _buttons(page, pattern).and_(page.locator(OVERLAY).locator("*"))
        )
        option = await _wait_control(page, _site(page, option), seconds=2)
        if option is not None and await option.is_enabled():
            await option.click()
            return False
        await page.keyboard.press("Escape")
    if kind != "video":
        return False
    entries = _chat_action(page, pattern).and_(page.locator(SIDEBAR).locator("*"))
    entry = await _wait_control(page, entries, seconds=1)
    if entry is None or not await entry.is_enabled():
        return False
    await entry.click()
    surface = page.locator(VIDEO_COMPOSER).or_(_buttons(page, ADD_IMAGE))
    if await _wait_control(page, _site(page, surface), seconds=5) is None:
        raise MediaError("无法确认 Gemini 已打开视频创作页面，未提交任务。")
    if await _visible(_site(page, page.locator(VIDEO_RESULT))) is not None:
        raise MediaError("Gemini 视频创作页面已有生成结果，未提交任务。")
    return True


async def _submit(page, editor, prompt, paths):
    if await page.locator(TURN).count() or (await _edit_value(editor)).strip():
        raise MediaError("Gemini 输入框已有内容，未提交任务。")
    prompt = prompt.replace("\r\n", "\n").replace("\r", "\n")
    await editor.fill(prompt)
    await _upload(page, editor, paths)
    send = await _composer_button(page, editor, SUBMIT)
    if send is None:
        raise MediaError("未找到 Gemini 发送按钮。")

    ready_since = None
    while True:
        await _check_state(page)
        if await page.locator(TURN).count() or (await _edit_value(editor)).strip() != prompt.strip():
            raise MediaError("Gemini 输入框状态发生变化，未提交任务。")
        ready = await send.is_visible() and await send.is_enabled() and await _attachments_ready(page, paths)
        now = asyncio.get_running_loop().time()
        if ready:
            ready_since = now if ready_since is None else ready_since
            if now - ready_since >= (0.4 if paths else 0):
                await _check_state(page)
                await send.click()
                return
        else:
            ready_since = None
        await asyncio.sleep(POLL)


async def _answer_text(response):
    content = await _visible_items(response.locator(f':is({ANSWER}):not(:is({ANSWER}) :is({ANSWER}))'))
    if content:
        paragraphs = [(await item.inner_text()).strip() for item in content]
        return "\n\n".join(filter(None, paragraphs))
    return (await response.evaluate(
        """(element, excluded) => {
            const text = (node) => {
                if (node.nodeType === Node.TEXT_NODE) return node.nodeValue;
                if (node.nodeType !== Node.ELEMENT_NODE || node.matches(excluded) || !node.getClientRects().length) return '';
                if (node.tagName === 'BR') return '\\n';
                if (!node.querySelector(excluded)) return node.innerText;
                const value = Array.from(node.childNodes, text).join('');
                return getComputedStyle(node).display.startsWith('inline') ? value : `\\n${value}\\n`;
            };
            return Array.from(element.childNodes, text).join('');
        }""",
        f'button, [role="button"], [role="toolbar"], [role="menu"], [role="status"], [role="alert"], '
        f'[aria-hidden="true"], {RESPONSE_ACTIONS}, {VIDEO_CONTAINER}, {IMAGE_CONTAINER}',
    )).strip()


def _response_controls(response, locator):
    return locator.and_(response.locator(f':not(:is({ANSWER}) *)'))


async def _source_links(response):
    links = response.locator('a[href]')
    toggle = await _visible(_response_controls(response, _buttons(response, SOURCES)))
    if toggle is not None and await toggle.is_enabled():
        if await toggle.get_attribute("aria-expanded") != "true":
            await _check_state(response.page)
            await toggle.click()
        pane_id = await toggle.get_attribute("aria-controls")
        if pane_id and re.fullmatch(r"[\w:-]+", pane_id):
            links = links.or_(response.page.locator(f'[id="{pane_id}"] a[href]'))
        deadline = asyncio.get_running_loop().time() + 1
        while not await _visible(links) and asyncio.get_running_loop().time() < deadline:
            await _check_state(response.page)
            await asyncio.sleep(POLL)
    sources = []
    for link in await _visible_items(links):
        href = await link.get_attribute("href")
        try:
            source = public_url(href or "")
        except MediaError:
            continue
        if urlsplit(source).hostname not in (HOST, "accounts.google.com") and source not in sources:
            sources.append(source)
    return sources


def _download_control(scope, name):
    return _buttons(scope, name).or_(scope.get_by_role("menuitem", name=name)).or_(scope.get_by_role("link", name=name))


async def _save_download(page, button, kind, directory, index):
    await _check_state(page)
    async with page.expect_download() as pending:
        await button.click()
    download = await pending.value
    url = _https_url(download.url.removeprefix("blob:"))
    google_hosts = ("googleusercontent.com", "usercontent.google.com", "googlevideo.com", "gstatic.com", "googleapis.com")
    if url is None or not any(url.hostname == host or url.hostname.endswith("." + host) for host in (HOST, *google_hosts)):
        raise MediaError("网页端生成文件的下载地址不受支持。")
    suffix = Path(download.suggested_filename).suffix.lower()
    expected = SIGNATURES[kind]
    if not any(suffix in suffixes for suffixes in expected.values()):
        raise MediaError("网页端下载文件的扩展名不受支持。")
    target = directory / f"generated-{kind}-{index}{suffix}"
    with tempfile.TemporaryDirectory(prefix=".gemini-download-", dir=directory) as staging:
        item = Path(staging) / ("media" + suffix)
        await download.save_as(str(item))
        signature = await asyncio.to_thread(file_signature, item, kind)
        if suffix not in expected.get(signature, ()):
            raise MediaError("网页端下载文件的格式与扩展名不一致。")
        try:
            target.touch(exist_ok=False)
        except FileExistsError:
            raise MediaError("生成文件路径已存在，请检查任务目录。") from None
        try:
            os.replace(item, target)
        except OSError:
            target.unlink(missing_ok=True)
            raise
    return str(target)


async def _image_button(response, image, count):
    await image.hover(timeout=2500)
    buttons = _download_control(response, DOWNLOAD_IMAGE)
    if await _wait_control(response.page, buttons, seconds=0.6) is None:
        return None
    parent = image.locator("xpath=..")
    while await parent.count():
        if await parent.evaluate("(element, selector) => element.matches(selector)", f"{RESPONSE}, body"):
            break
        local = await _visible_items(buttons.and_(parent.locator("*")))
        if len(local) == 1:
            return local[0]
        parent = parent.locator("xpath=..")
    visible = await _visible_items(buttons)
    if count == 1 and len(visible) == 1:
        return visible[0]
    return None


async def _download_images(response, directory):
    paths = []
    images = response.locator("img").and_(response.locator(f':not(:is({RESPONSE_ACTIONS}) *)'))
    targets = await _visible_items(images)
    if not targets:
        targets = await _visible_items(response.locator(IMAGE_CONTAINER))
    for image in targets:
        button = await _image_button(response, image, len(targets))
        if button is not None:
            paths.append(await _save_download(response.page, button, "image", directory, len(paths) + 1))
    return paths


async def _video_share(response, video):
    specific = _buttons(response, SHARE_VIDEO)
    generic = _buttons(response, SHARE)
    candidates = specific.or_(generic).and_(response.locator(f':not(:is({RESPONSE_ACTIONS}) *)'))
    container = video.locator("xpath=ancestor-or-self::*[self::generated-video or self::video-player or self::video-generation-result or @data-generated-video or @data-testid='generated-video'][1]")
    if await container.count() and (share := await _visible(candidates.and_(container.locator("*")))) is not None:
        return share
    if await response.locator("video").count() != 1:
        return None
    if (share := await _visible(_response_controls(response, specific))) is not None:
        return share
    # In the inline layout, the generic video Share action follows the player.
    nearby = video.locator("xpath=following-sibling::*[1]")
    return await _visible(generic.and_(nearby.or_(nearby.locator("*"))))


async def _download_videos(response, directory):
    paths = []
    page = response.page
    videos = await _visible_items(response.locator("video"))
    if not videos:
        videos = await _visible_items(response.locator(VIDEO_CONTAINER))
    candidates = _download_control(page, DOWNLOAD_VIDEO).and_(page.locator(OVERLAY).locator("*")).or_(
        _download_control(response, DOWNLOAD_VIDEO)
    )
    for video in videos:
        if await _visible(candidates) is not None:
            raise MediaError("Gemini 视频下载菜单已打开，无法确认属于当前视频。")
        share = await _video_share(response, video)
        if share is None:
            continue
        await _check_state(page)
        await share.click()
        download = await _wait_control(page, candidates, seconds=5)
        if download is None:
            raise MediaError("Gemini 视频分享菜单未提供「下载视频」。")
        if len(await _visible_items(candidates)) != 1:
            raise MediaError("Gemini 视频下载控件有多个，无法确定当前视频。")
        paths.append(await _save_download(page, download, "video", directory, len(paths) + 1))
        if await _visible(candidates) is not None:
            await page.keyboard.press("Escape")
    return paths


async def _download(response, kind, directory):
    if kind == "image":
        return await _download_images(response, directory)
    return await _download_videos(response, directory)


async def _response_state(page, response):
    busy = _site(page, _buttons(page, STOP)).or_(_response_controls(response, _buttons(response, STOP)))
    pending = response.locator(
        ':scope[aria-busy="true"], :scope[data-status="streaming"], :scope[data-status="generating"], '
        '[aria-busy="true"], [data-status="streaming"], [data-status="generating"], [role="progressbar"]'
    )
    pending = pending.or_(response.get_by_text(PENDING_GENERATION).and_(response.locator(STATUS)))
    busy = busy.or_(_response_controls(response, pending))
    if await _visible(busy) is not None:
        return "busy"
    errors = response.locator(
        'response-error, message-error, .response-error, [data-status="error"], [data-testid="response-error"]'
    )
    if await _visible(_response_controls(response, errors)) is not None:
        raise MediaError("网页端 Gemini 显示生成失败，请检查连接与生成权限。")
    notices = response.get_by_text(GENERATION_ERROR).and_(response.locator(STATUS))
    if await _visible(_response_controls(response, notices)) is not None:
        raise MediaError("网页端 Gemini 显示生成失败，请检查连接与生成权限。")
    completed = response.locator(
        f':scope[data-status="complete"], :scope[data-status="completed"], :scope[data-status="done"], {RESPONSE_ACTIONS}'
    )
    return "complete" if await _visible(completed) is not None else "idle"


async def _await_result(page, kind, directory, *, dedicated=False):
    selectors = f"{RESPONSE}, {VIDEO_RESULT}" if dedicated else RESPONSE
    responses = page.locator(f':is({selectors}):not(:is({selectors}) *)')
    prior = None
    stable_since = None
    observed_busy = False
    while True:
        await _check_state(page)
        response = await _visible(responses)
        if response is None:
            await asyncio.sleep(POLL)
            continue
        state = await _response_state(page, response)
        if state == "busy":
            observed_busy = True
            stable_since = None
            await asyncio.sleep(POLL)
            continue
        text = await _answer_text(response)
        media_counts = (
            await response.locator(f"img, {IMAGE_CONTAINER}").count(),
            await response.locator(f"video, {VIDEO_CONTAINER}").count(),
        )
        current = (text, media_counts, state)
        now = asyncio.get_running_loop().time()
        if current != prior or stable_since is None:
            stable_since = now
            prior = current
        done = state == "complete" or observed_busy
        stable = now - stable_since
        ready_text = bool(text) and kind == "text"
        ready_media = (kind != "video" and media_counts[0]) or (kind != "image" and media_counts[1])
        if stable >= (0.8 if done else 3.0) and (ready_text or ready_media or done and stable >= 3.0):
            images = await _download(response, "image", directory) if directory is not None and media_counts[0] and kind != "video" else []
            videos = await _download(response, "video", directory) if directory is not None and media_counts[1] and kind != "image" else []
            if kind == "image" and not images:
                if done:
                    raise MediaError("网页端 Gemini 没有返回可下载的生成图片。")
            elif kind == "video" and not videos:
                if done:
                    raise MediaError("网页端 Gemini 没有返回可下载的生成视频。")
            elif text or images or videos:
                sources = await _source_links(response) if text else []
                if sources:
                    text += "\n\n来源：\n" + "\n".join(sources)
                return PageResult(text, images, videos)
            elif done:
                raise MediaError("网页端 Gemini 返回了空结果。")
        await asyncio.sleep(POLL)

async def generate_page_content(config, prompt, *, files=(), model="auto", media_kind="text", directory=None):
    if media_kind not in ("text", "image", "video"):
        raise MediaError("不支持此网页端 Gemini 任务类型。")
    if not isinstance(prompt, str) or not prompt.strip():
        raise MediaError("网页端 Gemini 提示词不能为空。")
    destination = Path(directory).resolve() if directory is not None else None
    if (destination is None and media_kind != "text") or (destination is not None and not destination.is_dir()):
        raise MediaError("网页端 Gemini 任务目录不存在。")
    try:
        paths = [Path(item).resolve(strict=True) for item in files]
        if any(not path.is_file() for path in paths):
            raise MediaError("网页端 Gemini 附件不存在或不是文件。")
        async with asyncio.timeout(config.timeout_seconds):
            async with _page(config) as page:
                editor = await _fresh_chat(page)
                await _choose_model(page, model or "auto")
                dedicated = await _choose_tool(page, editor, media_kind)
                editor = await _editor(page)
                await _submit(page, editor, prompt, paths)
                return await _await_result(page, media_kind, destination, dedicated=dedicated)
    except MediaError:
        raise
    except TimeoutError:
        raise MediaError("网页端 Gemini 操作超时，请检查网页状态，任务不会自动重发。") from None
    except Exception as error:
        raise MediaError("网页端 Gemini 操作失败，请检查登录窗口、网页界面和网络，任务不会自动重发。") from error
