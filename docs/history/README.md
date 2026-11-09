# 历史材料索引（保留、不视为实时状态）

以下材料仍在原位置，避免破坏原始证据、现有链接或正在运行的脚本。发布脚本默认不打包它们；不因文件带有 AI 生成痕迹而删除。

| 历史类别 | 原位置 | 阅读限制 |
|---|---|---|
| 早期“完成”汇报 | 根目录 `FINAL_*`、`PROJECT_*`、`PHASE*`、`.test_completion_receipt.txt` | 是当时的声明，必须用对应测试日志核验，不能代表当前全绿 |
| 测试阶段文档 | 根目录 `TEST_*`、`COMPREHENSIVE_TEST_REPORT.md`、`SECURITY_AND_BUG_REPORT.md` | 样本范围、失败数与评分器版本可能过期 |
| 双运行时实验 | `LANGGRAPH_COMPARISON_REPORT.md`、`langgraph_comparison_results.json` | 16 次调用的小样本，模型波动与答案契约失败不能忽略 |
| 原始 LawBench | `output/benchmark_full_10k/run_20260903_145445/` | 缺 task instruction 的旧基线，不是可信模型准确率 |
| 修复后 1,000 题 | `output/benchmark_fixed_1000_20260905/` | 已完成；v2 原报告保留，最终 v3 审计位于同级 `benchmark_fixed_1000_20260905_audit_v3/` |
| 面试练习材料 | 根目录 `面试题库_*.md`、`docs/INTERVIEW_GUIDE.md` | 练习，不是产品功能或测试证据 |
| 一次性运维脚本 | 根目录 `auto_fix_*`、`monitor_*`、`download_*`、`rescore_*` 等 | 历史快照，可能覆盖文件；不要未经审查运行 |

新的权威入口是 [docs/README.md](../README.md) 和 [2026-09-05 验收记录](../runbook/validation-20260905.md)。历史报告需要公开时应单独审校、脱敏并附明确版本，不混入默认源码发布包。

## 2026-11-01 起归档的过程文档

| 类别 | 位置 | 阅读限制 |
|---|---|---|
| 计划书、实施记录与证据（2026-10-21—11-01） | [migration/](migration/README.md) | 内容按原样保留；其中“待执行”“未运行”等状态是当时的事实，现行状态以 [项目说明](../portfolio/project-brief.md) 为准 |
| 旧版 `app/static/` 前端说明 | [legacy-ui/](legacy-ui/mobile-workspace.md) | 旧界面已删除，仅供核对旧版本 |
| 提交作者改写 | [author-rewrite-20261101.md](author-rewrite-20261101.md) | 文档中出现的旧提交哈希可用对照表换算 |
| 代码结构重组（2026-11-09） | [restructure-20261109.md](restructure-20261109.md) | 过程记录里的旧路径（如 `app/services.py`、`scripts/eval_gate.py`）可用对照表换算；正文保持原文，链接已指向文件现在的位置 |
