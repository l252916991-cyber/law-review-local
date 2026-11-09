"""M03: deterministic follow-up resolution, re-retrieval and non-factual replies."""
import json
import unittest
from unittest.mock import patch

from tests.multiturn_fixtures import DOCUMENTS, SCENARIOS
from tests.support import IsolatedDatabaseTestCase


def create_case_with_documents(document_keys, title="多轮合同案件"):
    from app.storage.db import now, transaction

    with transaction() as conn:
        case_id = conn.execute(
            "INSERT INTO cases(title, status, created_at, updated_at) VALUES (?, '进行中', ?, ?)",
            (title, now(), now()),
        ).lastrowid
        document_ids = {}
        for key in document_keys:
            document = DOCUMENTS[key]
            document_id = conn.execute(
                """INSERT INTO documents(case_id, name, mime_type, pages, status, doc_type, people,
                                        date_range, summary, created_at, updated_at)
                   VALUES (?, ?, 'text/plain', ?, '已索引', ?, ?, ?, ?, ?, ?)""",
                (
                    case_id,
                    document["name"],
                    len(document["pages"]),
                    document["doc_type"],
                    document["people"],
                    document["date_range"],
                    document["summary"],
                    now(),
                    now(),
                ),
            ).lastrowid
            for page_no, text in enumerate(document["pages"], 1):
                conn.execute(
                    "INSERT INTO pages(document_id, page_no, text, summary) VALUES (?, ?, ?, ?)",
                    (document_id, page_no, text, text[:120]),
                )
            document_ids[key] = document_id
    return case_id, document_ids


def make_conversation(case_id, user_name="测试律师"):
    from app.storage.db import now, transaction

    with transaction() as conn:
        return conn.execute(
            "INSERT INTO conversations(case_id, user_name, title, created_at) VALUES (?, ?, '多轮会话', ?)",
            (case_id, user_name, now()),
        ).lastrowid


def append_turn(conversation_id, question, answer, *, kind="answer", citations=None, provenance=None):
    from app.storage.db import now, transaction

    with transaction() as conn:
        user_id = conn.execute(
            """INSERT INTO messages(conversation_id, role, content, citations_json, route, created_at)
               VALUES (?, 'user', ?, '[]', '事实检索', ?)""",
            (conversation_id, question, now()),
        ).lastrowid
        assistant_id = conn.execute(
            """INSERT INTO messages(conversation_id, role, content, citations_json, route, provenance_json,
                                    kind, created_at)
               VALUES (?, 'assistant', ?, ?, '事实检索', ?, ?, ?)""",
            (conversation_id, answer, json.dumps(citations or [], ensure_ascii=False),
             json.dumps(provenance or {}, ensure_ascii=False), kind, now()),
        ).lastrowid
    return user_id, assistant_id


def citation(document_id, name, page, index=1):
    return {
        "index": index,
        "document_id": document_id,
        "document_name": name,
        "page": page,
        "quote": "合成段落",
    }


class ResolutionTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from app.storage.db import init_db

        init_db(seed=False)

    def setUp(self):
        self.case_id, self.ids = create_case_with_documents(SCENARIOS["pair"])
        self.conversation_id = make_conversation(self.case_id)

    def resolve(self, question, cutoff=None):
        from app.review.context import load_history, resolve_question

        history = load_history(self.case_id, self.conversation_id, cutoff_message_id=cutoff)
        return resolve_question(self.case_id, question, history), history

    def test_single_cited_document_resolves_follow_up(self):
        append_turn(
            self.conversation_id, "甲公司采购合同的签署方有哪些？", "甲公司与丙公司[资料1]。",
            citations=[citation(self.ids["jia_contract"], "甲公司采购合同.txt", 1)],
        )
        resolution, history = self.resolve("它的付款期限呢？")
        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.method, "history_citation")
        self.assertEqual(resolution.object_document_ids, (self.ids["jia_contract"],))
        self.assertIn("甲公司采购合同", resolution.retrieval_query)
        self.assertEqual(len(resolution.basis_message_ids), 2)
        self.assertEqual(resolution.history_digest, history.digest)

    def test_multiple_cited_documents_require_clarification(self):
        append_turn(
            self.conversation_id, "两份采购合同的付款期限分别是什么？", "60日与30日[资料1][资料2]。",
            citations=[
                citation(self.ids["jia_contract"], "甲公司采购合同.txt", 2, 1),
                citation(self.ids["yi_contract"], "乙公司采购合同.txt", 2, 2),
            ],
        )
        resolution, _ = self.resolve("它的付款期限呢？")
        self.assertEqual(resolution.status, "needs_clarification")
        self.assertEqual(
            [option["document_id"] for option in resolution.options],
            [self.ids["jia_contract"], self.ids["yi_contract"]],
        )

    def test_explicit_correction_switches_object(self):
        append_turn(
            self.conversation_id, "甲公司采购合同的签署方有哪些？", "甲公司与丙公司[资料1]。",
            citations=[citation(self.ids["jia_contract"], "甲公司采购合同.txt", 1)],
        )
        resolution, _ = self.resolve("不是这份，改看乙公司的合同。它的付款期限呢？")
        self.assertIn(resolution.status, {"explicit", "resolved"})
        self.assertTrue(resolution.correction)
        self.assertEqual(resolution.object_document_ids, (self.ids["yi_contract"],))

    def test_new_conversation_does_not_inherit_old_object(self):
        append_turn(
            self.conversation_id, "甲公司采购合同的签署方有哪些？", "甲公司与丙公司[资料1]。",
            citations=[citation(self.ids["jia_contract"], "甲公司采购合同.txt", 1)],
        )
        fresh = make_conversation(self.case_id)
        from app.review.context import load_history, resolve_question

        resolution = resolve_question(self.case_id, "它的付款期限呢？", load_history(self.case_id, fresh))
        self.assertEqual(resolution.status, "needs_clarification")
        self.assertEqual(resolution.reason, "no_prior_context")
        self.assertEqual(resolution.basis_message_ids, ())
        # Candidate documents may be offered as clarification options, but the
        # new conversation must not claim a prior object.
        self.assertEqual(resolution.object_document_ids, ())

    def test_deleted_history_document_is_not_reused(self):
        append_turn(
            self.conversation_id, "这个合同的签署方有哪些？", "甲公司与丙公司[资料1]。",
            citations=[citation(999999, "已删除合同.txt", 1)],
        )
        resolution, _ = self.resolve("它的付款期限呢？")
        self.assertEqual(resolution.status, "needs_clarification")
        self.assertEqual(resolution.reason, "no_resolvable_object")

    def test_generic_type_word_does_not_override_unique_history_object(self):
        from app.review.context import load_history, resolve_question

        case_id, ids = create_case_with_documents(SCENARIOS["pair_with_supplement"], title="补充协议案件")
        conversation_id = make_conversation(case_id)
        append_turn(
            conversation_id, "不是这份，是补充协议。它的付款期限呢？", "变更后为90日[资料1]。",
            citations=[citation(ids["supplement"], "补充协议.txt", 1)],
        )
        resolution = resolve_question(case_id, "它是对哪份合同的补充？", load_history(case_id, conversation_id))
        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.object_document_ids, (ids["supplement"],))
        self.assertIn("补充协议", resolution.retrieval_query)

    def test_previous_clarification_option_can_be_selected(self):
        append_turn(
            self.conversation_id, "它的付款期限呢？", "请确认你指的是哪份材料：1.《甲公司采购合同.txt》；2.《乙公司采购合同.txt》。",
            kind="clarification",
            provenance={"clarification": {"options": [
                {"document_id": self.ids["jia_contract"], "name": "甲公司采购合同.txt"},
                {"document_id": self.ids["yi_contract"], "name": "乙公司采购合同.txt"},
            ]}},
        )
        resolution, _ = self.resolve("第2份")
        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.method, "clarification_selection")
        self.assertEqual(resolution.object_document_ids, (self.ids["yi_contract"],))

    def test_cross_case_conversation_is_never_resolved(self):
        other_case, other_ids = create_case_with_documents(SCENARIOS["yi_only"], title="另一案件")
        other_conversation = make_conversation(other_case)
        append_turn(
            other_conversation, "乙公司采购合同的付款期限是多少？", "30日[资料1]。",
            citations=[citation(other_ids["yi_contract"], "乙公司采购合同.txt", 2)],
        )
        # A conversation from another case cannot be loaded for this case at all.
        from app.review.context import ConversationNotFoundError, load_history

        with self.assertRaises(ConversationNotFoundError):
            load_history(self.case_id, other_conversation)


class ChatFollowUpTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from app.storage.db import init_db

        init_db(seed=False)

    def setUp(self):
        self.case_id, self.ids = create_case_with_documents(SCENARIOS["pair"])
        from app.review import context

        self.default_policy = context.context_policy()

    def ask(self, question, conversation_id=None, use_llm=False):
        from app.review.chat import chat

        return chat(self.case_id, question, "测试律师", conversation_id, use_llm, False)

    def test_follow_up_retrieves_again_and_cites_only_resolved_document(self):
        first = self.ask("甲公司采购合同的签署方有哪些？")
        self.assertEqual(first["answer_status"], "answered")
        self.assertTrue(all(item["document_id"] == self.ids["jia_contract"] for item in first["citations"]))
        second = self.ask("它的付款期限呢？", first["conversation_id"])
        self.assertEqual(second["answer_status"], "answered")
        self.assertEqual(second["context"]["resolution"]["status"], "resolved")
        self.assertEqual(second["context"]["resolution"]["object_document_ids"], [self.ids["jia_contract"]])
        self.assertIn("60日", second["answer"])
        self.assertTrue(second["citations"])
        self.assertTrue(all(item["document_id"] == self.ids["jia_contract"] for item in second["citations"]))
        self.assertIn("本轮仅检索《甲公司采购合同.txt》", second["answer"])
        self.assertEqual(second["context"]["history"]["turns"], 1)

    def test_correction_replaces_the_previous_object(self):
        first = self.ask("甲公司采购合同的签署方有哪些？")
        corrected = self.ask("不是这份，改看乙公司的合同。它的付款期限呢？", first["conversation_id"])
        self.assertEqual(corrected["answer_status"], "answered")
        self.assertEqual(
            corrected["context"]["resolution"]["object_document_ids"], [self.ids["yi_contract"]]
        )
        self.assertIn("30日", corrected["answer"])
        self.assertTrue(all(item["document_id"] == self.ids["yi_contract"] for item in corrected["citations"]))

    def test_ambiguous_follow_up_returns_clarification_and_accepts_choice(self):
        first = self.ask("两份采购合同的付款期限分别是什么？")
        ambiguous = self.ask("它的付款期限呢？", first["conversation_id"])
        self.assertEqual(ambiguous["answer_status"], "clarification")
        self.assertEqual(ambiguous["clarification"]["reason"], "multiple_prior_objects")
        self.assertEqual(ambiguous["citations"], [])
        self.assertNotIn("[资料", ambiguous["answer"])
        self.assertIn("请确认", ambiguous["answer"])
        self.assertTrue(ambiguous["validation"]["valid"])
        selected = self.ask("乙公司采购合同", ambiguous["conversation_id"])
        self.assertEqual(selected["answer_status"], "answered")
        self.assertEqual(
            selected["context"]["resolution"]["object_document_ids"], [self.ids["yi_contract"]]
        )
        self.assertIn("30日", selected["answer"])
        self.assertTrue(all(item["document_id"] == self.ids["yi_contract"] for item in selected["citations"]))

    def test_clarification_is_saved_as_non_factual_turn(self):
        from app.review.context import load_history

        first = self.ask("两份采购合同的付款期限分别是什么？")
        ambiguous = self.ask("它的付款期限呢？", first["conversation_id"])
        snapshot = load_history(self.case_id, ambiguous["conversation_id"])
        self.assertEqual(snapshot.turns[-1].kind, "clarification")
        self.assertFalse(snapshot.turns[-1].factual)
        self.assertEqual(
            {option["document_id"]: option["name"] for option in snapshot.turns[-1].clarification_options},
            {self.ids["jia_contract"]: "甲公司采购合同.txt", self.ids["yi_contract"]: "乙公司采购合同.txt"},
        )

    def test_ambiguous_new_conversation_is_not_answered(self):
        result = self.ask("它的付款期限呢？")
        self.assertEqual(result["answer_status"], "clarification")
        self.assertEqual(result["clarification"]["reason"], "no_prior_context")
        self.assertGreaterEqual(len(result["clarification"]["options"]), 2)
        self.assertEqual(result["citations"], [])

    def test_missing_current_source_returns_insufficient_evidence(self):
        with patch("app.retrieval.rag.HybridRetriever") as retriever:
            retriever.return_value.retrieve.return_value = ([], {"retrieval_mode": "hybrid_rrf"})
            result = self.ask("甲公司采购合同的保密期限是多少？")
        self.assertEqual(result["answer_status"], "insufficient_evidence")
        self.assertEqual(result["route"], "资料不足")
        self.assertIn("资料不足", result["answer"])
        self.assertEqual(result["citations"], [])
        self.assertNotIn("[资料", result["answer"])
        self.assertTrue(result["validation"]["valid"])
        self.assertEqual(result["clarification"]["reason"], "no_current_supporting_source")

    def test_asked_but_absent_specific_clause_returns_insufficient_evidence(self):
        result = self.ask("甲公司采购合同的保密期限是多少？")
        self.assertEqual(result["answer_status"], "insufficient_evidence")
        self.assertEqual(result["clarification"]["reason"], "asked_topic_not_in_current_sources")
        self.assertEqual(result["citations"], [])

    def test_insufficient_scope_is_limited_to_resolved_document(self):
        first = self.ask("甲公司采购合同的签署方有哪些？")
        with patch("app.retrieval.rag.HybridRetriever") as retriever:
            retriever.return_value.retrieve.return_value = ([], {"retrieval_mode": "hybrid_rrf"})
            result = self.ask("它的保密期限呢？", first["conversation_id"])
        self.assertEqual(result["answer_status"], "insufficient_evidence")
        self.assertIn("《甲公司采购合同.txt》", result["answer"])

    def test_history_conflict_is_not_reused_as_fact(self):
        conversation_id = make_conversation(self.case_id)
        append_turn(
            conversation_id, "甲公司采购合同的付款期限是多少？", "甲公司采购合同的付款期限为验收后30天[资料1]。",
            citations=[citation(self.ids["jia_contract"], "甲公司采购合同.txt", 1)],
        )
        result = self.ask("它的付款期限呢？", conversation_id)
        self.assertEqual(result["answer_status"], "answered")
        self.assertIn("60日", result["answer"])
        self.assertNotIn("30天", result["answer"])
        self.assertTrue(all(item["document_id"] == self.ids["jia_contract"] for item in result["citations"]))

    def test_explicit_question_is_not_rewritten_by_history(self):
        first = self.ask("甲公司采购合同的签署方有哪些？")
        result = self.ask("乙公司采购合同的付款期限是多少？", first["conversation_id"])
        self.assertEqual(result["context"]["resolution"]["status"], "explicit")
        self.assertEqual(
            result["context"]["resolution"]["object_document_ids"], [self.ids["yi_contract"]]
        )
        self.assertIn("30日", result["answer"])
        self.assertTrue(all(item["document_id"] == self.ids["yi_contract"] for item in result["citations"]))


class ModelInputTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from app.storage.db import init_db

        init_db(seed=False)

    def test_history_and_resolved_object_reach_the_model_but_only_current_contexts_cite(self):
        from app.review.chat import chat

        case_id, ids = create_case_with_documents(SCENARIOS["pair"])
        conversation_id = make_conversation(case_id)
        append_turn(
            conversation_id, "甲公司采购合同的签署方有哪些？", "甲公司与丙公司[资料1]。请律师复核原文与上述结论。",
            citations=[citation(ids["jia_contract"], "甲公司采购合同.txt", 1)],
        )
        answer = "付款期限为验收合格后60日内[资料1]。请律师复核原文与上述结论。"
        with patch("app.review.generation.local_llm_available", return_value=(True, "fake-model")), \
                patch("app.review.generation.egress_opener") as opener, \
                patch("app.review.generation.read_json_with_deadline", return_value={
                    "choices": [{"message": {"content": answer}}],
                }):
            result = chat(case_id, "它的付款期限呢？", "测试律师", conversation_id, True, False)
        self.assertEqual(result["answer_status"], "answered")
        self.assertTrue(result["context"]["history"]["used"])
        self.assertEqual(result["context"]["resolution"]["method"], "history_citation")
        request = opener.return_value.open.call_args.args[0]
        payload = json.loads(request.data)
        system, user = payload["messages"][0]["content"], payload["messages"][1]["content"]
        self.assertIn("历史对话与草稿线索只用于理解用户指代", system)
        self.assertIn("引用编号只能来自本轮卷宗片段", system)
        self.assertIn("【历史对话", user)
        self.assertIn("甲公司与丙公司", user)
        self.assertIn("本轮指代对象：《甲公司采购合同.txt》", user)
        self.assertIn("本轮卷宗片段", user)
        # The other contract was not part of this round's retrieval.
        self.assertNotIn("办公家具", user)
        self.assertTrue(all(item["document_id"] == ids["jia_contract"] for item in result["citations"]))

    def test_old_callers_default_to_no_history(self):
        import inspect

        from app.review.generation import call_local_llm

        signature = inspect.signature(call_local_llm)
        for name in ("history", "memory", "object_note"):
            self.assertEqual(signature.parameters[name].default, "")
            self.assertEqual(signature.parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)

    def test_clarification_contract_cannot_be_relaxed_into_a_fact_answer(self):
        from app.review.agents import validate_review_answer
        from app.review.context import validate_context_notice

        self.assertTrue(validate_context_notice("请确认你指的是哪份材料。", "clarification")["valid"])
        self.assertFalse(validate_context_notice("请确认；但付款期限是60日[资料1]。", "clarification")["valid"])
        self.assertFalse(validate_context_notice("请确认，请律师复核原文与上述结论。", "clarification")["valid"])
        self.assertTrue(validate_context_notice("资料不足，请补充材料。", "insufficient_evidence")["valid"])
        self.assertFalse(validate_context_notice("资料不足[资料1]。", "insufficient_evidence")["valid"])
        # The factual contract still requires a current citation.
        self.assertFalse(validate_review_answer("付款期限为60日。请律师复核原文与上述结论。", [{
            "document_id": 1, "page_no": 2, "quote": "60日",
        }])["valid"])


class ChatApiFollowUpTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from app.main import app
        from app.storage.db import init_db

        init_db(seed=False)
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()
        cls.case_id, cls.ids = create_case_with_documents(SCENARIOS["pair"])

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def post(self, question, conversation_id=None):
        return self.client.post(
            f"/api/cases/{self.case_id}/chat",
            json={
                "question": question,
                "conversation_id": conversation_id,
                "use_llm": False,
                "use_remote_embeddings": False,
            },
        )

    def test_api_follow_up_and_clarification_payload(self):
        first = self.post("甲公司采购合同的签署方有哪些？")
        self.assertEqual(first.status_code, 200, first.text)
        follow_up = self.post("它的付款期限呢？", first.json()["conversation_id"])
        self.assertEqual(follow_up.status_code, 200, follow_up.text)
        payload = follow_up.json()
        self.assertEqual(payload["answer_status"], "answered")
        self.assertEqual(payload["context"]["resolution"]["status"], "resolved")
        self.assertTrue(payload["citations"])
        self.assertTrue(all(item["document_id"] == self.ids["jia_contract"] for item in payload["citations"]))
        ambiguous = self.post("它的签署方是谁？", payload["conversation_id"])
        self.assertEqual(ambiguous.status_code, 200, ambiguous.text)
        # A single prior object is still resolvable, not ambiguous.
        self.assertEqual(ambiguous.json()["answer_status"], "answered")


if __name__ == "__main__":
    unittest.main()
