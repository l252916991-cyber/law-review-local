"""Source integrity, complete handoffs, disagreements and runtime recovery."""
from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from app.agents import (
    ContradictionAgent, CriticAgent, EvidenceAgent, FactAgent,
    create_coordinator, get_run_trace, run_specialist, specialist_specs, validate_specialist_output,
)
from app.db import connect, init_db, transaction
from app.langgraph_agents import LangGraphRunError, create_langgraph_coordinator
from app.review_collaboration import build_collaboration, source_reference
from tests.support import IsolatedDatabaseTestCase


CONTEXTS = [
    {"document_id": 1, "page_no": 1, "name": "陈述.txt", "doc_type": "询问笔录",
     "quote": "张某否认参与固定回报审批。", "text": "张某否认参与固定回报审批。"},
    {"document_id": 2, "page_no": 1, "name": "邮件.txt", "doc_type": "电子数据",
     "quote": "张某确认审批了固定回报方案。", "text": "张某确认审批了固定回报方案。"},
]
ANSWER = "张某的陈述需与邮件对照[资料1][资料2]。请律师复核原文与上述结论。"


def specialists(contexts=None):
    contexts = deepcopy(CONTEXTS if contexts is None else contexts)
    return {"facts": FactAgent().run("审批", contexts),
            "evidence": EvidenceAgent().run("审批", contexts),
            "contradiction": ContradictionAgent().run("审批", contexts)}


@pytest.mark.parametrize("field,value", [
    ("document_id", 999), ("page_no", 2), ("quote", "没有出处的内容"),
    ("context_sha256", "0" * 64), ("source_index", True), ("source_index", 3),
])
def test_new_findings_reject_changed_or_forged_source_bindings(field, value):
    output = specialists()["facts"]
    output["facts"][0]["citation"][field] = value
    with pytest.raises(ValueError):
        validate_specialist_output(output, CONTEXTS, schema="facts-v1")


def test_changed_source_text_invalidates_old_handoff_even_when_quote_is_unchanged():
    output = specialists()["facts"]
    changed = deepcopy(CONTEXTS)
    changed[0]["text"] += "后续补充材料。"
    with pytest.raises(ValueError, match="frozen source"):
        build_collaboration(changed, {"facts": output})


def test_controlled_search_hashes_full_page_before_display_truncation():
    from app.agent_tools import _search

    raw = "合成原文\n" * 1200
    contexts = [{**CONTEXTS[0], "text": raw}]
    with patch("app.rag.HybridRetriever.retrieve", return_value=(contexts, {})):
        items, _ = _search(1, {"query": "合成", "limit": 1}, False)
    assert len(items[0]["text"]) < len(raw)
    assert items[0]["page_text_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert items[0]["page_text_sha256"] != hashlib.sha256(items[0]["text"].encode()).hexdigest()


def test_complete_handoff_keeps_findings_and_does_not_assert_entailment():
    outputs = specialists()
    review = build_collaboration(CONTEXTS, outputs)
    assert review["status"] == "ready_for_lawyer_review"
    assert review["specialists"] == outputs
    assert len(review["source_checks"]) == 2
    assert all(check["source_matched"] for check in review["source_checks"])
    assert all(check["semantic_support"] == "not_checked" for check in review["source_checks"])
    assert all(check["independent_corroboration"] == "not_checked" for check in review["source_checks"])
    assert review["semantic_entailment_checked"] is False
    assert review["lawyer_review_required"] is True
    outputs["facts"]["facts"].clear()
    assert len(review["specialists"]["facts"]["facts"]) == 2


def test_legacy_and_missing_specialists_do_not_gain_verified_source_status():
    outputs = specialists()
    outputs["facts"] = {"summary": "旧版", "facts": [{"statement": "审批", "source": "资料1"}]}
    review = build_collaboration(CONTEXTS, outputs)
    assert review["status"] == "needs_review"
    assert review["source_checks"][0]["source_matched"] is False
    assert review["source_checks"][0]["citation"] is None
    del outputs["evidence"]
    assert {item["code"] for item in build_collaboration(CONTEXTS, outputs)["issues"]} >= {
        "missing_findings", "legacy_unbound_findings", "fact_source_not_matched",
    }


def test_same_page_and_claimed_lawyer_approval_are_rejected():
    outputs = specialists()
    outputs["contradiction"]["conflicts"][0]["citations"][1] = source_reference(CONTEXTS, 1)
    with pytest.raises(ValueError, match="different source pages"):
        build_collaboration(CONTEXTS, outputs)
    outputs = specialists()
    outputs["facts"]["facts"][0]["review_status"] = "approved"
    with pytest.raises(ValueError, match="lawyer approval"):
        build_collaboration(CONTEXTS, outputs)


def test_oversize_handoff_fails_explicitly_instead_of_dropping_findings():
    outputs = specialists()
    outputs["facts"]["summary"] = "大" * 40_000
    with pytest.raises(ValueError, match="input bound"):
        build_collaboration(CONTEXTS, outputs)


def test_model_sees_full_findings_as_untrusted_data_and_cannot_omit_disagreements():
    outputs = specialists()
    outputs["gap_detection"] = {"summary": "有待补事项", "gaps": [
        {"type": "缺少原件", "severity": "高", "description": "尚未取得邮件原始载体", "suggestion": "调取邮件原件"},
    ]}
    critic = CriticAgent(allow_tool_loop=False)
    with patch("app.agents.call_local_llm", return_value=ANSWER) as model:
        result = critic.run("审批是否存在矛盾？", "多文档对比", CONTEXTS, outputs, True,
                            object_note="仅看本案", history="历史指代", memory="草稿线索")
    assert model.call_count == 1
    assert model.call_args.args[0] == "审批是否存在矛盾？"
    note = model.call_args.kwargs["object_note"]
    assert "不可信数据" in note and "仅看本案" in note
    assert CONTEXTS[0]["quote"] in note and CONTEXTS[1]["quote"] in note
    assert '"finding_id": "fact-1"' in note and "尚未取得邮件原始载体" in note
    assert model.call_args.kwargs["history"] == "历史指代"
    assert model.call_args.kwargs["memory"] == "草稿线索"
    assert "保留的候选矛盾" in result["answer"] and "张某否认参与" in result["answer"]
    assert "调取邮件原件" in result["answer"]
    assert result["validation"]["valid"]
    assert result["collaboration"]["semantic_entailment_checked"] is False


def test_appendix_cannot_rescue_an_invalid_model_answer():
    outputs = specialists()
    outputs["facts"] = {"facts": [], "summary": "没有事实摘录"}
    with patch("app.agents.call_local_llm", return_value="没有引用的答案。请律师复核。"):
        result = CriticAgent().run("审批", "多文档对比", CONTEXTS, outputs, True)
    assert not result["llm_used"]
    assert "保留的候选矛盾" in result["answer"]
    assert not result["validation"]["valid"]
    assert not result["memory_eligible"]


def test_statute_text_stays_outside_the_case_fact_prompt():
    outputs = specialists()
    outputs["statutory_conflict"] = {"status": "needs_review", "summary": "待核验",
                                     "statutes": [{"text": "仅属于法条命名空间的正文"}]}
    with patch("app.agents.call_local_llm", return_value=ANSWER) as model:
        result = CriticAgent().run("审批适用法律？", "事实检索", CONTEXTS, outputs, True)
    assert "仅属于法条命名空间的正文" not in model.call_args.kwargs["object_note"]
    assert result["collaboration"]["specialists"]["statutory_conflict"] == outputs["statutory_conflict"]
    assert not result["memory_eligible"]


def test_missing_branch_is_visible_and_blocks_draft_memory():
    outputs = specialists()
    del outputs["evidence"]
    result = CriticAgent().run("审批", "多文档对比", CONTEXTS, outputs, False)
    assert result["collaboration"]["status"] == "needs_review"
    assert "不能视为完整分析" in result["answer"]
    assert not result["memory_eligible"]


class CollaborationRuntimeTests(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        init_db(seed=True)

    def graph(self, **kwargs):
        from app.db import get_db_path
        return create_langgraph_coordinator(1, False, checkpoint_path=Path(get_db_path()).parent / "checkpoints.sqlite", **kwargs)

    def test_native_and_langgraph_deliver_the_same_complete_review_and_gap_inputs(self):
        question = "张某审批陈述是否矛盾，证据存在什么疏漏？"
        native = create_coordinator(1, False).process_query(question, use_llm=False, persist_memory=False)
        graph = self.graph().process_query(question, use_llm=False, persist_memory=False)
        self.assertEqual(native["collaboration"], graph["collaboration"])
        self.assertEqual(native["answer"], graph["answer"])
        self.assertTrue(native["collaboration"]["source_checks"])
        for result in (native, graph):
            trace = get_run_trace(result["run_id"])
            critic = next(step for step in trace["steps"] if step["node_name"] == "critic")
            gap = next(step for step in trace["steps"] if step["node_name"] == "gap_detection")
            self.assertEqual(critic["output"]["collaboration"], result["collaboration"])
            self.assertEqual(gap["output"]["upstream_review"]["source_checks"], result["collaboration"]["source_checks"])

    def test_gap_node_consumes_missing_upstream_findings(self):
        coordinator = create_coordinator(1, False)
        spec = specialist_specs(coordinator)["gap_detection"]
        upstream = specialists()
        upstream["facts"] = {"facts": [], "summary": "未获得事实"}
        result = run_specialist(spec, "检查疏漏", CONTEXTS, upstream)
        self.assertEqual(result["upstream_review"]["status"], "needs_review")
        self.assertIn("专家来源核对待补", {gap["type"] for gap in result["gaps"]})
        self.assertGreaterEqual(result["severity_distribution"]["high"], 1)

    def test_resume_reuses_the_saved_complete_review_without_repeating_experts(self):
        def fail_memory(node, state):
            if node == "memory":
                raise RuntimeError("synthetic interruption after review")
        with self.assertRaises(LangGraphRunError) as failed:
            self.graph(failure_injector=fail_memory).process_query("张某审批陈述是否矛盾？", use_llm=False)
        trace = get_run_trace(failed.exception.run_id)
        saved = next(step["output"]["collaboration"] for step in trace["steps"] if step["node_name"] == "critic")
        with patch.object(FactAgent, "run", side_effect=AssertionError("facts repeated")), \
                patch.object(EvidenceAgent, "run", side_effect=AssertionError("evidence repeated")), \
                patch.object(CriticAgent, "run", side_effect=AssertionError("critic repeated")):
            resumed = self.graph().resume(failed.exception.run_id)
        self.assertEqual(resumed["collaboration"], saved)
        self.assertEqual(resumed["resume_count"], 1)

    def test_resume_rejects_changed_page_before_consuming_retry_or_reusing_findings(self):
        def fail_memory(node, state):
            if node == "memory":
                raise RuntimeError("synthetic interruption after review")
        with self.assertRaises(LangGraphRunError) as failed:
            self.graph(failure_injector=fail_memory).process_query("张某审批陈述是否矛盾？", use_llm=False)
        trace = get_run_trace(failed.exception.run_id)
        retrieval = next(step["output"] for step in trace["steps"] if step["node_name"] == "retrieve")
        page = retrieval["contexts"][0]
        with connect() as conn:
            original_text = conn.execute("SELECT text FROM pages WHERE document_id=? AND page_no=?",
                                         (page["document_id"], page["page_no"])).fetchone()[0]
        try:
            with transaction() as conn:
                conn.execute("UPDATE pages SET text=? WHERE document_id=? AND page_no=?",
                             (original_text + "新补充的合成说明。", page["document_id"], page["page_no"]))
            with patch.object(CriticAgent, "run", side_effect=AssertionError("critic repeated")), \
                    self.assertRaisesRegex(ValueError, "内容已变化"):
                self.graph().resume(failed.exception.run_id)
            with connect() as conn:
                row = conn.execute("SELECT status,resume_count FROM agent_runs WHERE id=?", (failed.exception.run_id,)).fetchone()
            self.assertEqual((row["status"], row["resume_count"]), ("failed", 0))
        finally:
            with transaction() as conn:
                conn.execute("UPDATE pages SET text=? WHERE document_id=? AND page_no=?",
                             (original_text, page["document_id"], page["page_no"]))
