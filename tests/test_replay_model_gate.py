"""The replay endpoint answers each question with that question's saved output, or not at all."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.replay_model_gate import load_recorded, recorded_answer


def _request(question: str) -> dict:
    return {"messages": [{"role": "system", "content": "阅卷助手"},
                         {"role": "user", "content": f"检索路由：事实检索\n原问题：{question}\n[资料1｜卷宗｜第1页]\n原文"}]}


class ReplayEndpointTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_dir = Path(directory.name)
        calls = [
            {"id": "a", "question": "李某称自己提醒过哪项资质问题？", "raw_answer": "没有金融牌照[资料1]"},
            {"id": "b", "question": "张某是谁？", "raw_answer": "法定代表人[资料1]"},
            {"id": "c", "question": "超时的题", "error": "RuntimeError: timeout"},
        ]
        (self.run_dir / "model-calls.jsonl").write_text(
            "".join(json.dumps(call, ensure_ascii=False) + "\n" for call in calls) + "\n", encoding="utf-8")

    def test_calls_without_output_are_not_replayed(self):
        self.assertEqual(set(load_recorded(self.run_dir)), {"李某称自己提醒过哪项资质问题？", "张某是谁？"})

    def test_each_question_gets_its_own_output(self):
        recorded = load_recorded(self.run_dir)
        self.assertEqual(recorded_answer(recorded, _request("张某是谁？")), "法定代表人[资料1]")
        self.assertEqual(recorded_answer(recorded, _request("李某称自己提醒过哪项资质问题？")), "没有金融牌照[资料1]")

    def test_a_question_the_run_never_sent_gets_nothing(self):
        recorded = load_recorded(self.run_dir)
        # A prefix of a recorded question is a different question.
        self.assertIsNone(recorded_answer(recorded, _request("张某")))
        self.assertIsNone(recorded_answer(recorded, _request("王某是谁？")))
        self.assertIsNone(recorded_answer(recorded, {"messages": []}))


if __name__ == "__main__":
    unittest.main()
