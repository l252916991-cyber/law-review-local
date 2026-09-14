# 评测协议与入口

统一模型评测入口是 `unified_benchmark_runner.py --help`。旧 `simple_benchmark.py`、`model_benchmark.py`、`comprehensive_benchmark.py`、各 `run_*test*`/`rescore_*` 属于历史实验，保留原件，不作为新实验推荐入口。2026-09-05 的 1,000 题已按[测试计划](../../BENCHMARK_TEST_PLAN.md)完成，原始记录和评分审计分目录保存。

统一 runner 在评分前对 2-1/2-7/2-9/3-8 的成功响应应用 `app/benchmark_postprocess.py` 的无参考答案规则（2-1 恢复源句标点与句尾形式；2-7 取有界导语摘要；2-9 按公开事件本体词表标注；3-8 去除 Markdown 标记并恢复题面要求的「回答→法律依据」结构）；2-10 因实测增益为噪声（15 升 16 降）未启用。1-1 需要法条库，传 `--corpus-dir <法条库>`（可重复）后才会在评分前用官方法条正文替换模型输出；不传则保持模型原始输出。同一参数会默认启用[3-2 词法 RAG 采纳协议](3-2-lexical-rag-adoption-20260913.md)；`--disable-3-2-rag` 提供同 solver 的无检索配对控制路径，未提供语料时 3-2 保留旧路径。`prediction` 是评分文本，处理、检索、路由、实际请求和有效配置均逐题保存。manifest 冻结任务范围、版本、路由、后处理与检索源码哈希、语料目录及文件哈希；旧 manifest、语料变更或缺失溯源的检查点不能续跑为新配置，请使用新运行目录。

已完成实验的评分修正入口为 `python scripts/audit_benchmark.py <原运行目录> <新的审计目录>`。它不调用模型、不覆盖旧产物，对新旧同题回答统一重评，记录逐题变化、源文件哈希与分层配对 bootstrap 区间。默认 `verify_benchmark_run.py` 同时要求源码版本和评分复算一致；源码升级后，可使用 `--artifacts-only` 验证历史文件完整性，但该模式不验证历史评分正确性，不能冒充完整评分复算。

离线重放确定性后处理的入口为 `python scripts/postprocess_audit.py <原运行目录> <新的审计目录> [--corpus-dir <法条库> ...] [--charge-canonicalization]`：
在已保存回答上应用 `app/benchmark_postprocess.py` 的无参考答案规则后重新评分，逐题保留原预测与原分数。
默认重放 2-1/2-7/2-9/2-10；提供 `--corpus-dir` 后额外重放 1-1 的精确条文策略（检索只用题面）。
`--charge-canonicalization` 是诊断选项，会把 3-3 本体外罪名按唯一超串映射回本体，属评分口径放宽，须与严格分分列。

法条库覆盖与补库工作单入口为 `python scripts/lawbench_coverage.py --run <运行目录> --corpus-dir <法条库> ...`：
只读题面统计 1-1 的精确命中率，并把未覆盖题写成 `scripts/build_npc_corpus.py --campaign` 可直接消费的 `inputs.jsonl`。

实测收益、已证伪手段与剩余差距见[均分提升实测与路线图](score-uplift-20260909.md)。

项目 RAG 回归入口为：

```sh
uv run --locked python rag_project_benchmark.py \
  --embedding-mode hashed-local --reranker off \
  --output-dir output/rag-240-v2-hashed
```

`--embedding-mode` 与 `--reranker` 独立配置，因此可以分别验证哈希召回、本地模型向量、神经重排及其组合；请求的配置、逐题实际 embedding 后端、降级状态、重排启用数、数据集和源码哈希都会写入摘要。模型或重排服务不可用时，对应模型配置直接失败，不静默换成正常样本。

`lexvault-rag-240-v2` 移除了题面案件号，负例改为库内存在高度相似词面的真弃答题。正例的 `passed` 要求全部金标页命中；摘要分列 Recall@k、MRR、页精度、完整召回率、多来源题、硬负例空召回率及按题型模板聚类的 bootstrap 区间。`quote_presence_rate` 只表示结果含原文摘录，不能表述为引用忠实度。

`--suite long-pages-v2`是长页切分对照套件（8案24题、一案一模板、页长282–8443、金标埋在首个子块窗口之后，另含主题重复干扰页使 k 小于候选池）。`--page-children`开启子块路径，`--child-chunk-profile {sentence-400,whole-page}`选择切分窗口（`whole-page`是只改窗口的消融档）。`--paired-with <目录>`把本 run 与既有 run 配对并落 `paired_comparison.json`：对页级基线调 `paired_run_comparison`（整套管线对照，自标非纯切分消融），对两个 page-children run 调 `paired_window_comparison`（窗口消融）。摘要新增 `latency`（p50/p95 与冷/热拆分）、`child_index`（每案子块数、重排候选数）、`child_retrieval_runtime`（子块命中数与页级降级原因）与 `child_chunk_profile`。子块总量超 10000 预算时退回页级并在逐题 `child_retrieval.fallback_reason` 记录，不再让查询失败。页内命中按页去重，每页最多回 1 个子块，`text`是整页、`quote`才是匹配窗口。

pytest 的 LawBench 装载、抽样和评分协议回归使用临时合成数据，避免 CI 依赖未提交的本地题库。真实模型测试仍严格加载固定上游题库；合成协议测试的题数不计入真实测试成绩。

## 四层结果不得混称

| 层级 | 必须记录 | 不可宣称 |
|---|---|---|
| 模型 LawBench | 任务 instruction、样本 ID、模型/提示词/评分版本、混合均分、分任务分数、失败/拒答/解析失败/截断 | 项目 RAG 准确率、现行法律可靠性 |
| 项目 RAG | 指定案件/语料、页级标准答案、Recall@k/MRR、页精度、完整召回、弃答率、实际后端、延迟 | 有 quote 字段就意味着答案被证据支持 |
| 法条检索 | 冻结 gold、dataset/config/index 身份、Hit@5/MRR@10/NDCG@10、逐题 available/failure、版本与语料身份 | 现行法律有效性、条文适用于本案；纯文本 `candidate` 不构成已核验引用 |
| 双运行时 | 同业务输出、同记忆快照、执行顺序、路由/引用/专家输出/答案契约、llm_used、checkpoint/恢复 | 两轮速度差证明框架必然更快 |

混合均分是不同任务指标的平均，不是简单准确率。报告需同时给出全样本分母和有效样本分母，明确失败如何计分；拒答和解析失败分开。新旧实验需同题、同评分器重评旧答案；不得修改标准答案或按答案定制提示词来提高分数。

## 数据集成熟度

- `benchmarks/lawbench/zero_shot`：固定上游 20 类 × 500 题，许可/commit 见 `SOURCE.md`；不能无条件称当前本地评分为“官方榜单分数”。
- `app/rag_benchmark_dataset.py`：可生成 240 条演示语料派生题，适合工程回归，不是独立标注的泛化测试。
- `benchmarks/rag_project/rag_240_skeleton.json`、`benchmarks/current_law/current_law_300_skeleton.json`：占位定义，不能计作完成测试集；现行法集需要法律版本日期和人工审校。
- 2026-09-03 开始的旧 10,000 题运行漏传任务说明，保留为缺陷复现基线；不能用于可靠法律能力宣传。

新原始 JSON/JSONL、日志、报告保存在私有 `output/<run>/`，不要覆盖旧 run。先验证数据覆盖、输入哈希、评分复算和恢复语义，再进行昂贵模型测试。
