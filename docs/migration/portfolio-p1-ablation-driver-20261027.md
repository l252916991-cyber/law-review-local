# 求职展示版 P1：A/B/C 三组隔离对照驱动与替身回归

- 日期：2026-10-27（Asia/Shanghai）。
- 对应计划：[agent-portfolio-plan-20261027.md](agent-portfolio-plan-20261027.md) 第 3、4 节；前置：[P0 范围与证据冻结](portfolio-p0-scope-evidence-20261027.md)。
- 实施起点：`3c1a197`。本批次只增加隔离实验适配层与回归，未改动 `services.py`、`agents.py`、`review_collaboration.py` 等受摘要门禁保护的既有算法文件，native 对照基线仍为 `specialist-review-v2`。

## 1. 本批交付

| 文件 | 行数 | SHA-256 |
|---|---|---|
| `app/collaboration_ablation.py` | 598 | `25bed206aebe5128d1fd2818cebb93bade6c24aef799e3ea8215f4fb6cc5d313` |
| `scripts/run_collaboration_ablation.py` | 144 | `2bd6e10b91fa165cf9f1d72067894aff64a554c0979c5c25b48683e81fb55570` |
| `tests/test_collaboration_ablation.py` | 253 | `bbceb4c88b9dd53fd82b1af494ec2794dff7a9b68f63836ad669ccbf29c27c9e` |

`prepare → precheck → run` 三段式，任何一段都不覆盖旧产物：

```sh
uv run --locked python scripts/run_collaboration_ablation.py --mode prepare \
  --output output/portfolio-ablation/dev-20261027
uv run --locked python scripts/run_collaboration_ablation.py --mode precheck \
  --output output/portfolio-ablation/dev-20261027
uv run --locked python scripts/run_collaboration_ablation.py --mode run \
  --output output/portfolio-ablation/dev-20261027
```

- `prepare`：新建隔离合成库 → 校验库内 14 页与 v2 冻结来源逐字一致 → 每题冻结一次检索上下文 → 运行四个确定性规则专家 → 组装并冻结三组提示 → 写 `manifest.json` 与逐题记录。**不调用模型。**
- `precheck`：探测端点并在 `/models` 中核对模型 ID，记录 `max_tokens`、`temperature`、`seed_sent=false`。
- `run`：只读已冻结批次，`load_prepared` 逐题重算提示摘要并要求与冻结值相等；端点不可用或 `max_tokens` 与登记值不符时写 `failure.json`，**一次批量调用也不发**。

## 2. 唯一变量与不变项

| 项 | A | B | C |
|---|---|---|---|
| 系统提示 | 同一份 `CHAT_SYSTEM_PROMPT` | 同 | 同 |
| 冻结检索上下文 | 同一批页面、同一 `[资料N]` 顺序 | 同 | 同 |
| 问题与路由 | 同 | 同 | 同 |
| 附加说明块 | 无 | 原始规则专家结构化发现 | B + 来源核对、跨专家待补信息 |
| 生成后处理 | 无 | 无 | 原始回答通过产品校验后追加确定性分歧附录 |
| 生成调用次数 | 1 | 1 | 1 |

- 附加块的组装是 `C = B + crosscheck`，回归直接断言 `notes["C"].startswith(notes["B"])`，并把 B/C 的说明块从 user 消息中移除后要求与 A 逐字节相同。
- B 的载荷按白名单生成：只含 `facts / evidence / contradiction / gap_detection` 四个专家各自的原始字段，剥离 `upstream_review`；回归断言 B 中不出现 `source_checks`、`upstream_review`。
- C 的增量载荷固定为 `source_checks`、`issues`、`pending_items`、`limits`，其中 `limits` 显式声明 `semantic_entailment_checked=false`、`independent_corroboration=not_checked`、`amount_calculations_checked=false`。`pending_items` 只搬运疏漏专家标记为“专家来源核对待补”的条目，不凭空生成结论。
- 专家为确定性规则节点，提示与附录都写明“未经过任何独立模型推理”，C 不能表述为多个自主 LLM 分别推理。

## 3. 与产品路径的一致性

- 生成走产品函数 `services.call_local_llm(question, route, contexts, object_note=note)`，与正式问答同一份 HTTP 出口校验、超时、`max_tokens` 与 `enable_thinking=false`。
- 回归测试用捕获型 opener 取到真实请求体，逐字段比较 `build_prompt()` 与 `messages`；产品提示路径若变动，该测试先失败，而不是静默换了被测提示。
- 冻结提示摘要 `sha256(system+user)` 写入 manifest 与逐题记录；`run` 前重新组装并比对，摘要不符直接拒绝运行。
- 无上下文的题目不补造上下文：产品路径在模型调用前拒答，三组统一记 `not_attempted`（`rule_refused=true`），仍计入分母。

## 4. 计量、失败与分母

| 机制 | 行为 |
|---|---|
| 硬上限 | 单次提示 48,000 字符；超过即 `budget_exceeded`，不发起调用、不截断 |
| 输出上限 | 登记 900 token；与实际配置不符时拒绝开始 |
| 失败分类 | `model_unavailable`、`timeout`、`transport_error`、`invalid_response`、`contract_failed`、`budget_exceeded`、`not_attempted`，逐条写入 `attempts.jsonl` |
| 不重试 | 任一失败都不自动重发；异常类型与截断原因入账 |
| 未知用量 | 端点未报 token 时写 `null` 并计入 `unknown_usage`，从不写 0 |
| 分母 | `pass_rate_all_planned` 含全部计划尝试；`pass_rate_executed_ok` 单列，二者同时报告 |
| 轮换 | `rotation(groups, index, repeat)` 预先登记，避免缓存与加载顺序总偏向同一组 |

## 5. 冻结批次（本机）

| 项 | 值 |
|---|---|
| 目录 | `output/portfolio-ablation/dev-20261027`（私有产物，`.gitignore` 已排除） |
| manifest SHA-256 | `0573d72d540f64558fe59d55fdfc57498bfe7bcf50da06bdcb305ce198dd36c2`（67,302 字节） |
| 题目 | 60 题；检索状态 59 `frozen` / 1 `no_context`（`injection-01`：产品路径在生成前拒答） |
| 隔离库 | 14 页，来源摘要与数据集 `source_sha256` 相同 |
| 提示长度（字符） | A 364–901（中位 855）；B 364–8,556（中位 7,568）；C 364–10,734（中位 9,719） |

预检与门禁实测：`precheck` 返回 `available=false` 并以退出码 3 结束；`run` 写 `run/failure.json`（`outcome=model_unavailable`、`batch_calls=0`、`attempts_not_started=true`）后同样以 3 结束。真实生成 0 次。

## 6. 替身回归

`uv run --locked python -m pytest tests/test_collaboration_ablation.py -q` → **15 passed**。

覆盖点：

1. 三组只在登记变量上不同（说明块前缀关系、剥离说明块后 user 逐字节相同、系统提示相同、长度递增）。
2. 产品请求体与 `build_prompt()` 完全一致，冻结摘要可复算。
3. 冻结批次绑定来源摘要与库内页数；篡改说明块后 `verify_prepared_integrity` 拒绝。
4. `prepare` 拒绝已存在的批次目录；`run` 的阶段产物不覆盖。
5. 超预算不触发模型调用。
6. 原始回答不合格时附录被扣留，`contract_failed` 且不通过。
7. C 仅在原始回答通过校验后追加附录。
8. `model_unavailable` / `timeout` / `transport_error` 分类落账且不重试。
9. 失败尝试留在分母，`pass_rate_all_planned < pass_rate_executed_ok`。
10. 未知用量为 `null` 且计入计数；已知用量正确累加。
11. 轮换顺序确定且三组首位次数均衡。

相关既有回归：`tests/test_review_collaboration.py`、`test_answer_eval.py`、`test_answer_eval_v2.py`、`test_answer_judge.py`、`test_collaboration_acceptance.py`、`test_agents.py` 共 **102 passed / 39 subtests passed**。静态检查：`ruff check app tests scripts` 通过，`mypy` 54 个文件无问题。

## 7. 结论与限制

- P1 退出标准“替身回归能验证三组只在规定变量上不同，来源和安全检查保持一致”已满足；驱动可复用现有采集/计量组件并按批冻结。
- **替身回归不是模型效果证据**：本节所有通过项只说明流程按设计执行，不说明回答质量。真实模型对照（P2）因资源不可用而未开始，见 P0 第 4 节。
- 已知未覆盖：保留集 24 题尚未编制；盲审三组配对与多案件输入尚未适配；框架子进程与原生/LangGraph 一致性未在本次新增驱动中重跑（未改动其代码路径）。
