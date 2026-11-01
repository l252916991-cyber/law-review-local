"""Read-only acceptance checks for the internal collaboration release.

Run this on the machine that will host the firm's instance, while the API is
stopped for the backup drill:

    uv run --locked python -m scripts.collaboration_acceptance --data-dir ~/lexvault-data
    uv run --locked python -m scripts.collaboration_acceptance \
        --data-dir ~/lexvault-data --backup-drill /tmp/lexvault-drill --quiescent

Checks cover identity configuration, member directory, schema/integrity,
storage headroom, optional OCR tools, an optional offline model probe and an
optional backup/restore drill. The script never prints tokens, never sends case
data and never mutates the source data directory: every database is opened
read-only, so a wrong path fails instead of creating an empty database.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.request
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MIN_TOKEN_LENGTH = 32
WARN_FREE_BYTES = 2 * 1024 * 1024 * 1024
MIN_FREE_BYTES = 512 * 1024 * 1024
# Rows compared after a drill; new system tables are reported, not required to
# match, so an older schema is classified instead of failing the backup.
BUSINESS_TABLES = ("cases", "documents", "evidence", "members", "review_tasks", "case_reports",
                   "case_report_versions", "case_report_events", "review_task_events", "bank_transactions")


@dataclass
class Check:
    name: str
    status: str  # pass | warn | fail | skip
    detail: str


def _check(name: str, status: str, detail: str) -> Check:
    return Check(name=name, status=status, detail=detail)


def _db_path(data_dir: Path) -> Path:
    return data_dir / "law_review.db"


def open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open an existing database for inspection; never create one."""
    if not db_path.is_file():
        raise FileNotFoundError(f"数据库不存在：{db_path}")
    return sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def _count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def check_environment(require_ocr: bool) -> list[Check]:
    checks = [
        _check("python", "pass" if (3, 11) <= sys.version_info[:2] < (3, 15) else "fail",
               f"{sys.version.split()[0]}（要求 3.11–3.14）"),
        _check("sqlite", "pass" if sqlite3.sqlite_version_info >= (3, 35) else "warn",
               f"SQLite {sqlite3.sqlite_version}"),
    ]
    with closing(sqlite3.connect(":memory:")) as probe:
        try:
            probe.execute("CREATE VIRTUAL TABLE fts_probe USING fts5(text)")
            checks.append(_check("fts5", "pass", "FTS5 可用"))
        except sqlite3.OperationalError:
            checks.append(_check("fts5", "fail", "FTS5 不可用，检索与页索引无法工作"))
    tools = {name: shutil.which(name) for name in ("pdfinfo", "pdftotext", "pdftoppm", "tesseract")}
    missing = [name for name, path in tools.items() if not path]
    ocr_status = "fail" if (require_ocr and missing) else ("warn" if missing else "pass")
    checks.append(_check("document_tools", ocr_status, "缺失：" + "、".join(missing) if missing else "PDF/OCR 工具齐全"))
    if tools["tesseract"]:
        languages = _tesseract_languages(str(tools["tesseract"]))
        ok = {"chi_sim", "eng"} <= set(languages)
        checks.append(_check("ocr_languages", "fail" if (require_ocr and not ok) else ("warn" if not ok else "pass"),
                             "、".join(languages) or "未列出语言"))
    return checks


def _tesseract_languages(binary: str) -> list[str]:
    import subprocess

    result = subprocess.run([binary, "--list-langs"], capture_output=True, text=True, timeout=10, check=False)
    return [line.strip() for line in result.stdout.splitlines()[1:] if line.strip()]


def check_identity(conn: sqlite3.Connection, *, multi_user: bool = False) -> list[Check]:
    mode = os.getenv("LAW_REVIEW_AUTH_MODE", "local")
    checks: list[Check] = [_check("auth_mode", "pass" if mode in {"local", "token"} else "fail", f"模式：{mode}")]
    active_members = _count(conn, "SELECT COUNT(*) FROM members WHERE active = 1")
    active_admins = _count(conn, "SELECT COUNT(*) FROM members WHERE active = 1 AND is_admin = 1")
    checks.append(_check("member_directory", "pass" if active_members else "skip",
                         f"启用成员 {active_members} 人，其中管理员 {active_admins} 人"))
    if mode == "local":
        checks.append(_check(
            "multi_user",
            "fail" if multi_user else "warn",
            "本机模式是单用户，不能作为多人交付验收结论"
            if multi_user else "本机模式是单用户；多人协作需要 LAW_REVIEW_AUTH_MODE=token",
        ))
        return checks
    raw = os.getenv("LAW_REVIEW_API_TOKENS_JSON", "").strip()
    env_tokens: dict[str, Any] = {}
    if raw and raw != "{}":
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                env_tokens = parsed
        except ValueError:
            checks.append(_check("env_tokens", "fail", "LAW_REVIEW_API_TOKENS_JSON 不是合法 JSON"))
    short = [token for token in env_tokens if len(token) < MIN_TOKEN_LENGTH]
    insecure = os.getenv("LAW_REVIEW_ALLOW_INSECURE_LOCAL_TOKEN", "").strip().lower() in {"1", "true", "yes", "on"}
    if short and not insecure:
        checks.append(_check("token_strength", "fail", f"{len(short)} 个环境令牌短于 {MIN_TOKEN_LENGTH} 字符"))
    else:
        checks.append(_check("token_strength", "warn" if insecure else "pass",
                             "已开启短令牌逃生口（仅回环）" if insecure else f"环境令牌 {len(env_tokens)} 个，长度合规"))
    hosts = {item.strip() for item in os.getenv("LAW_REVIEW_ALLOWED_HOSTS", "localhost,127.0.0.1,::1,testserver").split(",") if item.strip()}
    checks.append(_check("allowed_hosts", "fail" if "*" in hosts else "pass", "、".join(sorted(hosts))))
    env_admin = any(
        isinstance(value, dict) and value.get("admin") is True for value in env_tokens.values()
    )
    if not env_admin and not active_admins:
        checks.append(_check("administrator", "fail", "没有启用的管理员：既无环境管理员令牌，也无启用的目录管理员"))
    else:
        checks.append(_check("administrator", "pass",
                             f"环境管理员 {'有' if env_admin else '无'}，目录管理员 {active_admins} 人"))
    orphan_tokens = _count(
        conn,
        """SELECT COUNT(*) FROM member_tokens t JOIN members m ON m.id = t.member_id
           WHERE t.revoked_at IS NULL AND m.active = 0""",
    )
    checks.append(_check("revoked_access", "warn" if orphan_tokens else "pass",
                         f"停用成员仍持有效令牌 {orphan_tokens} 个" if orphan_tokens else "停用成员没有可用令牌"))
    return checks


def check_database(db_path: Path) -> list[Check]:
    if not db_path.is_file():
        return [_check("database", "fail", f"找不到 {db_path}")]
    from app.db import SCHEMA_VERSION

    with closing(open_readonly(db_path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        checks = [
            _check("database", "pass", f"{db_path}（{db_path.stat().st_size // 1024} KiB）"),
            _check("schema_version", "pass" if version == SCHEMA_VERSION else "fail",
                   f"user_version={version}，期望 {SCHEMA_VERSION}"),
        ]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        checks.append(_check("integrity", "pass" if integrity == "ok" else "fail", str(integrity)))
        counts: dict[str, int] = {"案件": _count(conn, "SELECT COUNT(*) FROM cases")}
        for label, table in (("材料", "documents"), ("证据", "evidence")):
            if _table_exists(conn, table):
                counts[label] = _count(conn, f"SELECT COUNT(*) FROM {table}")
        if _table_exists(conn, "evidence"):
            counts["已确认"] = _count(conn, "SELECT COUNT(*) FROM evidence WHERE status = '已确认'")
        if _table_exists(conn, "review_tasks"):
            counts["未结任务"] = _count(
                conn, "SELECT COUNT(*) FROM review_tasks WHERE status IN ('待处理','进行中','待复核')"
            )
        checks.append(_check("records", "pass", "，".join(f"{key} {value}" for key, value in counts.items())))
        if version < SCHEMA_VERSION:
            checks.append(_check("migration", "fail",
                                 f"数据库版本 {version} 低于 {SCHEMA_VERSION}：启动一次应用完成迁移后再验收"))
    return checks


def check_collaboration(db_path: Path) -> list[Check]:
    from app.report_basis import basis_hash, basis_snapshot

    with closing(open_readonly(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        checks: list[Check] = []
        unassigned = _count(
            conn,
            """SELECT COUNT(*) FROM members m WHERE m.active = 1 AND NOT EXISTS
               (SELECT 1 FROM case_members cm WHERE cm.member_id = m.id)""",
        )
        checks.append(_check("case_assignment", "warn" if unassigned else "pass",
                             f"{unassigned} 名启用成员没有分配任何案件" if unassigned else "启用成员均已分配案件"))
        overdue = _count(
            conn,
            """SELECT COUNT(*) FROM review_tasks WHERE status IN ('待处理','进行中','待复核')
               AND due_at IS NOT NULL AND due_at < date('now')""",
        )
        checks.append(_check("overdue_tasks", "warn" if overdue else "pass", f"逾期任务 {overdue} 个"))
        reports = conn.execute(
            "SELECT * FROM case_reports WHERE status = '已批准' ORDER BY case_id, version DESC"
        ).fetchall()
        stale = 0
        for report in reports:
            snapshot = basis_snapshot(conn, report["case_id"])
            if basis_hash(snapshot) != report["basis_hash"]:
                stale += 1
        columns = {row[1] for row in conn.execute("PRAGMA table_info(case_reports)")}
        if "approved_version_id" not in columns:
            checks.append(_check("signing_freeze", "warn",
                                 "该库还没有冻结正文列：启动一次当前版本完成迁移后重新验收"))
        else:
            legacy = _count(
                conn,
                "SELECT COUNT(*) FROM case_reports WHERE status = '已批准' AND approved_version_id IS NULL",
            )
            checks.append(_check(
                "signing_freeze", "warn" if legacy else "pass",
                f"{legacy} 份已批准报告没有冻结正文，需重新准备并审批" if legacy
                else "已批准报告均绑定冻结正文",
            ))
        checks.append(_check("report_basis", "warn" if stale else "pass",
                             f"{stale} 份已批准报告的材料依据已变化" if stale else
                             f"已批准报告 {len(reports)} 份，依据均未变化"))
        no_report = _count(
            conn,
            """SELECT COUNT(*) FROM cases c WHERE c.lifecycle_status = 'active' AND NOT EXISTS
               (SELECT 1 FROM case_reports r WHERE r.case_id = c.id AND r.status = '已批准')""",
        )
        checks.append(_check("pending_signing", "warn" if no_report else "pass",
                             f"{no_report} 个在办案件尚无已批准报告" if no_report else "在办案件均已有批准报告"))
    return checks


def check_storage(data_dir: Path) -> list[Check]:
    if not data_dir.exists():
        return [_check("storage", "fail", f"数据目录不存在：{data_dir}")]
    usage = shutil.disk_usage(data_dir)
    status = "fail" if usage.free < MIN_FREE_BYTES else ("warn" if usage.free < WARN_FREE_BYTES else "pass")
    return [_check("free_space", status,
                   f"剩余 {usage.free / (1024 ** 3):.1f} GiB，数据目录 {data_dir}")]


def _restored_artifact_problem(restored: sqlite3.Connection, root: Path) -> str:
    """Verify every approved frozen body resolves inside the restored copy."""
    if not _table_exists(restored, "case_report_versions"):
        return ""
    rows = restored.execute(
        """SELECT v.id, v.artifact_dir, v.content_json, v.content_hash
           FROM case_reports r JOIN case_report_versions v ON v.id = r.approved_version_id
           WHERE r.status = '已批准' AND r.approved_version_id IS NOT NULL"""
    ).fetchall()
    for version_id, artifact_dir, content_json, content_hash in rows:
        directory = (root / artifact_dir).resolve()
        if not directory.is_dir() or root.resolve() not in directory.parents:
            return f"批准版本 #{version_id} 的冻结工件目录缺失：{artifact_dir}"
        try:
            body = json.loads(content_json or "{}")
        except ValueError:
            return f"批准版本 #{version_id} 的正文记录损坏"
        import hashlib

        digest = hashlib.sha256(
            json.dumps({"blocks": body.get("blocks"), "transactions": body.get("transactions"),
                        "attachments": body.get("attachments")},
                       ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if digest != content_hash:
            return f"批准版本 #{version_id} 的正文摘要不一致"
        entries = [*(body.get("blocks") or []), body.get("transactions") or {}]
        for entry in entries:
            path = directory / entry.get("file", "")
            if not path.is_file():
                return f"批准版本 #{version_id} 缺少冻结文件：{entry.get('archive_name')}"
            if path.stat().st_size != entry.get("size"):
                return f"批准版本 #{version_id} 的冻结文件大小不符：{entry.get('archive_name')}"
    return ""


def backup_drill(
    data_dir: Path, target: Path, *, quiescent: bool, checkpoint: Path | None = None
) -> list[Check]:
    if not quiescent:
        return [_check("backup_drill", "fail", "需要先停止 API/worker 并显式传入 --quiescent")]
    from scripts import data_snapshot

    if target.exists():
        return [_check("backup_drill", "fail", f"演练目录已存在：{target}")]
    backup_dir = target / "backup"
    restore_dir = target / "restored"
    started = time.monotonic()
    try:
        target.mkdir(parents=True, exist_ok=True)
        manifest = data_snapshot.backup(data_dir, backup_dir, checkpoint=checkpoint, quiescent=True)
        data_snapshot.verify(backup_dir)
        data_snapshot.restore(backup_dir, restore_dir)
    except Exception as exc:  # the drill reports the real error instead of guessing
        return [_check("backup_drill", "fail", f"{type(exc).__name__}: {exc}")]
    elapsed = time.monotonic() - started
    checks = [_check("backup_drill", "pass",
                     f"备份 {len(manifest['files'])} 个文件并通过校验，恢复到新目录耗时 {elapsed:.1f}s")]
    if "langgraph_checkpoints.sqlite" in {Path(item["path"]).name for item in manifest["files"]}:
        checks.append(_check("restore_checkpoint", "pass",
                             f"已恢复 checkpoint：{Path(manifest['checkpoint_original'] or '')}"))
    with closing(open_readonly(_db_path(data_dir))) as original, \
            closing(open_readonly(_db_path(restore_dir))) as restored:
        integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = restored.execute("PRAGMA foreign_key_check").fetchall()
        checks.append(_check("restore_integrity", "pass" if integrity == "ok" and not foreign else "fail",
                             f"完整性 {integrity}，外键问题 {len(foreign)} 条"))
        missing = [table for table in BUSINESS_TABLES if not _table_exists(restored, table)]
        if missing:
            checks.append(_check("restore_schema", "warn",
                                 "恢复库缺少表（旧 schema 或未迁移）：" + "、".join(missing)))
        compared = [table for table in BUSINESS_TABLES
                    if _table_exists(original, table) and _table_exists(restored, table)]
        differences = []
        for table in compared:
            before = _count(original, f"SELECT COUNT(*) FROM {table}")
            after = _count(restored, f"SELECT COUNT(*) FROM {table}")
            if before != after:
                differences.append(f"{table}: {before} → {after}")
        checks.append(_check("restore_counts", "fail" if differences else "pass",
                             "；".join(differences) if differences else
                             f"{len(compared)} 张业务表行数一致"))
        problem = _restored_artifact_problem(restored, restore_dir)
        checks.append(_check("restore_artifacts", "fail" if problem else "pass",
                             problem or "已批准报告的冻结正文与工件可读"))
    return checks


def probe_model(url: str, model_id: str | None = None) -> list[Check]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return [_check("model_probe", "fail", "模型地址无效")]
    endpoint = url.rstrip("/") + "/models"
    request = urllib.request.Request(endpoint, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - explicit operator URL
            body = json.loads(response.read(1_000_000).decode("utf-8", "replace"))
    except Exception as exc:
        detail = f"{endpoint} 不可用：{type(exc).__name__}"
        return [_check("model_probe", "fail", detail),
                _check("model_endpoint", "fail", detail),
                _check("model_target", "fail" if model_id else "skip", "接口不可达，无法确认目标模型")]
    items = body.get("data") if isinstance(body, dict) else None
    models = [item.get("id") for item in items or [] if isinstance(item, dict)]
    checks = [_check("model_endpoint", "pass", f"{endpoint} 可访问，列出 {len(models)} 个模型"),
              _check("model_list", "pass" if models else "fail",
                     "模型列表非空" if models else "模型列表为空，不能视为模型就绪")]
    if model_id:
        present = model_id in models
        checks.append(_check("model_target", "pass" if present else "fail",
                             f"目标模型 {model_id} {'存在' if present else '未在 /models 中列出'}"))
    checks.append(_check("model_inference", "skip",
                         "推理质量与引用效果属于业务验收，需授权后由复核人执行"))
    return checks


def run_checks(
    data_dir: Path,
    *,
    require_ocr: bool = False,
    backup_target: Path | None = None,
    quiescent: bool = False,
    model_url: str | None = None,
    model_id: str | None = None,
    multi_user: bool = False,
    checkpoint: Path | None = None,
) -> dict[str, Any]:
    data_dir = data_dir.expanduser().resolve()
    os.environ["LAW_REVIEW_DATA_DIR"] = str(data_dir)
    # The target is the SQLite file in this data directory, never a configured
    # PostgreSQL database (which may hold real cases).
    os.environ["LAW_REVIEW_DATABASE_URL"] = ""
    db_path = _db_path(data_dir)
    checks = check_environment(require_ocr)
    checks.extend(check_storage(data_dir))
    checks.extend(check_database(db_path))
    if db_path.is_file():
        try:
            with closing(open_readonly(db_path)) as conn:
                checks.extend(check_identity(conn, multi_user=multi_user))
        except sqlite3.OperationalError as exc:
            checks.append(_check("member_directory", "fail", f"缺少成员表：{exc}"))
        try:
            checks.extend(check_collaboration(db_path))
        except sqlite3.OperationalError as exc:
            checks.append(_check("collaboration_tables", "fail", f"缺少协作表：{exc}"))
    if backup_target is not None:
        checks.extend(backup_drill(
            data_dir, backup_target.expanduser().resolve(), quiescent=quiescent,
            checkpoint=checkpoint.expanduser().resolve() if checkpoint else None,
        ))
    if model_url:
        checks.extend(probe_model(model_url, model_id))
    summary = {
        "pass": sum(check.status == "pass" for check in checks),
        "warn": sum(check.status == "warn" for check in checks),
        "fail": sum(check.status == "fail" for check in checks),
        "skip": sum(check.status == "skip" for check in checks),
    }
    return {"data_dir": str(data_dir), "checks": [asdict(check) for check in checks], "summary": summary}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("LAW_REVIEW_DATA_DIR", ROOT / "data")))
    parser.add_argument("--require-ocr", action="store_true", help="缺少 PDF/OCR 工具时判为失败")
    parser.add_argument("--backup-drill", type=Path, default=None, help="备份恢复演练目录（必须不存在）")
    parser.add_argument("--quiescent", action="store_true", help="确认 API/worker 已停止，允许执行备份演练")
    parser.add_argument("--probe-model", default=None, help="显式探测模型服务地址（不发送案件数据）")
    parser.add_argument("--model-id", default=None, help="要求 /models 列出的目标模型 ID")
    parser.add_argument("--multi-user", action="store_true",
                        help="按多人交付验收：本机单用户模式判为失败")
    parser.add_argument("--checkpoint-db", type=Path, default=None,
                        help="实际的 LangGraph checkpoint 数据库路径（透传给备份工具）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    report = run_checks(
        args.data_dir,
        require_ocr=args.require_ocr,
        backup_target=args.backup_drill,
        quiescent=args.quiescent,
        model_url=args.probe_model,
        model_id=args.model_id,
        multi_user=args.multi_user,
        checkpoint=args.checkpoint_db,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for check in report["checks"]:
            mark = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}[check["status"]]
            print(f"[{mark}] {check['name']}: {check['detail']}")
        summary = report["summary"]
        print(f"\n通过 {summary['pass']}，警告 {summary['warn']}，失败 {summary['fail']}，跳过 {summary['skip']}")
    return 1 if report["summary"]["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
