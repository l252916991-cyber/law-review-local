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

## 2026-09-09 部门规章补充集

为覆盖场景到法条任务中缺失的公开部门规章，构建器新增四个独立官方来源：国务院公报公开的《道路交通安全违法行为记分管理办法》，以及国家市场监督管理总局公开的《医疗器械注册与备案管理办法》《医疗器械经营监督管理办法》《化妆品生产经营监督管理办法》。它们合计 300 个基础条文；每份文书仍执行版本证据、连续条号、法定收尾句和哈希校验。

```sh
uv run --locked python scripts/build_legal_corpus.py \
  --output output/score85/legal_corpus_supplement_ministry_v1 \
  --source traffic-points-2021 \
  --source medical-device-registration-2021 \
  --source medical-device-operation-2022 \
  --source cosmetics-operation-2021
uv run --locked python scripts/build_legal_corpus.py \
  --output output/score85/legal_corpus_supplement_ministry_v1 --verify
```

该补充集只增加来源无关的语料覆盖，不改生成模型，也不把评测参考答案写入查询或语料。

## 交通警察执勤规范正文补充

常州市旧链接已重定向到首页。改用[昌都市公安局正文](https://gaj.changdu.gov.cn/cdsgaj/c102212/202005/cd0d15663c3d42019d8c2309991a3c1f.shtml)，现有解析器可直接识别完整的第1至86条。
[湖南省公安厅发布说明](https://gat.hunan.gov.cn/gat/jwgk/zfxxgk/xxgkml/ghjh/200812/t20081230_14734998.html)确认修订发布日期为2008-11-15；正文末条确认2009-01-01施行并废止2005年版。
版本日期补充来源写入文书元数据。页面只刊载部分附件，因此索引范围明确为86条正文，附件全部排除；原始网页完整保留。

```sh
uv run --locked python scripts/build_legal_corpus.py --output output/score85/legal_corpus_supplement_police_v1 --source traffic-police-duty-2008
uv run --locked python scripts/build_legal_corpus.py --output output/score85/legal_corpus_supplement_police_v1 --verify
```

构建与离线复验通过：1份文书、86条、valid=true。实验配置为 `benchmarks/score85/qwythos8bit-statutory-police-v9.json`。

固定模型复测：`dev20-statutory-police-v9` 为47.64分，上一版47.30分，1升19平0降；执法偏差题第79条召回排名第一，单题21.13→27.88。`confirm10-statutory-police-v9` 为47.96分，10题均与上一版相同；30次调用无错误、空答或截断。按已保留方案直接接入的要求，`weak4-candidate-v1.json` 已加入该库。小样本结果不代表200/1000题验收完成。

## 2026-09-11 宪法补充集（npc_v8）

`build_npc_corpus.py` 原按题目推导出的法名做官方标题精确匹配，官方库把整合版登记为《中华人民共和国宪法（2018年修正文本）》，导致 `中华人民共和国宪法` 报 "No exact official version"。新增 `TITLE_OVERRIDES` 将该请求名映射到官方整合版标题，抓取到 1 份文书、143 条（含第一百二十六条），valid=true。本次为完整的 2018 年修正后整合文本，非修正案（修正案本身只含第32–52项）。

`app/benchmark_retrieval.py` 增加最小回退：当问题点名某修正案、而该条不在修正案自身条文中时，落到同法制定的整合版文本按条号查询。第500题"宪法修正案2018年第一百二十六条"命中整合版宪法第一百二十六条。

```sh
uv run --locked python scripts/build_npc_corpus.py \
  --output output/score85/legal_corpus_supplement_npc_v8 \
  --campaign <仅含宪法题的 inputs.jsonl> \
  --existing-corpus <已有语料目录，可重复>
uv run --locked python scripts/build_npc_corpus.py --output output/score85/legal_corpus_supplement_npc_v8 --verify
```

LawBench 1-1 法条检索覆盖由 492/500 提升到 **500/500**（按 `weak4-candidate-v1.json` 链 + npc_v8 实测）。该口径只证明条号可精确检索到官方法条，不代表生成模型最终得分；模型基准仍需授权环境实测。

## P2 独立实验层（默认不启用）

`app/statutory_index.py` 接收 `LegalCorpus`、`IndexSpec`、以 `document_id/article_id` 为键的预计算 vectors，以及按 document_id 的 validity 映射。索引仅做文件与纯计算操作，不创建模型客户端，不接入现有 `retrieve` 或 `retrieve_statutory`。指纹包括完整 manifest/文书内容、schema、parser 版本与源码哈希、embedding 模型身份（应含不可变 revision）/backend/dim、normalization、segmentation 和 validity；加载缓存逐项失配即拒绝，不能只匹配维度。

现有 `effective_date` 仅是已核实施行日期，`version_date` 是公布/修订日期，`currentness_not_asserted` 不等于现行。validity 必须显式提供 `effective_from`、`effective_to`、非空 `source`；`effective_to: null` 是有依据的开放区间声明，不是字段缺失的默认值。不得从后版公布时间、年份状态字符串或金标推造历史区间。适配器检查与已有 effective_date 一致；需要人工核实来源真实性。按 `effective_from <= query_effective_date < effective_to` 过滤，无日期使用当天。元数据缺失、矛盾或同法多个有效版本会报告，实验请求不可用，不静默混用版本。

`app/statutory_hybrid.py` 的 `StatutoryHybrid.search` 提供 lexical、dense、rrf、rerank 四模式；explicit_law 是硬过滤，inferred_law 只加分。客户端按现有 `EmbeddingClient.embed` / `RerankClient.score` 协议注入，默认不创建客户端。query embedding 身份、backend、维度与有限值必须匹配；last_failure、无结果或非法分数不会伪装成 lexical 成功。rerank 对 RRF 候选排序，默认候选上限48。

`app/statutory_benchmark.evaluate(retriever, dataset, config, split="both")` 是独立离线基准入口。dataset 包含 `tasks`，每题提供唯一 id、query、dev/confirm split、冻结 query_effective_date、`gold: {"document_id/article_id": 1..3}` 和可选 explicit_law/inferred_law；fixture 必须设置 `fixture: true`。相同 query 禁止跨题复用，检索只看到 query/约束，不看到 gold。先在 dev 调参，冻结数据、索引与配置哈希后只在 confirm 验收，不按 confirm 调参。

输出四组 Hit@5、MRR@10、NDCG@10，逐题 available/failure，成功子集均值、与 lexical 的共同成功配对数、candidate-minus-lexical delta、固定随机种子的逐题 paired bootstrap 95% CI、全部尝试的 p50/p95、embedding calls/query、rerank candidates/query。失败指标是 null，不把失败当成功或回退；门槛要求所有 confirm 查询成功。模型时间由客户端真实计时，fixture 时间不可代表模型性能。

冻结门槛位于 `benchmarks/statutory-experiment-v1.json`：主指标 Hit@5；dev 至少20题，增益>=0.05且95% CI下界>=0；confirm 至少10题且Hit@5增益>0（同方向），MRR/NDCG不降，p95<=2000ms，embedding<=1次/query、rerank<=48候选/query。默认同时评估两个split，仅联合结果的 promotion 可验收；单独confirm永不能promotion。报告身份冻结 dataset/config/index、vectors摘要、检索候选数/RRF常量/soft boost、embedding/reranker模型身份；身份缺失（包括无reranker model）禁止promotion。这些是**预设建议，不是已证实收益**。fixture 永不通过收益验收。离线固定题目与 fake clients 见 `tests/test_statutory_experiment.py`；真实四模式实验尚缺核实的validity、预计算vectors、真实金标confirm和已授权模型服务，未授权任何外部或付费模型调用。

检索信号是调用方显式传入的 Query Signals，本层不从自然语言自动提取法律名称或日期。lexical 在资格过滤后使用与 LegalCorpus.search 相同的中文bigram公式、语料顺序平分规则和6位结果舍入；inferred_law存在时额外soft boost，因此纯原词法基线应不传inferred_law。索引的 search 负责无网络余弦向量检索。

### P1 后续阶段边界

本轮不实现 P1。后续完整覆盖预算 `{type: char, limit, used, excluded}`、typed char/span provenance、完整span超预算时跳过而不截断、`source_segments` 来源追踪、三类 guards 及四组 rerank 对照（含长度惩罚）。先冻结预算单位与分配、guard失败策略及长度惩罚公式，再用独立断言与 dev/confirm 对比验证；Graph/Rewrite延后。本次不扩展这些代码，不得把 P2 检索分数提升解释为上述 P1 已交付。
