"""Answer-level evaluation: dataset grounding, deterministic scoring, judge parsing."""
import tempfile
import unittest
from pathlib import Path

from app.answer_eval import (
    cohen_kappa,
    evaluate_answers,
    judge_answer,
    load_dataset,
    normalise,
    score_answer,
    summarise,
)
from app.db import connect, db_scope, init_db

DATASET = Path(__file__).resolve().parents[1] / "benchmarks/answer_eval/v1/demo-case.json"


class DatasetGroundingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-answer-eval-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=True)
        with connect() as conn:
            rows = conn.execute(
                """SELECT d.name, p.page_no, p.text FROM pages p
                   JOIN documents d ON d.id=p.document_id WHERE d.case_id=1"""
            ).fetchall()
        self.pages = {(row["name"], row["page_no"]): row["text"] for row in rows}

    def test_dataset_ids_are_unique_and_refusals_have_no_pages(self):
        dataset = load_dataset(DATASET)
        ids = [item["id"] for item in dataset["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        for item in dataset["items"]:
            if item["expect_refusal"]:
                self.assertEqual(item["acceptable_pages"], [])

    def test_every_acceptable_page_exists_in_the_demo_case(self):
        dataset = load_dataset(DATASET)
        for item in dataset["items"]:
            for name, page in item["acceptable_pages"]:
                with self.subTest(item=item["id"], page=f"{name}:{page}"):
                    self.assertIn((name, page), self.pages)

    def test_every_required_fact_appears_on_an_acceptable_page(self):
        dataset = load_dataset(DATASET)
        for item in dataset["items"]:
            if item["expect_refusal"]:
                continue
            corpus = "".join(normalise(self.pages[(name, page)]) for name, page in item["acceptable_pages"])
            for value in item["must_include"]:
                with self.subTest(item=item["id"], fact=value):
                    self.assertIn(normalise(value), corpus)


class ScoringTests(unittest.TestCase):
    ITEM = {
        "id": "t-1",
        "question": "金额与人数？",
        "must_include": ["2,860", "47"],
        "must_not_claim": ["1亿元"],
        "acceptable_pages": [["流水.txt", 1]],
        "expect_refusal": False,
    }

    def test_all_facts_with_an_acceptable_citation_pass(self):
        result = score_answer(
            self.ITEM, "累计流入2,860万元，涉及47名投资人。请律师复核。",
            citations=[{"document_name": "流水.txt", "page": 1}],
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["coverage"], 1.0)
        self.assertEqual(result["citation_precision"], 1.0)

    def test_missing_fact_fails(self):
        result = score_answer(self.ITEM, "累计流入2,860万元。", citations=[{"document_name": "流水.txt", "page": 1}])
        self.assertFalse(result["passed"])
        self.assertEqual(result["missing"], ["47"])

    def test_forbidden_claim_fails_even_when_facts_are_present(self):
        result = score_answer(
            self.ITEM, "累计流入2,860万元，涉及47人，规模约1亿元。",
            citations=[{"document_name": "流水.txt", "page": 1}],
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["violations"], ["1亿元"])

    def test_citation_outside_the_acceptable_set_fails(self):
        result = score_answer(
            self.ITEM, "累计流入2,860万元，涉及47人。",
            citations=[{"document_name": "流水.txt", "page": 2}],
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["citation_precision"], 0.0)

    def test_mixed_citations_report_partial_precision(self):
        result = score_answer(
            self.ITEM, "累计流入2,860万元，涉及47人。",
            citations=[{"document_name": "流水.txt", "page": 1}, {"document_name": "其他.txt", "page": 9}],
        )
        self.assertEqual(result["citation_precision"], 0.5)
        self.assertFalse(result["passed"])

    def test_refusal_items_require_a_refusal_and_no_assertion(self):
        item = {"id": "r-1", "question": "判决结果？", "must_include": [], "must_not_claim": ["判处"],
                "acceptable_pages": [], "expect_refusal": True}
        refused = score_answer(item, "本案材料中没有判决文书，资料不足，无法判断。", citations=[])
        self.assertTrue(refused["passed"])
        asserted = score_answer(item, "法院判处有期徒刑三年。", citations=[])
        self.assertFalse(asserted["passed"])

    def test_normalise_ignores_separators_and_full_width_percent(self):
        self.assertEqual(normalise("2,860 万元 12％"), normalise("2860万元12%"))


class SummaryTests(unittest.TestCase):
    def test_summary_uses_only_answerable_items_for_coverage(self):
        rows = [
            {"id": "a", "passed": True, "coverage": 1.0, "violations": [], "citation_precision": 1.0,
             "refusal_expected": False},
            {"id": "b", "passed": False, "coverage": 0.5, "violations": ["x"], "citation_precision": None,
             "refusal_expected": False},
            {"id": "c", "passed": True, "coverage": 1.0, "violations": [], "citation_precision": None,
             "refusal_expected": True},
        ]
        summary = summarise(rows)
        self.assertEqual(summary["queries"], 3)
        self.assertEqual(summary["passed"], 2)
        self.assertEqual(summary["coverage_avg"], 0.75)
        self.assertEqual(summary["citation_precision_avg"], 1.0)
        self.assertEqual(summary["violation_count"], 1)
        self.assertEqual(summary["refusal_correct"], 1)

    def test_evaluate_rejects_missing_answers(self):
        dataset = {"name": "x", "version": 1, "items": [
            {"id": "a", "question": "q", "must_include": [], "must_not_claim": [],
             "acceptable_pages": [], "expect_refusal": False}]}
        with self.assertRaises(ValueError):
            evaluate_answers(dataset, {})


class KappaTests(unittest.TestCase):
    def test_perfect_agreement(self):
        self.assertEqual(cohen_kappa([True, False, True], [True, False, True]), 1.0)

    def test_total_disagreement_is_negative_one(self):
        self.assertEqual(cohen_kappa([True, False], [False, True]), -1.0)

    def test_partial_agreement_is_between(self):
        value = cohen_kappa([True, True, False, False], [True, False, False, False])
        self.assertIsNotNone(value)
        self.assertTrue(-1 <= value < 1)

    def test_requires_matching_non_empty_labels(self):
        with self.assertRaises(ValueError):
            cohen_kappa([], [])
        with self.assertRaises(ValueError):
            cohen_kappa([True], [True, False])


class JudgeTests(unittest.TestCase):
    ITEM = {"id": "j-1", "question": "金额？", "must_include": ["2860"], "must_not_claim": []}

    def test_parses_structured_judge_output_and_records_prompt_version(self):
        result = judge_answer(
            self.ITEM, "2,860万元。",
            [{"document_name": "流水.txt", "page": 1, "quote": "2,860万元"}],
            call_model=lambda prompt: '{"coverage_ok": true, "unsupported_claims": [], "citation_ok": true, "refusal_ok": true, "rationale": "ok"}',
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["judge_version"], "answer-judge-v2-frozen-sources")
        self.assertTrue(result["parsed"]["coverage_ok"])

    def test_garbage_output_is_marked_unavailable_instead_of_passing(self):
        result = judge_answer(self.ITEM, "x", [], call_model=lambda prompt: "我觉得还行")
        self.assertFalse(result["available"])
        self.assertIsNone(result["parsed"])


if __name__ == "__main__":
    unittest.main()
