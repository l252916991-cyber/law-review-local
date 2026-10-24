# 系统架构

Web/worker 与模型服务器分别运行。应用仅使用 OpenAI-compatible HTTP 接口，不启用 LangSmith 或云端 Agent 服务。LangGraph 的传递依赖仍会安装 LangChain Core/LangSmith 客户端包；安装客户端不代表启用云端追踪，部署时不要打开相关 tracing 环境开关。

```text
Browser → FastAPI → business SQLite (documents/pages/evidence/audit/runs/memory)
               ├→ shared Agents → native Python DAG (default)
               ├→ shared Agents → LangGraph → independent SQLite checkpoint
               ├→ local LLM / embedding HTTP server
               └→ Redis queue → arq worker → document extraction/indexing
```

业务表保存可展示的审计与结果；checkpoint 保存图状态和待执行节点。LangGraph 线程以运行 ID 隔离，恢复仍使用原线程；节点副作用需要业务层幂等保护。SQLite 适合当前单机原型，不宣称多实例高并发运行能力。

FTS5/BM25、法律词项和本地向量经 RRF 融合；生产检索随后按问题信号做确定性重排，并对高分命中补充同文档邻页、限制单一文档最多占结果的 60%。该限制按相关性顺序执行，允许同文档多个关键页入选，不强制先为每份文档分配一页。精确人名、金额、日期、法条号和证据类型优先保留词法命中，开放式事实问题保留语义召回。无真实命中时不再把数据库前几页当作证据。结果附带来源多样性、直接命中数、邻页数和置信度诊断；低置信度只表示需要律师核验，不代表自动拒答或事实成立。向量缓存有效性应包含内容、模型身份、后端、维度与预处理版本；仅维度相同不能证明向量空间一致。模型不可用时有显式降级，降级结果不可计作成功模型性能样本。

`LAW_REVIEW_EMBEDDING_REVISION` 是同名模型权重变更时的显式版本标识；替换权重后必须修改它，使缓存失效重建。服务地址变更也应确认实际模型身份。旧版 `legacy` 记忆保留历史记录但不自动召回；通过当前格式校验的 `draft` 仍是未经过律师确认的草稿，不能升级为事实依据。

引用格式有效与证据支持事实是两个指标。最终答案、来源文档、页码、节点输出与耗时供律师复核，不保存/展示模型隐藏思维链。自动金额/疏漏规则只是线索，不是法律事实裁定。

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
