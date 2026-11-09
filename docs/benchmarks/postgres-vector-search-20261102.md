# PostgreSQL 页向量检索基准（2026-11-02）

结论：在一个 10,000 页的合成案件上，PostgreSQL + pgvector 在库内做精确相似度排序，p50 48 ms、p95 62 ms；原 SQLite 路径把整案向量读进 Python 逐页计算，p50 1.29 s、p95 1.60 s。30 个查询的前 20 页在两条路径上完全相同，顺序也相同。HNSW 近似索引在这批合成向量上召回不足，暂不启用。

原始结果：[postgres-vector-search-20261102.json](postgres-vector-search-20261102.json)；复现命令：

```sh
uv run --locked python scripts/benchmarks/benchmark_vector_search.py \
  --postgres "host=127.0.0.1 port=55432 user=postgres dbname=postgres" \
  --pages 10000 --cases 5 --queries 30
```

## 条件

| 项目 | 取值 |
| --- | --- |
| 机器 | 云端开发容器，2 vCPU、7 GiB 内存；PostgreSQL 与基准进程同机 |
| 软件 | PostgreSQL 16.15、pgvector 0.6.0（Ubuntu 24.04 软件源）、SQLite 3.45.1、Python 3.11 |
| 数据 | 5 个案件 × 10,000 页，页文本由固定种子从 40 个法律词组合生成；向量为应用自带的 384 维哈希向量 |
| 查询 | 30 个固定种子查询；单案检索取前 20 页，跨案检索取前 10 页 |
| 被测代码 | `app/search_backend.py` 的 `scored_page_vectors`，即检索时实际调用的函数 |

## 结果

| 场景 | 路径 | p50 | p95 | 与精确结果一致 |
| --- | --- | --- | --- | --- |
| 单案 10,000 页 | SQLite + Python 逐页计算 | 1,289 ms | 1,601 ms | 基准 |
| 单案 10,000 页 | PostgreSQL 库内精确排序 | 48 ms | 62 ms | 30/30 题前 20 页及顺序完全相同 |
| 跨案 50,000 页 | PostgreSQL 精确排序（关闭索引） | 39 ms | 43 ms | 基准 |
| 跨案 50,000 页 | HNSW，ef_search=40 | 2.6 ms | 3.4 ms | recall@10 = 0.50 |
| 跨案 50,000 页 | HNSW，ef_search=100 | 2.9 ms | 3.9 ms | recall@10 = 0.69 |
| 跨案 50,000 页 | HNSW，ef_search=200 | 6.0 ms | 6.8 ms | recall@10 = 0.87 |

HNSW 索引在 50,000 页上建索引用时 20.6 秒。

## 取舍

- **案件内检索用库内精确排序**：结果与原逐页计算完全一致，又快一个数量级以上，没有召回损失。法律检索漏一页的代价高于几十毫秒，这一档不引入近似索引。
- **HNSW 暂不启用**：合成哈希向量大量相似，近似搜索在这批数据上召回明显不足；是否启用，要等用真实嵌入模型的向量重测后再定。50,000 页的精确排序也只需约 40 ms，现阶段没有必要。
- **维度上限要提前规划**：默认嵌入模型 Qwen3-Embedding-4B 输出 2,560 维（[模型说明](https://huggingface.co/Qwen/Qwen3-Embedding-4B)），而 pgvector 的 HNSW 索引对 `vector` 类型最多支持 2,000 维、对 `halfvec` 最多 4,000 维（pgvector 0.7.0 及以上，见 [Supabase 文档](https://supabase.com/docs/guides/ai/vector-indexes/hnsw-indexes)）。将来要建 HNSW，需要升级 pgvector 并改用 `halfvec`，或利用该模型的可变输出维度降到 2,000 维以内。精确排序不受这个上限限制。

## 边界

- 向量是哈希向量，只测存储与打分路径的速度，不说明真实语义检索质量。
- PostgreSQL 与基准进程同机，不含网络往返；生产部署跨机时每次查询会再多一次网络延迟。
- 单次测量，未做多次重复取区间；数量级差异足以支持上面的取舍，细小差异不作结论。
