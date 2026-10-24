"""Optional vision-model transcription for scanned pages.

Scanned legal filings lose structure under classic OCR: tables collapse, seals
and handwriting are dropped. This adapter sends one rendered page to an
OpenAI-compatible vision endpoint and returns its transcription.

It is opt-in and strictly additive: any failure (disabled, misconfigured,
timeout, malformed response) raises and the caller falls back to the existing
Tesseract path. The adapter never writes text itself, so a failed vision call
cannot silently produce an empty page.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .services import assert_model_endpoint_allowed, egress_opener, read_json_with_deadline, strip_reasoning

MAX_IMAGE_BYTES = 12 * 1024 * 1024
DEFAULT_TIMEOUT = 120
TRANSCRIBE_PROMPT = (
    "请逐字转录这张法律文书扫描页的文字。保留段落顺序；表格用“单元格 | 单元格”逐行输出；"
    "印章与手写批注单独标注为【印章】/【手写】。无法辨认的字用“□”表示。"
    "只输出转录结果，不要解释、不要总结、不要补充图片中没有的内容。"
)


class VlmUnavailable(RuntimeError):
    """Vision transcription is disabled or not configured."""


class VlmError(RuntimeError):
    """The vision endpoint was reachable but did not return usable text."""


def vlm_enabled() -> bool:
    return os.getenv("LAW_REVIEW_VLM_OCR", "off").strip().lower() in {"1", "on", "true", "yes"}


def vlm_settings() -> tuple[str, str]:
    """Return ``(base_url, model)`` or raise when misconfigured."""
    if not vlm_enabled():
        raise VlmUnavailable("LAW_REVIEW_VLM_OCR 未开启")
    url = os.getenv("LAW_REVIEW_VLM_URL", "").strip().rstrip("/")
    model = os.getenv("LAW_REVIEW_VLM_MODEL", "").strip()
    if not url or not model:
        raise VlmUnavailable("LAW_REVIEW_VLM_URL 或 LAW_REVIEW_VLM_MODEL 为空")
    try:
        assert_model_endpoint_allowed(url)
    except ValueError as exc:
        raise VlmUnavailable(f"视觉端点不被允许：{exc}") from exc
    return url, model


def describe_status() -> dict[str, Any]:
    """Readiness probe helper; never raises."""
    try:
        url, model = vlm_settings()
    except VlmUnavailable as exc:
        return {"enabled": vlm_enabled(), "available": False, "reason": str(exc)}
    return {"enabled": True, "available": True, "model": model, "base_url": url}


def image_to_text(path: str | Path, *, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Transcribe one image through the configured vision endpoint."""
    url, model = vlm_settings()
    image_path = Path(path)
    payload_bytes = image_path.read_bytes()
    if not payload_bytes:
        raise VlmError("图片为空")
    if len(payload_bytes) > MAX_IMAGE_BYTES:
        raise VlmError(f"图片超过视觉解析上限（{MAX_IMAGE_BYTES} 字节）")
    mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": TRANSCRIBE_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{base64.b64encode(payload_bytes).decode('ascii')}"},
                    },
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": 1600,
        "stream": False,
    }
    request = urllib.request.Request(
        f"{url}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    deadline = time.monotonic() + timeout
    try:
        with egress_opener().open(request, timeout=timeout) as response:
            payload = read_json_with_deadline(response, timeout, deadline=deadline)
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
        raise VlmError(f"视觉解析失败（{type(exc).__name__}）") from exc
    choices = payload.get("choices") if isinstance(payload, dict) else None
    content = (choices or [{}])[0].get("message", {}).get("content") if choices else None
    text = strip_reasoning(content or "").strip()
    if not text:
        raise VlmError("视觉端点返回空文本或推理草稿")
    return text
