from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
import asyncio
import base64
import http.server
import http.client
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID

from .coordinator import CoordinatorError, WorkerCoordinator
from .relay import RelayEndpoint, read_framed_json, write_framed_json
from .worker_protocol import ArtifactDescriptor, CapabilityManifest, JobEvent, JobLease, JobManifest, ProtocolError, build_evidence_receipt, new_protocol_id
from .worker_runtime import BoundedProcessExecutor, ChunkedArtifactReceiver, ExecutionResult


class WorkerTransportError(RuntimeError):
    pass


def _model_grant_ttl_seconds(
    policy: Mapping[str, Any],
    job_timeout_seconds: int | float | None = None,
) -> int:
    """Keep grants valid through every permitted call and final accounting."""
    provider_deadline = int(policy.get("timeout_seconds") or 300)
    job_deadline = int(job_timeout_seconds or 0)
    return max(60, provider_deadline + 60, job_deadline + 60)


def tls_server_context(*, certificate: str | Path, private_key: str | Path, client_ca: str | Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(str(certificate), str(private_key))
    context.load_verify_locations(cafile=str(client_ca))
    return context


def tls_client_context(*, certificate: str | Path, private_key: str | Path, server_ca: str | Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(str(certificate), str(private_key))
    context.load_verify_locations(cafile=str(server_ca))
    return context


class CoordinatorSessionServer:
    """TLS 1.3 mutual-auth session endpoint. Workers always initiate connections."""

    def __init__(
        self,
        coordinator: WorkerCoordinator,
        *,
        host: str,
        port: int,
        ssl_context: ssl.SSLContext,
        artifact_root: str | Path,
        transport: str = "direct",
        model_gateway_url: str | None = None,
    ):
        if not host or host in {"0.0.0.0", "::", "*"}:
            raise WorkerTransportError("coordinator listener requires one explicit interface")
        if transport not in {"direct", "overlay"}:
            raise WorkerTransportError("coordinator session transport must be direct or overlay")
        self.coordinator = coordinator
        self.host = host
        self.port = int(port)
        self.ssl_context = ssl_context
        self.transport = transport
        self.model_gateway_url = str(model_gateway_url or "").strip() or None
        self.artifacts = ChunkedArtifactReceiver(artifact_root)
        self.server: asyncio.AbstractServer | None = None

    @property
    def bound_port(self) -> int:
        if not self.server or not self.server.sockets:
            return self.port
        return int(self.server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, host=self.host, port=self.port, ssl=self.ssl_context)

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        node_id: str | None = None
        disconnect_reason = "session_closed"
        try:
            hello = await asyncio.wait_for(read_framed_json(reader), timeout=10)
            if hello.get("type") != "worker.hello":
                raise WorkerTransportError("worker hello is required")
            capability = CapabilityManifest.from_dict(dict(hello.get("capability_manifest") or {}))
            node_id = capability.node_id
            identity_generation = int(hello.get("identity_generation") or 0)
            peer_identity = _tls_peer_identity(writer)
            self.coordinator.connect_node(
                capability,
                transport=self.transport,
                identity_generation=identity_generation,
                peer_node_id=peer_identity["node_id"],
                peer_certificate_fingerprint=peer_identity["certificate_fingerprint"],
                require_peer_identity=True,
            )
            await write_framed_json(
                writer,
                {
                    "type": "coordinator.ready",
                    "protocol_version": "across-worker-session/1.0",
                    "node_id": node_id,
                    "transport": self.transport,
                    "tls_version": writer.get_extra_info("ssl_object").version(),
                },
            )
            while True:
                request = await asyncio.wait_for(read_framed_json(reader), timeout=45)
                kind = request.get("type")
                if kind == "worker.heartbeat":
                    node = self.coordinator.heartbeat_node(node_id, current_load=float(request.get("current_load") or 0))
                    await write_framed_json(writer, {"type": "coordinator.heartbeat", "node": node})
                elif kind == "worker.lease_heartbeat":
                    control = self.coordinator.lease_control(
                        str(request.get("lease_id") or ""),
                        node_id=node_id,
                        attempt=int(request.get("attempt") or 0),
                    )
                    self.coordinator.heartbeat_node(node_id, current_load=float(request.get("current_load") or 1.0))
                    await write_framed_json(writer, {"type": "coordinator.lease_status", **control})
                elif kind == "worker.lease_request":
                    lease = self.coordinator.lease_next(node_id)
                    if lease is None:
                        await write_framed_json(
                            writer,
                            {
                                "type": "coordinator.no_job",
                                "update_directive": self.coordinator.node_update_directive(node_id),
                                "transport_directive": self.coordinator.node_transport_directive(node_id),
                            },
                        )
                    else:
                        job = self.coordinator.worker_job_payload(lease.job_id)
                        manifest = JobManifest.from_dict(job["manifest"])
                        model_grant = None
                        if manifest.model_policy.get("enabled"):
                            if not self.model_gateway_url:
                                raise WorkerTransportError("live-model job requires a host model gateway")
                            policy = manifest.model_policy
                            model_grant = self.coordinator.issue_model_grant(
                                job_id=manifest.job_id,
                                node_id=node_id,
                                purposes=("scenario_round_annotation",),
                                model_policy=str(policy.get("policy") or "host-default"),
                                max_calls=int(policy.get("max_calls") or 1),
                                max_tokens=int(policy.get("max_tokens") or 1),
                                max_concurrency=int(policy.get("max_concurrency") or 1),
                                max_cost_usd=float(policy.get("max_cost_usd") or 0),
                                ttl_seconds=_model_grant_ttl_seconds(
                                    policy,
                                    manifest.budgets.get("timeout_seconds"),
                                ),
                            ).to_dict()
                        await write_framed_json(
                            writer,
                            {
                                "type": "coordinator.job",
                                "lease": lease.to_dict(),
                                "manifest": manifest.to_dict(),
                                "inputs_base64": job["inputs_base64"],
                                "model_grant": model_grant,
                                "model_gateway_url": self.model_gateway_url if model_grant else None,
                            },
                        )
                elif kind == "worker.lease_ack":
                    lease = self.coordinator.acknowledge_lease(str(request.get("lease_id") or ""), str(request.get("manifest_hash") or ""))
                    await write_framed_json(writer, {"type": "coordinator.lease_acknowledged", "lease": lease.to_dict()})
                elif kind == "worker.event":
                    event = _event_from_dict(dict(request.get("event") or {}))
                    recorded = self.coordinator.record_event(event)
                    await write_framed_json(writer, {"type": "coordinator.event_recorded", "event": recorded})
                elif kind == "worker.artifact_begin":
                    descriptor = _artifact_from_dict(dict(request.get("artifact") or {}))
                    state = self.artifacts.begin(descriptor, chunk_size=int(request.get("chunk_size") or 1024 * 1024))
                    await write_framed_json(writer, {"type": "coordinator.artifact_ready", "state": state})
                elif kind == "worker.artifact_chunk":
                    content = base64.b64decode(str(request.get("content_base64") or ""), validate=True)
                    state = self.artifacts.write_chunk(
                        str(request.get("artifact_id") or ""),
                        int(request.get("index") or 0),
                        content,
                        str(request.get("sha256") or ""),
                    )
                    await write_framed_json(writer, {"type": "coordinator.artifact_chunk", "state": state})
                elif kind == "worker.artifact_finalize":
                    path = self.artifacts.finalize(str(request.get("artifact_id") or ""))
                    await write_framed_json(writer, {"type": "coordinator.artifact_verified", "name": path.name, "sha256": _file_hash(path), "size": path.stat().st_size})
                elif kind == "worker.update_result":
                    node = self.coordinator.record_node_update(
                        node_id,
                        directive_id=str(request.get("directive_id") or ""),
                        status=str(request.get("status") or ""),
                        error=str(request.get("error") or "") or None,
                    )
                    await write_framed_json(writer, {"type": "coordinator.update_recorded", "node": node})
                elif kind == "worker.transport_result":
                    node = self.coordinator.record_node_transport(
                        node_id,
                        directive_id=str(request.get("directive_id") or ""),
                        status=str(request.get("status") or ""),
                        error=str(request.get("error") or "") or None,
                    )
                    await write_framed_json(writer, {"type": "coordinator.transport_recorded", "node": node})
                elif kind == "worker.goodbye":
                    await write_framed_json(writer, {"type": "coordinator.goodbye", "node_id": node_id})
                    disconnect_reason = "worker_goodbye"
                    break
                else:
                    raise WorkerTransportError("unsupported worker session message")
        except asyncio.TimeoutError:
            disconnect_reason = "session_timeout"
        except (asyncio.IncompleteReadError, ConnectionError):
            disconnect_reason = "connection_lost"
        except (CoordinatorError, ProtocolError, WorkerTransportError, ValueError):
            disconnect_reason = "session_rejected"
        finally:
            if node_id:
                try:
                    self.coordinator.disconnect_node(node_id, reason=disconnect_reason)
                except CoordinatorError:
                    pass
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass


@dataclass(frozen=True)
class WorkerSessionResult:
    status: str
    transport: str
    tls_version: str
    job_id: str | None = None
    execution: ExecutionResult | None = None


class WorkerSessionClient:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        server_hostname: str,
        ssl_context: ssl.SSLContext,
        capability: CapabilityManifest,
        worker_root: str | Path,
        identity_generation: int = 1,
    ):
        self.host = host
        self.port = int(port)
        self.server_hostname = server_hostname
        self.ssl_context = ssl_context
        self.capability = capability
        self.worker_root = Path(worker_root).expanduser().resolve()
        self.identity_generation = identity_generation

    async def run_once(self, *, idle_poll_seconds: float = 0.0, idle_session_seconds: float = 0.0) -> WorkerSessionResult:
        reader, writer = await asyncio.open_connection(
            host=self.host,
            port=self.port,
            ssl=self.ssl_context,
            server_hostname=self.server_hostname,
        )
        try:
            await write_framed_json(
                writer,
                {
                    "type": "worker.hello",
                    "capability_manifest": self.capability.to_dict(),
                    "identity_generation": self.identity_generation,
                },
            )
            ready = await read_framed_json(reader)
            if ready.get("type") != "coordinator.ready" or ready.get("tls_version") != "TLSv1.3":
                raise WorkerTransportError("coordinator did not establish a TLS 1.3 session")
            idle_started = time.monotonic()
            while True:
                await write_framed_json(writer, {"type": "worker.lease_request", "node_id": self.capability.node_id})
                response = await read_framed_json(reader)
                if response.get("type") != "coordinator.no_job":
                    break
                if isinstance(response.get("update_directive"), Mapping):
                    update_status = await self._apply_update(reader, writer, dict(response["update_directive"]))
                    return WorkerSessionResult(status=update_status, transport=str(ready["transport"]), tls_version="TLSv1.3")
                if isinstance(response.get("transport_directive"), Mapping):
                    transport_status = await self._apply_transport(reader, writer, dict(response["transport_directive"]))
                    return WorkerSessionResult(status=transport_status, transport=str(ready["transport"]), tls_version="TLSv1.3")
                if idle_session_seconds <= 0 or time.monotonic() - idle_started >= idle_session_seconds:
                    await write_framed_json(writer, {"type": "worker.goodbye"})
                    await read_framed_json(reader)
                    return WorkerSessionResult(status="idle", transport=str(ready["transport"]), tls_version="TLSv1.3")
                await asyncio.sleep(max(0.1, min(float(idle_poll_seconds or 1.0), 5.0)))
            if response.get("type") != "coordinator.job":
                raise WorkerTransportError("coordinator returned an invalid lease response")
            lease = _lease_from_dict(dict(response.get("lease") or {}))
            manifest = JobManifest.from_dict(dict(response.get("manifest") or {}))
            if lease.node_id != self.capability.node_id or lease.manifest_hash != manifest.manifest_hash:
                raise WorkerTransportError("lease and manifest binding mismatch")
            await write_framed_json(writer, {"type": "worker.lease_ack", "lease_id": lease.lease_id, "manifest_hash": manifest.manifest_hash})
            acknowledgement = await read_framed_json(reader)
            if acknowledgement.get("type") != "coordinator.lease_acknowledged":
                raise WorkerTransportError("coordinator did not acknowledge the manifest hash")
            await self._send_event(reader, writer, lease, "preparing", 1)
            model_grant = response.get("model_grant") if isinstance(response.get("model_grant"), Mapping) else None
            input_payloads = {
                str(name): base64.b64decode(str(value), validate=True)
                for name, value in dict(response.get("inputs_base64") or {}).items()
            }
            extra_env = {"ACROSS_TRANSPORT": str(ready["transport"])}
            if model_grant:
                extra_env.update(
                    {
                        "ACROSS_MODEL_GRANT_ID": str(model_grant.get("grant_id") or ""),
                        "ACROSS_MODEL_TIMEOUT_SECONDS": str(manifest.model_policy.get("timeout_seconds") or 30),
                        "ACROSS_MODEL_MAX_TOKENS": str(
                            max(1, int(manifest.model_policy.get("max_tokens") or 1) // max(1, int(manifest.model_policy.get("max_calls") or 1)))
                        ),
                    }
                )
            proxy = None
            if model_grant:
                proxy = WorkerLocalGrantProxy(
                    upstream_url=str(response.get("model_gateway_url") or ""),
                    worker_root=self.worker_root,
                    binding={
                        "grant_id": str(model_grant.get("grant_id") or ""),
                        "run_id": manifest.run_id,
                        "job_id": manifest.job_id,
                        "node_id": self.capability.node_id,
                        "purpose": "scenario_round_annotation",
                    },
                )
                proxy.start()
                extra_env["ACROSS_MODEL_GATEWAY_URL"] = proxy.url
            try:
                cancel_event = threading.Event()
                started_event = threading.Event()
                execution_done = asyncio.Event()
                await self._poll_lease_control(reader, writer, lease, cancel_event)
                executor = BoundedProcessExecutor(self.worker_root)
                execution_task = asyncio.create_task(
                    asyncio.to_thread(
                        executor.execute,
                        manifest,
                        lease,
                        cancel_event=cancel_event,
                        started_event=started_event,
                        extra_env=extra_env,
                        input_payloads=input_payloads,
                    )
                )
                while not started_event.is_set() and not execution_task.done():
                    await asyncio.sleep(0.01)
                if started_event.is_set():
                    await self._send_event(reader, writer, lease, "running", 2)
                control_task = asyncio.create_task(self._maintain_lease(reader, writer, lease, cancel_event, execution_done))
                try:
                    execution = await execution_task
                finally:
                    execution_done.set()
                    await control_task
            finally:
                if proxy:
                    proxy.close()
            sequence = 3
            for artifact in execution.artifacts:
                await self._upload_artifact(reader, writer, artifact, execution.sandbox / "output" / artifact.logical_name)
                sequence += 1
            cleanup_status = execution.cleanup_status
            if int(manifest.cleanup_policy.get("retention_seconds") or 0) <= 0:
                try:
                    executor.cleanup(run_id=manifest.run_id, job_id=manifest.job_id, attempt=lease.attempt)
                    cleanup_status = "complete"
                except (OSError, CoordinatorError, ProtocolError):
                    cleanup_status = "failed"
            execution.cleanup_status = cleanup_status
            terminal_state = "failed" if cleanup_status == "failed" and execution.state == "completed" else execution.state
            terminal_reason = "cleanup_failed" if cleanup_status == "failed" and execution.state == "completed" else execution.reason_category
            receipt = build_evidence_receipt(
                manifest=manifest,
                node=self.capability,
                lease=lease,
                terminal_state=terminal_state,
                artifacts=execution.artifacts,
                quality_gates={},
                model_usage={},
                cleanup_status=cleanup_status,
                started_at=execution.started_at,
                ended_at=execution.ended_at,
                resource_usage=execution.resource_usage,
            )
            await self._send_event(
                reader,
                writer,
                lease,
                terminal_state,
                sequence,
                reason_category=terminal_reason,
                payload={
                    "artifacts": [item.to_dict() for item in execution.artifacts],
                    "exit_code": execution.exit_code,
                    "cleanup_status": cleanup_status,
                    "resource_usage": execution.resource_usage,
                    "evidence_receipt": receipt,
                },
            )
            await write_framed_json(writer, {"type": "worker.goodbye"})
            await read_framed_json(reader)
            return WorkerSessionResult(
                status=terminal_state,
                transport=str(ready["transport"]),
                tls_version="TLSv1.3",
                job_id=manifest.job_id,
                execution=execution,
            )
        finally:
            writer.close()
            await writer.wait_closed()

    async def _maintain_lease(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        lease: JobLease,
        cancel_event: threading.Event,
        execution_done: asyncio.Event,
    ) -> None:
        interval = max(0.1, min(float(lease.heartbeat_interval_seconds), 0.25))
        while not execution_done.is_set():
            try:
                await asyncio.wait_for(execution_done.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            await self._poll_lease_control(reader, writer, lease, cancel_event)

    async def _apply_update(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, directive: Mapping[str, Any]) -> str:
        directive_id = str(directive.get("directive_id") or "")
        await write_framed_json(writer, {"type": "worker.update_result", "directive_id": directive_id, "status": "downloading"})
        await read_framed_json(reader)
        try:
            from .worker_cli import update_worker_from_url

            await asyncio.to_thread(
                update_worker_from_url,
                self.worker_root,
                url=str(directive.get("url") or ""),
                expected_sha256=str(directive.get("sha256") or ""),
                version=str(directive.get("version") or ""),
                restart_service=False,
            )
        except Exception as exc:
            await write_framed_json(writer, {"type": "worker.update_result", "directive_id": directive_id, "status": "failed", "error": type(exc).__name__})
            await read_framed_json(reader)
            return "update_failed"
        await write_framed_json(writer, {"type": "worker.update_result", "directive_id": directive_id, "status": "completed"})
        await read_framed_json(reader)
        return "updated"

    async def _apply_transport(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, directive: Mapping[str, Any]) -> str:
        directive_id = str(directive.get("directive_id") or "")
        await write_framed_json(writer, {"type": "worker.transport_result", "directive_id": directive_id, "status": "applying"})
        await read_framed_json(reader)
        try:
            from .worker_cli import apply_transport_directive

            await asyncio.to_thread(apply_transport_directive, self.worker_root, directive=directive)
        except Exception as exc:
            await write_framed_json(writer, {"type": "worker.transport_result", "directive_id": directive_id, "status": "failed", "error": type(exc).__name__})
            await read_framed_json(reader)
            return "transport_switch_failed"
        await write_framed_json(writer, {"type": "worker.transport_result", "directive_id": directive_id, "status": "completed"})
        await read_framed_json(reader)
        return "transport_switched"

    async def _poll_lease_control(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        lease: JobLease,
        cancel_event: threading.Event,
    ) -> None:
        await write_framed_json(
            writer,
            {
                "type": "worker.lease_heartbeat",
                "lease_id": lease.lease_id,
                "attempt": lease.attempt,
                "current_load": 1.0,
            },
        )
        response = await read_framed_json(reader)
        if response.get("type") != "coordinator.lease_status":
            raise WorkerTransportError("coordinator rejected the lease heartbeat")
        if response.get("cancel_requested"):
            cancel_event.set()

    async def _send_event(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        lease: JobLease,
        state: str,
        sequence: int,
        *,
        reason_category: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        event = JobEvent(
            event_id=new_protocol_id("event"),
            job_id=lease.job_id,
            run_id=lease.run_id,
            node_id=lease.node_id,
            lease_id=lease.lease_id,
            attempt=lease.attempt,
            sequence=sequence,
            state=state,
            reason_category=reason_category,
            payload=dict(payload or {}),
        )
        await write_framed_json(writer, {"type": "worker.event", "event": event.to_dict()})
        response = await read_framed_json(reader)
        if response.get("type") != "coordinator.event_recorded":
            raise WorkerTransportError("coordinator rejected a job event")

    async def _upload_artifact(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, descriptor: ArtifactDescriptor, path: Path) -> None:
        chunk_size = 1024 * 1024
        await write_framed_json(writer, {"type": "worker.artifact_begin", "artifact": descriptor.to_dict(), "chunk_size": chunk_size})
        await read_framed_json(reader)
        with path.open("rb") as handle:
            index = 0
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                await write_framed_json(
                    writer,
                    {
                        "type": "worker.artifact_chunk",
                        "artifact_id": descriptor.artifact_id,
                        "index": index,
                        "sha256": sha256(chunk).hexdigest(),
                        "content_base64": base64.b64encode(chunk).decode("ascii"),
                    },
                )
                await read_framed_json(reader)
                index += 1
        await write_framed_json(writer, {"type": "worker.artifact_finalize", "artifact_id": descriptor.artifact_id})
        verified = await read_framed_json(reader)
        if verified.get("type") != "coordinator.artifact_verified" or verified.get("sha256") != descriptor.sha256:
            raise WorkerTransportError("coordinator artifact verification failed")


class WorkerLocalGrantProxy:
    """Loopback-only proxy that keeps the device mTLS identity outside Job sandboxes."""

    def __init__(self, *, upstream_url: str, worker_root: str | Path, binding: Mapping[str, str]):
        self.upstream_url = upstream_url
        self.worker_root = Path(worker_root).expanduser().resolve()
        self.binding = {str(key): str(value) for key, value in binding.items()}
        self.server: http.server.ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if not self.server:
            raise WorkerTransportError("Worker-local Model Grant proxy is not running")
        # Match the canonical host accepted by macOS SBPL network filters. The
        # server remains bound to IPv4 loopback only; `localhost` is the URL
        # spelling exposed to the sandboxed job.
        return f"http://localhost:{self.server.server_address[1]}/invoke"

    def start(self) -> None:
        parsed = urllib.parse.urlparse(self.upstream_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise WorkerTransportError("Model Grant upstream must use HTTPS")
        identity = self.worker_root / "identity"
        context = ssl.create_default_context(cafile=str(identity / "coordinator-ca.pem"))
        context.load_cert_chain(str(identity / "device-certificate.pem"), str(identity / "device-key.pem"))
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.maximum_version = ssl.TLSVersion.TLSv1_3
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/invoke":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("content-length") or 0)
                    if length < 2 or length > 512 * 1024:
                        raise ValueError("request size is invalid")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or any(str(payload.get(key) or "") != value for key, value in owner.binding.items()):
                        raise ValueError("Model Grant binding mismatch")
                    upstream = urllib.request.Request(
                        owner.upstream_url,
                        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                        headers={"content-type": "application/json", "accept": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(upstream, timeout=60, context=context) as response:
                        body = response.read(2 * 1024 * 1024)
                        status = response.status
                        content_type = response.headers.get("content-type", "application/json")
                except urllib.error.HTTPError as exc:
                    body = exc.read(2 * 1024 * 1024)
                    status = exc.code
                    content_type = exc.headers.get("content-type", "application/json")
                except Exception:
                    body = b'{"detail":{"code":"worker_model_proxy_rejected","category":"security"}}'
                    status = 403
                    content_type = "application/json"
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="across-model-grant-proxy", daemon=True)
        self.thread.start()

    def close(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2)
        self.server = None
        self.thread = None


class RelayCoordinatorSession:
    """One durable-job exchange over an opaque end-to-end encrypted Relay channel."""

    def __init__(
        self,
        coordinator: WorkerCoordinator,
        endpoint: RelayEndpoint,
        *,
        artifact_root: str | Path,
        model_gateway: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None = None,
    ):
        self.coordinator = coordinator
        self.endpoint = endpoint
        self.artifacts = ChunkedArtifactReceiver(artifact_root)
        self.model_gateway = model_gateway

    async def run_once(self) -> str:
        node_id: str | None = None
        disconnect_reason = "session_closed"
        try:
            hello = await self.endpoint.receive()
            if hello.get("type") != "worker.hello":
                raise WorkerTransportError("relay Worker hello is required")
            capability = CapabilityManifest.from_dict(dict(hello.get("capability_manifest") or {}))
            node_id = capability.node_id
            if node_id != self.endpoint.peer_node_id:
                raise WorkerTransportError("relay Worker identity does not match the encrypted peer binding")
            self.coordinator.connect_node(
                capability,
                transport="relay",
                identity_generation=int(hello.get("identity_generation") or 1),
                peer_node_id=self.endpoint.peer_node_id,
            )
            await self.endpoint.send({"type": "coordinator.ready", "protocol_version": "across-worker-session/1.0", "node_id": node_id, "transport": "relay", "end_to_end_encryption": "ChaCha20-Poly1305"})
            while True:
                request = await self.endpoint.receive()
                kind = request.get("type")
                if kind == "worker.heartbeat":
                    node = self.coordinator.heartbeat_node(node_id, current_load=float(request.get("current_load") or 0))
                    await self.endpoint.send({"type": "coordinator.heartbeat", "node": node})
                elif kind == "worker.lease_heartbeat":
                    control = self.coordinator.lease_control(
                        str(request.get("lease_id") or ""),
                        node_id=node_id,
                        attempt=int(request.get("attempt") or 0),
                    )
                    self.coordinator.heartbeat_node(node_id, current_load=float(request.get("current_load") or 1.0))
                    await self.endpoint.send({"type": "coordinator.lease_status", **control})
                elif kind == "worker.lease_request":
                    lease = self.coordinator.lease_next(node_id)
                    if lease is None:
                        await self.endpoint.send({
                            "type": "coordinator.no_job",
                            "update_directive": self.coordinator.node_update_directive(node_id),
                            "transport_directive": self.coordinator.node_transport_directive(node_id),
                        })
                    else:
                        job = self.coordinator.worker_job_payload(lease.job_id)
                        manifest = JobManifest.from_dict(job["manifest"])
                        model_grant = None
                        if manifest.model_policy.get("enabled"):
                            if self.model_gateway is None:
                                raise WorkerTransportError("relay live-model job requires a host model gateway")
                            policy = manifest.model_policy
                            model_grant = self.coordinator.issue_model_grant(
                                job_id=manifest.job_id,
                                node_id=node_id,
                                purposes=("scenario_round_annotation",),
                                model_policy=str(policy.get("policy") or "host-default"),
                                max_calls=int(policy.get("max_calls") or 1),
                                max_tokens=int(policy.get("max_tokens") or 1),
                                max_concurrency=int(policy.get("max_concurrency") or 1),
                                max_cost_usd=float(policy.get("max_cost_usd") or 0),
                                ttl_seconds=_model_grant_ttl_seconds(
                                    policy,
                                    manifest.budgets.get("timeout_seconds"),
                                ),
                            ).to_dict()
                        await self.endpoint.send({
                            "type": "coordinator.job",
                            "lease": lease.to_dict(),
                            "manifest": manifest.to_dict(),
                            "inputs_base64": job["inputs_base64"],
                            "model_grant": model_grant,
                        })
                elif kind == "worker.lease_ack":
                    lease = self.coordinator.acknowledge_lease(str(request.get("lease_id") or ""), str(request.get("manifest_hash") or ""))
                    await self.endpoint.send({"type": "coordinator.lease_acknowledged", "lease": lease.to_dict()})
                elif kind == "worker.event":
                    event = self.coordinator.record_event(_event_from_dict(dict(request.get("event") or {})))
                    await self.endpoint.send({"type": "coordinator.event_recorded", "event": event})
                elif kind == "worker.artifact_begin":
                    descriptor = _artifact_from_dict(dict(request.get("artifact") or {}))
                    state = self.artifacts.begin(descriptor, chunk_size=int(request.get("chunk_size") or 1024 * 1024))
                    await self.endpoint.send({"type": "coordinator.artifact_ready", "state": state})
                elif kind == "worker.artifact_chunk":
                    content = base64.b64decode(str(request.get("content_base64") or ""), validate=True)
                    state = self.artifacts.write_chunk(str(request.get("artifact_id") or ""), int(request.get("index") or 0), content, str(request.get("sha256") or ""))
                    await self.endpoint.send({"type": "coordinator.artifact_chunk", "state": state})
                elif kind == "worker.artifact_finalize":
                    path = self.artifacts.finalize(str(request.get("artifact_id") or ""))
                    await self.endpoint.send({"type": "coordinator.artifact_verified", "name": path.name, "sha256": _file_hash(path), "size": path.stat().st_size})
                elif kind == "worker.model_invoke":
                    if self.model_gateway is None:
                        raise WorkerTransportError("relay model gateway is unavailable")
                    payload = request.get("request")
                    if not isinstance(payload, Mapping) or str(payload.get("node_id") or "") != node_id:
                        raise WorkerTransportError("relay model request identity mismatch")
                    result = await self.model_gateway(dict(payload))
                    await self.endpoint.send({"type": "coordinator.model_result", **dict(result)})
                elif kind == "worker.update_result":
                    node = self.coordinator.record_node_update(
                        node_id,
                        directive_id=str(request.get("directive_id") or ""),
                        status=str(request.get("status") or ""),
                        error=str(request.get("error") or "") or None,
                    )
                    await self.endpoint.send({"type": "coordinator.update_recorded", "node": node})
                elif kind == "worker.transport_result":
                    node = self.coordinator.record_node_transport(
                        node_id,
                        directive_id=str(request.get("directive_id") or ""),
                        status=str(request.get("status") or ""),
                        error=str(request.get("error") or "") or None,
                    )
                    await self.endpoint.send({"type": "coordinator.transport_recorded", "node": node})
                elif kind == "worker.goodbye":
                    await self.endpoint.send({"type": "coordinator.goodbye", "node_id": node_id})
                    disconnect_reason = "worker_goodbye"
                    return node_id
                else:
                    raise WorkerTransportError("unsupported relay Worker session message")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            disconnect_reason = "connection_lost"
            raise
        except (CoordinatorError, ProtocolError, WorkerTransportError, ValueError):
            disconnect_reason = "session_rejected"
            raise
        finally:
            if node_id:
                try:
                    self.coordinator.disconnect_node(node_id, reason=disconnect_reason)
                except CoordinatorError:
                    pass


class RelayWorkerSessionClient:
    def __init__(self, *, endpoint: RelayEndpoint, capability: CapabilityManifest, worker_root: str | Path, identity_generation: int = 1):
        self.endpoint = endpoint
        self.capability = capability
        self.worker_root = Path(worker_root).expanduser().resolve()
        self.identity_generation = int(identity_generation)
        self._wire_lock: asyncio.Lock | None = None

    async def run_once(self) -> WorkerSessionResult:
        self._wire_lock = asyncio.Lock()
        ready = await self._request({"type": "worker.hello", "capability_manifest": self.capability.to_dict(), "identity_generation": self.identity_generation})
        if ready.get("type") != "coordinator.ready" or ready.get("transport") != "relay":
            raise WorkerTransportError("relay Coordinator did not accept the Worker session")
        response = await self._request({"type": "worker.lease_request", "node_id": self.capability.node_id})
        if response.get("type") == "coordinator.no_job":
            if isinstance(response.get("update_directive"), Mapping):
                status = await self._apply_update(dict(response["update_directive"]))
                return WorkerSessionResult(status=status, transport="relay", tls_version="TLSv1.3")
            if isinstance(response.get("transport_directive"), Mapping):
                status = await self._apply_transport(dict(response["transport_directive"]))
                return WorkerSessionResult(status=status, transport="relay", tls_version="TLSv1.3")
            await self._request({"type": "worker.goodbye"})
            return WorkerSessionResult(status="idle", transport="relay", tls_version="TLSv1.3")
        if response.get("type") != "coordinator.job":
            raise WorkerTransportError("relay Coordinator returned an invalid lease")
        lease = _lease_from_dict(dict(response.get("lease") or {}))
        manifest = JobManifest.from_dict(dict(response.get("manifest") or {}))
        if lease.node_id != self.capability.node_id or lease.manifest_hash != manifest.manifest_hash:
            raise WorkerTransportError("relay lease and manifest binding mismatch")
        if (await self._request({"type": "worker.lease_ack", "lease_id": lease.lease_id, "manifest_hash": manifest.manifest_hash})).get("type") != "coordinator.lease_acknowledged":
            raise WorkerTransportError("relay Coordinator did not acknowledge the manifest hash")
        await self._event(lease, "preparing", 1)
        inputs = {str(name): base64.b64decode(str(value), validate=True) for name, value in dict(response.get("inputs_base64") or {}).items()}
        model_grant = response.get("model_grant") if isinstance(response.get("model_grant"), Mapping) else None
        extra_env = {"ACROSS_TRANSPORT": "relay"}
        proxy = None
        if model_grant:
            extra_env.update({
                "ACROSS_MODEL_GRANT_ID": str(model_grant.get("grant_id") or ""),
                "ACROSS_MODEL_TIMEOUT_SECONDS": str(manifest.model_policy.get("timeout_seconds") or 30),
                "ACROSS_MODEL_MAX_TOKENS": str(
                    max(1, int(manifest.model_policy.get("max_tokens") or 1) // max(1, int(manifest.model_policy.get("max_calls") or 1)))
                ),
            })
            proxy = RelayWorkerLocalGrantProxy(
                invoke=self._invoke_model,
                binding={
                    "grant_id": str(model_grant.get("grant_id") or ""),
                    "run_id": manifest.run_id,
                    "job_id": manifest.job_id,
                    "node_id": self.capability.node_id,
                    "purpose": "scenario_round_annotation",
                },
            )
            proxy.start()
            extra_env["ACROSS_MODEL_GATEWAY_URL"] = proxy.url
        cancel_event = threading.Event()
        started_event = threading.Event()
        execution_done = asyncio.Event()
        await self._poll_lease_control(lease, cancel_event)
        executor = BoundedProcessExecutor(self.worker_root)
        try:
            execution_task = asyncio.create_task(
                asyncio.to_thread(
                    executor.execute,
                    manifest,
                    lease,
                    cancel_event=cancel_event,
                    started_event=started_event,
                    extra_env=extra_env,
                    input_payloads=inputs,
                )
            )
            while not started_event.is_set() and not execution_task.done():
                await asyncio.sleep(0.01)
            if started_event.is_set():
                await self._event(lease, "running", 2)
            control_task = asyncio.create_task(self._maintain_lease(lease, cancel_event, execution_done))
            try:
                execution = await execution_task
            finally:
                execution_done.set()
                await control_task
        finally:
            if proxy:
                proxy.close()
        sequence = 3
        for artifact in execution.artifacts:
            await self._upload(artifact, execution.sandbox / "output" / artifact.logical_name)
            sequence += 1
        cleanup_status = execution.cleanup_status
        if int(manifest.cleanup_policy.get("retention_seconds") or 0) <= 0:
            try:
                executor.cleanup(run_id=manifest.run_id, job_id=manifest.job_id, attempt=lease.attempt)
                cleanup_status = "complete"
            except (OSError, CoordinatorError, ProtocolError):
                cleanup_status = "failed"
        execution.cleanup_status = cleanup_status
        terminal_state = "failed" if cleanup_status == "failed" and execution.state == "completed" else execution.state
        terminal_reason = "cleanup_failed" if cleanup_status == "failed" and execution.state == "completed" else execution.reason_category
        receipt = build_evidence_receipt(
            manifest=manifest,
            node=self.capability,
            lease=lease,
            terminal_state=terminal_state,
            artifacts=execution.artifacts,
            quality_gates={},
            model_usage={},
            cleanup_status=cleanup_status,
            started_at=execution.started_at,
            ended_at=execution.ended_at,
            resource_usage=execution.resource_usage,
        )
        await self._event(
            lease,
            terminal_state,
            sequence,
            reason_category=terminal_reason,
            payload={
                "artifacts": [item.to_dict() for item in execution.artifacts],
                "exit_code": execution.exit_code,
                "cleanup_status": cleanup_status,
                "resource_usage": execution.resource_usage,
                "evidence_receipt": receipt,
            },
        )
        await self._request({"type": "worker.goodbye"})
        return WorkerSessionResult(status=terminal_state, transport="relay", tls_version="TLSv1.3", job_id=manifest.job_id, execution=execution)

    async def _maintain_lease(
        self,
        lease: JobLease,
        cancel_event: threading.Event,
        execution_done: asyncio.Event,
    ) -> None:
        interval = max(0.1, min(float(lease.heartbeat_interval_seconds), 0.25))
        while not execution_done.is_set():
            try:
                await asyncio.wait_for(execution_done.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            await self._poll_lease_control(lease, cancel_event)

    async def _poll_lease_control(self, lease: JobLease, cancel_event: threading.Event) -> None:
        response = await self._request({
                "type": "worker.lease_heartbeat",
                "lease_id": lease.lease_id,
                "attempt": lease.attempt,
                "current_load": 1.0,
            })
        if response.get("type") != "coordinator.lease_status":
            raise WorkerTransportError("relay Coordinator rejected the lease heartbeat")
        if response.get("cancel_requested"):
            cancel_event.set()

    async def _event(self, lease: JobLease, state: str, sequence: int, *, reason_category: str | None = None, payload: Mapping[str, Any] | None = None) -> None:
        event = JobEvent(event_id=new_protocol_id("event"), job_id=lease.job_id, run_id=lease.run_id, node_id=lease.node_id, lease_id=lease.lease_id, attempt=lease.attempt, sequence=sequence, state=state, reason_category=reason_category, payload=dict(payload or {}))
        if (await self._request({"type": "worker.event", "event": event.to_dict()})).get("type") != "coordinator.event_recorded":
            raise WorkerTransportError("relay Coordinator rejected a job event")

    async def _upload(self, descriptor: ArtifactDescriptor, path: Path) -> None:
        chunk_size = 1024 * 1024
        await self._request({"type": "worker.artifact_begin", "artifact": descriptor.to_dict(), "chunk_size": chunk_size})
        with path.open("rb") as handle:
            for index, chunk in enumerate(iter(lambda: handle.read(chunk_size), b"")):
                await self._request({"type": "worker.artifact_chunk", "artifact_id": descriptor.artifact_id, "index": index, "sha256": sha256(chunk).hexdigest(), "content_base64": base64.b64encode(chunk).decode("ascii")})
        verified = await self._request({"type": "worker.artifact_finalize", "artifact_id": descriptor.artifact_id})
        if verified.get("type") != "coordinator.artifact_verified" or verified.get("sha256") != descriptor.sha256:
            raise WorkerTransportError("relay Coordinator artifact verification failed")

    async def _request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._wire_lock is None:
            self._wire_lock = asyncio.Lock()
        async with self._wire_lock:
            await self.endpoint.send(dict(payload))
            return await self.endpoint.receive()

    async def _invoke_model(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        response = await self._request({"type": "worker.model_invoke", "request": dict(payload)})
        if response.get("type") != "coordinator.model_result":
            raise WorkerTransportError("relay Coordinator returned an invalid model result")
        return response

    async def _apply_update(self, directive: Mapping[str, Any]) -> str:
        directive_id = str(directive.get("directive_id") or "")
        await self._request({"type": "worker.update_result", "directive_id": directive_id, "status": "downloading"})
        try:
            from .worker_cli import update_worker_from_url

            await asyncio.to_thread(
                update_worker_from_url,
                self.worker_root,
                url=str(directive.get("url") or ""),
                expected_sha256=str(directive.get("sha256") or ""),
                version=str(directive.get("version") or ""),
                restart_service=False,
            )
        except Exception as exc:
            await self._request({"type": "worker.update_result", "directive_id": directive_id, "status": "failed", "error": type(exc).__name__})
            return "update_failed"
        await self._request({"type": "worker.update_result", "directive_id": directive_id, "status": "completed"})
        return "updated"

    async def _apply_transport(self, directive: Mapping[str, Any]) -> str:
        directive_id = str(directive.get("directive_id") or "")
        await self._request({"type": "worker.transport_result", "directive_id": directive_id, "status": "applying"})
        try:
            from .worker_cli import apply_transport_directive

            await asyncio.to_thread(apply_transport_directive, self.worker_root, directive=directive)
        except Exception as exc:
            await self._request({"type": "worker.transport_result", "directive_id": directive_id, "status": "failed", "error": type(exc).__name__})
            return "transport_switch_failed"
        await self._request({"type": "worker.transport_result", "directive_id": directive_id, "status": "completed"})
        return "transport_switched"


class RelayWorkerLocalGrantProxy:
    """Loopback proxy whose upstream is the encrypted Relay session."""

    def __init__(self, *, invoke: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]], binding: Mapping[str, str]):
        self.invoke = invoke
        self.binding = {str(key): str(value) for key, value in binding.items()}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server: http.server.ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if not self.server:
            raise WorkerTransportError("relay Worker model proxy is not running")
        return f"http://localhost:{self.server.server_address[1]}/invoke"

    def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                status: int
                body: bytes
                try:
                    length = int(self.headers.get("content-length") or 0)
                    if self.path != "/invoke" or length < 2 or length > 512 * 1024:
                        raise ValueError("request rejected")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or any(str(payload.get(key) or "") != value for key, value in owner.binding.items()):
                        raise ValueError("Model Grant binding mismatch")
                    future = asyncio.run_coroutine_threadsafe(owner.invoke(payload), owner.loop)
                    result = dict(future.result(timeout=65))
                    status = int(result.get("status_code") or 200)
                    body = base64.b64decode(str(result.get("body_base64") or ""), validate=True)
                    if len(body) > 2 * 1024 * 1024:
                        raise ValueError("model response too large")
                except Exception:
                    status = 403
                    body = b'{"detail":{"code":"worker_model_proxy_rejected","category":"security"}}'
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="across-relay-model-proxy", daemon=True)
        self.thread.start()

    def close(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2)
        self.server = None
        self.thread = None
        self.loop = None


class UnixSocketModelGateway:
    """Coordinator-only adapter to AAA's permission-protected Unix socket."""

    def __init__(self, socket_path: str | Path):
        self.socket_path = Path(socket_path).expanduser().resolve()

    async def __call__(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._invoke, dict(payload))

    def _invoke(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self.socket_path.is_socket():
            raise WorkerTransportError("AAA model gateway socket is unavailable")

        class Connection(http.client.HTTPConnection):
            def connect(connection_self) -> None:
                connection_self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection_self.sock.connect(str(self.socket_path))

        connection = Connection("localhost", timeout=65)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            connection.request(
                "POST",
                "/api/worker-control/model-gateway/invoke",
                body=body,
                headers={"content-type": "application/json", "content-length": str(len(body))},
            )
            response = connection.getresponse()
            response_body = response.read(2 * 1024 * 1024 + 1)
            if len(response_body) > 2 * 1024 * 1024:
                raise WorkerTransportError("AAA model gateway response is too large")
            return {
                "status_code": int(response.status),
                "body_base64": base64.b64encode(response_body).decode("ascii"),
            }
        finally:
            connection.close()


def _lease_from_dict(value: Mapping[str, Any]) -> JobLease:
    return JobLease(**dict(value))


def _event_from_dict(value: Mapping[str, Any]) -> JobEvent:
    return JobEvent(**dict(value))


def _artifact_from_dict(value: Mapping[str, Any]) -> ArtifactDescriptor:
    values = dict(value)
    values["chunks"] = tuple(values.get("chunks") or ())
    return ArtifactDescriptor(**values)


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tls_peer_identity(writer: asyncio.StreamWriter) -> dict[str, str]:
    ssl_object = writer.get_extra_info("ssl_object")
    if ssl_object is None:
        raise WorkerTransportError("Worker session has no TLS peer")
    encoded = ssl_object.getpeercert(binary_form=True)
    if not encoded:
        raise WorkerTransportError("Worker TLS peer certificate is unavailable")
    certificate = x509.load_der_x509_certificate(encoded)
    candidates: set[str] = set()
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        prefix = "spiffe://across.local/worker/"
        candidates.update(
            value[len(prefix) :]
            for value in san.get_values_for_type(x509.UniformResourceIdentifier)
            if value.startswith(prefix) and value[len(prefix) :]
        )
    except x509.ExtensionNotFound:
        pass
    candidates.update(
        value.value
        for value in certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if value.value
    )
    if len(candidates) != 1:
        raise WorkerTransportError("Worker TLS certificate has an ambiguous node identity")
    return {
        "node_id": next(iter(candidates)),
        "certificate_fingerprint": certificate.fingerprint(hashes.SHA256()).hex(),
    }
