# RAG结构改进进展

## 已核验

- 旧240题已去查询案件号、拆分实际embedding与reranker模式，报告模板聚类区间及完整召回。
- 法条已补齐部门规章与交通警察执勤规范正文，小样本方案已接入当前组合配置。
- 新增`--suite challenges`：劳动、知识产权、建设工程、伤害四个独立案件，12道相似页、文字变体、多方面证据诊断题。同案件变体按同一聚类统计。

```sh
uv run --locked python rag_project_benchmark.py --suite challenges --k 2 --embedding-mode hashed-local --reranker off --output-dir output/rag-challenges-local
uv run --locked python rag_project_benchmark.py --suite challenges --k 2 --embedding-mode model --reranker on --output-dir output/rag-challenges-full
```

2026-09-09实测：hashed-local完整召回10/12，多方面证据2/4；模型embedding+重排完整召回11/12，多方面证据3/4。后者12题均为omlx后端，零降级，重排全部启用。配对recall差+0.0417，案件聚类95%区间[0,0.125]；样本只够诊断，不能证明总体收益。伤害案仍遗漏伤情页。

`paired_comparison`拒绝重复ID、不同题目、标签或聚类定义；比较前还必须由调用方核对相同k与数据集哈希。

## 指标边界

检索器接收明确case_id，跨案件应测隔离泄漏，不能称为自动选案准确率。返回相似材料可能支持“证据不足”的回答，因此空检索率只是诊断，真正弃答必须评测答案层。quote存在率也不证明引用支持答案。新诊断集只有4个案件组，并非12个独立样本。

## 长页切分对照（long-pages-v2）

`--suite long-pages-v2`是专为页内子块假设构建的冻结套件：8 案 × 3 题 = 24 题，一案一模板（template 聚类即按案聚类）；每案 9 页，页长 282–8443 字符；金标事实埋在首页 offset≥400 之后，页面 2 是相似干扰、页 3 是短跨页事实，另 6 页是主题重复但无金标的干扰页，使 `k` 小于候选池、金标页必须挤掉同名词面页。每案子块数 52–199，远低于 10000 预算。

三臂同一数据集、同一 `k`、同一后端，用 `--paired-with` 直接产 `paired_comparison.json`：

```sh
# A 页级基线；B 子块 sentence-400；C 同管线 whole-page 窗口消融
uv run --locked python rag_project_benchmark.py --suite long-pages-v2 --k 3 --output-dir output/rc/A
uv run --locked python rag_project_benchmark.py --suite long-pages-v2 --k 3 --page-children --output-dir output/rc/B
uv run --locked python rag_project_benchmark.py --suite long-pages-v2 --k 3 --page-children \
  --child-chunk-profile whole-page --output-dir output/rc/C --paired-with output/rc/B
uv run --locked python rag_project_benchmark.py --suite long-pages-v2 --k 3 --page-children \
  --output-dir output/rc/B-paired --paired-with output/rc/A
```

2026-09-14 实测（hashed-local，reranker off，k=3）：A 页级 recall/complete=1.0；B 子块 recall=0.9375/complete=0.875；**C（whole-page）与 B 在全部五个指标上逐题完全相等**。C 与 B 只差切分窗口、共享同一条 exact-scan 排序，因此「子块窗口本身在本套件零收益」是可归因结论；B 相对 A 的缺口来自管线差异（B/C 缺 query expansion、profile 向量权重、特征重排、邻页扩展、多样度上限），不是切分。B 的 3 处漏检都是短的第 3 页跨页题。`--paired-with` 面对页级基线调用 `paired_run_comparison`（自标 `not_a_pure_chunking_ablation`），面对两个 page-children 运行调用 `paired_window_comparison`（自标 `window_only_but_shares_downstream_rrf`）。

两个必须随结论一起读的限制：页内结果按 `page_id` 去重，每页最多回 1 个子块，切分收益只能体现为「选中的窗口更准」，不可能体现为「一页贡献多片段」；A 在 k=3 时因 `_select_diverse` 每文档上限 `ceil(3*0.6)=2` 只返回 2 条，故 `within_document_page_precision` 的差距部分来自返回条数，须与 recall 同读。本套件 A 臂 recall 已饱和，只能测「切分是否伤害」，不能证明切分收益。

## 页内子块持久化边界

页内检索实验开关仍为`--page-children`且默认关闭。按段句边界切分到最多400字符、重叠50字符；保留原文起止字符，返回整页text与子块quote。`--child-chunk-profile whole-page`是同一管线的窗口消融档（一页一块，版本`page-whole-v1`），只改窗口不改排序。SQLite按页保存当前子块和向量，以原页哈希、切分版本、模型及revision、文本版本、后端和维度共同判定缓存身份；页面未变化时直接复用，页面编辑后在下一次查询懒重建，页面或文档删除时外键级联清理。空白页也保存零子块状态，避免反复处理。

构建阶段在写事务外调用embedding，整批向量全部通过有限、非零和维度校验后，才以短`BEGIN IMMEDIATE`事务重核父页快照并发布；失败不留下半成品。诊断字段区分新嵌入与复用子块，并明确`index_storage=sqlite_persistent`、`index_search=exact_scan`。

子块总量超过10000预算时不再抛出到调用方，而是抛出`ChildScanBudgetExceeded`并在检索层退回页级管线，逐题在`retrieval.child_retrieval`记录`fallback_reason=child_scan_budget_exceeded`；该异常是`ValueError`子类，仅捕获`ValueError`的既有调用方语义不变。

检索排序没有改变：中文bigram BM25和向量各取top50、标准RRF合并，有重排时仅按神经分数排序，再聚合到父页。每次查询仍在不超过10000个子块的预算内做词法与余弦精确全扫描；SQLite持久化消除的是重复切分和重复embedding，并未实现ANN或持久化词法倒排索引。因此该实验模式仍不适合仅凭结构实现切换生产默认，heading_path与前端高亮也尚未完成。

## 尚未完成

短页诊断集向量+重排完整召回11/12，与原页级方案持平；这些页面未超过切分阈值，不能当作子块切分收益证据。long-pages-v2提供了可归因的窗口对照，但A臂饱和，仍不能证明切分收益。长页原文覆盖和字符区间有单元测试。

- 建立A臂不饱和的长页套件，让页级与子块真正拉开差距，再判切分本身是否有正收益。
- 是否放开「每页多子块」的页内去重，让切分收益有第二种可能的表现形式。
- 扩大独立题型、长OCR页干扰、跨案件隔离和答案层弃答评测。
- heading_path与原始页字符区间的前端高亮。
- 纯RRF候选融合与重排最终排序的消融；多跳分解与主题覆盖。
- 法条BM25/向量双索引、重排及回答期引用核验；适用日期与版本歧义控制。
- 领域词典配置化、摘要信号消融。
- 200题策略比较与1000题组合模型验收。

这些结构项仍不能标记完成。当前保留页级引用、case_id隔离、向量空间身份检查及多版本显式选择。
