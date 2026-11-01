# 多轮追问与上下文利用：M01 基线冻结

日期：2026-10-24（北京时间）。状态：**M01 完成；质量基线为计划书指定的 `540b2ad`，其后的 4 个提交（`043481a`、`cb3e2cb`、`4e13799`、`b979db3`）只包含可核验性修复与文档，不改变本计划涉及的聊天/Agent 入口语义；本阶段冻结的实际代码基线为 `b979db3`，工作区干净。本阶段只冻结契约、样本与实验参数，未改业务代码、未运行模型评测。**

依据：[多轮追问执行计划](multiturn-context-execution-plan-20261024.md)。样本与逐题原文见 [tests/multiturn_fixtures.py](../../../tests/multiturn_fixtures.py)，结构自检见 [tests/test_multiturn_fixtures.py](../../../tests/test_multiturn_fixtures.py)。

## 1. 代码基线事实（逐条核对源码）

| 环节 | 当前实现 | 关键事实 |
| --- | --- | --- |
| 请求入口 | `POST /api/cases/{id}/chat`（[main.py](../../../app/main.py) `case_chat`）；`ChatRequest` 携带可选 `conversation_id` | `user_name` 只作展示作者；授权由 `require_case`/中间件按认证主体判定 |
| 普通聊天 | [services.py](../../../app/services.py) `chat` | 检索只用当前问题；历史消息不进入模型；会话校验与消息写入在**同一后置事务**中，显式不存在的会话 ID 会被静默改为新建 |
| 本地模型调用 | `call_local_llm(question, route, contexts, timeout, model_override)` | 请求体仅 system + 当前问题/卷宗片段；`PROMPT_VERSION=chat-system-v3-untrusted-case-data-required-review` |
| 原生 Agent | [agents.py](../../../app/agents.py) `LawReviewCoordinator.process_query` | 记忆召回只记录、不注入；审校 `CriticAgent.run` 接收问题、检索片段与专家摘要，不接收历史；`agent_runs` 记录问题/路由/答案/引用 |
| LangGraph | [langgraph_agents.py](../../../app/langgraph_agents.py) `ReviewState`、`process_query`、`resume` | 状态含 `memory_hits` 但审校未用；checkpoint 以 `agent-run-<run_id>` 为线程标识，恢复前校验 run/case/question 一致 |
| Agent 入口 | `POST /api/cases/{id}/agent-chat`、`POST /api/cases/{id}/agent-jobs` | 两类请求均无会话字段；后台成功后**新建**会话写两条消息 |
| 会话读取/归档 | `GET /api/cases/{id}/conversations`、`GET/POST/DELETE /api/conversations/{id}[/archive]` | 归档 7 天后清理；消息按 `id` 固定顺序 |
| 引用 | `chat`/`_citation` 都以**本轮检索片段**序号生成 `[资料N]` 与资料卡 | 无任何历史回答参与引用编号 |
| 草稿记忆 | [rag.py](../../../app/rag.py) `remember`/`recall_memories` | 只召回同案已完成运行的 `agent_draft_validated`，保留 `structural_checks_only_lawyer_review_required` |
| 持久化 | [db.py](../../../app/db.py) `messages`、`agent_runs` | schema v17；`messages` 无轮次类型/来源运行列，`agent_runs` 无会话/上下文快照列 |

结论：当前所有入口都是**单轮**语义，历史只用于展示。M02-M05 的改动必须在检索之前完成对象解析，并把“历史仅帮助理解指代”与“本轮卷宗片段才是证据”分开。

## 2. 冻结的接口与状态契约（v1）

### 2.1 策略开关（可用环境变量回退）

| 变量 | 默认 | 语义 |
| --- | --- | --- |
| `LAW_REVIEW_CONTEXT_HISTORY` | `on` | `off` 时不读取、不注入会话历史与草稿记忆；对象不明时仍请求澄清 |
| `LAW_REVIEW_CONTEXT_MEMORY` | `off` | 单独实验开关；关闭时草稿召回结果不进入模型输入 |
| `LAW_REVIEW_CONTEXT_HISTORY_TURNS` | `4` | 初始窗口：最近 4 个完整问答轮次 |
| `LAW_REVIEW_CONTEXT_HISTORY_CHARS` | `4000` | 历史总字符上限（含角色标签前的内容字符） |
| `LAW_REVIEW_CONTEXT_TURN_CHARS` | `2000` | 单轮内容上限，超出时截断并显式标记 |
| `LAW_REVIEW_CONTEXT_MEMORY_ITEMS` | `3` | 草稿线索最多 3 条 |
| `LAW_REVIEW_CONTEXT_MEMORY_CHARS` | `2000` | 草稿线索字符上限 |
| `LAW_REVIEW_CONTEXT_TOTAL_CHARS` | `6000` | 历史+草稿合计上限；先移除草稿，再淘汰旧轮次 |

策略版本：`multiturn-context-v1`。以上数值为**待评测初始参数**，M03 启动后冻结，不按失败题临时调整。

### 2.2 历史快照

`HistorySnapshot` 字段：`conversation_id`、`case_id`、`cutoff_message_id`、`turns[]`、`policy_version`、`digest`、`chars`、`dropped_turns`、`truncated`。每轮 `HistoryTurn` 记录 `user_message_id`、`assistant_message_id`、原文、路由、`kind`、引用摘要；`kind ∈ {answer, clarification, insufficient_evidence}`，后两类标记为非事实轮次。

- 按服务端数据库读取；不接受客户端传入历史正文。截止点固定为**读取时**的最大消息 ID。
- 排除当前请求；只收**成对**的用户+助手消息；失败/未完成轮次（无助手消息）不计入。
- 超出预算按**完整旧轮次**淘汰；单轮超限先截断并标记。摘要 `digest` 覆盖策略版本、会话、截止点与逐轮消息 ID/内容哈希，供并发与恢复身份比对。
- 历史、草稿、卷宗文本都作为不可信数据；历史助手内容不具事实权威。

### 2.3 对象解析

`ResolvedQuestion` 字段：`original`、`retrieval_query`、`status`、`object_document_ids`、`basis_message_ids`、`method`、`reason`、`options`、`history_digest`、`policy_version`。

- `status ∈ {explicit, resolved, needs_clarification}`。无省略且对象明确时为 `explicit`，保持原问题检索，不增加模型调用。
- 显式更正（“不是这份/改看…”）优先于历史对象；确定性名称/主体+文书类型匹配；匹配不唯一时澄清，不选相似度最高者。
- 省略且历史唯一对象 → `resolved`，用“对象名+原问题”重新检索，并把检索结果限定到解析出的材料；多对象时保留全部解析对象，无法唯一确定则澄清。
- 不使用模型补造金额、期限或法律结论；本版解析方法为确定性规则，记录 `method`（`none`/`explicit_name`/`explicit_entity_type`/`history_citation`/`history_mention`）与 `reason`。

### 2.4 三类结果与输出契约

| `answer_status` | 引用 | 校验范围 | 保存 `messages.kind` | 写入草稿记忆 |
| --- | --- | --- | --- | --- |
| `answered` | 必须全部来自**本轮**检索片段 | 现有 `validate_review_answer`（格式/范围/来源存在/复核提示） | `answer` | 仅 Agent 既有规则 |
| `clarification` | 空 | `validate_context_notice`：非空、无 `[资料N]`、无复核句伪装为事实、必须含澄清问题与选项边界 | `clarification` | 禁止 |
| `insufficient_evidence` | 空 | 同上，且必须说明限定范围与“资料不足” | `insufficient_evidence` | 禁止 |

澄清/资料不足消息可以作为会话轮次保存，用于接收下一轮选择；不得伪装为已验证事实，也不允许为它们放宽 `answered` 的引用校验。结果新增字段：`answer_status`、`clarification`（含 `reason`、`options[{document_id,name}]`）、`context`（历史/解析/预算/策略身份摘要）。

### 2.5 会话错误语义（行为变化）

- 显式提供但不存在、属于其他案件或不可访问的 `conversation_id`：**前置拒绝**（HTTP 404），不读取历史、不检索、不调用模型、不新建会话。
- 归档会话：保持现有只读冲突语义（HTTP 409，提示新建会话）。
- 仅**未提供**会话 ID 时才新建会话。普通聊天、同步 Agent、后台任务均返回实际 `conversation_id`。
- 写入前再次校验会话存在、未归档；失败时不写入成功回答。删除会话后其派生草稿记忆不得经上下文绕回。

### 2.6 持久化增量（schema v18，纯增量）

- `messages`：`kind TEXT NOT NULL DEFAULT 'answer'`、`source_run_id INTEGER`；`(source_run_id)` 非空唯一索引，保证同一运行/请求只保存一次。
- `agent_runs`：`conversation_id INTEGER`、`context_json TEXT NOT NULL DEFAULT '{}'`，保存当次冻结的上下文身份（策略版本、快照摘要、解析状态、预算与依据消息 ID）。
- 旧记录读取时缺省为无上下文（`context_json='{}'`、`kind='answer'`）；不得用当前历史重建旧运行冒充恢复。

### 2.7 模型输入与引用

- 旧调用默认无历史：`call_local_llm`/`CriticAgent.run` 新增参数必须可选，旧测试与旧路径行为不变。
- 输入分块：系统规则 → 当前问题与解析对象 → 有限历史（不可信，仅助指代）→ 可选草稿线索（不可信，非证据）→ 本轮卷宗片段。
- 提示身份与 provenance 记录“历史是否启用、对象如何确定、哪些线索被使用/排除”；日志不复制完整案件正文。
- 最终 `[资料N]` 只来自本轮检索片段，重新编号并逐项对齐资料卡；历史引用不得充当本轮引用。

## 3. 合成样本与逐题原文

样本为**完全虚构**的合同组（公司、金额、日期、人名均为合成），见 `tests/multiturn_fixtures.py`：

| 文档键 | 文件名 | 可核验事实（原文） |
| --- | --- | --- |
| `jia_contract` | 甲公司采购合同.txt | 甲方甲公司、乙方丙公司；总价 120 万元；验收合格后 60 日内支付；2026-03-01 前交付 |
| `yi_contract` | 乙公司采购合同.txt | 甲方乙公司、乙方丁公司；总价 80 万元；验收合格后 30 日内一次性支付 |
| `supplement` | 补充协议.txt | 对 JC-2026-001（甲公司合同）付款期限变更为验收合格后 90 日内 |
| `meeting_minutes` | 项目会议纪要.txt | 讨论交付进度，未变更付款期限（干扰材料） |

场景集：`jia_only`（仅甲公司合同）、`pair`（甲乙合同＋纪要）、`pair_with_supplement`（甲合同＋乙合同＋补充协议＋纪要）。

冻结题集：**12 组会话、24 道追问**，其中 S01-S08 为开发集，S09-S12 为保留集（首次评测前封存）；另设 8 道对象明确的单轮题检查退化。类别覆盖：单一对象省略（S01-S04）、多合同歧义与更正（S05-S06）、历史错误/旧草稿/当前冲突（S07-S08）、新会话/跨案/归档删除（S09-S10）、长历史截断/注入/无原文支持（S11-S12）。逐题预期状态、必答事实、原文定位见样本文件；`tests/test_multiturn_fixtures.py` 校验每条必答事实确实出现在其声明的文档页中，防止“凭空写答案”。

## 4. 冻结的实验参数与顺序

- **A**：不读取历史、不注入草稿（旧基线能力，用同一实现开关复现）；**B**：读取同会话历史、不注入草稿；**C**：B + 草稿线索。三者同一实现、模型、参数、材料与问题，开关切换。
- 主实验：冻结历史/草稿快照启动 A/B/C，各组交错执行，禁止前组输出污染后组；A 不读取快照，B 只读会话历史，C 再读草稿。检索结果差异是预期效应，不强行共享片段。
- 补充实验：端到端真实多轮（每分支空白隔离会话，先跑前置轮再追问），与冻结历史结果不混为同一因果结论。
- 预算：B/C 相对 A 的批次总 token ≤1.50、端到端 p95 ≤1.30；报告每题调用数、token 与 p50/p95 时延。此预算不是 G2 门槛替代。
- 重复运行不增加独立题数；保留集见题后调参即转开发集，须重设保留集再验证。
- 隔离：所有运行使用 `LAW_REVIEW_DATA_DIR` 临时库与合成材料；原始输出、模型返回与日志写入 `output/migration/multiturn-context-<执行日期>/`（已 Git 忽略），仓库只提交安全夹具、实现、测试与脱敏报告。

## 5. 本阶段明确不包含

- 候选 ForgeHarness/answerability 输入协议（候选 v10 未接通多轮）。
- 真实本机模型配对评测、使用者试用与专业复核（M06；本轮未执行，不能代签）。
- G2/E07、E17 或生产切换资格的任何状态变更。
- 任何“历史已使回答更可靠”的结论：M01 只冻结可复现的基线与样本，尚无对照结果。

## 6. 复现命令

```sh
uv run --locked pytest tests/test_multiturn_fixtures.py -q
```

样本自检不依赖模型、网络或真实案件材料。
