from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
import uuid

from across_agents_assistant.task_manager.orchestration.contract_acceptance import (
    run_delivery_contract_acceptance,
)
from across_agents_assistant.task_manager.orchestration.delivery_contract import (
    build_owner_delivery_contract,
)
from across_agents_assistant.task_manager.orchestration.release_e2e import (
    RELEASE_E2E_SCENARIO_ID,
    build_release_e2e_subtasks,
    build_release_e2e_task_request,
    write_release_e2e_reference_artifact,
)
from across_agents_assistant.task_manager.orchestration.requirements import (
    extract_requirement_manifest,
)


APP_GRADE_RELEASE_E2E_ENGINE = "app_grade_release_e2e"


def build_release_e2e_payload(
    *,
    task_id: str,
    project_root: str,
    run_label: str | None = None,
) -> Dict[str, Any]:
    request = build_release_e2e_task_request(
        scenario_id=RELEASE_E2E_SCENARIO_ID,
        project_dir=project_root,
        run_label=run_label,
    )
    manifest_obj = extract_requirement_manifest(
        task_id,
        request["description"],
        request["project_dir"],
    )
    manifest = {
        "manifest_id": manifest_obj.manifest_id,
        "task_id": manifest_obj.task_id,
        "project_dir": manifest_obj.project_dir,
        "deliverables": [asdict(item) for item in manifest_obj.deliverables],
        "quality_checks": [asdict(item) for item in manifest_obj.quality_checks],
        "created_at": manifest_obj.created_at,
        "updated_at": manifest_obj.updated_at,
    }
    contract = build_owner_delivery_contract(
        task_id=task_id,
        description=request["description"],
        task_types=request["task_types"],
        project_dir=request["project_dir"],
        manifest=manifest,
    )
    subtasks = build_release_e2e_subtasks(request["allowed_subtask_agents"])
    return {
        "engine": APP_GRADE_RELEASE_E2E_ENGINE,
        "scenario_id": request["scenario_id"],
        "request": request,
        "manifest": manifest,
        "contract": contract,
        "subtasks": subtasks,
    }


def run_release_e2e_payload(
    *,
    task_id: str,
    project_root: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    request = payload["request"]
    contract = payload["contract"]
    subtasks = payload["subtasks"]
    project_dir = request["project_dir"]
    written = write_release_e2e_reference_artifact(project_dir)
    fake_task = SimpleNamespace(
        task_id=task_id,
        description=request["description"],
        project_dir=project_dir,
        subtasks=[
            SimpleNamespace(
                subtask_id=f"st-{item['id']}",
                agent_id=item["agent"],
                status="completed",
            )
            for item in subtasks
        ],
    )
    artifact_records = _artifact_records(task_id, project_dir, subtasks)
    acceptance = run_delivery_contract_acceptance(fake_task, contract, artifact_records)
    exact_files = sorted(
        path.relative_to(project_dir).as_posix()
        for path in Path(project_dir).rglob("*")
        if path.is_file()
    )
    return {
        "engine": APP_GRADE_RELEASE_E2E_ENGINE,
        "scenario_id": request["scenario_id"],
        "scenario_title": request["scenario_title"],
        "complexity_score": request["complexity_score"],
        "project_root": project_dir,
        "required_files": list(request["required_files"]),
        "written_files": sorted(written),
        "exact_files": exact_files,
        "subtasks": [
            {
                "subtask_id": f"st-{item['id']}",
                "agent_id": item["agent"],
                "status": "completed",
                "deliverables": item.get("deliverables", []),
                "dependencies": item.get("dependencies", []),
            }
            for item in subtasks
        ],
        "delivery_quality": acceptance["delivery_quality"],
        "quality_report": acceptance["quality_report"],
        "acceptance_report": acceptance,
    }


def _artifact_records(task_id: str, project_dir: str, subtasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for item in subtasks:
        subtask_id = f"st-{item['id']}"
        for deliverable in item.get("deliverables", []) or []:
            path_hint = deliverable.get("path_hint")
            if not path_hint:
                continue
            records.append(
                {
                    "artifact_id": f"artifact-{uuid.uuid4().hex[:10]}",
                    "task_id": task_id,
                    "subtask_id": subtask_id,
                    "artifact_type": deliverable.get("artifact_type") or "file",
                    "path_hint": path_hint,
                    "path": str(Path(project_dir) / path_hint),
                    "status": "accepted",
                }
            )
    return records
