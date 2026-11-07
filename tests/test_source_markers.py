"""Source headers copied from the prompt are rewritten to ``[资料N]`` only when they are exact."""
import io
import json
import unittest
from unittest.mock import patch

from app import services
from app.agents import validate_review_answer
from app.services import canonical_source_markers, source_header

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

    def test_other_malformed_markers_are_not_repaired(self):
        # Seen from Qwythos-9B in the same run; a range or a comparison is not one
        # source, so these stay rejected.
        for answer in ("全篇没有法条[资料1-2]。请律师复核。", "说法冲突[资料1 vs 资料2]。请律师复核。"):
            with self.subTest(answer=answer):
                self.assertEqual(canonical_source_markers(answer, CONTEXTS), answer)
                self.assertFalse(validate_review_answer(answer, CONTEXTS)["valid"])

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
                patch.object(services, "local_llm_available", return_value=(True, "test-model")), \
                patch.object(services, "egress_opener", return_value=_Opener(payload)):
            return services.call_local_llm("问题", "事实检索", CONTEXTS, **kwargs)

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
