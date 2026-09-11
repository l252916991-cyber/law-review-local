"""Seed one complex synthetic demo case for manual website feature testing.

Unlike ``capacity_seed.py`` (which maximises row counts to exercise capacity), this
script writes a single coherent case: consistent people, companies, accounts,
amounts and dates across every volume, so that检索、证据链、银行流水图谱、缺口分析
and 问答 all have something meaningful to work on.

All text is generated from fixed templates. The script never deletes data and must
never be pointed at a directory holding real case material.

Usage::

    python scripts/seed_demo_case.py --data-dir /path/to/data
    python scripts/seed_demo_case.py --data-dir /path/to/data --json-out output/demo-case.json
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASE_TITLE = "云启科技股份有限公司涉嫌非法吸收公众存款案（合成演示）"
CASE_NO = "（2026）京海检刑诉字第0287号"
CASE_TYPE = "金融犯罪"
CLIENT_NAME = "云启科技股份有限公司"
PLATFORM = "云启财富App"
GROUP = "云启科技股份有限公司"
SHELL_A = "恒远商贸有限公司"
SHELL_B = "金泰物流有限公司"
INVESTORS = ("陈敏", "刘洋", "周静", "孙鹏", "郑洁", "何瑞")
ROLES = {"张伟": "法定代表人、实际控制人", "李娜": "财务总监", "王强": "业务部经理", "赵敏": "技术负责人"}
ACCOUNT_GROUP = "6222 0210 0100 1234 567"
ACCOUNT_SHELL_A = "6228 4801 8800 7654 321"
ACCOUNT_SHELL_B = "6217 0033 2200 1188 990"
TOTAL_RAISED = "128,640,000"
TOTAL_UNPAID = "43,270,000"
TOTAL_VICTIMS = "327"
FIRST_DATE = "2023年3月14日"
LAST_DATE = "2025年11月6日"
BANK = "京海银行城中支行"

# Per-case gold pages for this case's own RAG evaluation. Document names and page
# numbers are verified against the generated volumes; the application refuses to
# evaluate a non-demo case without its own ground truth, so this is shipped as an
# artifact rather than any fallback to the built-in demo answers.
GROUND_TRUTH: list[dict[str, Any]] = [
    {
        "query": "本案募集资金总额、投资人人数和未兑付金额分别是多少？",
        "expected": [("01-起诉意见书-云启科技.txt", 2), ("10-资金归集与流向专项审计报告.txt", 3)],
    },
    {
        "query": "募集资金主要通过哪些账户完成归集和划转？",
        "expected": [
            ("01-起诉意见书-云启科技.txt", 3),
            ("10-资金归集与流向专项审计报告.txt", 2),
            ("12-司法会计鉴定意见书.txt", 3),
        ],
    },
    {
        "query": "审计报告认定的资金去向和资金空转情况是什么？",
        "expected": [("10-资金归集与流向专项审计报告.txt", 4), ("10-资金归集与流向专项审计报告.txt", 5)],
    },
    {
        "query": "业务部对外宣传时使用了哪些与事实不符的表述？",
        "expected": [("06-王强询问笔录.txt", 3), ("13-微信聊天记录提取报告.txt", 4)],
    },
    {
        "query": "张伟个人及亲属支取的 1260 万元用于什么用途？",
        "expected": [("10-资金归集与流向专项审计报告.txt", 5), ("12-司法会计鉴定意见书.txt", 5)],
    },
    {"query": "本案是否涉及毒品犯罪的资金？", "expected": []},
    {"query": "本案是否涉及内幕交易？", "expected": []},
]


def _page(document_name: str, page_no: int, total_pages: int, *blocks: str) -> str:
    body = "\n".join(blocks)
    return f"【合成演示材料·非真实案件】{document_name} 第{page_no}/{total_pages}页\n{body}"


def _person(person: str) -> str:
    return f"{person}（{ROLES[person]}）" if person in ROLES else person


def _documents() -> list[dict[str, Any]]:
    """Volume catalogue; each entry returns its own pages so counts stay honest."""
    volumes: list[dict[str, Any]] = []

    def add(name: str, pages: list[str]) -> None:
        volumes.append({"name": name, "pages": pages})

    def build_pages(name: str, blocks: list[str]) -> list[str]:
        total = len(blocks)
        return [_page(name, i, total, block) for i, block in enumerate(blocks, start=1)]

    # 1. 起诉意见书
    name = "01-起诉意见书-云启科技.txt"
    add(name, build_pages(name, [
        f"京海市公安局城中分局\n起诉意见书\n京公城诉字〔2026〕第 0287 号\n\n"
        f"犯罪嫌疑人{_person('张伟')}，男，1979年生，{GROUP}法定代表人、实际控制人，"
        f"因涉嫌非法吸收公众存款罪，于 2025年11月12日被刑事拘留，同年12月18日经本院批准逮捕。\n"
        f"犯罪嫌疑人{_person('李娜')}，女，1985年生，{GROUP}财务总监，负责平台资金归集与划转。\n"
        f"犯罪嫌疑人{_person('王强')}，男，1988年生，{GROUP}业务部经理，负责投资人拓展与返利发放。",
        f"经依法侦查查明：2023年3月至 2025年11月，犯罪嫌疑人张伟等人未经国家金融管理部门批准，"
        f"以{GROUP}名义开发运营“{PLATFORM}”，通过线下推介会、微信群、短视频推广等方式公开宣传，"
        f"承诺年化收益 8% 至 14%，向社会不特定对象吸收资金，累计吸收公众存款人民币 {TOTAL_RAISED} 元，"
        f"涉及投资人 {TOTAL_VICTIMS} 名，案发时尚未兑付人民币 {TOTAL_UNPAID} 元。",
        f"资金去向：吸收资金进入{GROUP}在{BANK}开立的基本账户（账号 {ACCOUNT_GROUP}）后，"
        f"由李娜按张伟指令将大额资金划转至关联公司{SHELL_A}账户（账号 {ACCOUNT_SHELL_A}），"
        f"再经{SHELL_B}账户（账号 {ACCOUNT_SHELL_B}）完成过桥，最终用于支付前期投资人本息、"
        f"购买办公与车辆资产及对外借款，形成典型的“资金池”滚动运作。",
        f"证明上述事实的证据如下：一、犯罪嫌疑人供述与辩解，含张伟询问笔录三次、李娜及王强询问笔录；"
        f"二、被害人陈述，含陈敏、刘洋、周静等投资人陈述；三、证人证言；"
        f"四、书证：{PLATFORM}理财产品服务协议、后台运营邮件、微信聊天记录提取报告；"
        f"五、鉴定意见：司法会计鉴定意见书、资金归集与流向专项审计报告；六、勘验笔录及电子数据。",
        "法律依据：犯罪嫌疑人张伟、李娜、王强的行为已触犯《中华人民共和国刑法》第一百七十六条，"
        "涉嫌非法吸收公众存款罪；张伟系组织、策划、指挥者，应认定为主犯；李娜负责资金归集与划转，"
        "王强负责公开宣传与返利发放，均起主要作用。",
        "综上所述，本案犯罪事实清楚，证据确实、充分，现将本案移送审查起诉。\n"
        "此致\n京海市人民检察院\n\n京海市公安局城中分局（印）\n2026年1月20日",
    ]))

    # 2. 立案决定书及受案登记表
    name = "02-受案登记表及立案决定书.txt"
    add(name, build_pages(name, [
        f"京海市公安局城中分局\n受案登记表\n\n"
        f"报案人：陈敏，女，联系电话已隐去，住京海市海淀区。\n"
        f"报案时间：2025年11月2日 10 时 20 分。\n"
        f"报案内容：报案人陈敏称其在“{PLATFORM}”投入资金 86 万元，自 2025年9月起无法提现，"
        f"平台客服失联，办公场所已停止营业，怀疑被骗。",
        f"经初步审查：{PLATFORM}运营主体为{GROUP}，注册地京海市海淀区某科技园，"
        f"平台以“云启稳盈”系列理财产品对外募集资金，承诺年化收益 8% 至 14%。"
        f"公安机关于 2025年11月6日受理，同年11月8日对本案立案侦查，"
        f"侦查期限自立案之日起计算。\n\n京海市公安局城中分局（印）\n2025年11月8日",
    ]))

    # 3-4. 张伟询问笔录（两次）
    name = "03-张伟询问笔录-第一次.txt"
    add(name, build_pages(name, [
        f"询问时间：2025年11月12日 14 时 00 分至 18 时 30 分\n"
        f"询问地点：京海市公安局城中分局办案区询问室\n"
        f"询问人：李警官、赵警官　记录人：王警官\n被询问人：犯罪嫌疑人张伟",
        f"问：{GROUP}的主营业务是什么？\n"
        f"答：公司注册资本 5000 万元，主营软件开发。2023年初我提出做互联网理财，"
        f"开发了“{PLATFORM}”，对外叫“云启稳盈 1 号”。\n"
        f"问：平台吸收资金是否取得金融牌照？\n"
        f"答：没有。我们只在市场监管部门做了经营范围变更，没有向金融监管部门申请备案。",
        f"问：理财产品的收益和期限如何约定？\n"
        f"答：分 3 个月、6 个月、12 个月三档，年化收益 8% 到 14%，按月返息，到期还本。"
        f"早期为了打开市场，实际返息比合同写的高一些。",
        f"问：投资人的资金进入哪个账户？\n"
        f"答：进入公司{BANK}的基本户，账号是 {ACCOUNT_GROUP}，由李娜负责管理。\n"
        f"问：资金到账后如何使用？\n"
        f"答：一部分支付到期投资人的本息，一部分用于公司运营和推广，还有一部分我让李娜"
        f"转到恒远商贸的账上，用起来方便一些。",
        f"问：恒远商贸与{GROUP}是什么关系？\n"
        f"答：恒远商贸的实际控制人也是我，是我的关联公司，主要帮我走账。\n"
        f"问：{SHELL_B}呢？\n"
        f"答：金泰物流是我朋友的公司，我借用它的账户过账，具体是李娜对接的。",
        f"问：目前还有多少未兑付？\n"
        f"答：具体数字要问李娜，大概四千多万元。2025年9月开始提现的人太多，"
        f"资金池转不动了，我们停止了兑付。",
        f"问：你是否认罪认罚？\n"
        f"答：我承认吸收资金没有经过批准，也承认现在兑付不了，但我原本是想正常经营的。\n"
        f"问：以上笔录你看过吗？\n答：看过，和我说的一致。\n"
        f"被询问人（签名）：张伟　2025年11月12日",
    ]))

    name = "04-张伟询问笔录-第二次.txt"
    add(name, build_pages(name, [
        "补充询问时间：2025年12月3日 9 时 30 分至 12 时 00 分\n"
        "询问人：李警官　记录人：王警官\n被询问人：犯罪嫌疑人张伟",
        f"问：审计反映你从恒远商贸账户支取 1260 万元购置房产，是否属实？\n"
        f"答：属实。2024年6月我在京海市朝阳区买了两套住宅，一套自住，一套给父母，"
        f"房款是从恒远商贸账户直接支付的。\n"
        f"问：这笔钱的性质？\n答：是平台吸收的投资人的钱。",
        f"问：你还向他人出借过平台资金吗？\n"
        f"答：2024年10月我借给恒远商贸的供应商刘某 800 万元，约定月息 1.2%，"
        f"截至案发只收回了 200 万元利息。\n"
        f"问：业务员的提成和投资人的返利怎么发放？\n"
        f"答：王强负责，按投资额 2% 到 5% 提成，返利直接打到投资人账户。",
        "核对无误。\n被询问人（签名）：张伟　2025年12月3日",
    ]))

    # 5. 李娜询问笔录
    name = "05-李娜询问笔录.txt"
    add(name, build_pages(name, [
        "询问时间：2025年11月13日 9 时 00 分至 12 时 40 分\n"
        "询问地点：京海市公安局城中分局办案区询问室\n"
        "询问人：李警官、钱警官　记录人：王警官\n被询问人：犯罪嫌疑人李娜",
        f"问：你在{GROUP}的职务和职责？\n"
        f"答：我是财务总监，2022年入职。平台上线后负责资金收付、账户管理和账务处理。",
        f"问：平台募集资金的收款账户？\n"
        f"答：公司在{BANK}的基本户，账号 {ACCOUNT_GROUP}，还有恒远商贸的 {ACCOUNT_SHELL_A}。"
        f"两个账户的网银 U 盾都在我手里，转账由我操作，超过一定金额需要张伟微信确认。",
        f"问：资金划转的规则？\n"
        f"答：投资人打款进基本户，我按张伟的指令把大额资金转到恒远商贸；"
        f"需要付供应商或走账时，再从恒远商贸转到金泰物流。账上一般只留两百万元左右周转。",
        f"问：你如何记录这些往来？\n"
        f"答：有 Excel 台账，记录每笔投资人的姓名、金额、期数、返息日期。"
        f"还有一部分只在微信里沟通，没有别的凭证。",
        f"问：审计显示恒远商贸账户向张伟个人及亲属转账 1260 万元，你知情吗？\n"
        f"答：知情，是张伟让我按他给的账户转的，用途他说是买房和家用。\n"
        f"问：平台是否有资金托管？\n答：没有，资金全程由公司自己控制。",
        f"问：投资人数和金额你能确认吗？\n"
        f"答：台账上登记的投资人 {TOTAL_VICTIMS} 名，累计吸收 {TOTAL_RAISED} 元，"
        f"未兑付 {TOTAL_UNPAID} 元。\n"
        f"问：以上笔录是否属实？\n答：属实。\n被询问人（签名）：李娜　2025年11月13日",
    ]))

    # 6. 王强询问笔录
    name = "06-王强询问笔录.txt"
    add(name, build_pages(name, [
        "询问时间：2025年11月14日 10 时 00 分至 12 时 30 分\n"
        "询问人：李警官　记录人：王警官\n被询问人：犯罪嫌疑人王强",
        f"问：你在公司的职务？\n答：业务部经理，2023年2月入职，负责市场推广和客户维护。",
        f"问：{PLATFORM}如何对外宣传？\n"
        f"答：办线下推介会，在微信群里发收益截图，拍短视频讲“稳健理财”。"
        f"宣传话术是公司给的，强调“国资背景、保本保息”。",
        f"问：宣传内容是否真实？\n"
        f"答：不真实。公司没有国资背景，也没有金融牌照，到期兑付靠新投资人的钱。"
        f"张伟要求我们必须完成月度业绩。",
        f"问：业绩提成怎么算？\n"
        f"答：按团队募集金额 2% 到 5% 提成。我 2024年全年提成约 95 万元。"
        f"投资人返利由我申请，李娜审批后发放。",
        f"问：你发展的投资人有多少？\n"
        f"答：我这条线大概一百一十多人，金额三千万左右。\n"
        f"问：以上笔录是否属实？\n答：属实。\n被询问人（签名）：王强　2025年11月14日",
    ]))

    # 7-9. 投资人陈述
    for idx, (investor, amount, detail) in enumerate([
        ("陈敏", "860,000", "分四笔投入，2025年9月起无法提现，已收到返息 12 万元"),
        ("刘洋", "1,250,000", "通过王强介绍投入，2024年12月后再未收到返息"),
        ("周静", "430,000", "将养老金投入，2025年10月申请赎回被以“系统维护”为由拒绝"),
    ], start=7):
        name = f"{idx:02d}-{investor}询问笔录.txt"
        add(name, build_pages(name, [
            f"询问时间：2025年11月{10 + idx}日\n"
            f"询问人：钱警官　记录人：王警官\n被询问人：投资人{investor}",
            f"问：你何时、通过什么途径知道“{PLATFORM}”？\n"
            f"答：{investor}陈述：是通过{GROUP}的业务员王强介绍知道的，参加了推介会。\n"
            f"问：你投入了多少钱？\n答：累计投入人民币 {amount} 元，{detail}。",
            f"问：对方如何承诺收益？\n"
            f"答：承诺年化收益 10% 到 12%，说“公司有国资背景，保本保息”。"
            f"我签了《{PLATFORM}理财产品服务协议》，钱打到云启科技的账户。\n"
            f"问：现在能否拿回本金？\n答：不能，客服电话打不通，办公室也搬空了。\n"
            f"被询问人（签名）：{investor}",
        ]))

    # 10. 审计报告
    name = "10-资金归集与流向专项审计报告.txt"
    add(name, build_pages(name, [
        f"京海永信会计师事务所\n关于{GROUP}资金归集与流向的专项审计报告\n京永信审字〔2026〕第 015 号",
        f"一、审计范围与方法\n本次审计以 2023年3月至 2025年11月{PLATFORM}平台募集资金为对象，"
        f"依据{GROUP}在{BANK}开立的基本账户（{ACCOUNT_GROUP}）、"
        f"{SHELL_A}账户（{ACCOUNT_SHELL_A}）、{SHELL_B}账户（{ACCOUNT_SHELL_B}）的银行流水，"
        f"结合《理财产品服务协议》与后台运营数据，采用抽样与全面核对相结合的方法。",
        f"二、募集资金规模\n经核对，平台共与 {TOTAL_VICTIMS} 名投资人签订协议，"
        f"累计吸收资金人民币 {TOTAL_RAISED} 元；截至 2025年11月6日，"
        f"尚未兑付本金人民币 {TOTAL_UNPAID} 元，涉及投资人 214 名。",
        f"三、资金归集路径\n投资人款项先进入基本账户，再于 T+1 至 T+3日内以"
        f"“货款”“保证金”等摘要划转至{SHELL_A}账户，累计转出 {TOTAL_RAISED} 元中的 1.19 亿元，"
        f"占募集总额的 92.5%。基本账户仅保留日常周转资金约 200 万元。",
        f"四、资金去向\n（1）支付前期投资人本息 6,830 万元，占总流出 53.1%；"
        f"（2）经{SHELL_B}过桥后用于公司运营及推广费用 1,940 万元；"
        f"（3）张伟个人及亲属支取 1,260 万元，用途为购置房产；"
        f"（4）对外出借 800 万元。上述流出的收款方与{SHELL_B}无真实货物交易，"
        f"相关合同缺少履约凭证，属于资金空转。",
        f"五、账户特征\n{SHELL_A}账户资金呈现“快进快出、余额低、摘要模糊”特征，"
        f"单笔金额多为 100 万元以上整数；{SHELL_B}账户存在同日多笔对冲转账，"
        f"具备过桥账户特征。",
        "六、审计结论\n1. 平台未取得金融业务许可，募集资金未设立第三方托管；"
        "2. 资金归集与划转由公司单一控制人指令操作，缺少内部制衡；"
        "3. 募集资金与自有资金混同，存在资金池滚动运作；"
        "4. 关联公司间划转缺少真实交易背景，形成资金空转与个人侵占。",
        "附件：资金流水核对表、账户余额明细表。\n\n京海永信会计师事务所（盖章）\n2026年1月8日",
    ]))

    # 11. 服务协议
    name = "11-云启财富理财产品服务协议-模板.txt"
    add(name, build_pages(name, [
        f"{PLATFORM}理财产品服务协议\n甲方（投资人）：__________\n"
        f"乙方（服务方）：{GROUP}\n协议编号：YQWY-____-____\n"
        f"版本生效日期：2023年3月14日",
        "第一条 服务内容\n乙方向甲方提供“云启稳盈”系列理财产品的信息展示与资金收付服务，"
        "甲方自愿将自有资金委托乙方指定的收款账户，按约定周期获取收益、到期收回本金。",
        "第二条 收益与期限\n产品期限分为 3 个月、6 个月、12 个月三档，"
        "预期年化收益率为 8% 至 14%，按月支付收益，到期一次性返还本金。"
        "预期收益不等于实际收益，乙方以平台公布为准。",
        "第三条 收款账户\n甲方资金汇入乙方在{BANK}开立的账户："
        f"户名 {GROUP}，账号 {ACCOUNT_GROUP}。\n"
        "第四条 风险提示与争议解决\n本协议经双方签署后生效，争议提交乙方所在地人民法院管辖。\n"
        "（注：本模板未见第三方资金托管条款。）",
    ]))

    # 12. 司法会计鉴定意见书
    name = "12-司法会计鉴定意见书.txt"
    add(name, build_pages(name, [
        "京海市司法会计鉴定中心\n司法会计鉴定意见书\n京司会鉴字〔2026〕第 022 号\n"
        "鉴定日期：2026年1月12日",
        f"一、委托事项\n受京海市公安局城中分局委托，对{GROUP}平台募集资金数额、"
        f"资金去向及关联账户往来进行鉴定。",
        f"二、鉴定材料\n（1）{GROUP}基本账户流水（{ACCOUNT_GROUP}）；"
        f"（2）{SHELL_A}账户流水（{ACCOUNT_SHELL_A}）；"
        f"（3）{SHELL_B}账户流水（{ACCOUNT_SHELL_B}）；"
        f"（4）《{PLATFORM}理财产品服务协议》及投资人台账；"
        f"（5）财务总监李娜提供的资金收付 Excel 台账。",
        f"三、鉴定过程\n鉴定人依据银行流水逐笔核对资金流向，"
        f"以账号 {ACCOUNT_GROUP} 为起点追踪资金链路，"
        f"对{SHELL_A}、{SHELL_B}两账户进行穿透，"
        f"并按投资人维度汇总本金与已收回收益。",
        f"四、鉴定意见\n1. 2023年3月至 2025年11月，平台共吸收资金人民币 {TOTAL_RAISED} 元，"
        f"未兑付人民币 {TOTAL_UNPAID} 元；2. 资金接收与划转由张伟决策、李娜执行，"
        f"账号 {ACCOUNT_GROUP}、{ACCOUNT_SHELL_A}、{ACCOUNT_SHELL_B} 三账户构成完整资金通道；"
        f"3. 张伟个人及亲属支取 1,260 万元，与平台经营无关。",
    ]))

    # 13. 微信聊天记录
    name = "13-微信聊天记录提取报告.txt"
    add(name, build_pages(name, [
        "电子数据提取报告（微信）\n提取对象：张伟、李娜手机微信客户端\n"
        "提取方式：符合《公安机关办理刑事案件电子数据取证规则》的镜像提取\n"
        "提取时间：2025年11月12日",
        "聊天记录（张伟 ↔ 李娜，2025年9月18日）\n"
        "张伟：这个月到期的人太多，池子接不上了。\n"
        "李娜：基本户只剩两百多万，恒远那边还有八百万，先拿来顶？\n"
        "张伟：先顶上，宣传那边让王强压一压，别再让新人进大额的。",
        "聊天记录（张伟 ↔ 李娜，2025年10月4日）\n"
        "张伟：把恒远账上剩下的转到金泰去，摘要写货款，别写借款。\n"
        "李娜：好的，需要合同吗？\n张伟：不用，走一下就行。",
        "聊天记录（王强 ↔ 投资人陈敏，2025年8月22日）\n"
        "王强：姐，稳盈 6 号最后两天，年化 12%，很多人抢。\n"
        f"陈敏：能保本吗？\n王强：公司有国资背景，保本保息，放心。",
        "聊天记录（张伟 ↔ 王强，2025年7月9日）\n"
        "张伟：这个月募集额不够，提成先压着，下个月补。\n"
        "王强：行，我再冲一冲推介会。",
    ]))

    # 14. 后台运营邮件
    name = "14-后台运营邮件提取报告.txt"
    add(name, build_pages(name, [
        "电子数据提取报告（企业邮箱）\n提取对象：@yunqi-tech.example 域内邮箱\n"
        "提取时间：2025年11月15日\n涉及人员：张伟、李娜、王强、赵敏",
        "邮件一\n发件人：王强\n收件人：全体业务员\n时间：2025年6月3日\n"
        "主题：6月业绩目标与话术\n正文：本月目标 2500 万，推介会每周两场。"
        "话术重点：国资背景、保本保息、名额有限。收益截图统一用运营部提供的版本。",
        "邮件二\n发件人：李娜\n收件人：张伟\n时间：2025年9月20日\n"
        "主题：兑付安排\n正文：本月到期本息合计 1860 万，基本户余额 214 万，"
        "建议从恒远账户调入 1500 万，剩余部分延期兑付。",
        "邮件三\n发件人：赵敏\n收件人：张伟\n时间：2025年10月2日\n"
        "主题：后台数据导出\n正文：按要求已导出投资人明细 327 条，"
        "含姓名、金额、期数、返息记录，文件已加密发送至您的邮箱。",
        "邮件四\n发件人：张伟\n收件人：李娜、王强\n时间：2025年10月8日\n"
        "主题：对外口径\n正文：如遇投资人询问，统一答复“系统升级维护，"
        "11月中旬恢复提现”，不得承诺具体日期。",
    ]))

    # 15. 现场勘验笔录
    name = "15-现场勘验笔录.txt"
    add(name, build_pages(name, [
        "勘验时间：2025年11月6日 15 时 00 分至 18 时 20 分\n"
        "勘验地点：京海市海淀区某科技园 3 号楼 12 层原云启科技办公场所\n"
        "勘验人：李警官、钱警官　见证人：物业管理人员 高某",
        f"现场情况：办公场所面积约 420 平方米，工位空置，无员工在场。"
        f"前台背景墙标有“{GROUP}”字样，接待区摆放“{PLATFORM}”宣传展架 3 个。",
        "提取物品：\n1. 服务器一台（编号 A-01），已断电封存；\n"
        "2. 笔记本电脑两台（编号 B-01、B-02），属李娜办公位；\n"
        "3. 纸质台账一本，封面标注“投资人登记（2024）”；\n"
        "4. 宣传单页、协议范本各若干。上述物品已拍照固定并制作扣押清单。",
        "现场拍照 46 张，绘现场平面图 1 张。\n\n"
        "勘验人（签名）：李警官、钱警官\n见证人（签名）：高某\n2025年11月6日",
    ]))

    # 16. 被害人陈述汇总
    name = "16-被害人陈述汇总表.txt"
    add(name, build_pages(name, [
        f"投资人陈述汇总表（抽样 {len(INVESTORS) + 2} 人，全量 {TOTAL_VICTIMS} 人）\n"
        f"制表人：钱警官　制表日期：2025年12月20日",
        "姓名　金额（元）　期数　　返息情况　　　　　提现情况\n"
        + "\n".join(
            f"{person}　{amount}　　{term}　　{interest}　　{withdraw}"
            for person, amount, term, interest, withdraw in [
                ("陈敏", "860,000", "稳盈4号", "已收12万", "2025年9月起失败"),
                ("刘洋", "1,250,000", "稳盈5号", "已收18万", "2024年12月后未付"),
                ("周静", "430,000", "稳盈3号", "已收6万", "2025年10月被拒"),
                ("孙鹏", "2,100,000", "稳盈6号", "已收24万", "2025年9月起失败"),
                ("郑洁", "560,000", "稳盈4号", "已收8万", "2025年9月起失败"),
                ("何瑞", "1,780,000", "稳盈5号", "部分未付", "2025年8月起失败"),
            ]
        ),
        f"经核对，上述陈述与{PLATFORM}后台投资人台账、银行流水记录一致，"
        f"未发现虚假陈述。全量 {TOTAL_VICTIMS} 名投资人明细见附件电子表格。\n"
        f"制表人（签名）：钱警官",
    ]))

    # 17. 证据材料清单
    name = "17-证据材料清单及卷宗目录.txt"
    add(name, build_pages(name, [
        f"本案卷宗共 17 册，目录如下：\n"
        f"第1册 起诉意见书；第2册 受案登记表及立案决定书；"
        f"第3至4册 张伟询问笔录（第一、二次）；第5册 李娜询问笔录；"
        f"第6册 王强询问笔录；第7至9册 投资人陈敏、刘洋、周静陈述；"
        f"第10册 资金归集与流向专项审计报告；第11册 理财产品服务协议模板；"
        f"第12册 司法会计鉴定意见书；第13册 微信聊天记录提取报告；"
        f"第14册 后台运营邮件提取报告；第15册 现场勘验笔录；"
        f"第16册 被害人陈述汇总表；第17册 证据材料清单及卷宗目录。",
        f"证据分类统计：书证 4 份、询问笔录 7 份、电子数据 2 份、"
        f"鉴定与审计意见 2 份、勘验笔录 1 份、言词证据 1 份。\n"
        f"所有材料均为合成演示数据，不含真实案件信息。",
    ]))

    return volumes


def _evidence(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evidence items keyed to volume index (0-based) and page ranges."""
    def doc(index: int) -> int:
        return int(documents[index]["id"])

    def pages(index: int) -> int:
        return int(documents[index]["pages"])

    specs = [
        (doc(0), 2, min(3, pages(0)), "书证", "高", "已确认",
         "证明平台累计吸收公众存款 128,640,000 元、涉及投资人 327 名。",
         "累计吸收公众存款人民币 128,640,000 元，涉及投资人 327 名，案发时尚未兑付人民币 43,270,000 元。"),
        (doc(0), 1, 1, "书证", "高", "已确认",
         "证明张伟系公司实际控制人，李娜负责资金划转，王强负责公开宣传。",
         "犯罪嫌疑人张伟，男，1979年生，云启科技股份有限公司法定代表人、实际控制人。"),
        (doc(1), 2, 2, "书证", "高", "已确认",
         "证明案件来源为投资人陈敏报案，公安机关于 2025年11月8日立案。",
         "报案人陈敏称其在“云启财富App”投入资金 86 万元，自 2025年9月起无法提现。"),
        (doc(2), 3, 3, "询问笔录", "高", "已确认",
         "证明平台未取得金融牌照，张伟自认未经批准吸收资金。",
         "答：没有。我们只在市场监管部门做了经营范围变更，没有向金融监管部门申请备案。"),
        (doc(2), 4, 4, "询问笔录", "高", "已确认",
         "证明资金经恒远商贸走账，恒远商贸实际控制人为张伟。",
         "答：恒远商贸的实际控制人也是我，是我的关联公司，主要帮我走账。"),
        (doc(3), 2, 2, "询问笔录", "较高", "已确认",
         "证明 1260 万元平台资金被用于张伟购置房产。",
         "答：属实。2024年6月我在京海市朝阳区买了两套住宅，房款是从恒远商贸账户直接支付的。"),
        (doc(4), 4, 4, "询问笔录", "高", "已确认",
         "证明李娜持有两个收款账户网银 U 盾，按张伟指令划转资金。",
         "答：两个账户的网银 U 盾都在我手里，转账由我操作，超过一定金额需要张伟微信确认。"),
        (doc(4), 6, 6, "询问笔录", "高", "已确认",
         "证明平台未设资金托管，投资人 327 名、未兑付 43,270,000 元。",
         "答：台账上登记的投资人 327 名，累计吸收 128,640,000 元，未兑付 43,270,000 元。"),
        (doc(5), 2, 2, "询问笔录", "较高", "待质证",
         "证明业务部以“国资背景、保本保息”等虚假话术公开宣传。",
         "答：不真实。公司没有国资背景，也没有金融牌照，到期兑付靠新投资人的钱。"),
        (doc(5), 5, 5, "言词证据", "中", "待质证",
         "证明王强个人提成约 95 万元，发展投资人一百一十余人。",
         "答：我 2024年全年提成约 95 万元。我这条线大概一百一十多人，金额三千万左右。"),
        (doc(6), 2, 2, "言词证据", "较高", "已确认",
         "证明投资人陈敏累计投入 860,000 元且无法提现。",
         "答：累计投入人民币 860,000 元，分四笔投入，2025年9月起无法提现。"),
        (doc(7), 2, 2, "言词证据", "中", "待补证",
         "证明投资人刘洋经王强介绍投入 1,250,000 元。",
         "答：累计投入人民币 1,250,000 元，通过王强介绍投入，2024年12月后再未收到返息。"),
        (doc(9), 4, 4, "审计报告", "高", "已确认",
         "证明 92.5% 募集资金被划转至恒远商贸账户形成资金池。",
         "累计转出 128,640,000 元中的 1.19 亿元，占募集总额的 92.5%。"),
        (doc(9), 5, 5, "审计报告", "高", "已确认",
         "证明资金空转且 1260 万元被张伟个人支取。",
         "张伟个人及亲属支取 1,260 万元，用途为购置房产。"),
        (doc(9), 6, 6, "审计报告", "较高", "已确认",
         "证明恒远商贸账户具备快进快出特征，金泰物流账户具备过桥特征。",
         "恒远商贸账户资金呈现“快进快出、余额低、摘要模糊”特征。"),
        (doc(10), 3, 3, "书证", "中", "待质证",
         "证明服务协议约定收款账户为集团公司基本户，且无资金托管条款。",
         "甲方资金汇入乙方在京海银行城中支行开立的账户：户名 云启科技股份有限公司。"),
        (doc(11), 4, 4, "其他材料", "高", "已确认",
         "证明三个账户构成完整资金通道，张伟决策、李娜执行。",
         "资金接收与划转由张伟决策、李娜执行，三个账户构成完整资金通道。"),
        (doc(12), 2, 3, "电子数据", "高", "已确认",
         "证明张伟与李娜商议用恒远账户资金顶兑付，要求摘要写“货款”。",
         "张伟：把恒远账上剩下的转到金泰去，摘要写货款，别写借款。"),
        (doc(12), 4, 4, "电子数据", "较高", "已确认",
         "证明王强向投资人虚假承诺保本保息。",
         "王强：公司有国资背景，保本保息，放心。"),
        (doc(13), 3, 3, "电子数据", "较高", "已确认",
         "证明运营邮件要求统一“系统升级维护”对外口径。",
         "如遇投资人询问，统一答复“系统升级维护，11月中旬恢复提现”，不得承诺具体日期。"),
        (doc(14), 2, 2, "其他材料", "中", "已确认",
         "证明办案机关现场扣押服务器、笔记本电脑及投资人台账。",
         "提取物品：服务器一台（编号 A-01），笔记本电脑两台（编号 B-01、B-02），纸质台账一本。"),
        (doc(15), 2, 2, "书证", "中", "待补证",
         "证明抽样投资人陈述与后台台账、银行流水一致。",
         "经核对，上述陈述与云启财富App后台投资人台账、银行流水记录一致。"),
    ]
    items = []
    for index, (document_id, start, end, category, credibility, status, fact, quote) in enumerate(specs, start=1):
        items.append({
            "title": f"证据{index:02d}-{fact[:18]}",
            "category": category,
            "fact": fact,
            "credibility": credibility,
            "status": status,
            "source_document_id": document_id,
            "source_page_start": start,
            "source_page_end": max(start, end),
            "quote": quote,
        })
    return items


def _bank_transactions(rng: random.Random) -> bytes:
    """Deterministic CSV matching the parser's Chinese header aliases."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["账号", "收支方向", "交易金额", "交易日期", "对方户名", "摘要", "币种"])

    investors = INVESTORS + ("投资人（批量）",)
    rows: list[tuple[str, str, str, str, str, str]] = []

    def amount(lo: int, hi: int) -> str:
        return f"{rng.randint(lo, hi) * 1000:,}.00"

    for month in range(3, 13):
        for _ in range(2):
            rows.append((ACCOUNT_GROUP, "收入", amount(300, 2000), f"2023-{month:02d}-{rng.randint(1, 27):02d}",
                         rng.choice(investors), "理财认购款"))
    for month in range(1, 13):
        for _ in range(3):
            rows.append((ACCOUNT_GROUP, "收入", amount(500, 3000), f"2024-{month:02d}-{rng.randint(1, 27):02d}",
                         rng.choice(investors), "稳盈系列认购"))
        rows.append((ACCOUNT_GROUP, "支出", amount(2000, 9000), f"2024-{month:02d}-{rng.randint(1, 27):02d}",
                     SHELL_A, rng.choice(["货款", "保证金", "往来款"])))
    for month in range(1, 12):
        for _ in range(2):
            rows.append((ACCOUNT_GROUP, "收入", amount(400, 2600), f"2025-{month:02d}-{rng.randint(1, 27):02d}",
                         rng.choice(investors), "稳盈6号认购"))
        rows.append((ACCOUNT_GROUP, "支出", amount(1500, 8000), f"2025-{month:02d}-{rng.randint(1, 27):02d}",
                     SHELL_A, "货款"))

    for year, months in ((2024, range(1, 13)), (2025, range(1, 11))):
        for month in months:
            rows.append((ACCOUNT_SHELL_A, "收入", amount(1500, 9000), f"{year}-{month:02d}-{rng.randint(1, 27):02d}",
                         GROUP, "货款"))
            rows.append((ACCOUNT_SHELL_A, "支出", amount(300, 1800), f"{year}-{month:02d}-{rng.randint(1, 27):02d}",
                         SHELL_B, rng.choice(["往来款", "货款", "咨询服务费"])))
    for month in range(1, 11):
        rows.append((ACCOUNT_SHELL_A, "支出", amount(800, 1500), f"2025-{month:02d}-{rng.randint(1, 27):02d}",
                     "张伟", "购房款"))
        rows.append((ACCOUNT_SHELL_A, "支出", amount(30, 200), f"2025-{month:02d}-{rng.randint(1, 27):02d}",
                     rng.choice(INVESTORS), "返息"))

    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def seed(data_dir: Path, *, seed_value: int = 20260911) -> dict[str, Any]:
    data_dir = data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["LAW_REVIEW_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB", str(data_dir / "checkpoints.sqlite"))
    os.environ.setdefault("LAW_REVIEW_AUTH_MODE", "local")

    from app.db import connect, init_db, now
    from app.services import index_upload, persist_bank_transactions

    init_db(seed=False)
    rng = random.Random(seed_value)
    ts = now()

    with connect() as conn:
        case_id = int(conn.execute(
            """
            INSERT INTO cases(title, case_no, case_type, client_name, status, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, '证据复核', ?, ?, ?)
            """,
            (
                CASE_TITLE,
                CASE_NO,
                CASE_TYPE,
                CLIENT_NAME,
                f"合成演示案件：以{PLATFORM}名义公开宣传并承诺年化 8%~14% 收益，"
                f"{FIRST_DATE}至{LAST_DATE}累计吸收公众存款人民币 {TOTAL_RAISED} 元，"
                f"涉及投资人 {TOTAL_VICTIMS} 名，未兑付 {TOTAL_UNPAID} 元。"
                f"全部数据由 scripts/seed_demo_case.py 生成，不含任何真实案件材料。",
                ts,
                ts,
            ),
        ).lastrowid or 0)
        conn.commit()

    documents: list[dict[str, Any]] = []
    for volume_index, volume in enumerate(_documents(), start=1):
        payload = "\f".join(volume["pages"]).encode("utf-8")
        document = index_upload(
            case_id,
            volume["name"],
            payload,
            "text/plain",
            import_key=f"demo-cloudqi:{seed_value}:{case_id}:{volume_index}",
        )
        documents.append({"id": int(document["id"]), "name": volume["name"], "pages": len(volume["pages"])})

    bank_csv = _bank_transactions(rng)
    bank_document = index_upload(case_id, "18-恒远商贸对公账户流水.csv", bank_csv, "text/csv",
                                 import_key=f"demo-cloudqi:{seed_value}:{case_id}:bank")
    bank_document_id = int(bank_document["id"])
    persisted = persist_bank_transactions(case_id, bank_document_id,
                                          str(bank_document.get("content_hash", "")), bank_csv)

    evidence_items = _evidence(documents)
    with connect() as conn:
        evidence_ids: list[int] = []
        for item in evidence_items:
            cursor = conn.execute(
                """
                INSERT INTO evidence(case_id, title, category, fact, credibility, source_document_id,
                                     source_page_start, source_page_end, quote, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (case_id, item["title"], item["category"], item["fact"], item["credibility"],
                 item["source_document_id"], item["source_page_start"], item["source_page_end"],
                 item["quote"], item["status"], ts),
            )
            evidence_ids.append(int(cursor.lastrowid or 0))

        relation_types = ["相互印证", "相互印证", "补充说明", "来源同一账户"]
        for index in range(1, len(evidence_ids)):
            conn.execute(
                """
                INSERT INTO evidence_relations(case_id, from_evidence_id, to_evidence_id, relation_type, note)
                VALUES (?, ?, ?, ?, ?)
                """,
                (case_id, evidence_ids[index - 1], evidence_ids[index],
                 relation_types[index % len(relation_types)], "合成演示证据链"),
            )

        annotations = [
            ("质证意见", "审计结论与银行流水能够对应，建议作为资金池认定的核心证据。", "已完成"),
            ("待核实", "该笔返息缺少投资人签收凭证，需补充原始转账记录。", "待处理"),
            ("关联线索", "邮件口径与微信聊天记录可相互印证，指向主观明知。", "处理中"),
        ]
        for index, (kind, content, status) in enumerate(annotations):
            conn.execute(
                """
                INSERT INTO evidence_annotations(evidence_id, user_name, annotation_type, content,
                                                 quote_start, quote_end, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (evidence_ids[index * 6 % len(evidence_ids)], f"演示律师{index + 1}", kind, content,
                 0, 40, status, ts),
            )

        audit_actions = [
            ("创建案件", "登记云启科技涉嫌非法吸收公众存款案"),
            ("导入卷宗", "导入 18 册卷宗材料"),
            ("银行流水导入", f"导入流水 {persisted.get('rows', 0)} 行"),
            ("生成证据链", "按资金流向串联 22 条证据"),
            ("检索", "检索“资金池 恒远商贸”"),
            ("导出材料", "导出证据目录 CSV"),
        ]
        for action, detail in audit_actions:
            conn.execute("INSERT INTO audit_log(case_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
                         (case_id, action, detail, ts))

        conversations = [
            ("本案资金是否形成完整链条？", [
                ("user", "请说明平台吸收的资金主要流向了哪里。", "事实检索"),
                ("assistant", "募集资金进入集团公司基本户后，92.5% 在 T+1 至 T+3日内划转至恒远商贸账户，"
                              "再经金泰物流过桥，最终用于支付前期本息、公司运营及张伟个人支取。可参见审计报告第 4-5 页。",
                 "事实检索"),
                ("user", "恒远商贸与本案的关系是什么？", "事实检索"),
                ("assistant", "恒远商贸实际控制人为张伟，账户呈现快进快出特征，属于资金归集与走账通道。"
                              "见张伟第一次询问笔录第 5 页。", "事实检索"),
            ]),
            ("未兑付金额与投资人数如何认定？", [
                ("user", "未兑付金额的数据来源是什么？", "法条检索"),
                ("assistant", "未兑付 43,270,000 元来自财务总监李娜台账与银行流水核对结果，"
                              "审计报告与李娜笔录可相互印证。", "法条检索"),
            ]),
            ("罪名与量刑情节分析", [
                ("user", "本案涉嫌什么罪名？", "法条检索"),
                ("assistant", "涉嫌《中华人民共和国刑法》第一百七十六条非法吸收公众存款罪。"
                              "张伟系组织、策划、指挥者，应认定为主犯。", "法条检索"),
                ("user", "有无从轻情节？", "事实检索"),
                ("assistant", "现有材料未显示退赔、自首等从轻情节，建议核实张伟到案经过及退赃情况。", "事实检索"),
            ]),
            ("宣传行为的证据支撑", [
                ("user", "证明公开宣传的证据有哪些？", "事实检索"),
                ("assistant", "王强询问笔录、投资人陈述、运营邮件“保本保息”话术、微信聊天记录，"
                              "证据之间可相互印证。", "事实检索"),
            ]),
            ("证据缺口排查", [
                ("user", "目前证据链有什么缺口？", "事实检索"),
                ("assistant", "缺少资金去向的原始凭证、部分返息签收凭证，以及张伟到案经过材料。", "事实检索"),
            ]),
            ("审计报告要点", [
                ("user", "审计报告的核心结论是什么？", "事实检索"),
                ("assistant", "募集资金未设第三方托管、资金与自有资金混同、关联划转缺少真实交易背景，"
                              "形成资金池滚动运作。", "事实检索"),
            ]),
        ]
        for title, messages in conversations:
            cursor = conn.execute(
                "INSERT INTO conversations(case_id, user_name, title, created_at) VALUES (?, ?, ?, ?)",
                (case_id, "演示律师", title, ts),
            )
            conversation_id = int(cursor.lastrowid or 0)
            for role, content, route in messages:
                citations = json.dumps(
                    [{"document_id": documents[9]["id"], "page_no": 4, "title": documents[9]["name"]}]
                    if role == "assistant" else [],
                    ensure_ascii=False,
                )
                conn.execute(
                    """
                    INSERT INTO messages(conversation_id, role, content, citations_json, route, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (conversation_id, role, content, citations, route, ts),
                )

        run_specs = [
            ("请根据卷宗材料说明本案资金归集路径，并标注来源。",
             "募集资金经集团公司基本户归集后划转恒远商贸，再经金泰物流过桥，形成资金池。"),
            ("未兑付金额 43,270,000 元的证据是否充分？",
             "李娜台账、审计报告与银行流水三项证据可以相互印证，认定较为充分。"),
            ("张伟、李娜、王强三人各自的地位作用如何？",
             "张伟为组织指挥者，李娜负责资金划转，王强负责公开宣传，均起主要作用。"),
            ("平台是否存在自融或资金空转？",
             "恒远商贸与金泰物流账户之间缺少真实交易背景，存在资金空转。"),
            ("公开宣传的证据链是否完整？",
             "现有笔录、邮件与聊天记录可以证明公开宣传，但缺少线下推介会的影像资料。"),
            ("1260 万元个人支取的证据情况？",
             "审计报告与张伟第二次询问笔录相互印证，另有微信指令作为佐证。"),
            ("投资人数的统计口径是否一致？",
             "后台台账、汇总表与审计报告均为 327 名，口径一致。"),
            ("本案是否存在退赔等从轻情节？",
             "现有材料未发现退赔记录，建议补充张伟到案经过及财产查封情况。"),
        ]
        for question, answer in run_specs:
            cursor = conn.execute(
                """
                INSERT INTO agent_runs(case_id, question, route, status, retrieval_mode, final_answer,
                                       citations_json, total_ms, runtime, created_at, finished_at)
                VALUES (?, ?, ?, 'completed', 'hybrid_rrf', ?, ?, ?, 'native', ?, ?)
                """,
                (case_id, question, "事实检索", answer,
                 json.dumps([{"document_id": documents[9]["id"], "page_no": 4}], ensure_ascii=False),
                 rng.randint(1200, 8600), ts, ts),
            )
            run_id = int(cursor.lastrowid or 0)
            for node in ("retrieve", "analyze", "critique", "compose"):
                conn.execute(
                    """
                    INSERT INTO agent_steps(run_id, node_name, agent_role, status, input_json, output_json,
                                            latency_ms, started_at, finished_at)
                    VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?)
                    """,
                    (run_id, node, node,
                     json.dumps({"query": question}, ensure_ascii=False),
                     json.dumps({"ok": True}, ensure_ascii=False),
                     rng.randint(80, 2600), ts, ts),
                )

        gaps = [
            ("证据链缺口", "高", "资金去向缺少原始凭证，仅能依据流水与台账推定。",
             "建议调取恒远商贸、金泰物流与供应商之间的合同及发票。", [evidence_ids[12], evidence_ids[13]]),
            ("证据链缺口", "中", "部分返息发放缺少投资人签收凭证。",
             "建议补充调取返息发放当日的银行代发记录。", [evidence_ids[18]]),
            ("程序瑕疵", "中", "现场扣押清单未见持有人签名。",
             "建议补正扣押手续或补充见证人说明。", [evidence_ids[20]]),
            ("待补证", "高", "缺少张伟到案经过及是否自首的材料。",
             "建议补充到案经过说明与第一次讯问同步录音录像。", [evidence_ids[3]]),
            ("证据矛盾", "低", "王强陈述的提成比例与审计报告估算存在小幅差异。",
             "建议以业务部提成台账为准复核差异原因。", [evidence_ids[8], evidence_ids[9]]),
        ]
        for gap_type, severity, description, suggestion, affected in gaps:
            conn.execute(
                """
                INSERT INTO gap_detections(case_id, run_id, gap_type, severity, description, suggestion,
                                           affected_evidence_ids, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, '待核验', ?)
                """,
                (case_id, None, gap_type, severity, description, suggestion,
                 json.dumps(affected, ensure_ascii=False), ts),
            )
        conn.commit()

    with connect() as conn:
        summary = {
            "data_dir": str(data_dir),
            "case": {"id": case_id, "title": CASE_TITLE, "case_no": CASE_NO},
            "documents": len(documents) + 1,
            "pages": conn.execute("SELECT COALESCE(SUM(pages), 0) FROM documents WHERE case_id = ?", (case_id,)).fetchone()[0],
            "evidence": conn.execute("SELECT COUNT(*) FROM evidence WHERE case_id = ?", (case_id,)).fetchone()[0],
            "bank_transactions": conn.execute("SELECT COUNT(*) FROM bank_transactions WHERE case_id = ?", (case_id,)).fetchone()[0],
            "conversations": conn.execute("SELECT COUNT(*) FROM conversations WHERE case_id = ?", (case_id,)).fetchone()[0],
            "agent_runs": conn.execute("SELECT COUNT(*) FROM agent_runs WHERE case_id = ?", (case_id,)).fetchone()[0],
            "gap_detections": conn.execute("SELECT COUNT(*) FROM gap_detections WHERE case_id = ?", (case_id,)).fetchone()[0],
            "fts_rows": conn.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0],
        }

    # Emit this case's own gold pages so the operator can POST them as the
    # evaluate-rag body. Validated here against the case's real pages.
    truth_path = data_dir / f"ground-truth-case-{case_id}.json"
    truth_path.write_text(json.dumps(GROUND_TRUTH, ensure_ascii=False, indent=2), encoding="utf-8")
    with connect() as conn:
        available = {
            (row["name"], row["page_no"])
            for row in conn.execute(
                "SELECT d.name, p.page_no FROM pages p JOIN documents d ON d.id=p.document_id WHERE d.case_id=?",
                (case_id,),
            )
        }
    stale = [pair for item in GROUND_TRUTH for pair in item["expected"] if pair not in available]
    if stale:
        raise ValueError(f"ground_truth refers to pages not in case {case_id}: {stale}")
    summary["ground_truth"] = {"path": str(truth_path), "queries": len(GROUND_TRUTH)}
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed one complex synthetic demo case for website feature testing.")
    parser.add_argument("--data-dir", required=True, help="Target LAW_REVIEW_DATA_DIR (never real case material)")
    parser.add_argument("--seed", type=int, default=20260911)
    parser.add_argument("--json-out", help="Write the summary JSON to this path")
    arguments = parser.parse_args()
    summary = seed(Path(arguments.data_dir), seed_value=arguments.seed)
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if arguments.json_out:
        Path(arguments.json_out).write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
