"""Offline, verified backups and restores; never overwrite a source or existing target."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import ExitStack, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FORMAT = "lexvault-offline-snapshot-v1"
DB_NAME = "law_review.db"
CHECKPOINT_NAME = "langgraph_checkpoints.sqlite"
PATH_TABLES = ("documents", "batch_import_files")


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def inventory(root: Path, *, ignore_sqlite_locks: bool = False) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Symbolic links are not supported in snapshots: {path}")
        # SQLite readers update WAL shared-memory read marks without changing data.
        # Logical writes are still detected by data_version and WAL/main-file metadata.
        if ignore_sqlite_locks and path.name.endswith("-shm"):
            continue
        if path.is_file():
            stat = path.stat()
            result[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
    return result


def read_database(path: Path) -> sqlite3.Connection:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular SQLite file: {path}")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)


def table_exists(database: sqlite3.Connection, table: str) -> bool:
    return database.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def check_integrity(database: sqlite3.Connection) -> None:
    if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity_check failed")
    if database.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("SQLite foreign_key_check failed")


def require_idle(database: sqlite3.Connection) -> None:
    for table, statuses in (
        ("agent_runs", ("running",)),
        ("batch_imports", ("queued", "pending", "processing", "running")),
        ("batch_import_files", ("pending", "processing")),
        ("review_jobs", ("queued", "running", "processing")),
    ):
        if table_exists(database, table):
            placeholders = ",".join("?" for _ in statuses)
            if database.execute(f"SELECT 1 FROM {table} WHERE status IN ({placeholders}) LIMIT 1", statuses).fetchone():
                raise ValueError(f"Active work in {table}; drain/resolve it before backup (Redis jobs are not included)")


def relative_stored_path(value: str, source: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        try:
            relative = path.resolve().relative_to(source)
        except ValueError as exc:
            raise ValueError(f"Stored document path is outside the data directory: {value}") from exc
    elif path.parts and path.parts[0] in {"uploads", "exports"}:
        relative = path
    elif path.parts and path.parts[0] == source.name:
        relative = Path(*path.parts[1:])
    else:
        raise ValueError(f"Cannot safely relocate legacy relative path: {value}; normalize before backup")
    if not relative.parts or relative.parts[0] not in {"uploads", "exports"} or ".." in relative.parts:
        raise ValueError(f"Unsafe stored path: {value}")
    return relative


def collect_relocations(database: sqlite3.Connection, source: Path, files_root: Path | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for table in PATH_TABLES:
        if not table_exists(database, table):
            continue
        for record_id, stored_path in database.execute(f"SELECT id, stored_path FROM {table} WHERE stored_path != '' ORDER BY id"):
            relative = relative_stored_path(stored_path, source)
            original = (files_root or source) / relative
            if table == "documents" and not original.is_file():
                raise ValueError(f"Missing uploaded document: {original}")
            records.append({"table": table, "id": record_id, "relative_path": str(relative)})
    return records


def snapshot_database(source: sqlite3.Connection, destination: Path) -> None:
    deadline = time.monotonic() + 60

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite backup exceeded 60 seconds; check that services are stopped")

    with closing(sqlite3.connect(destination)) as target:
        source.backup(target, pages=256, progress=progress)
        # A portable offline snapshot should not need mutable WAL/SHM sidecars.
        target.execute("PRAGMA journal_mode=DELETE")
        check_integrity(target)


def ensure_new_target(target: Path, source: Path) -> None:
    if target.exists() or target.is_symlink():
        raise ValueError("Destination must not exist; existing data is never overwritten")
    if target.is_relative_to(source) or source.is_relative_to(target):
        raise ValueError("Source and destination must be separate, non-nested directories")


def backup(source: Path, destination: Path, *, checkpoint: Path | None = None, quiescent: bool = False) -> dict[str, Any]:
    if not quiescent:
        raise ValueError("Stop API/worker, wait for active work to finish, then explicitly pass --quiescent")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination must not exist; existing data is never overwritten")
    if checkpoint is not None and checkpoint.is_symlink():
        raise ValueError("Symbolic checkpoint paths are not supported")
    source = source.resolve(strict=True)
    destination = destination.resolve()
    ensure_new_target(destination, source)
    checkpoint = checkpoint.resolve() if checkpoint else source / CHECKPOINT_NAME
    databases = {DB_NAME: source / DB_NAME}
    if checkpoint.exists():
        databases[CHECKPOINT_NAME] = checkpoint
    with ExitStack() as stack:
        connections = {name: stack.enter_context(closing(read_database(path))) for name, path in databases.items()}
        business = connections[DB_NAME]
        check_integrity(business)
        require_idle(business)
        if table_exists(business, "agent_runs"):
            columns = {row[1] for row in business.execute("PRAGMA table_info(agent_runs)")}
            if "checkpoint_thread_id" in columns and CHECKPOINT_NAME not in connections:
                if business.execute("SELECT 1 FROM agent_runs WHERE checkpoint_thread_id != '' LIMIT 1").fetchone():
                    raise ValueError("LangGraph run references a missing checkpoint; pass the actual --checkpoint-db")
        relocations = collect_relocations(business, source)
        versions = {name: connection.execute("PRAGMA data_version").fetchone()[0] for name, connection in connections.items()}
        before = inventory(source, ignore_sqlite_locks=True)
        destination.mkdir(parents=True, mode=0o700)
        (destination / "INCOMPLETE").write_text("Do not restore until manifest.json exists and verifies.\n", encoding="utf-8")
        for name, connection in connections.items():
            snapshot_database(connection, destination / name)
        for directory in ("uploads", "exports"):
            if (source / directory).exists():
                shutil.copytree(source / directory, destination / directory, symlinks=True)
        if inventory(source, ignore_sqlite_locks=True) != before or any(
            connection.execute("PRAGMA data_version").fetchone()[0] != versions[name]
            for name, connection in connections.items()
        ):
            raise ValueError("Source changed during backup; incomplete target retained for inspection, do not restore")
        require_idle(business)
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_data_dir": str(source),
        "checkpoint_original": str(checkpoint) if CHECKPOINT_NAME in databases else None,
        "quiescent_operator_confirmed": True,
        "redis_included": False,
        "relocations": relocations,
        "files": [
            {"path": name, "size": (destination / name).stat().st_size, "sha256": digest(destination / name)}
            for name in inventory(destination) if name != "INCOMPLETE"
        ],
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination / "INCOMPLETE").unlink()
    verify(destination)
    return manifest


def verify(snapshot: Path) -> dict[str, Any]:
    snapshot = snapshot.resolve(strict=True)
    actual = inventory(snapshot)
    if "INCOMPLETE" in actual:
        raise ValueError("Incomplete snapshot must not be restored")
    manifest: dict[str, Any] = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT or not manifest.get("quiescent_operator_confirmed"):
        raise ValueError("Unsupported or unconfirmed snapshot format")
    names: set[str] = set()
    for item in manifest["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("Unsafe manifest path")
        if str(relative) in names:
            raise ValueError("Duplicate manifest path")
        names.add(str(relative))
        path = snapshot / relative
        if not path.is_file() or path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
            raise ValueError(f"Snapshot checksum mismatch: {relative}")
    if names != set(actual) - {"manifest.json"} or DB_NAME not in names:
        raise ValueError("Snapshot file set differs from manifest")
    for name in (DB_NAME, CHECKPOINT_NAME):
        if name in names:
            with closing(read_database(snapshot / name)) as database:
                check_integrity(database)
    return manifest


def restore(snapshot: Path, destination: Path) -> dict[str, Any]:
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination must not exist; existing data is never overwritten")
    snapshot = snapshot.resolve(strict=True)
    destination = destination.resolve()
    ensure_new_target(destination, snapshot)
    manifest = verify(snapshot)
    # Validate relocation metadata against the backed-up DB; never trust a manifest SQL table name.
    with closing(read_database(snapshot / DB_NAME)) as database:
        expected = collect_relocations(database, Path(manifest["source_data_dir"]), files_root=snapshot)
    if expected != manifest["relocations"]:
        raise ValueError("Relocation metadata does not match backed-up database")
    destination.mkdir(parents=True, mode=0o700)
    (destination / "RESTORE_INCOMPLETE").write_text("Do not start application until restore finishes.\n", encoding="utf-8")
    for item in manifest["files"]:
        path = destination / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot / item["path"], path, follow_symlinks=False)
        if path.is_symlink() or path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
            raise ValueError("Snapshot changed while restoring; incomplete destination retained")
    with closing(sqlite3.connect(destination / DB_NAME)) as database:
        for item in manifest["relocations"]:
            if item["table"] not in PATH_TABLES:
                raise ValueError("Unsafe relocation table")
            relative = Path(item["relative_path"])
            if relative.is_absolute() or ".." in relative.parts or relative.parts[0] not in {"uploads", "exports"}:
                raise ValueError("Unsafe relocation path")
            if item["table"] == "documents" and not (destination / relative).is_file():
                raise ValueError("Restored document missing")
            database.execute(f"UPDATE {item['table']} SET stored_path=? WHERE id=?", (str(destination / relative), item["id"]))
        database.commit()
        check_integrity(database)
    report = {"snapshot": str(snapshot), "data_dir": str(destination), "relocated_paths": len(manifest["relocations"]),
              "checkpoint_env": str(destination / CHECKPOINT_NAME) if manifest["checkpoint_original"] else ""}
    (destination / "RESTORE_REPORT.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination / "RESTORE_INCOMPLETE").unlink()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--data-dir", required=True, type=Path)
    backup_parser.add_argument("--output", required=True, type=Path)
    backup_parser.add_argument("--checkpoint-db", type=Path)
    backup_parser.add_argument("--quiescent", action="store_true")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("snapshot", type=Path)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("snapshot", type=Path)
    restore_parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "backup":
        result = backup(args.data_dir, args.output, checkpoint=args.checkpoint_db, quiescent=args.quiescent)
    elif args.action == "verify":
        result = verify(args.snapshot)
    else:
        result = restore(args.snapshot, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
