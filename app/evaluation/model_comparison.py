"""Two local-model comparisons on disposable copies; never write the source DB."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from copy import deepcopy
from pathlib import Path

from app.integration.candidate_domain import CandidateDeployment, CandidateDomainIntegration
from app.integration.domain_integration import DomainSession
from app.integration.host_writes import digest
from app.integration.model_domain import LOCAL_PROMPT_VERSION, LocalModelDomainIntegration, local_model_payload
from app.integration.runtime_adapter import current_actor_identity
from app.integration.runtime_ports import ContractValidationError, ModelConfig, RunRequest, RunScope
from app.llm.local_backends import BackendUnknown, GovernedRetrievalLedger
from app.paths import APP_DIR
from app.review import agents
from app.review.answerability import POLICY_VERSION, answer_body, parse_decision, policy_identity
from app.review.generation import PROMPT_VERSION
from app.storage.db import connect, db_scope

# The source commit predates the reviewed changes below. The exact new
# snapshot is identified by a version and literal hashes, not a future commit.
NATIVE_BASELINE_SOURCE_COMMIT = "31a811c92acae30caf82c9c5109ce812bd99bb04"
NATIVE_BASELINE_REVISION = "specialist-review-v16"
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
    "2026-10-30（specialist-review-v4）：模型调用改由 app/llm/model_ports.py 决定 wire 协议与凭证，"
    "services.py 的多轮对话请求/响应编解码移出内联字典，审计记录增加 api/provider/declared 三个字段。"
    "CHAT_SYSTEM_PROMPT、PROMPT_VERSION、检索、回答契约、校验与工具循环行为均未改动："
    "默认协议仍是 openai-completions，请求体与响应解析结果与改动前逐字段一致（test_chat_stream.py 覆盖）。"
    "agents.py 仅把回退目标自带的协议透传给 call_local_llm，专家发现顺序与提示模板未变。"
    "2026-11-01（specialist-review-v5）：启用 ruff Pyflakes/isort/pyupgrade/bugbear 规则后的机械修改——"
    "删除未使用的导入与局部重复导入、导入排序、typing.Callable 改为 collections.abc.Callable、"
    "去掉无占位符 f-string 的前缀；rag.py 的重排失败异常补上 from exc 以保留原因链。提示模板、检索、回答契约、专家规则、校验与模型调用路径均未改动，"
    "test_chat_stream.py 与专家/审校回归逐字段覆盖；v4 结果保留且不被覆盖。"
    "2026-11-01（specialist-review-v6）：services.chat 拆为 _record_notice_turn、_statistics_turn、"
    "_generate_model_answer、_citation_cards 四个内部函数，调用顺序、提示参数、回退与计量、"
    "落库内容及返回字段不变；CHAT_SYSTEM_PROMPT、PROMPT_VERSION、检索与回答契约未改动。"
    "2026-11-02（specialist-review-v7）：为支持 PostgreSQL，rag.py 中全文候选与页向量打分两段 SQL 移入"
    "新增的 app/retrieval/search_backend.py（同时纳入本清单）。SQLite 下 FTS5 查询文本、bm25 权重、向量兼容性检查、"
    "哈希回退的词项过滤、余弦打分与排序原样保留：评测门禁 60 题逐题结果与改动前一致，向量通道逐页得分一致。"
    "PROMPT_VERSION、提示模板、可答性、回答契约与专家规则均未改动；v6 结果保留且不被覆盖。"
    "2026-11-02（specialist-review-v8）：**提示模板有改动**。首轮模型门禁（Qwen3.8-27B，51/60）中两题因 700 token"
    "截断丢失复核提醒、一题拒答未带引用而被回答契约拒绝，CHAT_SYSTEM_PROMPT 增加两条要求：资料不足时写明缺什么并"
    "引用核对过的片段、不得推测金额日期编号；全文控制在 600 字以内、先结论后依据、不用宽表格。PROMPT_VERSION 升为"
    "chat-system-v5-length-budget-cited-refusal。回答契约、检索、可答性与专家规则未改动。v8 与 v7 及以前的模型结果"
    "不可直接比较；v7 结果保留且不被覆盖。"
    "2026-11-07（specialist-review-v9）：跨模型门禁中 Qwen3.6-35B-A3B 有 3 个内容正确的回答把提示里的来源标题"
    "“[资料1｜文件名｜第3页]”原样写进引用，被回答契约当作格式错误拒绝。call_local_llm 在返回前把这种写法改回"
    "“[资料1]”，条件是文件名和页码与第 1 条来源完全一致；不一致的、范围写法和其他格式仍被拒绝。提示模板、PROMPT_VERSION、"
    "检索、可答性与回答契约本身未改动，27B 两轮 107 个保存回答没有这种写法，改写对它们是恒等；35B、9B 的保存回答经回放验证只有这几题结果变化。"
    "v8 结果保留且不被覆盖。"
    "2026-11-07（specialist-review-v10）：35B-A3B 第二次运行与 9B 中又有回答把几个来源写在一个括号里"
    "（“[资料2、资料3]”“[资料1/2]”“[资料1 vs 资料2]”“[资料1-6]”），被回答契约拒绝。canonical_source_markers"
    "把它们拆成每个来源一个 [资料N]，条件是每个编号都在本轮来源范围内、范围递增；其余照旧拒绝。提示模板、"
    "PROMPT_VERSION、检索、可答性与回答契约本身未改动；27B 两轮回答不含这些写法，35B 两次与 9B 的保存回答"
    "回放后只有这 7 题结果变化。v9 结果保留且不被覆盖。"
    "2026-11-09（specialist-review-v11）：评分 v4 样本外运行中发现两个回答契约问题。一是 35B-A3B 把提示里的占位符"
    "“[资料N]”原样写进正文，整条回答被拒；canonical_source_markers 删除这种不指向任何来源的占位符，删后没有真实引用"
    "的回答照旧被拒。二是复核提醒检查把正文里描述案情的“无需核验特定身份”当成放弃复核；agents.py 改为只有提到律师、"
    "或放弃复核的词结束分句时才算放弃，并让“请/需/须/待……复核”不再匹配“无需……”中的“需”。提示模板、PROMPT_VERSION、"
    "检索与可答性未改动；9 次保存运行新旧代码回放的逐题差异见评分 v4 记录。v10 结果保留且不被覆盖。"
    "2026-11-09（specialist-review-v12）：删除无调用者的代码——agents.py 的 AnalysisAgent、CrossExaminationAgent 兼容外壳及其两个从未被读取的属性，services.py 的 current_turn_key 与未使用常量 EXPORT_BLOCK_TYPES。提示模板、检索、回答契约、专家规则与调用路径均未改动。v11 结果保留且不被覆盖。"
    "2026-11-09（specialist-review-v13）：app/ 按领域拆成子包，八个算法文件移动到 review/、retrieval/、integration/，相对导入改为绝对导入；文件路径键改为相对 app 包的路径。函数体、提示模板、检索与调用路径均未改动。v12 结果保留且不被覆盖。"
    "2026-11-09（specialist-review-v14）：services.py 按职责拆分。钉住的 review/services.py 换成承接其推理代码的 review/chat.py（对话编排）、review/generation.py（提示与模型调用）、retrieval/search.py（关键词检索与路由）和 core/egress.py（出站与限时读取）；函数体逐字搬移，提示模板、检索、回答契约与调用路径均未改动。v13 结果保留且不被覆盖。"
    "2026-11-09（specialist-review-v15）：清零 mypy 豁免名单。agents.py、chat.py 只补变量类型注解，review_collaboration.py 加一处 cast，rag.py 把 `model or 默认` 写成等价的条件表达式；运行行为、提示模板、检索与调用路径均未改动。v14 结果保留且不被覆盖。"
    "2026-11-09（specialist-review-v16）：修复 search_backend.is_postgres 在拆包后仍按旧包名查找 db_postgres，导致 PostgreSQL 连接被当成 SQLite；SQLite 路径行为不变。v15 结果保留且不被覆盖。"
)
NATIVE_ALGORITHM_HASHES = {
    "review/agents.py": "cd9079120ea4daa573796341bed88c6645c8ad72618e257e6ab9c2e62dfc9257",
    "review/review_collaboration.py": "bd31436a75e69b9571f9300ed68dbe5efc519ac4bc09f0faae4aeec4fb66ee9b",
    "review/agent_tools.py": "07478f077cd29d7af407d175d7f84e9047aec199b08338231392420ec80c2dcb",
    "retrieval/rag.py": "66e793374377b67ca6f3e6ba11e6afd270f987e2119a0266c17724181bf49b2b",
    "retrieval/search_backend.py": "c52bc34710b004c9afebb514d2f40e73bf0fbe4a7321f95a811723074997b98d",
    "review/chat.py": "3d5282a80b6d923c0274003e96e12488adc94b06117b80e335d6c50201261a09",
    "review/generation.py": "e76677eadc80f51dc369cdc4c7a14b1dbf3508d690d8d7a2ce379f39e37cb314",
    "retrieval/search.py": "c20867d789449ffbddd3d9ecff1a6703e54950b6f45c1d3f6c90f34ffd93b92a",
    "core/egress.py": "213228963aff19f39adfaad746023b0c14dd3ffebeea21b825f53e4cddf0e008",
    "integration/model_domain.py": "b32602b3ede6ed3b6dfd8be3f246e45b492e3e990b3b0d1338e5b9bbff28855b",
    "retrieval/statutory_retrieval.py": "2e3540e3795c347f8002da7d9798646dc0ac609351a789b3517b781482492d53",
}


def native_baseline_identity():
    actual = {name: hashlib.sha256((APP_DIR / name).read_bytes()).hexdigest()
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
