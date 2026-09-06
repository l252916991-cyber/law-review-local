"""Deterministic 240-question, page-grounded benchmark for LexVault retrieval."""

from __future__ import annotations

from typing import Any


SURNAMES = ["赵", "钱", "孙", "李", "周", "吴", "郑", "王", "冯", "陈", "褚", "卫"]
INDUSTRIES = ["新能源", "医疗器械", "物流", "教育", "软件", "农业", "建筑", "传媒", "环保", "零售", "制造", "旅游"]


def build_rag_benchmark() -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for case_index, (surname, industry) in enumerate(zip(SURNAMES, INDUSTRIES), 1):
        case_key = f"RAG-{case_index:02d}"
        person = f"{surname}某"
        amount = 137 + case_index * 19
        rate = 5 + case_index
        investors = 21 + case_index * 3
        account = f"6222{case_index:04d}9917"
        approval_date = f"2025-{(case_index % 9) + 1:02d}-{(case_index * 2 % 25) + 1:02d}"
        meeting_date = f"2025-{(case_index % 9) + 2:02d}-{(case_index * 3 % 25) + 1:02d}"
        documents = [
            {
                "name": f"{case_key}_01_立案材料.txt",
                "pages": [
                    f"案件编号{case_key}。涉案企业从事{industry}业务，负责人为{person}。",
                    f"初步统计共向{investors}名参与人募集人民币{amount}万元。统计口径不含已退款项。",
                ],
            },
            {
                "name": f"{case_key}_02_询问笔录.txt",
                "pages": [
                    f"{person}陈述自己从未批准承诺收益，只负责{industry}技术工作。",
                    f"{person}又确认参加了{meeting_date}经营会议，但称没有看到宣传稿。",
                ],
            },
            {
                "name": f"{case_key}_03_电子邮件.txt",
                "pages": [
                    f"邮件日期{approval_date}。{person}回复：同意宣传稿采用年化{rate}%固定回报表述，请市场部执行。",
                    f"附件版本记录显示{person}账号在回复后十分钟下载了最终宣传稿。",
                ],
            },
            {
                "name": f"{case_key}_04_银行流水.txt",
                "pages": [
                    f"募集专户尾号{account[-4:]}，累计入账{amount}万元，付款摘要为投资款。",
                    f"其中{case_index + 7}万元转入{person}个人账户{account}，用途备注为顾问费。",
                ],
            },
            {
                "name": f"{case_key}_05_会议纪要.txt",
                "pages": [
                    f"{meeting_date}会议参会人员包括{person}、市场负责人和财务负责人，议题为募集方案。",
                    f"会议决定对外宣传固定回报{rate}%，宣传稿须经{person}最后确认。",
                ],
            },
            {
                "name": f"{case_key}_06_退款清单.txt",
                "pages": [
                    f"截至2026-01-{case_index + 2:02d}，已向{case_index + 2}人退款{case_index * 2 + 5}万元。",
                    "退款均由募集专户支付，未发现现金退款记录。",
                ],
            },
        ]
        specs = [
            ("负责人是谁？", [(0, 1)]),
            ("企业从事什么业务？", [(0, 1)]),
            ("募集参与人数是多少？", [(0, 2)]),
            ("募集总额是多少万元？", [(0, 2), (3, 1)]),
            ("负责人如何解释自己未批准固定回报？", [(1, 1)]),
            ("负责人承认参加哪一天的会议？", [(1, 2), (4, 1)]),
            ("哪份材料直接证明负责人同意固定回报？", [(2, 1)]),
            ("邮件批准的年化回报率是多少？", [(2, 1)]),
            ("负责人是否下载过最终宣传稿？", [(2, 2)]),
            ("募集专户尾号是什么？", [(3, 1)]),
            ("转入负责人个人账户多少万元？", [(3, 2)]),
            ("个人收款的用途备注是什么？", [(3, 2)]),
            ("经营会议讨论了什么议题？", [(4, 1)]),
            ("宣传稿最后由谁确认？", [(4, 2)]),
            ("固定收益承诺由哪些材料相互印证？", [(2, 1), (4, 2)]),
            ("不知情辩解可由哪些客观记录反驳？", [(2, 1), (2, 2), (4, 1)]),
            ("已经退款多少万元？", [(5, 1)]),
            ("退款通过什么账户支付？", [(5, 2)]),
            ("材料是否记载境外加密货币账户？", []),
            ("材料能否证明负责人持有火星采矿许可证？", []),
        ]
        for question_index, (query, expected) in enumerate(specs, 1):
            questions.append(
                {
                    "id": f"{case_key}-Q{question_index:02d}",
                    "case_key": case_key,
                    "query": f"{case_key}：{query}",
                    "expected": [
                        {"document": documents[doc_index]["name"], "page": page_no}
                        for doc_index, page_no in expected
                    ],
                    "answerable": bool(expected),
                    "documents": documents,
                }
            )
    if len(questions) != 240 or len({item["id"] for item in questions}) != 240:
        raise AssertionError("RAG benchmark must contain exactly 240 unique records")
    return questions
