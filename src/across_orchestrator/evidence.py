from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib

from .models import Task


def artifact_record(project_root: str, path: str) -> dict[str, Any]:
    root = Path(project_root).resolve()
    target = (root / path).resolve()
    if not str(target).startswith(str(root)):
        return {"path": path, "present": False, "error": "outside_project"}
    if not target.exists() or not target.is_file():
        return {"path": path, "present": False}
    data = target.read_bytes()
    return {
        "path": path,
        "present": True,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def build_quality(task: Task) -> dict[str, Any]:
    required = list(task.contract.get("requiredArtifacts", []))
    artifacts = [artifact_record(task.project_root, path) for path in required]
    present = [artifact for artifact in artifacts if artifact.get("present")]
    missing = [artifact["path"] for artifact in artifacts if not artifact.get("present")]
    return {
        "status": "passed" if len(present) == len(required) else "failed",
        "required_artifacts": len(required),
        "present_artifacts": len(present),
        "missing_artifacts": missing,
        "gates": {
            "required_artifacts_present": not missing,
            "no_artifacts_outside_project": True,
        },
    }


def build_evidence_bundle(task: Task, events: list[dict[str, Any]]) -> dict[str, Any]:
    required = list(task.contract.get("requiredArtifacts", []))
    artifacts = [artifact_record(task.project_root, path) for path in required]
    quality = build_quality(task)
    return {
        "schema_version": "0.1",
        "task_id": task.task_id,
        "goal": task.goal,
        "status": task.status,
        "project_root": task.project_root,
        "contract": task.contract,
        "subtasks": [subtask.__dict__ for subtask in task.subtasks],
        "artifacts": artifacts,
        "quality": quality,
        "events": events,
    }
