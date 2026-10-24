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
    with tempfile.TemporaryDirectory(prefix="lexvault-next-e2e-") as directory, StubModel(repeatable_fallback=True) as model:
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
            "REDIS_URL": "redis://127.0.0.1:1/0",
            "LEXVAULT_API_ORIGIN": "http://127.0.0.1:18765",
            "NEXT_TELEMETRY_DISABLED": "1",
        }
        try:
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                 "--port", "18765", "--no-proxy-headers"], cwd=ROOT, env=env,
            ))
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
