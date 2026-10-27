# 求职展示版 P2 预检：模型资源不可用，真实对照未开始

- 日期：2026-10-27（Asia/Shanghai）。
- 对应计划：[agent-portfolio-plan-20261027.md](agent-portfolio-plan-20261027.md) 第 4.2、7 节；前置：[P0](portfolio-p0-scope-evidence-20261027.md)、[P1](portfolio-p1-ablation-driver-20261027.md)。
- 结论：**P2 未开始，生成调用 0 次**。原因是本机没有可调用的 LLM 端点与权重，属资源层阻塞；不产生任何质量、成本或时延数值。

## 1. 预检结果（实测）

```sh
uv run --locked python scripts/run_collaboration_ablation.py --mode prepare \
  --output output/portfolio-ablation/dev-20261027        # exit 0，冻结 60 题
uv run --locked python scripts/run_collaboration_ablation.py --mode precheck \
  --output output/portfolio-ablation/dev-20261027        # exit 3
uv run --locked python scripts/run_collaboration_ablation.py --mode run \
  --output output/portfolio-ablation/dev-20261027        # exit 3，未发起批量调用
```

`precheck.json`：

```json
{
  "endpoint": "http://127.0.0.1:8000/v1",
  "model": "Qwen3.6-35B-A3B-OptiQ-4bit",
  "available": false,
  "max_tokens": 900,
  "temperature": 0.2,
  "seed_sent": false,
  "checked_at": "2026-10-27T20:07:07+0800"
}
```

`run/failure.json`：`outcome=model_unavailable`、`batch_calls=0`、`attempts_not_started=true`。门禁行为符合计划第 7 节“预检失败先定位资源/协议问题，停止无效批量调用”。

## 2. 资源核查

| 项 | 实测 | 是否满足 |
|---|---|---|
| 端点监听（8000/1234/8080/11434/8001） | 无进程监听 | 否 |
| `/v1/models` 返回登记模型 ID | 探针在连接阶段失败，`local_llm_available()` 返回 `(False, 'Qwen3.6-35B-A3B-OptiQ-4bit')` | 否 |
| 本地 LLM 权重 | 仓库 `models/` 为空；用户目录无 `models`、无 MLX 权重；仅有 ComfyUI 图像/CLIP 权重 | 否 |
| 推理运行时 | `import mlx_lm` 失败（未安装） | 否 |
| `.env` 模型登记 | 不存在 | 否 |
| 采样约束 | `temperature=0.2`、`max_tokens=900` 可从配置读取 | 是（驱动已登记） |

本机存在的 ComfyUI 图像模型不是登记的 LLM 端点，也不能走产品 `services.call_local_llm` 的出口校验与引用契约，**不能替代**。

## 3. 因此未执行的项目

| 计划项 | 状态 | 原因 |
|---|---|---|
| 9 次预检生成（正常/资料不足/矛盾 × 3 组） | 未执行 | 端点不可用 |
| 开发集 180 次主实验 | 未执行 | 同上 |
| 保留集 24 题 × 3 组 × 3 轮 = 216 次 | 未执行 | 同上；保留集已于同日冻结（[holdout-v1](portfolio-p2-holdout-20261027.md)），只等端点 |
| Judge 打分与 κ 复算 | 未执行 | 无模型；无人工标签 |
| 质量、token、时延、稳定性结论 | 未产生 | 不能用替身或历史报告替代 |

分析口径已另行实现（[配对分析层](portfolio-p2-analysis-20261027.md)：同题配对、精确 McNemar、分类/分案例、案例级重采样），但只在真实账本存在时才有数值。

计划第 7 节要求“所有失败和未知用量均保留”，本文件即该失败记录；对应的批次产物保留在 `output/portfolio-ablation/dev-20261027/`（含冻结提示、隔离库、预检与失败记录，`.gitignore` 已排除）。

## 4. 恢复条件与继续步骤

1. 在本机启动登记的本地 LLM 服务，使 `http://127.0.0.1:8000/v1/models` 返回 `Qwen3.6-35B-A3B-OptiQ-4bit`（或把实际 ID 写入 `LAW_REVIEW_LLM_MODEL` 并同步 `data/model_settings.json`）。
2. 重新执行 `--mode precheck`，把新的 `precheck.json` 作为新的批次身份（不要覆盖本次失败记录）。
3. 通过后按 P1 的冻结批次执行 `--mode run`；预检 9 次 → 开发集 180 次 → 失败归因 → 冻结候选 → 保留集 216 次。
4. 保留集需先按计划第 4.2 节编制 6 个独立案例 × 4 题，冻结来源、预期、评分标准与摘要；在此之前不得把开发集结果当保留集结果。

## 5. 不用来补缺的东西

- 不使用模型替身、固定答案或 `fallback_answer` 的规则文本冒充模型回答。
- 不把 `output/answer_eval/smoke.json`（v1 规则冒烟）或历史模型报告迁移为当前版本指标。
- 不把确定性关键词通过率写成回答质量或法律准确率。
- 不以“调通了流程”代替“模型效果已测量”。
