# 维护文档入口

以代码、测试产物与以下维护文档为准；根目录早期报告保留为历史证据，不作为“全部完成”的实时声明。

- [系统架构](architecture/system.md)：运行时、数据边界与降级。
- [企业级改进报告](runbook/enterprise-improvement-report.md)：能力基线、30 项工作包、真实卷宗验收、分阶段路线图与放行门槛。
- [运维手册](runbook/operations.md)：依赖、服务、配置、故障排查。
- [发布与数据保护](runbook/release.md)：白名单打包、备份与发布门槛。
- [CI 与质量门禁](runbook/quality.md)：离线测试、覆盖率、锁文件、渐进类型检查。
- [模型配置](runbook/models.md)：已有模型核查与显式版本下载，不自动替换模型。
- [评测协议](benchmarks/README.md)：LawBench、项目 RAG、双运行时分别评价。
- [混合均分 85 分实施计划](benchmarks/SCORE_85_PLAN.md)：冻结口径、开发/确认拆分、真实模型初筛和当前执行进展。
- [均分提升实测与路线图](benchmarks/score-uplift-20260909.md)：10,000 题离线复算、确定性后处理收益、已证伪手段与剩余差距。
- [历史索引](history/README.md)：旧报告的适用范围与限制。
- [42 项清单修复映射](runbook/improvement-checklist.md)：逐项状态、验收与保留边界。
- [2026-09-05 修复验收](runbook/validation-20260905.md)：全量测试、干净源码包验证、1000 题真实模型与评分审计结果。

新增实验报告须包含时间、代码/评分版本、命令、模型配置、样本范围、失败与降级数量、原始数据位置。不得仅凭一次旧报告把项目状态标为完成。
