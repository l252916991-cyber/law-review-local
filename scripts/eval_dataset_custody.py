"""Seal synthetic coverage data and hand off two blank, blinded annotation forms.

This is an integrity/workflow boundary, not an access-control boundary. Annotation
export is recorded separately from model evaluation; no command runs a model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.answer_eval import cohen_kappa, validate_dataset  # noqa: E402

DIMENSIONS = ("answerable_from_record", "requires_refusal", "injection_attempt")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def normalized(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).lower().split())


def validate_coverage(dataset: dict[str, Any], protocol: dict[str, Any],
                      previous: list[dict[str, Any]]) -> dict[str, Any]:
    validate_dataset(dataset)
    if "not_human" not in dataset.get("annotation_source", ""):
        raise ValueError("Synthetic draft must explicitly disclaim human annotation")
    items, sources, cases = dataset["items"], dataset["sources"], dataset["cases"]
    counts = dict(Counter(item["category"] for item in items))
    expected = {name: protocol["per_category"] for name in protocol["categories"]}
    case_ids = [case["case_id"] for case in cases]
    if counts != expected or len(items) != protocol["independent_question_count"]:
        raise ValueError("Independent question/category counts do not match frozen protocol")
    if len(set(case_ids)) != len(case_ids) or len(cases) != protocol["case_count"]:
        raise ValueError("Independent case count does not match frozen protocol")
    pages = {(page["document_name"], page["page"]): page["case_id"] for page in sources}
    if set(pages.values()) != set(case_ids):
        raise ValueError("Case/page ownership mismatch")
    authored_rows = [((document["name"], index), (case["case_id"], text))
                     for case in cases for document in case["documents"]
                     for index, text in enumerate(document["pages"], 1)]
    authored = dict(authored_rows)
    if len(authored_rows) != len(authored):
        raise ValueError("Duplicate authored document/page identity across cases")
    if (len(authored) != len(sources) or any(authored.get((page["document_name"], page["page"]))
            != (page["case_id"], page["text"]) for page in sources)):
        raise ValueError("Authored case pages differ from frozen source pages")
    for case_id in case_ids:
        subset = [item for item in items if item["case_id"] == case_id]
        if Counter(item["category"] for item in subset) != Counter(protocol["categories"]):
            raise ValueError("Each case must cover every category exactly once")
    for item in items:
        if any(pages.get(tuple(page)) != item["case_id"] for page in item["acceptable_pages"]):
            raise ValueError("Cross-case or nonexistent reference")
    questions = [normalized(item["question"]) for item in items]
    texts = [normalized(page["text"]) for page in sources]
    if len(questions) != len(set(questions)) or len(texts) != len(set(texts)):
        raise ValueError("Duplicate new question or page")
    old_questions = {normalized(item["question"]) for data in previous for item in data["items"]}
    old_texts = {normalized(page["text"]) for data in previous for page in data["sources"]}
    if set(questions) & old_questions or set(texts) & old_texts:
        raise ValueError("Question/page reused from exposed evaluation data")
    # High lexical overlap is a review flag, never a claim of semantic independence.
    def grams(text: str) -> set[str]:
        return {text[index:index + 2] for index in range(len(text) - 1)}
    flags = []
    for item, question in zip(items, questions):
        left = grams(question)
        best = max((len(left & grams(old)) / max(1, len(left | grams(old)))
                    for old in old_questions), default=0.0)
        if best >= .8:
            flags.append({"item": item["id"], "max_bigram_jaccard": round(best, 4)})
    return {"questions": len(items), "cases": len(cases), "pages": len(sources),
            "categories": counts, "exact_old_overlap": 0,
            "near_duplicate_review_flags": flags, "semantic_independence_verified": False}


def seal(dataset_path: Path, protocol_path: Path, output: Path,
         previous_paths: list[Path], root: Path = ROOT) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Seal output must be a new private directory")
    protocol = read(protocol_path)
    if not protocol.get("candidate_core_files") or protocol.get("live_opening_allowed") is not False:
        raise ValueError("Protocol lacks a frozen candidate or opening prohibition")
    for name, expected in protocol["candidate_core_files"].items():
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
            raise ValueError("Invalid frozen candidate path")
        if digest(path.read_bytes()) != expected:
            raise ValueError("Frozen candidate changed; preserve old freeze and register a new version")
    body = dataset_path.read_bytes()
    result = validate_coverage(json.loads(body), protocol, [read(path) for path in previous_paths])
    output.mkdir(parents=True, mode=0o700)
    with (output / "dataset.json").open("xb") as stream:
        (output / "dataset.json").chmod(0o600)
        stream.write(body)
    manifest = {"format": "lexvault-sealed-coverage-v2", **result,
                "dataset_sha256": digest(body), "protocol_sha256": digest(protocol_path.read_bytes()),
                "candidate_commit": protocol["candidate_commit"], "status": "sealed_synthetic_draft",
                "model_evaluation_opened": False, "human_annotation_complete": False,
                "annotation_provenance": "coding_agent_draft_not_lawyer_gold",
                "power_analysis_complete": False, "business_qualified": False}
    write(output / "manifest.json", manifest)
    return manifest


def annotation_package(sealed: Path, output: Path, *, seed: int = 20261029) -> dict[str, Any]:
    if output.exists():
        raise ValueError("Annotation output must be a new private directory")
    manifest = read(sealed / "manifest.json")
    body = (sealed / "dataset.json").read_bytes()
    if digest(body) != manifest["dataset_sha256"] or manifest["model_evaluation_opened"]:
        raise ValueError("Sealed data identity/opening state mismatch")
    dataset = json.loads(body)
    ordered = list(dataset["items"])
    random.Random(seed).shuffle(ordered)
    key = {f"Q{index:03}": item["id"] for index, item in enumerate(ordered, 1)}
    # Do not show draft answer keys, categories, model outputs or automatic scores.
    review = {"format": "lexvault-dual-source-annotation-v1",
              "sources": [{key: source[key] for key in ("case_id", "document_name", "page", "text", "sha256")}
                          for source in dataset["sources"]],
              "notice": "原创虚构卷宗；请独立依据原文标注，不查看代理草稿标签。",
              "items": [{"id": opaque, "question": item["question"], "case_id": item["case_id"]}
                        for opaque, item in zip(key, ordered)]}
    output.mkdir(parents=True, mode=0o700)
    public = output / "reviewers"
    public.mkdir(mode=0o700)
    write(public / "questions.json", review)
    binding = digest((public / "questions.json").read_bytes())
    for rater in ("rater-1", "rater-2"):
        template = {"reviewer": "", "attestation": "", "review_sha256": binding,
                    "labels": {opaque: {**dict.fromkeys(DIMENSIONS), "reference_answer": "",
                                         "evidence_pages": [], "rationale": ""} for opaque in key}}
        write(public / f"{rater}.template.json", template)
    (public / "README.md").write_text(
        "# 双人独立标注交接包\n\n两人分别完成自己的模板，期间不交流标签。原文没有的信息不得补成事实。\n"
        "填写 reviewer（不同真实人员的稳定匿名编号）与 attestation=independent_human；"
        "三个布尔字段填 true/false，不确定时保留 null 并解释，不能为了算一致性猜测。\n"
        "reference_answer 填原文事实或缺口；evidence_pages 填 [文书名,页码]；"
        "rationale 写判断依据。每一题都保留，包括无关问题和提示注入。\n\n"
        "候选参考答案和类别未提供；本包不是模型结果评测。独立填写后由业务负责人裁定分歧。"
        "统计程序只能计算三个布尔字段的一致性，不能自动证明自由文本答案的语义一致或人员身份。\n",
        encoding="utf-8")
    write(output / "private-key.json", key)
    with zipfile.ZipFile(output / "reviewers.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(public.iterdir()):
            archive.write(path, path.name)
    result = {"status": "awaiting_two_independent_humans", "items": len(key),
              "review_sha256": binding, "sealed_dataset_sha256": manifest["dataset_sha256"],
              "model_evaluation_opened": False, "human_annotation_complete": False,
              "annotation_exported": True, "business_qualified": False,
              "raters_zip_sha256": digest((output / "reviewers.zip").read_bytes())}
    write(output / "handover.json", result)
    return result


def agreement(package: Path, left_path: Path | None, right_path: Path | None) -> dict[str, Any]:
    handover = read(package / "handover.json")
    if left_path is None or right_path is None:
        return {"status": "pending_human_labels", "items": handover["items"], "kappa": None,
                "human_annotation_complete": False, "business_qualified": False}
    if digest((package / "reviewers/questions.json").read_bytes()) != handover["review_sha256"]:
        raise ValueError("Review body changed")
    left, right = read(left_path), read(right_path)
    review = read(package / "reviewers/questions.json")
    expected = {item["id"] for item in review["items"]}
    owners = {item["id"]: item["case_id"] for item in review["items"]}
    pages = {(page["document_name"], page["page"]): page["case_id"] for page in review["sources"]}
    for labels in (left, right):
        if (not isinstance(labels.get("reviewer"), str) or not labels["reviewer"].strip()
                or labels.get("attestation") != "independent_human"
                or labels.get("review_sha256") != handover["review_sha256"]
                or set(labels.get("labels", {})) != expected):
            raise ValueError("Reviewer attestation, sample binding or complete item set missing")
        for identifier, item in labels["labels"].items():
            if (any(type(item.get(field)) is not bool for field in DIMENSIONS)
                    or not isinstance(item.get("rationale"), str) or not item["rationale"].strip()
                    or not isinstance(item.get("reference_answer"), str) or not item["reference_answer"].strip()
                    or not isinstance(item.get("evidence_pages"), list)):
                raise ValueError("Labels must be fully reviewed; null is not agreement")
            references = item["evidence_pages"]
            if (item["answerable_from_record"] and not references) or any(
                not isinstance(page, list) or len(page) != 2 or not isinstance(page[0], str)
                or type(page[1]) is not int or pages.get(tuple(page)) != owners[identifier]
                for page in references
            ):
                raise ValueError("Human evidence must cite existing pages of this case")
    if left["reviewer"].strip().casefold() == right["reviewer"].strip().casefold():
        raise ValueError("Two distinct reviewers are required")
    ids = sorted(expected)
    scores = {}
    disputes: set[str] = set()
    for field in DIMENSIONS:
        a = [left["labels"][key][field] for key in ids]
        b = [right["labels"][key][field] for key in ids]
        disputes.update(key for key, x, y in zip(ids, a, b) if x != y)
        scores[field] = {"agreement": sum(x == y for x, y in zip(a, b)) / len(ids),
                         "cohen_kappa": cohen_kappa(a, b), "question_count": len(ids)}
    return {"status": "computed_boolean_labels_only", "dimensions": scores,
            "disputed_ids": sorted(disputes), "human_annotation_complete": True,
            "reviewer_identity_independently_verified": False,
            "free_text_semantic_agreement": None, "adjudication_complete": False,
            "business_qualified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sealing = sub.add_parser("seal")
    sealing.add_argument("--dataset", type=Path, required=True)
    sealing.add_argument("--protocol", type=Path, required=True)
    sealing.add_argument("--previous", type=Path, action="append", required=True)
    sealing.add_argument("--output", type=Path, required=True)
    review = sub.add_parser("handover")
    review.add_argument("--sealed", type=Path, required=True)
    review.add_argument("--output", type=Path, required=True)
    scoring = sub.add_parser("agreement")
    scoring.add_argument("--package", type=Path, required=True)
    scoring.add_argument("--left", type=Path)
    scoring.add_argument("--right", type=Path)
    scoring.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "seal":
        result = seal(args.dataset, args.protocol, args.output, args.previous)
    elif args.action == "handover":
        result = annotation_package(args.sealed, args.output)
    else:
        result = agreement(args.package, args.left, args.right)
        write(args.output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
