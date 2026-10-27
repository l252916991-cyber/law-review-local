# 求职展示版 P2：驱动链路端到端演练（替身服务，非模型结果）

- 日期：2026-10-27（Asia/Shanghai）。
- 目的：在没有真实模型时，用仓库自带的 OpenAI 兼容替身服务把 `prepare → precheck → run → analyse` 全链路跑通，验证账本、计量、轮换、预算门禁与配对分析确实工作。
- 边界：**这不是模型效果证据**。替身返回固定答案、固定 usage；任何通过率、token 或时延数字都只证明采集与统计口径可用。

## 1. 设置

| 项 | 值 |
|---|---|
| 替身服务 | `scripts/stub_model.py` 的 `StubModel`（`/v1/models`、`/v1/chat/completions`、`/v1/embeddings`、`/v1/rerank`） |
| 模型 ID | `stub-chat`（仅用于演练，不在任何真实配置中登记） |
| 数据 | 保留集 3 题：`A-normal-01`、`B-normal-02`、`D-insufficient-01`（覆盖 3 个案例） |
| 端点 | 由 `--endpoint` 显式传入；本轮新增 `live_generator(endpoint=…)` 把该端点交给 `call_local_llm` 的 `base_url_override`，不再依赖进程默认地址 |

```sh
# 替身服务在演练脚本内以临时端口启动；以下为等价命令
uv run --locked python scripts/run_collaboration_ablation.py --mode prepare \
  --dataset benchmarks/answer_eval/holdout-v1/dataset.json \
  --output output/portfolio-drill/batch \
  --item-ids A-normal-01,B-normal-02,D-insufficient-01
uv run --locked python scripts/run_collaboration_ablation.py --mode run \
  --dataset benchmarks/answer_eval/holdout-v1/dataset.json \
  --output output/portfolio-drill/batch --endpoint <stub-url>/v1 --model stub-chat
uv run --locked python scripts/analyse_collaboration_ablation.py \
  --run output/portfolio-drill/batch/run --baseline A --compare C \
  --output output/portfolio-drill/analysis-A-vs-C.json
```

## 2. 演练暴露并修掉的问题

| 问题 | 现象 | 修复 |
|---|---|---|
| 生成器忽略显式端点 | `--endpoint` 只用于预检，实际生成仍打进程默认地址，非默认端点的批次会静默打到错误目标 | `live_generator(model, timeout, endpoint)` 传入 `base_url_override`；新增回归 `test_live_generator_targets_the_probed_endpoint_explicitly` |
| 未知题目 id 被静默丢弃 | `--item-ids` 里写错一个 id（`C-insufficient-01`）时批次只剩 2 题，命令仍返回 0 | `prepare_batch` 在创建目录前校验 id，未知即失败且不留半成品目录；新增回归 |
| 分析 CLI 的多余必填 | `--run` 标为必填，与“可直接给 `--attempts`”的用法冲突 | 二者至少其一，缺失时报参数错误；新增回归 |

## 3. 演练结果

| 项 | 值 |
|---|---|
| 计划 / 实际尝试 | 9 / **9**（3 题 × 3 组），全部 `ok` |
| 用量采集 | 每次 `input_tokens=96`、`output_tokens=32`（替身固定值），`unknown_usage=0` |
| 轮换 | `A-normal-01` 顺序 A→B→C，`B-normal-02` 为 B→C→A，`D-insufficient-01` 为 C→A→B，与 `rotation(index, repeat)` 一致 |
| 附录 | C 组三次都通过产品校验并追加分歧附录（`appendix_applied=3`） |
| 配对分析 | 3 对；A 与 C 通过率均为 0（替身答案不含题目要点，属预期）；案例级重采样 3 个案例，区间 `[0, 0]` |
| 产物摘要 | 批次 `manifest.json` `feafc5d3d037a97117f9e00e92f08bcb927019518f38ef7cfedff30f1e508a5c`；`attempts.jsonl` `7226952ff272311e9f7b267bca455dca50ea17e89e6929d96826d7213510dba3`；分析报告 `f4e05c21a632498c7daee52b3a25d81a15e9edf8a3d97c171de0c914ba13d412` |

替身答案不含必需要点，所以 A/C 通过率同为 0：这恰好说明评分按题目要点判定，而不是“有引用就通过”。

## 4. 未验证的部分

| 项 | 原因 |
|---|---|
| 真实模型的输出质量、token 与延迟 | 端点不可用；替身 usage 是常量，不能代表真实计量 |
| 405 次批量与轮换全量 | 演练只跑 9 次；规模压测留给真实批次 |
| 盲审包 | 需要至少 20 题三组成功尝试，演练只有 3 题；盲审路径由 24 题合成账本的回归覆盖 |
| 预检失败路径 | 已在 [P2 预检记录](portfolio-p2-precheck-blocked-20261027.md) 记录（真实端点 `available=false`） |

## 5. 复现

```sh
uv run --locked python -m pytest tests/test_collaboration_ablation.py tests/test_holdout_eval.py \
  tests/test_ablation_analysis.py -q
```

新增回归 3 项（端点传递、未知 id 拒绝、分析 CLI 参数）已包含在内；演练产物位于 `output/portfolio-drill/`（`.gitignore` 已排除）。
