# 系统架构

Web/worker 与模型服务器分别运行。应用仅使用 OpenAI-compatible HTTP 接口，不启用 LangSmith 或云端 Agent 服务。LangGraph 的传递依赖仍会安装 LangChain Core/LangSmith 客户端包；安装客户端不代表启用云端追踪，部署时不要打开相关 tracing 环境开关。

```text
Browser → FastAPI → business SQLite (documents/pages/evidence/audit/runs/memory)
               ├→ shared Agents → native Python DAG (default)
               ├→ shared Agents → LangGraph → independent SQLite checkpoint
               ├→ local LLM / embedding HTTP server
               └→ Redis queue → arq worker → document extraction/indexing
```

HTTP 层：`app/main.py` 只负责应用生命周期、中间件、前端静态托管和路由装配；接口处理函数按领域放在 `app/routes/`（`system`、`cases`、`documents`、`evidence`、`chat`、`agent_runs`、`bank`、`exports`，共享的案件访问校验与 Redis 探测在 `common`），成员、人工任务、报告、复核任务与登录沿用各自模块的 `APIRouter`。业务逻辑仍在 `app/services.py`、`app/agents.py` 等模块，路由层不复制规则。

业务表保存可展示的审计与结果；checkpoint 保存图状态和待执行节点。LangGraph 线程以运行 ID 隔离，恢复仍使用原线程；节点副作用需要业务层幂等保护。SQLite 适合当前单机原型，不宣称多实例高并发运行能力。

FTS5/BM25、法律词项和本地向量经 RRF 融合；生产检索随后按问题信号做确定性重排，并对高分命中补充同文档邻页、限制单一文档最多占结果的 60%。该限制按相关性顺序执行，允许同文档多个关键页入选，不强制先为每份文档分配一页。精确人名、金额、日期、法条号和证据类型优先保留词法命中，开放式事实问题保留语义召回。无真实命中时不再把数据库前几页当作证据。结果附带来源多样性、直接命中数、邻页数和置信度诊断；低置信度只表示需要律师核验，不代表自动拒答或事实成立。向量缓存有效性应包含内容、模型身份、后端、维度与预处理版本；仅维度相同不能证明向量空间一致。模型不可用时有显式降级，降级结果不可计作成功模型性能样本。

`LAW_REVIEW_EMBEDDING_REVISION` 是同名模型权重变更时的显式版本标识；替换权重后必须修改它，使缓存失效重建。服务地址变更也应确认实际模型身份。旧版 `legacy` 记忆保留历史记录但不自动召回；通过当前格式校验的 `draft` 仍是未经过律师确认的草稿，不能升级为事实依据。

引用格式有效与证据支持事实是两个指标。最终答案、来源文档、页码、节点输出与耗时供律师复核，不保存/展示模型隐藏思维链。自动金额/疏漏规则只是线索，不是法律事实裁定。

## 估算费用与定价快照（A02.2）

费用是"token 数 × 配置单价"，是估算值，不是账单。`app/pricing.py` 从 `LAW_REVIEW_PRICING_JSON`（内联 JSON）或 `LAW_REVIEW_PRICING_FILE` 读取价目表：`version`、`currency`、`unit_tokens`、`source` 和每个模型的输入/输出单价。

- 绑定快照：`llm_calls.pricing_json` 在写入时复制当时的价目快照（版本、币种、单位、来源、单价），改价不会重算历史；`usage_summary` 因此能同时报出历史与当前版本。
- 未知不入账：缺 usage 或该模型未配置单价时，单次费用未知，不进合计；`cost` 块分别给出 `no_price_calls` 与 `usage_missing_calls`，只有全部调用可计价时才有 `total`，否则只给 `partial_total`（已知部分）与覆盖计数。
- 不跨币种求和：出现多种币种时 `mixed_currency=true`、不给出小计，也不假设汇率。
- 问答级：`llm_calls.turn_key` 把一次提问的调用归组（聊天用 `request:<request_id>` 或每次生成的 turn 键，Agent 用 `run:<run_id>`）；`turns` 给出每次问答的调用数、模型、token、已知/未知调用数与费用，`turn_count` 与调用级合计分开报告。
- 舍入固定：内部 8 位小数（ROUND_HALF_UP），展示 4 位小数并带币种；未配置定价时显示"未配置成本"，不写 0。
- 配置不可用时失败关闭：报 `config_error`，所有费用保持未知，不影响问答本身。

## Agent 只读工具循环（A04.1）

原生 DAG 的审校节点（`app/agents.py` 的 `CriticAgent`）在 `LAW_REVIEW_TOOL_LOOP=on` 时复用 `app/tool_loop.py`，不复制第二套参数校验或出口实现：

- 案件范围由服务端注入（`CriticAgent.case_id`）；模型给出的 `case_id` 只是惰性参数，工具执行器只接受服务端值并自行反查文档归属。
- 只读白名单来自 `app/agent_tools.py`；轮次默认 3（上限 5）、工具调用默认 6（上限 10）、单条工具结果 12k 字符上限，超限返回结构化错误而不是继续放大。
- 工具结果、卷宗正文、历史消息与文件名一律作为不可信数据拼进消息；工具结果带 `data_notice`。
- 端点拒绝 `tools` 参数时抛 `ToolsUnsupportedError`，审校节点显式回退到单次调用并在 `tools.reason` 记录 `endpoint_rejected_tools`。
- 工具循环直接向模型端点发请求，因此**在宿主管控作用域内拒绝运行**（`tool_loop_unavailable_under_host_governance`），不绕过准入账本。
- 轨迹：`tools = {enabled, reason, rounds, tool_calls, trace[{round, tool, ok, latency_ms, count}], model, budget}`，同时进入 `agent_steps` 的 critic 输出与最终结果；工具轮次的用量按每次调用记入 `llm_calls`（`kind=tool_loop`），不再额外记一条 critic 调用。
- LangGraph 路径的 `CriticAgent` 没有案件作用域（构造时不传 `case_id`），因此记录 `case_scope_unavailable` 并保持单次调用；该路径的工具接入留待单独验收，不声称已接入。

## 审校节点的一次性模型回退（A09.1）

`app/model_router.py` 的策略被审校节点复用：主模型先答，**只有**主答案被 `validate_review_answer` 拒绝时才尝试一次回退模型；传输失败走既有失败路径（规则汇总 + 诊断），不再发起第二次模型调用。不引入"低置信"之类的新阈值。

- 回退使用同一案件范围、同一引用校验；回退答案不过校验就回到规则汇总，`rejected_llm_validation` 保留主答案的失败项。
- 每次调用分别记账：主调用记 `critic`，回退记 `critic_fallback`，都带同一个 `run_id`；`model_route = {fallback, reason, model}` 进入 critic 输出、`agent_steps` 与最终结果。
- 半配置或远端未获准的回退端点抛 `FallbackConfigError`，按失败关闭处理并在 `model_route.reason=fallback_misconfigured` 记录原因，不静默退回主模型。
- 回退只做单次调用，不重复工具循环：工具预算不会在另一个端点上再花一遍。
- LangGraph 的审校节点现在把 `case_id` 与 `run_id` 传入 `CriticAgent`，用量与回退调用因此可归属；工具循环在该路径按运行时策略关闭（`tools.reason=runtime_policy`），因为恢复会重新执行 critic 节点，不应再花一轮工具调用。

## 聊天流式协议（A01.1）

`POST /api/cases/{case_id}/chat/stream` 使用 SSE，每帧一个事件。客户端可提交 `request_id`（`[A-Za-z0-9][A-Za-z0-9_-]{7,63}`）作为幂等键。

成功顺序：`status(phase=retrieval)` → `status(phase=generating)`（有模型调用时）→ `token`* → `status(phase=validating)` → `citations` → `final` → `done(status=ok)`。失败顺序：`status` →（可能的 `token`*）→ `retract` → `done(status=failed)`；失败路径不发送 `citations`/`final`，也不在 `retract` 之后发送成功帧。

- `token` 是**未校验草稿**（`provisional: true`），只用于打字机展示；答案正文可能自带 `[资料N]` 标记，与"引用对象延后挂载"是两件事，前者不构成校验结论。
- `citations` 只在整段答案通过引用校验后发出，携带结构化来源；它和 `final.citations` 内容一致。
- `final` 保留为兼容帧：旧桌面客户端靠它渲染完整答案。新客户端应同时处理 `citations` 与 `done`，但不得因为收到 `final` 就跳过 `done` 的终态判断。
- `done` 携带 `status`、`conversation_id`、`message_id`、`answer_status`、`citation_check`、`llm_used`、`ttft_ms`（服务端首个 token 到达时间，无 token 时为 null）、`usage`（上游未返回 usage 时为 null）与 `request_id`。聊天不创建 `agent_runs`，因此 `done` 不含 `run_id`。
- 重放：同一 `request_id` 再次提交且已提交过时，服务端回放 `status(phase=replayed)` → `citations` → `final` → `done(replayed: true)`，不重复调用模型、不重复写消息；重放帧不重新报告 TTFT/usage，逐次用量仍以 `llm_calls` 为准。
- 并发重复：同一 `request_id` 正在处理中时返回 409；中断在提交边界之前的请求标记为 `cancelled`，可以用同一键重试。提交边界是 `record_turn` 的单事务：消息、审计与幂等记录一起提交，所以"取消"与"提交"竞争的结果只会是"整轮写入"或"完全没有写入"。
- 断连：工作线程在模型调用前和提交前检查协作取消标志；已发生的模型调用仍写入 `llm_calls`，不因断连删除账本。底层 HTTP 读取不被强制打断，仍受既有绝对超时约束。

长期记忆、checkpoint、导出与日志都可能含案件信息，不能仅清理上传目录。批量导入和 Web 应用必须使用同一个数据目录；案件级权限应覆盖关联文档、证据、运行轨迹和导出，不能只保护案件列表。
