"""Structural checks for the frozen multi-turn samples (M01).

These tests do not run a model or a web process. They prove that every expected
fact is actually supported by the page it names, that the frozen session counts
match the execution plan, and that the samples stay synthetic.
"""
import re
import unittest

from tests import multiturn_fixtures as fixtures

ANAPHORA = re.compile(r"(?:[它他她]|该(?:合|协|文|材|份|笔|案)|这(?:份|个|家|笔)|那(?:份|个|家|笔)|上述|前述|刚才)")


class MultiturnFixtureTest(unittest.TestCase):
    def test_frozen_session_and_question_counts(self):
        self.assertEqual(len(fixtures.SESSIONS), 12)
        followups = fixtures.followups()
        self.assertGreaterEqual(len(followups), 24)
        self.assertEqual(sum(1 for session in fixtures.SESSIONS if session["split"] == "dev"), 8)
        self.assertEqual(sum(1 for session in fixtures.SESSIONS if session["split"] == "reserve"), 4)
        self.assertEqual(len(fixtures.SINGLE_TURN), 8)
        ids = [session["id"] for session in fixtures.SESSIONS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_referenced_document_exists(self):
        for scenario, keys in fixtures.SCENARIOS.items():
            for key in keys:
                self.assertIn(key, fixtures.DOCUMENTS, f"scenario {scenario} references {key}")
        for session in fixtures.SESSIONS:
            self.assertIn(session["scenario"], fixtures.SCENARIOS)
            if "cross_case_scenario" in session:
                self.assertIn(session["cross_case_scenario"], fixtures.SCENARIOS)
            for followup in session["followups"]:
                for key in followup.get("objects", []):
                    self.assertIn(key, fixtures.DOCUMENTS)
                for key in followup.get("options", []):
                    self.assertIn(key, fixtures.DOCUMENTS)
                for key in followup.get("citations_within", []):
                    self.assertIn(key, fixtures.DOCUMENTS)
                for fact in followup.get("facts", []):
                    self.assertIn(fact["document"], fixtures.DOCUMENTS)

    def test_expected_facts_are_on_the_named_page(self):
        facts = [
            fact
            for collection in (fixtures.followups(), fixtures.SINGLE_TURN)
            for followup in collection
            for fact in followup.get("facts", [])
        ]
        self.assertTrue(facts)
        for fact in facts:
            document = fixtures.DOCUMENTS[fact["document"]]
            pages = document["pages"]
            self.assertTrue(1 <= fact["page"] <= len(pages), fact)
            self.assertIn(fact["text"], pages[fact["page"] - 1], fact)

    def test_expectations_are_complete(self):
        statuses = {"answered", "clarification", "insufficient_evidence"}
        for followup in fixtures.followups():
            self.assertIn(followup["expected_status"], statuses, followup["id"])
            self.assertTrue(followup["question"].strip(), followup["id"])
            if followup.get("lifecycle"):
                self.assertIn(followup["lifecycle"], {"archived", "deleted"})
                continue
            if followup["expected_status"] == "answered":
                self.assertTrue(followup.get("facts"), followup["id"])
                self.assertTrue(followup.get("citations_within"), followup["id"])
            elif followup["expected_status"] == "clarification":
                self.assertTrue(followup.get("options"), followup["id"])
                self.assertFalse(followup.get("facts"), followup["id"])
            else:
                self.assertTrue(followup.get("objects"), followup["id"])
                self.assertFalse(followup.get("facts"), followup["id"])

    def test_single_turn_regressions_stay_object_explicit(self):
        for question in fixtures.SINGLE_TURN:
            self.assertIsNone(ANAPHORA.search(question["question"]), question["id"])
            self.assertTrue(question["facts"], question["id"])

    def test_samples_stay_synthetic(self):
        joined = "\n".join(
            f"{document['name']}\n" + "\n".join(document["pages"])
            for document in fixtures.DOCUMENTS.values()
        )
        self.assertIn("合成样本", joined)
        for pattern in (r"\d{6,}", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", r"1[3-9]\d{9}"):
            self.assertIsNone(re.search(pattern, joined), pattern)
        self.assertNotIn("身份证", joined)


if __name__ == "__main__":
    unittest.main()
