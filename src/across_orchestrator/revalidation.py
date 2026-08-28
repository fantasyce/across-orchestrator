from __future__ import annotations

import uuid
from collections.abc import Sequence

from .goal_graph import InvalidationPlan


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
        "state": "queued",
    }
