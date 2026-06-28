from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any


A2A_DELEGATION_SCHEMA = "across-a2a-task-delegation/1.0"


def create_a2a_task_delegation(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a minimal executable A2A-style task envelope.

    This does not call a remote agent. It gives any host a deterministic task,
    message, artifact, and evidence receipt shape that can be sent through an
    A2A-compatible gateway or inspected locally.
    """

    payload = payload or {}
    goal = _text(payload.get("goal") or payload.get("task") or "Run Across agent-team readiness workflow")
    task_id = _text(payload.get("task_id") or payload.get("taskId") or _stable_task_id(goal))
    pack_id = _text(payload.get("pack_id") or payload.get("packId") or "plugin-compatibility-lab-v2")
    artifacts = _artifact_list(payload.get("artifacts"))
    state = _text(payload.get("state") or "submitted")
    if state not in {"submitted", "working", "input-required", "completed", "failed", "canceled"}:
        state = "submitted"
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": A2A_DELEGATION_SCHEMA,
        "provider": "across-orchestrator",
        "task": {
            "id": task_id,
            "kind": "task",
            "state": state,
            "created_at": now,
            "metadata": {
                "pack_id": pack_id,
                "trust_receipt_required": True,
                "human_promotion_gate": True,
                "secrets_allowed": False,
            },
        },
        "message": {
            "role": "user",
            "parts": [
                {
                    "kind": "text",
                    "text": goal,
                }
            ],
        },
        "artifacts": artifacts,
        "evidence_receipt": {
            "schema_version": "across-agent-team-trust-receipt/1.0",
            "required": ["runtime_policy", "trust_boundary", "host_exports", "evidence_graph", "validation_gates"],
            "graph_schema": "across-evidence-graph/1.0",
        },
        "status": "passed",
    }


def _artifact_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        value = ["run://plugin-compatibility-lab/report.md", "run://plugin-compatibility-lab/evidence.json"]
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value[:8]):
        if isinstance(item, dict):
            uri = _text(item.get("uri") or item.get("ref") or item.get("path") or f"artifact:{index}")
            name = _text(item.get("name") or uri.rsplit("/", 1)[-1])
            media_type = _text(item.get("media_type") or item.get("mediaType") or _media_type(uri))
        else:
            uri = _text(item)
            name = uri.rsplit("/", 1)[-1] if uri else f"artifact-{index}"
            media_type = _media_type(uri)
        result.append({"id": f"artifact-{index + 1}", "name": name, "uri": uri, "media_type": media_type})
    return result


def _media_type(uri: str) -> str:
    if uri.endswith(".json"):
        return "application/json"
    if uri.endswith(".md") or uri.endswith(".markdown"):
        return "text/markdown"
    return "text/plain"


def _stable_task_id(goal: str) -> str:
    digest = hashlib.sha256(goal.encode("utf-8")).hexdigest()[:12]
    return f"a2a-task-{digest}"


def _text(value: Any) -> str:
    return str(value or "").strip()
