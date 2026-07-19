from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
from datetime import datetime, timedelta, timezone
import asyncio
import base64
import json
import os
import sys
import time
import socket
import ssl
import tarfile
import io

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from across_orchestrator.coordinator import CoordinatorError, WorkerCoordinator
from across_orchestrator.relay import AsyncRelayServer, RelayEndpoint, RelayFrame, RelayRouter, create_tls_context, open_relay_payload, seal_relay_payload
from across_orchestrator.worker_cli import apply_transport_directive, create_join_request, install_worker, install_worker_pack, leave_worker, list_worker_packs, remove_worker_pack, rollback_worker, uninstall_worker, update_worker, worker_status
from across_orchestrator.worker_protocol import (
    ArtifactDescriptor,
    CAPABILITY_SCHEMA,
    JOB_SCHEMA,
    CapabilityManifest,
    JobEvent,
    JobLease,
    JobManifest,
    ProtocolError,
    build_evidence_receipt,
    canonical_json,
    new_protocol_id,
    payload_hash,
    sanitize_public,
)
from across_orchestrator.worker_runtime import BoundedProcessExecutor, ChunkedArtifactReceiver, WORKER_VERSION
from across_orchestrator.worker_store import WorkerControlStore
from across_orchestrator.worker_control_command import handle_worker_control_command, serve_worker_control
from across_orchestrator.worker_transport import CoordinatorSessionServer, RelayCoordinatorSession, RelayWorkerSessionClient, WorkerSessionClient, WorkerTransportError, _model_grant_ttl_seconds, tls_client_context, tls_server_context
import across_orchestrator.worker_cli as worker_cli_module


class Clock:
    def __init__(self, value: float = 1_800_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_model_grant_ttl_includes_provider_and_accounting_margin():
    assert _model_grant_ttl_seconds({"timeout_seconds": 30}) == 90
    assert _model_grant_ttl_seconds({"timeout_seconds": 300}) == 360
    assert _model_grant_ttl_seconds({"timeout_seconds": 60}, 390) == 450


@pytest.mark.asyncio
async def test_worker_control_server_round_trip_uses_private_socket(tmp_path):
    socket_path = Path("/tmp") / f"across-worker-{sha256(str(tmp_path).encode()).hexdigest()[:12]}.sock"
    coordinator = WorkerCoordinator(WorkerControlStore(tmp_path / "store"))
    server_task = asyncio.create_task(serve_worker_control(socket_path, coordinator))
    try:
        for _ in range(100):
            if socket_path.exists():
                break
            await asyncio.sleep(0.01)
        assert socket_path.exists()
        assert socket_path.stat().st_mode & 0o777 == 0o600
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(json.dumps({
            "schema_version": "across-worker-control-command/1.0",
            "action": "snapshot",
            "payload": {},
        }).encode("utf-8") + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
        assert response["schema_version"] == "across-worker-control-snapshot/1.0"
        assert response["nodes"] == []
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task
    assert not socket_path.exists()


def capability(node_id: str = "node-test", **overrides) -> CapabilityManifest:
    values = {
        "node_id": node_id,
        "worker_version": "0.10.0",
        "os": "macos",
        "os_version": "14.0",
        "architecture": "arm64",
        "cpu_count": 8,
        "memory_bytes": 8 * 1024**3,
        "disk_available_bytes": 32 * 1024**3,
        "executors": ("bounded-process",),
        "isolation_level": "bounded",
        "labels": ("fast",),
        "verification_status": "verified",
    }
    values.update(overrides)
    return CapabilityManifest(**values)


def manifest(job_id: str = "job-test", run_id: str = "run-test", **overrides) -> JobManifest:
    values = {
        "job_id": job_id,
        "run_id": run_id,
        "project_id": "project-test",
        "workflow_id": "scenario-simulation",
        "idempotency_key": f"idem-{job_id}",
        "command_argv": (sys.executable, "-c", "print('ok')"),
        "required_capabilities": {"os": "macos", "architecture": "arm64"},
        "permissions": {"filesystem": {"mode": "run-scoped"}, "network": {"mode": "none"}},
        "budgets": {"timeout_seconds": 5, "memory_bytes": 256 * 1024**2, "max_output_bytes": 1024**2},
        "expected_outputs": (),
        "retry_policy": {"max_attempts": 2, "retry_safe": True},
    }
    values.update(overrides)
    return JobManifest(**values)


def approved_coordinator(tmp_path: Path, clock: Clock | None = None, node: CapabilityManifest | None = None) -> WorkerCoordinator:
    timer = clock or Clock()
    coordinator = WorkerCoordinator(WorkerControlStore(tmp_path / "coordinator"), clock=timer, lease_seconds=9)
    cap = node or capability()
    pairing = coordinator.enrollment.create_pairing_code()
    pending = coordinator.enrollment.submit_pairing(
        enrollment_id=pairing["enrollment_id"],
        pairing_code=pairing["pairing_code"],
        public_identity={"node_id": cap.node_id, "display_name": "Test Worker", "fingerprint": "a" * 64, "algorithm": "ed25519"},
        capability_summary=cap.to_dict(),
    )
    coordinator.enrollment.approve(cap.node_id, pending["verification_code"])
    coordinator.connect_node(cap, transport="direct")
    return coordinator


def _write_tls_fixture(root: Path, *, expired_client: bool = False):
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Across Test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = root / "ca.pem"
    ca_key_path = root / "ca-key.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    ca_key_path.write_bytes(
        ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    ca_key_path.chmod(0o600)

    def issue(name: str, *, server: bool):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        not_valid_before = now - timedelta(minutes=1)
        not_valid_after = now + timedelta(hours=1)
        if not server and expired_client:
            not_valid_before = now - timedelta(hours=2)
            not_valid_after = now - timedelta(hours=1)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_valid_before)
            .not_valid_after(not_valid_after)
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH if server else x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=True,
            )
        )
        if server:
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        cert = builder.sign(ca_key, hashes.SHA256())
        cert_path = root / f"{name}.pem"
        key_path = root / f"{name}-key.pem"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        )
        key_path.chmod(0o600)
        return cert_path, key_path

    server_cert, server_key = issue("localhost", server=True)
    client_cert, client_key = issue("node-test", server=False)
    return ca_path, server_cert, server_key, client_cert, client_key


def _issue_tls_client(root: Path, node_id: str):
    now = datetime.now(timezone.utc)
    ca_key = serialization.load_pem_private_key((root / "ca-key.pem").read_bytes(), password=None)
    ca_cert = x509.load_pem_x509_certificate((root / "ca.pem").read_bytes())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    cert_path = root / f"{node_id}.pem"
    key_path = root / f"{node_id}-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    return cert_path, key_path


def _bind_node_certificate(coordinator: WorkerCoordinator, node_id: str, certificate: Path) -> str:
    fingerprint = x509.load_pem_x509_certificate(certificate.read_bytes()).fingerprint(hashes.SHA256()).hex()
    node = coordinator.store.get("nodes", node_id)
    assert node is not None
    node["certificate_fingerprint"] = fingerprint
    coordinator.store.put("nodes", node_id, node)
    return fingerprint


def test_nw_006_007_pairing_code_is_short_lived_single_use_and_rate_limited(tmp_path):
    clock = Clock()
    coordinator = WorkerCoordinator(WorkerControlStore(tmp_path), clock=clock)
    pairing = coordinator.enrollment.create_pairing_code(ttl_seconds=600, max_failures=2)
    serialized = canonical_json(pairing)
    assert pairing["expires_at"] - clock() == 600
    assert pairing["contains_long_term_secret"] is False
    assert "private" not in serialized.lower()

    with pytest.raises(CoordinatorError, match="rejected"):
        coordinator.enrollment.submit_pairing(
            enrollment_id=pairing["enrollment_id"],
            pairing_code="0000-0000-0000",
            public_identity={},
            capability_summary=capability().to_dict(),
        )
    with pytest.raises(CoordinatorError, match="rejected"):
        coordinator.enrollment.submit_pairing(
            enrollment_id=pairing["enrollment_id"],
            pairing_code="1111-1111-1111",
            public_identity={},
            capability_summary=capability().to_dict(),
        )
    with pytest.raises(CoordinatorError, match="rate limited"):
        coordinator.enrollment.submit_pairing(
            enrollment_id=pairing["enrollment_id"],
            pairing_code=pairing["pairing_code"],
            public_identity={},
            capability_summary=capability().to_dict(),
        )

    expired = coordinator.enrollment.create_pairing_code()
    clock.advance(601)
    with pytest.raises(CoordinatorError, match="rejected"):
        coordinator.enrollment.submit_pairing(
            enrollment_id=expired["enrollment_id"],
            pairing_code=expired["pairing_code"],
            public_identity={},
            capability_summary=capability().to_dict(),
        )


def test_nw_008_009_016_enrollment_requires_human_code_and_revocation_stops_sessions(tmp_path):
    clock = Clock()
    coordinator = WorkerCoordinator(WorkerControlStore(tmp_path), clock=clock)
    cap = capability()
    pairing = coordinator.enrollment.create_pairing_code()
    pending = coordinator.enrollment.submit_pairing(
        enrollment_id=pairing["enrollment_id"],
        pairing_code=pairing["pairing_code"],
        public_identity={"node_id": cap.node_id, "display_name": "Remote", "fingerprint": "b" * 64},
        capability_summary=cap.to_dict(),
    )
    with pytest.raises(CoordinatorError, match="does not match"):
        coordinator.enrollment.approve(cap.node_id, "000000")
    approved = coordinator.enrollment.approve(cap.node_id, pending["verification_code"])
    assert approved["state"] == "offline"
    assert coordinator.connect_node(cap, transport="direct")["state"] == "online_idle"
    coordinator.enrollment.revoke(cap.node_id)
    with pytest.raises(CoordinatorError, match="revoked"):
        coordinator.connect_node(cap, transport="direct")
    deleted = coordinator.enrollment.delete(cap.node_id)
    assert deleted


def test_nw_017_018_019_020_capability_probe_and_stable_scheduler(tmp_path):
    coordinator = approved_coordinator(tmp_path / "a", node=capability("node-a", current_load=0.8, labels=("fast",)))
    cap_b = capability("node-b", current_load=0.1, labels=("fast", "gpu"))
    pairing = coordinator.enrollment.create_pairing_code()
    pending = coordinator.enrollment.submit_pairing(
        enrollment_id=pairing["enrollment_id"], pairing_code=pairing["pairing_code"],
        public_identity={"node_id": cap_b.node_id, "display_name": "B", "fingerprint": "c" * 64}, capability_summary=cap_b.to_dict()
    )
    coordinator.enrollment.approve(cap_b.node_id, pending["verification_code"])
    coordinator.connect_node(cap_b, transport="overlay")
    selected = coordinator.choose_node(manifest(preferred_labels=("gpu",)))
    assert selected and selected["node_id"] == "node-b"
    assert coordinator.choose_node(manifest(required_capabilities={"os": "linux"})) is None
    coordinator.heartbeat_node("node-b", current_load=0.4)
    forged = capability("node-forged", verification_status="unverified")
    assert not forged.supports({"os": "macos"})


def test_nw_035_incompatible_protocol_is_visible_and_never_scheduled(tmp_path):
    coordinator = approved_coordinator(tmp_path)
    incompatible = replace(capability(), protocol_versions=("across-worker-protocol/0.9",))
    with pytest.raises(CoordinatorError, match="incompatible"):
        coordinator.connect_node(incompatible, transport="direct")
    assert coordinator.list_nodes()[0]["state"] == "incompatible"
    assert coordinator.choose_node(manifest()) is None


def test_nw_025_026_027_031_032_leases_recover_monotonically_and_terminal_is_idempotent(tmp_path):
    clock = Clock()
    coordinator = approved_coordinator(tmp_path, clock=clock)
    job_manifest = manifest()
    coordinator.submit_job(job_manifest)
    assert coordinator.submit_job(job_manifest)["job_id"] == job_manifest.job_id
    lease = coordinator.lease_next("node-test")
    assert lease is not None
    with pytest.raises(CoordinatorError, match="hash mismatch"):
        coordinator.acknowledge_lease(lease.lease_id, "0" * 64)
    lease = coordinator.lease_next("node-test")
    assert lease is not None and lease.attempt == 2
    coordinator.acknowledge_lease(lease.lease_id, job_manifest.manifest_hash)
    coordinator.heartbeat_lease(lease.lease_id, node_id="node-test", attempt=2)
    grant = coordinator.issue_model_grant(job_id=job_manifest.job_id, node_id="node-test")
    event = JobEvent(
        event_id="event-complete", job_id=job_manifest.job_id, run_id=job_manifest.run_id,
        node_id="node-test", lease_id=lease.lease_id, attempt=2, sequence=1, state="completed"
    )
    coordinator.record_event(event)
    assert coordinator.store.get("grants", grant.grant_id)["revoked_at"] == clock.value
    assert coordinator.record_event(event)["event_id"] == event.event_id
    with pytest.raises(CoordinatorError, match="already"):
        coordinator.record_event(replace(event, event_id="event-complete-2", sequence=2))

    side_effect = manifest(
        job_id="job-side-effect", run_id="run-side-effect",
        retry_policy={"max_attempts": 3, "retry_safe": False, "external_side_effects": True},
    )
    coordinator.submit_job(side_effect)
    lease2 = coordinator.lease_next("node-test")
    assert lease2
    coordinator.acknowledge_lease(lease2.lease_id, side_effect.manifest_hash)
    clock.advance(10)
    assert coordinator.recover_expired_leases() == ["job-side-effect"]
    assert coordinator.job("job-side-effect")["status"] == "waiting_review"


def test_nw_030_033_039_040_043_048_049_bounded_executor_and_cleanup(tmp_path):
    executor = BoundedProcessExecutor(tmp_path / "worker")
    command = (
        sys.executable,
        "-c",
        "import json,os,pathlib; p=pathlib.Path(os.environ['ACROSS_OUTPUT_DIR'])/'result.json'; p.write_text(json.dumps({'home':os.environ['HOME'],'network':os.environ['ACROSS_NETWORK_POLICY'],'worker_python':os.environ['ACROSS_WORKER_PYTHON']}))",
    )
    job = manifest(command_argv=command, expected_outputs=("result.json",), cleanup_policy={"retention_seconds": 0})
    lease = approved_coordinator(tmp_path / "coordinator").submit_job(job)
    # Runtime needs only the lease binding; the coordinator lifecycle is tested separately.
    from across_orchestrator.worker_protocol import JobLease

    runtime_lease = JobLease(
        lease_id="lease-runtime", job_id=job.job_id, run_id=job.run_id, node_id="node-test",
        attempt=1, manifest_hash=job.manifest_hash, issued_at=time.time(), expires_at=time.time() + 60, heartbeat_interval_seconds=10,
    )
    result = executor.execute(job, runtime_lease, extra_env={"OPENAI_API_KEY": "must-not-leak", "CUSTOM": "ok"})
    assert result.state == "completed"
    assert len(result.artifacts) == 1
    artifact_body = json.loads((result.sandbox / "output" / "result.json").read_text())
    assert artifact_body["network"] == "none"
    assert artifact_body["worker_python"] == sys.executable
    assert str(tmp_path) in artifact_body["home"]
    assert "OPENAI_API_KEY" not in (result.sandbox / ".across-job.json").read_text()
    assert executor.cleanup(run_id=job.run_id, job_id=job.job_id, attempt=1, dry_run=True) == ["sandboxes/run-test/job-test/1"]
    executor.cleanup(run_id=job.run_id, job_id=job.job_id, attempt=1)
    assert not result.sandbox.exists()
    assert not result.sandbox.parent.exists()
    assert not result.sandbox.parent.parent.exists()
    with pytest.raises(ProtocolError):
        manifest(expected_outputs=("../escape",))

    disk_job = manifest(
        job_id="job-disk-preflight",
        run_id="run-disk-preflight",
        required_capabilities={"os": "macos", "architecture": "arm64", "disk_bytes": 2**63},
    )
    disk_lease = JobLease(
        lease_id="lease-disk", job_id=disk_job.job_id, run_id=disk_job.run_id, node_id="node-test",
        attempt=1, manifest_hash=disk_job.manifest_hash, issued_at=time.time(), expires_at=time.time() + 60, heartbeat_interval_seconds=10,
    )
    disk_result = executor.execute(disk_job, disk_lease)
    assert disk_result.state == "failed" and disk_result.reason_category == "disk_budget_unavailable"


def test_nw_030_cancel_terminates_process_tree_within_budget(tmp_path):
    executor = BoundedProcessExecutor(tmp_path / "worker")
    job = manifest(command_argv=(sys.executable, "-c", "import time; time.sleep(30)"))
    from across_orchestrator.worker_protocol import JobLease

    lease = JobLease(
        lease_id="lease-cancel", job_id=job.job_id, run_id=job.run_id, node_id="node-test", attempt=1,
        manifest_hash=job.manifest_hash, issued_at=time.time(), expires_at=time.time() + 60, heartbeat_interval_seconds=10,
    )
    cancel = Event()
    holder = {}
    thread = Thread(target=lambda: holder.setdefault("result", executor.execute(job, lease, cancel_event=cancel)))
    thread.start()
    time.sleep(0.15)
    started = time.monotonic()
    cancel.set()
    thread.join(timeout=3)
    assert time.monotonic() - started < 2.0
    assert holder["result"].state == "cancelled"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox contract")
def test_nw_040_041_macos_sandbox_hides_worker_identity_and_denies_undeclared_network(tmp_path):
    worker_root = tmp_path / "worker"
    secret = worker_root / "identity" / "device-key.pem"
    secret.parent.mkdir(parents=True)
    secret.write_text("PRIVATE-DEVICE-KEY")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    login_home = Path.home()
    repository = Path(__file__).resolve().parents[1]
    script = (
        "import json,os,pathlib,socket; "
        f"secret=pathlib.Path({str(secret)!r}); "
        f"probes={[str(login_home / 'Desktop'), str(login_home / 'Downloads'), str(login_home / '.config'), str(repository)]!r}; "
        "readable=True; network=True; hidden=[]; "
        "\ntry: secret.read_text()\nexcept Exception: readable=False\n"
        "\nfor probe in probes:\n"
        " try: pathlib.Path(probe).iterdir().__next__(); hidden.append(False)\n"
        " except Exception: hidden.append(True)\n"
        f"\ntry:\n s=socket.create_connection(('127.0.0.1',{port}),timeout=.2); s.close()\nexcept Exception: network=False\n"
        "pathlib.Path(os.environ['ACROSS_OUTPUT_DIR'],'policy.json').write_text(json.dumps({'identity_readable':readable,'network_reachable':network,'private_paths_hidden':all(hidden),'hidden_probes':hidden}))"
    )
    job = manifest(command_argv=(sys.executable, "-c", script), expected_outputs=("policy.json",))
    from across_orchestrator.worker_protocol import JobLease

    lease = JobLease(
        lease_id="lease-policy", job_id=job.job_id, run_id=job.run_id, node_id="node-test", attempt=1,
        manifest_hash=job.manifest_hash, issued_at=time.time(), expires_at=time.time() + 60, heartbeat_interval_seconds=10,
    )
    try:
        result = BoundedProcessExecutor(worker_root).execute(job, lease)
    finally:
        listener.close()
    assert result.state == "completed"
    policy = json.loads((result.sandbox / "output" / "policy.json").read_text())
    assert policy == {"identity_readable": False, "network_reachable": False, "private_paths_hidden": True, "hidden_probes": [True, True, True, True]}
    profile = (result.sandbox / ".across-sandbox.sb").read_text()
    assert "(deny default)" in profile
    assert "(allow default)" not in profile


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox contract")
def test_macos_sandbox_allows_only_declared_worker_local_gateway(tmp_path):
    worker_root = tmp_path / "worker"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    received = Event()

    def accept_once():
        connection, _ = listener.accept()
        connection.close()
        received.set()

    Thread(target=accept_once, daemon=True).start()
    script = (
        "import os,pathlib,socket; "
        f"s=socket.create_connection(('127.0.0.1',{port}),timeout=2); s.close(); "
        "pathlib.Path(os.environ['ACROSS_OUTPUT_DIR'],'connected.txt').write_text('ok')"
    )
    job = manifest(
        command_argv=(sys.executable, "-c", script),
        expected_outputs=("connected.txt",),
        permissions={
            "filesystem": {"mode": "run-scoped"},
            "network": {"mode": "allowlist", "purposes": ["aaa-model-gateway"]},
            "model": {"mode": "none"},
        },
    )
    from across_orchestrator.worker_protocol import JobLease

    lease = JobLease(
        lease_id="lease-loopback", job_id=job.job_id, run_id=job.run_id, node_id="node-test", attempt=1,
        manifest_hash=job.manifest_hash, issued_at=time.time(), expires_at=time.time() + 60, heartbeat_interval_seconds=10,
    )
    try:
        result = BoundedProcessExecutor(worker_root).execute(
            job,
            lease,
            extra_env={"ACROSS_MODEL_GATEWAY_URL": f"http://127.0.0.1:{port}/invoke"},
        )
    finally:
        listener.close()

    assert result.state == "completed"
    assert received.wait(1)
    profile = (result.sandbox / ".across-sandbox.sb").read_text()
    assert f'(allow network-outbound (remote tcp "localhost:{port}"))' in profile
    assert "(deny network*)" not in profile


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS resource watchdog contract")
def test_nw_030_macos_resource_watchdog_enforces_memory_budget(tmp_path):
    from across_orchestrator.worker_protocol import JobLease

    job = manifest(
        job_id="job-memory-budget",
        run_id="run-memory-budget",
        command_argv=(sys.executable, "-c", "import time; value=bytearray(96*1024*1024); time.sleep(5)"),
        budgets={"timeout_seconds": 10, "memory_bytes": 64 * 1024**2, "process_count": 16, "max_output_bytes": 1024 * 1024},
    )
    lease = JobLease(
        lease_id="lease-memory-budget", job_id=job.job_id, run_id=job.run_id, node_id="node-test", attempt=1,
        manifest_hash=job.manifest_hash, issued_at=time.time(), expires_at=time.time() + 60, heartbeat_interval_seconds=10,
    )
    result = BoundedProcessExecutor(tmp_path / "worker").execute(job, lease)
    assert result.state == "failed"
    assert result.reason_category == "memory_budget_exceeded"
    assert result.resource_usage["peak_memory_bytes"] >= 64 * 1024**2


def test_nw_037_038_chunked_artifact_resume_and_integrity(tmp_path):
    payload = os.urandom(2 * 1024 * 1024 + 17)
    descriptor = ArtifactDescriptor(
        artifact_id="artifact-test", run_id="run-test", job_id="job-test", node_id="node-test",
        logical_name="result.bin", media_type="application/octet-stream", size=len(payload), sha256=sha256(payload).hexdigest(),
    )
    receiver = ChunkedArtifactReceiver(tmp_path)
    receiver.begin(descriptor, chunk_size=1024 * 1024)
    chunks = [payload[index : index + 1024 * 1024] for index in range(0, len(payload), 1024 * 1024)]
    for index, chunk in enumerate(chunks[:2]):
        receiver.write_chunk(descriptor.artifact_id, index, chunk, sha256(chunk).hexdigest())
    restarted = ChunkedArtifactReceiver(tmp_path)
    assert restarted.confirmed_chunks(descriptor.artifact_id) == [0, 1]
    for index, chunk in enumerate(chunks[2:], start=2):
        restarted.write_chunk(descriptor.artifact_id, index, chunk, sha256(chunk).hexdigest())
    duplicate = restarted.write_chunk(descriptor.artifact_id, 0, chunks[0], sha256(chunks[0]).hexdigest())
    assert duplicate["duplicate"] is True
    final = restarted.finalize(descriptor.artifact_id)
    assert final.read_bytes() == payload
    with pytest.raises(ProtocolError, match="hash mismatch"):
        restarted.write_chunk(descriptor.artifact_id, 9, b"bad", "0" * 64)


def test_nw_013_014_015_029_relay_is_opaque_authenticated_and_replay_safe():
    clock = Clock()
    router = RelayRouter(clock=clock)
    router.register_session("session-test", ["node-host", "node-worker"], ttl_seconds=120)
    key = bytes(range(32))
    payload = {"job": {"prompt": "confidential", "artifact": "secret-body"}}
    frame = seal_relay_payload(
        key, session_id="session-test", source_node_id="node-host", target_node_id="node-worker",
        sequence=1, payload=payload, expires_at=clock() + 60, nonce=b"0" * 12,
    )
    routed = router.route(frame)
    assert "confidential" not in canonical_json(routed)
    assert "secret-body" not in canonical_json(routed)
    assert open_relay_payload(key, RelayFrame.from_dict(routed), now=clock()) == payload
    with pytest.raises(ProtocolError, match="replay"):
        router.route(frame)
    tampered = frame.to_dict()
    tampered["ciphertext"] = tampered["ciphertext"][:-2] + "AA"
    with pytest.raises(ProtocolError, match="authentication"):
        open_relay_payload(key, RelayFrame.from_dict(tampered), now=clock())
    with pytest.raises(ProtocolError, match="cross-node"):
        router.route(replace(frame, target_node_id="node-other", sequence=2))
    router.revoke_session("session-test")
    with pytest.raises(ProtocolError, match="unavailable"):
        router.route(replace(frame, sequence=2))


def test_nw_013_014_relay_network_routes_only_ciphertext_between_mutually_authenticated_peers(tmp_path):
    ca, server_cert, server_key, _, _ = _write_tls_fixture(tmp_path / "tls")
    host_cert, host_key = _issue_tls_client(tmp_path / "tls", "node-host")
    worker_cert, worker_key = _issue_tls_client(tmp_path / "tls", "node-worker")
    router = RelayRouter()
    key = bytes(range(32))

    async def exercise():
        server = AsyncRelayServer(
            router,
            host="127.0.0.1",
            port=0,
            ssl_context=create_tls_context(server=True, certificate=server_cert, private_key=server_key, trust_store=ca),
        )
        await server.start()
        host = RelayEndpoint(
            host="127.0.0.1",
            port=server.bound_port,
            server_hostname="localhost",
            ssl_context=create_tls_context(server=False, certificate=host_cert, private_key=host_key, trust_store=ca),
            node_id="node-host",
            peer_node_id="node-worker",
            session_id="session-network",
            session_key=key,
        )
        worker = RelayEndpoint(
            host="127.0.0.1",
            port=server.bound_port,
            server_hostname="localhost",
            ssl_context=create_tls_context(server=False, certificate=worker_cert, private_key=worker_key, trust_store=ca),
            node_id="node-worker",
            peer_node_id="node-host",
            session_id="session-network",
            session_key=key,
        )
        await host.connect()
        registered = await host.register_session()
        assert registered["node_ids"] == ["node-host", "node-worker"]
        await worker.connect()
        try:
            frame = await host.send({"type": "coordinator.job", "prompt": "relay-secret", "artifact": "private-body"})
            received = await worker.receive()
            await worker.send({"type": "worker.event", "state": "completed", "job_id": "job-relay"})
            returned = await host.receive()
            return frame, received, returned
        finally:
            await host.close()
            await worker.close()
            await server.close()

    frame, received, returned = asyncio.run(exercise())
    assert "relay-secret" not in canonical_json(frame.to_dict())
    assert "private-body" not in canonical_json(frame.to_dict())
    assert received["type"] == "coordinator.job"
    assert returned == {"type": "worker.event", "state": "completed", "job_id": "job-relay"}
    assert router.health()["stores_job_content"] is False


def test_nw_013_relay_runs_complete_job_and_returns_verified_artifact(tmp_path):
    ca, server_cert, server_key, _, _ = _write_tls_fixture(tmp_path / "tls-job")
    host_cert, host_key = _issue_tls_client(tmp_path / "tls-job", "node-host")
    worker_cert, worker_key = _issue_tls_client(tmp_path / "tls-job", "node-test")
    router = RelayRouter()
    key = bytes(reversed(range(32)))
    coordinator = approved_coordinator(tmp_path / "coordinator")
    command = (
        sys.executable,
        "-c",
        "import os,pathlib; pathlib.Path(os.environ['ACROSS_OUTPUT_DIR'],'result.json').write_text('{\"transport\":\"relay\"}')",
    )
    job = manifest(job_id="job-relay-e2e", run_id="run-relay-e2e", command_argv=command, expected_outputs=("result.json",))
    coordinator.submit_job(job)

    async def exercise():
        server = AsyncRelayServer(router, host="127.0.0.1", port=0, ssl_context=create_tls_context(server=True, certificate=server_cert, private_key=server_key, trust_store=ca))
        await server.start()
        host_context = create_tls_context(server=False, certificate=host_cert, private_key=host_key, trust_store=ca)
        worker_context = create_tls_context(server=False, certificate=worker_cert, private_key=worker_key, trust_store=ca)
        host = RelayEndpoint(host="127.0.0.1", port=server.bound_port, server_hostname="localhost", ssl_context=host_context, node_id="node-host", peer_node_id="node-test", session_id="session-job", session_key=key)
        worker = RelayEndpoint(host="127.0.0.1", port=server.bound_port, server_hostname="localhost", ssl_context=worker_context, node_id="node-test", peer_node_id="node-host", session_id="session-job", session_key=key)
        await host.connect()
        await host.register_session()
        await worker.connect()
        try:
            host_session = RelayCoordinatorSession(coordinator, host, artifact_root=tmp_path / "artifacts")
            worker_session = RelayWorkerSessionClient(endpoint=worker, capability=capability(), worker_root=tmp_path / "worker")
            host_result, worker_result = await asyncio.gather(host_session.run_once(), worker_session.run_once())
            return host_result, worker_result
        finally:
            await host.close()
            await worker.close()
            await server.close()

    host_result, worker_result = asyncio.run(exercise())
    assert host_result == "node-test"
    assert worker_result.status == "completed"
    assert worker_result.transport == "relay"
    assert coordinator.job(job.job_id)["status"] == "completed"
    artifact = next((tmp_path / "artifacts").glob("*/artifact.bin"))
    assert artifact.read_text() == '{"transport":"relay"}'


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="bounded loopback allowlists use sandbox-exec; Linux requires the OCI executor",
)
def test_sim_004_relay_live_model_uses_e2e_grant_proxy_without_worker_credentials(tmp_path):
    ca, server_cert, server_key, _, _ = _write_tls_fixture(tmp_path / "tls-relay-model")
    host_cert, host_key = _issue_tls_client(tmp_path / "tls-relay-model", "node-host")
    worker_cert, worker_key = _issue_tls_client(tmp_path / "tls-relay-model", "node-test")
    router = RelayRouter()
    key = bytes(range(32))
    coordinator = approved_coordinator(tmp_path / "coordinator-model")
    script = """
import json, os, pathlib, urllib.request
payload = {
  'grant_id': os.environ['ACROSS_MODEL_GRANT_ID'],
  'run_id': os.environ['ACROSS_RUN_ID'],
  'job_id': os.environ['ACROSS_JOB_ID'],
  'node_id': os.environ['ACROSS_NODE_ID'],
  'purpose': 'scenario_round_annotation',
  'message': 'bounded relay model request',
  'max_tokens': 8,
}
request = urllib.request.Request(os.environ['ACROSS_MODEL_GATEWAY_URL'], data=json.dumps(payload).encode(), headers={'content-type':'application/json'}, method='POST')
with urllib.request.urlopen(request, timeout=10) as response:
  result = json.load(response)
pathlib.Path(os.environ['ACROSS_OUTPUT_DIR'], 'result.json').write_text(json.dumps(result, sort_keys=True))
"""
    job = manifest(
        job_id="job-relay-model",
        run_id="run-relay-model",
        command_argv=(sys.executable, "-c", script),
        permissions={"filesystem": {"mode": "run-scoped"}, "network": {"mode": "allowlist", "purposes": ["aaa-model-gateway"]}, "model": {"mode": "grant-required"}},
        model_policy={"enabled": True, "policy": "host-default", "max_calls": 1, "max_tokens": 8, "max_concurrency": 1, "max_cost_usd": 1.0, "timeout_seconds": 30},
        expected_outputs=("result.json",),
    )
    coordinator.submit_job(job)
    forwarded = []

    async def gateway(payload):
        forwarded.append(dict(payload))
        body = json.dumps({"content": "relay-model-ok", "usage": {"tokens": 2, "cost_usd": 0.01}}).encode()
        return {"status_code": 200, "body_base64": base64.b64encode(body).decode("ascii")}

    async def exercise():
        server = AsyncRelayServer(router, host="127.0.0.1", port=0, ssl_context=create_tls_context(server=True, certificate=server_cert, private_key=server_key, trust_store=ca))
        await server.start()
        host = RelayEndpoint(host="127.0.0.1", port=server.bound_port, server_hostname="localhost", ssl_context=create_tls_context(server=False, certificate=host_cert, private_key=host_key, trust_store=ca), node_id="node-host", peer_node_id="node-test", session_id="session-model", session_key=key)
        worker = RelayEndpoint(host="127.0.0.1", port=server.bound_port, server_hostname="localhost", ssl_context=create_tls_context(server=False, certificate=worker_cert, private_key=worker_key, trust_store=ca), node_id="node-test", peer_node_id="node-host", session_id="session-model", session_key=key)
        await host.connect()
        await host.register_session()
        await worker.connect()
        try:
            return await asyncio.gather(
                RelayCoordinatorSession(coordinator, host, artifact_root=tmp_path / "artifacts-model", model_gateway=gateway).run_once(),
                RelayWorkerSessionClient(endpoint=worker, capability=capability(), worker_root=tmp_path / "worker-model").run_once(),
            )
        finally:
            await host.close()
            await worker.close()
            await server.close()

    _, worker_result = asyncio.run(exercise())
    assert worker_result.status == "completed"
    assert forwarded[0]["message"] == "bounded relay model request"
    assert forwarded[0]["node_id"] == "node-test"
    assert not any(name for name in os.environ if name.startswith("OPENAI_") and name in forwarded[0])
    result = json.loads(next((tmp_path / "artifacts-model").glob("*/artifact.bin")).read_text())
    assert result["content"] == "relay-model-ok"


def test_nw_042_044_045_model_grants_are_bound_budgeted_and_revocable(tmp_path):
    coordinator = approved_coordinator(tmp_path)
    job = manifest()
    coordinator.submit_job(job)
    lease = coordinator.lease_next("node-test")
    assert lease
    grant = coordinator.issue_model_grant(job_id=job.job_id, node_id="node-test", max_calls=1, max_tokens=10, max_cost_usd=1.0)
    usage = coordinator.consume_model_grant(
        grant.grant_id, run_id=job.run_id, job_id=job.job_id, node_id="node-test", audience="aaa-model-gateway",
        scope="model.invoke", purpose="workflow", tokens=5, cost_usd=0.1,
    )
    assert usage["calls"] == 1
    with pytest.raises(CoordinatorError, match="budget"):
        coordinator.consume_model_grant(
            grant.grant_id, run_id=job.run_id, job_id=job.job_id, node_id="node-test", audience="aaa-model-gateway",
            scope="model.invoke", purpose="workflow", tokens=1, cost_usd=0.1,
        )
    coordinator.revoke_model_grant(grant.grant_id)
    with pytest.raises(ProtocolError, match="revoked"):
        coordinator.consume_model_grant(
            grant.grant_id, run_id=job.run_id, job_id=job.job_id, node_id="node-test", audience="aaa-model-gateway",
            scope="model.invoke", purpose="workflow", tokens=0, cost_usd=0,
        )


def test_nw_042_044_model_grant_call_reservations_enforce_concurrency_and_actual_usage(tmp_path):
    coordinator = approved_coordinator(tmp_path)
    job = manifest()
    coordinator.submit_job(job)
    assert coordinator.lease_next("node-test")
    grant = coordinator.issue_model_grant(
        job_id=job.job_id,
        node_id="node-test",
        max_calls=2,
        max_tokens=20,
        max_concurrency=1,
        max_cost_usd=1,
    )
    started = coordinator.begin_model_grant_call(
        grant.grant_id,
        run_id=job.run_id,
        job_id=job.job_id,
        node_id="node-test",
        audience="aaa-model-gateway",
        scope="model.invoke",
        purpose="workflow",
        requested_tokens=10,
    )
    with pytest.raises(CoordinatorError, match="concurrency"):
        coordinator.begin_model_grant_call(
            grant.grant_id,
            run_id=job.run_id,
            job_id=job.job_id,
            node_id="node-test",
            audience="aaa-model-gateway",
            scope="model.invoke",
            purpose="workflow",
            requested_tokens=1,
        )
    usage = coordinator.finish_model_grant_call(grant.grant_id, started["call_id"], tokens=4, cost_usd=0.1)
    assert usage == {"calls": 1, "tokens": 4, "cost_usd": 0.1, "active_calls": 0, "outcome": "completed"}


def test_security_model_grant_rejects_wrong_binding_scope_audience_and_expiry(tmp_path):
    clock = Clock()
    coordinator = approved_coordinator(tmp_path, clock=clock)
    job = manifest()
    coordinator.submit_job(job)
    assert coordinator.lease_next("node-test")
    grant = coordinator.issue_model_grant(
        job_id=job.job_id,
        node_id="node-test",
        audience="aaa-model-gateway",
        scopes=("model.invoke",),
        purposes=("workflow",),
        max_calls=10,
        max_tokens=100,
        max_cost_usd=1,
        ttl_seconds=30,
    )
    base = dict(
        run_id=job.run_id,
        job_id=job.job_id,
        node_id="node-test",
        audience="aaa-model-gateway",
        scope="model.invoke",
        purpose="workflow",
        tokens=1,
    )
    for field, value in (
        ("run_id", "run-other"),
        ("job_id", "job-other"),
        ("node_id", "node-other"),
        ("audience", "worker-pass-through"),
        ("scope", "model.admin"),
        ("purpose", "unapproved-purpose"),
    ):
        attempt = {**base, field: value}
        with pytest.raises(ProtocolError):
            coordinator.consume_model_grant(grant.grant_id, **attempt)
    clock.advance(31)
    with pytest.raises(ProtocolError, match="expired"):
        coordinator.consume_model_grant(grant.grant_id, **base)


def test_security_rejects_false_isolation_symlink_artifact_archive_escape_and_log_flood(tmp_path):
    assert not capability("node-false", capability_source="self-report", isolation_level="isolated", executors=("oci-container",)).supports({"isolation_level": "isolated"})
    from across_orchestrator.worker_protocol import JobLease

    executor = BoundedProcessExecutor(tmp_path / "worker")
    symlink_job = manifest(
        job_id="job-symlink",
        run_id="run-symlink",
        command_argv=(sys.executable, "-c", "import os,pathlib; pathlib.Path(os.environ['ACROSS_OUTPUT_DIR'],'result.txt').symlink_to('/etc/passwd')"),
        expected_outputs=("result.txt",),
    )
    symlink_lease = JobLease(lease_id="lease-symlink", job_id=symlink_job.job_id, run_id=symlink_job.run_id, node_id="node-test", attempt=1, manifest_hash=symlink_job.manifest_hash, issued_at=time.time(), expires_at=time.time() + 60, heartbeat_interval_seconds=10)
    result = executor.execute(symlink_job, symlink_lease)
    assert result.state == "failed" and result.reason_category == "artifact_validation_failed"

    flood_job = manifest(
        job_id="job-flood",
        run_id="run-flood",
        command_argv=(sys.executable, "-c", "import sys,time; sys.stdout.write('x'*200000); sys.stdout.flush(); time.sleep(5)"),
        budgets={"timeout_seconds": 10, "memory_bytes": 128 * 1024**2, "max_output_bytes": 4096},
    )
    flood_lease = JobLease(lease_id="lease-flood", job_id=flood_job.job_id, run_id=flood_job.run_id, node_id="node-test", attempt=1, manifest_hash=flood_job.manifest_hash, issued_at=time.time(), expires_at=time.time() + 60, heartbeat_interval_seconds=10)
    flooded = executor.execute(flood_job, flood_lease)
    assert flooded.state == "failed" and flooded.reason_category == "output_budget_exceeded"

    for kind in ("traversal", "symlink", "bomb"):
        archive_path = tmp_path / f"unsafe-{kind}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            if kind == "traversal":
                info = tarfile.TarInfo("../escape")
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
            elif kind == "symlink":
                info = tarfile.TarInfo("link")
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                archive.addfile(info)
            else:
                for index in range(10_001):
                    archive.addfile(tarfile.TarInfo(f"empty-{index}"))
        with pytest.raises(ValueError, match="unsafe|too large"):
            install_worker_pack(tmp_path / f"pack-root-{kind}", artifact=str(archive_path), expected_sha256=sha256(archive_path.read_bytes()).hexdigest())


def test_nw_002_003_004_005_036_050_051_worker_lifecycle_is_user_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    root = tmp_path / "worker-dev" / "run-test"
    installed = install_worker(root, start=False, label="com.across.worker.test")
    assert installed["status"] == "installed"
    assert str(root) in installed["home"]
    join = create_join_request(root, pairing_code="1111-2222-3333", enrollment_id="enrollment-test", display_name="Test", node_id="node-test")
    assert join["contains_private_key"] is False
    assert (root / "identity" / "device-key.pem").stat().st_mode & 0o777 == 0o600
    (root / "runtime" / "site-packages").mkdir(parents=True)
    (root / "runtime" / "site-packages" / "provider_adapter.py").write_text(
        "OPENAI_API_KEY=\"documented-placeholder-value\"\n",
        encoding="utf-8",
    )
    (root / "bootstrap" / "venv" / "site-packages").mkdir(parents=True)
    (root / "bootstrap" / "venv" / "site-packages" / "ssh.py").write_text(
        "EXAMPLE = 'documentation-placeholder-value'\n",
        encoding="utf-8",
    )
    assert worker_status(root)["provider_key_present"] is False
    (root / "state" / "unexpected-provider.env").write_text(
        "OPENAI_API_KEY=actual-secret-shaped-value\n",
        encoding="utf-8",
    )
    assert worker_status(root)["provider_key_present"] is True
    (root / "state" / "unexpected-provider.env").unlink()
    artifact = tmp_path / "worker.tar.gz"
    with tarfile.open(artifact, "w:gz") as archive:
        for name, body, mode in (
            ("worker-distribution.json", b'{"schema_version":"across-worker-distribution/1.0","version":"0.10.1-rc.1","entrypoint":"src/across_orchestrator/worker_cli.py"}', 0o644),
            ("src/across_orchestrator/__init__.py", b"", 0o644),
            ("src/across_orchestrator/worker_cli.py", b"CANDIDATE = True\n", 0o644),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = mode
            archive.addfile(info, io.BytesIO(body))
    digest = sha256(artifact.read_bytes()).hexdigest()
    updated = update_worker(
        root,
        artifact=str(artifact),
        expected_sha256=digest,
        version="0.10.1-rc.1",
        restart_service=False,
    )
    assert updated["status"] == "updated"
    assert updated["service_restart"]["reason"] == "deferred_until_session_acknowledged"
    assert (root / "runtime" / "current").resolve().name == "0.10.1-rc.1"
    (root / "state" / "session.json").write_text(
        json.dumps(
            {
                "schema_version": "across-worker-session-config/1.0",
                "endpoint": "https://coordinator.example.invalid:9443",
                "transport": "direct",
                "ca_file": str(root / "identity" / "coordinator-ca.pem"),
                "certificate": str(root / "identity" / "device-certificate.pem"),
                "private_key": str(root / "identity" / "device-key.pem"),
            }
        ),
        encoding="utf-8",
    )
    switched = apply_transport_directive(
        root,
        directive={
            "transport": "relay",
            "endpoint": "https://relay.example.invalid:9444",
            "relay_session_id": "relay-session-test",
            "relay_peer_node_id": "node-host",
            "relay_session_key": base64.urlsafe_b64encode(b"r" * 32).decode("ascii"),
        },
    )
    assert switched["transport"] == "relay"
    assert json.loads((root / "state" / "session.json").read_text())["relay_peer_node_id"] == "node-host"
    rolled_back = rollback_worker(root)
    assert rolled_back["status"] == "rolled_back"
    assert (root / "runtime" / "current").resolve().name == WORKER_VERSION
    pack = tmp_path / "scenario-pack.tar.gz"
    with tarfile.open(pack, "w:gz") as archive:
        for name, body, mode in (
            ("pack.json", b'{"schema_version":"across-worker-pack/1.0","pack_id":"scenario-simulation","version":"1.0.0","entrypoint":"bin/across-scenario-simulation"}', 0o644),
            ("bin/across-scenario-simulation", b"#!/bin/sh\nexit 0\n", 0o755),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = mode
            archive.addfile(info, io.BytesIO(body))
    pack_hash = sha256(pack.read_bytes()).hexdigest()
    assert install_worker_pack(root, artifact=str(pack), expected_sha256=pack_hash)["status"] == "installed"
    assert list_worker_packs(root)[0]["pack_id"] == "scenario-simulation"
    assert remove_worker_pack(root, pack_id="scenario-simulation")["removed"] is True
    assert leave_worker(root)["status"] == "left"
    result = uninstall_worker(root, purge=True, label="com.across.worker.test")
    assert result["purged"] is True
    assert not root.exists()


def test_worker_renews_identity_automatically_before_or_after_expiry(tmp_path, monkeypatch):
    root = tmp_path / "worker-renewal"
    create_join_request(
        root,
        pairing_code="1111-2222-3333",
        enrollment_id="enrollment-renewal",
        display_name="Renewal Worker",
        node_id="node-renewal",
        endpoint="https://127.0.0.1:39443",
        transport="direct",
        enrollment_endpoint="https://127.0.0.1:39445",
    )
    node_path = root / "state" / "node.json"
    node = json.loads(node_path.read_text())
    node.update({"identity_generation": 1, "certificate_not_after": time.time() - 1})
    node_path.write_text(json.dumps(node), encoding="utf-8")
    assert worker_cli_module._identity_renewal_due(node)
    assert not worker_cli_module._identity_renewal_due({**node, "certificate_not_after": time.time() + 8 * 24 * 60 * 60})

    observed = {}

    def fake_post(url, payload, *, ca_file):
        observed.update({"url": url, "payload": payload, "ca_file": ca_file})
        proof = {
            "schema_version": "across-worker-identity-renewal-proof/1.0",
            "node_id": payload["node_id"],
            "current_generation": payload["current_generation"],
            "nonce": payload["nonce"],
        }
        public_key = serialization.load_pem_public_key((root / "identity" / "device-public.pem").read_bytes())
        public_key.verify(
            base64.urlsafe_b64decode(payload["signature"] + "=" * (-len(payload["signature"]) % 4)),
            canonical_json(proof).encode("utf-8"),
        )
        return {
            "status": "renewed",
            "activation": {
                "schema_version": "across-worker-activation/1.0",
                "node_id": "node-renewal",
                "session_generation": 2,
            },
        }

    def fake_activate(worker_root, *, approval_file):
        activation = json.loads(Path(approval_file).read_text())["activation"]
        refreshed = json.loads(node_path.read_text())
        refreshed.update(
            {
                "identity_generation": activation["session_generation"],
                "certificate_not_after": time.time() + 30 * 24 * 60 * 60,
            }
        )
        node_path.write_text(json.dumps(refreshed), encoding="utf-8")
        return {"status": "activated", "node_id": "node-renewal"}

    monkeypatch.setattr(worker_cli_module, "_enrollment_post", fake_post)
    monkeypatch.setattr(worker_cli_module, "activate_worker", fake_activate)
    renewed = worker_cli_module._renew_identity_once(root)
    assert renewed["status"] == "activated"
    assert observed["url"] == "https://127.0.0.1:39445/v1/identity/renew"
    assert observed["payload"]["current_generation"] == 1
    refreshed = json.loads(node_path.read_text())
    assert refreshed["identity_generation"] == 2
    assert refreshed["last_identity_renewed_at"] > 0
    assert not list((root / "state").glob(".identity-renewal-*.json"))


def test_nw_036_coordinator_drains_and_tracks_verified_worker_update(tmp_path):
    coordinator = approved_coordinator(tmp_path)
    requested = coordinator.request_node_update(
        "node-test",
        version="0.10.1",
        url="https://example.invalid/across-worker-macos-arm64.tar.gz",
        sha256_value="d" * 64,
    )
    assert requested["state"] == "draining"
    directive = coordinator.node_update_directive("node-test")
    assert directive["version"] == "0.10.1"
    assert directive["status"] == "requested"
    downloading = coordinator.record_node_update(
        "node-test", directive_id=directive["directive_id"], status="downloading"
    )
    assert downloading["update_directive"]["status"] == "downloading"
    completed = coordinator.record_node_update(
        "node-test", directive_id=directive["directive_id"], status="completed"
    )
    assert completed["state"] == "offline"
    assert completed["draining"] is False
    assert coordinator.node_update_directive("node-test") is None
    with pytest.raises(CoordinatorError, match="credential-free HTTPS"):
        coordinator.request_node_update(
            "node-test", version="0.10.2", url="https://user:secret@example.invalid/worker.tgz", sha256_value="e" * 64
        )


def test_nw_015_transport_switch_keeps_identity_and_hides_relay_key(tmp_path):
    coordinator = approved_coordinator(tmp_path)
    session_key = base64.urlsafe_b64encode(b"r" * 32).decode("ascii")
    public = coordinator.request_node_transport(
        "node-test",
        transport="relay",
        endpoint="https://relay.example.invalid:9444",
        relay_session_id="relay-session-test",
        relay_peer_node_id="node-host",
        relay_session_key=session_key,
    )
    assert public["transport_directive"]["relay_session_key"] == "[redacted]"
    directive = coordinator.node_transport_directive("node-test")
    assert directive["relay_session_key"] == session_key
    coordinator.record_node_transport(
        "node-test", directive_id=directive["directive_id"], status="applying"
    )
    completed = coordinator.record_node_transport(
        "node-test", directive_id=directive["directive_id"], status="completed"
    )
    assert completed["node_id"] == "node-test"
    assert completed["transport"] == "relay"
    assert "relay_session_key" not in canonical_json(completed)


def test_nw_047_public_redaction_removes_tokens_and_user_paths():
    private_path = "/".join(("", "Users", "alice", "private", "project"))
    secret_value = "-".join(("sk", "example-secret-value"))
    value = sanitize_public(
        {
            "api_key": secret_value,
            "message": f"failed under {private_path} with Bearer abc.def.ghi",
        }
    )
    rendered = canonical_json(value)
    assert "sk-example" not in rendered
    assert private_path not in rendered
    assert "abc.def.ghi" not in rendered


def test_nw_046_evidence_receipt_binds_execution_artifacts_model_and_cleanup():
    job = manifest(evidence_requirements=("node", "model_usage", "cleanup_status"))
    lease = JobLease(
        lease_id="lease-evidence", job_id=job.job_id, run_id=job.run_id,
        node_id="node-test", attempt=1, manifest_hash=job.manifest_hash,
        issued_at=10, expires_at=70, heartbeat_interval_seconds=10,
    )
    artifact = ArtifactDescriptor(
        artifact_id="artifact-evidence", run_id=job.run_id, job_id=job.job_id, node_id="node-test",
        logical_name="result.json",
        media_type="application/json", size=2, sha256=sha256(b"{}").hexdigest(),
    )
    receipt = build_evidence_receipt(
        manifest=job,
        node=capability(),
        lease=lease,
        terminal_state="completed",
        artifacts=(artifact,),
        quality_gates={"artifact_hashes_match": True},
        model_usage={"calls": 1, "api_key": "must-not-leak"},
        cleanup_status="complete",
        started_at=11,
        ended_at=12,
    )
    assert receipt["manifest_hash"] == job.manifest_hash
    assert receipt["lease_id"] == lease.lease_id
    assert receipt["node"]["node_id"] == "node-test"
    assert receipt["artifacts"][0]["sha256"] == artifact.sha256
    assert receipt["cleanup_status"] == "complete"
    assert receipt["receipt_hash"] == payload_hash({key: value for key, value in receipt.items() if key != "receipt_hash"})
    assert "must-not-leak" not in canonical_json(receipt)


def test_security_direct_mtls_rejects_wrong_ca_forged_expired_and_revoked_identity(tmp_path):
    coordinator = approved_coordinator(tmp_path / "coordinator")
    ca, server_cert, server_key, client_cert, client_key = _write_tls_fixture(tmp_path / "trusted")
    wrong_ca, _, _, forged_cert, forged_key = _write_tls_fixture(tmp_path / "forged")
    _, _, _, expired_cert, expired_key = _write_tls_fixture(tmp_path / "expired", expired_client=True)
    _bind_node_certificate(coordinator, "node-test", client_cert)

    async def exercise():
        server = CoordinatorSessionServer(
            coordinator,
            host="127.0.0.1",
            port=0,
            ssl_context=tls_server_context(certificate=server_cert, private_key=server_key, client_ca=ca),
            artifact_root=tmp_path / "received",
        )
        await server.start()
        try:
            async def rejected(certificate, private_key, server_ca):
                client = WorkerSessionClient(
                    host="127.0.0.1",
                    port=server.bound_port,
                    server_hostname="localhost",
                    ssl_context=tls_client_context(certificate=certificate, private_key=private_key, server_ca=server_ca),
                    capability=capability(),
                    worker_root=tmp_path / "worker",
                )
                with pytest.raises((ssl.SSLError, ConnectionError, asyncio.IncompleteReadError, WorkerTransportError)):
                    await client.run_once()

            await rejected(client_cert, client_key, wrong_ca)
            await rejected(forged_cert, forged_key, ca)
            await rejected(expired_cert, expired_key, ca)
            coordinator.enrollment.revoke("node-test")
            await rejected(client_cert, client_key, ca)
        finally:
            await server.close()

    asyncio.run(exercise())


def test_security_direct_mtls_rejects_cross_node_impersonation_with_trusted_certificate(tmp_path):
    coordinator = approved_coordinator(tmp_path / "coordinator")
    ca, server_cert, server_key, client_cert, _ = _write_tls_fixture(tmp_path / "trusted")
    _bind_node_certificate(coordinator, "node-test", client_cert)
    other_cert, other_key = _issue_tls_client(tmp_path / "trusted", "node-other")

    async def exercise():
        server = CoordinatorSessionServer(
            coordinator,
            host="127.0.0.1",
            port=0,
            ssl_context=tls_server_context(certificate=server_cert, private_key=server_key, client_ca=ca),
            artifact_root=tmp_path / "received",
        )
        await server.start()
        try:
            client = WorkerSessionClient(
                host="127.0.0.1",
                port=server.bound_port,
                server_hostname="localhost",
                ssl_context=tls_client_context(certificate=other_cert, private_key=other_key, server_ca=ca),
                capability=capability("node-test"),
                worker_root=tmp_path / "worker",
            )
            with pytest.raises((asyncio.IncompleteReadError, ConnectionError, WorkerTransportError)):
                await client.run_once()
        finally:
            await server.close()

    asyncio.run(exercise())


def test_nw_010_023_025_037_direct_mtls_worker_executes_and_uploads_verified_artifact(tmp_path):
    coordinator = approved_coordinator(tmp_path / "coordinator")
    input_payload = b'{"message":"remote-ok"}'
    job = manifest(
        command_argv=(
            sys.executable,
            "-c",
            "import json,os,pathlib; value=json.loads(pathlib.Path(os.environ['ACROSS_INPUT_DIR'],'input.json').read_text()); pathlib.Path(os.environ['ACROSS_OUTPUT_DIR'],'result.txt').write_text(value['message'])",
        ),
        input_artifacts=({"logical_name": "input.json", "sha256": sha256(input_payload).hexdigest(), "sensitivity": "internal"},),
        expected_outputs=("result.txt",),
    )
    coordinator.submit_job(job, input_payloads={"input.json": input_payload})
    ca, server_cert, server_key, client_cert, client_key = _write_tls_fixture(tmp_path / "tls")
    _bind_node_certificate(coordinator, "node-test", client_cert)

    async def exercise():
        server = CoordinatorSessionServer(
            coordinator,
            host="127.0.0.1",
            port=0,
            ssl_context=tls_server_context(certificate=server_cert, private_key=server_key, client_ca=ca),
            artifact_root=tmp_path / "received",
        )
        await server.start()
        try:
            client = WorkerSessionClient(
                host="127.0.0.1",
                port=server.bound_port,
                server_hostname="localhost",
                ssl_context=tls_client_context(certificate=client_cert, private_key=client_key, server_ca=ca),
                capability=capability(),
                worker_root=tmp_path / "worker",
            )
            return await client.run_once()
        finally:
            await server.close()

    result = asyncio.run(exercise())
    assert result.status == "completed"
    assert result.tls_version == "TLSv1.3"
    assert result.transport == "direct"
    stored = coordinator.job(job.job_id)
    assert stored["status"] == "completed"
    assert [event["state"] for event in stored["events"]] == ["preparing", "running", "completed"]
    received = list((tmp_path / "received").glob("*/artifact.bin"))
    assert len(received) == 1
    assert received[0].read_text() == "remote-ok"
    assert stored["cleanup_status"] == "complete"
    assert stored["evidence_receipt"]["receipt_hash"]
    assert stored["evidence_receipt"]["resource_usage"]["wall_seconds"] >= 0
    assert result.execution is not None and not result.execution.sandbox.exists()


def test_nw_030_direct_session_heartbeats_lease_and_honors_remote_cancel(tmp_path):
    coordinator = approved_coordinator(tmp_path / "coordinator")
    job = manifest(
        job_id="job-remote-cancel",
        run_id="run-remote-cancel",
        command_argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        budgets={"timeout_seconds": 40, "memory_bytes": 128 * 1024**2, "max_output_bytes": 1024 * 1024},
    )
    coordinator.submit_job(job)
    ca, server_cert, server_key, client_cert, client_key = _write_tls_fixture(tmp_path / "tls-cancel")
    _bind_node_certificate(coordinator, "node-test", client_cert)

    async def exercise():
        server = CoordinatorSessionServer(
            coordinator,
            host="127.0.0.1",
            port=0,
            ssl_context=tls_server_context(certificate=server_cert, private_key=server_key, client_ca=ca),
            artifact_root=tmp_path / "received",
        )
        await server.start()
        client = WorkerSessionClient(
            host="127.0.0.1",
            port=server.bound_port,
            server_hostname="localhost",
            ssl_context=tls_client_context(certificate=client_cert, private_key=client_key, server_ca=ca),
            capability=capability(),
            worker_root=tmp_path / "worker",
        )
        task = asyncio.create_task(client.run_once())
        try:
            for _ in range(50):
                if coordinator.job(job.job_id)["status"] == "running":
                    break
                await asyncio.sleep(0.05)
            assert coordinator.job(job.job_id)["status"] == "running"
            coordinator.cancel_job(job.job_id)
            started = time.monotonic()
            result = await asyncio.wait_for(task, timeout=3)
            return result, time.monotonic() - started
        finally:
            await server.close()

    result, elapsed = asyncio.run(exercise())
    assert result.status == "cancelled"
    assert elapsed < 2.0
    stored = coordinator.job(job.job_id)
    assert stored["status"] == "cancelled"
    assert stored["terminal_event_id"]


def test_performance_idle_worker_reuses_one_tls_session_and_assigns_within_two_seconds(tmp_path):
    coordinator = approved_coordinator(tmp_path / "coordinator")
    ca, server_cert, server_key, client_cert, client_key = _write_tls_fixture(tmp_path / "tls")
    _bind_node_certificate(coordinator, "node-test", client_cert)

    class CountingServer(CoordinatorSessionServer):
        connection_count = 0

        async def _handle(self, reader, writer):
            self.connection_count += 1
            await super()._handle(reader, writer)

    async def exercise():
        server = CountingServer(
            coordinator,
            host="127.0.0.1",
            port=0,
            ssl_context=tls_server_context(certificate=server_cert, private_key=server_key, client_ca=ca),
            artifact_root=tmp_path / "received",
        )
        await server.start()
        client = WorkerSessionClient(
            host="127.0.0.1",
            port=server.bound_port,
            server_hostname="localhost",
            ssl_context=tls_client_context(certificate=client_cert, private_key=client_key, server_ca=ca),
            capability=capability(),
            worker_root=tmp_path / "worker",
        )
        task = asyncio.create_task(client.run_once(idle_poll_seconds=0.1, idle_session_seconds=3))
        await asyncio.sleep(0.35)
        job = manifest(job_id="job-idle-session", run_id="run-idle-session")
        submitted_at = time.monotonic()
        coordinator.submit_job(job)
        result = await asyncio.wait_for(task, timeout=2)
        elapsed = time.monotonic() - submitted_at
        await server.close()
        return result, elapsed, server.connection_count

    result, elapsed, connections = asyncio.run(exercise())
    assert result.status == "completed"
    assert elapsed < 2
    assert connections == 1


def test_fault_matrix_recovers_all_runtime_states_without_duplicate_terminal_events(tmp_path):
    states = ("preparing", "running", "waiting_model", "uploading", "verifying")
    faults = (
        "aaa_app_restart",
        "aaa_backend_restart",
        "coordinator_restart",
        "worker_restart",
        "relay_restart",
        "network_disconnect",
        "model_gateway_timeout",
        "artifact_upload_interruption",
        "user_cancel",
        "node_revoked",
    )
    for state in states:
        for fault in faults:
            clock = Clock()
            root = tmp_path / state / fault
            coordinator = approved_coordinator(root, clock=clock)
            job = manifest(
                job_id=f"job-{state}-{fault}",
                run_id=f"run-{state}-{fault}",
                retry_policy={"max_attempts": 2, "retry_safe": True},
            )
            coordinator.submit_job(job)
            lease = coordinator.lease_next("node-test")
            assert lease
            coordinator.acknowledge_lease(lease.lease_id, job.manifest_hash)
            coordinator.record_event(JobEvent(
                event_id=f"event-{state}", job_id=job.job_id, run_id=job.run_id, node_id="node-test",
                lease_id=lease.lease_id, attempt=1, sequence=1, state=state,
            ))
            if fault in {"aaa_app_restart", "aaa_backend_restart", "coordinator_restart"}:
                coordinator = WorkerCoordinator(WorkerControlStore(root / "coordinator"), clock=clock, lease_seconds=9)
                assert coordinator.job(job.job_id)["status"] == state
                coordinator.record_event(JobEvent(
                    event_id=f"terminal-{fault}", job_id=job.job_id, run_id=job.run_id, node_id="node-test",
                    lease_id=lease.lease_id, attempt=1, sequence=2, state="completed",
                ))
            elif fault in {"worker_restart", "relay_restart", "network_disconnect", "artifact_upload_interruption"}:
                clock.advance(10)
                assert coordinator.recover_expired_leases() == [job.job_id]
                coordinator.connect_node(capability(), transport="relay" if fault == "relay_restart" else "direct")
                retry = coordinator.lease_next("node-test")
                assert retry and retry.attempt == 2
                coordinator.acknowledge_lease(retry.lease_id, job.manifest_hash)
                coordinator.record_event(JobEvent(
                    event_id=f"terminal-{fault}", job_id=job.job_id, run_id=job.run_id, node_id="node-test",
                    lease_id=retry.lease_id, attempt=2, sequence=1, state="completed",
                ))
            elif fault == "model_gateway_timeout":
                coordinator.record_event(JobEvent(
                    event_id="terminal-model-timeout", job_id=job.job_id, run_id=job.run_id, node_id="node-test",
                    lease_id=lease.lease_id, attempt=1, sequence=2, state="failed", reason_category="provider_timeout",
                ))
                assert coordinator.job(job.job_id)["reason_category"] == "provider_timeout"
            elif fault == "user_cancel":
                coordinator.cancel_job(job.job_id)
                coordinator.record_event(JobEvent(
                    event_id="terminal-user-cancel", job_id=job.job_id, run_id=job.run_id, node_id="node-test",
                    lease_id=lease.lease_id, attempt=1, sequence=2, state="cancelled", reason_category="user_cancelled",
                ))
                assert coordinator.job(job.job_id)["reason_category"] == "user_cancelled"
            else:
                revoked = coordinator.revoke_node("node-test", reason="fault_injection")
                assert job.job_id in revoked["affected_job_ids"]
                assert coordinator.job(job.job_id)["status"] == "waiting_review"
            terminal = [event for event in coordinator.job(job.job_id)["events"] if event["state"] in {"completed", "failed", "cancelled", "lost", "expired"}]
            assert len(terminal) <= 1


def test_host_worker_control_command_uses_protocol_not_implementation_imports(tmp_path):
    coordinator = WorkerCoordinator(WorkerControlStore(tmp_path))
    imported = handle_worker_control_command(
        {
            "schema_version": "across-worker-control-command/1.0",
            "action": "node.import_approved",
            "payload": {
                "capability_manifest": capability().to_dict(),
                "display_name": "Remote",
                "fingerprint": "f" * 64,
                "session_generation": 1,
            },
        },
        coordinator,
    )
    assert imported["state"] == "offline"
    snapshot = handle_worker_control_command(
        {"schema_version": "across-worker-control-command/1.0", "action": "snapshot", "payload": {}},
        coordinator,
    )
    assert snapshot["nodes"][0]["node_id"] == "node-test"
