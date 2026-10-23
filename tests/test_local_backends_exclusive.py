from __future__ import annotations

import io
import json
import time
import urllib.error

import pytest

from app.local_backends import LocalModels
from tests.test_domain_writes import domain  # noqa: F401
from tests.test_host_writes import isolated  # noqa: F401
from tests.test_local_backends import local_models  # noqa: F401

CHAT_BODY = {
    "model": "llm",
    "choices": [{"message": {"content": "合成回答 [资料1]。请律师复核。"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 2},
}


class _FakeResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode()
        self.closed = False

    def read1(self, size=-1):
        chunk = self._body[:size]
        self._body = self._body[size:]
        return chunk

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _FakeOpener:
    def __init__(self, unloads=None, chat_body=None):
        self.unloads = unloads or {}
        self.chat_body = chat_body or CHAT_BODY
        self.requests = []

    def open(self, request, timeout=None):
        url = request.full_url
        self.requests.append((url, request.get_method(), request.data, timeout))
        if url.endswith("/unload"):
            captured = url.rsplit("/", 2)[-2]
            outcome = self.unloads.get(captured, ("not_loaded", None, None))
            if outcome[0] == "http":
                payload = json.dumps({"error": {"message": outcome[2], "type": "invalid_request_error"}}).encode()
                raise urllib.error.HTTPError(url, outcome[1], "unload failed", {}, io.BytesIO(payload))
            if outcome[0] == "timeout":
                raise TimeoutError("unload request timed out")
            return _FakeResponse({"status": "ok", "model_id": captured})
        return _FakeResponse(self.chat_body)


def _models(fixture, *, exclusive_residency=False):
    names = {kind: kind for kind in ("llm", "embedding", "reranker")}
    return LocalModels(endpoint="http://127.0.0.1:1/v1", identities=fixture.identities, models=names,
                       transport=None, exclusive_residency=exclusive_residency)


def _llm_payload(models):
    return {"model": models.models["llm"], "messages": [{"role": "user", "content": "合成测试"}],
            "temperature": models.temperature, "max_tokens": models.max_tokens, "stream": False}


def test_exclusive_residency_unloads_registered_models_before_inference(local_models, monkeypatch):
    fixture, _ = local_models
    models = _models(fixture, exclusive_residency=True)
    opener = _FakeOpener()
    monkeypatch.setattr("app.local_backends.egress_opener", lambda: opener)

    body = models.invoke("llm", _llm_payload(models), 1)

    assert body["usage"] == {"prompt_tokens": 7, "completion_tokens": 2}
    unload_urls = [url for url, _, _, _ in opener.requests if url.endswith("/unload")]
    assert [url.rsplit("/", 2)[-2] for url in unload_urls] == ["embedding", "llm", "reranker"]
    assert opener.requests[-1][0].endswith("/chat/completions")
    assert all(method == "POST" for _, method, _, _ in opener.requests)


def test_exclusive_residency_accepts_the_admin_detail_error_shape(local_models, monkeypatch):
    fixture, _ = local_models
    models = _models(fixture, exclusive_residency=True)
    opener = _FakeOpener()
    original_open = opener.open

    def open_admin_shape(request, timeout=None):
        if request.full_url.endswith("/unload"):
            url = request.full_url
            opener.requests.append((url, request.get_method(), request.data, timeout))
            captured = url.rsplit("/", 2)[-2]
            payload = json.dumps({"detail": f"Model not loaded: {captured}"}).encode()
            raise urllib.error.HTTPError(url, 400, "unload failed", {}, io.BytesIO(payload))
        return original_open(request, timeout)

    opener.open = open_admin_shape
    monkeypatch.setattr("app.local_backends.egress_opener", lambda: opener)

    body = models.invoke("llm", _llm_payload(models), 1)

    assert body["usage"]["completion_tokens"] == 2
    assert opener.requests[-1][0].endswith("/chat/completions")


def test_exclusive_residency_policy_is_bound_to_model_identity(local_models):
    fixture, _ = local_models
    bound = _models(fixture, exclusive_residency=True)
    plain = _models(fixture, exclusive_residency=False)

    assert bound.binding["local_service_policy"] == "omlx-exclusive-residency-v1"
    assert "local_service_policy" not in plain.binding
    assert bound.fingerprint != plain.fingerprint
    bound.assert_current()


def test_exclusive_residency_rejects_an_unexpected_unload_error(local_models, monkeypatch):
    fixture, _ = local_models
    models = _models(fixture, exclusive_residency=True)
    opener = _FakeOpener(unloads={"llm": ("http", 500, "unload failed")})
    monkeypatch.setattr("app.local_backends.egress_opener", lambda: opener)

    with pytest.raises(RuntimeError, match="residency admission"):
        models.invoke("llm", _llm_payload(models), 1)

    assert not any(url.endswith("/chat/completions") for url, _, _, _ in opener.requests)


def test_exclusive_residency_propagates_an_unload_timeout(local_models, monkeypatch):
    fixture, _ = local_models
    models = _models(fixture, exclusive_residency=True)
    opener = _FakeOpener(unloads={"embedding": ("timeout", None, None)})
    monkeypatch.setattr("app.local_backends.egress_opener", lambda: opener)

    with pytest.raises(TimeoutError, match="unload request timed out"):
        models.invoke("llm", _llm_payload(models), 1)

    assert not any(url.endswith("/chat/completions") for url, _, _, _ in opener.requests)


def test_exclusive_residency_deadline_prevents_any_unload_request(local_models, monkeypatch):
    fixture, _ = local_models
    models = _models(fixture, exclusive_residency=True)
    opener = _FakeOpener()
    monkeypatch.setattr("app.local_backends.egress_opener", lambda: opener)

    with pytest.raises(TimeoutError, match="original deadline"):
        models._release_registered_models(time.monotonic() - 1)
    assert opener.requests == []
