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


def hash_text(text: str) -> str:
    """计算文本的 SHA256 哈希"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    from app.benchmark_reporting import PROMPT_VERSION, atomic_json, prompt_for, source_hashes, write_report

    if args.dataset != "lawbench":
        raise ValueError("This audited runner currently supports --dataset lawbench only; other datasets need dedicated scorers.")
    records = load_all_datasets(args)
    if not records:
        raise ValueError("No questions loaded")
    strategy = getattr(args, "prompt_strategy", "task_guided")
    if strategy not in {"direct", "task_guided", "hybrid"}:
        raise ValueError("prompt_strategy must be direct, task_guided or hybrid")
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
    manifest = {
        "protocol_version": PROMPT_VERSION, "scorer_version": SCORER_VERSION,
        "postprocess": {"version": POSTPROCESS_VERSION, "tasks": list(POSTPROCESS_TASKS)},
        "prompt_strategy": getattr(args, "prompt_strategy", "task_guided"),
        "model_config": dict(MODEL_CONFIG), "sample_seed": getattr(args, "sample_seed", None),
        "retry": args.retry, "planned_questions": len(records), "source_hashes": source_hashes(),
        "baseline_results": baseline,
        "baseline_sha256": hashlib.sha256(Path(baseline).read_bytes()).hexdigest() if baseline else None,
        "samples": [{"question_id": r["question_id"], "question_hash": r["question_hash"],
                     "record_hash": hash_text(json.dumps(r, ensure_ascii=False, sort_keys=True)),
                     "prompt_hash": hash_text(json.dumps(prompt_for(r, strategy=strategy), ensure_ascii=False))}
                    for r in records],
    }
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
        for path in checkpoint_dir.glob("*.json"):
            row = json.loads(path.read_text(encoding="utf-8"))
            key = row["question_id"]
            if key not in planned or key in completed:
                raise ValueError(f"Unexpected/duplicate checkpoint: {key}")
            expected = planned[key]
            if any(row.get(k) != v for k, v in expected.items()) or row.get("scorer_version") != SCORER_VERSION:
                raise ValueError(f"Checkpoint mismatch: {key}")
            if row["task"] in POSTPROCESS_TASKS and not row.get("error"):
                raw = row.get("original_prediction")
                if not isinstance(raw, str):
                    raise ValueError(f"Checkpoint missing original prediction: {key}")
                prediction, metadata = postprocess(row["task"], row["question"], raw)
                if row["prediction"] != prediction or row.get("postprocess") != metadata:
                    raise ValueError(f"Checkpoint postprocess mismatch: {key}")
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
                system, prompt = prompt_for({**record, "prompt_strategy": strategy})
                response_metadata = {}
                prediction, latency, error = call_model(prompt, system, MODEL_CONFIG, retry=args.retry, metadata=response_metadata)
                if record["task"] in POSTPROCESS_TASKS and not error:
                    response_metadata["original_prediction"] = prediction
                    prediction, response_metadata["postprocess"] = postprocess(record["task"], record["question"], prediction)
                scored = score_lawbench_item(record["task"], prediction, record["reference"], question=record["question"]).to_dict() if not error else {
                    "score": 0.0, "metric": "error", "abstained": False, "parse_failed": False,
                    "parsed_prediction": None, "parsed_reference": None,
                }
                row = {**record, **scored, "prediction": prediction, "error": error, "latency_ms": latency,
                       "model_config": dict(MODEL_CONFIG), "scorer_version": SCORER_VERSION, "prompt_version": PROMPT_VERSION,
                       "system_prompt": system, "user_prompt": prompt,
                       "prompt_hash": hash_text(json.dumps((system, prompt), ensure_ascii=False)),
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
        "--prompt-strategy", choices=["direct", "task_guided", "hybrid"], default="task_guided",
        help="选择通用提示、按任务指导提示或仅对受益任务启用指导的混合策略；默认 task_guided",
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
