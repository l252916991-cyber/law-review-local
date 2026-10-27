"""Explicit judge transport using the existing endpoint and host egress gates."""
from __future__ import annotations

import json
import re
import time
import urllib.request
from typing import Any

from .answer_eval import JUDGE_INSTRUCTION
from .forge_governance import governed_backend
from .services import assert_model_endpoint_allowed, egress_opener, read_json_with_deadline


_THINK = re.compile(r"<think[\s\S]*?</think>", re.IGNORECASE)


def strip_think_only(text: str) -> str:
    """Drop reasoning blocks but keep JSON that merely mentions planning words.

    ``services.strip_reasoning`` also blanks answers whose first 240 characters
    look like planning text; a judge rationale such as "问题要求…" triggered it and
    turned a valid judgement into an empty response.
    """
    return _THINK.sub("", text or "").strip()


@governed_backend("llm")
def request_judge(prompt: str, *, endpoint: str, model: str, calls: list[dict[str, Any]], timeout: int = 30,
                  max_tokens: int = 500) -> str:
    assert_model_endpoint_allowed(endpoint)
    started = time.perf_counter()
    observation: dict[str, Any] = {"model": model, "status": "failed", "usage": None, "estimated_cost": None}
    try:
        body = {"model": model, "messages": [{"role": "system", "content": JUDGE_INSTRUCTION},
                 {"role": "user", "content": prompt}], "temperature": 0, "max_tokens": max_tokens,
                "stream": False, "chat_template_kwargs": {"enable_thinking": False}}
        request = urllib.request.Request(endpoint.rstrip("/") + "/chat/completions",
                                         data=json.dumps(body, ensure_ascii=False).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        deadline = time.monotonic() + timeout
        with egress_opener().open(request, timeout=timeout) as response:
            payload = read_json_with_deadline(response, timeout, deadline=deadline)
        observation["usage"] = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
        text = strip_think_only(payload["choices"][0]["message"]["content"])
        if not text:
            raise ValueError("empty judge response")
        observation["status"] = "completed"
        return text
    except Exception as exc:
        observation["error_type"] = type(exc).__name__
        raise
    finally:
        observation["latency_ms"] = round((time.perf_counter() - started) * 1000)
        calls.append(observation)
