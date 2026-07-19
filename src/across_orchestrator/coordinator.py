from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping
import hmac
import base64
import os
import re
import secrets
import time
import urllib.parse

from .worker_protocol import (
    ArtifactDescriptor,
    CapabilityManifest,
    JobEvent,
    JobLease,
    JobManifest,
    ModelGrant,
    NODE_STATES,
    PROTOCOL_VERSION,
    ProtocolError,
    TERMINAL_JOB_STATES,
    build_evidence_receipt,
    new_protocol_id,
    payload_hash,
    sanitize_public,
)
from .worker_store import WorkerControlStore


class CoordinatorError(RuntimeError):
    pass


class EnrollmentAuthority:
    """Host-controlled pairing lifecycle; only public identity crosses the boundary."""

    def __init__(self, store: WorkerControlStore, *, clock=time.time, pairing_secret: bytes | None = None):
        self.store = store
        self.clock = clock
        self._secret = pairing_secret or secrets.token_bytes(32)

    def create_pairing_code(self, *, ttl_seconds: int = 600, max_failures: int = 5) -> dict[str, Any]:
        ttl = max(60, min(int(ttl_seconds), 600))
        enrollment_id = new_protocol_id("enrollment")
        raw_code = "-".join(f"{secrets.randbelow(10000):04d}" for _ in range(3))
        now = self.clock()
        record = {
            "schema_version": "across-worker-enrollment/1.0",
            "enrollment_id": enrollment_id,
            "code_hash": self._hash_code(enrollment_id, raw_code),
            "created_at": now,
            "expires_at": now + ttl,
            "used_at": None,
            "failed_attempts": 0,
            "max_failures": max(1, min(int(max_failures), 20)),
            "status": "issued",
        }
        self.store.put("enrollments", enrollment_id, record)
        self._audit("pairing.created", {"enrollment_id": enrollment_id, "expires_at": record["expires_at"]})
        return {
            "schema_version": record["schema_version"],
            "enrollment_id": enrollment_id,
            "pairing_code": raw_code,
            "expires_at": record["expires_at"],
            "contains_long_term_secret": False,
        }

    def submit_pairing(
        self,
        *,
        enrollment_id: str,
        pairing_code: str,
        public_identity: Mapping[str, Any],
        capability_summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self.store.lock(f"pairing-{enrollment_id}"):
            record = self.store.get("enrollments", enrollment_id)
            if record is None:
                raise CoordinatorError("pairing request rejected")
            now = self.clock()
            if record.get("status") != "issued" or record.get("used_at") is not None:
                self._audit("pairing.replay_rejected", {"enrollment_id": enrollment_id})
                raise CoordinatorError("pairing request rejected")
            if now >= float(record.get("expires_at") or 0):
                record["status"] = "expired"
                self.store.put("enrollments", enrollment_id, record)
                self._audit("pairing.expired_rejected", {"enrollment_id": enrollment_id})
                raise CoordinatorError("pairing request rejected")
            if int(record.get("failed_attempts") or 0) >= int(record.get("max_failures") or 5):
                record["status"] = "rate_limited"
                self.store.put("enrollments", enrollment_id, record)
                self._audit("pairing.rate_limited", {"enrollment_id": enrollment_id})
                raise CoordinatorError("pairing request rate limited")
            expected = str(record.get("code_hash") or "")
            supplied = self._hash_code(enrollment_id, pairing_code)
            if not hmac.compare_digest(expected, supplied):
                record["failed_attempts"] = int(record.get("failed_attempts") or 0) + 1
                self.store.put("enrollments", enrollment_id, record)
                self._audit("pairing.invalid_rejected", {"enrollment_id": enrollment_id, "failed_attempts": record["failed_attempts"]})
                raise CoordinatorError("pairing request rejected")
            node_id = str(public_identity.get("node_id") or "")
            CapabilityManifest.from_dict(capability_summary)
            if node_id != capability_summary.get("node_id"):
                raise CoordinatorError("pairing identity does not match capability manifest")
            fingerprint = str(public_identity.get("fingerprint") or "")
            if not fingerprint or len(fingerprint) < 16:
                raise CoordinatorError("pairing public identity is invalid")
            verification_code = self._verification_code(enrollment_id, node_id, fingerprint)
            pending = {
                "schema_version": "across-node-record/1.0",
                "node_id": node_id,
                "state": "pending_approval",
                "display_name": str(public_identity.get("display_name") or node_id),
                "public_identity": {"fingerprint": fingerprint, "algorithm": str(public_identity.get("algorithm") or "ed25519")},
                "capability_manifest": dict(capability_summary),
                "verification_code": verification_code,
                "approved_at": None,
                "revoked_at": None,
                "last_seen_at": None,
                "transport": "pending",
            }
            record["used_at"] = now
            record["status"] = "pending_approval"
            record["node_id"] = node_id
            self.store.put("enrollments", enrollment_id, record)
            self.store.put("nodes", node_id, pending)
        self._audit("pairing.submitted", {"enrollment_id": enrollment_id, "node_id": node_id})
        return {"node_id": node_id, "state": "pending_approval", "verification_code": verification_code}

    def approve(self, node_id: str, verification_code: str) -> dict[str, Any]:
        with self.store.lock(f"node-{node_id}"):
            node = self._node(node_id)
            if node.get("state") != "pending_approval":
                raise CoordinatorError("node is not pending approval")
            if not hmac.compare_digest(str(node.get("verification_code") or ""), str(verification_code or "")):
                self._audit("node.approval_rejected", {"node_id": node_id})
                raise CoordinatorError("verification code does not match")
            now = self.clock()
            node["state"] = "offline"
            node["approved_at"] = now
            node["identity_expires_at"] = now + 24 * 60 * 60
            node["session_generation"] = 1
            self.store.put("nodes", node_id, node)
        self._audit("node.approved", {"node_id": node_id})
        return sanitize_public(node)

    def reject(self, node_id: str) -> dict[str, Any]:
        node = self._node(node_id)
        if node.get("state") != "pending_approval":
            raise CoordinatorError("node is not pending approval")
        node["state"] = "revoked"
        node["revoked_at"] = self.clock()
        self.store.put("nodes", node_id, node)
        self._audit("node.rejected", {"node_id": node_id})
        return sanitize_public(node)

    def revoke(self, node_id: str, *, reason: str = "host_revoked") -> dict[str, Any]:
        node = self._node(node_id)
        node["state"] = "revoked"
        node["revoked_at"] = self.clock()
        node["revocation_reason"] = str(reason)[:120]
        node["session_generation"] = int(node.get("session_generation") or 0) + 1
        self.store.put("nodes", node_id, node)
        self._audit("node.revoked", {"node_id": node_id, "reason": reason})
        return sanitize_public(node)

    def delete(self, node_id: str) -> bool:
        node = self._node(node_id)
        if node.get("state") != "revoked":
            self.revoke(node_id, reason="deleted")
        deleted = self.store.delete("nodes", node_id)
        self._audit("node.deleted", {"node_id": node_id})
        return deleted

    def _node(self, node_id: str) -> dict[str, Any]:
        node = self.store.get("nodes", node_id)
        if node is None:
            raise CoordinatorError("node not found")
        return node

    def _hash_code(self, enrollment_id: str, code: str) -> str:
        return hmac.new(self._secret, f"{enrollment_id}:{code}".encode(), sha256).hexdigest()

    def _verification_code(self, enrollment_id: str, node_id: str, fingerprint: str) -> str:
        raw = hmac.new(self._secret, f"{enrollment_id}:{node_id}:{fingerprint}".encode(), sha256).hexdigest()
        return f"{int(raw[:8], 16) % 1_000_000:06d}"

    def _audit(self, event: str, payload: Mapping[str, Any]) -> None:
        self.store.append("audit", "worker-control", {"event": event, "created_at": self.clock(), "payload": sanitize_public(payload)})


class WorkerCoordinator:
    def __init__(self, store: WorkerControlStore | None = None, *, clock=time.time, lease_seconds: float = 30.0):
        self.store = store or WorkerControlStore()
        self.clock = clock
        self.lease_seconds = max(5.0, float(lease_seconds))
        self.enrollment = EnrollmentAuthority(self.store, clock=clock)

    def list_nodes(self) -> list[dict[str, Any]]:
        return [sanitize_public(item) for item in self.store.list("nodes")]

    def import_approved_node(
        self,
        manifest: CapabilityManifest,
        *,
        display_name: str | None = None,
        fingerprint: str,
        session_generation: int = 1,
        identity_expires_at: float | None = None,
    ) -> dict[str, Any]:
        if not fingerprint or len(fingerprint) < 16:
            raise CoordinatorError("approved node fingerprint is invalid")
        record = {
            "schema_version": "across-node-record/1.0",
            "node_id": manifest.node_id,
            "state": "offline",
            "display_name": str(display_name or manifest.node_id)[:120],
            "public_identity": {"fingerprint": fingerprint, "algorithm": "ed25519"},
            "certificate_fingerprint": fingerprint,
            "capability_manifest": manifest.to_dict(),
            "approved_at": self.clock(),
            "identity_expires_at": float(identity_expires_at or (self.clock() + 24 * 60 * 60)),
            "revoked_at": None,
            "last_seen_at": None,
            "transport": "pending",
            "session_generation": max(1, int(session_generation)),
        }
        self.store.put("nodes", manifest.node_id, record)
        self._audit("node.approval_imported", {"node_id": manifest.node_id})
        return sanitize_public(record)

    def connect_node(
        self,
        manifest: CapabilityManifest,
        *,
        transport: str,
        identity_generation: int = 1,
        peer_node_id: str | None = None,
        peer_certificate_fingerprint: str | None = None,
        require_peer_identity: bool = False,
    ) -> dict[str, Any]:
        if transport not in {"direct", "overlay", "relay", "local"}:
            raise CoordinatorError("unsupported transport")
        with self.store.lock(f"node-{manifest.node_id}"):
            node = self.store.get("nodes", manifest.node_id)
            if node is None or not node.get("approved_at"):
                raise CoordinatorError("node is not approved")
            if require_peer_identity:
                if not peer_node_id or peer_node_id != manifest.node_id:
                    raise CoordinatorError("TLS peer identity does not match claimed node")
                expected_fingerprint = str(
                    node.get("certificate_fingerprint")
                    or (node.get("public_identity") or {}).get("certificate_fingerprint")
                    or ""
                )
                if not expected_fingerprint:
                    raise CoordinatorError("approved node has no bound TLS certificate")
                if not peer_certificate_fingerprint or not hmac.compare_digest(
                    expected_fingerprint.lower(), str(peer_certificate_fingerprint).lower()
                ):
                    raise CoordinatorError("TLS peer certificate does not match approved node")
            if node.get("state") == "revoked" or int(node.get("session_generation") or 0) != identity_generation:
                raise CoordinatorError("node identity is revoked or stale")
            if PROTOCOL_VERSION not in manifest.protocol_versions:
                node["state"] = "incompatible"
                self.store.put("nodes", manifest.node_id, node)
                raise CoordinatorError("worker protocol is incompatible")
            node["capability_manifest"] = manifest.to_dict()
            node["state"] = "draining" if node.get("draining") else "online_idle"
            node["last_seen_at"] = self.clock()
            node["transport"] = transport
            self.store.put("nodes", manifest.node_id, node)
        self._audit("node.connected", {"node_id": manifest.node_id, "transport": transport})
        return sanitize_public(node)

    def disconnect_node(self, node_id: str, *, reason: str = "session_closed") -> dict[str, Any]:
        """Mark a closed transport session offline without manufacturing a heartbeat."""
        with self.store.lock(f"node-{node_id}"):
            node = self._node(node_id)
            if node.get("state") not in {"revoked", "incompatible", "pending_approval"}:
                node["state"] = "offline"
            node["last_disconnected_at"] = self.clock()
            node["disconnect_reason"] = str(reason)[:120]
            self.store.put("nodes", node_id, node)
        self._audit("node.disconnected", {"node_id": node_id, "reason": reason})
        return sanitize_public(node)

    def expire_stale_nodes(self, *, stale_after_seconds: float = 45.0) -> list[str]:
        cutoff = self.clock() - max(5.0, float(stale_after_seconds))
        expired: list[str] = []
        for node in self.store.list("nodes"):
            if node.get("state") not in {"online_idle", "online_busy", "draining"}:
                continue
            if float(node.get("last_seen_at") or 0) > cutoff:
                continue
            node_id = str(node.get("node_id") or "")
            if not node_id:
                continue
            self.disconnect_node(node_id, reason="heartbeat_stale")
            expired.append(node_id)
        return expired

    def heartbeat_node(self, node_id: str, *, current_load: float = 0.0) -> dict[str, Any]:
        node = self._node(node_id)
        if node.get("state") == "revoked":
            raise CoordinatorError("node is revoked")
        manifest = CapabilityManifest.from_dict(node["capability_manifest"])
        updated = replace(manifest, current_load=max(0.0, min(float(current_load), 1.0)), last_verified_at=self.clock())
        node["capability_manifest"] = updated.to_dict()
        node["last_seen_at"] = self.clock()
        if node.get("state") not in {"draining", "incompatible"}:
            node["state"] = "online_busy" if current_load > 0 else "online_idle"
        self.store.put("nodes", node_id, node)
        return sanitize_public(node)

    def set_draining(self, node_id: str, draining: bool = True) -> dict[str, Any]:
        node = self._node(node_id)
        node["draining"] = bool(draining)
        node["state"] = "draining" if draining else "online_idle"
        self.store.put("nodes", node_id, node)
        self._audit("node.draining_changed", {"node_id": node_id, "draining": draining})
        return sanitize_public(node)

    def request_node_update(self, node_id: str, *, version: str, url: str, sha256_value: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", str(version or "")):
            raise CoordinatorError("Worker update version is invalid")
        parsed = urllib.parse.urlparse(str(url or ""))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise CoordinatorError("Worker update URL must be credential-free HTTPS")
        if not re.fullmatch(r"[0-9a-f]{64}", str(sha256_value or "")):
            raise CoordinatorError("Worker update checksum is invalid")
        node = self._node(node_id)
        if node.get("state") in {"revoked", "pending_approval"}:
            raise CoordinatorError("Worker update requires an approved node")
        directive = {
            "directive_id": new_protocol_id("update"),
            "version": str(version),
            "url": str(url),
            "sha256": str(sha256_value),
            "status": "requested",
            "requested_at": self.clock(),
        }
        node["draining"] = True
        node["state"] = "draining"
        node["update_directive"] = directive
        self.store.put("nodes", node_id, node)
        self._audit("node.update_requested", {"node_id": node_id, "directive_id": directive["directive_id"], "version": version})
        return sanitize_public(node)

    def node_update_directive(self, node_id: str) -> dict[str, Any] | None:
        node = self._node(node_id)
        directive = node.get("update_directive")
        if not isinstance(directive, Mapping) or directive.get("status") not in {"requested", "downloading"}:
            return None
        return sanitize_public(dict(directive))

    def record_node_update(self, node_id: str, *, directive_id: str, status: str, error: str | None = None) -> dict[str, Any]:
        if status not in {"downloading", "completed", "failed"}:
            raise CoordinatorError("Worker update status is invalid")
        node = self._node(node_id)
        directive = node.get("update_directive")
        if not isinstance(directive, Mapping) or str(directive.get("directive_id") or "") != directive_id:
            raise CoordinatorError("Worker update directive is stale")
        updated = dict(directive)
        updated["status"] = status
        updated["updated_at"] = self.clock()
        if error:
            updated["error"] = str(error)[:120]
        node["update_directive"] = updated
        if status in {"completed", "failed"}:
            node["draining"] = False
            node["state"] = "offline"
        self.store.put("nodes", node_id, node)
        self._audit("node.update_status", {"node_id": node_id, "directive_id": directive_id, "status": status, "error": error})
        return sanitize_public(node)

    def request_node_transport(
        self,
        node_id: str,
        *,
        transport: str,
        endpoint: str,
        server_name: str | None = None,
        relay_session_id: str | None = None,
        relay_peer_node_id: str | None = None,
        relay_session_key: str | None = None,
    ) -> dict[str, Any]:
        if transport not in {"direct", "overlay", "relay"}:
            raise CoordinatorError("Worker transport is invalid")
        parsed = urllib.parse.urlparse(str(endpoint or ""))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise CoordinatorError("Worker transport endpoint must be credential-free HTTPS")
        node = self._node(node_id)
        if node.get("state") in {"revoked", "pending_approval"}:
            raise CoordinatorError("Worker transport change requires an approved node")
        directive: dict[str, Any] = {
            "directive_id": new_protocol_id("transport"),
            "transport": transport,
            "endpoint": str(endpoint),
            "server_name": str(server_name or parsed.hostname),
            "status": "requested",
            "requested_at": self.clock(),
        }
        if transport == "relay":
            session_id = str(relay_session_id or "")
            peer_node_id = str(relay_peer_node_id or "")
            session_key = str(relay_session_key or "")
            try:
                decoded_key = base64.urlsafe_b64decode(session_key.encode("ascii"))
            except (ValueError, UnicodeEncodeError) as exc:
                raise CoordinatorError("Worker Relay session key is invalid") from exc
            if not session_id or not peer_node_id or len(decoded_key) != 32:
                raise CoordinatorError("Worker Relay session material is invalid")
            directive.update(
                {
                    "relay_session_id": session_id,
                    "relay_peer_node_id": peer_node_id,
                    "relay_session_key": session_key,
                }
            )
        node["transport_directive"] = directive
        self.store.put("nodes", node_id, node)
        self._audit(
            "node.transport_requested",
            {"node_id": node_id, "directive_id": directive["directive_id"], "transport": transport},
        )
        return sanitize_public(node)

    def node_transport_directive(self, node_id: str) -> dict[str, Any] | None:
        node = self._node(node_id)
        directive = node.get("transport_directive")
        if not isinstance(directive, Mapping) or directive.get("status") not in {"requested", "applying"}:
            return None
        # This method is consumed only by the authenticated Worker session. The
        # Relay session key must never be returned by public snapshots.
        return dict(directive)

    def record_node_transport(
        self,
        node_id: str,
        *,
        directive_id: str,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"applying", "completed", "failed"}:
            raise CoordinatorError("Worker transport status is invalid")
        node = self._node(node_id)
        directive = node.get("transport_directive")
        if not isinstance(directive, Mapping) or str(directive.get("directive_id") or "") != directive_id:
            raise CoordinatorError("Worker transport directive is stale")
        target_transport = str(directive.get("transport") or "")
        if status == "applying":
            updated = dict(directive)
            updated["status"] = status
            updated["updated_at"] = self.clock()
        else:
            updated = {
                "directive_id": directive_id,
                "transport": target_transport,
                "status": status,
                "updated_at": self.clock(),
            }
            if error:
                updated["error"] = str(error)[:120]
            if status == "completed":
                node["transport"] = target_transport
        node["transport_directive"] = updated
        self.store.put("nodes", node_id, node)
        self._audit(
            "node.transport_status",
            {"node_id": node_id, "directive_id": directive_id, "transport": target_transport, "status": status, "error": error},
        )
        return sanitize_public(node)

    def revoke_node(self, node_id: str, *, reason: str = "host_revoked") -> dict[str, Any]:
        with self.store.lock(f"node-{node_id}"):
            node = self._node(node_id)
            node.update(
                {
                    "state": "revoked",
                    "draining": True,
                    "revoked_at": self.clock(),
                    "revocation_reason": str(reason)[:120],
                    "session_generation": int(node.get("session_generation") or 0) + 1,
                }
            )
            self.store.put("nodes", node_id, node)
            affected: list[str] = []
            for job in self.store.list("jobs"):
                if job.get("node_id") != node_id or job.get("terminal_event_id"):
                    continue
                job.update(
                    {
                        "status": "waiting_review",
                        "recovery_reason": "node_revoked",
                        "node_id": None,
                        "lease_id": None,
                        "updated_at": self.clock(),
                    }
                )
                self.store.put("jobs", str(job["job_id"]), job)
                affected.append(str(job["job_id"]))
            for grant in self.store.list("grants"):
                if grant.get("node_id") == node_id and not grant.get("revoked_at"):
                    grant["revoked_at"] = self.clock()
                    grant["active_calls"] = {}
                    self.store.put("grants", str(grant["grant_id"]), grant)
        self._audit("node.revoked", {"node_id": node_id, "reason": reason, "affected_job_ids": affected})
        return sanitize_public({**node, "affected_job_ids": affected})

    def delete_node(self, node_id: str) -> dict[str, Any]:
        node = self._node(node_id)
        if node.get("state") != "revoked":
            self.revoke_node(node_id, reason="deleted")
        deleted = self.store.delete("nodes", node_id)
        self._audit("node.deleted", {"node_id": node_id})
        return {"node_id": node_id, "deleted": deleted}

    def submit_job(self, manifest: JobManifest, *, input_payloads: Mapping[str, bytes] | None = None) -> dict[str, Any]:
        existing = self.store.get("idempotency", manifest.idempotency_key)
        if existing:
            job = self.store.get("jobs", str(existing["job_id"]))
            if job and job.get("manifest_hash") != manifest.manifest_hash:
                raise CoordinatorError("idempotency key was reused with a different manifest")
            if job:
                return sanitize_public({key: value for key, value in job.items() if key != "inputs_base64"})
        encoded_inputs = self._validate_inputs(manifest, input_payloads or {})
        record = {
            "schema_version": "across-coordinator-job/1.0",
            "job_id": manifest.job_id,
            "run_id": manifest.run_id,
            "manifest": manifest.to_dict(),
            "manifest_hash": manifest.manifest_hash,
            "status": "queued",
            "attempt": 0,
            "node_id": None,
            "lease_id": None,
            "terminal_event_id": None,
            "created_at": self.clock(),
            "updated_at": self.clock(),
            "inputs_base64": encoded_inputs,
        }
        self.store.put("jobs", manifest.job_id, record)
        self.store.put("idempotency", manifest.idempotency_key, {"idempotency_key": manifest.idempotency_key, "job_id": manifest.job_id, "manifest_hash": manifest.manifest_hash})
        self._audit("job.created", {"job_id": manifest.job_id, "run_id": manifest.run_id})
        return sanitize_public({key: value for key, value in record.items() if key != "inputs_base64"})

    def choose_node(self, manifest: JobManifest) -> dict[str, Any] | None:
        candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        for node in self.store.list("nodes"):
            if node.get("state") not in {"online_idle", "online_busy"} or node.get("draining"):
                continue
            try:
                capability = CapabilityManifest.from_dict(node["capability_manifest"])
            except (KeyError, ProtocolError):
                continue
            if not capability.supports({**manifest.required_capabilities, "executor": manifest.executor}):
                continue
            preferred = len(set(manifest.preferred_labels) & set(capability.labels))
            score = (
                -preferred,
                capability.current_load,
                -capability.disk_available_bytes,
                capability.node_id,
            )
            candidates.append((score, node))
        if not candidates:
            return None
        return sanitize_public(sorted(candidates, key=lambda item: item[0])[0][1])

    def lease_next(self, node_id: str) -> JobLease | None:
        with self.store.lock("worker-scheduler"):
            return self._lease_next_locked(node_id)

    def _lease_next_locked(self, node_id: str) -> JobLease | None:
        node = self._node(node_id)
        if node.get("state") not in {"online_idle", "online_busy"} or node.get("draining"):
            return None
        manifest_capability = CapabilityManifest.from_dict(node["capability_manifest"])
        for job in sorted(self.store.list("jobs"), key=lambda item: (float(item.get("created_at") or 0), str(item.get("job_id") or ""))):
            if job.get("status") != "queued":
                continue
            manifest = JobManifest.from_dict(job["manifest"])
            if not manifest_capability.supports({**manifest.required_capabilities, "executor": manifest.executor}):
                continue
            selected = self.choose_node(manifest)
            if not selected or selected.get("node_id") != node_id:
                continue
            attempt = int(job.get("attempt") or 0) + 1
            now = self.clock()
            lease = JobLease(
                lease_id=new_protocol_id("lease"),
                job_id=manifest.job_id,
                run_id=manifest.run_id,
                node_id=node_id,
                attempt=attempt,
                manifest_hash=manifest.manifest_hash,
                issued_at=now,
                expires_at=now + self.lease_seconds,
                heartbeat_interval_seconds=min(10.0, self.lease_seconds / 3),
            )
            scheduling_decision = {
                "selected_node_id": node_id,
                "reason": "safe capability match ranked by preferred labels, current load, available disk, then stable Node ID",
                "required_capabilities": dict(manifest.required_capabilities),
                "preferred_labels": list(manifest.preferred_labels),
                "decided_at": now,
            }
            job.update({
                "status": "leased",
                "attempt": attempt,
                "node_id": node_id,
                "lease_id": lease.lease_id,
                "updated_at": now,
                "scheduling_decision": scheduling_decision,
            })
            self.store.put("leases", lease.lease_id, lease.to_dict())
            self.store.put("jobs", manifest.job_id, job)
            self._audit("job.leased", {"job_id": manifest.job_id, "node_id": node_id, "lease_id": lease.lease_id, "attempt": attempt, "scheduling_decision": scheduling_decision})
            return lease
        return None

    def acknowledge_lease(self, lease_id: str, manifest_hash: str) -> JobLease:
        record = self._lease(lease_id)
        if self.clock() >= float(record["expires_at"]):
            self._requeue_expired(record)
            raise CoordinatorError("lease expired before acknowledgement")
        if not hmac.compare_digest(str(record["manifest_hash"]), str(manifest_hash)):
            self._requeue_expired(record, reason="manifest_hash_mismatch")
            raise CoordinatorError("manifest hash mismatch")
        record["acknowledged_at"] = self.clock()
        self.store.put("leases", lease_id, record)
        return JobLease(**record)

    def heartbeat_lease(self, lease_id: str, *, node_id: str, attempt: int) -> JobLease:
        record = self._lease(lease_id)
        if record["node_id"] != node_id or int(record["attempt"]) != int(attempt):
            raise CoordinatorError("stale lease heartbeat")
        if record.get("acknowledged_at") is None:
            raise CoordinatorError("lease must be acknowledged before heartbeat")
        now = self.clock()
        if now >= float(record["expires_at"]):
            self._requeue_expired(record)
            raise CoordinatorError("lease expired")
        record["expires_at"] = now + self.lease_seconds
        self.store.put("leases", lease_id, record)
        return JobLease(**record)

    def lease_control(self, lease_id: str, *, node_id: str, attempt: int) -> dict[str, Any]:
        lease = self.heartbeat_lease(lease_id, node_id=node_id, attempt=attempt)
        job = self._job(lease.job_id)
        return {
            "lease": lease.to_dict(),
            "cancel_requested": bool(job.get("cancel_requested_at")),
            "cancel_reason": str(job.get("cancel_reason") or "") or None,
        }

    def record_event(self, event: JobEvent) -> dict[str, Any]:
        job = self._job(event.job_id)
        if job.get("lease_id") != event.lease_id or job.get("node_id") != event.node_id or int(job.get("attempt") or 0) != event.attempt:
            raise CoordinatorError("event belongs to an old or different lease")
        events = self.store.read_log("events", event.job_id)
        if any(item.get("event_id") == event.event_id for item in events):
            return sanitize_public(next(item for item in events if item.get("event_id") == event.event_id))
        current_sequence = max((int(item.get("sequence") or 0) for item in events if int(item.get("attempt") or 0) == event.attempt), default=0)
        if event.sequence <= current_sequence:
            raise CoordinatorError("event sequence must increase monotonically")
        if job.get("terminal_event_id"):
            raise CoordinatorError("job already has a valid terminal event")
        serialized = event.to_dict()
        self.store.append("events", event.job_id, serialized)
        job["status"] = event.state
        job["updated_at"] = self.clock()
        if event.reason_category:
            job["reason_category"] = event.reason_category
        if event.state in TERMINAL_JOB_STATES:
            job["terminal_event_id"] = event.event_id
            if isinstance(event.payload.get("resource_usage"), Mapping):
                job["resource_usage"] = dict(event.payload["resource_usage"])
            job["cleanup_status"] = str(event.payload.get("cleanup_status") or "unknown")
            worker_receipt = event.payload.get("evidence_receipt") if isinstance(event.payload.get("evidence_receipt"), Mapping) else {}
            artifacts: list[ArtifactDescriptor] = []
            for raw in event.payload.get("artifacts") or ():
                if not isinstance(raw, Mapping):
                    continue
                values = dict(raw)
                values["chunks"] = tuple(values.get("chunks") or ())
                artifacts.append(ArtifactDescriptor(**values))
            model_usage = {"calls": 0, "tokens": 0, "cost_usd": 0.0}
            terminal_grant_ids: list[str] = []
            for grant in self.store.list("grants"):
                if grant.get("job_id") != event.job_id:
                    continue
                grant_id = str(grant.get("grant_id") or "")
                if grant_id:
                    terminal_grant_ids.append(grant_id)
                usage = grant.get("usage") if isinstance(grant.get("usage"), Mapping) else {}
                model_usage["calls"] += int(usage.get("calls") or 0)
                model_usage["tokens"] += int(usage.get("tokens") or 0)
                model_usage["cost_usd"] += float(usage.get("cost_usd") or 0)
            manifest = JobManifest.from_dict(job["manifest"])
            lease = JobLease(**self._lease(event.lease_id))
            node = CapabilityManifest.from_dict(self._node(event.node_id)["capability_manifest"])
            job["model_usage"] = model_usage
            job["evidence_receipt"] = build_evidence_receipt(
                manifest=manifest,
                node=node,
                lease=lease,
                terminal_state=event.state,
                artifacts=tuple(artifacts),
                quality_gates=dict(worker_receipt.get("quality_gates") or {}),
                model_usage=model_usage,
                cleanup_status=job["cleanup_status"],
                started_at=float(worker_receipt.get("started_at") or event.created_at),
                ended_at=float(worker_receipt.get("ended_at") or event.created_at),
                resource_usage=dict(job.get("resource_usage") or {}),
            )
            for grant_id in terminal_grant_ids:
                self.revoke_model_grant(grant_id)
        self.store.put("jobs", event.job_id, job)
        self._audit("job.event", {"job_id": event.job_id, "event_id": event.event_id, "state": event.state, "attempt": event.attempt})
        return sanitize_public(serialized)

    def cancel_job(self, job_id: str, *, reason: str = "user_cancelled") -> dict[str, Any]:
        job = self._job(job_id)
        if job.get("terminal_event_id"):
            return sanitize_public(job)
        job["cancel_requested_at"] = self.clock()
        job["cancel_reason"] = str(reason)[:120]
        if job.get("status") == "queued":
            job["status"] = "cancelled"
            job["terminal_event_id"] = new_protocol_id("cancel")
        self.store.put("jobs", job_id, job)
        self._audit("job.cancel_requested", {"job_id": job_id, "reason": reason})
        return sanitize_public(job)

    def recover_expired_leases(self) -> list[str]:
        recovered: list[str] = []
        now = self.clock()
        for record in self.store.list("leases"):
            if now < float(record.get("expires_at") or 0):
                continue
            job = self.store.get("jobs", str(record.get("job_id") or ""))
            if not job or job.get("terminal_event_id") or job.get("lease_id") != record.get("lease_id"):
                continue
            manifest = JobManifest.from_dict(job["manifest"])
            retry_safe = bool(manifest.retry_policy.get("retry_safe", True)) and not bool(manifest.retry_policy.get("external_side_effects"))
            attempts_left = int(job.get("attempt") or 0) < int(manifest.retry_policy.get("max_attempts") or 1)
            job["status"] = "queued" if retry_safe and attempts_left else "waiting_review"
            job["node_id"] = None
            job["lease_id"] = None
            job["updated_at"] = now
            self.store.put("jobs", manifest.job_id, job)
            recovered.append(manifest.job_id)
            self._audit("job.lease_recovered", {"job_id": manifest.job_id, "result": job["status"]})
        return recovered

    def issue_model_grant(
        self,
        *,
        job_id: str,
        node_id: str,
        audience: str = "aaa-model-gateway",
        scopes: Iterable[str] = ("model.invoke",),
        purposes: Iterable[str] = ("workflow",),
        model_policy: str = "host-default",
        max_calls: int = 1,
        max_tokens: int = 1024,
        max_concurrency: int = 1,
        max_cost_usd: float = 0.0,
        ttl_seconds: int = 300,
    ) -> ModelGrant:
        job = self._job(job_id)
        if job.get("node_id") != node_id:
            raise CoordinatorError("model grant node does not own the job lease")
        now = self.clock()
        grant = ModelGrant(
            grant_id=new_protocol_id("grant"),
            run_id=str(job["run_id"]),
            job_id=job_id,
            node_id=node_id,
            audience=audience,
            scopes=tuple(scopes),
            model_policy=model_policy,
            purposes=tuple(purposes),
            max_calls=max_calls,
            max_tokens=max_tokens,
            max_concurrency=max_concurrency,
            max_cost_usd=max_cost_usd,
            issued_at=now,
            expires_at=now + max(30, min(int(ttl_seconds), 3600)),
        )
        self.store.put("grants", grant.grant_id, {**grant.to_dict(), "usage": {"calls": 0, "tokens": 0, "cost_usd": 0.0}})
        self._audit("model_grant.issued", {"grant_id": grant.grant_id, "job_id": job_id, "node_id": node_id})
        return grant

    def consume_model_grant(self, grant_id: str, *, run_id: str, job_id: str, node_id: str, audience: str, scope: str, purpose: str, tokens: int, cost_usd: float = 0.0) -> dict[str, Any]:
        with self.store.lock(f"grant-{grant_id}"):
            record = self.store.get("grants", grant_id)
            if record is None:
                raise CoordinatorError("model grant not found")
            grant_fields = {key: value for key, value in record.items() if key not in {"usage"}}
            grant_fields["scopes"] = tuple(grant_fields.get("scopes") or ())
            grant_fields["purposes"] = tuple(grant_fields.get("purposes") or ())
            grant = ModelGrant(**grant_fields)
            grant.authorize(run_id=run_id, job_id=job_id, node_id=node_id, audience=audience, scope=scope, purpose=purpose, now=self.clock())
            usage = dict(record.get("usage") or {})
            next_calls = int(usage.get("calls") or 0) + 1
            next_tokens = int(usage.get("tokens") or 0) + max(0, int(tokens))
            next_cost = float(usage.get("cost_usd") or 0.0) + max(0.0, float(cost_usd))
            if next_calls > grant.max_calls or next_tokens > grant.max_tokens or next_cost > grant.max_cost_usd + 1e-9:
                self._audit("model_grant.budget_denied", {"grant_id": grant_id, "job_id": job_id})
                raise CoordinatorError("model grant budget exceeded")
            record["usage"] = {"calls": next_calls, "tokens": next_tokens, "cost_usd": next_cost}
            self.store.put("grants", grant_id, record)
        return sanitize_public(record["usage"])

    def begin_model_grant_call(
        self,
        grant_id: str,
        *,
        run_id: str,
        job_id: str,
        node_id: str,
        audience: str,
        scope: str,
        purpose: str,
        requested_tokens: int,
    ) -> dict[str, Any]:
        with self.store.lock(f"grant-{grant_id}"):
            record = self.store.get("grants", grant_id)
            if record is None:
                raise CoordinatorError("model grant not found")
            grant = _grant_from_record(record)
            grant.authorize(
                run_id=run_id,
                job_id=job_id,
                node_id=node_id,
                audience=audience,
                scope=scope,
                purpose=purpose,
                now=self.clock(),
            )
            usage = dict(record.get("usage") or {})
            reservations = dict(record.get("reservations") or {})
            requested = max(1, int(requested_tokens))
            reserved_tokens = sum(int(item.get("requested_tokens") or 0) for item in reservations.values())
            if len(reservations) >= grant.max_concurrency:
                raise CoordinatorError("model grant concurrency budget exceeded")
            if int(usage.get("calls") or 0) + len(reservations) + 1 > grant.max_calls:
                raise CoordinatorError("model grant call budget exceeded")
            if int(usage.get("tokens") or 0) + reserved_tokens + requested > grant.max_tokens:
                raise CoordinatorError("model grant token budget exceeded")
            call_id = new_protocol_id("model-call")
            reservations[call_id] = {"requested_tokens": requested, "started_at": self.clock()}
            record["reservations"] = reservations
            self.store.put("grants", grant_id, record)
        self._audit("model_grant.call_started", {"grant_id": grant_id, "call_id": call_id, "job_id": job_id})
        return {"call_id": call_id, "grant_id": grant_id, "requested_tokens": requested, "active_calls": len(reservations)}

    def finish_model_grant_call(
        self,
        grant_id: str,
        call_id: str,
        *,
        tokens: int,
        cost_usd: float = 0.0,
        outcome: str = "completed",
    ) -> dict[str, Any]:
        with self.store.lock(f"grant-{grant_id}"):
            record = self.store.get("grants", grant_id)
            if record is None:
                raise CoordinatorError("model grant not found")
            grant = _grant_from_record(record)
            reservations = dict(record.get("reservations") or {})
            reservation = reservations.pop(call_id, None)
            if not isinstance(reservation, Mapping):
                raise CoordinatorError("model grant call reservation not found")
            usage = dict(record.get("usage") or {})
            next_calls = int(usage.get("calls") or 0) + 1
            next_tokens = int(usage.get("tokens") or 0) + max(0, int(tokens))
            next_cost = float(usage.get("cost_usd") or 0.0) + max(0.0, float(cost_usd))
            if next_calls > grant.max_calls or next_tokens > grant.max_tokens or next_cost > grant.max_cost_usd + 1e-9:
                record["reservations"] = reservations
                self.store.put("grants", grant_id, record)
                raise CoordinatorError("model grant final usage exceeded budget")
            record["usage"] = {"calls": next_calls, "tokens": next_tokens, "cost_usd": next_cost}
            record["reservations"] = reservations
            self.store.put("grants", grant_id, record)
        self._audit("model_grant.call_finished", {"grant_id": grant_id, "call_id": call_id, "outcome": str(outcome)[:40]})
        return sanitize_public({**record["usage"], "active_calls": len(reservations), "outcome": outcome})

    def revoke_model_grant(self, grant_id: str) -> dict[str, Any]:
        record = self.store.get("grants", grant_id)
        if record is None:
            raise CoordinatorError("model grant not found")
        record["revoked_at"] = self.clock()
        self.store.put("grants", grant_id, record)
        self._audit("model_grant.revoked", {"grant_id": grant_id})
        return sanitize_public(record)

    def job(self, job_id: str) -> dict[str, Any]:
        result = self._job(job_id)
        result.pop("inputs_base64", None)
        result["events"] = self.store.read_log("events", job_id)
        return sanitize_public(result)

    def worker_job_payload(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        return {
            "manifest": dict(job["manifest"]),
            "inputs_base64": dict(job.get("inputs_base64") or {}),
        }

    def _validate_inputs(self, manifest: JobManifest, inputs: Mapping[str, bytes]) -> dict[str, str]:
        declared = {str(item.get("logical_name") or ""): str(item.get("sha256") or "") for item in manifest.input_artifacts}
        if set(inputs) != set(declared):
            if declared or inputs:
                raise CoordinatorError("job input payloads do not match the manifest")
            return {}
        encoded: dict[str, str] = {}
        for name, payload in inputs.items():
            if not isinstance(payload, bytes) or sha256(payload).hexdigest() != declared[name]:
                raise CoordinatorError("job input payload hash mismatch")
            encoded[name] = base64.b64encode(payload).decode("ascii")
        return encoded

    def _requeue_expired(self, lease: Mapping[str, Any], *, reason: str = "expired") -> None:
        job = self._job(str(lease["job_id"]))
        if job.get("terminal_event_id") or job.get("lease_id") != lease.get("lease_id"):
            return
        job.update({"status": "queued", "node_id": None, "lease_id": None, "updated_at": self.clock(), "last_lease_failure": reason})
        self.store.put("jobs", str(job["job_id"]), job)
        self._audit("job.requeued", {"job_id": job["job_id"], "reason": reason})

    def _node(self, node_id: str) -> dict[str, Any]:
        node = self.store.get("nodes", node_id)
        if node is None:
            raise CoordinatorError("node not found")
        if node.get("state") not in NODE_STATES:
            raise CoordinatorError("node record has invalid state")
        return node

    def _job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get("jobs", job_id)
        if job is None:
            raise CoordinatorError("job not found")
        return job

    def _lease(self, lease_id: str) -> dict[str, Any]:
        lease = self.store.get("leases", lease_id)
        if lease is None:
            raise CoordinatorError("lease not found")
        return lease

    def _audit(self, event: str, payload: Mapping[str, Any]) -> None:
        self.store.append("audit", "worker-control", {"event": event, "created_at": self.clock(), "payload": sanitize_public(payload), "payload_hash": payload_hash(sanitize_public(payload))})


def safe_cleanup_plan(worker_home: str | Path, run_id: str, job_id: str, attempt: int) -> list[Path]:
    root = Path(worker_home).expanduser().resolve()
    sandbox_root = (root / "sandboxes").resolve()
    candidate = (sandbox_root / run_id / job_id / str(attempt)).resolve()
    if sandbox_root not in candidate.parents or candidate in {root, sandbox_root, Path.home().resolve(), Path("/")}:
        raise CoordinatorError("cleanup target is outside the managed job sandbox")
    return [candidate]


def _grant_from_record(record: Mapping[str, Any]) -> ModelGrant:
    values = {key: value for key, value in record.items() if key not in {"usage", "reservations"}}
    values["scopes"] = tuple(values.get("scopes") or ())
    values["purposes"] = tuple(values.get("purposes") or ())
    return ModelGrant(**values)
