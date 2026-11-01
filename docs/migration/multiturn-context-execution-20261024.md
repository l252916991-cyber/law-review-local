# 多轮追问与上下文利用：执行记录（M01–M06）

日期：2026-10-24（北京时间）。状态：**M01–M05 代码、测试与原子提交完成；M06 的离线配对管线、规则模式配对结果、独立隔离进程 HTTP 验证与完整离线检查已完成；真实本机 9B 模型配对评测结果见 §5（未完成或失败的题如实保留），使用者试用、真实浏览器点击流程与专业原文复核未执行。本记录不改变 G2/E07、E17 或生产切换资格。**

依据：[执行计划](multiturn-context-execution-plan-20261024.md)、[M01 基线](multiturn-context-baseline-20261024.md)。样本与逐题原文：[tests/multiturn_fixtures.py](../../tests/multiturn_fixtures.py)。证据目录（Git 忽略）：`output/migration/multiturn-context-20261024/`。

## 1. 提交与范围

| 提交 | 主题 | 范围 |
| --- | --- | --- |
| `886756f` | `test(multiturn)`：冻结基线与合成合同样本 | 契约、12 组会话 24 道追问、8 道单轮回归题、原文页自检 |
| `9f2720f` | `feat(context)`：前置会话校验与有界历史快照 | schema v18 增量、历史窗口/截止/摘要、404/409 语义、幂等轮次写入 |
| `108bc5a` | `feat(chat)`：同会话追问与澄清 | 确定性对象解析、重新检索与对象限定、三类结果契约、模型输入 v4、桌面澄清候选 |
| `2ee0e29` | `feat(agents)`：两运行时共用上下文与冻结恢复 | TurnContext、原生/LangGraph/后台会话绑定、运行快照、恢复校验、澄清不建运行 |
| `af6ddaf` | `feat(memory)`：独立开关注入草稿线索 | 来源运行/信任/会话/资料页/预算校验、引用编号剥离、两运行时与聊天共用 |
| `93e93fe` | `fix(chat)`：泛化类型词让位与缺条款判定 | 历史对象优先于泛化类型词；封闭集合“所问但不存在”返回资料不足 |
| 本次 M06 | `test/docs`：配对评测与执行记录 | 评测脚本、规则/模型配对结果、进程验证、完整检查、本记录 |

工作区基线：`b979db3`（另有与本计划无关的 `a876b2e` 单文档检索覆盖计划文档，未改动本计划文件）。以上提交均为本地提交，未推送、未合并。

## 2. 已实现的行为

### 2.1 会话与历史

- 普通聊天、同步 `agent-chat`、后台 `agent-jobs` 均接受可选 `conversation_id`。显式提供但不存在、跨案或不可访问：受理阶段 `404`，不读历史、不检索、不调模型；归档：`409`。仅省略 ID 时新建会话；三个入口都返回实际 `conversation_id`。
- 服务端按受信案件/会话读取快照：默认最近 4 个完整问答轮次、合计 4000 字符，单轮上限 2000 字符；按消息 ID 固定顺序，排除未完成轮次；超预算整轮淘汰并记录。摘要覆盖策略版本、会话、截止点与逐轮消息 ID/内容哈希。
- 消息新增 `kind`（`answer`/`clarification`/`insufficient_evidence`）与 `source_run_id` 唯一索引；同一运行/请求只写一次。`agent_runs` 新增 `conversation_id`/`context_json`（冻结策略、历史摘要、解析对象、渲染后的历史与对象注释）。
- 写入前再次校验会话存在且未归档；生成期间删除/归档/撤权时不写入成功回答。

### 2.2 对象解析与三类结果

- 确定性解析：显式名称、主体+文书类型、历史引用、历史提及、上一轮澄清选项；显式更正优先。泛化类型词（“哪份合同”）在多对象且存在省略时让位于可验证的历史对象，避免无谓澄清；真正的多对象歧义仍要求澄清。
- 解析后重新检索，并把本轮检索片段限定到解析材料；历史仅在“已解析”轮次注入模型，渲染时移除旧 `[资料N]` 编号。
- `answered`：必须通过原引用校验（格式、范围、来源存在、复核句）。`clarification`/`insufficient_evidence`：HTTP 200、无引用、独立校验（无私造引用、不伪装为事实回答）、保存为非事实轮次、不写草稿记忆。
- 资料不足触发：本轮检索为空；或所问的封闭集合特定条款词（保密、违约金、仲裁等）在当前使用页中不存在。该门控是保守的确定性检查，不是语义蕴含判断。

### 2.3 Agent、后台与恢复

- 原生 DAG 与 LangGraph 共用 `TurnContext`；规划/检索/专家处理使用解析后问题，审校模型接收不可信历史与对象注释，最终引用仍只来自本轮片段。
- 后台任务可续写指定会话；省略 ID 仍新建。澄清类 Agent 请求直接返回澄清并保存轮次，不创建运行、不调用模型。
- LangGraph 恢复复用运行快照：消费重试前校验会话访问/未归档、历史截止消息数量、checkpoint 与运行快照一致、冻结上下文中的资料页仍存在；任一变化拒绝恢复。旧运行无快照时按“无上下文”恢复，不用当前历史回填。
- 恢复/重试通过 `source_run_id` 只写一次问答轮次。

### 2.4 草稿消融开关

- 默认关闭（`LAW_REVIEW_CONTEXT_MEMORY=off`）。启用后仅当本轮实际使用了会话历史且调用模型时注入；最多 3 条、合计 2000 字符。
- 每条线索须通过：来源运行同案且已完成、信任标记为 `structural_checks_only_lawyer_review_required`、来源会话未删除、来源引用页仍存在、非空内容与字符预算。排除原因写入 `memory.excluded`，不进入提示词。
- 线索去除历史引用编号、以“不可信、非证据”呈现，不分配本轮 `[资料N]`、不写入引用；与当前原文冲突时以本轮检索为准。

## 3. 互操作与持久化契约

- schema v18 为纯增量：`messages.kind`、`messages.source_run_id`、`(source_run_id)` 非空唯一索引、`(conversation_id,id)` 索引、`agent_runs.conversation_id`、`agent_runs.context_json`；旧行默认 `kind='answer'`、`context_json='{}'`、`conversation_id` 为 NULL。
- 旧调用默认无历史：`call_local_llm(..., history="", memory="", object_note="")` 与 `CriticAgent.run(..., history="", memory="", object_note="")` 均为可选关键字参数。
- 聊天结果新增 `answer_status`、`clarification`、`context`（policy/history/resolution/memory）；Agent 结果新增 `answer_status`、`clarification`、`conversation_id`、`context`、`memory_meta`。既有字段与 `retrieval_metrics` 保持原样。
- 回退：`LAW_REVIEW_CONTEXT_HISTORY=off` 关闭历史读取/注入与记忆；`LAW_REVIEW_CONTEXT_MEMORY=off`（默认）关闭草稿注入。关闭后对象不明仍返回澄清，不回退到猜测；已保存消息与审计保留。

## 4. 自动化验证

### 4.1 新增测试

| 文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `tests/test_multiturn_fixtures.py` | 6 | 样本计数/原文支撑/期望完整性/合成材料边界 |
| `tests/test_multiturn_context.py` | 14 | 窗口/截断/摘要冻结/幂等写入/迁移默认值/入口拒绝顺序 |
| `tests/test_multiturn_chat.py` | 22 | 唯一/多对象/更正/删除旧引用/澄清选择/历史冲突/模型请求体/资料不足/API |
| `tests/test_multiturn_agents.py` | 8 | 两运行时上下文/会话绑定/澄清不建运行/后台续写/恢复与归档 |
| `tests/test_multiturn_memory.py` | 15 | 信任/来源/删除会话/失效页/预算/污染草稿/两运行时注入 |
| 合计 | **65** | 另更新既有服务、运行时适配器与后台任务测试 |

分阶段运行的受影响回归（各阶段提交前）：M02 130 通过；M03 202 通过；M04 304 通过/37 跳过（独立框架 Python 未装配的既有跳过）；M05+M06 回归 275 通过。

### 4.2 完整离线检查

见 §7；以最终一次结果为准。

### 4.3 隔离进程 HTTP 验证

在独立数据目录启动真实 `uvicorn app.main:app` 进程（`127.0.0.1:8765`），上传合成合同（`\f` 分页），执行 17 项检查：**17/17 通过**。覆盖：首次问答、追问解析到唯一合同、当前引用限定、60 日条款、会话身份保持、多合同歧义澄清与候选、按名称选择后重新检索 30 日条款、新会话不继承、未知会话 404、归档会话 409、原生 Agent 追问与当前引用、桌面页与聊天脚本可加载。证据：`output/migration/multiturn-context-20261024/process-http.json`。

未执行：真实浏览器点击、引用页跳转、移动端页面流程、系统通知/权限边界。前端脚本语法检查因本机无 Node.js 未执行（静态代码人工复核，CSS/JS 改动很小）；不能声称“全部前端脚本检查通过”。

## 5. 配对评测

### 5.1 规则模式（确定性管线检查）

`uv run python scripts/multiturn_context_eval.py --output output/migration/multiturn-context-20261024`，12 组会话/24 道追问，每道题从同一冻结基线库复制后运行 A/B/C：

| 分支 | 运行数 | 全部检查通过 | 澄清 | 资料不足 | 草稿使用 | p50 ms | p95 ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A 无历史 | 24 | 9 | 18 | 0 | 0 | 2.81 | 7.23 |
| B 同会话历史 | 24 | 24 | 3 | 2 | 0 | 7.35 | 8.38 |
| C 历史+草稿 | 24 | 24 | 3 | 2 | 0 | 7.25 | 9.12 |

- A 的 9 项通过均为对象明确、无需前文的题；18 次澄清是“无历史时对象不明”的预期行为。B 在依赖前文的题上恢复为有据回答；两合同歧义题在 A/B/C 均澄清（该题在无历史时同样无法唯一确定）。
- 规则模式不调用模型，草稿分支 C 与 B 等价（草稿只在模型调用时注入）；C 的增量效果必须由模型评测回答，不能由本表推断。
- 这是管线/状态契约检查，**不是模型质量结论**；token 记为 null，不与模型结果合并。

### 5.2 真实本机模型

模型：本机 oMLX `Qwythos-9B-v2-OptiQ-4bit`，`LAW_REVIEW_LLM_TEMPERATURE=0`，规则检索（hashed embedding 离线）；同一进程内 A/B/C 从冻结基线库复制后运行。

**结果（24 道追问 × 3 分支，2026-10-24 20:08 完成，约 8 分钟）：**

| 分支 | 运行数 | 全部检查通过 | LLM 使用 | 澄清 | 资料不足 | 草稿使用 | p50 ms | p95 ms |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A 无历史 | 24 | 8 | 4 | 18 | 0 | 0 | 3.98 | 9002.88 |
| B 同会话历史 | 24 | 20 | 14 | 3 | 2 | 0 | 8747.07 | 11792.83 |
| C 历史+草稿 | 24 | 20 | 14 | 3 | 2 | 1 | 7960.42 | 12646.11 |

- **历史增益**：B 通过 20/24，A 通过 8/24；A 的其余 16 项为依赖前文的第 2 问无历史澄清（预期）或模型校验失败。B 的 4 项失败均为模型输出未通过引用契约（`RuntimeError`/`Chat validation failed issues=citations_valid,sources_valid`），分别为 S02-T2、S07-T1、S07-T2、S10-T2；这些题在规则模式 B 中均通过，说明对象解析/检索定位正确，失败发生在模型引用格式环节。本结果支持“依赖前文的可回答题 B 高于 A”，但不声称已达到人工核验标准。
- **历史冲突（S07）**：模型模式 B/C 两次均因引用校验失败未返回答案，**无法据此判断是否复述旧错误**；规则模式 S07 两题通过（当前原文 60 日、未出现 30 天）。该项保持未完成，不记为通过。
- **草稿增益**：C 仅 1 次实际注入草稿（S08-T1）；该答案写当前 120 万元并明确“历史对话曾提及 100 万元，与本轮片段冲突，以本轮为准，需更正历史结论”，引用仍来自当前页。C 的通过数与 B 相同（20），**没有可计为新增的正确题**，因此维持草稿注入默认关闭。
- **跨案/歧义/新会话**：B/C 的 S04 歧义、S09 新会话、S10-T1 跨案澄清均按预期通过；S10-T2 因模型校验失败。零跨案读取/写入。
- **性能**：B/A 的 p95 为 11.79/9.00 秒（比 1.31，略高于探索预算 1.30，A 仅 4 次模型调用，样本小）；B 的 p50 8.75 秒。**未记录 token**（聊天结果不返回 usage），不能计算 token 比；时延不能替代 G2 的 1.10/1.20 门槛。
- **模型失败分类**：全部为校验失败（模型未给合法 `[资料N]` 或来源范围不符），没有记录模型原始正文——`chat` 在返回前抛错，当前证据只保留失败类型；不得把失败题当作“正确拒答”。

## 6. 放行判断与回退

- **正确性（本轮范围）**：聊天与两运行时的对象解析、澄清/资料不足、引用限定与恢复边界均有自动化断言；隔离进程 17/17；跨案/归档/删除会话的读取为零容忍项。真实模型模式有 4 题因引用契约失败未返回答案（S02-T2、S07-T1/T2、S10-T2），按失败保留，未计为正确拒答。
- **历史增益**：规则模式 B 在依赖前文的题上 24/24 通过，A 仅对象明确题通过；模型模式 B 20/24 高于 A 8/24。按原始分布报告，不做统计显著或普遍业务增益声明。
- **草稿增益**：模型模式 C 与 B 同为 20/24；C 有 1 次草稿注入且未把草稿数字当作事实，但没有新增正确题。默认保持关闭。
- **性能**：规则模式 p95 7.23→8.38 ms（B/A）；模型模式 B/A 的 p95 比 1.31（A 仅 4 次模型调用）、B p50 8.75 秒。未记录 token，不能计算 token 比；未据此宣称达到 G2 的 1.10/1.20 门槛（本计划预算 1.50/1.30 仅是探索预算）。
- **回退演练**：`LAW_REVIEW_CONTEXT_HISTORY=off`（A 分支）与 `LAW_REVIEW_CONTEXT_MEMORY=off`（默认）由评测与测试实际执行；关闭后无历史注入、对象不明澄清、无跨案读取。已保存消息与审计保留。

## 7. 完整离线检查与证据

按 CI 对应命令在隔离数据目录执行（`PYTHONHASHSEED=0`、禁用网络、独立 `LAW_REVIEW_DATA_DIR` 与 checkpoint 数据库）：

| 检查 | 结果 |
| --- | --- |
| 完整 pytest（含 pytest-cov、pytest-socket、JUnit） | **985 passed / 4 failed / 87 skipped / 15 warnings，79.0 秒**；覆盖率 **79.69%**（门槛 70%） |
| 4 项失败 | 全部为 `tests/test_mobile_ui.py` 的 Node 子进程测试：本机未安装 `node`，`FileNotFoundError: 'node'`；不是业务断言失败 |
| Ruff | 通过 |
| Mypy（`scripts`，43 文件） | 通过 |
| compileall（`app tests scripts`） | 通过 |
| 依赖导出一致性 | `uv export --locked` 与 `requirements.txt`/`requirements-dev.txt` 无差异 |
| 发布清单 `scripts/package_release.py --dry-run` | 退出码 0，允许清单可重建 |
| `node --check app/static/*.js` | **未执行**（无 Node.js）；与 mobile UI 失败同因 |

证据：`/tmp/lexvault-ci/pytest.log`、`/tmp/lexvault-ci/coverage.json`（运行期临时路径，不提交）；`output/migration/multiturn-context-20261024/` 保存配对评测与 HTTP 验证结果。87 项跳过均为独立框架 Python 3.12 环境或 `mlx_lm` 缺失的既有跳过，不计入通过。

### 7.1 本批对比较基线的更新

`app/model_comparison.py` 以内容哈希固定 native 对比基线（`agents.py`、`rag.py`、`services.py`、`model_domain.py`、`statutory_retrieval.py`）。本批有意修改了 `agents.py` 与 `services.py`（上下文、记忆、资料不足门控），旧哈希必然失配。已更新这两个哈希并把基线指向包含最终被固定文件的提交 `93e93fe`；此前基于旧基线的 native 对比结果与更新后的实现**不可直接混用**，需要重新采集。候选/answerability 路径未改动。

### 7.2 未执行或不可用

- 真实浏览器页面流程（点击澄清候选、引用跳转、移动端）；本机无浏览器自动化与 Node。
- 真实律师使用者试用与专业原文复核（无可用试用者；不得代签）。
- 独立框架 Python 3.12 实际进程测试（环境变量未装配；本批未改框架接线，跳过不计通过）。

## 8. 未完成、限制与遗留问题

1. **使用者试用与专业复核未执行**：无独立律师试用者；不得代签“专业业务验收通过”。
2. **真实浏览器流程未执行**：只验证了隔离进程 HTTP 与静态资源可达；桌面澄清候选按钮、历史回放候选、引用页跳转未在浏览器点击验证。Node.js 未安装，`node --check` 未运行。
3. **Agent 资料不足门控弱于聊天**：特定条款“所问但不存在”的确定性门控只在普通聊天实现；Agent 仍主要依赖空检索与模型/审校规则。两运行时之间一致，但聊天与 Agent 的“资料不足”触发面不同。
4. **对象解析为确定性规则**：名称/主体+文书类型与历史引用覆盖本轮样本；未实现模型解析，也未处理更复杂的跨文档指代链。保留集样本在实现完成后未重新冻结（本记录明确：本轮未执行“先封存后评测”的正式模型流程）。
5. **草稿记忆的生命周期**：仅对已有 `conversation_id` 关联的来源运行执行“会话已删除则排除”；v18 之前的记忆没有会话关联，只能按来源运行与资料页校验。恢复时草稿文本随 checkpoint 冻结，不在恢复时重新召回或重验（默认开关关闭）。
6. **token/成本**：聊天/Agent 结果不返回实际 token；规则模式记为 null，模型评测只报告时延与调用次数，未报告 token 比。
7. **候选链路未接通**：ForgeHarness/answerability 候选输入协议未加入多轮字段；本轮结论不覆盖候选 v10。
8. **G2/E07/E17 与生产切换未变**：本计划只证明指定模型、材料和流程下的上下文利用实现；不改变既有质量状态或发布资格。

## 9. 复现命令

```sh
# 65 项多轮专项
uv run --locked pytest tests/test_multiturn_fixtures.py tests/test_multiturn_context.py \
  tests/test_multiturn_chat.py tests/test_multiturn_agents.py tests/test_multiturn_memory.py -q

# 规则模式配对（无模型）
uv run --locked python scripts/multiturn_context_eval.py --output output/migration/multiturn-context-20261024

# 真实模型配对（需要本机 OpenAI 兼容端点；模型/温度按评测记录固定）
LAW_REVIEW_LLM_MODEL=Qwythos-9B-v2-OptiQ-4bit LAW_REVIEW_LLM_TEMPERATURE=0 \
  uv run --locked python scripts/multiturn_context_eval.py \
  --output output/migration/multiturn-context-20261024-llm --use-llm
```
