from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import asyncio
import base64
import json
import os
import stat

from .coordinator import WorkerCoordinator
from .worker_protocol import CapabilityManifest, JobEvent, JobManifest, ProtocolError


COMMAND_SCHEMA = "across-worker-control-command/1.0"
MAX_CONTROL_MESSAGE_BYTES = 64 * 1024 * 1024


async def serve_worker_control(socket_path: str | Path, coordinator: WorkerCoordinator | None = None) -> None:
    """Serve the public Worker control protocol over a private local socket."""
    path = Path(socket_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not stat.S_ISSOCK(path.stat().st_mode):
            raise RuntimeError("worker control socket path is not a socket")
        path.unlink()
    runtime = coordinator or WorkerCoordinator()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=30)
            if len(raw) > MAX_CONTROL_MESSAGE_BYTES:
                raise ValueError("worker control request is too large")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("worker control request must be an object")
            response = handle_worker_control_command(request, runtime)
        except Exception:
            response = {"status": "error", "error": "worker_control_request_rejected"}
        encoded = json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        writer.write(encoded)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(path), limit=MAX_CONTROL_MESSAGE_BYTES + 1)
    os.chmod(path, 0o600)
    try:
        async with server:
            await server.serve_forever()
    finally:
        server.close()
        await server.wait_closed()
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def handle_worker_control_command(request: Mapping[str, Any], coordinator: WorkerCoordinator | None = None) -> dict[str, Any]:
    if request.get("schema_version") != COMMAND_SCHEMA:
        raise ProtocolError("unsupported worker control command schema")
    action = str(request.get("action") or "")
    payload = request.get("payload") or {}
    if not isinstance(payload, Mapping):
        raise ProtocolError("worker control payload must be an object")
    runtime = coordinator or WorkerCoordinator()

    if action == "snapshot":
        return {"schema_version": "across-worker-control-snapshot/1.0", "nodes": runtime.list_nodes(), "jobs": [runtime.job(str(item["job_id"])) for item in runtime.store.list("jobs")]}
    if action == "node.import_approved":
        manifest = CapabilityManifest.from_dict(_object(payload, "capability_manifest"))
        return runtime.import_approved_node(
            manifest,
            display_name=str(payload.get("display_name") or manifest.node_id),
            fingerprint=str(payload.get("certificate_fingerprint") or payload.get("fingerprint") or ""),
            session_generation=int(payload.get("session_generation") or 1),
            identity_expires_at=float(payload.get("identity_expires_at") or 0) or None,
        )
    if action == "node.connect":
        manifest = CapabilityManifest.from_dict(_object(payload, "capability_manifest"))
        return runtime.connect_node(manifest, transport=str(payload.get("transport") or ""), identity_generation=int(payload.get("session_generation") or 1))
    if action == "node.heartbeat":
        return runtime.heartbeat_node(str(payload.get("node_id") or ""), current_load=float(payload.get("current_load") or 0))
    if action == "node.drain":
        return runtime.set_draining(str(payload.get("node_id") or ""), bool(payload.get("draining", True)))
    if action == "node.request_update":
        return runtime.request_node_update(
            str(payload.get("node_id") or ""),
            version=str(payload.get("version") or ""),
            url=str(payload.get("url") or ""),
            sha256_value=str(payload.get("sha256") or ""),
        )
    if action == "node.update_status":
        return runtime.record_node_update(
            str(payload.get("node_id") or ""),
            directive_id=str(payload.get("directive_id") or ""),
            status=str(payload.get("status") or ""),
            error=str(payload.get("error") or "") or None,
        )
    if action == "node.request_transport":
        return runtime.request_node_transport(
            str(payload.get("node_id") or ""),
            transport=str(payload.get("transport") or ""),
            endpoint=str(payload.get("endpoint") or ""),
            server_name=str(payload.get("server_name") or "") or None,
            relay_session_id=str(payload.get("relay_session_id") or "") or None,
            relay_peer_node_id=str(payload.get("relay_peer_node_id") or "") or None,
            relay_session_key=str(payload.get("relay_session_key") or "") or None,
        )
    if action == "node.transport_status":
        return runtime.record_node_transport(
            str(payload.get("node_id") or ""),
            directive_id=str(payload.get("directive_id") or ""),
            status=str(payload.get("status") or ""),
            error=str(payload.get("error") or "") or None,
        )
    if action == "node.revoke":
        return runtime.revoke_node(str(payload.get("node_id") or ""), reason=str(payload.get("reason") or "host_revoked"))
    if action == "node.delete":
        return runtime.delete_node(str(payload.get("node_id") or ""))
    if action == "job.submit":
        inputs = {
            str(name): base64.b64decode(str(value), validate=True)
            for name, value in dict(payload.get("inputs_base64") or {}).items()
        }
        return runtime.submit_job(JobManifest.from_dict(_object(payload, "manifest")), input_payloads=inputs)
    if action == "job.get":
        return runtime.job(str(payload.get("job_id") or ""))
    if action == "job.lease_next":
        lease = runtime.lease_next(str(payload.get("node_id") or ""))
        return {"lease": lease.to_dict() if lease else None}
    if action == "job.acknowledge":
        return runtime.acknowledge_lease(str(payload.get("lease_id") or ""), str(payload.get("manifest_hash") or "")).to_dict()
    if action == "job.heartbeat":
        return runtime.heartbeat_lease(str(payload.get("lease_id") or ""), node_id=str(payload.get("node_id") or ""), attempt=int(payload.get("attempt") or 0)).to_dict()
    if action == "job.event":
        event = JobEvent(**_object(payload, "event"))
        return runtime.record_event(event)
    if action == "job.cancel":
        return runtime.cancel_job(str(payload.get("job_id") or ""), reason=str(payload.get("reason") or "user_cancelled"))
    if action == "job.recover":
        return {"recovered_job_ids": runtime.recover_expired_leases()}
    if action == "model_grant.issue":
        grant = runtime.issue_model_grant(
            job_id=str(payload.get("job_id") or ""),
            node_id=str(payload.get("node_id") or ""),
            audience=str(payload.get("audience") or "aaa-model-gateway"),
            scopes=tuple(map(str, payload.get("scopes") or ("model.invoke",))),
            purposes=tuple(map(str, payload.get("purposes") or ("workflow",))),
            model_policy=str(payload.get("model_policy") or "host-default"),
            max_calls=int(payload.get("max_calls") or 1),
            max_tokens=int(payload.get("max_tokens") or 1024),
            max_concurrency=int(payload.get("max_concurrency") or 1),
            max_cost_usd=float(payload.get("max_cost_usd") or 0),
            ttl_seconds=int(payload.get("ttl_seconds") or 300),
        )
        return grant.to_dict()
    if action == "model_grant.consume":
        return runtime.consume_model_grant(
            str(payload.get("grant_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            job_id=str(payload.get("job_id") or ""),
            node_id=str(payload.get("node_id") or ""),
            audience=str(payload.get("audience") or ""),
            scope=str(payload.get("scope") or ""),
            purpose=str(payload.get("purpose") or ""),
            tokens=int(payload.get("tokens") or 0),
            cost_usd=float(payload.get("cost_usd") or 0),
        )
    if action == "model_grant.begin":
        return runtime.begin_model_grant_call(
            str(payload.get("grant_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            job_id=str(payload.get("job_id") or ""),
            node_id=str(payload.get("node_id") or ""),
            audience=str(payload.get("audience") or ""),
            scope=str(payload.get("scope") or ""),
            purpose=str(payload.get("purpose") or ""),
            requested_tokens=int(payload.get("requested_tokens") or 0),
        )
    if action == "model_grant.finish":
        return runtime.finish_model_grant_call(
            str(payload.get("grant_id") or ""),
            str(payload.get("call_id") or ""),
            tokens=int(payload.get("tokens") or 0),
            cost_usd=float(payload.get("cost_usd") or 0),
            outcome=str(payload.get("outcome") or "completed"),
        )
    if action == "model_grant.revoke":
        return runtime.revoke_model_grant(str(payload.get("grant_id") or ""))
    raise ProtocolError("unsupported worker control action")


def _object(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{key} must be an object")
    return dict(value)
