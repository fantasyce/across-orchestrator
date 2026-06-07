"""Release-quality benchmark helpers for task delivery reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence


@dataclass(frozen=True)
class BenchmarkThresholds:
    min_quality_score: int = 70
    max_remediation_attempts: int = 2


def evaluate_delivery_benchmark(
    task_payloads: Sequence[Dict[str, Any]],
    *,
    benchmark_id: str,
    expected_files: Optional[Sequence[str]] = None,
    required_probes: Optional[Sequence[str]] = None,
    min_quality_score: int = 70,
    max_remediation_attempts: int = 2,
) -> Dict[str, Any]:
    """Evaluate one or more task status payloads against release-quality gates."""
    thresholds = BenchmarkThresholds(
        min_quality_score=int(min_quality_score),
        max_remediation_attempts=int(max_remediation_attempts),
    )
    scenarios = [
        _evaluate_scenario(
            payload,
            expected_files=list(expected_files or []),
            required_probes=list(required_probes or []),
            thresholds=thresholds,
        )
        for payload in task_payloads
    ]
    failed = [scenario for scenario in scenarios if scenario["status"] != "passed"]
    return {
        "benchmark_id": benchmark_id,
        "benchmark_version": "1.0",
        "status": "failed" if failed else "passed",
        "summary": {
            "scenario_count": len(scenarios),
            "passed_scenarios": len(scenarios) - len(failed),
            "failed_scenarios": len(failed),
            "min_quality_score": min((item["quality_score"] for item in scenarios), default=0),
            "max_remediation_attempts": max((item["remediation_attempts"] for item in scenarios), default=0),
        },
        "scenarios": scenarios,
    }


def _evaluate_scenario(
    payload: Dict[str, Any],
    *,
    expected_files: List[str],
    required_probes: List[str],
    thresholds: BenchmarkThresholds,
) -> Dict[str, Any]:
    quality_health = dict(payload.get("quality_health") or {})
    delivery_report = dict(payload.get("delivery_report") or {})
    delivery_quality = (
        quality_health.get("delivery_quality_report")
        or (payload.get("last_owner_decision") or {}).get("delivery_quality")
        or {}
    )
    quality_report = delivery_quality.get("quality_report") or delivery_report.get("quality_report") or {}
    probe_results = list(delivery_quality.get("probe_results") or [])
    produced_files = _normalize_produced_files(
        delivery_quality.get("produced_required")
        or delivery_report.get("produced_required")
        or []
    )
    remediation = dict(delivery_report.get("remediation") or {})
    remediation_attempts = _remediation_attempt_count(payload, remediation)
    active_remediation = list(remediation.get("active_subtasks") or quality_health.get("active_quality_remediation") or [])
    quality_score = int(quality_report.get("final_quality_score") or 0)
    quality_gate = (
        delivery_report.get("quality_gate")
        or quality_health.get("quality_gate")
        or quality_report.get("quality_gate")
    )
    final_status = delivery_report.get("final_status") or payload.get("status")

    checks: Dict[str, bool] = {
        "task_completed": final_status == "completed",
        "quality_gate_passed": quality_gate == "passed",
        "quality_score_threshold": quality_score >= thresholds.min_quality_score,
        "no_required_failures": int(quality_report.get("required_failed_count") or 0) == 0,
        "no_manual_required": int(quality_report.get("manual_required_count") or 0) == 0,
        "no_required_skips": int(quality_report.get("required_skipped_count") or 0) == 0,
        "no_active_remediation": not active_remediation,
        "remediation_budget": remediation_attempts <= thresholds.max_remediation_attempts,
        "expected_file_inventory": _expected_file_inventory_matches(produced_files, expected_files),
    }
    for probe_type in required_probes:
        checks[f"{probe_type}_passed"] = _probe_passed(probe_results, probe_type)

    failures = _failure_messages(
        checks=checks,
        produced_files=produced_files,
        expected_files=expected_files,
        required_probes=required_probes,
        active_remediation=active_remediation,
        remediation_attempts=remediation_attempts,
        thresholds=thresholds,
        quality_score=quality_score,
        final_status=final_status,
        quality_gate=quality_gate,
    )
    return {
        "task_id": payload.get("task_id"),
        "status": "passed" if not failures else "failed",
        "quality_gate": quality_gate,
        "final_status": final_status,
        "quality_score": quality_score,
        "remediation_attempts": remediation_attempts,
        "produced_files": produced_files,
        "checks": checks,
        "failures": failures,
    }


def _normalize_produced_files(items: Iterable[Any]) -> List[str]:
    files: List[str] = []
    for item in items:
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            value = item.get("path_hint") or item.get("content_ref") or item.get("name")
        else:
            value = None
        if value:
            files.append(str(value))
    return sorted(dict.fromkeys(files))


def _expected_file_inventory_matches(produced_files: Sequence[str], expected_files: Sequence[str]) -> bool:
    if not expected_files:
        return True
    return set(produced_files) == set(str(item) for item in expected_files)


def _probe_passed(probe_results: Sequence[Dict[str, Any]], probe_type: str) -> bool:
    return any(
        str(probe.get("probe_type") or probe.get("id") or "") == probe_type
        and bool(probe.get("passed"))
        for probe in probe_results
    )


def _remediation_attempt_count(payload: Dict[str, Any], remediation: Dict[str, Any]) -> int:
    explicit_count = remediation.get("subtask_count")
    if explicit_count is not None:
        try:
            return max(0, int(explicit_count))
        except (TypeError, ValueError):
            pass

    subtasks = payload.get("subtasks")
    if isinstance(subtasks, list):
        count = sum(
            1
            for item in subtasks
            if isinstance(item, dict)
            and str(item.get("subtask_id") or "").startswith("st-quality-")
        )
        if count:
            return count

    attempts_by_requirement = dict(remediation.get("attempts_by_requirement") or {})
    rounds: List[int] = []
    for value in attempts_by_requirement.values():
        try:
            rounds.append(max(0, int(value or 0)))
        except (TypeError, ValueError):
            continue
    return max(rounds, default=0)


def _failure_messages(
    *,
    checks: Dict[str, bool],
    produced_files: Sequence[str],
    expected_files: Sequence[str],
    required_probes: Sequence[str],
    active_remediation: Sequence[str],
    remediation_attempts: int,
    thresholds: BenchmarkThresholds,
    quality_score: int,
    final_status: Any,
    quality_gate: Any,
) -> List[str]:
    failures: List[str] = []
    if not checks["task_completed"]:
        failures.append(f"task final status is {final_status!r}, expected 'completed'")
    if not checks["quality_gate_passed"]:
        failures.append(f"quality gate is {quality_gate!r}, expected 'passed'")
    if not checks["quality_score_threshold"]:
        failures.append(f"quality score {quality_score} is below {thresholds.min_quality_score}")
    if not checks["no_required_failures"]:
        failures.append("required quality gate failures are present")
    if not checks["no_manual_required"]:
        failures.append("manual-required checks remain")
    if not checks["no_required_skips"]:
        failures.append("required checks were skipped")
    if not checks["no_active_remediation"]:
        failures.append(f"active remediation subtasks remain: {', '.join(active_remediation)}")
    if not checks["remediation_budget"]:
        failures.append(
            f"remediation attempts {remediation_attempts} exceeded {thresholds.max_remediation_attempts}"
        )
    if not checks["expected_file_inventory"]:
        unexpected = sorted(set(produced_files) - set(expected_files))
        missing = sorted(set(expected_files) - set(produced_files))
        if unexpected:
            failures.append(f"unexpected produced files: {', '.join(unexpected)}")
        if missing:
            failures.append(f"expected files missing: {', '.join(missing)}")
    for probe_type in required_probes:
        if not checks.get(f"{probe_type}_passed"):
            failures.append(f"required probe did not pass: {probe_type}")
    return failures
