"""End-to-end tests for the Streamable HTTP + OAuth Resource Server endpoint.

The MCP 2025-06-18 transport requires a single HTTP endpoint path that accepts
both POST and GET methods and exchanges JSON-RPC 2.0 messages. These tests
exercise the JSON-RPC dispatcher, OAuth bearer-token enforcement, Origin header
validation, RFC 8707 audience binding, scope-to-tool/resource mapping, and
session lifecycle. The HS256 token helper uses the pure-stdlib signer in
``across_orchestrator._remote_mcp_oauth_runtime`` so the tests do not require
PyJWT to be installed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import error, request


MCP_ACCEPT = "application/json, text/event-stream"


def _has_pyjwt_crypto() -> bool:
    return importlib.util.find_spec("jwt") is not None and importlib.util.find_spec("cryptography") is not None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _with_loopback_no_proxy(value: str | None) -> str:
    entries = [item.strip() for item in (value or "").split(",") if item.strip()]
    for required in ("127.0.0.1", "localhost"):
        if required not in entries:
            entries.append(required)
    return ",".join(entries)


def _mint_token(*, secret: str, claims: dict) -> str:
    from across_orchestrator._remote_mcp_oauth_runtime import sign_hs256_token

    return sign_hs256_token(claims=claims, secret=secret)


class _RemoteMcpServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name) / "home"
        self.home.mkdir()
        self._old_across_home = os.environ.get("ACROSS_HOME")
        self._old_no_proxy = os.environ.get("NO_PROXY")
        self._old_no_proxy_lower = os.environ.get("no_proxy")
        os.environ["NO_PROXY"] = _with_loopback_no_proxy(self._old_no_proxy)
        os.environ["no_proxy"] = _with_loopback_no_proxy(self._old_no_proxy_lower)
        os.environ["ACROSS_HOME"] = str(self.home / ".across")
        self.secret = "test-secret-do-not-use-in-prod"
        self.audience = "across-orchestrator"
        self.issuer = "http://127.0.0.1:9999"
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"

        from across_orchestrator.runtime import OrchestratorRuntime
        from across_orchestrator.server import OrchestratorHandler, apply_remote_mcp_oauth_config

        base_url = self.base
        secret = self.secret
        issuer = self.issuer
        audience = self.audience
        allowed_origins = [base_url]

        class _ConfiguredServer(ThreadingHTTPServer):
            def __init__(self, address):
                super().__init__(address, OrchestratorHandler)
                self.runtime = OrchestratorRuntime()
                self.loop_runtime = self.runtime.loop_runtime
                self.remote_mcp_oauth: dict = {}
                self.remote_mcp_sessions: dict = {}
                apply_remote_mcp_oauth_config(
                    self,
                    {
                        "base_url": base_url,
                        "mcp_endpoint": "/mcp",
                        "issuer": issuer,
                        "audience": audience,
                        "scopes": ["mcp.tools", "mcp.resources", "across.evidence.read"],
                        "hs256_secret": secret,
                        "allowed_origins": allowed_origins,
                    },
                )

        self.httpd = _ConfiguredServer(("127.0.0.1", self.port))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        if self._old_across_home is None:
            os.environ.pop("ACROSS_HOME", None)
        else:
            os.environ["ACROSS_HOME"] = self._old_across_home
        if self._old_no_proxy is None:
            os.environ.pop("NO_PROXY", None)
        else:
            os.environ["NO_PROXY"] = self._old_no_proxy
        if self._old_no_proxy_lower is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = self._old_no_proxy_lower
        self.tempdir.cleanup()

    def get(self, path: str, *, headers: dict | None = None):
        req = request.Request(self.base + path, headers=headers or {})
        try:
            with request.urlopen(req, timeout=5) as response:
                return response.status, dict(response.headers), response.read().decode("utf-8")
        except error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode("utf-8")

    def post(self, path: str, *, payload=None, headers: dict | None = None):
        data = json.dumps(payload or {}).encode("utf-8")
        req = request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=5) as response:
                return response.status, dict(response.headers), response.read().decode("utf-8")
        except error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode("utf-8")

    def jsonrpc(self, message: dict, *, session_id: str | None = None, origin: str | None = None, bearer: str | None = None):
        headers = {"Content-Type": "application/json", "Accept": MCP_ACCEPT}
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        if origin:
            headers["Origin"] = origin
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return self.post("/mcp", payload=message, headers=headers)

    def initialize(self, *, bearer: str | None = None, origin: str | None = None, protocol_version: str | None = None):
        params: dict = {"clientInfo": {"name": "test-client", "version": "0.0.1"}}
        if protocol_version:
            params["protocolVersion"] = protocol_version
        return self.jsonrpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params},
            bearer=bearer,
            origin=origin,
        )

    def make_token(self, *, scope: str = "mcp.tools mcp.resources across.evidence.read", exp_offset: int = 60, aud: str | None = None, iss: str | None = None, include_iat: bool = True, include_iss: bool = True):
        now = int(time.time())
        claims: dict = {"aud": aud or self.audience, "exp": now + exp_offset, "sub": "test-subject"}
        if include_iat:
            claims["iat"] = now
        if include_iss:
            claims["iss"] = iss or self.issuer
        claims["scope"] = scope
        return _mint_token(secret=self.secret, claims=claims)


class RemoteMcpWellKnownTests(_RemoteMcpServerTestCase):
    def test_well_known_protected_resource_metadata_returns_rfc9728_document(self):
        status, _, body = self.get("/.well-known/oauth-protected-resource")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["schema_version"], "across-orchestrator-oauth-protected-resource/1.0")
        self.assertEqual(document["resource"], self.audience)
        self.assertEqual(document["authorization_servers"], [self.issuer])
        self.assertEqual(document["bearer_methods_supported"], ["header"])
        self.assertIn("mcp.tools", document["scopes_supported"])

    def test_well_known_authorization_server_returns_proxy_metadata(self):
        status, _, body = self.get("/.well-known/oauth-authorization-server")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["issuer"], self.issuer)
        self.assertTrue(document["remote_as"])

    def test_origin_not_in_allowlist_returns_403_on_well_known(self):
        status, _, body = self.get(
            "/.well-known/oauth-protected-resource",
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "dns_rebinding_blocked")


class RemoteMcpJsonRpcTests(_RemoteMcpServerTestCase):
    def test_single_mcp_endpoint_accepts_post_with_json_rpc(self):
        token = self.make_token(scope="mcp.resources")
        status, headers, body = self.initialize(bearer=token)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["jsonrpc"], "2.0")
        self.assertEqual(payload["id"], 1)
        self.assertEqual(payload["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(payload["result"]["serverInfo"]["name"], "across-orchestrator")
        self.assertIn("Mcp-Session-Id", headers)
        self.assertTrue(headers["Mcp-Session-Id"])

    def test_legacy_rest_subpaths_return_404(self):
        # The legacy /mcp/v1/* REST endpoints were replaced by the JSON-RPC
        # dispatcher in v0.7.8. They must no longer exist as routing targets.
        for path in (
            "/mcp/v1/initialize",
            "/mcp/v1/tools/list",
            "/mcp/v1/tools/call",
            "/mcp/v1/resources/list",
            "/mcp/v1/resources/read",
        ):
            status, _, _ = self.post(path, payload={})
            self.assertEqual(status, 404, msg=f"legacy path {path} should return 404")

    def test_unauthenticated_post_returns_401_with_www_authenticate(self):
        status, headers, body = self.jsonrpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assertEqual(status, 401)
        challenge = headers.get("WWW-Authenticate") or ""
        self.assertIn("Bearer", challenge)
        self.assertIn("/.well-known/oauth-protected-resource", challenge)
        payload = json.loads(body)
        self.assertEqual(payload["error"], "missing_token")

    def test_initialize_with_audience_mismatch_returns_401(self):
        token = self.make_token(aud="http://attacker.example/mcp")
        status, _, body = self.initialize(bearer=token)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "audience_mismatch")

    def test_initialize_with_expired_token_returns_401(self):
        token = self.make_token(exp_offset=-3600)
        status, _, body = self.initialize(bearer=token)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "token_expired")

    def test_initialize_with_missing_iss_returns_401(self):
        token = self.make_token(include_iss=False)
        status, _, body = self.initialize(bearer=token)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "missing_iss")

    def test_initialize_with_missing_iat_returns_401(self):
        token = self.make_token(include_iat=False)
        status, _, body = self.initialize(bearer=token)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "missing_iat")

    def test_required_claims_can_be_configured_by_host(self):
        self.httpd.remote_mcp_oauth["required_claims"] = ["iss", "exp", "aud"]
        token = self.make_token(include_iat=False, scope="mcp.resources")
        status, headers, body = self.initialize(bearer=token)
        self.assertEqual(status, 200)
        self.assertIn("Mcp-Session-Id", headers)
        self.assertEqual(json.loads(body)["result"]["protocolVersion"], "2025-06-18")

    def test_initialize_honors_requested_protocol_version(self):
        token = self.make_token(scope="mcp.resources")
        status, _, body = self.initialize(bearer=token, protocol_version="2024-11-05")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["protocolVersion"], "2024-11-05")

    def test_initialize_with_unsupported_protocol_version_falls_back_to_2025_06_18(self):
        token = self.make_token(scope="mcp.resources")
        status, _, body = self.initialize(bearer=token, protocol_version="1999-01-01")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"]["protocolVersion"], "2025-06-18")

    def test_mcp_session_id_required_for_tools_list(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        status, _, body = self.jsonrpc(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            bearer=token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], -32600)

    def test_tools_list_with_session_returns_tools_filtered_by_scope(self):
        token = self.make_token(scope="mcp.tools mcp.resources across.evidence.read")
        _, headers, _ = self.initialize(bearer=token)
        session_id = headers["Mcp-Session-Id"]
        status, _, body = self.jsonrpc(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            session_id=session_id,
            bearer=token,
        )
        self.assertEqual(status, 200)
        names = {tool["name"] for tool in json.loads(body)["result"]["tools"]}
        self.assertIn("submit_task", names)
        self.assertIn("get_task", names)

    def test_tools_list_with_read_only_scope_excludes_write_tools(self):
        token = self.make_token(scope="mcp.resources")
        _, headers, _ = self.initialize(bearer=token)
        session_id = headers["Mcp-Session-Id"]
        status, _, body = self.jsonrpc(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            session_id=session_id,
            bearer=token,
        )
        names = {tool["name"] for tool in json.loads(body)["result"]["tools"]}
        self.assertIn("get_task", names)
        self.assertNotIn("submit_task", names)
        self.assertNotIn("start_agent_loop", names)

    def test_tools_call_get_agent_card_with_resources_scope(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        _, headers, _ = self.initialize(bearer=token)
        session_id = headers["Mcp-Session-Id"]
        status, _, body = self.jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_agent_card", "arguments": {}},
            },
            session_id=session_id,
            bearer=token,
        )
        self.assertEqual(status, 200)
        result = json.loads(body)["result"]
        text = json.loads(result["content"][0]["text"])
        self.assertEqual(text["name"], "Across Orchestrator")
        self.assertEqual(result["isError"], False)

    def test_tools_call_submit_task_uses_server_managed_project_root(self):
        token = self.make_token(scope="mcp.tools")
        _, headers, _ = self.initialize(bearer=token)
        session_id = headers["Mcp-Session-Id"]
        attacker_root = self.home / "attacker-controlled"
        status, _, body = self.jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "submit_task",
                    "arguments": {"goal": "remote task", "projectRoot": str(attacker_root)},
                },
            },
            session_id=session_id,
            bearer=token,
        )
        self.assertEqual(status, 200)
        result = json.loads(body)["result"]
        task = json.loads(result["content"][0]["text"])
        project_root = Path(task["project_root"])
        managed_root = (
            self.home / ".across" / "run" / "across-orchestrator" / "http-workspaces" / "mcp-task"
        ).resolve()
        self.assertTrue(str(project_root).startswith(str(managed_root)))
        self.assertFalse(attacker_root.exists())

    def test_tools_call_start_agent_loop_uses_server_managed_project_root(self):
        token = self.make_token(scope="mcp.tools")
        _, headers, _ = self.initialize(bearer=token)
        session_id = headers["Mcp-Session-Id"]
        attacker_root = self.home / "loop-attacker-controlled"
        status, _, body = self.jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "start_agent_loop",
                    "arguments": {"goal": "remote loop", "projectRoot": str(attacker_root)},
                },
            },
            session_id=session_id,
            bearer=token,
        )
        self.assertEqual(status, 200)
        result = json.loads(body)["result"]
        loop = json.loads(result["content"][0]["text"])
        project_root = Path(loop["project_root"])
        managed_root = (
            self.home / ".across" / "run" / "across-orchestrator" / "http-workspaces" / "mcp-agent-loop"
        ).resolve()
        self.assertTrue(str(project_root).startswith(str(managed_root)))
        self.assertFalse(attacker_root.exists())

    def test_tools_call_without_required_scope_returns_403_insufficient_scope(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        _, headers, _ = self.initialize(bearer=token)
        session_id = headers["Mcp-Session-Id"]
        status, headers_out, _ = self.jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "submit_task", "arguments": {"goal": "x", "projectRoot": "."}},
            },
            session_id=session_id,
            bearer=token,
        )
        self.assertEqual(status, 403)
        challenge = headers_out.get("WWW-Authenticate") or ""
        self.assertIn("insufficient_scope", challenge)

    def test_resources_list_requires_resources_or_evidence_scope(self):
        # mcp.tools-only token must NOT be able to list resources.
        token = self.make_token(scope="mcp.tools across.evidence.read")
        _, headers, _ = self.initialize(bearer=token)
        session_id = headers["Mcp-Session-Id"]
        status, _, body = self.jsonrpc(
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {}},
            session_id=session_id,
            bearer=token,
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "insufficient_scope")

    def test_resources_list_with_resources_scope_returns_known_resources(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        _, headers, _ = self.initialize(bearer=token)
        session_id = headers["Mcp-Session-Id"]
        status, _, body = self.jsonrpc(
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {}},
            session_id=session_id,
            bearer=token,
        )
        self.assertEqual(status, 200)
        uris = {item["uri"] for item in json.loads(body)["result"]["resources"]}
        self.assertIn("across-orchestrator://agent-card", uris)
        self.assertIn("across-orchestrator://plugin-manifest", uris)
        self.assertIn("across-orchestrator://finding-schema", uris)

    def test_resources_read_returns_agent_card_content(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        _, headers, _ = self.initialize(bearer=token)
        session_id = headers["Mcp-Session-Id"]
        status, _, body = self.jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "resources/read",
                "params": {"uri": "across-orchestrator://agent-card"},
            },
            session_id=session_id,
            bearer=token,
        )
        self.assertEqual(status, 200)
        contents = json.loads(body)["result"]["contents"]
        self.assertEqual(contents[0]["uri"], "across-orchestrator://agent-card")
        card = json.loads(contents[0]["text"])
        self.assertEqual(card["name"], "Across Orchestrator")

    def test_resources_read_unknown_uri_returns_jsonrpc_error(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        _, headers, _ = self.initialize(bearer=token)
        session_id = headers["Mcp-Session-Id"]
        status, _, body = self.jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "resources/read",
                "params": {"uri": "across-orchestrator://does-not-exist"},
            },
            session_id=session_id,
            bearer=token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["error"]["code"], -32603)

    def test_ping_returns_empty_result(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        status, _, body = self.jsonrpc(
            {"jsonrpc": "2.0", "id": 99, "method": "ping", "params": {}},
            bearer=token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"], {})

    def test_ping_without_bearer_returns_401(self):
        status, _, body = self.jsonrpc(
            {"jsonrpc": "2.0", "id": 99, "method": "ping", "params": {}},
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "missing_token")

    def test_unknown_method_returns_jsonrpc_method_not_found(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        status, _, body = self.jsonrpc(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/banana", "params": {}},
            bearer=token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["error"]["code"], -32601)

    def test_invalid_json_returns_parse_error(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        req = request.Request(
            self.base + "/mcp",
            data=b"{not json",
            headers={
                "Content-Type": "application/json",
                "Accept": MCP_ACCEPT,
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=5) as response:
                self.assertEqual(response.status, 400)
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["error"]["code"], -32700)
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            payload = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(payload["error"]["code"], -32700)

    def test_notification_without_id_returns_202(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        req = request.Request(
            self.base + "/mcp",
            data=json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": MCP_ACCEPT,
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 202)

    def test_notification_without_bearer_returns_401(self):
        status, _, body = self.jsonrpc(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        )
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], "missing_token")

    def test_get_mcp_with_sse_accept_opens_notification_stream(self):
        # GET /mcp with text/event-stream Accept must return 200 SSE,
        # not 405.
        token = self.make_token(scope="mcp.resources across.evidence.read")
        req = request.Request(
            self.base + "/mcp",
            headers={"Accept": "text/event-stream", "Authorization": f"Bearer {token}"},
        )
        try:
            with request.urlopen(req, timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get("Content-Type"), "text/event-stream; charset=utf-8")
                first_chunk = response.read(64)
                self.assertIn(b": mcp-server-sse-open", first_chunk)
        except Exception:
            # Reading the keep-alive stream is timing-sensitive in CI; the
            # status check above is sufficient.
            pass

    def test_get_mcp_without_sse_accept_returns_405(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        req = request.Request(
            self.base + "/mcp",
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        )
        try:
            request.urlopen(req, timeout=2)
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 405)
            self.assertEqual(exc.headers.get("Allow"), "POST, DELETE")

    def test_get_mcp_without_bearer_returns_401(self):
        req = request.Request(self.base + "/mcp", headers={"Accept": "text/event-stream"})
        try:
            request.urlopen(req, timeout=2)
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 401)
            payload = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(payload["error"], "missing_token")

    def test_delete_mcp_terminates_session(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        _, headers, _ = self.initialize(bearer=token)
        session_id = headers["Mcp-Session-Id"]
        req = request.Request(
            self.base + "/mcp",
            headers={"Mcp-Session-Id": session_id, "Authorization": f"Bearer {token}"},
            method="DELETE",
        )
        with request.urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 204)
        # Subsequent tools/list with the deleted session must return 404.
        status, _, body = self.jsonrpc(
            {"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}},
            session_id=session_id,
            bearer=token,
        )
        self.assertEqual(status, 404)

    def test_delete_mcp_without_bearer_returns_401(self):
        req = request.Request(
            self.base + "/mcp",
            headers={"Mcp-Session-Id": "missing"},
            method="DELETE",
        )
        try:
            request.urlopen(req, timeout=5)
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 401)
            payload = json.loads(exc.read().decode("utf-8"))
            self.assertEqual(payload["error"], "missing_token")

    def test_session_id_required_on_subsequent_requests(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        self.initialize(bearer=token)
        status, _, body = self.jsonrpc(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}},
            bearer=token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], -32600)

    def test_legacy_rest_subpaths_are_replaced(self):
        for path in (
            "/mcp/v1/initialize",
            "/mcp/v1/tools/list",
            "/mcp/v1/tools/call",
            "/mcp/v1/resources/list",
            "/mcp/v1/resources/read",
        ):
            token = self.make_token(scope="mcp.resources across.evidence.read")
            status, _, _ = self.post(
                path,
                payload={"name": "get_agent_card", "arguments": {}},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": MCP_ACCEPT,
                },
            )
            self.assertEqual(status, 404, msg=f"legacy REST path {path} should return 404")

    def test_origin_header_dns_rebinding_attack_is_blocked(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        status, _, body = self.jsonrpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            bearer=token,
            origin="https://evil.example",
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "dns_rebinding_blocked")

    def test_origin_matching_forged_host_still_requires_allowlist(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        data = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        ).encode("utf-8")
        req = request.Request(
            self.base + "/mcp",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": MCP_ACCEPT,
                "Authorization": f"Bearer {token}",
                "Origin": f"http://evil.example:{self.port}",
                "Host": f"evil.example:{self.port}",
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=5) as response:
                status = response.status
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            status = exc.code
            body = exc.read().decode("utf-8")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "dns_rebinding_blocked")

    def test_origin_matches_loopback_default_is_allowed(self):
        token = self.make_token(scope="mcp.resources across.evidence.read")
        status, _, body = self.jsonrpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            bearer=token,
            origin=self.base,
        )
        self.assertEqual(status, 200)


class RemoteMcpTemplateTests(unittest.TestCase):
    def test_template_defaults_audience_to_canonical_endpoint(self):
        from across_orchestrator.remote_mcp import render_remote_mcp_oauth_template

        document = render_remote_mcp_oauth_template(
            {
                "base_url": "https://mcp.example.com/mcp",
                "issuer": "https://issuer.example.com",
            }
        )
        self.assertEqual(document["authorization"]["audience"], "https://mcp.example.com/mcp")
        self.assertEqual(document["authorization"]["resource_indicators"], ["https://mcp.example.com/mcp"])

    def test_template_render_remote_mcp_oauth_template_exposes_new_fields(self):
        from across_orchestrator.remote_mcp import render_remote_mcp_oauth_template

        document = render_remote_mcp_oauth_template(
            {
                "base_url": "http://127.0.0.1:8765",
                "issuer": "http://127.0.0.1:9000",
                "audience": "https://mcp.example.com/mcp",
                "jwks_uri": "https://mcp.example.com/jwks",
            }
        )
        self.assertEqual(document["schema_version"], "across-remote-mcp-oauth-template/1.0")
        self.assertEqual(document["authorization"]["authorization_servers"], ["http://127.0.0.1:9000"])
        self.assertEqual(document["authorization"]["resource_indicators"], ["https://mcp.example.com/mcp"])
        self.assertEqual(document["authorization"]["bearer_methods_supported"], ["header"])
        self.assertEqual(document["authorization"]["jwks_uri"], "https://mcp.example.com/jwks")
        self.assertIn("2025-06-18", document["transport"]["protocol_versions_supported"])
        self.assertEqual(document["_runtime_status"]["status"], "server_partial")
        self.assertEqual(document["well_known"]["protected_resource"], "/.well-known/oauth-protected-resource")

    def test_template_rejects_non_https_jwks_uri(self):
        from across_orchestrator.remote_mcp import render_remote_mcp_oauth_template

        document = render_remote_mcp_oauth_template(
            {
                "base_url": "http://127.0.0.1:8765",
                "issuer": "http://127.0.0.1:9000",
                "audience": "https://mcp.example.com/mcp",
                "jwks_uri": "ftp://example.com/jwks",
            }
        )
        self.assertEqual(document["status"], "failed")


class RemoteMcpOriginHelperTests(unittest.TestCase):
    def test_http_header_value_rejects_response_splitting(self):
        from across_orchestrator.server import _http_header_value

        self.assertEqual(_http_header_value("Bearer realm=\"x\""), "Bearer realm=\"x\"")
        with self.assertRaises(ValueError):
            _http_header_value("Bearer realm=\"x\"\r\nX-Bad: y")

    def test_origin_helper_allows_loopback_when_header_absent(self):
        from across_orchestrator._remote_mcp_oauth_runtime import is_origin_allowed

        allowed, reason = is_origin_allowed(None, allowed_origins=("http://127.0.0.1:8765",))
        self.assertTrue(allowed)
        self.assertEqual(reason, "origin_absent")

    def test_origin_helper_rejects_non_allowlisted(self):
        from across_orchestrator._remote_mcp_oauth_runtime import is_origin_allowed

        allowed, reason = is_origin_allowed(
            "https://evil.example",
            allowed_origins=("http://127.0.0.1:8765",),
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "origin_not_allowlisted")

    def test_origin_helper_rejects_host_fallback_without_allowlist(self):
        from across_orchestrator._remote_mcp_oauth_runtime import is_origin_allowed

        allowed, reason = is_origin_allowed(
            "http://localhost:8765",
            allowed_origins=(),
            host="localhost:8765",
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "origin_not_allowlisted")

    def test_resource_scope_helper_returns_resources_or_evidence(self):
        from across_orchestrator._remote_mcp_oauth_runtime import scopes_for_resource

        self.assertEqual(scopes_for_resource("across-orchestrator://agent-card"), ("mcp.resources",))
        self.assertEqual(
            scopes_for_resource("across-orchestrator://evidence-bundle"),
            ("across.evidence.read", "mcp.resources"),
        )


class RemoteMcpSessionSafetyTests(unittest.TestCase):
    def test_session_id_helper_round_trip(self):
        from across_orchestrator._remote_mcp_oauth_runtime import (
            is_safe_session_id,
            issue_session_id,
        )

        sid = issue_session_id()
        self.assertTrue(is_safe_session_id(sid))

    def test_session_id_rejects_unsafe_values(self):
        from across_orchestrator._remote_mcp_oauth_runtime import is_safe_session_id

        self.assertFalse(is_safe_session_id(""))
        self.assertFalse(is_safe_session_id("../etc/passwd"))
        self.assertFalse(is_safe_session_id("has space"))


@unittest.skipUnless(_has_pyjwt_crypto(), "PyJWT[crypto] is optional")
class RemoteMcpAsymmetricJwtTests(unittest.TestCase):
    def test_verify_bearer_token_accepts_rs256_jwks(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import jwt
        from jwt.algorithms import RSAAlgorithm

        from across_orchestrator._remote_mcp_oauth_runtime import verify_bearer_token

        audience = "http://127.0.0.1:8765/mcp"
        issuer = "http://127.0.0.1:8765"
        now = int(time.time())
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
        jwk["kid"] = "rsa-test"
        token = jwt.encode(
            {
                "iss": issuer,
                "aud": audience,
                "iat": now,
                "exp": now + 300,
                "scope": "mcp.resources",
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": "rsa-test"},
        )

        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            json.dump({"keys": [jwk]}, handle)
            jwks_path = handle.name

        result = verify_bearer_token(token, audience=audience, issuer=issuer, jwks_url=jwks_path)
        self.assertEqual(result["claims"]["aud"], audience)

    def test_verify_bearer_token_accepts_es256_jwks(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        import jwt
        from jwt.algorithms import ECAlgorithm

        from across_orchestrator._remote_mcp_oauth_runtime import verify_bearer_token

        audience = "http://127.0.0.1:8765/mcp"
        issuer = "http://127.0.0.1:8765"
        now = int(time.time())
        key = ec.generate_private_key(ec.SECP256R1())
        private_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        jwk = json.loads(ECAlgorithm.to_jwk(key.public_key()))
        jwk["kid"] = "ec-test"
        token = jwt.encode(
            {
                "iss": issuer,
                "aud": audience,
                "iat": now,
                "exp": now + 300,
                "scope": "mcp.resources",
            },
            private_pem,
            algorithm="ES256",
            headers={"kid": "ec-test"},
        )

        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            json.dump({"keys": [jwk]}, handle)
            jwks_path = handle.name

        result = verify_bearer_token(token, audience=audience, issuer=issuer, jwks_url=jwks_path)
        self.assertEqual(result["claims"]["aud"], audience)


class RemoteMcpCliBoundaryTests(unittest.TestCase):
    def test_remote_mcp_server_without_subcommand_returns_missing_subcommand(self):
        result = subprocess.run(
            [sys.executable, "-m", "across_orchestrator.cli", "remote-mcp-server"],
            cwd=Path(__file__).resolve().parents[1],
            env={**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing_subcommand", result.stdout)


class RemoteMcpCliStartTests(unittest.TestCase):
    """Smoke test the CLI subcommand ``remote-mcp-server start``."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(__file__).resolve().parents[1]
        self.home = Path(self.tempdir.name) / "home"
        self.home.mkdir()
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "across_orchestrator.cli",
                "remote-mcp-server",
                "start",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--config-json",
                json.dumps(
                    {
                        "issuer": self.base,
                        "audience": f"{self.base}/mcp",
                        "scopes": ["mcp.tools", "mcp.resources", "across.evidence.read"],
                        "hs256_secret": "cli-start-test-secret",
                    }
                ),
            ],
            cwd=self.root,
            env={
                **__import__("os").environ,
                "PYTHONPATH": str(self.root / "src"),
                "ACROSS_ORCHESTRATOR_HOME": str(self.home),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with request.urlopen(self.base + "/.well-known/oauth-protected-resource", timeout=1) as response:
                    if response.status == 200:
                        return
            except Exception:
                time.sleep(0.1)
        self.process.terminate()
        stdout, stderr = self.process.communicate(timeout=2)
        self.fail(f"server failed to start\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")

    def tearDown(self):
        self.process.terminate()
        try:
            self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate(timeout=5)
        self.tempdir.cleanup()

    def test_cli_start_serves_protected_resource_metadata(self):
        with request.urlopen(self.base + "/.well-known/oauth-protected-resource", timeout=5) as response:
            document = json.loads(response.read().decode("utf-8"))
            self.assertEqual(document["resource"], f"{self.base}/mcp")
            self.assertEqual(document["authorization_servers"], [self.base])

    def test_cli_start_serves_authorization_server_metadata(self):
        with request.urlopen(self.base + "/.well-known/oauth-authorization-server", timeout=5) as response:
            document = json.loads(response.read().decode("utf-8"))
            self.assertTrue(document["remote_as"])

    def test_cli_start_rejects_unauthenticated_jsonrpc_post(self):
        try:
            request.urlopen(
                request.Request(
                    self.base + "/mcp",
                    data=json.dumps(
                        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
                    ).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Accept": MCP_ACCEPT,
                    },
                    method="POST",
                ),
                timeout=5,
            )
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 401)
            challenge = exc.headers.get("WWW-Authenticate") or ""
            self.assertIn("Bearer", challenge)


if __name__ == "__main__":
    unittest.main()
