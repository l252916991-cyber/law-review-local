"""Record the identity a demo recording must bind to, without starting a model or a server.

The report covers what the P3 checklist asks for: loaded code path, commit and
working-tree state, prompt/policy versions, frontend build binding, model
endpoint probe and synthetic source digests. It never records case content and
never claims that a model answered anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

BACKEND_FILES = (
    "app/main.py", "app/review/chat.py", "app/review/generation.py", "app/review/agents.py", "app/review/review_collaboration.py",
    "app/evaluation/collaboration_ablation.py", "app/evaluation/ablation_analysis.py", "app/evaluation/answer_eval.py",
)
FRONTEND_BINDING = Path("frontend/out/build-info.json")
EXIT_PROTOCOL = 2


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def git_state(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    commit = run("rev-parse", "HEAD")
    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    status = run("status", "--porcelain")
    return {"commit": commit, "branch": branch,
            "dirty": None if status is None else bool(status),
            "dirty_paths": [] if not status else sorted(
                line[3:] for line in status.splitlines() if line.strip())}


def frontend_binding(root: Path) -> dict[str, Any]:
    path = root / FRONTEND_BINDING
    if not path.is_file():
        return {"present": False, "path": str(FRONTEND_BINDING),
                "note": "未构建前端静态产物；录制前必须补建并登记摘要"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    assets = payload.get("assets") or []
    return {"present": True, "path": str(FRONTEND_BINDING), "format_version": payload.get("format_version"),
            "assets": len(assets), "sha256": sha256_file(path)}


def dataset_identity(root: Path) -> dict[str, Any]:
    from app.evaluation.collaboration_ablation import load_batch_dataset

    result: dict[str, Any] = {}
    for label, relative in (("dev", "benchmarks/answer_eval/v2/dataset.json"),
                            ("holdout", "benchmarks/answer_eval/holdout-v1/dataset.json")):
        path = root / relative
        if not path.is_file():
            result[label] = {"present": False, "path": relative}
            continue
        dataset = load_batch_dataset(path)
        result[label] = {"present": True, "path": relative, "name": dataset.get("name"),
                         "items": len(dataset["items"]), "sources": len(dataset["sources"]),
                         "source_sha256": dataset["source_sha256"],
                         "file_sha256": sha256_file(path)}
    return result


def build_report(root: Path) -> dict[str, Any]:
    import app.main as main_module
    from app.evaluation.answer_eval import JUDGE_PROMPT_VERSION
    from app.evaluation.collaboration_ablation import precheck
    from app.integration.model_domain import LOCAL_PROMPT_VERSION
    from app.review.answerability import POLICY_VERSION, policy_identity
    from app.review.generation import PROMPT_VERSION

    probe = precheck(None, None)
    return {
        "schema_version": 1,
        "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": {"platform": platform.platform(), "machine": platform.machine(),
                 "python": platform.python_version(), "executable": sys.executable},
        "git": git_state(root),
        "loaded_backend": {"module_file": str(Path(main_module.__file__).resolve()),
                           "root": str(root)},
        "backend_files": {name: sha256_file(root / name) for name in BACKEND_FILES if (root / name).is_file()},
        "identity": {"prompt_version": PROMPT_VERSION, "local_prompt_version": LOCAL_PROMPT_VERSION,
                     "policy_version": POLICY_VERSION, "answerability": policy_identity(),
                     "judge_prompt_version": JUDGE_PROMPT_VERSION},
        "model": probe,
        "frontend": frontend_binding(root),
        "datasets": dataset_identity(root),
        "limits": [
            "本报告只记录身份与环境，不包含任何案件正文或模型输出",
            "模型端点不可用时 available=false，不代表本次演示有模型响应",
            "前端未构建时 frontend.present=false，录制前必须补建并重新生成本报告",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="录制前身份核对报告")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    report = build_report(args.root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError:
        print(f"{args.output} 已存在；换新路径，不覆盖旧身份记录", file=sys.stderr)
        return EXIT_PROTOCOL
    print(json.dumps({"output": str(args.output), "commit": report["git"]["commit"],
                      "dirty": report["git"]["dirty"], "model_available": report["model"]["available"],
                      "frontend_present": report["frontend"]["present"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
