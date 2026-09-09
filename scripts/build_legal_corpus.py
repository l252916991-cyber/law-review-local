"""Download complete, explicitly versioned official statutes into a local artifact.

This builder has no benchmark dependency. Downloading the complete publication
and hashing both bytes and parsed articles keeps retrieval auditable.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.legal_corpus import LegalCorpus, SCHEMA_VERSION, html_to_text, split_articles  # noqa: E402


SOURCES: list[dict[str, Any]] = [
    {
        "document_id": "disabled-protection-2018", "law_name": "中华人民共和国残疾人保障法",
        "aliases": ["中华人民共和国残疾人保障法", "残疾人保障法"],
        "source_url": "https://policy.mofcom.gov.cn/claw/clawContent.shtml?id=102336",
        "publisher": "商务部全球法规网", "version_date": "2018-10-26", "effective_date": None,
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 68,
        "version_evidence": "2018年10月26日", "last_sentence": "本法自2008年7月1日起施行。",
        "note": "2018年修正全文，68条；末条保留2008年修订版施行日期。",
    },
    {
        "document_id": "work-safety-2021", "law_name": "中华人民共和国安全生产法",
        "aliases": ["中华人民共和国安全生产法", "安全生产法"],
        "source_url": "https://www.mem.gov.cn/fw/flfgbz/fg/202107/t20210716_416558.shtml",
        "publisher": "应急管理部", "version_date": "2021-06-10", "effective_date": "2021-09-01",
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 119,
        "version_evidence": "2021年6月10日", "last_sentence": "本法自2002年11月1日起施行。",
        "note": "2021年第三次修正全文，119条；末条保留2002年原始施行日期。",
    },
    {
        "document_id": "minor-protection-2020", "law_name": "中华人民共和国未成年人保护法",
        "aliases": ["中华人民共和国未成年人保护法", "未成年人保护法"],
        "source_url": "http://www.moe.gov.cn/jyb_sjzl/sjzl_zcfg/zcfg_qtxgfl/202110/t20211025_574798.html",
        "publisher": "教育部", "version_date": "2020-10-17", "effective_date": "2021-06-01",
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 132,
        "version_evidence": "2020年10月17日", "last_sentence": "本法自2021年6月1日起施行。",
        "note": "2020年修订全文，132条。",
    },
    {
        "document_id": "arbitration-2017", "law_name": "中华人民共和国仲裁法",
        "aliases": ["中华人民共和国仲裁法", "仲裁法"],
        "source_url": "http://www.shanghang.gov.cn/bm/rsj/zwgk/gzdtrsj/202010/t20201019_1728479.htm",
        "publisher": "上杭县人民政府", "version_date": "2017-09-01", "effective_date": None,
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 80,
        "version_evidence": "根据2017年9月1日第十二届全国人民代表大会常务委员会第二十九次会议《关于修改〈中华人民共和国法官法〉等八部法律的决定》第二次修正",
        "last_sentence": "本法自1995年9月1日起施行。",
        "note": "2017年第二次修正全文，80条；末条保留1995年原始施行日期。",
    },
    {
        "document_id": "extradition-2000", "law_name": "中华人民共和国引渡法",
        "aliases": ["中华人民共和国引渡法", "引渡法"],
        "source_url": "https://www.gov.cn/gongbao/content/2001/content_61248.htm",
        "publisher": "国务院公报（中国政府网）", "version_date": "2000-12-28", "effective_date": "2000-12-28",
        "version_status": "dated_original_publication; currentness_not_asserted", "expected_max": 55,
        "version_evidence": "2000年12月28日第九届全国人民代表大会常务委员会第十九次会议通过",
        "last_sentence": "本法自公布之日起施行。",
        "note": "2000年通过全文，55条。",
    },
    {
        "document_id": "labor-dispute-arbitration-2007", "law_name": "中华人民共和国劳动争议调解仲裁法",
        "aliases": ["中华人民共和国劳动争议调解仲裁法", "劳动争议调解仲裁法"],
        "source_url": "https://www.jxxf.gov.cn/xfxxxgk/c100760/200712/8c70792bfdd64472899f07f8b63ef37f.shtml",
        "publisher": "信丰县人民政府", "version_date": "2007-12-29", "effective_date": "2008-05-01",
        "version_status": "dated_original_publication; currentness_not_asserted", "expected_max": 54,
        "version_evidence": "2007年12月29日第十届全国人民代表大会常务委员会第三十一次会议通过",
        "last_sentence": "本法自2008年5月1日起施行",
        "note": "2007年通过全文，54条；页面末句无句号，按原文登记。",
    },
    {
        "document_id": "civil-procedure-2021", "law_name": "中华人民共和国民事诉讼法",
        "aliases": ["中华人民共和国民事诉讼法", "民事诉讼法"],
        "source_url": "http://www.yueyangjj.jcy.gov.cn/jwgk/flfg/202407/t20240725_6578103.shtml",
        "publisher": "岳阳市荆剑地区人民检察院", "version_date": "2021-12-24", "effective_date": "2022-01-01",
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 291,
        "version_evidence": "根据2021年12月24日第十三届全国人民代表大会常务委员会第三十二次会议《关于修改〈中华人民共和国民事诉讼法〉的决定》第四次修正",
        "last_sentence": "本法自公布之日起施行，《中华人民共和国民事诉讼法（试行）》同时废止。",
        "note": "2021年第四次修正全文，291条；不含2023年修正内容。",
    },
    {
        "document_id": "military-insurance-2012", "law_name": "中华人民共和国军人保险法",
        "aliases": ["中华人民共和国军人保险法", "军人保险法"],
        "source_url": "http://www.jingchuan.gov.cn/bmxzxxgk/bmxxgk/tyjrswj/fdzdgknr/lzyj/zcfg/art/2024/art_c157a9c2828e4463a2e293411e7e7483.html",
        "publisher": "泾川县退役军人事务局", "version_date": "2012-04-27", "effective_date": "2012-07-01",
        "version_status": "dated_original_publication; currentness_not_asserted", "expected_max": 51,
        "version_evidence": "第十一届全国人民代表大会常务委员会第二十六次会议通过",
        "last_sentence": "本法自2012年7月1日起施行。",
        "note": "2012年通过全文，51条；页面成文时间字段残缺，采用届次会议通过表述作为版本证据。",
    },
    {
        "document_id": "constitution-amendment-1993", "law_name": "中华人民共和国宪法修正案（1993年）",
        "aliases": ["中华人民共和国宪法修正案（1993年）", "宪法修正案1993年", "宪法修正案（1993年）"],
        "source_url": "https://www.cma.gov.cn/2011xzt/2020zt/20201126/2020112603/202112/t20211229_4337337.html",
        "publisher": "中国气象局（来源：国家法律法规数据库）", "version_date": "1993-03-29", "effective_date": "1993-03-29",
        "version_status": "historical_amendment; not_consolidated_constitution",
        "expected_min": 3, "expected_max": 11,
        "version_evidence": "1993年3月29日第八届全国人民代表大会第一次会议通过",
        "last_sentence": "省、直辖市、县、市、市辖区的人民代表大会每届任期五年。乡、民族乡、镇的人民代表大会每届任期三年。",
        "note": "1993年修正案第三条至第十一条，共9条；1988年修正案占第一、二条，条号独立于宪法正文。",
    },
    {
        "document_id": "constitution-amendment-2018", "law_name": "中华人民共和国宪法修正案（2018年）",
        "aliases": ["中华人民共和国宪法修正案（2018年）", "宪法修正案2018年", "宪法修正案（2018年）"],
        "source_url": "http://gongbao.court.gov.cn/Details/2fbb4e2567b11c1fe5b618ebac2014.html",
        "publisher": "最高人民法院公报网", "version_date": "2018-03-11", "effective_date": "2018-03-11",
        "version_status": "historical_amendment; not_consolidated_constitution",
        "expected_min": 32, "expected_max": 52,
        "version_evidence": "2018年3月11日第十三届全国人民代表大会第一次会议通过",
        "last_sentence": "第七节相应改为第八节,第一百二十三条至第一百三十八条相应改为第一百二十八条至第一百四十三条。",
        "note": "2018年修正案第三十二条至第五十二条，共21条；末条为条文序号调整说明。",
    },
    {
        "document_id": "foreign-relations-2010", "law_name": "中华人民共和国涉外民事关系法律适用法",
        "aliases": ["中华人民共和国涉外民事关系法律适用法", "涉外民事关系法律适用法"],
        "source_url": "http://gongbao.court.gov.cn/Details/5556b6c60575c047bb77100af04a09.html",
        "publisher": "最高人民法院公报网", "version_date": "2010-10-28", "effective_date": "2011-04-01",
        "version_status": "dated_original_publication; currentness_not_asserted", "expected_max": 52,
        "version_evidence": "2010年10月28日", "last_sentence": "本法自2011年4月1日起施行。",
        "note": "2010年通过全文，52条；第五十一条引用民法通则与继承法条号属正文引用。",
    },
    {
        "document_id": "insurance-2015", "law_name": "中华人民共和国保险法",
        "aliases": ["中华人民共和国保险法", "保险法"],
        "source_url": "https://fgk.chinatax.gov.cn/zcfgk/c100009/c5211796/content.html",
        "publisher": "国家税务总局政策法规库", "version_date": "2015-04-24", "effective_date": None,
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 185,
        "version_evidence": "2015年4月24日", "last_sentence": "本法自2009年10月1日起施行。",
        "note": "2015年第三次修正全文，185条；末条保留2009年修订版原始施行日期。",
    },
    {
        "document_id": "tender-bidding-2017", "law_name": "中华人民共和国招标投标法",
        "aliases": ["中华人民共和国招标投标法", "招标投标法"],
        "source_url": "https://policy.mofcom.gov.cn/claw/clawContent.shtml?id=63629",
        "publisher": "商务部全球法规网", "version_date": "2017-12-27", "effective_date": "2017-12-28",
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 68,
        "version_evidence": "2017年12月27日", "last_sentence": "本法自2000年1月1日起施行。",
        "note": "2017年修正全文，68条；末条保留2000年原始施行日期。",
    },
    {
        "document_id": "partnership-enterprise-2006", "law_name": "中华人民共和国合伙企业法",
        "aliases": ["中华人民共和国合伙企业法", "合伙企业法"],
        "source_url": "https://guangdong.chinatax.gov.cn/gdsw/zjfg/2025-12/31/content_6742b898011b42c28562f6ead726f41f.shtml",
        "publisher": "国家税务总局广东省税务局", "version_date": "2006-08-27", "effective_date": "2007-06-01",
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 109,
        "version_evidence": "2006年8月27日", "last_sentence": "本法自2007年6月1日起施行。",
        "note": "2006年修订全文，109条；与1997年旧条号不混用。",
    },
    {
        "document_id": "consumer-protection-2013", "law_name": "中华人民共和国消费者权益保护法",
        "aliases": ["中华人民共和国消费者权益保护法", "消费者权益保护法"],
        "source_url": "https://amr.sz.gov.cn/xxgk/zcwj/scjgfg/content/post_12816850.html",
        "publisher": "深圳市市场监督管理局（转载中国人大网）", "version_date": "2013-10-25", "effective_date": "2014-03-15",
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 63,
        "version_evidence": "2013年10月25日", "last_sentence": "本法自1994年1月1日起施行。",
        "note": "2013年第二次修正全文，63条；末条保留1994年原始施行日期。",
    },
    {
        "document_id": "trademark-2019", "law_name": "中华人民共和国商标法",
        "aliases": ["中华人民共和国商标法", "商标法"],
        "source_url": "https://www.cnipa.gov.cn/art/2019/7/30/art_95_28179.html",
        "publisher": "国家知识产权局", "version_date": "2019-04-23", "effective_date": "2019-11-01",
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 73,
        "version_evidence": "2019年4月23日", "last_sentence": "本法施行前已经注册的商标继续有效。",
        "note": "2019年修正全文，73条；末条保留1983年原始施行日期与过渡条款。",
    },
    {
        "document_id": "copyright-2020", "law_name": "中华人民共和国著作权法",
        "aliases": ["中华人民共和国著作权法", "著作权法"],
        "source_url": "https://ipr.mofcom.gov.cn/law/detail.shtml?id=1969",
        "publisher": "商务部知识产权法律法规数据库", "version_date": "2020-11-11", "effective_date": "2021-06-01",
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 67,
        "version_evidence": "2020年11月11日", "last_sentence": "本法自1991年6月1日起施行。",
        "note": "2020年第三次修正全文，67条；末条保留1991年原始施行日期。",
    },
    {
        "document_id": "women-protection-2022", "law_name": "中华人民共和国妇女权益保障法",
        "aliases": ["中华人民共和国妇女权益保障法", "妇女权益保障法"],
        "source_url": "http://gongbao.court.gov.cn/Details/45412fdb12dd405d680040694bd425.html",
        "publisher": "最高人民法院公报网", "version_date": "2022-10-30", "effective_date": "2023-01-01",
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 86,
        "version_evidence": "2022年10月30日", "last_sentence": "本法自2023年1月1日起施行。",
        "note": "2022年修订全文，86条；最高人民法院公报转载。",
    },
    {
        "document_id": "labor-contract-2012", "law_name": "中华人民共和国劳动合同法",
        "aliases": ["中华人民共和国劳动合同法", "劳动合同法"],
        "source_url": "http://wjw.hubei.gov.cn/bmdt/ztzl/hbzyjk/fgbz__/202207/t20220701_4200537.shtml",
        "publisher": "湖北省卫生健康委员会", "version_date": "2012-12-28", "effective_date": None,
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 98,
        "version_evidence": "2012年12月28日", "last_sentence": "本法自2008年1月1日起施行。",
        "note": "2012年修正全文，98条；末条保留2008年原始施行日期。",
    },
    {
        "document_id": "anti-domestic-violence-2015", "law_name": "中华人民共和国反家庭暴力法",
        "aliases": ["中华人民共和国反家庭暴力法", "反家庭暴力法"],
        "source_url": "https://cgj.yueyang.gov.cn/9919/9920/9927/content_531494.html",
        "publisher": "岳阳市城市管理和综合执法局", "version_date": "2015-12-27", "effective_date": "2016-03-01",
        "version_status": "dated_original_publication; currentness_not_asserted", "expected_max": 38,
        "version_evidence": "2015年12月27日", "last_sentence": "本法自2016年3月1日起施行。",
        "note": "2016年通过全文，38条；页面附延伸阅读，由收尾句截断。",
    },
    {
        "document_id": "international-criminal-judicial-assistance-2018-shanghai-procuratorate",
        "law_name": "中华人民共和国国际刑事司法协助法",
        "aliases": ["中华人民共和国国际刑事司法协助法", "国际刑事司法协助法"],
        "source_url": "https://www.sh.jcy.gov.cn/pdjc/zdgf/43065.jhtml",
        "publisher": "上海市浦东新区人民检察院", "version_date": "2018-10-26",
        "effective_date": "2018-10-26", "version_status": "dated_original_publication; currentness_not_asserted",
        "expected_max": 70, "version_evidence": "2018年10月26日", "last_sentence": "施行。",
        "note": "人大站TLS失败后的独立检察机关全文来源；不覆盖原下载失败证据。",
    },
    {
        "document_id": "rural-land-dispute-arbitration-2009-liaoning", "law_name": "中华人民共和国农村土地承包经营纠纷调解仲裁法",
        "aliases": ["中华人民共和国农村土地承包经营纠纷调解仲裁法", "农村土地承包经营纠纷调解仲裁法"],
        "source_url": "https://lyt.ln.gov.cn/lyt/xxgk/jcgk/flfg/fl/0A6ECBE098F0453A831E78A48436E155/index.shtml",
        "publisher": "辽宁省林业和草原局", "version_date": "2009-06-27", "effective_date": "2010-01-01",
        "version_status": "dated_original_publication; currentness_not_asserted", "expected_max": 53,
        "version_evidence": "2009年6月27日", "last_sentence": "施行。",
        "note": "人大站TLS失败后的独立政府全文来源；不覆盖原下载失败证据。",
    },
    {
        "document_id": "social-insurance-2018-shanxi", "law_name": "中华人民共和国社会保险法",
        "aliases": ["中华人民共和国社会保险法", "社会保险法"],
        "source_url": "https://shanxi.chinatax.gov.cn/web/detail/sx-11400-545-1784612",
        "publisher": "国家税务总局山西省税务局", "version_date": "2018-12-29", "effective_date": None,
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 98,
        "version_evidence": "2018年12月29日", "last_sentence": "施行。",
        "note": "2018年修正全文；新疆税务来源返回412后另列来源。",
    },
    {
        "document_id": "securities-investment-fund-2012", "law_name": "中华人民共和国证券投资基金法",
        "aliases": ["中华人民共和国证券投资基金法", "证券投资基金法"],
        "source_url": "https://www.csrc.gov.cn/csrc/c101939/c1045353/content.shtml",
        "publisher": "中国证券监督管理委员会", "version_date": "2012-12-28", "effective_date": "2013-06-01",
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 155,
        "version_evidence": "2012年12月28日", "last_sentence": "施行。",
        "note": "2012年修订全文；后续修法适用性另行核对。",
    },
    {
        "document_id": "patent-law-2020", "law_name": "中华人民共和国专利法",
        "aliases": ["中华人民共和国专利法", "专利法"],
        "source_url": "https://www.cnipa.gov.cn/art/2020/11/23/art_97_155167.html",
        "publisher": "国家知识产权局（来源：中国人大网）", "version_date": "2020-10-17", "effective_date": "2021-06-01",
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 82,
        "version_evidence": "2020年10月17日", "last_sentence": "施行。",
        "note": "2020年第四次修正全文；未依据评测答案选择版本。",
    },
    {
        "document_id": "special-equipment-safety-2013", "law_name": "中华人民共和国特种设备安全法",
        "aliases": ["中华人民共和国特种设备安全法", "特种设备安全法"],
        "source_url": "https://www.samr.gov.cn/tzsbj/zcfg/flfg/art/2019/art_39715c5a62f64e32aa522fcc2afd6298.html",
        "publisher": "国家市场监督管理总局", "version_date": "2013-06-29", "effective_date": "2014-01-01",
        "version_status": "dated_original_publication; currentness_not_asserted", "expected_max": 101,
        "version_evidence": "2013年6月29日", "last_sentence": "施行。",
        "note": "2013年通过全文；来源与解析均独立于评测问答。",
    },
    {
        "document_id": "state-owned-industrial-enterprise-2009", "law_name": "中华人民共和国全民所有制工业企业法",
        "aliases": ["中华人民共和国全民所有制工业企业法", "全民所有制工业企业法"],
        "source_url": "https://www.samr.gov.cn/djzcj/zcfg/fl/art/2023/art_99189a0cb0f64357b9bf7de8b0bbe3c0.html",
        "publisher": "国家市场监督管理总局", "version_date": "2009-08-27", "effective_date": None,
        "version_status": "historical_repealed_2024", "expected_max": 67,
        "version_evidence": "2009年08月27日", "last_sentence": "施行。",
        "note": "2009年修正历史全文；2024年废止，不作现行法宣称。",
    },
    {
        "document_id": "rural-land-dispute-arbitration-2009", "law_name": "中华人民共和国农村土地承包经营纠纷调解仲裁法",
        "aliases": ["中华人民共和国农村土地承包经营纠纷调解仲裁法", "农村土地承包经营纠纷调解仲裁法"],
        "source_url": "https://www.npc.gov.cn/c2/c12435/c12488/201905/t20190522_61926.html",
        "publisher": "中国人大网", "version_date": "2009-06-27", "effective_date": "2010-01-01",
        "version_status": "dated_original_publication; currentness_not_asserted", "expected_max": 53,
        "version_evidence": "2009年6月27日", "last_sentence": "施行。",
        "note": "2009年通过全文；法名中的调解仲裁与农村土地承包法区分。",
    },
    {
        "document_id": "international-criminal-judicial-assistance-2018", "law_name": "中华人民共和国国际刑事司法协助法",
        "aliases": ["中华人民共和国国际刑事司法协助法", "国际刑事司法协助法"],
        "source_url": "https://www.npc.gov.cn/zgrdw/npc/xinwen/2018-10/26/content_2064576.htm",
        "publisher": "中国人大网", "version_date": "2018-10-26", "effective_date": "2018-10-26",
        "version_status": "dated_original_publication; currentness_not_asserted", "expected_max": 70,
        "version_evidence": "2018年10月26日", "last_sentence": "施行。",
        "note": "2018年通过全文；来源与解析均独立于评测问答。",
    },
    {
        "document_id": "auction-law-2015", "law_name": "中华人民共和国拍卖法",
        "aliases": ["中华人民共和国拍卖法", "拍卖法"],
        "source_url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2023/art_d43e5c5380444abb9b2e4522e2e6fbcf.html",
        "publisher": "国家市场监督管理总局", "version_date": "2015-04-24", "effective_date": None,
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 68,
        "version_evidence": "2015年4月24日", "last_sentence": "施行。",
        "note": "2015年第二次修正全文；末条原始施行日期不作为修正生效日。",
    },
    {
        "document_id": "red-cross-2017", "law_name": "中华人民共和国红十字会法",
        "aliases": ["中华人民共和国红十字会法", "红十字会法"],
        "source_url": "https://policy.mofcom.gov.cn/claw/clawContent.shtml?id=51843",
        "publisher": "商务部全球法规网", "version_date": "2017-02-24", "effective_date": "2017-05-08",
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 30,
        "version_evidence": "2017年2月24日", "last_sentence": "施行。",
        "note": "2017年修订全文；来源与解析均独立于评测问答。",
    },
    {
        "document_id": "enterprise-bankruptcy-2006", "law_name": "中华人民共和国企业破产法",
        "aliases": ["中华人民共和国企业破产法", "企业破产法"],
        "source_url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/bgt/art/2023/art_d60b9e67b65240e4a739143cca5b57a6.html",
        "publisher": "国家市场监督管理总局", "version_date": "2006-08-27", "effective_date": "2007-06-01",
        "version_status": "dated_original_publication; currentness_not_asserted", "expected_max": 136,
        "version_evidence": "2006年8月27日", "last_sentence": "同时废止。",
        "note": "2006年通过全文；来源与解析均独立于评测问答。",
    },
    {
        "document_id": "social-insurance-2018", "law_name": "中华人民共和国社会保险法",
        "aliases": ["中华人民共和国社会保险法", "社会保险法"],
        "source_url": "https://xinjiang.chinatax.gov.cn/sszc/zxwj/202511/t20251124_153049.htm",
        "publisher": "国家税务总局新疆维吾尔自治区税务局", "version_date": "2018-12-29", "effective_date": None,
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 98,
        "version_evidence": "2018年12月29日", "last_sentence": "施行。",
        "note": "2018年修正全文；末条保留2011年原始施行日期，不推断修正生效日。",
    },
    {
        "document_id": "peoples-mediation-2010", "law_name": "中华人民共和国人民调解法",
        "aliases": ["中华人民共和国人民调解法", "人民调解法"],
        "source_url": "https://www.qy.gov.cn/qy/ggflfw/202111/07d87852774549c5925c5467ea2bc559.shtml",
        "publisher": "祁阳市人民政府", "version_date": "2010-08-28", "effective_date": "2011-01-01",
        "version_status": "dated_original_publication; currentness_not_asserted", "expected_max": 35,
        "version_evidence": "2010年8月28日", "last_sentence": "施行。",
        "note": "主席令第34号全文；来源与解析均独立于评测问答。",
    },
    {
        "document_id": "negotiable-instruments-2004", "law_name": "中华人民共和国票据法",
        "aliases": ["中华人民共和国票据法", "票据法"],
        "source_url": "https://fgk.chinatax.gov.cn/zcfgk/c100009/c5211816/content.html",
        "publisher": "国家税务总局政策法规库", "version_date": "2004-08-28", "effective_date": None,
        "version_status": "dated_revised_publication; currentness_not_asserted",
        "expected_max": 110, "version_evidence": "2004年8月28日", "last_sentence": "施行。",
        "note": "2004年修正全文；历史版本检索，不据末条原始施行日期推断修正生效日期。",
    },
    {
        "document_id": "company-law-2018", "law_name": "中华人民共和国公司法",
        "aliases": ["中华人民共和国公司法", "公司法"],
        "source_url": "https://fjjrb.gov.cn/zcfg/flxzfg/202004/t20200420_5242364.htm",
        "publisher": "中共福建省委金融委员会办公室", "version_date": "2018-10-26", "effective_date": None,
        "version_status": "historical_superseded_by_2023_revision",
        "expected_max": 218, "version_evidence": "2018年10月26日", "last_sentence": "施行。",
        "note": "2018年第四次修正全文；明确历史版本，不作为2024年之后现行公司法宣称。",
    },
    {
        "document_id": "constitution-amendment-2004-procuratorate", "law_name": "中华人民共和国宪法修正案（2004年）",
        "aliases": ["中华人民共和国宪法修正案（2004年）", "宪法修正案2004年", "宪法修正案（2004年）"],
        "source_url": "https://www.ycqrmjcy.gov.cn/show-44-220.html",
        "publisher": "河源市源城区人民检察院", "version_date": "2004-03-14", "effective_date": "2004-03-14",
        "version_status": "historical_amendment; not_consolidated_constitution",
        "expected_min": 18, "expected_max": 31, "version_evidence": "2004年3月14日",
        "last_sentence": "中华人民共和国国歌是《义勇军进行曲》。",
        "note": "独立官方转载全文；原人大站TLS失败后另列来源，不覆盖失败下载证据。",
    },
    {
        "document_id": "juvenile-delinquency-prevention-2020", "law_name": "中华人民共和国预防未成年人犯罪法",
        "aliases": ["中华人民共和国预防未成年人犯罪法", "预防未成年人犯罪法"],
        "source_url": "https://www.moe.gov.cn/jyb_sjzl/sjzl_zcfg/zcfg_qtxgfl/202110/t20211025_574843.html",
        "publisher": "教育部（来源：国家法律法规数据库）", "version_date": "2020-12-26",
        "effective_date": "2021-06-01", "version_status": "dated_revised_publication; currentness_not_asserted",
        "expected_max": 68, "version_evidence": "2020年12月26日", "last_sentence": "施行。",
        "note": "2020年修订全文，68条；用于独立法条检索，不含基准问答。",
    },
    {
        "document_id": "constitution-amendment-2004", "law_name": "中华人民共和国宪法修正案（2004年）",
        "aliases": ["中华人民共和国宪法修正案（2004年）", "宪法修正案2004年", "宪法修正案（2004年）"],
        "source_url": "https://www.npc.gov.cn/c2/c183/c198/201905/t20190522_3826.html",
        "publisher": "中国人大网", "version_date": "2004-03-14", "effective_date": "2004-03-14",
        "version_status": "historical_amendment; not_consolidated_constitution",
        "expected_min": 18, "expected_max": 31, "version_evidence": "2004年3月14日",
        "last_sentence": "中华人民共和国国歌是《义勇军进行曲》。",
        "note": "2004年完整修正案编号从第十八条至第三十一条，不与宪法正文条号混用。",
    },
    {
        "document_id": "civil-code-2020", "law_name": "中华人民共和国民法典", "aliases": ["中华人民共和国民法典", "民法典"],
        "source_url": "https://tjca.miit.gov.cn/zwgk/zcwj/flfg/art/2020/art_20cf1a2e1b854924b5caa744c8045d1f.html",
        "publisher": "天津市通信管理局（工信部）", "version_date": "2020-05-28", "effective_date": "2021-01-01",
        "version_status": "dated_original_publication; currentness_not_asserted", "expected_max": 1260,
        "version_evidence": "2020年5月28日第十三届全国人民代表大会第三次会议通过",
        "last_sentence": "同时废止。", "note": "完整2020年通过文本；网页发布日期不作为修订年份。",
    },
    {
        "document_id": "farmer-cooperatives-2006", "law_name": "中华人民共和国农民专业合作社法",
        "aliases": ["中华人民共和国农民专业合作社法", "农民专业合作社法"],
        "source_url": "https://jiwei.yantai.gov.cn/art/2015/11/27/art_5733_150968.html",
        "publisher": "中共烟台市纪律检查委员会、烟台市监察委员会", "version_date": "2006-10-31", "effective_date": "2007-07-01",
        "version_status": "historical_superseded_by_2017_revision", "expected_max": 56,
        "version_evidence": "2006年10月31日第十届全国人民代表大会常务委员会第二十四次会议通过",
        "last_sentence": "施行。", "note": "2006年通过的历史版本，56条。2017年修订为74条，条号不可混用。",
    },
    {
        "document_id": "farmer-cooperatives-2017", "law_name": "中华人民共和国农民专业合作社法",
        "aliases": ["中华人民共和国农民专业合作社法", "农民专业合作社法"],
        "source_url": "https://fgs.moa.gov.cn/flfg/202007/t20200716_6348749.htm",
        "publisher": "农业农村部（来源：中国人大网）", "version_date": "2017-12-27", "effective_date": "2018-07-01",
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 74,
        "version_evidence": "2017年12月27日第十二届全国人民代表大会常务委员会第三十一次会议修订",
        "last_sentence": "施行。", "note": "2017年修订全文，74条。不能作为2006年旧条号的替代答案。",
    },
    {
        "document_id": "administrative-litigation-2017", "law_name": "中华人民共和国行政诉讼法",
        "aliases": ["中华人民共和国行政诉讼法", "行政诉讼法"],
        "source_url": "https://www.moe.gov.cn/jyb_sjzl/sjzl_zcfg/zcfg_qtxgfl/202110/t20211029_576011.html",
        "publisher": "教育部（来源：国家法律法规数据库）", "version_date": "2017-06-27", "effective_date": None,
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 103,
        "version_evidence": "2017年6月27日", "last_sentence": "施行。",
        "note": "2017年第二次修正全文；末条1990年施行日期是本法原始日期，未猜测本次修正生效日期。",
    },
    {
        "document_id": "traffic-points-2021", "law_name": "道路交通安全违法行为记分管理办法",
        "aliases": ["道路交通安全违法行为记分管理办法", "交通违法记分管理办法"],
        "source_url": "https://www.gov.cn/gongbao/content/2022/content_5679697.htm",
        "publisher": "国务院公报（中国政府网）", "version_date": "2021-12-17",
        "effective_date": "2022-04-01", "version_status": "dated_original_publication; currentness_not_asserted",
        "expected_max": 37, "version_evidence": "2021年12月17日", "last_sentence": "本办法自2022年4月1日起施行。",
        "note": "公安部令第163号全文，37条；按公布日期登记版本，未依据评测答案选择条文。",
    },
    {
        "document_id": "medical-device-registration-2021", "law_name": "医疗器械注册与备案管理办法",
        "aliases": ["医疗器械注册与备案管理办法", "医疗器械注册备案管理办法"],
        "source_url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2023/art_568880e3ee344c45b38d073bba1c53ad.html",
        "publisher": "国家市场监督管理总局", "version_date": "2021-08-26",
        "effective_date": "2021-10-01", "version_status": "dated_original_publication; currentness_not_asserted",
        "expected_max": 124, "version_evidence": "2021年8月26日",
        "last_sentence": "本办法自2021年10月1日起施行。2014年7月30日原国家食品药品监督管理总局令第4号公布的《医疗器械注册管理办法》同时废止。",
        "note": "国家市场监督管理总局令第47号全文，124条。",
    },
    {
        "document_id": "medical-device-operation-2022", "law_name": "医疗器械经营监督管理办法",
        "aliases": ["医疗器械经营监督管理办法", "医疗器械经营管理办法"],
        "source_url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2023/art_51afc62ef3c84455b28b113a628f9e35.html",
        "publisher": "国家市场监督管理总局", "version_date": "2022-03-10",
        "effective_date": "2022-05-01", "version_status": "dated_original_publication; currentness_not_asserted",
        "expected_max": 73, "version_evidence": "2022年3月10日",
        "last_sentence": "本办法自2022年5月1日起施行。2014年7月30日原国家食品药品监督管理总局令第8号公布的《医疗器械经营监督管理办法》同时废止。",
        "note": "国家市场监督管理总局令第54号全文，73条。",
    },
    {
        "document_id": "cosmetics-operation-2021", "law_name": "化妆品生产经营监督管理办法",
        "aliases": ["化妆品生产经营监督管理办法", "化妆品生产经营管理办法"],
        "source_url": "https://www.samr.gov.cn/zw/zfxxgk/fdzdgknr/fgs/art/2023/art_bb0c54e8c2374a5a9b4b0c8ddaf04e13.html",
        "publisher": "国家市场监督管理总局", "version_date": "2021-08-02",
        "effective_date": "2022-01-01", "version_status": "dated_original_publication; currentness_not_asserted",
        "expected_max": 66, "version_evidence": "2021年8月2日", "last_sentence": "本办法自2022年1月1日起施行。",
        "note": "国家市场监督管理总局令第46号全文，66条。",
    },
    {
        "document_id": "criminal-law-2023", "law_name": "中华人民共和国刑法", "aliases": ["中华人民共和国刑法", "刑法"],
        "source_url": "https://fgk.chinatax.gov.cn/zcfgk/c100009/c5212248/content.html",
        "publisher": "国家税务总局政策法规库", "version_date": "2023-12-29", "effective_date": None,
        "version_status": "dated_revised_publication; currentness_not_asserted", "expected_max": 452,
        "version_evidence": "2023年12月29日", "last_sentence": "适用本法规定。",
        "appendix_last_text": "关于惩治虚开、伪造和非法出售增值税专用发票犯罪的决定",
        "note": "截至修正案（十二）的2023年官方汇编。第199条保留官方的（删去）占位，附录单独保存。未据网站年份猜测本次修正生效日期。",
    },
]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_document(source: dict[str, Any], raw: bytes, downloaded_at: str,
                   actual_url: str | None = None) -> dict[str, Any]:
    # These official pages declare UTF-8. A failed decode is an error, not silently
    # replaced characters that would become purported legal text.
    charset = re.search(rb"charset\s*=\s*[\"']?([A-Za-z0-9_-]+)", raw[:20000], re.I)
    encoding = charset.group(1).decode("ascii") if charset else "utf-8"
    page = raw.decode(encoding)
    text = html_to_text(page)
    normalized = unicodedata.normalize("NFKC", text)
    if re.sub(r"\s+", "", source["version_evidence"]) not in re.sub(r"\s+", "", normalized):
        raise ValueError(f"Declared version not supported by publication: {source['document_id']}")
    # Validate article sequence first, then trim the final article at its explicit
    # statutory closing sentence. This excludes navigation, copyright and annexes.
    first_number = source.get("expected_min", 1)
    articles = split_articles(text, source["expected_max"] if first_number == 1 else None)
    if first_number != 1:
        actual = {item["article_number"] for item in articles if item["subarticle_number"] is None}
        if actual != set(range(first_number, source["expected_max"] + 1)):
            raise ValueError("Incomplete amendment article sequence")
    final = articles[-1]
    boundary = final["text"].find(source["last_sentence"])
    if boundary < 0:
        raise ValueError("Missing statutory closing sentence")
    final_end = boundary + len(source["last_sentence"])
    appendix = ""
    if source.get("appendix_last_text"):
        remainder = final["text"][final_end:]
        appendix_end = remainder.find(source["appendix_last_text"])
        if appendix_end < 0:
            raise ValueError("Missing complete statutory appendix")
        appendix = remainder[:appendix_end + len(source["appendix_last_text"])].strip()
    final["text"] = final["text"][:final_end]
    final["text_sha256"] = _sha(final["text"].encode())
    return {
        "schema_version": SCHEMA_VERSION,
        **{key: value for key, value in source.items() if key not in {"last_sentence", "expected_max", "appendix_last_text"}},
        "document_year": int(source["version_date"][:4]) if source.get("version_date") else None,
        "downloaded_at": downloaded_at, "downloaded_url": actual_url or source["source_url"],
        "raw_sha256": _sha(raw), "raw_bytes": len(raw), "raw_encoding": encoding,
        "article_count": len(articles), "max_article_number": source["expected_max"],
        "completeness_check": "all_base_articles_1_through_max_present_once" if first_number == 1 else f"all_base_articles_{first_number}_through_{source['expected_max']}_present_once",
        "provenance_policy": "complete_official_publication_only; no_benchmark_answers_or_predictions",
        "articles": articles, "appendix_text": appendix,
    }


def build(output: Path, source_ids: set[str] | None = None) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    selected = [source for source in SOURCES if source_ids is None or source["document_id"] in source_ids]
    if not selected or (source_ids and source_ids - {source["document_id"] for source in selected}):
        raise ValueError("Unknown source id")
    # Fail rather than overwrite a frozen corpus. Rebuild into a new directory.
    if (output / "manifest.json").exists():
        raise FileExistsError(f"Frozen corpus already exists: {output}")
    documents: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for source in selected:
        now = datetime.now(timezone.utc).isoformat()
        raw: bytes | None = None
        try:
            host = urlparse(source["source_url"]).hostname or ""
            if not host.endswith(".gov.cn"):
                raise ValueError("Only explicitly listed official .gov.cn sources are allowed")
            request = Request(source["source_url"], headers={"User-Agent": "LexVault-public-law-corpus/1.0"})
            with urlopen(request, timeout=45) as response:
                actual_url = response.geturl()
                if not (urlparse(actual_url).hostname or "").endswith(".gov.cn"):
                    raise ValueError("Download redirected outside official government domain")
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("Publication exceeds 8 MiB download limit")
            # A few official sites return gzip even without Accept-Encoding.
            # Hash and preserve the decoded publication bytes used by parsing;
            # decompression is bounded to the same artifact limit.
            if raw.startswith(b"\x1f\x8b"):
                raw = gzip.decompress(raw)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError("Decompressed publication exceeds 8 MiB limit")
            (output / f"{source['document_id']}.html").write_bytes(raw)
            document = build_document(source, raw, now, actual_url)
            stem = source["document_id"]
            serialized = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
            (output / f"{stem}.json").write_bytes(serialized)
            documents.append({"document_id": stem, "document_file": f"{stem}.json",
                              "document_sha256": _sha(serialized), "raw_file": f"{stem}.html",
                              "raw_sha256": _sha(raw), "source_url": source["source_url"],
                              "version_date": source["version_date"], "article_count": document["article_count"],
                              "downloaded_at": now})
        except Exception as error:
            failures.append({"document_id": source["document_id"], "source_url": source["source_url"],
                             "attempted_at": now, "error": f"{type(error).__name__}: {error}",
                             "raw_sha256": _sha(raw) if raw is not None else "",
                             "raw_file": f"{source['document_id']}.html" if raw is not None else ""})
    manifest = {"schema_version": SCHEMA_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                "source_policy": "complete_official_publications; independent_of_benchmark_labels",
                "version_policy": "explicit_versions; no_implicit_current_law_claim", "documents": documents,
                "failures": failures}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def verify(directory: Path) -> dict[str, Any]:
    """Recheck frozen bytes and re-extract articles from the saved publication."""
    corpus = LegalCorpus(directory)
    errors: list[str] = []
    verified_articles = 0
    sources = {source["document_id"]: source for source in SOURCES}
    for entry in corpus.manifest["documents"]:
        document = next(doc for doc in corpus.documents if doc["document_id"] == entry["document_id"])
        raw_path = directory / entry["raw_file"]
        if raw_path.resolve().parent != directory.resolve():
            raise ValueError("Raw source path must remain in the corpus directory")
        raw = raw_path.read_bytes()
        if _sha(raw) != entry["raw_sha256"] or _sha(raw) != document["raw_sha256"]:
            errors.append(f"Raw publication hash mismatch: {entry['document_id']}")
            continue
        source = sources.get(entry["document_id"])
        if source is None:
            errors.append(f"Unregistered source: {entry['document_id']}")
            continue
        rebuilt = build_document(source, raw, document["downloaded_at"], document["downloaded_url"])
        if rebuilt != document:
            errors.append(f"Publication re-extraction mismatch: {entry['document_id']}")
            continue
        verified_articles += len(document["articles"])
    return {"schema_version": SCHEMA_VERSION, "valid": not errors, "errors": errors,
            "document_count": len(corpus.documents), "verified_articles": verified_articles,
            "checked_at": datetime.now(timezone.utc).isoformat()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/score85/legal_corpus"))
    parser.add_argument("--source", action="append", choices=[source["document_id"] for source in SOURCES])
    parser.add_argument("--verify", action="store_true", help="Verify saved publication hashes and article extraction without network")
    args = parser.parse_args()
    if args.verify:
        result = verify(args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["valid"]:
            raise SystemExit(1)
        return
    manifest = build(args.output, set(args.source) if args.source else None)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if manifest["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
