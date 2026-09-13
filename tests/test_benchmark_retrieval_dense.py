from __future__ import annotations

import hashlib
import json

import pytest

from app import benchmark_retrieval
from app.benchmark_retrieval import DENSE_INDEX_ENV, retrieve
from app.legal_corpus import SCHEMA_VERSION, LegalCorpus, split_articles
from app.statutory_index import IndexSpec, StatutoryIndex, build_validity

MODEL = "fake-dense-v1"


def write_corpus(tmp_path, laws):
    entries = []
    for document_id, law_name, aliases, text in laws:
        doc = {"schema_version": SCHEMA_VERSION, "document_id": document_id, "law_name": law_name,
               "aliases": aliases, "version_date": "2020-01-01", "effective_date": None,
               "version_status": "test_fixture", "source_url": f"https://example.invalid/{document_id}",
               "articles": split_articles(text)}
        raw = json.dumps(doc, ensure_ascii=False).encode()
        (tmp_path / f"{document_id}.json").write_bytes(raw)
        entries.append({"document_file": f"{document_id}.json", "document_sha256": hashlib.sha256(raw).hexdigest()})
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": SCHEMA_VERSION, "documents": entries}))
    return str(tmp_path)


def make_index(corpus_dir, vectors_by_id):
    corpus = LegalCorpus(corpus_dir)
    spec = IndexSpec(MODEL, 2, "omlx")
    vectors = {f"{doc['document_id']}/{a['article_id']}": vec
               for doc in corpus.documents for a, vec in vectors_by_id.items()
               if a["doc"] == doc["document_id"]}
    # vectors_by_id maps {("doc-id", article_text_snippet): vector}; build keys directly instead.
    return corpus, spec, vectors


class FakeEmbedder:
    model = MODEL
    last_failure = None
    backend = "omlx"

    def __init__(self, vector, *, fail=False):
        self.vector = vector
        self.fail = fail

    def embed(self, texts):
        if self.fail:
            self.last_failure = "boom"
            raise RuntimeError("embedding_model_unavailable:boom")
        self.last_failure = None
        return [list(self.vector) for _ in texts], self.backend


@pytest.fixture
def corpus(tmp_path):
    return write_corpus(tmp_path, [
        ("labor-2020", "中华人民共和国劳动合同法", ["中华人民共和国劳动合同法", "劳动合同法"],
         "第一条 劳动报酬支付义务。\n第二条 合同履行约定。"),
        ("civil-2020", "中华人民共和国民法典", ["中华人民共和国民法典", "民法典"],
         "第一条 保护民事权益。"),
    ])


def save_index(corpus_dir, path, mapping):
    """mapping: {"article_snippet": [vx, vy]}; every other article gets a neutral vector."""
    corpus = LegalCorpus(corpus_dir)
    vectors = {}
    for doc in corpus.documents:
        for article in doc["articles"]:
            vector = [0.0, 1.0]
            for snippet, mapped in mapping.items():
                if snippet in article["text"]:
                    vector = mapped
            vectors[f"{doc['document_id']}/{article['article_id']}"] = vector
    spec = IndexSpec(MODEL, 2, "omlx")
    index = StatutoryIndex(corpus, spec, vectors=vectors, validity=build_validity(corpus))
    index.save(path)
    return path


def test_env_unset_keeps_lexical(corpus, tmp_path, monkeypatch):
    save_index(corpus, tmp_path / "index.json", {"劳动报酬": [1.0, 0.0]})
    monkeypatch.delenv(DENSE_INDEX_ENV, raising=False)
    result = retrieve("3-2", "劳动合同法的劳动报酬规定", [corpus])
    assert result["ranker"] == "lexical"
    assert result["hits"][0]["article_id"] == "1"


def test_dense_ranking_overrides_lexical_order(corpus, tmp_path, monkeypatch):
    # Lexically 合同履行 (article 2) matches the query best; the dense vector pulls
    # article 1 (劳动报酬) to the top instead.
    path = save_index(corpus, tmp_path / "index.json", {
        "劳动报酬": [1.0, 0.0], "合同履行": [0.0, 1.0]})
    monkeypatch.setenv(DENSE_INDEX_ENV, str(path))
    benchmark_retrieval._index_cache = None
    result = retrieve("3-2", "合同履行约定", [corpus], embedder=FakeEmbedder([1.0, 0.0]))
    assert result["ranker"] == "dense"
    assert result["hits"][0]["article_id"] == "1"
    assert result["hits"][0]["document_id"] == "labor-2020"
    hit = result["hits"][0]
    for field in ("text", "law_name", "version_date", "source_url", "document_id", "article_id"):
        assert field in hit


def test_explicit_lexical_ignores_inherited_dense_environment(corpus, tmp_path, monkeypatch):
    path = save_index(corpus, tmp_path / "index.json", {
        "劳动报酬": [1.0, 0.0], "合同履行": [0.0, 1.0]})
    monkeypatch.setenv(DENSE_INDEX_ENV, str(path))
    benchmark_retrieval._index_cache = None
    result = retrieve("3-2", "合同履行约定", [corpus],
                      embedder=FakeEmbedder([1.0, 0.0]), ranker_policy="lexical")
    assert result["ranker_policy"] == "lexical"
    assert result["ranker"] == "lexical"
    assert result["hits"][0]["article_id"] == "2"


def test_embedding_failure_falls_back_to_lexical(corpus, tmp_path, monkeypatch):
    path = save_index(corpus, tmp_path / "index.json", {"劳动报酬": [1.0, 0.0]})
    monkeypatch.setenv(DENSE_INDEX_ENV, str(path))
    benchmark_retrieval._index_cache = None
    result = retrieve("3-2", "合同履行约定", [corpus], embedder=FakeEmbedder([1.0, 0.0], fail=True))
    assert result["ranker"] == "lexical"
    assert result["hits"][0]["article_id"] == "2"


def test_index_corpus_mismatch_falls_back(corpus, tmp_path, monkeypatch):
    # Saved for a different corpus snapshot: load() must reject it via fingerprint.
    path = save_index(corpus, tmp_path / "index.json", {"劳动报酬": [1.0, 0.0]})
    tampered = json.loads((tmp_path / "tampered-corpus.json").read_text()) if False else None
    # Re-save the corpus with one article changed so the fingerprint no longer matches.
    doc_path = tmp_path / "labor-2020.json"
    doc = json.loads(doc_path.read_text())
    doc["articles"][0]["text"] = "第一条 劳动报酬支付义务被修改。"
    raw = json.dumps(doc, ensure_ascii=False).encode()
    doc_path.write_bytes(raw)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for entry in manifest["documents"]:
        if entry["document_file"] == "labor-2020.json":
            entry["document_sha256"] = hashlib.sha256(raw).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setenv(DENSE_INDEX_ENV, str(path))
    benchmark_retrieval._index_cache = None
    result = retrieve("3-2", "合同履行约定", [corpus], embedder=FakeEmbedder([1.0, 0.0]))
    assert result["ranker"] == "lexical" and result["hits"]


def test_multi_directory_corpus_stays_lexical(corpus, tmp_path, monkeypatch):
    (tmp_path / "second").mkdir()
    other = write_corpus(tmp_path / "second", [
        ("other-2020", "其他法", ["其他法"], "第一条 无关内容。")])
    path = save_index(corpus, tmp_path / "index.json", {"劳动报酬": [1.0, 0.0]})
    monkeypatch.setenv(DENSE_INDEX_ENV, str(path))
    benchmark_retrieval._index_cache = None
    result = retrieve("3-2", "合同履行约定", [corpus, other], embedder=FakeEmbedder([1.0, 0.0]))
    assert result["ranker"] == "lexical"


def test_model_mismatch_falls_back(corpus, tmp_path, monkeypatch):
    path = save_index(corpus, tmp_path / "index.json", {"劳动报酬": [1.0, 0.0]})
    monkeypatch.setenv(DENSE_INDEX_ENV, str(path))
    benchmark_retrieval._index_cache = None
    embedder = FakeEmbedder([1.0, 0.0])
    embedder.model = "another-model"
    result = retrieve("3-2", "合同履行约定", [corpus], embedder=embedder)
    assert result["ranker"] == "lexical"


def test_one_one_exact_path_ignores_dense(corpus, tmp_path, monkeypatch):
    path = save_index(corpus, tmp_path / "index.json", {"劳动报酬": [1.0, 0.0]})
    monkeypatch.setenv(DENSE_INDEX_ENV, str(path))
    benchmark_retrieval._index_cache = None
    result = retrieve("1-1", "劳动合同法第一条的内容是什么？", [corpus], embedder=FakeEmbedder([1.0, 0.0]))
    assert result["mode"] == "exact_article" and result["hits"]
    assert "ranker" not in result
