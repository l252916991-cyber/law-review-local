# 独立官方法条语料

`app/legal_corpus.py` 与 `scripts/build_legal_corpus.py` 提供按法律版本隔离的全文下载、按条切分、条号精确查询及中文二元词检索。模块不会读取基准答案、旧预测、应用案件数据库或调用本地模型，也尚未改变应用的默认检索行为。

语料位置是 `output/score85/legal_corpus/`。2026-09-05 首次完整构建获得 **4 部法律、5 个版本、1998 个条文单元**：

| 法律与原文版本 | 官方公开来源 | 条文单元 | 版本说明 |
| --- | --- | ---: | --- |
| 民法典，2020-05-28 | [天津市通信管理局](https://tjca.miit.gov.cn/zwgk/zcwj/flfg/art/2020/art_20cf1a2e1b854924b5caa744c8045d1f.html) | 1260 | 2020 年通过原文 |
| 农民专业合作社法，2006-10-31 | [烟台市纪委监委](https://jiwei.yantai.gov.cn/art/2015/11/27/art_5733_150968.html) | 56 | 被 2017 年修订替代的历史文本 |
| 农民专业合作社法，2017-12-27 | [农业农村部](https://fgs.moa.gov.cn/flfg/202007/t20200716_6348749.htm) | 74 | 2017 年修订全文 |
| 行政诉讼法，2017-06-27 | [教育部](https://www.moe.gov.cn/jyb_sjzl/sjzl_zcfg/zcfg_qtxgfl/202110/t20211029_576011.html) | 103 | 2017 年第二次修正全文 |
| 刑法，2023-12-29 | [国家税务总局政策法规库](https://fgk.chinatax.gov.cn/zcfgk/c100009/c5212248/content.html) | 505 | 452 个基础条号（含第 199 条官方“删去”占位）与 53 个“之一”等增补条文；附录另存 |

每份 JSON 记录文书年份、通过/修订日期、已核实的施行日期或 `null`、版本状态、下载时间、原文 URL、原始 HTML SHA-256、每条文本 SHA-256；manifest 还保存整份 JSON 的 SHA-256。原始网页完整保存，文章尾部、页脚及刑法附录边界经过显式检查。版本日期来自公布内容，不能由网页路径或转载年份代替。除明确的历史状态外，本轮未开展截至执行日的全部法律有效性审查，因此语料没有自动标成“现行法”。

## 复现与使用

```sh
.venv/bin/python scripts/build_legal_corpus.py --output output/score85/legal_corpus_rebuild
.venv/bin/python scripts/build_legal_corpus.py --output output/score85/legal_corpus --verify
.venv/bin/python -m pytest -q tests/test_legal_corpus.py --disable-socket --allow-unix-socket
```

已有 manifest 的目录拒绝覆盖。重建应使用新目录；`--verify` 完全离线，校验文件哈希，并从原始 HTML 重新切分，要求完整文书与保存 JSON 一致。

```python
from app.legal_corpus import LegalCorpus

corpus = LegalCorpus("output/score85/legal_corpus")
article = corpus.lookup("农民专业合作社法", 44, version_date="2017-12-27")
matches = corpus.search("盈余 分配 成员 交易量", law_name="农民专业合作社法",
                        version_date="2017-12-27", limit=3)
```

结果保留原文、条号、法律名称、版本日期和来源。法律存在多个版本但调用方没有指定版本时，精确查询和指定法律检索会抛出 `VersionAmbiguityError`。跨法律搜索可以返回不同版本的单独结果，但结果不能被拼成一部无版本的“法”。

## 与固定基准的关系

历史基准的题干常只问法律名称与条号，省略年份。例如合作社法从 56 条修订为 74 条，同一条号在两版中可以讨论不同事项。用较新版本替换旧条号会造成法律语义错误，也可能降低对历史参考答案的重合度。接入固定基准时，版本只能依据题干明确年份、公开的任务年代规范或事先冻结的全任务版本策略选择；不得读取单题参考答案选择哪一版更容易得分。题干无法消除版本歧义，应记录该不确定性。2023 年刑法也不能无说明地被当成历史试题年代的刑法。

对法条背诵，精确条号查询可减少模型记忆错误；对场景预测，当前纯词汇检索仅提供候选条文，不能证明适用性。此语料准备尚不是混合均分达到 85 的证据，需要在冻结评分协议下通过真实推理验证。

## 构建时发现的数据质量问题

本机按文件清单检查到基准题库、输出报告及案件材料，未发现带来源、版本与完整性证明的可直接复用法条库。没有把题库或案件材料导入这个语料。

- 湖南省市场监管局的 2006 年合作社法转载在本机发生 TLS `BAD_ECPOINT`，没有关闭证书验证绕过。
- 辽宁省民政厅的同法页面只提取到前 28 条，完整性检查拒绝作为 56 条完整法律导入。
- 广州市统计局的刑法 2020 年转载缺第十二条，且没有第 199 条删除占位；构建检查报缺条并拒收。中间构建记录保留在 `output/score85/legal_corpus_initial/manifest.json`。后续改用条号完整的国家税务总局 2023 年公开文本。
- 山东法院一个 2020 年刑法页面在本机 TLS 握手超时。以上下载失败没有通过补写法条或拼接基准答案掩盖。

完整性检查证明所登记的基础条号齐全且不重复，逐字法律准确性仍依赖来源文本；官方域名本身不等于转载绝无错误。
