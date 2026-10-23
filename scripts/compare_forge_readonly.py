"""Run the authorized, offline M2 comparison; save a private report explicitly.

Use the host's local authentication context. Token-mode deployments must call
the Python interface from an authenticated host, rather than pass identities
or credentials on a command line. This command never enables a product route.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.forge_bridge import framework_binding, run_framework
from app.forge_readonly import ReadonlyCaseSession
from app.runtime_adapter import current_actor_identity
from app.runtime_ports import ModelConfig, RunRequest, RunScope, new_request_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True, type=int)
    parser.add_argument("--question", required=True)
    parser.add_argument("--framework-python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("report already exists; choose a new private output path")
    request = RunRequest(request_id=new_request_id(), scope=RunScope(args.case_id),
                         actor=current_actor_identity(), question=args.question,
                         runtime="forgeharness-readonly", framework_version=framework_binding()[0]["version_label"],
                         model=ModelConfig(use_llm=False), use_remote_embeddings=False,
                         persist_memory=False)
    with ReadonlyCaseSession(request) as session:
        report = run_framework(session, args.framework_python)
        report["independent_comparison"] = session.compare_native(shared_inputs=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"status": report["status"], "comparison": report["comparison"],
                      "report": str(args.output)}))
    return 0 if (report["status"] == "succeeded" and report["comparison"]["passed"]
                 and report["independent_comparison"]["passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
