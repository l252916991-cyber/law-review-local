"""PostgreSQL backend: SQL translation, sqlite3-compatible semantics, schema parity, search parity.

The translation tests need no server. The rest run when
``LAW_REVIEW_TEST_POSTGRES_URL`` points at a PostgreSQL server with pgvector
(CI starts one); without it they are skipped, and the SQLite suite is unchanged.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

from app.storage.db_postgres import Row, _python_value, identity_tables, translate

ADMIN_URL = os.environ.get("LAW_REVIEW_TEST_POSTGRES_URL")
needs_postgres = pytest.mark.skipif(not ADMIN_URL, reason="set LAW_REVIEW_TEST_POSTGRES_URL to a PostgreSQL server")


# --- translation (no server) -----------------------------------------------------

def test_placeholders_become_psycopg_and_percent_is_escaped_everywhere():
    statement = translate("SELECT * FROM pages WHERE text LIKE '%?%' AND id = ? AND name LIKE ?")
    assert statement.text == "SELECT * FROM pages WHERE text LIKE '%%?%%' AND id = %s AND name LIKE %s"


def test_insert_into_identity_table_returns_its_id():
    statement = translate("INSERT INTO cases(title, created_at, updated_at) VALUES (?, ?, ?)")
    assert statement.returning_id and statement.text.endswith("RETURNING id")
    assert statement.sync_identity is None
    assert "cases" in identity_tables() and "embedding_cache" not in identity_tables()


def test_explicit_id_moves_the_identity_sequence():
    assert translate("INSERT INTO cases(id, title, created_at, updated_at) VALUES (?,?,?,?)").sync_identity == "cases"


def test_insert_or_ignore_becomes_on_conflict_before_returning():
    statement = translate("INSERT OR IGNORE INTO gap_detections(case_id, gap_type) VALUES (?, ?)")
    assert "INSERT INTO gap_detections" in statement.text
    assert statement.text.index("ON CONFLICT DO NOTHING") < statement.text.index("RETURNING id")


def test_insert_or_replace_is_refused_rather_than_guessed():
    with pytest.raises(NotImplementedError):
        translate("INSERT OR REPLACE INTO cases(id, title) VALUES (?, ?)")


@pytest.mark.parametrize("sql,kind", [
    ("BEGIN IMMEDIATE", "writer_lock"), ("BEGIN", "noop"), ("PRAGMA foreign_keys = ON", "pragma"),
    ("PRAGMA table_info(cases)", "pragma"), ("PRAGMA user_version = 26", "pragma"),
])
def test_transaction_and_pragma_statements(sql, kind):
    assert translate(sql).kind == kind


def test_rows_read_like_sqlite_rows_and_values_keep_sqlite_types():
    row = Row((1, "标题"), {"id": 0, "title": 1})
    assert row[0] == row["id"] == 1 and dict(zip(row.keys(), row, strict=True)) == {"id": 1, "title": "标题"}
    assert _python_value(Decimal("12")) == 12 and isinstance(_python_value(Decimal("12")), int)
    assert _python_value(Decimal("1.5")) == 1.5
    assert _python_value(True) == 1 and type(_python_value(True)) is int


def test_a_sqlite_process_never_imports_the_postgresql_driver(tmp_path):
    """psycopg costs ~0.1 s to import; SQLite deployments must not pay it per process."""
    import subprocess
    import sys

    script = (
        "import sys, tempfile; from pathlib import Path\n"
        "from app.evaluation.eval_gate import _isolated_case\n"
        "from app.review.chat import chat\n"
        "with _isolated_case(Path(sys.argv[1]), endpoint='http://127.0.0.1:1/v1', model='none'):\n"
        "    chat(1, '涉案账户累计流入多少万元', 'test', None, False, False)\n"
        "assert 'psycopg' not in sys.modules, 'psycopg imported on the SQLite path'\n"
    )
    environment = {key: value for key, value in os.environ.items() if key != "LAW_REVIEW_DATABASE_URL"}
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=Path(__file__).resolve().parents[1],
                            env={**environment, "LAW_REVIEW_AUTH_MODE": "local"}, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]


def test_generated_postgres_schema_matches_the_current_sqlite_schema():
    from scripts.generate_postgres_schema import main

    assert main(["--check"]) == 0, "run scripts/generate_postgres_schema.py after a SQLite migration"


# --- live PostgreSQL ----------------------------------------------------------------

@pytest.fixture
def pg_url():
    from app.storage.db_postgres import create_database, drop_database, ensure_role
    from tests.support import POSTGRES_APP_ROLE

    ensure_role(ADMIN_URL, POSTGRES_APP_ROLE)
    name = f"lexvault_pg_{os.getpid()}_{uuid.uuid4().hex[:12]}"
    url = create_database(ADMIN_URL, name, owner=POSTGRES_APP_ROLE)
    try:
        yield url
    finally:
        drop_database(ADMIN_URL, name)


@pytest.fixture
def pg_db(pg_url):
    from app.storage.db import init_db, postgres_scope

    with tempfile.TemporaryDirectory() as directory, \
            pytest.MonkeyPatch.context() as patcher, postgres_scope(pg_url):
        patcher.setenv("LAW_REVIEW_DATA_DIR", directory)
        init_db(seed=False)
        yield pg_url


@needs_postgres
def test_postgres_tables_and_columns_equal_the_sqlite_schema(pg_db):
    from app.storage.db import connect, db_scope, init_db, table_names

    with closing(connect()) as conn:
        pg = {name: {row["name"] for row in conn.execute(f"PRAGMA table_info({name})")}
              for name in table_names(conn)}
    with tempfile.TemporaryDirectory() as directory, db_scope(Path(directory) / "law_review.db"):
        init_db(seed=False)
        with closing(connect()) as conn:
            lite = {name: {row["name"] for row in conn.execute(f"PRAGMA table_info({name})")}
                    for name in table_names(conn) if not name.startswith(("sqlite_", "pages_fts"))}
    assert set(lite) <= set(pg)
    for name, columns in lite.items():
        extra = pg[name] - columns
        assert columns <= pg[name] and extra <= {"embedding"}, name
    assert set(pg) - set(lite) == {"lexvault_schema_meta", "pages_fts"}


@needs_postgres
def test_failed_statement_leaves_the_transaction_usable_like_sqlite(pg_db):
    from app.storage.db import connect, now

    with closing(connect()) as conn:
        case_id = conn.execute("INSERT INTO cases(title, created_at, updated_at) VALUES ('保留', ?, ?)",
                               (now(), now())).lastrowid
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO cases(id, title, created_at, updated_at) VALUES (?, '重复', ?, ?)",
                         (case_id, now(), now()))
        later = conn.execute("INSERT INTO cases(title, created_at, updated_at) VALUES ('之后', ?, ?)",
                             (now(), now())).lastrowid
        conn.commit()
        titles = [row["title"] for row in conn.execute("SELECT title FROM cases ORDER BY id")]
    assert titles == ["保留", "之后"] and later == case_id + 1


@needs_postgres
def test_explicit_ids_do_not_collide_with_later_inserts(pg_db):
    from app.storage.db import now, transaction

    with transaction() as conn:
        conn.execute("INSERT INTO cases(id, title, created_at, updated_at) VALUES (40, '指定', ?, ?)", (now(), now()))
        assert conn.execute("INSERT INTO cases(title, created_at, updated_at) VALUES ('自动', ?, ?)",
                            (now(), now())).lastrowid == 41


@needs_postgres
def test_begin_immediate_serializes_writers_across_connections(pg_db):
    import psycopg

    from app.storage.db import connect
    from app.storage.db_postgres import WRITER_LOCK_KEY

    holder = connect()
    try:
        holder.execute("BEGIN IMMEDIATE")
        with psycopg.connect(pg_db, autocommit=True) as other:
            assert other.execute("SELECT pg_try_advisory_lock(%s)", (WRITER_LOCK_KEY,)).fetchone()[0] is False
            holder.commit()
            assert other.execute("SELECT pg_try_advisory_lock(%s)", (WRITER_LOCK_KEY,)).fetchone()[0] is True
    finally:
        holder.close()


@needs_postgres
def test_full_text_index_follows_page_and_document_changes(pg_db):
    from app.retrieval.search_backend import fts_page_candidates
    from app.storage.db import connect, now, transaction

    with transaction() as conn:
        case_id = conn.execute("INSERT INTO cases(title, created_at, updated_at) VALUES ('检索', ?, ?)",
                               (now(), now())).lastrowid
        document_id = conn.execute(
            "INSERT INTO documents(case_id, name, mime_type, pages, created_at, updated_at) "
            "VALUES (?, '借款合同.txt', 'text/plain', 1, ?, ?)", (case_id, now(), now())).lastrowid
        page_id = conn.execute("INSERT INTO pages(document_id, page_no, text) VALUES (?, 1, '约定：借款 十万元')",
                               (document_id,)).lastrowid

    def hits(term: str) -> list[int]:
        with closing(connect()) as conn:
            return [row["page_id"] for row in fts_page_candidates(conn, [term], case_id, 10)]

    assert hits("借款") == [page_id]
    with transaction() as conn:
        conn.execute("UPDATE pages SET text = '约定：还款 十万元' WHERE id = ?", (page_id,))
        conn.execute("UPDATE documents SET name = '还款协议.txt' WHERE id = ?", (document_id,))
    assert hits("借款") == [] and hits("还款") == [page_id]
    with closing(connect()) as conn:
        assert conn.execute("SELECT document_name FROM pages_fts WHERE page_id = ?", (page_id,)).fetchone()[0] == "还款协议.txt"
    with transaction() as conn:
        conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))
    assert hits("还款") == []


@needs_postgres
def test_text_parameters_drop_nul_and_booleans_store_as_integers(pg_db):
    from app.storage.db import connect, now

    with closing(connect()) as conn:
        case_id = conn.execute("INSERT INTO cases(title, created_at, updated_at) VALUES (?, ?, ?)",
                               ("测试\x00案件", now(), now())).lastrowid
        assert conn.execute("SELECT title FROM cases WHERE id = ?", (case_id,)).fetchone()[0] == "测试案件"
        assert conn.execute("SELECT ? AS flag", (True,)).fetchone()["flag"] == 1


@needs_postgres
def test_vector_search_returns_the_same_pages_and_scores_on_both_backends():
    from app.evaluation.eval_gate import _isolated_case
    from app.retrieval.rag import HybridRetriever

    questions = ["涉案账户累计流入多少万元", "谁负责客户招揽", "宣传材料由谁审批"]
    results = {}
    for backend, admin in (("sqlite", None), ("postgresql", ADMIN_URL)):
        with tempfile.TemporaryDirectory() as directory, _isolated_case(
                Path(directory), endpoint="http://127.0.0.1:1/v1", model="none", postgres_admin_url=admin):
            retriever = HybridRetriever(1, False)
            results[backend] = [[(item["page_id"], item["vector_score"]) for item in retriever.vector_search(q, 8)]
                                for q in questions]
    for lite, pg in zip(results["sqlite"], results["postgresql"], strict=True):
        assert [page for page, _ in lite] == [page for page, _ in pg]
        assert all(abs(a - b) < 1e-5 for (_, a), (_, b) in zip(lite, pg, strict=True))
        assert lite, "the demo case must produce vector candidates"


@needs_postgres
def test_answer_quality_gate_passes_on_postgresql(tmp_path):
    import json

    from app.evaluation.eval_gate import DEFAULT_BASELINES, compare, run

    report = run("rules", workdir=tmp_path, postgres_admin_url=ADMIN_URL)
    assert report["manifest"]["database_backend"] == "postgresql"
    verdict = compare(report, json.loads(DEFAULT_BASELINES["rules"].read_text(encoding="utf-8")))
    assert verdict["passed"], verdict["failures"]
    assert verdict["regressions"] == []


@needs_postgres
def test_sqlite_file_snapshots_are_refused_on_postgresql(pg_db):
    from app.integration.host_ports import capture_case_rows
    from app.integration.runtime_ports import UnsupportedCapabilityError
    from app.storage.db import SQLITE_FILE_ONLY

    with pytest.raises(UnsupportedCapabilityError, match=SQLITE_FILE_ONLY):
        capture_case_rows(1)


def test_isolated_harnesses_never_inherit_a_configured_postgresql(monkeypatch, tmp_path):
    from app.storage.db import backend_name
    from scripts.validate_application_acceptance import isolated_environment

    monkeypatch.setenv("LAW_REVIEW_DATABASE_URL", "postgresql://example.invalid/real_cases")
    assert backend_name() == "postgresql"
    with isolated_environment(tmp_path, "http://127.0.0.1:1/v1", "none", ""):
        assert backend_name() == "sqlite"
    assert backend_name() == "postgresql"


@needs_postgres
def test_postgresql_schemas_that_cannot_be_upgraded_in_place_are_refused(pg_db):
    """Older versions with migration steps upgrade (tests/test_postgres_migrations.py);
    one older than the migration baseline, or newer than the code, is refused."""
    import psycopg

    from app.storage.db import SCHEMA_VERSION, init_db
    from app.storage.db_postgres import MIGRATION_BASELINE

    with psycopg.connect(pg_db, autocommit=True) as conn:
        conn.execute("UPDATE lexvault_schema_meta SET version = %s", (MIGRATION_BASELINE - 1,))
    with pytest.raises(RuntimeError, match="predates"):
        init_db(seed=False)
    with psycopg.connect(pg_db, autocommit=True) as conn:
        conn.execute("UPDATE lexvault_schema_meta SET version = %s", (SCHEMA_VERSION + 1,))
    with pytest.raises(RuntimeError, match="refusing downgrade"):
        init_db(seed=False)
