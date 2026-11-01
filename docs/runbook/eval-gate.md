# 答案质量门禁

改动提示词、检索、路由、可答性判断或换模型之前，先跑门禁：在冻结的 v2 评测集（演示案件 60 题）上，本次结果不能比已登记的基线差。实现见 `app/eval_gate.py`，命令行入口为 `scripts/eval_gate.py`。

## 两种模式

| 模式 | 用途 | 规则 |
| --- | --- | --- |
| `rules` | CI 每次推送都跑；不调用模型，走真实的检索、路由、可答性和引用卡片路径 | 结果确定：任何一道原本通过的题失败即阻断；通过题数、正确拒答数、引用到全部必需页的题数都不许下降；禁止断言不许增加。推理配置与基线相同而结果不同时，按“不确定性”阻断 |
| `model` | 本机接真实模型时跑 | 采样有噪声：通过题数最多允许比基线少 2 题、正确拒答最多少 1 题；禁止断言不许增加；基线 p95 时延 ≥ 200 ms 时，本次 p95 不得超过基线的 1.25 倍 |

阈值写在 `THRESHOLDS`，属于预先登记的口径，改动它需要单独提交并说明理由。模式或评测集与基线不一致时直接报配置错误（退出码 2），不做比较。

## 命令

```sh
# 与已登记基线比较；通过退出 0，被拦下退出 1
uv run --locked python scripts/eval_gate.py --report output/eval-gate.json

# 本机真实模型（端点必须是回环地址）
uv run --locked python scripts/eval_gate.py --mode model \
  --endpoint http://127.0.0.1:8000/v1 --model ACTUAL_MODEL_ID

# 有意改动并确认结果后登记新基线；原因与上一版摘要写进 history
uv run --locked python scripts/eval_gate.py --update-baseline --reason "说明改了什么、为什么可以接受"
```

模型模式还没有基线：第一次在本机跑通后，用 `--mode model --update-baseline --reason ...` 登记到 `benchmarks/eval_gate/baseline-model.json`。如果没有任何一题实际调用到模型（端点不通、模型名错误），门禁报配置错误，不会把规则回答当成模型结果。单题模型调用失败记为该题失败，不跳过。

## 报告里有什么

- **推理版本**：`inference_id` 由提示词版本、系统提示摘要、可答性与上下文策略版本、检索与回答相关模块的文件摘要、数据库后端、模型设置共同决定。同一个 `inference_id` 一定对应同一套推理配置。
- **指标**：通过题数、引用到全部必需页的题数、正确拒答数、禁止断言数、p50/p95 时延。评分规则是 `app/answer_eval.py` 的 `score_answer_v2`，不调用模型。
- **逐题变化**：原本通过、现在失败的题号（阻断原因），以及原本失败、现在通过的题号（确认后可登记新基线）。

CI 把 Markdown 摘要写进 GitHub Actions 的 Job Summary，完整 JSON 随测试证据上传。

## 边界

v2 评测集由编码助手在单一虚构案件上构造，未经律师标注（见[答案级评测手册](answer-evaluation.md)）。门禁回答的是“这次改动有没有让已有的题变差”，不是法律准确率；规则模式通过不代表模型回答正确。
