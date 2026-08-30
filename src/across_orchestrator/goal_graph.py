from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class InvalidationPlan:
    changed_fingerprints: tuple[str, ...]
    affected_criterion_ids: tuple[str, ...]
    stale_evidence_ids: tuple[str, ...]
    preserved_evidence_ids: tuple[str, ...]
    stale_evidence_by_criterion: tuple[tuple[str, tuple[str, ...]], ...] = ()


def compute_invalidation(
    graph: Mapping[str, Any], changed_fingerprints: set[str]
) -> InvalidationPlan:
    criteria = graph.get("criteria")
    if not isinstance(criteria, Mapping):
        raise ValueError("goal graph criteria must be an object")
    changed = tuple(sorted({str(item) for item in changed_fingerprints if str(item)}))
    affected = {
        str(criterion_id)
        for criterion_id, raw in criteria.items()
        if set(map(str, (raw or {}).get("input_fingerprints") or ())).intersection(changed)
    }
    advanced = True
    while advanced:
        advanced = False
        for criterion_id, raw in criteria.items():
            identifier = str(criterion_id)
            dependencies = set(map(str, (raw or {}).get("depends_on") or ()))
            if identifier not in affected and dependencies.intersection(affected):
                affected.add(identifier)
                advanced = True
    stale = {
        str(evidence_id)
        for criterion_id, raw in criteria.items()
        if str(criterion_id) in affected
        for evidence_id in (raw or {}).get("evidence_ids") or ()
    }
    all_evidence = {
        str(evidence_id)
        for raw in criteria.values()
        for evidence_id in (raw or {}).get("evidence_ids") or ()
    }
    return InvalidationPlan(
        changed_fingerprints=changed,
        affected_criterion_ids=tuple(sorted(affected)),
        stale_evidence_ids=tuple(sorted(stale)),
        preserved_evidence_ids=tuple(sorted(all_evidence - stale)),
        stale_evidence_by_criterion=tuple(
            (
                str(criterion_id),
                tuple(sorted(map(str, (raw or {}).get("evidence_ids") or ()))),
            )
            for criterion_id, raw in sorted(criteria.items(), key=lambda item: str(item[0]))
            if str(criterion_id) in affected
        ),
    )
