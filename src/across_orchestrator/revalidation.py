from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .goal_graph import InvalidationPlan, compute_invalidation
from .worker_protocol import payload_hash

if TYPE_CHECKING:
    from .coordinator import WorkerCoordinator
    from .worker_protocol import JobManifest


REVALIDATION_REQUEST_SCHEMA = "across-goal-revalidation-request/1.1"
REVALIDATION_PLAN_SCHEMA = "across-goal-revalidation-plan/1.1"
REVALIDATION_ATTEMPT_SCHEMA = "across-goal-revalidation-attempt/1.1"
HOST_EVIDENCE_SCHEMA = "across-goal-host-validation-evidence/1.1"


def _non_empty_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _sha256(value: object, field_name: str) -> str:
    text = _non_empty_text(value, field_name).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{field_name} must be a SHA-256 digest")
    return text


def _request_authority(payload: Mapping[str, Any]) -> dict[str, object]:
    if payload.get("schema_version") != REVALIDATION_REQUEST_SCHEMA:
        raise ValueError(f"goal revalidation schema_version must be {REVALIDATION_REQUEST_SCHEMA}")
    graph = payload.get("graph")
    if not isinstance(graph, Mapping):
        raise ValueError("goal revalidation graph must be an object")
    goal_revision = int(payload.get("goal_revision") or 0)
    if goal_revision < 1:
        raise ValueError("goal_revision must be positive")
    return {
        "graph": graph,
        "goal_id": _non_empty_text(payload.get("goal_id"), "goal_id"),
        "goal_revision": goal_revision,
        "task_id": _non_empty_text(payload.get("task_id"), "task_id"),
        "input_fingerprint": _sha256(payload.get("input_fingerprint"), "input_fingerprint"),
    }


def _plan_fields(payload: Mapping[str, Any]) -> dict[str, object]:
    authority = _request_authority(payload)
    changed = {str(item) for item in payload.get("changed_fingerprints") or () if str(item)}
    plan = compute_invalidation(authority["graph"], changed)
    selected = sorted({str(item) for item in payload.get("criterion_ids") or () if str(item)})
    if not selected or not set(selected).issubset(plan.affected_criterion_ids):
        raise ValueError("revalidation criteria must be affected by the invalidation plan")
    evidence_by_criterion = dict(plan.stale_evidence_by_criterion)
    superseded = sorted({
        evidence_id
        for criterion_id in selected
        for evidence_id in evidence_by_criterion.get(criterion_id, plan.stale_evidence_ids)
    })
    return {
        "goal_id": authority["goal_id"],
        "goal_revision": authority["goal_revision"],
        "task_id": authority["task_id"],
        "input_fingerprint": authority["input_fingerprint"],
        "criterion_ids": selected,
        "changed_fingerprints": list(plan.changed_fingerprints),
        "affected_criterion_ids": list(plan.affected_criterion_ids),
        "supersedes_evidence_ids": superseded,
        "preserved_evidence_ids": list(plan.preserved_evidence_ids),
        "attempt_number": max(0, int(payload.get("prior_attempt_number") or 0)) + 1,
    }


def plan_revalidation(payload: Mapping[str, Any]) -> dict[str, object]:
    """Return a deterministic, side-effect-free revalidation plan."""

    unsigned = {"schema_version": REVALIDATION_PLAN_SCHEMA, **_plan_fields(payload)}
    return {**unsigned, "plan_hash": payload_hash(unsigned)}


def _invalidation_plan_from_fields(fields: Mapping[str, Any]) -> InvalidationPlan:
    return InvalidationPlan(
        changed_fingerprints=tuple(map(str, fields["changed_fingerprints"])),
        affected_criterion_ids=tuple(map(str, fields["affected_criterion_ids"])),
        stale_evidence_ids=tuple(map(str, fields["supersedes_evidence_ids"])),
        preserved_evidence_ids=tuple(map(str, fields["preserved_evidence_ids"])),
    )


def start_revalidation_attempt(
    coordinator: "WorkerCoordinator",
    payload: Mapping[str, Any],
) -> dict[str, object]:
    """Persist an explicit host-validation or Worker-reexecution attempt."""

    planned = plan_revalidation(payload)
    if _sha256(payload.get("plan_hash"), "plan_hash") != planned["plan_hash"]:
        raise ValueError("plan_hash does not match the deterministic revalidation plan")
    execution_mode = _non_empty_text(payload.get("execution_mode"), "execution_mode")
    if execution_mode not in {"host_validation", "worker_reexecution"}:
        raise ValueError("execution_mode must be host_validation or worker_reexecution")
    if execution_mode == "host_validation" and "job_manifests" in payload:
        raise ValueError("host_validation must not include job_manifests")
    idempotency_key = _non_empty_text(payload.get("idempotency_key"), "idempotency_key")
    idempotency_id = f"goal-revalidation-{payload_hash(idempotency_key)[:32]}"
    request_hash = payload_hash(dict(payload))

    with coordinator.store.lock(f"goal-revalidation-start-{idempotency_id}"):
        existing_idempotency = coordinator.store.get("idempotency", idempotency_id)
        if existing_idempotency:
            if existing_idempotency.get("request_hash") != request_hash:
                raise ValueError("revalidation idempotency key conflicts with existing work")
            existing_attempt = coordinator.store.get(
                "revalidation_attempts", str(existing_idempotency.get("attempt_id") or "")
            )
            if not existing_attempt:
                raise ValueError("revalidation idempotency record references missing work")
            return existing_attempt

        authority = coordinator.store.get("goal_revisions", str(planned["goal_id"]))
        if authority and int(authority.get("goal_revision") or 0) > int(planned["goal_revision"]):
            raise ValueError("revalidation Goal revision is older than host authority")

        if execution_mode == "worker_reexecution":
            from .worker_protocol import JobManifest

            raw_manifests = payload.get("job_manifests") or ()
            if not isinstance(raw_manifests, Sequence) or isinstance(raw_manifests, (str, bytes)):
                raise ValueError("job_manifests must be an array")
            manifests = [JobManifest.from_dict(item) for item in raw_manifests]
            attempt = create_revalidation_attempt(
                coordinator,
                _invalidation_plan_from_fields(planned),
                planned["criterion_ids"],
                manifests,
                prior_attempt_number=int(planned["attempt_number"]) - 1,
            )
            attempt = {
                **attempt,
                "schema_version": REVALIDATION_ATTEMPT_SCHEMA,
                "execution_mode": execution_mode,
                "input_fingerprint": planned["input_fingerprint"],
                "plan_hash": planned["plan_hash"],
            }
            coordinator.store.put("revalidation_attempts", str(attempt["attempt_id"]), attempt)
        else:
            attempt_id = f"revalidation-attempt-{payload_hash({'idempotency_key': idempotency_key, 'request_hash': request_hash})[:32]}"
            plan_id = f"invalidation-plan-{planned['plan_hash'][:32]}"
            now = coordinator.clock()
            plan_record = {
                **planned,
                "schema_version": REVALIDATION_PLAN_SCHEMA,
                "plan_id": plan_id,
                "created_at": now,
            }
            attempt = {
                "schema_version": REVALIDATION_ATTEMPT_SCHEMA,
                "attempt_id": attempt_id,
                "attempt_number": planned["attempt_number"],
                "plan_id": plan_id,
                "plan_hash": planned["plan_hash"],
                "goal_id": planned["goal_id"],
                "goal_revision": planned["goal_revision"],
                "task_id": planned["task_id"],
                "input_fingerprint": planned["input_fingerprint"],
                "criterion_ids": planned["criterion_ids"],
                "changed_fingerprints": planned["changed_fingerprints"],
                "supersedes_evidence_ids": planned["supersedes_evidence_ids"],
                "preserved_evidence_ids": planned["preserved_evidence_ids"],
                "execution_mode": execution_mode,
                "state": "awaiting_host_evidence",
                "job_ids": [],
                "created_at": now,
                "updated_at": now,
            }
            created_plan = coordinator.store.get("invalidation_plans", plan_id) is None
            try:
                coordinator.store.put("invalidation_plans", plan_id, plan_record)
                coordinator.store.put("revalidation_attempts", attempt_id, attempt)
            except Exception:
                coordinator.store.delete("revalidation_attempts", attempt_id)
                if created_plan:
                    coordinator.store.delete("invalidation_plans", plan_id)
                raise

        try:
            coordinator.store.put(
                "idempotency",
                idempotency_id,
                {
                    "kind": "goal_revalidation_start",
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "attempt_id": attempt["attempt_id"],
                },
            )
        except Exception:
            if execution_mode == "host_validation":
                coordinator.store.delete("revalidation_attempts", str(attempt["attempt_id"]))
            raise
        return attempt


def _validate_host_receipt(attempt: Mapping[str, Any], receipt: Mapping[str, Any]) -> str:
    if receipt.get("schema_version") != HOST_EVIDENCE_SCHEMA:
        raise ValueError(f"host evidence schema_version must be {HOST_EVIDENCE_SCHEMA}")
    receipt_hash = _sha256(receipt.get("receipt_hash"), "receipt_hash")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    if payload_hash(unsigned) != receipt_hash:
        raise ValueError("host evidence receipt hash is invalid")
    expected = {
        "attempt_id": attempt["attempt_id"],
        "goal_id": attempt["goal_id"],
        "goal_revision": attempt["goal_revision"],
        "task_id": attempt["task_id"],
        "criterion_ids": attempt["criterion_ids"],
        "input_fingerprint": attempt["input_fingerprint"],
    }
    for field_name, expected_value in expected.items():
        if receipt.get(field_name) != expected_value:
            raise ValueError(f"host evidence {field_name} does not match the revalidation attempt")
    if receipt.get("verdict") != "verified":
        raise ValueError("host evidence verdict must be verified")
    _non_empty_text(receipt.get("validator_id"), "validator_id")
    artifact_digests = receipt.get("artifact_digests")
    if not isinstance(artifact_digests, Mapping) or not artifact_digests:
        raise ValueError("host evidence artifact_digests must be a non-empty object")
    for logical_name, digest in artifact_digests.items():
        _non_empty_text(logical_name, "artifact logical name")
        _sha256(digest, "artifact digest")
    return receipt_hash


def complete_host_revalidation_attempt(
    coordinator: "WorkerCoordinator",
    payload: Mapping[str, Any],
) -> dict[str, object]:
    """Complete a host-validation attempt with a hash-valid bound receipt."""

    attempt_id = _non_empty_text(payload.get("attempt_id"), "attempt_id")
    receipt = payload.get("receipt")
    if not isinstance(receipt, Mapping):
        raise ValueError("receipt must be an object")
    with coordinator.store.lock(f"goal-revalidation-complete-{attempt_id}"):
        attempt = coordinator.store.get("revalidation_attempts", attempt_id)
        if not attempt:
            raise ValueError("revalidation attempt was not found")
        if attempt.get("execution_mode") != "host_validation":
            raise ValueError("only a host_validation attempt accepts host evidence")
        receipt_hash = _validate_host_receipt(attempt, receipt)
        if attempt.get("state") == "completed":
            if attempt.get("evidence_receipt_hash") != receipt_hash:
                raise ValueError("completed revalidation attempt has different evidence")
            return attempt
        if attempt.get("state") != "awaiting_host_evidence":
            raise ValueError("revalidation attempt is not awaiting host evidence")
        updated = {
            **attempt,
            "state": "completed",
            "evidence_receipt_hash": receipt_hash,
            "replacement_evidence_ids": [f"evidence-{receipt_hash[:24]}"],
            "completed_at": coordinator.clock(),
            "updated_at": coordinator.clock(),
        }
        coordinator.store.put("revalidation_attempts", attempt_id, updated)
        return updated


def build_revalidation_attempt(
    plan: InvalidationPlan,
    criterion_ids: Sequence[str],
    *,
    prior_attempt_number: int = 0,
) -> dict[str, object]:
    selected = sorted(set(map(str, criterion_ids)))
    if not selected or not set(selected).issubset(plan.affected_criterion_ids):
        raise ValueError("revalidation criteria must be affected by the invalidation plan")
    evidence_by_criterion = dict(plan.stale_evidence_by_criterion)
    superseded = sorted({
        evidence_id
        for criterion_id in selected
        for evidence_id in evidence_by_criterion.get(criterion_id, plan.stale_evidence_ids)
    })
    return {
        "schema_version": "across-goal-revalidation-attempt/1.0",
        "attempt_id": f"revalidation-attempt-{uuid.uuid4().hex}",
        "attempt_number": max(0, int(prior_attempt_number)) + 1,
        "criterion_ids": selected,
        "changed_fingerprints": list(plan.changed_fingerprints),
        "supersedes_evidence_ids": superseded,
        "preserved_evidence_ids": list(plan.preserved_evidence_ids),
        "state": "planned",
    }


def create_revalidation_attempt(
    coordinator: "WorkerCoordinator",
    plan: InvalidationPlan,
    criterion_ids: Sequence[str],
    manifests: Sequence["JobManifest"],
    *,
    prior_attempt_number: int = 0,
) -> dict[str, object]:
    """Persist invalidation authority and enqueue criterion-scoped replacement jobs."""

    attempt = build_revalidation_attempt(
        plan,
        criterion_ids,
        prior_attempt_number=prior_attempt_number,
    )
    selected = set(map(str, attempt["criterion_ids"]))
    by_criterion: dict[str, "JobManifest"] = {}
    for manifest in manifests:
        if len(manifest.criterion_ids) != 1:
            raise ValueError("revalidation Job manifest must bind exactly one criterion")
        criterion_id = manifest.criterion_ids[0]
        if criterion_id not in selected or criterion_id in by_criterion:
            raise ValueError("revalidation Job manifests must match selected criteria exactly")
        if not manifest.goal_id or not manifest.task_id:
            raise ValueError("revalidation Job manifest must include the complete Goal binding")
        by_criterion[criterion_id] = manifest
    if set(by_criterion) != selected:
        raise ValueError("one revalidation Job manifest is required per selected criterion")
    bindings = {(manifest.goal_id, manifest.task_id, manifest.goal_revision) for manifest in by_criterion.values()}
    if len(bindings) != 1:
        raise ValueError("revalidation Job manifests must share one Goal, Task, and revision")
    goal_id, task_id, goal_revision = next(iter(bindings))

    # Preflight every durable collision before creating the plan or any Job.
    for manifest in by_criterion.values():
        if manifest.input_artifacts:
            raise ValueError("revalidation Job manifests with input artifacts require an atomic payload-aware submission path")
        authority = coordinator.store.get("goal_revisions", str(manifest.goal_id))
        if authority and int(authority.get("goal_revision") or 0) > int(manifest.goal_revision or 0):
            raise ValueError("revalidation Goal revision is older than host authority")
        existing_job = coordinator.store.get("jobs", manifest.job_id)
        if existing_job and existing_job.get("manifest_hash") != manifest.manifest_hash:
            raise ValueError("revalidation Job id conflicts with existing work")
        existing = coordinator.store.get("idempotency", manifest.idempotency_key)
        if existing:
            job = coordinator.store.get("jobs", str(existing.get("job_id") or ""))
            if not job or job.get("manifest_hash") != manifest.manifest_hash:
                raise ValueError("revalidation idempotency key conflicts with existing work")

    plan_id = f"invalidation-plan-{uuid.uuid4().hex}"
    now = coordinator.clock()
    plan_record = {
        "schema_version": "across-goal-invalidation-plan/1.0",
        "plan_id": plan_id,
        "changed_fingerprints": list(plan.changed_fingerprints),
        "affected_criterion_ids": list(plan.affected_criterion_ids),
        "stale_evidence_ids": list(plan.stale_evidence_ids),
        "preserved_evidence_ids": list(plan.preserved_evidence_ids),
        "goal_id": goal_id,
        "task_id": task_id,
        "goal_revision": goal_revision,
        "created_at": now,
    }
    job_ids: list[str] = []
    persisted = {
        **attempt,
        "plan_id": plan_id,
        "job_ids": job_ids,
        "goal_id": goal_id,
        "task_id": task_id,
        "goal_revision": goal_revision,
        "state": "queued",
        "created_at": now,
        "updated_at": now,
    }
    created_jobs: list["JobManifest"] = []
    try:
        with coordinator.store.lock(f"revalidation-batch-{attempt['attempt_id']}"):
            for criterion_id in sorted(by_criterion):
                manifest = by_criterion[criterion_id]
                existed = coordinator.store.get("jobs", manifest.job_id) is not None
                coordinator.submit_job(manifest)
                job_ids.append(manifest.job_id)
                if not existed:
                    created_jobs.append(manifest)
            persisted["job_ids"] = job_ids
            coordinator.store.put("invalidation_plans", plan_id, plan_record)
            coordinator.store.put("revalidation_attempts", str(attempt["attempt_id"]), persisted)
    except Exception:
        coordinator.store.delete("revalidation_attempts", str(attempt["attempt_id"]))
        coordinator.store.delete("invalidation_plans", plan_id)
        for manifest in created_jobs:
            coordinator.store.delete("jobs", manifest.job_id)
            coordinator.store.delete("idempotency", manifest.idempotency_key)
        raise
    return persisted
