"""Inference isolation, local transport and complete error provenance."""

import hashlib
import io
import json
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest

from app import benchmark_solver as solver


CONFIG = {"url": "http://127.0.0.1:8000/v1", "model": "test-local"}


def response(content="答案", *, finish="stop", reasoning=None):
    return io.BytesIO(json.dumps({
        "model": "test-local", "choices": [{"message": {"content": content, "reasoning_content": reasoning}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }, ensure_ascii=False).encode())


def run_mock(*responses, strategy="direct", config=None, task="1-2"):
    with patch.object(solver.urllib.request, "build_opener") as build:
        build.return_value.open.side_effect = responses
        result = solver.solve(task, "  只输出选项。  ", "测试问题？", {**CONFIG, "strategy": strategy, **(config or {})})
    return result, build


def test_direct_preserves_existing_public_prompt_and_audits_request():
    result, build = run_mock(response(" A "))
    assert result["error"] is None
    assert result["prediction"] == "A"
    assert result["calls"][0]["raw_final_answer"] == " A "
    assert len(result["calls"]) == 1
    request = build.return_value.open.call_args.args[0]
    call = result["calls"][0]
    assert call["request"]["messages"] == [
        {"role": "system", "content": solver.DIRECT_SYSTEM},
        {"role": "user", "content": "只输出选项。\n测试问题？"},
    ]
    assert call["request"] == json.loads(request.data)
    assert call["request_sha256"] == hashlib.sha256(request.data).hexdigest()
    assert call["request_url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert result["usage"]["total_tokens"] == 15
    assert result["model_config"]["max_tokens"] == 900
    assert result["model_config"]["enable_thinking"] is False


def test_gold_and_old_predictions_cannot_enter_requests_or_config():
    poison = {"reference": "GOLD_SECRET", "answer": "GOLD_SECRET", "old_prediction": "OLD_SECRET", "label_space": ["GOLD_SECRET"]}
    with patch("builtins.open", side_effect=AssertionError("inference cannot open datasets")):
        result, _ = run_mock(response("A"), response("B"), strategy="verify", config=poison)
    assert result["error"] is None
    assert len(result["calls"]) == 2
    archived = json.dumps(result, ensure_ascii=False)
    assert "GOLD_SECRET" not in archived
    assert "OLD_SECRET" not in archived
    assert result["calls"][1]["request"]["messages"][-2] == {"role": "assistant", "content": "A"}


@pytest.mark.parametrize("task", tuple(solver.TASK_GUIDANCE))
def test_every_task_has_guidance_without_changing_original_input(task):
    result, _ = run_mock(response(), strategy="task_guided", task=task)
    assert result["error"] is None
    messages = result["calls"][0]["request"]["messages"]
    assert solver.TASK_GUIDANCE[task] in messages[0]["content"]
    assert messages[1]["content"] == "只输出选项。\n测试问题？"


def test_verify_retains_both_calls_and_uses_final_revision():
    result, _ = run_mock(response("A"), response("B"), strategy="verify")
    assert result["prediction"] == "B"
    assert [call["raw_final_answer"] for call in result["calls"]] == ["A", "B"]
    assert [call["phase"] for call in result["calls"]] == ["draft", "verify"]
    assert result["calls"][0]["request_sha256"] != result["calls"][1]["request_sha256"]
    assert len(result["calls"][0]["request"]["messages"]) == 2
    assert len(result["calls"][1]["request"]["messages"]) == 4


def test_review_failure_is_not_hidden_by_successful_draft():
    result, build = run_mock(response("A"), urllib.error.URLError("test failure"), strategy="verify")
    assert "test failure" in result["error"]
    assert result["prediction"] == ""
    assert len(result["calls"]) == build.return_value.open.call_count == 2
    assert result["calls"][0]["raw_final_answer"] == "A"
    assert result["calls"][1]["error"]


@pytest.mark.parametrize("strategy", ["direct", "task_guided", "verify"])
def test_truncated_draft_is_recorded_as_failure_without_review(strategy):
    result, build = run_mock(response("半句", finish="length"), strategy=strategy)
    assert "truncated_response" in result["error"]
    assert result["prediction"] == "半句"
    assert result["finish_reason"] == "length"
    assert len(result["calls"]) == build.return_value.open.call_count == 1


def test_truncated_review_preserves_both_calls():
    result, _ = run_mock(response("A"), response("B", finish="length"), strategy="verify")
    assert result["error"] and result["finish_reason"] == "length"
    assert len(result["calls"]) == 2
    assert result["calls"][0]["error"] is None


def test_closed_thinking_is_removed_and_never_reused_by_review():
    result, _ = run_mock(response("<think>PRIVATE</think>\nA", reasoning="PRIVATE2"), response("B"), strategy="verify")
    assert result["error"] is None
    assert result["calls"][0]["reasoning_characters"] == len("PRIVATEPRIVATE2")
    assert result["calls"][0]["raw_final_answer"] == "\nA"
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("content,reasoning", [(None, "reasoning only"), ("", None), ("<think>incomplete", None), ("analysis</think>A", None)])
def test_reasoning_is_never_used_as_final_answer(content, reasoning):
    result, _ = run_mock(response(content, reasoning=reasoning), strategy="verify")
    assert result["error"]
    assert result["prediction"] == ""
    assert len(result["calls"]) == 1


@pytest.mark.parametrize("url", [
    "https://example.com/v1", "http://192.168.1.2/v1", "http://127.0.0.1.example.com/v1",
    "file:///tmp/model", "http://user:pass@127.0.0.1/v1", "http://127.0.0.1/v1?key=secret",
])
def test_nonlocal_or_credentialed_endpoints_rejected_before_network(url):
    result, build = run_mock(config={"url": url})
    assert result["error"]
    assert result["calls"] == []
    build.assert_not_called()


@pytest.mark.parametrize("url,expected", [
    ("http://localhost:8000/v1", "http://127.0.0.1:8000/v1/chat/completions"),
    ("http://[::1]:8000/v1", "http://[::1]:8000/v1/chat/completions"),
    ("http://127.0.0.1:8000/v1/chat/completions", "http://127.0.0.1:8000/v1/chat/completions"),
])
def test_local_endpoints_are_canonicalized_and_proxies_disabled(url, expected):
    result, build = run_mock(response(), config={"url": url})
    assert result["error"] is None
    assert result["calls"][0]["request_url"] == expected
    handlers = build.call_args.args
    assert handlers[0].proxies == {}
    assert isinstance(handlers[1], solver._NoRedirect)
    assert handlers[1].redirect_request(None, None, 302, None, None, "https://example.com") is None


def test_total_deadline_is_passed_through_and_errors_retained():
    with patch.object(solver, "read_json_with_deadline", side_effect=TimeoutError("deadline")) as reader:
        result, _ = run_mock(response())
    assert "TimeoutError" in result["error"]
    assert len(result["calls"]) == 1
    assert reader.call_args.kwargs["deadline"] > 0
    assert result["calls"][0]["latency_ms"] >= 0


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{"message": {"content": ["not a string"]}, "finish_reason": "stop"}]}])
def test_malformed_payload_is_a_recorded_error(payload):
    result, _ = run_mock(io.BytesIO(json.dumps(payload).encode()))
    assert result["error"]
    assert len(result["calls"]) == 1


@pytest.mark.parametrize("config", [{"strategy": "oracle"}, {"max_tokens": 0}, {"timeout": -1}, {"temperature": float("nan")}, {"enable_thinking": "false"}])
def test_invalid_config_cannot_make_requests(config):
    result, build = run_mock(config=config)
    assert result["error"]
    assert not result["calls"]
    build.assert_not_called()
