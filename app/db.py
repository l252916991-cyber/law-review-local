from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("LAW_REVIEW_DATA_DIR", ROOT / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "law_review.db"
_INITIAL_DB_PATH = DB_PATH
_scoped_db: ContextVar[Path | None] = ContextVar("lexvault_db", default=None)


def get_db_path() -> Path:
    # Explicit patching remains supported for old migration tests. Runtime paths
    # are otherwise resolved at use time, not captured by module import order.
    if DB_PATH != _INITIAL_DB_PATH:
        return Path(DB_PATH)
    return _scoped_db.get() or Path(os.getenv("LAW_REVIEW_DATA_DIR", ROOT / "data")) / "law_review.db"


@contextmanager
def db_scope(path: str | Path):
    token = _scoped_db.set(Path(path))
    try:
        yield
    finally:
        _scoped_db.reset(token)


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_dirs() -> None:
    base = get_db_path().parent
    (base / "uploads").mkdir(parents=True, exist_ok=True)
    (base / "exports").mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(get_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def process_ownership():
    """Hold a local-filesystem Web owner lock before any startup recovery.

    The descriptor stays open for the entire lifespan; never unlink the lock
    file, which would let another process lock a different inode.
    """
    import fcntl

    ensure_dirs()
    path = get_db_path().resolve().with_suffix(".web.lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("This database already has a Web process owner; use one worker") from exc
        yield
    finally:
        os.close(descriptor)


def record_security_event(
    event_type: str,
    outcome: str,
    *,
    actor: str | None = None,
    case_id: int | None = None,
    detail: str = "",
    request_path: str = "",
) -> None:
    """Append one privacy-safe security event to the independent trail.

    Events deliberately have no foreign key to cases, so deleting a case
    cannot erase accountability records. A failed write is logged at error
    level and never blocks the operation it observes.
    """
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO security_events(event_type,outcome,actor,case_id,detail,request_path,created_at) VALUES (?,?,?,?,?,?,?)",
                (event_type, outcome, actor, case_id, detail, request_path, now()),
            )
    except Exception as exc:
        logging.getLogger(__name__).error("Security event write failed (%s)", type(exc).__name__)


@contextmanager
def transaction():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    case_no TEXT NOT NULL DEFAULT '',
    case_type TEXT NOT NULL DEFAULT '刑事',
    client_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '阅卷中',
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    stored_path TEXT NOT NULL DEFAULT '',
    import_key TEXT,
    mime_type TEXT NOT NULL DEFAULT 'text/plain',
    pages INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT '已索引',
    doc_type TEXT NOT NULL DEFAULT '其他材料',
    people TEXT NOT NULL DEFAULT '',
    date_range TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    UNIQUE(document_id, page_no)
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '书证',
    fact TEXT NOT NULL DEFAULT '',
    credibility TEXT NOT NULL DEFAULT '待核验',
    source_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    source_page_start INTEGER NOT NULL DEFAULT 1,
    source_page_end INTEGER NOT NULL DEFAULT 1,
    quote TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '待复核',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    from_evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    to_evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL DEFAULT '相互印证',
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    user_name TEXT NOT NULL DEFAULT '本机律师',
    title TEXT NOT NULL DEFAULT '新会话',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    citations_json TEXT NOT NULL DEFAULT '[]',
    route TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '内置知识库'
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER REFERENCES cases(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embedding_cache (
    page_id INTEGER PRIMARY KEY REFERENCES pages(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    backend TEXT NOT NULL DEFAULT 'local',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    route TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    retrieval_mode TEXT NOT NULL DEFAULT 'hybrid_rrf',
    final_answer TEXT NOT NULL DEFAULT '',
    citations_json TEXT NOT NULL DEFAULT '[]',
    total_ms INTEGER NOT NULL DEFAULT 0,
    runtime TEXT NOT NULL DEFAULT 'native',
    checkpoint_thread_id TEXT NOT NULL DEFAULT '',
    resume_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS agent_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    node_name TEXT NOT NULL,
    agent_role TEXT NOT NULL,
    status TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    user_name TEXT NOT NULL DEFAULT '本机律师',
    kind TEXT NOT NULL DEFAULT 'agent_conclusion',
    content TEXT NOT NULL,
    vector_json TEXT NOT NULL DEFAULT '[]',
    embedding_model TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.5,
    source_run_id INTEGER REFERENCES agent_runs(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rag_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    query TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    returned_json TEXT NOT NULL,
    recall_at_k REAL NOT NULL,
    mrr REAL NOT NULL,
    citation_coverage REAL NOT NULL,
    latency_ms INTEGER NOT NULL,
    evaluation_id TEXT NOT NULL DEFAULT '',
    dataset_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    page_id UNINDEXED,
    case_id UNINDEXED,
    document_id UNINDEXED,
    page_no UNINDEXED,
    document_name,
    doc_type,
    text,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS batch_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    total_files INTEGER NOT NULL,
    processed_files INTEGER NOT NULL DEFAULT 0,
    successful_files INTEGER NOT NULL DEFAULT 0,
    failed_files INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    error_log_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS batch_import_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batch_imports(id) ON DELETE CASCADE,
    file_key TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    status TEXT NOT NULL DEFAULT 'pending',
    document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(batch_id, file_key)
);

CREATE TABLE IF NOT EXISTS review_jobs (
    id TEXT PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    principal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    request_json TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    run_id INTEGER REFERENCES agent_runs(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS gap_detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    run_id INTEGER REFERENCES agent_runs(id) ON DELETE SET NULL,
    gap_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    description TEXT NOT NULL,
    suggestion TEXT NOT NULL,
    affected_evidence_ids TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT '待核验',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    user_name TEXT NOT NULL,
    annotation_type TEXT NOT NULL,
    content TEXT NOT NULL,
    quote_start INTEGER,
    quote_end INTEGER,
    status TEXT NOT NULL DEFAULT '待处理',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_case ON documents(case_id);
CREATE INDEX IF NOT EXISTS idx_pages_document ON pages(document_id, page_no);
CREATE INDEX IF NOT EXISTS idx_evidence_case ON evidence(case_id);
CREATE INDEX IF NOT EXISTS idx_conversations_case ON conversations(case_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_case ON agent_runs(case_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_steps(run_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_steps_run_node ON agent_steps(run_id, node_name);
CREATE INDEX IF NOT EXISTS idx_memories_case ON memories(case_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_source_run ON memories(source_run_id) WHERE source_run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_rag_evaluations_case ON rag_evaluations(case_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_batch_imports_case ON batch_imports(case_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_gap_detections_case ON gap_detections(case_id, severity);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gap_detections_run_item
ON gap_detections(run_id, gap_type, description) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_annotations_evidence ON evidence_annotations(evidence_id);
"""


SCHEMA_VERSION = 9

# Built-in case-closing export templates (E32). Block schema is owned by the
# renderer in services.py; this constant is the seeding source of truth.
BUILTIN_EXPORT_TEMPLATES = [
    {
        "name": "刑事阅卷结案包",
        "description": "完整阅卷归档：案件摘要、双层目录、证据清单、问答记录与原始卷宗。",
        "blocks": [
            {"type": "case_summary", "title": "案件摘要", "filename": "案件摘要.md", "required": False},
            {"type": "catalog_csv", "title": "内容级目录", "filename": "内容级目录.csv", "required": True},
            {"type": "evidence_table", "title": "证据目录", "filename": "证据目录.csv", "evidence_status": None, "required": True},
            {"type": "qa_log", "title": "阅卷问答记录", "filename": "阅卷问答记录.md", "required": False},
            {"type": "attachments", "title": "原始卷宗", "filename": "原始卷宗", "required": True},
        ],
    },
    {
        "name": "质证材料包",
        "description": "面向质证场景：案件摘要、全状态证据清单（状态列区分草稿与已确认）与问答记录。",
        "blocks": [
            {"type": "case_summary", "title": "案件摘要", "filename": "案件摘要.md", "required": False},
            {"type": "evidence_table", "title": "质证证据清单", "filename": "质证证据清单.csv", "evidence_status": None, "required": True},
            {"type": "qa_log", "title": "阅卷问答记录", "filename": "阅卷问答记录.md", "required": False},
            {"type": "attachments", "title": "原始卷宗", "filename": "原始卷宗", "required": False},
        ],
    },
]


def _execute_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute DDL without executescript's implicit transaction commit."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete migration SQL")


def _migrate_v1(conn: sqlite3.Connection) -> None:
    _execute_script(conn, SCHEMA)


def _migrate_v2(conn: sqlite3.Connection) -> None:
    # Legacy version 1 installations predate several auxiliary tables.
    _execute_script(conn, SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_runs)")}
    migrations = {
        "runtime": "ALTER TABLE agent_runs ADD COLUMN runtime TEXT NOT NULL DEFAULT 'native'",
        "checkpoint_thread_id": "ALTER TABLE agent_runs ADD COLUMN checkpoint_thread_id TEXT NOT NULL DEFAULT ''",
        "resume_count": "ALTER TABLE agent_runs ADD COLUMN resume_count INTEGER NOT NULL DEFAULT 0",
    }
    for column, statement in migrations.items():
        if column not in columns:
            conn.execute(statement)
    document_columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if "import_key" not in document_columns:
        conn.execute("ALTER TABLE documents ADD COLUMN import_key TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_import_key ON documents(import_key) WHERE import_key IS NOT NULL")
    evaluation_columns = {row[1] for row in conn.execute("PRAGMA table_info(rag_evaluations)")}
    for name in ("evaluation_id", "dataset_name"):
        if name not in evaluation_columns:
            conn.execute(f"ALTER TABLE rag_evaluations ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    _execute_script(conn, FTS_TRIGGERS)


def _migrate_v3(conn: sqlite3.Connection) -> None:
    # Durable dispatch outbox: a batch registered in SQLite but whose enqueue
    # outcome was never confirmed is re-discharged on the next startup.
    _execute_script(
        conn,
        """
CREATE TABLE IF NOT EXISTS batch_dispatch (
    batch_id INTEGER PRIMARY KEY REFERENCES batch_imports(id) ON DELETE CASCADE,
    case_id INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','discharged')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batch_dispatch_pending ON batch_dispatch(state, batch_id);
""",
    )


def _migrate_v4(conn: sqlite3.Connection) -> None:
    # Integrity and accountability fields: original-file hashes, approval
    # attribution, and a security event trail that survives case deletion
    # (no foreign key on purpose).
    _execute_script(
        conn,
        """
ALTER TABLE documents ADD COLUMN content_hash TEXT;
ALTER TABLE evidence ADD COLUMN approved_by TEXT;
ALTER TABLE evidence ADD COLUMN approved_at TEXT;
CREATE INDEX IF NOT EXISTS idx_documents_case_hash ON documents(case_id, content_hash);
CREATE TABLE IF NOT EXISTS security_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    actor TEXT,
    case_id INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    request_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_security_events_time ON security_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_security_events_type ON security_events(event_type, created_at DESC);
""",
    )


def _migrate_v5(conn: sqlite3.Connection) -> None:
    # Answer provenance (E23): identity of the inference setup behind each
    # assistant message, so any answer can be traced to its model and prompt.
    _execute_script(conn, "ALTER TABLE messages ADD COLUMN provenance_json TEXT NOT NULL DEFAULT '{}';")


def _migrate_v6(conn: sqlite3.Connection) -> None:
    # Template-driven case-closing exports (E32). Built-in templates are seeded
    # once and marked immutable; custom templates are managed via the API.
    _execute_script(
        conn,
        """
CREATE TABLE IF NOT EXISTS export_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    blocks_json TEXT NOT NULL,
    builtin INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1)),
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
""",
    )
    current = now()
    for template in BUILTIN_EXPORT_TEMPLATES:
        conn.execute(
            """INSERT INTO export_templates(name, description, blocks_json, builtin, created_by, created_at, updated_at)
               VALUES (?, ?, ?, 1, 'system', ?, ?)
               ON CONFLICT(name) DO NOTHING""",
            (template["name"], template["description"], json.dumps(template["blocks"], ensure_ascii=False), current, current),
        )


def _migrate_v7(conn: sqlite3.Connection) -> None:
    _execute_script(
        conn,
        """
CREATE TABLE IF NOT EXISTS auth_sessions (
    session_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('token', 'oidc')),
    credential_fingerprint TEXT,
    oidc_subject TEXT,
    expires_at INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (
        (kind = 'token' AND credential_fingerprint IS NOT NULL AND oidc_subject IS NULL)
        OR (kind = 'oidc' AND credential_fingerprint IS NULL AND oidc_subject IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at);
""",
    )


def purge_expired_sessions(conn: sqlite3.Connection, current_epoch: int | None = None) -> int:
    current_epoch = int(time.time()) if current_epoch is None else current_epoch
    return conn.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (current_epoch,)).rowcount


def _migrate_v8(conn: sqlite3.Connection) -> None:
    _execute_script(
        conn,
        """
CREATE TABLE IF NOT EXISTS bank_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    source_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    source_document_hash TEXT NOT NULL DEFAULT '',
    source_sheet TEXT NOT NULL DEFAULT '',
    source_row_number INTEGER NOT NULL,
    source_page_start INTEGER,
    source_page_end INTEGER,
    source_ref_json TEXT NOT NULL DEFAULT '{}',
    account TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT 'unknown' CHECK(direction IN ('inflow','outflow','unknown')),
    amount_minor INTEGER,
    currency TEXT NOT NULL DEFAULT 'CNY',
    amount_raw TEXT NOT NULL DEFAULT '',
    transaction_time TEXT,
    time_raw TEXT NOT NULL DEFAULT '',
    counterparty TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    raw_row_json TEXT NOT NULL DEFAULT '{}',
    parse_status TEXT NOT NULL DEFAULT 'parsed' CHECK(parse_status IN ('parsed','needs_review')),
    parse_warnings_json TEXT NOT NULL DEFAULT '[]',
    parser_version TEXT NOT NULL,
    row_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_document_id, source_sheet, source_row_number, row_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_bank_transactions_case_time ON bank_transactions(case_id, transaction_time);
CREATE INDEX IF NOT EXISTS idx_bank_transactions_case_account ON bank_transactions(case_id, account);
CREATE INDEX IF NOT EXISTS idx_bank_transactions_case_counterparty ON bank_transactions(case_id, counterparty);
CREATE TABLE IF NOT EXISTS bank_transaction_relations (
    transaction_id INTEGER NOT NULL REFERENCES bank_transactions(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL DEFAULT '资金链路',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY(transaction_id, evidence_id, relation_type)
);
""",
    )


def _migrate_v9(conn: sqlite3.Connection) -> None:
    _execute_script(
        conn,
        """
ALTER TABLE conversations ADD COLUMN archived_at TEXT;
ALTER TABLE conversations ADD COLUMN archive_expires_at INTEGER;
ALTER TABLE conversations ADD COLUMN archived_by TEXT;
CREATE INDEX IF NOT EXISTS idx_conversations_archive_expiry ON conversations(archive_expires_at);
""",
    )


CONVERSATION_ARCHIVE_RETENTION_SECONDS = 7 * 24 * 60 * 60


def purge_expired_conversations(conn: sqlite3.Connection, current_epoch: int | None = None) -> int:
    current_epoch = int(time.time()) if current_epoch is None else current_epoch
    return conn.execute(
        "DELETE FROM conversations WHERE archive_expires_at IS NOT NULL AND archive_expires_at <= ?",
        (current_epoch,),
    ).rowcount


def init_db(seed: bool = True, *, recover_runs: bool = False) -> None:
    ensure_dirs()
    with transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema {version} is newer than supported {SCHEMA_VERSION}; refusing downgrade")
        migrations = (_migrate_v1, _migrate_v2, _migrate_v3, _migrate_v4, _migrate_v5, _migrate_v6, _migrate_v7, _migrate_v8, _migrate_v9)
        for target in range(version + 1, SCHEMA_VERSION + 1):
            migrations[target - 1](conn)
            conn.execute(f"PRAGMA user_version = {target}")
    if seed:
        seed_demo()
    # A synchronous local-model call can be interrupted by an app restart.
    # Recover stale runs explicitly instead of leaving permanent "running" rows.
    if recover_runs:
        with transaction() as conn:
            conn.execute(
            """
            UPDATE agent_runs SET status='failed', final_answer='服务重启：运行已中断，可安全重试',
                                  finished_at=?, total_ms=0
            WHERE status='running'
            """,
                (now(),),
            )
            conn.execute("UPDATE review_jobs SET status='interrupted', error_json='{}', finished_at=? WHERE status IN ('queued','running')", (now(),))
    with transaction() as conn:
        purge_expired_conversations(conn)
    sync_fts_index()


FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS pages_fts_insert AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(page_id,case_id,document_id,page_no,document_name,doc_type,text)
    SELECT NEW.id,d.case_id,NEW.document_id,NEW.page_no,d.name,d.doc_type,NEW.text FROM documents d WHERE d.id=NEW.document_id;
END;
CREATE TRIGGER IF NOT EXISTS pages_fts_delete AFTER DELETE ON pages BEGIN
    DELETE FROM pages_fts WHERE page_id=OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS pages_fts_update AFTER UPDATE ON pages BEGIN
    DELETE FROM pages_fts WHERE page_id=OLD.id;
    INSERT INTO pages_fts(page_id,case_id,document_id,page_no,document_name,doc_type,text)
    SELECT NEW.id,d.case_id,NEW.document_id,NEW.page_no,d.name,d.doc_type,NEW.text FROM documents d WHERE d.id=NEW.document_id;
END;
CREATE TRIGGER IF NOT EXISTS documents_fts_update AFTER UPDATE OF name,doc_type,case_id ON documents BEGIN
    UPDATE pages_fts SET document_name=NEW.name,doc_type=NEW.doc_type,case_id=NEW.case_id WHERE document_id=NEW.id;
END;
"""


def sync_fts_index() -> int:
    """Rebuild the small local FTS index so migrations and manual edits stay consistent."""
    with transaction() as conn:
        conn.execute("DELETE FROM pages_fts")
        conn.execute(
            """
            INSERT INTO pages_fts(page_id, case_id, document_id, page_no, document_name, doc_type, text)
            SELECT p.id, d.case_id, d.id, p.page_no, d.name, d.doc_type, p.text
            FROM pages p JOIN documents d ON d.id = p.document_id
            """
        )
        return conn.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0]


def seed_demo() -> None:
    with transaction() as conn:
        if conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]:
            return

        ts = now()
        case_id = conn.execute(
            """
            INSERT INTO cases(title, case_no, case_type, client_name, status, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "某科技公司涉嫌非法吸收公众存款案（演示）",
                "（2026）演刑初字第008号",
                "金融犯罪",
                "某科技公司",
                "证据复核",
                "完全虚构的演示案件，用于展示私有化阅卷、证据关联与原页溯源。",
                ts,
                ts,
            ),
        ).lastrowid

        demo_docs = [
            {
                "name": "01_起诉意见书.txt",
                "doc_type": "起诉意见书",
                "people": "张某、李某、王某",
                "date_range": "2024-01 至 2025-03",
                "summary": "指控某科技公司通过理财计划向不特定对象吸收资金，并列明主要涉案人员与金额。",
                "pages": [
                    "起诉意见书（演示）\n犯罪嫌疑人张某，某科技公司法定代表人。侦查机关认为，2024年1月至2025年3月，公司通过“稳盈计划”向社会公众募集资金。",
                    "经初步审计，相关账户累计流入人民币2,860万元，涉及投资人47名。李某负责客户招揽，王某负责财务记账。",
                    "张某供述其认为该业务属于合法融资，但未能提供金融监管部门许可文件。以上材料仅为虚构演示数据。",
                ],
            },
            {
                "name": "02_张某询问笔录.txt",
                "doc_type": "询问笔录",
                "people": "张某",
                "date_range": "2025-03-18",
                "summary": "张某陈述其负责公司整体经营，否认知晓资金面向不特定公众募集。",
                "pages": [
                    "询问笔录（张某）\n问：你是否知道稳盈计划向社会公众募集资金？\n答：我不知道具体客户来源，我以为都是合作伙伴介绍的熟人。",
                    "问：收益率由谁确定？\n答：市场部提出年化12%的方案，我只看过汇报，没有参与具体设计。\n问：是否取得金融许可？\n答：不清楚，合规由李某负责。",
                    "问：你是否审批过宣传材料？\n答：我看过一版，但没有看到“保本保息”的表述。电子邮件记录显示其回复“按12%固定回报版本执行”。",
                ],
            },
            {
                "name": "03_李某询问笔录.txt",
                "doc_type": "询问笔录",
                "people": "李某、张某",
                "date_range": "2025-03-19",
                "summary": "李某称募集方案与固定收益率均由张某审批，并描述客户招揽流程。",
                "pages": [
                    "询问笔录（李某）\n问：稳盈计划由谁决定推出？\n答：张某在经营会上要求尽快募集资金，收益率也是他最后确认的。",
                    "问：客户范围如何确定？\n答：最初是熟人，后来允许老客户转发二维码，任何人都可以登记。我们没有逐一核验是否属于特定对象。",
                    "问：合规问题由谁负责？\n答：我提醒过没有金融牌照，张某说先做规模，后面再补手续。",
                ],
            },
            {
                "name": "04_账户流水摘要.txt",
                "doc_type": "银行流水",
                "people": "某科技公司、张某",
                "date_range": "2024-01-05 至 2025-03-02",
                "summary": "汇总涉案账户流入、返还与经营支出，并标记大额关联交易。",
                "pages": [
                    "账户流水摘要\n2024-01至2025-03，涉案账户共收到47名个人转款，累计28,600,000元，备注多为“理财”“稳盈”。",
                    "期间向投资人返还本金及收益合计8,420,000元，转入公司经营账户12,300,000元，转入张某个人账户1,200,000元。",
                    "2024-11-08，张某个人账户收到公司转款800,000元；摘要为“备用金”。未见对应报销凭证。",
                ],
            },
            {
                "name": "05_电子邮件与宣传材料.txt",
                "doc_type": "电子数据",
                "people": "张某、李某",
                "date_range": "2024-02 至 2024-12",
                "summary": "包含固定回报宣传文案、审批邮件和对外发布记录。",
                "pages": [
                    "电子邮件记录\n李某发送主题“稳盈计划宣传稿V3”，正文写明“年化12%，到期还本付息”。张某回复：“按固定回报版本执行，本周上线。”",
                    "宣传页面包含公开二维码，无受邀人名单或合格投资人核验步骤。后台记录显示页面访问者来自多个公开社群。",
                ],
            },
        ]

        doc_ids: list[int] = []
        for doc in demo_docs:
            doc_id = conn.execute(
                """
                INSERT INTO documents(case_id, name, mime_type, pages, status, doc_type, people, date_range, summary, created_at, updated_at)
                VALUES (?, ?, 'text/plain', ?, '已索引', ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    doc["name"],
                    len(doc["pages"]),
                    doc["doc_type"],
                    doc["people"],
                    doc["date_range"],
                    doc["summary"],
                    ts,
                    ts,
                ),
            ).lastrowid
            doc_ids.append(doc_id)
            for page_no, text in enumerate(doc["pages"], 1):
                conn.execute(
                    "INSERT INTO pages(document_id, page_no, text, summary) VALUES (?, ?, ?, ?)",
                    (doc_id, page_no, text, text[:120]),
                )

        evidence_rows = [
            ("募集对象具有不特定性", "电子数据", "公开二维码允许任何人登记，未核验特定对象范围。", "较高", doc_ids[4], 2, 2, "宣传页面包含公开二维码，无受邀人名单或合格投资人核验步骤。", "待复核"),
            ("固定回报方案经审批", "电子数据", "张某邮件确认采用年化12%的固定回报宣传版本。", "高", doc_ids[4], 1, 1, "张某回复：按固定回报版本执行，本周上线。", "已确认"),
            ("张某关于审批的陈述存在矛盾", "言词证据", "张某称未见保本保息表述，与邮件审批记录不一致。", "较高", doc_ids[1], 3, 3, "我看过一版，但没有看到“保本保息”的表述。", "待质证"),
            ("募集资金规模", "银行流水", "47名个人累计转入2,860万元。", "高", doc_ids[3], 1, 1, "共收到47名个人转款，累计28,600,000元。", "已确认"),
            ("80万元个人转款缺少凭证", "银行流水", "公司向张某个人账户支付80万元备用金，未见报销凭证。", "中", doc_ids[3], 3, 3, "张某个人账户收到公司转款800,000元；摘要为备用金。未见对应报销凭证。", "待补证"),
        ]
        evidence_ids = []
        for row in evidence_rows:
            evidence_ids.append(
                conn.execute(
                    """
                    INSERT INTO evidence(case_id, title, category, fact, credibility, source_document_id,
                                         source_page_start, source_page_end, quote, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (case_id, *row, ts),
                ).lastrowid
            )

        relations = [
            (evidence_ids[0], evidence_ids[1], "相互印证", "公开传播方式与固定回报文案共同指向公开募集。"),
            (evidence_ids[1], evidence_ids[2], "相互矛盾", "邮件审批记录与张某笔录中的否认陈述不一致。"),
            (evidence_ids[3], evidence_ids[0], "相互印证", "个人转款人数与公开招揽方式相互印证。"),
            (evidence_ids[4], evidence_ids[3], "资金链路", "个人转款属于募集资金去向核验的一部分。"),
        ]
        for from_id, to_id, rel_type, note in relations:
            conn.execute(
                "INSERT INTO evidence_relations(case_id, from_evidence_id, to_evidence_id, relation_type, note) VALUES (?, ?, ?, ?, ?)",
                (case_id, from_id, to_id, rel_type, note),
            )

        knowledge = [
            ("证据审查基本原则", "审查方法", "审查证据应关注真实性、合法性、关联性，并核对证据之间能否相互印证。", "演示知识库"),
            ("非法吸收公众存款审查要点", "金融犯罪", "通常需要围绕未经许可、公开宣传、面向不特定对象、承诺还本付息等事实要素进行证据审查。", "演示知识库"),
            ("言词证据矛盾处理", "审查方法", "对不同笔录或笔录与客观证据之间的矛盾，应定位原文、形成对照并记录待核验事项。", "演示知识库"),
        ]
        conn.executemany("INSERT INTO knowledge(title, category, text, source) VALUES (?, ?, ?, ?)", knowledge)
        conn.execute(
            "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, '创建演示案件', '载入虚构卷宗与证据关系', ?)",
            (case_id, ts),
        )
