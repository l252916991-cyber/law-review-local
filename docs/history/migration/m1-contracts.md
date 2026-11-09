# M1 公共接入契约（2026-10-21 冻结；同日按状态审查补强至协议 v2）

2026-10-23 后续：七项读端口与只读控制面见 [E04](e04-host-ports-20261022.md)；当前共享执行器候选、法条来源契约 v1 和离线模型测试见[下一批实施记录](next-batch-execution-20261023.md)。下文 §6 的 0.6.0 为当时绑定的历史记录，当前实际发行物以 `vendor/FRAMEWORK_BINDING.json` 及新验收证据为准；宿主协议/事件/结果仍分别为 v2/v2/v1。

依据：[迁移计划书 §4.1/§5 M1](../../architecture/forgeharness-migration-plan-20261021.md)、[现状检查与改进计划书](status-and-improvement-plan-20261021.md)（A1/A2/A3 补强）、[迁移台账](ledger.md)。

机器可读的权威定义是 [app/integration/runtime_ports.py](../../../app/integration/runtime_ports.py)（纯标准库、零业务/零框架导入，由 `tests/test_runtime_ports.py` 的 AST 边界测试强制）；本文档记录字段与旧行为的对应关系、语义规则和 M1 边界。两者不一致时以代码为准并修订本文档。

**补强摘要（状态审查 I-01/I-02/I-03，2026-10-21 当日）**：`RUNTIME_PROTOCOL_VERSION` 1→2（actor 与认证主体强校验、租户显式拒绝、幂等重放需规范摘要一致）；`RUN_EVENT_VERSION` 1→2（事件明确为快照投影，身份 = (run_id, trace_revision, seq)）；schema v15 为 `runtime_requests` 增加 `request_digest`/`principal_name` 绑定列；`RUN_RESULT_VERSION` 保持 1（结果语义未变）。详见 §10。

## 1. 契约清单与实现状态

| 契约（计划书 §4.1） | 定义 | M1 实现 | 后续 |
| --- | --- | --- | --- |
| RunRequest | `runtime_ports.RunRequest` | 校验 + 幂等受理（[runtime_adapter.py](../../../app/integration/runtime_adapter.py)） | M2 跨进程编码 |
| HostReadPort | `runtime_ports.HostReadPort` | 仅冻结接口（字段对照现有只读工具/检索） | M2 授权导出/读端口实现 |
| HostWritePort | `runtime_ports.HostWritePort` | 仅冻结接口（动作枚举 + 回执） | M2/M4 宿主写入实现 |
| RunEvent | `runtime_ports.RunEvent` | 从 agent_steps 幂等投影 | M2 对接框架 trace 事件 |
| RunResult | `runtime_ports.RunResult` | 与旧结果 dict 无损互转 | 全程保持 |
| RuntimeControl | `runtime_ports.RuntimeControl` | `LegacyRuntimeControl`（旧运行时兼容接口） | M2 ForgeHarness 只读实现；M4 完整接入 |

版本号：`RUNTIME_PROTOCOL_VERSION = 2`、`RUN_EVENT_VERSION = 2`、`RUN_RESULT_VERSION = 1`；摘要方案 `REQUEST_DIGEST_SCHEME = "lexvault-request-binding/1"`、`TRACE_REVISION_SCHEME = "lexvault-trace-revision/1"`；框架侧对照 `forgeharness.sdk.API_VERSION = 1`（锁定发行物内核对，见 §6）。

## 2. RunRequest 字段 ↔ 旧语义

| 字段 | 旧行为锚点 | 语义 |
| --- | --- | --- |
| `request_id` | 新增（幂等键，语法同框架 TaskId `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`） | 相同 request_id 重试不得创建第二次业务运行 |
| `scope.case_id` / `scope.tenant_id` | `require_case_access`； LexVault 单租户，`tenant_id` 默认 `lexvault-local`，为 M2 框架 RunContext 映射预留 | 每次调用重新检查当前授权；**不支持的 tenant 显式拒绝**（`ContractValidationError`），不接受看似有效但未隔离的租户字段（能力表 `supported_tenants` 声明） |
| `actor` | `security.current_principal()`（宿主断言身份） | **v2 起适配器逐字段校验断言与实时认证主体一致（name/admin/local），不一致抛 `IdentityMismatchError`**；权限标志永不取自载荷；跨进程接入（M2+）必须由宿主核验凭据 |
| `question` | `ReviewJobRequest.question`（1..3000 字符） | 同旧边界 |
| `runtime` | `agent-chat` 的 `mode`：`multi_agent`→`native`、`langgraph`→`langgraph` | M1 只受理 native/langgraph；`forgeharness` 值保留给 M2，当前显式拒绝 |
| `execution_mode` | 同步=`agent-chat`；后台=`agent-jobs` | M1 仅 `sync`；`background` 显式抛 `UnsupportedExecutionModeError`（M4 接线） |
| `user_name` / `persist_memory` / `memory_snapshot` | `process_query` 同名参数；`user_name` 对应旧路由的 `actor_name()` 归一化 | `user_name` 是**客户端展示名**：token 模式被归一化为认证主体名、local 模式保留（与旧 agent-jobs 行为一致），归一化后的有效作者进入记忆归属并参与绑定摘要；`persist_memory=False` 仍只抑制 memories 写入（§5.2 约束不变） |
| `use_remote_embeddings` | 协调器 `prefer_remote_embeddings` | 语义不变 |
| `model` | `LLMConfig` + 环境变量覆盖 | M1 仅传输 `use_llm`；`model`/`timeout_seconds` 记录但不生效（宿主配置仍拥有模型选择），能力声明如实标注 |
| `budget` | 旧运行时无预算 | 字段与框架 RunBudget 对齐；M1 不执行（`budget_enforcement=False`），值不被静默丢弃 |
| `protocol_version` / `app_version` / `framework_version` / `data_snapshot_version` | 新增 | 版本不匹配显式拒绝（`ProtocolVersionError`），不静默翻译 |

## 3. RunResult / RunEvent ↔ 旧结果语义

- `RunResult.from_legacy(dict)` 将 `process_query` 返回值（native：[agents.py 返回块](../../../app/review/agents.py)；langgraph：`_complete`）拆为类型化字段 + `extra`；`to_legacy()` 精确还原原 dict（含 `agent_type`、`tools_used`、`memory_hits`、`mode`、`user_name`、`checkpoint_size_bytes`、`conversation_id` 及未来新增键），两个测试固定了逐字节往返。
- 类型化字段即计划书 §4.1 RunResult 行：answer、citations、route、plan、steps、retrieval_metrics、validation、llm_used、fallback_reason、runtime、resumable（+ resume_count、checkpoint_thread_id、total_ms、memory_persisted、citation_check、llm_model、run_id）。
- `RunEvent` 是 agent_steps 的**快照投影**（事件版本 2，状态审查 A3）：旧运行时的步骤行是可变的——LangGraph 恢复会把失败节点原地改写为完成——因此 `seq`（按 `agent_steps.id ASC` 单调）**不是不可变的追加事件身份**。事件身份 = `(run_id, trace_revision, seq)`：消费者必须按 `RunTrace.trace_revision` 缓存与更新（修订变化→整份投影替换），不得把旧快照的事件当终态；同一修订内重复读取幂等且跨进程/重启确定。`node_id`=node_name、`usage=None`（旧运行时不按节点归因 token，未知保持未知）。`trace_revision` 由 `compute_trace_revision()` 对运行行与全部步骤行做内容寻址（方案 `lexvault-trace-revision/1`）。框架侧（M2+）提供持久追加事件流；两种语义的映射由适配层文档化，若未来统一为追加事件需先落持久事件表与兼容投影，单独升版本号不解决漏更新。
- `RunTrace.raw_steps` 保留旧 step dict 原样，保证现有 UI 载荷兼容；事件是并行提供的版本化投影，不替换旧字段。

## 4. RuntimeControl ↔ 现有 API 映射（M4 接线依据）

| 契约方法 | 现有入口 | M1 行为 | M4 HTTP 错误映射 |
| --- | --- | --- | --- |
| `start(RunRequest)` | `POST /api/cases/{id}/agent-chat`（同步路径同一协调器代码） | 幂等受理 + 同步执行 + 映射持久化 | ProtocolVersion/ContractValidation→422；UnsupportedExecutionMode→501；AccessDenied→404 |
| `inspect(run_id)` | `GET /api/agent-runs/{run_id}` | RunTrace + 事件投影 | RunNotFound/AccessDenied→404 |
| `resume(run_id)` | `POST /api/agent-runs/{run_id}/resume` | 仅 langgraph；native 抛 `ResumeNotSupportedError`；前置校验失败→`ResumeConflictError`（旧 ValueError 语义） | 409（含能力详情） |
| `status(job_id)` | `GET /api/agent-jobs/{job_id}` | review_jobs 行投影，interrupted 合成提示与旧路由一致 | RunNotFound→404 |

能力声明（`RuntimeCapabilities`，实现为 `LEGACY_CAPABILITIES` 常量）：`start_modes={sync}`、`resumable_runtimes={langgraph}`、`background_start=False`、`budget_enforcement=False`、`event_projection=True`、`idempotent_start=True`、`persistent_request_mapping=True`。前端"可恢复"按钮只能由能力声明 + 逐 run 的 `resumable` 驱动（计划书 §6 后台与产品）。

## 5. 幂等、映射与状态所有权（schema v14 + v15）

- 表 `runtime_requests(request_id PK, case_id, runtime, protocol_version, run_id, job_id, created_at, request_digest, principal_name)`（v15 增列 `request_digest`/`principal_name`，`NOT NULL DEFAULT ''`）：**只做关联**，执行状态仍由 agent_runs/review_jobs 单一拥有（计划书 §4.2：不得成为第二套调度状态机）。`run_id` 外键 `ON DELETE SET NULL`；`job_id` 无外键（队列行生命周期独立）。
- **请求绑定摘要（状态审查 A1）**：`request_digest = sha256(lexvault-request-binding/1 规范化 JSON)`，覆盖 tenant/case、**已验证的**认证主体名、归一化作者、runtime、execution_mode、去空白 question、`use_remote_embeddings`、`persist_memory`、`memory_snapshot`、model（use_llm/model/timeout）与 budget 全字段；显式排除 request_id 本身、protocol_version（单独校验拒绝）与描述性版本字段（app/framework/data_snapshot，不影响执行）。摘要绑定的是可信解析后的身份值，绝不绑定客户端原始声明。
- 受理协议：`INSERT OR IGNORE` 抢占 request_id（连同摘要与主体名）→ 抢到者执行；未抢到者读映射并按序判定：
  1. 存量行**无摘要**（v15 前记录）→ `ReconciliationRequiredError`（明确兼容/对账状态，绝不视为任意新请求的成功重放，不泄露旧关联）；
  2. 摘要**不一致** → `RequestConflictError`（同键不同请求：换案件/主体/问题/运行时/关键参数一律拒绝，不执行、不写入，错误消息不含旧关联的 run/案件/状态详情）；
  3. 摘要一致但 `run_id` 为 NULL → `ReconciliationRequiredError`（崩溃窗口，fail-closed，禁止盲重放）；
  4. 摘要一致且已链接 → **先对实际映射 run 的案件重新核验当前授权**（防陈旧/被篡改映射），再返回状态投影（`started_new=False`，不重放执行；完整结果经 `inspect()` 获取）。
- 运行关联通过协调器 `on_run_started` 回调在 agent_runs 行提交后立即持久化（review_jobs 的猴子补丁机制在 M4 接线时移除，本轮未改动其行为）。
- 2026-10-22 复核修正：协调器在 run 提交后调用回调，“未收到回调”不能证明没有落库。只有进入执行之前的构造失败可释放声明；进入执行后通用异常且无关联，保留声明并返回 `needs_reconciliation=True`，同键重试走对账路径 3。已有落库后丢回调用例验证不重复执行。
- 重放还核对实际 run 的案件/运行时/问题；公共控制方法固定数据库作用域。运行与步骤在同一 SQLite 读事务中投影。对账工具和 job 终态收口仍属 M3/M4。
- 回滚兼容：v14/v15 为纯增量（新表/新列带默认值）；旧版本代码（≤v13）打开 v15 库按既有防降级检查拒绝（`refusing downgrade`），回滚需恢复备份——与历史每次版本 bump 行为一致，M6 回滚演练覆盖。文件级回滚基线见 `output/migration/m0-baseline-20261021/pre-change/`。

## 6. 锁定发行物（M1「构建锁定框架包」；同日按状态审查 I-05/I-06 换绑 0.6.0）

**当前绑定（2026-10-21 晚起）**：
- `vendor/forgeharness-0.6.0-py3-none-any.whl`，sha256 `69ce604c51325085ae179917605c9479e423c1d437970758733d4f580e367a16`（sdist `cbea0088…`）
- 构建源码身份 `13d50704639062208558cc6b94670808d071f6ad1c0a6b1f154380d1d458f39c`（420 文件，候选 `reports/release-0.6.0-r1`）；验证 run `20261021T125950Z-8663a4b9`：**87/87 门禁全过、qualified=true**、1127 测试 0 失败（源码+干净副本）、纯分支覆盖 86.74%、依赖审计修复（aiohttp→3.14.5、certifi→2026.10.19、pillow→13.0.0，唯一豁免仍为既有 NLTK FH-019）；business_qualified=false（R6 真实模型评测未做，设计边界）
- 绑定记录：[vendor/FRAMEWORK_BINDING.json](../../../vendor/FRAMEWORK_BINDING.json)；其中 `source`/`verification.run` 等绝对路径仅为**溯源信息**，重建不依赖它们（见下）
- **版本标签冲突已解决**：0.6.0 与此前两个同标 0.5.0 的发行物（电商绑定 `58579893…`、M1 初冻 `8275baba…`）版本区分；电商保持其 0.5.0 绑定至 M7，身份一律以 sha256 指认

**历史（M1 初冻，已换绑）**：0.5.0 wheel `8275baba…`（源码身份 `783e5fbd…`，验证 run `20261021T054049Z-ff445d75` 81/87，6 项可选依赖审计失败未处置）——已被状态审查 I-06 判定不满足正式绑定条件；权威副本保存在框架验证 run 目录，台账 §2 留档。

**B3 可重建交付路径（唯一正式路径）**：精确 wheel + 绑定记录纳入发布允许清单（`scripts/package_release.py` EXACT_FIXTURES，不整包 vendor/、不含私有数据）。重建验证 = `scripts/verify_deployment_release.py`：构建发布包 → 隔离解压 → wheel sha256 对照绑定记录 → 干净框架 venv 安装 → SDK/契约冒烟。2026-10-21 初次结果 **PASS**（211 文件，证据 `output/migration/b3-delivery-20261021/`）。2026-10-22 增加第三个精确文件 `vendor/forgeharness-runtime-requirements.txt`，依赖带 hash 固定并校验清单摘要；重建还实际运行解压源码的合成 M2 对照，见 [M2 记录](m2-engineering-20261022.md)。宿主依赖使用当前环境，框架环境新建。

**覆盖范围声明**：源码清单（`scripts/migration_source_manifest.py`，即发布允许清单口径）覆盖发布源码、精确夹具、vendor 发行物/绑定及核心依赖导出；部署清单 = 同一允许清单产出的发布 zip，二者由同一 `source_files()` 定义。私有运行数据（data/、output/、模型权重、案件材料）均不包含。

- M1 宿主契约与适配器仍不导入 forgeharness（AST 测试强制）。2026-10-22 新增独立解释器中的 `forge_worker.py`，仅显式 M2 工程路径执行；详见 [M2 记录](m2-engineering-20261022.md)。交付清单另增加带 hash 的框架核心依赖导出，宿主进程与框架环境保持独立。

## 7. 宿主端口冻结说明（M2 实现依据）

- `HostReadPort` 方法与现有实现的字段对照：`list_documents`/`list_evidence`/`saved_gap_analysis` ↔ [agent_tools.py](../../../app/review/agent_tools.py) 白名单工具（含 `concise` 截断策略）；`search` ↔ `HybridRetriever.retrieve` 允许字段集（`SearchHit.extra` 携带其余诊断字段）；`read_page` ↔ pages 表 + `text_sha256` 内容身份（与框架 bundle 页身份对齐：document_id + 文档内页号 + 摘要哈希，覆盖多文档同页码场景）；`recall_memories` ↔ `rag.recall_memories`；`search_statutes` ↔ 法条检索族（corpus_version 显式携带）。
- 界限常量冻结：query ≤500 字符、search limit 1..20（默认 6）、目录 limit 1..100（默认 50）、单页文本 ≤20000 字符（与框架 `MAX_PAGE_CHARS` 一致）。
- 每次访问重新检查当前权限；返回内容是未受信任案件数据；M2 试点审计写隔离实验存储（现行 `execute_readonly_tool` 写业务 audit_log，只读对照不得直接使用——计划书 §5.2）。
- `HostWritePort` 动作枚举 = M1 冻结的全集（run_record/step_record/gap_detection/memory_draft/conversation_message/audit_entry），与现有幂等键对齐：agent_steps `ON CONFLICT(run_id,node_name)`、memories `source_run_id` 唯一部分索引、gap_detections `(run_id,gap_type,description)` 唯一部分索引。回执含 `duplicate` 标志；未知结果先 `query_receipt` 再决定重试，禁止盲重放。新动作需要契约版本说明，不允许临时载荷。

## 8. 版本化规则（冻结）

- 递增式兼容变更（新增可选字段、新增枚举值且旧值语义不变）：不升版本，文档登记。
- 破坏性变更（字段语义/必填性/顺序保证变化、事件序号语义变化、动作枚举删除）：升对应版本号（协议/事件/结果独立），并在台账登记兼容矩阵；实现必须显式拒绝旧版本（`ProtocolVersionError`），不静默翻译。
- 双轨期规则：宿主 run 的历史读取按保存的 `runtime`/`protocol_version` 处理；框架侧 SDK/事件/持久状态版本随发行物冻结（`FRAMEWORK_BINDING.json`）。

版本登记簿：

| 日期 | 变更 | 版本动作 |
| --- | --- | --- |
| 2026-10-21（M1 初冻） | 六项契约首次固化 | PROTOCOL=1、EVENT=1、RESULT=1 |
| 2026-10-21（状态审查补强） | actor 强校验 + 租户拒绝 + 摘要绑定重放（受理语义收紧）；RunTrace 新增 `trace_revision`（可选字段，附加式）；事件去重身份改为 (run_id, trace_revision, seq) | **PROTOCOL 1→2、EVENT 1→2**；RESULT 不变；新摘要方案均为 /1 首版；schema v15 增量列；v1 存量行经"无摘要对账态"兼容，不静默翻译 |

## 9. M1 退出条件核对

| 退出条件（计划书 §5） | 结果 |
| --- | --- |
| 旧流程回归通过 | 全量离线套件（CI 同参数 + node v24.21.0）：**725 passed / 3 failed / 3 skipped**，3 个失败为 M0 已登记既有缺口（缺失冻结实验配置 `benchmarks/statutory-experiment-v1.json`），无新增失败；覆盖率 **87.01%**（门槛 70%，基线 86.31%）。ruff / mypy（38 文件）/ compileall / node --check（4/4）/ 发布清单 dry-run 全过。新增 43 项契约/适配器测试全绿，两个新模块覆盖 99%。证据：`output/migration/m1-regression-20261021/final-verification.log` |
| 安装不依赖开发目录 | §6 干净 venv 安装验证通过 |
| 核心不导入具体业务 | `runtime_ports.py` 仅标准库导入（AST 测试）；`runtime_adapter.py` 导入面白名单（app/fastapi/标准库）且不导入 forgeharness（AST 测试） |

## 10. 状态审查补强记录（A 批次，2026-10-21）

对应 [现状检查与改进计划书 §3/§4.A](status-and-improvement-plan-20261021.md) 的 I-01/I-02/I-03：

| 问题 | 修复 | 验收测试（tests/test_runtime_adapter.py、tests/test_runtime_ports.py） |
| --- | --- | --- |
| I-01 重复请求缺少原请求绑定 | v15 绑定列 + `compute_request_digest` 规范化摘要；重放四态判定（无摘要对账/冲突拒绝/崩溃窗口对账/同请求重放）；返回旧关联前对**实际映射 run 的案件**重核验授权；冲突消息零泄露 | 跨案、换问题/运行时/use_llm/persist_memory/作者/embeddings、换主体 → `RequestConflictError` 且不泄露 run/状态；合法重试（含空白归一化）仅一个业务运行；撤权后重放 → `AccessDeniedError`；篡改映射指向他案 run → `AccessDeniedError`；v14 无摘要行 → 对账态；崩溃窗口 → 对账态；并发同键仅一次执行 |
| I-02 可信身份未落实 | `_resolve_identity`：actor 断言与实时 `current_principal()` 逐字段比对（name/admin/local），不一致 → `IdentityMismatchError`；作者经 `actor_name()` 归一化后进入协调器与摘要；token 模式无主体（后台身份丢失）→ `AccessDeniedError`，不回退匿名身份；租户白名单显式拒绝 | 伪造姓名/提权标志拒绝且零执行零映射；token 模式记忆作者=主体名而非展示名；错误租户拒绝；`current_actor_identity()` 助手保证断言来自认证上下文 |
| I-03 事件去重与可变节点冲突 | `RunTrace.trace_revision` 内容寻址修订号 + 事件版本 2 快照语义（§3）；消费者规则：按 (run_id, revision) 缓存、修订变化整份替换 | 失败→首读→重启重读（确定性）→恢复→再读：修订变化、seq 集合不变、memory 节点 failed→completed、修订键控消费者终态为完成、无重复副作用（run 行唯一、记忆 ≤1、疏漏结构化去重）；原生完成态修订稳定 |

补强后全量验证数字见台账变更记录；本轮未接线 HTTP/UI（I-07 属 M4），未改动 review_jobs 行为。
