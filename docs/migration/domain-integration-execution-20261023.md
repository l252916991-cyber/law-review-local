# 领域接入实施记录

日期：2026-10-23（北京时间）。依据：[下一步计划书](domain-integration-plan-20261023.md)。实施、自审和自动化验证为 Codex；未代替专业评审。

## 1. 起点与授权

起点为本地提交 `b5c0718`，工作区干净；实施分支 `codex/domain-integration`。精确框架仍为 0.8.3，正式入口状态按原能力声明。DevSpace 打开工作区仍返回内部错误，使用本地补丁与命令工具。

用户在本次执行中确认：每案最多 500 页、正文共 200 万字符，超限明确拒绝；允许合成材料调用已有本机模型服务，不外发、不付费。未提供真实案件材料授权或专业评审结论。

## 2. D00：来源记录

已修正 `scripts/migration_source_manifest.py` 的固定“无 Git”描述。Git 检出记录 HEAD 和整个工作区是否有未提交/未跟踪内容；内容身份始终来自当前发布允许清单，不能以 HEAD 替代。支持 `.git` 为文件的独立 worktree、尚无提交的仓库、无 Git 的源码包，以及 Git 命令不可用的明确状态。源码包位于其他仓库内部时不继承外层 HEAD。

文件集、SHA-256 规范化算法与 schema v1 保持兼容；旧清单仍按原 tree_digest 校验。来源元数据不记录远端、用户配置或私有文件名，私有数据仍被发布允许清单排除。

验证：4 项新增来源测试加 8 项现有工程工具测试，**12 passed**；`uv run --locked ruff check app tests scripts`、`uv run --locked mypy`（41 个脚本）通过。本项没有重新运行全量业务测试。

## 3. D01：领域恢复设计

已形成[领域接入设计](domain-integration-design-20261023.md)，明确固定领域图、节点结果/回执、无内存依赖的前驱重建、真实引用、复合效果、模型未知用量和版本边界。容量确认已同步 E03。设计核对了当前节点依赖、只读临时快照、SDK 0.8.3 DurableGraph 和六类写入实现；未把设计当作后续实现通过。

## 4. D02：领域写入契约

新增独立 `DomainHostWritePort`：回执 v2、领域步骤角色/成功或失败状态及输入输出、实际非空引用与法条成员核验、复合子效果回执检查。原合成端口仍使用 v1 精确字段和空引用限制。领域引用检查覆盖索引、同案文档/页码、原名称、跳转路径和真实原文片段；法条不只核对自报摘要，还核对当前语料成员。结果包保留 UTF-8 硬上限 180 万字节，不截断来源。

新增 15 项测试，包括已确认 500 页/200 万字符的等于和超限边界、非法引用零写入、语料变更拒绝旧回执、子效果被篡改拒绝以及正常/失败步骤幂等。复用现有语料校验实现，无宿主 SDK 导入。

本机完整离线检查：Python 3.11/3.12/3.14 各 **894 passed / 3 skipped**，覆盖率约 84.9%；实际独立框架解释器已设置，框架进程测试未跳过。3 项跳过仍为两项未检出的冻结法条语料和宿主未安装 mlx_lm。Ruff、Mypy、依赖兼容性、两份 requirements 导出一致性、doctor、compileall、Node 22 语法和发布允许清单通过。证据位于 `output/migration/domain-integration-20261023/d02{,-311,-314}/`。这是本机 macOS 验证，未声称托管 Ubuntu CI 或 HTTP/UI 通过。

## 5. D03：实际领域节点的隔离持久图

新增 `IsolatedDomainIntegration`，独立运行时 `forgeharness-domain-isolated-v1` / 领域图 v1 / wire v3，复用原受理、lease、启动授权、事件、取消及回执机制。首次受理固定原 Planner 路由，原领域专家、检索和规则 Critic 经独立框架调度，显式 specialist join；领域步骤输出与子效果同事务持久化。后续节点逐条核验前驱回执并重建私有快照，完成结果不重算。快照过滤本运行时输出，保留输入记忆所需的历史 FK run。

finish 先在宿主核验引用，再由实际 wheel 的 `BundleCitationVerifier` / 法条 verifier 验证最终输出；不使用合成 `NoopVerifier` 为领域图放行。成功投影保存实际答案及非空引用；无来源会留下明确失败步骤，不写成功消息或记忆。记忆仍为 runtime_draft，缺口仍待核验。新增运行时被排除出旧进程启动时的“可安全重试”标记。另修正只读状态/回执查询的对象构建权限：写入、取消和恢复仍逐次要求 edit。

17 项新增测试与既有宿主/领域写入测试共 **56 passed**。覆盖实际新进程、受理后终止、节点计算后未提交、SQL/回执前终止、提交后丢响应、journal 到 checkpoint 间隙及最终投影前终止；已派发且无回执的三类窗口仍保持 unknown 和预留，不盲重算。还验证审批/重复批准、运行中取消、可选记忆关闭、法条独立来源、跨运行时查询拒绝、前驱结果篡改和同案两个运行互不自我失效。

重新安装精确 wheel 的独立 Python 3.12 环境后，本机完整离线三版各 **911 passed / 3 skipped**：Python 3.11/3.12/3.14 覆盖率分别 84.81% / 84.82% / 84.81%。Ruff、Mypy、compileall、发布允许清单通过；依赖未变化，沿用 D02 同锁文件的导出一致性结果。测试和覆盖率原始报告在 `d03-311/`、`d03-312/`、`d03-314/`；框架新环境为本次证据目录的 `framework-env/`。这是离线领域工程范围，不代表真实材料、真实模型、正式路由或 HTTP/UI 验收。

早期针对性测试发现并修正：新测试的 fixture 导入/令牌长度不完整；明确失败节点缺少可知用量而进入保守 unknown，已按 SDK 嵌套用量语义显式声明零嵌套调用，同时由 SDK 计入一次实际工具调用，并返回不可重试的校验拒绝。没有放宽失败断言或修改 wheel。初次日志保留在 `d03/`。

## 6. D04：本机模型与持久调用治理

新增 `LocalModels`、`BackendGate`、`GovernedRetrievalLedger` 和 `LocalModelDomainIntegration`。生成走实际 SDK model 节点，embedding/reranker 经受控检索回调；端点仅允许字面量 loopback HTTP，禁用代理和重定向，不接受请求正文覆盖服务地址。首次校验实际 config/权重 SHA-256，逐调用检查文件身份，固定温度 0.2、最多 900 生成 token 和提示词版本。模型名不作为权重证明；本机转换权重的上游 revision/可复现转换仍未证实。

schema v17 保存每次调用的稳定身份、派发前准入、请求摘要、实际响应/摘要和服务报告用量。未知响应或缺失 usage 保留预留且不自动重发；已有完整响应可跨新宿主/框架进程恢复。生成最大 40,000 UTF-8 输入字节，检索每次 6,100,000 字节，整次最多 128 次实际后端调用及 2,000 万输入字符，均在调用前检查；SDK 另有逐节点 token/tool/step 上限及原整次 deadline。未把字符数当作实际 token，费用仅限获准的本机无 API 付费范围。非法检索响应不能被旧检索降级吞掉；截断/非法模型响应以实际已知用量拒绝，无成功消息或草稿。

新增 18 项测试：路由/权重/输入/响应篡改拒绝、实际 SDK 嵌套用量累加、未知用量不填零、检索非法返回不伪装降级成功、截断回答拒绝、schema 16 重复迁移保留旧记录；实际进程在模型派发、已收响应和 journal/checkpoint 边界 SIGKILL 后，新进程不重发已派发模型。SDK 0.8.3 将有 usage 的 protocol violation 记账路径要求 `recoverable=True`；此固定 DurableGraph 直接失败，不实现 re-ask，也不修改 wheel。若已收非法响应但在协议拒绝记录前崩溃，仍保守保留 unknown；没有编造未执行证明。

本机实测使用已有 `Qwen3.6-35B-A3B-OptiQ-4bit`、`Qwen3-Embedding-4B-4bit-DWQ`、`bge-reranker-v2-m3-mlx` 的实际字节清单。临时服务仅监听 127.0.0.1:18083，离线运行，不修改项目默认模型。首次 35B 被安全内存守卫 HTTP 507 拒绝，保留该请求 unknown。用户明确授权停止后台视频模型；确认并停止 ComfyUI 模型进程后，35B 成功加载。随后首轮推理达到 900 token 上限且引用格式不符，被正确拒绝；补充独立版本的简洁输出/短引用提示后，新隔离合成运行成功，耗时约 17.88 秒，后端报告总输入 1,981 / 生成 134 token（其中生成模型 616 / 134），共 5 次后端调用。重复恢复零新增后端调用、零重复效果。这是合成工程实测，不是法律结论正确率或专业验收。

原始证据保留在 `d04/real-first/`、`d04/real-after-video-stop/`、`d04/real-bounded/` 和 `local-model/`，包含失败、模型清单和实际响应；均被 Git 排除。离线替身测试的 `measurement_valid=false`，不混入上述实测用量。

最终完整本机离线检查：Python 3.11/3.12/3.14 各 **929 passed / 3 skipped**，覆盖率分别 84.55% / 84.56% / 84.55%，跳过原因与 D03 相同。Ruff、Mypy、doctor、compileall、Node22、依赖兼容性、两份 requirements 导出一致性及发布允许清单通过。原始报告在 `d04-311/`、`d04-312/`、`d04-314/` 和 `d04/`。早期协议测试发现的非法响应用量路径问题保留在 `targeted-third.log`，最终 18 项通过见 `targeted-final.log`。

## 7. D05：受信部署边界与候选领域写入

新增 `CandidateDeployment` / `CandidateDomainIntegration`，使用独立 candidate 运行时、应用身份、图和 wire v5。部署配置来自宿主对象，绑定 SQLite 路径及设备/inode、允许案件集合、工程绝对 timeout、审批策略、配置 revision 和确认容量；每次历史查询、读写/恢复核对文件、当前主体/权限、来源及配置身份。客户端 RunRequest 不能提供部署配置。新建验收库没有 synthetic 标记，复用实际六类领域写契约；旧隔离控制仍拒绝该库，候选也拒绝接管已有 synthetic 标记。没有增加环境开启开关或 HTTP 路由。

公开工厂排他创建新验收库，拒绝覆盖已有文件；`environment=production` 明确拒绝。宿主受信 Python 配置不是对任意现有业务库的验证或授权。更换文件、允许案件外的操作、配置变化、撤权、归档和删除均验证拒绝/清理；删除级联清理调用账本/回执，框架目录经原 lease/outbox 机制回收。

新增 11 项候选测试，包含 **500 页 × 4,000 字符 = 200 万字符的实际离线领域整图成功**，501 页和多 1 字符均在受理前拒绝；实际新宿主/框架进程在受理、模型派发、已收响应和 journal/checkpoint 窗口恢复，调用和业务效果不重复。与原宿主、领域和模型测试共同验证。初次文件替换测试暴露 SQLite 在路径校验前打开损坏文件，已前置文件身份检查，未弱化异常断言。

E12：六动作、非空引用、同事务回执、已确认容量与受信候选准入在合成验收库通过，生产环境启用和真实材料验收未执行。E13：领域/模型的适用正常、故障、取消、审批、权限和生命周期机制已在隔离库验证；未知窗口仍保留对账责任。上述结果不能替代完整生产 E12/E13、G2 或 E14 的放行。

最终针对性测试 **70 passed**；完整本机离线 Python 3.11/3.12/3.14 各 **940 passed / 3 skipped**，覆盖率 84.67% / 84.69% / 84.68%，原跳过原因不变。Ruff、Mypy、compileall、发布允许清单通过，锁文件/依赖未变，导出及 Node22 检查沿用 D04。证据为 `d05/`、`d05-311/`、`d05-312/`、`d05-314/`。

## 8. D06：Qwythos 9B 双对照工具与五题合成对照

已收敛未提交实现：新增 `app/model_comparison.py`、`scripts/compare_local_models.py`、`tests/test_model_comparison.py` 和 `tests/test_local_backends_exclusive.py`；`LocalModels` 增加显式 `exclusive_residency`，每次推理前仅卸载已登记的三个模型角色，未知卸载错误、超时和身份绑定均不降级；`local_model_payload` 统一候选与原生对照的请求构造；`read_json_with_deadline` 兼容实际 oMLX `HTTPError` 响应体。新增 6 项独占驻留测试和原有模型/对照测试合计 **30 passed**；Ruff、Mypy（42 个脚本）通过。

本机身份清单在私有证据 `output/migration/domain-integration-20261023/d06-qwythos9b/local-model/` 重新计算，生成模型固定为 `Qwythos-9B-v2-OptiQ-4bit`，embedding/reranker 分别固定为 `Qwen3-Embedding-4B-4bit-DWQ`、`bge-reranker-v2-m3-mlx`。模型均记录完整顶层文件 SHA-256、字节数和本地身份摘要；上游 revision 与转换可复现性仍标为未证实。临时 oMLX 0.7.0 服务仅监听 `127.0.0.1:18083`，`--memory-guard safe`、`--max-concurrent-requests 1`，未修改项目默认模型，未调用外部或付费推理服务。服务日志观察到生成模型多次加载/卸载，验证开始下一次推理前释放已登记模型；实际生成引擎为 VLM 发现失败后回退的 LLM BatchedEngine，约 6.95GB。

五题合成对照实际完成（原始 `report.json`、逐题比较和服务器日志在私有证据目录）：**常规、矛盾、证据缺口**三题候选均 `succeeded`，原生共享/独立两类对照工程检查通过；**空案件**候选 `failed`、原生未生成有效法律结论，工程检查通过；**无关查询**候选 `succeeded` 并对本案卷宗给出带引用法律结论，未按预期拒绝，`expected_outcome=false`，因此该负例失败并**阻止 G2 通过**。所有对照 `measurement_valid=true`，逐题记录实际后端调用、服务报告用量和调用延迟，无未知用量；源案件身份摘要前后一致，源库报告 `source_unchanged=true`。这只能证明对应推理和工程对照发生，不证明答案法律质量、语义蕴含或真实案件适用性。

**D06 结论**：工程实现与测量有效性通过；负例失败使 G2 不能通过，未改题、未删除失败记录。真实开发材料授权、专业律师终评和 E14 正式路由仍未满足。D07 候选冻结与发布重建待执行。

## 9. D07：最终候选冻结与发布复验

最终代码候选为 D06 提交 `f645fd462cc803791fc10e542605ff1af277c001`；D07 只新增本记录、台账和能力状态说明，不修改业务代码。最终本机离线矩阵以该提交源码为准：

| Python | 结果 | 覆盖率 | 跳过 |
| --- | --- | --- | --- |
| 3.11.15 | **952 passed / 0 failed / 3 skipped** | 84.90% | 2 项冻结法条语料未检出、1 项宿主未安装 `mlx_lm` |
| 3.12.13 | **952 passed / 0 failed / 3 skipped** | 84.92% | 同上 |
| 3.14.6 | **952 passed / 0 failed / 3 skipped** | 84.91% | 同上 |

矩阵使用隔离数据目录和 `pytest-socket` 禁网，运行 `tests/` 与三个工程脚本测试，包含实际独立框架 3.12 进程；原始 JUnit、覆盖率与 pytest 日志在 `d07-final-311/`、`d07-final-312/`、`d07-final-314/`。初次矩阵运行未把本机 Node 路径加入 `PATH`，4 项 mobile UI JavaScript 测试失败；固定 Node 22 路径重跑后三版全过，失败过程未当作通过。静态检查通过 Ruff、Mypy（42 个脚本）、compileall、doctor 和 Node 语法检查；两份 `uv export` 与本仓库 `requirements*.txt` 逐字节一致；发布允许清单为 266 文件，`private_runtime_data_included=false`、`lawbench_included=false`。

发布复验从最终允许清单重新构建私有发布包并实际解压、摘要校验和安装：绑定 ForgeHarness 0.8.3 wheel `1d5880ed2ba1cabcb20b0970a55727bcca7b68f9b4beace70ce7600d58ba7555`、框架源码 `a124764a94b5c519f9efa051b682cae782913028557db24bd8abe54e56ecb6cf`，`scripts/verify_deployment_release.py` 从发布包重建框架环境和契约冒烟，结果为 **PASS**，包含只读领域提取源码冒烟、隔离宿主提取源码冒烟和新宿主进程结果复用。最终发布包路径、文件数、SHA-256 和私有证据清单见 `output/migration/domain-integration-20261023/d07-final/final-identity.json`（勿提交私有证据本体）。

“全新宿主”另行从解压发布包的 `requirements.txt` 按 hash 安装到独立 Python 3.12 环境，再从发布包 `vendor/` 安装独立框架 wheel；对提取源码连续运行两次隔离宿主流程：第一次 `duplicate=false`、第二次 `duplicate=true`，`framework_run_id` 和结果一致，6 个工具调用及效果/回执不重复。该验证覆盖可重复安装、提取源码和跨进程恢复，但不覆盖真实业务数据、真实模型、生产部署或 HTTP/UI。

模型配置身份沿用 D06 私有注册清单：生成模型 `Qwythos-9B-v2-OptiQ-4bit`、embedding `Qwen3-Embedding-4B-4bit-DWQ`、reranker `bge-reranker-v2-m3-mlx` 的完整文件 SHA-256、本地身份摘要、温度 0.2、最多 900 token 和 oMLX 独占驻留策略；分类账、报告与源库最终摘要保留在私有证据目录。发布验证不重新下载、不替换、不降低模型或内存守卫。

## 10. 结论、能力声明与遗留

**D07 结论：离线工程与发布重建通过，最终候选可复现。** D06 的无关查询负例仍失败，因此 **E07/G2 不通过**；本批不把合成题结果、工程加载或发布重建当作法律质量或业务验收。正式产品能力声明不变：真实领域入口仍走原路径，未新增 HTTP 路由或环境开关；E12/E13 生产全范围、专业律师终评和 E14 正式接线仍未完成。

E14 的硬前置逐项状态：E07/G2 未通过；E11/G3 框架工程适用项已完成；E13 仅隔离合成机制已验证，生产恢复未验收；生产整次绝对超时仍需在 E14 前确认。因此不得开始正式路由或业务切换。

D07 交付提交哈希、工作区状态和逐项通过/失败/跳过将在提交后报告；私有原始证据、模型权重、数据库和服务器日志继续保存在 Git 忽略的 `output/migration/domain-integration-20261023/`，公开文档只保留上述摘要。

## 2026-10-24 后续状态

[G2 修复实施记录](g2-remediation-execution-20261024.md)记录了本次候选 v2、拒绝持久化与新进程恢复、冻结原生对照及发布验证。最终 9B 原五题均符合预期；19 题中 17 题通过，仍有非法 JSON、正例误拒与单题用量超限。以上 D06/D07 是历史批次，不覆盖新结论；E07/G2 继续不通过，正式入口未启用。
