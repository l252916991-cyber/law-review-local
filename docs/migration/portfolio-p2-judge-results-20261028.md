# P2 Judge 评分：开发集三组（Qwen3.8-27B，60 题 × 3 组）

- 日期：2026-10-28（Asia/Shanghai）。
- 目的：用模型 Judge 作为**观察辅助**，与确定性评分并列报告，检查负结果是否只在规则口径下成立。
- 边界：Judge 与生成器是同一个模型，存在自偏好风险；没有独立人工标签，因此**不计算 κ，也不把 Judge 当作质量真值**。

## 1. 设置

| 项 | 值 |
|---|---|
| Judge 提示 | `answer-judge-v2-frozen-sources`（题目、回答、引用均为不可信数据；独立来源取冻结集全文） |
| 模型 / 端点 | `Qwen3.8-27B-oQ4e-mtp` / `http://127.0.0.1:8000/v1` |
| 采样 | `temperature=0`、`enable_thinking=false` |
| 输入 | `output/portfolio-judge/dev-{A,B,C}-answers.json`（由 `scripts/export_ablation_answers.py` 从账本导出，引用按驱动同一规则映射） |
| 超时 / 输出上限 | 180–240 s / 500 token（B 组重跑用 900 token，见第 2 节） |
| 报告 | `output/portfolio-judge/dev-{A,B,C}-judge*.json` |

## 2. 过程中修掉的两个传输问题

| 问题 | 现象 | 处理 |
|---|---|---|
| 推理剥离误伤 JSON | A 组首轮 60 题全部判失败。`services.strip_reasoning` 会把“前 240 字符像计划文本”的答案清空，而 Judge 的 rationale 里出现“问题要求…”被误判，正文被清成空串 | 新增 `answer_judge_client.strip_think_only`：只剥离 `<think>` 块，保留 JSON；补一项回归 |
| 输出上限截断 | B 组首轮在 `contradiction-06` 处 `invalid_judge_schema`，原始输出正好 500 token 被截断，JSON 未闭合；脚本按设计停止后续调用，只完成 35 题 | Judge 输出上限改为可配置（`--judge-max-tokens`），B 组用 900 token 重跑并完成 60 题；首轮失败报告保留为负记录 |

## 3. 结果

| 组 | 已判 | Judge 通过 | 确定性 v2 通过 | 一致 | 仅 Judge 过 | 仅 v2 过 | Judge 耗时中位 | 输出 token 中位 |
|---|---|---|---|---|---|---|---|---|
| A 基础 RAG | 60 | **57** | 52 | 51/60 | 7 | 2 | 28.3 s | 136 |
| B 结构化交接 | 60 | **57** | 46 | 43/60 | 14 | 3 | 12.5 s | 144 |
| C 交叉审校 | 60 | **57** | 52 | 49/60 | 8 | 3 | 55.2 s | 150 |

- 三组 Judge 通过数**完全相同（57/60）**，与确定性评分的方向不一致：规则下 B 明显更差（46），Judge 看不出差别。
- Judge 报告里另有 `summary.passed` 一列（A 20、B 13、C 19），那是 `evaluate_answers` 的 **v1 规则**结果，不能与 v2 混用；本文件统一引用 v2 数字。

## 4. 关键分歧（逐题核对过）

- **空答案被判通过**：`injection-01` 三组都是 `not_attempted`（无检索上下文、未生成），Judge 却判 `coverage_ok/citation_ok/refusal_ok` 全真——它把“没有回答”当成“正确拒答”。这是 Judge 的明确缺陷，已记录。
- **无关题**：`irrelevant-01/03/04` 在 A/B/C 多数组被 Judge 判过，规则判不过；Judge 对“答非所问但引用齐全”缺乏惩罚。
- **反向个例**：`contradiction-06`（A）、`insufficient-08`（A/C）、`injection-02`（B/C）规则判过而 Judge 判不过，多为要求并列呈现双方陈述时 Judge 认为覆盖不足。

## 5. 这批 Judge 结果能说明什么

- 能说明：同一模型作 Judge 时，对“带引用但答非所问/空答案”缺乏辨别力，三组都被判到 57/60，无法复现确定性评分显示的差异；Judge 与规则的一致率 43–51/60（72%–85%）。
- 不能说明：哪一方“对”。规则要求精确要点与必引页，Judge 看语义但宽松且自偏好；没有人工标签时，两者都只是观察。
- 因此**主口径仍用确定性 v2**（可复算、无自偏好），Judge 结果只作敏感性检查；负结果的表述保持不变：协作机制未见收益、成本更高。

## 6. 未完成与下一步

| 项 | 状态 |
|---|---|
| 独立人工评分与 κ | 缺评审人，保持未完成；不代填标签 |
| Judge 与人工一致性 | 无人工标签，`human_agreement.status=missing_human_labels` |
| 保留集 Judge | 未跑（24 题 × 3 组 ≈ 72 次，约 40 分钟）；需要时按同一命令执行 |

复算：

```sh
uv run --locked python scripts/export_ablation_answers.py --batch <批次> --group C --output <answers.json>
uv run --locked python scripts/run_answer_judge.py --dataset benchmarks/answer_eval/v2/dataset.json \
  --answers <answers.json> --judge --endpoint http://127.0.0.1:8000/v1 \
  --judge-model Qwen3.8-27B-oQ4e-mtp --judge-timeout 240 --judge-max-tokens 900 --output <report.json>
```
