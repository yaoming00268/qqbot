"""Backend options shared by QQ command parsing and the console API."""
from __future__ import annotations

import re


WEB_TASKS = {"search", "task", "command", "process"}
GENERATION_TASKS = {"generate_image", "generate_video"}
PROVIDERS = {"native", "api", "gemini_web"}
SIZE_PATTERN = r"auto|[1-9][0-9]{1,4}x[1-9][0-9]{1,4}"
OPTION = re.compile(r'''\A--(?P<key>provider|model|count|size)(?:=|\s+)(?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^\s]+)(?=\s|$)''')


def backend_options(kind, provider="", model=""):
    if not isinstance(provider, str) or not isinstance(model, str):
        raise ValueError("后端和模型必须是字符串。")
    provider = provider.strip().lower().replace("-", "_")
    model = model.strip()
    if model and not provider:
        provider = "gemini_web"
    if provider and provider not in PROVIDERS:
        raise ValueError("后端支持 native、api、gemini_web。")
    if provider and ((kind not in GENERATION_TASKS and (kind not in WEB_TASKS or provider == "api"))
                     or kind == "generate_video" and provider == "api"):
        raise ValueError("该命令不支持此后端；网页端可用于生图、生成视频、搜索、任务、执行和附件分析；api 仅支持生图。")
    if model and (provider == "native" or len(model) > 200 or any(ord(c) < 32 for c in model)):
        raise ValueError("模型参数无效；native 沿用反重力模型设置，网页端请使用 --provider gemini_web --model pro。")
    return {**({"provider": provider} if provider else {}), **({"model": model} if model else {})}


def parse_backend_options(text, kind):
    """Consume only leading options; preserve the original prompt and newlines."""
    remaining, options = text.strip(), {}
    while remaining.startswith("--"):
        if remaining == "--" or remaining.startswith("-- ") or remaining.startswith("--\n"):
            remaining = remaining[2:].lstrip()
            break
        match = OPTION.match(remaining)
        if not match:
            raise ValueError("命令选项无效；把 --provider/--model 放在正文前，正文以 -- 开头时先加 -- 分隔。")
        key, value = match["key"], match["value"]
        if key in options or (key in ("count", "size") and kind != "generate_image"):
            raise ValueError("命令选项重复或不适用于该命令。")
        if value[:1] in ("'", '"'):
            value = value[1:-1]
        if not value or value.startswith("--"):
            raise ValueError("命令选项缺少值。")
        options[key] = value
        remaining = remaining[match.end():].lstrip()
    if not remaining:
        raise ValueError("选项之后需要完整的任务描述。")
    route = backend_options(kind, options.get("provider", ""), options.get("model", ""))
    if "count" in options:
        if not re.fullmatch(r"[1-9]|10", options["count"]):
            raise ValueError("生成数量需要是 1–10。")
        route["count"] = int(options["count"])
    if "size" in options:
        if not re.fullmatch(SIZE_PATTERN, options["size"]):
            raise ValueError("图片尺寸需要是 auto 或宽x高。")
        route["size"] = options["size"]
    return remaining, route
