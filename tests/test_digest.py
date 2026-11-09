"""The canonical JSON form is part of every stored digest: these bytes must never change."""
import hashlib
import json
import math

import pytest

from app.core.digest import canonical_json, json_sha256

VALUE = {"b": [1, 2.5, None, True], "a": {"名称": "合同", "z": "é"}, "": ""}


def test_canonical_form_is_sorted_compact_utf8():
    assert canonical_json(VALUE) == '{"":"","a":{"z":"é","名称":"合同"},"b":[1,2.5,null,true]}'.encode()


def test_digest_matches_the_expression_each_module_used_before():
    legacy = hashlib.sha256(json.dumps(VALUE, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    assert json_sha256(VALUE) == legacy == json_sha256(VALUE, allow_nan=False)
    assert legacy == "de5c8537bbe2eb1deb9dd688384b37c7368a58726eace6d21eb72fabc024b31d"


def test_non_finite_numbers_are_rejected_only_on_request():
    assert canonical_json([math.inf]) == b"[Infinity]"
    with pytest.raises(ValueError):
        canonical_json([math.nan], allow_nan=False)
