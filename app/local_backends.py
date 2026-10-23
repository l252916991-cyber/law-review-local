"""Host-owned local models and durable admission; no provider retry after dispatch."""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request

from .db import connect, db_scope, now, transaction
from .forge_governance import BackendLedger
from .host_writes import canonical, digest
from .runtime_ports import AccessDeniedError, ContractValidationError, RequestConflictError
from .services import egress_opener, read_json_with_deadline


class BackendUnknown(RuntimeError):
    """A dispatched request or its measured usage cannot safely be invented/replayed."""


class LocalModels:
    def __init__(self, *, endpoint, identities, models, temperature=0.2, max_tokens=900, transport=None):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path.rstrip("/") != "/v1"):
            raise ContractValidationError("local model validation only admits literal loopback /v1 endpoints")
        if (type(max_tokens) is not int or not 1 <= max_tokens <= 900
                or type(temperature) not in {int, float} or not 0 <= temperature <= 2):
            raise ContractValidationError("invalid local model generation bound")
        if set(models) != {"llm", "embedding", "reranker"}:
            raise ContractValidationError("all three local backend identities are required")
        self.endpoint = endpoint.rstrip("/")
        self.models, self.identities = deepcopy(models), deepcopy(identities)
        self.temperature, self.max_tokens, self.transport = temperature, max_tokens, transport
        self._stats = {}
        for name in set(models.values()):
            entry = self.identities[name]
            if entry["identity"] != "local-sha256:" + digest(entry["files"]):
                raise ContractValidationError("model identity manifest digest differs")
            directory = Path(entry["path"]).resolve(strict=True)
            if "config.json" not in entry["files"] or not any(n.endswith(".safetensors") for n in entry["files"]):
                raise ContractValidationError("model config and weights are required")
            for relative, expected in entry["files"].items():
                path = directory / relative
                if path.parent != directory or Path(relative).name != relative:
                    raise ContractValidationError("invalid model manifest path")
                before = path.stat()
                hasher = hashlib.sha256()
                with path.open("rb") as file:
                    while chunk := file.read(8 * 1024 * 1024):
                        hasher.update(chunk)
                if hasher.hexdigest() != expected["sha256"] or before.st_size != expected["bytes"]:
                    raise RequestConflictError("local model bytes differ from the frozen manifest")
                signature = self._signature(path)
                if signature != (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino):
                    raise RequestConflictError("local model changed while hashing")
                self._stats[path] = signature
        self.binding = {"endpoint": self.endpoint, "models": {
            kind: {"name": name, "identity": self.identities[name]["identity"]} for kind, name in self.models.items()},
            "temperature": temperature, "max_tokens": max_tokens,
            "measurement_valid": transport is None}
        self.fingerprint = digest(self.binding)

    @staticmethod
    def _signature(path):
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino

    def assert_current(self):
        current = {"endpoint": self.endpoint, "models": {
            kind: {"name": name, "identity": self.identities[name]["identity"]} for kind, name in self.models.items()},
            "temperature": self.temperature, "max_tokens": self.max_tokens,
            "measurement_valid": self.transport is None}
        if digest(current) != self.fingerprint or digest(self.binding) != self.fingerprint:
            raise RequestConflictError("local backend configuration changed")
        for path, expected in self._stats.items():
            if self._signature(path) != expected:
                raise RequestConflictError("local model artifacts changed")

    def invoke(self, kind, payload, timeout):
        self.assert_current()
        if payload.get("model") != self.models[kind]:
            raise RequestConflictError("provider model differs from the trusted binding")
        if kind == "llm" and (payload.get("max_tokens") != self.max_tokens
                or payload.get("temperature") != self.temperature or payload.get("stream") is not False):
            raise ContractValidationError("generation parameters differ from trusted configuration")
        route = {"llm": "chat/completions", "embedding": "embeddings", "reranker": "rerank"}[kind]
        if self.transport is not None:
            return self.transport(kind, deepcopy(payload), timeout)
        request = Request(self.endpoint + "/" + route, data=canonical(payload).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        deadline = time.monotonic() + timeout
        with egress_opener().open(request, timeout=timeout) as response:
            return read_json_with_deadline(response, timeout, deadline=deadline)


def measured_usage(kind, body):
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None, None
    incoming = usage.get("prompt_tokens")
    outgoing = usage.get("completion_tokens")
    if kind != "llm":
        incoming = usage.get("prompt_tokens", usage.get("total_tokens"))
        outgoing = 0  # embedding/rerank have no generated text tokens
    if type(incoming) is int and incoming >= 0 and type(outgoing) is int and outgoing >= 0:
        return incoming, outgoing
    return None, None


class BackendGate:
    def __init__(self, control, row, models):
        self.control, self.row, self.models = control, row, models
        self.path = control.path

    def _authorize(self, *, writing=False):
        self.models.assert_current()
        with db_scope(self.path), closing(connect()) as conn:
            conn.execute("BEGIN")
            return self.control._load(conn, self.row["request_id"], writing=writing)

    def lookup(self, call_id):
        self._authorize()
        with db_scope(self.path), closing(connect()) as conn:
            row = conn.execute("SELECT * FROM host_backend_calls WHERE request_id=? AND call_id=?",
                               (self.row["request_id"], call_id)).fetchone()
        if row is None:
            return None
        if row["model_identity"] != self.models.fingerprint:
            raise RequestConflictError("saved backend identity differs")
        record = dict(row)
        if record["response_json"] is not None:
            body = json.loads(record["response_json"])
            if (digest(body) != record["response_digest"]
                    or measured_usage(record["kind"], body) != (record["input_tokens"], record["output_tokens"])):
                raise RequestConflictError("saved provider response changed")
        return record

    def call(self, kind, payload, call_id):
        self.models.assert_current()
        raw = canonical(payload)
        bound = digest({"kind": kind, "payload": payload, "models": self.models.fingerprint})
        # Generation is one bounded SDK model node; retrieval has its own bounded inputs.
        limit = 40_000 if kind == "llm" else 6_100_000
        if len(raw.encode()) > limit:
            raise ContractValidationError("local backend input exceeds its declared bound")
        with db_scope(self.path), transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self.control._load(conn, self.row["request_id"], writing=True)
            old = conn.execute("SELECT * FROM host_backend_calls WHERE request_id=? AND call_id=?",
                               (current["request_id"], call_id)).fetchone()
            if old is not None:
                if old["request_digest"] != bound:
                    raise RequestConflictError("backend invocation input changed")
                if old["response_json"] is None:
                    raise BackendUnknown("dispatched provider call has no proven response; not resent")
            else:
                if current["cancel_requested"] or time.time() >= current["deadline"]:
                    raise AccessDeniedError("backend admission cancelled or expired")
                count, chars = conn.execute("SELECT COUNT(*),COALESCE(SUM(input_chars),0) FROM host_backend_calls WHERE request_id=?",
                                            (current["request_id"],)).fetchone()
                if count >= 128 or chars + len(raw) > 20_000_000:
                    raise ContractValidationError("persistent backend call/input budget exhausted")
                conn.execute("INSERT INTO host_backend_calls(request_id,call_id,kind,request_digest,model_identity,input_chars,status,created_at) "
                             "VALUES (?,?,?,?,?,?,'dispatched',?)", (current["request_id"], call_id, kind, bound,
                              self.models.fingerprint, len(raw), now()))
        if old is None:
            try:
                body = self.models.invoke(kind, payload, min(180, max(0.01, current["deadline"] - time.time())))
                encoded = canonical(body)
                if not isinstance(body, dict) or len(encoded.encode()) > 8_000_000:
                    raise ValueError("provider response exceeds contract")
                incoming, outgoing = measured_usage(kind, body)
                status = "completed" if incoming is not None else "response_known_usage_unknown"
                # Capture the already-received response even if authorization/deadline
                # changed while waiting. Subsequent release still reauthorizes.
                with db_scope(self.path), transaction() as conn:
                    updated = conn.execute("UPDATE host_backend_calls SET status=?,response_json=?,response_digest=?,input_tokens=?,output_tokens=? "
                        "WHERE request_id=? AND call_id=?", (status, encoded, digest(body), incoming, outgoing,
                         current["request_id"], call_id)).rowcount
                    if updated != 1:
                        raise AccessDeniedError("backend request was erased")
            except Exception:
                with db_scope(self.path), transaction() as conn:
                    conn.execute("UPDATE host_backend_calls SET status='unknown' WHERE request_id=? AND call_id=? AND response_json IS NULL",
                                 (current["request_id"], call_id))
                raise BackendUnknown("provider response or usage requires reconciliation") from None
        record = self.lookup(call_id)
        current = self._authorize(writing=True)
        if current["cancel_requested"] or time.time() >= current["deadline"]:
            raise AccessDeniedError("provider completed after cancellation/deadline; response retained")
        return record


class GovernedRetrievalLedger(BackendLedger):
    def __init__(self, authorize, gate, node_id):
        super().__init__(authorize)
        self.gate, self.node_id = gate, node_id

    def invoke(self, kind, client, args, kwargs, operation):
        if kind == "embedding" and not client.prefer_remote:
            return super().invoke(kind, client, args, kwargs, operation)
        if kind not in {"embedding", "reranker"}:
            raise ContractValidationError("generation must use the SDK model port")
        self.authorize()
        client.model = self.gate.models.models[kind]
        client.base_url = self.gate.models.endpoint
        if kind == "embedding":
            texts = args[0] if args else kwargs["texts"]
            if not texts:
                return [], "none"
            payload = {"model": self.gate.models.models[kind], "input": texts}
        else:
            query = args[0] if args else kwargs["query"]
            documents = args[1] if len(args) > 1 else kwargs["documents"]
            if not documents:
                return []
            payload = {"model": self.gate.models.models[kind], "query": query, "documents": documents}
        call_id = self.node_id + ":backend:" + str(len(self.events))
        record = self.gate.call(kind, payload, call_id)
        event = {"kind": kind, "call_id": call_id, "model": payload["model"], "backend": "local-pinned",
                 "input_tokens": record["input_tokens"], "output_tokens": record["output_tokens"],
                 "input_chars": record["input_chars"], "usage_known": record["status"] == "completed",
                 "measurement_valid": self.gate.models.transport is None, "executed": True,
                 "request_sha256": record["request_digest"], "response_sha256": record["response_digest"]}
        self.events.append(event)
        if record["status"] != "completed":
            raise BackendUnknown("retrieval response has unknown usage; reservation retained")
        body = json.loads(record["response_json"])
        try:
            value = self._decode(kind, payload, body)
        except (KeyError, TypeError, IndexError, ValueError, ContractValidationError):
            event["invalid_response"] = True
            raise ContractValidationError("invalid measured local retrieval response") from None
        return value

    @staticmethod
    def _decode(kind, payload, body):
        if body.get("model", payload["model"]) != payload["model"]:
            raise ContractValidationError("provider reported a different model")
        if kind == "embedding":
            from .rag import valid_vector
            data = sorted(body["data"], key=lambda item: item["index"])
            vectors = [item["embedding"] for item in data]
            if (not vectors or len(vectors) != len(payload["input"])
                    or any(type(item["index"]) is not int for item in data)
                    or [item["index"] for item in data] != list(range(len(vectors)))
                    or not all(valid_vector(v, len(vectors[0])) for v in vectors)):
                raise ContractValidationError("invalid local embedding response")
            return vectors, "omlx"
        import math
        results = body["results"]
        if (any(type(item["index"]) is not int for item in results)
                or sorted(item["index"] for item in results) != list(range(len(payload["documents"])))):
            raise ContractValidationError("reranker did not cover every document exactly once")
        scores = [0.0] * len(results)
        for item in results:
            score = item["relevance_score"]
            if type(score) not in {int, float} or not math.isfinite(score):
                raise ContractValidationError("invalid local reranker score")
            scores[item["index"]] = score
        return scores
