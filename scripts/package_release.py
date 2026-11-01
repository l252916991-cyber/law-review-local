"""Build an allowlisted source archive without touching private runtime data."""
from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    "README.md", "README.en.md", "pyproject.toml", "uv.lock", "requirements.txt", "requirements-dev.txt",
    ".gitignore", ".env.example", ".pre-commit-config.yaml", ".gitleaksignore",
    ".dockerignore", "Dockerfile", "docker-compose.yml", "AGENTS.md", "LICENSE",
    "run.sh", "run_worker.sh",
    "unified_benchmark_runner.py", "verify_benchmark_run.py", "compare_agent_runtimes.py",
    "rag_project_benchmark.py", "BENCHMARK_TEST_PLAN.md",
)
EXACT_FIXTURES = (
    "frontend/package.json", "frontend/pnpm-lock.yaml", "frontend/pnpm-workspace.yaml",
    "frontend/.node-version", "frontend/.npmrc", "frontend/tsconfig.json",
    "frontend/next-env.d.ts", "frontend/next.config.ts", "frontend/eslint.config.mjs",
    "frontend/playwright.config.ts", "frontend/.prettierignore",
    "benchmarks/fewshot/2-2_sft_pool.jsonl",
    "benchmarks/answer_eval/v1/demo-case.json",
    "benchmarks/answer_eval/README.md",
    "benchmarks/answer_eval/v2/questions.json",
    "benchmarks/answer_eval/v2/dataset.json",
    "benchmarks/answer_eval/v2/manifest.json",
    # Committed answer-quality gate baseline; tests/test_eval_gate.py needs it.
    "benchmarks/eval_gate/baseline-rules.json",
    # Six-case holdout: authored synthetic fixture plus its frozen dataset and
    # manifest. Needed to rebuild the holdout batch from the package alone;
    # contains no real parties, amounts or uploaded material.
    "benchmarks/answer_eval/holdout-v1/cases.json",
    "benchmarks/answer_eval/holdout-v1/dataset.json",
    "benchmarks/answer_eval/holdout-v1/manifest.json",
    # Frozen experiment configuration (non-sensitive test fixture input);
    # tests/test_statutory_experiment.py fails closed without it. Restored
    # 2026-10-21 from the public origin repository, see docs/history/migration/ledger.md.
    "benchmarks/statutory-experiment-v1.json",
    # B3 delivery path (status-and-improvement-plan-20261021.md): the exact
    # locked framework wheel plus its vetted binding record ship inside the
    # release archive, so the deployment binding can be rebuilt and digest-
    # verified from the package alone (scripts/verify_deployment_release.py).
    # Exact files only - never the whole vendor/ directory, never private data.
    "vendor/FRAMEWORK_BINDING.json",
    "vendor/forgeharness-0.8.3-py3-none-any.whl",
    "vendor/forgeharness-runtime-requirements.txt",
)
TREE_TYPES = {
    "frontend/src": {".ts", ".tsx", ".css", ".svg"},
    "frontend/scripts": {".mjs"},
    "frontend/tests": {".ts"},
    "frontend/e2e": {".ts"},
    "app": {".py", ".html", ".js", ".css", ".svg", ".sql"},
    "tests": {".py"},
    "scripts": {".py", ".json"},
    "docs": {".md", ".json"},
    ".github/workflows": {".yml", ".yaml"},
}


def source_files(root: Path, with_benchmarks: bool = False, with_frontend: bool = False) -> list[Path]:
    files = {root / name for name in ROOT_FILES if (root / name).is_file()}
    files.update(root / name for name in EXACT_FIXTURES if (root / name).is_file())
    for directory, suffixes in TREE_TYPES.items():
        for candidate in (root / directory).rglob("*"):
            relative = candidate.relative_to(root)
            if relative.as_posix() == "docs/INTERVIEW_GUIDE.md":
                continue
            if candidate.suffix not in suffixes or "__pycache__" in relative.parts:
                continue
            if candidate.is_file():
                files.add(candidate)
    if with_benchmarks:
        # Explicit opt-in: license/source notice and exact upstream test set, never case uploads.
        files.update(p for p in (root / "benchmarks" / "lawbench").rglob("*") if p.is_file())
    if with_frontend:
        public = root / "frontend/out"
        binding = public / "build-info.json"
        payload = json.loads(binding.read_text(encoding="utf-8"))
        entries = payload.get("assets")
        if payload.get("format_version") != 1 or not isinstance(entries, list) or not entries:
            raise ValueError("Missing frontend asset binding; rebuild with pnpm build:static")
        seen: set[str] = set()
        for entry in entries:
            name = entry["path"]
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or name in seen:
                raise ValueError("Invalid frontend asset path")
            if name != "index.html" and not (name.startswith("_next/") and relative.suffix.lower() in
                    {".js", ".css", ".woff", ".woff2", ".svg", ".png", ".jpg", ".webp", ".ico"}):
                raise ValueError("Unexpected frontend build file")
            candidate = public / relative
            if any(parent.is_symlink() for parent in (candidate, *candidate.parents) if parent.is_relative_to(public)) or not candidate.resolve().is_relative_to(public.resolve()):
                raise ValueError("Refusing frontend symlink escape")
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("Frontend build digest mismatch")
            files.add(candidate)
            seen.add(name)
        if "index.html" not in seen:
            raise ValueError("Frontend build has no index")
        files.add(binding)
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Refusing symbolic link or path outside source root: {path}")
    return sorted(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "lexvault-source.zip")
    parser.add_argument("--with-benchmarks", action="store_true", help="Include LawBench; review its source licenses first")
    parser.add_argument("--with-frontend", action="store_true", help="Include only digest-bound public frontend build assets")
    parser.add_argument("--dry-run", action="store_true", help="Print archive members without writing")
    args = parser.parse_args(argv)
    files = source_files(ROOT, args.with_benchmarks, args.with_frontend)
    manifest = {
        "format_version": 1,
        "private_runtime_data_included": False,
        "lawbench_included": args.with_benchmarks,
        "frontend_build_included": args.with_frontend,
        "files": [
            {"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in files
        ],
    }
    if args.dry_run:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive mode makes repeated invocation safe: never overwrite a previous release.
    with zipfile.ZipFile(output, mode="x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, str(path.relative_to(ROOT)))
        archive.writestr("RELEASE_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"Created {output} ({len(files)} source files; no models, cases, credentials or raw reports)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
