# 求职展示版 P2：三组账本的配对分析层

- 日期：2026-10-27（Asia/Shanghai）。
- 对应计划：[agent-portfolio-plan-20261027.md](agent-portfolio-plan-20261027.md) 第 4.3 节“以同题配对差异、每类结果、样本数及各轮波动为主要报告”“按案件聚合/重采样并说明仅 6 个保留案例的限制”。
- 结论：分析层已实现并有回归；**本机没有真实模型账本，因此尚无任何对照数值**。

## 1. 它做什么

| 输出 | 口径 |
|---|---|
| 同题配对 | 按 `(题目, 轮次)` 配对 baseline 与 compare；只出现在一侧的记录列进 `unpaired`，不静默丢弃 |
| 分母 | 未尝试、传输失败、超时、契约失败、规则拒答一律按“未通过”计入配对分母 |
| 配对差异 | `delta_pass_rate`、`changed`，以及不一致对的精确 McNemar（`baseline_only` / `compare_only`，无 scipy 依赖） |
| 分类结果 | `by_category`：每类的配对数、两组通过数、差值、McNemar |
| 按案例 | `by_case`：每案例的配对结果，保留“只有 6 个案例”的事实 |
| 案例级区间 | 先按案例求均值，再对该均值做有种子重采样，报告 2.5%–97.5%；结论注明不能当总体估计或 SLA |
| 轮次波动 | `repeat_variation`：每个分组有多少题在不同轮次间通过/不通过翻转 |
| 成本 | 输入/输出 token 与耗时的配对中位差；端点未报用量时计入 `unknown_pairs`，不写 0 |

不做的三件事：不重新评分（只读账本里的 `passed` 与 `score`）、不给缺失分组补造记录、不把附录文本算进模型通过率（通过判定来自驱动写入的原始回答评分）。

## 2. 用法

```sh
uv run --locked python scripts/analyse_collaboration_ablation.py \
  --run output/portfolio-ablation/<批次>/run \
  --baseline A --compare C \
  --iterations 2000 --seed 20261027 \
  --output output/portfolio-ablation/<批次>/analysis-A-vs-C.json
```

- `--run` 读取其中的 `attempts.jsonl`（或用 `--attempts` 直接指定）。
- 报告写入 `--output`，使用排他创建：已存在即拒绝，不覆盖旧报告。
- 分组未注册、`--baseline` 与 `--compare` 相同、账本行缺字段、账本为空都会显式失败。

## 3. 报告字段

```json
{
  "schema_version": 1,
  "groups": {"baseline": "A", "compare": "C"},
  "paired": 60,
  "unpaired": [],
  "summary": {"pass_rate_baseline": null, "pass_rate_compare": null, "delta_pass_rate": null,
               "changed": null, "mcnemar": {"baseline_only": 0, "compare_only": 0, "p_value": null}},
  "by_category": {}, "by_case": {},
  "repeat_variation": {"A": {}, "C": {}},
  "case_bootstrap": {"cases": 0, "delta": null, "interval": null, "iterations": 2000, "seed": 20261027},
  "cost": {"input_tokens": {}, "output_tokens": {}, "elapsed_seconds": {}, "unknown_usage_pairs": 0},
  "notes": []
}
```

上表是结构示例，数值位置留给真实批次；当前没有真实账本，任何数字都不能预先填写。

## 4. 验证

`uv run --locked python -m pytest tests/test_ablation_analysis.py -q` → **10 passed**：配对与未配对、失败计入分母、McNemar 精确值、分类/分案例聚合、轮次翻转、案例重采样可复现且有界、缺少 `case_id` 时拒绝重采样、成本区分已知与未知用量、账本校验、CLI 一次性写入与参数拒绝。

## 5. 与真实批次的关系

驱动 `scripts/run_collaboration_ablation.py` 在模型端点可用前不产生账本；预检失败时只写 `run/failure.json`。因此本文件的贡献是“方法已就绪且被回归固定”，不是“已有结论”。真实批次完成后，分析报告与账本一起构成计划第 6 节要求的“对照报告”。

## 6. 已知限制

- 六案例保留集的案例级区间只能描述这一批案例的波动；同案多题不构成独立案件。
- 精确 McNemar 只看不一致对，样本很小，`p_value` 不应被当作决策门槛。
- 确定性评分不含语义判断；与 Judge/人工结论必须分列，不能合并成一个总分。
