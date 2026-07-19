from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping
import json
import re
import time
import uuid


PROTOCOL_VERSION = "across-worker-session/1.0"
CAPABILITY_SCHEMA = "across-node-capability/1.0"
JOB_SCHEMA = "across-job-manifest/1.0"
LEASE_SCHEMA = "across-job-lease/1.0"
EVENT_SCHEMA = "across-job-event/1.0"
ARTIFACT_SCHEMA = "across-artifact-manifest/1.0"
MODEL_GRANT_SCHEMA = "across-model-grant/1.0"
EVIDENCE_SCHEMA = "across-worker-evidence/1.0"

NODE_STATES = frozenset(
    {
        "pending_approval",
        "online_idle",
        "online_busy",
        "draining",
        "offline",
        "incompatible",
        "degraded",
        "revoked",
    }
)
JOB_STATES = (
    "queued",
    "leased",
    "preparing",
    "running",
    "waiting_model",
    "uploading",
    "verifying",
    "waiting_review",
    "completed",
    "failed",
    "cancelled",
    "lost",
    "expired",
)
TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled", "lost", "expired"})
EXECUTORS = frozenset({"bounded-process", "oci-container", "macos-container-or-vm", "agent-adapter", "workflow-runtime"})
ISOLATION_LEVELS = frozenset({"isolated", "bounded", "unavailable"})

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET_KEY = re.compile(r"(?:api[_-]?key|authorization|credential|password|private[_-]?key|session[_-]?key|secret|token)$", re.I)
_ABSOLUTE_USER_PATH = re.compile(r"/(?:Users|home)/[^/\s]+")


class ProtocolError(ValueError):
    """A stable, public-safe worker protocol validation error."""


def new_protocol_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def payload_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require_identifier(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not _IDENTIFIER.fullmatch(text):
        raise ProtocolError(f"{field_name} must be a stable protocol identifier")
    return text


def require_schema(value: Mapping[str, Any], expected: str) -> None:
    if value.get("schema_version") != expected:
        raise ProtocolError(f"unsupported schema_version; expected {expected}")


def normalize_relative_path(value: Any, field_name: str = "path") -> str:
    raw = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProtocolError(f"{field_name} must stay inside the job sandbox")
    return str(path)


def sanitize_public(value: Any) -> Any:
    """Remove secret-shaped fields and user paths from public errors/evidence."""
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _SECRET_KEY.search(name) and not isinstance(item, bool):
                clean[name] = "[redacted]"
            else:
                clean[name] = sanitize_public(item)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [sanitize_public(item) for item in value]
    if isinstance(value, str):
        text = _ABSOLUTE_USER_PATH.sub("<user-home>", value)
        text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [redacted]", text)
        text = re.sub(r"(?i)(?:sk|gh[op])[-_][A-Za-z0-9_-]{16,}", "[redacted]", text)
        return text
    return value


@dataclass(frozen=True)
class CapabilityManifest:
    node_id: str
    worker_version: str
    os: str
    os_version: str
    architecture: str
    cpu_count: int
    memory_bytes: int
    disk_available_bytes: int
    executors: tuple[str, ...] = ("bounded-process",)
    isolation_level: str = "bounded"
    roles: tuple[str, ...] = ("worker",)
    protocol_versions: tuple[str, ...] = (PROTOCOL_VERSION,)
    workflow_runtimes: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    agents: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    max_concurrency: int = 1
    current_load: float = 0.0
    verification_status: str = "verified"
    capability_source: str = "local-probe"
    last_verified_at: float = field(default_factory=time.time)
    schema_version: str = CAPABILITY_SCHEMA

    def __post_init__(self) -> None:
        require_identifier(self.node_id, "node_id")
        if self.schema_version != CAPABILITY_SCHEMA:
            raise ProtocolError("unsupported capability manifest schema")
        if self.architecture not in {"x86_64", "arm64"}:
            raise ProtocolError("unsupported worker architecture")
        if self.os not in {"macos", "linux"}:
            raise ProtocolError("unsupported worker operating system")
        if not self.executors or any(item not in EXECUTORS for item in self.executors):
            raise ProtocolError("capability manifest contains an unsupported executor")
        if self.isolation_level not in ISOLATION_LEVELS:
            raise ProtocolError("unsupported isolation level")
        if self.max_concurrency < 1:
            raise ProtocolError("max_concurrency must be positive")
        if min(self.cpu_count, self.memory_bytes, self.disk_available_bytes) < 0:
            raise ProtocolError("resource values cannot be negative")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CapabilityManifest":
        require_schema(value, CAPABILITY_SCHEMA)
        return cls(
            node_id=str(value.get("node_id") or ""),
            worker_version=str(value.get("worker_version") or ""),
            os=str(value.get("os") or ""),
            os_version=str(value.get("os_version") or ""),
            architecture=str(value.get("architecture") or ""),
            cpu_count=int(value.get("cpu_count") or 0),
            memory_bytes=int(value.get("memory_bytes") or 0),
            disk_available_bytes=int(value.get("disk_available_bytes") or 0),
            executors=tuple(map(str, value.get("executors") or ())),
            isolation_level=str(value.get("isolation_level") or "unavailable"),
            roles=tuple(map(str, value.get("roles") or ("worker",))),
            protocol_versions=tuple(map(str, value.get("protocol_versions") or ())),
            workflow_runtimes=tuple(map(str, value.get("workflow_runtimes") or ())),
            tools=tuple(map(str, value.get("tools") or ())),
            agents=tuple(map(str, value.get("agents") or ())),
            labels=tuple(map(str, value.get("labels") or ())),
            max_concurrency=int(value.get("max_concurrency") or 1),
            current_load=float(value.get("current_load") or 0),
            verification_status=str(value.get("verification_status") or "unknown"),
            capability_source=str(value.get("capability_source") or "self-report"),
            last_verified_at=float(value.get("last_verified_at") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("executors", "roles", "protocol_versions", "workflow_runtimes", "tools", "agents", "labels"):
            data[key] = list(data[key])
        return data

    def supports(self, required: Mapping[str, Any]) -> bool:
        if self.verification_status != "verified" or self.capability_source != "local-probe" or PROTOCOL_VERSION not in self.protocol_versions:
            return False
        if required.get("os") and required["os"] != self.os:
            return False
        if required.get("architecture") and required["architecture"] != self.architecture:
            return False
        if required.get("executor") and required["executor"] not in self.executors:
            return False
        if required.get("isolation_level") == "isolated" and self.isolation_level != "isolated":
            return False
        checks: Iterable[tuple[str, tuple[str, ...]]] = (
            ("labels", self.labels),
            ("tools", self.tools),
            ("workflow_runtimes", self.workflow_runtimes),
            ("agents", self.agents),
        )
        for key, available in checks:
            if not set(map(str, required.get(key) or ())).issubset(set(available)):
                return False
        if int(required.get("memory_bytes") or 0) > self.memory_bytes:
            return False
        if int(required.get("disk_bytes") or 0) > self.disk_available_bytes:
            return False
        return True


@dataclass(frozen=True)
class JobManifest:
    job_id: str
    run_id: str
    project_id: str
    workflow_id: str
    idempotency_key: str
    command_argv: tuple[str, ...]
    required_capabilities: Mapping[str, Any]
    permissions: Mapping[str, Any]
    budgets: Mapping[str, Any]
    expected_outputs: tuple[str, ...]
    executor: str = "bounded-process"
    retry_policy: Mapping[str, Any] = field(default_factory=lambda: {"max_attempts": 1, "retry_safe": True})
    cancellation_policy: Mapping[str, Any] = field(default_factory=lambda: {"kill_process_tree": True})
    cleanup_policy: Mapping[str, Any] = field(default_factory=lambda: {"retention_seconds": 0})
    model_policy: Mapping[str, Any] = field(default_factory=lambda: {"enabled": False})
    input_artifacts: tuple[Mapping[str, Any], ...] = ()
    preferred_labels: tuple[str, ...] = ()
    quality_gates: tuple[str, ...] = ()
    evidence_requirements: tuple[str, ...] = ()
    created_by: str = "host"
    created_at: float = field(default_factory=time.time)
    schema_version: str = JOB_SCHEMA

    def __post_init__(self) -> None:
        for name in ("job_id", "run_id", "project_id", "workflow_id", "idempotency_key"):
            require_identifier(getattr(self, name), name)
        if self.schema_version != JOB_SCHEMA:
            raise ProtocolError("unsupported job manifest schema")
        if self.executor not in EXECUTORS:
            raise ProtocolError("unsupported job executor")
        if not self.command_argv or any(not isinstance(item, str) or not item for item in self.command_argv):
            raise ProtocolError("command_argv must be a non-empty structured argument list")
        if len(self.command_argv) > 128 or sum(len(item) for item in self.command_argv) > 32_768:
            raise ProtocolError("command_argv exceeds protocol limits")
        for output in self.expected_outputs:
            normalize_relative_path(output, "expected_outputs")
        if bool(self.retry_policy.get("external_side_effects")) and bool(self.retry_policy.get("retry_safe", True)):
            raise ProtocolError("jobs with external side effects cannot be marked retry_safe")
        if self.permissions.get("network", {}).get("mode", "none") not in {"none", "allowlist"}:
            raise ProtocolError("worker network permission must be none or allowlist")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JobManifest":
        require_schema(value, JOB_SCHEMA)
        command = value.get("command_argv")
        if not isinstance(command, list):
            raise ProtocolError("command_argv must be an array, never a shell string")
        return cls(
            job_id=str(value.get("job_id") or ""),
            run_id=str(value.get("run_id") or ""),
            project_id=str(value.get("project_id") or ""),
            workflow_id=str(value.get("workflow_id") or ""),
            idempotency_key=str(value.get("idempotency_key") or ""),
            command_argv=tuple(command),
            required_capabilities=dict(value.get("required_capabilities") or {}),
            permissions=dict(value.get("permissions") or {}),
            budgets=dict(value.get("budgets") or {}),
            expected_outputs=tuple(map(str, value.get("expected_outputs") or ())),
            executor=str(value.get("executor") or "bounded-process"),
            retry_policy=dict(value.get("retry_policy") or {}),
            cancellation_policy=dict(value.get("cancellation_policy") or {}),
            cleanup_policy=dict(value.get("cleanup_policy") or {}),
            model_policy=dict(value.get("model_policy") or {}),
            input_artifacts=tuple(dict(item) for item in value.get("input_artifacts") or ()),
            preferred_labels=tuple(map(str, value.get("preferred_labels") or ())),
            quality_gates=tuple(map(str, value.get("quality_gates") or ())),
            evidence_requirements=tuple(map(str, value.get("evidence_requirements") or ())),
            created_by=str(value.get("created_by") or "host"),
            created_at=float(value.get("created_at") or time.time()),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("command_argv", "expected_outputs", "input_artifacts", "preferred_labels", "quality_gates", "evidence_requirements"):
            data[key] = list(data[key])
        return data

    @property
    def manifest_hash(self) -> str:
        return payload_hash(self.to_dict())


@dataclass(frozen=True)
class JobLease:
    lease_id: str
    job_id: str
    run_id: str
    node_id: str
    attempt: int
    manifest_hash: str
    issued_at: float
    expires_at: float
    heartbeat_interval_seconds: float
    acknowledged_at: float | None = None
    schema_version: str = LEASE_SCHEMA

    def __post_init__(self) -> None:
        for name in ("lease_id", "job_id", "run_id", "node_id"):
            require_identifier(getattr(self, name), name)
        if self.attempt < 1 or self.expires_at <= self.issued_at:
            raise ProtocolError("lease attempt and expiry must be valid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.manifest_hash):
            raise ProtocolError("lease manifest_hash must be sha256")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JobEvent:
    event_id: str
    job_id: str
    run_id: str
    node_id: str
    lease_id: str
    attempt: int
    sequence: int
    state: str
    reason_category: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    schema_version: str = EVENT_SCHEMA

    def __post_init__(self) -> None:
        if self.state not in JOB_STATES:
            raise ProtocolError("unsupported job state")
        if self.attempt < 1 or self.sequence < 1:
            raise ProtocolError("event attempt and sequence must be positive")
        for name in ("event_id", "job_id", "run_id", "node_id", "lease_id"):
            require_identifier(getattr(self, name), name)

    def to_dict(self) -> dict[str, Any]:
        return sanitize_public(asdict(self))


@dataclass(frozen=True)
class ArtifactDescriptor:
    artifact_id: str
    run_id: str
    job_id: str
    node_id: str
    logical_name: str
    media_type: str
    size: int
    sha256: str
    sensitivity: str = "internal"
    retention: str = "run"
    producer: str = "worker"
    chunks: tuple[Mapping[str, Any], ...] = ()
    upload_status: str = "pending"
    verification_status: str = "pending"
    created_at: float = field(default_factory=time.time)
    schema_version: str = ARTIFACT_SCHEMA

    def __post_init__(self) -> None:
        for name in ("artifact_id", "run_id", "job_id", "node_id"):
            require_identifier(getattr(self, name), name)
        normalize_relative_path(self.logical_name, "logical_name")
        if self.size < 0 or not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ProtocolError("artifact size/hash is invalid")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["chunks"] = list(data["chunks"])
        return data


@dataclass(frozen=True)
class ModelGrant:
    grant_id: str
    run_id: str
    job_id: str
    node_id: str
    audience: str
    scopes: tuple[str, ...]
    model_policy: str
    purposes: tuple[str, ...]
    max_calls: int
    max_tokens: int
    max_concurrency: int
    max_cost_usd: float
    issued_at: float
    expires_at: float
    revoked_at: float | None = None
    schema_version: str = MODEL_GRANT_SCHEMA

    def __post_init__(self) -> None:
        for name in ("grant_id", "run_id", "job_id", "node_id", "audience"):
            require_identifier(getattr(self, name), name)
        if self.expires_at <= self.issued_at or min(self.max_calls, self.max_tokens, self.max_concurrency) < 0:
            raise ProtocolError("model grant limits or expiry are invalid")

    def authorize(self, *, run_id: str, job_id: str, node_id: str, audience: str, scope: str, purpose: str, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        if self.revoked_at is not None or timestamp >= self.expires_at:
            raise ProtocolError("model grant is expired or revoked")
        if (run_id, job_id, node_id, audience) != (self.run_id, self.job_id, self.node_id, self.audience):
            raise ProtocolError("model grant binding mismatch")
        if scope not in self.scopes or purpose not in self.purposes:
            raise ProtocolError("model grant scope or purpose denied")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["scopes"] = list(data["scopes"])
        data["purposes"] = list(data["purposes"])
        return data


def build_evidence_receipt(
    *,
    manifest: JobManifest,
    node: CapabilityManifest,
    lease: JobLease,
    terminal_state: str,
    artifacts: Iterable[ArtifactDescriptor],
    quality_gates: Mapping[str, Any],
    model_usage: Mapping[str, Any] | None,
    cleanup_status: str,
    started_at: float,
    ended_at: float,
    resource_usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if terminal_state not in TERMINAL_JOB_STATES | {"waiting_review"}:
        raise ProtocolError("evidence receipt requires a terminal or review state")
    receipt = {
        "schema_version": EVIDENCE_SCHEMA,
        "run_id": manifest.run_id,
        "job_id": manifest.job_id,
        "goal_hash": payload_hash({"workflow_id": manifest.workflow_id, "project_id": manifest.project_id}),
        "manifest_hash": manifest.manifest_hash,
        "permission_plan_hash": payload_hash(manifest.permissions),
        "node": {
            "node_id": node.node_id,
            "worker_version": node.worker_version,
            "platform": f"{node.os}/{node.architecture}",
            "executor": manifest.executor,
            "isolation_level": node.isolation_level,
        },
        "attempt": lease.attempt,
        "lease_id": lease.lease_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "terminal_state": terminal_state,
        "artifacts": [
            {"artifact_id": item.artifact_id, "logical_name": item.logical_name, "size": item.size, "sha256": item.sha256}
            for item in artifacts
        ],
        "model_usage": sanitize_public(dict(model_usage or {})),
        "quality_gates": sanitize_public(dict(quality_gates)),
        "cleanup_status": cleanup_status,
        "resource_usage": sanitize_public(dict(resource_usage or {})),
    }
    receipt["receipt_hash"] = payload_hash(receipt)
    return receipt
