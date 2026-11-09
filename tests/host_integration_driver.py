"""Test-only process boundary injection. Never shipped as a product entrypoint."""
from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from app.integration.host_integration import IsolatedHostIntegration
from app.integration.host_writes import ISOLATED_RUNTIME, LocalHostWritePort
from app.integration.runtime_adapter import current_actor_identity
from app.integration.runtime_ports import ModelConfig, RunRequest, RunScope, WriteAction
from app.storage.db import db_scope


def request():
    return RunRequest(request_id="process-request", scope=RunScope(1), actor=current_actor_identity(),
                      question="合成闭环请求", runtime=ISOLATED_RUNTIME,
                      model=ModelConfig(use_llm=False), use_remote_embeddings=False)


def crash():
    os.killpg(os.getpgrp(), signal.SIGKILL)


def main():
    path, mode = Path(sys.argv[1]), sys.argv[2]
    with db_scope(path):
        control = IsolatedHostIntegration(require_execution_approval=mode == "approval")
        control.accept(request())
        if mode == "accept":
            crash()
        if mode in {"before", "inside"}:
            original = LocalHostWritePort._apply

            def apply(self, conn, action, payload):
                if action == WriteAction.MEMORY_DRAFT and mode == "before":
                    crash()
                result = original(self, conn, action, payload)
                if action == WriteAction.MEMORY_DRAFT:
                    crash()
                return result
            LocalHostWritePort._apply = apply
        elif mode == "response":
            import app.integration.host_integration as integration
            send = integration._send

            def committed(process, message):
                if (message.get("receipt") or {}).get("action") == WriteAction.MEMORY_DRAFT:
                    crash()
                return send(process, message)
            integration._send = committed
        elif mode == "journal":
            import app.integration.host_integration as integration
            real_worker = integration.ROOT / "app/integration/host_integration_worker.py"
            wrapper_root = path.parent / "test-worker"
            (wrapper_root / "app/integration").mkdir(parents=True)
            (wrapper_root / "app/integration/host_integration_worker.py").write_text(
                "import os, signal, runpy\n"
                "from forgeharness.sdk.durable import DurableRunner\n"
                "original = DurableRunner._complete\n"
                "def complete(self, node, *args, **kwargs):\n"
                "    if node.id == 'draft': os.killpg(os.getpgrp(), signal.SIGKILL)\n"
                "    return original(self, node, *args, **kwargs)\n"
                "DurableRunner._complete = complete\n"
                f"runpy.run_path({str(real_worker)!r}, run_name='__main__')\n")
            integration.ROOT = wrapper_root
        elif mode == "projection":
            IsolatedHostIntegration._project = lambda *args, **kwargs: crash()
        elif mode == "cancel":
            import app.integration.host_integration as integration
            send = integration._send
            cancelled = False

            def cancel_after_commit(process, message):
                nonlocal cancelled
                if message.get("receipt") and not cancelled:
                    cancelled = True
                    control.cancel("process-request")
                return send(process, message)
            integration._send = cancel_after_commit
        result = control.drive("process-request", command="approve" if mode == "approve" else "recover")
        import json
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
