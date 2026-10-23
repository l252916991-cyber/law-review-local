"""Offline tests for synthetic capacity seeding (temporary directory, no network)."""

from __future__ import annotations

import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.capacity_seed import build_page_text, seed


class SeedTests(unittest.TestCase):
    def test_seed_creates_expected_counts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lexvault-capacity-seed-") as directory:
            data_dir = Path(directory)
            with patch.dict(
                os.environ,
                {
                    "LAW_REVIEW_DATA_DIR": directory,
                    "LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB": str(data_dir / "checkpoints.sqlite"),
                    "LAW_REVIEW_AUTH_MODE": "local",
                },
            ):
                summary = seed(
                    data_dir,
                    cases=1,
                    docs_per_case=2,
                    pages_per_doc=3,
                    evidence_per_case=2,
                    annotations_per_evidence=1,
                    audit_events=2,
                    conversations=1,
                    agent_runs=1,
                )
            case = summary["cases"][0]
            self.assertEqual(case["documents"], 2)
            self.assertEqual(case["pages"], 6)
            self.assertEqual(case["evidence"], 2)
            self.assertEqual(case["annotations"], 2)
            self.assertEqual(summary["totals"]["pages"], 6)
            self.assertEqual(summary["totals"]["fts_rows"], 6)
            self.assertTrue((data_dir / "law_review.db").exists())

    def test_seed_is_deterministic_for_same_seed(self) -> None:
        rng_a = random.Random(7)
        rng_b = random.Random(7)
        self.assertEqual(
            build_page_text(rng_a, "合成材料.txt", 1, 2),
            build_page_text(rng_b, "合成材料.txt", 1, 2),
        )

    def test_page_text_carries_searchable_terms(self) -> None:
        text = build_page_text(random.Random(1), "合成材料.txt", 2, 5)
        self.assertIn("第2/5页", text)
        self.assertIn("借款", text)


if __name__ == "__main__":
    unittest.main()
