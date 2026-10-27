"""Record the recovery anomaly on the current code: reuse vs. refuse after a source edit.

Three phases, each a separate process, so a reuse cannot be explained by cached
Python state:

1. ``interrupt`` — run a LangGraph review offline (model disabled), fail after the
   critic, and record the run id plus the saved review digest.
2. ``resume``    — in a new process, resume that run and require the saved review
   to be reused without repeating the specialist nodes.
3. ``tamper``    — a separate run, edit one retrieved page, and require resume to
   refuse with a stale-source error without consuming a retry.

The model is intentionally disabled: this demonstrates the recovery guard, not
answer quality. No fixed answer stands in for a model response.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import connect, db_scope, init_db, transaction  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.langgraph_agents import ReviewState

DEFAULT_QUESTION = "张某审批陈述是否矛盾，证据存在什么疏漏？"
EXIT_REJECTED = 4


def canonical(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def step_count(run_id: int) -> int:
    with closing(connect()) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM agent_steps WHERE run_id=?", (run_id,)).fetchone()[0])


def run_row(run_id: int) -> dict[str, Any]:
    with closing(connect()) as conn:
        row = conn.execute("SELECT status,resume_count FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(f"运行记录不存在：{run_id}")
    return {"status": row["status"], "resume_count": int(row["resume_count"])}


def interrupt_once(checkpoint: Path, question: str) -> tuple[int, dict[str, Any]]:
    """Run until the memory node fails, keeping the completed review checkpoint."""
    from app.agents import get_run_trace
    from app.langgraph_agents import LangGraphRunError, create_langgraph_coordinator

    def fail_memory(node: str, state: "ReviewState") -> None:
        if node == "memory":
            raise RuntimeError("演示用合成中断：审校完成后停止")

    coordinator = create_langgraph_coordinator(1, False, failure_injector=fail_memory,
                                               checkpoint_path=checkpoint)
    try:
        coordinator.process_query(question, use_llm=False, persist_memory=False)
    except LangGraphRunError as failed:
        trace = get_run_trace(failed.run_id)
        critic = next(step for step in trace["steps"] if step["node_name"] == "critic")
        return failed.run_id, {"collaboration_sha256": canonical(critic["output"]["collaboration"]),
                               "collaboration_status": critic["output"]["collaboration"]["status"],
                               "retrieval_pages": [[c["document_id"], c["page_no"]]
                                                   for c in next(step for step in trace["steps"]
                                                                 if step["node_name"] == "retrieve")["output"]["contexts"]]}
    raise RuntimeError("预期的合成中断没有发生")


def phase_interrupt(data_dir: Path, output: Path, question: str) -> int:
    checkpoint = data_dir / "checkpoints.sqlite"
    with db_scope(data_dir / "law_review.db"):
        init_db(seed=True)
        run_id, saved = interrupt_once(checkpoint, question)
        record = {"phase": "interrupt", "run_id": run_id, "question": question,
                  "steps": step_count(run_id), "run": run_row(run_id), **saved,
                  "model_disabled": True,
                  "notice": "关闭模型，只演示恢复校验；不是模型效果证据"}
        write_json(output, record)
        print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def phase_resume(data_dir: Path, output: Path, state: Path) -> int:
    checkpoint = data_dir / "checkpoints.sqlite"
    saved = json.loads(state.read_text(encoding="utf-8"))
    from app.langgraph_agents import create_langgraph_coordinator

    with db_scope(data_dir / "law_review.db"):
        before = step_count(saved["run_id"])
        resumed = create_langgraph_coordinator(1, False, checkpoint_path=checkpoint).resume(saved["run_id"])
        after = step_count(saved["run_id"])
        record = {"phase": "resume", "run_id": saved["run_id"],
                  "steps_before": before, "steps_after": after,
                  "resume_count": resumed["resume_count"], "run": run_row(saved["run_id"]),
                  "collaboration_reused": canonical(resumed["collaboration"]) == saved["collaboration_sha256"],
                  "answer_sha256": canonical(resumed["answer"]), "model_disabled": True,
                  "notice": "独立进程恢复；未重复运行专家节点，未发起模型调用"}
        write_json(output, record)
        print(json.dumps(record, ensure_ascii=False, indent=2))
        ok = record["collaboration_reused"] and after == before and record["resume_count"] == 1
        return 0 if ok else EXIT_REJECTED


def phase_tamper(data_dir: Path, output: Path, question: str) -> int:
    checkpoint = data_dir / "checkpoints.sqlite"
    from app.langgraph_agents import create_langgraph_coordinator

    with db_scope(data_dir / "law_review.db"):
        init_db(seed=True)
        run_id, saved = interrupt_once(checkpoint, question)
        document_id, page_no = saved["retrieval_pages"][0]
        with closing(connect()) as conn:
            original = conn.execute("SELECT text FROM pages WHERE document_id=? AND page_no=?",
                                    (document_id, page_no)).fetchone()[0]
        with transaction() as conn:
            conn.execute("UPDATE pages SET text=? WHERE document_id=? AND page_no=?",
                         (original + "新补充的合成说明。", document_id, page_no))
        outcome, detail = "resumed", ""
        try:
            create_langgraph_coordinator(1, False, checkpoint_path=checkpoint).resume(run_id)
        except ValueError as exc:
            outcome, detail = "refused", str(exc)
        record = {"phase": "tamper", "run_id": run_id, "edited_page": [document_id, page_no],
                  "outcome": outcome, "detail": detail, "run": run_row(run_id),
                  "collaboration_sha256": saved["collaboration_sha256"], "model_disabled": True,
                  "notice": "来源页被改后拒绝复用旧审校结果，且不消耗重试次数"}
        write_json(output, record)
        print(json.dumps(record, ensure_ascii=False, indent=2))
        refused = outcome == "refused" and "内容已变化" in detail and record["run"]["resume_count"] == 0
        return 0 if refused else EXIT_REJECTED


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="恢复校验演示：复用或拒绝")
    parser.add_argument("--phase", choices=("interrupt", "resume", "tamper"), required=True)
    parser.add_argument("--data-dir", type=Path, required=True, help="一次性数据目录；interrupt 会新建并初始化")
    parser.add_argument("--output", type=Path, required=True, help="证据 JSON 路径，必须不存在")
    parser.add_argument("--state", type=Path, help="resume 阶段读取 interrupt 写下的状态")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    args = parser.parse_args(argv)

    if args.phase in {"interrupt", "tamper"}:
        try:
            args.data_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        except FileExistsError:
            print(f"{args.data_dir} 已存在；演示使用一次性目录，不覆盖旧证据", file=sys.stderr)
            return EXIT_REJECTED
        if args.phase == "interrupt":
            return phase_interrupt(args.data_dir, args.output, args.question)
        return phase_tamper(args.data_dir, args.output, args.question)
    if args.state is None:
        parser.error("resume 阶段需要 --state")
    if not (args.data_dir / "law_review.db").exists():
        parser.error(f"{args.data_dir} 中没有可恢复的数据库")
    if args.data_dir.resolve() == Path(os.getenv("LAW_REVIEW_DATA_DIR", str(ROOT / "data"))).resolve():
        parser.error("拒绝在运行数据目录上执行演示")
    return phase_resume(args.data_dir, args.output, args.state)


if __name__ == "__main__":
    raise SystemExit(main())
