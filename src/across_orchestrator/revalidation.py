from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .goal_graph import InvalidationPlan

if TYPE_CHECKING:
    from .coordinator import WorkerCoordinator
    from .worker_protocol import JobManifest


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

    plan_id = f"invalidation-plan-{uuid.uuid4().hex}"
    now = coordinator.clock()
    plan_record = {
        "schema_version": "across-goal-invalidation-plan/1.0",
        "plan_id": plan_id,
        "changed_fingerprints": list(plan.changed_fingerprints),
        "affected_criterion_ids": list(plan.affected_criterion_ids),
        "stale_evidence_ids": list(plan.stale_evidence_ids),
        "preserved_evidence_ids": list(plan.preserved_evidence_ids),
        "created_at": now,
    }
    coordinator.store.put("invalidation_plans", plan_id, plan_record)
    job_ids: list[str] = []
    for criterion_id in sorted(by_criterion):
        manifest = by_criterion[criterion_id]
        coordinator.submit_job(manifest)
        job_ids.append(manifest.job_id)
    persisted = {
        **attempt,
        "plan_id": plan_id,
        "job_ids": job_ids,
        "state": "queued",
        "created_at": now,
        "updated_at": now,
    }
    coordinator.store.put("revalidation_attempts", str(attempt["attempt_id"]), persisted)
    return persisted
