from __future__ import annotations

from typing import Any


REMOTE_MCP_OAUTH_TEMPLATE_SCHEMA = "across-remote-mcp-oauth-template/1.0"

SUPPORTED_MCP_PROTOCOL_VERSIONS = ("2025-06-18", "2024-11-05")

DEFAULT_REQUIRED_SCOPES = ["mcp.tools", "mcp.resources", "across.evidence.read"]

DEFAULT_RUNTIME_STATUS = {
    "status": "server_partial",
    "streamable_http": "implemented",
    "oauth_resource_server": "implemented",
    "rfc8707_resource_indicators": "implemented",
    "pkce_supported": True,
    "jwks_caching": "in_memory",
    "scope_to_tool_mapping": "enforced",
    "session_header": "Mcp-Session-Id",
    "well_known_paths": [
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
    ],
    "notes": (
        "Streamable HTTP transport and OAuth Resource Server metadata endpoints are "
        "implemented in v0.7.8. Token validation, JWKS caching, scope enforcement, and "
        "RFC 8707 resource-indicator audience binding are enforced server-side. Client "
        "credentials and signing keys stay with the host."
    ),
}


def render_remote_mcp_oauth_template(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Render a secret-free remote MCP Streamable HTTP/OAuth deployment contract.

    The returned envelope stays read-only and never embeds secrets; the host is
    expected to inject issuer / JWKS URL / scopes via --config-json.

    v0.7.8 adds ``authorization_servers`` / ``resource_indicators`` /
    ``bearer_methods_supported`` / ``jwks_uri`` and a top-level ``_runtime_status``
    block describing the actual Streamable HTTP + OAuth Resource Server behavior
    shipped by this version. The schema_version stays
    ``across-remote-mcp-oauth-template/1.0`` so downstream consumers (AAA,
    Autopilot workflow packs, Plugin Compatibility Lab v2) remain compatible.
    """

    config = config or {}
    base_url = _text(config.get("base_url") or config.get("baseUrl") or "https://example.com/across/mcp")
    issuer = _text(config.get("issuer") or "https://issuer.example.com")
    audience = _text(config.get("audience") or base_url)
    jwks_uri = _text(config.get("jwks_uri") or config.get("jwksUri") or "")
    scopes = _string_list(config.get("scopes") or DEFAULT_REQUIRED_SCOPES)
    errors: list[str] = []
    if not (base_url.startswith("https://") or base_url.startswith("http://127.0.0.1") or base_url.startswith("http://localhost")):
        errors.append("base_url must be https or localhost for development.")
    if not issuer.startswith("https://"):
        errors.append("issuer must be an https URL.")
    if config.get("client_secret") or config.get("clientSecret"):
        errors.append("client secrets must stay with the host and cannot be embedded in the template.")
    if not scopes:
        errors.append("at least one OAuth scope is required.")
    if jwks_uri and not (jwks_uri.startswith("https://") or jwks_uri.startswith("http://127.0.0.1") or jwks_uri.startswith("http://localhost")):
        errors.append("jwks_uri must be https or localhost for development.")

    status = "passed" if not errors else "failed"

    authorization: dict[str, Any] = {
        "type": "oauth2_resource_server",
        "issuer": issuer,
        "audience": audience,
        "authorization_servers": [issuer],
        "resource_indicators": [audience],
        "required_scopes": scopes,
        "bearer_methods_supported": ["header"],
        "pkce_required": True,
        "pkce_code_challenge_methods_supported": ["S256"],
        "secrets_embedded": False,
        "scopes_supported": scopes,
    }
    if jwks_uri:
        authorization["jwks_uri"] = jwks_uri

    return {
        "schema_version": REMOTE_MCP_OAUTH_TEMPLATE_SCHEMA,
        "status": status,
        "transport": {
            "type": "streamable_http",
            "endpoint": base_url,
            "methods": ["POST"],
            "session": {"header": "Mcp-Session-Id", "resumable": True},
            "protocol_versions_supported": list(SUPPORTED_MCP_PROTOCOL_VERSIONS),
            "resumability": {
                "last_event_id_header": "Last-Event-ID",
                "stream_redelivery": False,
            },
        },
        "authorization": authorization,
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
        "well_known": {
            "protected_resource": "/.well-known/oauth-protected-resource",
            "authorization_server": "/.well-known/oauth-authorization-server",
        },
        "checks": [
            _check("https_or_localhost", not errors or "base_url must be https or localhost for development." not in errors),
            _check("oauth_issuer_https", not errors or "issuer must be an https URL." not in errors),
            _check("no_client_secret", "client secrets must stay with the host and cannot be embedded in the template." not in errors),
            _check("scopes_declared", bool(scopes)),
        ],
        "errors": errors,
        "_runtime_status": dict(DEFAULT_RUNTIME_STATUS),
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
