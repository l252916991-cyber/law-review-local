"""The capture wrapper records what the model returned before the marker rewrite."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.review import chat as chat_turn
from app.review import generation
from scripts.evaluation.capture_model_gate import Recorder

CONTEXTS = [{"document_id": 3, "name": "03_李某询问笔录.txt", "page_no": 3, "quote": "我提醒过没有金融牌照。"}]
RAW = "李某提醒过没有金融牌照[资料1｜03_李某询问笔录.txt｜第3页]。"


def fake_model(question, route, contexts, **kwargs):
    # Stands in for call_local_llm: the rewrite is looked up on the generation module.
    return generation.canonical_source_markers(RAW, contexts)


def fake_chat(*args, **kwargs):
    # Stands in for the chat turn: the model call is looked up on the chat module.
    return {"answer": chat_turn.call_local_llm("问题一", "事实检索", CONTEXTS), "answer_status": "answered"}


class RecorderTest(unittest.TestCase):
    def test_raw_and_rewritten_answers_are_both_kept(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(chat_turn, "chat", fake_chat), patch.object(chat_turn, "call_local_llm", fake_model):
            out = Path(directory)
            recorder = Recorder(out, iter([{"id": "q1", "question": "问题一"}]))
            recorder.install()
            try:
                result = chat_turn.chat(1, "问题一")
            finally:
                recorder.uninstall()
            self.assertIs(chat_turn.chat, fake_chat)
            self.assertIs(chat_turn.call_local_llm, fake_model)
            call = json.loads((out / "model-calls.jsonl").read_text(encoding="utf-8"))
            answer = json.loads((out / "answers.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(call["id"], "q1")
        self.assertEqual(call["question"], "问题一")
        self.assertEqual(call["raw_answer"], RAW)
        self.assertEqual(call["rewritten_answer"], "李某提醒过没有金融牌照[资料1]。")
        self.assertEqual(result["answer"], call["rewritten_answer"])
        self.assertEqual(answer["id"], "q1")
        self.assertEqual(answer["result"]["answer_status"], "answered")

    def test_a_failed_model_call_is_recorded_and_re_raised(self):
        def failing(*args, **kwargs):
            raise RuntimeError("timeout")

        with tempfile.TemporaryDirectory() as directory, patch.object(chat_turn, "call_local_llm", failing):
            out = Path(directory)
            recorder = Recorder(out, iter([]))
            recorder.install()
            try:
                with self.assertRaises(RuntimeError):
                    chat_turn.call_local_llm("问题", "事实检索", CONTEXTS)
            finally:
                recorder.uninstall()
            call = json.loads((out / "model-calls.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(call["error"], "RuntimeError: timeout")
        self.assertNotIn("raw_answer", call)


if __name__ == "__main__":
    unittest.main()
