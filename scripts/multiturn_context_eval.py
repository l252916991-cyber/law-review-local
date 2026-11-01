"""Paired A/B/C harness for multi-turn context (offline by default).

A: no conversation history, no draft memory.
B: same-conversation history, no draft memory.
C: B plus draft-memory clues (only when the switch is enabled).

Each session starts from one frozen base database (prerequisite turns already
written), which is copied for every branch/run so all branches see the same
history. This is the plan's "frozen-history paired experiment" shape. Pass
``--use-llm`` to exercise a real local model: it needs a reachable endpoint and
records actual wall-clock latency but no token counts (the chat API does not
expose usage).

Default (rule) mode is a deterministic plumbing check, not a model-quality
result. Reports never merge the two.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BRANCHES = {
    "A": {"LAW_REVIEW_CONTEXT_HISTORY": "off", "LAW_REVIEW_CONTEXT_MEMORY": "off"},
    "B": {"LAW_REVIEW_CONTEXT_HISTORY": "on", "LAW_REVIEW_CONTEXT_MEMORY": "off"},
    "C": {"LAW_REVIEW_CONTEXT_HISTORY": "on", "LAW_REVIEW_CONTEXT_MEMORY": "on"},
}


def load_fixtures() -> Any:
    from tests import multiturn_fixtures

    return multiturn_fixtures


def create_case(conn: Any, documents: dict[str, Any], keys: Any, title: str) -> tuple[int, dict[str, int]]:
    from app.db import now

    case_id = conn.execute(
        "INSERT INTO cases(title, status, created_at, updated_at) VALUES (?, '进行中', ?, ?)",
        (title, now(), now()),
    ).lastrowid
    document_ids = {}
    for key in keys:
        document = documents[key]
        document_id = conn.execute(
            """INSERT INTO documents(case_id, name, mime_type, pages, status, doc_type, people,
                                    date_range, summary, created_at, updated_at)
               VALUES (?, ?, 'text/plain', ?, '已索引', ?, ?, ?, ?, ?, ?)""",
            (case_id, document["name"], len(document["pages"]), document["doc_type"],
             document["people"], document["date_range"], document["summary"], now(), now()),
        ).lastrowid
        for page_no, text in enumerate(document["pages"], 1):
            conn.execute(
                "INSERT INTO pages(document_id, page_no, text, summary) VALUES (?, ?, ?, ?)",
                (document_id, page_no, text, text[:120]),
            )
        document_ids[key] = document_id
    return case_id, document_ids


def setup_base(database_path: Path, session: dict[str, Any], fixtures: Any, use_llm: bool = False) -> dict[str, Any]:
    """Create the synthetic case and freeze prerequisite history in a base DB."""
    from app.db import db_scope, init_db, now, transaction

    with db_scope(database_path):
        init_db(seed=False)
        with transaction() as conn:
            case_id, document_ids = create_case(
                conn, fixtures.DOCUMENTS, fixtures.SCENARIOS[session["scenario"]],
                f"评测-{session['id']}",
            )
            conversation_id = conn.execute(
                "INSERT INTO conversations(case_id, user_name, title, created_at) VALUES (?, '评测律师', ?, ?)",
                (case_id, session["id"], now()),
            ).lastrowid
            if "frozen_history" in session:
                for message in session["frozen_history"]:
                    citations = [
                        {"index": index, "document_id": document_ids[key],
                         "document_name": fixtures.DOCUMENTS[key]["name"], "page": 1}
                        for index, key in enumerate(message.get("document_ids", []), 1)
                    ]
                    conn.execute(
                        """INSERT INTO messages(conversation_id, role, content, citations_json, route, created_at)
                           VALUES (?, ?, ?, ?, '事实检索', ?)""",
                        (conversation_id, message["role"], message["content"],
                         json.dumps(citations, ensure_ascii=False), now()),
                    )
        base = {
            "case_id": case_id,
            "conversation_id": conversation_id,
            "document_ids": document_ids,
            "frozen_history": "frozen_history" in session,
        }
        if not base["frozen_history"]:
            from app.services import chat

            for question in session.get("prerequisite", []):
                chat(case_id, question, "评测律师", conversation_id, use_llm, False)
        if session.get("cross_case"):
            with transaction() as conn:
                cross_case_id, cross_ids = create_case(
                    conn, fixtures.DOCUMENTS, fixtures.SCENARIOS[session["cross_case_scenario"]],
                    f"评测-{session['id']}-另一案件",
                )
            base["cross_case_id"] = cross_case_id
            base["cross_case_documents"] = cross_ids
        if "frozen_memory" in session:
            seed_memory(base, session, fixtures)
    return base


def seed_memory(base: dict[str, Any], session: dict[str, Any], fixtures: Any) -> None:
    """Create one structurally validated draft whose source run/pages still exist."""
    from app.db import now, transaction
    from app.rag import remember

    key = session["scenario"]
    document_key = fixtures.SCENARIOS[key][0]
    document_id = base["document_ids"][document_key]
    citations = [{"index": 1, "document_id": document_id,
                  "document_name": fixtures.DOCUMENTS[document_key]["name"], "page": 1}]
    with transaction() as conn:
        run_id = conn.execute(
            """INSERT INTO agent_runs(case_id, question, route, status, runtime, conversation_id,
                                    citations_json, final_answer, created_at)
               VALUES (?, '合成草稿来源', '事实检索', 'completed', 'native', ?, ?, '合成', ?)""",
            (base["case_id"], base["conversation_id"], json.dumps(citations, ensure_ascii=False), now()),
        ).lastrowid
    remember(base["case_id"], "评测律师", session["frozen_memory"][0]["content"], run_id,
             prefer_remote_embeddings=False, validated=True)


def copy_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def run_branch(
    run_path: Path, base: dict[str, Any], followup: dict[str, Any],
    branch: str, session: dict[str, Any], fixtures: Any, use_llm: bool,
) -> dict[str, Any]:
    from app.context import context_policy
    from app.db import db_scope
    from app.services import chat

    case_id = base["case_id"]
    conversation_id = base["conversation_id"]
    environment = dict(BRANCHES[branch])
    if session.get("cross_case"):
        case_id = base["cross_case_id"]
        conversation_id = None
    if session.get("fresh_conversation"):
        conversation_id = None
    if followup.get("lifecycle") == "archived":
        from app.db import now, transaction

        with db_scope(run_path), transaction() as conn:
            conn.execute(
                "UPDATE conversations SET archived_at=?, archive_expires_at=? WHERE id=?",
                (now(), 2**62, conversation_id),
            )
    if followup.get("lifecycle") == "deleted":
        from app.db import transaction

        with db_scope(run_path), transaction() as conn:
            conn.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))

    old_environment = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    started = time.perf_counter()
    record: dict[str, Any] = {
        "branch": branch, "followup": followup["id"], "session": session["id"],
        "category": session["category"], "cross_case": bool(session.get("cross_case")),
        "question": followup["question"], "policy": context_policy().as_dict(),
        "llm_calls": 0, "tokens": None,
    }
    try:
        with db_scope(run_path):
            result = chat(case_id, followup["question"], "评测律师", conversation_id, use_llm, False)
        record.update({
            "status": result["answer_status"],
            "answer": result["answer"],
            "route": result["route"],
            "object_document_ids": result["context"]["resolution"]["object_document_ids"] if result["context"].get("resolution") else [],
            "citation_document_ids": sorted({item["document_id"] for item in result["citations"]}),
            "clarification_options": [item["document_id"] for item in (result.get("clarification") or {}).get("options", [])],
            "memory": (result["context"].get("memory") or {}),
            "history": (result["context"].get("history") or {}),
            "llm_used": bool(result["llm_used"]),
            "llm_calls": 1 if result["llm_used"] else 0,
            "validation_valid": bool(result["validation"]["valid"]),
        })
    except Exception as exc:  # lifecycle errors are expected values, not crashes
        record.update({"status": f"error_{type(exc).__name__}", "answer": "", "route": "", "error": str(exc)})
    finally:
        record["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        for key, value in old_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return record


def evaluate(followup: dict[str, Any], record: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    expected_status = followup["expected_status"]
    lifecycle = followup.get("lifecycle")
    status = record["status"]
    answer = record.get("answer", "")
    checks: dict[str, Any] = {}
    document_ids = base["cross_case_documents"] if record.get("cross_case") else base["document_ids"]
    if lifecycle:
        expected_error = {
            "archived": "error_ArchivedConversationError",
            "deleted": "error_ConversationNotFoundError",
        }[lifecycle]
        checks["lifecycle"] = status == expected_error
        checks["status"] = True
        checks["objects"] = True
        checks["facts"] = True
        checks["citations"] = True
    else:
        checks["status"] = status == expected_status
        translated_objects = {document_ids[key] for key in followup.get("objects", []) if key in document_ids}
        actual_objects = set(record.get("object_document_ids") or [])
        checks["objects"] = actual_objects == translated_objects if translated_objects else not actual_objects
        facts = followup.get("facts") or []
        normalized = re.sub(r"\s+", "", answer)
        checks["facts"] = all(re.sub(r"\s+", "", fact["text"]) in normalized for fact in facts) if expected_status == "answered" else True
        allowed_keys = followup.get("citations_within") or []
        allowed_ids = {document_ids[key] for key in allowed_keys if key in document_ids}
        checks["citations"] = set(record.get("citation_document_ids") or []) <= allowed_ids if allowed_ids else True
        checks["clarification"] = True
        if expected_status == "clarification":
            option_keys = followup.get("options") or []
            expected_options = {document_ids[key] for key in option_keys if key in document_ids}
            actual_options = set(record.get("clarification_options") or [])
            checks["clarification"] = expected_options <= actual_options if expected_options else bool(actual_options)
    checks["no_unexpected_citation"] = set(record.get("citation_document_ids") or []) <= set(document_ids.values())
    return checks


def summarize(records: list[dict[str, Any]], followups_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"branches": {}, "by_category": {}}
    for record in records:
        followup = followups_by_id[record["followup"]]
        branch = record["branch"]
        entry = summary["branches"].setdefault(branch, {
            "runs": 0, "all_checks_pass": 0, "llm_used": 0, "clarifications": 0,
            "insufficient": 0, "latency_ms": [], "memory_used": 0,
        })
        entry["runs"] += 1
        entry["all_checks_pass"] += int(all(record["checks"].values()))
        entry["llm_used"] += int(record.get("llm_used", False))
        entry["clarifications"] += int(record["status"] == "clarification")
        entry["insufficient"] += int(record["status"] == "insufficient_evidence")
        entry["memory_used"] += int(bool((record.get("memory") or {}).get("used")))
        entry["latency_ms"].append(record["latency_ms"])
        category = summary["by_category"].setdefault(followup["category"], {}).setdefault(
            branch, {"runs": 0, "all_checks_pass": 0}
        )
        category["runs"] += 1
        category["all_checks_pass"] += int(all(record["checks"].values()))
    for entry in summary["branches"].values():
        latencies = sorted(entry.pop("latency_ms"))
        entry["latency_p50_ms"] = latencies[len(latencies) // 2] if latencies else 0
        entry["latency_p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0
    return summary


def render_report(meta: dict[str, Any], summary: dict[str, Any], records: list[dict[str, Any]]) -> str:
    lines = [
        "# 多轮上下文配对评测报告（A/B/C）",
        "",
        f"- 生成时间：{meta['generated_at']}",
        f"- 模式：{'真实本机模型调用（未记录 token）' if meta['use_llm'] else '规则检索（确定性管线检查，非模型质量结论）'}",
        f"- 题数：{meta['followup_count']} 道追问 / {meta['session_count']} 组会话",
        "- 分支：A=无历史；B=同会话历史；C=历史+草稿线索（开关开启）",
        "",
        "## 汇总",
        "",
        "| 分支 | 运行数 | 全部检查通过 | LLM 使用 | 澄清 | 资料不足 | 草稿使用 | p50 ms | p95 ms |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for branch in ("A", "B", "C"):
        entry = summary["branches"].get(branch, {})
        lines.append(
            f"| {branch} | {entry.get('runs', 0)} | {entry.get('all_checks_pass', 0)} | {entry.get('llm_used', 0)} | "
            f"{entry.get('clarifications', 0)} | {entry.get('insufficient', 0)} | {entry.get('memory_used', 0)} | "
            f"{entry.get('latency_p50_ms', 0)} | {entry.get('latency_p95_ms', 0)} |"
        )
    lines += ["", "## 分类汇总", "", "| 类别 | 分支 | 运行数 | 全部通过 |", "| --- | --- | --- | --- |"]
    for category, branches in summary["by_category"].items():
        for branch in ("A", "B", "C"):
            entry = branches.get(branch)
            if entry:
                lines.append(f"| {category} | {branch} | {entry['runs']} | {entry['all_checks_pass']} |")
    lines += ["", "## 逐题结果", "", "| 题号 | 分支 | 实际状态 | 检查 | 对象 ID | 引用文档 ID |", "| --- | --- | --- | --- | --- | --- |"]
    for record in records:
        failed = [name for name, passed in record["checks"].items() if not passed]
        lines.append(
            f"| {record['followup']} | {record['branch']} | {record['status']} | "
            f"{'通过' if not failed else '失败:' + ','.join(failed)} | {record.get('object_document_ids')} | "
            f"{record.get('citation_document_ids')} |"
        )
    lines += [
        "",
        "## 说明",
        "",
        "- 本报告只覆盖合成合同、指定实现、指定问题与开关组合；不证明业务效果或法律适用正确。",
        "- 规则模式不调用模型，token 记为 null，不能与模型评测结果合并。",
        "- 保留集仅在首次正式模型评测前封存；本脚本按开发集/保留集同时运行只是工程核对。",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="report directory (must be gitignored output/)")
    parser.add_argument("--use-llm", action="store_true", help="call the configured local model")
    parser.add_argument("--limit", type=int, default=0, help="run only the first N sessions (debug)")
    args = parser.parse_args()

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    fixtures = load_fixtures()
    sessions = fixtures.SESSIONS[: args.limit or None]
    followups_by_id = {followup["id"]: followup for followup in fixtures.followups()}

    records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="lexvault-multiturn-eval-") as temporary:
        workdir = Path(temporary)
        old_data = os.environ.get("LAW_REVIEW_DATA_DIR")
        os.environ["LAW_REVIEW_DATA_DIR"] = str(workdir / "setup")
        os.environ["LAW_REVIEW_AUTH_MODE"] = "local"
        try:
            for session in sessions:
                base_path = workdir / f"{session['id']}-base.db"
                base = setup_base(base_path, session, fixtures, args.use_llm)
                for branch in BRANCHES:
                    # One branch database per session: follow-ups chain in order,
                    # while all branches start from the same frozen base.
                    run_path = workdir / f"{session['id']}-{branch}.db"
                    copy_database(base_path, run_path)
                    for followup in session["followups"]:
                        if followup["id"] not in followups_by_id:
                            continue
                        record = run_branch(run_path, base, followup, branch, session, fixtures, args.use_llm)
                        record["checks"] = evaluate(followup, record, base)
                        records.append(record)
        finally:
            if old_data is None:
                os.environ.pop("LAW_REVIEW_DATA_DIR", None)
            else:
                os.environ["LAW_REVIEW_DATA_DIR"] = old_data

    meta = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "use_llm": args.use_llm,
        "session_count": len(sessions),
        "followup_count": len({record["followup"] for record in records}),
    }
    summary = summarize(records, followups_by_id)
    (output / "report.json").write_text(
        json.dumps({"meta": meta, "summary": summary, "records": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output / "report.md").write_text(render_report(meta, summary, records), encoding="utf-8")
    print(render_report(meta, summary, records))
    if not args.use_llm:
        print("\n注意：规则模式仅验证管线与状态契约，不是模型质量评测。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
