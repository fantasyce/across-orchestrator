from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import json
import re
import shutil
import subprocess

from .models import Task
from .findings import enrich_with_finding_state
from .redaction import redact_sensitive_value


EVIDENCE_RECEIPT_SCHEMA = "across-evidence-receipt/1.0"


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
    app_grade = task.metadata.get("app_grade") or {}
    if app_grade.get("quality_report"):
        report = dict(app_grade["quality_report"])
        report.setdefault("status", report.get("quality_gate", "unknown"))
        return enrich_with_finding_state(
            report,
            finding_id="app_grade_quality",
            source_gate="app_grade",
            summary="App-grade quality report.",
        )
    required = list(task.contract.get("requiredArtifacts", []))
    artifacts = [artifact_record(task.project_root, path) for path in required]
    present = [artifact for artifact in artifacts if artifact.get("present")]
    missing = [artifact["path"] for artifact in artifacts if not artifact.get("present")]
    if task.metadata.get("execution_mode") == "reference_delivery":
        gates = _reference_delivery_gates(task, artifacts)
        gates["required_artifacts_present"] = not missing
        gates["no_artifacts_outside_project"] = True
        gates["artifact_integrity"] = not missing
        failed = [key for key, passed in gates.items() if not passed]
        return enrich_with_finding_state({
            "status": "passed" if not failed else "failed",
            "required_artifacts": len(required),
            "present_artifacts": len(present),
            "missing_artifacts": missing,
            "gates": gates,
            "failures": failed,
            "produced_files": sorted(artifact["path"] for artifact in present),
            "required_files": required,
        }, finding_id="reference_delivery_quality", source_gate="reference_delivery", summary="Reference delivery quality gate.")
    return enrich_with_finding_state({
        "status": "passed" if len(present) == len(required) else "failed",
        "required_artifacts": len(required),
        "present_artifacts": len(present),
        "missing_artifacts": missing,
        "gates": {
            "required_artifacts_present": not missing,
            "no_artifacts_outside_project": True,
        },
        "findings": [{
            "id": "task_artifact_quality",
            "state": "pass" if not missing else "failed",
            "severity": "info" if not missing else "error",
            "summary": "Required artifact quality gate passed." if not missing else "Required artifacts are missing.",
            "source_gate": "required_artifacts",
            "evidence": {"missing_artifacts": missing, "required_artifacts": required},
            "suggested_action": None if not missing else "Produce the missing required artifacts.",
        }],
    }, finding_id="task_artifact_quality", source_gate="required_artifacts", summary="Required artifact quality gate.")


def build_evidence_bundle(task: Task, events: list[dict[str, Any]]) -> dict[str, Any]:
    required = list(task.contract.get("requiredArtifacts", []))
    artifacts = [artifact_record(task.project_root, path) for path in required]
    quality = build_quality(task)
    bundle = {
        "schema_version": "0.1",
        "task_id": task.task_id,
        "goal": task.goal,
        "status": task.status,
        "project_root": task.project_root,
        "contract": task.contract,
        "metadata": task.metadata,
        "subtasks": [subtask.__dict__ for subtask in task.subtasks],
        "artifacts": artifacts,
        "finding_state": quality.get("finding_state") or task.finding_state,
        "findings": quality.get("findings") or task.findings,
        "finding_history": task.finding_history,
        "quality": quality,
        "events": events,
    }
    if task.metadata.get("app_grade"):
        bundle["app_grade"] = task.metadata["app_grade"]
    sandbox_entries = list(task.metadata.get("sandbox_executions") or [])
    sandbox_receipts = [
        dict(entry.get("receipt") or {})
        for entry in sandbox_entries
        if isinstance(entry, dict) and isinstance(entry.get("receipt"), dict)
    ]
    if sandbox_receipts:
        unified_receipts = [
            build_evidence_receipt({
                "workspace": {
                    "root": task.project_root,
                    "workspace_id": task.task_id,
                    "commit_sha": str(task.metadata.get("commit_sha") or ""),
                },
                "sandbox_receipt": receipt,
                "validations": [quality],
                "artifacts": artifacts,
                "provenance": {
                    "producer": "across-orchestrator",
                    "task_id": task.task_id,
                    "sandbox_receipt_sha256": receipt.get("receipt_sha256"),
                },
            })
            for receipt in sandbox_receipts
        ]
        bundle["sandbox_executions"] = sandbox_receipts
        bundle["evidence_receipts"] = unified_receipts
        bundle["sandbox_execution"] = sandbox_receipts[-1]
        bundle["evidence_receipt"] = unified_receipts[-1]
    return bundle


def build_evidence_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic, secret-free receipt from execution evidence."""

    workspace = dict(payload.get("workspace") or payload.get("workspace_binding") or {})
    workspace_root = str(workspace.get("root") or payload.get("workspace_root") or "").strip()
    commit_sha = str(workspace.get("commit_sha") or payload.get("commit_sha") or "").strip()
    if workspace_root:
        resolved_root = Path(workspace_root).expanduser().resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError("workspace root must be a directory")
        if not commit_sha:
            commit_sha = _git_commit_sha(resolved_root)
        workspace_sha256 = _text_sha256(str(resolved_root))
    else:
        workspace_sha256 = str(workspace.get("workspace_sha256") or "").strip()
    if not commit_sha:
        commit_sha = "unversioned"
    if not workspace_sha256:
        raise ValueError("workspace_root or workspace_sha256 is required")

    binding = {
        "commit_sha": commit_sha,
        "commit_state": "bound" if commit_sha != "unversioned" else "unversioned",
        "workspace_sha256": workspace_sha256,
        "workspace_id": str(workspace.get("workspace_id") or payload.get("workspace_id") or "workspace"),
    }
    sandbox_receipt = _secret_free_sandbox_receipt(payload.get("sandbox_receipt") or payload.get("sandbox") or {})
    validations = _secret_free_value(payload.get("validations") or [])
    artifacts = _normalize_receipt_artifacts(payload.get("artifacts") or [])
    provenance = _secret_free_value(payload.get("provenance") or {})
    component_hashes = {
        "workspace_binding_sha256": _canonical_sha256(binding),
        "sandbox_receipt_sha256": _canonical_sha256(sandbox_receipt),
        "validations_sha256": _canonical_sha256(validations),
        "artifacts_sha256": _canonical_sha256(artifacts),
        "provenance_sha256": _canonical_sha256(provenance),
    }
    receipt = {
        "schema_version": EVIDENCE_RECEIPT_SCHEMA,
        "verdict": _evidence_verdict(sandbox_receipt, validations),
        "workspace_binding": binding,
        "sandbox_receipt": sandbox_receipt,
        "validations": validations,
        "artifacts": artifacts,
        "provenance": {
            "sources": provenance,
            "hashes": component_hashes,
        },
    }
    receipt["evidence_sha256"] = _canonical_sha256(receipt)
    return receipt


def _evidence_verdict(sandbox_receipt: dict[str, Any], validations: Any) -> str:
    sandbox_status = str(sandbox_receipt.get("status") or "").lower()
    if sandbox_status != "completed":
        return "blocked"

    validation_rows = validations if isinstance(validations, list) else [validations]
    validation_statuses = {
        str(row.get("status") or row.get("quality_gate") or "").lower()
        for row in validation_rows
        if isinstance(row, dict)
    }
    if validation_statuses.intersection({"blocked", "failed", "error", "cancelled", "timed_out"}):
        return "blocked"
    if validation_statuses.intersection({"needs_review", "attention", "pending", "unknown"}):
        return "needs_review"

    enforcement = sandbox_receipt.get("enforcement")
    if not isinstance(enforcement, dict) or not enforcement:
        return "needs_review"
    enforcement_values = {str(value).lower() for value in enforcement.values()}
    if any("not_" in value or "declared" in value for value in enforcement_values):
        return "needs_review"
    return "ready"


def _git_commit_sha(workspace_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _secret_free_sandbox_receipt(value: Any) -> dict[str, Any]:
    receipt = _secret_free_value(value)
    if not isinstance(receipt, dict):
        return {}
    output = receipt.get("output")
    if isinstance(output, dict):
        safe_output = {key: item for key, item in output.items() if key not in {"stdout", "stderr"}}
        for stream in ("stdout", "stderr"):
            raw = output.get(stream)
            if isinstance(raw, str):
                safe_output[f"{stream}_sha256"] = _text_sha256(raw)
        receipt["output"] = safe_output
    return receipt


def _normalize_receipt_artifacts(values: Any) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    if not isinstance(values, list):
        raise ValueError("artifacts must be an array")
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("each artifact must be an object")
        original_content = value.get("content")
        safe = _secret_free_value(value)
        safe.pop("content", None)
        if original_content is not None and "sha256" not in safe:
            safe["sha256"] = _text_sha256(str(original_content))
        path = str(safe.get("path") or "")
        if path and Path(path).is_absolute():
            safe["path"] = Path(path).name
        artifacts.append(safe)
    return artifacts


def _secret_free_value(value: Any) -> Any:
    safe = redact_sensitive_value(value)
    if isinstance(safe, dict):
        return {
            str(key): _secret_free_value(item)
            for key, item in safe.items()
            if str(key).lower() not in {"env", "environment", "headers", "authorization"}
        }
    if isinstance(safe, list):
        return [_secret_free_value(item) for item in safe]
    return safe


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reference_delivery_gates(task: Task, artifacts: list[dict[str, Any]]) -> dict[str, bool]:
    project_root = Path(task.project_root).resolve()
    present_paths = [str(item.get("path") or "") for item in artifacts if item.get("present")]
    gates = {
        "serial_wave_dependencies": _serial_wave_dependencies_pass(task),
        "content_quality": _content_quality_pass(project_root, present_paths),
        "workspace_hygiene": _workspace_hygiene_pass(project_root, present_paths),
        "security_privacy": _security_privacy_pass(project_root, present_paths),
        "agent_mix": len({subtask.agent for subtask in task.subtasks}) >= 1,
        "static_web_smoke": _static_web_smoke_pass(project_root, present_paths),
        "api_service": _api_service_pass(project_root, present_paths),
        "cli_generic": _node_script_pass(project_root, "cli/verify.mjs") if "cli/verify.mjs" in present_paths else True,
        "browser_e2e": _node_script_pass(project_root, "tests/e2e-serial.mjs") if "tests/e2e-serial.mjs" in present_paths else True,
    }
    return gates


def _serial_wave_dependencies_pass(task: Task) -> bool:
    if not task.contract.get("serialPlan"):
        return True
    subtasks_by_id = {subtask.subtask_id: subtask for subtask in task.subtasks}
    waves = sorted({subtask.wave for subtask in task.subtasks})
    if not waves:
        return True
    first_wave = waves[0]
    for subtask in task.subtasks:
        if subtask.wave == first_wave:
            continue
        if not subtask.dependencies:
            return False
        for dependency in subtask.dependencies:
            dep = subtasks_by_id.get(dependency)
            if dep is None or dep.wave >= subtask.wave:
                return False
    return True


def _content_quality_pass(project_root: Path, paths: list[str]) -> bool:
    for path in paths:
        target = (project_root / path).resolve()
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if not text.strip():
            return False
        if "Generated by Across Orchestrator demo adapter" in text:
            return False
        lower = text.lower()
        if (
            "across orchestrator reference delivery" not in lower
            and "reference delivery" not in lower
            and "across-reference-delivery" not in lower
        ):
            if path not in {"api/server.mjs", "cli/verify.mjs", "tests/e2e-serial.mjs", "web/index.html", "web/styles.css", "web/app.js"}:
                return False
    return True


def _workspace_hygiene_pass(project_root: Path, expected_paths: list[str]) -> bool:
    expected = set(expected_paths)
    forbidden_dirs = {"node_modules", ".git", "__pycache__", ".pytest_cache"}
    for item in project_root.rglob("*"):
        rel = item.relative_to(project_root).as_posix()
        if any(part in forbidden_dirs for part in rel.split("/")):
            return False
        if item.is_file() and rel not in expected:
            return False
    return True


_SECRET_PATTERN = re.compile(r"(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")


def _security_privacy_pass(project_root: Path, paths: list[str]) -> bool:
    for path in paths:
        target = project_root / path
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _SECRET_PATTERN.search(text):
            return False
        if "http://".lower() in text.lower() and "127.0.0.1" not in text and "localhost" not in text:
            return False
        if "https://" in text and "nodejs.org" not in text:
            return False
    return True


def _static_web_smoke_pass(project_root: Path, paths: list[str]) -> bool:
    if "web/index.html" not in paths:
        return True
    html = (project_root / "web/index.html").read_text(encoding="utf-8")
    if "./styles.css" not in html or "./app.js" not in html:
        return False
    if "https://" in html or "http://" in html:
        return False
    if "web/styles.css" in paths and not (project_root / "web/styles.css").read_text(encoding="utf-8").strip():
        return False
    if "web/app.js" in paths and "localStorage" not in (project_root / "web/app.js").read_text(encoding="utf-8"):
        return False
    return True


def _api_service_pass(project_root: Path, paths: list[str]) -> bool:
    if "api/server.mjs" not in paths:
        return True
    source = (project_root / "api/server.mjs").read_text(encoding="utf-8")
    return all(marker in source for marker in ["createServer", "/health", "/api/pipeline", "/api/gates"])


def _node_script_pass(project_root: Path, relative_path: str) -> bool:
    if not shutil.which("node"):
        return False
    completed = subprocess.run(
        ["node", relative_path],
        cwd=project_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    return completed.returncode == 0
