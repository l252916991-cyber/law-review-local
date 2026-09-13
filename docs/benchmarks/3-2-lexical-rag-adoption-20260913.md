# 3-2 词法 RAG 采纳与官方运行协议（2026-09-13）

当前冻结配置在 LawBench 3-2 全量 500 题同题配对评估中，词法 statutory RAG 从
35.7374% 提升到 47.2492%，均分差 **+11.5118 个百分点**，配对 bootstrap 95% CI
为 **[+9.6830, +13.3368]**，逐题胜/负/平为 333/132/35。因此统一 runner 在提供
`--corpus-dir` 时，3-2 默认采用词法 statutory RAG。该结果是已检查题目的采纳评估，
不是盲测，也不是本次配置提交后的官方重跑结果。

3-2 路由按运行参数冻结：有法条库时默认调用 `app.benchmark_rag_solver.solve`，并在每次
检索中显式指定 `lexical`，不受 `LAW_REVIEW_STATUTORY_INDEX` 环境变量影响；加
`--disable-3-2-rag` 时改走同一 solver 的 task-guided 单次调用控制臂；未提供法条库时
保留旧统一 runner 路径并打印提示。solver 路径不做传输重试，正式运行必须显式使用
`--retry 0`，避免误读参数。

Qwythos-9B-v2-8bit、temperature 0、max_tokens 1600、timeout 240 秒、canonical 85 部法
的 500 题官方运行命令为：

```sh
LAW_REVIEW_LLM_URL=http://127.0.0.1:8000/v1 \
LAW_REVIEW_LLM_MODEL=Qwythos-9B-v2-8bit-mlx \
uv run --locked python unified_benchmark_runner.py \
  --dataset lawbench --tasks 3-2 \
  --corpus-dir output/score85/legal_corpus_canonical_v2 \
  --max-tokens 1600 --timeout 240 --retry 0 \
  --run-dir output/official-3-2-lexical-rag-20260913
```

不传 `--limit-per-task` 即运行 3-2 全量 500 题。运行前清空机器上的竞争模型任务并确认
目标模型服务已加载；运行完成后执行：

```sh
uv run --locked python verify_benchmark_run.py \
  output/official-3-2-lexical-rag-20260913
```

manifest 在首次模型调用前冻结逐题路由、词法策略、实际消息哈希、检索上下文及哈希、
模型和生成参数、语料文件哈希与相关源码快照。逐题记录保存实际请求体及哈希、检索结果、
solver 有效配置、响应结束原因、usage、错误和评分；技术失败仍以零分进入全题分母。
续跑和离线校验会拒绝缺失或篡改的路由、上下文、请求以及 manifest 配置溯源。
