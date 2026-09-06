from unittest.mock import patch

from app.benchmark_rag_solver import solve


CONFIG = {"model": "test-model", "url": "http://localhost:8000/v1", "strategy": "statutory_rag",
          "corpus_directories": ["/test-corpus"], "reference": "GOLD_CANARY"}
CALL = {"prediction": "final", "error": None, "finish_reason": "stop", "usage": {"completion_tokens": 1}}


def test_rag_only_adds_provenanced_context_and_keeps_original_question():
    context = {"context": "official article", "hits": [{"source_url": "https://example.gov.cn/law"}], "warnings": []}
    with patch("app.benchmark_rag_solver.retrieve", return_value=context) as retrieve, patch("app.benchmark_rag_solver._call", return_value=CALL) as call:
        result = solve("1-1", "instruction", "question", CONFIG)
    retrieve.assert_called_once_with("1-1", "question", ["/test-corpus"])
    messages, config, phase = call.call_args.args
    assert messages[1] == {"role": "user", "content": "instruction\nquestion"}
    assert messages[2] == {"role": "user", "content": "official article"}
    assert "GOLD_CANARY" not in str(messages) + str(config)
    assert result["prediction"] == "final" and result["retrieval"] == context and result["postprocess"]["applied"] is False
    assert len(result["calls"]) == 1 and phase == "statutory_rag"


def test_exact_article_call_is_retained_but_final_answer_uses_frozen_text():
    context = {"context": "official article", "mode": "exact_article", "warnings": [], "hits": [{
        "document_id": "law-2020", "article_id": "1", "text": "第一条 官方正文。",
    }]}
    with patch("app.benchmark_rag_solver.retrieve", return_value=context), patch(
        "app.benchmark_rag_solver._call", return_value=CALL,
    ):
        result = solve("1-1", "instruction", "question", CONFIG)
    assert result["calls"][0]["prediction"] == "final"
    assert result["prediction"] == "官方正文。"
    assert result["postprocess"]["document_articles"] == ["law-2020/1"]


def test_no_hits_has_explicit_unchanged_guided_fallback():
    with patch("app.benchmark_rag_solver.retrieve", return_value={"context": "", "hits": []}), patch("app.benchmark_rag_solver.base_solve", return_value={**CALL, "calls": [CALL]}) as fallback:
        result = solve("2-1", "instruction", "question", CONFIG)
    assert fallback.call_args.args[3]["strategy"] == "task_guided"
    assert "reference" not in fallback.call_args.args[3]
    assert result["retrieval"]["hits"] == [] and result["prediction"] == "final"


def test_bad_corpus_fails_closed_without_model_call():
    with patch("app.benchmark_rag_solver.retrieve", side_effect=ValueError("integrity mismatch")), patch("app.benchmark_rag_solver._call") as call:
        result = solve("1-1", "instruction", "question", CONFIG)
    assert "integrity mismatch" in result["error"] and result["calls"] == []
    call.assert_not_called()


def test_correction_postprocess_is_recorded_after_raw_call():
    context = {"context": "", "hits": []}
    raw = {**CALL, "prediction": "在此情况下，各别部门改为 2012 年。", "calls": [{**CALL, "prediction": "在此情况下，各别部门改为 2012 年。"}]}
    with patch("app.benchmark_rag_solver.retrieve", return_value=context), patch("app.benchmark_rag_solver.base_solve", return_value=raw):
        result = solve("2-1", "instruction", "在此情况下,各别部门改为2012年", CONFIG)
    assert result["prediction"] == "在此情况下,各别部门改为2012年"
    assert result["calls"][0]["prediction"].endswith("。") and result["postprocess"]["applied"]
