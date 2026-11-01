"""Explicit local-model candidate layered on the isolated durable domain control."""
from __future__ import annotations

import json
from copy import deepcopy

from .agents import validate_review_answer
from .domain_integration import DomainSession, IsolatedDomainIntegration
from .host_ports import memory_record_context, search_hit_context
from .host_writes import digest
from .local_backends import BackendGate, GovernedRetrievalLedger
from .runtime_ports import ContractValidationError, RequestConflictError, UnsupportedCapabilityError
from .services import CHAT_SYSTEM_PROMPT, PROMPT_VERSION, strip_reasoning

MODEL_NODE = "critic-model"
LOCAL_PROMPT_VERSION = "domain-local-v1-bounded-citations"
LOCAL_PROMPT_SUFFIX = (
    "全文控制在400个汉字以内，最多列出三项发现并保留复核提醒。"
    "引用只能使用[资料1]这种短格式；不要在方括号内加入文件名、竖线或页码。"
)


def local_model_payload(models, question, route, contexts):
    context = "\n\n".join(f"[资料{i}｜{item['name']}｜第{item['page_no']}页]\n{item['quote']}"
                          for i, item in enumerate(contexts, 1))
    return {"model": models.models["llm"], "messages": [
        {"role": "system", "content": CHAT_SYSTEM_PROMPT + LOCAL_PROMPT_SUFFIX},
        {"role": "user", "content": f"检索路由：{route}\n问题：{question}\n\n卷宗片段：\n{context}"}],
        "temperature": models.temperature, "max_tokens": models.max_tokens,
        "stream": False, "chat_template_kwargs": {"enable_thinking": False}}


class LocalModelSession(DomainSession):
    def _call(self, node):
        if self.network_retrieval and node == "memory_recall":
            records = self.port.recall_memories(self.request.scope, self.request.question.strip(),
                                               limit=3, use_remote_embeddings=True)
            return {"items": [memory_record_context(record) for record in records]}
        if self.network_retrieval and node == "retrieve":
            if "memory_recall" not in self.outputs:
                raise ContractValidationError("memory must complete before retrieval")
            result = self.port.search(self.request.scope, self.request.question.strip(), limit=6, use_remote_embeddings=True)
            return {"contexts": [search_hit_context(hit) for hit in result.hits], "metrics": result.metrics}
        return super()._call(node)


class LocalModelDomainIntegration(IsolatedDomainIntegration):
    graph_version = "legal-review-local-v1"
    wire_version = 4
    app_version = "domain-local-host-v1"
    profile = "domain-local-v1"
    application_id = "lexvault-domain-local"

    def __init__(self, *, models, **kwargs):
        self.models = models
        models.assert_current()
        super().__init__(**kwargs)

    def _validate_request(self, request):
        if (request.runtime != self.runtime or request.execution_mode != "sync"
                or request.memory_snapshot is not None or request.budget is not None
                or request.model.timeout_seconds is not None
                or request.model.model not in {None, self.models.models["llm"]}):
            raise UnsupportedCapabilityError("local candidate only accepts host-bound models and budgets")

    def _extra_binding(self, request):
        return {**super()._extra_binding(request), "models": deepcopy(self.models.binding),
                "model_fingerprint": self.models.fingerprint, "prompt_version": PROMPT_VERSION,
                "local_prompt_version": LOCAL_PROMPT_VERSION,
                "use_llm": request.model.use_llm, "network_retrieval": request.use_remote_embeddings}

    def _load(self, conn, request_id, *, writing=False):
        row = super()._load(conn, request_id, writing=writing)
        self.models.assert_current()
        binding = json.loads(row["binding_json"])
        if (binding.get("model_fingerprint") != self.models.fingerprint
                or binding.get("models") != self.models.binding or binding.get("prompt_version") != PROMPT_VERSION
                or binding.get("local_prompt_version") != LOCAL_PROMPT_VERSION):
            raise RequestConflictError("saved model/prompt identity changed; no fallback")
        return row

    def _actions(self, run_id, conversation, request):
        actions = super()._actions(run_id, conversation, request)
        for action in actions:
            if action["id"] in {"memory_recall", "retrieve"} and request.use_remote_embeddings:
                action["max_usage"] = {"tool_calls": 129, "input_tokens": 6_100_000}
        if request.model.use_llm:
            position = next(i for i, a in enumerate(actions) if a["id"] == "finish")
            actions[position]["dependencies"].append(MODEL_NODE)
            actions.insert(position, {"id": MODEL_NODE, "kind": "model", "dependencies": ["critic"],
                "max_usage": {"steps": 1, "input_tokens": 40_000, "output_tokens": self.models.max_tokens}})
        return actions

    def _gate(self, row):
        return BackendGate(self, row, self.models)

    def _model_reply(self, record):
        if record is None or record["status"] != "completed":
            return {"model_result": None}
        body = json.loads(record["response_json"])
        usage = {"input_tokens": record["input_tokens"], "output_tokens": record["output_tokens"]}
        try:
            if body.get("model", self.models.models["llm"]) != self.models.models["llm"]:
                raise ValueError("model mismatch")
            if body["choices"][0].get("finish_reason") not in {None, "stop"}:
                raise ValueError("model did not finish normally")
            answer = strip_reasoning(body["choices"][0]["message"]["content"])
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("empty model answer")
        except (ValueError, KeyError, IndexError, TypeError):
            return {"model_error": "invalid_local_model_response", "usage": usage, "model_result": None}
        return {"model_result": {"action": {"kind": "final", "content": answer}, "usage": usage,
                                 "model_name": self.models.models["llm"]}}

    def _read_outputs(self, conn, row):
        outputs, completed = super()._read_outputs(conn, row)
        for step in conn.execute("SELECT output_json FROM agent_steps WHERE run_id=?", (row["run_id"],)):
            self._check_backend_events(row, json.loads(step["output_json"]).get("backend_calls", []))
        binding = json.loads(row["binding_json"])
        if binding["use_llm"]:
            record = self._gate(row).lookup(MODEL_NODE)
            if record is not None:
                payload = self._model_payload(row, outputs, completed)
                if record["request_digest"] != digest({"kind": "llm", "payload": payload, "models": self.models.fingerprint}):
                    raise RequestConflictError("saved model input proof changed")
            reply = self._model_reply(record)
            if reply.get("model_result"):
                outputs[MODEL_NODE] = reply["model_result"]
                completed.add(MODEL_NODE)
            elif "finish" in outputs:
                raise RequestConflictError("saved finish has no measured model response")
        return outputs, completed

    def _session(self, row, request, action):
        session = LocalModelSession(request)
        session.network_retrieval = json.loads(row["binding_json"])["network_retrieval"]
        session.port.allow_remote_embeddings = session.network_retrieval
        session.backends = GovernedRetrievalLedger(session.assert_current, self._gate(row), action["id"])
        return session

    def _compute(self, row, action, outputs):
        result = super()._compute(row, action, outputs)
        # Legacy retrieval may catch a backend exception and offer a fallback.
        # It cannot erase this candidate's durable unknown usage/reservation.
        from contextlib import closing

        from .db import connect
        from .local_backends import BackendUnknown
        with closing(connect()) as conn:
            unresolved = conn.execute("SELECT 1 FROM host_backend_calls WHERE request_id=? "
                "AND call_id LIKE ? AND status!='completed' LIMIT 1",
                (row["request_id"], action["id"] + ":backend:%")).fetchone()
        if unresolved:
            raise BackendUnknown("retrieval fallback cannot discard an unresolved provider call")
        if any(event.get("invalid_response") for event in result[1]):
            from .domain_integration import DomainNodeRejected
            raise DomainNodeRejected(ContractValidationError("invalid measured retrieval response"), result[1])
        return result

    def _prepare_outputs(self, row, action, outputs):
        result = deepcopy(outputs)
        if action["id"] == "finish" and json.loads(row["binding_json"])["use_llm"]:
            answer = result[MODEL_NODE]["action"]["content"]
            validation = validate_review_answer(answer, result["retrieve"]["contexts"])
            if not validation["valid"]:
                raise ContractValidationError("local model answer failed the domain contract")
            result["critic"] = {**result["critic"], "answer": answer, "validation": validation,
                                "llm_used": True, "llm_model": self.models.models["llm"], "memory_eligible": False}
        return result

    def _extra_operation(self, row, kind, message):
        if kind not in {"model", "model_receipt"}:
            return super()._extra_operation(row, kind, message)
        if message.get("node") != MODEL_NODE or message.get("key") != self._invocation_key(row, MODEL_NODE):
            raise ContractValidationError("unregistered model invocation")
        gate = self._gate(row)
        from contextlib import closing

        from .db import connect
        with closing(connect()) as conn:
            conn.execute("BEGIN")
            current = self._load(conn, row["request_id"], writing=True)
            outputs, completed = self._read_outputs(conn, current)
        if kind == "model_receipt":
            return self._model_reply(gate.lookup(MODEL_NODE))
        payload = self._model_payload(row, outputs, completed)
        return self._model_reply(gate.call("llm", payload, MODEL_NODE))

    def _model_payload(self, row, outputs, completed):
        binding = json.loads(row["binding_json"])
        if not binding["use_llm"] or "critic" not in completed:
            raise ContractValidationError("model predecessors have not completed")
        experts = [a["id"] for a in json.loads(row["graph_json"]) if a["id"] in {
            "facts", "evidence", "contradiction", "gap_detection", "statutory_conflict"}]
        summary = "；".join(outputs[name].get("summary", "") for name in sorted(experts))
        question = f"{binding['question']}\n专家节点摘要：{summary}。请只输出给律师的最终结论。"
        return local_model_payload(self.models, question, f"Multi-Agent/{outputs['planner']['route']}",
                                   outputs["retrieve"]["contexts"])

    def _receipt_reply(self, receipt, action):
        reply = super()._receipt_reply(receipt, action)
        if receipt is not None:
            from contextlib import closing

            from .db import connect
            with closing(connect()) as conn:
                saved = json.loads(conn.execute("SELECT output_json FROM agent_steps WHERE id=?",
                                   (json.loads(receipt.detail)["row_id"],)).fetchone()[0])
            calls = [e for e in saved["backend_calls"] if e["backend"] == "local-pinned"]
            if any(not e["usage_known"] for e in calls):
                raise RequestConflictError("unknown backend usage cannot become a completed tool")
            reply["nested_usage"] = {"tool_calls": len(calls),
                "input_tokens": sum(e["input_tokens"] for e in calls),
                "output_tokens": sum(e["output_tokens"] for e in calls)}
        return reply

    def _check_backend_events(self, row, events):
        gate = self._gate(row)
        for event in events:
            if event.get("backend") != "local-pinned":
                continue
            record = gate.lookup(event["call_id"])
            expected = {"kind": event["kind"], "input_tokens": event["input_tokens"],
                        "output_tokens": event["output_tokens"], "input_chars": event["input_chars"],
                        "request_digest": event["request_sha256"], "response_digest": event["response_sha256"]}
            if (record is None or record["status"] != "completed"
                    or any(record[key] != value for key, value in expected.items())
                    or self.models.models[event["kind"]] != event["model"]):
                raise RequestConflictError("saved retrieval measurement proof changed")

    def _worker_configuration(self, row):
        return {"model_identity": self.models.fingerprint}
