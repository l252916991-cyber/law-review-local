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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True, help="private host JSON: endpoint, identities, models")
    parser.add_argument("--framework-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-holdout", action="store_true", help="also evaluate the fixed independent synthetic delivery case")
    args = parser.parse_args()
    args.output.mkdir(parents=True, mode=0o700)
    values = json.loads(args.registry.read_text())
    models = LocalModels(**values)
    source = args.output / "synthetic-source" / "host.sqlite"
    reports = []
    with db_scope(source):
        init_db(seed=True)
        with transaction() as conn:
            conn.execute("INSERT INTO cases(title,created_at,updated_at) VALUES ('Synthetic empty comparison',?,?)", (now(), now()))
        if args.include_holdout:
            seed_holdout()
        questions = QUESTIONS + (DEVELOPMENT_QUESTIONS + HOLDOUT_QUESTIONS if args.include_holdout else ())
        private_report(args.output / "frozen-samples.json", {"original": QUESTIONS,
            "development": DEVELOPMENT_QUESTIONS if args.include_holdout else (),
            "holdout": HOLDOUT_QUESTIONS if args.include_holdout else (),
            "scope": "synthetic development, frozen before evaluation; not E17"})
        for name, case_id, question, expected in questions:
            report = compare_local_models(case_id=case_id, question=question, expected=expected, models=models,
                framework_python=args.framework_python, output=args.output / name)
            reports.append({"name": name, "engineering_passed": report["engineering_passed"],
                "candidate": report["candidate"]["status"], "business_outcome": report["candidate_business_outcome"],
                "source_identity": report["source_identity"]})
            print(json.dumps(reports[-1]), flush=True)
    private_report(args.output / "summary.json", {"material": "built-in synthetic seed only", "runs": reports,
        "measurement_valid": models.transport is None, "g2_accepted": False})
    return 0 if all(r["engineering_passed"] for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
