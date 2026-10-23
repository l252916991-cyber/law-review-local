"""Version 2 isolated host-write sidecar; imports only the installed wheel.

This synthetic graph demonstrates the E12/E13 durable boundary. Domain review
nodes remain on the read-only path until E07/E14 are accepted separately.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

import forgeharness
from pydantic import BaseModel, ConfigDict
from forgeharness.sdk import (
    Actor, BusinessReceipt, Capability, DurableGraph, DurableNode, DurableOrchestrator, Harness,
    NoopVerifier, PolicyDecision, PolicyDecisionType, ReconciledNode, ResourceScope,
    RiskLevel, RunBudget, RunContext, ToolCall, ToolEffectClass, ToolOutput, ToolSpec, Usage,
    compose_application, verify_trace,
)

WIRE_VERSION = 2
MAX_BYTES = 2_000_000


def receive() -> dict[str, Any]:
    raw = sys.stdin.buffer.readline(MAX_BYTES + 1)
    if not raw.endswith(b"\n") or len(raw) > MAX_BYTES:
        raise ValueError("missing or oversized host response")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("host response must be an object")
    return value


def send(value: dict[str, Any]) -> None:
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    if len(raw) > MAX_BYTES:
        raise ValueError("oversized sidecar output")
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


class Channel:
    def __init__(self, token: str):
        self.token, self.seq = token, 0
        self.cancel_handler = None
        self.handling_cancel = False

    def call(self, kind: str, **fields: Any) -> dict[str, Any]:
        self.seq += 1
        send({"wire_version": WIRE_VERSION, "token": self.token, "seq": self.seq, "type": kind, **fields})
        reply = receive()
        if (reply.get("wire_version") != WIRE_VERSION or reply.get("token") != self.token
                or reply.get("seq") != self.seq or reply.get("ok") is not True):
            raise PermissionError("host rejected sidecar operation")
        if reply.get("cancel") and self.cancel_handler is not None and not self.handling_cancel:
            self.handling_cancel = True
            try:
                self.cancel_handler()
            finally:
                self.handling_cancel = False
        return reply


def output_for(receipt: dict[str, Any], source: str) -> ToolOutput:
    # Normalize duplicate execution and authoritative query to identical output.
    return ToolOutput(ok=True, content=json.dumps({"receipt_id": receipt["receipt_id"],
                      "result": json.loads(receipt["detail"])}, ensure_ascii=False, sort_keys=True),
                      receipt=BusinessReceipt(resource_id=receipt["receipt_id"], status="applied", version=source,
                                              evidence_refs=("host-receipt:" + receipt["receipt_id"],)))


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node: str


class HostWriteTool:
    input_model = Input
    spec = ToolSpec(name="host_write", description="Execute one registered synthetic host action",
                    input_schema=Input.model_json_schema(), risk=RiskLevel.WRITE,
                    effect_class=ToolEffectClass.IDEMPOTENT, timeout_seconds=30)

    def __init__(self, channel: Channel, source: str):
        self.channel, self.source = channel, source

    async def execute(self, arguments, context):
        if not context.idempotency_key:
            raise ValueError("framework invocation key is required")
        response = self.channel.call("write", node=arguments.node, key=context.idempotency_key)
        if response.get("denied"):
            return ToolOutput(ok=False, content="host admission cancelled or expired; no business effect")
        return output_for(response["receipt"], self.source)


class Policy:
    def __init__(self, channel: Channel, approval: bool):
        self.channel, self.approval = channel, approval

    def evaluate(self, request):
        self.channel.call("validate")
        needs = self.approval and request.call.arguments["node"] == "audit"
        return PolicyDecision(type=PolicyDecisionType.REQUIRE_APPROVAL if needs else PolicyDecisionType.ALLOW,
                              reason="registered isolated execution policy")

    def can_approve(self, *, requester, owner, call):
        self.channel.call("validate")
        return requester.actor == owner.actor and requester.scope == owner.scope


class NoModel:
    async def decide(self, request):
        raise RuntimeError("models are disabled for synthetic host integration")


async def main() -> None:
    init = receive()
    if (init.get("wire_version") != WIRE_VERSION or init.get("version") != forgeharness.__version__
            or forgeharness.__version__ != "0.8.3" or sys.version_info[:2] != (3, 12)):
        raise ValueError("unsupported host-write sidecar identity")
    distribution = importlib.metadata.distribution("forgeharness")
    for name, expected in init["package_files"].items():
        if hashlib.sha256(Path(distribution.locate_file(name)).read_bytes()).hexdigest() != expected:
            raise ValueError("installed framework differs from the bound wheel")
    channel = Channel(init["token"])
    tool = HostWriteTool(channel, init["source_version"])
    nodes = tuple(DurableNode(id=a["id"], kind="tool",
                             dependencies=("joined",) if set(a["dependencies"]) == {"step", "gap"} else tuple(a["dependencies"]),
                             call=ToolCall(id=a["id"], name="host_write", arguments={"node": a["id"]}),
                             max_usage=Usage(tool_calls=1)) for a in init["actions"])
    nodes += (DurableNode(id="joined", kind="join", dependencies=("step", "gap")),)
    graph = DurableGraph(version=init["graph_version"], model_identity="disabled-synthetic-v1",
        resource_versions={"case": init["source_version"], "deadline": str(init["deadline"]),
                           "actions": hashlib.sha256(json.dumps(init["actions"], sort_keys=True,
                                      ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()},
        nodes=nodes, final_node="audit")

    def validate(context, versions=None):
        channel.call("validate")
        return True

    async def reconcile(context, node, key):
        receipt = channel.call("receipt", node=node.id, key=key)["receipt"]
        if receipt is None:
            # No receipt does not prove a dispatched call was never accepted.
            return None
        return ReconciledNode(output=output_for(receipt, init["source_version"]).model_dump(mode="json"),
                              usage=Usage(tool_calls=1), evidence_refs=("host-receipt:" + receipt["receipt_id"],))

    app = compose_application(id="lexvault-host-isolated", version="1", policy_version="1",
        model_factory=NoModel, capabilities=(Capability("host-writes-v1", tools=(tool,)),),
        policy=Policy(channel, init["require_approval"]), verifier=NoopVerifier(),
        budget=RunBudget(max_steps=1, max_tool_calls=len(init["actions"]), max_wall_seconds=init["timeout_seconds"]))
    # authorize_run is a trusted host callback, independent of tool policy.
    from dataclasses import replace
    app = replace(app, authorize_run=validate)
    context = RunContext(application_id=app.id, application_version=app.version, policy_version=app.policy_version,
        run_id=init["run_id"], session_id=init["run_id"], correlation_id=init["run_id"],
        actor=Actor(id=init["actor_id"]), scope=ResourceScope(tenant_id="lexvault-local", resources=(f"case:{init['case_id']}",)))
    harness = Harness(app, state_dir=Path("state"), orchestrator=DurableOrchestrator(
                      graph, validate_resources=validate, reconcile_node=reconcile))

    def propagate_cancel():
        # Guard recursion: SDK authorization itself calls back into the host.
        existing = harness.find(context)
        if existing is not None and existing.status.value not in {"succeeded", "failed", "cancelled", "exhausted"}:
            harness.cancel(context)

    channel.cancel_handler = propagate_cancel
    result = harness.find(context)
    if result is None:
        if init["command"] == "inspect":
            raise ValueError("framework state not yet created")
        if not channel.call("start")["start_allowed"]:
            raise ValueError("missing durable state after start authorization; reconciliation required")
        result = await harness.run(context=context, task="Synthetic host writes; no legal conclusion or model inference.")
    else:
        if init["cancel"]:
            harness.cancel(context)
        if init["command"] == "approve":
            grant = harness.approve(context=context, approver=context, node_id="audit")
            result = await harness.resume(context=context, grant=grant, approver=context)
        elif init["command"] == "recover":
            result = await harness.resume(context=context)
            state = harness.inspect_orchestration(context)
            for node in state.nodes:
                if node.status == "unknown":
                    result = await harness.reconcile(context=context, node_id=node.id)
    state = harness.inspect_orchestration(context)
    trace_path = harness.trace_path(context)
    trace_valid = verify_trace(trace_path).valid
    if not trace_valid:
        raise ValueError("invalid framework trace")
    events = [{"host_event_version": 1, **entry["event"]["payload"]} for line in trace_path.read_text().splitlines()
              if (entry := json.loads(line))["event"]["type"] == "orchestration.transition"]
    send({"wire_version": WIRE_VERSION, "token": init["token"], "type": "result",
          "artifact_verified": True, "trace_valid": True, "events": events,
          "result": {"revision": state.revision, "status": result.status.value, "usage": result.usage.model_dump(mode="json"),
                     "final_output": result.final_output, "error": result.error,
                     "nodes": [{"id": n.id, "status": n.status, "used": n.used.model_dump(mode="json"),
                                "reservation": n.reservation.model_dump(mode="json")} for n in state.nodes]}})


if __name__ == "__main__":
    asyncio.run(main())
