"""Synthetic process failures around the durable model port; never real models."""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

from app.db import db_scope
from app.local_backends import LocalModels
from app.model_domain import LocalModelDomainIntegration
from app.runtime_ports import ModelConfig
from tests.test_domain_integration import request


def crash():
    os.killpg(os.getpgrp(), signal.SIGKILL)


def main():
    path, mode = Path(sys.argv[1]), sys.argv[2]
    calls = path.parent / "provider-calls.jsonl"

    def transport(kind, payload, timeout):
        with calls.open("a") as stream:
            stream.write(json.dumps({"kind": kind}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if mode == "dispatched":
            crash()
        return {"model": "llm", "choices": [{"finish_reason": "stop", "message": {
            "content": "合成审批记录 [资料1]。请律师复核原文与上述结论。"}}],
            "usage": {"prompt_tokens": 24, "completion_tokens": 8}}

    models = LocalModels(endpoint="http://127.0.0.1:1/v1",
        identities=json.loads((path.parent / "model-identities.json").read_text()),
        models={kind: kind for kind in ("llm", "embedding", "reranker")}, transport=transport)
    with db_scope(path):
        control = LocalModelDomainIntegration(models=models)
        control.accept(request(model=ModelConfig(use_llm=True), persist_memory=False))
        if mode == "response":
            original = control._extra_operation

            def operation(row, kind, message):
                result = original(row, kind, message)
                if kind == "model":
                    crash()
                return result
            control._extra_operation = operation
        if mode == "journal":
            import app.host_integration as integration
            real_worker = integration.ROOT / "app/host_integration_worker.py"
            wrapper_root = path.parent / "local-model-test-worker"
            (wrapper_root / "app").mkdir(parents=True)
            (wrapper_root / "app/host_integration_worker.py").write_text(
                "import os, signal, runpy\n"
                "from forgeharness.sdk.durable import DurableRunner\n"
                "original = DurableRunner._complete\n"
                "def complete(self, node, *args, **kwargs):\n"
                "    if node.id == 'critic-model': os.killpg(os.getpgrp(), signal.SIGKILL)\n"
                "    return original(self, node, *args, **kwargs)\n"
                "DurableRunner._complete = complete\n"
                f"runpy.run_path({str(real_worker)!r}, run_name='__main__')\n")
            integration.ROOT = wrapper_root
        print(json.dumps(control.drive("domain-process"), ensure_ascii=False))


if __name__ == "__main__":
    main()
