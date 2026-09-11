from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.corpus_audit import audit
from app.corpus_canonical import REASON, build, select, source_tier
from app.legal_corpus import SCHEMA_VERSION, LegalCorpus, split_articles
from scripts.build_canonical_corpus import main as build_main


def write_corpus(root: Path, name: str, docs: list[dict]) -> str:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    for index, doc in enumerate(docs):
        payload = {"schema_version": SCHEMA_VERSION, "document_id": doc.get("document_id", f"{name}-{index}"),
                   "law_name": doc["law_name"], "aliases": doc.get("aliases", [doc["law_name"]]),
                   "version_date": doc["version_date"], "effective_date": None,
                   "version_status": "test_fixture", "source_url": doc["source_url"],
                   "publisher": doc.get("publisher"), "articles": doc["articles"]}
        raw = json.dumps(payload, ensure_ascii=False).encode()
        (directory / f"{payload['document_id']}.json").write_bytes(raw)
        entries.append({"document_file": f"{payload['document_id']}.json",
                        "document_sha256": hashlib.sha256(raw).hexdigest(), "raw_sha256": "0" * 64})
    (directory / "manifest.json").write_text(json.dumps({"schema_version": SCHEMA_VERSION, "documents": entries}))
    return str(directory)


def articles(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"article_id": aid, "text": text} for aid, text in pairs]


NPC = "https://flk.npc.gov.cn/detail?id=1"
LOCAL = "https://www.shanghang.gov.cn/law/2"


def test_source_tier_does_not_conflate_departments_with_state_council():
    assert source_tier(NPC) == 0
    assert source_tier("https://www.gov.cn/x") == 1
    assert source_tier("https://www.mem.gov.cn/x") == 2  # department, not state council
    assert source_tier(None) == 3
    assert source_tier("https://random.example/x") == 3


def test_cosmetic_duplicate_prefers_higher_authority_source(tmp_path):
    local = write_corpus(tmp_path, "local", [{"law_name": "测试法", "version_date": "2020-01-01",
                                             "source_url": LOCAL, "articles": articles(("1", "第一条 内容。"))}])
    npc = write_corpus(tmp_path, "npc", [{"law_name": "测试法", "version_date": "2020-01-01",
                                         "source_url": NPC, "articles": articles(("1", "第一条　【目的】内容,"))}])
    report = build([local, npc], tmp_path / "out")
    assert report["duplicate_keys_merged"] == 1
    entry = report["documents"][0]
    assert entry["canonicalization_reason"] == "cosmetic_duplicate"
    assert entry["canonical_source"] == npc and entry["canonical_source_url"] == NPC
    assert [item["source"] for item in entry["equivalent_sources"]] == [local]
    # Body text is copied verbatim from the chosen publication, not re-normalized.
    body = json.loads((tmp_path / "out" / entry["document_file"]).read_text())
    assert body["articles"][0]["text"] == "第一条　【目的】内容,"


def test_boundary_difference_prefers_leak_free_copy(tmp_path):
    leaky = write_corpus(tmp_path, "leaky", [{"law_name": "测试法", "version_date": "2020-01-01",
                                             "source_url": NPC, "articles": articles(
                                                 ("1", "第一条 甲。"), ("2", "第二条 乙。\n附 则"))}])
    clean = write_corpus(tmp_path, "clean", [{"law_name": "测试法", "version_date": "2020-01-01",
                                             "source_url": LOCAL, "articles": articles(
                                                 ("1", "第一条 甲。"), ("2", "第二条 乙。"))}])
    report = build([leaky, clean], tmp_path / "out")
    entry = report["documents"][0]
    assert entry["canonicalization_reason"] == "boundary_preferred_source"
    # The leak-free copy wins even though its host is lower in the authority order.
    assert entry["canonical_source"] == clean
    assert report["structural_leaks"] == {}


def test_substantive_conflict_blocks_build(tmp_path):
    a = write_corpus(tmp_path, "a", [{"law_name": "测试法", "version_date": "2020-01-01",
                                     "source_url": NPC, "articles": articles(("1", "第一条 甲。"))}])
    b = write_corpus(tmp_path, "b", [{"law_name": "测试法", "version_date": "2020-01-01",
                                     "source_url": LOCAL, "articles": articles(("1", "第一条 完全不同。"))}])
    with pytest.raises(ValueError, match="substantive conflicts require manual review"):
        build([a, b], tmp_path / "out")
    assert not (tmp_path / "out").exists()
    with pytest.raises(ValueError, match="Substantive conflict"):
        select([{"source_dir": a, "articles": {}, "document": {}, "entry": {}}] * 2, "substantive_conflict")


def test_unique_publications_are_preserved_and_loadable(tmp_path):
    one = write_corpus(tmp_path, "one", [{"law_name": "甲法", "version_date": "2020-01-01",
                                         "source_url": NPC, "articles": articles(("1", "第一条 甲。"))}])
    two = write_corpus(tmp_path, "two", [{"law_name": "乙法", "version_date": "2021-01-01",
                                         "source_url": NPC, "articles": articles(("1", "第一条 乙。"))}])
    report = build([one, two], tmp_path / "out")
    assert report["publication_count"] == 2 and report["duplicate_keys_merged"] == 0
    assert {entry["canonicalization_reason"] for entry in report["documents"]} == {"unique_publication"}
    assert len(LegalCorpus(tmp_path / "out").documents) == 2


def test_canonical_output_has_zero_duplicates_and_documents_are_unique(tmp_path):
    texts = [("1", "第一条 甲。"), ("2", "第二条 乙。")]
    npc = write_corpus(tmp_path, "npc", [{"law_name": "测试法", "version_date": "2020-01-01",
                                         "source_url": NPC, "articles": articles(*texts)}])
    dup = write_corpus(tmp_path, "dup", [{"law_name": "测试法", "version_date": "2020-01-01",
                                         "source_url": LOCAL, "articles": articles(*texts)}])
    build([npc, dup], tmp_path / "out")
    assert audit([tmp_path / "out"])["duplicate_keys"] == 0
    assert (tmp_path / "out" / "manifest.json").exists()


def test_build_is_deterministic_across_input_order(tmp_path):
    a = write_corpus(tmp_path, "aaa", [{"law_name": "测试法", "version_date": "2020-01-01",
                                       "source_url": None, "articles": articles(("1", "第一条 内容。"))}])
    b = write_corpus(tmp_path, "bbb", [{"law_name": "测试法", "version_date": "2020-01-01",
                                       "source_url": None, "articles": articles(("1", "第一条 内容。"))}])
    first = build([a, b], tmp_path / "o1")["documents"][0]["canonical_source"]
    second = build([b, a], tmp_path / "o2")["documents"][0]["canonical_source"]
    assert first == second == a  # tie broken on source dir, not input order


def test_rebuild_ignores_previous_canonical_output(tmp_path):
    src = write_corpus(tmp_path, "src", [{"law_name": "测试法", "version_date": "2020-01-01",
                                         "source_url": NPC, "articles": articles(("1", "第一条 内容。"))}])
    target = tmp_path / "canonical"
    build([src], target)
    # Rerunning with the canonical dir (and itself) among the inputs must be a no-op,
    # not a self-duplicate ingestion.
    again = build([src, str(target)], target)
    assert again["publication_count"] == 1 and again["duplicate_keys_merged"] == 0
    assert again["classification_counts"]["exact_duplicate"] == 0


def test_real_corpus_builds_clean_canonical(tmp_path):
    root = Path(__file__).resolve().parents[1] / "output/score85"
    directories = sorted(str(path) for path in root.glob("legal_corpus*") if (path / "manifest.json").exists())
    if len(directories) < 2:
        pytest.skip("frozen statute corpus not checked out")
    target = tmp_path / "canonical"
    assert build_main(["--root", str(root), "--output", str(target)]) == 0
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["publication_count"] == 71 and manifest["duplicate_keys_merged"] == 19
    assert audit([target])["duplicate_keys"] == 0
    docs = LegalCorpus(target).documents
    assert len(docs) == len({(doc["law_name"], doc["version_date"]) for doc in docs})
    assert REASON["boundary_difference"] == "boundary_preferred_source"
    civil = next(doc for doc in docs if doc["law_name"] == "中华人民共和国民法典")
    article = next(item for item in civil["articles"] if item["article_id"] == "1258")
    assert not any(line.strip().startswith("附") for line in article["text"].splitlines())
    from app.benchmark_retrieval import retrieve

    refs = {"1-1": "民法典第一千二百五十八条的内容是什么？",
            "3-2": "三个以上农民专业合作社出资设立联合社依据哪条法律？"}
    for task, question in refs.items():
        assert retrieve(task, question, [str(target)])["mode"] != "skipped"
