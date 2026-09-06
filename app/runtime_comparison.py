"""Comparable, framework-neutral checks for native and LangGraph runs."""

from __future__ import annotations

import json
from typing import Any

from .agents import answer_contract, get_run_trace


SPECIALIST_NODES = ("facts", "evidence", "contradiction", "gap_detection")


def _citation_set(result: dict[str, Any]) -> set[tuple[int, int]]:
    return {
        (int(item["document_id"]), int(item["page"]))
        for item in result.get("citations", [])
    }


def _completed_nodes(result: dict[str, Any]) -> set[str]:
    return {
        item["node"]
        for item in result.get("steps", [])
        if item.get("status") == "completed"
    }


def _specialist_outputs(run_id: int, trace: dict[str, Any] | None = None) -> dict[str, str]:
    trace = get_run_trace(run_id) if trace is None else trace
    return {
        step["node_name"]: json.dumps(step["output"], ensure_ascii=False, sort_keys=True)
        for step in trace.get("steps", [])
        if step["node_name"] in SPECIALIST_NODES and step["status"] == "completed"
    }


def compare_runtime_results(
    native: dict[str, Any], langgraph: dict[str, Any],
    native_trace: dict[str, Any] | None = None, langgraph_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    native_contract = answer_contract(native.get("answer", ""), len(native.get("citations", [])))
    langgraph_contract = answer_contract(
        langgraph.get("answer", ""), len(langgraph.get("citations", []))
    )
    route_match = native.get("route") == langgraph.get("route")
    citation_match = _citation_set(native) == _citation_set(langgraph)
    node_match = _completed_nodes(native) == _completed_nodes(langgraph)
    specialist_match = _specialist_outputs(native["run_id"], native_trace) == _specialist_outputs(
        langgraph["run_id"], langgraph_trace
    )
    normalize_metrics = lambda result: {
        key: value for key, value in result.get("retrieval_metrics", {}).items() if key != "latency_ms"
    }
    retrieval_match = normalize_metrics(native) == normalize_metrics(langgraph)
    memory_match = native.get("memory_hits", []) == langgraph.get("memory_hits", [])
    structurally_equivalent = all(
        (route_match, citation_match, node_match, specialist_match, retrieval_match, memory_match)
    )
    answer_contract_match = all(native_contract.values()) and all(langgraph_contract.values())
    native_ms = int(native.get("total_ms") or 0)
    langgraph_ms = int(langgraph.get("total_ms") or 0)
    delta = langgraph_ms - native_ms
    return {
        "equivalent": structurally_equivalent and answer_contract_match,
        "structurally_equivalent": structurally_equivalent,
        "route_match": route_match,
        "citation_match": citation_match,
        "node_match": node_match,
        "specialist_output_match": specialist_match,
        "retrieval_metrics_match": retrieval_match,
        "memory_snapshot_match": memory_match,
        "answer_contract_match": answer_contract_match,
        "answer_contracts": {"native": native_contract, "langgraph": langgraph_contract},
        "native_total_ms": native_ms,
        "langgraph_total_ms": langgraph_ms,
        "latency_delta_ms": delta,
        "langgraph_overhead_percent": round(delta / native_ms * 100, 2) if native_ms else None,
        "checkpoint_size_bytes": int(langgraph.get("checkpoint_size_bytes") or 0),
    }
