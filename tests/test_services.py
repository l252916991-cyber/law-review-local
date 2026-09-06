import io
import json
import os
import tempfile
import unittest
from tests.support import IsolatedDatabaseTestCase
from pathlib import Path
from unittest.mock import Mock, patch


class LawReviewServicesTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        # 为此测试类创建独立的临时目录
        cls.test_data_dir = tempfile.mkdtemp(prefix="lexvault-tests-")
        cls.old_data_dir = os.environ.get("LAW_REVIEW_DATA_DIR")
        os.environ["LAW_REVIEW_DATA_DIR"] = cls.test_data_dir

        # 延迟导入，确保环境变量已设置
        from app.db import init_db
        init_db(seed=True)

    @classmethod
    def tearDownClass(cls):
        # 恢复原环境变量
        if cls.old_data_dir:
            os.environ["LAW_REVIEW_DATA_DIR"] = cls.old_data_dir
        else:
            os.environ.pop("LAW_REVIEW_DATA_DIR", None)

        # 清理临时目录
        import shutil
        shutil.rmtree(cls.test_data_dir, ignore_errors=True)

    def test_demo_seed_has_two_level_index(self):
        from app.db import connect
        conn = connect()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0], 1)
            self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 5)
            self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 10)
        finally:
            conn.close()

    def test_route_and_semantic_mapping(self):
        from app.services import detect_route, expand_semantics
        self.assertEqual(detect_route("张某和李某的说法是否矛盾"), "多文档对比")
        self.assertEqual(detect_route("共有多少份卷宗"), "目录统计")
        expanded = expand_semantics("哪些口供表示不知道")
        self.assertIn("不清楚", expanded)
        self.assertIn("我以为合法", expanded)

    def test_date_extraction_keeps_two_digit_month_and_day(self):
        from app.services import infer_dates
        self.assertEqual(infer_dates("询问日期：2024年12月31日"), "2024年12月31日")
        self.assertEqual(infer_dates("期间：2024-06-12 至 2024-11-09"), "2024-06-12 至 2024-11-09")

    def test_reasoning_scratchpad_is_never_exposed(self):
        from app.services import strip_reasoning
        self.assertEqual(strip_reasoning("<think>内部推理</think>最终结论"), "最终结论")
        self.assertEqual(strip_reasoning("<think>未完成的内部推理"), "")
        self.assertEqual(strip_reasoning("我们需要回答用户：先分析资料"), "")

    def test_configured_model_must_exist_without_silent_fallback(self):
        from app.services import local_llm_available
        opener = Mock()
        opener.open.return_value = io.BytesIO(
            json.dumps({"data": [{"id": "another-model"}]}).encode()
        )
        with patch("app.services.urllib.request.build_opener", return_value=opener):
            available, model = local_llm_available("Qwythos-9B-v2-4bit-mlx")
        self.assertFalse(available)
        self.assertEqual(model, "Qwythos-9B-v2-4bit-mlx")

    def test_configured_qwythos_model_is_detected(self):
        from app.services import local_llm_available
        opener = Mock()
        opener.open.return_value = io.BytesIO(
            json.dumps({"data": [{"id": "Qwythos-9B-v2-4bit-mlx"}]}).encode()
        )
        with patch("app.services.urllib.request.build_opener", return_value=opener):
            available, model = local_llm_available("Qwythos-9B-v2-4bit-mlx")
        self.assertTrue(available)
        self.assertEqual(model, "Qwythos-9B-v2-4bit-mlx")

    def test_keepalive_reader_enforces_wall_clock_deadline(self):
        from app.services import read_json_with_deadline
        response = Mock()
        response.read1.return_value = b" "
        with patch("app.services.time.monotonic", side_effect=[0.0, 0.5, 1.1]):
            with self.assertRaisesRegex(TimeoutError, "1 秒总时限"):
                read_json_with_deadline(response, 1)
        response.close.assert_called_once()

    def test_page_retrieval_keeps_source(self):
        from app.services import search_pages
        hits = search_pages(1, "固定回报 审批 邮件", 5)
        self.assertTrue(hits)
        self.assertTrue(any("电子邮件" in item["name"] for item in hits))
        self.assertTrue(all(item["page_no"] >= 1 for item in hits))

    def test_rule_chat_is_traceable(self):
        from app.services import chat
        result = chat(1, "张某关于固定回报的陈述是否存在矛盾？", "测试律师", None, False)
        self.assertEqual(result["route"], "多文档对比")
        self.assertTrue(result["citations"])
        self.assertIn("document_id", result["citations"][0])

    def test_csv_cells_neutralize_formulas_and_preserve_plain_values(self):
        from app.services import csv_safe_cell
        for value in ['=1+1', '+cmd', '-2+3', '@SUM(A1)', ' \t=1', '\ufeff=1', '\u2003=1', '\u2003\ufeff =1', '\ttext', '\rtext', '\ntext']:
            with self.subTest(value=value):
                self.assertEqual(csv_safe_cell(value), "'" + value)
        for value in [None, 3, -2, '', '普通文本', 'a,b', '第一行\n第二行', "'=1"]:
            with self.subTest(value=value):
                self.assertEqual(csv_safe_cell(value), value)

    def test_export_csv_safety_does_not_mutate_evidence(self):
        from app.db import db_scope, init_db
        with tempfile.TemporaryDirectory(prefix='lexvault-csv-') as directory:
            with db_scope(Path(directory) / 'test.db'):
                init_db(seed=True)
                self._assert_export_csv_safety()

    def _assert_export_csv_safety(self):
        import csv
        import zipfile
        from app.db import transaction
        from app.services import build_export
        with transaction() as conn:
            conn.execute("UPDATE documents SET name='=1+1', summary='@SUM(A1)' WHERE case_id=1")
            conn.execute("UPDATE evidence SET title='+1+1', quote=? WHERE case_id=1", (' \t=1+1, "quoted"\nnext',))
        with zipfile.ZipFile(build_export(1)) as archive:
            directory = list(csv.reader(io.StringIO(archive.read('内容级目录.csv').decode('utf-8-sig'))))
            evidence = list(csv.reader(io.StringIO(archive.read('证据目录.csv').decode('utf-8-sig'))))
        self.assertGreater(len(directory), 1)
        self.assertGreater(len(evidence), 1)
        self.assertTrue(all(row[0] == "'=1+1" and row[5] == "'@SUM(A1)" for row in directory[1:]))
        self.assertTrue(all(row[0] == "'+1+1" and row[6] == "' \t=1+1, \"quoted\"\nnext" for row in evidence[1:]))
        with transaction() as conn:
            self.assertEqual(conn.execute("SELECT title FROM evidence WHERE case_id=1 LIMIT 1").fetchone()[0], '+1+1')

    def test_export_package(self):
        from app.services import build_export
        path = build_export(1)
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 300)

    def test_parse_budget_rejects_oversized_files_and_caps_text(self):
        from app.services import MAX_PARSE_BYTES, _cap_total_text, _check_parse_budget
        with tempfile.TemporaryDirectory(prefix="lexvault-budget-") as directory:
            path = Path(directory) / "large.txt"
            path.write_bytes(b"x" * 64)
            with patch("app.services.MAX_PARSE_BYTES", 32):
                with self.assertRaises(ValueError):
                    _check_parse_budget(path)
            self.assertEqual(MAX_PARSE_BYTES, 50 * 1024 * 1024)
        with patch("app.services.MAX_TEXT_CHARS", 100):
            capped = _cap_total_text(["一" * 80, "二" * 80])
        self.assertLessEqual(sum(len(x) for x in capped), 100 + len("\n[文本超出提取上限，已截断]"))
        self.assertIn("文本超出提取上限", capped[-1])

    def test_contained_path_rejects_escape_and_symlink(self):
        from app.services import contained_path
        with tempfile.TemporaryDirectory(prefix="lexvault-contain-") as directory:
            root = Path(directory) / "data"
            (root / "uploads").mkdir(parents=True)
            inside = root / "uploads" / "a.txt"
            inside.write_text("ok", encoding="utf-8")
            self.assertEqual(contained_path(inside, root), inside.resolve())
            outside = Path(directory) / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            with self.assertRaises(ValueError):
                contained_path(outside, root)
            link = root / "uploads" / "escape.txt"
            link.symlink_to(outside)
            with self.assertRaises(ValueError):
                contained_path(link, root)


if __name__ == "__main__":
    unittest.main()
