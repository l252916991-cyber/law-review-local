# 项目 RAG 评测口径

`rag_project_benchmark.py` 是页级检索工程回归，不是法律回答正确率或真实案件验收。运行时必须保留逐题结果、数据集与源码哈希、请求配置，以及实际观测到的 embedding 后端、降级状态和重排启用状态。

## 页级指标

- `recall_at_k`：返回的唯一金标文档页占全部金标文档页的比例。
- `page_precision_at_k`：金标文档页占全部返回页的比例。该指标保留原有口径，跨文档结果也进入分母。
- `within_document_page_precision`：仅在返回结果中属于任意金标文档的页上计算，分子是其中的金标页。不可回答题或没有返回任何金标文档页时为 `null`，缺失旧字段也不按零分处理。
- `complete_recall_rate`：可回答题是否找齐全部金标页的平均值。

`within_document_page_precision` 是条件指标，排除了跨文档误命中。它必须与 `recall_at_k` 一起报告：只命中一个金标文档中的正确页时，该指标可能很高，但其他金标页仍可能完全漏召回。它不能替代总体页精度或召回率。

摘要中的 overall、`by_challenge`、`by_runtime` 和模板聚类 bootstrap 区间都包含该指标，并报告参与条件均值的有效 query 数。配对比较也包含它及有效 pair 数；任一侧缺少该字段的题对会被跳过，不会把缺失值当作 0。只有全部题对都缺少可比值时，整组差值和区间才为 `null`、有效 pair 数为 0。

## 实际运行配置分组

`configuration` 记录请求配置，`by_runtime` 按每题实际观测到的以下三项组合分组：

- embedding backend；
- degraded 标志；
- reranker enabled 标志。

每个稳定组名和值都显式包含这三个维度，值还包含与 overall 相同口径的组内指标。只要一次运行同时出现完整和降级路径，它们就会成为不同组；不得只引用请求的 model/on 配置或混合 overall，把混合结果表述成完整模型运行结果。

缺失的 degraded 或 reranker 遥测保留为 `null`，组名写为 `unknown`，不会与明确观测到的 `false` 合并或被表述为完整运行。

## 解释边界

- `unanswerable_empty_rate` 只判断检索结果是否为空。检索为空不等于答案系统实施了弃答；检索到相关材料也可能支持“证据不足”的答案。
- `quote_presence_rate` 只检查结果中是否存在 quote 文本，不验证引文是否支持主张，因此不是 citation faithfulness 或 entailment。
- classic 240 题由 20 个问题模板在 12 个合成案件变体上生成。它是确定性工程回归，不是 240 个独立标注的真实案件验收样本；任何合成模板数量（包括 200 条）都不得直接宣称为 200 个独立验收案例。
- challenges 12 题同样是合成诊断集，不是独立真实案件验收。

空结果集和非正 `k` 不生成摘要。模型或重排配置失败时应显式失败，不得把降级结果并入完整运行组。真实业务结论仍需独立来源、人工标注和冻结验收集。
