"""Freeze a reproducible source manifest for the ForgeHarness migration (M0).

This script records Git provenance when available, while the content-addressed
identity always describes current allowlisted files, including local changes.
Every file in the
release allowlist (scripts/package_release.py source_files()) is hashed with
SHA-256, and a deterministic tree digest is computed over the sorted mapping.
The algorithm intentionally mirrors ForgeHarness ``benchmarks/
framework_snapshot.py`` so both sides of the ledger use the same identity
scheme.

Private runtime data (data/, output/, model weights, case uploads) is never
included: the file set is exactly the release allowlist, which already
excludes it by construction.

Usage (from the repository root):

    uv run --locked python -m scripts.migration_source_manifest --output out.json
    uv run --locked python -m scripts.migration_source_manifest --check frozen.json

``--check`` re-computes the manifest and fails on drift, so a frozen baseline
can be re-validated before any comparison run (candidate changed => refreeze).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.package_release import source_files

SCHEMA_VERSION = 1
PROFILE = "lexvault-release-allowlist"

def source_provenance(root: Path) -> dict[str, Any]:
    """Do not confuse HEAD, an enclosing repository, or an archive with source bytes."""
    if not (root / ".git").exists():
        return {"source_kind": "working checkout; no Git metadata"}

    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(root), *arguments],
            capture_output=True, text=True, timeout=10, check=False,
        )

    try:
        top = git("rev-parse", "--show-toplevel")
        if top.returncode or Path(top.stdout.strip()).resolve() != root.resolve():
            return {"source_kind": "working checkout; Git provenance unavailable"}
        head = git("rev-parse", "--verify", "HEAD")
        status = git("status", "--porcelain=v1", "-z", "--untracked-files=normal")
        if status.returncode:
            return {"source_kind": "working checkout; Git provenance unavailable"}
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return {"source_kind": "working checkout; Git provenance unavailable"}
    return {
        "source_kind": "Git working checkout; current release-allowlisted content",
        "git": {"head_commit": head.stdout.strip() if head.returncode == 0 else None,
                "working_tree_dirty": bool(status.stdout)},
    }


def build_manifest(root: Path) -> dict[str, Any]:
    """Hash every allowlisted source file under *root* and digest the mapping."""
    files = source_files(root)
    hashes: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    tree_digest = hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": PROFILE,
        **source_provenance(root),
        "tree_digest": tree_digest,
        "files": dict(sorted(hashes.items())),
    }


def _canonical(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=default_root, help="project root (default: repository root)")
    parser.add_argument("--output", type=Path, default=None, help="write the manifest JSON here (default: stdout)")
    parser.add_argument("--check", type=Path, default=None, metavar="FROZEN",
                        help="compare the live tree against a frozen manifest; exit 1 on drift")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    manifest = build_manifest(root)

    if args.check is not None:
        frozen_path: Path = args.check
        frozen = json.loads(frozen_path.read_text())
        if frozen.get("tree_digest") == manifest["tree_digest"]:
            print(f"source verified: {manifest['tree_digest']} ({len(manifest['files'])} files)")
            return 0
        frozen_files: dict[str, str] = frozen.get("files", {})
        live_files: dict[str, str] = manifest["files"]
        added = sorted(set(live_files) - set(frozen_files))
        removed = sorted(set(frozen_files) - set(live_files))
        changed = sorted(k for k in set(live_files) & set(frozen_files) if live_files[k] != frozen_files[k])
        print(
            f"source DRIFT: frozen={frozen.get('tree_digest')} live={manifest['tree_digest']} "
            f"added={len(added)} removed={len(removed)} changed={len(changed)}",
            file=sys.stderr,
        )
        for label, names in (("added", added), ("removed", removed), ("changed", changed)):
            for name in names[:50]:
                print(f"  {label}: {name}", file=sys.stderr)
        return 1

    payload = _canonical(manifest)
    if args.output is not None:
        output: Path = args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload)
        print(f"manifest frozen: {manifest['tree_digest']} ({len(manifest['files'])} files) -> {output}")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
