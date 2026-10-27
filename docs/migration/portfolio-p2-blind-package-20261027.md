# 求职展示版 P2：三组盲审包（隐藏组别）

- 日期：2026-10-27（Asia/Shanghai）。
- 对应计划：[agent-portfolio-plan-20261027.md](agent-portfolio-plan-20261027.md) 第 4.3 节“盲审预先抽取至少 20 道分层题目，对三组答案隐藏组别并随机呈现……三组配对与多案件输入需经 P1 适配验证”。
- 结论：生成器与回归已完成；**本机没有真实账本，只有替身演练包**，因此不构成任何一致性或质量结论。

## 1. 规则

| 项 | 规则 |
|---|---|
| 入选条件 | 同一题在同一轮次上三组都有 `ok` 尝试；否则该题**不进样本**，也不补造答案 |
| 样本量 | 至少 20 题；不足则显式失败 |
| 分层 | 每类先取 2 题（某类总数不足 2 时取全部），其余按 `sha256(数据集摘要 + 题号)` 顺序补齐 |
| 多案例 | 补齐时优先覆盖尚未出现过的案例，避免 6 个案例被一个案子代表 |
| 隐藏 | 每题三份候选统一标为 `X1/X2/X3`，呈现顺序按 `(seed, 题号)` 打乱；组别映射只写进 `key.json` |
| 绑定 | `review.json` 记录数据集摘要、账本摘要与样本摘要；`PACK_MANIFEST.json` 记录四个文件的字节摘要 |
| 替身 | `--collection-mode stub` 必须显式加 `--allow-stub`，且 `notice`/`README.md` 会写明“替身答案，只用于流程演练” |

## 2. 用法

```sh
uv run --locked python scripts/build_ablation_blind_package.py \
  --run output/portfolio-ablation/<批次>/run \
  --dataset benchmarks/answer_eval/holdout-v1/dataset.json \
  --output output/portfolio-review/<批次>-blind \
  --collection-mode real_model --size 20
```

产物：`review.json`（审核人可见）、`key.json`（组织者单独保管）、`PACK_MANIFEST.json`、`README.md`。包目录必须不存在；任一步失败都不会留下“看起来完整”的旧包。

裁判侧完整性检查由 `app.ablation_review.preview_scores()` 提供：把标签映射回组别并复算确定性评分，只用于确认映射正确，不是人工标签。

## 3. 替身演练（不是质量证据）

用 72 行替身账本（24 题 × 3 组，全部 `ok`）生成 `output/portfolio-review/stub-package-20261027/`：

| 项 | 值 |
|---|---|
| 采集方式 | `stub`（`README.md` 与 `review.json` 均带替身标记） |
| 样本 | 20 题，覆盖 6 个案例；可选池 24 题 |
| 抽样规则 | `two-per-category-case-coverage-then-sha256-order-v1` |
| 样本摘要（题号序列，`review`/`key` 一致） | `5e2b61cd1d08149ae1946b44e28c8e160bc49fd4d442dde276fed53e13c606cb` |
| `manifest.review_sha256`（整份评审文件） | `b0135b3cbcbd08eb10213e4e839381cd73b170d218dba9a1b2974409299e6178` |
| `review.json` 字节摘要 | `22b201a20974ad8cddcba4d236882f81d661837094688028a0fc554a429c8449` |
| `key.json` 字节摘要 | `265ab6ba98c1420dc963bd5ede6aa34b73a846e90c2e36ddbc892a44dc7ac9da` |
| `README.md` 字节摘要 | `ac7df2e9a432defa6315e71324c903f8e10326ba2a0c707c46b55cad540e3640` |
| `PACK_MANIFEST.json` 字节摘要 | `91900bea4a25069610212ddcf109d08b3dd946265c76212a070cdc54c8306d02` |

演练证明的是“流程可跑、组别确实被隐藏、映射与摘要可复算”，**不代表**任何回答质量或 Judge/人工一致性。

## 4. 验证

`uv run --locked python -m pytest tests/test_ablation_blind_package.py -q` → **7 passed**：

1. 分层抽样确定且覆盖全部案例；
2. 缺一组成功的题目不进样本，样本不足时显式失败；
3. `review.json` 只有 X1–X3、无组别字段，`key.json` 可反查；
4. 标签顺序按种子打乱（不同题目的顺序不同），同种子可复现；
5. 替身需显式放行且保留标记；
6. 裁判侧映射与确定性评分一致；
7. CLI 一次性写入、拒绝覆盖、拒绝未放行的替身包。

## 5. 尚未执行

| 项 | 原因 |
|---|---|
| 真实账本的盲审包 | 模型端点不可用，没有真实答案 |
| 独立审核人与 ≥20 个有效标签 | 未落实；缺人工时一致性结果保持未完成 |
| Judge 与人工 κ | 同上 |

真实批次完成后，按下述顺序闭环：生成包 → 审核人按 `README.md` 打分 → 组织者锁定标签后再开 `key.json` → 用 `human_agreement()` 计算 κ（至少 20 个有效匹配标签，缺项留空）。
