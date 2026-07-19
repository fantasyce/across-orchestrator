from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence
import argparse
import base64
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tarfile
import asyncio
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .relay import AsyncRelayServer, RelayEndpoint, RelayRouter, create_tls_context
from .worker_protocol import canonical_json, payload_hash, sanitize_public
from .worker_runtime import WORKER_VERSION, probe_capabilities, worker_home
from .worker_transport import RelayWorkerSessionClient, WorkerSessionClient, tls_client_context


IDENTITY_RENEWAL_WINDOW_SECONDS = 7 * 24 * 60 * 60


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="across-worker", description="Across cross-platform Worker Runtime")
    parser.add_argument("--home", help="isolated worker data root")
    parser.add_argument("--json", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install", help="install the user-level worker service")
    install.add_argument("--no-start", action="store_true")
    install.add_argument("--label", default="com.across.worker")

    join = subparsers.add_parser("join", help="create a device identity and pairing request")
    join.add_argument("--pairing-code", required=True)
    join.add_argument("--enrollment-id", required=True)
    join.add_argument("--display-name", default=platform.node() or "Across Worker")
    join.add_argument("--node-id")
    join.add_argument("--endpoint", required=True)
    join.add_argument("--transport", choices=("direct", "overlay", "relay"), required=True)
    join.add_argument("--server-name")
    join.add_argument("--ca-file")
    join.add_argument("--certificate")
    join.add_argument("--private-key")
    join.add_argument("--enrollment-endpoint")

    activate = subparsers.add_parser("activate", help="activate a host-approved short-lived device identity")
    activate.add_argument("--approval-file", required=True)

    run = subparsers.add_parser("run", help="run the worker session loop")
    run.add_argument("--once", action="store_true", help="attempt one session and exit")
    run.add_argument("--poll-seconds", type=float, default=2.0)
    subparsers.add_parser("status", help="show worker service and identity status")
    subparsers.add_parser("drain", help="stop accepting new jobs")
    subparsers.add_parser("leave", help="disconnect and remove the active device identity")

    update = subparsers.add_parser("update", help="stage and activate a verified worker artifact")
    update.add_argument("--artifact", required=True)
    update.add_argument("--sha256", required=True)
    update.add_argument("--version", required=True)
    subparsers.add_parser("rollback", help="atomically reactivate the previous verified Worker runtime")

    cleanup = subparsers.add_parser("cleanup", help="inspect or remove one managed job sandbox")
    cleanup.add_argument("--run-id", required=True)
    cleanup.add_argument("--job-id", required=True)
    cleanup.add_argument("--attempt", required=True, type=int)
    cleanup.add_argument("--dry-run", action="store_true")

    uninstall = subparsers.add_parser("uninstall", help="remove the user service")
    uninstall.add_argument("--purge", action="store_true")
    uninstall.add_argument("--yes", action="store_true")
    uninstall.add_argument("--label", default="com.across.worker")
    pack = subparsers.add_parser("pack", help="manage verified Worker workflow packs")
    pack_subparsers = pack.add_subparsers(dest="pack_command", required=True)
    pack_install = pack_subparsers.add_parser("install")
    pack_install.add_argument("--artifact", required=True)
    pack_install.add_argument("--sha256", required=True)
    pack_subparsers.add_parser("list")
    pack_remove = pack_subparsers.add_parser("remove")
    pack_remove.add_argument("--pack-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.home).expanduser().resolve() if args.home else worker_home()
    try:
        if args.command == "install":
            result = install_worker(root, start=not args.no_start, label=args.label)
        elif args.command == "join":
            result = enroll_worker(
                root,
                pairing_code=args.pairing_code,
                enrollment_id=args.enrollment_id,
                display_name=args.display_name,
                node_id=args.node_id,
                endpoint=args.endpoint,
                transport=args.transport,
                server_name=args.server_name,
                ca_file=args.ca_file,
                certificate=args.certificate,
                private_key=args.private_key,
                enrollment_endpoint=args.enrollment_endpoint,
            )
        elif args.command == "run":
            result = run_worker(root, once=args.once, poll_seconds=args.poll_seconds)
        elif args.command == "activate":
            result = activate_worker(root, approval_file=args.approval_file)
        elif args.command == "status":
            result = worker_status(root)
        elif args.command == "drain":
            result = set_drain(root, True)
        elif args.command == "leave":
            result = leave_worker(root)
        elif args.command == "update":
            result = update_worker(root, artifact=args.artifact, expected_sha256=args.sha256, version=args.version)
        elif args.command == "rollback":
            result = rollback_worker(root)
        elif args.command == "cleanup":
            from .worker_runtime import BoundedProcessExecutor

            removed = BoundedProcessExecutor(root).cleanup(
                run_id=args.run_id,
                job_id=args.job_id,
                attempt=args.attempt,
                dry_run=args.dry_run,
            )
            result = {"status": "planned" if args.dry_run else "cleaned", "targets": removed, "dry_run": args.dry_run}
        elif args.command == "uninstall":
            if args.purge and not args.yes:
                raise ValueError("uninstall --purge requires --yes confirmation")
            result = uninstall_worker(root, purge=args.purge, label=args.label)
        elif args.command == "pack":
            if args.pack_command == "install":
                result = install_worker_pack(root, artifact=args.artifact, expected_sha256=args.sha256)
            elif args.pack_command == "list":
                result = {"status": "ok", "packs": list_worker_packs(root)}
            else:
                result = remove_worker_pack(root, pack_id=args.pack_id)
        else:
            raise ValueError("unsupported worker command")
        _print(result, json_output=args.json)
        if args.command == "run" and not args.once and result.get("status") == "updated":
            # The authenticated session has already acknowledged completion.
            # A non-zero service exit lets launchd/systemd load the activated
            # runtime without killing the acknowledgement in flight.
            return 75
        return 0
    except Exception as exc:
        _print({"status": "error", "error": str(sanitize_public(str(exc)))}, json_output=True, stream=sys.stderr)
        return 2


def relay_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="across-relay", description="Across opaque relay")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int, default=9444)
    parser.add_argument("--certificate")
    parser.add_argument("--private-key")
    parser.add_argument("--trust-store")
    parser.add_argument("--sessions", help="JSON file containing opaque participant session registrations")
    args = parser.parse_args(argv)
    router = RelayRouter()
    if args.health:
        print(canonical_json(router.health()))
        return 0
    if args.serve:
        required = {"host": args.host, "certificate": args.certificate, "private-key": args.private_key, "trust-store": args.trust_store, "sessions": args.sessions}
        missing = [name for name, value in required.items() if not value]
        if missing:
            parser.error(f"Relay serve requires: {', '.join(missing)}")
        registrations = _read_json(Path(args.sessions).expanduser().resolve(), default={})
        if registrations.get("schema_version") != "across-relay-sessions/1.0":
            parser.error("Relay session registration file has an incompatible schema")
        for record in registrations.get("sessions") or []:
            router.register_session(
                str(record.get("session_id") or ""),
                list(map(str, record.get("node_ids") or ())),
                ttl_seconds=int(record.get("ttl_seconds") or 300),
            )
        context = create_tls_context(
            server=True,
            certificate=args.certificate,
            private_key=args.private_key,
            trust_store=args.trust_store,
        )

        async def serve() -> None:
            server = AsyncRelayServer(router, host=args.host, port=args.port, ssl_context=context)
            await server.start()
            try:
                await asyncio.Event().wait()
            finally:
                await server.close()

        import asyncio

        asyncio.run(serve())
        return 0
    parser.error("network relay requires explicit TLS listener configuration")
    return 2


def install_worker(root: Path, *, start: bool, label: str) -> dict[str, Any]:
    root = _safe_root(root)
    for directory in ("bin", "identity", "state", "logs", "sandboxes", "versions", "runtime", "cache"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    runtime_target = _stage_builtin_runtime(root, WORKER_VERSION)
    _activate_runtime(root, runtime_target, version=WORKER_VERSION, sha256=payload_hash({"builtin": WORKER_VERSION}))
    launcher = root / "bin" / "across-worker"
    _atomic_text(
        launcher,
        "#!/bin/sh\n"
        f'RUNTIME="$(cd "{root}/runtime/current" 2>/dev/null && pwd -P)"\n'
        'if [ -z "$RUNTIME" ] || [ ! -d "$RUNTIME/src/across_orchestrator" ]; then\n'
        '  echo "Across Worker runtime is unavailable" >&2\n'
        '  exit 70\n'
        'fi\n'
        f'PYTHONPATH="$RUNTIME/src" exec "{sys.executable}" -m across_orchestrator.worker_cli --home "{root}" "$@"\n',
        mode=0o755,
    )
    service = _install_service(root, launcher=launcher, start=start, label=label)
    marker = {
        "schema_version": "across-worker-install/1.0",
        "version": WORKER_VERSION,
        "platform": platform.system().lower(),
        "architecture": platform.machine(),
        "installed_at": time.time(),
        "service": service,
        "root_hash": payload_hash({"root_name": root.name, "version": WORKER_VERSION}),
    }
    _atomic_json(root / "state" / "install.json", marker)
    return {"status": "installed", **marker, "home": str(root)}


def create_join_request(
    root: Path,
    *,
    pairing_code: str,
    enrollment_id: str,
    display_name: str,
    node_id: str | None,
    endpoint: str = "https://127.0.0.1:1",
    transport: str = "direct",
    server_name: str | None = None,
    ca_file: str | None = None,
    certificate: str | None = None,
    private_key: str | None = None,
    enrollment_endpoint: str | None = None,
) -> dict[str, Any]:
    root = _safe_root(root)
    tls_private_key_path = private_key
    identity = root / "identity"
    identity.mkdir(parents=True, exist_ok=True)
    private_path = identity / "device-key.pem"
    public_path = identity / "device-public.pem"
    if private_path.exists():
        private_key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ValueError("existing device key has unsupported type")
    else:
        private_key = Ed25519PrivateKey.generate()
        _atomic_bytes(
            private_path,
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            mode=0o600,
        )
    public_bytes = private_key.public_key().public_bytes(encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)
    _atomic_bytes(public_path, public_bytes, mode=0o644)
    fingerprint = payload_hash({"public_key": public_bytes.decode("ascii")})
    stable_node_id = node_id or f"node-{fingerprint[:20]}"
    capabilities = probe_capabilities(node_id=stable_node_id, home=root).to_dict()
    request = {
        "schema_version": "across-worker-join-request/1.0",
        "enrollment_id": enrollment_id,
        "pairing_code": pairing_code,
        "public_identity": {
            "node_id": stable_node_id,
            "display_name": display_name,
            "algorithm": "ed25519",
            "fingerprint": fingerprint,
            "public_key_pem": public_bytes.decode("ascii"),
        },
        "capability_summary": capabilities,
        "contains_private_key": False,
        "contains_provider_key": False,
    }
    _atomic_json(identity / "join-request.json", request)
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("worker endpoint must be a credential-free HTTPS URL")
    if transport not in {"direct", "overlay", "relay"}:
        raise ValueError("worker transport is unsupported")
    session = {
        "schema_version": "across-worker-session-config/1.0",
        "endpoint": endpoint,
        "transport": transport,
        "server_name": server_name or parsed.hostname,
        "ca_file": _optional_managed_path(ca_file, root),
        "certificate": _optional_managed_path(certificate, root),
        "private_key": _optional_managed_path(tls_private_key_path, root),
        "enrollment_endpoint": _validated_enrollment_endpoint(enrollment_endpoint),
    }
    _atomic_json(root / "state" / "session.json", session)
    _atomic_json(root / "state" / "node.json", {"node_id": stable_node_id, "state": "pending_approval", "display_name": display_name, "fingerprint": fingerprint})
    return sanitize_public({**request, "pairing_code": "[submitted]", "private_key_remains_on_worker": True})


def enroll_worker(
    root: Path,
    *,
    pairing_code: str,
    enrollment_id: str,
    display_name: str,
    node_id: str | None,
    endpoint: str,
    transport: str,
    server_name: str | None,
    ca_file: str | None,
    certificate: str | None,
    private_key: str | None,
    enrollment_endpoint: str | None,
) -> dict[str, Any]:
    result = create_join_request(
        root,
        pairing_code=pairing_code,
        enrollment_id=enrollment_id,
        display_name=display_name,
        node_id=node_id,
        endpoint=endpoint,
        transport=transport,
        server_name=server_name,
        ca_file=ca_file,
        certificate=certificate,
        private_key=private_key,
        enrollment_endpoint=enrollment_endpoint,
    )
    if not enrollment_endpoint:
        return result
    submitted = _submit_enrollment(root)
    activated = _poll_activation_once(root)
    return {
        **result,
        "submission": sanitize_public(submitted),
        "activation": sanitize_public(activated),
        "service_will_poll_for_approval": activated.get("status") == "pending_approval",
    }


def run_worker(root: Path, *, once: bool = False, poll_seconds: float = 2.0) -> dict[str, Any]:
    root = _safe_root(root)
    if poll_seconds < 0.1 or poll_seconds > 300:
        raise ValueError("worker poll interval must be between 0.1 and 300 seconds")
    state = _read_json(root / "state" / "node.json", default={})
    if not state:
        waiting = {"state": "awaiting_pairing", "process_state": "awaiting_pairing", "pid": os.getpid(), "last_heartbeat_at": time.time()}
        _atomic_json(root / "state" / "runtime.json", waiting)
        if once:
            return _runtime_summary(root, waiting, {}, status="awaiting_pairing")
        while not (root / "state" / "node.json").exists():
            waiting["last_heartbeat_at"] = time.time()
            _atomic_json(root / "state" / "runtime.json", waiting)
            time.sleep(poll_seconds)
        state = _read_json(root / "state" / "node.json", default={})
    if (root / "state" / "draining").exists():
        state["state"] = "draining"
    state["last_started_at"] = time.time()
    state["pid"] = os.getpid()
    state["process_state"] = "starting"
    _atomic_json(root / "state" / "runtime.json", state)
    session = _read_json(root / "state" / "session.json", default={})
    ready = all(session.get(key) for key in ("ca_file", "certificate", "private_key"))
    if once and not ready:
        return _runtime_summary(root, state, session, status="awaiting_approval")
    backoff = poll_seconds
    while True:
        state = _read_json(root / "state" / "node.json", default=state)
        session = _read_json(root / "state" / "session.json", default=session)
        ready = all(session.get(key) for key in ("ca_file", "certificate", "private_key"))
        if (root / "state" / "draining").exists():
            state.update({"state": "draining", "process_state": "draining", "last_heartbeat_at": time.time()})
            _atomic_json(root / "state" / "runtime.json", state)
            if once:
                return _runtime_summary(root, state, session, status="draining")
            time.sleep(poll_seconds)
            continue
        if not ready:
            if session.get("enrollment_endpoint"):
                try:
                    activation = _poll_activation_once(root)
                    if activation.get("status") == "activated":
                        state = _read_json(root / "state" / "node.json", default=state)
                        session = _read_json(root / "state" / "session.json", default=session)
                        ready = all(session.get(key) for key in ("ca_file", "certificate", "private_key"))
                except (ConnectionError, OSError, ValueError):
                    ready = False
            if ready:
                continue
            state.update({"process_state": "awaiting_approval", "last_heartbeat_at": time.time()})
            _atomic_json(root / "state" / "runtime.json", state)
            if once:
                return _runtime_summary(root, state, session, status="awaiting_approval")
            time.sleep(poll_seconds)
            continue
        if ready and _identity_renewal_due(state):
            try:
                renewal = _renew_identity_once(root)
                if renewal.get("status") == "activated":
                    state = _read_json(root / "state" / "node.json", default=state)
                    session = _read_json(root / "state" / "session.json", default=session)
            except (ConnectionError, OSError, ValueError) as exc:
                expires_at = float(state.get("certificate_not_after") or 0)
                state.update(
                    {
                        "last_identity_renewal_attempt_at": time.time(),
                        "last_identity_renewal_error": str(sanitize_public(str(exc)))[:300],
                    }
                )
                if expires_at <= time.time():
                    state.update(
                        {
                            "state": "offline",
                            "process_state": "renewing_identity",
                            "last_heartbeat_at": time.time(),
                            "last_error_category": "identity_renewal_unavailable",
                        }
                    )
                    _atomic_json(root / "state" / "runtime.json", state)
                    if once:
                        return _runtime_summary(root, state, session, status="identity_renewal_unavailable")
                    time.sleep(backoff)
                    backoff = min(30.0, max(poll_seconds, backoff * 2))
                    continue
                _atomic_json(root / "state" / "runtime.json", state)
        try:
            result = asyncio.run(
                _worker_session_once(
                    root,
                    state,
                    session,
                    idle_poll_seconds=min(1.0, poll_seconds),
                    idle_session_seconds=0.0 if once else 30.0,
                )
            )
            state.update(
                {
                    "state": "online",
                    "process_state": "idle" if result.status == "idle" else result.status,
                    "last_heartbeat_at": time.time(),
                    "last_transport": result.transport,
                    "last_tls_version": result.tls_version,
                    "last_job_id": result.job_id,
                    "last_error_category": None,
                }
            )
            _atomic_json(root / "state" / "runtime.json", state)
            backoff = poll_seconds
            if result.status == "updated":
                return _runtime_summary(root, state, session, status="updated")
            if once:
                return _runtime_summary(root, state, session, status=result.status)
        except (ConnectionError, OSError, asyncio.TimeoutError, ValueError) as exc:
            fallback = session.get("fallback_transport")
            if session.get("transport") != "relay" and isinstance(fallback, Mapping):
                try:
                    apply_transport_directive(root, directive=fallback)
                    state.update(
                        {
                            "process_state": "switching_transport",
                            "last_heartbeat_at": time.time(),
                            "last_error_category": "primary_transport_unavailable",
                        }
                    )
                    _atomic_json(root / "state" / "runtime.json", state)
                    continue
                except ValueError:
                    pass
            state.update(
                {
                    "process_state": "reconnecting",
                    "last_heartbeat_at": time.time(),
                    "last_error_category": "transport_unavailable",
                    "last_error": str(sanitize_public(str(exc)))[:300],
                }
            )
            _atomic_json(root / "state" / "runtime.json", state)
            if once:
                return _runtime_summary(root, state, session, status="transport_unavailable")
            time.sleep(backoff)
            backoff = min(30.0, max(poll_seconds, backoff * 2))
        else:
            time.sleep(poll_seconds)


async def _worker_session_once(
    root: Path,
    state: dict[str, Any],
    session: dict[str, Any],
    *,
    idle_poll_seconds: float = 0.0,
    idle_session_seconds: float = 0.0,
):
    parsed = urlparse(str(session.get("endpoint") or ""))
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("worker session endpoint is invalid")
    if session.get("transport") not in {"direct", "overlay", "relay"}:
        raise ValueError("worker session transport is unsupported")
    capability = probe_capabilities(node_id=str(state.get("node_id") or ""), home=root)
    if session.get("transport") == "relay":
        context = create_tls_context(
            server=False,
            certificate=str(session["certificate"]),
            private_key=str(session["private_key"]),
            trust_store=str(session.get("relay_server_ca")) if session.get("relay_server_ca") else None,
        )
        relay_session_id = str(session.get("relay_session_id") or "")
        relay_peer_node_id = str(session.get("relay_peer_node_id") or "")
        try:
            relay_session_key = base64.urlsafe_b64decode(str(session.get("relay_session_key") or "").encode())
        except Exception as exc:
            raise ValueError("worker Relay session key is invalid") from exc
        endpoint = RelayEndpoint(
            host=parsed.hostname,
            port=int(parsed.port or 443),
            server_hostname=str(session.get("server_name") or parsed.hostname),
            ssl_context=context,
            node_id=capability.node_id,
            peer_node_id=relay_peer_node_id,
            session_id=relay_session_id,
            session_key=relay_session_key,
        )
        await endpoint.connect()
        try:
            return await RelayWorkerSessionClient(
                endpoint=endpoint,
                capability=capability,
                worker_root=root,
                identity_generation=int(state.get("identity_generation") or 1),
            ).run_once()
        finally:
            await endpoint.close()
    context = tls_client_context(
        certificate=str(session["certificate"]),
        private_key=str(session["private_key"]),
        server_ca=str(session["ca_file"]),
    )
    client = WorkerSessionClient(
        host=parsed.hostname,
        port=int(parsed.port or 443),
        server_hostname=str(session.get("server_name") or parsed.hostname),
        ssl_context=context,
        capability=capability,
        worker_root=root,
        identity_generation=int(state.get("identity_generation") or 1),
    )
    return await client.run_once(idle_poll_seconds=idle_poll_seconds, idle_session_seconds=idle_session_seconds)


def _runtime_summary(root: Path, state: dict[str, Any], session: dict[str, Any], *, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "node": sanitize_public(state),
        "session_transport": session.get("transport") or "not-configured",
        "endpoint_configured": bool(session.get("endpoint")),
        "mutual_tls_ready": all(session.get(key) for key in ("ca_file", "certificate", "private_key")),
        "home": str(root),
    }


def activate_worker(root: Path, *, approval_file: str) -> dict[str, Any]:
    root = _safe_root(root)
    approval_path = Path(approval_file).expanduser().resolve()
    activation = _read_json(approval_path, default={})
    if activation.get("activation") and isinstance(activation["activation"], dict):
        activation = activation["activation"]
    if activation.get("schema_version") != "across-worker-activation/1.0":
        raise ValueError("worker activation schema is incompatible")
    node = _read_json(root / "state" / "node.json", default={})
    node_id = str(activation.get("node_id") or "")
    if not node or node.get("node_id") != node_id:
        raise ValueError("worker activation belongs to a different node")
    private_key_path = root / "identity" / "device-key.pem"
    private_key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    certificate = x509.load_pem_x509_certificate(str(activation.get("certificate_pem") or "").encode("ascii"))
    ca_certificate = x509.load_pem_x509_certificate(str(activation.get("ca_certificate_pem") or "").encode("ascii"))
    local_public = private_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    certificate_public = certificate.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if local_public != certificate_public:
        raise ValueError("worker activation certificate does not match its device key")
    ca_certificate.public_key().verify(certificate.signature, certificate.tbs_certificate_bytes, padding.PKCS1v15(), certificate.signature_hash_algorithm)
    now = time.time()
    if certificate.not_valid_before_utc.timestamp() > now or certificate.not_valid_after_utc.timestamp() <= now:
        raise ValueError("worker activation certificate is not currently valid")
    uris = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.UniformResourceIdentifier)
    if f"spiffe://across.local/worker/{node_id}" not in uris:
        raise ValueError("worker activation certificate has the wrong identity")
    certificate_path = root / "identity" / "device-certificate.pem"
    ca_path = root / "identity" / "coordinator-ca.pem"
    _atomic_bytes(certificate_path, certificate.public_bytes(serialization.Encoding.PEM), mode=0o644)
    _atomic_bytes(ca_path, ca_certificate.public_bytes(serialization.Encoding.PEM), mode=0o644)
    session = _read_json(root / "state" / "session.json", default={})
    endpoint = str(activation.get("endpoint") or session.get("endpoint") or "")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("worker activation endpoint is invalid")
    session.update(
        {
            "schema_version": "across-worker-session-config/1.0",
            "endpoint": endpoint,
            "transport": str(activation.get("transport") or session.get("transport") or "direct"),
            "server_name": str(session.get("server_name") or parsed.hostname),
            "ca_file": str(ca_path),
            "certificate": str(certificate_path),
            "private_key": str(private_key_path),
        }
    )
    if session["transport"] == "relay":
        for key in ("relay_session_id", "relay_peer_node_id", "relay_session_key"):
            value = str(activation.get(key) or "")
            if not value:
                raise ValueError(f"worker activation is missing {key}")
            session[key] = value
    elif all(activation.get(key) for key in ("relay_endpoint", "relay_session_id", "relay_peer_node_id", "relay_session_key")):
        relay_endpoint = str(activation["relay_endpoint"])
        relay_parsed = urlparse(relay_endpoint)
        if relay_parsed.scheme != "https" or not relay_parsed.hostname:
            raise ValueError("worker fallback Relay endpoint is invalid")
        session["fallback_transport"] = {
            "transport": "relay",
            "endpoint": relay_endpoint,
            "server_name": relay_parsed.hostname,
            "relay_session_id": str(activation["relay_session_id"]),
            "relay_peer_node_id": str(activation["relay_peer_node_id"]),
            "relay_session_key": str(activation["relay_session_key"]),
        }
    node.update(
        {
            "state": "offline",
            "identity_generation": int(activation.get("session_generation") or 1),
            "certificate_not_after": certificate.not_valid_after_utc.timestamp(),
        }
    )
    _atomic_json(root / "state" / "session.json", session)
    _atomic_json(root / "state" / "node.json", node)
    (root / "identity" / "join-request.json").unlink(missing_ok=True)
    return {"status": "activated", "node_id": node_id, "certificate_not_after": node["certificate_not_after"], "mutual_tls_ready": True}


def _submit_enrollment(root: Path) -> dict[str, Any]:
    root = _safe_root(root)
    request = _read_json(root / "identity" / "join-request.json", default={})
    session = _read_json(root / "state" / "session.json", default={})
    endpoint = str(session.get("enrollment_endpoint") or "")
    if not request or not endpoint:
        raise ValueError("Worker enrollment request is unavailable")
    private_key = serialization.load_pem_private_key((root / "identity" / "device-key.pem").read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("Worker enrollment key type is unsupported")
    signature = base64.urlsafe_b64encode(private_key.sign(canonical_json(request).encode("utf-8"))).decode("ascii").rstrip("=")
    response = _enrollment_post(
        endpoint + "/v1/pairings",
        {"request": request, "signature": signature},
        ca_file=str(session.get("ca_file") or "") or None,
    )
    _atomic_json(root / "state" / "enrollment.json", {"submitted_at": time.time(), **sanitize_public(response)})
    return response


def _poll_activation_once(root: Path) -> dict[str, Any]:
    root = _safe_root(root)
    session = _read_json(root / "state" / "session.json", default={})
    node = _read_json(root / "state" / "node.json", default={})
    request = _read_json(root / "identity" / "join-request.json", default={})
    endpoint = str(session.get("enrollment_endpoint") or "")
    enrollment_id = str(request.get("enrollment_id") or "")
    node_id = str(node.get("node_id") or "")
    if not endpoint or not enrollment_id or not node_id:
        return {"status": "not_configured"}
    nonce = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
    proof = {
        "schema_version": "across-worker-activation-proof/1.0",
        "node_id": node_id,
        "enrollment_id": enrollment_id,
        "nonce": nonce,
    }
    private_key = serialization.load_pem_private_key((root / "identity" / "device-key.pem").read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("Worker enrollment key type is unsupported")
    payload = {
        "node_id": node_id,
        "enrollment_id": enrollment_id,
        "nonce": nonce,
        "signature": base64.urlsafe_b64encode(private_key.sign(canonical_json(proof).encode("utf-8"))).decode("ascii").rstrip("="),
    }
    response = _enrollment_post(
        endpoint + "/v1/activations",
        payload,
        ca_file=str(session.get("ca_file") or "") or None,
    )
    if response.get("status") != "approved" or not isinstance(response.get("activation"), dict):
        return sanitize_public(response)
    approval = root / "state" / f".activation-{time.time_ns()}.json"
    _atomic_json(approval, {"activation": response["activation"]})
    try:
        return activate_worker(root, approval_file=str(approval))
    finally:
        approval.unlink(missing_ok=True)


def _identity_renewal_due(state: Mapping[str, Any], *, now: float | None = None) -> bool:
    expires_at = float(state.get("certificate_not_after") or 0)
    return expires_at > 0 and expires_at <= float(time.time() if now is None else now) + IDENTITY_RENEWAL_WINDOW_SECONDS


def _renew_identity_once(root: Path) -> dict[str, Any]:
    root = _safe_root(root)
    session = _read_json(root / "state" / "session.json", default={})
    node = _read_json(root / "state" / "node.json", default={})
    endpoint = str(session.get("enrollment_endpoint") or "")
    node_id = str(node.get("node_id") or "")
    generation = int(node.get("identity_generation") or 0)
    if not endpoint or not node_id or generation < 1:
        raise ValueError("Worker identity renewal is not configured")
    nonce = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")
    proof = {
        "schema_version": "across-worker-identity-renewal-proof/1.0",
        "node_id": node_id,
        "current_generation": generation,
        "nonce": nonce,
    }
    private_key = serialization.load_pem_private_key((root / "identity" / "device-key.pem").read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("Worker identity renewal key type is unsupported")
    response = _enrollment_post(
        endpoint + "/v1/identity/renew",
        {
            "node_id": node_id,
            "current_generation": generation,
            "nonce": nonce,
            "signature": base64.urlsafe_b64encode(private_key.sign(canonical_json(proof).encode("utf-8"))).decode("ascii").rstrip("="),
        },
        ca_file=str(session.get("ca_file") or "") or None,
    )
    if response.get("status") not in {"renewed", "already_renewed"} or not isinstance(response.get("activation"), dict):
        raise ValueError("Worker identity renewal response is invalid")
    approval = root / "state" / f".identity-renewal-{time.time_ns()}.json"
    _atomic_json(approval, {"activation": response["activation"]})
    try:
        result = activate_worker(root, approval_file=str(approval))
    finally:
        approval.unlink(missing_ok=True)
    refreshed = _read_json(root / "state" / "node.json", default={})
    refreshed["last_identity_renewed_at"] = time.time()
    refreshed.pop("last_identity_renewal_error", None)
    _atomic_json(root / "state" / "node.json", refreshed)
    return result


def _enrollment_post(url: str, payload: Mapping[str, Any], *, ca_file: str | None) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Worker enrollment endpoint must use credential-free HTTPS")
    context = ssl.create_default_context(cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    request = urllib.request.Request(
        url,
        data=canonical_json(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, context=context, timeout=15) as response:
            body = response.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read(64 * 1024).decode("utf-8"))
        except Exception:
            detail = {}
        code = ((detail.get("detail") or {}).get("code") if isinstance(detail, dict) else None) or "enrollment_rejected"
        raise ValueError(f"Worker enrollment was rejected: {code}") from exc
    value = json.loads(body.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Worker enrollment response is invalid")
    return value


def _validated_enrollment_endpoint(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(str(value))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Worker enrollment endpoint must use credential-free HTTPS")
    return str(value).rstrip("/")


def worker_status(root: Path) -> dict[str, Any]:
    root = _safe_root(root)
    install = _read_json(root / "state" / "install.json", default={})
    current = _read_json(root / "state" / "current-version.json", default={})
    node = _read_json(root / "state" / "node.json", default={})
    runtime = _read_json(root / "state" / "runtime.json", default={})
    return {
        "schema_version": "across-worker-status/1.0",
        "status": "installed" if install else "not_installed",
        # The install record describes the bootstrap installation. Managed
        # updates atomically switch ``current-version.json`` without rewriting
        # that immutable provenance record, so the user-facing status must
        # report the active runtime rather than the originally installed one.
        "version": current.get("version") or install.get("version"),
        "node": sanitize_public(node),
        "runtime": sanitize_public(runtime),
        "draining": (root / "state" / "draining").exists(),
        "home": str(root),
        "private_key_present": (root / "identity" / "device-key.pem").exists(),
        "provider_key_present": _contains_provider_key(root),
    }


def set_drain(root: Path, enabled: bool) -> dict[str, Any]:
    root = _safe_root(root)
    marker = root / "state" / "draining"
    marker.parent.mkdir(parents=True, exist_ok=True)
    if enabled:
        _atomic_text(marker, f"{time.time()}\n", mode=0o600)
    else:
        marker.unlink(missing_ok=True)
    return {"status": "draining" if enabled else "online", "draining": enabled}


def leave_worker(root: Path) -> dict[str, Any]:
    root = _safe_root(root)
    identity = root / "identity"
    removed: list[str] = []
    for name in (
        "device-key.pem",
        "device-public.pem",
        "device-certificate.pem",
        "coordinator-ca.pem",
        "enrollment-ca.pem",
        "join-request.json",
    ):
        target = identity / name
        if target.exists():
            target.unlink()
            removed.append(name)
    (root / "state" / "node.json").unlink(missing_ok=True)
    (root / "state" / "runtime.json").unlink(missing_ok=True)
    (root / "state" / "session.json").unlink(missing_ok=True)
    (root / "state" / "enrollment.json").unlink(missing_ok=True)
    return {"status": "left", "identity_files_removed": removed}


def update_worker(
    root: Path,
    *,
    artifact: str,
    expected_sha256: str,
    version: str,
    restart_service: bool = True,
) -> dict[str, Any]:
    root = _safe_root(root)
    source = Path(artifact).expanduser().resolve()
    if not source.is_file():
        raise ValueError("worker update artifact does not exist")
    actual = _file_hash(source)
    if actual != expected_sha256:
        raise ValueError("worker update artifact hash mismatch")
    set_drain(root, True)
    safe_version = _safe_pack_id(version)
    target = root / "versions" / safe_version
    temporary = root / "versions" / f".install-{safe_version}-{time.time_ns()}"
    prior = _read_json(root / "state" / "current-version.json", default={})
    try:
        _extract_worker_distribution(source, temporary, expected_version=safe_version)
        _atomic_text(temporary / ".distribution-sha256", actual + "\n", mode=0o600)
        if target.exists():
            shutil.rmtree(temporary)
            recorded_hash = (target / ".distribution-sha256").read_text(encoding="utf-8").strip() if (target / ".distribution-sha256").is_file() else ""
            if recorded_hash != actual:
                raise ValueError("an existing Worker version has different verified content")
        else:
            os.replace(temporary, target)
        _activate_runtime(root, target, version=safe_version, sha256=actual, previous=prior or None)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    finally:
        set_drain(root, False)
    restart = _restart_installed_service(root) if restart_service else {
        "attempted": False,
        "restarted": False,
        "reason": "deferred_until_session_acknowledged",
    }
    return {"status": "updated", "version": safe_version, "sha256": actual, "rollback_available": bool(prior), "service_restart": restart}


def update_worker_from_url(
    root: Path,
    *,
    url: str,
    expected_sha256: str,
    version: str,
    restart_service: bool = True,
) -> dict[str, Any]:
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("worker update URL must be credential-free HTTPS")
    request = urllib.request.Request(str(url), headers={"user-agent": "across-worker-updater/1"})
    with tempfile.TemporaryDirectory(prefix="across-worker-update-") as temporary:
        artifact = Path(temporary) / "worker-distribution.tar.gz"
        total = 0
        try:
            with urllib.request.urlopen(request, timeout=60) as response, artifact.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 512 * 1024 * 1024:
                        raise ValueError("worker update exceeds size limit")
                    handle.write(chunk)
        except urllib.error.URLError as exc:
            raise ValueError("worker update download failed") from exc
        return update_worker(
            root,
            artifact=str(artifact),
            expected_sha256=expected_sha256,
            version=version,
            restart_service=restart_service,
        )


def apply_transport_directive(root: Path, *, directive: Mapping[str, Any]) -> dict[str, Any]:
    root = _safe_root(root)
    transport = str(directive.get("transport") or "")
    if transport not in {"direct", "overlay", "relay"}:
        raise ValueError("worker transport directive is invalid")
    endpoint = str(directive.get("endpoint") or "")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("worker transport endpoint must be credential-free HTTPS")
    session = _read_json(root / "state" / "session.json", default={})
    if not all(session.get(key) for key in ("ca_file", "certificate", "private_key")):
        raise ValueError("worker transport change requires an active identity")
    session.update(
        {
            "schema_version": "across-worker-session-config/1.0",
            "transport": transport,
            "endpoint": endpoint,
            "server_name": str(directive.get("server_name") or parsed.hostname),
        }
    )
    if transport == "relay":
        relay_values = {
            key: str(directive.get(key) or "")
            for key in ("relay_session_id", "relay_peer_node_id", "relay_session_key")
        }
        try:
            decoded_key = base64.urlsafe_b64decode(relay_values["relay_session_key"].encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("worker Relay session key is invalid") from exc
        if not relay_values["relay_session_id"] or not relay_values["relay_peer_node_id"] or len(decoded_key) != 32:
            raise ValueError("worker Relay session material is invalid")
        session.update(relay_values)
    else:
        for key in ("relay_session_id", "relay_peer_node_id", "relay_session_key", "relay_server_ca"):
            session.pop(key, None)
    _atomic_json(root / "state" / "session.json", session)
    return {"status": "transport_switched", "transport": transport, "endpoint": endpoint}


def rollback_worker(root: Path) -> dict[str, Any]:
    root = _safe_root(root)
    current = _read_json(root / "state" / "current-version.json", default={})
    previous = current.get("previous") if isinstance(current.get("previous"), dict) else None
    if not previous or not previous.get("version"):
        raise ValueError("no verified Worker rollback is available")
    target = root / "versions" / _safe_pack_id(previous["version"])
    if not (target / "src" / "across_orchestrator" / "worker_cli.py").is_file():
        raise ValueError("previous Worker runtime is unavailable")
    set_drain(root, True)
    _activate_runtime(
        root,
        target,
        version=str(previous["version"]),
        sha256=str(previous.get("sha256") or ""),
        previous={key: value for key, value in current.items() if key != "previous"},
    )
    set_drain(root, False)
    restart = _restart_installed_service(root)
    return {"status": "rolled_back", "version": previous["version"], "rollback_available": True, "service_restart": restart}


def uninstall_worker(root: Path, *, purge: bool, label: str) -> dict[str, Any]:
    root = _safe_root(root)
    service = _remove_service(label)
    launcher = root / "bin" / "across-worker"
    launcher.unlink(missing_ok=True)
    if purge and root.exists():
        managed = _read_json(root / "state" / "install.json", default={})
        if not managed or managed.get("schema_version") != "across-worker-install/1.0":
            raise ValueError("refusing to purge an unmanaged worker root")
        shutil.rmtree(root)
    return {"status": "uninstalled", "purged": purge, "service": service}


def install_worker_pack(root: Path, *, artifact: str, expected_sha256: str) -> dict[str, Any]:
    root = _safe_root(root)
    source = Path(artifact).expanduser().resolve()
    if not source.is_file() or _file_hash(source) != expected_sha256:
        raise ValueError("Worker pack artifact hash mismatch")
    with tarfile.open(source, mode="r:gz") as archive:
        members = archive.getmembers()
        if not members or len(members) > 10_000:
            raise ValueError("Worker pack archive is empty or too large")
        total_size = 0
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk() or member.isdev():
                raise ValueError("Worker pack archive contains an unsafe path or file type")
            total_size += max(0, int(member.size))
        if total_size > 512 * 1024 * 1024:
            raise ValueError("Worker pack archive exceeds the unpacked size limit")
        manifest_member = next((item for item in members if item.name == "pack.json" and item.isfile()), None)
        if not manifest_member:
            raise ValueError("Worker pack manifest is missing")
        handle = archive.extractfile(manifest_member)
        manifest = json.loads(handle.read().decode("utf-8") if handle else "{}")
        if manifest.get("schema_version") != "across-worker-pack/1.0":
            raise ValueError("Worker pack manifest schema is incompatible")
        pack_id = _safe_pack_id(manifest.get("pack_id"))
        version = _safe_pack_id(manifest.get("version"))
        entrypoint = Path(str(manifest.get("entrypoint") or ""))
        if entrypoint.is_absolute() or ".." in entrypoint.parts or not entrypoint.parts:
            raise ValueError("Worker pack entrypoint is unsafe")
        target = root / "packs" / pack_id / version
        temporary = root / "packs" / pack_id / f".install-{time.time_ns()}"
        temporary.mkdir(parents=True, exist_ok=False)
        try:
            for member in members:
                if member.isdir():
                    (temporary / member.name).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                destination = (temporary / member.name).resolve()
                if temporary.resolve() not in destination.parents:
                    raise ValueError("Worker pack extraction escaped its staging root")
                destination.parent.mkdir(parents=True, exist_ok=True)
                reader = archive.extractfile(member)
                if not reader:
                    raise ValueError("Worker pack file is unavailable")
                with destination.open("wb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                destination.chmod(member.mode & 0o755 if member.mode else 0o644)
            if not (temporary / entrypoint).is_file():
                raise ValueError("Worker pack entrypoint is missing")
            if target.exists():
                shutil.rmtree(temporary)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, target)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    _atomic_json(root / "packs" / pack_id / "current.json", {"pack_id": pack_id, "version": version, "entrypoint": str(entrypoint), "sha256": expected_sha256})
    return {"status": "installed", "pack_id": pack_id, "version": version, "sha256": expected_sha256, "entrypoint": str(target / entrypoint)}


def list_worker_packs(root: Path) -> list[dict[str, Any]]:
    root = _safe_root(root)
    packs = []
    for pointer in sorted((root / "packs").glob("*/current.json")):
        value = _read_json(pointer, default={})
        if value.get("pack_id"):
            packs.append(sanitize_public(value))
    return packs


def remove_worker_pack(root: Path, *, pack_id: str) -> dict[str, Any]:
    root = _safe_root(root)
    target = root / "packs" / _safe_pack_id(pack_id)
    if target.exists():
        shutil.rmtree(target)
    return {"status": "removed", "pack_id": pack_id, "removed": not target.exists()}


def _stage_builtin_runtime(root: Path, version: str) -> Path:
    safe_version = _safe_pack_id(version)
    target = root / "versions" / safe_version
    package_target = target / "src" / "across_orchestrator"
    if package_target.is_dir():
        return target
    temporary = root / "versions" / f".install-{safe_version}-{time.time_ns()}"
    source = Path(__file__).resolve().parent
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copytree(
            source,
            temporary / "src" / "across_orchestrator",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
        _atomic_json(
            temporary / "worker-distribution.json",
            {
                "schema_version": "across-worker-distribution/1.0",
                "version": safe_version,
                "entrypoint": "src/across_orchestrator/worker_cli.py",
                "python_requires": ">=3.11",
            },
        )
        if target.exists():
            shutil.rmtree(temporary)
        else:
            os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def _extract_worker_distribution(source: Path, destination: Path, *, expected_version: str) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(source, mode="r:gz") as archive:
            members = archive.getmembers()
            if not members or len(members) > 20_000:
                raise ValueError("Worker distribution archive is empty or too large")
            total_size = 0
            for member in members:
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk() or member.isdev():
                    raise ValueError("Worker distribution contains an unsafe path or file type")
                total_size += max(0, int(member.size))
            if total_size > 256 * 1024 * 1024:
                raise ValueError("Worker distribution exceeds the unpacked size limit")
            manifest_member = next((item for item in members if item.name == "worker-distribution.json" and item.isfile()), None)
            if not manifest_member:
                raise ValueError("Worker distribution manifest is missing")
            handle = archive.extractfile(manifest_member)
            manifest = json.loads(handle.read().decode("utf-8") if handle else "{}")
            if manifest.get("schema_version") != "across-worker-distribution/1.0":
                raise ValueError("Worker distribution schema is incompatible")
            if _safe_pack_id(manifest.get("version")) != expected_version:
                raise ValueError("Worker distribution version does not match the requested version")
            for member in members:
                if member.isdir():
                    (destination / member.name).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                target = (destination / member.name).resolve()
                if destination.resolve() not in target.parents:
                    raise ValueError("Worker distribution extraction escaped staging")
                target.parent.mkdir(parents=True, exist_ok=True)
                reader = archive.extractfile(member)
                if not reader:
                    raise ValueError("Worker distribution member is unavailable")
                with target.open("wb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                target.chmod(member.mode & 0o755 if member.mode else 0o644)
        if not (destination / "src" / "across_orchestrator" / "worker_cli.py").is_file():
            raise ValueError("Worker distribution entrypoint is missing")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _activate_runtime(
    root: Path,
    target: Path,
    *,
    version: str,
    sha256: str,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    link = runtime / "current"
    temporary = runtime / f".current-{time.time_ns()}"
    os.symlink(os.path.relpath(target, runtime), temporary)
    os.replace(temporary, link)
    record = {
        "schema_version": "across-worker-active-runtime/1.0",
        "version": version,
        "sha256": sha256,
        "runtime": str(target),
        "activated_at": time.time(),
        "previous": previous,
    }
    _atomic_json(root / "state" / "current-version.json", record)
    return record


def _install_service(root: Path, *, launcher: Path, start: bool, label: str) -> dict[str, Any]:
    system = platform.system().lower()
    if system == "darwin":
        destination = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>'
            f'<key>Label</key><string>{label}</string>'
            f'<key>ProgramArguments</key><array><string>{launcher}</string><string>run</string></array>'
            f'<key>WorkingDirectory</key><string>{root}</string>'
            '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>'
            f'<key>StandardOutPath</key><string>{root / "logs" / "worker.out.log"}</string>'
            f'<key>StandardErrorPath</key><string>{root / "logs" / "worker.err.log"}</string>'
            '</dict></plist>\n'
        )
        _atomic_text(destination, plist, mode=0o644)
        started = False
        if start:
            result = subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination)], capture_output=True, text=True)
            started = result.returncode == 0
        return {"manager": "launchd-user", "unit": str(destination), "label": label, "started": started}
    if system == "linux":
        destination = Path.home() / ".config" / "systemd" / "user" / f"{label}.service"
        unit = (
            "[Unit]\nDescription=Across Worker Runtime\nAfter=network-online.target\n\n"
            "[Service]\nType=simple\n"
            f"ExecStart={launcher} run\nWorkingDirectory={root}\nRestart=on-failure\nRestartSec=2\n"
            "NoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=read-only\n"
            f"ReadWritePaths={root}\n\n[Install]\nWantedBy=default.target\n"
        )
        _atomic_text(destination, unit, mode=0o644)
        started = False
        if start and shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            result = subprocess.run(["systemctl", "--user", "enable", "--now", f"{label}.service"], capture_output=True)
            started = result.returncode == 0
        return {"manager": "systemd-user", "unit": str(destination), "label": label, "started": started}
    raise ValueError("unsupported worker platform")


def _remove_service(label: str) -> dict[str, Any]:
    system = platform.system().lower()
    if system == "darwin":
        destination = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}", str(destination)], capture_output=True)
        destination.unlink(missing_ok=True)
        return {"manager": "launchd-user", "removed": not destination.exists()}
    destination = Path.home() / ".config" / "systemd" / "user" / f"{label}.service"
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{label}.service"], capture_output=True)
    destination.unlink(missing_ok=True)
    return {"manager": "systemd-user", "removed": not destination.exists()}


def _restart_installed_service(root: Path) -> dict[str, Any]:
    install = _read_json(root / "state" / "install.json", default={})
    service = install.get("service") if isinstance(install.get("service"), dict) else {}
    if not service or not service.get("started"):
        return {"attempted": False, "restarted": False, "reason": "service_not_running"}
    label = str(service.get("label") or "com.across.worker")
    manager = str(service.get("manager") or "")
    if manager == "launchd-user":
        result = subprocess.run(
            ["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
        )
    elif manager == "systemd-user" and shutil.which("systemctl"):
        result = subprocess.run(["systemctl", "--user", "restart", f"{label}.service"], capture_output=True, text=True)
    else:
        return {"attempted": False, "restarted": False, "reason": "service_manager_unavailable"}
    return {
        "attempted": True,
        "restarted": result.returncode == 0,
        "reason": None if result.returncode == 0 else "service_restart_failed",
    }


def _safe_root(root: Path) -> Path:
    target = root.expanduser().resolve()
    home = Path.home().resolve()
    if target in {Path("/"), home} or target.parent == Path("/"):
        raise ValueError("unsafe worker root")
    return target


def _optional_managed_path(value: str | None, root: Path) -> str | None:
    if not value:
        return None
    target = Path(value).expanduser().resolve()
    if not target.is_file():
        raise ValueError("worker TLS file does not exist")
    if root not in target.parents:
        raise ValueError("worker TLS files must be stored under the Worker root")
    return str(target)


def _safe_pack_id(value: Any) -> str:
    import re

    text = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", text):
        raise ValueError("Worker pack identifier is invalid")
    return text


def _contains_provider_key(root: Path) -> bool:
    import re

    secret_patterns = (
        re.compile(r"(?i)(?:OPENAI|ANTHROPIC|GOOGLE|DEEPSEEK|MINIMAX)_API_KEY\s*[=:]\s*['\"]?[A-Za-z0-9._-]{8,}"),
        re.compile(r"(?i)['\"]api[_-]?key['\"]\s*:\s*['\"][^'\"]{8,}['\"]"),
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    )
    for target in root.rglob("*") if root.exists() else ():
        relative = target.relative_to(root)
        if (
            (relative.parts and relative.parts[0] in {"bootstrap", "cache", "packs", "runtime", "versions"})
            or not target.is_file()
            or target.stat().st_size > 1024 * 1024
            or target.name == "device-key.pem"
        ):
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(pattern.search(text) for pattern in secret_patterns):
            return True
    return False


def _file_hash(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(default)
    return value if isinstance(value, dict) else dict(default)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", mode=0o600)


def _atomic_text(path: Path, value: str, *, mode: int) -> None:
    _atomic_bytes(path, value.encode("utf-8"), mode=mode)


def _atomic_bytes(path: Path, value: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)


def _print(value: Any, *, json_output: bool, stream=sys.stdout) -> None:
    if json_output:
        print(canonical_json(sanitize_public(value)), file=stream)
    else:
        print(json.dumps(sanitize_public(value), ensure_ascii=False, indent=2, sort_keys=True), file=stream)


if __name__ == "__main__":
    raise SystemExit(main())
