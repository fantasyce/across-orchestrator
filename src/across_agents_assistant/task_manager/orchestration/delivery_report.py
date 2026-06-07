"""Final delivery report builder for task quality closure."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional


def build_delivery_report(
    *,
    task: Any,
    manifest: Optional[Dict[str, Any]],
    artifact_records: List[Dict[str, Any]],
    acceptance_records: List[Dict[str, Any]],
    quality_health: Dict[str, Any],
    final_status: Optional[str] = None,
) -> Dict[str, Any]:
    deliverables = list((manifest or {}).get("deliverables", []) or [])
    artifacts_by_path = {}
    for artifact in artifact_records or []:
        content_ref = artifact.get("content_ref")
        name = artifact.get("name")
        if content_ref:
            artifacts_by_path[content_ref] = artifact
            artifacts_by_path[os.path.realpath(content_ref)] = artifact
        if name:
            artifacts_by_path[name] = artifact

    produced_required = []
    missing_required = []
    invalid_required = []
    for item in deliverables:
        if not item.get("required", True):
            continue
        status = item.get("status") or "unassigned"
        path_hint = item.get("path_hint")
        evidence = dict(item.get("evidence") or {})
        produced = {
            "requirement_id": item.get("requirement_id"),
            "path_hint": path_hint,
            "artifact_type": item.get("artifact_type"),
            "status": status,
            "evidence": evidence,
        }
        if status in {"produced", "accepted"}:
            produced_required.append(produced)
        elif status == "missing":
            missing_required.append(path_hint or item.get("artifact_type") or item.get("requirement_id"))
        elif status == "invalid":
            invalid_required.append(produced)

    decision = getattr(task, "last_owner_decision", None) or {}
    delivery_quality = decision.get("delivery_quality") or quality_health.get("delivery_quality_report") or {}
    failed_constraints = delivery_quality.get("failed_constraints", []) or []
    attempts = decision.get("quality_remediation_attempts") or {}
    quality_gate = quality_health.get("quality_gate")
    final_status = final_status or getattr(
        getattr(task, "status", None), "value", getattr(task, "status", None)
    )

    quality_remediation_subtasks = [
        st for st in getattr(task, "subtasks", [])
        if str(getattr(st, "subtask_id", "") or "").startswith("st-quality-")
    ]
    active_quality = [
        st.subtask_id for st in quality_remediation_subtasks
        if getattr(getattr(st, "status", None), "value", getattr(st, "status", None)) in {"pending", "dispatched", "running"}
    ]
    attempted = bool(attempts) or bool(active_quality)

    if final_status == "failed" and quality_gate == "passed":
        summary = "Required deliverables were produced, but the task still failed due to orchestration or validation errors."
    elif quality_gate == "passed":
        summary = "All required deliverables were produced and accepted."
    elif quality_gate == "failed":
        missing = ", ".join(str(item) for item in missing_required)
        summary = f"Required deliverables are missing: {missing}." if missing else "Project quality acceptance failed."
    elif quality_gate == "partial":
        summary = "Some required deliverables were produced, but final acceptance is not complete."
    else:
        summary = "Delivery quality is not finalized."

    terminal_statuses = {"completed", "failed", "completed_with_failures", "cancelled"}
    is_terminal = final_status in terminal_statuses

    consistency = {
        "has_active_quality_remediation": bool(active_quality),
        "has_missing_required": bool(missing_required),
        "has_failed_constraints": bool(failed_constraints),
        "is_terminal": is_terminal,
        "terminal_with_active_remediation": is_terminal and bool(active_quality),
    }

    return {
        "task_id": getattr(task, "task_id", None),
        "quality_gate": quality_gate,
        "final_status": final_status,
        "summary": summary,
        "quality_report": delivery_quality.get("quality_report") or {},
        "required_total": sum(1 for item in deliverables if item.get("required", True)),
        "accepted_total": quality_health.get("manifest_accepted", 0),
        "missing_required": missing_required,
        "invalid_required": invalid_required,
        "produced_required": produced_required,
        "failed_constraints": failed_constraints,
        "acceptance_record_count": len(acceptance_records or []),
        "remediation": {
            "attempted": attempted,
            "attempts_by_requirement": attempts,
            "subtask_count": len(quality_remediation_subtasks),
            "max_attempts": decision.get("max_quality_remediation_attempts", 4),
            "active_subtasks": active_quality,
            "exhausted": decision.get("blocked_reason") == "quality_failed",
        },
        "consistency": consistency,
        "next_action": quality_health.get("next_repair_action"),
    }
