"""M02: bounded server-sourced history, conversation validation and turn writes."""
import json
import os
import unittest
from contextlib import closing
from unittest.mock import patch

from tests.support import IsolatedDatabaseTestCase


def make_conversation(case_id=1, title="多轮测试会话", archived=False):
    from app.storage.db import now, transaction

    with transaction() as conn:
        conversation_id = conn.execute(
            "INSERT INTO conversations(case_id, user_name, title, created_at) VALUES (?, '测试律师', ?, ?)",
            (case_id, title, now()),
        ).lastrowid
        if archived:
            conn.execute(
                "UPDATE conversations SET archived_at=?, archive_expires_at=? WHERE id=?",
                (now(), 2**62, conversation_id),
            )
    return conversation_id


def append_turn(conversation_id, question, answer, *, kind="answer", citations=None):
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
               VALUES (?, 'assistant', ?, ?, '事实检索', '{}', ?, ?)""",
            (conversation_id, answer, json.dumps(citations or [], ensure_ascii=False), kind, now()),
        ).lastrowid
    return user_id, assistant_id


class ContextHistoryTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from app.storage.db import init_db

        init_db(seed=True)

    def test_history_keeps_last_four_complete_turns_and_ignores_orphans(self):
        from app.review.context import load_history

        conversation_id = make_conversation()
        pairs = [
            append_turn(conversation_id, f"问题{i}", f"回答{i}" * 40, citations=[
                {"document_id": 1, "document_name": "01_起诉意见书.txt", "page": 1, "index": 1}
            ])
            for i in range(1, 7)
        ]
        # A failed/incomplete request leaves an unpaired user message.
        from app.storage.db import now, transaction

        with transaction() as conn:
            conn.execute(
                "INSERT INTO messages(conversation_id, role, content, citations_json, route, created_at) VALUES (?, 'user', '未完成问题', '[]', '事实检索', ?)",
                (conversation_id, now()),
            )
        snapshot = load_history(1, conversation_id)
        self.assertTrue(snapshot.enabled)
        self.assertEqual(len(snapshot.turns), 4)
        self.assertEqual(snapshot.dropped_turns, 2)
        self.assertEqual([turn.message_ids for turn in snapshot.turns], pairs[-4:])
        self.assertEqual(snapshot.turns[-1].citations[0]["document_name"], "01_起诉意见书.txt")
        self.assertTrue(snapshot.turns[-1].factual)
        self.assertLessEqual(snapshot.chars, 4000)
        self.assertEqual(len(snapshot.digest), 64)

    def test_cutoff_and_digest_freeze_one_snapshot(self):
        from app.review.context import load_history

        conversation_id = make_conversation()
        append_turn(conversation_id, "问题1", "回答1")
        first = load_history(1, conversation_id)
        append_turn(conversation_id, "问题2", "回答2")
        frozen = load_history(1, conversation_id, cutoff_message_id=first.cutoff_message_id)
        fresh = load_history(1, conversation_id)
        self.assertEqual(len(frozen.turns), 1)
        self.assertEqual(frozen.digest, first.digest)
        self.assertEqual(len(fresh.turns), 2)
        self.assertNotEqual(frozen.digest, fresh.digest)

    def test_budget_drops_whole_old_turns_and_marks_truncation(self):
        from app.review.context import load_history

        conversation_id = make_conversation()
        append_turn(conversation_id, "旧问题", "旧" * 300)
        append_turn(conversation_id, "中问题", "中" * 300)
        append_turn(conversation_id, "新问题", "新" * 300)
        with patch.dict(os.environ, {"LAW_REVIEW_CONTEXT_HISTORY_CHARS": "700"}):
            snapshot = load_history(1, conversation_id)
        self.assertEqual([turn.user_content for turn in snapshot.turns], ["中问题", "新问题"])
        self.assertEqual(snapshot.dropped_turns, 1)
        self.assertFalse(snapshot.truncated)

    def test_single_newest_turn_over_budget_is_truncated_not_dropped(self):
        from app.review.context import load_history

        conversation_id = make_conversation()
        append_turn(conversation_id, "长问题" * 200, "长回答" * 400)
        with patch.dict(os.environ, {"LAW_REVIEW_CONTEXT_HISTORY_CHARS": "400", "LAW_REVIEW_CONTEXT_TURN_CHARS": "200"}):
            snapshot = load_history(1, conversation_id)
        self.assertEqual(len(snapshot.turns), 1)
        self.assertLessEqual(snapshot.chars, 400)
        self.assertTrue(snapshot.truncated)

    def test_history_disabled_returns_empty_snapshot_without_reading(self):
        from app.review.context import load_history

        conversation_id = make_conversation()
        append_turn(conversation_id, "问题", "回答")
        with patch.dict(os.environ, {"LAW_REVIEW_CONTEXT_HISTORY": "off"}):
            snapshot = load_history(1, conversation_id)
        self.assertFalse(snapshot.enabled)
        self.assertEqual(snapshot.turns, ())
        self.assertEqual(snapshot.as_dict()["used"], False)

    def test_non_factual_turns_keep_their_kind(self):
        from app.review.context import load_history, render_history

        conversation_id = make_conversation()
        append_turn(conversation_id, "它呢？", "请确认你指的是哪份材料。", kind="clarification")
        append_turn(conversation_id, "保密期限？", "当前材料没有相关依据。", kind="insufficient_evidence")
        snapshot = load_history(1, conversation_id)
        self.assertEqual([turn.kind for turn in snapshot.turns], ["clarification", "insufficient_evidence"])
        self.assertFalse(any(turn.factual for turn in snapshot.turns))
        rendered = render_history(snapshot.turns)
        self.assertIn("澄清请求", rendered)
        self.assertIn("不是事实结论", rendered)


class ConversationValidationTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from app.storage.db import init_db

        init_db(seed=True)

    def test_missing_cross_case_and_archived_are_distinguished(self):
        from app.review.context import ArchivedConversationError, ConversationNotFoundError, validate_conversation
        from app.storage.db import now, transaction

        with transaction() as conn:
            other_case = conn.execute(
                "INSERT INTO cases(title, status, created_at, updated_at) VALUES ('另一个案件', '进行中', ?, ?)",
                (now(), now()),
            ).lastrowid
        foreign = make_conversation(other_case)
        archived = make_conversation(archived=True)
        with self.assertRaises(ConversationNotFoundError):
            validate_conversation(1, 999999)
        with self.assertRaises(ConversationNotFoundError):
            validate_conversation(1, foreign)
        with self.assertRaises(ArchivedConversationError):
            validate_conversation(1, archived)
        row = validate_conversation(1, archived, require_writable=False)
        self.assertIsNotNone(row["archived_at"])

    def test_chat_rejects_bad_conversation_before_retrieval_or_model(self):
        from app.review.context import ArchivedConversationError, ConversationNotFoundError
        from app.review.services import chat

        archived = make_conversation(archived=True)
        with patch("app.retrieval.rag.HybridRetriever") as retriever, patch("app.review.services.call_local_llm") as model:
            with self.assertRaises(ConversationNotFoundError):
                chat(1, "固定回报是否存在", "测试律师", 999999, True, False)
            with self.assertRaises(ArchivedConversationError):
                chat(1, "固定回报是否存在", "测试律师", archived, True, False)
        retriever.assert_not_called()
        model.assert_not_called()

    def test_chat_returns_bounded_history_identity_on_next_turn(self):
        from app.review.services import chat
        from app.storage.db import connect

        first = chat(1, "张某关于固定回报的陈述是否存在矛盾？", "测试律师", None, False, False)
        self.assertGreater(first["conversation_id"], 0)
        self.assertEqual(first["context"]["history"]["turns"], 0)
        self.assertEqual(first["context"]["policy"]["version"], "multiturn-context-v2")
        second = chat(1, "固定回报是否存在", "测试律师", first["conversation_id"], False, False)
        history = second["context"]["history"]
        self.assertTrue(history["enabled"])
        self.assertEqual(history["turns"], 1)
        self.assertEqual(len(history["message_ids"]), 2)
        self.assertEqual(history["message_ids"][1], history["cutoff_message_id"])
        with closing(connect()) as conn:
            kinds = [row["kind"] for row in conn.execute(
                "SELECT kind FROM messages WHERE conversation_id=? ORDER BY id", (first["conversation_id"],)
            )]
        self.assertEqual(kinds, ["answer", "answer", "answer", "answer"])

    def test_record_turn_is_idempotent_per_source_run(self):
        from app.review.context import record_turn
        from app.storage.db import connect

        conversation_id = make_conversation()
        kwargs = dict(
            question="梳理资金流向", answer="回答[资料1]。请律师复核原文与上述结论。",
            citations=[{"index": 1}], route="事实检索", provenance={"mode": "agent"},
            user_name="测试律师", source_run_id=77,
        )
        first = record_turn(1, conversation_id, **kwargs)
        second = record_turn(1, conversation_id, **kwargs)
        self.assertEqual(first, second)
        with closing(connect()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM messages WHERE source_run_id=77").fetchone()[0]
            messages = conn.execute(
                "SELECT role, kind FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,)
            ).fetchall()
        self.assertEqual(count, 1)
        self.assertEqual([row["role"] for row in messages], ["user", "assistant"])
        self.assertEqual(messages[-1]["kind"], "answer")

    def test_schema_v18_is_incremental_and_old_rows_default(self):
        from app.storage.db import SCHEMA_VERSION, connect, now, transaction

        self.assertGreaterEqual(SCHEMA_VERSION, 18)
        with transaction() as conn:
            conversation_id = conn.execute(
                "INSERT INTO conversations(case_id, user_name, title, created_at) VALUES (1, '旧记录', '旧会话', ?)",
                (now(),),
            ).lastrowid
            conn.execute(
                "INSERT INTO messages(conversation_id, role, content, citations_json, route, created_at) VALUES (?, 'assistant', '旧回答', '[]', '', ?)",
                (conversation_id, now()),
            )
            conn.execute(
                "INSERT INTO agent_runs(case_id, question, route, status, created_at) VALUES (1, '旧运行', '', 'completed', ?)",
                (now(),),
            )
        with closing(connect()) as conn:
            kind = conn.execute("SELECT kind FROM messages ORDER BY id DESC LIMIT 1").fetchone()[0]
            run = conn.execute("SELECT conversation_id, context_json FROM agent_runs ORDER BY id DESC LIMIT 1").fetchone()
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        self.assertEqual(kind, "answer")
        self.assertIsNone(run["conversation_id"])
        self.assertEqual(run["context_json"], "{}")
        self.assertTrue({"kind", "source_run_id"} <= columns)


class ConversationApiTest(IsolatedDatabaseTestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        from app.main import app
        from app.storage.db import init_db

        init_db(seed=True)
        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)

    def test_unknown_conversation_is_rejected_before_generation(self):
        response = self.client.post(
            "/api/cases/1/chat",
            json={"question": "固定回报是否存在", "conversation_id": 999999, "use_llm": False},
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIn("会话", response.json()["detail"])

    def test_archived_conversation_returns_read_only_conflict(self):
        conversation_id = make_conversation(archived=True)
        response = self.client.post(
            "/api/cases/1/chat",
            json={"question": "固定回报是否存在", "conversation_id": conversation_id, "use_llm": False},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("已归档", response.json()["detail"])

    def test_chat_response_carries_conversation_and_context_identity(self):
        response = self.client.post(
            "/api/cases/1/chat",
            json={"question": "固定回报是否存在", "use_llm": False, "use_remote_embeddings": False},
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertGreater(payload["conversation_id"], 0)
        self.assertEqual(payload["context"]["history"]["digest"].__len__(), 64)
        self.assertEqual(payload["context"]["policy"]["history_enabled"], True)


if __name__ == "__main__":
    unittest.main()
