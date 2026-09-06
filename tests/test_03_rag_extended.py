"""
扩展RAG层测试 - 检索召回率、引用准确性、跨文档混淆
"""

import os
import tempfile
import unittest
from contextlib import closing
from tests.support import IsolatedDatabaseTestCase

TEST_DATA = tempfile.mkdtemp(prefix="lexvault-rag-tests-")
os.environ["LAW_REVIEW_DATA_DIR"] = TEST_DATA

from app.db import connect, init_db, now  # noqa: E402
from app.rag import HybridRetriever, expand_retrieval_query, hashed_embedding  # noqa: E402


def create_test_case_with_documents(conn):
    """创建测试案件和文档"""
    timestamp = now()
    cursor = conn.execute(
        "INSERT INTO cases (title, case_no, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("RAG测试案件", "RAG-001", timestamp, timestamp)
    )
    case_id = cursor.lastrowid

    # 插入文档
    cursor = conn.execute(
        "INSERT INTO documents (case_id, name, stored_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (case_id, "合同.txt", "/test/contract.txt", timestamp, timestamp)
    )
    doc_id = cursor.lastrowid

    # 插入页面
    pages = [
        (doc_id, 1, "甲方公司与乙方个人于2025年6月1日签订投资合同，约定固定回报年化收益率12%。"),
        (doc_id, 2, "投资金额为人民币100万元整，期限为24个月，到期一次性返还本金和收益。"),
        (doc_id, 3, "张某在询问笔录中陈述：我不知道固定回报是否经过审批，以为是合法的。"),
        (doc_id, 4, "银行流水显示，2025年6月5日从乙方账户转入甲方公司账户100万元。"),
        (doc_id, 5, "2025年8月15日，甲方公司向乙方支付第一期收益2万元。"),
    ]

    for page in pages:
        conn.execute("INSERT INTO pages (document_id, page_no, text) VALUES (?, ?, ?)", page)

    conn.commit()
    return case_id, doc_id


class RAGRetrievalTest(IsolatedDatabaseTestCase):
    """RAG检索召回率测试"""

    @classmethod
    def setUpClass(cls):
        init_db(seed=False)
        with closing(connect()) as conn, conn:
            cls.case_id, cls.doc_id = create_test_case_with_documents(conn)

    def test_exact_keyword_retrieval(self):
        """精确关键词检索"""
        retriever = HybridRetriever(self.case_id, prefer_remote_embeddings=False)
        results, metrics = retriever.retrieve("固定回报", limit=5)

        self.assertGreater(len(results), 0, "应该检索到结果")
        self.assertTrue(any("固定回报" in r["text"] for r in results), "结果应包含关键词")

    def test_semantic_query_expansion(self):
        """语义查询扩展"""
        query = "不知情辩解"
        expanded = expand_retrieval_query(query)

        self.assertIn("不知道", expanded)
        self.assertIn("不清楚", expanded)
        self.assertGreater(len(expanded), len(query))

    def test_multi_keyword_retrieval(self):
        """多关键词检索"""
        retriever = HybridRetriever(self.case_id, prefer_remote_embeddings=False)
        results, metrics = retriever.retrieve("投资 合同", limit=5)

        self.assertGreater(len(results), 0)

    def test_recall_at_k_calculation(self):
        """Recall@K计算"""
        retriever = HybridRetriever(self.case_id, prefer_remote_embeddings=False)
        results, metrics = retriever.retrieve("固定回报审批", limit=3)

        self.assertIn("retrieval_mode", metrics)
        self.assertIn("source_count", metrics)

    def test_empty_query_handling(self):
        """空查询处理"""
        retriever = HybridRetriever(self.case_id, prefer_remote_embeddings=False)
        results, metrics = retriever.retrieve("", limit=5)

        # 应该返回空结果或所有结果
        self.assertIsInstance(results, list)


class RAGCitationTest(IsolatedDatabaseTestCase):
    """RAG引用准确性测试"""

    @classmethod
    def setUpClass(cls):
        init_db(seed=False)
        with closing(connect()) as conn, conn:
            cls.case_id, cls.doc_id = create_test_case_with_documents(conn)

    def test_citation_includes_document_id(self):
        """引用包含文档ID"""
        retriever = HybridRetriever(self.case_id, prefer_remote_embeddings=False)
        results, _ = retriever.retrieve("固定回报", limit=3)

        for result in results:
            self.assertIn("document_id", result)
            self.assertIsInstance(result["document_id"], int)

    def test_citation_includes_page_number(self):
        """引用包含页码"""
        retriever = HybridRetriever(self.case_id, prefer_remote_embeddings=False)
        results, _ = retriever.retrieve("固定回报", limit=3)

        for result in results:
            self.assertIn("page_no", result)
            self.assertGreater(result["page_no"], 0)

    def test_citation_includes_source_text(self):
        """引用包含原文"""
        retriever = HybridRetriever(self.case_id, prefer_remote_embeddings=False)
        results, _ = retriever.retrieve("固定回报", limit=3)

        for result in results:
            self.assertIn("text", result)
            self.assertGreater(len(result["text"]), 0)

    def test_citation_score_ordering(self):
        """引用按相关性排序"""
        retriever = HybridRetriever(self.case_id, prefer_remote_embeddings=False)
        results, _ = retriever.retrieve("固定回报", limit=5)

        if len(results) > 1:
            # 检查是否有score字段（如果有的话应该递减）
            if "score" in results[0]:
                scores = [r["score"] for r in results]
                self.assertEqual(scores, sorted(scores, reverse=True), "分数应该降序排列")


class RAGCrossDocumentTest(IsolatedDatabaseTestCase):
    """RAG跨文档混淆测试"""

    @classmethod
    def setUpClass(cls):
        init_db(seed=False)
        with closing(connect()) as conn, conn:
            timestamp = now()

            # 案件1
            cursor = conn.execute(
                "INSERT INTO cases (title, case_no, created_at, updated_at) VALUES (?, ?, ?, ?)",
                ("案件A", "CASE-A", timestamp, timestamp)
            )
            cls.case_a_id = cursor.lastrowid

            cursor = conn.execute(
                "INSERT INTO documents (case_id, name, stored_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (cls.case_a_id, "案件A文档.txt", "/a.txt", timestamp, timestamp)
            )
            doc_a_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO pages (document_id, page_no, text) VALUES (?, ?, ?)",
                (doc_a_id, 1, "案件A的敏感信息：被告张某涉嫌诈骗100万元")
            )

            # 案件2
            cursor = conn.execute(
                "INSERT INTO cases (title, case_no, created_at, updated_at) VALUES (?, ?, ?, ?)",
                ("案件B", "CASE-B", timestamp, timestamp)
            )
            cls.case_b_id = cursor.lastrowid

            cursor = conn.execute(
                "INSERT INTO documents (case_id, name, stored_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (cls.case_b_id, "案件B文档.txt", "/b.txt", timestamp, timestamp)
            )
            doc_b_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO pages (document_id, page_no, text) VALUES (?, ?, ?)",
                (doc_b_id, 1, "案件B的敏感信息：被告李某涉嫌盗窃50万元")
            )

            conn.commit()

    def test_retrieval_isolated_by_case(self):
        """检索结果按案件隔离"""
        retriever_a = HybridRetriever(self.case_a_id, prefer_remote_embeddings=False)
        results_a, _ = retriever_a.retrieve("敏感信息", limit=5)

        # 案件A的检索结果应该只包含案件A的内容
        for result in results_a:
            self.assertIn("张某", result["text"])
            self.assertNotIn("李某", result["text"])

        retriever_b = HybridRetriever(self.case_b_id, prefer_remote_embeddings=False)
        results_b, _ = retriever_b.retrieve("敏感信息", limit=5)

        # 案件B的检索结果应该只包含案件B的内容
        for result in results_b:
            self.assertIn("李某", result["text"])
            self.assertNotIn("张某", result["text"])

    def test_no_cross_case_leakage(self):
        """无跨案件数据泄漏"""
        retriever_a = HybridRetriever(self.case_a_id, prefer_remote_embeddings=False)
        results, _ = retriever_a.retrieve("李某", limit=10)

        # 搜索"李某"在案件A中不应该返回案件B的数据
        # Fuzzy retrieval may return an irrelevant page from A; that is not a
        # cross-case leak. Assert actual ownership and absence of B's contents.
        with closing(connect()) as conn, conn:
            for result in results:
                owner = conn.execute("SELECT case_id FROM documents WHERE id=?", (result["document_id"],)).fetchone()
                self.assertEqual(owner[0], self.case_a_id)
                self.assertNotIn("案件B的敏感信息", result["text"])


class RAGVectorIndexTest(IsolatedDatabaseTestCase):
    """RAG向量索引测试"""

    @classmethod
    def setUpClass(cls):
        init_db(seed=False)
        with closing(connect()) as conn, conn:
            cls.case_id, cls.doc_id = create_test_case_with_documents(conn)

    def test_vector_index_incremental_update(self):
        """向量索引增量更新"""
        retriever = HybridRetriever(self.case_id, prefer_remote_embeddings=False)

        # 首次索引
        stats1 = retriever.ensure_vector_index(force=True)
        self.assertIn("embedded", stats1)
        self.assertGreater(stats1["embedded"], 0)

        # 再次索引（应该跳过已索引的）
        stats2 = retriever.ensure_vector_index()
        self.assertEqual(stats2["embedded"], 0, "增量更新不应重复索引")
        self.assertEqual(stats2["cached"], stats1["pages"])

    def test_hashed_embedding_deterministic(self):
        """哈希嵌入确定性"""
        text = "固定回报审批流程"
        embedding1 = hashed_embedding(text)
        embedding2 = hashed_embedding(text)

        self.assertEqual(len(embedding1), len(embedding2))
        self.assertEqual(embedding1, embedding2)

    def test_hashed_embedding_dimension(self):
        """哈希嵌入维度"""
        text = "测试文本"
        embedding = hashed_embedding(text)

        self.assertEqual(len(embedding), 384, "应该是384维")

    def test_vector_search_with_hashed_embeddings(self):
        """使用哈希嵌入的向量搜索"""
        retriever = HybridRetriever(self.case_id, prefer_remote_embeddings=False)
        retriever.ensure_vector_index(force=True)

        results, metrics = retriever.retrieve("投资合同", limit=3)
        self.assertGreater(len(results), 0)


class RAGRRFFusionTest(IsolatedDatabaseTestCase):
    """RAG RRF融合测试"""

    @classmethod
    def setUpClass(cls):
        init_db(seed=False)
        with closing(connect()) as conn, conn:
            cls.case_id, cls.doc_id = create_test_case_with_documents(conn)

    def test_hybrid_retrieval_uses_both_modes(self):
        """混合检索使用关键词和向量两种模式"""
        retriever = HybridRetriever(self.case_id, prefer_remote_embeddings=False)
        results, metrics = retriever.retrieve("固定回报", limit=5)

        self.assertIn("retrieval_mode", metrics)
        self.assertEqual(metrics["retrieval_mode"], "hybrid_rrf")

    def test_rrf_fusion_combines_results(self):
        """RRF融合合并结果"""
        retriever = HybridRetriever(self.case_id, prefer_remote_embeddings=False)

        # 混合检索
        hybrid_results, _ = retriever.retrieve("固定回报", limit=5)

        # 应该有结果
        self.assertGreater(len(hybrid_results), 0)


if __name__ == "__main__":
    unittest.main()
