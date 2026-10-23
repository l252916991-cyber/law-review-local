import argparse
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import unified_benchmark_runner as runner
from verify_benchmark_run import verify


def _record(task: str, index: int = 0) -> dict:
    record = runner.load_lawbench_dataset([task], 1)[0]
    record.update(
        question_id=f"{task}_{index:04d}", question=f"{task} question",
        instruction=f"{task} instruction", reference="GOLD_CANARY",
    )
    record["question_hash"] = runner.hash_text(record["question"])
    return record


def _args(run_dir: Path, corpus: bool = True, **overrides) -> argparse.Namespace:
    values = {
        "dataset": "lawbench", "tasks": ["3-2"], "limit_per_task": 1,
        "sample_seed": 42, "run_dir": str(run_dir), "output_dir": str(run_dir.parent),
        "resume": False, "retry": 0, "baseline_results": None,
        "corpus_dir": [Path("/frozen-corpus")] if corpus else None,
        "disable_3_2_rag": False, "prompt_strategy": "task_guided",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _solver_call(messages, config, phase):
    body = {
        "model": config["model"], "messages": messages,
        "temperature": config["temperature"], "max_tokens": config["max_tokens"],
        "stream": False, "chat_template_kwargs": {"enable_thinking": config["enable_thinking"]},
    }
    return {
        "phase": phase, "request_url": f"{config['url']}/chat/completions", "request": body,
        "request_sha256": runner.hash_json(body), "latency_ms": 1.0,
        "raw_final_answer": "model answer", "prediction": "model answer",
        "reasoning_characters": 0, "finish_reason": "stop", "usage": {"completion_tokens": 2},
        "response_model": config["model"], "error": None,
    }


def _legacy_call(prompt, system, config, retry=0, metadata=None):
    assert metadata is not None
    metadata.update(attempts=1, attempt_errors=[], finish_reason="stop", usage={"completion_tokens": 2})
    return "legacy answer", 1.0, None


LEXICAL = {
    "version": runner.RETRIEVAL_VERSION, "policy": "explicit_revision_year_else_latest_available",
    "ranker_policy": "lexical", "ranker": "lexical", "hits": [{"article_id": "1"}],
    "warnings": [], "mode": "lexical_search", "context": "official article context",
}


def test_mixed_run_routes_only_3_2_through_lexical_rag_and_freezes_actual_request():
    records = [_record("3-2"), _record("3-8")]
    with tempfile.TemporaryDirectory() as directory:
        run_dir = Path(directory) / "run"
        with patch.object(runner, "load_all_datasets", return_value=records), \
                patch.object(runner, "verify_model"), \
                patch.object(runner, "corpus_fingerprint", return_value={"manifest": "digest"}), \
                patch.object(runner, "retrieve", return_value=LEXICAL) as retrieval, \
                patch.object(runner, "call_model", side_effect=_legacy_call) as legacy, \
                patch("app.benchmark_rag_solver._call", side_effect=_solver_call):
            runner.run_benchmark(_args(run_dir, tasks=["3-2", "3-8"]))

        rows = {row["task"]: row for row in map(json.loads, (run_dir / "detailed_results.jsonl").read_text().splitlines())}
        rag = rows["3-2"]
        assert rag["task_route"] == "statutory_rag"
        assert rows["3-8"]["task_route"] == "legacy_prompt"
        assert retrieval.call_args.kwargs["ranker_policy"] == "lexical"
        assert legacy.call_count == 1
        assert rag["request_messages"] == rag["calls"][0]["request"]["messages"]
        assert rag["prompt_hash"] == runner.hash_json(rag["request_messages"])
        assert rag["retrieval"] == LEXICAL and rag["effective_config"]["retrieval_ranker_policy"] == "lexical"
        assert "GOLD_CANARY" not in json.dumps(rag["calls"][0]["request"], ensure_ascii=False)
        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert manifest["routing"]["task_3_2"]["ranker_policy"] == "lexical"
        assert manifest["samples"][0]["prompt_hash"] == rag["prompt_hash"]

        with patch("app.benchmark_retrieval.retrieve", return_value=LEXICAL):
            assert verify(run_dir, write=False)["valid"]

        resume_args = _args(run_dir, tasks=["3-2", "3-8"], resume=True)
        with patch.object(runner, "load_all_datasets", return_value=records), \
                patch.object(runner, "verify_model"), \
                patch.object(runner, "corpus_fingerprint", return_value={"manifest": "digest"}), \
                patch.object(runner, "retrieve", return_value=LEXICAL), \
                patch.object(runner, "call_model") as legacy_resume, \
                patch("app.benchmark_rag_solver._call") as rag_resume:
            runner.run_benchmark(resume_args)
        legacy_resume.assert_not_called()
        rag_resume.assert_not_called()

        original = json.loads(json.dumps(rag))

        def replace_rag(changed):
            ordered = [changed if row["task"] == "3-2" else row for row in rows.values()]
            (run_dir / "detailed_results.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in ordered),
            )
            (run_dir / "checkpoints" / f"{changed['question_id']}.json").write_text(json.dumps(changed))

        changed = json.loads(json.dumps(original))
        changed["calls"][0]["request"]["max_tokens"] += 1
        changed["calls"][0]["request_sha256"] = runner.hash_json(changed["calls"][0]["request"])
        replace_rag(changed)
        with patch("app.benchmark_retrieval.retrieve", return_value=LEXICAL):
            assert any("request body" in issue for issue in verify(run_dir, write=False)["issues"])

        changed = json.loads(json.dumps(original))
        changed["effective_config"]["max_tokens"] += 1
        replace_rag(changed)
        with patch("app.benchmark_retrieval.retrieve", return_value=LEXICAL):
            assert any("effective config" in issue for issue in verify(run_dir, write=False)["issues"])


def test_explicit_off_uses_same_solver_task_guided_control_and_no_corpus_keeps_legacy():
    record = _record("3-2")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with patch.object(runner, "load_all_datasets", return_value=[record]), \
                patch.object(runner, "verify_model"), \
                patch.object(runner, "corpus_fingerprint", return_value={}), \
                patch.object(runner, "retrieve") as retrieval, \
                patch.object(runner, "call_model") as legacy, \
                patch("app.benchmark_solver._call", side_effect=_solver_call):
            runner.run_benchmark(_args(root / "off", disable_3_2_rag=True))
        off = json.loads((root / "off" / "detailed_results.jsonl").read_text())
        assert off["task_route"] == "solver_task_guided_control"
        assert off["calls"][0]["phase"] == "draft"
        retrieval.assert_not_called()
        legacy.assert_not_called()

        with patch.object(runner, "load_all_datasets", return_value=[record]), \
                patch.object(runner, "verify_model"), \
                patch.object(runner, "call_model", side_effect=_legacy_call) as legacy, \
                patch("app.benchmark_solver._call") as solver:
            runner.run_benchmark(_args(root / "legacy", corpus=False))
        no_corpus = json.loads((root / "legacy" / "detailed_results.jsonl").read_text())
        assert no_corpus["task_route"] == "legacy_prompt"
        legacy.assert_called_once()
        solver.assert_not_called()


def test_rag_error_scores_zero_with_call_trace_and_tampering_is_rejected():
    record = _record("3-2")

    def failed_call(messages, config, phase):
        call = _solver_call(messages, config, phase)
        call.update(prediction="", raw_final_answer="", error="TimeoutError: timed out",
                    finish_reason=None, usage=None)
        return call

    with tempfile.TemporaryDirectory() as directory:
        run_dir = Path(directory) / "run"
        args = _args(run_dir)
        patches = (
            patch.object(runner, "load_all_datasets", return_value=[record]),
            patch.object(runner, "verify_model"),
            patch.object(runner, "corpus_fingerprint", return_value={}),
            patch.object(runner, "retrieve", return_value=LEXICAL),
            patch("app.benchmark_rag_solver._call", side_effect=failed_call),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            runner.run_benchmark(args)
        row = json.loads((run_dir / "detailed_results.jsonl").read_text())
        assert row["score"] == 0 and row["metric"] == "error"
        assert row["attempts"] == 1 and row["calls"][0]["error"] == row["error"]
        with patch("app.benchmark_retrieval.retrieve", return_value=LEXICAL):
            assert verify(run_dir, write=False)["valid"]

        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["routing"]["task_3_2"]["ranker_policy"] = "auto"
        manifest_path.write_text(json.dumps(manifest))
        with patch("app.benchmark_retrieval.retrieve", return_value=LEXICAL):
            result = verify(run_dir, write=False)
        assert not result["valid"]
        assert any("RAG route policy" in issue or "Route/config provenance" in issue for issue in result["issues"])

        manifest_path.write_text(json.dumps({**manifest, "routing": {
            **manifest["routing"], "task_3_2": {
                **manifest["routing"]["task_3_2"], "ranker_policy": "lexical",
            },
        }}))
        checkpoint_path = run_dir / "checkpoints" / f"{record['question_id']}.json"
        original_checkpoint = json.loads(checkpoint_path.read_text())
        args.resume = True

        def resume_rejects(broken):
            checkpoint_path.write_text(json.dumps(broken))
            with patch.object(runner, "load_all_datasets", return_value=[record]), \
                    patch.object(runner, "corpus_fingerprint", return_value={}), \
                    patch.object(runner, "retrieve", return_value=LEXICAL), \
                    patch.object(runner, "verify_model"):
                try:
                    runner.run_benchmark(args)
                except ValueError as exc:
                    assert "Checkpoint request provenance" in str(exc)
                else:
                    raise AssertionError("tampered checkpoint was accepted for resume")

        broken = json.loads(json.dumps(original_checkpoint))
        broken["calls"][0]["request"]["max_tokens"] += 1
        broken["calls"][0]["request_sha256"] = runner.hash_json(broken["calls"][0]["request"])
        resume_rejects(broken)
        broken = json.loads(json.dumps(original_checkpoint))
        broken["effective_config"]["max_tokens"] += 1
        resume_rejects(broken)
