"""Compare pinned existing local models on fresh synthetic seed data only."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.db import db_scope, init_db, now, transaction
from app.local_backends import LocalModels
from app.model_comparison import compare_local_models, private_report

QUESTIONS = (
    ("ordinary", 1, "请梳理固定回报审批的事实与证据。", "success"),
    ("contradiction", 1, "核对固定回报审批矛盾。", "success"),
    ("gaps", 1, "核对固定回报审批矛盾和证据疏漏", "success"),
    ("unrelated-query", 1, "量子香蕉完全无关资料", "source_rejection"),
    ("empty-case", 2, "量子香蕉完全无关资料", "source_rejection"),
)

# Originally frozen independently, then used for protocol/broad-query debugging.
DEVELOPMENT_QUESTIONS: tuple[tuple[str, int, str, str], ...] = (
    ("delivery-paraphrase", 3, "货已经交到客户手里，尾款还没有结清，应核对哪些记录？", "success"),
    ("delivery-broad", 3, "请概括这个案子能确认的事实和需要律师核验的事项。", "success"),
    ("delivery-partial", 3, "能否根据这份签收单确认北极科研许可证已经批准？", "source_rejection"),
    ("delivery-missing", 3, "是哪位经理口头批准免除剩余货款？", "source_rejection"),
    ("delivery-unrelated", 3, "请用银行转账账号推算下一次流星雨的日期。", "source_rejection"),
)

# This independent case later exposed a formatting failure, so it is now dev.
DEVELOPMENT_QUESTIONS += (
    ("rental-broad", 4, "梳理现有材料能够确认的经过和仍需核验的事项。", "success"),
    ("rental-paraphrase", 4, "住户已经搬走，保证金有没有退还的凭据？", "success"),
    ("rental-unrelated", 4, "能否用租金收据确认这栋楼已经取得航空器适航证？", "source_rejection"),
)

# Fixed after the host-rendered findings protocol, before its first evaluation.
DEVELOPMENT_QUESTIONS += (
    ("tuition-broad", 5, "根据已移交资料列出已知事实及仍待处理的事项。", "success"),
    ("tuition-paraphrase", 5, "培训机构停课后，尚有多少学费待退，应核对什么？", "success"),
    ("tuition-unrelated", 5, "能否用这份学费退款单判断卫星频率许可证已经生效？", "source_rejection"),
)

# Final independent case, frozen after v5 input compaction and before inference.
HOLDOUT_QUESTIONS = (
    ("repair-broad", 6, "根据这些材料梳理可确认的经过和待核验事项。", "success"),
    ("repair-paraphrase", 6, "送修之后，有没有修好并交付的记录？", "success"),
    ("repair-unrelated", 6, "能否根据这份维修回执确认国际航线已经获批？", "source_rejection"),
)

# New synthetic holdout frozen before first execution of the v9 grammar scheme.
# It covers supported paraphrase, explicit "no record" limited-scope answers,
# not-mentioned insufficiency and unrelated questions. The materials are
# synthetic and are not E17 business evaluation data.
STABILITY_HOLDOUT = (
    ("supplier-first-receipt", 7, "第一批货物有没有签收记录？", "success"),
    ("supplier-refund-record", 7, "第二批货的退款有没有记录？", "success"),
    ("supplier-waiver-decider", 7, "免除尾款的书面决定由谁作出？", "source_rejection"),
    ("supplier-unrelated", 7, "请根据首付款金额计算台风登陆时间。", "source_rejection"),
    ("insurance-loss-known", 8, "理赔申请列明了哪些损失？", "success"),
    ("insurance-decision-record", 8, "有没有理赔决定书？", "success"),
    ("insurance-payment-unmentioned", 8, "理赔款是否已经支付？", "source_rejection"),
    ("insurance-unrelated", 8, "请用损失清单判断地震预警是否已发布。", "source_rejection"),
    ("labor-payment-receipt", 9, "工资支付有没有收据？", "success"),
    ("labor-award-unmentioned", 9, "仲裁庭是否已经作出裁决？", "source_rejection"),
    ("rental-equipment-deposit-receipt", 10, "押金退还的凭证有吗？", "success"),
    ("rental-equipment-penalty-unmentioned", 10, "承租人是否已支付逾期违约金？", "source_rejection"),
)


def seed_holdout() -> None:
    with transaction() as conn:
        conn.execute("INSERT INTO cases(id,title,created_at,updated_at) VALUES (3,'合成货物交付案',?,?)", (now(), now()))
        for name, text in (
            ("合成配送签收单", "配送签收单：2025年3月1日，客户确认收到全部货品，签收数量100箱。"),
            ("合成对账函", "对账函：发票货款人民币十万元，已支付六万元，尚欠四万元。银行转账记录只显示已付六万元。材料未记录剩余款项减免或相关口头授权。"),
        ):
            doc = conn.execute("INSERT INTO documents(case_id,name,stored_path,mime_type,pages,created_at,updated_at) "
                "VALUES (3,?,'','text/plain',1,?,?)", (name, now(), now())).lastrowid
            conn.execute("INSERT INTO pages(document_id,page_no,text) VALUES (?,1,?)", (doc, text))
        conn.execute("INSERT INTO cases(id,title,created_at,updated_at) VALUES (4,'合成租赁案',?,?)", (now(), now()))
        doc = conn.execute("INSERT INTO documents(case_id,name,stored_path,mime_type,pages,created_at,updated_at) "
            "VALUES (4,'合成交接与收款记录','','text/plain',1,?,?)", (now(), now())).lastrowid
        conn.execute("INSERT INTO pages(document_id,page_no,text) VALUES (?,1,?)", (doc,
            "租赁交接记录：2025年6月30日承租人已腾退房屋并归还钥匙。租金收据：租金已付清。保证金返还凭证未见于本次移交资料，无法确认是否已退还。以上为合成材料。"))
        conn.execute("INSERT INTO cases(id,title,created_at,updated_at) VALUES (5,'合成学费退费案',?,?)", (now(), now()))
        doc = conn.execute("INSERT INTO documents(case_id,name,stored_path,mime_type,pages,created_at,updated_at) "
            "VALUES (5,'合成课程退费对账单','','text/plain',1,?,?)", (now(), now())).lastrowid
        conn.execute("INSERT INTO pages(document_id,page_no,text) VALUES (?,1,?)", (doc,
            "课程退费对账单：培训机构于2025年9月1日停课。应退学费5000元，已退2000元，尚待退付3000元。需核对后续退款凭证及到账情况。以上均为合成数据。"))
        conn.execute("INSERT INTO cases(id,title,created_at,updated_at) VALUES (6,'合成设备保修案',?,?)", (now(), now()))
        doc = conn.execute("INSERT INTO documents(case_id,name,stored_path,mime_type,pages,created_at,updated_at) "
            "VALUES (6,'合成设备维修回执','','text/plain',1,?,?)", (now(), now())).lastrowid
        conn.execute("INSERT INTO pages(document_id,page_no,text) VALUES (?,1,?)", (doc,
            "设备维修回执：2025年10月1日供应商已收到送修设备，确认故障发生在保修期内，同意免费维修。移交材料未见维修完成或交付回执，尚需核验维修进度和交付情况。全部内容为合成。"))


def seed_stability_holdout() -> None:
    with transaction() as conn:
        conn.execute("INSERT INTO cases(id,title,created_at,updated_at) VALUES (7,'合成供应商对账案',?,?)", (now(), now()))
        doc = conn.execute("INSERT INTO documents(case_id,name,stored_path,mime_type,pages,created_at,updated_at) "
            "VALUES (7,'合成供应商对账说明','','text/plain',1,?,?)", (now(), now())).lastrowid
        conn.execute("INSERT INTO pages(document_id,page_no,text) VALUES (?,1,?)", (doc,
            "供应商对账说明：采购合同约定分两批交付。第一批100件已于2025年6月1日签收；第二批80件尚未交付，亦未见退款记录。首付款30万元已支付，尾款20万元未付。以上均为合成材料。"))
        conn.execute("INSERT INTO cases(id,title,created_at,updated_at) VALUES (8,'合成保险理赔案',?,?)", (now(), now()))
        doc = conn.execute("INSERT INTO documents(case_id,name,stored_path,mime_type,pages,created_at,updated_at) "
            "VALUES (8,'合成保险理赔申请','','text/plain',1,?,?)", (now(), now())).lastrowid
        conn.execute("INSERT INTO pages(document_id,page_no,text) VALUES (?,1,?)", (doc,
            "保险理赔申请：2025年7月20日提交，事故发生于2025年6月30日，损失清单列明设备维修费3万元。截至本次移交材料，尚未作出理赔决定，未见理赔决定书。以上均为合成材料。"))
        conn.execute("INSERT INTO cases(id,title,created_at,updated_at) VALUES (9,'合成劳动仲裁案',?,?)", (now(), now()))
        doc = conn.execute("INSERT INTO documents(case_id,name,stored_path,mime_type,pages,created_at,updated_at) "
            "VALUES (9,'合成仲裁申请与交接记录','','text/plain',1,?,?)", (now(), now())).lastrowid
        conn.execute("INSERT INTO pages(document_id,page_no,text) VALUES (?,1,?)", (doc,
            "仲裁申请书：申请人主张2025年3月工资未支付。被申请人称已现金支付，但未提交收据。交接清单载明考勤记录已移交，未见工资支付凭证。以上均为合成材料。"))
        conn.execute("INSERT INTO cases(id,title,created_at,updated_at) VALUES (10,'合成设备租赁案',?,?)", (now(), now()))
        doc = conn.execute("INSERT INTO documents(case_id,name,stored_path,mime_type,pages,created_at,updated_at) "
            "VALUES (10,'合成设备租赁与交还记录','','text/plain',1,?,?)", (now(), now())).lastrowid
        conn.execute("INSERT INTO pages(document_id,page_no,text) VALUES (?,1,?)", (doc,
            "设备租赁合同：2025年8月1日起租，租期三个月，押金2万元。交还记录未见，押金退还凭证未见。以上均为合成材料。"))


def _row(report: dict, name: str) -> dict:
    comparisons = report.get("comparisons", [])
    independent = comparisons[1] if len(comparisons) > 1 else (comparisons[0] if comparisons else {})
    native = independent.get("native_usage", {})
    candidate = report.get("candidate_usage", {})
    candidate_tokens = candidate.get("input_tokens", 0) + candidate.get("output_tokens", 0)
    native_tokens = native.get("input_tokens", 0) + native.get("output_tokens", 0)
    ratio = candidate_tokens / native_tokens if native_tokens else None
    return {
        "name": name,
        "expected": report["expected"],
        "business_outcome": report["candidate_business_outcome"],
        "candidate_decision": report.get("candidate_decision"),
        "candidate_status": report["candidate"]["status"],
        "engineering_passed": report["engineering_passed"],
        "measurement_valid": report["measurement_valid"],
        "candidate_calls": candidate.get("calls"),
        "candidate_input_tokens": candidate.get("input_tokens"),
        "candidate_output_tokens": candidate.get("output_tokens"),
        "candidate_tokens": candidate_tokens,
        "native_independent_tokens": native_tokens,
        "token_ratio": ratio,
        "usage_within_1_10": bool(ratio is not None and ratio <= 1.10),
        "source_identity": report["source_identity"],
    }


def _questions(*, include_holdout: bool, include_stability: bool, selected: set[str]) -> tuple[tuple[str, int, str, str, str], ...]:
    selected_sets = []
    if include_holdout:
        selected_sets.extend([("original", QUESTIONS), ("development", DEVELOPMENT_QUESTIONS),
                              ("holdout", HOLDOUT_QUESTIONS)])
    if include_stability:
        selected_sets.append(("stability_holdout", STABILITY_HOLDOUT))
    if not selected_sets:
        selected_sets.append(("original", QUESTIONS))
    questions = tuple((name, case_id, question, expected, set_name)
                      for set_name, entries in selected_sets
                      for name, case_id, question, expected in entries)
    if selected:
        known = {entry[0] for entry in questions}
        missing = selected - known
        if missing:
            raise SystemExit(f"unknown selected questions: {sorted(missing)}")
        questions = tuple(entry for entry in questions if entry[0] in selected)
    return questions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True, help="private host JSON: endpoint, identities, models")
    parser.add_argument("--framework-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-holdout", action="store_true", help="also evaluate the fixed independent synthetic delivery case")
    parser.add_argument("--include-stability", action="store_true", help="also evaluate the new v9 stability holdout")
    parser.add_argument("--selected", default="", help="comma-separated question names")
    parser.add_argument("--run-suffix", default="", help="suffix appended to each question output directory")
    parser.add_argument("--enforce-usage", action="store_true", help="return non-zero when any per-question token ratio exceeds 1.10")
    args = parser.parse_args()
    selected = {item.strip() for item in args.selected.split(",") if item.strip()}
    questions = _questions(include_holdout=args.include_holdout, include_stability=args.include_stability,
                           selected=selected)
    args.output.mkdir(parents=True, mode=0o700)
    values = json.loads(args.registry.read_text())
    models = LocalModels(**values)
    source = args.output / "synthetic-source" / "host.sqlite"
    reports = []
    unknown_usage = 0
    with db_scope(source):
        init_db(seed=True)
        with transaction() as conn:
            conn.execute("INSERT INTO cases(title,created_at,updated_at) VALUES ('Synthetic empty comparison',?,?)", (now(), now()))
        if args.include_holdout:
            seed_holdout()
        if args.include_stability:
            seed_stability_holdout()
        private_report(args.output / "frozen-samples.json", {
            "questions": [list(entry[:4]) for entry in questions],
            "sets": sorted({entry[4] for entry in questions}),
            "selected": sorted(selected) if selected else [],
            "scope": "synthetic development/holdout, frozen before this execution; not E17",
            "registry_identity": {"models": values["models"],
                "exclusive_residency": values.get("exclusive_residency", False),
                "answerability_grammar": values.get("answerability_grammar", False)},
        })
        for name, case_id, question, expected, _set_name in questions:
            output = args.output / (name + args.run_suffix)
            report = compare_local_models(case_id=case_id, question=question, expected=expected, models=models,
                framework_python=args.framework_python, output=output)
            row = _row(report, name)
            row["output"] = str(output.relative_to(args.output))
            reports.append(row)
            unknown_usage += report.get("candidate_usage", {}).get("unknown_usage") or 0
            print(json.dumps(row, ensure_ascii=False), flush=True)
    total_candidate = sum(row["candidate_tokens"] for row in reports)
    total_native = sum(row["native_independent_tokens"] for row in reports)
    usage_passed = all(row["usage_within_1_10"] for row in reports)
    summary = {"material": "built-in synthetic seed only", "runs": reports,
        "measurement_valid": models.transport is None,
        "usage_budget_passed": usage_passed,
        "aggregate_token_ratio": total_candidate / total_native if total_native else None,
        "engineering_passed": all(row["engineering_passed"] for row in reports),
        "unknown_usage": unknown_usage,
        "g2_accepted": False}
    private_report(args.output / "summary.json", summary)
    engineering_ok = summary["engineering_passed"]
    usage_ok = summary["usage_budget_passed"] or not args.enforce_usage
    return 0 if engineering_ok and usage_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
