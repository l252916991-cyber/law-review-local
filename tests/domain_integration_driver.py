"""Only synthetic tests may inject these process-group termination boundaries."""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

from app.integration.domain_integration import IsolatedDomainIntegration
from app.integration.domain_writes import DomainHostWritePort
from app.integration.runtime_ports import WriteAction
from app.storage.db import db_scope
from tests.test_domain_integration import request


def crash():
    os.killpg(os.getpgrp(), signal.SIGKILL)


def main():
    path, mode = Path(sys.argv[1]), sys.argv[2]
    with db_scope(path):
        control = IsolatedDomainIntegration(require_execution_approval=mode == "approval")
        control.accept(request())
        if mode == "accept":
            crash()
        if mode == "node":
            original = control._compute
            def compute(row, action, outputs):
                result = original(row, action, outputs)
                if action["id"] == "retrieve":
                    crash()
                return result
            control._compute = compute
        if mode in {"inside", "effects"}:
            original_apply = DomainHostWritePort._apply
            def apply(self, conn, action, payload):
                result = original_apply(self, conn, action, payload)
                if ((mode == "inside" and action == WriteAction.STEP_RECORD and payload["node_name"] == "retrieve")
                        or (mode == "effects" and action == WriteAction.MEMORY_DRAFT)):
                    crash()
                return result
            DomainHostWritePort._apply = apply
        if mode in {"response", "cancel"}:
            import app.integration.host_integration as integration
            send = integration._send
            def committed(process, message):
                if '"node":"retrieve"' in message.get("content", ""):
                    if mode == "response":
                        crash()
                    control.cancel("domain-process")
                return send(process, message)
            integration._send = committed
        if mode == "journal":
            import app.integration.host_integration as integration
            real_worker = integration.ROOT / "app/integration/host_integration_worker.py"
            wrapper_root = path.parent / "domain-test-worker"
            (wrapper_root / "app/integration").mkdir(parents=True)
            (wrapper_root / "app/integration/host_integration_worker.py").write_text(
                "import os, signal, runpy\n"
                "from forgeharness.sdk.durable import DurableRunner\n"
                "original = DurableRunner._complete\n"
                "def complete(self, node, *args, **kwargs):\n"
                "    if node.id == 'retrieve': os.killpg(os.getpgrp(), signal.SIGKILL)\n"
                "    return original(self, node, *args, **kwargs)\n"
                "DurableRunner._complete = complete\n"
                f"runpy.run_path({str(real_worker)!r}, run_name='__main__')\n")
            integration.ROOT = wrapper_root
        if mode == "projection":
            control._project = lambda *args, **kwargs: crash()
        print(json.dumps(control.drive("domain-process", command="approve" if mode == "approve" else "recover"),
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
