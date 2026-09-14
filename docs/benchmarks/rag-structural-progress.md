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

四臂同一数据集、同一 `k`、同一后端：

```sh
uv run --locked python rag_project_benchmark.py --suite long-pages-v2 --k 3 --output-dir output/rc/A   # 页级基线
uv run --locked python rag_project_benchmark.py --suite long-pages-v2 --k 3 --page-children \
  --output-dir output/rc/B --paired-with output/rc/A                                                  # 子块 exact-scan
uv run --locked python rag_project_benchmark.py --suite long-pages-v2 --k 3 --page-children \
  --child-chunk-profile whole-page --output-dir output/rc/C --paired-with output/rc/B                 # 窗口消融
uv run --locked python rag_project_benchmark.py --suite long-pages-v2 --k 3 --page-children \
  --child-pipeline unified --output-dir output/rc/D --paired-with output/rc/A                         # 共享管线
```

2026-09-14 实测（hashed-local，reranker off，k=3）：

| 臂 | 管线 | 完整召回 | 页精度 | quote 覆盖金标率 | 最大窗口 |
|---|---|---|---|---|---|
| A 页级 | — | 1.0 | 0.6667 | 0.1667 | 202 |
| B 子块 | exact-scan | 0.875 | 0.4028 | 0.9167 | 398 |
| C 子块 | exact-scan/whole-page | 0.875 | 0.4028 | 0.9375 | 8438 |
| D 子块 | unified | 0.9167 | 0.625 | 0.9375 | 398 |

三条可归因结论：

1. **切分窗口本身对金标召回零收益**：C 与 B 只差窗口、共享 exact-scan 排序，五个指标里四个逐题完全相等，`quote_gold_fact_rate` 差 +0.021（CI[0,0.0625]）。两者返回的页面在 24 题中有 19 题不同，但没有一题改变金标命中或排名。
2. **缺口来自管线，不是切分**：B 相对 A 的 `complete_recall` 差 −0.125、页精度差 −0.264。D 把子块通道接进 A 的融合/特征重排/邻页/多样度后，缺口收窄到 `complete_recall` −0.083、页精度 −0.042，召回 −0.042。
3. **切分的真实收益在引用定位，不在召回**：D 的 quote 覆盖金标率 **0.9375 vs 页级 0.1667**（配对差 +0.771，CI[0.667,0.875]）。页级 quote 锚在首个撞名干扰段（实测金标在 offset 1037、quote 锚在 44），子块 quote 起点就落在金标段（金标 1062、窗口起点 1012），窗口宽 371–398 字符，与页级 202 字符同一量级——是定位变准，不是窗口变宽。

`--paired-with`对页级基线且候选为 `unified` 时调 `paired_channel_comparison`（自标 `channel_granularity_within_shared_pipeline`），对页级基线且候选为 `exact-scan` 时调 `paired_run_comparison`（自标 `not_a_pure_chunking_ablation`），对两个 page-children 运行调 `paired_window_comparison`（自标 `window_only_but_shares_downstream_rrf`）。

两个必须随结论一起读的限制：页内结果按 `page_id` 去重，每页最多回 1 个子块，切分收益只能体现为「选中的窗口更准」，不可能体现为「一页贡献多片段」；A 在 k=3 时因 `_select_diverse` 每文档上限 `ceil(3*0.6)=2` 只返回 2 条、B/C/D 返回 3 条，页精度分母不同，须与 recall 同读。本套件 A 臂 recall 仍饱和（1.0），故它只能显示「切分是否伤害召回」，召回层收益仍需 A 臂不饱和的套件。

## 页内子块持久化边界

页内检索实验开关仍为`--page-children`且默认关闭。按段句边界切分到最多400字符、重叠50字符；保留原文起止字符，返回整页text与子块quote。`--child-chunk-profile whole-page`是同一管线的窗口消融档（一页一块，版本`page-whole-v1`），只改窗口不改排序。`--child-pipeline unified`把最佳子块作为向量通道喂给页级融合/特征重排/邻页扩展/多样度（`paired_channel_comparison` 的 `channel_granularity_within_shared_pipeline`），`exact-scan`是独立的子块排序管线；同一套件的 unified 与页级对照因此只差向量通道的匹配粒度。SQLite按页保存当前子块和向量，以原页哈希、切分版本、模型及revision、文本版本、后端和维度共同判定缓存身份；页面未变化时直接复用，页面编辑后在下一次查询懒重建，页面或文档删除时外键级联清理。空白页也保存零子块状态，避免反复处理。

构建阶段在写事务外调用embedding，整批向量全部通过有限、非零和维度校验后，才以短`BEGIN IMMEDIATE`事务重核父页快照并发布；失败不留下半成品。诊断字段区分新嵌入与复用子块，并明确`index_storage=sqlite_persistent`、`index_search=exact_scan`。

子块总量超过10000预算时不再抛出到调用方，而是抛出`ChildScanBudgetExceeded`并在检索层退回页级管线，逐题在`retrieval.child_retrieval`记录`fallback_reason=child_scan_budget_exceeded`；该异常是`ValueError`子类，仅捕获`ValueError`的既有调用方语义不变。

exact-scan 排序没有改变：中文bigram BM25和向量各取top50、标准RRF合并，有重排时仅按神经分数排序，再聚合到父页。每次查询仍在不超过10000个子块的预算内做词法与余弦精确全扫描；SQLite持久化消除的是重复切分和重复embedding，并未实现ANN或持久化词法倒排索引。因此该实验模式仍不适合仅凭结构实现切换生产默认，heading_path与前端高亮也尚未完成。

## 尚未完成

短页诊断集向量+重排完整召回11/12，与原页级方案持平；这些页面未超过切分阈值，不能当作子块切分收益证据。long-pages-v2给出了可归因的窗口与通道对照，并显示切分的收益在引用定位，但A臂饱和，召回层收益仍未被证明。长页原文覆盖和字符区间有单元测试。

- 建立A臂不饱和的长页套件，让页级与子块在召回上真正拉开差距，再判切分是否有召回收益。
- 是否放开「每页多子块」的页内去重，让切分收益有第二种可能的表现形式。
- 用 quote 定位做前端高亮与 heading_path，把已验证的定位优势接到产品。
- 扩大独立题型、长OCR页干扰、跨案件隔离和答案层弃答评测。
- 纯RRF候选融合与重排最终排序的消融；多跳分解与主题覆盖。
- 法条BM25/向量双索引、重排及回答期引用核验；适用日期与版本歧义控制。
- 领域词典配置化、摘要信号消融。
- 200题策略比较与1000题组合模型验收。

这些结构项仍不能标记完成。当前保留页级引用、case_id隔离、向量空间身份检查及多版本显式选择。
