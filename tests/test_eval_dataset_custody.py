"""Custody rejects contamination and never turns empty annotations into agreement."""
import hashlib
import json
import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path

from scripts.eval_dataset_custody import (
    DIMENSIONS,
    agreement,
    annotation_package,
    seal,
    validate_coverage,
)


def fixture():
    text = "虚构材料记载仓储费为713元。"
    sources = [{"case_id": 101, "document_name": "new.txt", "page": 1, "text": text,
                "sha256": hashlib.sha256(text.encode()).hexdigest()}]
    data = {"version": 2, "annotation_source": "coding_agent_not_human", "sources": sources,
            "source_sha256": hashlib.sha256(json.dumps(sources, ensure_ascii=False, sort_keys=True,
                                                        separators=(",", ":")).encode()).hexdigest(),
            "cases": [{"case_id": 101, "documents": [{"name": "new.txt", "pages": [text]}]}],
            "items": [{"id": "a", "category": "normal", "case_id": 101, "case_description": "虚构仓储",
                       "question": "仓储费用记载多少？", "must_include": ["713"],
                       "acceptable_pages": [["new.txt", 1]], "expect_refusal": False, "requires_citation": True},
                      {"id": "b", "category": "insufficient", "case_id": 101, "case_description": "虚构仓储",
                       "question": "运输保险的签订时间是什么？", "must_include": [],
                       "acceptable_pages": [], "expect_refusal": True}]}
    protocol = {"categories": ["normal", "insufficient"], "case_count": 1, "per_category": 1,
                "independent_question_count": 2}
    return data, protocol


class CustodyTests(unittest.TestCase):
    def test_rejects_old_question_page_wrong_owner_and_false_human_claim(self):
        data, protocol = fixture()
        self.assertEqual(validate_coverage(data, protocol, [])["questions"], 2)
        with self.assertRaisesRegex(ValueError, "reused"):
            validate_coverage(data, protocol, [data])
        changed = deepcopy(data)
        changed["items"][0]["acceptable_pages"] = [["missing.txt", 1]]
        with self.assertRaises(ValueError):
            validate_coverage(changed, protocol, [])
        changed = deepcopy(data)
        changed["annotation_source"] = "independent_lawyer_gold"
        with self.assertRaisesRegex(ValueError, "human"):
            validate_coverage(changed, protocol, [])
        changed = deepcopy(data)
        changed["cases"][0]["documents"][0]["pages"] = ["different"]
        with self.assertRaisesRegex(ValueError, "Authored"):
            validate_coverage(changed, protocol, [])

    def _package(self, directory):
        root = Path(directory)
        data, protocol = fixture()
        (root / "candidate.py").write_text("frozen candidate")
        protocol.update(candidate_core_files={"candidate.py": hashlib.sha256(b"frozen candidate").hexdigest()},
                        live_opening_allowed=False, candidate_commit="synthetic-test")
        (root / "input.json").write_text(json.dumps(data, ensure_ascii=False))
        (root / "protocol.json").write_text(json.dumps(protocol))
        result = seal(root / "input.json", root / "protocol.json", root / "sealed", [], root)
        annotation_package(root / "sealed", root / "package")
        return root, result

    def test_seal_no_overwrite_and_changed_core_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root, result = self._package(directory)
            self.assertFalse(result["business_qualified"])
            self.assertFalse(result["model_evaluation_opened"])
            self.assertEqual((root / "sealed/dataset.json").stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(ValueError, "new private"):
                seal(root / "input.json", root / "protocol.json", root / "sealed", [], root)
            (root / "candidate.py").write_text("changed")
            with self.assertRaisesRegex(ValueError, "candidate changed"):
                seal(root / "input.json", root / "protocol.json", root / "new-seal", [], root)
            self.assertFalse((root / "new-seal").exists())

    def test_cross_case_duplicate_document_cannot_hide_an_omitted_authored_page(self):
        data, protocol = fixture()
        data["cases"][0]["documents"].append({"name": "shared.txt", "pages": ["omitted original page"]})
        text = "另一合成案明确记载金额883元。"
        data["cases"].append({"case_id": 102, "documents": [{"name": "shared.txt", "pages": [text]}]})
        data["sources"].append({"case_id": 102, "document_name": "shared.txt", "page": 1,
                                "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()})
        data["source_sha256"] = hashlib.sha256(json.dumps(data["sources"], ensure_ascii=False,
                                                          sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        other = deepcopy(data["items"])
        for item in other:
            item["id"] += "-second"
            item["case_id"] = 102
            item["question"] += "仅另一案"
            if not item["expect_refusal"]:
                item["acceptable_pages"] = [["shared.txt", 1]]
                item["must_include"] = ["883"]
        data["items"].extend(other)
        protocol.update(case_count=2, per_category=2, independent_question_count=4)
        with self.assertRaisesRegex(ValueError, "Duplicate authored"):
            validate_coverage(data, protocol, [])

    def test_zip_has_no_draft_labels_category_or_private_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _ = self._package(directory)
            with zipfile.ZipFile(root / "package/reviewers.zip") as archive:
                self.assertNotIn("private-key.json", archive.namelist())
                review = json.loads(archive.read("questions.json"))
                self.assertNotIn("must_include", json.dumps(review))
                self.assertNotIn("category", json.dumps(review))
                for name in ["rater-1.template.json", "rater-2.template.json"]:
                    labels = json.loads(archive.read(name))
                    self.assertEqual(labels["attestation"], "")
                    self.assertTrue(all(value[field] is None for value in labels["labels"].values()
                                        for field in DIMENSIONS))
            result = agreement(root / "package", None, None)
            self.assertIsNone(result["kappa"])
            self.assertFalse(result["human_annotation_complete"])

    def test_source_extra_metadata_cannot_leak_draft_labels_to_reviewers(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _ = self._package(directory)
            path = root / "sealed/dataset.json"
            data = json.loads(path.read_text())
            data["sources"][0].update(category="normal", draft_reference_answer="private draft gold")
            data["source_sha256"] = hashlib.sha256(json.dumps(
                data["sources"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest()
            validate_coverage(data, fixture()[1], [])
            path.write_text(json.dumps(data))
            manifest = json.loads((root / "sealed/manifest.json").read_text())
            manifest["dataset_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            (root / "sealed/manifest.json").write_text(json.dumps(manifest))
            annotation_package(root / "sealed", root / "safe-package")
            with zipfile.ZipFile(root / "safe-package/reviewers.zip") as archive:
                review = archive.read("questions.json").decode()
                self.assertNotIn("draft_reference_answer", review)
                self.assertNotIn("private draft gold", review)
                self.assertNotIn('"category"', review)

    def test_real_label_import_requires_distinct_raters_full_labels_and_same_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _ = self._package(directory)
            template = json.loads((root / "package/reviewers/rater-1.template.json").read_text())
            left, right = root / "left.json", root / "right.json"
            template.update(reviewer="rater-one", attestation="independent_human")
            for label in template["labels"].values():
                label.update(dict.fromkeys(DIMENSIONS, True), rationale="核对原文", reference_answer="原文事实",
                             evidence_pages=[["new.txt", 1]])
            left.write_text(json.dumps(template))
            right.write_text(json.dumps(template))
            with self.assertRaisesRegex(ValueError, "distinct"):
                agreement(root / "package", left, right)
            template["reviewer"] = "rater-two"
            first = next(iter(template["labels"].values()))
            first["requires_refusal"] = False
            right.write_text(json.dumps(template))
            report = agreement(root / "package", left, right)
            self.assertEqual(len(report["disputed_ids"]), 1)
            self.assertIsNone(report["free_text_semantic_agreement"])
            self.assertFalse(report["business_qualified"])
            first["evidence_pages"] = [["not-in-this-case.txt", 1]]
            right.write_text(json.dumps(template))
            with self.assertRaisesRegex(ValueError, "existing pages"):
                agreement(root / "package", left, right)
            first["evidence_pages"] = [["new.txt", 1]]
            first["requires_refusal"] = None
            right.write_text(json.dumps(template))
            with self.assertRaisesRegex(ValueError, "fully reviewed"):
                agreement(root / "package", left, right)
            template["review_sha256"] = "wrong"
            right.write_text(json.dumps(template))
            with self.assertRaisesRegex(ValueError, "binding"):
                agreement(root / "package", left, right)
