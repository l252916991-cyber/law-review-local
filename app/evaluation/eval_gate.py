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
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.evaluation.answer_eval import (
    load_dataset,
    overreach_findings,
    score_answer_v2,
    score_answer_v3,
    score_answer_v4,
    summarise,
)

GATE_VERSION = "eval-gate-v3"
#: Which scorer each gate version used. A baseline is only comparable with a
#: run scored the same way; an older report can be re-scored from its answers.
GATE_SCORERS: dict[str, Callable[..., dict[str, Any]]] = {
    "eval-gate-v1": score_answer_v2,
    "eval-gate-v2": score_answer_v3,
    "eval-gate-v3": score_answer_v4,
}
from app.paths import APP_DIR, ROOT

DEFAULT_DATASET = ROOT / "benchmarks" / "answer_eval" / "v2" / "dataset.json"
DEFAULT_BASELINES = {
    "rules": ROOT / "benchmarks" / "eval_gate" / "baseline-rules.json",
    "model": ROOT / "benchmarks" / "eval_gate" / "baseline-model.json",
}
#: Modules whose code decides retrieval, routing, prompting, answerability and
#: citation cards. A digest change means "the inference path changed".
#: Paths are relative to the ``app`` package.
INFERENCE_MODULES = (
    "review/agents.py", "review/answerability.py", "review/context.py", "integration/model_domain.py",
    "retrieval/rag.py", "retrieval/rag_child_index.py", "review/review_collaboration.py",
    "retrieval/search_backend.py", "review/chat.py", "review/generation.py", "retrieval/search.py",
    "core/egress.py", "retrieval/statutory_retrieval.py",
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
    from app.integration.model_domain import LOCAL_PROMPT_VERSION
    from app.review.answerability import POLICY_VERSION
    from app.review.context import CONTEXT_POLICY_VERSION
    from app.review.generation import CHAT_SYSTEM_PROMPT, PROMPT_VERSION

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
        "modules": {name: _sha256((APP_DIR / name).read_bytes()) for name in INFERENCE_MODULES},
        "database_backend": backend,
        "embeddings": "hashed-local",
    }
    if mode == "model":
        manifest["model"] = {
            "name": model, "endpoint": endpoint,
            "temperature": os.getenv("LAW_REVIEW_LLM_TEMPERATURE", ""),
            "max_tokens": os.getenv("LAW_REVIEW_LLM_MAX_TOKENS", "700"),
        }
    manifest["inference_id"] = _inference_id(manifest)
    return manifest


def _inference_id(manifest: dict[str, Any]) -> str:
    identity = {key: value for key, value in manifest.items() if key not in ("gate_version", "inference_id")}
    return _sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode())[:16]


def check_report_matches_code(report: dict[str, Any], *, dataset_path: Path = DEFAULT_DATASET) -> None:
    """Refuse to register a saved report unless the current code would have produced it.

    The run's model settings (name, endpoint, sampling) are taken from the
    report; everything the code decides must match the current checkout.
    """
    missing = [key for key in ("summary", "items", "manifest", "generated_at") if key not in report]
    if missing:
        raise GateConfigurationError("报告缺少字段：" + "、".join(missing))
    manifest = report["manifest"]
    if manifest.get("gate_version") != GATE_VERSION:
        raise GateConfigurationError(f"报告由门禁 {manifest.get('gate_version')} 生成，当前为 {GATE_VERSION}，"
                                     "评分口径可能不同，需要重新运行")
    if manifest.get("inference_id") != _inference_id(manifest):
        raise GateConfigurationError("报告的推理版本与报告内容不符")
    mode = manifest.get("mode")
    if mode not in DEFAULT_BASELINES:
        raise GateConfigurationError(f"未知的门禁模式：{mode}")
    run_model = manifest.get("model") or {}
    if mode == "model" and not (run_model.get("name") and run_model.get("endpoint")):
        raise GateConfigurationError("模型模式的报告缺少模型名或端点")
    current = inference_manifest(mode, dataset_path=dataset_path, model=run_model.get("name"),
                                 endpoint=run_model.get("endpoint"),
                                 backend=manifest.get("database_backend", "sqlite"))
    if mode == "model":
        current["model"] = run_model
    differing = sorted(f"modules/{name}" for name, digest in current["modules"].items()
                       if manifest.get("modules", {}).get(name) != digest)
    differing += sorted(key for key in current
                        if key not in ("modules", "inference_id", "gate_version") and current[key] != manifest.get(key))
    if differing:
        raise GateConfigurationError("报告与当前代码的推理配置不同（" + "、".join(differing) + "），需要用当前代码重新运行")
    if len(report["items"]) != report["summary"].get("queries"):
        raise GateConfigurationError("报告的逐题结果与汇总题数不一致")


@contextmanager
def _database(directory: Path, postgres_admin_url: str | None) -> Iterator[None]:
    """A throwaway SQLite file, or a throwaway PostgreSQL database."""
    from app.storage.db import db_scope, postgres_scope

    if not postgres_admin_url:
        with db_scope(directory / "law_review.db"):
            yield
        return
    from app.storage.db_postgres import create_database, drop_database

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
    from app.storage.db import init_db

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
    from app.review.chat import chat

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
            answers[item["id"]] = answer_record(result, (time.perf_counter() - started) * 1000)
    return answers


def answer_record(result: dict[str, Any], latency_ms: float) -> dict[str, Any]:
    """What scoring needs from one ``chat`` result; kept in the report for re-scoring."""
    return {
        "answer": result.get("answer") or "",
        "citations": result.get("citations") or [],
        "answer_status": result.get("answer_status"),
        "llm_used": bool(result.get("llm_used")),
        "latency_ms": round(latency_ms, 2),
    }


def model_error_record(latency_ms: float) -> dict[str, Any]:
    return answer_record({"answer": "", "citations": [], "answer_status": "model_error", "llm_used": False},
                         latency_ms)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 2)


def evaluate(dataset: dict[str, Any], answers: dict[str, dict[str, Any]],
             scorer: Callable[..., dict[str, Any]] = GATE_SCORERS[GATE_VERSION]) -> dict[str, Any]:
    rows = []
    for item in dataset["items"]:
        answer = answers[item["id"]]
        score = scorer(item, answer["answer"], citations=answer["citations"],
                       answer_status=answer["answer_status"])
        score["category"] = item.get("category")
        score["latency_ms"] = answer["latency_ms"]
        # Reported only: model answers that decide a legal question for the lawyer.
        score["overreach"] = overreach_findings(answer["answer"]) if answer.get("llm_used") else []
        rows.append(score)
    summary = summarise(rows)
    answerable = [row for row in rows if not row["refusal_expected"]]
    latencies = [row["latency_ms"] for row in rows]
    summary.update(
        required_pages_cited=sum(1 for row in answerable if row["required_pages_cited"]),
        latency_p50_ms=_percentile(latencies, 0.5),
        latency_p95_ms=_percentile(latencies, 0.95),
        llm_used=sum(1 for answer in answers.values() if answer.get("llm_used")),
        overreach_answers=sum(1 for row in rows if row["overreach"]),
    )
    return {"summary": summary, "items": {row["id"]: row for row in rows}}


def compare(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Decide pass/fail against the baseline; every failure says why."""
    mode = current["manifest"]["mode"]
    if baseline["manifest"].get("gate_version") != current["manifest"].get("gate_version"):
        raise GateConfigurationError(
            f"基线由门禁 {baseline['manifest'].get('gate_version')} 评分，本次为 {current['manifest'].get('gate_version')}；"
            "评分口径不同不能比较，需要重新登记基线（可用 --from-report 加保存的回答重新评分）")
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
    # Synthetic case only; kept so a later scoring change can re-score this run.
    report["answers"] = answers
    return report


def rescore(report: dict[str, Any], answers: dict[str, dict[str, Any]], *,
            dataset_path: Path = DEFAULT_DATASET) -> dict[str, Any]:
    """Score a saved run again with the current gate's scorer.

    The answers must first reproduce the report's original scores with the
    scorer that produced them; otherwise they are not this run's answers.
    """
    manifest = report.get("manifest") or {}
    original = GATE_SCORERS.get(manifest.get("gate_version", ""))
    if original is None:
        raise GateConfigurationError(f"不认识报告的门禁版本：{manifest.get('gate_version')}")
    dataset = load_dataset(dataset_path)
    if manifest.get("dataset", {}).get("sha256") != _sha256(dataset_path.read_bytes()):
        raise GateConfigurationError("评测集与报告不一致")
    missing = [item["id"] for item in dataset["items"] if item["id"] not in answers]
    if missing:
        raise GateConfigurationError("保存的回答缺少题目：" + "、".join(missing[:5]))
    answers = {key: {**answers[key], "latency_ms": report["items"][key]["latency_ms"]} for key in report["items"]}
    replay = evaluate(dataset, answers, scorer=original)
    fields = ("passed", "refused", "violations", "matched", "required_pages_cited", "extra_citations")
    differing = sorted(key for key, row in replay["items"].items()
                       if any(row[field] != report["items"][key].get(field) for field in fields))
    # Metrics added after the report was written are not part of the replay.
    if differing or any(replay["summary"].get(key) != value for key, value in report["summary"].items()):
        raise GateConfigurationError("保存的回答用原评分复现不出报告（" + "、".join(differing[:5]) + "），拒绝重新评分")
    current = evaluate(dataset, answers)
    changed = sorted(key for key, row in current["items"].items() if row["passed"] != report["items"][key]["passed"])
    return {
        **current, "manifest": {**manifest, "gate_version": GATE_VERSION},
        "generated_at": report["generated_at"], "answers": answers,
        "rescored": {"from_gate_version": manifest["gate_version"], "to_gate_version": GATE_VERSION,
                     "summary_before": report["summary"], "changed": changed},
    }


def answers_from_runner_log(lines: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Answers captured by a diagnostic runner around ``chat``: one JSON object per line,
    ``{"id", "result"}`` or ``{"id", "error"}``. Errors become model errors, as in ``collect``."""
    answers: dict[str, dict[str, Any]] = {}
    for line in lines:
        if not line.strip():
            continue
        record = json.loads(line)
        if "error" in record or not isinstance(record.get("result"), dict):
            answers[record["id"]] = model_error_record(0.0)
        else:
            answers[record["id"]] = answer_record(record["result"], 0.0)
    return answers


def baseline_from(report: dict[str, Any], *, reason: str, previous: dict[str, Any] | None,
                  source: dict[str, Any] | None = None) -> dict[str, Any]:
    """``source``: where a baseline registered from a saved report came from."""
    if not reason.strip():
        raise GateConfigurationError("登记新基线必须写明原因")
    history = list((previous or {}).get("history", []))
    if previous:
        history.append({"inference_id": previous["manifest"]["inference_id"],
                        "gate_version": previous["manifest"].get("gate_version"),
                        "registered_at": previous.get("registered_at"),
                        "reason": previous.get("reason"), "summary": previous["summary"]})
    baseline = {
        "manifest": report["manifest"], "summary": report["summary"],
        "items": {key: {"passed": row["passed"]} for key, row in report["items"].items()},
        "registered_at": report["generated_at"], "reason": reason.strip(), "history": history,
    }
    if source:
        baseline["registered_from"] = source
    return baseline


def render_markdown(report: dict[str, Any], verdict: dict[str, Any] | None, baseline: dict[str, Any] | None,
                    *, registered: bool = False) -> str:
    summary = report["summary"]
    manifest = report["manifest"]
    if registered:
        status = "已登记为基线"
    elif verdict is None:
        status = "未比较（没有基线）"  # never "passed": nothing was compared
    else:
        status = "通过" if verdict["passed"] else "未通过"
    lines = [f"## 评测门禁（{manifest['mode']}）：{status}", ""]
    lines.append(f"推理版本 `{manifest['inference_id']}`，数据库 {manifest['database_backend']}，"
                 f"评测集 {summary['queries']} 题，评分 {manifest.get('gate_version')}。")
    rescored = report.get("rescored")
    if rescored:
        changed = "、".join(f"`{key}`" for key in rescored["changed"]) or "无"
        lines.append(f"用保存的回答按 {rescored['to_gate_version']} 重新评分（原 {rescored['from_gate_version']} "
                     f"通过 {rescored['summary_before']['passed']} 题），改判：{changed}。")
    lines += ["", "| 指标 | 本次 | 基线 |", "| --- | --- | --- |"]
    rows = [("通过题数", "passed"), ("引用到全部必需页", "required_pages_cited"),
            ("正确拒答", "refusal_correct"), ("禁止断言", "violation_count"),
            ("替律师下结论的回答（只报告）", "overreach_answers"),
            ("p50 时延 ms", "latency_p50_ms"), ("p95 时延 ms", "latency_p95_ms")]
    for label, key in rows:
        before = baseline["summary"].get(key, "") if baseline else ""
        lines.append(f"| {label} | {summary.get(key, '')} | {before} |")
    flagged = sorted(key for key, row in report["items"].items() if row.get("overreach"))
    if flagged:
        lines += ["", "替律师下结论的回答（不影响通过与否，供复核）：" + "、".join(f"`{key}`" for key in flagged)]
    if verdict:
        if verdict["failures"]:
            lines += ["", "**阻断原因**", ""] + [f"- {text}" for text in verdict["failures"]]
        if verdict["regressions"]:
            lines += ["", "原本通过、现在失败：" + "、".join(f"`{key}`" for key in verdict["regressions"])]
        if verdict["improvements"]:
            lines += ["", "原本失败、现在通过：" + "、".join(f"`{key}`" for key in verdict["improvements"])
                      + "（确认后可登记为新基线）"]
    return "\n".join(lines) + "\n"
