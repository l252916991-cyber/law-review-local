# LawBench 1,000 题修复验证

## 目的和边界

修复评测流程，提高模型对任务说明及答案格式的遵循程度；不训练模型，不向模型传入参考答案，不放宽评分标准追分。
本次不是原生 DAG / LangGraph / RAG 的集成质量评测，也不是官方 LawBench 排行榜分数。

## 固定设计

- 模型：Qwythos-9B-v2-**8bit**-mlx，本地 oMLX；temperature=0，thinking=false，max_tokens=900，timeout=180 秒。
  （2026-09-07 起由 4bit 改为 8bit：oMLX 当前加载与历史最优配置均为 8bit；换用不影响评测流程，仅模型权重量化不同。）
- 数据：固定版本 LawBench zero-shot 的 20 类任务；每类随机 50 题，seed=42；总计 1,000 题。
- 正式目录：`output/benchmark_fixed_1000_20260907_8bit`（8bit 配置的正式运行；2026-09-05 的 `output/benchmark_fixed_1000_20260905` 为 4bit 历史记录，不可重开）。
- 旧回答：`output/benchmark_full_10k/run_20260903_145445/detailed_results.jsonl`，只取相同 question_id，先核对 question/reference。
- 新旧回答使用当前冻结评分器（`lawbench-local-v3`）重评；旧保存分数只作历史参考。
- 全题混合均分不排除空回答、解析失败或技术失败。无期/死刑参考答案单独标记为 reference_invalid，保留零分，不冒充模型错误。
- 模型调用前冻结抽样清单、提示词哈希、源码哈希、模型配置和旧数据哈希；运行中不修改提示词和评分器。

## 修复范围

1. 保留并发送每题 instruction；缺失则在模型调用前报错。
2. 分类解析不再只查找当前参考答案中的标签；额外标签会降低分数，否定性叙述不会因提及答案而获满分。
3. 金额题提取明确的最终总额，不匹配正文中的任意数字；支持千分位及中文数字。
4. 法条编号支持中文数字，不把日期、金额当法条。
5. 刑期区分最终预测、日期和范围；解析失败不再标为拒答；畸形数字不导致评分崩溃。
6. 实体抽取的额外实体类型参与精确率计算。
7. 保存 parse_failed、reference_invalid、finish_reason、usage、重试次数和错误；耗时包含重试与退避。
8. 续跑必须指定原目录并通过 manifest 一致性检查；已保存回答不重跑，文件锁防止并发重复执行。
9. 非 LawBench 数据集此前缺少完整评分路径，本审计运行器现明确拒绝，避免生成假的全零结果；其他专项运行器不变。

## 自动化检查

```sh
python3 -m unittest discover -s tests -p 'test_benchmark*.py' -v
python3 -m unittest discover -s tests -v
```

2026-09-05 执行结果：

- 评测专项：30 / 30 通过，包括原有 10 项和新增 20 项；含离线验收器对完整输出和被篡改汇总的验证。
- 全项目：143 项，130 通过，12 失败，1 错误；没有把它描述成“全项目通过”。
- 全项目失败集中在 API 契约、测试数据隔离、检索断言，与此前审查记录的失败数量及范围一致。本次未扩展修改这些业务功能。
- 新评分器对旧 10,000 条回答离线预检：无异常，分数均处于 [0,1]。
- 日志：`output/benchmark_fix_validation_20260905/benchmark_unit_tests.log`、`full_unit_tests_final.log`。

2026-09-07 8bit 正式运行（`output/benchmark_fixed_1000_20260907_8bit`）：

- 1,000/1,000 完成，`verify_benchmark_run.py` 全项通过（抽样清单、哈希、离线重评一致）。
- 混合均分 **57.35%**（scorer `lawbench-local-v3`、prompt `lawbench-task-guided-v3`）；解析失败 2、截断 3、技术失败 0；均延迟 3.27s、p50 2.23s、p95 7.97s，总时长 0.91 小时。
- 对照旧 10k 回答同题重评 16.53%：+40.8pp（622 升 / 127 降）；差值主要反映旧运行的提示与解析质量，不作为本次增强证据。
- 历史最优 59.31%（hybrid 白名单 + statutory RAG 增强配置）与本次直接运行相差约 2pp；本次为未启用检索增强的固定流程复测。

## 正式运行和恢复

```sh
LAW_REVIEW_LLM_MODEL=Qwythos-9B-v2-8bit-mlx \
LAW_REVIEW_LLM_URL=http://127.0.0.1:8000/v1 \
python3 -u unified_benchmark_runner.py \
  --dataset lawbench --tasks all --limit-per-task 50 --sample-seed 42 \
  --timeout 180 --max-tokens 900 --retry 2 \
  --run-dir output/benchmark_fixed_1000_20260907_8bit \
  --baseline-results output/benchmark_full_10k/run_20260903_145445/detailed_results.jsonl
```

已有结果的目录不能重新开始；中断后使用完全相同的命令并加 `--resume`。只重试技术失败的网络请求，不重试低分答案。
正常运行时不要再开第二个模型评测进程。完成后离线验证：

```sh
python3 verify_benchmark_run.py output/benchmark_fixed_1000_20260907_8bit
```

验收：1,000 个唯一 question_id、20 类各 50 题、checkpoint 与 JSONL 逐条一致、提示词和源码哈希一致、离线重评与保存分数一致、汇总和配对差值可复算。
接口成功率、解析失败率、截断数量、均分及分任务变化全部报告；不预先承诺分数一定提升。

## 输出

- `manifest.json`：冻结配置及抽样清单。
- `source_snapshot/`：运行时使用的评分/提示词/加载代码。
- `checkpoints/`、`detailed_results.jsonl`：原始回答及调用记录。
- `summary.json`、`REPORT.md`：每 25 题更新，完整结果须检查 status=completed 或 completed_with_errors。
- `paired_comparison.json`：旧原分、旧回答重评分、新分及同题差值。
- `verification.json`：完成后的离线一致性验收。

评分限制：校对、阅读理解、实体和触发词评分使用本地近似；格式保守解析可能扣除长篇叙述答案。不能将混合均分称为准确率或人工法律审查结论。
上游刑期数据问题说明见 [固定版本 ljp_imprison.py](https://github.com/open-compass/LawBench/blob/e30981bb3ff54c41571f222e0b23e92d27375388/evaluation/evaluation_functions/ljp_imprison.py)。
