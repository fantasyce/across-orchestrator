from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any


A2A_DELEGATION_SCHEMA = "across-a2a-task-delegation/2.0"


def create_a2a_task_delegation(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create an LF-compatible A2A task delegation projection.

    This does not call a remote agent. It gives hosts a deterministic Agent
    Card, JSON-RPC task request, streaming, push notification, artifact, and
    evidence receipt shape that can be sent through an A2A-compatible gateway
    or inspected locally.
    """

    payload = payload or {}
    goal = _text(payload.get("goal") or payload.get("task") or "Run Across agent-team readiness workflow")
    task_id = _text(payload.get("task_id") or payload.get("taskId") or _stable_task_id(goal))
    pack_id = _text(payload.get("pack_id") or payload.get("packId") or "plugin-compatibility-lab-v2")
    artifacts = _artifact_list(payload.get("artifacts"))
    state = _text(payload.get("state") or "submitted")
    if state not in {"submitted", "working", "input-required", "completed", "failed", "canceled"}:
        state = "submitted"
    agent_url = _text(payload.get("agent_url") or payload.get("agentUrl") or "https://agent.example.com/a2a")
    review_agent = _text(payload.get("review_agent") or payload.get("reviewAgent") or "review-agent")
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": A2A_DELEGATION_SCHEMA,
        "compatible_schema_versions": ["across-a2a-task-delegation/1.0"],
        "a2a_profile": "linux-foundation-a2a",
        "projection_only": True,
        "provider": "across-orchestrator",
        "agent_card": {
            "name": "Across Orchestrator Review Delegation",
            "url": agent_url,
            "version": "2.0",
            "capabilities": {
                "streaming": True,
                "pushNotifications": True,
                "stateTransitionHistory": True,
            },
            "skills": [
                {
                    "id": "plugin-compatibility-review",
                    "name": "Plugin Compatibility Review",
                    "description": "Review candidate workspaces and produce evidence for human promotion.",
                    "tags": ["plugin-compatibility", "review", "evidence"],
                }
            ],
        },
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
                "assigned_agent": review_agent,
            },
        },
        "jsonrpc": {
            "jsonrpc": "2.0",
            "id": task_id,
            "method": "tasks/send",
            "params": {
                "id": task_id,
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": goal}],
                },
                "metadata": {
                    "pack_id": pack_id,
                    "human_promotion_gate": True,
                    "candidate_workspace_review": True,
                },
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
        "streaming": {
            "method": "tasks/sendSubscribe",
            "events": ["TaskStatusUpdateEvent", "TaskArtifactUpdateEvent"],
            "resume": "task_id",
        },
        "push_notification": {
            "method": "tasks/pushNotificationConfig/set",
            "required_for_background_review": False,
            "host_must_confirm_endpoint": True,
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
