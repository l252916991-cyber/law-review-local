"""Replay a saved model-gate run through the current code, without the model.

A real model run (``output/eval-gate-model*/<run>/``) keeps every raw model
output in ``model-calls.jsonl``. This script serves those outputs from a local
fake endpoint, one per question, and runs the gate in model mode against it.
Everything after the model call (source-marker handling, the answer contract,
answerability, scoring) therefore runs on the current checkout, so a change to
that code can be checked on another model's real answers without the machine
that ran the model.

First replay with the code the run was made with: the result must match the
saved report question by question, otherwise the replay is not faithful and
the second replay proves nothing. Latency in the replayed report is not model
latency.

    uv run --locked python scripts/replay_model_gate.py RUN_DIR --report replay.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_recorded(run_dir: Path) -> dict[str, str]:
    """Question -> raw model output, from the run's ``model-calls.jsonl``."""
    recorded: dict[str, str] = {}
    for line in (run_dir / "model-calls.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        call = json.loads(line)
        if "raw_answer" in call:
            recorded[call["question"]] = call["raw_answer"]
    return recorded


def recorded_answer(recorded: dict[str, str], request: dict[str, Any]) -> str | None:
    """The saved output for the question in this chat request, if exactly one matches."""
    text = "\n".join(str(message.get("content")) for message in request.get("messages", [])) + "\n"
    hits = [question for question in recorded if f"原问题：{question}\n" in text]
    return recorded[hits[0]] if len(hits) == 1 else None


def serve(recorded: dict[str, str], model: str) -> tuple[ThreadingHTTPServer, list[str]]:
    unmatched: list[str] = []

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self._send(200, {"object": "list", "data": [{"id": model, "object": "model"}]})

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            answer = recorded_answer(recorded, request)
            if answer is None:
                unmatched.append(json.dumps(request.get("messages", []), ensure_ascii=False)[:200])
                self._send(500, {"error": "no recorded output for this question"})
                return
            self._send(200, {
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": answer}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            })

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, unmatched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path, help="directory holding report.json and model-calls.jsonl")
    parser.add_argument("--report", type=Path, required=True, help="where to write the replayed report")
    args = parser.parse_args(argv)

    original = json.loads((args.run_dir / "report.json").read_text(encoding="utf-8"))
    run_model = original["manifest"]["model"]
    server, unmatched = serve(load_recorded(args.run_dir), run_model["name"])
    for key in [key for key in os.environ if key.startswith("LAW_REVIEW_")]:
        del os.environ[key]
    os.environ.update({
        "LAW_REVIEW_LLM_TEMPERATURE": str(run_model.get("temperature", "")),
        "LAW_REVIEW_LLM_MAX_TOKENS": str(run_model.get("max_tokens", "700")),
        "LAW_REVIEW_LLM_TIMEOUT": "30", "LAW_REVIEW_LLM_API": "openai-completions",
    })
    from app import eval_gate

    try:
        with tempfile.TemporaryDirectory(prefix="lexvault-replay-") as work:
            report = eval_gate.run("model", workdir=Path(work), model=run_model["name"],
                                   endpoint=f"http://127.0.0.1:{server.server_port}/v1")
    finally:
        server.shutdown()
    report["replayed_from"] = {"run_dir": args.run_dir.name, "unmatched_model_calls": len(unmatched)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if original["manifest"].get("gate_version") != report["manifest"]["gate_version"]:
        print(f"注意：原报告由 {original['manifest'].get('gate_version')} 评分，回放用 "
              f"{report['manifest']['gate_version']}，下面的变化可能来自评分口径", file=sys.stderr)
    changed = sorted(key for key, row in report["items"].items()
                     if row["passed"] != original["items"][key]["passed"])
    print(f"原报告 {original['summary']['passed']}/{original['summary']['queries']}，"
          f"回放 {report['summary']['passed']}/{report['summary']['queries']}")
    for key in changed:
        print(f"  {key}: {'通过' if original['items'][key]['passed'] else '失败'} → "
              f"{'通过' if report['items'][key]['passed'] else '失败'}")
    if unmatched:
        print(f"有 {len(unmatched)} 次模型调用找不到保存的输出：本次代码改变了送给模型的问题", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
