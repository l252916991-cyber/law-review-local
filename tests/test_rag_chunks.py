import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from app import rag_child_index
from app.db import connect, db_scope, init_db, now, transaction
from app.rag import HybridRetriever, embedding_identity, hashed_embedding
from app.rag_chunks import page_chunks


class FakeEmbeddingClient:
    prefer_remote = False

    def __init__(self, *, model="test-model", backend="test-backend", dimensions=12, on_call=None, fail_on=None):
        self.model = model
        self.backend = backend
        self.dimensions = dimensions
        self.on_call = on_call
        self.fail_on = fail_on
        self.calls = []
        self.last_failure = None

    def embed(self, texts):
        self.calls.append(list(texts))
        call_number = len(self.calls)
        if self.on_call is not None:
            self.on_call(call_number, texts)
        if call_number == self.fail_on:
            raise RuntimeError("injected embedding failure")
        return [hashed_embedding(text, self.dimensions) for text in texts], self.backend


def _create_case(database, texts):
    with db_scope(database):
        init_db(seed=False)
        with transaction() as conn:
            case_id = conn.execute(
                "INSERT INTO cases(title,case_no,created_at,updated_at) VALUES (?,?,?,?)",
                ("子块缓存测试", "CHILD-CACHE", now(), now()),
            ).lastrowid
            document_id = conn.execute(
                "INSERT INTO documents(case_id,name,created_at,updated_at) VALUES (?,?,?,?)",
                (case_id, "缓存材料.txt", now(), now()),
            ).lastrowid
            page_ids = []
            for page_no, text in enumerate(texts, 1):
                page_ids.append(conn.execute(
                    "INSERT INTO pages(document_id,page_no,text) VALUES (?,?,?)",
                    (document_id, page_no, text),
                ).lastrowid)
    return int(case_id), int(document_id), [int(page_id) for page_id in page_ids]


def _retriever(case_id, client, *, pipeline="exact-scan", profile="sentence-400"):
    retriever = HybridRetriever(
        case_id,
        prefer_remote_embeddings=False,
        use_neural_reranker=False,
        use_page_children=True,
        child_pipeline=pipeline,
        child_chunk_profile=profile,
    )
    retriever.embedding_client = client
    return retriever


def test_chunks_preserve_source_offsets_and_cover_long_page():
    text = "  第一段证据。\n" * 120 + "末尾关键记录"
    chunks = page_chunks(text)
    assert len(chunks) > 2
    covered = set()
    for child in chunks:
        start, end = child["char_start"], child["char_end"]
        assert child["text"] == text[start:end]
        assert 0 < end - start <= 400
        covered.update(range(start, end))
    assert covered == set(range(len(text)))
    assert chunks[-1]["char_end"] == len(text)


def test_empty_and_invalid_chunk_parameters():
    assert page_chunks("") == []
    assert page_chunks(" \n ") == []
    with pytest.raises(ValueError):
        page_chunks("材料", size=10, overlap=10)


def test_page_children_preserve_quotes_parents_case_scope_and_unique_pages(tmp_path):
    database = tmp_path / "children.sqlite"
    repeated_page = (
        "仓库先核对运输单和封签，随后按区域记录货物外观。冷链校验标记出现在交接表第一部分，用于确认记录来源。\n\n"
        + "巡检人员逐项检查温度探头、备用电源和车门状态，并把每项结果写入独立段落。" * 18
        + "\n\n冷链校验标记在页面后部再次出现，用于复核同一页多个子块能否聚合为唯一父页。"
    )
    second_page = (
        "收货人员读取记录仪并检查托盘。冷链校验标记对应的复核表由收货主管签字，"
        "该页与前页属于同一案件但页码不同。"
    )
    leaked_page = "另一案件也写有冷链校验标记，但显式案件范围不得返回这段隔离材料。"

    with db_scope(database):
        init_db(seed=False)
        with transaction() as conn:
            first_case = conn.execute(
                "INSERT INTO cases(title,case_no,created_at,updated_at) VALUES (?,?,?,?)",
                ("子块案件甲", "CHILD-A", now(), now()),
            ).lastrowid
            second_case = conn.execute(
                "INSERT INTO cases(title,case_no,created_at,updated_at) VALUES (?,?,?,?)",
                ("子块案件乙", "CHILD-B", now(), now()),
            ).lastrowid
            assert first_case is not None and second_case is not None
            first_document = conn.execute(
                "INSERT INTO documents(case_id,name,created_at,updated_at) VALUES (?,?,?,?)",
                (first_case, "甲案记录.txt", now(), now()),
            ).lastrowid
            second_document = conn.execute(
                "INSERT INTO documents(case_id,name,created_at,updated_at) VALUES (?,?,?,?)",
                (second_case, "乙案记录.txt", now(), now()),
            ).lastrowid
            assert first_document is not None and second_document is not None
            conn.executemany(
                "INSERT INTO pages(document_id,page_no,text) VALUES (?,?,?)",
                [
                    (first_document, 1, repeated_page),
                    (first_document, 2, second_page),
                    (second_document, 1, leaked_page),
                ],
            )

        retriever = HybridRetriever(
            first_case,
            prefer_remote_embeddings=False,
            use_neural_reranker=False,
            use_page_children=True,
        )
        hits, metrics = retriever.retrieve("冷链校验标记", limit=5)
        with closing(connect()) as conn:
            indexed_page_ids = {
                row[0] for row in conn.execute("SELECT page_id FROM page_child_index_state")
            }
            case_page_ids = {
                row[0] for row in conn.execute(
                    "SELECT p.id FROM pages p JOIN documents d ON d.id=p.document_id WHERE d.case_id=?",
                    (first_case,),
                )
            }

    parents = {1: repeated_page, 2: second_page}
    assert hits
    assert metrics["embedding"]["backend"] == "hashed-local"
    assert metrics["reranker"]["enabled"] is False
    assert metrics["child_count"] > len(parents)
    assert len({hit["page_id"] for hit in hits}) == len(hits)
    assert all(hit["document_id"] == first_document for hit in hits)
    assert indexed_page_ids == case_page_ids
    assert all(leaked_page not in hit["text"] for hit in hits)
    for hit in hits:
        parent = parents[hit["page_no"]]
        assert hit["text"] == parent
        assert hit["quote"] == parent[hit["char_start"]:hit["char_end"]]


def test_whole_page_profile_emits_one_child_per_page():
    text = "整页材料。" * 100
    chunks = page_chunks(text, None, 0)

    assert chunks == [{"char_start": 0, "char_end": len(text), "text": text}]
    assert page_chunks("   \n ", None, 0) == []
    with pytest.raises(ValueError):
        page_chunks(text, None, 10)


def test_unified_child_pipeline_quotes_matched_window_but_returns_whole_page(tmp_path):
    database = tmp_path / "unified.sqlite"
    filler = "例行台账仅登记设备编号与外观状态。\n\n"
    gold = "值班技师在末尾段落确认关闭二号机组，登记原因为回油管渗漏。"
    text = filler * 20 + gold
    case_id, _, _ = _create_case(database, [text])
    with db_scope(database):
        hits, metrics = _retriever(
            case_id, FakeEmbeddingClient(), pipeline="unified"
        ).retrieve("二号机组 关闭 回油管", limit=1)

    assert hits
    hit = hits[0]
    assert metrics["child_retrieval"]["used"] is True
    assert metrics["child_retrieval"]["pipeline"] == "unified"
    # The point of the unified path: the quote is the matched child window, which is
    # narrower than the page and lands on the gold sentence rather than the first
    # filler mention of the query nouns.
    assert hit["text"] == text
    assert hit["quote"] == text[hit["char_start"]:hit["char_end"]]
    assert len(hit["quote"]) < len(text)
    assert gold in hit["quote"]


def test_cold_build_and_reopened_retriever_reuses_all_child_vectors(tmp_path, monkeypatch):
    database = tmp_path / "warm.sqlite"
    text = "运输记录逐项核对。" * 80 + "唯一冷链标记位于长页末尾。"
    case_id, _, _ = _create_case(database, [text])

    cold_client = FakeEmbeddingClient()
    with db_scope(database):
        cold_hits, cold = _retriever(case_id, cold_client).retrieve("唯一冷链标记", limit=3)
    assert cold["index_storage"] == "sqlite_persistent"
    assert cold["index_search"] == "exact_scan"
    assert cold["embedded_child_count"] == cold["child_count"] > 1
    assert sum(len(call) for call in cold_client.calls[1:]) == cold["child_count"]

    def unexpected_split(_text, *_args):
        raise AssertionError("warm child cache must not split parent text")

    monkeypatch.setattr(rag_child_index, "page_chunks", unexpected_split)
    warm_client = FakeEmbeddingClient()
    with db_scope(database):
        warm_hits, warm = _retriever(case_id, warm_client).retrieve("唯一冷链标记", limit=3)
    assert warm_hits == cold_hits
    assert warm_client.calls == [["唯一冷链标记"]]
    assert warm["embedded_child_count"] == 0
    assert warm["reused_child_count"] == warm["child_count"]
    assert warm["reused_page_count"] == 1


def test_only_changed_page_is_rechunked_and_reembedded(tmp_path):
    database = tmp_path / "changed.sqlite"
    case_id, _, page_ids = _create_case(database, ["甲页含有核验标记。", "乙页原始记录。"])
    with db_scope(database):
        _retriever(case_id, FakeEmbeddingClient()).retrieve("核验标记", limit=2)
        with transaction() as conn:
            conn.execute("UPDATE pages SET text=? WHERE id=?", ("乙页修改后也含有核验标记。", page_ids[1]))
        client = FakeEmbeddingClient()
        hits, metrics = _retriever(case_id, client).retrieve("核验标记", limit=2)

    assert client.calls[0] == ["核验标记"]
    assert sum(len(call) for call in client.calls[1:]) == 1
    assert metrics["rebuilt_page_count"] == 1
    assert metrics["reused_page_count"] == 1
    assert metrics["embedded_child_count"] == 1
    assert all(hit["quote"] == hit["text"][hit["char_start"]:hit["char_end"]] for hit in hits)


def test_space_identity_and_chunk_version_changes_rebuild(tmp_path, monkeypatch):
    database = tmp_path / "identity.sqlite"
    case_id, _, _ = _create_case(database, ["模型空间身份核验标记。"])

    def run(client):
        with db_scope(database):
            _, metrics = _retriever(case_id, client).retrieve("身份核验标记", limit=1)
        assert metrics["embedded_child_count"] == 1
        assert len(client.calls) == 2

    monkeypatch.setenv("LAW_REVIEW_EMBEDDING_REVISION", "revision-a")
    run(FakeEmbeddingClient())
    monkeypatch.setenv("LAW_REVIEW_EMBEDDING_REVISION", "revision-b")
    run(FakeEmbeddingClient())
    run(FakeEmbeddingClient(model="other-model"))
    run(FakeEmbeddingClient(model="other-model", backend="other-backend"))
    run(FakeEmbeddingClient(model="other-model", backend="other-backend", dimensions=9))
    monkeypatch.setattr(rag_child_index, "CHUNK_VERSION", "test-chunk-version-2")
    run(FakeEmbeddingClient(model="other-model", backend="other-backend", dimensions=9))


def test_corrupt_cached_vector_rebuilds_its_page(tmp_path):
    database = tmp_path / "corrupt.sqlite"
    case_id, _, page_ids = _create_case(database, ["损坏向量核验标记。"])
    with db_scope(database):
        _retriever(case_id, FakeEmbeddingClient()).retrieve("损坏向量", limit=1)
        with transaction() as conn:
            conn.execute(
                "UPDATE page_child_chunks SET vector_json='[]' WHERE page_id=? AND ordinal=0",
                (page_ids[0],),
            )
        client = FakeEmbeddingClient()
        _, metrics = _retriever(case_id, client).retrieve("损坏向量", limit=1)
    assert len(client.calls) == 2
    assert metrics["rebuilt_page_count"] == 1
    assert metrics["embedded_child_count"] == 1


def test_page_and_document_delete_cascade_child_index(tmp_path):
    database = tmp_path / "cascade.sqlite"
    case_id, document_id, page_ids = _create_case(database, ["第一页索引。", "第二页索引。"])
    with db_scope(database):
        _retriever(case_id, FakeEmbeddingClient()).retrieve("索引", limit=2)
        with transaction() as conn:
            conn.execute("DELETE FROM pages WHERE id=?", (page_ids[0],))
            assert conn.execute(
                "SELECT COUNT(*) FROM page_child_index_state WHERE page_id=?", (page_ids[0],)
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM page_child_chunks WHERE page_id=?", (page_ids[0],)
            ).fetchone()[0] == 0
            conn.execute("DELETE FROM documents WHERE id=?", (document_id,))
            assert conn.execute("SELECT COUNT(*) FROM page_child_index_state").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM page_child_chunks").fetchone()[0] == 0


def test_embedding_failure_does_not_publish_partial_index(tmp_path):
    database = tmp_path / "embedding-failure.sqlite"
    text = "".join(f"第{index}段待嵌入材料" + "甲" * 360 + "。\n" for index in range(36))
    case_id, _, _ = _create_case(database, [text])
    with db_scope(database):
        client = FakeEmbeddingClient(fail_on=3)
        with pytest.raises(RuntimeError, match="injected embedding failure"):
            _retriever(case_id, client).retrieve("待嵌入", limit=1)
        with closing(connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM page_child_index_state").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM page_child_chunks").fetchone()[0] == 0
    assert len(client.calls) == 3


def test_concurrent_cold_builds_publish_one_unique_child_set(tmp_path):
    database = tmp_path / "concurrent-build.sqlite"
    case_id, _, page_ids = _create_case(database, ["并发索引材料。" * 100])
    barrier = threading.Barrier(2)

    def build_once():
        client = FakeEmbeddingClient(on_call=lambda call, _texts: barrier.wait() if call == 2 else None)
        with db_scope(database):
            return _retriever(case_id, client).retrieve("并发索引", limit=1)[1]

    with ThreadPoolExecutor(max_workers=2) as pool:
        metrics = list(pool.map(lambda _index: build_once(), range(2)))
    with db_scope(database), closing(connect()) as conn:
        state_count = conn.execute(
            "SELECT child_count FROM page_child_index_state WHERE page_id=?", (page_ids[0],)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT ordinal FROM page_child_chunks WHERE page_id=? ORDER BY ordinal", (page_ids[0],)
        ).fetchall()
    assert all(item["embedded_child_count"] == state_count for item in metrics)
    assert [row[0] for row in rows] == list(range(state_count))


@pytest.mark.parametrize("action", ["edit", "delete", "renumber", "move_case"])
def test_parent_edit_or_delete_during_embedding_refuses_publish(tmp_path, action):
    database = tmp_path / f"race-{action}.sqlite"
    case_id, document_id, page_ids = _create_case(database, ["嵌入期间保持不变的父页。"])
    other_case_id = None
    if action == "move_case":
        with db_scope(database), transaction() as conn:
            other_case_id = conn.execute(
                "INSERT INTO cases(title,case_no,created_at,updated_at) VALUES (?,?,?,?)",
                ("其他案件", "OTHER", now(), now()),
            ).lastrowid

    def mutate(call_number, _texts):
        if call_number != 2:
            return
        with transaction() as conn:
            if action == "edit":
                conn.execute("UPDATE pages SET text='嵌入期间已修改' WHERE id=?", (page_ids[0],))
            elif action == "delete":
                conn.execute("DELETE FROM pages WHERE id=?", (page_ids[0],))
            elif action == "renumber":
                conn.execute("UPDATE pages SET page_no=2 WHERE id=?", (page_ids[0],))
            else:
                conn.execute("UPDATE documents SET case_id=? WHERE id=?", (other_case_id, document_id))

    with db_scope(database):
        with pytest.raises(RuntimeError, match="page_changed_during_child_index_build"):
            _retriever(case_id, FakeEmbeddingClient(on_call=mutate)).retrieve("父页", limit=1)
        with closing(connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM page_child_index_state").fetchone()[0] == 0


def test_cold_build_rechecks_hot_parent_before_publish(tmp_path):
    database = tmp_path / "hot-parent-race.sqlite"
    case_id, document_id, page_ids = _create_case(database, ["热页原始内容。"])
    with db_scope(database):
        _retriever(case_id, FakeEmbeddingClient()).retrieve("热页", limit=1)
        with transaction() as conn:
            cold_page_id = conn.execute(
                "INSERT INTO pages(document_id,page_no,text) VALUES (?,?,?)",
                (document_id, 2, "新增冷页内容。"),
            ).lastrowid

        def edit_hot_parent(call_number, _texts):
            if call_number == 2:
                with transaction() as conn:
                    conn.execute("UPDATE pages SET text='热页已并发修改。' WHERE id=?", (page_ids[0],))

        with pytest.raises(RuntimeError, match="page_changed_during_child_index_build"):
            _retriever(case_id, FakeEmbeddingClient(on_call=edit_hot_parent)).retrieve("内容", limit=2)
        with closing(connect()) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM page_child_index_state WHERE page_id=?", (cold_page_id,)
            ).fetchone()[0] == 0


def test_state_and_vectors_are_read_from_one_sqlite_snapshot(tmp_path, monkeypatch):
    database = tmp_path / "snapshot.sqlite"
    case_id, _, page_ids = _create_case(database, ["一致快照向量。"])
    monkeypatch.setenv("LAW_REVIEW_EMBEDDING_REVISION", "snapshot-a")
    with db_scope(database):
        _retriever(case_id, FakeEmbeddingClient()).retrieve("一致快照", limit=1)
        with closing(connect()) as conn:
            old_vector = json.loads(conn.execute(
                "SELECT vector_json FROM page_child_chunks WHERE page_id=?", (page_ids[0],)
            ).fetchone()[0])

        def replace_cache():
            with transaction() as conn:
                conn.execute(
                    "UPDATE page_child_index_state SET model_identity='snapshot-b' WHERE page_id=?",
                    (page_ids[0],),
                )
                conn.execute(
                    "UPDATE page_child_chunks SET vector_json=? WHERE page_id=?",
                    (json.dumps([0.0] * 11 + [1.0]), page_ids[0]),
                )

        original_hash = rag_child_index._content_hash
        replaced = False

        def hash_after_concurrent_replace(text):
            nonlocal replaced
            if not replaced:
                replaced = True
                replace_cache()
            return original_hash(text)

        monkeypatch.setattr(rag_child_index, "_content_hash", hash_after_concurrent_replace)

        def unexpected_embed(_texts):
            raise AssertionError("consistent warm snapshot must not rebuild")

        children, metrics = rag_child_index.load_or_build_child_index(
            case_id,
            backend="test-backend",
            dimensions=12,
            model_identity=embedding_identity("test-model", "test-backend"),
            embed=unexpected_embed,
        )
    assert children[0]["vector"] == old_vector
    assert metrics["reused_child_count"] == 1


def test_blank_page_state_is_cached_without_child_embedding(tmp_path):
    database = tmp_path / "blank.sqlite"
    case_id, _, page_ids = _create_case(database, [" \n "])
    with db_scope(database):
        cold_client = FakeEmbeddingClient()
        hits, cold = _retriever(case_id, cold_client).retrieve("任意查询", limit=1)
        assert hits == []
        assert cold_client.calls == [["任意查询"]]
        assert cold["rebuilt_page_count"] == 1
        assert cold["embedded_child_count"] == 0
        with closing(connect()) as conn:
            state = conn.execute(
                "SELECT child_count FROM page_child_index_state WHERE page_id=?", (page_ids[0],)
            ).fetchone()
        assert state[0] == 0

        warm_client = FakeEmbeddingClient()
        _, warm = _retriever(case_id, warm_client).retrieve("任意查询", limit=1)
    assert warm_client.calls == [["任意查询"]]
    assert warm["reused_page_count"] == 1


def test_child_scan_budget_exceeded_falls_back_to_page_level(tmp_path, monkeypatch):
    database = tmp_path / "budget.sqlite"
    fresh_database = tmp_path / "budget-fresh.sqlite"
    case_id, _, _ = _create_case(database, ["甲乙丙丁"])
    fresh_case_id, _, _ = _create_case(fresh_database, ["甲乙丙丁"])
    monkeypatch.setattr(rag_child_index, "MAX_CHILDREN", 2)
    monkeypatch.setattr(rag_child_index, "page_chunks", lambda _text, *_args: [
        {"char_start": 0, "char_end": 2, "text": "甲乙"},
        {"char_start": 2, "char_end": 4, "text": "丙丁"},
    ])
    with db_scope(database):
        _, warm = _retriever(case_id, FakeEmbeddingClient()).retrieve("甲乙", limit=1)
        assert warm["child_count"] == 2

        # The cached index now exceeds the budget, so the request must degrade to
        # the page-level pipeline instead of failing.
        monkeypatch.setattr(rag_child_index, "MAX_CHILDREN", 1)
        cached_hits, cached_metrics = _retriever(case_id, FakeEmbeddingClient()).retrieve("甲乙", limit=1)

    # A freshly built index that cannot fit the budget degrades the same way.
    with db_scope(fresh_database):
        _, fresh_metrics = _retriever(fresh_case_id, FakeEmbeddingClient()).retrieve("甲乙", limit=1)

    assert cached_hits and cached_hits[0]["page_no"] == 1
    assert cached_metrics["retrieval_mode"] == "hybrid_rrf"
    assert cached_metrics["child_retrieval"] == {
        "requested": True, "used": False, "fallback_reason": "child_scan_budget_exceeded",
    }
    assert fresh_metrics["retrieval_mode"] == "hybrid_rrf"
    assert fresh_metrics["child_retrieval"]["fallback_reason"] == "child_scan_budget_exceeded"


def test_exceeded_child_budget_keeps_named_valueerror_contract(tmp_path, monkeypatch):
    """Existing guards that catch ValueError still see the budget failure."""
    database = tmp_path / "budget-type.sqlite"
    case_id, _, _ = _create_case(database, ["甲乙丙丁"])
    monkeypatch.setattr(rag_child_index, "MAX_CHILDREN", 1)
    monkeypatch.setattr(rag_child_index, "page_chunks", lambda _text, *_args: [
        {"char_start": 0, "char_end": 2, "text": "甲乙"},
        {"char_start": 2, "char_end": 4, "text": "丙丁"},
    ])
    with db_scope(database):
        client = FakeEmbeddingClient()
        with pytest.raises(ValueError, match="exceeds 1 chunks"):
            rag_child_index.load_or_build_child_index(
                case_id,
                backend=client.backend,
                dimensions=client.dimensions,
                model_identity=embedding_identity(client.model, client.backend),
                embed=client.embed,
            )


def test_exceeded_child_budget_keeps_named_valueerror_contract(tmp_path, monkeypatch):
    """Existing guards that catch ValueError still see the budget failure."""
    database = tmp_path / "budget-type.sqlite"
    case_id, _, _ = _create_case(database, ["甲乙丙丁"])
    monkeypatch.setattr(rag_child_index, "MAX_CHILDREN", 1)
    monkeypatch.setattr(rag_child_index, "page_chunks", lambda _text, *_args: [
        {"char_start": 0, "char_end": 2, "text": "甲乙"},
        {"char_start": 2, "char_end": 4, "text": "丙丁"},
    ])
    with db_scope(database):
        client = FakeEmbeddingClient()
        with pytest.raises(ValueError, match="exceeds 1 chunks"):
            rag_child_index.load_or_build_child_index(
                case_id,
                backend=client.backend,
                dimensions=client.dimensions,
                model_identity=embedding_identity(client.model, client.backend),
                embed=client.embed,
            )


def test_invalid_child_query_or_limit_never_calls_embedding(tmp_path):
    database = tmp_path / "invalid.sqlite"
    case_id, _, _ = _create_case(database, ["参数校验。"])
    client = FakeEmbeddingClient()
    with db_scope(database):
        retriever = _retriever(case_id, client)
        with pytest.raises(ValueError, match="query must be text"):
            retriever.retrieve(123, limit=1)
        with pytest.raises(ValueError, match="Positive integer"):
            retriever.retrieve("参数", limit=0)
        assert retriever.retrieve("  ", limit=1)[0] == []
    assert client.calls == []
