# 求职展示版 P5：项目说明、面试材料与简历条目

- 日期：2026-10-27（Asia/Shanghai）。
- 对应计划：[agent-portfolio-plan-20261027.md](agent-portfolio-plan-20261027.md) 第 6 节；素材来源：[P0](portfolio-p0-scope-evidence-20261027.md)、[P1](portfolio-p1-ablation-driver-20261027.md)、[P2](portfolio-p2-precheck-blocked-20261027.md)、[P3](portfolio-p3-demo-status-20261027.md)、[P4](portfolio-p4-reproducible-delivery-20261027.md)。
- 结论：**材料已成形，质量数字仍留空**。所有已写的数字都能回溯到命令与产物；真实模型对照未完成，因此“提升多少”类表述继续为占位。

## 1. 交付物

| 文件 | 内容 | 状态 |
|---|---|---|
| [project-brief.md](../portfolio/project-brief.md) | 一页定位、架构与数据流、技术栈、运行步骤、质量基线、证据索引、未覆盖 | 完成 |
| [interview-notes.md](../portfolio/interview-notes.md) | 8 个问题：为何拆专家、为何保留规则、如何保证只差一个变量、如何控预算、如何防过期恢复、如何评测、负结果、个人边界 | 完成 |
| [resume-bullets.md](../portfolio/resume-bullets.md) | 4 条已完成 + 1 条待测占位；含“不能写成的话”清单 | 完成（质量数字待 P2） |

## 2. 可追溯性核对

| 简历/材料中的数字 | 来源 | 复算方式 |
|---|---|---|
| 提示长度 A 855 / B 7,568 / C 9,719 字符（中位） | 冻结批次 `output/portfolio-ablation/dev-20261027/manifest.json` | 读 `items[].prompt_chars` 取中位 |
| 60 题、14 页、来源摘要 `b912f4…` | `benchmarks/answer_eval/v2/dataset.json` | `scripts/build_answer_eval.py` 幂等复算 |
| 恢复片段 steps 9→9、`resume_count=1`、拒绝恢复 | `output/portfolio-demo/anomaly-20261027/*.json` | 重跑 `scripts/demo_recovery_check.py` 三个阶段 |
| 1389 passed / 85.72% | 全量离线检查日志 `output/portfolio-ci/pytest2.log` | 按 P4 第 5 节命令重跑 |
| 440 成员、zip `e3a3a1d4…`、包内 71 passed | `output/portfolio-repro/lexvault-source-20261027b.zip` | 重新打包并核对清单 |

## 3. 明确不写的表述

- “多个自主 LLM 专家协作”——专家是确定性规则节点。
- “来源一致率 = 法律准确率”——来源匹配不等于语义蕴含或律师确认。
- 任何质量提升数字——P2 未执行。
- “线上 SLA / 已上线律所”——本机/受控内网单实例，未上线。
- “跨案件泛化”——开发集单案 60 题，保留集未编制。

## 4. 待办（按依赖顺序）

1. 启动登记的本地模型端点 → P2 预检通过 → 9 次预检 → 180 次开发集 → 失败归因 → 216 次保留集。
2. 编制保留集 6 案例 × 4 题并冻结来源、预期、评分标准与摘要。
3. 落实独立评分人与 ≥20 个有效匹配标签，才能计算 Judge/人工 κ。
4. 模型可用后重录 P3 主演示，并补齐 Docker 与前端构建门禁。
5. 完成上述后，回填简历占位与 P4/P5 的质量栏目。

## 5. 材料使用边界

项目级许可证与对外公开权利尚未确定；材料默认私下审阅，对外分享视频、源码包或在线体验前须由维护者确认权利范围。第三方依赖、模型与数据分别遵循其来源许可。
