"""Start isolated FastAPI + Next.js for Playwright, with synthetic model output."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import FrameType

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.stub_model import CHAT_MODEL, EMBEDDING_MODEL, FALLBACK_MODEL, StubModel


def seed_failure(directory: Path) -> None:
    """One explicit failed node, for trace UI acceptance rather than model quality."""
    from app.db import db_scope, init_db, now, transaction

    with db_scope(directory / "law_review.db"):
        init_db(seed=True)
        with transaction() as conn:
            run_id = conn.execute(
                "INSERT INTO agent_runs(case_id,question,route,status,created_at) VALUES (1,?,?,?,?)",
                ("合成失败节点验收", "事实检索", "failed", now()),
            ).lastrowid
            conn.execute(
                """INSERT INTO agent_steps(run_id,node_name,agent_role,status,output_json,started_at,finished_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (run_id, "evidence", "证据 Agent", "failed", json.dumps({
                    "summary": "合成节点失败", "diagnostic": {"code": "synthetic_node_failure"},
                    "hidden_chain_of_thought": "NEVER_RENDER_PRIVATE_REASONING",
                }), now(), now()),
            )
        # Real checkpoints interrupted before the memory commit, one per
        # browser project so a successful resume never consumes another test.
        from app.langgraph_agents import LangGraphRunError, ReviewState, create_langgraph_coordinator
        from app.security import Principal, _principal

        def interrupt_memory(node: str, _state: ReviewState) -> None:
            if node == "memory":
                raise RuntimeError("synthetic interruption before memory commit")

        marker = _principal.set(Principal("合成页面验证", True))
        try:
            for project in ("desktop", "mobile"):
                coordinator = create_langgraph_coordinator(
                    1, False, failure_injector=interrupt_memory,
                    checkpoint_path=directory / "checkpoints.sqlite",
                )
                try:
                    coordinator.process_query(f"合成断点恢复验收 {project}", use_llm=False)
                except LangGraphRunError:
                    pass
                else:
                    raise AssertionError("resume fixture must interrupt before memory commit")
        finally:
            _principal.reset(marker)


def main() -> int:
    token = os.environ["LEXVAULT_E2E_TOKEN"]
    if len(token) < 32:
        raise ValueError("E2E token must be randomly generated with at least 32 characters")
    stopped = False

    def stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    processes: list[subprocess.Popen[bytes]] = []
    static = os.getenv("LEXVAULT_E2E_MODE", "static") == "static"
    with tempfile.TemporaryDirectory(prefix="lexvault-next-e2e-") as directory, StubModel(repeatable_fallback=True) as model:
        seed_failure(Path(directory))
        env = {
            **os.environ,
            "LAW_REVIEW_DATA_DIR": directory,
            "LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB": str(Path(directory) / "checkpoints.sqlite"),
            "LAW_REVIEW_AUTH_MODE": "token",
            "LAW_REVIEW_API_TOKENS_JSON": json.dumps({token: {"name": "合成页面验证", "admin": True}}),
            "LAW_REVIEW_LLM_URL": model.base_url,
            "LAW_REVIEW_LLM_MODEL": CHAT_MODEL,
            "LAW_REVIEW_EMBEDDING_URL": model.base_url,
            "LAW_REVIEW_EMBEDDING_MODEL": EMBEDDING_MODEL,
            "LAW_REVIEW_EMBEDDING_BACKEND": "openai",
            "LAW_REVIEW_RERANKER_BACKEND": "heuristic",
            "LAW_REVIEW_TOOL_LOOP": "on",
            "LAW_REVIEW_LLM_FALLBACK": "on",
            "LAW_REVIEW_LLM_FALLBACK_URL": model.base_url,
            "LAW_REVIEW_LLM_FALLBACK_MODEL": FALLBACK_MODEL,
            "LAW_REVIEW_PRICING_JSON": "",
            "LAW_REVIEW_PRICING_FILE": "",
            "LAW_REVIEW_LEGAL_CORPUS_DIR": "",
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "LEXVAULT_API_ORIGIN": "http://127.0.0.1:18765",
            "NEXT_TELEMETRY_DISABLED": "1",
        }
        if static:
            env["LAW_REVIEW_FRONTEND_DIR"] = str(ROOT / "frontend/out")
        try:
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                 "--port", "13000" if static else "18765", "--no-proxy-headers"], cwd=ROOT, env=env,
            ))
            if not static:
                processes.append(subprocess.Popen(
                    ["node", "node_modules/next/dist/bin/next", "start", "--hostname", "127.0.0.1",
                     "--port", "13000"], cwd=ROOT / "frontend", env=env,
                ))
            while not stopped:
                if any(process.poll() is not None for process in processes):
                    return 1
                time.sleep(0.2)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
