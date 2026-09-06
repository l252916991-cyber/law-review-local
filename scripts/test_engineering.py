"""No-network tests for release/model tooling, using only synthetic temporary files."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.package_release import source_files
from scripts.setup_models import inspect_model, pinned_download


class EngineeringToolTests(unittest.TestCase):
    def test_release_excludes_private_files_and_backups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "README.md", "app/main.py", "app/main.py.backup", "models/config.json",
                "data/case.txt", "output/result.json", ".env", ".env.example",
                "docs/INTERVIEW_GUIDE.md", "docs/runbook/operations.md",
            ):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic fixture", encoding="utf-8")
            names = {str(path.relative_to(root)) for path in source_files(root)}
            self.assertEqual(names, {"README.md", "app/main.py", ".env.example", "docs/runbook/operations.md"})

    def test_release_refuses_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app").mkdir()
            secret = root / "private.txt"
            secret.write_text("synthetic private content", encoding="utf-8")
            (root / "app" / "leak.py").symlink_to(secret)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                source_files(root)

    def test_lawbench_requires_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "benchmarks/lawbench/zero_shot/fixture.json"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("[]", encoding="utf-8")
            self.assertEqual(source_files(root), [])
            self.assertEqual(source_files(root, True), [fixture])

    def test_model_download_rejects_mutable_revision(self) -> None:
        with self.assertRaisesRegex(ValueError, "40-character"):
            pinned_download("owner/model", "main", Path("/tmp/model-test"), False)

    def test_model_download_rejects_source_tree(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the source tree"):
            pinned_download("owner/model", "a" * 40, Path(__file__).parent, False)

    def test_model_dry_run_does_not_write_or_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "new-model"
            with patch("scripts.setup_models.subprocess.run") as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pinned_download("owner/model", "a" * 40, target, False), 0)
                run.assert_not_called()
            self.assertFalse(target.exists())

    def test_model_inspect_is_explicit_about_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "config.json").write_text(json.dumps({"model_type": "fixture"}), encoding="utf-8")
            (target / "weights.safetensors").write_bytes(b"fixture")
            report = inspect_model(target)
            self.assertEqual(report["weight_bytes"], 7)
            self.assertIn("does not establish", str(report["provenance"]))


if __name__ == "__main__":
    unittest.main()
