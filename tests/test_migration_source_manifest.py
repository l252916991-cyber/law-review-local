"""Source bytes remain authoritative when Git provenance is present or absent."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from scripts.ops.migration_source_manifest import build_manifest, main


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True, check=True,
    ).stdout.strip()


def repository(root: Path) -> str:
    git(root, "init", "-b", "main")
    (root / "README.md").write_text("reviewed source\n")
    git(root, "add", "--", "README.md")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
        "-c", "core.hooksPath=/dev/null", "commit", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


def test_git_clean_dirty_and_current_content_identity(tmp_path):
    head = repository(tmp_path)
    clean = build_manifest(tmp_path)
    assert clean["git"] == {"head_commit": head, "working_tree_dirty": False}
    assert clean["source_kind"].startswith("Git working checkout")
    (tmp_path / "README.md").write_text("local uncommitted content\n")
    dirty = build_manifest(tmp_path)
    assert dirty["git"] == {"head_commit": head, "working_tree_dirty": True}
    assert dirty["tree_digest"] != clean["tree_digest"]
    assert dirty["files"]["README.md"] == hashlib.sha256(b"local uncommitted content\n").hexdigest()
    assert ".git/config" not in dirty["files"]


def test_archive_inside_repository_has_no_inherited_git_identity(tmp_path):
    repository(tmp_path)
    archive = tmp_path / "extracted"
    archive.mkdir()
    (archive / "README.md").write_text("source archive\n")
    (archive / "data").mkdir()
    (archive / "data" / "private.db").write_bytes(b"private")
    manifest = build_manifest(archive)
    assert "no Git metadata" in manifest["source_kind"]
    assert "git" not in manifest
    assert list(manifest["files"]) == ["README.md"]
    expected = hashlib.sha256(json.dumps(manifest["files"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert manifest["tree_digest"] == expected
    frozen = tmp_path / "legacy.json"
    # Legacy manifests have no Git fields and remain valid for the same content.
    frozen.write_text(json.dumps({"source_kind": "working checkout; no Git metadata", **manifest}))
    assert main(["--root", str(archive), "--check", str(frozen)]) == 0
    (archive / "README.md").write_text("changed\n")
    assert main(["--root", str(archive), "--check", str(frozen)]) == 1


def test_unborn_and_unavailable_git_are_explicit(tmp_path, monkeypatch):
    git(tmp_path, "init", "-b", "main")
    assert build_manifest(tmp_path)["git"] == {"head_commit": None, "working_tree_dirty": False}

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(subprocess, "run", unavailable)
    manifest = build_manifest(tmp_path)
    assert "provenance unavailable" in manifest["source_kind"]
    assert "git" not in manifest


def test_linked_worktree_and_untracked_changes(tmp_path):
    root, worktree = tmp_path / "repo", tmp_path / "worktree"
    root.mkdir()
    head = repository(root)
    git(root, "worktree", "add", "--detach", str(worktree), "HEAD")
    assert (worktree / ".git").is_file()
    assert build_manifest(worktree)["git"] == {"head_commit": head, "working_tree_dirty": False}
    (worktree / "local.txt").write_text("untracked\n")
    manifest = build_manifest(worktree)
    assert manifest["git"]["working_tree_dirty"] is True
    assert "local.txt" not in manifest["files"]
