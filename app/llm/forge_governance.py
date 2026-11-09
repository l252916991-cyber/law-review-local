"""Host-owned backend admission for the isolated E05 engineering path.

The ContextVar is installed only while an authorized snapshot node executes.
Existing product entry points retain their own policy until E14. No endpoint or
credential is taken from a run payload. Offline mode rejects network backends
before execution, records unknown usage honestly, and bounds calls/input/deadline.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any


class BackendAdmissionError(RuntimeError):
    pass


@dataclass(frozen=True)
class OfflineModelTest:
    """Trusted test configuration, never client-selected model configuration."""
    scenario: str = "echo"
    max_steps: int = 2
    max_input_tokens: int = 100_000
    max_output_tokens: int = 20_000
    max_context_tokens: int = 32_000

    def validate(self) -> None:
        if not isinstance(self.scenario, str) or self.scenario not in {"echo", "empty", "provider_error", "invalid_quote", "protocol", "over_usage"}:
            raise ValueError("unsupported offline model scenario")
        for value in (self.max_steps, self.max_input_tokens, self.max_output_tokens, self.max_context_tokens):
            if type(value) is not int or value < 1:
                raise ValueError("offline model budgets must be positive integers")
        if self.max_steps > 1000:
            raise ValueError("offline model max_steps exceeds the framework limit of 1000")


_active: ContextVar[BackendLedger | None] = ContextVar("lexvault_backend_ledger", default=None)


def governance_active() -> bool:
    """True while an authorized host node is executing with an admission ledger."""
    return _active.get() is not None


class BackendLedger:
    def __init__(self, authorize: Callable[[], None], *, max_calls: int = 128,
                 max_input_chars: int = 20_000_000, clock: Callable[[], float] = time.monotonic):
        if type(max_calls) is not int or not 1 <= max_calls <= 1000:
            raise ValueError("backend max_calls must be in [1, 1000]")
        if type(max_input_chars) is not int or not 1 <= max_input_chars <= 20_000_000:
            raise ValueError("backend max_input_chars must be in [1, 20000000]")
        self.authorize, self.max_calls, self.max_input_chars = authorize, max_calls, max_input_chars
        self.clock = clock
        self.deadline: float | None = None
        self.events: list[dict[str, Any]] = []
        self.calls = self.input_chars = 0

    def set_deadline(self, deadline: float) -> None:
        self.deadline = deadline if self.deadline is None else min(self.deadline, deadline)

    @contextmanager
    def activate(self):
        token = _active.set(self)
        try:
            yield
        finally:
            _active.reset(token)

    def invoke(self, kind: str, client: Any, args: tuple[Any, ...], kwargs: dict[str, Any],
               operation: Callable[[], Any]) -> Any:
        self.authorize()
        request = json.dumps({"args": args, "kwargs": kwargs}, ensure_ascii=False,
                             sort_keys=True, allow_nan=False)
        network = kind != "embedding" or bool(client.prefer_remote)
        backend = "hashed-local" if not network else "network-disabled"
        started = self.clock()
        event = {"call_id": f"backend-{len(self.events)+1}", "kind": kind,
                 "model": "hashed-bigram-v1" if not network else str(getattr(client, "model", "host-configured")),
                 "backend": backend, "request_sha256": hashlib.sha256(request.encode()).hexdigest(),
                 "input_chars": len(request), "executed": False, "outcome": "denied",
                 "input_tokens": None, "output_tokens": None, "usage_known": False,
                 "measurement_valid": False}
        # The first rejected request stops a node; retained records stay bounded.
        if len(self.events) > self.max_calls:
            raise BackendAdmissionError("backend evidence bound exceeded")
        self.events.append(event)
        if network:
            event["error_type"] = "OfflineNetworkDenied"
            raise BackendAdmissionError("network model backends are disabled in this engineering path")
        if (self.calls >= self.max_calls or self.input_chars + len(request) > self.max_input_chars
                or (self.deadline is not None and started >= self.deadline)):
            event["error_type"] = "BackendBudgetExceeded"
            raise BackendAdmissionError("backend call/input/deadline budget exhausted")
        self.calls += 1
        self.input_chars += len(request)
        event["executed"] = True
        try:
            value = operation()
            self.authorize()
            if self.deadline is not None and self.clock() >= self.deadline:
                raise BackendAdmissionError("backend completed after the admission deadline")
            event["outcome"] = "completed"
            return value
        except Exception as exc:
            event["outcome"], event["error_type"] = "failed", type(exc).__name__
            raise
        finally:
            event["elapsed_ms"] = round((self.clock() - started) * 1000)


def governed_backend(kind: str):
    """Observe/enforce only the active snapshot execution scope."""
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            active = _active.get()
            if active is None:
                return function(*args, **kwargs)
            client = args[0] if args else kwargs.get("self")
            payload_args = args[1:] if kind in {"embedding", "reranker"} else args
            payload_kwargs = {key: value for key, value in kwargs.items() if key != "self"}
            return active.invoke(kind, client, payload_args, payload_kwargs, lambda: function(*args, **kwargs))
        return call
    return decorate
