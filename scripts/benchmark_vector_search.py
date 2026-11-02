"""Benchmark page-vector search: SQLite + Python scan versus PostgreSQL + pgvector.

Builds one synthetic case of ``--pages`` pages (deterministic text, the app's
384-dimension hashed embedding) in a temporary SQLite file and in a temporary
PostgreSQL database, then times ``search_backend.scored_page_vectors`` -- the
exact function retrieval calls -- on both. With ``--cases`` > 1 it also builds an
HNSW index over all cases and measures recall of approximate cross-case search
against the exact answer.

    uv run --locked python scripts/benchmark_vector_search.py \\
        --postgres "host=127.0.0.1 port=5432 user=postgres dbname=postgres" \\
        --pages 10000 --cases 5 --rls --output output/benchmarks/vector-search.json

Synthetic text and hashed vectors measure the storage and scoring path only;
they say nothing about retrieval quality with a real embedding model.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VOCABULARY = (
    "借款 合同 利息 担保 还款 违约 转账 账户 流水 证人 笔录 签字 印章 收据 欠条 判决 上诉 管辖 时效 鉴定 "
    "股权 董事 会议 决议 工资 加班 解除 赔偿 社保 工伤 专利 商标 许可 侵权 货款 发票 交付 验收 质量 租赁"
).split()
BACKEND, MODEL = "hashed-local", "benchmark-hashed-384"


def page_text(rng: random.Random) -> str:
    return "，".join("".join(rng.sample(VOCABULARY, 3)) for _ in range(12)) + "。"


def build_rows(cases: int, pages: int, seed: int) -> list[tuple[int, int, int, str, list[float]]]:
    from app.rag import hashed_embedding

    rng = random.Random(seed)
    rows = []
    page_id = 0
    for case in range(1, cases + 1):
        for index in range(pages):
            page_id += 1
            text = page_text(rng)
            rows.append((case, page_id, index // 100 + 1 + (case - 1) * 1000, text, hashed_embedding(text)))
    return rows


def load(conn: Any, rows: list[tuple[int, int, int, str, list[float]]], *, postgres: bool) -> None:
    from app.db import now

    stamp = now()
    cases = sorted({row[0] for row in rows})
    documents = sorted({(row[0], row[2]) for row in rows})
    if postgres:
        raw = conn._conn  # bulk COPY; the wrapper's per-statement savepoints are for application writes
        with raw.cursor() as cursor:
            for case in cases:
                cursor.execute("INSERT INTO cases(id, title, created_at, updated_at) VALUES (%s, %s, %s, %s)",
                               (case, f"基准案件{case}", stamp, stamp))
            with cursor.copy("COPY documents(id, case_id, name, mime_type, pages, created_at, updated_at) FROM STDIN") as copy:
                for case, document in documents:
                    copy.write_row((document, case, f"卷宗{document}.txt", "text/plain", 100, stamp, stamp))
            with cursor.copy("COPY pages(id, document_id, page_no, text) FROM STDIN") as copy:
                for _, page_id, document, text, _ in rows:
                    copy.write_row((page_id, document, (page_id - 1) % 100 + 1, text))
            with cursor.copy("COPY embedding_cache(page_id, model, dimensions, content_hash, vector_json, backend, updated_at) FROM STDIN") as copy:
                for _, page_id, _, _, vector in rows:
                    copy.write_row((page_id, MODEL, len(vector), "", json.dumps(vector), BACKEND, stamp))
            for table in ("cases", "documents", "pages"):
                cursor.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT MAX(id) FROM {table}))")
            cursor.execute("ANALYZE")
        raw.commit()
        return
    conn.executemany("INSERT INTO cases(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                     [(case, f"基准案件{case}", stamp, stamp) for case in cases])
    conn.executemany("INSERT INTO documents(id, case_id, name, mime_type, pages, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                     [(document, case, f"卷宗{document}.txt", "text/plain", 100, stamp, stamp) for case, document in documents])
    conn.executemany("INSERT INTO pages(id, document_id, page_no, text) VALUES (?, ?, ?, ?)",
                     [(page_id, document, (page_id - 1) % 100 + 1, text) for _, page_id, document, text, _ in rows])
    conn.executemany("INSERT INTO embedding_cache(page_id, model, dimensions, content_hash, vector_json, backend, updated_at) VALUES (?,?,?,?,?,?,?)",
                     [(page_id, MODEL, len(vector), "", json.dumps(vector), BACKEND, stamp) for _, page_id, _, _, vector in rows])
    conn.commit()


def queries(count: int, seed: int) -> list[list[float]]:
    from app.rag import hashed_embedding

    rng = random.Random(seed + 1)
    return [hashed_embedding("".join(rng.sample(VOCABULARY, 4))) for _ in range(count)]


def time_search(conn: Any, vectors: list[list[float]], *, limit: int) -> tuple[list[float], list[list[int]]]:
    from app.search_backend import scored_page_vectors

    timings, results = [], []
    for vector in vectors:
        started = time.perf_counter()
        scored, _ = scored_page_vectors(conn, case_id=1, query_vector=vector, backend=BACKEND, model=MODEL,
                                        required_terms=None, limit=limit)
        timings.append((time.perf_counter() - started) * 1000)
        results.append([item["page_id"] for _, item in scored])
    return timings, results


def summary(timings: list[float]) -> dict[str, float]:
    ordered = sorted(timings)
    return {"p50_ms": round(statistics.median(ordered), 2),
            "p95_ms": round(ordered[max(0, int(0.95 * len(ordered)) - 1)], 2),
            "max_ms": round(ordered[-1], 2)}


def hnsw_experiment(conn: Any, vectors: list[list[float]], *, limit: int, ef_values: tuple[int, ...]) -> dict[str, Any]:
    raw = conn._conn
    started = time.perf_counter()
    raw.execute("SET maintenance_work_mem = '512MB'")
    raw.execute("CREATE INDEX embedding_cache_hnsw_384 ON embedding_cache USING hnsw "
                "((embedding::vector(384)) vector_cosine_ops) WHERE dimensions = 384")
    raw.commit()
    build_seconds = round(time.perf_counter() - started, 1)
    query = ("SELECT page_id FROM embedding_cache WHERE dimensions = 384 "
             "ORDER BY embedding::vector(384) <=> %s::vector(384) LIMIT %s")

    def run(settings: list[str]) -> tuple[list[float], list[list[int]]]:
        timings, results = [], []
        for vector in vectors:
            with raw.transaction():
                for setting in settings:
                    raw.execute(setting)
                started = time.perf_counter()
                rows = raw.execute(query, (json.dumps(vector), limit)).fetchall()
                timings.append((time.perf_counter() - started) * 1000)
            results.append([row[0] for row in rows])
        return timings, results

    exact_timings, exact = run(["SET LOCAL enable_indexscan = off"])
    hnsw: list[dict[str, Any]] = []
    report: dict[str, Any] = {"index_build_seconds": build_seconds, "exact_cross_case": summary(exact_timings), "hnsw": hnsw}
    for ef in ef_values:
        timings, approx = run([f"SET LOCAL hnsw.ef_search = {ef}"])
        recall = statistics.mean(len(set(a) & set(e)) / len(e) for a, e in zip(approx, exact, strict=True))
        hnsw.append({"ef_search": ef, **summary(timings), "recall_at_k": round(recall, 4)})
    return report


def rls_experiment(admin_url: str, rows: list[tuple[int, int, int, str, list[float]]], vectors: list[list[float]],
                   *, limit: int, baseline: list[list[int]]) -> dict[str, Any]:
    """The same search as a non-superuser application role, with an outsider identity and walls in place."""
    from app.db import connect, init_db, now, postgres_scope, system_access, transaction
    from app.db_postgres import PgConnection, _with_database, create_database, drop_database, ensure_role
    from app.security import Principal, _principal  # importing security registers the identity provider

    role = "lexvault_app"
    ensure_role(admin_url, role)
    name = f"lexvault_bench_rls_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    url = create_database(admin_url, name, owner=role)
    try:
        with postgres_scope(url):
            init_db(seed=False)
            # COPY is refused under row-level security; bulk loading is an
            # administrator's job, searching is the application's.
            with closing(PgConnection(_with_database(admin_url, name))) as admin:
                load(admin, rows, postgres=True)
            stamp = now()
            with system_access(), transaction() as conn:
                walled = sorted({row[0] for row in rows})[1:]
                if not walled:
                    walled = [conn.execute("INSERT INTO cases(title, created_at, updated_at) VALUES ('设墙案件', ?, ?)",
                                           (stamp, stamp)).lastrowid]
                for case_id in walled:
                    conn.execute("INSERT INTO case_walls(case_id, reason, created_by, created_at, updated_at) "
                                 "VALUES (?, '基准', 'benchmark', ?, ?)", (case_id, stamp, stamp))
                    conn.execute("INSERT INTO case_wall_insiders(case_id, principal_name, added_by, added_at) "
                                 "VALUES (?, '其他律师', 'benchmark', ?)", (case_id, stamp))
            measured = {}
            # An outsider has every other case hidden; an insider has nothing
            # hidden, which is what most requests look like.
            for label, who in (("outsider", "基准律师"), ("insider", "其他律师")):
                token = _principal.set(Principal(who, False, (1,)))
                try:
                    with closing(connect()) as conn:
                        time_search(conn, vectors[:3], limit=limit)
                        measured[label] = time_search(conn, vectors, limit=limit)
                finally:
                    _principal.reset(token)
        return {"walled_cases": len(walled),
                "postgresql_pgvector_exact_with_rls": summary(measured["outsider"][0]),
                "postgresql_pgvector_exact_with_rls_nothing_hidden": summary(measured["insider"][0]),
                "identical_to_superuser": {label: sum(a == b for a, b in zip(results, baseline, strict=True))
                                           for label, (_, results) in measured.items()}}
    finally:
        drop_database(admin_url, name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--postgres", required=True, metavar="ADMIN_URL")
    parser.add_argument("--pages", type=int, default=10_000, help="pages per case")
    parser.add_argument("--cases", type=int, default=1)
    parser.add_argument("--queries", type=int, default=30)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20261102)
    parser.add_argument("--rls", action="store_true",
                        help="also run as a non-superuser role with an outsider identity and walls in place")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    from app.db import connect, db_scope, init_db, postgres_scope
    from app.db_postgres import create_database, drop_database

    rows = build_rows(args.cases, args.pages, args.seed)
    vectors = queries(args.queries, args.seed)
    report: dict[str, Any] = {"pages_per_case": args.pages, "cases": args.cases, "dimensions": 384,
                    "queries": args.queries, "limit": args.limit, "embedding": "hashed-local (synthetic)"}
    with tempfile.TemporaryDirectory(prefix="lexvault-vector-bench-") as directory:
        os.environ["LAW_REVIEW_DATA_DIR"] = directory
        with db_scope(Path(directory) / "law_review.db"):
            init_db(seed=False)
            with closing(connect()) as conn:
                load(conn, rows, postgres=False)
                time_search(conn, vectors[:3], limit=args.limit)  # warm the page cache
                lite_timings, lite = time_search(conn, vectors, limit=args.limit)
        name = f"lexvault_bench_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        url = create_database(args.postgres, name)
        try:
            with postgres_scope(url):
                init_db(seed=False)
                with closing(connect()) as conn:
                    load(conn, rows, postgres=True)
                    time_search(conn, vectors[:3], limit=args.limit)
                    pg_timings, pg = time_search(conn, vectors, limit=args.limit)
                    overlap = statistics.mean(len(set(a) & set(b)) / max(1, len(a)) for a, b in zip(lite, pg, strict=True))
                    report["within_case"] = {
                        "sqlite_python_scan": summary(lite_timings),
                        "postgresql_pgvector_exact": summary(pg_timings),
                        "top_k_overlap": round(overlap, 4),
                        "identical_order": sum(a == b for a, b in zip(lite, pg, strict=True)),
                    }
                    if args.cases > 1:
                        report["cross_case"] = hnsw_experiment(conn, vectors, limit=10, ef_values=(40, 100, 200))
                    report["postgresql"] = conn.execute(
                        "SELECT split_part(version(), ' ', 2) AS server, "
                        "(SELECT extversion FROM pg_extension WHERE extname = 'vector') AS pgvector").fetchone()
                    report["postgresql"] = dict(zip(report["postgresql"].keys(), report["postgresql"], strict=True))
        finally:
            drop_database(args.postgres, name)
        if args.rls:
            report["row_level_security"] = rls_experiment(args.postgres, rows, vectors, limit=args.limit, baseline=pg)
    report["sqlite"] = sqlite3.sqlite_version
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
