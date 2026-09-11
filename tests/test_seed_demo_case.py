"""Offline tests for the complex demo-case seeder (temporary directory, no network)."""

from __future__ import annotations

import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bank_transactions import parse_csv
from scripts.seed_demo_case import _bank_transactions, _documents, _evidence, seed


class DemoCaseSeedTests(unittest.TestCase):
    def _seed(self, directory: str) -> dict:
        data_dir = Path(directory)
        with patch.dict(
            os.environ,
            {
                "LAW_REVIEW_DATA_DIR": directory,
                "LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB": str(data_dir / "checkpoints.sqlite"),
                "LAW_REVIEW_AUTH_MODE": "local",
            },
        ):
            return seed(data_dir)

    def test_seed_writes_full_feature_surface(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lexvault-demo-seed-") as directory:
            summary = self._seed(directory)
            self.assertGreaterEqual(summary["documents"], 18)
            self.assertGreaterEqual(summary["pages"], 50)
            self.assertGreaterEqual(summary["evidence"], 20)
            self.assertGreater(summary["bank_transactions"], 0)
            self.assertGreaterEqual(summary["conversations"], 6)
            self.assertGreaterEqual(summary["agent_runs"], 8)
            self.assertGreaterEqual(summary["gap_detections"], 5)
            self.assertTrue((Path(directory) / "law_review.db").exists())

    def test_seed_is_isolated_from_other_cases(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lexvault-demo-seed-") as directory:
            self._seed(directory)
            self._seed(directory)
            # Seeding twice creates two independent demo cases; it never deletes.
            with patch.dict(os.environ, {"LAW_REVIEW_DATA_DIR": directory}):
                from app.db import connect

                with connect() as conn:
                    cases = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
            self.assertEqual(cases, 2)

    def test_evidence_targets_real_pages(self) -> None:
        volumes = _documents()
        self.assertGreaterEqual(len(volumes), 17)
        for volume in volumes:
            self.assertGreaterEqual(len(volume["pages"]), 1)
        # _evidence() needs ids; reuse the catalogue order with synthetic ids.
        indexed = [{"id": i + 1, "pages": len(v["pages"])} for i, v in enumerate(volumes)]
        for item in _evidence(indexed):
            self.assertGreaterEqual(item["source_page_start"], 1)
            self.assertLessEqual(item["source_page_start"], item["source_page_end"])

    def test_bank_csv_parses_into_directed_rows(self) -> None:
        rows = parse_csv(_bank_transactions(random.Random(1)))
        self.assertGreater(len(rows), 100)
        directions = {row.direction for row in rows}
        self.assertIn("inflow", directions)
        self.assertIn("outflow", directions)
        self.assertTrue(all(row.amount_minor and row.amount_minor > 0 for row in rows))
        self.assertTrue(all(row.transaction_time for row in rows))

    def test_documents_carry_narrative_terms(self) -> None:
        text = "\n".join(page for volume in _documents() for page in volume["pages"])
        for term in ("云启科技股份有限公司", "恒远商贸有限公司", "43,270,000", "非法吸收公众存款"):
            self.assertIn(term, text)

    def test_seed_emits_case_specific_ground_truth(self) -> None:
        import json

        from app.evaluation import _case_ground_truth

        with tempfile.TemporaryDirectory(prefix="lexvault-demo-gt-") as directory:
            case_id = self._seed(directory)["case"]["id"]
            path = Path(directory) / f"ground-truth-case-{case_id}.json"
            self.assertTrue(path.exists())
            supplied = json.loads(path.read_text(encoding="utf-8"))
            with patch.dict(os.environ, {"LAW_REVIEW_DATA_DIR": directory}):
                # The demo case must supply its own answers; the built-in demo
                # dataset is never reused for it.
                with self.assertRaisesRegex(ValueError, "非演示案件"):
                    _case_ground_truth(case_id, None)
                truth, dataset = _case_ground_truth(case_id, supplied)
            self.assertEqual(dataset, "case-specific-ground-truth")
            self.assertTrue(truth)
            self.assertTrue(any(not item["expected"] for item in truth))


if __name__ == "__main__":
    unittest.main()
