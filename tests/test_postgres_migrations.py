"""PostgreSQL schema upgrades: the migration steps alone must rebuild the current schema."""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest

from app.db import SCHEMA_VERSION
from app.db_postgres import (
    MIGRATION_BASELINE,
    MIGRATIONS_DIR,
    apply_schema,
    create_database,
    drop_database,
    ensure_role,
    migration_file,
    schema_fingerprint,
)
from tests.support import POSTGRES_APP_ROLE

ADMIN_URL = os.environ.get("LAW_REVIEW_TEST_POSTGRES_URL")
BASELINE = MIGRATIONS_DIR / f"baseline_v{MIGRATION_BASELINE}.sql"
needs_postgres = pytest.mark.skipif(not ADMIN_URL, reason="set LAW_REVIEW_TEST_POSTGRES_URL to a PostgreSQL server")


def test_every_version_after_the_baseline_has_a_migration_step():
    assert BASELINE.exists()
    assert [version for version in range(MIGRATION_BASELINE + 1, SCHEMA_VERSION + 1)
            if not migration_file(version).exists()] == []
    # No step beyond the code's version: it would never run.
    assert not migration_file(SCHEMA_VERSION + 1).exists()


@pytest.fixture
def databases() -> Iterator[list[str]]:
    assert ADMIN_URL
    ensure_role(ADMIN_URL, POSTGRES_APP_ROLE)
    created: list[str] = []
    urls: list[str] = []

    def make() -> str:
        name = f"lexvault_mig_{os.getpid()}_{uuid.uuid4().hex[:10]}"
        created.append(name)
        url = create_database(ADMIN_URL, name, owner=POSTGRES_APP_ROLE)
        urls.append(url)
        return url

    yield make  # type: ignore[misc]
    for name in created:
        drop_database(ADMIN_URL, name)


def at_baseline(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(BASELINE.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO lexvault_schema_meta(singleton, version) VALUES (1, %s)", (MIGRATION_BASELINE,))


def describe(left: dict, right: dict) -> str:
    lines = []
    for kind in sorted(set(left) | set(right)):
        only_left = sorted(set(left.get(kind, [])) - set(right.get(kind, [])))
        only_right = sorted(set(right.get(kind, [])) - set(left.get(kind, [])))
        lines += [f"{kind} only after migration: {row}" for row in only_left[:5]]
        lines += [f"{kind} only in a new database: {row}" for row in only_right[:5]]
    return "\n".join(lines)


@needs_postgres
def test_migration_steps_alone_rebuild_the_current_schema(databases):
    upgraded, fresh = databases(), databases()
    at_baseline(upgraded)
    with psycopg.connect(upgraded, autocommit=True) as conn:
        for version in range(MIGRATION_BASELINE + 1, SCHEMA_VERSION + 1):
            conn.execute(migration_file(version).read_text(encoding="utf-8"))
    apply_schema(fresh, SCHEMA_VERSION)
    left, right = schema_fingerprint(upgraded), schema_fingerprint(fresh)
    assert left == right, describe(left, right)


@needs_postgres
def test_startup_upgrades_an_old_database_in_place_and_keeps_its_rows(databases):
    url = databases()
    at_baseline(url)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("INSERT INTO cases(id, title, status, created_at, updated_at) "
                     "VALUES (7, '迁移前的案件', '进行中', '2026-11-01 00:00:00', '2026-11-01 00:00:00')")
    apply_schema(url, SCHEMA_VERSION)
    apply_schema(url, SCHEMA_VERSION)  # a second start is a no-op
    with psycopg.connect(url) as conn:
        assert conn.execute("SELECT version FROM lexvault_schema_meta").fetchone() == (SCHEMA_VERSION,)
        assert conn.execute("SELECT title FROM cases WHERE id = 7").fetchone() == ("迁移前的案件",)


@needs_postgres
def test_unsupported_upgrades_are_refused_before_touching_the_database(databases):
    url = databases()
    at_baseline(url)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("UPDATE lexvault_schema_meta SET version = %s", (MIGRATION_BASELINE - 1,))
    with pytest.raises(RuntimeError, match="predates"):
        apply_schema(url, SCHEMA_VERSION)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("UPDATE lexvault_schema_meta SET version = %s", (SCHEMA_VERSION,))
    with pytest.raises(RuntimeError, match="migration missing"):
        apply_schema(url, SCHEMA_VERSION + 1)
    with pytest.raises(RuntimeError, match="newer"):
        apply_schema(url, SCHEMA_VERSION - 1)
    with psycopg.connect(url) as conn:
        assert conn.execute("SELECT version FROM lexvault_schema_meta").fetchone() == (SCHEMA_VERSION,)


@needs_postgres
def test_the_comparison_notices_what_a_forgotten_migration_would_leave_out(databases):
    upgraded, fresh = databases(), databases()
    at_baseline(upgraded)
    apply_schema(fresh, SCHEMA_VERSION)
    with psycopg.connect(upgraded, autocommit=True) as conn:
        for version in range(MIGRATION_BASELINE + 1, SCHEMA_VERSION + 1):
            conn.execute(migration_file(version).read_text(encoding="utf-8"))
        conn.execute("ALTER TABLE evidence DROP COLUMN updated_at")
        conn.execute("DROP POLICY lexvault_wall ON pages")
        conn.execute("DROP INDEX idx_pages_document")
    difference = describe(schema_fingerprint(upgraded), schema_fingerprint(fresh))
    assert "'evidence', 'updated_at'" in difference
    assert "'pages', 'lexvault_wall'" in difference
    assert "idx_pages_document" in difference
