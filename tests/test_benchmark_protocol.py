import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import unified_benchmark_runner as runner
from app.benchmark_metrics import score_lawbench_item, extract_final_amount, extract_final_months
from app.benchmark_reporting import metrics, prompt_for, paired_baseline, source_hashes


class ScoringRegressionTest(unittest.TestCase):
    def test_compound_crime_delimiters_preserved_and_extras_penalized(self):
        compound = "过失损坏广播电视设施、公用电信设施"
        labels = {compound, "盗窃", "诈骗"}
        r = score_lawbench_item("3-3", f"[罪名]{compound}罪;盗窃罪<eoa>", f"罪名:{compound};盗窃", label_space=labels)
        self.assertEqual(r.score, 1)
        self.assertEqual(set(r.parsed_prediction), {compound, "盗窃"})
        self.assertEqual(set(r.parsed_reference), {compound, "盗窃"})
        extra = score_lawbench_item("3-3", f"[罪名]{compound};诈骗<eoa>", f"罪名:{compound}", label_space=labels)
        self.assertAlmostEqual(extra.score, 2 / 3)

    def test_wrong_labels_are_not_parse_failures_or_substring_matches(self):
        cases = [("3-3", "[罪名]合同诈骗罪<eoa>", "罪名:诈骗", {"诈骗"}),
                 ("2-3", "[类别]无法定类别<eoa>", "类别:支付抚养费。", {"支付抚养费"}),
                 ("2-9", "扣押物品", "搜查/扣押", {"搜查/扣押"})]
        for task, prediction, reference, labels in cases:
            with self.subTest(task=task):
                result = score_lawbench_item(task, prediction, reference, label_space=labels)
                self.assertEqual(result.score, 0)
                self.assertFalse(result.parse_failed)
                self.assertTrue(result.parsed_prediction)
        malformed = score_lawbench_item("3-3", "[罪名]<eoa>", "罪名:盗窃", label_space={"盗窃"})
        self.assertTrue(malformed.parse_failed)
        self.assertFalse(malformed.abstained)

    def test_crime_presentation_variants_do_not_rewrite_semantics(self):
        reference = "组织、强迫、引诱、容留、介绍卖淫"
        result = score_lawbench_item("3-3", "[罪名]组织，强迫，引诱，容留，介绍卖淫罪<eoa>", f"罪名:{reference}", label_space={reference})
        self.assertEqual(result.score, 1)
        self.assertEqual(result.parsed_prediction, [reference])
        for prediction in ("[罪名]诈骗，盗窃<eoa>", "涉嫌诈骗罪，但不确定。", "不是诈骗罪"):
            self.assertEqual(score_lawbench_item("3-3", prediction, "罪名:诈骗", label_space={"诈骗", "盗窃"}).score, 0)

    def test_negated_gold_is_not_full_score(self):
        self.assertEqual(score_lawbench_item("3-3", "不是盗窃罪，而是诈骗罪。", "罪名:盗窃").score, 0)

    def test_extra_labels_reduce_score_without_gold_only_lookup(self):
        r = score_lawbench_item("3-3", "[罪名]盗窃;诈骗<eoa>", "罪名:盗窃")
        self.assertAlmostEqual(r.score, 2 / 3)
        self.assertIn("诈骗", r.parsed_prediction)
        self.assertEqual(score_lawbench_item("2-4", "[类别]婚姻家庭、劳动纠纷<eoa>", "婚姻家庭").score, 0)

    def test_prediction_parsing_does_not_depend_on_reference(self):
        for task, answer, refs in [("3-3", "[罪名]盗窃;诈骗<eoa>", ["罪名:盗窃", "罪名:诈骗"]),
                                  ("2-4", "[类别]劳动纠纷<eoa>", ["劳动纠纷", "婚姻家庭"])]:
            self.assertEqual(*[score_lawbench_item(task, answer, ref).parsed_prediction for ref in refs])

    def test_amount_is_final_total_not_any_number(self):
        text = "单项金额1000元，另有2000元，犯罪总金额3000元。"
        self.assertEqual(score_lawbench_item("3-7", text, "犯罪金额:1000元").score, 0)
        self.assertEqual(score_lawbench_item("3-7", text, "犯罪金额:3000元").score, 1)

    def test_amount_formats(self):
        for text, expected in [("[金额]8,500元<eoa>", 8500), ("[金额]1.2万元<eoa>", 12000),
                               ("总金额8500元", 8500), ("[金额]八千五百元<eoa>", 8500),
                               ("日期2018年4月，单项500元", None)]:
            with self.subTest(text=text):
                self.assertEqual(extract_final_amount(text), expected)

    def test_chinese_articles_not_dates(self):
        self.assertEqual(score_lawbench_item("3-1", "[法条]刑法第二百六十四条<eoa>", "法条:刑法第264条").score, 1)
        self.assertEqual(score_lawbench_item("3-1", "2016年案件金额264元", "法条:刑法第264条").parsed_prediction, [])

    def test_duration_is_prediction_not_date_or_range(self):
        for text, expected in [("[刑期]18个月<eoa>", 18), ("判处一年六个月", 18), ("判处1年6个月", 18),
                               ("案发2018年，构成犯罪", None), ("可判三年以上十年以下", None),
                               ("判处6个月或12个月", None)]:
            with self.subTest(text=text):
                self.assertEqual(extract_final_months(text), expected)

    def test_parse_failure_is_not_empty_response(self):
        r = score_lawbench_item("3-4", "被告人构成犯罪。", "刑期:4个月")
        self.assertTrue(r.parse_failed)
        self.assertFalse(r.abstained)
        self.assertEqual(r.score, 0)
        empty = score_lawbench_item("3-4", "", "刑期:4个月")
        self.assertTrue(empty.abstained)
        self.assertFalse(empty.parse_failed)

    def test_entity_extras_reduce_score(self):
        self.assertLess(score_lawbench_item("2-6", "受害人:张某;地点:北京", "受害人:张某").score, 1)

    def test_non_month_reference_is_flagged_not_a_model_failure(self):
        for reference in ("刑期:死刑", "刑期:无期"):
            r = score_lawbench_item("3-4", "[刑期]12个月<eoa>", reference)
            self.assertTrue(r.reference_invalid)
            self.assertFalse(r.parse_failed)
            self.assertFalse(r.abstained)

    def test_malformed_chinese_numbers_do_not_crash(self):
        self.assertEqual(score_lawbench_item("3-1", "千万条", "法条:刑法第264条").score, 0)
        self.assertIsNone(extract_final_amount("[金额]万元<eoa>"))

    def test_option_set_not_truncated(self):
        r = score_lawbench_item("1-2", "[正确答案]A、B<eoa>", "A")
        self.assertEqual(r.parsed_prediction, ["A", "B"])
        self.assertEqual(r.score, 0)


class BenchmarkProtocolTest(unittest.TestCase):
    def test_1000_stratified_unique_reproducible(self):
        rows = runner.load_lawbench_dataset(["all"], 50, 42)
        self.assertEqual(len(rows), 1000)
        self.assertEqual(len({r["question_id"] for r in rows}), 1000)
        self.assertEqual(rows, runner.load_lawbench_dataset(["all"], 50, 42))
        self.assertNotEqual(rows, runner.load_lawbench_dataset(["all"], 50, 43))
        for task in runner.TASK_NAMES:
            self.assertEqual(sum(r["task"] == task for r in rows), 50)
        for row in rows:
            system, prompt = prompt_for(row)
            self.assertIn(row["instruction"].strip(), prompt)
            self.assertIn(row["question"], prompt)
            self.assertIn(runner.TASK_GUIDANCE[row["task"]], system)

    def test_hybrid_guides_only_whitelisted_tasks(self):
        from app.benchmark_reporting import GUIDED_TASK_WHITELIST, SYSTEM_PROMPT

        row = runner.load_lawbench_dataset(["2-2"], 1)[0]
        guided_system, _ = prompt_for(row, strategy="hybrid")
        self.assertIn(runner.TASK_GUIDANCE["2-2"], guided_system)
        row_direct = runner.load_lawbench_dataset(["3-1"], 1)[0]
        direct_system, _ = prompt_for(row_direct, strategy="hybrid")
        self.assertEqual(direct_system, SYSTEM_PROMPT)
        self.assertNotIn(runner.TASK_GUIDANCE["3-1"], direct_system)
        self.assertTrue(GUIDED_TASK_WHITELIST <= set(runner.TASK_NAMES))

    def test_prompt_never_uses_reference(self):
        row = runner.load_lawbench_dataset(["1-2"], 1)[0]
        original = prompt_for(row)
        row["reference"] = "SECRET_GOLD_DO_NOT_SEND"
        self.assertEqual(original, prompt_for(row))
        row.pop("instruction")
        with self.assertRaises(ValueError):
            prompt_for(row)

    def test_summary_keeps_zero_error_and_parse_failure(self):
        rows = [{"score": 1, "prediction": "A", "error": None, "latency_ms": 10},
                {"score": 0, "prediction": "无法解析", "error": None, "parse_failed": True, "latency_ms": 20},
                {"score": 0, "prediction": "", "error": "timeout", "latency_ms": 100}]
        m = metrics(rows)
        self.assertEqual(m["mean_score_all"], 1 / 3)
        self.assertEqual(m["parse_failed"], 1)
        self.assertEqual(m["completed"], 2)
        self.assertEqual(m["failed"], 1)
        self.assertEqual(m["empty_responses"], 0)

    def test_call_records_retries_finish_reason_and_usage(self):
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": "A"}, "finish_reason": "length"}], "usage": {"completion_tokens": 900}, "model": "test"}).encode())
        opener = Mock()
        opener.open.side_effect = [TimeoutError("timeout"), response]
        metadata = {}
        with patch.object(runner.urllib.request, "build_opener", return_value=opener), patch.object(runner.time, "sleep"):
            answer, latency, error = runner.call_model("q", "s", runner.MODEL_CONFIG, 1, metadata)
        self.assertEqual(answer, "A")
        self.assertIsNone(error)
        self.assertGreater(latency, 0)
        self.assertEqual(metadata["attempts"], 2)
        self.assertEqual(len(metadata["attempt_errors"]), 1)
        self.assertEqual(metadata["finish_reason"], "length")
        self.assertEqual(metadata["usage"]["completion_tokens"], 900)

    def test_resume_preserves_completed_rows_and_rejects_changed_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(dataset="lawbench", tasks=["1-2"], limit_per_task=2, sample_seed=42,
                                      run_dir=directory, output_dir=directory, resume=False, retry=0, baseline_results=None)
            with patch.object(runner, "verify_model"), patch.object(runner, "call_model", side_effect=[("[正确答案]A<eoa>", 1, None), KeyboardInterrupt()]):
                with self.assertRaises(KeyboardInterrupt):
                    runner.run_benchmark(args)
            root = Path(directory)
            self.assertEqual(len(list((root / "checkpoints").glob("*.json"))), 1)
            args.resume = True
            with patch.object(runner, "verify_model"), patch.object(runner, "call_model", return_value=("[正确答案]B<eoa>", 1, None)) as call:
                runner.run_benchmark(args)
                self.assertEqual(call.call_count, 1)
            self.assertEqual(len((root / "detailed_results.jsonl").read_text().splitlines()), 2)
            self.assertEqual(json.loads((root / "summary.json").read_text())["status"], "completed")
            args.sample_seed = 43
            with self.assertRaises(ValueError):
                runner.run_benchmark(args)

    def test_correction_locate_strategy_is_wired_end_to_end(self):
        # Locate strategy: the model returns only the fragments, the runner applies
        # them to the source sentence and scores the result. Mirrors the measured
        # 2-1 protocol (+2.04 pt over a single-pass rewrite on all 500 questions).
        record = runner.load_lawbench_dataset(["2-1"], 1)[0]
        record.update(question="句子：借阅档案费按规定及时归还",
                      reference="借阅档案未按规定及时归还")
        record["question_hash"] = runner.hash_text(record["question"])
        located = '{"edits":[{"original":"费按","corrected":"未按"}]}'
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(dataset="lawbench", tasks=["2-1"], limit_per_task=1,
                                      run_dir=directory, output_dir=directory, resume=False, retry=0,
                                      prompt_strategy="correction_locate")
            with patch.object(runner, "load_all_datasets", return_value=[record]), \
                    patch.object(runner, "verify_model"), \
                    patch.object(runner, "call_model", return_value=(located, 1, None)):
                runner.run_benchmark(args)
            row = json.loads((Path(directory) / "checkpoints" / f"{record['question_id']}.json").read_text())
        self.assertEqual(row["prediction"], "借阅档案未按规定及时归还")
        self.assertEqual(row["score"], 1)
        self.assertEqual(row["prompt_version"], "lawbench-task-guided-v4")
        self.assertIn("edits", row["system_prompt"])
        self.assertEqual(row["postprocess"]["policy"], "located-edits; no reference access")

    def test_correction_surface_is_scored_and_resume_checks_provenance(self):
        from app.benchmark_postprocess import POSTPROCESS_VERSION

        raw, corrected = "本院认为，支付 100 元。", "本院认为,支付100元"
        record = runner.load_lawbench_dataset(["2-1"], 1)[0]
        record.update(question="句子：本院认未,支付100元", reference=corrected)
        record["question_hash"] = runner.hash_text(record["question"])
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(dataset="lawbench", tasks=["2-1"], limit_per_task=1,
                                      run_dir=directory, output_dir=directory, resume=False, retry=0)
            with patch.object(runner, "load_all_datasets", return_value=[record]), \
                    patch.object(runner, "verify_model"), \
                    patch.object(runner, "call_model", return_value=(raw, 1, None)):
                runner.run_benchmark(args)
            root = Path(directory)
            checkpoint = root / "checkpoints" / f"{record['question_id']}.json"
            row = json.loads(checkpoint.read_text())
            self.assertEqual(row["original_prediction"], raw)
            self.assertEqual(row["prediction"], corrected)
            self.assertEqual(row["score"], 1)
            self.assertTrue(row["postprocess"]["applied"])
            self.assertEqual(row["postprocess"]["version"], POSTPROCESS_VERSION)
            self.assertIn("app/benchmark_postprocess.py", source_hashes())
            args.resume = True
            with patch.object(runner, "load_all_datasets", return_value=[record]), \
                    patch.object(runner, "verify_model") as verify, \
                    patch.object(runner, "call_model") as call:
                runner.run_benchmark(args)
                call.assert_not_called()
                self.assertEqual(json.loads(checkpoint.read_text()), row)
                verify.reset_mock()
                for key in ("original_prediction", "postprocess"):
                    broken = {k: v for k, v in row.items() if k != key}
                    checkpoint.write_text(json.dumps(broken))
                    with self.assertRaisesRegex(ValueError, "Checkpoint"):
                        runner.run_benchmark(args)
                    verify.assert_not_called()
                checkpoint.write_text(json.dumps(row))
                manifest_path = root / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                del manifest["postprocess"]
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, "Resume manifest differs"):
                    runner.run_benchmark(args)
                verify.assert_not_called()
                call.assert_not_called()

    def test_locate_strategy_applies_edits_and_leaves_baseline_intact(self):
        from app.benchmark_postprocess import apply_located_edits, postprocess
        from app.benchmark_reporting import prompt_for

        question = "句子：借阅档案费按规定及时归还"
        base = {"dataset": "lawbench", "task": "2-1", "question_id": "Q", "instruction": "纠正。",
                "question": question, "reference": "借阅档案未按规定及时归还"}

        in_locate = prompt_for({**base, "prompt_strategy": "correction_locate"}, strategy="correction_locate")[0]
        self.assertIn("edits", in_locate)
        self.assertNotIn("edits", prompt_for({**base, "prompt_strategy": "task_guided"}, strategy="task_guided")[0])

        # A plain rewrite is not the located contract: None keeps baseline behaviour.
        self.assertIsNone(apply_located_edits(question, "借阅档案未按规定及时归还"))
        self.assertIsNone(apply_located_edits(question, "不是 JSON"))
        self.assertIsNone(apply_located_edits(question, '{"note":"没有 edits 键"}'))
        # Located edits are applied to the source; an empty list leaves it unchanged.
        located = '{"edits":[{"original":"费按","corrected":"未按"}]}'
        self.assertEqual(apply_located_edits(question, located), "借阅档案未按规定及时归还")
        self.assertEqual(apply_located_edits(question, '{"edits":[]}'), "借阅档案费按规定及时归还")
        self.assertEqual(apply_located_edits(question, '{"edits":[{"original":"不存在","corrected":"x"}]}'),
                         "借阅档案费按规定及时归还")
        # postprocess routes located output through the applier and says so.
        revised, details = postprocess("2-1", question, located)
        self.assertEqual(revised, "借阅档案未按规定及时归还")
        self.assertEqual(details["policy"], "located-edits; no reference access")
        # Baseline strategy keeps the old policy and surface (trailing 。 stripped).
        revised_plain, details_plain = postprocess("2-1", question, "借阅档案未按规定及时归还。")
        self.assertEqual(revised_plain, "借阅档案未按规定及时归还")
        self.assertEqual(details_plain["policy"], "source-surface-only; no reference access")

    def test_postprocess_applies_to_its_tasks_and_leaves_errors_and_others_untouched(self):
        # A task outside POSTPROCESS_TASKS stays byte-for-byte untouched.
        for task, raw, error in (("2-1", "原文， 100。", "timeout"), ("1-2", "原文， 100。", None),
                                 ("2-1", "", None), ("2-1", "原文", None),
                                 ("2-7", "第一句事实。第二句事实。", None),
                                 ("2-9", "被告通过微信购买药品", None)):
            with self.subTest(task=task, raw=raw, error=error), tempfile.TemporaryDirectory() as directory:
                args = argparse.Namespace(dataset="lawbench", tasks=[task], limit_per_task=1,
                                          run_dir=directory, output_dir=directory, resume=False, retry=0)
                with patch.object(runner, "verify_model"), \
                        patch.object(runner, "call_model", return_value=(raw, 1, error)):
                    runner.run_benchmark(args)
                row = json.loads((Path(directory) / "detailed_results.jsonl").read_text())
                if task in runner.POSTPROCESS_TASKS and not error:
                    self.assertEqual(row["original_prediction"], raw)
                    self.assertEqual(row["postprocess"]["version"], runner.POSTPROCESS_VERSION)
                else:
                    self.assertEqual(row["prediction"], raw)
                    self.assertNotIn("postprocess", row)
                    self.assertNotIn("original_prediction", row)
                if error:
                    self.assertEqual(row["score"], 0)

    def test_postprocess_repairs_are_scored_not_just_recorded(self):
        # 2-9 maps explicit source language to the public ontology; scoring must see the repair.
        record = runner.load_lawbench_dataset(["2-9"], 1)[0]
        record.update(question="被告人通过微信购买药品", reference="买入;联络")
        record["question_hash"] = runner.hash_text(record["question"])
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(dataset="lawbench", tasks=["2-9"], limit_per_task=1,
                                      run_dir=directory, output_dir=directory, resume=False, retry=0)
            with patch.object(runner, "load_all_datasets", return_value=[record]), \
                    patch.object(runner, "verify_model"), \
                    patch.object(runner, "call_model", return_value=("买入", 1, None)):
                runner.run_benchmark(args)
            row = json.loads((Path(directory) / "detailed_results.jsonl").read_text())
            self.assertEqual(row["original_prediction"], "买入")
            self.assertEqual(row["prediction"], "买入;联络")
            self.assertTrue(row["postprocess"]["applied"])
            expected = score_lawbench_item("2-9", row["prediction"], row["reference"], question=row["question"]).score
            self.assertEqual(row["score"], expected)

    def test_resume_requires_original_directory(self):
        args = argparse.Namespace(dataset="lawbench", tasks=["1-2"], limit_per_task=1, resume=True)
        with self.assertRaises(ValueError):
            runner.run_benchmark(args)

    def test_statutory_corpus_replaces_1_1_output_and_is_frozen_on_resume(self):
        import hashlib

        from app.legal_corpus import SCHEMA_VERSION, split_articles

        body = "第一条 当事人订立合同，应当具有相应的民事权利能力和民事行为能力。"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"
            corpus.mkdir()
            document = {"schema_version": SCHEMA_VERSION, "document_id": "contract", "law_name": "合同法",
                        "aliases": ["合同法"], "version_date": "2020-01-01", "effective_date": "2020-02-01",
                        "version_status": "currentness_not_asserted", "source_url": "https://example.invalid/law",
                        "articles": split_articles(body)}
            raw = json.dumps(document).encode()
            (corpus / "contract.json").write_bytes(raw)
            (corpus / "manifest.json").write_text(json.dumps({"schema_version": SCHEMA_VERSION, "documents": [
                {"document_file": "contract.json", "document_sha256": hashlib.sha256(raw).hexdigest()}]}))
            record = runner.load_lawbench_dataset(["1-1"], 1)[0]
            record.update(question="请背诵《合同法》第一条。", reference="当事人订立合同，应当具有相应的民事权利能力和民事行为能力。")
            record["question_hash"] = runner.hash_text(record["question"])
            run_dir = root / "run"

            def namespace(**overrides):
                return argparse.Namespace(
                    **{"dataset": "lawbench", "tasks": ["1-1"], "limit_per_task": 1,
                       "run_dir": str(run_dir), "output_dir": str(root), "resume": False, "retry": 0,
                       "corpus_dir": [corpus], **overrides})

            with patch.object(runner, "load_all_datasets", return_value=[record]), \
                    patch.object(runner, "verify_model"), \
                    patch.object(runner, "call_model", return_value=("完全错误的答案", 1, None)):
                runner.run_benchmark(namespace())
            row = json.loads((run_dir / "detailed_results.jsonl").read_text())
            self.assertEqual(row["original_prediction"], "完全错误的答案")
            self.assertIn("民事行为能力", row["prediction"])
            self.assertEqual(row["retrieval"]["mode"], "exact_article")
            self.assertEqual(row["postprocess"]["version"], runner.POSTPROCESS_VERSION)
            manifest = json.loads((run_dir / "manifest.json").read_text())
            self.assertIn("1-1", manifest["postprocess"]["tasks"])
            self.assertEqual(manifest["retrieval"]["corpus_directories"], [str(corpus.resolve())])
            self.assertTrue(manifest["retrieval"]["corpus_files"])

            # Resume must reproduce the same repair and reject a checkpoint whose frozen retrieval was dropped.
            with patch.object(runner, "load_all_datasets", return_value=[record]), \
                    patch.object(runner, "verify_model"), \
                    patch.object(runner, "call_model") as call:
                runner.run_benchmark(namespace(resume=True))
                call.assert_not_called()
            checkpoint = run_dir / "checkpoints" / f"{record['question_id']}.json"
            for key in ("retrieval", "original_prediction", "postprocess"):
                broken = {k: v for k, v in row.items() if k != key}
                checkpoint.write_text(json.dumps(broken))
                with patch.object(runner, "load_all_datasets", return_value=[record]), \
                        patch.object(runner, "verify_model"):
                    with self.assertRaisesRegex(ValueError, "Checkpoint"):
                        runner.run_benchmark(namespace(resume=True))

    def test_1_1_without_corpus_dir_keeps_model_output(self):
        record = runner.load_lawbench_dataset(["1-1"], 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(dataset="lawbench", tasks=["1-1"], limit_per_task=1,
                                      run_dir=directory, output_dir=directory, resume=False, retry=0)
            with patch.object(runner, "load_all_datasets", return_value=[record]), \
                    patch.object(runner, "verify_model"), \
                    patch.object(runner, "call_model", return_value=("模型原始答案", 1, None)):
                runner.run_benchmark(args)
            row = json.loads((Path(directory) / "detailed_results.jsonl").read_text())
            self.assertEqual(row["prediction"], "模型原始答案")
            self.assertNotIn("postprocess", row)
            self.assertNotIn("retrieval", row)

    def test_offline_verifier_accepts_complete_run_and_detects_tampering(self):
        from verify_benchmark_run import verify

        def fake_model(*args, metadata, **kwargs):
            metadata.update(attempts=1, finish_reason="stop", usage={"completion_tokens": 10})
            return "[正确答案]A<eoa>", 10, None

        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(dataset="lawbench", tasks=["1-2"], limit_per_task=2, sample_seed=42,
                                      run_dir=directory, output_dir=directory, resume=False, retry=0, baseline_results=None)
            with patch.object(runner, "verify_model"), patch.object(runner, "call_model", side_effect=fake_model):
                runner.run_benchmark(args)
            root = Path(directory)
            self.assertTrue(verify(root)["valid"])
            with patch("verify_benchmark_run.source_hashes", return_value={**source_hashes(), "app/benchmark_metrics.py": "changed"}):
                self.assertFalse(verify(root, write=False)["valid"])
                artifacts = verify(root, artifacts_only=True, write=False)
                self.assertTrue(artifacts["valid"])
                self.assertFalse(artifacts["scoring_verified"])
                self.assertFalse(artifacts["live_source_matches"])
            summary_path = root / "summary.json"
            summary = json.loads(summary_path.read_text())
            summary["mean_score_all"] = 999
            summary_path.write_text(json.dumps(summary))
            self.assertIn("Summary mismatch: mean_score_all", verify(root)["issues"])

    def test_offline_audit_preserves_originals_and_rejects_overwrite(self):
        from scripts.audit_benchmark import audit
        from verify_benchmark_run import verify

        def fake_model(*args, metadata, **kwargs):
            metadata.update(attempts=1, finish_reason="stop", usage={"completion_tokens": 10})
            return "[罪名]盗窃;诈骗<eoa>", 10, None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source", root / "audit"
            args = argparse.Namespace(dataset="lawbench", tasks=["3-3"], limit_per_task=2, sample_seed=42,
                                      run_dir=str(source), output_dir=str(root), resume=False, retry=0, baseline_results=None)
            with patch.object(runner, "verify_model"), patch.object(runner, "call_model", side_effect=fake_model):
                runner.run_benchmark(args)
            saved = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
            with patch.object(runner, "call_model", side_effect=AssertionError("Audit must not call model")):
                result = audit(source, output)
            self.assertTrue(result["input_hashes_unchanged"])
            self.assertEqual(result["new_model_calls"], 0)
            self.assertTrue(verify(output, write=False)["scoring_verified"])
            self.assertEqual(saved, {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()})
            with self.assertRaises(FileExistsError):
                audit(source, output)
            with self.assertRaises(ValueError):
                audit(source, source / "nested")

    def test_paired_comparison_uses_same_ids_and_scorer(self):
        old = {"question_id": "3-7_0000", "task": "3-7", "question": "q", "reference": "犯罪金额:1000元",
               "prediction": "单项1000元，总金额3000元", "score": 1, "error": None}
        new = {**old, "prediction": "[金额]1000元<eoa>"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.jsonl"
            path.write_text(json.dumps(old) + "\n")
            result = paired_baseline([new], path)["overall"]
            self.assertEqual(result["old_stored_score"], 1)
            self.assertEqual(result["old_rescored_score"], 0)
            self.assertEqual(result["delta"], 1)
            new["question"] = "changed"
            with self.assertRaises(ValueError):
                paired_baseline([new], path)


if __name__ == "__main__":
    unittest.main()
