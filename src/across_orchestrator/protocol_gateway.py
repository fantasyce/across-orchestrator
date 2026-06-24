from __future__ import annotations

from typing import Any, Mapping

from .agent_card import render_agent_card
from .external_agents import render_external_agent_registry


PROTOCOL_GATEWAY_SCHEMA_VERSION = "across-orchestrator-protocol-gateway/1.0"


def render_protocol_gateway(card: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the Orchestrator-owned protocol gateway matrix.

    This is a bounded, public contract for AAA hosts that need to reason about
    external agent interop without scraping prose from the Agent Card.
    """

    agent_card = dict(card or render_agent_card())
    protocols = agent_card.get("protocols") if isinstance(agent_card.get("protocols"), Mapping) else {}
    external_agents = render_external_agent_registry([])
    routes = [
        _route(
            "a2a_agent_card",
            "A2A-like Agent Card",
            bool(_nested(protocols, "a2a", "agentCard")),
            _nested(protocols, "a2a", "agentCard"),
            "external_agents",
        ),
        _route(
            "http_agent_loop",
            "HTTP Agent Loop Runtime",
            bool(_nested(protocols, "http", "loopHealth") and _nested(protocols, "http", "loopEvidenceSummary")),
            _nested(protocols, "http", "loopHealth"),
            "agent_loop_runtime",
        ),
        _route(
            "mcp_agent_loop_controls",
            "MCP Agent Loop Controls",
            bool(_nested(protocols, "mcp", "tools") and _nested(protocols, "mcp", "approveAgentLoopAction")),
            "across-orchestrator mcp",
            "mcp_tools",
        ),
        _route(
            "host_model_decision",
            "Host Model Decision Boundary",
            bool(_nested(protocols, "http", "hostModelDecision")),
            _nested(protocols, "http", "hostModelDecision"),
            "model_control",
        ),
        _route(
            "autopilot_metadata",
            "Autopilot Metadata Contract",
            bool(_nested(protocols, "http", "autopilotMetadata")),
            _nested(protocols, "http", "autopilotMetadata"),
            "autopilot_interop",
        ),
        _route(
            "external_agent_plugins",
            "External Agent Plugin Registry",
            True,
            "across-orchestrator external-agents list --json",
            "external_agents",
            summary=external_agents["summary"],
        ),
    ]
    ready_count = sum(1 for item in routes if item["status"] == "passed")
    return {
        "schema_version": PROTOCOL_GATEWAY_SCHEMA_VERSION,
        "owner": "across-orchestrator",
        "status": "passed" if ready_count == len(routes) else "attention",
        "summary": {
            "route_count": len(routes),
            "ready_route_count": ready_count,
            "agent_card": agent_card.get("name"),
            "version": agent_card.get("version"),
        },
        "routes": routes,
        "security": {
            "secrets_included": False,
            "credentials_stay_with_host": True,
            "execution_boundary": "orchestrator_runtime",
        },
    }


def _route(route_id: str, title: str, ready: bool, endpoint: Any, kind: str, *, summary: Mapping[str, Any] | None = None) -> dict[str, Any]:
    route = {
        "id": route_id,
        "title": title,
        "kind": kind,
        "status": "passed" if ready else "attention",
        "endpoint": endpoint,
    }
    if summary:
        route["summary"] = dict(summary)
    return route


def _nested(value: Mapping[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current
