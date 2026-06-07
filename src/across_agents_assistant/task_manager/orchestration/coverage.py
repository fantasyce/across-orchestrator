"""Decomposition coverage gate — ensures every required manifest deliverable
has an owning subtask contract before business subtasks are dispatched."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from across_agents_assistant.task_manager.models import RequirementDeliverable
from across_agents_assistant.task_manager.orchestration.requirements import canonical_requirement_key


@dataclass
class CoverageGap:
    requirement_id: str
    path_hint: Optional[str]
    artifact_type: str
    reason: str


@dataclass
class CoverageResult:
    passed: bool
    assigned: Dict[str, str] = field(default_factory=dict)
    gaps: List[CoverageGap] = field(default_factory=list)


def normalize_hint(path_hint: Optional[str]) -> str:
    """Normalize a path hint for comparison (strip prefix, normalize slashes)."""
    if not path_hint:
        return ""
    value = path_hint.strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


def evaluate_decomposition_coverage(
    manifest: Dict[str, Any],
    subtask_contracts: List[Dict[str, Any]],
) -> CoverageResult:
    """Compare manifest deliverables against subtask contracts.

    Returns a ``CoverageResult`` that lists which requirements are assigned
    and which have no matching contract deliverable (gaps).
    """
    result = CoverageResult(passed=True)
    deliverables = manifest.get("deliverables", []) or []

    # Flatten subtask-level contract deliverables
    contract_deliverables: List[Dict[str, Any]] = []
    for contract in subtask_contracts:
        if contract.get("level") != "subtask":
            continue
        for deliverable in contract.get("expected_deliverables", []) or []:
            item = dict(deliverable)
            item["_subtask_id"] = contract.get("subtask_id")
            contract_deliverables.append(item)

    for req in deliverables:
        if not req.get("required", True):
            continue
        match = find_matching_contract_deliverable(req, contract_deliverables)
        if match:
            result.assigned[req["requirement_id"]] = match["_subtask_id"]
        else:
            result.passed = False
            result.gaps.append(
                CoverageGap(
                    requirement_id=req["requirement_id"],
                    path_hint=req.get("path_hint"),
                    artifact_type=req.get("artifact_type", "file"),
                    reason="required_deliverable_unassigned",
                )
            )

    return result


def find_matching_contract_deliverable(
    requirement: Dict[str, Any],
    contract_deliverables: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Try to match a manifest requirement to a contract deliverable.

    Matching order:
    1. Exact normalized path_hint.
    2. Basename match (same filename, possibly different parent path).
    3. If neither side has a path_hint, match by artifact_type.
    """
    req_hint = normalize_hint(requirement.get("path_hint"))
    req_type = requirement.get("artifact_type")
    for deliverable in contract_deliverables:
        item_hint = normalize_hint(deliverable.get("path_hint"))
        item_type = deliverable.get("artifact_type")
        if req_hint and item_hint and req_hint == item_hint:
            return deliverable
        if req_hint and item_hint and canonical_requirement_key(req_hint) == canonical_requirement_key(item_hint):
            return deliverable
        if req_hint and item_hint and os.path.basename(req_hint) == os.path.basename(item_hint):
            return deliverable
        if not req_hint and req_type and req_type == item_type:
            return deliverable
    return None
