# 对照批次可复现性修正：专家发现顺序与 `specialist-review-v3`

- 日期：2026-10-27（Asia/Shanghai）。
- 起因：编制 [六案例保留集](portfolio-p2-holdout-20261027.md) 时，对同一份开发集连续两次执行 `--mode prepare`，发现 60 题里 `contradiction-10` 的提示摘要不同。
- 结论：定位到规则专家的集合迭代顺序缺陷，已修复并把 native 对照基线重登记为 `specialist-review-v3`；旧 v2 结果保留、不覆盖。

## 1. 缺陷

`GapDetectionAgent._detect_single_source_evidence` 用集合遍历关键证据类型：

```python
critical_types = {"询问笔录", "银行流水", "合同"}
for doc_type in critical_types:  # 顺序随进程哈希种子变化
```

当一份检索上下文同时出现多类“仅有一份”的关键证据时，生成的多条 `单一来源` 疏漏的先后顺序随进程变化。后果：

1. 同一冻结输入在不同进程得到不同的 `specialists` / `collaboration`，专家发现与 C 组分歧附录的顺序不稳定；
2. 提示摘要 `sha256(system+user)` 因此变化，`prepare` 的“同一批次即同一提示”前提被破坏；
3. 跨机器或换进程复跑时，账本里的 `prompt_sha256` 对不上，复算会误判为“批次被改过”。

## 2. 修复

只把遍历对象改为固定顺序的元组，检测条件、提示模板、检索、回答契约与引用校验都不动：

```python
# 顺序固定为元组：集合的迭代顺序随进程哈希种子变化，
# 会让同一批冻结输入的专家发现与提示摘要不稳定。
critical_types = ("询问笔录", "银行流水", "合同")
```

同时排查了其余候选：`present_types` 只做成员判断；原生 DAG 的 `pending` 集合在调度前 `sorted()`；`objective_types` 只做成员判断。未发现其他影响输出顺序的集合迭代。

## 3. 身份重登记

`app/model_comparison.py` 的摘要门禁覆盖 `agents.py`，因此按计划第 4.1 节“若修改其受检算法，显式登记新身份，旧结果不被覆盖”处理：

| 项 | 值 |
|---|---|
| 版本 | `specialist-review-v2` → **`specialist-review-v3`** |
| `agents.py` 摘要 | `6ae41ec6…e55a` → **`4dd5451bf26fd2df89efe077e0471e687fadee9200c27b89d875d7298b77aa2c`** |
| 其余六个算法文件 | 摘要不变 |
| 评审说明 | 写入 `NATIVE_BASELINE_REVIEW`：只固定顺序；提示模板、检索、回答契约未改动；旧 v2 结果保留 |
| 测试 | `tests/test_model_comparison.py` 的版本断言同步为 v3 |

## 4. 复验

| 检查 | 结果 |
|---|---|
| 开发集两次独立进程 `prepare` | 60/60 `prompt_sha256` 相同；两份 `manifest.json` 逐字节相同，SHA-256 `5b45ee0c611c6617df32116b4498aa503916bf2b02578ec9347963c26f7344e6` |
| 保留集两次独立进程 `prepare` | 24/24 相同；manifest SHA-256 `e42d43c5226383dac4cd957f727187464bd541cc712eb181dc77f8247b3490aa` |
| 新增排序回归 | `tests/test_agents.py::GapOrderingTest` 2 项：固定类型顺序、上下文逆序不改变单一来源疏漏 |
| 受影响回归 | `tests/test_agents.py`、`test_model_comparison.py`、`test_review_collaboration.py`、`test_collaboration_ablation.py`、`test_holdout_eval.py`、`test_answer_eval*.py`、`test_collaboration_acceptance.py`、`test_04_langgraph_runtime.py`：**124 passed / 7 skipped**（skip 为未设 `LAW_REVIEW_FORGE_PYTHON`）；补设框架环境后 `test_model_comparison.py` **14 passed**，覆盖真实侧车与 native 对照 |
| 静态检查 | `ruff`、`mypy`（56 文件）通过 |

## 5. 作废与保留

| 产物 | 处置 |
|---|---|
| 修复前的开发集批次 `output/portfolio-ablation/dev-20261027/`（manifest `0573d7…`） | **不再用于对照**，保留为缺陷证据；其 `contradiction-10` 摘要与修复后不一致 |
| 修复后的批次 `dev-a` / `dev-b`（`5b45ee…`） | 互为跨进程复算证据；正式批次仍会在模型预检通过后以新目录重建 |
| `specialist-review-v2` 的历史模型报告 | 保留，不迁移、不改写成 v3 结果 |

## 6. 影响边界

本次修复改变的是“同一输入下专家发现的排列顺序”，因此：

- 不改变任何检索结果、提示模板、回答契约或引用校验逻辑；
- 不改变确定性评分的判定规则，但可能改变 C 组附录中条目的先后；
- 旧 v2 数值若与 v3 比较，必须声明身份不同，不能当成同一基线的波动。
