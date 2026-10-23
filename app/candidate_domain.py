"""Host-configured write candidate, exercised only in a new acceptance database.

No HTTP route, environment enable switch, or request-supplied deployment config.
This is the E12/E13 adapter candidate; E14 must authorize production admission.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .db import db_scope, get_db_path, init_db
from .domain_writes import CANDIDATE_RUNTIME, DomainHostWritePort
from .host_writes import digest
from .model_domain import LocalModelDomainIntegration
from .model_domain import MODEL_NODE
from .answerability import SourceRejection, answerability_payload, parse_decision, policy_identity
from .runtime_ports import AccessDeniedError, ContractValidationError, RequestConflictError, UnsupportedCapabilityError


@dataclass(frozen=True)
class CandidateDeployment:
    database: Path
    file_identity: tuple[int, int]
    case_ids: tuple[int, ...]
    timeout_seconds: int = 300
    require_execution_approval: bool = False
    environment: str = "acceptance"
    revision: str = "candidate-acceptance-v1"

    def __post_init__(self):
        if (self.environment != "acceptance" or self.revision != "candidate-acceptance-v1"
                or not self.database.is_absolute() or self.database.is_symlink()
                or not self.case_ids or len(set(self.case_ids)) != len(self.case_ids)
                or any(type(i) is not int or i < 1 for i in self.case_ids)
                or type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 600
                or type(self.require_execution_approval) is not bool):
            raise ContractValidationError("invalid trusted acceptance deployment")
        self.assert_file()

    @classmethod
    def create_new(cls, path, *, case_ids=(1,), timeout_seconds=300, require_execution_approval=False):
        path = Path(path).absolute()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb"):
            pass
        with db_scope(path):
            init_db(seed=False)
        stat = path.stat()
        return cls(path, (stat.st_dev, stat.st_ino), tuple(case_ids), timeout_seconds, require_execution_approval)

    def assert_file(self):
        if self.database.is_symlink():
            raise AccessDeniedError("candidate database cannot be a symbolic link")
        stat = self.database.stat()
        if (stat.st_dev, stat.st_ino) != self.file_identity:
            raise AccessDeniedError("candidate database file was replaced")

    @property
    def identity(self):
        return digest({"database": str(self.database), "file": self.file_identity, "cases": self.case_ids,
            "timeout_seconds": self.timeout_seconds, "require_execution_approval": self.require_execution_approval,
            "environment": self.environment, "revision": self.revision,
            "capacity": {"pages": 500, "body_chars": 2_000_000, "over_limit": "reject"}})

    def guard(self, conn):
        self.assert_file()
        actual = dict((r[1], r[2]) for r in conn.execute("PRAGMA database_list")).get("main")
        if get_db_path().resolve() != self.database.resolve() or Path(actual).resolve() != self.database.resolve():
            raise AccessDeniedError("candidate deployment database differs")
        if conn.execute("SELECT 1 FROM host_integration_config").fetchone():
            raise AccessDeniedError("candidate cannot relabel a synthetic database")


class CandidateDomainIntegration(LocalModelDomainIntegration):
    runtime = CANDIDATE_RUNTIME
    graph_version = "legal-review-candidate-v2"
    wire_version = 5
    app_version = "domain-candidate-host-v2"
    profile = "domain-candidate-v1"
    application_id = "lexvault-domain-candidate"

    def _model_reply(self, record):
        # Structured reasons can legitimately contain the legacy scratchpad
        # heuristic's Chinese markers. Never apply that heuristic to this JSON.
        if record is None or record["status"] != "completed":
            return {"model_result": None}
        usage = {"input_tokens": record["input_tokens"], "output_tokens": record["output_tokens"]}
        try:
            body = json.loads(record["response_json"])
            answer = body["choices"][0]["message"]["content"]
            if (body.get("model", self.models.models["llm"]) != self.models.models["llm"]
                    or body["choices"][0].get("finish_reason") not in {None, "stop"}
                    or not isinstance(answer, str) or not answer.strip()):
                raise ValueError("invalid structured response")
        except (ValueError, KeyError, IndexError, TypeError):
            return {"model_error": "invalid_local_model_response", "usage": usage, "model_result": None}
        return {"model_result": {"action": {"kind": "final", "content": answer},
                "usage": usage, "model_name": self.models.models["llm"]}}

    def _validate_request(self, request):
        super()._validate_request(request)
        if not request.model.use_llm:
            raise UnsupportedCapabilityError("candidate v2 requires measured answerability; no rule-only fallback")

    def _model_payload(self, row, outputs, completed):
        binding = json.loads(row["binding_json"])
        if "critic" not in completed:
            raise ContractValidationError("model predecessors have not completed")
        return answerability_payload(self.models, binding["question"], outputs["planner"]["route"],
                                     outputs["retrieve"]["contexts"])

    def _prepare_outputs(self, row, action, outputs):
        result = deepcopy(outputs)
        if action["id"] == "finish":
            decision = parse_decision(result[MODEL_NODE]["action"]["content"], result["retrieve"]["contexts"])
            if decision["decision"] != "supported":
                raise SourceRejection(decision)
            result[MODEL_NODE]["action"]["content"] = decision["answer"]
        return super()._prepare_outputs(row, action, result)

    def _compute(self, row, action, outputs):
        value, calls = super()._compute(row, action, outputs)
        if action["id"] == "finish":
            value["answerability"] = parse_decision(outputs[MODEL_NODE]["action"]["content"],
                                                    outputs["retrieve"]["contexts"])
        return value, calls

    def _failed_value(self, error):
        if isinstance(error, SourceRejection):
            return {"diagnostic": {"code": "answerability_rejected", "decision": error.decision["decision"]},
                    "answerability": error.decision}
        return super()._failed_value(error)

    def __init__(self, *, deployment, models, framework_python=None):
        if not isinstance(deployment, CandidateDeployment):
            raise ContractValidationError("trusted deployment object required")
        self.deployment = deployment

        class BoundCandidatePort(DomainHostWritePort):
            runtime = CANDIDATE_RUNTIME
            _database_guard = staticmethod(deployment.guard)

            @classmethod
            def _source_version(cls, conn, scope, *, writing=False):
                deployment.guard(conn)
                if scope.case_id not in deployment.case_ids:
                    raise AccessDeniedError("case outside candidate deployment scope")
                return super()._source_version(conn, scope, writing=writing)

        self.port_type = BoundCandidatePort
        super().__init__(models=models, framework_python=framework_python,
            timeout_seconds=deployment.timeout_seconds, require_execution_approval=deployment.require_execution_approval)

    def _database_guard(self, conn):
        self.deployment.guard(conn)

    def _path_guard(self):
        self.deployment.assert_file()
        super()._path_guard()

    def _extra_binding(self, request):
        return {**super()._extra_binding(request), "deployment": self.deployment.identity,
                "answerability_policy": policy_identity()}

    def _load(self, conn, request_id, *, writing=False):
        row = super()._load(conn, request_id, writing=writing)
        if json.loads(row["binding_json"]).get("deployment") != self.deployment.identity:
            raise RequestConflictError("trusted candidate deployment changed")
        if json.loads(row["binding_json"]).get("answerability_policy") != policy_identity():
            raise RequestConflictError("saved answerability policy changed; no reinterpretation")
        return row
