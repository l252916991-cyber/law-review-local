# 维护文档入口

以代码、测试产物与以下维护文档为准；根目录早期报告保留为历史证据，不作为“全部完成”的实时声明。

- [系统架构](architecture/system.md)：运行时、数据边界与降级。
- [2026-10-23 领域接入实施记录](migration/domain-integration-execution-20261023.md)：D00–D07 实际进度、用户确认范围、验证与剩余验收条件。
- [2026-10-23 领域恢复设计](migration/domain-integration-design-20261023.md)：版本化领域图、前驱结果与复合回执、引用、模型用量和正式接入边界。
- [2026-10-23 下一步计划书：真实领域接入](migration/domain-integration-plan-20261023.md)：Git 基线后的 D00–D07 工作包、领域持久图与写契约、模型治理及双对照、验收矩阵和正式接入前置条件。
- [2026-10-23 本地 Git 基线初始化](migration/git-initialization-20261023.md)：已审查的源码范围、仓库级作者、既有验证及本地提交规则。
- [2026-10-23 宿主接入实施记录](migration/host-integration-execution-20261023.md)：六类隔离写入、持久映射、真实进程恢复、合成双对照与未放行边界。
- [2026-10-23 宿主接入设计](migration/host-integration-design-20261023.md)：事务/回执、启动授权、源版本、追加事件、生命周期及 schema v16 兼容。
- [2026-10-23 宿主接入原计划](migration/host-integration-plan-20261023.md)：H00–H06 工作包与原验收要求；已交付范围见实施记录，后续安排见真实领域接入计划。
- [2026-10-23 R2 实施记录](migration/r2-execution-20261023.md)：P00–P07 离线工程、0.8.3 最终候选、43 项 R2 专项、三版宿主复验与后续接入边界。
- [2026-10-23 R2 持久编排执行计划书](migration/r2-execution-plan-20261023.md)：E09–E11 的原计划快照；本批实际完成状态以实施记录为准。
- [2026-10-23 下一批实施记录](migration/next-batch-execution-20261023.md)：共享执行器、案件/法条来源、大页包边界、离线模型治理及最终候选验收状态。
- [2026-10-23 下一批执行计划书](migration/next-batch-plan-20261023.md)：E01–E04 后续，E08 共享执行器主线、E06 并行、E05 受控接入及本批验收与外部条件。
- [2026-10-22 统一框架替换执行计划书](migration/execution-plan-20261022.md)：M0–M7 剩余任务、编号与依赖、责任分工、验收操作、切换回退和全项目交付清单。
- [2026-10-22 E01 项目与 AI 入口总表](migration/e01-entry-inventory-20261022.md)：两应用全部 AI 入口、目标治理方式、旁路登记与未决项目。
- [2026-10-22 E02 摘要/发行物/兼容矩阵](migration/e02-baseline-compatibility-20261022.md)：两侧源码与依赖摘要、旧发行物登记、数据 schema 兼容与可重建性、Git 来源与授权状态。
- [2026-10-22 E03 验收参数冻结表](migration/e03-acceptance-parameters-20261022.md)：数据集、规模、并发、超时、模型、预算、评分与观察门槛，及待负责人确认项。
- [2026-10-22 E04 工程记录](migration/e04-host-ports-20261022.md)：类型化宿主读端口七方法、框架版 RuntimeControl、契约变更与验证结果。
- [2026-10-22 M2 只读工程接入](migration/m2-engineering-20261022.md)：独立框架进程、原专家/检索对照、权限与零源业务写入验证，以及未完成的验收边界。
- [2026-10-21 现状检查与迁移改进计划](migration/status-and-improvement-plan-20261021.md)：本轮离线验证、M1 接入前缺口、发行交付问题和后续实施顺序。
- [ForgeHarness 框架替换计划书](architecture/forgeharness-migration-plan-20261021.md)：统一框架目标、分阶段接入、验收、切换回滚与旧运行时退出；M0/M1 已有实现，当前缺口见现状检查。
- [2026-09-14 策略基线](architecture/strategy-baseline-20260914.md)：默认 RAG、子块实验边界、模型配置及后续变更条件。
- [企业级改进报告](runbook/enterprise-improvement-report.md)：能力基线、30 项工作包、真实卷宗验收、分阶段路线图与放行门槛。
- [运维手册](runbook/operations.md)：依赖、服务、配置、故障排查。
- [发布与数据保护](runbook/release.md)：白名单打包、备份与发布门槛。
- [CI 与质量门禁](runbook/quality.md)：离线测试、覆盖率、锁文件、渐进类型检查。
- [模型配置](runbook/models.md)：已有模型核查与显式版本下载，不自动替换模型。
- [评测协议](benchmarks/README.md)：LawBench、项目 RAG、法条检索、双运行时分别评价。
- [独立官方法条语料](benchmarks/legal-corpus.md)：语料构建、版本与来源，以及法条引用安全边界（能保证/不能保证）。
- [混合均分 85 分实施计划](benchmarks/SCORE_85_PLAN.md)：冻结口径、开发/确认拆分、真实模型初筛和当前执行进展。
- [均分提升实测与路线图](benchmarks/score-uplift-20260909.md)：10,000 题离线复算、确定性后处理收益、已证伪手段与剩余差距。
- [历史索引](history/README.md)：旧报告的适用范围与限制。
- [42 项清单修复映射](runbook/improvement-checklist.md)：逐项状态、验收与保留边界。
- [2026-09-05 修复验收](runbook/validation-20260905.md)：全量测试、干净源码包验证、1000 题真实模型与评分审计结果。

新增实验报告须包含时间、代码/评分版本、命令、模型配置、样本范围、失败与降级数量、原始数据位置。不得仅凭一次旧报告把项目状态标为完成。
