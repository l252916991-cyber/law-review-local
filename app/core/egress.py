"""Outbound HTTP: the model endpoint allowlist, a redirect-refusing opener and a JSON reader
that enforces a wall-clock deadline."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
REMOTE_MODEL_APPROVAL_ENV = "LAW_REVIEW_ALLOW_REMOTE_MODELS"
LOCAL_MODEL_HOSTS_ENV = "LAW_REVIEW_LOCAL_MODEL_HOSTS"


def assert_model_endpoint_allowed(base_url: str) -> None:
    """Case text must not leave this machine without explicit deployment approval.

    Loopback model endpoints are always allowed. Any other destination requires
    LAW_REVIEW_ALLOW_REMOTE_MODELS=1 and https, so an operator cannot silently
    point case data at an unapproved remote service.
    """
    parsed = urllib.parse.urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if not parsed.scheme or not host:
        raise ValueError("模型服务地址无效")
    configured_local_hosts = {
        item.strip().lower()
        for item in os.getenv(LOCAL_MODEL_HOSTS_ENV, "").split(",")
        if item.strip()
    }
    if host in LOOPBACK_HOSTS | configured_local_hosts and parsed.scheme in {"http", "https"}:
        return
    if os.getenv(REMOTE_MODEL_APPROVAL_ENV) != "1":
        raise ValueError("外发模型服务未获批准：仅允许本机回环地址，或显式配置 LAW_REVIEW_ALLOW_REMOTE_MODELS=1")
    if parsed.scheme != "https":
        raise ValueError("远端模型服务必须使用 https")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a server cannot relay request bodies elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def egress_opener() -> urllib.request.OpenerDirector:
    """Opener with system proxies disabled and redirects refused."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def read_json_with_deadline(
    response: Any, timeout: float, *, deadline: float | None = None, max_bytes: int = 8 * 1024 * 1024,
) -> dict[str, Any]:
    """Bound urllib response-body reads, including a stalled read after keepalive.

    urllib exposes its socket through HTTPResponse.fp.raw. Resetting its timeout
    for *each* read prevents a final blocking read from gaining another full
    timeout window. In-memory/test streams have no transport to configure.
    This does not independently interrupt OS DNS resolution or header parsing.
    """
    deadline = time.monotonic() + timeout if deadline is None else deadline
    payload = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            response.close()
            raise TimeoutError(f"超过 {timeout} 秒总时限")
        transport = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
        if transport is not None:
            transport.settimeout(remaining)
        reader = getattr(response, "read1", None)
        if not callable(reader):
            reader = response.read
        try:
            chunk = reader(64 * 1024)
        except TimeoutError as exc:
            response.close()
            raise TimeoutError(f"超过 {timeout} 秒总时限") from exc
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > max_bytes:
            response.close()
            raise ValueError("本地模型响应超过大小限制")
    return json.loads(payload)
