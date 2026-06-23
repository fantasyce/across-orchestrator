from __future__ import annotations

from typing import Any


AUTOPILOT_METADATA_SCHEMA = "across-orchestrator-autopilot-metadata/1.0"
REQUIRED_AUTOPILOT_FIELDS = {"run_id", "spec_id", "schema_version", "evidence_contract"}


def validate_autopilot_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate optional Autopilot task metadata carried by Agent Loop runs."""

    if not isinstance(metadata, dict):
        return None
    autopilot = metadata.get("autopilot")
    if autopilot is None:
        return None
    if not isinstance(autopilot, dict):
        raise ValueError("metadata.autopilot must be an object")
    missing = sorted(field for field in REQUIRED_AUTOPILOT_FIELDS if not str(autopilot.get(field) or "").strip())
    if missing:
        raise ValueError(f"metadata.autopilot missing required fields: {', '.join(missing)}")
    if autopilot.get("schema_version") != "across-loop-spec/1.0":
        raise ValueError("metadata.autopilot.schema_version must be across-loop-spec/1.0")
    if autopilot.get("evidence_contract") != "across-loop-evidence/1.0":
        raise ValueError("metadata.autopilot.evidence_contract must be across-loop-evidence/1.0")
    for key in ("actions_allowed", "actions_blocked"):
        value = autopilot.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"metadata.autopilot.{key} must be a string array")
    sandbox = autopilot.get("sandbox") or {}
    if sandbox and not isinstance(sandbox, dict):
        raise ValueError("metadata.autopilot.sandbox must be an object")
    return {
        "schema_version": AUTOPILOT_METADATA_SCHEMA,
        "run_id": str(autopilot["run_id"]),
        "spec_id": str(autopilot["spec_id"]),
        "candidate_id": str(autopilot.get("candidate_id") or ""),
        "candidate_mode": str(autopilot.get("candidate_mode") or ""),
        "candidate_manifest": str(autopilot.get("candidate_manifest") or ""),
        "loop_spec_schema": str(autopilot["schema_version"]),
        "evidence_contract": str(autopilot["evidence_contract"]),
        "actions_allowed_count": len(autopilot.get("actions_allowed") or []),
        "actions_blocked_count": len(autopilot.get("actions_blocked") or []),
        "sandbox_root": str(sandbox.get("root") or "") if sandbox else "",
    }


def autopilot_metadata_summary(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    try:
        return validate_autopilot_metadata(metadata)
    except ValueError as exc:
        return {
            "schema_version": AUTOPILOT_METADATA_SCHEMA,
            "valid": False,
            "error": str(exc),
        }
