"""Release gate: a change to prompts, retrieval or the model must not lower answer quality.

The gate replays the frozen v2 answer set (60 questions on the seeded demo case)
through the production ``chat`` path, scores each answer with the deterministic
v2 rules and compares the result with a committed baseline.

Two modes:

* ``rules``: no model; retrieval, routing, answerability and citation cards are
  exercised end to end. Deterministic, so CI runs it on every push and any
  question that used to pass and now fails blocks the change.
* ``model``: a local OpenAI-compatible endpoint answers. Sampling makes single
  questions noisy, so the gate allows a small, pre-registered drop in passed
  questions but still fails on latency regressions or new forbidden claims.

A baseline is only comparable when the dataset and mode match. Every run records
an inference manifest (prompt versions, system-prompt digest, digests of the
retrieval and answering modules, model settings, database backend) so a report
always says which configuration produced it.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .answer_eval import load_dataset, score_answer_v2, summarise

GATE_VERSION = "eval-gate-v1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "benchmarks" / "answer_eval" / "v2" / "dataset.json"
DEFAULT_BASELINES = {
    "rules": ROOT / "benchmarks" / "eval_gate" / "baseline-rules.json",
    "model": ROOT / "benchmarks" / "eval_gate" / "baseline-model.json",
}
#: Modules whose code decides retrieval, routing, prompting, answerability and
#: citation cards. A digest change means "the inference path changed".
INFERENCE_MODULES = (
    "agents.py", "answerability.py", "context.py", "model_domain.py", "rag.py",
    "rag_child_index.py", "review_collaboration.py", "search_backend.py", "services.py",
    "statutory_retrieval.py",
)
#: Pre-registered tolerances. Rules mode is deterministic: nothing may drop.
#: Model mode tolerates sampling noise of up to two questions out of sixty.
THRESHOLDS: dict[str, dict[str, float]] = {
    "rules": {"max_passed_drop": 0, "max_refusal_drop": 0, "max_violation_increase": 0,
              "max_required_pages_drop": 0, "max_p95_ratio": 0.0},
    "model": {"max_passed_drop": 2, "max_refusal_drop": 1, "max_violation_increase": 0,
              "max_required_pages_drop": 2, "max_p95_ratio": 1.25},
}
#: Latency below this is dominated by scheduling noise; the ratio check only
#: applies when the baseline p95 is at least this slow.
LATENCY_FLOOR_MS = 200.0


class GateConfigurationError(ValueError):
    """The run and the baseline cannot be compared."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inference_manifest(mode: str, *, dataset_path: Path, model: str | None = None,
                       endpoint: str | None = None, backend: str = "sqlite") -> dict[str, Any]:
    from .answerability import POLICY_VERSION
    from .context import CONTEXT_POLICY_VERSION
    from .model_domain import LOCAL_PROMPT_VERSION
    from .services import CHAT_SYSTEM_PROMPT, PROMPT_VERSION

    here = Path(__file__).resolve().parent
    manifest: dict[str, Any] = {
        "gate_version": GATE_VERSION,
        "mode": mode,
        "dataset": {"path": dataset_path.relative_to(ROOT).as_posix() if dataset_path.is_relative_to(ROOT)
                    else dataset_path.name, "sha256": _sha256(dataset_path.read_bytes())},
        "prompt_version": PROMPT_VERSION,
        "local_prompt_version": LOCAL_PROMPT_VERSION,
        "answerability_policy": POLICY_VERSION,
        "context_policy": CONTEXT_POLICY_VERSION,
        "system_prompt_sha256": _sha256(CHAT_SYSTEM_PROMPT.encode("utf-8")),
        "modules": {name: _sha256((here / name).read_bytes()) for name in INFERENCE_MODULES},
        "database_backend": backend,
        "embeddings": "hashed-local",
    }
    if mode == "model":
        manifest["model"] = {
            "name": model, "endpoint": endpoint,
            "temperature": os.getenv("LAW_REVIEW_LLM_TEMPERATURE", ""),
            "max_tokens": os.getenv("LAW_REVIEW_LLM_MAX_TOKENS", "700"),
        }
    identity = {key: value for key, value in manifest.items() if key != "gate_version"}
    manifest["inference_id"] = _sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode())[:16]
    return manifest


@contextmanager
def _database(directory: Path, postgres_admin_url: str | None) -> Iterator[None]:
    """A throwaway SQLite file, or a throwaway PostgreSQL database."""
    from .db import db_scope, postgres_scope

    if not postgres_admin_url:
        with db_scope(directory / "law_review.db"):
            yield
        return
    from .db_postgres import create_database, drop_database

    name = f"lexvault_gate_{os.getpid()}_{time.monotonic_ns()}"
    url = create_database(postgres_admin_url, name)
    try:
        with postgres_scope(url):
            yield
    finally:
        drop_database(postgres_admin_url, name)


@contextmanager
def _isolated_case(directory: Path, *, endpoint: str, model: str,
                   postgres_admin_url: str | None = None) -> Iterator[None]:
    """A throwaway database holding only the seeded demo case."""
    from .db import init_db

    values = {
        "LAW_REVIEW_DATA_DIR": str(directory),
        "LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB": str(directory / "checkpoints.sqlite"),
        "LAW_REVIEW_AUTH_MODE": "local", "LAW_REVIEW_ALLOW_REMOTE_MODELS": "off",
        "LAW_REVIEW_LLM_URL": endpoint, "LAW_REVIEW_LLM_MODEL": model,
        "LAW_REVIEW_LLM_MAX_TOKENS": os.getenv("LAW_REVIEW_LLM_MAX_TOKENS", "700"),
        "LAW_REVIEW_TOOL_LOOP": "off", "LAW_REVIEW_LLM_FALLBACK": "off",
        "REDIS_URL": "redis://127.0.0.1:1/0",
    }
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        with _database(directory, postgres_admin_url):
            init_db(seed=True)
            yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def collect(dataset: dict[str, Any], directory: Path, *, mode: str, endpoint: str | None = None,
            model: str | None = None, postgres_admin_url: str | None = None) -> dict[str, dict[str, Any]]:
    """Ask every question once through ``chat`` and keep what scoring needs."""
    from .services import chat

    use_llm = mode == "model"
    answers: dict[str, dict[str, Any]] = {}
    with _isolated_case(directory, endpoint=endpoint or "http://127.0.0.1:1/v1", model=model or "no-model",
                        postgres_admin_url=postgres_admin_url):
        for item in dataset["items"]:
            started = time.perf_counter()
            try:
                result = chat(1, item["question"], "评测门禁", None, use_llm, False)
            except RuntimeError as exc:
                if not use_llm:
                    raise
                # A model failure is a failed question, never a skipped one.
                result = {"answer": "", "citations": [], "answer_status": "model_error",
                          "llm_used": False, "error": type(exc).__name__}
            elapsed = (time.perf_counter() - started) * 1000
            answers[item["id"]] = {
                "answer": result.get("answer") or "",
                "citations": result.get("citations") or [],
                "answer_status": result.get("answer_status"),
                "llm_used": bool(result.get("llm_used")),
                "latency_ms": round(elapsed, 2),
            }
    return answers


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 2)


def evaluate(dataset: dict[str, Any], answers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for item in dataset["items"]:
        answer = answers[item["id"]]
        score = score_answer_v2(item, answer["answer"], citations=answer["citations"],
                                answer_status=answer["answer_status"])
        score["category"] = item.get("category")
        score["latency_ms"] = answer["latency_ms"]
        rows.append(score)
    summary = summarise(rows)
    answerable = [row for row in rows if not row["refusal_expected"]]
    latencies = [row["latency_ms"] for row in rows]
    summary.update(
        required_pages_cited=sum(1 for row in answerable if row["required_pages_cited"]),
        latency_p50_ms=_percentile(latencies, 0.5),
        latency_p95_ms=_percentile(latencies, 0.95),
        llm_used=sum(1 for answer in answers.values() if answer.get("llm_used")),
    )
    return {"summary": summary, "items": {row["id"]: row for row in rows}}


def compare(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Decide pass/fail against the baseline; every failure says why."""
    mode = current["manifest"]["mode"]
    if baseline["manifest"]["mode"] != mode:
        raise GateConfigurationError(f"基线模式为 {baseline['manifest']['mode']}，本次为 {mode}")
    if baseline["manifest"]["dataset"]["sha256"] != current["manifest"]["dataset"]["sha256"]:
        raise GateConfigurationError("评测集与基线不一致；换评测集需要重新登记基线")
    limits = THRESHOLDS[mode]
    now, then = current["summary"], baseline["summary"]
    failures: list[str] = []

    def drop(metric: str, limit_key: str, label: str) -> None:
        delta = then[metric] - now[metric]
        if delta > limits[limit_key]:
            failures.append(f"{label}从 {then[metric]} 降到 {now[metric]}（允许下降 {int(limits[limit_key])}）")

    drop("passed", "max_passed_drop", "通过题数")
    drop("refusal_correct", "max_refusal_drop", "应拒答题的正确拒答数")
    drop("required_pages_cited", "max_required_pages_drop", "引用到全部必需页的题数")
    increase = now["violation_count"] - then["violation_count"]
    if increase > limits["max_violation_increase"]:
        failures.append(f"禁止断言从 {then['violation_count']} 增加到 {now['violation_count']}")
    ratio_limit = limits["max_p95_ratio"]
    if ratio_limit and then["latency_p95_ms"] >= LATENCY_FLOOR_MS:
        if now["latency_p95_ms"] > then["latency_p95_ms"] * ratio_limit:
            failures.append(f"p95 时延从 {then['latency_p95_ms']} ms 升到 {now['latency_p95_ms']} ms"
                            f"（允许 {ratio_limit} 倍）")

    regressions = sorted(key for key, row in current["items"].items()
                         if not row["passed"] and baseline["items"].get(key, {}).get("passed"))
    improvements = sorted(key for key, row in current["items"].items()
                          if row["passed"] and not baseline["items"].get(key, {}).get("passed"))
    if mode == "rules" and regressions and not any(text.startswith("通过题数") for text in failures):
        failures.append(f"{len(regressions)} 道原本通过的题现在失败")
    same_inference = current["manifest"]["inference_id"] == baseline["manifest"]["inference_id"]
    if mode == "rules" and same_inference and (regressions or improvements):
        failures.append("推理配置与基线相同，结果却不同：离线路径出现了不确定性")
    return {"passed": not failures, "failures": failures, "regressions": regressions,
            "improvements": improvements, "same_inference": same_inference,
            "thresholds": limits}


def run(mode: str = "rules", *, dataset_path: Path = DEFAULT_DATASET, workdir: Path,
        endpoint: str | None = None, model: str | None = None,
        postgres_admin_url: str | None = None) -> dict[str, Any]:
    dataset = load_dataset(dataset_path)
    answers = collect(dataset, workdir, mode=mode, endpoint=endpoint, model=model,
                      postgres_admin_url=postgres_admin_url)
    if mode == "model" and not any(answer["llm_used"] for answer in answers.values()):
        raise GateConfigurationError("模型模式下没有任何一题实际调用了模型；检查端点与模型名")
    report = evaluate(dataset, answers)
    report["manifest"] = inference_manifest(mode, dataset_path=dataset_path, model=model, endpoint=endpoint,
                                            backend="postgresql" if postgres_admin_url else "sqlite")
    report["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return report


def baseline_from(report: dict[str, Any], *, reason: str, previous: dict[str, Any] | None) -> dict[str, Any]:
    if not reason.strip():
        raise GateConfigurationError("登记新基线必须写明原因")
    history = list((previous or {}).get("history", []))
    if previous:
        history.append({"inference_id": previous["manifest"]["inference_id"],
                        "registered_at": previous.get("registered_at"),
                        "reason": previous.get("reason"), "summary": previous["summary"]})
    return {
        "manifest": report["manifest"], "summary": report["summary"],
        "items": {key: {"passed": row["passed"]} for key, row in report["items"].items()},
        "registered_at": report["generated_at"], "reason": reason.strip(), "history": history,
    }


def render_markdown(report: dict[str, Any], verdict: dict[str, Any] | None, baseline: dict[str, Any] | None) -> str:
    summary = report["summary"]
    manifest = report["manifest"]
    lines = [f"## 评测门禁（{manifest['mode']}）：" + ("通过" if verdict is None or verdict["passed"] else "未通过"), ""]
    lines.append(f"推理版本 `{manifest['inference_id']}`，数据库 {manifest['database_backend']}，"
                 f"评测集 {summary['queries']} 题。")
    lines += ["", "| 指标 | 本次 | 基线 |", "| --- | --- | --- |"]
    rows = [("通过题数", "passed"), ("引用到全部必需页", "required_pages_cited"),
            ("正确拒答", "refusal_correct"), ("禁止断言", "violation_count"),
            ("p50 时延 ms", "latency_p50_ms"), ("p95 时延 ms", "latency_p95_ms")]
    for label, key in rows:
        before = baseline["summary"].get(key, "") if baseline else ""
        lines.append(f"| {label} | {summary[key]} | {before} |")
    if verdict:
        if verdict["failures"]:
            lines += ["", "**阻断原因**", ""] + [f"- {text}" for text in verdict["failures"]]
        if verdict["regressions"]:
            lines += ["", "原本通过、现在失败：" + "、".join(f"`{key}`" for key in verdict["regressions"])]
        if verdict["improvements"]:
            lines += ["", "原本失败、现在通过：" + "、".join(f"`{key}`" for key in verdict["improvements"])
                      + "（确认后可登记为新基线）"]
    return "\n".join(lines) + "\n"
