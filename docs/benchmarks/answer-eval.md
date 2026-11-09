# 答案级评测口径

`app/answer_eval.py` + `scripts/evaluation/run_answer_eval.py` 回答的是"答案对不对"，与页级检索评测（"页找没找到"）分开报告，两者不可互相替代。

## 1. 与其它层的区别

| 层 | 问题 | 入口 |
| --- | --- | --- |
| 页级检索 | 金标页是否召回、排序是否靠前 | `scripts/benchmarks/rag_project_benchmark.py` |
| 法条检索 | 正确法条是否排在前列 | `scripts/benchmarks/statutory_gold_report.py` |
| **答案级** | 必需要点是否写全、是否出现无据断言、引用页是否可接受 | `scripts/evaluation/run_answer_eval.py` |
| 模型 LawBench | 任务级混合分，不等于应用准确率 | `scripts/benchmarks/unified_benchmark_runner.py` |

## 2. 数据集

`benchmarks/answer_eval/v1/demo-case.json`，17 题（13 可答 + 4 应拒答），字段：

```json
{"id": "fact-01", "question": "...", "must_include": ["2,860", "47"],
 "must_not_claim": ["1亿元"], "acceptable_pages": [["04_账户流水摘要.txt", 1]],
 "expect_refusal": false}
```

来源约束：

- 题目由内置**演示案件**的页文本构造，属于工程回归夹具。
- **不是律师标注的验收集**，不能据此宣称法律准确率或业务效果。
- 数据集自带 `notice` 字段，报告会原样带出，避免下游引用时丢口径。
- `tests/test_answer_eval.py` 会校验每个 `must_include` 确实出现在其可接受页、每页确实属于该案件。

## 3. 确定性评分

| 指标 | 口径 |
| --- | --- |
| `coverage` | 命中的 `must_include` ÷ 全部必需要点（归一化忽略空格、逗号、全角百分号） |
| `violations` | 命中 `must_not_claim` 的条数，任一条命中即判失败 |
| `citation_precision` | 引用页中属于可接受页的比例；无引用时为 `null`，不按 0 计 |
| `refused` | `answer_status` 为 `insufficient_evidence`/`clarification`，或回答含明确拒答标记 |
| `passed` | 可答题：要点全中 + 无违规 + 引用精度为 1 或无可引用；拒答题：确实拒答且无违规断言 |

## 4. 模型 judge（可选）

- 提示版本 `answer-judge-v1`，要求输出结构化 JSON：`coverage_ok` / `unsupported_claims` / `citation_ok` / `rationale`。
- judge 输出不可解析时标记 `available=false`，**不会**默认判通过。
- judge 与确定性分数并列报告，不替代确定性结论。
- 同模型自评存在偏差，需用人工抽检计算 Cohen's κ；`--human-labels` 会输出 `kappa` 与分歧题号。κ 低于 0.6 时应先调整提示或换 judge 模型，而不是继续引用分数。

## 5. 运行

```bash
# 离线评分（CI 使用，无需模型）
uv run --locked python scripts/evaluation/run_answer_eval.py --answers answers.json --output output/answer_eval/report.json

# 在线收集后评分（需要本地模型）
uv run --locked python scripts/evaluation/run_answer_eval.py --live --case-id 1
```

## 6. 已知限制

- 17 题规模只够做回归，不够做统计结论；扩充时应保持"可接受页"与演示案件一致。
- `must_include` 是词面匹配，同义改写会漏判；这是确定性评分的固有代价，需与 judge/人工结合。
- 拒答判定基于标记词，模型换一种说法时可能漏判。
- 未覆盖多轮追问与跨文档推理的答案质量。
