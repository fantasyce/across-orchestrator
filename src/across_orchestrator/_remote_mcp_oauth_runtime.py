"""OAuth Resource Server helpers for the remote MCP Streamable HTTP endpoint.

This module is **read-only side-effect-free** logic. It produces Protected
Resource Metadata (RFC 9728), constructs WWW-Authenticate challenges (RFC 6750
+ RFC 9728 §5.1), maps OAuth scopes to MCP tools, and validates JWT bearer
tokens against an in-memory JWKS cache. Signing keys, client secrets, and
issuers stay with the host; this module never persists them.

The PyJWT dependency is optional. When PyJWT (with cryptography) is not
installed, :func:`verify_bearer_token` returns a ``jwt_unavailable`` error
instead of raising so HTTP handlers can respond with a clean 401. PyJWT is
exposed as a Python extra ``[remote-mcp]`` so the package's stdlib-only
``dependencies = []`` contract is preserved.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any
from urllib import error as url_error
from urllib import request as url_request


OAUTH_PROTECTED_RESOURCE_SCHEMA = "across-orchestrator-oauth-protected-resource/1.0"

OAUTH_AUTHORIZATION_SERVER_SCHEMA = "across-orchestrator-oauth-authorization-server/1.0"

DEFAULT_REQUIRED_SCOPES = ("mcp.tools", "mcp.resources", "across.evidence.read")

DEFAULT_BEARER_REALM = "across-orchestrator"

# Each MCP tool requires at least one of these scopes; default mapping is
# conservative — read-only tools get ``mcp.resources``, write tools get
# ``mcp.tools``, evidence-bundle tools get ``across.evidence.read``.
_TOOL_SCOPE_HINTS: dict[str, tuple[str, ...]] = {
    "submit_task": ("mcp.tools",),
    "run_task": ("mcp.tools",),
    "submit_release_e2e_task": ("mcp.tools",),
    "get_task": ("mcp.resources",),
    "get_evidence_bundle": ("across.evidence.read", "mcp.resources"),
    "get_agent_card": ("mcp.resources",),
    "evaluate_sandbox_policy": ("mcp.tools",),
    "build_evidence_graph": ("mcp.tools",),
    "evaluate_agent_team_readiness": ("mcp.tools",),
    "render_remote_mcp_oauth_template": ("mcp.resources",),
    "create_a2a_task_delegation": ("mcp.tools",),
    "project_agui_events": ("mcp.resources",),
    "create_agent_team": ("mcp.tools",),
    "export_otel_genai_spans": ("mcp.tools",),
    "start_agent_loop": ("mcp.tools",),
    "run_agent_loop": ("mcp.tools",),
    "approve_agent_loop_action": ("mcp.tools",),
    "reject_agent_loop_action": ("mcp.tools",),
    "cancel_agent_loop": ("mcp.tools",),
    "retry_agent_loop_step": ("mcp.tools",),
    "get_agent_loop": ("mcp.resources",),
    "get_agent_loop_health": ("mcp.resources",),
    "get_agent_loop_events": ("mcp.resources",),
    "get_agent_loop_evidence_summary": ("across.evidence.read", "mcp.resources"),
    "get_agent_loop_telemetry": ("mcp.resources",),
    "validate_external_agent_plugin": ("mcp.tools",),
    "register_external_agent_plugin": ("mcp.tools",),
    "list_external_agent_plugins": ("mcp.resources",),
    "get_external_agent_plugin_health": ("mcp.resources",),
}


def scopes_for_tool(tool_name: str) -> tuple[str, ...]:
    """Return the OAuth scopes required to invoke ``tool_name``.

    Falls back to ``mcp.tools`` for unknown tool names so future tools stay
    locked behind the standard write scope by default.
    """

    return _TOOL_SCOPE_HINTS.get(tool_name, ("mcp.tools",))


# MCP resources are read-only and always require the ``mcp.resources`` scope.
# Evidence bundles additionally require ``across.evidence.read`` because they
# leak loop/task history to the caller.
_RESOURCE_SCOPE_HINTS: dict[str, tuple[str, ...]] = {
    "across-orchestrator://agent-card": ("mcp.resources",),
    "across-orchestrator://plugin-manifest": ("mcp.resources",),
    "across-orchestrator://plugin-status": ("mcp.resources",),
    "across-orchestrator://agent-loop-schema": ("mcp.resources",),
    "across-orchestrator://sandbox-policy": ("mcp.resources",),
    "across-orchestrator://external-agent-plugins": ("mcp.resources",),
    "across-orchestrator://projection-contracts": ("mcp.resources",),
}


def scopes_for_resource(uri: str) -> tuple[str, ...]:
    """Return the OAuth scopes required to read ``uri``.

    Falls back to ``mcp.resources`` for unknown URIs so future resources stay
    locked behind the standard read scope by default.
    """

    if "evidence" in uri.lower():
        return ("across.evidence.read", "mcp.resources")
    return _RESOURCE_SCOPE_HINTS.get(uri, ("mcp.resources",))


def token_satisfies_resource_scopes(claims: dict[str, Any], uri: str) -> bool:
    """Return True iff the token's ``scope`` claim satisfies reading ``uri``."""

    token_scopes = _extract_scope_set(claims.get("scope"))
    if not token_scopes:
        return False
    return any(scope in token_scopes for scope in scopes_for_resource(uri))


def required_scopes(config: dict[str, Any] | None = None) -> list[str]:
    """Resolve the host-configured required scopes (default: standard set)."""

    if not config:
        return list(DEFAULT_REQUIRED_SCOPES)
    scopes = config.get("scopes") or config.get("required_scopes")
    if isinstance(scopes, str):
        scopes = [scopes]
    if not isinstance(scopes, list) or not scopes:
        return list(DEFAULT_REQUIRED_SCOPES)
    return [str(item).strip() for item in scopes if str(item).strip()]


def render_protected_resource_metadata(
    *,
    base_url: str,
    issuer: str,
    audience: str,
    scopes: list[str] | tuple[str, ...] | None = None,
    jwks_uri: str | None = None,
) -> dict[str, Any]:
    """Render an RFC 9728 OAuth 2.0 Protected Resource Metadata document.

    The ``resource`` field is the audience identifier that tokens MUST bind to
    via the ``aud`` claim (RFC 8707 Resource Indicators).
    """

    resolved_scopes = list(scopes) if scopes else list(DEFAULT_REQUIRED_SCOPES)
    document: dict[str, Any] = {
        "schema_version": OAUTH_PROTECTED_RESOURCE_SCHEMA,
        "resource": audience,
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "resource_signing_alg_values_supported": ["RS256", "ES256", "HS256"],
        "resource_documentation": "https://github.com/fantasyce/across-orchestrator",
        "scopes_supported": resolved_scopes,
        "tls_client_certificate_bound_access_tokens": False,
        "resource_policy_uri": "https://github.com/fantasyce/across-orchestrator",
    }
    if jwks_uri:
        document["jwks_uri"] = jwks_uri
    return document


def render_authorization_server_metadata(
    *,
    issuer: str,
    scopes: list[str] | tuple[str, ...] | None = None,
    jwks_uri: str | None = None,
) -> dict[str, Any]:
    """Render an RFC 8414 OAuth 2.0 Authorization Server Metadata document.

    Across Orchestrator is **not** an authorization server; it proxies the host-
    configured external issuer. The metadata therefore reflects the external AS
    contract rather than declaring any local endpoint. Hosts that need full
    AS metadata should fetch the document directly from ``issuer``.
    """

    resolved_scopes = list(scopes) if scopes else list(DEFAULT_REQUIRED_SCOPES)
    document: dict[str, Any] = {
        "schema_version": OAUTH_AUTHORIZATION_SERVER_SCHEMA,
        "issuer": issuer,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
        "scopes_supported": resolved_scopes,
        "tls_client_certificate_bound_access_tokens": False,
        "remote_as": True,
    }
    if jwks_uri:
        document["jwks_uri"] = jwks_uri
    return document


def render_www_authenticate(
    *,
    base_url: str,
    realm: str = DEFAULT_BEARER_REALM,
    error: str | None = None,
    error_description: str | None = None,
    scope: str | None = None,
) -> str:
    """Build a WWW-Authenticate Bearer challenge per RFC 6750 §3 + RFC 9728 §5.1.

    ``resource_metadata`` is an absolute URL pointing at the protected-resource
    metadata document so clients can discover scopes and the resource value.
    """

    parts = [f'Bearer realm="{realm}"']
    metadata_url = base_url.rstrip("/") + "/.well-known/oauth-protected-resource"
    parts.append(f'resource_metadata="{metadata_url}"')
    if error:
        parts.append(f'error="{error}"')
    if error_description:
        parts.append(f'error_description="{error_description}"')
    if scope:
        parts.append(f'scope="{scope}"')
    return ", ".join(parts)


def parse_bearer_token(authorization_header: str | None) -> str | None:
    """Extract a bearer token from the ``Authorization`` header.

    Returns ``None`` if the header is missing, malformed, or uses a scheme
    other than ``Bearer``. Case-insensitive scheme match per RFC 6750 §2.1.
    """

    if not authorization_header:
        return None
    header = authorization_header.strip()
    if not header:
        return None
    scheme, _, remainder = header.partition(" ")
    if scheme.lower() != "bearer" or not remainder:
        return None
    return remainder.strip() or None


def token_claims_present(claims: dict[str, Any]) -> bool:
    """Light sanity check used before delegating to PyJWT."""

    return isinstance(claims, dict) and bool(claims)


class _InMemoryJWKSCache:
    """Minimal JWKS cache keyed by URL with TTL-based expiry."""

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, url: str) -> dict[str, Any] | None:
        entry = self._store.get(url)
        if not entry:
            return None
        expires_at, value = entry
        if expires_at < time.time():
            self._store.pop(url, None)
            return None
        return value

    def set(self, url: str, value: dict[str, Any]) -> None:
        self._store[url] = (time.time() + self._ttl, value)


_JWKS_CACHE = _InMemoryJWKSCache()


def clear_jwks_cache() -> None:
    """Reset the in-memory JWKS cache. Useful for tests and forced reload."""

    _JWKS_CACHE._store.clear()


def fetch_jwks(url: str, *, force: bool = False) -> dict[str, Any] | None:
    """Fetch a JWKS document from ``url`` with TTL caching.

    Returns ``None`` on network error or invalid JSON so the caller can fall
    back to a host-injected JWKS file.
    """

    if not force:
        cached = _JWKS_CACHE.get(url)
        if cached:
            return cached
    try:
        with url_request.urlopen(url, timeout=5) as response:
            raw = response.read().decode("utf-8")
        document = json.loads(raw)
    except (url_error.URLError, url_error.HTTPError, ValueError, OSError):
        return None
    if not isinstance(document, dict):
        return None
    _JWKS_CACHE.set(url, document)
    return document


def _pyjwt():
    """Lazy import of PyJWT so the package stays stdlib-only by default."""

    try:
        import jwt  # type: ignore[import-not-found]
    except ImportError:
        return None
    return jwt


def _b64url_decode(value: str) -> bytes:
    """Decode a base64url string without external padding helpers."""

    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _hs256_sign(payload_bytes: bytes, secret: str) -> str:
    """Minimal HS256 signature implementation (no PyJWT required)."""

    digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def sign_hs256_token(
    *,
    claims: dict[str, Any],
    secret: str,
    headers: dict[str, Any] | None = None,
) -> str:
    """Sign a JWT with HS256. Pure stdlib implementation for tests.

    Production callers should configure their own authorization server; this
    helper exists so the in-process tests can mint tokens without depending on
    PyJWT.
    """

    header = {"alg": "HS256", "typ": "JWT"}
    if headers:
        header.update(headers)
    header_segment = base64.urlsafe_b64encode(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    payload_segment = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature_segment = _hs256_sign(signing_input, secret)
    return f"{header_segment}.{payload_segment}.{signature_segment}"


def _verify_hs256(token: str, secret: str, *, audience: str, issuer: str) -> dict[str, Any] | str:
    """Pure-stdlib HS256 verification; returns claims dict or error code."""

    parts = token.split(".")
    if len(parts) != 3:
        return "malformed_token"
    header_segment, payload_segment, signature_segment = parts
    try:
        header = json.loads(_b64url_decode(header_segment))
        claims = json.loads(_b64url_decode(payload_segment))
    except (ValueError, KeyError):
        return "malformed_token"
    if header.get("alg") != "HS256":
        return "unsupported_algorithm"
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    expected = _b64url_decode(signature_segment + "=" * (-len(signature_segment) % 4))
    computed = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, computed):
        return "invalid_signature"
    return claims


def _jwk_keys_for_token(jwt_module: Any, token: str, jwks: dict[str, Any]) -> tuple[list[Any], str | None]:
    """Resolve PyJWT signing keys from a JWKS document for ``token``."""

    try:
        header = jwt_module.get_unverified_header(token)
    except Exception:
        return [], "malformed_token"

    raw_keys = jwks.get("keys")
    if isinstance(raw_keys, list):
        key_documents = [item for item in raw_keys if isinstance(item, dict)]
    elif jwks.get("kty"):
        key_documents = [jwks]
    else:
        return [], "jwks_invalid"

    kid = header.get("kid")
    alg = header.get("alg")
    candidates = [
        key
        for key in key_documents
        if (not kid or str(key.get("kid")) == str(kid))
        and (not alg or not key.get("alg") or str(key.get("alg")) == str(alg))
    ]
    if not candidates and kid:
        return [], "jwk_kid_not_found"
    if not candidates:
        candidates = key_documents

    resolved: list[Any] = []
    for key_document in candidates:
        try:
            resolved.append(jwt_module.PyJWK.from_dict(key_document).key)
        except Exception:
            continue
    if not resolved:
        return [], "jwk_unsupported"
    return resolved, None


def verify_bearer_token(
    token: str,
    *,
    audience: str,
    issuer: str,
    hs256_secret: str | None = None,
    jwks_url: str | None = None,
    required_scopes_list: list[str] | tuple[str, ...] | None = None,
    leeway_seconds: int = 30,
    required_claims: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Validate a bearer token and return its claims, or an error envelope.

    Returns ``{"error": "<code>", "error_description": "..."}`` on failure so
    HTTP handlers can respond with a structured body. Supports HS256 via pure
    stdlib (always available) and RS256/ES256 via PyJWT when the ``[remote-mcp]``
    extra is installed.

    Enforced checks:

    - All claims in ``required_claims`` (default: ``("iss", "iat", "exp", "aud")``)
      MUST be present. Missing claim → error code ``missing_<claim>``.
    - ``iss`` MUST equal ``issuer`` (RFC 8414 §2).
    - ``aud`` MUST equal ``audience`` (RFC 8707 Resource Indicators).
    - ``exp`` MUST be in the future (with ``leeway_seconds`` slack).
    - ``nbf``, if present, MUST be in the past.
    - ``scope``, if present, MUST contain at least one of ``required_scopes_list``
      (default: standard set). Callers needing per-tool scope enforcement should
      call :func:`token_satisfies_tool_scopes`.
    """

    default_required_claims = ("iss", "iat", "exp", "aud")
    claim_set = tuple(required_claims) if required_claims else default_required_claims

    if not token:
        return {"error": "missing_token", "error_description": "Bearer token is empty."}
    if hs256_secret:
        result = _verify_hs256(token, hs256_secret, audience=audience, issuer=issuer)
        if isinstance(result, str):
            return {"error": result, "error_description": f"HS256 verification failed: {result}."}
        claims = result
    else:
        jwt = _pyjwt()
        if jwt is None:
            return {
                "error": "jwt_unavailable",
                "error_description": (
                    "PyJWT is not installed; install across-orchestrator[remote-mcp] "
                    "to enable RS256/ES256 verification."
                ),
            }
        if not jwks_url:
            return {
                "error": "jwks_unconfigured",
                "error_description": (
                    "Either HS256 secret or JWKS URL must be configured for "
                    "asymmetric signature verification."
                ),
            }
        jwks = fetch_jwks(jwks_url) if jwks_url.startswith(("http://", "https://")) else None
        if jwks is None:
            try:
                with open(jwks_url, encoding="utf-8") as handle:
                    jwks = json.load(handle)
            except (OSError, ValueError):
                return {
                    "error": "jwks_unreachable",
                    "error_description": f"Could not load JWKS from {jwks_url}.",
                }
        signing_keys, key_error = _jwk_keys_for_token(jwt, token, jwks)
        if key_error:
            return {
                "error": key_error,
                "error_description": f"Could not resolve a signing key from JWKS: {key_error}.",
            }
        last_error: Exception | None = None
        for signing_key in signing_keys:
            try:
                claims = jwt.decode(
                    token,
                    key=signing_key,
                    algorithms=["RS256", "ES256"],
                    audience=audience,
                    issuer=issuer,
                    leeway=leeway_seconds,
                    options={"require": list(claim_set)},
                )
                break
            except Exception as exc:  # PyJWT raises many specific subclasses
                last_error = exc
        else:
            return {
                "error": "invalid_token",
                "error_description": str(last_error or "Token did not validate against JWKS."),
            }

    for claim in claim_set:
        if claim not in claims or claims.get(claim) in (None, ""):
            return {
                "error": f"missing_{claim}",
                "error_description": f"Token is missing required {claim!r} claim.",
            }

    now = int(time.time())
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return {"error": "missing_exp", "error_description": "Token missing exp claim."}
    if int(exp) + leeway_seconds < now:
        return {"error": "token_expired", "error_description": "Token exp claim is in the past."}
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and int(nbf) - leeway_seconds > now:
        return {"error": "token_not_yet_valid", "error_description": "Token nbf claim is in the future."}
    iss = claims.get("iss")
    if iss is None:
        return {
            "error": "missing_iss",
            "error_description": "Token is missing the iss claim required for issuer binding.",
        }
    if str(iss) != str(issuer):
        return {
            "error": "issuer_mismatch",
            "error_description": f"Token iss={iss!r} does not match issuer={issuer!r}.",
        }
    aud = claims.get("aud")
    if isinstance(aud, list):
        if audience not in aud and str(audience) not in {str(item) for item in aud}:
            return {
                "error": "audience_mismatch",
                "error_description": f"Token aud={aud!r} does not include resource={audience!r}.",
            }
    elif aud is None:
        return {
            "error": "missing_aud",
            "error_description": "Token aud claim is required for RFC 8707 resource binding.",
        }
    elif str(aud) != str(audience):
        return {
            "error": "audience_mismatch",
            "error_description": f"Token aud={aud!r} does not match resource={audience!r}.",
        }

    if required_scopes_list:
        token_scopes = _extract_scope_set(claims.get("scope"))
        allowed = {str(item) for item in required_scopes_list}
        if not (token_scopes & allowed):
            return {
                "error": "insufficient_scope",
                "error_description": (
                    f"Token scope={sorted(token_scopes)!r} does not include any of "
                    f"{sorted(allowed)!r}."
                ),
            }

    return {"claims": claims}


def token_satisfies_tool_scopes(claims: dict[str, Any], tool_name: str) -> bool:
    """Return True iff the token's ``scope`` claim includes the tool's required scope."""

    token_scopes = _extract_scope_set(claims.get("scope"))
    if not token_scopes:
        return False
    return any(scope in token_scopes for scope in scopes_for_tool(tool_name))


def _extract_scope_set(raw: Any) -> set[str]:
    """Parse the OAuth ``scope`` claim which may be a space-separated string or list."""

    if raw is None:
        return set()
    if isinstance(raw, str):
        return {part for part in raw.split() if part}
    if isinstance(raw, list):
        return {str(item) for item in raw if str(item)}
    return set()


def issue_session_id() -> str:
    """Generate a fresh MCP session identifier."""

    import uuid

    return str(uuid.uuid4())


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._\-]+$")


def is_safe_session_id(value: str) -> bool:
    return bool(value) and bool(_SAFE_TOKEN.match(value))


def normalize_origin(value: str | None) -> str:
    """Return the lowercase ``scheme://host[:port]`` form of an Origin header.

    Empty or malformed values normalize to an empty string so callers can use a
    single ``if not normalized`` check. If the value lacks a scheme, ``http://``
    is added so bare host strings (``localhost:8765``) can be compared against
    full origins (``http://localhost:8765``).
    """

    if not value:
        return ""
    candidate = value.strip().lower()
    if not candidate:
        return ""
    if "://" not in candidate:
        candidate = "http://" + candidate
    return candidate


def is_origin_allowed(
    origin_header: str | None,
    *,
    allowed_origins: tuple[str, ...] | list[str] | None = None,
    host: str | None = None,
    allow_loopback_when_no_origin: bool = True,
) -> tuple[bool, str]:
    """Validate the ``Origin`` header against an allowlist per MCP 2025-06-18 §Security.

    MCP spec §"Streamable HTTP" mandates Origin validation:

    - When ``Origin`` is present, it MUST appear in ``allowed_origins``.
    - When ``Origin`` is absent (typical CLI clients without browsers), the
      request is accepted unless the host has not opted into loopback trust.

    Returns ``(allowed, reason)``. ``reason`` is a short token describing the
    decision and is safe to log.
    """

    normalized = normalize_origin(origin_header)
    allowed_list = [normalize_origin(item) for item in (allowed_origins or []) if item]
    if not normalized:
        return (allow_loopback_when_no_origin, "origin_absent")
    if normalized in allowed_list:
        return (True, "origin_allowed")
    return (False, "origin_not_allowlisted")
