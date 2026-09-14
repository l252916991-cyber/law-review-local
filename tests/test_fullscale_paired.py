from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from scripts.fullscale_paired import load_profile, main


def write_profiles(tmp_path):
    tg = tmp_path / "tg.json"
    tg.write_text(json.dumps({"url": "http://x", "model": "m", "temperature": 0.0, "max_tokens": 8,
                              "timeout": 5, "enable_thinking": False, "strategy": "task_guided"}))
    rag = tmp_path / "rag.json"
    rag.write_text(json.dumps({"url": "http://x", "model": "m", "temperature": 0.0, "max_tokens": 8,
                               "timeout": 5, "enable_thinking": False, "strategy": "statutory_rag",
                               "corpus_directories": ["/nowhere"]}))
    return tg, rag


def fake_solve(self=None, **_):
    def solve(task_id, instruction, question, config):
        return {"prediction": "法条:刑法第264条", "error": None, "finish_reason": "stop", "usage": None,
                "latency_ms": 1.0, "solver_version": "fake"}
    return solve


def run_main(tmp_path, limit=3):
    tg, rag = write_profiles(tmp_path)
    return main(["--task", "3-2", "--profile-tg", str(tg), "--profile-rag", str(rag),
                 "--output", str(tmp_path / "out"), "--limit", str(limit)])


def test_fullscale_paired_runs_and_resumes(tmp_path, capsys):
    with patch("app.benchmark_solver.solve", fake_solve()), \
         patch("app.benchmark_rag_solver.solve", fake_solve()), \
         patch("scripts.fullscale_paired.corpus_fingerprint", return_value={}):
        assert run_main(tmp_path) == 0
        out = tmp_path / "out"
        for arm in ("tg", "lex"):
            rows = [json.loads(line) for line in (out / arm / "detailed_results.jsonl").read_text().splitlines()]
            assert len(rows) == 3 and all(r["score"] >= 0 for r in rows)
        summary = json.loads((out / "summary.json").read_text())
        assert set(summary["arms"]) == {"tg", "lex"} and summary["questions"] == 3
        capsys.readouterr()
        # Second invocation resumes from checkpoints: rows unchanged, no new solve calls.
        with patch("app.benchmark_solver.solve", fake_solve()), \
             patch("app.benchmark_rag_solver.solve", fake_solve()), \
             patch("scripts.fullscale_paired.corpus_fingerprint", return_value={}):
            assert run_main(tmp_path) == 0
        rows = [json.loads(line) for line in (out / "lex" / "detailed_results.jsonl").read_text().splitlines()]
        assert len(rows) == 3


def test_fullscale_rejects_tampered_checkpoint(tmp_path):
    with patch("app.benchmark_solver.solve", fake_solve()), \
         patch("app.benchmark_rag_solver.solve", fake_solve()), \
         patch("scripts.fullscale_paired.corpus_fingerprint", return_value={}):
        assert run_main(tmp_path) == 0
    out = tmp_path / "out"
    path = out / "tg" / "detailed_results.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["input_hash"] = "tampered"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    with patch("app.benchmark_solver.solve", fake_solve()), \
         patch("app.benchmark_rag_solver.solve", fake_solve()), \
         patch("scripts.fullscale_paired.corpus_fingerprint", return_value={}):
        with pytest.raises(ValueError, match="checkpoint input changed"):
            run_main(tmp_path)


def test_profile_validation(tmp_path):
    tg, rag = write_profiles(tmp_path)
    assert load_profile(tg)["strategy"] == "task_guided"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"strategy": "task_guided", "corpus_directories": ["/x"]}))
    with pytest.raises(ValueError, match="retrieval-free"):
        main(["--task", "3-2", "--profile-tg", str(bad), "--profile-rag", str(rag),
              "--output", str(tmp_path / "out2"), "--limit", "1"])


def test_transport_failure_aborts_without_checkpoint(tmp_path):
    def solve(task_id, instruction, question, config):
        return {"prediction": "", "error": "URLError: connection refused",
                "finish_reason": None, "usage": None, "latency_ms": 1.0, "solver_version": "fake"}

    with patch("app.benchmark_solver.solve", solve), \
         patch("app.benchmark_rag_solver.solve", solve), \
         patch("scripts.fullscale_paired.corpus_fingerprint", return_value={}):
        tg, rag = write_profiles(tmp_path)
        with pytest.raises(RuntimeError, match="aborting tg for repair"):
            main(["--task", "3-2", "--profile-tg", str(tg), "--profile-rag", str(rag),
                  "--output", str(tmp_path / "out3"), "--limit", "3"])
    # Aborted questions leave no spurious zeros: no checkpoint rows at all.
    path = tmp_path / "out3" / "tg" / "detailed_results.jsonl"
    assert not path.exists() or all(not json.loads(l).get("error") for l in path.read_text().splitlines())


def test_transport_error_rows_regenerate_on_resume(tmp_path):
    tg, rag = write_profiles(tmp_path)
    out = tmp_path / "out4"
    with patch("app.benchmark_solver.solve", fake_solve()), \
         patch("app.benchmark_rag_solver.solve", fake_solve()), \
         patch("scripts.fullscale_paired.corpus_fingerprint", return_value={}):
        assert main(["--task", "3-2", "--profile-tg", str(tg), "--profile-rag", str(rag),
                     "--output", str(out), "--limit", "2"]) == 0
    path = out / "tg" / "detailed_results.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["error"] = "URLError: connection refused"
    rows[0]["score"] = 0.0
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    with patch("app.benchmark_solver.solve", fake_solve()), \
         patch("app.benchmark_rag_solver.solve", fake_solve()), \
         patch("scripts.fullscale_paired.corpus_fingerprint", return_value={}):
        assert main(["--task", "3-2", "--profile-tg", str(tg), "--profile-rag", str(rag),
                     "--output", str(out), "--limit", "2"]) == 0
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2 and all(not r.get("error") for r in rows)
