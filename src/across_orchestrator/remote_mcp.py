from __future__ import annotations

from typing import Any


REMOTE_MCP_OAUTH_TEMPLATE_SCHEMA = "across-remote-mcp-oauth-template/1.0"


def render_remote_mcp_oauth_template(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Render a secret-free remote MCP Streamable HTTP/OAuth deployment contract."""

    config = config or {}
    base_url = _text(config.get("base_url") or config.get("baseUrl") or "https://example.com/across/mcp")
    issuer = _text(config.get("issuer") or "https://issuer.example.com")
    audience = _text(config.get("audience") or "across-orchestrator")
    scopes = _string_list(config.get("scopes") or ["mcp.tools", "mcp.resources", "across.evidence.read"])
    errors: list[str] = []
    if not (base_url.startswith("https://") or base_url.startswith("http://127.0.0.1") or base_url.startswith("http://localhost")):
        errors.append("base_url must be https or localhost for development.")
    if not issuer.startswith("https://"):
        errors.append("issuer must be an https URL.")
    if config.get("client_secret") or config.get("clientSecret"):
        errors.append("client secrets must stay with the host and cannot be embedded in the template.")
    if not scopes:
        errors.append("at least one OAuth scope is required.")

    status = "passed" if not errors else "failed"
    return {
        "schema_version": REMOTE_MCP_OAUTH_TEMPLATE_SCHEMA,
        "status": status,
        "transport": {
            "type": "streamable_http",
            "endpoint": base_url,
            "methods": ["POST"],
            "session": {"header": "Mcp-Session-Id", "resumable": True},
        },
        "authorization": {
            "type": "oauth2_resource_server",
            "issuer": issuer,
            "audience": audience,
            "required_scopes": scopes,
            "pkce_required": True,
            "secrets_embedded": False,
        },
        "tool_surface": {
            "tools": [
                "evaluate_sandbox_policy",
                "build_evidence_graph",
                "evaluate_agent_team_readiness",
                "export_otel_genai_spans",
                "create_a2a_task_delegation",
            ],
            "resources": [
                "across-orchestrator://agent-card",
                "across-orchestrator://plugin-manifest",
            ],
        },
        "checks": [
            _check("https_or_localhost", not errors or "base_url must be https or localhost for development." not in errors),
            _check("oauth_issuer_https", not errors or "issuer must be an https URL." not in errors),
            _check("no_client_secret", "client secrets must stay with the host and cannot be embedded in the template." not in errors),
            _check("scopes_declared", bool(scopes)),
        ],
        "errors": errors,
    }


def _check(check_id: str, passed: bool) -> dict[str, str]:
    return {"id": check_id, "status": "passed" if passed else "failed"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
