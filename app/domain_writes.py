"""Versioned domain write contract; still restricted to newly marked isolated DBs."""
from __future__ import annotations

import json
import re
from copy import deepcopy

from .db import now
from .forge_readonly import capture_corpus, exact_quote
from .forge_sources import verify_statutes
from .host_writes import FIELDS, LocalHostWritePort, canonical, digest, source_version
from .runtime_ports import AccessDeniedError, ContractValidationError, RequestConflictError, WriteAction

DOMAIN_RUNTIME = "forgeharness-domain-isolated-v1"
DOMAIN_GRAPH = "legal-review-isolated-v1"
CANDIDATE_RUNTIME = "forgeharness-domain-candidate-v1"


def corpus_identity():
    return capture_corpus()[0]


class DomainHostWritePort(LocalHostWritePort):
    runtime = DOMAIN_RUNTIME
    receipt_version = 2
    # A domain result can contain the full 200k-character retrieval bundle in UTF-8.
    max_write_bytes = 1_800_000
    fields = deepcopy(FIELDS)
    fields[WriteAction.STEP_RECORD] = (
        {"run_id", "node_name", "role", "status", "input", "output"},
        {"run_id", "node_name", "role", "status", "input", "output"},
    )
    fields[WriteAction.CONVERSATION_MESSAGE] = (
        FIELDS[WriteAction.CONVERSATION_MESSAGE][0],
        FIELDS[WriteAction.CONVERSATION_MESSAGE][1] | {"strict_answer", "statutory_sources"},
    )

    @classmethod
    def _source_version(cls, conn, scope, *, writing=False):
        return digest({"domain_source": 1, "host": source_version(conn, scope, writing=writing),
                       "corpus": corpus_identity()})

    def _validate_citation_shape(self, payload):
        if "citations" not in payload:
            return
        citations = payload["citations"]
        if not isinstance(citations, list) or len(citations) > 100:
            raise ContractValidationError("citations exceed domain contract")
        if payload["role"] == "user":
            if citations or "strict_answer" in payload or "statutory_sources" in payload:
                raise ContractValidationError("user message cannot assert verified sources")
            return
        if not isinstance(payload.get("strict_answer"), str) or not isinstance(payload.get("statutory_sources"), list):
            raise ContractValidationError("assistant requires a strict answer and statutory source list")
        allowed = {"index", "document_id", "document_name", "page", "quote", "url", "retrieval_explain", "channels"}
        required = allowed - {"retrieval_explain", "channels"}
        for index, citation in enumerate(citations, 1):
            if (not isinstance(citation, dict) or not required <= citation.keys() or citation.keys() - allowed
                    or type(citation["index"]) is not int or citation["index"] != index
                    or type(citation["document_id"]) is not int or citation["document_id"] < 1
                    or type(citation["page"]) is not int or citation["page"] < 1
                    or any(not isinstance(citation[k], str) or not citation[k].strip()
                           for k in ("document_name", "quote", "url"))):
                raise ContractValidationError("invalid citation identity")

    def _validate(self, request):
        payload = super()._validate(request)
        if request.action == WriteAction.STEP_RECORD:
            if (payload["status"] not in {"completed", "failed"}
                    or not isinstance(payload["role"], str) or not payload["role"].strip()
                    or not isinstance(payload["input"], dict)):
                raise ContractValidationError("invalid domain step")
        return payload

    def verify_sources(self, conn, payload):
        """Verify source identity and exact spans; never claim semantic entailment."""
        pages = {}
        for citation in payload["citations"]:
            row = conn.execute("SELECT d.name,p.text FROM pages p JOIN documents d ON d.id=p.document_id "
                "WHERE d.case_id=? AND d.id=? AND p.page_no=?",
                (self.scope.case_id, citation["document_id"], citation["page"])).fetchone()
            if row is None:
                raise AccessDeniedError("citation source unavailable")
            if (row["name"] != citation["document_name"] or citation["url"] !=
                    f"/api/documents/{citation['document_id']}/file#page={citation['page']}"):
                raise ContractValidationError("citation metadata differs from source")
            exact_quote(row["text"], citation["quote"])
            pages[citation["index"]] = row["text"]
        strict = payload["strict_answer"]
        markers = re.findall(r"\[page:(\d+)\]", strict)
        quotes = re.findall(r"\[page:(\d+)\]「([^」]*)」", strict)
        if len(markers) != len(quotes) or any(
            int(index) not in pages or not quote.strip() or quote not in pages[int(index)]
            for index, quote in quotes
        ):
            raise ContractValidationError("answer quote differs from pinned page")
        statutes = payload["statutory_sources"]
        if statutes or "[statute:" in strict:
            current_corpus, corpus = capture_corpus()
            valid, _ = verify_statutes(strict, statutes, corpus_version=current_corpus)
            if not valid or corpus is None:
                raise ContractValidationError("statutory source validation failed")
            # A source's self-reported digest is not evidence of membership.
            from .forge_sources import statute_source
            for source in statutes:
                hits = [corpus._result(document, article) for document in corpus.documents
                        if document["document_id"] == source["document_id"]
                        for article in document["articles"]
                        if article["article_number"] == source["article_number"]
                        and article["subarticle_number"] == source["subarticle_number"]]
                candidates = [statute_source(h, current_corpus, source["index"]) for h in hits]
                if source not in candidates:
                    raise ContractValidationError("statute is not a member of the pinned corpus")
        elif not markers:
            raise ContractValidationError("no sources; evidence is insufficient")
        for index in re.findall(r"\[\s*资料\s*(\d+)\s*\]", payload["content"]):
            if int(index) not in pages:
                raise ContractValidationError("answer cites unavailable source")

    def _apply(self, conn, action, payload):
        if action == WriteAction.CONVERSATION_MESSAGE and payload["role"] == "assistant":
            self.verify_sources(conn, payload)
        if action != WriteAction.STEP_RECORD:
            return super()._apply(conn, action, payload)
        run = conn.execute("SELECT case_id,runtime FROM agent_runs WHERE id=?", (payload["run_id"],)).fetchone()
        if run is None or run["case_id"] != self.scope.case_id or run["runtime"] != self.runtime:
            raise AccessDeniedError("run unavailable")
        row_id = conn.execute(
            "INSERT INTO agent_steps(run_id,node_name,agent_role,status,input_json,output_json,started_at,finished_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (payload["run_id"], payload["node_name"], payload["role"], payload["status"],
             canonical(payload["input"]), canonical(payload["output"]), now(), now())).lastrowid
        return {"schema_version": self.receipt_version, "table": "agent_steps", "row_id": row_id,
                "effect_version": self._effect_version(conn, "agent_steps", row_id)}

    def _check_effect(self, conn, row):
        super()._check_effect(conn, row)
        result = json.loads(row["result_json"])
        if result["table"] != "agent_steps":
            return
        output = json.loads(conn.execute("SELECT output_json FROM agent_steps WHERE id=?", (result["row_id"],)).fetchone()[0])
        for receipt_id in output.get("_effect_receipts", []):
            child = conn.execute("SELECT * FROM host_write_receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
            if (child is None or child["case_id"] != row["case_id"] or child["principal_key"] != row["principal_key"]
                    or child["source_version"] != row["source_version"]
                    or not child["idempotency_key"].startswith(row["idempotency_key"] + ":effect:")
                    or json.loads(child["result_json"])["table"] == "agent_steps"):
                raise RequestConflictError("composite effect receipt unavailable")
            super()._check_effect(conn, child)
