from __future__ import annotations

from pathlib import Path
from typing import Any
import hashlib
import re
import shutil
import subprocess

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
    app_grade = task.metadata.get("app_grade") or {}
    if app_grade.get("quality_report"):
        report = dict(app_grade["quality_report"])
        report.setdefault("status", report.get("quality_gate", "unknown"))
        return report
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
        return {
            "status": "passed" if not failed else "failed",
            "required_artifacts": len(required),
            "present_artifacts": len(present),
            "missing_artifacts": missing,
            "gates": gates,
            "failures": failed,
            "produced_files": sorted(artifact["path"] for artifact in present),
            "required_files": required,
        }
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
        "quality": quality,
        "events": events,
    }
    if task.metadata.get("app_grade"):
        bundle["app_grade"] = task.metadata["app_grade"]
    return bundle


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
