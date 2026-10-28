"""No-network tests for release/model tooling, using only synthetic temporary files."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.package_release import source_files
from scripts.migration_demo import package_contents, run as migration_demo
from scripts.setup_models import inspect_model, pinned_download


class EngineeringToolTests(unittest.TestCase):
    def test_migration_demo_never_reuses_existing_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            marker = target / "law_review.db"
            marker.write_bytes(b"existing synthetic database")
            with patch("scripts.migration_demo.subprocess.check_output") as execute:
                with self.assertRaisesRegex(ValueError, "new directory"):
                    migration_demo(target)
                execute.assert_not_called()
            self.assertEqual(marker.read_bytes(), b"existing synthetic database")
            self.assertEqual(list(target.iterdir()), [marker])

    def test_migration_delivery_checks_identity_and_detects_corrupt_members(self) -> None:
        def fixture(*, tampered: bool = False, artifact: bool = False) -> bytes:
            stream = io.BytesIO()
            item = {"archive_name": "正文.txt", "sha256": hashlib.sha256(b"synthetic").hexdigest()}
            manifest = {"finalized": True, "report": {"id": 7, "content_hash": "body", "frozen_version_id": 9},
                        "frozen": {"content_hash": "body", "version_id": 9},
                        "files": [] if artifact else [item], "artifacts": [item] if artifact else []}
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("清单.json", json.dumps(manifest))
                archive.writestr("正文.txt", b"corrupt" if tampered else b"synthetic")
            return stream.getvalue()

        for artifact in (False, True):
            with self.subTest(artifact=artifact):
                self.assertEqual(package_contents(fixture(artifact=artifact), 7, "body", 9),
                                 {"正文.txt": hashlib.sha256(b"synthetic").hexdigest()})
                with self.assertRaisesRegex(ValueError, "digest mismatch"):
                    package_contents(fixture(tampered=True, artifact=artifact), 7, "body", 9)
        for report, content_hash, version, error in ((8, "body", 9, "report mismatch"),
                                                    (7, "different", 9, "content hash mismatch"),
                                                    (7, "body", 10, "version mismatch")):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                package_contents(fixture(), report, content_hash, version)

    def test_release_excludes_private_files_and_backups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "README.md", "app/main.py", "app/main.py.backup", "models/config.json",
                "data/case.txt", "output/result.json", ".env", ".env.example",
                "Dockerfile", "docker-compose.yml", ".dockerignore", "LICENSE",
                "benchmarks/fewshot/2-2_sft_pool.jsonl", "benchmarks/fewshot/private.jsonl",
                "benchmarks/lawbench/zero_shot/upstream.json",
                "docs/INTERVIEW_GUIDE.md", "docs/runbook/operations.md",
            ):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic fixture", encoding="utf-8")
            names = {str(path.relative_to(root)) for path in source_files(root)}
            self.assertEqual(names, {
                "README.md", "app/main.py", ".env.example", "docs/runbook/operations.md",
                "Dockerfile", "docker-compose.yml", ".dockerignore", "LICENSE",
                "benchmarks/fewshot/2-2_sft_pool.jsonl",
            })

    def test_release_refuses_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app").mkdir()
            secret = root / "private.txt"
            secret.write_text("synthetic private content", encoding="utf-8")
            (root / "app" / "leak.py").symlink_to(secret)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                source_files(root)

    def test_frontend_build_requires_exact_public_digest_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "frontend/out"
            public.mkdir(parents=True)
            index = public / "index.html"
            index.write_text("synthetic UI")
            binding = public / "build-info.json"
            binding.write_text(json.dumps({"format_version": 1, "assets": [
                {"path": "index.html", "sha256": hashlib.sha256(index.read_bytes()).hexdigest()},
            ]}))
            (public / "private.json").write_text("not a build output")
            self.assertEqual(source_files(root), [])
            self.assertEqual(set(source_files(root, with_frontend=True)), {index, binding})
            index.write_text("tampered")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                source_files(root, with_frontend=True)

    def test_frontend_build_binding_rejects_private_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            public = root / "frontend/out"
            public.mkdir(parents=True)
            for name in ("../private.js", "/tmp/private.js", ".env", "_next/private.json"):
                (public / "build-info.json").write_text(json.dumps({"format_version": 1, "assets": [
                    {"path": name, "sha256": "0" * 64},
                ]}))
                with self.subTest(path=name), self.assertRaises(ValueError):
                    source_files(root, with_frontend=True)

    def test_frontend_release_excludes_dependencies_builds_and_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = {"frontend/package.json", "frontend/pnpm-lock.yaml", "frontend/src/app/page.tsx",
                        "frontend/scripts/contract.mjs", "frontend/tests/stream.test.ts"}
            excluded = {"frontend/.env.local", "frontend/node_modules/private.js", "frontend/.next/server.js",
                        "frontend/test-results/case.json", "frontend/src/private.json"}
            for name in expected | excluded:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic fixture", encoding="utf-8")
            self.assertEqual({str(p.relative_to(root)) for p in source_files(root)}, expected)

    def test_lawbench_requires_explicit_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "benchmarks/lawbench/zero_shot/fixture.json"
            fixture.parent.mkdir(parents=True)
            fixture.write_text("[]", encoding="utf-8")
            self.assertEqual(source_files(root), [])
            self.assertEqual(source_files(root, True), [fixture])

    def test_actual_release_can_be_extracted_and_compiled(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "release.zip"
            subprocess.run([sys.executable, str(root / "scripts/package_release.py"),
                            "--output", str(output)], check=True, capture_output=True)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                self.assertTrue({"Dockerfile", "docker-compose.yml", ".dockerignore", "run.sh",
                                 "benchmarks/answer_eval/v1/demo-case.json",
                                 "benchmarks/fewshot/2-2_sft_pool.jsonl"} <= names)
                self.assertFalse(any(name.startswith(("data/", "output/", "models/", "benchmarks/lawbench/"))
                                     for name in names))
                manifest = json.loads(archive.read("RELEASE_MANIFEST.json"))
                self.assertEqual({item["path"] for item in manifest["files"]}, names - {"RELEASE_MANIFEST.json"})
                for item in manifest["files"]:
                    self.assertEqual(hashlib.sha256(archive.read(item["path"])).hexdigest(), item["sha256"])
                archive.extractall(directory)
            subprocess.run([sys.executable, "-m", "compileall", "-q", "app", "scripts"],
                           cwd=directory, check=True, capture_output=True)

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
