"""A/B/C isolation: one frozen context, one model call, one registered variable."""
from __future__ import annotations

import io
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from app import collaboration_ablation as ablation
from app import services
from app.collaboration_ablation import (
    GROUPS,
    Generation,
    build_prompt,
    group_note,
    load_batch_dataset,
    load_prepared,
    prepare_batch,
    rotation,
    run_batch,
    summarise_attempts,
    verify_prepared_integrity,
)

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "benchmarks/answer_eval/v2/dataset.json"
GOOD_ANSWER = "累计流入2,860万元，涉及47名投资人[资料1][资料2]。请律师复核原文与上述结论。"
INVALID_ANSWER = "累计流入2,860万元，涉及47名投资人。"


class _FakeResponse:
    def __init__(self, payload: bytes):
        self.buffer = io.BytesIO(payload)

    def read1(self, size: int) -> bytes:
        return self.buffer.read(size)

    def read(self, size: int) -> bytes:
        return self.buffer.read(size)

    def close(self) -> None:
        pass

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _CapturingOpener:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.requests: list[object] = []

    def open(self, request, timeout=None):  # noqa: ANN001 - mirrors urllib signature
        self.requests.append(request)
        return _FakeResponse(self.payload)


@pytest.fixture(scope="module")
def batch(tmp_path_factory):
    dataset = load_batch_dataset(DATASET)
    directory = tmp_path_factory.mktemp("ablation") / "batch"
    manifest = prepare_batch(dataset, directory, item_ids=["normal-01", "contradiction-01"])
    return dataset, manifest, directory


def _stub(answer: str, usage: dict | None = None):
    calls: list[tuple[str, str, str]] = []

    def generate(question: str, route: str, contexts: list, note: str) -> Generation:
        calls.append((question, route, note))
        return Generation(text=answer, usage=usage, model="stub-model")

    generate.calls = calls  # type: ignore[attr-defined]
    return generate


def test_groups_differ_only_in_the_registered_note(batch):
    _, manifest, directory = batch
    normal = json.loads((directory / "items" / "normal-01.json").read_text())
    notes = normal["notes"]
    assert notes["A"] == ""
    assert notes["C"] == notes["B"] + notes["C"][len(notes["B"]):]
    assert notes["C"].startswith(notes["B"])
    assert "source_checks" not in notes["B"] and "source_checks" in notes["C"]
    assert "upstream_review" not in notes["B"] and "upstream_review" not in notes["C"]

    users = {group: normal["prompts"][group]["user"] for group in GROUPS}
    assert users["B"].replace("\n\n" + notes["B"], "", 1) == users["A"]
    assert users["C"].replace("\n\n" + notes["C"], "", 1) == users["A"]
    for group in GROUPS:
        assert normal["prompts"][group]["system"] == normal["prompts"]["A"]["system"]
    assert normal["prompts"]["A"]["chars"] < normal["prompts"]["B"]["chars"] < normal["prompts"]["C"]["chars"]
    assert [entry["id"] for entry in manifest["items"]] == ["normal-01", "contradiction-01"]


def test_unregistered_group_or_group_note_is_rejected():
    with pytest.raises(ValueError):
        group_note("D", "b", "c")
    with pytest.raises(ValueError):
        rotation([], 0, 0)


def test_prompt_mirrors_the_product_chat_request(batch):
    _, manifest, directory = batch
    prepared = json.loads((directory / "items" / "normal-01.json").read_text())
    note = prepared["notes"]["C"]
    opener = _CapturingOpener(json.dumps({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }).encode())
    with patch.object(services, "local_llm_available", return_value=(True, "test-model")), \
            patch.object(services, "egress_opener", return_value=opener):
        services.call_local_llm(prepared["question"], prepared["route"], prepared["contexts"],
                                timeout=5, model_override="test-model", object_note=note)
    body = json.loads(opener.requests[-1].data)  # type: ignore[attr-defined]
    expected = build_prompt(prepared["question"], prepared["route"], prepared["contexts"], note)
    assert body["messages"] == [
        {"role": "system", "content": expected["system"]},
        {"role": "user", "content": expected["user"]},
    ]
    assert ablation.digest(expected) == prepared["prompts"]["C"]["sha256"]


def test_prepared_batch_binds_frozen_sources_and_prompt_digests(batch):
    dataset, manifest, directory = batch
    assert manifest["case"]["pages"] == len(dataset["sources"])
    assert manifest["case"]["source_sha256"] == dataset["source_sha256"]
    assert manifest["protocol"]["single_generation_call_per_attempt"] is True
    assert manifest["protocol"]["failed_attempts_kept_in_denominator"] is True
    reloaded_manifest, prepared = load_prepared(directory)
    assert reloaded_manifest["items"] == manifest["items"]
    for record in prepared:
        verify_prepared_integrity(record)
        record["notes"]["B"] += "篡改"
        with pytest.raises(ValueError, match="冻结提示"):
            verify_prepared_integrity(record)


def test_prepare_refuses_an_existing_directory(batch):
    dataset, _, directory = batch
    with pytest.raises(FileExistsError):
        prepare_batch(dataset, directory, item_ids=["normal-01"])


def test_budget_exceeded_never_calls_the_model(batch):
    dataset, _, directory = batch
    _, prepared = load_prepared(directory)
    stub = _stub(GOOD_ANSWER)
    run = directory.parent / "run-budget"
    report = run_batch(dataset, prepared[:1], run, generate=stub, model="stub-model",
                       max_prompt_chars=1)
    summary = report["summary"]["A"]
    assert summary["outcomes"] == {"budget_exceeded": 1}
    assert summary["planned_attempts"] == 1 and summary["passed"] == 0
    assert stub.calls == []  # type: ignore[attr-defined]


def test_invalid_raw_answer_cannot_be_rescued_by_the_appendix(batch):
    dataset, _, directory = batch
    _, prepared = load_prepared(directory)
    item = [record for record in prepared if record["id"] == "contradiction-01"][0]
    run = directory.parent / "run-invalid"
    report = run_batch(dataset, [item], run, generate=_stub(INVALID_ANSWER), model="stub-model",
                       groups=["C"])
    row = json.loads((run / "attempts.jsonl").read_text(encoding="utf-8").strip())
    assert row["outcome"] == "contract_failed" and row["passed"] is False
    assert row["appendix_withheld"] is True
    assert "专家交叉核对" not in row["final_answer"]
    assert report["summary"]["C"]["pass_rate_all_planned"] == 0.0


def test_c_appendix_is_appended_only_after_a_valid_answer(batch):
    dataset, _, directory = batch
    _, prepared = load_prepared(directory)
    item = [record for record in prepared if record["id"] == "contradiction-01"][0]
    run = directory.parent / "run-appendix"
    report = run_batch(dataset, [item], run, generate=_stub(GOOD_ANSWER), model="stub-model", groups=["C"])
    row = json.loads((run / "attempts.jsonl").read_text(encoding="utf-8").strip())
    assert row["outcome"] == "ok" and row["raw_valid"] is True
    assert row["appendix_withheld"] is False
    assert "专家交叉核对（待律师复核）" in row["final_answer"]
    assert row["final_answer"].startswith(GOOD_ANSWER)
    assert report["summary"]["C"]["appendix_applied"] == 1


@pytest.mark.parametrize("message,expected", [
    ("本地模型不可用：Qwythos-9B-v2-4bit-mlx", "model_unavailable"),
    ("本地模型调用失败（TimeoutError）", "timeout"),
    ("本地模型调用失败（URLError）", "transport_error"),
])
def test_backend_failures_are_classified_without_retry(batch, message, expected):
    dataset, _, directory = batch
    _, prepared = load_prepared(directory)
    run = directory.parent / f"run-{expected}"

    def broken(question: str, route: str, contexts: list, note: str) -> Generation:
        raise RuntimeError(message)

    report = run_batch(dataset, prepared[:1], run, generate=broken, model="stub-model", groups=["A"])
    row = json.loads((run / "attempts.jsonl").read_text(encoding="utf-8").strip())
    assert row["outcome"] == expected and row["executed"] is True
    assert report["summary"]["A"]["outcomes"] == {expected: 1}


def test_failed_attempts_stay_in_every_denominator(batch):
    dataset, _, directory = batch
    _, prepared = load_prepared(directory)
    answers = iter([GOOD_ANSWER, ""])

    def mixed(question: str, route: str, contexts: list, note: str) -> Generation:
        return Generation(text=next(answers), model="stub-model")

    run = directory.parent / "run-mixed"
    report = run_batch(dataset, prepared[:1], run, generate=mixed, model="stub-model", groups=["A"],
                       repeats=2)
    summary = report["summary"]["A"]
    assert summary["planned_attempts"] == 2
    assert summary["outcomes"] == {"invalid_response": 1, "ok": 1}
    assert summary["pass_rate_all_planned"] < summary["pass_rate_executed_ok"]


def test_unknown_usage_is_reported_and_never_zeroed(batch):
    dataset, _, directory = batch
    _, prepared = load_prepared(directory)
    run = directory.parent / "run-usage"
    report = run_batch(dataset, prepared[:1], run, generate=_stub(GOOD_ANSWER), model="stub-model",
                       groups=["A"])
    summary = report["summary"]["A"]
    assert summary["unknown_usage"] == 1
    assert summary["input_tokens_total"] is None and summary["output_tokens_total"] is None

    run_known = directory.parent / "run-usage-known"
    known = _stub(GOOD_ANSWER, usage={"usage": {"prompt_tokens": 30, "completion_tokens": 7}, "latency_ms": 5})
    report_known = run_batch(dataset, prepared[:1], run_known, generate=known, model="stub-model",
                             groups=["A"])
    summary_known = report_known["summary"]["A"]
    assert summary_known["input_tokens_total"] == 30 and summary_known["output_tokens_total"] == 7
    assert summary_known["unknown_usage"] == 0


def test_rotation_is_registered_and_balanced(batch):
    assert rotation(GROUPS, 0, 0) == ["A", "B", "C"]
    assert rotation(GROUPS, 1, 0) == ["B", "C", "A"]
    assert rotation(GROUPS, 0, 1) == ["B", "C", "A"]
    first_position = [rotation(GROUPS, index, 0)[0] for index in range(6)]
    assert sorted(first_position) == ["A", "A", "B", "B", "C", "C"]


def test_group_summary_rejects_an_unknown_item(batch):
    dataset, _, directory = batch
    _, prepared = load_prepared(directory)
    rows = [{"item": "missing", "group": "A", "outcome": "ok", "executed": True, "passed": True,
             "category": "normal", "score": {"coverage": 1.0, "citation_precision": 1.0, "violations": []},
             "usage_known": True, "input_tokens": 1, "output_tokens": 1, "elapsed_seconds": 0.1,
             "appendix_withheld": False}]
    with pytest.raises(KeyError):
        summarise_attempts(rows, dataset)


def test_appendix_cannot_inflate_the_scored_model_answer(batch):
    dataset, _, directory = batch
    _, prepared = load_prepared(directory)
    item = [record for record in prepared if record["id"] == "normal-01"][0]
    # Point the required phrase at text that only the deterministic appendix can
    # contain: a raw answer that omits it must still be scored as failed.
    adjusted = deepcopy(dataset)
    target = next(row for row in adjusted["items"] if row["id"] == "normal-01")
    target["must_include"] = ["专家交叉核对"]
    answer = "材料显示资金往来情况[资料1][资料2]。请律师复核原文与上述结论。"
    run = directory.parent / "run-appendix-effect"
    report = run_batch(adjusted, [item], run, generate=_stub(answer), model="stub-model", groups=["C"])
    row = json.loads((run / "attempts.jsonl").read_text(encoding="utf-8").strip())
    assert row["outcome"] == "ok"
    assert row["passed"] is False and row["score"]["coverage"] == 0.0
    assert row["final_score"]["coverage"] == 1.0 and row["final_score"]["passed"] is True
    assert row["appendix_changed_verdict"] is True
    summary = report["summary"]["C"]
    assert summary["pass_rate_all_planned"] == 0.0
    assert summary["final_passed_including_appendix"] == 1
    assert summary["appendix_changed_verdict"] == 1


def test_live_generator_targets_the_probed_endpoint_explicitly():
    from unittest.mock import patch

    from app.collaboration_ablation import live_generator

    captured = {}

    def fake_call(question, route, contexts, **kwargs):
        captured.update(kwargs)
        return "合成回答[资料1]。请律师复核原文与上述结论。"

    with patch("app.services.call_local_llm", side_effect=fake_call):
        generated = live_generator("stub-chat", 30, "http://127.0.0.1:18000/v1")(
            "问题", "事实检索", [], "")
    assert captured["base_url_override"] == "http://127.0.0.1:18000/v1"
    assert captured["model_override"] == "stub-chat"
    assert generated.text.startswith("合成回答")


def test_extra_citations_do_not_fail_an_answer_that_cites_the_required_page(batch):
    dataset, _, directory = batch
    _, prepared = load_prepared(directory)
    item = [record for record in prepared if record["id"] == "normal-01"][0]
    # The model cites the required page plus another retrieved page.
    answer = ("累计流入2,860万元，涉及47名投资人[资料1][资料2][资料3]。"
              "请律师复核原文与上述结论。")
    run = directory.parent / "run-extra-citations"
    report = run_batch(dataset, [item], run, generate=_stub(answer), model="stub-model", groups=["A"])
    row = json.loads((run / "attempts.jsonl").read_text(encoding="utf-8").strip())
    assert row["outcome"] == "ok"
    assert row["score"]["required_pages_cited"] is True
    assert row["score"]["extra_citations"]
    assert row["passed"] is True
    assert report["scoring"]["version"] == 2


def test_citation_outside_the_frozen_context_fails(batch):
    dataset, _, directory = batch
    _, prepared = load_prepared(directory)
    item = [record for record in prepared if record["id"] == "normal-01"][0]
    answer = "累计流入2,860万元，涉及47名投资人[资料1][资料99]。请律师复核原文与上述结论。"
    run = directory.parent / "run-context-escape"
    run_batch(dataset, [item], run, generate=_stub(answer), model="stub-model", groups=["A"])
    row = json.loads((run / "attempts.jsonl").read_text(encoding="utf-8").strip())
    # An out-of-range marker is dropped by answer_citations, so the contract check fails instead.
    assert row["outcome"] == "contract_failed" or row["passed"] is False
