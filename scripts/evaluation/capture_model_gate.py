"""Run the model-mode gate and keep every raw model output for later replay.

The gate report keeps the final answers, but the citation-marker rewrite inside
``call_local_llm`` has already run on them. Replaying a run through other code
(``scripts/evaluation/replay_model_gate.py``) needs what the model actually
returned, so this wrapper records it next to the report::

    uv run --locked python scripts/evaluation/capture_model_gate.py --out output/gate-run \\
        --endpoint http://127.0.0.1:8000/v1 --model ACTUAL_MODEL_ID

Written to ``--out``: ``model-calls.jsonl`` (one line per model call: question,
``raw_answer``, ``rewritten_answer`` when the rewrite changed it, usage, seconds
or the error), ``answers.jsonl`` (one line per question), ``report.json``,
``gate-summary.md`` and ``completion.json`` with the gate's exit code.
Generation settings are fixed here, as in the registered model runs:
temperature 0.2, at most 700 tokens, 180 s per call.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RECORDS = ("model-calls.jsonl", "answers.jsonl", "report.json", "completion.json")
SETTINGS = {
    "LAW_REVIEW_LLM_TEMPERATURE": "0.2",
    "LAW_REVIEW_LLM_MAX_TOKENS": "700",
    "LAW_REVIEW_LLM_TIMEOUT": "180",
    "LAW_REVIEW_LLM_API": "openai-completions",
}


class Recorder:
    """Wraps the chat turn, the model call and the marker rewrite for one gate run.

    The chat path looks ``call_local_llm`` up in ``app.review.chat`` and the
    rewrite up in ``app.review.generation``, so those are the names replaced.
    """

    def __init__(self, out: Path, questions: Iterator[dict[str, Any]]) -> None:
        self.out = out
        self.questions = questions
        self.current: dict[str, Any] = {}
        self._restore: list[Callable[[], None]] = []

    def _append(self, name: str, value: dict[str, Any]) -> None:
        with (self.out / name).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")

    def _replace(self, module: Any, name: str, wrapper: Callable[..., Any]) -> None:
        original = getattr(module, name)
        setattr(module, name, wrapper)
        self._restore.append(lambda: setattr(module, name, original))

    def install(self) -> None:
        from app.review import chat as chat_turn
        from app.review import generation

        chat, model, rewrite = chat_turn.chat, chat_turn.call_local_llm, generation.canonical_source_markers
        self._replace(chat_turn, "chat", lambda *a, **k: self._chat(chat, *a, **k))
        self._replace(chat_turn, "call_local_llm", lambda *a, **k: self._model(model, generation, *a, **k))
        self._replace(generation, "canonical_source_markers", lambda answer, contexts: self._rewrite(rewrite, answer, contexts))

    def uninstall(self) -> None:
        while self._restore:
            self._restore.pop()()

    def _rewrite(self, original: Callable[..., str], answer: str, contexts: list[dict[str, Any]]) -> str:
        self.current["model_text"] = answer
        return original(answer, contexts)

    def _model(self, original: Callable[..., str], generation: Any, *args: Any, **kwargs: Any) -> str:
        started = time.perf_counter()
        record: dict[str, Any] = {"id": self.current.get("id"),
                                  "question": args[0] if args else kwargs.get("question")}
        self.current.pop("model_text", None)
        try:
            answer = original(*args, **kwargs)
            record["raw_answer"] = self.current.get("model_text", answer)
            if record["raw_answer"] != answer:
                record["rewritten_answer"] = answer
            record["usage"] = generation.last_llm_usage()
            return answer
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["seconds"] = round(time.perf_counter() - started, 3)
            self._append("model-calls.jsonl", record)

    def _chat(self, original: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
        item = next(self.questions)
        self.current.clear()
        self.current.update(item)
        started = time.perf_counter()
        record: dict[str, Any] = {"id": item["id"], "question": item["question"],
                                  "started_at": datetime.now(UTC).isoformat()}
        print(f"START {item['id']}", flush=True)
        try:
            result = original(*args, **kwargs)
            record["result"] = result
            return result
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["seconds"] = round(time.perf_counter() - started, 3)
            self._append("answers.jsonl", record)
            status = record.get("result", {}).get("answer_status", record.get("error"))
            print(f"DONE {item['id']} {record['seconds']}s status={status}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True,
                        help="output directory; refused if it already holds a report or model calls")
    parser.add_argument("--endpoint", required=True, help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8000/v1")
    parser.add_argument("--model", required=True, help="model id exactly as the server lists it")
    parser.add_argument("--baseline", type=Path, default=None,
                        help="compare against this baseline instead of the registered one "
                             "(a path that does not exist only writes the report)")
    args = parser.parse_args(argv)
    existing = [name for name in RECORDS if (args.out / name).exists()]
    if existing:
        print(f"{args.out} 里已有 {'、'.join(existing)}，不会覆盖或追加到已有记录", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    for key in [key for key in os.environ if key.startswith("LAW_REVIEW_")]:
        del os.environ[key]
    os.environ.update({**SETTINGS, "LAW_REVIEW_DATA_DIR": str(args.out / "isolated-defaults"),
                       "LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB": str(args.out / "isolated-defaults/checkpoints.sqlite")})

    from app.evaluation import eval_gate
    from scripts.evaluation.eval_gate import main as gate_main

    dataset = json.loads(eval_gate.DEFAULT_DATASET.read_text(encoding="utf-8"))
    recorder = Recorder(args.out, iter(dataset["items"]))
    recorder.install()
    started = time.perf_counter()
    try:
        extra = ["--baseline", str(args.baseline)] if args.baseline else []
        code = gate_main(["--mode", "model", "--endpoint", args.endpoint, "--model", args.model,
                          "--report", str(args.out / "report.json"),
                          "--markdown", str(args.out / "gate-summary.md"), *extra])
    except BaseException:
        traceback.print_exc()
        code = 99
    finally:
        recorder.uninstall()
    (args.out / "completion.json").write_text(json.dumps({
        "exit_code": code, "seconds": round(time.perf_counter() - started, 3),
        "completed_at": datetime.now(UTC).isoformat(),
    }, indent=2) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
