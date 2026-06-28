from __future__ import annotations

import hashlib
import json
from typing import Any

EVIDENCE_GRAPH_SCHEMA = "across-evidence-graph/1.0"


def build_evidence_graph_from_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    if payload.get("schema_version") == EVIDENCE_GRAPH_SCHEMA:
        return payload
    existing = payload.get("evidence_graph")
    if isinstance(existing, dict) and existing.get("schema_version") == EVIDENCE_GRAPH_SCHEMA:
        return {
            **existing,
            "verified_by": "across-orchestrator",
        }

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []

    def add_node(node: dict[str, Any]) -> None:
        if not node.get("id") or any(item["id"] == node["id"] for item in nodes):
            return
        node = dict(node)
        node.setdefault("hash", _sha256(node.get("payload", node.get("label", node["id"]))))
        nodes.append(node)

    def add_edge(source: str, target: str, relation: str) -> None:
        if source and target:
            edges.append({"from": source, "to": target, "relation": relation})

    run_id = str(payload.get("run_id") or payload.get("task_id") or payload.get("loop_id") or "unknown")
    spec_id = str(payload.get("spec_id") or payload.get("project") or payload.get("goal") or "unknown")
    run_node = f"run:{run_id}"
    spec_node = f"spec:{spec_id}"
    add_node({"id": spec_node, "type": "spec", "label": spec_id, "status": "declared", "payload": {"spec_id": spec_id}})
    add_node({"id": run_node, "type": "run", "label": run_id, "status": str(payload.get("status") or "unknown"), "payload": {"run_id": run_id}})
    add_edge(spec_node, run_node, "executes")

    _add_collection(payload.get("sources"), "source", run_node, "reads", add_node, add_edge)
    _add_collection(payload.get("actions"), "action", run_node, "runs", add_node, add_edge)
    _add_collection(payload.get("gates"), "gate", run_node, "validates", add_node, add_edge)
    _add_collection(payload.get("outputs") or payload.get("artifacts"), "output", run_node, "writes", add_node, add_edge)

    quality = payload.get("quality")
    if isinstance(quality, dict):
        add_node({"id": f"{run_node}:quality", "type": "quality", "label": "quality", "status": str(quality.get("status") or "unknown"), "payload": quality})
        add_edge(run_node, f"{run_node}:quality", "checks")

    memory = payload.get("memory")
    if isinstance(memory, dict):
        for index, item in enumerate(memory.get("written") or []):
            node_id = f"memory:{item.get('memory_id') or item.get('id') or index}"
            add_node({"id": node_id, "type": "memory", "label": node_id, "status": str(item.get("status") or "pending"), "payload": item})
            add_edge(run_node, node_id, "remembers")

    failure = payload.get("failure")
    if isinstance(failure, dict):
        node_id = f"{run_node}:failure"
        add_node({"id": node_id, "type": "failure", "label": str(failure.get("code") or "failure"), "status": "failed", "payload": failure})
        add_edge(run_node, node_id, "failed_with")

    return {
        "schema_version": EVIDENCE_GRAPH_SCHEMA,
        "run_id": run_id,
        "spec_id": spec_id,
        "status": str(payload.get("status") or "unknown"),
        "nodes": nodes,
        "edges": edges,
        "summary": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "source_count": len(payload.get("sources") or []),
            "action_count": len(payload.get("actions") or []),
            "gate_count": len(payload.get("gates") or []),
            "output_count": len(payload.get("outputs") or payload.get("artifacts") or []),
        },
        "verified_by": "across-orchestrator",
    }


def _add_collection(
    values: Any,
    node_type: str,
    run_node: str,
    relation: str,
    add_node: Any,
    add_edge: Any,
) -> None:
    if not isinstance(values, list):
        return
    for index, value in enumerate(values):
        item = value if isinstance(value, dict) else {"value": value}
        identifier = str(item.get("id") or item.get("adapter") or item.get("path") or index)
        node_id = f"{node_type}:{identifier}"
        add_node({
            "id": node_id,
            "type": node_type,
            "label": identifier,
            "status": str(item.get("status") or "unknown"),
            "payload": item,
        })
        add_edge(run_node, node_id, relation)


def _sha256(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
