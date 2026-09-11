import pytest

from app.db import db_scope, init_db, now, transaction
from app.rag import HybridRetriever
from app.rag_chunks import page_chunks


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

    parents = {1: repeated_page, 2: second_page}
    assert hits
    assert metrics["embedding"]["backend"] == "hashed-local"
    assert metrics["reranker"]["enabled"] is False
    assert metrics["child_count"] > len(parents)
    assert len({hit["page_id"] for hit in hits}) == len(hits)
    assert all(hit["document_id"] == first_document for hit in hits)
    assert all(leaked_page not in hit["text"] for hit in hits)
    for hit in hits:
        parent = parents[hit["page_no"]]
        assert hit["text"] == parent
        assert hit["quote"] == parent[hit["char_start"]:hit["char_end"]]
