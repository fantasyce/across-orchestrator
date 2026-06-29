from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import json
import os
import time
import uuid

from .agui_projection import project_event_sse, project_events_to_agui
from .agent_card import render_agent_card
from .agent_loop import AgentLoopConcurrencyError, AgentLoopRuntime
from .host_conformance import evaluate_host_conformance
from .mcp import handle_tool_call, read_resource, resource_definitions, tool_definitions
from .paths import COMPONENT_ID, contains_protected_user_reference, is_developer_mode, is_product_mode, run_home
from .plugin_manifest import render_plugin_health, render_plugin_manifest
from .runtime import OrchestratorRuntime
from ._remote_mcp_oauth_runtime import (
    is_origin_allowed,
    is_safe_session_id,
    issue_session_id,
    parse_bearer_token,
    render_authorization_server_metadata,
    render_protected_resource_metadata,
    render_www_authenticate,
    token_satisfies_resource_scopes,
    token_satisfies_tool_scopes,
    verify_bearer_token,
)


LOOP_STREAM_CLOSING_EVENT_TYPES = {
    "loop.approval_required",
    "loop.completed",
    "loop.failed",
    "loop.stopped",
    "loop.cancelled",
}


# ----- Remote MCP OAuth Resource Server (Streamable HTTP) ---------------------

STREAMABLE_HTTP_PROTOCOL_VERSION = "2025-06-18"
STREAMABLE_HTTP_PROTOCOL_LEGACY_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = (STREAMABLE_HTTP_PROTOCOL_VERSION, STREAMABLE_HTTP_PROTOCOL_LEGACY_VERSION)

DEFAULT_LEGACY_PROTOCOL_VERSION = "2025-03-26"
OAUTH_CHALLENGE_ERRORS = {"invalid_request", "invalid_token", "insufficient_scope"}
OAUTH_CHALLENGE_SCOPES = {
    "all": "mcp.tools mcp.resources across.evidence.read",
    "tools": "mcp.tools",
    "resources": "mcp.resources",
    "evidence": "mcp.resources across.evidence.read",
}
OAUTH_CHALLENGE_DESCRIPTIONS = {
    "invalid_request": "Bearer token is required.",
    "invalid_token": "Bearer token did not satisfy resource server policy.",
    "insufficient_scope": "Token scope does not permit the requested operation.",
}

JSON_RPC_PARSE_ERROR = -32700
JSON_RPC_INVALID_REQUEST = -32600
JSON_RPC_METHOD_NOT_FOUND = -32601
JSON_RPC_INVALID_PARAMS = -32602
JSON_RPC_INTERNAL_ERROR = -32603

MCP_SERVER_INFO = {"name": "across-orchestrator", "version": "0.7.8"}

MCP_SERVER_CAPABILITIES = {
    "tools": {"listChanged": False},
    "resources": {"subscribe": False},
}

MCP_DEFAULT_INSTRUCTIONS = (
    "Across Orchestrator exposes durable task and Agent Loop execution. "
    "Tokens are bound to the configured canonical resource URI (RFC 8707)."
)


def _jsonrpc_error(message_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


def _jsonrpc_result(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _http_header_value(value: Any) -> str:
    text = str(value)
    if "\r" in text or "\n" in text:
        raise ValueError("HTTP header values may not contain CR or LF")
    return text


def _server_managed_project_root(kind: str) -> str:
    safe_kind = "".join(ch for ch in str(kind or "task") if ch.isalnum() or ch in {"-", "_"}).strip("-_")
    if not safe_kind:
        safe_kind = "task"
    return str(run_home() / "http-workspaces" / safe_kind / f"{safe_kind}-{uuid.uuid4().hex}")


def build_remote_mcp_oauth_config(
    *,
    host: str,
    port: int,
    issuer: str | None = None,
    audience: str | None = None,
    jwks_uri: str | None = None,
    hs256_secret: str | None = None,
    jwks_url: str | None = None,
    scopes: list[str] | tuple[str, ...] | None = None,
    allowed_origins: list[str] | tuple[str, ...] | None = None,
    required_claims: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Compose the runtime OAuth config consumed by ``OrchestratorHandler``.

    The default ``audience`` is the canonical MCP server URI per RFC 8707 §2 +
    MCP 2025-06-18 §Authorization — ``<scheme>://<host>:<port>/mcp``. Hosts
    must inject the production HTTPS canonical URI through ``--config-json``
    when running outside ``localhost`` (e.g. ``https://mcp.example.com/mcp``).

    The default ``issuer`` is the loopback ``host:port`` so tests and local
    installs work without additional configuration. Production hosts must
    inject the real issuer through ``--config-json``.

    The default ``allowed_origins`` mirrors the loopback base URL so browser-
    based MCP clients can connect without extra config. Hosts can override
    with ``--config-json`` to broaden or tighten the list.
    """

    base_url = f"http://{host}:{port}"
    resolved_audience = audience or f"{base_url}/mcp"
    resolved_issuer = issuer or base_url
    resolved_scopes = list(scopes) if scopes else ["mcp.tools", "mcp.resources", "across.evidence.read"]
    config: dict[str, Any] = {
        "base_url": base_url,
        "mcp_endpoint": "/mcp",
        "issuer": resolved_issuer,
        "audience": resolved_audience,
        "scopes": resolved_scopes,
        "well_known_protected_resource": "/.well-known/oauth-protected-resource",
        "well_known_authorization_server": "/.well-known/oauth-authorization-server",
        "allowed_origins": list(allowed_origins) if allowed_origins else [base_url],
    }
    if jwks_uri:
        config["jwks_uri"] = jwks_uri
    if hs256_secret:
        config["hs256_secret"] = hs256_secret
    if jwks_url:
        config["jwks_url"] = jwks_url
    if required_claims:
        config["required_claims"] = [str(claim).strip() for claim in required_claims if str(claim).strip()]
    return config


def apply_remote_mcp_oauth_config(server: OrchestratorHTTPServer, config: dict[str, Any] | None) -> None:
    """Attach OAuth runtime config to the underlying HTTP server."""

    server.remote_mcp_oauth = dict(config) if config else {}


def _oauth_config(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    return getattr(handler.server, "remote_mcp_oauth", {}) or {}


def _public_base_url(handler: BaseHTTPRequestHandler, config: dict[str, Any]) -> str:
    explicit = config.get("base_url")
    if explicit:
        return str(explicit)
    host, port = handler.server.server_address[:2]
    return f"http://{host}:{port}"


# ----- Existing HTTP server ---------------------------------------------------


class OrchestratorHandler(BaseHTTPRequestHandler):
    server_version = "AcrossOrchestrator/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def runtime(self) -> OrchestratorRuntime:
        return self.server.runtime  # type: ignore[attr-defined]

    @property
    def loop_runtime(self) -> AgentLoopRuntime:
        return self.server.loop_runtime  # type: ignore[attr-defined]

    # ----- MCP shared helpers ------------------------------------------------

    def _origin_allows(self) -> bool:
        """Validate the Origin header against the configured allowlist."""

        config = _oauth_config(self)
        allowed, _reason = is_origin_allowed(
            self.headers.get("Origin"),
            allowed_origins=tuple(config.get("allowed_origins") or ()),
            allow_loopback_when_no_origin=True,
        )
        if not allowed:
            self._respond_jsonrpc_with_status(
                {
                    "error": "dns_rebinding_blocked",
                    "error_description": (
                        "Origin header does not match the configured allowlist "
                        "for this MCP server."
                    ),
                },
                status=403,
            )
            return False
        return True

    def _authorize(self) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Run OAuth bearer-token verification; return (config, claims) or None."""

        config = _oauth_config(self)
        if not config:
            self.respond({"error": "remote_mcp_oauth_not_configured"}, status=404)
            return None
        token = parse_bearer_token(self.headers.get("Authorization"))
        if not token:
            self.respond_unauthorized(
                {"error": "missing_token", "error_description": "Authorization Bearer token required."},
                error="invalid_request",
            )
            return None
        verification = verify_bearer_token(
            token,
            audience=str(config.get("audience") or f"{_public_base_url(self, config)}/mcp"),
            issuer=str(config.get("issuer") or _public_base_url(self, config)),
            hs256_secret=config.get("hs256_secret"),
            jwks_url=config.get("jwks_url"),
            required_scopes_list=config.get("scopes"),
            required_claims=tuple(str(claim) for claim in (config.get("required_claims") or []) if str(claim).strip()) or None,
        )
        if "error" in verification:
            error_code = str(verification["error"])
            challenge_error = error_code if error_code in {"invalid_token", "invalid_request", "insufficient_scope"} else "invalid_token"
            self.respond_unauthorized(
                {
                    "error": error_code,
                    "error_description": verification.get("error_description"),
                },
                status=403 if error_code == "insufficient_scope" else 401,
                error=challenge_error,
                scope="all",
            )
            return None
        return config, verification["claims"]

    def _negotiated_protocol_version(self) -> str:
        """Honor ``MCP-Protocol-Version`` or fall back to 2025-06-18."""

        requested = self.headers.get("MCP-Protocol-Version") or STREAMABLE_HTTP_PROTOCOL_VERSION
        if requested not in SUPPORTED_PROTOCOL_VERSIONS:
            return STREAMABLE_HTTP_PROTOCOL_VERSION
        return requested

    # ----- do_DELETE: session termination ----------------------------------

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/mcp":
            self.respond({"error": "not_found"}, status=404)
            return
        try:
            if not self._origin_allows():
                return
            if self._authorize() is None:
                return
            session_id = self.headers.get("Mcp-Session-Id") or ""
            if session_id and not is_safe_session_id(session_id):
                self.respond({"error": "bad_request", "detail": "Invalid session id."}, status=400)
                return
            sessions = getattr(self.server, "remote_mcp_sessions", None)
            if sessions is not None and session_id:
                sessions.pop(session_id, None)
            self.send_response(204)
            self.end_headers()
        except Exception:
            self.respond({"error": "internal_error"}, status=500)

    # ----- GET /mcp: SSE notification stream --------------------------------

    def handle_mcp_get_sse(self) -> None:
        if self._authorize() is None:
            return
        accept = self.headers.get("Accept") or ""
        if "text/event-stream" not in accept:
            # MCP 2025-06-18 §Streamable HTTP: server MUST return 405 if it does
            # not offer an SSE stream at this endpoint.
            self.send_response(405)
            self.send_header("Allow", "POST, DELETE")
            self.end_headers()
            return
        # Streamable HTTP server-to-client notifications. Across Orchestrator
        # does not push unsolicited server requests in this release; we emit
        # an idle keep-alive comment until the client closes.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(b": mcp-server-sse-open\n\n")
            self.wfile.flush()
            deadline = time.time() + 30
            while time.time() < deadline:
                time.sleep(5)
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    # ----- POST /mcp: JSON-RPC dispatcher ----------------------------------

    def handle_mcp_post(self) -> None:
        if not self._origin_allows():
            return
        if self._authorize() is None:
            return
        accept = self.headers.get("Accept") or ""
        if "application/json" not in accept or "text/event-stream" not in accept:
            # MCP spec §"Sending Messages": clients MUST include both content
            # types. We still accept either for compat but emit a hint when
            # neither is present.
            pass

        try:
            raw = self._read_request_body()
        except ValueError as exc:
            self._respond_jsonrpc_with_status(
                _jsonrpc_error(None, JSON_RPC_PARSE_ERROR, "Parse error: " + str(exc)),
                status=400,
            )
            return

        if not raw:
            self._respond_jsonrpc_with_status(
                _jsonrpc_error(None, JSON_RPC_INVALID_REQUEST, "Request body is required."),
                status=400,
            )
            return

        try:
            message = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._respond_jsonrpc_with_status(
                _jsonrpc_error(None, JSON_RPC_PARSE_ERROR, "Invalid JSON: " + str(exc)),
                status=400,
            )
            return

        # Notifications / responses: server returns 202 Accepted with no body.
        method = message.get("method") if isinstance(message, dict) else None
        is_request = isinstance(message, dict) and "method" in message and ("id" in message)
        if not is_request:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        message_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}

        if method == "initialize":
            self._mcp_dispatch_initialize(message_id, params)
            return
        if method == "tools/list":
            self._mcp_dispatch_tools_list(message_id, params)
            return
        if method == "tools/call":
            self._mcp_dispatch_tools_call(message_id, params)
            return
        if method == "resources/list":
            self._mcp_dispatch_resources_list(message_id, params)
            return
        if method == "resources/read":
            self._mcp_dispatch_resources_read(message_id, params)
            return
        if method == "ping":
            self._respond_jsonrpc(_jsonrpc_result(message_id, {}))
            return

        self._respond_jsonrpc(
            _jsonrpc_error(message_id, JSON_RPC_METHOD_NOT_FOUND, f"Unknown method: {method!r}")
        )

    def _mcp_dispatch_initialize(self, message_id: Any, params: dict[str, Any]) -> None:
        # initialize is the one request that may not carry an Mcp-Session-Id;
        # OAuth bearer is still required per MCP Authorization §"Access Token Usage".
        auth = self._authorize()
        if auth is None:
            return
        protocol_version = self._negotiated_protocol_version()
        negotiated = params.get("protocolVersion") or protocol_version
        if negotiated not in SUPPORTED_PROTOCOL_VERSIONS:
            negotiated = STREAMABLE_HTTP_PROTOCOL_VERSION
        session_id = issue_session_id()
        sessions = getattr(self.server, "remote_mcp_sessions", None)
        if sessions is not None:
            sessions[session_id] = {
                "created_at": time.time(),
                "protocol_version": negotiated,
            }
        result = {
            "protocolVersion": negotiated,
            "serverInfo": MCP_SERVER_INFO,
            "capabilities": MCP_SERVER_CAPABILITIES,
            "instructions": MCP_DEFAULT_INSTRUCTIONS,
        }
        body = _jsonrpc_result(message_id, result)
        self._respond_jsonrpc_with_status(
            body,
            status=200,
            extra_headers={"Mcp-Session-Id": session_id},
        )

    def _mcp_dispatch_tools_list(self, message_id: Any, params: dict[str, Any]) -> None:
        if not self._enforce_session_id():
            return
        auth = self._authorize()
        if auth is None:
            return
        claims = auth[1]
        definitions = tool_definitions()
        filtered = [
            tool
            for tool in definitions
            if token_satisfies_tool_scopes(claims, str(tool.get("name") or ""))
        ]
        result = {"tools": filtered}
        if "cursor" in params:
            result["nextCursor"] = None
        self._respond_jsonrpc(_jsonrpc_result(message_id, result))

    def _mcp_dispatch_tools_call(self, message_id: Any, params: dict[str, Any]) -> None:
        if not self._enforce_session_id():
            return
        auth = self._authorize()
        if auth is None:
            return
        claims = auth[1]
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if not name:
            self._respond_jsonrpc(_jsonrpc_error(message_id, JSON_RPC_INVALID_PARAMS, "name is required."))
            return
        if not token_satisfies_tool_scopes(claims, name):
            self.respond_unauthorized(
                {"error": "insufficient_scope", "tool": name},
                status=403,
                error="insufficient_scope",
                scope="tools",
            )
            return
        try:
            if name == "submit_task":
                result = self.runtime.submit_task(
                    goal=arguments.get("goal") or "",
                    project_root=_server_managed_project_root("mcp-task"),
                    deliverables=arguments.get("deliverables") or ["README.md"],
                    agent=arguments.get("agent") or "demo",
                    subtasks=arguments.get("subtasks") or None,
                    strict_dependency=bool(arguments.get("strictDependency") or arguments.get("strict_dependency")),
                    task_types=arguments.get("taskTypes") or arguments.get("task_types") or None,
                    agent_adapters=arguments.get("agentAdapters") or arguments.get("agent_adapters") or None,
                ).to_dict()
            elif name == "submit_release_e2e_task":
                result = self.runtime.submit_release_e2e_task(
                    project_root=_server_managed_project_root("mcp-release-e2e"),
                    run_label=arguments.get("runLabel") or arguments.get("run_label"),
                    allowed_agents=arguments.get("allowedSubtaskAgents") or arguments.get("allowed_subtask_agents"),
                ).to_dict()
            elif name == "start_agent_loop":
                result = self.loop_runtime.start_loop(
                    goal=arguments.get("goal") or "",
                    project_root=_server_managed_project_root("mcp-agent-loop"),
                    agent=arguments.get("agent") or "owner",
                    max_turns=arguments.get("maxTurns") or arguments.get("max_turns") or 8,
                    memory_policy=arguments.get("memoryPolicy") or arguments.get("memory_policy"),
                    approval_policy=arguments.get("approvalPolicy") or arguments.get("approval_policy"),
                    metadata=arguments.get("metadata"),
                ).to_dict()
            else:
                result = handle_tool_call(self.runtime, name, arguments or {})
        except KeyError as exc:
            self._respond_jsonrpc(
                _jsonrpc_error(message_id, JSON_RPC_INVALID_PARAMS, f"Missing argument: {exc}")
            )
            return
        except ValueError as exc:
            self._respond_jsonrpc(
                _jsonrpc_error(message_id, JSON_RPC_INVALID_PARAMS, str(exc))
            )
            return
        wrapped = {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, sort_keys=True)}],
            "structuredContent": result if isinstance(result, (dict, list)) else None,
            "isError": False,
        }
        self._respond_jsonrpc(_jsonrpc_result(message_id, wrapped))

    def _mcp_dispatch_resources_list(self, message_id: Any, params: dict[str, Any]) -> None:
        if not self._enforce_session_id():
            return
        auth = self._authorize()
        if auth is None:
            return
        claims = auth[1]
        # resources/* requires mcp.resources. across.evidence.read is an
        # additional scope for evidence-bearing resources (see
        # :func:`scopes_for_resource`) but does not by itself grant generic
        # resources/list access.
        token_scopes = set((claims.get("scope") or "").split())
        if "mcp.resources" not in token_scopes:
            self.respond_unauthorized(
                {"error": "insufficient_scope", "resource": "*"},
                status=403,
                error="insufficient_scope",
                scope="resources",
            )
            return
        result = {"resources": resource_definitions()}
        if "cursor" in params:
            result["nextCursor"] = None
        self._respond_jsonrpc(_jsonrpc_result(message_id, result))

    def _mcp_dispatch_resources_read(self, message_id: Any, params: dict[str, Any]) -> None:
        if not self._enforce_session_id():
            return
        auth = self._authorize()
        if auth is None:
            return
        claims = auth[1]
        uri = str(params.get("uri") or "")
        if not uri:
            self._respond_jsonrpc(
                _jsonrpc_error(message_id, JSON_RPC_INVALID_PARAMS, "uri is required.")
            )
            return
        if not token_satisfies_resource_scopes(claims, uri):
            self.respond_unauthorized(
                {"error": "insufficient_scope", "resource": uri},
                status=403,
                error="insufficient_scope",
                scope="evidence",
            )
            return
        try:
            contents = read_resource(uri)
        except ValueError as exc:
            self._respond_jsonrpc(
                _jsonrpc_error(message_id, JSON_RPC_INTERNAL_ERROR, str(exc))
            )
            return
        self._respond_jsonrpc(_jsonrpc_result(message_id, contents))

    def _enforce_session_id(self) -> bool:
        session_id = self.headers.get("Mcp-Session-Id") or ""
        if not session_id or not is_safe_session_id(session_id):
            self._respond_jsonrpc_with_status(
                _jsonrpc_error(
                    None,
                    JSON_RPC_INVALID_REQUEST,
                    "Missing or invalid Mcp-Session-Id header.",
                ),
                status=400,
            )
            return False
        sessions = getattr(self.server, "remote_mcp_sessions", None)
        if sessions is None:
            return True
        if session_id not in sessions:
            self._respond_jsonrpc_with_status(
                _jsonrpc_error(
                    None,
                    JSON_RPC_INVALID_REQUEST,
                    "Mcp-Session-Id is not recognized; send a fresh initialize request.",
                ),
                status=404,
            )
            return False
        return True

    def _read_request_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or "0")
        if not length:
            return b""
        return self.rfile.read(length)

    def _respond_jsonrpc(self, payload: dict[str, Any]) -> None:
        self._respond_jsonrpc_with_status(payload, status=200)

    def _respond_jsonrpc_with_status(
        self,
        payload: dict[str, Any],
        *,
        status: int,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, _http_header_value(value))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/health":
                self.respond(render_plugin_health())
                return
            if path == "/.well-known/agent-card.json":
                self.respond(render_agent_card())
                return
            if path == "/.well-known/across-plugin.json":
                self.respond(render_plugin_manifest())
                return
            if path == "/mcp":
                if not self._origin_allows():
                    return
                self.handle_mcp_get_sse()
                return
            if path == "/.well-known/oauth-protected-resource":
                if not self._origin_allows():
                    return
                config = _oauth_config(self)
                base_url = _public_base_url(self, config)
                scopes = config.get("scopes") or ["mcp.tools", "mcp.resources", "across.evidence.read"]
                document = render_protected_resource_metadata(
                    base_url=base_url,
                    issuer=str(config.get("issuer") or base_url),
                    audience=str(config.get("audience") or f"{base_url}/mcp"),
                    scopes=list(scopes),
                    jwks_uri=str(config["jwks_uri"]) if config.get("jwks_uri") else None,
                )
                self.respond(document)
                return
            if path == "/.well-known/oauth-authorization-server":
                if not self._origin_allows():
                    return
                config = _oauth_config(self)
                base_url = _public_base_url(self, config)
                scopes = config.get("scopes") or ["mcp.tools", "mcp.resources", "across.evidence.read"]
                document = render_authorization_server_metadata(
                    issuer=str(config.get("issuer") or base_url),
                    scopes=list(scopes),
                    jwks_uri=str(config["jwks_uri"]) if config.get("jwks_uri") else None,
                )
                self.respond(document)
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) == 2 and parts[0] == "tasks":
                self.respond(self.runtime.get_task(parts[1]).to_dict())
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "events":
                self.respond(self.runtime.list_events(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "agui":
                self.respond(
                    project_events_to_agui(
                        {
                            "source": "task",
                            "task_id": parts[1],
                            "events": self.runtime.list_events(parts[1]),
                        }
                    )
                )
                return
            if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "events" and parts[3] == "stream":
                self.respond_sse(self.runtime.list_events(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "evidence-bundle":
                self.respond(self.runtime.evidence_bundle(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "quality-benchmark":
                self.respond(self.runtime.quality_benchmark(parts[1]))
                return
            if len(parts) == 2 and parts[0] == "loops":
                self.respond(self.loop_runtime.get_loop(parts[1]).to_dict())
                return
            if len(parts) == 3 and parts[0] == "loops" and parts[2] == "health":
                self.respond(self.loop_runtime.get_loop_health(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "loops" and parts[2] == "evidence-summary":
                self.respond(self.loop_runtime.get_loop_evidence_summary(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "loops" and parts[2] == "telemetry":
                self.respond(self.loop_runtime.get_loop_telemetry(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "loops" and parts[2] == "events":
                self.respond(self.loop_runtime.list_loop_events(parts[1], after_sequence=_query_int(query, "after_sequence")))
                return
            if len(parts) == 3 and parts[0] == "loops" and parts[2] == "agui":
                self.respond(
                    project_events_to_agui(
                        {
                            "source": "loop",
                            "loop_id": parts[1],
                            "events": self.loop_runtime.list_loop_events(parts[1], after_sequence=_query_int(query, "after_sequence")),
                        }
                    )
                )
                return
            if len(parts) == 4 and parts[0] == "loops" and parts[2] == "agui" and parts[3] == "stream":
                self.respond_agui_loop_sse(parts[1], after_sequence=_query_int(query, "after_sequence"))
                return
            if len(parts) == 4 and parts[0] == "loops" and parts[2] == "events" and parts[3] == "stream":
                after_sequence = _query_int(query, "after_sequence")
                if _query_truthy(query.get("follow", [""])[0]):
                    self.respond_loop_sse(parts[1], after_sequence=after_sequence)
                    return
                self.respond_sse(self.loop_runtime.list_loop_events(parts[1], after_sequence=after_sequence))
                return
            self.respond({"error": "not_found"}, status=404)
        except KeyError:
            self.respond({"error": "not_found"}, status=404)
        except AgentLoopConcurrencyError as exc:
            self.respond(
                {
                    "error": "max_concurrent_loops_exceeded",
                    "active_count": exc.active_count,
                    "max_concurrent_loops": exc.max_concurrent_loops,
                },
                status=409,
            )
        except ValueError as exc:
            self.respond({"error": "bad_request", "detail": str(exc)}, status=400)
        except Exception:
            self.respond({"error": "internal_error"}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/mcp":
                self.handle_mcp_post()
                return
            payload = self.read_json()
            if path == "/tasks":
                task = self.runtime.submit_task(
                    goal=payload.get("goal") or payload.get("text") or "",
                    project_root=_server_managed_project_root("http-task"),
                    deliverables=payload.get("deliverables") or ["README.md"],
                    agent=payload.get("agent") or "demo",
                    subtasks=payload.get("subtasks") or None,
                    strict_dependency=bool(payload.get("strictDependency") or payload.get("strict_dependency")),
                    task_types=payload.get("taskTypes") or payload.get("task_types") or None,
                    agent_adapters=payload.get("agentAdapters") or payload.get("agent_adapters") or None,
                )
                self.respond(task.to_dict(), status=201)
                return
            if path == "/release-e2e":
                task = self.runtime.submit_release_e2e_task(
                    project_root=_server_managed_project_root("http-release-e2e"),
                    run_label=payload.get("runLabel") or payload.get("run_label"),
                    allowed_agents=payload.get("allowedSubtaskAgents")
                    or payload.get("allowed_subtask_agents")
                    or payload.get("agents"),
                )
                self.respond(task.to_dict(), status=201)
                return
            if path == "/host-conformance":
                report = evaluate_host_conformance(payload)
                self.respond(report, status=200 if report["passed"] else 422)
                return
            if path == "/loops":
                loop = self.loop_runtime.start_loop(
                    goal=payload.get("goal") or "",
                    project_root=_server_managed_project_root("http-loop"),
                    agent=payload.get("agent") or "owner",
                    max_turns=payload.get("maxTurns") or payload.get("max_turns") or 8,
                    memory_policy=payload.get("memoryPolicy") or payload.get("memory_policy"),
                    approval_policy=payload.get("approvalPolicy") or payload.get("approval_policy"),
                    metadata=payload.get("metadata"),
                )
                self.respond(loop.to_dict(), status=201)
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "run":
                task = self.runtime.run_task(parts[1])
                self.respond(task.to_dict())
                return
            if len(parts) == 3 and parts[0] == "loops" and parts[2] == "run":
                loop = self.loop_runtime.run_loop(parts[1])
                self.respond(loop.to_dict())
                return
            if len(parts) == 3 and parts[0] == "loops" and parts[2] == "cancel":
                loop = self.loop_runtime.cancel_loop(
                    parts[1],
                    reason=payload.get("reason"),
                    cancel_category=payload.get("cancelCategory") or payload.get("cancel_category"),
                )
                self.respond(loop.to_dict())
                return
            if len(parts) == 5 and parts[0] == "loops" and parts[2] == "actions" and parts[4] == "approve":
                loop = self.loop_runtime.approve_action(parts[1], parts[3])
                self.respond(loop.to_dict())
                return
            if len(parts) == 5 and parts[0] == "loops" and parts[2] == "actions" and parts[4] == "reject":
                loop = self.loop_runtime.reject_action(parts[1], parts[3], reason=payload.get("reason"))
                self.respond(loop.to_dict())
                return
            if len(parts) == 5 and parts[0] == "loops" and parts[2] == "steps" and parts[4] == "retry":
                loop = self.loop_runtime.retry_step(parts[1], parts[3])
                self.respond(loop.to_dict())
                return
            self.respond({"error": "not_found"}, status=404)
        except KeyError:
            self.respond({"error": "not_found"}, status=404)
        except AgentLoopConcurrencyError as exc:
            self.respond(
                {
                    "error": "max_concurrent_loops_exceeded",
                    "active_count": exc.active_count,
                    "max_concurrent_loops": exc.max_concurrent_loops,
                },
                status=409,
            )
        except ValueError as exc:
            self.respond({"error": "bad_request", "detail": str(exc)}, status=400)
        except Exception:
            self.respond({"error": "internal_error"}, status=500)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    # ----- Legacy REST handlers were removed in v0.7.8 ----------------------
    # The MCP 2025-06-18 Streamable HTTP transport requires a single MCP
    # endpoint that exchanges JSON-RPC messages. The legacy REST sub-paths
    # ``/mcp/v1/initialize``, ``/mcp/v1/tools/list``, ``/mcp/v1/tools/call``,
    # ``/mcp/v1/resources/list``, and ``/mcp/v1/resources/read`` were replaced
    # by :meth:`handle_mcp_post` which dispatches based on the JSON-RPC
    # ``method`` field. Sessions, OAuth bearer tokens, Origin headers, and the
    # MCP ``Mcp-Session-Id`` lifecycle are all enforced at the dispatcher.

    # ----- do_DELETE: session termination ----------------------------------

    def respond_unauthorized(
        self,
        payload: dict[str, Any],
        *,
        status: int = 401,
        error: str = "invalid_token",
        scope: str | None = None,
    ) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        challenge_error = error if error in OAUTH_CHALLENGE_ERRORS else "invalid_token"
        challenge_scope = OAUTH_CHALLENGE_SCOPES.get(scope or "")
        challenge = render_www_authenticate(
            base_url=_public_base_url(self, _oauth_config(self)),
            error=challenge_error,
            error_description=OAUTH_CHALLENGE_DESCRIPTIONS[challenge_error],
            scope=challenge_scope,
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("WWW-Authenticate", challenge)
        self.end_headers()
        self.wfile.write(body)

    def respond(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_sse(self, events: list[dict[str, Any]]) -> None:
        chunks = []
        for event in events:
            chunks.append(f"event: {_http_header_value(event.get('type', 'message'))}\n")
            chunks.append(f"data: {json.dumps(event, sort_keys=True)}\n\n")
        body = "".join(chunks).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_loop_sse(self, loop_id: str, *, after_sequence: int | None = None) -> None:
        self.loop_runtime.get_loop(loop_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        sent_keys: set[str] = set()
        idle_deadline = time.time() + 30
        while True:
            events = self.loop_runtime.list_loop_events(loop_id, after_sequence=after_sequence)
            new_events = [event for event in events if _event_key(event) not in sent_keys]
            if new_events:
                idle_deadline = time.time() + 30

            closing_seen = False
            for event in new_events:
                sent_keys.add(_event_key(event))
                if not self.write_sse_event(event):
                    return
                if event.get("type") in LOOP_STREAM_CLOSING_EVENT_TYPES:
                    closing_seen = True

            if closing_seen:
                return
            if time.time() >= idle_deadline:
                self.write_sse_comment("idle_timeout")
                return
            time.sleep(0.1)

    def respond_agui_loop_sse(self, loop_id: str, *, after_sequence: int | None = None) -> None:
        self.loop_runtime.get_loop(loop_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        sent_keys: set[str] = set()
        idle_deadline = time.time() + 30
        while True:
            events = self.loop_runtime.list_loop_events(loop_id, after_sequence=after_sequence)
            new_events = [event for event in events if _event_key(event) not in sent_keys]
            if new_events:
                idle_deadline = time.time() + 30

            closing_seen = False
            for event in new_events:
                sent_keys.add(_event_key(event))
                if not self.write_sse_chunk(project_event_sse(event, task_id=loop_id, source="loop")):
                    return
                if event.get("type") in LOOP_STREAM_CLOSING_EVENT_TYPES:
                    closing_seen = True

            if closing_seen:
                return
            if time.time() >= idle_deadline:
                self.write_sse_comment("idle_timeout")
                return
            time.sleep(0.1)

    def write_sse_event(self, event: dict[str, Any]) -> bool:
        event_type = str(event.get("type") or "message")
        chunk = f"event: {event_type}\ndata: {json.dumps(event, sort_keys=True)}\n\n".encode("utf-8")
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def write_sse_comment(self, comment: str) -> bool:
        try:
            self.wfile.write(f": {comment}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def write_sse_chunk(self, chunk: str) -> bool:
        try:
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False


class OrchestratorHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int]):
        super().__init__(server_address, OrchestratorHandler)
        self.runtime = OrchestratorRuntime()
        self.loop_runtime = self.runtime.loop_runtime
        self.remote_mcp_oauth: dict[str, Any] = {}
        self.remote_mcp_sessions: dict[str, dict[str, Any]] = {}


def _query_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _query_int(query: dict[str, list[str]], key: str) -> int | None:
    raw_values = query.get(key) or []
    if not raw_values or raw_values[0] == "":
        return None
    return int(raw_values[0])


def _event_key(event: dict[str, Any]) -> str:
    return str(event.get("event_id") or event.get("sequence") or json.dumps(event, sort_keys=True))


def _runtime_info_path(runtime_id: str, runtime_info: str | None = None) -> Path:
    if runtime_info and runtime_info.strip():
        if not (
            is_product_mode()
            and not is_developer_mode()
            and contains_protected_user_reference(runtime_info)
        ):
            return Path(runtime_info).expanduser().resolve()
    return run_home() / f"{runtime_id}.json"


def _write_runtime_info(server: OrchestratorHTTPServer, host: str, runtime_id: str, runtime_info: str | None) -> Path:
    actual_host, actual_port = server.server_address[:2]
    endpoint_host = host or actual_host
    path = _runtime_info_path(runtime_id, runtime_info)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "componentId": COMPONENT_ID,
        "runtimeId": runtime_id,
        "pid": os.getpid(),
        "host": endpoint_host,
        "port": actual_port,
        "endpoint": f"http://{endpoint_host}:{actual_port}",
        "transport": "http",
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    runtime_id: str | None = None,
    runtime_info: str | None = None,
    remote_mcp_oauth_config: dict[str, Any] | None = None,
) -> None:
    server = OrchestratorHTTPServer((host, port))
    if remote_mcp_oauth_config:
        apply_remote_mcp_oauth_config(server, remote_mcp_oauth_config)
    info_path = _write_runtime_info(server, host, runtime_id, runtime_info) if runtime_id else None
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if info_path:
            try:
                info_path.unlink()
            except FileNotFoundError:
                pass
