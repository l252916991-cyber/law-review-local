"""Citation markers naming real sources in another form are rewritten to ``[资料N]``; nothing else is."""
import io
import json
import unittest
from unittest.mock import patch

from app.review import generation
from app.review.agents import validate_review_answer
from app.review.generation import canonical_source_markers, source_header

CONTEXTS = [
    {"document_id": 3, "name": "03_李某询问笔录.txt", "page_no": 3, "quote": "我提醒过没有金融牌照。"},
    {"document_id": 2, "name": "02_张某询问笔录.txt", "page_no": 2, "quote": "合规由李某负责。"},
]

#: Qwen3.6-35B-A3B, 2026-11-07 cross-model gate, normal-09 (shortened). The content
#: is right; before this change the answer contract rejected it as a model error.
COPIED_HEADERS = (
    "**结论**\n李某称自己提醒过没有金融牌照的资质问题。\n\n**依据**\n"
    "1. 李某称其曾提醒过“没有金融牌照”[资料1｜03_李某询问笔录.txt｜第3页]。\n"
    "2. 张某称合规由李某负责[资料2｜02_张某询问笔录.txt｜第2页]。\n\n请律师复核原文与上述结论"
)


class CanonicalSourceMarkersTest(unittest.TestCase):
    def test_the_prompt_header_is_what_gets_recognised(self):
        header = source_header(1, CONTEXTS[0])
        self.assertEqual(header, "[资料1｜03_李某询问笔录.txt｜第3页]")
        self.assertEqual(canonical_source_markers(f"结论{header}", CONTEXTS), "结论[资料1]")

    def test_a_correct_answer_citing_with_copied_headers_now_passes_the_contract(self):
        self.assertFalse(validate_review_answer(COPIED_HEADERS, CONTEXTS)["valid"])
        rewritten = canonical_source_markers(COPIED_HEADERS, CONTEXTS)
        self.assertIn("没有金融牌照”[资料1]。", rewritten)
        self.assertIn("合规由李某负责[资料2]。", rewritten)
        self.assertTrue(validate_review_answer(rewritten, CONTEXTS)["valid"])

    def test_spacing_bracket_and_separator_variants(self):
        for marker in ("【资料1｜03_李某询问笔录.txt｜第3页】", "[资料1 | 03_李某询问笔录.txt | 第 3 页]",
                       "［资料 1｜03_李某询问笔录.txt｜第3页］"):
            with self.subTest(marker=marker):
                self.assertEqual(canonical_source_markers(f"a{marker}b", CONTEXTS), "a[资料1]b")

    def test_a_header_that_does_not_match_its_source_is_left_alone_and_still_rejected(self):
        wrong = (
            "[资料1｜03_李某询问笔录.txt｜第2页]",   # wrong page
            "[资料1｜02_张某询问笔录.txt｜第3页]",   # another source's file
            "[资料3｜03_李某询问笔录.txt｜第3页]",   # index outside this turn's sources
            "[资料0｜03_李某询问笔录.txt｜第3页]",
        )
        for marker in wrong:
            with self.subTest(marker=marker):
                answer = f"结论{marker}。请律师复核。"
                self.assertEqual(canonical_source_markers(answer, CONTEXTS), answer)
                self.assertFalse(validate_review_answer(answer, CONTEXTS)["valid"])

    def test_several_sources_in_one_bracket_become_one_marker_each(self):
        # Seen from Qwen3.6-35B-A3B and Qwythos-9B in the 2026-11-07 runs.
        cases = {
            "[资料1/2]": "[资料1][资料2]",
            "[资料1、资料2]": "[资料1][资料2]",
            "[资料1 vs 资料2]": "[资料1][资料2]",
            "[资料2 vs 资料1]": "[资料2][资料1]",
            "[资料1-2]": "[资料1][资料2]",
            "【资料1，资料2】": "[资料1][资料2]",
            "[资料1和资料2]": "[资料1][资料2]",
            "[资料1至2]": "[资料1][资料2]",
        }
        for marker, expected in cases.items():
            with self.subTest(marker=marker):
                answer = f"说法冲突{marker}。请律师复核。"
                rewritten = canonical_source_markers(answer, CONTEXTS)
                self.assertEqual(rewritten, f"说法冲突{expected}。请律师复核。")
                self.assertTrue(validate_review_answer(rewritten, CONTEXTS)["valid"])

    def test_a_list_or_range_outside_this_turns_sources_is_left_alone(self):
        for marker in ("[资料1-6]", "[资料2-1]", "[资料1-1]", "[资料1、3]", "[资料0/1]", "[资料1, 资料9]"):
            with self.subTest(marker=marker):
                answer = f"全篇没有法条{marker}。请律师复核。"
                self.assertEqual(canonical_source_markers(answer, CONTEXTS), answer)
                self.assertFalse(validate_review_answer(answer, CONTEXTS)["valid"])

    def test_the_six_source_range_seen_in_the_runs(self):
        six = [{"document_id": i, "name": f"{i}.txt", "page_no": 1, "quote": "原文"} for i in range(1, 7)]
        answer = "全文未引用任何司法解释[资料1-6]。请律师复核。"
        rewritten = canonical_source_markers(answer, six)
        self.assertIn("[资料1][资料2][资料3][资料4][资料5][资料6]", rewritten)
        self.assertTrue(validate_review_answer(rewritten, six)["valid"])

    def test_the_prompts_placeholder_is_removed(self):
        # Qwen3.6-35B-A3B, 2026-11-08, irrelevant-02: a correct refusal lost to "[资料N]".
        answer = ("**结论**\n无法依据本案材料推导量子场论公式[资料1]。\n\n**缺失信息**\n"
                  "1. 具体的物理模型设定[资料N]。\n2. 相关的物理常数【资料n】。\n\n请律师复核原文与上述结论")
        self.assertFalse(validate_review_answer(answer, CONTEXTS)["valid"])
        rewritten = canonical_source_markers(answer, CONTEXTS)
        self.assertNotIn("资料N", rewritten)
        self.assertNotIn("资料n", rewritten)
        self.assertIn("推导量子场论公式[资料1]", rewritten)
        self.assertTrue(validate_review_answer(rewritten, CONTEXTS)["valid"])

    def test_an_answer_citing_only_placeholders_is_still_rejected(self):
        answer = "结论：缺少法条原文[资料N]。请律师复核。"
        self.assertFalse(validate_review_answer(canonical_source_markers(answer, CONTEXTS), CONTEXTS)["valid"])

    def test_plain_markers_and_text_are_unchanged(self):
        answer = "结论[资料1][资料2]，第3页另见。请律师复核。"
        self.assertEqual(canonical_source_markers(answer, CONTEXTS), answer)
        self.assertEqual(canonical_source_markers("", CONTEXTS), "")


class _Response:
    def __init__(self, payload: bytes):
        self.buffer = io.BytesIO(payload)

    def read1(self, size: int) -> bytes:
        return self.buffer.read(size)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class _Opener:
    def __init__(self, payload: bytes):
        self.payload = payload

    def open(self, request, timeout=None):
        return _Response(self.payload)


class ModelCallReturnsCanonicalMarkersTest(unittest.TestCase):
    def _call(self, payload: bytes, **kwargs) -> str:
        env = {"LAW_REVIEW_LLM_URL": "http://127.0.0.1:8000/v1", "LAW_REVIEW_LLM_API": "",
               "LAW_REVIEW_LLM_API_KEY": "", "LAW_REVIEW_MODEL_CATALOG": "",
               "LAW_REVIEW_MODEL_CATALOG_FILE": "", "LAW_REVIEW_MODEL_CREDENTIALS_FILE": ""}
        with patch.dict("os.environ", env), \
                patch.object(generation, "local_llm_available", return_value=(True, "test-model")), \
                patch.object(generation, "egress_opener", return_value=_Opener(payload)):
            return generation.call_local_llm("问题", "事实检索", CONTEXTS, **kwargs)

    def test_non_streaming_answer(self):
        body = json.dumps({"choices": [{"message": {"content": COPIED_HEADERS}}]}).encode()
        answer = self._call(body)
        self.assertNotIn("｜", answer)
        self.assertTrue(validate_review_answer(answer, CONTEXTS)["valid"])

    def test_streaming_answer(self):
        half = len(COPIED_HEADERS) // 2
        frames = [json.dumps({"choices": [{"delta": {"content": part}}]})
                  for part in (COPIED_HEADERS[:half], COPIED_HEADERS[half:])]
        payload = "".join(f"data: {frame}\n\n" for frame in [*frames, "[DONE]"]).encode()
        tokens: list[str] = []
        answer = self._call(payload, on_token=tokens.append)
        self.assertTrue(tokens)  # drafts still stream as produced; only the final answer is rewritten
        self.assertEqual(answer, canonical_source_markers(COPIED_HEADERS, CONTEXTS))


if __name__ == "__main__":
    unittest.main()
