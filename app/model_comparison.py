"""Two local-model comparisons on disposable copies; never write the source DB."""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import time
from contextlib import closing
from copy import deepcopy
from pathlib import Path

from . import agents
from .candidate_domain import CandidateDeployment, CandidateDomainIntegration
from .db import connect, db_scope
from .domain_integration import DomainSession
from .host_writes import digest
from .local_backends import BackendUnknown, GovernedRetrievalLedger
from .model_domain import LOCAL_PROMPT_VERSION, LocalModelDomainIntegration, local_model_payload
from .runtime_adapter import current_actor_identity
from .runtime_ports import ContractValidationError, ModelConfig, RunRequest, RunScope
from .services import PROMPT_VERSION
from .answerability import POLICY_VERSION, answer_body, parse_decision, policy_identity

# The source commit predates the reviewed changes below. The exact new
# snapshot is identified by a version and literal hashes, not a future commit.
NATIVE_BASELINE_SOURCE_COMMIT = "31a811c92acae30caf82c9c5109ce812bd99bb04"
NATIVE_BASELINE_REVISION = "specialist-review-v4"
# Reviewed change since specialist-review-v1, recorded so a future drift is
# explained instead of re-pinned silently. The file digest covers the whole
# module, so a non-algorithmic edit still moves it; the review below states, for
# each re-registration, whether prompt, retrieval, answerability or the model-call
# path changed, and why the comparison stays comparable.
NATIVE_BASELINE_REVIEW = (
    "services.py: 2e57cce 增加流水全量迭代（列表分页不变，汇总/图谱/导出覆盖全部行）；"
    "2582573 报告批准绑定材料快照；c88cebd 交付包改为读取冻结正文并移出渲染代码。"
    "PROMPT_VERSION、LOCAL_PROMPT_VERSION、POLICY_VERSION 与消息/检索路径均未改动，"
    "因此对照算法未漂移，仅整文件摘要需要按本次评审重新登记。"
    "2026-10-27（specialist-review-v3）：疏漏专家的单一来源检测改为按固定顺序遍历关键证据类型。"
    "原先使用集合，迭代顺序随进程哈希种子变化，使同一冻结输入的专家发现顺序、审校附录与提示摘要不稳定；"
    "本次只固定顺序，检测条件、检索、提示模板与回答契约均未改动，旧 v2 结果保留且不被覆盖。"
    "2026-10-30（specialist-review-v4）：模型调用改由 app/model_ports.py 决定 wire 协议与凭证，"
    "services.py 的多轮对话请求/响应编解码移出内联字典，审计记录增加 api/provider/declared 三个字段。"
    "CHAT_SYSTEM_PROMPT、PROMPT_VERSION、检索、回答契约、校验与工具循环行为均未改动："
    "默认协议仍是 openai-completions，请求体与响应解析结果与改动前逐字段一致（test_chat_stream.py 覆盖）。"
    "agents.py 仅把回退目标自带的协议透传给 call_local_llm，专家发现顺序与提示模板未变。"
)
NATIVE_ALGORITHM_HASHES = {
    "agents.py": "53cdd159704eddd8152fb39a1b2cbdb129b2a241cde3e810a7cd060f083e37da",
    "review_collaboration.py": "512c03d6719bddfd98d422c7cb65e46c26331b8aab7ab56b40e651a4a07d632a",
    "agent_tools.py": "2af80bdaa3e1277de96a4127e2fd9bbabbb906b1213a94e6beb43a54d28dfb34",
    "rag.py": "a79cd859fc639c409d043f18379e0ea45a286f077c9b633f40d6a06fbad949cc",
    "services.py": "08228d5e90fa86a1d26495761967d159f5a756ad622f666bc36f1711bb086d11",
    "model_domain.py": "f1e69e1d56d0fc73a15e51888b2b31e4bda6ce6e1f3a57bccad39f2f9dce5cbc",
    "statutory_retrieval.py": "d9d2f60c01ac03f405ca4e1da593ff049a32cd6e072d3fa4bcb98f8be4b9fbe8",
}


def native_baseline_identity():
    actual = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
              for name in NATIVE_ALGORITHM_HASHES}
    if actual != NATIVE_ALGORITHM_HASHES:
        raise ContractValidationError("native comparison algorithms differ from the pinned baseline")
    return {"schema_version": 2, "source_commit": NATIVE_BASELINE_SOURCE_COMMIT,
            "revision": NATIVE_BASELINE_REVISION, "algorithm_files": actual,
            "review_note": NATIVE_BASELINE_REVIEW,
            "scope": "reviewed source snapshot derived from source_commit; exact identity is revision plus algorithm_files"}


def private_report(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def clone_snapshot(source, target):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as original, closing(sqlite3.connect(target)) as copy:
        original.backup(copy)


class NativeMeasurementLedger(GovernedRetrievalLedger):
    """Observe the native reference through the same pinned local backend setup."""
    def invoke(self, kind, client, args, kwargs, operation):
        if getattr(self, "unresolved", False):
            raise BackendUnknown("native comparison stopped after an unknown backend call")
        started = time.monotonic()
        try:
            return self._invoke(kind, client, args, kwargs, operation)
        except BackendUnknown:
            self.unresolved = True
            elapsed = round(time.monotonic() - started, 6)
            if self.events and self.events[-1].get("kind") == kind and not self.events[-1]["usage_known"]:
                self.events[-1]["outcome"] = "unknown"
                self.events[-1].setdefault("elapsed_seconds", elapsed)
            else:
                self.events.append({"kind": kind, "backend": "local-pinned", "usage_known": False,
                    "input_tokens": None, "output_tokens": None, "measurement_valid": False,
                    "outcome": "unknown", "executed": True, "elapsed_seconds": elapsed})
            raise

    def _invoke(self, kind, client, args, kwargs, operation):
        if kind != "llm":
            return super().invoke(kind, client, args, kwargs, operation)
        self.authorize()
        question, route, contexts = args[:3]
        payload = local_model_payload(self.gate.models, question, route, contexts)
        started = time.monotonic()
        record = self.gate.call("llm", payload, self.node_id + ":backend:" + str(len(self.events)))
        self.events.append({"kind": "llm", "backend": "local-pinned", "input_tokens": record["input_tokens"],
            "output_tokens": record["output_tokens"], "usage_known": record["status"] == "completed",
            "measurement_valid": self.gate.models.transport is None,
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "request_sha256": record["request_digest"], "response_sha256": record["response_digest"]})
        reply = LocalModelDomainIntegration._model_reply(self.gate.control, record)
        if reply.get("model_error"):
            self.events[-1]["invalid_response"] = True
            raise RuntimeError("native measured model response rejected")
        if reply.get("model_result") is None:
            raise BackendUnknown("native model usage unknown; no replay")
        return reply["model_result"]["action"]["content"]


def _page_keys(contexts):
    return [(c["document_id"], c["page_no"]) for c in contexts]


def compare_local_models(*, case_id, question, models, framework_python, output, expected="success"):
    native_identity = native_baseline_identity()
    if expected not in {"success", "source_rejection"}:
        raise ContractValidationError("comparison requires an explicit expected outcome")
    output = Path(output).absolute()
    output.mkdir(parents=True, mode=0o700)  # refuse an existing run; never resend it implicitly
    source_request = RunRequest(request_id="readonly-local-comparison", scope=RunScope(case_id),
        actor=current_actor_identity(), question=question, runtime="forgeharness-readonly",
        model=ModelConfig(use_llm=False), use_remote_embeddings=False, persist_memory=False)
    with DomainSession(source_request) as source:
        source_identity = source.source_identity
        deployment = CandidateDeployment.create_new(output / "candidate" / "host.sqlite", case_ids=(case_id,))
        clone_snapshot(source.snapshot, deployment.database)
        with db_scope(deployment.database):
            control = CandidateDomainIntegration(deployment=deployment, models=models, framework_python=framework_python)
            request = RunRequest(request_id="candidate-local-comparison", scope=RunScope(case_id),
                actor=current_actor_identity(), question=question, runtime=control.runtime,
                model=ModelConfig(use_llm=True), use_remote_embeddings=True, persist_memory=False)
            control.accept(request)
            started = time.monotonic()
            candidate = control.drive(request.request_id)
            elapsed = time.monotonic() - started
            with closing(connect()) as conn:
                row = control._load(conn, request.request_id)
                outputs, _ = control._read_outputs(conn, row)
                failed = conn.execute("SELECT output_json FROM agent_steps WHERE run_id=? "
                    "AND node_name='finish' AND status='failed'", (row["run_id"],)).fetchone()
                failure_value = json.loads(failed[0])["value"] if failed else {}
                call_count, input_tokens, output_tokens, unknown_calls = conn.execute(
                    "SELECT COUNT(*),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),"
                    "COALESCE(SUM(status!='completed'),0) FROM host_backend_calls WHERE request_id=?",
                    (request.request_id,)).fetchone()
                candidate_usage = {"calls": call_count, "input_tokens": input_tokens,
                                   "output_tokens": output_tokens, "unknown_usage": unknown_calls}
        if candidate["needs_reconciliation"]:
            raise BackendUnknown("candidate requires reconciliation; comparison stopped without retry")
        retrieval = deepcopy(outputs["retrieve"])
        refusal = failure_value.get("answerability")
        decision_record = refusal or outputs.get("finish", {}).get("answerability") or {}
        candidate_decision = decision_record.get("decision")
        rejected = False
        if refusal and failure_value.get("diagnostic", {}).get("code") == "answerability_rejected":
            measured_decision = parse_decision(outputs["critic-model"]["action"]["content"], retrieval["contexts"],
                                               question=question)
            rejected = (candidate["status"] == "failed" and refusal == measured_decision
                        and refusal["decision"] in {"unrelated", "insufficient"})
        outcome = "source_rejection" if rejected else "success" if candidate["status"] == "succeeded" else "system_failure"
        comparisons = []
        for shared in (True, False):
            mode = "shared_retrieval_and_memory_snapshot" if shared else "independent_retrieval_and_memory"
            native_db = output / mode / "host.sqlite"
            clone_snapshot(source.snapshot, native_db)
            with db_scope(deployment.database):
                measurement_request = RunRequest(request_id=mode, scope=RunScope(case_id),
                    actor=current_actor_identity(), question=question, runtime=control.runtime,
                    model=ModelConfig(use_llm=True), use_remote_embeddings=True, persist_memory=False)
                control.accept(measurement_request)
                with closing(connect()) as conn:
                    row = control._load(conn, mode)
                ledger = NativeMeasurementLedger(source.assert_current, control._gate(row), "native-" + mode)
                ledger.set_deadline(time.monotonic() + max(0, row["deadline"] - time.time()))

            class FrozenRetrieval:
                def retrieve(self, query, limit=6):
                    return deepcopy(retrieval)

            started = time.monotonic()
            with db_scope(native_db), ledger.activate():
                native = agents.LawReviewCoordinator(case_id, True)
                native.statutory_conflict_agent = source._statutory_agent()
                native.critic_agent.model = models.models["llm"]
                if shared:
                    native.retrieval_agent = FrozenRetrieval()
                result = native.process_query(question, use_llm=True, persist_memory=False,
                    memory_snapshot=deepcopy(outputs["memory_recall"]["items"]) if shared else None)
                trace = agents.get_run_trace(result["run_id"])
            native_elapsed = time.monotonic() - started
            source.assert_current()
            steps = {step["node_name"]: step["output"] for step in trace["steps"]}
            specialists = {name: value for name, value in steps.items() if name in source.specs}
            known = all(e["usage_known"] for e in ledger.events if e["backend"] == "local-pinned")
            invalid = any(e.get("invalid_response") for e in ledger.events)
            if not known:
                private_report(output / "failure.json", {"mode": mode, "outcome": "unknown_usage",
                    "native_backend_calls": ledger.events, "source_business_writes": 0,
                    "provider_calls_not_retried": True, "g2_accepted": False})
                raise BackendUnknown("native usage remains unknown; evidence retained without retry")
            success = outcome == "success" and result["llm_used"] and result["validation"]["valid"]
            checks = {"expected_outcome": bool(success if expected == "success" else rejected),
                "specialists_equal": specialists == {name: outputs[name] for name in specialists},
                "retrieved_pages_equal": _page_keys(steps["retrieve"]["contexts"]) == _page_keys(retrieval["contexts"]),
                "known_usage": known, "no_invalid_backend_fallback": not invalid}
            measured = [e for e in ledger.events if e["backend"] == "local-pinned"]
            comparison = {"mode": mode, "checks": checks, "engineering_passed": all(checks.values()),
                "answer_equal": result["answer"] == outputs.get("finish", {}).get("answer"),
                "candidate_outcome": candidate["status"], "native_llm_used": result["llm_used"],
                "candidate_business_outcome": outcome,
                "native_baseline_negative_accepted": expected == "source_rejection" and result["validation"]["valid"],
                "native_correctness_assessed": False,
                "native_validation": result["validation"], "native_answer": result["answer"],
                "native_citations": result["citations"], "native_specialists": specialists,
                "native_backend_calls": deepcopy(ledger.events), "native_elapsed_seconds": native_elapsed,
                "native_usage": {"calls": len(measured), "input_tokens": sum(e["input_tokens"] for e in measured),
                    "output_tokens": sum(e["output_tokens"] for e in measured)},
                "source_business_writes": 0, "model_quality_assessed": False}
            private_report(output / f"{mode}.json", comparison)
            comparisons.append(comparison)
            with db_scope(deployment.database):
                control.cancel(mode)  # measurement handle was never sent to the framework
        source.assert_current()
        report = {"schema_version": 3, "source_identity": source_identity, "question": question,
            "expected": expected, "models": deepcopy(models.binding), "prompt_version": PROMPT_VERSION,
            "local_prompt_version": LOCAL_PROMPT_VERSION, "measurement_valid": models.transport is None,
            "candidate": candidate, "candidate_elapsed_seconds": elapsed,
            "answerability_policy": policy_identity(), "candidate_business_outcome": outcome,
            "candidate_decision": candidate_decision, "candidate_usage": candidate_usage,
            "native_baseline": native_identity, "candidate_prompt_version": POLICY_VERSION,
            "candidate_refusal": refusal, "candidate_failure": failure_value.get("diagnostic"),
            "candidate_answer": outputs.get("finish", {}).get("answer"),
            "candidate_answer_body": answer_body(decision_record),
            "candidate_findings": deepcopy(decision_record.get("findings", [])),
            "candidate_citations": outputs.get("finish", {}).get("citations", []),
            "candidate_retrieval_digest": digest(retrieval), "comparisons": comparisons,
            "source_unchanged": True, "engineering_passed": all(c["engineering_passed"] for c in comparisons),
            "g2_accepted": False, "g2_pending": ["authorized representative development materials", "quality review"],
            "semantic_entailment_checked": False, "lawyer_review_required": True}
        private_report(output / "report.json", report)
        return report
