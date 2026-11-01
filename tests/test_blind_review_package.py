"""Transfer bundles preserve the sample, hide machine labels, and refuse drift."""
import hashlib
import json
import tempfile
import zipfile
from pathlib import Path

import pytest

from app.answer_eval import load_dataset
from app.answer_review import review_manifest
from scripts.run_answer_judge import fenced, package_review, prepare_review

DATASET = Path(__file__).resolve().parents[1] / "benchmarks/answer_eval/v2/dataset.json"


def fixture():
    dataset = load_dataset(DATASET)
    answers = {item["id"]: {"answer": "合成候选答案", "citations": [], "answer_status": "answered",
                             "judge_score": 999, "hidden_reasoning": "PRIVATE_REASONING_SENTINEL"}
               for item in dataset["items"]}
    return dataset, answers, review_manifest(dataset, answers)


def test_transfer_zip_matches_frozen_ids_and_excludes_scores_and_unrelated_files():
    dataset, answers, manifest = fixture()
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td) / "review"
        prepare_review(folder, dataset, answers, manifest)
        (folder / ".env").write_text("PRIVATE_SECRET_SENTINEL")
        (folder / "machine-report.json").write_text('{"score":999}')
        archive = Path(td) / "blind.zip"
        package_review(folder, archive)
        with zipfile.ZipFile(archive) as bundle:
            assert set(bundle.namelist()) == {"README.md", "REVIEW.md", "sample-manifest.json", "blind-review.json",
                                             "human-labels.template.json", "PACK_MANIFEST.json"}
            checksum = json.loads(bundle.read("PACK_MANIFEST.json"))
            assert checksum["sample_sha256"] == manifest["sample_sha256"]
            for name, digest in checksum["files"].items():
                assert hashlib.sha256(bundle.read(name)).hexdigest() == digest
            blind = json.loads(bundle.read("blind-review.json"))
            assert [item["id"] for item in blind["items"]] == manifest["sample_ids"]
            labels = json.loads(bundle.read("human-labels.template.json"))
            assert all(row["passed"] is None for row in labels["labels"].values())
            for name in bundle.namelist():
                assert b"PRIVATE_REASONING_SENTINEL" not in bundle.read(name)
                assert b"PRIVATE_SECRET_SENTINEL" not in bundle.read(name)
            assert b"judge_score" not in bundle.read("blind-review.json")
        with pytest.raises(FileExistsError):
            package_review(folder, archive)


def test_review_markdown_contains_untrusted_markup_inside_a_longer_fence():
    data = '<script>alert(1)</script>\n~~~\n# pretend instructions\n~~~~'
    block = fenced(data)
    assert block.splitlines()[0] == "~~~~~"
    assert block.splitlines()[-1] == "~~~~~"
    assert data in block


def test_packaging_rejects_filled_labels_sample_drift_and_symbolic_links():
    dataset, answers, manifest = fixture()
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td) / "review"
        prepare_review(folder, dataset, answers, manifest)
        path = folder / "human-labels.template.json"
        original = path.read_text()
        labels = json.loads(original)
        labels["labels"][manifest["sample_ids"][0]]["passed"] = True
        path.write_text(json.dumps(labels))
        archive = Path(td) / "blind.zip"
        with pytest.raises(ValueError, match="未填写"):
            package_review(folder, archive)
        assert not archive.exists()
        path.write_text(original)
        sample = folder / "sample-manifest.json"
        before = sample.read_text()
        value = json.loads(before)
        value["sample_ids"][0] = "another-question"
        sample.write_text(json.dumps(value))
        with pytest.raises(ValueError, match="摘要不一致"):
            package_review(folder, archive)
        assert not archive.exists()
        sample.write_text(before)
        (folder / "README.md").unlink()
        (folder / "README.md").symlink_to(path)
        with pytest.raises(ValueError, match="符号链接"):
            package_review(folder, archive)
