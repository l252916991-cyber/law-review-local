# 文档入口

以代码、测试与对应提交的 CI 结果为准。这里只列现行文档；过程记录在 [history/](history/README.md)。

## 先读这六份

| 文档 | 内容 |
|---|---|
| [项目说明](portfolio/project-brief.md) | 一页定位、架构、技术栈、运行步骤、实测结果与明确未覆盖项 |
| [面试材料](portfolio/interview-notes.md) | 8 个设计取舍问答，每条附实现位置与证据 |
| [系统架构](architecture/system.md) | 组件、数据流、运行时边界与降级 |
| [威胁模型](security/threat-model.md) | 资产、信任边界、提示注入与工具越权的缓解及未覆盖项 |
| [评测协议](benchmarks/README.md) | 模型 / 检索 / 法条 / 答案四层指标的口径与不可互相替代的原因 |
| [运维手册](runbook/operations.md) | 配置、鉴权、健康检查、故障排查 |

## 按任务查

- **部署与交付**：[交付指南](runbook/delivery.md)、[发布手册](runbook/release.md)、[部署验收](runbook/deployment-acceptance.md)、[多人协作验收](runbook/collaboration-acceptance.md)、[合成迁移演示](runbook/migration-demo.md)
- **开发与质量**：[质量门禁](runbook/quality.md)、[前端运行手册](runbook/frontend.md)、[组件许可](runbook/licenses.md)、[开发与提交规则](../AGENTS.md)
- **模型与接入**：[模型配置](runbook/models.md)、[MCP 接入](runbook/mcp.md)、[企业登录（OIDC）](enterprise-login.md)
- **评测**：[答案级评测手册](runbook/answer-evaluation.md)、[答案评测口径](benchmarks/answer-eval.md)、[法条语料](benchmarks/legal-corpus.md)、[样本分母审计](benchmarks/lexvault-evaluation-sample-audit-20261029.md)、[法条冻结资产恢复](benchmarks/collaboration-corpus-recovery-20261028.md)
- **演示**：[录屏手册](runbook/demo-recording.md)
- **简历**：[简历条目](portfolio/resume-bullets.md)

## 历史与过程记录

- [过程文档归档](history/migration/README.md)：2026-10-21 至 11-01 的全部计划书、实施记录与证据（三组对照、多人协作、评测补齐、框架接入等），按原样保留。
- [提交作者改写与哈希对照](history/author-rewrite-20261101.md)：旧哈希 → 新哈希的完整对照。
- [优化编年史](history/optimization-chronicle.md)、[历史材料索引](history/README.md)、[旧版前端说明](history/legacy-ui/mobile-workspace.md)。
- 早期计划与报告仍在 `runbook/`：[企业化改进报告](runbook/enterprise-improvement-report.md)、[42 项修复映射](runbook/improvement-checklist.md)、[2026-09-05 验收记录](runbook/validation-20260905.md)。
