import unittest

from app.benchmark_metrics import (
    char_f1,
    correction_f05,
    extract_option_set,
    rouge_l,
    score_lawbench_item,
    score_lexeval_item,
    set_f1,
)
from app.rag_benchmark_dataset import DATASET_VERSION, build_rag_benchmark


class BenchmarkMetricTest(unittest.TestCase):
    def test_option_parser_supports_single_and_multiple_answers(self):
        cases = {
            "[正确答案]C<eoa>": {"C"}, "正确答案：ABD。": {"A", "B", "D"},
            "答案 AC": {"A", "C"}, "BD": {"B", "D"}, "无法判断": set(),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(extract_option_set(text), expected)

    def test_basic_metrics_boundaries(self):
        self.assertEqual(char_f1("同一答案", "同一答案"), 1)
        self.assertEqual(char_f1("甲", "乙"), 0)
        self.assertEqual(set_f1({"A", "B"}, {"A", "B"}), 1)
        self.assertEqual(set_f1({"A"}, set()), 0)
        self.assertEqual(rouge_l("中华人民共和国民法典", "中华人民共和国民法典"), 1)

    def test_lexeval_requires_exact_option_set(self):
        self.assertEqual(score_lexeval_item("[正确答案]AC<eoa>", "AC").score, 1)
        self.assertEqual(score_lexeval_item("[正确答案]A<eoa>", "AC").score, 0)
        self.assertTrue(score_lexeval_item("不知道", "A").abstained)

    def test_lawbench_selection_tasks(self):
        for task, reference in (("1-2", "正确答案：B。"), ("2-8", "[正确答案]C<eoa>"), ("3-6", "正确答案:C。")):
            with self.subTest(task=task):
                self.assertEqual(score_lawbench_item(task, "[正确答案]C<eoa>" if task != "1-2" else "B", reference).score, 1)

    def test_lawbench_generation_tasks(self):
        for task in ("1-1", "2-7", "3-2", "3-8"):
            with self.subTest(task=task):
                self.assertEqual(score_lawbench_item(task, "完全相同", "完全相同").score, 1)

    def test_lawbench_classification_tasks(self):
        cases = [
            ("2-2", "责任认定", "争议焦点类别：责任认定。", {"责任认定", "合同效力"}, 1),
            ("2-3", "婚后有子女、支付抚养费", "类别:婚后有子女、支付抚养费。", {"婚后有子女", "支付抚养费"}, 1),
            ("2-4", "婚姻家庭", "婚姻家庭", {"婚姻家庭", "劳动纠纷"}, 1),
            ("2-9", "支付/给付;获利", "支付/给付;获利", {"支付/给付", "获利"}, 1),
            ("3-3", "盗窃", "罪名:盗窃", {"盗窃", "抢劫"}, 1),
        ]
        for task, prediction, reference, labels, expected in cases:
            with self.subTest(task=task):
                self.assertEqual(score_lawbench_item(task, prediction, reference, label_space=labels).score, expected)

    def test_reading_comprehension_and_entities(self):
        self.assertEqual(score_lawbench_item("2-5", "21万元借款", "回答:21万元借款").score, 1)
        self.assertEqual(score_lawbench_item("2-6", "受害人:张某;地点:北京", "受害人:张某;地点:北京").score, 1)
        self.assertEqual(score_lawbench_item("2-10", "查扣;退给", "查扣;退给").score, 1)

    def test_article_and_numeric_metrics(self):
        self.assertEqual(score_lawbench_item("3-1", "刑法第264条", "法条:刑法第264条").score, 1)
        self.assertEqual(score_lawbench_item("3-4", "判处4个月", "刑期:4个月").score, 1)
        self.assertEqual(score_lawbench_item("3-5", "判处1年", "刑期:12个月").score, 1)
        self.assertEqual(score_lawbench_item("3-7", "总金额8500元", "上文涉及到的犯罪金额:8500.0元。").score, 1)

    def test_correction_metric(self):
        self.assertEqual(correction_f05("指控实施", "指控事实", "指控事实"), 1)
        self.assertLess(correction_f05("指控实施", "指控事实", "指控行为"), 1)

    def test_project_rag_dataset_has_240_unique_grounded_questions(self):
        records = build_rag_benchmark()
        self.assertEqual(len(records), 240)
        self.assertEqual(len({item["id"] for item in records}), 240)
        self.assertEqual(DATASET_VERSION, "lexvault-rag-240-v2")
        self.assertEqual(len({item["query"] for item in records}), 20)
        self.assertTrue(all(item["case_key"] not in item["query"] for item in records))
        self.assertEqual(sum(not item["answerable"] for item in records), 24)
        self.assertEqual(sum(item["challenge"] == "hard_unanswerable" for item in records), 24)
        for item in records:
            document_pages = {(doc["name"], page) for doc in item["documents"] for page in range(1, len(doc["pages"]) + 1)}
            self.assertTrue(all((gold["document"], gold["page"]) in document_pages for gold in item["expected"]))


if __name__ == "__main__":
    unittest.main()
