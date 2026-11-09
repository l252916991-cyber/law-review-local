"""Install the reviewed framework wheel into a new, separate Python 3.12 venv."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def setup(destination: Path, *, offline: bool = False) -> Path:
    from app.integration.forge_bridge import framework_binding

    binding, wheel = framework_binding()
    requirements = ROOT / "vendor/forgeharness-runtime-requirements.txt"
    if hashlib.sha256(requirements.read_bytes()).hexdigest() != binding["runtime_requirements_sha256"]:
        raise ValueError("framework runtime requirements digest mismatch")
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("framework environment already exists; select a new directory")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")
    prefix = [uv, *(["--offline"] if offline else [])]
    subprocess.run([*prefix, "venv", "--python", "3.12", str(destination)], check=True)
    python = destination / "bin/python"
    subprocess.run([*prefix, "pip", "install", "--python", str(python), "--require-hashes",
                    "-r", str(requirements)], check=True)
    subprocess.run([*prefix, "pip", "install", "--python", str(python), "--no-deps", str(wheel)], check=True)
    subprocess.run([*prefix, "pip", "check", "--python", str(python)], check=True)
    return python


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    print(json.dumps({"framework_python": str(setup(args.output, offline=args.offline))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
