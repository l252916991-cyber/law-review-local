"""Seed synthetic capacity fixtures for the E33 deployment-acceptance smoke test.

All text is generated from fixed word lists. This script must never be pointed at a
directory holding real case material: it creates its own cases and never deletes data.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SURNAMES = "张李王刘陈杨赵黄周吴徐孙马朱胡林郭何高罗"
GIVEN_NAMES = ["伟", "娜", "强", "敏", "磊", "静", "军", "洋", "勇", "艳", "杰", "娟"]
COMPANIES = ["恒远商贸有限公司", "嘉禾建设集团", "云启科技股份有限公司", "金泰物流有限公司", "昌隆实业有限公司"]
DOC_TYPES = ["银行流水", "合同协议", "借条", "笔录", "鉴定意见", "聊天记录", "转账凭证", "证人证言"]
ACTIONS = [
    "向{company}转账人民币{amount}元，摘要为“{memo}”",
    "与{company}签订编号为 {contract} 的采购合同，约定分三期付款",
    "出具借条一份，载明借款本金人民币{amount}元，月息按同期利率计算",
    "在询问笔录中陈述，双方于{date}在办公室就还款计划进行协商",
    "微信聊天记录显示，对方承认尚欠人民币{amount}元并承诺月底归还",
    "鉴定意见认定涉案印章与样本印章存在形态差异，需进一步核验",
]
MEMOS = ["货款", "借款", "保证金", "往来款", "服务费", "工程款"]
CATEGORIES = ["书证", "物证", "证人证言", "电子数据", "鉴定意见", "勘验笔录"]


def _rng(seed: int) -> random.Random:
    return random.Random(seed)


def _person(rng: random.Random) -> str:
    return rng.choice(SURNAMES) + rng.choice(GIVEN_NAMES)


def _amount(rng: random.Random) -> str:
    return f"{rng.randint(1, 900) * 1000:,}"


def _date(rng: random.Random) -> str:
    return f"2025-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"


def build_page_text(rng: random.Random, document_name: str, page_no: int, total_pages: int) -> str:
    """Synthetic page text with high term overlap so search exercises real scans."""
    lines = [f"【合成材料·非真实案件】{document_name} 第{page_no}/{total_pages}页"]
    for _ in range(6):
        template = rng.choice(ACTIONS)
        lines.append(
            template.format(
                company=rng.choice(COMPANIES),
                amount=_amount(rng),
                memo=rng.choice(MEMOS),
                contract=f"HT{rng.randint(2024, 2026)}{rng.randint(1000, 9999)}",
                date=_date(rng),
            )
        )
    lines.append(
        f"经办人：{_person(rng)}；见证人：{_person(rng)}；"
        f"材料形成时间：{_date(rng)}；本页关键词：借款、合同、转账、人民币、身份证。"
    )
    return "\n".join(lines)


def seed(
    data_dir: Path,
    *,
    cases: int,
    docs_per_case: int,
    pages_per_doc: int,
    evidence_per_case: int,
    annotations_per_evidence: int,
    audit_events: int,
    conversations: int,
    agent_runs: int,
    seed_value: int = 20260909,
) -> dict[str, Any]:
    data_dir = data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["LAW_REVIEW_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB", str(data_dir / "checkpoints.sqlite"))
    os.environ.setdefault("LAW_REVIEW_AUTH_MODE", "local")

    from app.db import connect, init_db, now
    from app.services import index_upload

    init_db(seed=False)
    rng = _rng(seed_value)
    ts = now()
    summary: dict[str, Any] = {"data_dir": str(data_dir), "cases": []}

    for case_index in range(1, cases + 1):
        with connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO cases(title, case_no, case_type, client_name, status, description, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"容量冒烟-合成案件{case_index}-{pages_per_doc * docs_per_case}页",
                    f"（2026）合成字第{case_index:03d}号",
                    rng.choice(["刑事", "民事", "金融犯罪", "合同纠纷"]),
                    rng.choice(COMPANIES),
                    "阅卷中",
                    "容量冒烟用合成数据，不含任何真实案件材料。",
                    ts,
                    ts,
                ),
            )
            case_id = int(cursor.lastrowid or 0)
            conn.commit()

        documents: list[int] = []
        for doc_index in range(1, docs_per_case + 1):
            document_name = f"合成材料{case_index}-{doc_index:03d}-{rng.choice(DOC_TYPES)}.txt"
            pages = [
                build_page_text(rng, document_name, page_no, pages_per_doc)
                for page_no in range(1, pages_per_doc + 1)
            ]
            document = index_upload(
                case_id,
                document_name,
                # Form feed is the TXT page separator, so pages_per_doc is honoured.
                "\f".join(pages).encode("utf-8"),
                "text/plain",
                import_key=f"capacity:{seed_value}:{case_index}:{doc_index}",
            )
            documents.append(int(document["id"]))

        with connect() as conn:
            evidence_ids: list[int] = []
            for evidence_index in range(1, evidence_per_case + 1):
                source = documents[evidence_index % len(documents)] if documents else None
                cursor = conn.execute(
                    """
                    INSERT INTO evidence(case_id, title, category, fact, credibility, source_document_id,
                                         source_page_start, source_page_end, quote, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        case_id,
                        f"合成证据{evidence_index:04d}",
                        rng.choice(CATEGORIES),
                        f"证明{_person(rng)}与{_person(rng)}之间存在人民币{_amount(rng)}元的资金往来。",
                        rng.choice(["待核验", "已核验", "存疑"]),
                        source,
                        rng.randint(1, max(1, pages_per_doc)),
                        rng.randint(1, max(1, pages_per_doc)),
                        f"合成引文：向{COMPANIES[0]}转账人民币{_amount(rng)}元。",
                        rng.choice(["待复核", "已确认", "已排除"]),
                        ts,
                    ),
                )
                evidence_ids.append(int(cursor.lastrowid or 0))
            for index, evidence_id in enumerate(evidence_ids[1:], start=1):
                conn.execute(
                    """
                    INSERT INTO evidence_relations(case_id, from_evidence_id, to_evidence_id, relation_type, note)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (case_id, evidence_ids[index - 1], evidence_id, "相互印证", "合成关系"),
                )
            for evidence_id in evidence_ids:
                for annotation_index in range(1, annotations_per_evidence + 1):
                    conn.execute(
                        """
                        INSERT INTO evidence_annotations(evidence_id, user_name, annotation_type, content,
                                                         quote_start, quote_end, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            evidence_id,
                            f"合成律师{annotation_index}",
                            rng.choice(["质证意见", "待核实", "关联线索"]),
                            f"合成标注：需核对该笔人民币{_amount(rng)}元的原始凭证。",
                            rng.randint(0, 20),
                            rng.randint(21, 60),
                            rng.choice(["待处理", "处理中", "已完成"]),
                            ts,
                        ),
                    )
            for audit_index in range(1, audit_events + 1):
                conn.execute(
                    "INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
                    (case_id, rng.choice(["查看卷宗", "检索", "更新证据", "导出材料"]), f"合成审计{audit_index}", ts),
                )
            for conversation_index in range(1, conversations + 1):
                cursor = conn.execute(
                    "INSERT INTO conversations(case_id, user_name, title, created_at) VALUES (?, ?, ?, ?)",
                    (case_id, f"合成律师{conversation_index}", f"合成会话{conversation_index}", ts),
                )
                conversation_id = int(cursor.lastrowid or 0)
                for message_index in range(1, 5):
                    conn.execute(
                        """
                        INSERT INTO messages(conversation_id, role, content, citations_json, route, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            conversation_id,
                            "user" if message_index % 2 else "assistant",
                            f"合成消息{message_index}：请核对人民币{_amount(rng)}元的资金流向。",
                            json.dumps([], ensure_ascii=False),
                            "事实检索",
                            ts,
                        ),
                    )
            for run_index in range(1, agent_runs + 1):
                cursor = conn.execute(
                    """
                    INSERT INTO agent_runs(case_id, question, route, status, retrieval_mode, final_answer,
                                           citations_json, total_ms, runtime, created_at, finished_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        case_id,
                        f"合成问题{run_index}：本案资金往来是否形成完整链条？",
                        "事实检索",
                        "completed",
                        "hybrid_rrf",
                        "合成结论：材料显示存在多笔资金往来，需律师复核。",
                        json.dumps([], ensure_ascii=False),
                        rng.randint(800, 9000),
                        "native",
                        ts,
                        ts,
                    ),
                )
                run_id = int(cursor.lastrowid or 0)
                for node in ("retrieve", "analyze", "critique", "compose"):
                    conn.execute(
                        """
                        INSERT INTO agent_steps(run_id, node_name, agent_role, status, input_json, output_json,
                                                latency_ms, started_at, finished_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (run_id, node, node, "completed", "{}", "{}", rng.randint(50, 3000), ts, ts),
                    )
            for gap_index in range(1, 3):
                conn.execute(
                    """
                    INSERT INTO gap_detections(case_id, run_id, gap_type, severity, description, suggestion,
                                               affected_evidence_ids, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        case_id,
                        None,
                        "证据链缺口",
                        rng.choice(["高", "中", "低"]),
                        f"合成缺口{gap_index}：缺少资金去向的原始凭证。",
                        "建议补充调取银行原始流水。",
                        json.dumps([], ensure_ascii=False),
                        "待核验",
                        ts,
                    ),
                )
            conn.commit()

        page_count = pages_per_doc * docs_per_case
        summary["cases"].append(
            {
                "case_id": case_id,
                "documents": len(documents),
                "pages": page_count,
                "evidence": evidence_per_case,
                "annotations": evidence_per_case * annotations_per_evidence,
                "audit_events": audit_events,
                "conversations": conversations,
                "agent_runs": agent_runs,
            }
        )

    with connect() as conn:
        summary["totals"] = {
            "cases": conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0],
            "documents": conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "pages": conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
            "evidence": conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
            "fts_rows": conn.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0],
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed synthetic capacity fixtures into an isolated data directory.")
    parser.add_argument("--data-dir", required=True, help="Isolated LAW_REVIEW_DATA_DIR (never real case material)")
    parser.add_argument("--cases", type=int, default=1)
    parser.add_argument("--docs-per-case", type=int, default=20)
    parser.add_argument("--pages-per-doc", type=int, default=50)
    parser.add_argument("--evidence-per-case", type=int, default=100)
    parser.add_argument("--annotations-per-evidence", type=int, default=1)
    parser.add_argument("--audit-events", type=int, default=200)
    parser.add_argument("--conversations", type=int, default=10)
    parser.add_argument("--agent-runs", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260909)
    parser.add_argument("--json-out", help="Write the summary JSON to this path")
    arguments = parser.parse_args()
    summary = seed(
        Path(arguments.data_dir),
        cases=arguments.cases,
        docs_per_case=arguments.docs_per_case,
        pages_per_doc=arguments.pages_per_doc,
        evidence_per_case=arguments.evidence_per_case,
        annotations_per_evidence=arguments.annotations_per_evidence,
        audit_events=arguments.audit_events,
        conversations=arguments.conversations,
        agent_runs=arguments.agent_runs,
        seed_value=arguments.seed,
    )
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if arguments.json_out:
        Path(arguments.json_out).write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
