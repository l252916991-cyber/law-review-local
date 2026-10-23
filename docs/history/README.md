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
