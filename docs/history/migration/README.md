# 过程文档归档索引（2026-10-21—11-01）

> 2026-11-01 起，`docs/migration/` 下的计划书、实施记录与证据整体移入本目录，内容不改（仅在三份求职展示记录顶部加了 2026-11-01 状态更新说明）。本页是原文档总入口的完整保留版，现行入口见 [docs/README.md](../../README.md)。

以代码、测试产物与以下维护文档为准；根目录早期报告保留为历史证据，不作为“全部完成”的实时声明。

- [2026-10-29—30 律所评测缺漏与标注交接](lexvault-evaluation-completion-20261029.md)：48题/8案新封存草稿、双人盲标注包、两题各五次真实生成、40次实际故障注入、成本导出与统计边界；人工标注和总体效力尚未完成。
- [律所评测样本分母审计](../../benchmarks/lexvault-evaluation-sample-audit-20261029.md)：区分问题/案件/claim和重复采样，纠正小类别、不同分母差值及旧导出缺usage的结论。

- [系统架构](../../architecture/system.md)：运行时、数据边界与降级。
- [威胁模型](../../security/threat-model.md)：资产、信任边界、提示注入与工具越权的缓解及未覆盖项。
- [2026-10-27 求职展示版计划书](agent-portfolio-plan-20261027.md)：面向 AI 应用 / Agent 工程求职，推进多专家真实模型对照、可复现演示和简历证据；与正式交付验收分别记录。
- [模型层拆分：provider 与 wire api](model-ports-wire-api-20261030.md)：`app/model_ports.py` 的协议/凭证分层、`anthropic-messages` 接入、native 基线 `specialist-review-v4` 重登记与本机验证结果。
- [三组对照实验协议（冻结 v1）](portfolio-protocol-20261027.md)：组别定义、不变项、数据与代码身份、预算与停止规则、采样轮换、评分与失败分类、分析与盲审规则、变更控制。
- [求职展示版阶段记录](portfolio-p0-scope-evidence-20261027.md)：P0 身份与证据冻结、[P1](portfolio-p1-ablation-driver-20261027.md) A/B/C 隔离驱动与回归、[P2](portfolio-p2-precheck-blocked-20261027.md) 模型预检阻塞、[P3](portfolio-p3-demo-status-20261027.md) 恢复片段、[P4](portfolio-p4-reproducible-delivery-20261027.md) 新目录重建；每份含命令、实测值与未覆盖项。
- [项目说明](../../portfolio/project-brief.md)、[面试材料](../../portfolio/interview-notes.md)、[简历条目](../../portfolio/resume-bullets.md)：一页架构、运行步骤、证据索引、8 个问答与待测占位。
- [求职展示版 P5 交付记录](portfolio-p5-materials-20261027.md)：材料清单、数字可追溯性核对、不写的表述、按依赖排序的待办。
- [求职展示版 P2 前置：六案例保留集](portfolio-p2-holdout-20261027.md)：24 题冻结身份、案例隔离方式、已验/未验清单与复算命令。
- [对照批次可复现性修正](portfolio-determinism-20261027.md)：专家发现顺序的集合迭代缺陷、`specialist-review-v3` 重登记与跨进程复验。
- [三组账本配对分析](portfolio-p2-analysis-20261027.md)：同题配对、精确 McNemar、分类/分案例、轮次波动与案例级重采样。
- [三组盲审包](portfolio-p2-blind-package-20261027.md)：隐藏组别的 X1–X3 呈现、分层抽样与案例覆盖、替身演练与裁判侧映射校验。
- [Judge 评分结果](portfolio-p2-judge-results-20261028.md)：同模型 Judge 的三组结果、与规则一致率、空答案误判与两次传输修正。
- [保留集三组结果](portfolio-p2-holdout-results-20261028.md)：六案例 24 题 × 3 轮实测、配对区间与三轮波动。
- [开发集三组结果](portfolio-p2-dev-results-20261027.md)：Qwen3.8-27B 上 60 题 A/B/C 实测（B 退步、C 打平且更贵）、配对差异、成本与盲审包摘要。
- [模型切换与评分 v2](portfolio-p2-model-switch-20261027.md)：本机 Qwen3.8-27B 身份、预检两轮结果、评分口径修正与开发集开跑记录。
- [驱动链路端到端演练](portfolio-p2-harness-drill-20261027.md)：用仓库替身服务跑通 prepare→run→analyse，记录暴露的三个问题与修复，明确非模型结果。
- [求职展示版 P3 演示状态](portfolio-p3-demo-status-20261027.md)：恢复片段证据、录制前身份核对报告（`scripts/demo_identity_report.py`）与主演示的阻塞条件。
- [2026-10-27 多人协作首版改进计划书](collaboration-first-release-plan-20261027.md)：成员分工、人工任务、并发保护、报告签发与部署验收的工作包、阶段与验收用例。
- [2026-10-27 多人协作首版交付前收尾计划书](collaboration-release-readiness-plan-20261027.md)：权限交集、冻结签发内容及 R1–R3 实施记录，保留完整检查和验收用例。
- [2026-10-27 G1 放行与律所验收续行计划书](collaboration-g1-to-acceptance-plan-20261027.md)：G1、M1、U1 原计划及状态入口；本机五人实际执行结果见下方 2026-10-29 记录。
- [2026-10-28 G1 续行执行证据](collaboration-g1-execution-evidence-20261028.md)：新冻结法条、双版本完整检查、gitleaks、指定基础镜像容器、真实队列/OCR 及含前端候选包。
- [2026-10-28 M1 源库登记与执行单](collaboration-m1-preparation-20261028.md)：历史准备记录、候选路径和停写后副本演练步骤；本机旧库副本实测见 2026-10-29 记录，源库未正式升级。
- [求职展示：合成迁移与恢复](../../runbook/migration-demo.md)：复现命令、工程取舍与五分钟讲解顺序；[2026-10-28 实测记录](collaboration-m1-synthetic-demo-20261028.md)覆盖 schema 16→26、checkpoint、双身份签发及二次恢复交付。
- [2026-10-28—29 落地缺陷修补记录](collaboration-production-fixes-20261028.md)：七项已确认缺陷的六个修复提交、双版本完整离线矩阵、28 项页面回归、真实 Redis 恢复及新候选包；真实律所接收条件继续保留。
- [2026-10-29 本机五人技术试点验收](local-five-user-acceptance-20261029.md)：旧库 v13→26 副本迁移及旧程序回退、五身份流程、容量/OCR/真实模型固定事实检查和双版本工程门禁；保留模型失败、法律措辞及值守接收边界。
- [2026-10-28 法条恢复与冻结输入](../../benchmarks/collaboration-corpus-recovery-20261028.md)：五份官方历史文本、71 份独立资产契约及旧基准未复现的边界。
- [多人协作部署与运行验收](../../runbook/collaboration-acceptance.md)：验收脚本、备份恢复演练、五个身份的人工验收、离职撤权与监控建议。
- [AI 应用升级计划](ai-application-upgrade-plan-20261024.md)：A01–A13 工作包、验收口径与未实施原因。
- [AI 应用升级实施记录](ai-application-upgrade-execution-20261024.md)：提交清单、验证结果、关键设计与发现的问题。
- [AI 应用升级收尾计划书](ai-application-upgrade-continuation-plan-20261024.md)：A-E0、A01.1、A02.1–A02.2、A04.1、A06.1、A09.1 与 A11.1–A11.4 的执行顺序、验收与门禁。
- [AI 应用升级证据与环境核对](ai-application-upgrade-evidence-20261025.md)：统计口径更正、本机 Node/框架解释器/模型端点/文档工具的事实与环境缺口。
- [AI 应用升级实施记录（A-E0 至 A06.1）](ai-application-upgrade-execution-20261025.md)：六个收尾包的提交、改动、验证数字、真实缺陷与 A11 阻塞。
- [MCP 接入](../../runbook/mcp.md)：只读案件工具的 stdio 启动、客户端配置与安全边界。
- [2026-10-23 领域接入实施记录](domain-integration-execution-20261023.md)：D00–D07 实际进度、用户确认范围、验证与剩余验收条件。
- [2026-10-23 领域恢复设计](domain-integration-design-20261023.md)：版本化领域图、前驱结果与复合回执、引用、模型用量和正式接入边界。
- [2026-10-23 下一步计划书：真实领域接入](domain-integration-plan-20261023.md)：Git 基线后的 D00–D07 工作包、领域持久图与写契约、模型治理及双对照、验收矩阵和正式接入前置条件。
- [2026-10-23 本地 Git 基线初始化](git-initialization-20261023.md)：已审查的源码范围、仓库级作者、既有验证及本地提交规则。
- [2026-10-23 宿主接入实施记录](host-integration-execution-20261023.md)：六类隔离写入、持久映射、真实进程恢复、合成双对照与未放行边界。
- [2026-10-23 宿主接入设计](host-integration-design-20261023.md)：事务/回执、启动授权、源版本、追加事件、生命周期及 schema v16 兼容。
- [2026-10-23 宿主接入原计划](host-integration-plan-20261023.md)：H00–H06 工作包与原验收要求；已交付范围见实施记录，后续安排见真实领域接入计划。
- [2026-10-23 R2 实施记录](r2-execution-20261023.md)：P00–P07 离线工程、0.8.3 最终候选、43 项 R2 专项、三版宿主复验与后续接入边界。
- [2026-10-23 R2 持久编排执行计划书](r2-execution-plan-20261023.md)：E09–E11 的原计划快照；本批实际完成状态以实施记录为准。
- [2026-10-23 下一批实施记录](next-batch-execution-20261023.md)：共享执行器、案件/法条来源、大页包边界、离线模型治理及最终候选验收状态。
- [2026-10-23 下一批执行计划书](next-batch-plan-20261023.md)：E01–E04 后续，E08 共享执行器主线、E06 并行、E05 受控接入及本批验收与外部条件。
- [2026-10-22 统一框架替换执行计划书](execution-plan-20261022.md)：M0–M7 剩余任务、编号与依赖、责任分工、验收操作、切换回退和全项目交付清单。
- [2026-10-22 E01 项目与 AI 入口总表](e01-entry-inventory-20261022.md)：两应用全部 AI 入口、目标治理方式、旁路登记与未决项目。
- [2026-10-22 E02 摘要/发行物/兼容矩阵](e02-baseline-compatibility-20261022.md)：两侧源码与依赖摘要、旧发行物登记、数据 schema 兼容与可重建性、Git 来源与授权状态。
- [2026-10-22 E03 验收参数冻结表](e03-acceptance-parameters-20261022.md)：数据集、规模、并发、超时、模型、预算、评分与观察门槛，及待负责人确认项。
- [2026-10-22 E04 工程记录](e04-host-ports-20261022.md)：类型化宿主读端口七方法、框架版 RuntimeControl、契约变更与验证结果。
- [2026-10-22 M2 只读工程接入](m2-engineering-20261022.md)：独立框架进程、原专家/检索对照、权限与零源业务写入验证，以及未完成的验收边界。
- [2026-10-21 现状检查与迁移改进计划](status-and-improvement-plan-20261021.md)：本轮离线验证、M1 接入前缺口、发行交付问题和后续实施顺序。
- [ForgeHarness 框架替换计划书](../../architecture/forgeharness-migration-plan-20261021.md)：统一框架目标、分阶段接入、验收、切换回滚与旧运行时退出；M0/M1 已有实现，当前缺口见现状检查。
- [2026-09-14 策略基线](../../architecture/strategy-baseline-20260914.md)：默认 RAG、子块实验边界、模型配置及后续变更条件。
- [企业级改进报告](../../runbook/enterprise-improvement-report.md)：能力基线、30 项工作包、真实卷宗验收、分阶段路线图与放行门槛。
- [运维手册](../../runbook/operations.md)：依赖、服务、配置、故障排查。
- [发布与数据保护](../../runbook/release.md)：白名单打包、备份与发布门槛。
- [CI 与质量门禁](../../runbook/quality.md)：离线测试、覆盖率、锁文件、渐进类型检查。
- [模型配置](../../runbook/models.md)：已有模型核查与显式版本下载，不自动替换模型。
- [评测协议](../../benchmarks/README.md)：LawBench、项目 RAG、法条检索、双运行时分别评价。
- [独立官方法条语料](../../benchmarks/legal-corpus.md)：语料构建、版本与来源，以及法条引用安全边界（能保证/不能保证）。
- [混合均分 85 分实施计划](../../benchmarks/SCORE_85_PLAN.md)：冻结口径、开发/确认拆分、真实模型初筛和当前执行进展。
- [均分提升实测与路线图](../../benchmarks/score-uplift-20260909.md)：10,000 题离线复算、确定性后处理收益、已证伪手段与剩余差距。
- [历史索引](../README.md)：旧报告的适用范围与限制。
- [42 项清单修复映射](../../runbook/improvement-checklist.md)：逐项状态、验收与保留边界。
- [2026-09-05 修复验收](../../runbook/validation-20260905.md)：全量测试、干净源码包验证、1000 题真实模型与评分审计结果。

新增实验报告须包含时间、代码/评分版本、命令、模型配置、样本范围、失败与降级数量、原始数据位置。不得仅凭一次旧报告把项目状态标为完成。

- [答案级评测口径](../../benchmarks/answer-eval.md)：要点覆盖、无据断言、引用精度与 judge/κ 边界。
