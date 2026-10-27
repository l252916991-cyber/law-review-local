# 求职展示版 P2 前置：六案例保留集冻结（holdout-v1）

- 日期：2026-10-27（Asia/Shanghai）。
- 对应计划：[agent-portfolio-plan-20261027.md](agent-portfolio-plan-20261027.md) 第 4.2 节“保留集：6 个独立案例、每案 4 题，共 24 题”；前置：[P0](portfolio-p0-scope-evidence-20261027.md)、[P1](portfolio-p1-ablation-driver-20261027.md)、[P2 预检记录](portfolio-p2-precheck-blocked-20261027.md)。
- 结论：**保留集已编制并冻结，尚未运行**。真实模型对照仍受模型端点阻塞；本文件只补“保留集缺失”这一项，不产生任何质量数值。

## 1. 冻结身份

| 项 | 值 |
|---|---|
| 目录 | `benchmarks/answer_eval/holdout-v1/` |
| fixture | `cases.json`，SHA-256 `86c8968fff65ad435009f92e58592d2922ea99835b2837e4b67c6319e0b0443e` |
| 数据集 | `dataset.json`，SHA-256 `10739d034242cc0474fe1c66fc98e2c921c2b70c7b026fc1fa6128910596749b` |
| 来源摘要 | `66265fdbadf71e847f248525bf69ab273b9dc165d064fb960c58248ca33acfff` |
| 规模 | 6 案例 / 24 页 / 24 题 |
| 类别分布 | 正常 12、候选矛盾 6、资料不足 3、无关 2、法条缺失 1 |
| 标注来源 | `assistant_generated_from_authored_synthetic_fixture`（编码助手编制，未经律师或独立人工标注） |

六个案例：建设工程价款结算（A）、劳动争议解除与工资（B）、买卖合同质量异议（C）、民间借贷本金与还款（D）、交通事故责任与费用（E）、股权转让价款支付（F）。每案 2 份文书、每份 1–2 页，当事人、金额、文书名与问题均与开发集不同，页面不共享。

## 2. 与开发集的隔离方式

| 手段 | 实现 |
|---|---|
| 数据隔离 | 保留集不引用 `app.db.seed_demo`；隔离库由 `dataset.json` 的 `cases` 直接播种（`init_db(seed=False)`） |
| 文本一致 | 播种的页正文就是冻结正文，`verify_frozen_pages` 逐页比对隔离库与 `sources`，不一致直接失败 |
| 引用隔离 | `validate_cross_case` 要求每题的 `acceptable_pages` 都属于本题 `case_id`，跨案例引用在冻结与测试阶段都被拒绝 |
| 运行隔离 | `prepare` 的 manifest 记录 `case_id`、`context_pages`、提示摘要；批次目录新建，不覆盖旧批次 |

## 3. 已经验证的内容

```sh
uv run --locked python scripts/build_holdout_eval.py            # 核对冻结产物（幂等）
uv run --locked python scripts/run_collaboration_ablation.py --mode prepare \
  --dataset benchmarks/answer_eval/holdout-v1/dataset.json --output output/portfolio-ablation/holdout-v1
```

| 检查 | 结果 |
|---|---|
| 冻结复算 | `build(load_cases())` 与已提交 `dataset.json` 逐字段相同；manifest 摘要与实际文件一致 |
| 类别结构 | 每案 2 道正常题 + 1 道张力题（矛盾/不足/无关/缺法条），总数与计划一致 |
| 跨案例引用 | 篡改一题的 `acceptable_pages` 指向其他案例后，`validate_cross_case` 抛错 |
| 覆盖拒绝 | 修改 fixture 后重跑 `--write` 报“冻结产物不同”，旧数据集字节不变 |
| 隔离库播种 | `prepare` 成功播种 6 个案例 24 页，manifest 的 `case.cases == [1..6]`、`pages == 24` |
| 上下文归属 | 逐题检查检索上下文，全部落在本题所属案例（0 处跨案泄漏） |
| 检索状态 | 23 题 `frozen`，1 题 `no_context`（`E-insufficient-01`，产品路径在生成前拒答，记为 `not_attempted`） |
| 计分绑定 | 桩模型回答按**本题所属案例**的要点与可接受页计分（`D-normal-01` 覆盖度 1.0） |

提示长度（24 题冻结值）：A 355–713 字符、B 355–4,950、C 355–6,751；仍低于 48,000 字符硬上限。

## 3.1 冻结前的可复现性修正

首次 `prepare` 两次结果不一致（`contradiction-10` 提示摘要变化），根因是疏漏专家的集合迭代顺序；已修复并把 native 基线重登记为 `specialist-review-v3`，详见[对照批次可复现性修正](portfolio-determinism-20261027.md)。修复后，开发集与保留集各自两次独立进程 `prepare` 的提示摘要全部一致。

本文件第 1 节的摘要与第 3 节表格基于修复后的冻结文件；修复前生成的批次只作缺陷证据，不再用于对照。

## 4. 尚未执行

| 项 | 原因 |
|---|---|
| 保留集 24 题 × 3 组 × 3 轮 = 216 次生成 | 模型端点不可用（见 [P2 预检](portfolio-p2-precheck-blocked-20261027.md)） |
| 轮换顺序的实际执行与波动统计 | 同上 |
| Judge 与人工 κ | 无模型、无人工标签 |

运行条件恢复后，按计划第 4.2 节：先用开发集定位问题并冻结候选，再在新批次目录上跑保留集三组各三轮；本文件冻结的 `dataset.json` 与 `cases.json` 不得因结果而修改，需要改动就建立 `holdout-v2`。

## 5. 复算方式

```sh
uv run --locked python scripts/build_holdout_eval.py              # 应输出与 manifest 相同的摘要
uv run --locked python -m pytest tests/test_holdout_eval.py -q    # 6 项：冻结、结构、跨案引用、覆盖拒绝、播种、计分
```
