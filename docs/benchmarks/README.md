# 评测协议与入口

统一模型评测入口是 `unified_benchmark_runner.py --help`。旧 `simple_benchmark.py`、`model_benchmark.py`、`comprehensive_benchmark.py`、各 `run_*test*`/`rescore_*` 属于历史实验，保留原件，不作为新实验推荐入口。2026-09-05 的 1,000 题已按[测试计划](../../BENCHMARK_TEST_PLAN.md)完成，原始记录和评分审计分目录保存。

已完成实验的评分修正入口为 `python scripts/audit_benchmark.py <原运行目录> <新的审计目录>`。它不调用模型、不覆盖旧产物，对新旧同题回答统一重评，记录逐题变化、源文件哈希与分层配对 bootstrap 区间。默认 `verify_benchmark_run.py` 同时要求源码版本和评分复算一致；源码升级后，可使用 `--artifacts-only` 验证历史文件完整性，但该模式不验证历史评分正确性，不能冒充完整评分复算。

离线重放确定性后处理的入口为 `python scripts/postprocess_audit.py <原运行目录> <新的审计目录> [--charge-canonicalization]`：
在已保存回答上应用 `app/benchmark_postprocess.py` 的无参考答案规则后重新评分，逐题保留原预测与原分数。
`--charge-canonicalization` 是诊断选项，会把 3-3 本体外罪名按唯一超串映射回本体，属评分口径放宽，须与严格分分列。

法条库覆盖与补库工作单入口为 `python scripts/lawbench_coverage.py --run <运行目录> --corpus-dir <法条库> ...`：
只读题面统计 1-1 的精确命中率，并把未覆盖题写成 `scripts/build_npc_corpus.py --campaign` 可直接消费的 `inputs.jsonl`。

实测收益、已证伪手段与剩余差距见[均分提升实测与路线图](score-uplift-20260909.md)。

pytest 的 LawBench 装载、抽样和评分协议回归使用临时合成数据，避免 CI 依赖未提交的本地题库。真实模型测试仍严格加载固定上游题库；合成协议测试的题数不计入真实测试成绩。

## 三层结果不得混称

| 层级 | 必须记录 | 不可宣称 |
|---|---|---|
| 模型 LawBench | 任务 instruction、样本 ID、模型/提示词/评分版本、混合均分、分任务分数、失败/拒答/解析失败/截断 | 项目 RAG 准确率、现行法律可靠性 |
| 项目 RAG | 指定案件/语料、页级标准答案、Recall@k/MRR、来源有效性、延迟 | 有 quote 字段就意味着答案被证据支持 |
| 双运行时 | 同业务输出、同记忆快照、执行顺序、路由/引用/专家输出/答案契约、llm_used、checkpoint/恢复 | 两轮速度差证明框架必然更快 |

混合均分是不同任务指标的平均，不是简单准确率。报告需同时给出全样本分母和有效样本分母，明确失败如何计分；拒答和解析失败分开。新旧实验需同题、同评分器重评旧答案；不得修改标准答案或按答案定制提示词来提高分数。

## 数据集成熟度

- `benchmarks/lawbench/zero_shot`：固定上游 20 类 × 500 题，许可/commit 见 `SOURCE.md`；不能无条件称当前本地评分为“官方榜单分数”。
- `app/rag_benchmark_dataset.py`：可生成 240 条演示语料派生题，适合工程回归，不是独立标注的泛化测试。
- `benchmarks/rag_project/rag_240_skeleton.json`、`benchmarks/current_law/current_law_300_skeleton.json`：占位定义，不能计作完成测试集；现行法集需要法律版本日期和人工审校。
- 2026-09-03 开始的旧 10,000 题运行漏传任务说明，保留为缺陷复现基线；不能用于可靠法律能力宣传。

新原始 JSON/JSONL、日志、报告保存在私有 `output/<run>/`，不要覆盖旧 run。先验证数据覆盖、输入哈希、评分复算和恢复语义，再进行昂贵模型测试。
