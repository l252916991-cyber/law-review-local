# 求职展示版 P0：范围、身份与证据清单冻结

- 日期：2026-10-27（Asia/Shanghai）。
- 对应计划：[agent-portfolio-plan-20261027.md](agent-portfolio-plan-20261027.md)。
- 受检代码：`3c1a197`（分支 `codex/agent-portfolio-plan`）。工作区仅有一个与本任务无关的未跟踪目录 `tmp_eq/`（本地图片草稿），未纳入本次任何产物。
- 结论：**P0 通过（可进入 P1）**；**P2 真实模型调用在资源层不可开始**，原因见第 4 节，不写任何推测数值。

## 1. 代码与算法身份

`app/model_comparison.py` 的固定摘要门禁在本机运行通过：`schema_version=2`、`revision=specialist-review-v2`、`source_commit=c63abf953bf10f6f025c2ff22e5819247590e58a`，七个算法文件摘要与登记值逐字节一致。

| 算法文件 | SHA-256（与登记值一致） |
|---|---|
| `agents.py` | `6ae41ec65c7bc174c7ee9bfb7ab98c63e739f1a6fa0862b1ed016b6e7d6ae55a` |
| `review_collaboration.py` | `512c03d6719bddfd98d422c7cb65e46c26331b8aab7ab56b40e651a4a07d632a` |
| `agent_tools.py` | `2af80bdaa3e1277de96a4127e2fd9bbabbb906b1213a94e6beb43a54d28dfb34` |
| `rag.py` | `a79cd859fc639c409d043f18379e0ea45a286f077c9b633f40d6a06fbad949cc` |
| `services.py` | `4c2047b9ca49ec85619351e8b877bdadb672b5c5ae8fca30509fdd6d8eab9468` |
| `model_domain.py` | `f1e69e1d56d0fc73a15e51888b2b31e4bda6ce6e1f3a57bccad39f2f9dce5cbc` |
| `statutory_retrieval.py` | `d9d2f60c01ac03f405ca4e1da593ff049a32cd6e072d3fa4bcb98f8be4b9fbe8` |

> 2026-10-27 后续：`agents.py` 因专家发现顺序缺陷修复而改动，native 基线重登记为 `specialist-review-v3`（见[可复现性修正](portfolio-determinism-20261027.md)）。本表保留为 P0 当时核对的快照，六个未改动的算法文件摘要仍然有效。

提示、策略与评分身份（本次冻结值）：

| 项 | 值 |
|---|---|
| `PROMPT_VERSION`（`services.py`） | `chat-system-v4-multiturn-untrusted-history` |
| `LOCAL_PROMPT_VERSION`（`model_domain.py`） | `domain-local-v1-bounded-citations` |
| `POLICY_VERSION`（`answerability.py`） | `candidate-answerability-v11-detail`，提示摘要 `fbb3769a614dc14c5e393f834e78608c26b998fbf7f9c5ad5ff19d2b9c330fb7`，实现摘要 `9868326b8e14befaf3f98ccab853a9bc94bc932dd0fa6d30d359b5baab14f778` |
| 引导文法 | `omlx-guided-grammar-v1`，摘要 `8112a8673333f3f7981a3535a66bf8590e669ebb0fd832ebec0d675a95d91ba3` |
| Judge 提示 | `answer-judge-v2-frozen-sources` |

## 2. 数据身份

| 项 | 值 |
|---|---|
| 数据集 | `benchmarks/answer_eval/v2/dataset.json`，`demo-case-answer-v2`，version 2 |
| 题目 / 独立来源 | 60 题 / 14 页；类别：正常 30、候选矛盾 10、资料不足 8、无关 4、缺法条 4、问题注入 4 |
| 文件摘要 | `428264866dccc02b6ab8e83b9cbc8a9ac52f392906eeb8c1aff8b3a609eabf47`（与本目录 `manifest.json` 一致） |
| 来源摘要 | `b912f4ca65417cd8e0571b26869cf2ad5e1f74a1248bb7e2a7e2c2475b0f9566`（`scripts/build_answer_eval.py` 可从仓库合成种子幂等复算） |
| 标注来源 | `assistant_generated_from_repository_synthetic_fixture`：编码助手依单一虚构案件构造，**未经律师或独立人工标注** |

已知限制（随材料一同交付）：60 题同属一个虚构案件，不能证明跨案件泛化；关键词规则受同义表达与否定句影响；页级检索、确定性关键词、模型 Judge、人工评分必须分别报告。

## 3. 已有工程证据

| 证据 | 绑定身份 | 本轮可用性 |
|---|---|---|
| [多专家交接实施记录](specialist-collaboration-20261026.md)：121 passed，含框架子进程与恢复检查 | `c63abf9` 起点的实现快照 | **可复用**：算法摘要仍一致，代码自该记录后未变 |
| [G1 工程检查证据](collaboration-release-readiness-evidence-20261027.md)：Python 3.12 离线 1411 passed / 2 failed / 1 skipped，综合覆盖率 86.13% | `19bdb68` | **可复用为前提，但需按新提交重跑**：`19bdb68..3c1a197` 只改文档与 `.gitleaksignore`，`app/ tests/ scripts/ frontend/ benchmarks/` 无差异；本轮新增模块后测试数与覆盖率会变 |
| 前端构建、签发与权限流程 | 同上 | 本轮未改前端，沿用；演示前需确认进程加载的构建摘要 |
| 离线评分、Judge、20 题盲审包 | `test_answer_eval_v2.py`、`test_answer_judge.py`、`test_blind_review_package.py` 通过 | **可复用** |
| 三组 A/B/C 对照 | 从未存在 | **缺失**，见 P1 |
| 真实模型答案与用量 | 历史报告基于旧代码身份 | **不可复用**：模型身份、端点与本机资源均不同 |

## 4. 模型资源（P2 的阻塞点）

| 项 | 实测值 |
|---|---|
| 配置端点 | `http://127.0.0.1:8000/v1`（`LAW_REVIEW_LLM_URL` 默认值，与 `data/model_settings.json` 一致） |
| 配置模型 | `Qwen3.6-35B-A3B-OptiQ-4bit`（`LAW_REVIEW_LLM_MODEL` 默认值，与 `data/model_settings.json` 一致） |
| 采样与上限 | `temperature=0.2`、`max_tokens=900`；驱动不发送 `seed`，不承诺跨设备逐字一致 |
| 可用性探针 | `services.local_llm_available()` → `(False, 'Qwen3.6-35B-A3B-OptiQ-4bit')` |
| 端口监听 | 无进程监听 8000/1234/8080/11434/8001 |
| 权重目录 | 仓库 `models/` 为空；本机用户目录无 `models`、无 MLX 权重目录 |
| `.env` | 不存在，模型身份未登记 |

结论：**真实模型预检失败在资源层**。按计划第 7 节“预检失败先定位资源/协议问题，停止无效批量调用”，P2 的 405 次计划生成在本机无法开始，任何数值都不能由替身、历史报告或规则输出补齐。恢复条件是：启动声明的本地端点、使 `/models` 返回登记的模型 ID，再以新批次身份执行。

## 5. 首轮预算登记

| 批次 | 计划生成次数 | 依据 |
|---|---|---|
| 预检 | 9 | 开发集正常/资料不足/矛盾各 1 题 × 3 组 |
| 开发集主实验 | 180 | 60 题 × 3 组 × 1 轮 |
| 保留集 | 216 | 24 题 × 3 组 × 3 轮 |
| 合计 | **405** | 不含 Judge、重跑与演示 |

- 本题集三组提示长度为 A 中位 855（364–901）、B 中位 7,568（364–8,556）、C 中位 9,719（364–10,734）字符（60 题，全部按 `prepare` 冻结值统计）。B/C 的增量是协作机制本身的输入成本。
- 硬上限：单次提示 48,000 字符（`MAX_PROMPT_CHARS`），超过即 `budget_exceeded` 且不截断；输出上限 900 token，与配置不一致时拒绝开始。
- 单价与费用：本机无价格登记，费用为**未知**；本地 token 不是商业账单，也不等于零成本。
- 停止上限：预检不可用即停止（已发生，批量调用 0 次）；单批次出现契约/安全/数据完整性问题即停止并保存证据，修复后用新批次身份重跑。

## 6. 协议冻结要点（进入 P1 的判据）

1. 三组共享同一冻结检索上下文、同一问题、同一模型、同一采样参数与一次最终生成调用；唯一变量是附加到模型输入的说明块。
2. A 不含任何专家信息；B 只含原始规则专家结构化发现，不含 `source_checks`、`upstream_review` 等衍生审校信息；C 在 B 上增加来源核对与跨专家待补信息，并在原始回答通过产品校验后追加确定性分歧附录。
3. 附录不能挽救不合格输出；三组均保留不可信草稿标记与法条/案件命名空间隔离；不读写长期记忆。
4. 失败、未尝试、未知用量分别统计，失败尝试计入分母；不接受“只算成功样本”的成功率。
5. C 不能宣称逐个专家独立进行了模型推理：专家为确定性规则节点。

## 7. 结论

- P0 的“可复用 / 需重跑 / 缺失 / 不适用”已列出：可复用的是冻结数据、评分与交接实现；需重跑的是三组对照、Judge、演示与发布检查；缺失的是真实模型资源、保留集 6 案例 24 题、独立评分人；不适用的是新增独立 LLM 专家、自主反思循环、更换框架、多实例部署（计划明确排除）。
- 下一步：按第 6 节实现 A/B/C 隔离驱动与替身回归（P1）；P2 保持阻塞状态，直到第 4 节的资源条件满足。
