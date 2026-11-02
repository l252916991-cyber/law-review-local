"""Offline fixtures are synthetic and never require redistributing LawBench."""
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_corpus_regression(root, contract):
    """Verify the exact reviewed inputs before any real-corpus assertion runs."""
    from app.legal_corpus import LegalCorpus

    directories = []
    for item in contract["sources"]:
        directory = (root / item["path"]).resolve()
        if not directory.is_relative_to(root.resolve()):
            raise ValueError("Frozen corpus path escapes asset root")
        manifest_path = directory / "manifest.json"
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != item["manifest_sha256"]:
            raise ValueError("Frozen corpus manifest identity mismatch")
        corpus = LegalCorpus(directory)
        if len(corpus.documents) != item["document_count"]:
            raise ValueError("Frozen corpus document count mismatch")
        for entry in corpus.manifest["documents"]:
            raw_path = (directory / entry["raw_file"]).resolve()
            if raw_path.parent != directory:
                raise ValueError("Frozen raw publication escapes corpus directory")
            if hashlib.sha256(raw_path.read_bytes()).hexdigest() != entry["raw_sha256"]:
                raise ValueError("Frozen raw publication digest mismatch")
        directories.append(str(directory))
    return {"directories": directories, "expected": contract["expected"]}


@pytest.fixture(scope="session")
def frozen_corpus_regression():
    configured = os.environ.get("LAW_REVIEW_CORPUS_REGRESSION_ROOT")
    if configured is None:
        return None  # Preserve the original legacy-asset assertions unchanged.
    contract_path = Path(__file__).resolve().parents[1] / "docs/benchmarks/collaboration-corpus-freeze-20261028.json"
    return _load_corpus_regression(Path(configured), json.loads(contract_path.read_text()))


@pytest.fixture
def corpus_regression_loader():
    return _load_corpus_regression


@pytest.fixture(scope="session", autouse=True)
def synthetic_lawbench(tmp_path_factory):
    from app import lawbench
    from app.benchmark_metrics import task_label_space

    directory = tmp_path_factory.mktemp("synthetic-lawbench")
    references = {
        "1-2": ["正确答案：A。", "正确答案：B。"],
        "2-2": ["争议焦点类别：责任认定。", "争议焦点类别：合同效力。"],
        "2-3": ["类别:婚后有子女、支付抚养费。", "类别:不动产分割。"],
        "2-4": ["婚姻家庭", "劳动纠纷"],
        "2-8": ["[正确答案]A<eoa>", "[正确答案]B<eoa>"],
        "2-9": ["支付/给付;获利", "搜查/扣押"],
        "3-1": ["法条:刑法第264条"],
        "3-3": ["罪名:盗窃;诈骗", "罪名:过失损坏广播电视设施、公用电信设施"],
        "3-4": ["刑期:4个月"], "3-5": ["刑期:12个月"],
        "3-6": ["正确答案:A。"], "3-7": ["犯罪金额:1000元"],
    }
    for task in lawbench.TASK_NAMES:
        options = references.get(task, ["合成参考答案"])
        rows = [{"instruction": "这是工程测试的合成题。直接输出指定格式。",
                 "question": f"合成问题 {task}/{index}：用于测试装载、抽样及协议，不评估法律知识。",
                 "answer": options[index % len(options)]} for index in range(500)]
        (directory / f"{task}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    lawbench.load_task.cache_clear()
    task_label_space.cache_clear()
    with patch.object(lawbench, "LAW_BENCH_DIR", directory):
        yield
    lawbench.load_task.cache_clear()
    task_label_space.cache_clear()


@pytest.fixture(autouse=True)
def postgres_database_per_test(request):
    """With LAW_REVIEW_TEST_POSTGRES_URL, every test gets an empty PostgreSQL database.

    Tests that pick their own SQLite file (``db_scope``, a patched ``DB_PATH``)
    keep it: an explicit file scope wins over the URL. Classes built on
    ``IsolatedDatabaseTestCase`` manage one database per class instead, and a
    module or class that sets ``SQLITE_ONLY`` / ``sqlite_only`` is about the
    SQLite file itself and runs on SQLite.
    """
    admin_url = os.environ.get("LAW_REVIEW_TEST_POSTGRES_URL")
    from tests.support import IsolatedDatabaseTestCase

    if (not admin_url or getattr(request.module, "SQLITE_ONLY", "")
            or request.node.get_closest_marker("sqlite_only") is not None
            or (request.cls is not None and issubclass(request.cls, IsolatedDatabaseTestCase))):
        yield
        return
    import uuid

    from app.db_postgres import create_database, drop_database, ensure_role
    from tests.support import POSTGRES_APP_ROLE

    ensure_role(admin_url, POSTGRES_APP_ROLE)
    name = f"lexvault_f_{os.getpid()}_{uuid.uuid4().hex[:12]}"
    url = create_database(admin_url, name, owner=POSTGRES_APP_ROLE)
    try:
        with patch.dict(os.environ, {"LAW_REVIEW_DATABASE_URL": url}):
            yield
    finally:
        drop_database(admin_url, name)


_POSTGRES_TESTS: set[str] = set()
_ALL_TESTS: set[str] = set()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if not os.environ.get("LAW_REVIEW_TEST_POSTGRES_URL"):
        yield
        return
    from app import db_postgres

    before = db_postgres.connections_opened
    yield
    _ALL_TESTS.add(item.nodeid)
    if db_postgres.connections_opened > before:
        _POSTGRES_TESTS.add(item.nodeid)


def pytest_terminal_summary(terminalreporter):
    if not os.environ.get("LAW_REVIEW_TEST_POSTGRES_URL"):
        return
    line = (f"PostgreSQL: {len(_POSTGRES_TESTS)} of {len(_ALL_TESTS)} executed tests opened a "
            "PostgreSQL connection; the rest did not touch a database or are marked sqlite_only")
    terminalreporter.write_line(line)
    report = os.environ.get("LAW_REVIEW_TEST_POSTGRES_REPORT")
    if report:
        Path(report).parent.mkdir(parents=True, exist_ok=True)
        Path(report).write_text(json.dumps({"postgres_tests": sorted(_POSTGRES_TESTS),
                                            "executed_tests": len(_ALL_TESTS)}, indent=1), encoding="utf-8")
