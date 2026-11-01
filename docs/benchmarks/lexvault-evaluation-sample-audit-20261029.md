# LexVault 评测样本量、标注与旧账本审计

- 日期：2026-10-29（Asia/Shanghai）；只涉及本律所项目。
- 基线：`b70d783` 的答案评测 pipeline；分支 `codex/lexvault-eval-completion`。与多人协作部署验收分支分别保留，本文不把 HTTP/权限/备份验收当成模型评测。
- 原始资产来自主检出 `/Users/xiaoy/Documents/本地律所`，仅只读复算；不改旧题、旧回答或六臂忠实度 JSON。新增复算记录放在忽略目录 `output/evaluation-gap-audit-20261029/`。

## 1. 题、案件、重复采样和 claim 是不同分母

| 类别 | dev 题数 | dev 案件数 | 旧 holdout 题数 | 旧 holdout 案件数 |
|---|---:|---:|---:|---:|
| normal | 30 | 1 | 12 | 6 |
| contradiction | 10 | 1 | 6 | 6 |
| insufficient | 8 | 1 | 3 | 3 |
| irrelevant | 4 | 1 | 2 | 2 |
| statute_missing | 4 | 1 | 1 | 1 |
| injection | 4 | 1 | 0 | 0 |
| 全集 | 60 | 1 | 24 | 6 |

数据为 [dev v2](../../benchmarks/answer_eval/v2/dataset.json) 和 [旧 holdout-v1](../../benchmarks/answer_eval/holdout-v1/dataset.json)。dev 全部来自同一合成案件；同案问题共享文书与事实，不能直接当成跨案件独立样本。A/B/C 三臂及同题多次生成不增加题目/案件数。六臂 1,451 条由程序切分的事实 claim 也不是 1,451 道独立题。

小类别的原始计数是：

| 观察 | 题数 / 案件数 | supported / 事实 claim | 正确解释 |
|---|---:|---:|---|
| dev-A statute_missing | 4 / 1 | 1 / 8（12.5%） | 这四题输出的 claim 代理支持率 |
| dev-B statute_missing | 4 / 1 | 1 / 10（10.0%） | 同上，不是十道独立题 |
| holdout-C statute_missing | 1 / 1 | 7 / 24（29.17%） | 单题 `C-statute_missing-01` 的输出诊断 |
| holdout-C irrelevant | 2 / 2 | 0 / 5（0%） | 两题输出的 claim 诊断 |
| holdout-A irrelevant | 2 / 2 | 0 / 1（0%） | 两题中只有一题产生可计分事实 claim |

因此不再根据这些比例给出“最不忠实的类别”或稳定类别排名。dev normal（30题）与 contradiction（10题）题数相对较多，可以作本批探索性描述；**这两类也未被证明有统计显著性或充分统计效力**。其中只有一个案件，尚无独立人工标签、预先登记的效应量/功效或适当总体抽样依据。三轮重复和拆分 claim 不能弥补这些缺口。

“每类至少八题”只是覆盖设计下限，不是统计效力门槛。要判断“某类下降三个点”，还需预先确定最小有意义差异、容许区间、聚类单位、配对方式及样本量依据；不能先看到结果再改变门槛。

复核明细见 `sample-denominators.json`：题数、案件数、产生事实 claim 的题数、supported 和 claim 分开保存，`human_review=false`。dev 数据集 SHA-256 为 `428264866dccc02b6ab8e83b9cbc8a9ac52f392906eeb8c1aff8b3a609eabf47`，旧 holdout 数据集为 `10739d034242cc0474fe1c66fc98e2c921c2b70c7b026fc1fa6128910596749b`。

## 2. 代理分数与人工标注质量分开报告

两份数据集的 `annotation_source` 分别为 `assistant_generated_from_repository_synthetic_fixture` 和 `assistant_generated_from_authored_synthetic_fixture`，notice 均明确未经律师或独立人工标注。新建题目、第二个模型评分或编码助手复查，不等于两个独立人的标注一致性。没有独立标签时，人工一致性和 κ 保持未完成，不能填 0、1 或以代理得分代替。

六臂忠实度使用的 IDF n-gram/数字一致性是结构性代理判据，不自动判定法律语义或断言蕴含。缺口陈述的分桶可能漏掉合理拒答；被判为 unsupported 的句子需要逐条核对，不能全部称为幻觉。

dev-A 关键词覆盖 97.5% 与 claim 代理支持率 46.37% 可以并列报告，说明两种指标观察的方面不同。两者计分对象、分母与判据不同，**相减得到的约 51 个百分点不能解释为“引用幻觉比例”**。臂间描述性排序差异同样不能自动表述为显著差异。

## 3. 已打开的旧集不能通过追加题目重新变成 clean holdout

[旧保留集结果](../history/migration/portfolio-p2-holdout-results-20261028.md) 已公开逐类别和案例差异，并明确这批题用于后续调参时应转为开发数据。旧 24 题保留历史身份和结果；在其上补七道缺法条、六道无关题，不能把合并后的整套数据重新称为未见终评集。

新的候选应先冻结实现、检索/提示配置、模型、评分规则和预算，再单独编制并登记新版本。新版本需区分作者可见、实现者已见、是否已运行、是否经过独立人工标注及是否用于调参；“未运行”本身不等于盲测。本文没有生成或读取未来的新 holdout 正文，也不宣布新保留集或律师验收已完成。

## 4. 旧账本已含真实 usage；是导出层丢了字段

原始 `output/portfolio-ablation/{dev,holdout}-qwen38-27b-20261027/run/attempts.jsonl` 已记录生成调用的 input/output tokens、provider latency、elapsed、outcome 和 usage_known。旧 `export_ablation_answers.py` 只导出正文、引用与 answer_status，因此“导出的 answers 缺 usage”不能扩大解释为原始生成没有成本数据。

本轮导出保留这些原字段和账本摘要：缺失保持 null，记录的零仍为零；失败和调用前拒答也保留独立 outcome，不根据空正文或 answer_status 冒充生成成功。新增分析器 [analyse_eval_reliability.py](../../scripts/analyse_eval_reliability.py) 只读账本，计划分母包含失败及 not_attempted；已执行调用的成本也包含 contract_failed，不只统计 ok。

| 数据 / 臂 | 计划 / 已执行 | 通过 / 计划 | 已执行 input + output token（含失败） | 每通过采样 token |
|---|---:|---:|---:|---:|
| dev-A | 60 / 59 | 52 / 60 | 31,167 + 19,514 = 50,681 | 974.63 |
| dev-B | 60 / 59 | 46 / 60 | 216,330 + 27,449 = 243,779 | 5,299.54 |
| dev-C | 60 / 59 | 52 / 60 | 270,964 + 26,580 = 297,544 | 5,722.00 |
| holdout-A | 72 / 69 | 65 / 72 | 28,287 + 25,408 = 53,695 | 826.08 |
| holdout-B | 72 / 69 | 64 / 72 | 144,504 + 33,618 = 178,122 | 2,783.16 |
| holdout-C | 72 / 69 | 63 / 72 | 190,113 + 36,841 = 226,954 | 3,602.44 |

这里是记录的生成 token，不含 embedding、rerank、Judge 或价格，不是完整部署账单。旧汇总若只加 ok 的 token，会遗漏 B/C 的已付出但契约失败的调用，因此与本表可能不同。dev 177 次已执行调用、holdout 207 次已执行调用都有记录的 usage/timing；另有 3、9 次 not_attempted 保持缺失值，不擅自填零。所有计划记录口径与“已执行且成本完整”的口径分别输出。

## 5. 旧账本只能补三轮的结构稳定性，不能冒充五轮或语义一致性

dev 每题每臂只有一次记录，不能估同题重复方差；单条记录不算稳定。旧 holdout 每题每臂三次记录，24题仍只有24题、六案；其中23题实际生成，另1题每次都在生成前拒答。

三轮内 A、B 各有1题确定性通过判据翻转，C 为0题；outcome 变化分别为0、1、0题。分析器将布尔通过方差、outcome 变化和实际生成答案数分开保存；全部调用前拒答的稳定不当成模型答案稳定。原始文本摘要不同只代表措辞/字节不同，不据此判语义冲突；稳定错误也可能每次一致。

以要求五轮复算，两批各臂满足完整五轮的题数均为 **0**。案例级区间先对同题重复求均值，再按案件聚类重采样；dev 单案不出区间，holdout 六案只给探索性描述区间，`population_inference_validated=false`，不声称总体效果、法律质量或 SLA。此分析不替代接口失败、任务超时、返回阶段超时的独立故障注入。

## 6. 复算与验证

```sh
uv run --locked python scripts/analyse_eval_reliability.py \
  --attempts <已有只读批次>/run/attempts.jsonl \
  --expected-repeats 5 --output <全新私有路径>/reliability.json
uv run --locked python scripts/export_ablation_answers.py \
  --batch <已有只读批次> --group A --repeat 0 --output <另一个新路径>/answers.json
```

实际复算产物为 `dev-reliability.json`、`holdout-reliability.json` 和六份 `*-answers-with-usage.json`；输出目录为本文开头的忽略路径。源 dev 账本 SHA-256 为 `968112158a724437eb59a9d2fc9c0d55a5c7538402e6fe452c501d86b7054dec`，旧 holdout 账本为 `748cc50861f5fb48fd845524b97a2da3ebf510b7f94c95e0b7178e2a9e0a062c`；原记录未被覆盖，复算未调用模型。新增局部测试覆盖失败分母、未知成本、零值、重复身份、单案不出区间、三次不冒充五次和拒绝覆盖旧结果。
