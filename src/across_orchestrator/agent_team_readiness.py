from __future__ import annotations

from typing import Any


AGENT_TEAM_READINESS_SCHEMA = "across-agent-team-readiness/1.0"

REQUIRED_HOST_TARGETS = {"codex", "claude_code", "mcp", "a2a", "across"}
REQUIRED_RECEIPT_EVIDENCE = {"runtime_policy", "trust_boundary", "host_exports", "evidence_graph", "validation_gates"}


def evaluate_agent_team_readiness(payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate whether a Workflow Pack export is market-ready for agent teams.

    The check is intentionally independent from Autopilot. Autopilot owns the
    pack and product card; Orchestrator verifies that the exported contract is
    concrete enough for a generic host, coding-agent dashboard, or human review
    flow to use without relying on hidden AAA internals.
    """

    pack_id = str(payload.get("pack_id") or payload.get("id") or "")
    product_card = _dict(payload.get("product_card") or payload.get("productCard"))
    trust_receipt = _dict(payload.get("trust_receipt") or payload.get("trustReceipt"))
    protocol_readiness = _dict(payload.get("protocol_readiness") or payload.get("protocolReadiness"))
    frontier_interop = _dict(payload.get("frontier_interop") or payload.get("frontierInterop"))
    trust_boundary = _dict(payload.get("trust_boundary") or payload.get("trustBoundary"))
    host_targets = set(str(item) for item in _list(payload.get("host_targets") or payload.get("hostTargets")))

    checks = [
        _check(
            "product_card_present",
            product_card.get("schema_version") == "across-workflow-pack-product-card/1.0",
            "Workflow export includes a user-facing product task card.",
            required=True,
        ),
        _check(
            "workflow_first_positioning",
            bool(product_card.get("user_problem")) and bool(product_card.get("job_to_be_done")) and bool(product_card.get("quickstart")),
            "Product card explains the user problem, job-to-be-done, and quickstart.",
            required=True,
        ),
        _check(
            "host_targets_complete",
            REQUIRED_HOST_TARGETS.issubset(host_targets),
            "Workflow can be exported to Codex, Claude Code, MCP, A2A, and Across hosts.",
            required=True,
            details={"host_targets": sorted(host_targets)},
        ),
        _check(
            "trust_receipt_present",
            trust_receipt.get("schema_version") == "across-agent-team-trust-receipt/1.0",
            "Workflow export includes an adoption/promotion trust receipt template.",
            required=True,
        ),
        _check(
            "evidence_contract_complete",
            REQUIRED_RECEIPT_EVIDENCE.issubset(set(_list(_dict(trust_receipt.get("evidence_contract")).get("required")))),
            "Trust receipt requires runtime policy, boundary, host exports, evidence graph, and validation gates.",
            required=True,
        ),
        _check(
            "no_secret_boundary",
            trust_boundary.get("secrets") == "not_allowed",
            "Workflow export keeps secrets outside the pack boundary.",
            required=True,
        ),
        _check(
            "human_promotion_gate",
            _nested(payload, "runtime_policy", "promotion", "human_approval_required") is True,
            "Promotion remains human-gated.",
            required=True,
        ),
        _check(
            "protocol_readiness_present",
            protocol_readiness.get("schema_version") == "across-workflow-pack-protocol-readiness/1.0",
            "Workflow export includes honest protocol maturity status.",
            required=True,
        ),
        _check(
            "honest_protocol_claims",
            _nested(protocol_readiness, "summary", "honest_protocol_claims") is True,
            "Protocol readiness distinguishes shipped, partial, and planned capabilities.",
            required=True,
        ),
        _check(
            "remote_mcp_not_overclaimed",
            _protocol_status(protocol_readiness, "remote_mcp_http_oauth") in {"planned", "partial", "passed", ""},
            "Remote MCP/OAuth claim is backed by a declared projection or shipped endpoint status.",
            required=True,
        ),
        _check(
            "frontier_interop_present",
            frontier_interop.get("schema_version") == "across-workflow-pack-frontier-interop/1.0",
            "Workflow export includes remote MCP, A2A, and OTel/eval contracts.",
            required=True,
        ),
        _check(
            "remote_mcp_oauth_template_ready",
            _nested(frontier_interop, "remote_mcp", "schema_version") == "across-remote-mcp-oauth-template/1.0"
            and _nested(frontier_interop, "remote_mcp", "oauth_required") is True,
            "Remote MCP Streamable HTTP/OAuth template is present without secrets.",
            required=True,
        ),
        _check(
            "a2a_delegation_contract_ready",
            _nested(frontier_interop, "a2a", "schema_version") in {
                "across-a2a-task-delegation/1.0",
                "across-a2a-task-delegation/2.0",
            },
            "A2A task/message/artifact delegation contract is present.",
            required=True,
        ),
        _check(
            "projection_status_ready",
            _projection_contracts_ready(frontier_interop),
            "MCP Tasks, A2A, AG-UI, Remote MCP/OAuth, and OTel projections are visible to host scoring.",
            required=True,
        ),
        _check(
            "otel_genai_export_ready",
            _nested(frontier_interop, "observability", "otel_schema") == "across-otel-genai-export/1.0"
            and _nested(frontier_interop, "observability", "raw_transcripts_included") is False,
            "OTel/GenAI export contract is present and excludes raw transcripts.",
            required=True,
        ),
        _check(
            "otlp_trace_export_ready",
            _nested(frontier_interop, "observability", "otlp_trace_schema") == "otlp-traces-json/1.0",
            "Collector-friendly OTLP trace JSON contract is present for external observability smoke tests.",
            required=True,
        ),
        _check(
            "first_value_artifact",
            bool(_nested(product_card, "market_readiness", "first_value_artifact")),
            "Product card names the first artifact that proves value.",
        ),
    ]

    failed_required = [item for item in checks if item["required"] and item["status"] == "failed"]
    passed_count = sum(1 for item in checks if item["status"] == "passed")
    score = round((passed_count / max(1, len(checks))) * 100)
    status = "failed" if failed_required else "passed" if score >= 80 else "attention"
    return {
        "schema_version": AGENT_TEAM_READINESS_SCHEMA,
        "plugin": "across-orchestrator",
        "pack_id": pack_id or None,
        "status": status,
        "score": score,
        "summary": {
            "passed_count": passed_count,
            "failed_count": len(checks) - passed_count,
            "required_failure_count": len(failed_required),
            "market_ready": status == "passed",
            "differentiation": "agent-team trust layer",
        },
        "checks": checks,
    }


def _check(check_id: str, passed: bool, message: str, *, required: bool = False, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": "passed" if passed else "failed",
        "required": required,
        "message": message,
        **({"details": details} if details else {}),
    }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _nested(value: dict[str, Any], *path: str) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _protocol_status(protocol_readiness: dict[str, Any], check_id: str) -> str:
    for item in _list(protocol_readiness.get("checks")):
        if isinstance(item, dict) and item.get("id") == check_id:
            return str(item.get("status") or "")
    return ""


def _projection_contracts_ready(frontier_interop: dict[str, Any]) -> bool:
    projection = _dict(frontier_interop.get("projections") or frontier_interop.get("projection_status"))
    if not projection:
        return True
    dimensions = _dict(projection.get("dimensions"))
    if dimensions:
        projection = dimensions
    required = {"mcp_tasks", "a2a", "ag_ui", "remote_mcp_oauth", "otel"}
    statuses = {key: str(value.get("status") if isinstance(value, dict) else value) for key, value in projection.items()}
    return required.issubset(statuses) and all(statuses[item] in {"passed", "partial", "projection_only"} for item in required)
