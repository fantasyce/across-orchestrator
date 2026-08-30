from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import hashlib
import json
import re
import shutil
import subprocess

from .models import Task
from .findings import enrich_with_finding_state
from .redaction import redact_sensitive_value


EVIDENCE_RECEIPT_SCHEMA = "across-evidence-receipt/1.0"


def artifact_record(project_root: str, path: str, *, baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(project_root).resolve()
    target = (root / path).resolve()
    if not str(target).startswith(str(root)):
        return {"path": path, "present": False, "error": "outside_project"}
    if not target.exists() or not target.is_file():
        return {"path": path, "present": False}
    data = target.read_bytes()
    record = {
        "path": path,
        "present": True,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    if baseline:
        record["fresh"] = not (
            str(baseline.get("sha256") or "") == record["sha256"]
            and int(baseline.get("size") or -1) == record["size"]
        )
    return record


def task_artifact_records(task: Task) -> list[dict[str, Any]]:
    required = list(task.contract.get("requiredArtifacts", []))
    if task.metadata.get("artifact_delivery_mode") != "managed_read_only":
        baseline = task.metadata.get("artifact_baseline") or {}
        return [artifact_record(task.project_root, path, baseline=baseline.get(path)) for path in required]

    managed = task.metadata.get("managed_artifacts") or {}
    root_value = str(task.metadata.get("managed_artifact_root") or "")
    root = Path(root_value).resolve() if root_value else None
    root_is_valid = bool(root and root.name == task.task_id and root.parent.name == "artifacts")
    records: list[dict[str, Any]] = []
    for path in required:
        item = dict(managed.get(path) or {})
        parts = [part for part in str(path).replace("\\", "/").lstrip("/").split("/") if part and part != "."]
        if not root_is_valid or not parts or any(part == ".." for part in parts):
            records.append({"path": path, "present": False, "fresh": False, "error": "invalid_managed_path"})
            continue
        target = (root / "/".join(parts)).resolve()
        if root not in target.parents or not target.is_file():
            records.append({"path": path, "present": False, "fresh": False})
            continue
        data = target.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        expected_path = str(item.get("storage_path") or "")
        valid = expected_path == str(target) and digest == str(item.get("sha256") or "")
        records.append({
            "path": path,
            "storage_path": str(target),
            "present": valid,
            "fresh": True,
            "size": len(data),
            "sha256": digest,
            "source": "managed_read_only",
        })
    return records


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
    artifacts = task_artifact_records(task)
    present = [artifact for artifact in artifacts if artifact.get("present") and artifact.get("fresh") is not False]
    missing = [artifact["path"] for artifact in artifacts if not artifact.get("present")]
    stale = [artifact["path"] for artifact in artifacts if artifact.get("present") and artifact.get("fresh") is False]
    if task.metadata.get("execution_mode") == "reference_delivery":
        gates = _reference_delivery_gates(task, artifacts)
        gates["required_artifacts_present"] = not missing and not stale
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
    unsupported_claims = _unsupported_execution_claims(task, artifacts)
    semantic_review_required = bool((task.metadata.get("host_metadata") or {}).get("semantic_review_required"))
    analysis_outcome = _analysis_outcome(task, artifacts)
    hard_failures = [
        *(f"missing_artifact:{path}" for path in missing),
        *(f"stale_artifact:{path}" for path in stale),
        *unsupported_claims,
    ]
    structurally_passed = not hard_failures and len(present) == len(required)
    quality_score = 0 if not structurally_passed else 70 if semantic_review_required else 100
    return enrich_with_finding_state({
        "status": "passed" if structurally_passed else "failed",
        "quality_score": quality_score,
        "verification_scope": "structural_plus_human_semantic_review" if semantic_review_required else "structural",
        "analysis_outcome": analysis_outcome,
        "required_artifacts": len(required),
        "present_artifacts": len(present),
        "missing_artifacts": missing,
        "stale_artifacts": stale,
        "failures": hard_failures,
        "gates": {
            "required_artifacts_present": not missing and not stale,
            "required_artifacts_fresh": not stale,
            "no_artifacts_outside_project": True,
            "execution_claims_bound_to_evidence": not unsupported_claims,
            "semantic_review_recorded": semantic_review_required,
        },
        "quality_report": {
            "quality_gate": "passed" if structurally_passed else "failed",
            "can_complete": structurally_passed,
            "generated_quality_score": quality_score,
            "final_quality_score": quality_score,
            "required_failed_count": len(hard_failures),
            "manual_required_count": 1 if semantic_review_required and structurally_passed else 0,
            "skipped_required_count": 0,
            "checks": {
                "artifact_integrity": not missing and not stale,
                "execution_claims_bound_to_evidence": not unsupported_claims,
            },
        },
        "findings": [{
            "id": "task_artifact_quality",
            "state": "pass" if structurally_passed else "failed",
            "severity": "info" if structurally_passed else "error",
            "summary": "Structural delivery checks passed; semantic acceptance remains human-reviewed." if structurally_passed and semantic_review_required else "Required artifact quality gate passed." if structurally_passed else "Required artifacts are missing, stale, or contain unsupported execution claims.",
            "source_gate": "required_artifacts",
            "evidence": {"missing_artifacts": missing, "stale_artifacts": stale, "unsupported_execution_claims": unsupported_claims, "required_artifacts": required},
            "suggested_action": None if structurally_passed else "Produce fresh artifacts and remove claims that are not backed by durable execution evidence.",
        }],
    }, finding_id="task_artifact_quality", source_gate="required_artifacts", summary="Required artifact quality gate.")


def _unsupported_execution_claims(task: Task, artifacts: list[dict[str, Any]]) -> list[str]:
    host_metadata = task.metadata.get("host_metadata") or {}
    execution_contract = host_metadata.get("execution_contract") or {}
    if str(execution_contract.get("route") or "local").lower() == "worker":
        return []
    positive_remote = re.compile(
        r"(?:\b(?:used|ran|executed|dispatched|delegated|through|via)\b.{0,40}\bremote\s+worker\b)|"
        r"(?:(?:使用|调用|通过|交给|委派给|由).{0,30}(?:远端|远程).{0,12}(?:worker|工作节点|节点))|"
        r"(?:\bremote\s+worker\b.{0,30}\b(?:completed|executed|returned|produced)\b)",
        re.IGNORECASE,
    )
    negative = re.compile(r"(?:没有|未|并未|不曾|无法|不能|no|not|without).{0,18}(?:远端|远程|remote).{0,12}(?:worker|工作节点|节点)", re.IGNORECASE)
    claims: list[str] = []
    for artifact in artifacts:
        for line in _artifact_text(task, artifact).splitlines():
            if positive_remote.search(line) and not negative.search(line):
                claims.append(f"unsupported_remote_worker_claim:{artifact.get('path')}")
                break
    return sorted(set(claims))


def _analysis_outcome(task: Task, artifacts: list[dict[str, Any]]) -> str:
    blocked = re.compile(
        r"不可安全执行|无法安全执行|不应继续执行|not safe to (?:execute|proceed)|cannot safely (?:execute|proceed)",
        re.IGNORECASE,
    )
    return "decision_required" if any(blocked.search(_artifact_text(task, item)) for item in artifacts) else "delivered"


def _artifact_text(task: Task, artifact: dict[str, Any], *, max_bytes: int = 2 * 1024 * 1024) -> str:
    raw_path = artifact.get("storage_path")
    if raw_path:
        target = Path(str(raw_path)).resolve()
    else:
        root = Path(task.project_root).resolve()
        target = (root / str(artifact.get("path") or "")).resolve()
        if target != root and root not in target.parents:
            return ""
    try:
        if not target.is_file() or target.stat().st_size > max_bytes:
            return ""
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def build_evidence_bundle(task: Task, events: list[dict[str, Any]]) -> dict[str, Any]:
    artifacts = task_artifact_records(task)
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


def bind_evidence_to_criteria(
    receipt: Mapping[str, Any], binding: Mapping[str, Any], *, authority: Mapping[str, Any]
) -> dict[str, Any]:
    """Project criterion evidence only from coordinator-owned authority."""

    if not isinstance(receipt, Mapping) or not isinstance(binding, Mapping) or not isinstance(authority, Mapping):
        raise ValueError("evidence receipt, binding, and authority must be objects")
    if binding.get("schema_version") != "across-goal-evidence-binding/1.0":
        raise ValueError("unsupported goal evidence binding schema")
    caller_owned_forbidden = {
        "verified", "passed", "verdict", "trust_state", "lease_state", "validator",
        "validator_results", "goal_id", "goal_revision", "task_id", "job_id", "run_id",
        "attempt", "attempt_id", "lease_id", "input_fingerprint",
    }
    if caller_owned_forbidden.intersection(binding):
        raise ValueError("evidence trust cannot be supplied by the caller")
    receipt_hash_field = (
        "receipt_hash" if receipt.get("schema_version") == "across-worker-evidence/1.0" else "evidence_sha256"
    )
    receipt_hash = str(receipt.get(receipt_hash_field) or "")
    unhashed = dict(receipt)
    unhashed.pop(receipt_hash_field, None)
    observed_hash = (
        _worker_payload_hash(unhashed)
        if receipt_hash_field == "receipt_hash"
        else _canonical_sha256(unhashed)
    )
    if receipt_hash != observed_hash:
        raise ValueError("evidence receipt hash is invalid")
    for field in (
        "goal_id", "goal_revision", "task_id", "job_id", "run_id", "attempt", "lease_id", "input_fingerprint"
    ):
        if authority.get(field) != receipt.get(field):
            raise ValueError(f"evidence {field} binding mismatch")
    if authority.get("lease_state") != "terminal_valid":
        raise ValueError("evidence lease is expired or invalid")
    if receipt.get("terminal_state") != "completed":
        raise ValueError("evidence receipt is not completed")
    receipt_criteria = set(map(str, receipt.get("criterion_ids") or ()))
    bound_criteria = set(map(str, binding.get("criterion_ids") or ()))
    if not bound_criteria or not bound_criteria.issubset(receipt_criteria):
        raise ValueError("evidence criterion binding mismatch")
    receipt_artifacts = {
        str(item.get("artifact_id")): str(item.get("sha256"))
        for item in receipt.get("artifacts") or ()
        if isinstance(item, Mapping)
    }
    bound_artifacts = {
        str(key): str(value) for key, value in (binding.get("artifact_digests") or {}).items()
    }
    if bound_artifacts != receipt_artifacts:
        raise ValueError("evidence artifact digest mismatch")
    registered = set(map(str, authority.get("registered_validator_ids") or ()))
    validator_results = authority.get("validator_results")
    if not isinstance(validator_results, Mapping):
        raise ValueError("host validator results must be an object")
    validators: dict[str, dict[str, str]] = {}
    verified = True
    for criterion_id in sorted(bound_criteria):
        result = validator_results.get(criterion_id)
        if not isinstance(result, Mapping):
            verified = False
            continue
        validator_id = str(result.get("validator_id") or "")
        method = str(result.get("method") or "")
        status = str(result.get("status") or "").lower()
        if not validator_id or validator_id not in registered or not method:
            raise ValueError("evidence validator is not registered by the host")
        if status not in {"passed", "ready", "verified"}:
            verified = False
        validators[criterion_id] = {
            "validator_id": validator_id,
            "method": method,
            "status": status,
        }
    verdict = "verified" if verified else "needs_review"
    return {
        **dict(binding),
        "goal_id": authority["goal_id"],
        "goal_revision": authority["goal_revision"],
        "task_id": authority["task_id"],
        "job_id": authority["job_id"],
        "run_id": authority["run_id"],
        "attempt": authority["attempt"],
        "attempt_id": f"{authority['job_id']}:{authority['attempt']}",
        "lease_id": authority["lease_id"],
        "lease_state": authority["lease_state"],
        "input_fingerprint": authority["input_fingerprint"],
        "criterion_ids": sorted(bound_criteria),
        "artifact_digests": dict(sorted(bound_artifacts.items())),
        "validators": validators,
        "receipt_hash": receipt_hash,
        "verdict": verdict,
        "trust_state": verdict,
        "authority": "across-orchestrator-worker-coordinator",
    }


def _worker_payload_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    return _git_commit_sha_from_metadata(workspace_root)


def _git_commit_sha_from_metadata(workspace_root: Path) -> str:
    """Resolve HEAD without spawning Git, including linked worktrees.

    Evidence projection runs inside the long-lived HTTP sidecar. On macOS a
    nested Git process can stall in that environment, and Git's parent search
    may escape the client-selected project root. HEAD and refs are stable,
    read-only metadata, so read them directly only when ``.git`` belongs to
    the selected root.
    """
    root = workspace_root.resolve()
    marker = root / ".git"
    if not marker.exists():
        return ""
    try:
        if marker.is_dir():
            git_dir = marker
        else:
            declaration = marker.read_text(encoding="utf-8").strip()
            if not declaration.startswith("gitdir:"):
                return ""
            value = declaration.partition(":")[2].strip()
            git_dir = (marker.parent / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()

        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
            return head.lower()
        if not head.startswith("ref:"):
            return ""
        ref_name = head.partition(":")[2].strip()
        common_dir = git_dir
        common_marker = git_dir / "commondir"
        if common_marker.is_file():
            common_value = common_marker.read_text(encoding="utf-8").strip()
            common_dir = (git_dir / common_value).resolve()
        for base in (git_dir, common_dir):
            ref_path = base / ref_name
            if ref_path.is_file():
                commit = ref_path.read_text(encoding="utf-8").strip()
                if re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
                    return commit.lower()
            packed_refs = base / "packed-refs"
            if packed_refs.is_file():
                for line in packed_refs.read_text(encoding="utf-8").splitlines():
                    if not line or line.startswith(("#", "^")):
                        continue
                    commit, separator, packed_ref = line.partition(" ")
                    if separator and packed_ref == ref_name and re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
                        return commit.lower()
    except (OSError, UnicodeDecodeError):
        return ""
    return ""


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
