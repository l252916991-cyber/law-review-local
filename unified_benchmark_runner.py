#!/usr/bin/env python3
"""
统一基准测试运行器 - 支持续跑、检查点和多数据集
LexVault 全量测试框架
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import statistics
import sys
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from app.benchmark_metrics import SCORER_VERSION, score_lawbench_item, score_lexeval_item
from app.benchmark_postprocess import POSTPROCESS_VERSION, postprocess
from app.benchmark_retrieval import RETRIEVAL_VERSION, corpus_fingerprint, retrieve
from app.benchmark_solver import TASK_GUIDANCE
from app.lawbench import LAW_BENCH_COMMIT, TASK_NAMES, load_task, validate_lawbench
from app.services import LOCAL_LLM_MODEL, LOCAL_LLM_URL, read_json_with_deadline


# 配置常量
MODEL_CONFIG = {
    "url": os.getenv("LAW_REVIEW_LLM_URL", LOCAL_LLM_URL).rstrip("/"),
    "model": os.getenv("LAW_REVIEW_LLM_MODEL", LOCAL_LLM_MODEL),
    "temperature": 0.0,  # 评测固定为 0
    "max_tokens": 900,
    "timeout": 180,
    "enable_thinking": False,
}

LEXEVAL_COMMIT = "3624461b7c8df680412d18c9af7b9d210a15af82"
LEXEVAL_TASKS = {
    "6_1": ("偏见与歧视", 1000),
    "6_2": ("道德", 1000),
    "6_3": ("隐私", 500),
}
LEXEVAL_CACHE = Path(__file__).resolve().parent / "benchmarks" / "lexeval"
CURRENT_LAW_PATH = Path(__file__).resolve().parent / "benchmarks" / "current_law" / "current_law_300.json"
RAG_PROJECT_PATH = Path(__file__).resolve().parent / "benchmarks" / "rag_project" / "rag_240.json"
# Tasks whose gold-blind deterministic output repair is applied before scoring.
# 2-10 is deliberately excluded: the measured gain was noise (15 up, 16 down).
POSTPROCESS_TASKS = ("2-1", "2-7", "2-9", "3-8")
# Tasks whose repair needs the frozen statutory corpus; only active when --corpus-dir is supplied.
RETRIEVAL_TASKS = ("1-1",)
STATUTORY_RAG_TASK = "3-2"
STATUTORY_RAG_RANKER = "lexical"
ROUTING_VERSION = "lawbench-task-routing-v1"


def hash_text(text: str) -> str:
    """计算文本的 SHA256 哈希"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_json(value: object) -> str:
    """Stable hash for frozen protocol objects and actual request messages."""
    return hash_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def task_route(task: str, corpus_directories: list[str], disable_3_2_rag: bool) -> str:
    """Choose the frozen per-task inference path."""
    if task != STATUTORY_RAG_TASK or not corpus_directories:
        return "legacy_prompt"
    return "solver_task_guided_control" if disable_3_2_rag else "statutory_rag"


def route_messages(record: dict[str, Any], route: str, strategy: str,
                   config: dict[str, Any], retrieval: dict[str, Any] | None = None,
                   ) -> list[dict[str, str]]:
    """Build the exact first request messages for manifest prompt hashing."""
    if route == "legacy_prompt":
        from app.benchmark_reporting import prompt_for

        system, user = prompt_for(record, strategy=strategy)
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if route == "solver_task_guided_control":
        from app.benchmark_solver import messages_for

        messages, _ = messages_for(record["task"], record["instruction"], record["question"],
                                   {**config, "strategy": "task_guided"})
        return messages
    if retrieval is None:
        raise ValueError("statutory_rag route requires frozen retrieval")
    from app.benchmark_rag_solver import messages_for

    messages, _ = messages_for(record["task"], record["instruction"], record["question"],
                               {**config, "strategy": "statutory_rag"}, retrieval)
    return messages


def solver_provenance_issues(row: dict[str, Any], route: str, messages: list[dict[str, str]],
                             config: dict[str, Any], corpus_directories: list[str]) -> list[str]:
    """Validate one solver-backed row against its frozen request configuration."""
    from app.benchmark_solver import SOLVER_VERSION, _configuration, _endpoint

    issues = []
    expected_request = {
        "model": config["model"], "messages": messages,
        "temperature": config["temperature"], "max_tokens": config["max_tokens"],
        "stream": False, "chat_template_kwargs": {"enable_thinking": config["enable_thinking"]},
    }
    effective = _configuration({**config, "strategy": "task_guided"})
    expected_effective = effective
    expected_phase = "draft"
    expected_solver = SOLVER_VERSION
    if route == "statutory_rag":
        expected_effective = {
            **effective, "strategy": "statutory_rag", "corpus_directories": corpus_directories,
            "retrieval_ranker_policy": STATUTORY_RAG_RANKER,
        }
        expected_phase = "statutory_rag" if len(messages) == 3 else "draft"
        expected_solver += "+statutory-rag-v2"
    calls = row.get("calls")
    if row.get("request_messages") != messages:
        issues.append("request messages")
    if row.get("effective_config") != expected_effective:
        issues.append("effective config")
    if row.get("solver_version") != expected_solver:
        issues.append("solver version")
    if row.get("attempts") != 1 or not isinstance(calls, list) or len(calls) != 1:
        issues.append("single-call trace")
        return issues
    call = calls[0]
    if call.get("phase") != expected_phase or call.get("request_url") != _endpoint(config["url"]):
        issues.append("call route")
    if call.get("request") != expected_request or call.get("request_sha256") != hash_json(expected_request):
        issues.append("request body")
    expected_errors = [call["error"]] if call.get("error") else []
    if row.get("attempt_errors") != expected_errors or row.get("error") != call.get("error") \
            or row.get("finish_reason") != call.get("finish_reason") or row.get("usage") != call.get("usage"):
        issues.append("call result")
    expected_prediction = call.get("prediction")
    if route == "statutory_rag":
        from app.benchmark_postprocess import postprocess

        expected_prediction, expected_postprocess = postprocess(
            row["task"], row["question"], expected_prediction or "", retrieval=row.get("retrieval"),
        )
        if row.get("postprocess") != expected_postprocess:
            issues.append("postprocess result")
    if row.get("prediction") != expected_prediction:
        issues.append("selected prediction")
    return issues


def postprocess_with_retrieval(task: str, question: str, prediction: str, corpus_directories: list[str],
                               ) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    """Deterministic output repair; 1-1 resolves the official article text from the frozen corpus."""
    context = retrieve(task, question, corpus_directories) if task in RETRIEVAL_TASKS else None
    revised, metadata = postprocess(task, question, prediction, context)
    return revised, metadata, context


def call_model(prompt: str, system: str, config: dict[str, Any], retry: int = 0, metadata: dict | None = None) -> tuple[str, float, str | None]:
    """
    调用模型并返回响应、延迟和错误信息

    Returns:
        (response, latency_ms, error)
    """
    overall_started = time.perf_counter()
    details = metadata if metadata is not None else {}
    details.update(attempts=0, attempt_errors=[], finish_reason=None, usage=None)
    for attempt in range(retry + 1):
        details["attempts"] = attempt + 1
        try:
            body = json.dumps(
                {
                    "model": config["model"],
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                    "temperature": config["temperature"],
                    "max_tokens": config["max_tokens"],
                    "stream": False,
                    "chat_template_kwargs": {"enable_thinking": config["enable_thinking"]},
                },
                ensure_ascii=False,
            ).encode("utf-8")

            request = urllib.request.Request(
                f"{config['url']}/chat/completions",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            started = time.perf_counter()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            with opener.open(request, timeout=config["timeout"]) as response:
                payload = read_json_with_deadline(response, config["timeout"])

            latency = (time.perf_counter() - overall_started) * 1000
            content = payload["choices"][0]["message"]["content"].strip()
            details.update(finish_reason=payload["choices"][0].get("finish_reason"), usage=payload.get("usage"), response_model=payload.get("model"))
            return content, latency, None

        except urllib.error.URLError as e:
            error_msg = f"网络错误: {e.reason}"
            details["attempt_errors"].append(error_msg)
            if attempt < retry:
                time.sleep(2 ** attempt)  # 指数退避
                continue
            return "", (time.perf_counter() - overall_started) * 1000, error_msg
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            details["attempt_errors"].append(error_msg)
            if attempt < retry:
                time.sleep(2 ** attempt)
                continue
            return "", (time.perf_counter() - overall_started) * 1000, error_msg

    return "", 0, "重试次数耗尽"


def verify_model(config: dict[str, Any]) -> None:
    """验证模型服务可用性"""
    try:
        request = urllib.request.Request(f"{config['url']}/models")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            payload = json.load(response)

        available = {str(item.get("id")) for item in payload.get("data", [])}
        if config["model"] not in available:
            raise RuntimeError(f"模型服务未暴露配置模型 {config['model']}；可用模型：{sorted(available)}")

        print(f"✓ 模型服务正常: {config['url']}")
        print(f"✓ 目标模型: {config['model']}")
    except Exception as e:
        raise RuntimeError(f"模型服务验证失败: {e}")


def load_lawbench_dataset(tasks: list[str], limit_per_task: int | None, seed: int | None = None) -> list[dict[str, Any]]:
    """加载 LawBench 数据集"""
    validation = validate_lawbench()
    if not validation["valid"]:
        raise RuntimeError(f"LawBench 数据不完整：{validation['invalid_tasks']}")

    selected = list(TASK_NAMES) if "all" in tasks else tasks
    invalid = [task for task in selected if task not in TASK_NAMES]
    if invalid:
        raise ValueError(f"无效 LawBench 任务：{','.join(invalid)}")

    records = []
    for task_id in selected:
        task_records = list(enumerate(load_task(task_id)))
        if limit_per_task is not None and limit_per_task < 1:
            raise ValueError("limit_per_task must be positive")
        if limit_per_task and limit_per_task < len(task_records):
            task_records = sorted(random.Random(f"{seed}:{task_id}").sample(task_records, limit_per_task)) if seed is not None else task_records[:limit_per_task]
        for index, item in task_records:
            records.append({
                "dataset": "lawbench",
                "task": task_id,
                "task_name": TASK_NAMES[task_id],
                "question_id": f"{task_id}_{index:04d}",
                "question": item["question"],
                "instruction": item["instruction"],
                "dataset_version": LAW_BENCH_COMMIT,
                "reference": item["answer"],
                "question_hash": hash_text(item["question"]),
            })

    return records


def load_lexeval_dataset(tasks: list[str], limit_per_task: int | None) -> list[dict[str, Any]]:
    """加载 LexEval 伦理安全数据集"""
    # 待实现：下载和解析 LexEval 数据
    print("⚠️  LexEval 数据集尚未完整集成，跳过")
    return []


def load_current_law_dataset(limit: int | None) -> list[dict[str, Any]]:
    """加载现行法律时效性数据集"""
    if not CURRENT_LAW_PATH.exists():
        print(f"⚠️  现行法律数据集不存在: {CURRENT_LAW_PATH}")
        return []

    with open(CURRENT_LAW_PATH, encoding="utf-8") as f:
        data = json.load(f)

    records = []
    for index, item in enumerate(data[: limit or None]):
        records.append({
            "dataset": "current_law",
            "task": item.get("category", "general"),
            "question_id": f"current_law_{index:04d}",
            "question": item["question"],
            "reference": item["answer"],
            "question_hash": hash_text(item["question"]),
            "legal_basis": item.get("legal_basis", ""),
            "effective_date": item.get("effective_date", ""),
        })

    return records


def load_rag_project_dataset(limit: int | None) -> list[dict[str, Any]]:
    """加载项目专用 RAG 数据集"""
    if not RAG_PROJECT_PATH.exists():
        print(f"⚠️  RAG 项目数据集不存在: {RAG_PROJECT_PATH}")
        return []

    with open(RAG_PROJECT_PATH, encoding="utf-8") as f:
        data = json.load(f)

    records = []
    for index, item in enumerate(data[: limit or None]):
        records.append({
            "dataset": "rag_project",
            "task": item.get("category", "retrieval"),
            "question_id": f"rag_project_{index:04d}",
            "question": item["question"],
            "reference": item["answer"],
            "evidence_source": item.get("evidence_source", []),
            "difficulty": item.get("difficulty", "medium"),
            "question_hash": hash_text(item["question"]),
        })

    return records


def load_all_datasets(args: argparse.Namespace) -> list[dict[str, Any]]:
    """根据命令行参数加载所有数据集"""
    records = []

    if args.dataset in {"lawbench", "all"}:
        print(f"\n📚 加载 LawBench 数据集...")
        lawbench_records = load_lawbench_dataset(args.tasks, args.limit_per_task, getattr(args, "sample_seed", None))
        records.extend(lawbench_records)
        print(f"   ✓ LawBench: {len(lawbench_records)} 题")

    if args.dataset in {"lexeval", "all"}:
        print(f"\n📚 加载 LexEval 数据集...")
        lexeval_records = load_lexeval_dataset(args.tasks, args.limit_per_task)
        records.extend(lexeval_records)
        print(f"   ✓ LexEval: {len(lexeval_records)} 题")

    if args.dataset in {"current_law", "all"}:
        print(f"\n📚 加载现行法律数据集...")
        current_law_records = load_current_law_dataset(args.limit_per_task)
        records.extend(current_law_records)
        print(f"   ✓ 现行法律: {len(current_law_records)} 题")

    if args.dataset in {"rag_project", "all"}:
        print(f"\n📚 加载 RAG 项目数据集...")
        rag_records = load_rag_project_dataset(args.limit_per_task)
        records.extend(rag_records)
        print(f"   ✓ RAG 项目: {len(rag_records)} 题")

    return records


def run_benchmark(args: argparse.Namespace) -> None:
    """Frozen, resumable run; checkpoints are the source of truth."""
    from app.benchmark_reporting import PROMPT_VERSION, atomic_json, source_hashes, write_report

    if args.dataset != "lawbench":
        raise ValueError("This audited runner currently supports --dataset lawbench only; other datasets need dedicated scorers.")
    records = load_all_datasets(args)
    if not records:
        raise ValueError("No questions loaded")
    strategy = getattr(args, "prompt_strategy", "task_guided")
    if strategy not in {"direct", "task_guided", "hybrid", "correction_locate"}:
        raise ValueError("prompt_strategy must be direct, task_guided, hybrid or correction_locate")
    corpus_directories = [str(Path(path).resolve()) for path in (getattr(args, "corpus_dir", None) or [])]
    disable_3_2_rag = bool(getattr(args, "disable_3_2_rag", False))
    postprocess_tasks = list(POSTPROCESS_TASKS) + (list(RETRIEVAL_TASKS) if corpus_directories else [])
    if not corpus_directories:
        if any(record["task"] in RETRIEVAL_TASKS for record in records):
            print("⚠️  未提供 --corpus-dir：1-1 使用模型原始输出，不启用官方条文替换", flush=True)
        if any(record["task"] == STATUTORY_RAG_TASK for record in records):
            print("⚠️  未提供 --corpus-dir：3-2 保持旧统一 runner 提示路径，不启用已采纳的词法 RAG", flush=True)
    explicit_dir = getattr(args, "run_dir", None)
    if args.resume and not explicit_dir:
        raise ValueError("--resume requires the original --run-dir; never creates a new run")
    output_dir = Path(explicit_dir) if explicit_dir else Path(args.output_dir) / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    manifest_path = output_dir / "manifest.json"
    baseline = getattr(args, "baseline_results", None)
    baseline = str(Path(baseline).resolve()) if baseline else None
    # Validate the full paired baseline before spending any inference time.
    if baseline:
        old = {r["question_id"]: r for line in Path(baseline).read_text(encoding="utf-8").splitlines() if line.strip() for r in [json.loads(line)]}
        for r in records:
            b = old.get(r["question_id"])
            if not b or (b["question"], b["reference"]) != (r["question"], r["reference"]):
                raise ValueError(f"Baseline missing/mismatched: {r['question_id']}")
    route_samples = []
    for record in records:
        route = task_route(record["task"], corpus_directories, disable_3_2_rag)
        frozen_retrieval = None
        if route == "statutory_rag":
            frozen_retrieval = retrieve(
                record["task"], record["question"], corpus_directories,
                ranker_policy=STATUTORY_RAG_RANKER,
            )
            if frozen_retrieval.get("ranker") != STATUTORY_RAG_RANKER:
                raise ValueError(f"3-2 retrieval did not use lexical ranking: {record['question_id']}")
        messages = route_messages(record, route, strategy, MODEL_CONFIG, frozen_retrieval)
        route_samples.append({
            "question_id": record["question_id"], "question_hash": record["question_hash"],
            "record_hash": hash_text(json.dumps(record, ensure_ascii=False, sort_keys=True)),
            "route": route, "prompt_hash": hash_json(messages),
            "retrieval_hash": hash_json(frozen_retrieval) if frozen_retrieval is not None else None,
            "retrieval": frozen_retrieval,
        })
    routing = {
        "version": ROUTING_VERSION,
        "task_3_2": {
            "enabled": bool(corpus_directories) and not disable_3_2_rag,
            "corpus_required": True,
            "enabled_route": "statutory_rag",
            "disabled_route": "solver_task_guided_control",
            "no_corpus_route": "legacy_prompt",
            "ranker_policy": STATUTORY_RAG_RANKER,
            "solver_transport_retries": 0,
        },
    }
    if args.retry and any(sample["route"] in {"statutory_rag", "solver_task_guided_control"}
                          for sample in route_samples):
        print(f"⚠️  3-2 solver 路径固定单次调用；--retry={args.retry} 仅适用于旧提示路径", flush=True)
    manifest = {
        "protocol_version": PROMPT_VERSION, "scorer_version": SCORER_VERSION,
        "postprocess": {"version": POSTPROCESS_VERSION, "tasks": postprocess_tasks},
        "retrieval": {"version": RETRIEVAL_VERSION, "corpus_directories": corpus_directories,
                      "corpus_files": corpus_fingerprint(corpus_directories) if corpus_directories else {}},
        "prompt_strategy": getattr(args, "prompt_strategy", "task_guided"),
        "model_config": dict(MODEL_CONFIG), "sample_seed": getattr(args, "sample_seed", None),
        "retry": args.retry, "planned_questions": len(records), "source_hashes": source_hashes(),
        "routing": routing,
        "baseline_results": baseline,
        "baseline_sha256": hashlib.sha256(Path(baseline).read_bytes()).hexdigest() if baseline else None,
        "samples": route_samples,
    }
    manifest_config_hash = hash_json({key: manifest[key] for key in (
        "protocol_version", "scorer_version", "postprocess", "retrieval", "prompt_strategy",
        "model_config", "retry", "routing",
    )})
    if args.resume:
        stored_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if stored_manifest != manifest:
            raise ValueError("Resume manifest differs (model, sample, prompt, source or scorer); start a new run")
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        if any(output_dir.iterdir()):
            raise ValueError("Run directory must be empty; existing results will not be overwritten")
        atomic_json(manifest_path, manifest)
        source_dir = output_dir / "source_snapshot"
        source_dir.mkdir()
        root = Path(__file__).resolve().parent
        for name in manifest["source_hashes"]:
            destination = source_dir / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((root / name).read_bytes())

    checkpoint_dir = output_dir / "checkpoints"
    checkpoint_dir.mkdir(exist_ok=True)
    # Exclusive process lock: prevents two resumes from invoking the same question.
    import fcntl
    with (output_dir / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another process is already executing this run")
        completed = {}
        planned = {r["question_id"]: r for r in records}
        frozen_samples = {sample["question_id"]: sample for sample in manifest["samples"]}
        for path in checkpoint_dir.glob("*.json"):
            row = json.loads(path.read_text(encoding="utf-8"))
            key = row["question_id"]
            if key not in planned or key in completed:
                raise ValueError(f"Unexpected/duplicate checkpoint: {key}")
            expected = planned[key]
            if any(row.get(k) != v for k, v in expected.items()) or row.get("scorer_version") != SCORER_VERSION:
                raise ValueError(f"Checkpoint mismatch: {key}")
            if row.get("error") and (row.get("score") != 0.0 or row.get("metric") != "error"):
                raise ValueError(f"Checkpoint failure score mismatch: {key}")
            sample = frozen_samples[key]
            if row.get("task_route") != sample["route"] or row.get("prompt_hash") != sample["prompt_hash"] \
                    or row.get("manifest_config_hash") != manifest_config_hash:
                raise ValueError(f"Checkpoint route/protocol mismatch: {key}")
            if sample["route"] in {"statutory_rag", "solver_task_guided_control"}:
                messages = route_messages(expected, sample["route"], strategy, MODEL_CONFIG, sample.get("retrieval"))
                if solver_provenance_issues(
                    row, sample["route"], messages, MODEL_CONFIG, corpus_directories,
                ):
                    raise ValueError(f"Checkpoint request provenance mismatch: {key}")
                if sample["route"] == "statutory_rag" and row.get("retrieval") != sample.get("retrieval"):
                    raise ValueError(f"Checkpoint retrieval mismatch: {key}")
            if row["task"] in postprocess_tasks and not row.get("error"):
                raw = row.get("original_prediction")
                if not isinstance(raw, str):
                    raise ValueError(f"Checkpoint missing original prediction: {key}")
                prediction, metadata, context = postprocess_with_retrieval(row["task"], row["question"], raw, corpus_directories)
                if row["prediction"] != prediction or row.get("postprocess") != metadata:
                    raise ValueError(f"Checkpoint postprocess mismatch: {key}")
                if context is not None and row.get("retrieval") != context:
                    raise ValueError(f"Checkpoint retrieval mismatch: {key}")
            completed[key] = row
        verify_model(MODEL_CONFIG)
        rows = [completed[r["question_id"]] for r in records if r["question_id"] in completed]
        prior_duration = 0
        if (output_dir / "summary.json").exists():
            prior_duration = json.loads((output_dir / "summary.json").read_text())["duration_hours"] * 3600
        # Recover log from atomic checkpoints, including a crash between checkpoint and append.
        detail_path = output_dir / "detailed_results.jsonl"
        detail_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        started = time.perf_counter()
        status = "running"
        print(f"Run: {output_dir.resolve()} | {len(rows)}/{len(records)} already saved", flush=True)
        write_report(rows, output_dir, prior_duration, MODEL_CONFIG, manifest, status)
        try:
            for record in records:
                key = record["question_id"]
                if key in completed:
                    continue
                sample = frozen_samples[key]
                route = sample["route"]
                messages = route_messages(record, route, strategy, MODEL_CONFIG, sample.get("retrieval"))
                system, prompt = messages[0]["content"], messages[1]["content"]
                response_metadata: dict[str, Any] = {}
                if route == "legacy_prompt":
                    prediction, latency, error = call_model(
                        prompt, system, MODEL_CONFIG, retry=args.retry, metadata=response_metadata,
                    )
                else:
                    solver_config = {**MODEL_CONFIG, "strategy": "task_guided"}
                    if route == "statutory_rag":
                        from app.benchmark_rag_solver import solve

                        solver_config.update(
                            strategy="statutory_rag", corpus_directories=corpus_directories,
                            retrieval_ranker_policy=STATUTORY_RAG_RANKER,
                            retrieval_context=sample["retrieval"],
                        )
                    else:
                        from app.benchmark_solver import solve
                    result = solve(record["task"], record["instruction"], record["question"], solver_config)
                    prediction, latency, error = result["prediction"], result["latency_ms"], result["error"]
                    calls = result.get("calls", [])
                    response_metadata.update(
                        attempts=len(calls), attempt_errors=[call["error"] for call in calls if call.get("error")],
                        finish_reason=result.get("finish_reason"), usage=result.get("usage"),
                        calls=calls, solver_version=result.get("solver_version"),
                        effective_config=result.get("model_config"), request_messages=messages,
                    )
                    if route == "statutory_rag":
                        response_metadata.update(retrieval=result.get("retrieval"),
                                                 postprocess=result.get("postprocess"))
                if route == "legacy_prompt" and record["task"] in postprocess_tasks and not error:
                    response_metadata["original_prediction"] = prediction
                    prediction, metadata, context = postprocess_with_retrieval(
                        record["task"], record["question"], prediction, corpus_directories)
                    response_metadata["postprocess"] = metadata
                    if context is not None:
                        response_metadata["retrieval"] = context
                scored = score_lawbench_item(record["task"], prediction, record["reference"], question=record["question"]).to_dict() if not error else {
                    "score": 0.0, "metric": "error", "abstained": False, "parse_failed": False,
                    "parsed_prediction": None, "parsed_reference": None,
                }
                row = {**record, **scored, "prediction": prediction, "error": error, "latency_ms": latency,
                       "model_config": dict(MODEL_CONFIG), "scorer_version": SCORER_VERSION, "prompt_version": PROMPT_VERSION,
                       "system_prompt": system, "user_prompt": prompt, "request_messages": messages,
                       "prompt_hash": hash_json(messages), "task_route": route,
                       "manifest_config_hash": manifest_config_hash,
                       "timestamp": datetime.now().isoformat(), **response_metadata}
                atomic_json(checkpoint_dir / f"{key}.json", row)
                with detail_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                    stream.flush()
                completed[key] = row
                rows.append(row)
                print(f"[{len(rows)}/{len(records)}] {key} score={row['score']:.4f} parse_failed={row['parse_failed']} latency={latency / 1000:.2f}s error={error}", flush=True)
                if len(rows) % 25 == 0:
                    write_report(rows, output_dir, prior_duration + time.perf_counter() - started, MODEL_CONFIG, manifest, status)
            status = "completed_with_errors" if any(r.get("error") for r in rows) else "completed"
        except BaseException:
            status = "interrupted"
            raise
        finally:
            write_report(rows, output_dir, prior_duration + time.perf_counter() - started, MODEL_CONFIG, manifest, status)
        print(f"Finished: {output_dir.resolve()} ({status})", flush=True)


def generate_summary_report(results: list[dict], output_dir: Path, duration: float, model_config: dict) -> None:
    """Backward-compatible reporting entry point with the corrected denominator."""
    from app.benchmark_reporting import write_report
    write_report(results, output_dir, duration, model_config, {"planned_questions": len(results)}, "completed")


def main():
    parser = argparse.ArgumentParser(description="LexVault 统一基准测试运行器")
    parser.add_argument(
        "--dataset",
        choices=["lawbench", "lexeval", "current_law", "rag_project", "all"],
        default="lawbench",
        help="选择数据集",
    )
    parser.add_argument(
        "--tasks",
        nargs="+",
        default=["all"],
        help="选择任务（对 LawBench 和 LexEval 有效）",
    )
    parser.add_argument(
        "--limit-per-task",
        type=int,
        default=None,
        help="限制每个任务的题目数",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="续跑模式：跳过已完成的题目",
    )
    parser.add_argument(
        "--retry",
        type=int,
        default=2,
        help="失败题目重试次数",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="output/benchmark_runs",
        help="输出目录",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="单题超时时间（秒）",
    )

    parser.add_argument("--sample-seed", type=int, default=42, help="分层随机抽样种子，默认42")
    parser.add_argument("--run-dir", help="显式运行目录；续跑必须指向原目录")
    parser.add_argument("--baseline-results", help="旧回答 JSONL，用于同题同规则比较，不额外调用模型")
    parser.add_argument("--max-tokens", type=int, default=900)
    parser.add_argument(
        "--prompt-strategy", choices=["direct", "task_guided", "hybrid", "correction_locate"], default="task_guided",
        help="选择通用提示、按任务指导提示、仅对受益任务启用指导的混合策略，或 2-1 两段式定位纠错；默认 task_guided",
    )
    parser.add_argument(
        "--corpus-dir", type=Path, action="append", default=None,
        help="可重复：冻结法条库目录；启用 1-1 条文替换，并默认启用 3-2 词法 statutory RAG（不读取参考答案）",
    )
    parser.add_argument(
        "--disable-3-2-rag", action="store_true",
        help="提供法条库时显式关闭 3-2 RAG，改走同 solver 的 task-guided 单次调用配对控制路径",
    )
    args = parser.parse_args()
    if args.retry < 0 or args.timeout < 1 or args.max_tokens < 1:
        parser.error("retry must be non-negative; timeout/max-tokens must be positive")

    # 更新超时配置
    MODEL_CONFIG["timeout"] = args.timeout
    MODEL_CONFIG["max_tokens"] = args.max_tokens

    try:
        run_benchmark(args)
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断，进度已保存到检查点")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n❌ 运行失败: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
