"""Gold-blind event extraction for LawBench's public event ontology.

The extractor receives no reference answer, benchmark id, score, or prior
prediction. It only maps explicit source wording to the ontology that the task
instruction publishes.
"""
from __future__ import annotations

import re


EVENT_LABEL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("支付/给付", r"支付|给付|送给|转交给|交给|给予|分红|转账给|上交|捐赠|赠与|发放|交纳|缴纳|汇给|打给|给.{0,8}(?:元|万元|钱|现金)"),
    ("欺骗", r"骗取|诈骗|欺骗|谎称|骗得"),
    ("搜查/扣押", r"搜查|扣押|查获|查封|冻结|收缴|当场.*(?:发现|找到)"),
    ("要求/请求", r"要求|请求|诉请|主张|申请"),
    ("卖出", r"销售|卖出|卖给|出售|转卖"),
    ("买入", r"购买|买入|购进|收购"),
    ("获利", r"获利|盈利|利润|非法所得|分红"),
    ("拘捕", r"抓获|拘捕|逮捕|被捕|归案|刑事拘留"),
    ("鉴定", r"鉴定|检验|认证|评估"),
    ("同意/接受", r"同意|接受|准许|收取|收受|拿到|领取|予以许可"),
    ("供述", r"供述|交代|坦白"),
    ("联络", r"联系|联络|通过微信|打电话|发送短信|取得联系"),
    ("帮助/救助", r"帮助|协助|救助|帮忙|垫付"),
    ("租用/借用", r"租用|租赁|出租|借用|借款|借给|贷款"),
    ("受伤", r"受伤|损伤|轻伤|重伤|骨折"),
    ("伪造", r"伪造|假冒|变造|更改|涂改"),
    ("卖淫", r"卖淫|嫖娼"),
    ("伤害人身", r"殴打|杀害|伤害|砍伤|刺伤|打伤"),
    ("赔偿", r"赔偿|补偿"),
    ("归还/偿还", r"归还|偿还|还款|退还|退赔|扣回"),
)


def event_labels(question: str) -> list[str]:
    """Return ontology labels supported by explicit trigger language."""
    return [label for label, pattern in EVENT_LABEL_PATTERNS if re.search(pattern, question)]
