"""QQBot's lightweight BERT inference adapter; no training/torch dependencies."""
from __future__ import annotations

import json
import math
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener


def predict(service_file, texts):
    try:
        service = json.loads(Path(service_file).expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError("请先在 emotion_studio 中加载 BERT 分析模型，启动本地分析服务。") from error
    address = service.get("url", "")
    parsed = urlparse(address)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.path not in ("", "/"):
        raise ValueError("BERT 服务描述必须使用本机 HTTP 地址。")
    if not isinstance(service.get("token"), str) or not service["token"]:
        raise ValueError("BERT 服务缺少令牌，请重新加载分析模型。")
    request = Request(address.rstrip("/") + "/predict", json.dumps({"texts": texts}, ensure_ascii=False).encode("utf-8"),
                      {"Content-Type": "application/json", "X-Emotion-Token": service["token"]})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=180) as response:
            result = json.load(response)
    except HTTPError as error:
        detail = json.loads(error.read().decode("utf-8"))
        raise RuntimeError("BERT 分析失败：" + str(detail.get("error", error))) from error
    except URLError as error:
        raise RuntimeError("无法连接 BERT 本地分析服务，请在情感工作台重新加载模型。") from error
    rows = result.get("predictions")
    if not isinstance(rows, list) or len(rows) != len(texts):
        raise ValueError("BERT 服务返回的句子数量不匹配。")
    for text, row in zip(texts, rows):
        if not isinstance(row, dict):
            raise ValueError("BERT 服务返回的预测格式无效。")
        probabilities = row.get("probabilities") if isinstance(row, dict) else None
        if (row.get("text") != text or not isinstance(probabilities, dict) or row.get("label") not in probabilities
                or any(not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities.values())
                or not math.isclose(sum(probabilities.values()), 1, abs_tol=.001)):
            raise ValueError("BERT 服务返回的标签或概率无效。")
    return rows
