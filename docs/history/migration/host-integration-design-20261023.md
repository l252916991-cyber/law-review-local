# H01：隔离宿主写入与恢复设计

日期：2026-10-23（北京时间）。实现、自审、自动化验证：Codex；没有独立复核人或专业评审人。本设计落实[执行计划](host-integration-plan-20261023.md)允许的合成隔离原型，生产容量未获确认，不能作为 E12 生产全验收或 E13/E14 正式接入的放行依据。

## 1. 边界与版本

- 使用现有 `WriteAction`、`WriteRequest`、`WriteReceipt`、`HostWritePort` 六类动作，公共运行协议/历史快照事件/结果仍为 v2/v2/v1。
- 新实现为 `app/integration/host_writes.py::LocalHostWritePort`，仅接受通过 `create_isolated_database` 排他新建并标记的合成数据库。现有库升级不会自动获得隔离标记，没有开启真实库写入的配置开关。
- 隔离控制为 `app/integration/host_integration.py::IsolatedHostIntegration`，运行时 `forgeharness-isolated-v1`，显式合成图 `synthetic-host-writes-v1`，宿主写入 schema v1，独立往返协议 v2，宿主持久追加事件 v1。它不是正式 `RuntimeControl` 路由；`FrameworkReadonlyControl` 的原能力声明保持不变。
- 框架仍为精确 0.8.3 wheel，SHA-256 `1d5880ed2ba1cabcb20b0970a55727bcca7b68f9b4beace70ce7600d58ba7555`。独立 Python 3.12 只导入安装包；宿主不导入 ForgeHarness。
- 新 schema v16 增量迁移只添加表、约束与删除 outbox。v15 旧代码按既有防降级规则拒绝 v16；不会自动降级库。v14/v15 历史请求列继续保留原含义及未绑定身份的拒绝逻辑。

## 2. 动作字段、权限与原子性

所有动作均要求当前主体具有案件 `view`、`edit` 权限。每次调用重新核对主体、案件及权限；token/OIDC 模式重新解析当前配置，拒绝撤权、主体变化及有歧义的同名配置。查询回执要求当前 `view` 和案件授权，并核对原主体绑定，不返回其他主体或案件的旧内容。

| 动作 | 精确合法字段 | 业务效果 |
| --- | --- | --- |
| `run_record` | `question` | 新建一条隔离 `agent_runs`；运行状态由框架事件投影 |
| `step_record` | `run_id,node_name,output` | 一条完成的合成步骤；run/node 唯一约束 |
| `gap_detection` | `run_id,gap_type,severity,description,suggestion,affected_evidence_ids` | 同案证据校验后的待核验缺口，不修改证据审批状态 |
| `memory_draft` | `run_id,content,importance` | `runtime_draft` 草稿；已有检索只召回 `agent_draft_validated`，不把本草稿作为已验证事实 |
| `conversation_message` | `conversation_id,role,content,citations` | 同案、未归档会话的一条 user/assistant 消息；合成原型只接受空引用列表 |
| `audit_entry` | `detail` | 当前主体的一条隔离业务审计 |

不允许附加字段、SQL、任意表名或非有限 JSON。单次写入序列化上限 20 万字节。关联 run 必须属于同案隔离运行时，关联证据和会话必须属于同案。工程材料界限为默认 500 页、200 万正文字符、单页 20000 字符；超限拒绝，未实现分批身份或汇总。

`BEGIN IMMEDIATE` 内完成当前输入版本核验、唯一键查找、业务效果与回执插入。回执表唯一约束 `(action,idempotency_key)`，绑定摘要还覆盖可信主体摘要、tenant/case、动作、source_version、规范化 payload 和写入 schema。全局键碰撞到其他主体/案件时只报冲突。相同内容重复提交复用 receipt_id；换内容或源版本拒绝。

回执 detail 为版本化 JSON，包含效果表、行 ID 和效果摘要；不传递原案件正文。查询/重复执行重新核验效果仍存在且未被修改；run 只绑定不可变身份字段，允许受信框架投影更新状态。消息所属会话归档后不再返回原成功。业务效果与回执提交失败时一起回滚，不依赖进程内去重集合。

输入版本覆盖案件、文档、原页、证据、证据关系、输入记忆和合成知识行；排除当前运行的步骤、消息、缺口、审计和 `runtime_draft`，避免自己的输出使后续写入失效。实际外部法条语料、模型 revision 和领域节点仍由只读链路/后续实际适配绑定，本合成图没有声称消费它们。

## 3. 受理、配额与故障事实

同一宿主事务创建 run 回执、唯一会话、`runtime_requests` 和 `host_runtime_links`；请求绑定继续使用现有规范化摘要及可信主体。job ID 为该隔离请求 ID，host run 和稳定 framework run ID 持久关联。这里没有接入正式 `review_jobs` 执行器或 HTTP 后台队列。

准入按数据库中非终态映射计数，上限 4；运行进程用数据库所属目录的 OS lease 限制为 2，并对同一请求加独占锁。进程退出自动释放执行 lease，重启不重置未终结请求的准入责任。终态计数直接来自持久投影，没有重复释放信号量的窗口。取消等待中的、尚未开始的请求与 queued→running 共用数据库锁；运行中的取消仅记受理事实，最终停止由框架状态确认，未知效果仍占准入并保留对账责任。

| 故障窗口 | 持久事实与处理 |
| --- | --- |
| 受理事务未提交 | 全部回滚；没有半个 run/会话/映射 |
| 受理已提交、响应丢失 | 按原请求重读同一映射，不再建 run/会话 |
| 框架首次启动授权已提交、checkpoint 未可查 | `start_authorized` 保留不确定性；拒绝再次创建框架运行 |
| 框架 run 已建、宿主结果投影未到达 | 稳定 framework run ID + SDK find/inspect/resume 查原运行 |
| 业务效果及回执未提交 | 同宿主事务回滚；如框架已派发，单纯没有回执不足以证明可重试，继续待对账 |
| 回执已提交、响应丢失 | 新框架进程将调用标为 unknown，经真实宿主 resolver 查询原回执，按固定无模型工具的实际一次调用用量对账 |
| 工具 journal 已提交、节点 checkpoint 未保存 | 框架复用 journal 结果；宿主不调度第二次业务动作 |
| 全部框架事实已完成、宿主事件投影丢失 | 重读原框架 trace 和结果，投影去重后收敛 |
| 框架文件丢失但已有启动授权 | 明确 unknown；不凭空重建运行或重置用量 |

框架图包含工具分支及显式 `joined` 节点，框架负责依赖、join、用量预留和审批。普通内部保存无需业务批准；可选的合成 audit 执行审批仅用于框架审批恢复验证，不改变证据确认语义。完整动作列表摘要进入图 resource_versions，待审批动作变更会改变图身份，原运行/批准不能复用。

## 4. 往返、投影与恢复

受信 sidecar 用单次随机 token、严格递增 RPC 序号和 200 万字节报文边界通信，消息种类只有 validate/start/write/receipt/result。宿主选择保存过的动作和 payload，校验框架调用键与固定 SDK 命名空间、run、node 相符，不接受 pipe 中的 SQL、身份或数据库路径。进程仅能访问部署给它的受信代码和自身私有状态；这是进程/依赖隔离，未宣称 OS 沙箱。

恢复先重新核验主体、授权、资料版本、会话状态、框架/wheel/协议/图版本。首次创建框架状态前必须取得一次性持久启动授权；后续先 find，已存在只 inspect/resume/reconcile。unknown 节点 resolver 只查询真实宿主回执，不执行动作；不存在回执时保持 unknown，不能伪造“未执行”证明。

受理时保存绝对 UTC deadline；排队、审批等待和重启不更新它。宿主业务写入在同一事务中检查取消与原 deadline。排队已超时且未派发时直接 exhausted；框架晚到成功按未知结果对账，不能显示为及时成功。sidecar 清理有有限 pipe 时限，finally kill/wait/close，不拿清理时限刷新业务预算。框架自身继续核对保存的 run deadline、预算、审批和执行身份。

追加事件取自实际框架 trace 的 `orchestration.transition`，使用 immutable event_id、revision 和独立 host_event_version=1。事务内存储、去重并按连续序号推进 cursor；同序号/同事件 ID 换内容拒绝，缺事件不释放准入，终态不回退。公共 RunEvent v2 仍是旧运行快照修订协议，未改成时间序列。`rebuild_projection` 只调用实际框架 inspect 后重建，不执行业务工具。

## 5. 生命周期与后续接入

案件归档后禁止新写；归档/资料变化会使原版本成功失效。删除案件通过外键级联清除映射、事件、回执和业务效果；删除触发器留下仅含 request/framework ID 的清理 outbox。管理员显式排空时，先取得 worker lease 再删除该请求的独占合成框架目录；活跃 worker 时保留待办，重复清理幂等，拒绝符号链接和非固定格式路径。

本清理只针对每个请求独占的隔离原型目录，未替代生产保留策略或 E18 备份、schema 兼容回退演练。正式域图、真实后端/费用治理、正式后台和产品路由需分别继续 E05/E07、E12/E13 完整验收及 E14。不得直接用本合成图替换法律审查流程。
