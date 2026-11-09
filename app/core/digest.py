"""Canonical JSON and its SHA-256, the serialisation behind stored content digests.

Sorted keys, compact separators and UTF-8 without ASCII escaping. Every digest
written by these helpers depends on that exact byte form, so it is fixed here and
pinned by ``tests/test_digest.py``. Digests that were registered with other
``json.dumps`` settings (default separators, unsorted keys) keep their own code.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any, *, allow_nan: bool = True) -> bytes:
    """``allow_nan=False`` raises ``ValueError`` for NaN or infinity instead of writing them."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=allow_nan).encode("utf-8")


def json_sha256(value: Any, *, allow_nan: bool = True) -> str:
    return hashlib.sha256(canonical_json(value, allow_nan=allow_nan)).hexdigest()
