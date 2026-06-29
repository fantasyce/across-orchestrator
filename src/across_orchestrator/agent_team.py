from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from .redaction import redact_sensitive_value


AGENT_TEAM_SCHEMA = "across-agent-team/1.0"


def create_agent_team(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a first-class agent-team contract from host-supplied subagents."""

    payload = payload or {}
    team_id = _text(payload.get("team_id") or payload.get("teamId") or _stable_id(payload))
    owner = _text(payload.get("owner_agent") or payload.get("ownerAgent") or payload.get("owner") or "owner")
    raw_agents = payload.get("subagent_agents") or payload.get("subagentAgents") or payload.get("subtask_agents") or payload.get("subtaskAgents") or payload.get("agents")
    agents = _normalize_agents(raw_agents, owner=owner)
    context = _normalize_context(payload.get("context") or payload.get("team_context") or payload.get("teamContext"))
    handoffs = _normalize_handoffs(payload.get("handoffs"), agents)
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": AGENT_TEAM_SCHEMA,
        "team_id": team_id,
        "provider": "across-orchestrator",
        "created_at": now,
        "owner_agent": owner,
        "agents": agents,
        "context": context,
        "handoffs": handoffs,
        "checkpoint_policy": {
            "per_agent_checkpoint": True,
            "independent_session": True,
            "rewind_ready": True,
            "context_handoff": "notes_plus_just_in_time_retrieval",
        },
        "trust_boundary": {
            "secrets_included": False,
            "raw_transcripts_included": False,
            "host_owns_credentials": True,
        },
        "status": "passed" if agents else "attention",
    }


def _normalize_agents(raw_agents: Any, *, owner: str) -> list[dict[str, Any]]:
    values = raw_agents if isinstance(raw_agents, list) else []
    if not values:
        values = [{"id": owner, "role": "owner"}]
    result = []
    seen: set[str] = set()
    for index, value in enumerate(values[:24]):
        item = value if isinstance(value, dict) else {"id": value}
        agent_id = _text(item.get("id") or item.get("agent") or item.get("agent_id") or item.get("name") or f"agent-{index + 1}")
        if not agent_id or agent_id in seen:
            continue
        seen.add(agent_id)
        role = _text(item.get("role") or ("owner" if agent_id == owner else "subagent"))
        session_id = _text(item.get("session_id") or item.get("sessionId") or f"session:{agent_id}:{index + 1}")
        context_refs = item.get("context_refs") or item.get("contextRefs") or item.get("notes") or []
        if isinstance(context_refs, str):
            context_refs = [context_refs]
        result.append(
            {
                "agent_id": agent_id,
                "role": role,
                "session": {
                    "id": session_id,
                    "independent": True,
                    "checkpoint_ref": f"checkpoint:{agent_id}:{index + 1}",
                },
                "capabilities": [str(capability) for capability in item.get("capabilities", [])] if isinstance(item.get("capabilities"), list) else [],
                "context_refs": [str(ref) for ref in context_refs[:12]],
                "checkpoint": redact_sensitive_value(item.get("checkpoint") or {}),
            }
        )
    return result


def _normalize_context(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    notes = raw.get("notes") if isinstance(raw.get("notes"), list) else []
    retrieval = raw.get("retrieval") if isinstance(raw.get("retrieval"), dict) else {}
    return {
        "schema_version": "across-agent-team-context/1.0",
        "notes": [redact_sensitive_value(str(item)) for item in notes[:20]],
        "retrieval": redact_sensitive_value(retrieval),
        "raw_transcripts_included": False,
        "secrets_included": False,
    }


def _normalize_handoffs(raw: Any, agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values = raw if isinstance(raw, list) else []
    agent_ids = [agent["agent_id"] for agent in agents]
    if not values and len(agent_ids) > 1:
        values = [{"from": agent_ids[index], "to": agent_ids[index + 1], "mode": "notes"} for index in range(len(agent_ids) - 1)]
    result = []
    for index, value in enumerate(values[:24]):
        item = value if isinstance(value, dict) else {}
        result.append(
            {
                "id": _text(item.get("id") or f"handoff-{index + 1}"),
                "from": _text(item.get("from") or item.get("from_agent") or item.get("fromAgent") or (agent_ids[0] if agent_ids else "")),
                "to": _text(item.get("to") or item.get("to_agent") or item.get("toAgent") or (agent_ids[-1] if agent_ids else "")),
                "mode": _text(item.get("mode") or "notes_plus_retrieval"),
                "artifact": redact_sensitive_value(item.get("artifact") or {"path": "NOTES.md"}),
            }
        )
    return result


def _stable_id(payload: dict[str, Any]) -> str:
    seed = repr(redact_sensitive_value(payload)).encode("utf-8")
    return "agent-team-" + hashlib.sha256(seed).hexdigest()[:12]


def _text(value: Any) -> str:
    return str(value or "").strip()
