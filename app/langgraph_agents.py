"""LangGraph runtime for the existing grounded law-review workflow.

The business nodes deliberately reuse app.agents.  LangGraph owns only graph
topology, checkpointing, parallel fan-out/fan-in, and failure resumption.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from copy import deepcopy
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, Callable, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .agents import (
    CriticAgent,
    EvidenceAgent,
    FactAgent,
    GapDetectionAgent,
    PlannerAgent,
    RetrievalAgent,
    ContradictionAgent,
    _citation,
    answer_contract,
    failure_diagnostic,
)
from .db import connect, db_scope, get_db_path, now, transaction
from .rag import recall_memories, remember


logger = logging.getLogger(__name__)


def _merge_dicts(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {**(left or {}), **(right or {})}


def _with_database_scope(method):
    """Keep every orchestration read/write on the coordinator's captured DB."""
    @wraps(method)
    def scoped(self, *args, **kwargs):
        with db_scope(self.db_path):
            return method(self, *args, **kwargs)
    return scoped


class ReviewState(TypedDict, total=False):
    case_id: int
    run_id: int
    question: str
    user_name: str
    use_llm: bool
    prefer_remote_embeddings: bool
    persist_memory: bool
    route: str
    plan: list[dict[str, Any]]
    memory_hits: list[dict[str, Any]]
    memory_snapshot: list[dict[str, Any]]
    contexts: list[dict[str, Any]]
    retrieval_metrics: dict[str, Any]
    specialist_outputs: Annotated[dict[str, dict[str, Any]], _merge_dicts]
    critic: dict[str, Any]
    memory_result: dict[str, Any]
    node_status: Annotated[dict[str, str], _merge_dicts]


class LangGraphRunError(RuntimeError):
    """Preserve the recoverable run identifier across the API boundary."""

    def __init__(self, message: str, run_id: int, resumable: bool = True, resume_count: int = 0):
        super().__init__(message)
        self.run_id = run_id
        self.resumable = resumable
        self.runtime = "langgraph"
        self.checkpoint_thread_id = f"agent-run-{run_id}"
        self.resume_count = resume_count


FailureInjector = Callable[[str, ReviewState], None]


class LangGraphCoordinator:
    runtime = "langgraph"

    def __init__(
        self,
        case_id: int,
        prefer_remote_embeddings: bool = True,
        failure_injector: FailureInjector | None = None,
        checkpoint_path: str | Path | None = None,
    ):
        self.case_id = case_id
        self.prefer_remote_embeddings = prefer_remote_embeddings
        self.failure_injector = failure_injector
        self.db_path = get_db_path()
        configured = os.getenv("LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB", "").strip()
        self.checkpoint_path = Path(checkpoint_path or configured or self.db_path.parent / "langgraph_checkpoints.sqlite")

    def _start_run(self, question: str, route: str) -> tuple[int, str]:
        with db_scope(self.db_path), transaction() as conn:
            run_id = conn.execute(
                """INSERT INTO agent_runs(case_id, question, route, status, runtime, created_at)
                   VALUES (?, ?, ?, 'running', 'langgraph', ?)""",
                (self.case_id, question, route, now()),
            ).lastrowid
            thread_id = f"agent-run-{run_id}"
            conn.execute(
                "UPDATE agent_runs SET checkpoint_thread_id=? WHERE id=?",
                (thread_id, run_id),
            )
        return run_id, thread_id

    def _record_step(
        self,
        run_id: int,
        node_name: str,
        role: str,
        status: str,
        input_data: dict[str, Any],
        output_data: dict[str, Any],
        started_at: str,
        latency_ms: int,
    ) -> dict[str, Any]:
        finished_at = now()
        with db_scope(self.db_path), transaction() as conn:
            conn.execute(
                """
                INSERT INTO agent_steps(run_id, node_name, agent_role, status, input_json, output_json,
                                        latency_ms, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, node_name) DO UPDATE SET
                    agent_role=excluded.agent_role,
                    status=excluded.status,
                    input_json=excluded.input_json,
                    output_json=excluded.output_json,
                    latency_ms=excluded.latency_ms,
                    started_at=excluded.started_at,
                    finished_at=excluded.finished_at
                """,
                (
                    run_id,
                    node_name,
                    role,
                    status,
                    json.dumps(input_data, ensure_ascii=False),
                    json.dumps(output_data, ensure_ascii=False),
                    latency_ms,
                    started_at,
                    finished_at,
                ),
            )
            step_id = conn.execute(
                "SELECT id FROM agent_steps WHERE run_id=? AND node_name=?", (run_id, node_name)
            ).fetchone()[0]
        return {
            "id": step_id,
            "node": node_name,
            "role": role,
            "status": status,
            "latency_ms": latency_ms,
            "summary": output_data.get("summary", output_data.get("citation_check", status)),
        }

    def _run_node(
        self,
        state: ReviewState,
        node_name: str,
        role: str,
        input_data: dict[str, Any],
        function: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        started_at = now()
        started = time.perf_counter()
        try:
            with db_scope(self.db_path):
                if self.failure_injector:
                    self.failure_injector(node_name, state)
                output = function()
        except Exception as exc:
            latency_ms = round((time.perf_counter() - started) * 1000)
            diagnostic = failure_diagnostic(exc, node_name)
            logger.warning("agent_node_failed runtime=langgraph run_id=%s node=%s error_type=%s",
                           state["run_id"], node_name, type(exc).__name__)
            self._record_step(
                state["run_id"],
                node_name,
                role,
                "failed",
                input_data,
                {"summary": diagnostic["code"], "diagnostic": diagnostic},
                started_at,
                latency_ms,
            )
            raise
        latency_ms = round((time.perf_counter() - started) * 1000)
        self._record_step(
            state["run_id"], node_name, role, "completed", input_data, output, started_at, latency_ms
        )
        return output

    @staticmethod
    def _plan_names(state: ReviewState) -> set[str]:
        return {item["name"] for item in state.get("plan", [])}

    def _planner_node(self, state: ReviewState) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            route, nodes = PlannerAgent().build_plan(state["question"])
            plan = [
                {
                    "name": node.name,
                    "role": node.role,
                    "depends_on": list(node.depends_on),
                    "objective": node.objective,
                }
                for node in nodes
            ]
            return {"summary": f"生成 {len(plan)} 节点 DAG", "route": route, "plan": plan}

        output = self._run_node(
            state, "planner", "规划 Agent", {"question": state["question"]}, run
        )
        return {"route": output["route"], "plan": output["plan"]}

    def _memory_recall_node(self, state: ReviewState) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            items = deepcopy(state["memory_snapshot"]) if "memory_snapshot" in state else recall_memories(
                state["case_id"],
                state["question"],
                3,
                state["prefer_remote_embeddings"],
            )
            return {"items": items, "summary": f"召回 {len(items)} 条案件长期记忆"}

        output = self._run_node(
            state, "memory_recall", "记忆 Agent", {"query": state["question"]}, run
        )
        return {"memory_hits": output["items"]}

    def _retrieval_node(self, state: ReviewState) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            output = RetrievalAgent(
                state["case_id"], state["prefer_remote_embeddings"]
            ).retrieve(state["question"], 6)
            output["summary"] = f"双路召回后融合 {len(output['contexts'])} 条页级证据"
            return output

        output = self._run_node(
            state, "retrieve", "检索 Agent", {"query": state["question"]}, run
        )
        return {"contexts": output["contexts"], "retrieval_metrics": output["metrics"]}

    def _facts_node(self, state: ReviewState) -> dict[str, Any]:
        output = self._run_node(
            state,
            "facts",
            "事实 Agent",
            {"context_count": len(state.get("contexts", []))},
            lambda: FactAgent().run(state["question"], state.get("contexts", [])),
        )
        return {"specialist_outputs": {"facts": output}}

    def _evidence_node(self, state: ReviewState) -> dict[str, Any]:
        output = self._run_node(
            state,
            "evidence",
            "证据 Agent",
            {"context_count": len(state.get("contexts", []))},
            lambda: EvidenceAgent().run(state["question"], state.get("contexts", [])),
        )
        return {"specialist_outputs": {"evidence": output}}

    def _contradiction_node(self, state: ReviewState) -> dict[str, Any]:
        if "contradiction" not in self._plan_names(state):
            return {"node_status": {"contradiction": "skipped"}}
        output = self._run_node(
            state,
            "contradiction",
            "矛盾 Agent",
            {"context_count": len(state.get("contexts", []))},
            lambda: ContradictionAgent().run(state["question"], state.get("contexts", [])),
        )
        return {"specialist_outputs": {"contradiction": output}}

    def _gap_detection_node(self, state: ReviewState) -> dict[str, Any]:
        if "gap_detection" not in self._plan_names(state):
            return {"node_status": {"gap_detection": "skipped"}}
        output = self._run_node(
            state,
            "gap_detection",
            "疏漏 Agent",
            {
                "context_count": len(state.get("contexts", [])),
                "dependencies": sorted(state.get("specialist_outputs", {})),
            },
            lambda: GapDetectionAgent(state["case_id"]).run(
                state["question"], state.get("contexts", [])
            ),
        )
        return {"specialist_outputs": {"gap_detection": output}}

    def _critic_node(self, state: ReviewState) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            output = CriticAgent().run(
                state["question"],
                state["route"],
                state.get("contexts", []),
                state.get("specialist_outputs", {}),
                state["use_llm"],
            )
            output["summary"] = (
                f"引用检查 {output['citation_check']}，LLM={'on' if output['llm_used'] else 'off'}"
            )
            return output

        output = self._run_node(
            state,
            "critic",
            "审校 Agent",
            {"specialists": sorted(state.get("specialist_outputs", {}))},
            run,
        )
        return {"critic": output}

    @staticmethod
    def _persist_gaps(state: ReviewState) -> None:
        gaps = state.get("specialist_outputs", {}).get("gap_detection", {}).get("gaps", [])
        if not gaps:
            return
        with transaction() as conn:
            for gap in gaps:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO gap_detections(
                        case_id, run_id, gap_type, severity, description,
                        suggestion, affected_evidence_ids, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        state["case_id"],
                        state["run_id"],
                        gap["type"],
                        gap["severity"],
                        gap["description"],
                        gap["suggestion"],
                        json.dumps(gap.get("affected_evidence_ids", []), ensure_ascii=False),
                        now(),
                    ),
                )

    @staticmethod
    def _remember_once(state: ReviewState) -> int:
        with transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM memories WHERE source_run_id=?", (state["run_id"],)
            ).fetchone()
        if existing:
            return existing[0]
        return remember(
            state["case_id"],
            state["user_name"],
            state["critic"]["answer"],
            state["run_id"],
            0.75,
            state["prefer_remote_embeddings"],
            validated=True,
        )

    def _memory_node(self, state: ReviewState) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            self._persist_gaps(state)
            if not state["persist_memory"] or not state["critic"].get("memory_eligible", False):
                return {
                    "memory_id": None,
                    "persisted": False,
                    "summary": "对比模式未写入长期记忆" if not state["persist_memory"] else "模型失败或答案未通过校验，未写入长期记忆",
                }
            memory_id = self._remember_once(state)
            return {
                "memory_id": memory_id,
                "persisted": True,
                "summary": "通过结构校验的待律师复核草稿已写入长期记忆",
            }

        output = self._run_node(
            state, "memory", "记忆 Agent", {"user_name": state["user_name"]}, run
        )
        return {"memory_result": output}

    def _build_graph(self, checkpointer: SqliteSaver):
        builder = StateGraph(ReviewState)
        builder.add_node("planner", self._planner_node)
        builder.add_node("memory_recall", self._memory_recall_node)
        builder.add_node("retrieve", self._retrieval_node)
        builder.add_node("facts", self._facts_node)
        builder.add_node("evidence", self._evidence_node)
        builder.add_node("contradiction", self._contradiction_node)
        builder.add_node("gap_detection", self._gap_detection_node)
        builder.add_node("critic", self._critic_node)
        builder.add_node("memory", self._memory_node)
        builder.add_edge(START, "planner")
        builder.add_edge("planner", "memory_recall")
        builder.add_edge("memory_recall", "retrieve")
        builder.add_edge("retrieve", "facts")
        builder.add_edge("retrieve", "evidence")
        builder.add_edge("retrieve", "contradiction")
        builder.add_edge(["facts", "evidence", "contradiction"], "gap_detection")
        builder.add_edge("gap_detection", "critic")
        builder.add_edge("critic", "memory")
        builder.add_edge("memory", END)
        return builder.compile(checkpointer=checkpointer, name="law-review-langgraph")

    def _checkpoint_connection(self) -> sqlite3.Connection:
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.checkpoint_path, check_same_thread=False)

    @staticmethod
    def _steps(run_id: int) -> list[dict[str, Any]]:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT * FROM agent_steps WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "node": row["node_name"],
                    "role": row["agent_role"],
                    "status": row["status"],
                    "latency_ms": row["latency_ms"],
                    "summary": json.loads(row["output_json"] or "{}").get("summary", row["status"]),
                }
                for row in rows
            ]
        finally:
            conn.close()

    def checkpoint_size_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for path in self.checkpoint_path.parent.glob(f"{self.checkpoint_path.name}*")
            if path.is_file()
        )

    def checkpoint_available(self, thread_id: str) -> bool:
        """Check the exact thread, without creating or changing a checkpoint file."""
        if not thread_id or not self.checkpoint_path.is_file():
            return False
        conn = None
        try:
            conn = sqlite3.connect(f"{self.checkpoint_path.resolve().as_uri()}?mode=ro", uri=True)
            return conn.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id=? AND checkpoint_ns='' LIMIT 1",
                (thread_id,),
            ).fetchone() is not None
        except sqlite3.Error:
            return False
        finally:
            if conn is not None:
                conn.close()

    def _complete(self, state: ReviewState, total_ms: int, thread_id: str) -> dict[str, Any]:
        contexts = state.get("contexts", [])
        citations = [_citation(item, index) for index, item in enumerate(contexts, 1)]
        critic = state["critic"]
        with transaction() as conn:
            conn.execute(
                """UPDATE agent_runs SET status='completed', route=?, final_answer=?, citations_json=?,
                   total_ms=?, finished_at=? WHERE id=?""",
                (
                    state["route"],
                    critic["answer"],
                    json.dumps(citations, ensure_ascii=False),
                    total_ms,
                    now(),
                    state["run_id"],
                ),
            )
            run = conn.execute(
                "SELECT resume_count FROM agent_runs WHERE id=?", (state["run_id"],)
            ).fetchone()
        specialists = state.get("specialist_outputs", {})
        return {
            "run_id": state["run_id"],
            "answer": critic["answer"],
            "route": state["route"],
            "agent_type": "LangGraph Multi-Agent",
            "plan": state.get("plan", []),
            "steps": self._steps(state["run_id"]),
            "tools_used": [
                "BM25",
                "Local Embedding",
                "RRF",
                *[key for key in ("facts", "evidence", "contradiction", "gap_detection") if key in specialists],
                "Critic",
                "Vector Memory",
                "LangGraph Checkpoint",
            ],
            "citations": citations,
            "retrieval_metrics": state.get("retrieval_metrics", {}),
            "memory_hits": state.get("memory_hits", []),
            "llm_used": critic["llm_used"],
            "llm_model": critic["llm_model"],
            "fallback_reason": critic["fallback_reason"],
            "validation": critic.get("validation"),
            "citation_check": critic["citation_check"],
            "failure_diagnostic": critic.get("failure_diagnostic"),
            "memory_persisted": state.get("memory_result", {}).get("persisted", False),
            "total_ms": total_ms,
            "runtime": "langgraph",
            "mode": "langgraph",
            "user_name": state["user_name"],
            "checkpoint_thread_id": thread_id,
            "checkpoint_size_bytes": self.checkpoint_size_bytes(),
            "resumable": False,
            "resume_count": run["resume_count"] if run else 0,
        }

    @_with_database_scope
    def process_query(
        self,
        question: str,
        user_name: str = "本机律师",
        use_llm: bool = True,
        persist_memory: bool = True,
        memory_snapshot: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        route, _ = PlannerAgent().build_plan(question)
        run_id, thread_id = self._start_run(question, route)
        initial: ReviewState = {
            "case_id": self.case_id,
            "run_id": run_id,
            "question": question,
            "user_name": user_name,
            "use_llm": use_llm,
            "prefer_remote_embeddings": self.prefer_remote_embeddings,
            "persist_memory": persist_memory,
            "specialist_outputs": {},
        }
        if memory_snapshot is not None:
            initial["memory_snapshot"] = deepcopy(memory_snapshot)
        started = time.perf_counter()
        conn = None
        try:
            conn = self._checkpoint_connection()
            graph = self._build_graph(SqliteSaver(conn))
            state = graph.invoke(initial, {"configurable": {"thread_id": thread_id}})
            total_ms = round((time.perf_counter() - started) * 1000)
            return self._complete(state, total_ms, thread_id)
        except Exception as exc:
            total_ms = round((time.perf_counter() - started) * 1000)
            diagnostic = failure_diagnostic(exc, "langgraph_run")
            with transaction() as db:
                db.execute(
                    """UPDATE agent_runs SET status='failed', final_answer=?, total_ms=?, finished_at=?
                       WHERE id=?""",
                    (json.dumps(diagnostic), total_ms, now(), run_id),
                )
            raise LangGraphRunError(diagnostic["code"], run_id, self.checkpoint_available(thread_id)) from exc
        finally:
            if conn is not None:
                conn.close()

    @_with_database_scope
    def resume(self, run_id: int) -> dict[str, Any]:
        db = connect()
        try:
            run = db.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        finally:
            db.close()
        if not run or run["case_id"] != self.case_id:
            raise KeyError("Agent 运行记录不存在")
        if run["runtime"] != "langgraph":
            raise ValueError("原生 DAG 运行不支持 checkpoint 恢复")
        if run["status"] != "failed":
            raise ValueError("仅失败的 LangGraph 运行可以恢复")
        thread_id = run["checkpoint_thread_id"]
        if thread_id != f"agent-run-{run_id}":
            raise ValueError("运行 checkpoint 标识不匹配")
        if not self.checkpoint_available(thread_id):
            raise ValueError("运行没有可用的 LangGraph checkpoint")

        # A restored/misconfigured checkpoint database must not execute another
        # case's saved state under this run. Validate before consuming a retry.
        validation_conn = self._checkpoint_connection()
        try:
            snapshot = self._build_graph(SqliteSaver(validation_conn)).get_state(
                {"configurable": {"thread_id": thread_id}}
            )
            saved = snapshot.values
            if saved.get("run_id") != run_id or saved.get("case_id") != self.case_id or saved.get("question") != run["question"]:
                raise ValueError("Checkpoint 与运行记录不一致，拒绝恢复")
        finally:
            validation_conn.close()

        with transaction() as db:
            claimed = db.execute(
                """UPDATE agent_runs SET status='running', resume_count=resume_count+1,
                   finished_at='' WHERE id=? AND status='failed'""",
                (run_id,),
            )
            if claimed.rowcount != 1:
                raise ValueError("运行已被其他请求恢复")
        started = time.perf_counter()
        previous_ms = int(run["total_ms"] or 0)
        conn = None
        try:
            conn = self._checkpoint_connection()
            graph = self._build_graph(SqliteSaver(conn))
            state = graph.invoke(None, {"configurable": {"thread_id": thread_id}})
            total_ms = previous_ms + round((time.perf_counter() - started) * 1000)
            return self._complete(state, total_ms, thread_id)
        except Exception as exc:
            total_ms = previous_ms + round((time.perf_counter() - started) * 1000)
            diagnostic = failure_diagnostic(exc, "langgraph_resume")
            with transaction() as db:
                db.execute(
                    """UPDATE agent_runs SET status='failed', final_answer=?, total_ms=?, finished_at=?
                       WHERE id=?""",
                    (json.dumps(diagnostic), total_ms, now(), run_id),
                )
            raise LangGraphRunError(
                diagnostic["code"], run_id, self.checkpoint_available(thread_id), int(run["resume_count"]) + 1
            ) from exc
        finally:
            if conn is not None:
                conn.close()


def create_langgraph_coordinator(
    case_id: int,
    prefer_remote_embeddings: bool = True,
    failure_injector: FailureInjector | None = None,
    checkpoint_path: str | Path | None = None,
) -> LangGraphCoordinator:
    return LangGraphCoordinator(
        case_id,
        prefer_remote_embeddings,
        failure_injector=failure_injector,
        checkpoint_path=checkpoint_path,
    )


def resume_langgraph_run(run_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        run = conn.execute("SELECT case_id FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    finally:
        conn.close()
    if not run:
        raise KeyError("Agent 运行记录不存在")
    return LangGraphCoordinator(run["case_id"]).resume(run_id)


# answer_contract is re-exported from app.agents for backwards compatibility.
