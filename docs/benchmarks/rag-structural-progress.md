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

## 尚未完成

页内检索现有实验开关：`--page-children`，默认关闭。按段句边界切分到最多400字符、重叠50字符；保留原文起止字符，返回整页text与子块quote。使用中文bigram BM25和向量各top50、标准RRF合并，有重排时仅按神经分数排序，再聚合到页。当前是最多10000子块的临时全扫描，每次重新计算向量，尚未实现持久索引、heading_path及前端高亮，不适合直接切换生产默认。

短页诊断集向量+重排完整召回11/12，与原页级方案持平；这些页面未超过切分阈值，不能当作子块切分收益证据。长页原文覆盖和字符区间有单元测试，长页检索质量仍需独立评测。

- 扩大独立题型、长OCR页干扰、跨案件隔离和答案层弃答评测。
- parent-child索引、原始页字符区间与高亮，迁移及索引身份测试。
- 纯RRF候选融合与重排最终排序的消融；多跳分解与主题覆盖。
- 法条BM25/向量双索引、重排及回答期引用核验；适用日期与版本歧义控制。
- 领域词典配置化、摘要信号消融。
- 200题策略比较与1000题组合模型验收。

这些结构项仍不能标记完成。当前保留页级引用、case_id隔离、向量空间身份检查及多版本显式选择。
