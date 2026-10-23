"""Inspect existing model files, or explicitly download one pinned Hugging Face revision."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def inspect_model(path: Path) -> dict[str, object]:
    path = path.expanduser().resolve(strict=True)
    config = path / "config.json"
    if not config.is_file():
        raise ValueError("Model directory must contain config.json")
    payload = json.loads(config.read_text(encoding="utf-8"))
    weights = sorted(path.glob("*.safetensors"))
    return {
        "path": str(path),
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "model_type": payload.get("model_type"),
        "quantization": payload.get("quantization", payload.get("quantization_config")),
        "weight_files": len(weights),
        "weight_bytes": sum(weight.stat().st_size for weight in weights),
        "symlink_weight_files": [weight.name for weight in weights if weight.is_symlink()],
        "tokenizer_present": (path / "tokenizer.json").is_file(),
        "provenance": "local inventory only; does not establish upstream revision or conversion reproducibility",
    }


def pinned_download(repo: str, revision: str, target: Path, execute: bool) -> int:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Use an explicit owner/model repository name")
    if not re.fullmatch(r"[a-fA-F0-9]{40}", revision):
        raise ValueError("Use the verified immutable 40-character commit SHA, not main/latest")
    target = target.expanduser().resolve()
    if target.is_relative_to(ROOT) or target == Path.home() or target == Path("/"):
        raise ValueError("Choose a dedicated model directory outside the source tree (not a home/root directory)")
    command = ["hf", "download", repo, "--revision", revision, "--local-dir", str(target)]
    print(json.dumps({"command": command, "execute": execute}, ensure_ascii=False, indent=2))
    if not execute:
        return 0
    if not shutil.which("hf"):
        raise RuntimeError("Install Hugging Face CLI in the separate model environment; no packages are auto-installed")
    identity = {"repo": repo, "revision": revision}
    marker = target / "lexvault-model-source.json"
    if target.exists() and any(target.iterdir()):
        if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != identity:
            raise ValueError("Target is non-empty without a matching source marker; existing models will not be overwritten")
    target.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
    completed = subprocess.run(command, check=False)
    if completed.returncode == 0:
        print(json.dumps(inspect_model(target), ensure_ascii=False, indent=2))
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="Read config and file sizes; no network")
    inspect_parser.add_argument("path", type=Path)
    download = subparsers.add_parser("download", help="Dry-run by default; never chooses another model")
    download.add_argument("--repo", required=True)
    download.add_argument("--revision", required=True)
    download.add_argument("--target", type=Path, required=True)
    download.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.action == "inspect":
        print(json.dumps(inspect_model(args.path), ensure_ascii=False, indent=2))
        return 0
    return pinned_download(args.repo, args.revision, args.target, args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
