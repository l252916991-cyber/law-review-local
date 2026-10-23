"""A real SQLite/LangGraph restore drill with synthetic seed data and no model calls."""
from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.data_snapshot import backup, restore


class RuntimeRestoreTests(unittest.TestCase):
    def test_failed_graph_resumes_from_restored_checkpoint_without_repeating_critic(self) -> None:
        # Imports stay inside the test so importing release tooling never initializes the app.
        from app.db import connect, db_scope, init_db
        from app.langgraph_agents import LangGraphCoordinator, LangGraphRunError

        with tempfile.TemporaryDirectory(prefix="lexvault-restore-drill-") as directory:
            root = Path(directory).resolve()
            source = root / "original"
            source.mkdir()

            def fail_memory(node: str, _state: Any) -> None:
                if node == "memory":
                    raise RuntimeError("synthetic pre-memory interruption")

            with db_scope(source / "law_review.db"):
                init_db(seed=True)
                coordinator = LangGraphCoordinator(1, False, fail_memory, source / "langgraph_checkpoints.sqlite")
                with self.assertRaises(LangGraphRunError) as raised:
                    coordinator.process_query("梳理募集资金流向", use_llm=False)
                run_id = raised.exception.run_id
            backup(source, root / "snapshot", quiescent=True)
            restore(root / "snapshot", root / "restored")
            resumed_nodes: list[str] = []
            with db_scope(root / "restored/law_review.db"):
                coordinator = LangGraphCoordinator(
                    1, False, lambda node, _state: resumed_nodes.append(node),
                    root / "restored/langgraph_checkpoints.sqlite",
                )
                with patch("app.langgraph_agents.CriticAgent.run", side_effect=AssertionError("Critic must not repeat")):
                    result = coordinator.resume(run_id)
                self.assertEqual(result["resume_count"], 1)
                self.assertEqual(resumed_nodes, ["memory"])
                with closing(connect()) as database:
                    self.assertEqual(database.execute("SELECT status FROM agent_runs WHERE id=?", (run_id,)).fetchone()[0], "completed")
            with db_scope(source / "law_review.db"), closing(connect()) as database:
                self.assertEqual(database.execute("SELECT status FROM agent_runs WHERE id=?", (run_id,)).fetchone()[0], "failed")


if __name__ == "__main__":
    unittest.main()
