"""Offline replay audit must repair only gold-blind output and never touch the source."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import unified_benchmark_runner as runner
from scripts.postprocess_audit import CHARGE_POLICY, canonicalize_charges, replay


def _run(tmp_path: Path, tasks: list[str], answer: str, limit: int = 2) -> Path:
    """Create a synthetic completed run with a fixed fake model answer."""
    source = tmp_path / "source"

    def fake_model(*_args, metadata, **_kwargs):
        metadata.update(attempts=1, finish_reason="stop", usage={"completion_tokens": 10})
        return answer, 10, None

    args = argparse.Namespace(dataset="lawbench", tasks=tasks, limit_per_task=limit, sample_seed=42,
                              run_dir=str(source), output_dir=str(tmp_path), resume=False, retry=0,
                              baseline_results=None)
    with patch.object(runner, "verify_model"), patch.object(runner, "call_model", side_effect=fake_model):
        runner.run_benchmark(args)
    return source


def test_charge_canonicalization_maps_only_a_unique_superstring():
    prediction, mapped = canonicalize_charges("[罪名]过失损坏广播电视设施<eoa>")
    assert prediction == "罪名：过失损坏广播电视设施、公用电信设施"
    assert mapped == ["过失损坏广播电视设施 -> 过失损坏广播电视设施、公用电信设施"]
    assert canonicalize_charges("[罪名]盗窃<eoa>") == ("[罪名]盗窃<eoa>", [])


def test_ambiguous_charge_is_left_untouched():
    ontology = frozenset({"盗窃、抢劫", "盗窃、诈骗", "诈骗"})
    with patch("scripts.postprocess_audit.task_label_space", return_value=ontology):
        assert canonicalize_charges("罪名：盗窃") == ("罪名：盗窃", [])


def test_replay_repairs_output_and_preserves_every_original(tmp_path):
    source = _run(tmp_path, ["2-1"], "合成问题 2-1/0：用于测试装载、抽样及协议,不评估法律知识。")
    destination = tmp_path / "audit"
    originals = [json.loads(line) for line in (source / "detailed_results.jsonl").read_text(encoding="utf-8").splitlines()]
    result = replay(source, destination)
    assert result["new_model_calls"] == 0
    assert result["input_hashes_unchanged"] is True
    assert result["verification"]["valid"] is True
    assert result["verification"]["scoring_verified"] is True
    audited = [json.loads(line) for line in (destination / "detailed_results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["original_prediction"] for row in audited] == [row["prediction"] for row in originals]
    assert any(row["postprocess"]["applied"] for row in audited)
    assert all(row["scorer_version"] == result["audited_scorer"] for row in audited)


def test_replay_changes_charge_scores_only_with_the_diagnostic_flag(tmp_path):
    answer = "[罪名]过失损坏广播电视设施<eoa>"
    source = _run(tmp_path, ["3-3"], answer, limit=20)
    strict = replay(source, tmp_path / "strict")
    relaxed = replay(source, tmp_path / "relaxed", charge_canonicalization=True)
    assert strict["tasks"]["3-3"]["delta"] == 0
    assert relaxed["tasks"]["3-3"]["after"] > relaxed["tasks"]["3-3"]["before"]
    assert relaxed["tasks"]["3-3"]["improved"] > 0
    assert CHARGE_POLICY in relaxed["policies"]
    assert relaxed["score_delta"] > strict["score_delta"]


def test_retrieval_replay_is_opt_in_and_uses_only_the_question(tmp_path):
    from unittest.mock import Mock

    source = _run(tmp_path, ["1-1"], "模型幻觉条文")
    context = {"mode": "exact_article", "hits": [{"document_id": "law", "article_id": "1", "text": "第一条 官方正文。"}]}
    fake = Mock(return_value=context)
    with patch("scripts.postprocess_audit.retrieve", fake):
        replay(source, tmp_path / "without-corpus")
    fake.assert_not_called()
    with patch("scripts.postprocess_audit.retrieve", fake):
        result = replay(source, tmp_path / "with-corpus", corpus_directories=["/frozen/corpus"])
    assert result["corpus_directories"] == ["/frozen/corpus"]
    audited = [json.loads(line) for line in (tmp_path / "with-corpus" / "detailed_results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(row["prediction"] == "官方正文。" for row in audited)
    assert all(row["postprocess"]["policy"].startswith("exact-retrieved-article-content") for row in audited)
    assert all(call.args[1] == row["question"] for call, row in zip(fake.call_args_list, audited))


def test_replay_rejects_overwrite_and_nesting(tmp_path):
    source = _run(tmp_path, ["2-1"], "合成问题 2-1/0：用于测试装载、抽样及协议,不评估法律知识。")
    destination = tmp_path / "audit"
    replay(source, destination)
    with pytest.raises(FileExistsError):
        replay(source, destination)
    with pytest.raises(ValueError):
        replay(source, source / "nested")
