from __future__ import annotations

from collections import Counter
from statistics import mean
import time
from typing import Any, Dict, Iterable, List, Optional

from across_agents_assistant.agent_ids import LOCAL_CLI_AGENT_IDS
from across_agents_assistant.llm_gateway.provider_registry import get_default_provider_ids


TERMINAL_STATUSES = {
    "completed",
    "completed_with_failures",
    "failed",
    "cancelled",
}

PASSING_GATES = {"passed"}
BLOCKING_GATES = {"failed", "inconsistent"}
ATTENTION_GATES = {"manual_required", "partial"}
LOCAL_AGENT_IDS = set(LOCAL_CLI_AGENT_IDS)
CLOUD_AGENT_IDS = set(get_default_provider_ids())
REQUIRED_RELEASE_PROBES = {
    "workspace_hygiene",
    "security_privacy",
    "static_web",
    "api_service",
    "cli_generic",
    "browser_e2e",
}
PROBE_TYPE_ALIASES = {
    "static_web_smoke": "static_web",
}


def build_release_evaluation_summary(
    task_rows: Iterable[Dict[str, Any]],
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Summarize cached task quality reports into a release-candidate signal.

    This is intentionally read-only and probe-free. It consumes already stored
    delivery quality data so opening the task page cannot resume historical work
    or run expensive browser/build checks.
    """
    evaluated: List[Dict[str, Any]] = []
    terminal_task_count = 0
    stack_coverage: Counter[str] = Counter()
    agent_coverage: Counter[str] = Counter()

    for row in task_rows:
        status = str(row.get("status") or "created")
        if status in TERMINAL_STATUSES:
            terminal_task_count += 1

        quality = _extract_quality(row)
        if not quality:
            continue

        item = _build_evaluation_item(row, quality)
        evaluated.append(item)
        for stack in _task_stacks(row):
            stack_coverage[stack] += 1
        for agent_id in _task_agents(row):
            agent_coverage[agent_id] += 1

    evaluated.sort(
        key=lambda item: item.get("updated_at") or item.get("created_at") or 0,
        reverse=True,
    )

    evaluated_count = len(evaluated)
    passed_count = sum(1 for item in evaluated if item["quality_gate"] in PASSING_GATES)
    blocked_count = sum(1 for item in evaluated if item["is_blocked"])
    manual_count = sum(1 for item in evaluated if item["manual_required_count"] > 0)
    skipped_count = sum(1 for item in evaluated if item["skipped_required_count"] > 0)
    scores = [item["final_quality_score"] for item in evaluated if item["final_quality_score"] is not None]
    average_score = int(round(mean(scores))) if scores else None
    pass_rate = round(passed_count / evaluated_count, 4) if evaluated_count else 0.0
    total_remediation_count = sum(item["remediation_count"] for item in evaluated)
    gate_breakdown = Counter(item["quality_gate"] for item in evaluated)
    quality_trend = _build_quality_trend(evaluated)
    agent_mix_summary = _build_agent_mix_summary(agent_coverage)
    probe_coverage = _build_probe_coverage(evaluated)
    top_risks = _build_top_risks(
        evaluated,
        evaluated_count=evaluated_count,
        passed_count=passed_count,
        blocked_count=blocked_count,
        manual_count=manual_count,
        skipped_count=skipped_count,
        average_score=average_score,
        agent_mix_summary=agent_mix_summary,
    )
    readiness = _release_readiness(
        evaluated_count=evaluated_count,
        pass_rate=pass_rate,
        blocked_count=blocked_count,
        manual_count=manual_count,
        skipped_count=skipped_count,
        average_score=average_score,
        agent_mix_summary=agent_mix_summary,
    )
    readiness_checks = _build_readiness_checks(
        evaluated_count=evaluated_count,
        pass_rate=pass_rate,
        blocked_count=blocked_count,
        manual_count=manual_count,
        skipped_count=skipped_count,
        average_score=average_score,
        quality_trend=quality_trend,
        agent_mix_summary=agent_mix_summary,
        probe_coverage=probe_coverage,
    )

    return {
        "release_readiness": readiness,
        "generated_at": float(now if now is not None else time.time()),
        "evaluated_task_count": evaluated_count,
        "terminal_task_count": terminal_task_count,
        "passed_task_count": passed_count,
        "blocked_task_count": blocked_count,
        "manual_task_count": manual_count,
        "skipped_task_count": skipped_count,
        "pass_rate": pass_rate,
        "average_final_quality_score": average_score,
        "total_remediation_count": total_remediation_count,
        "gate_breakdown": dict(sorted(gate_breakdown.items())),
        "top_risks": top_risks,
        "readiness_checks": readiness_checks,
        "quality_trend": quality_trend,
        "agent_mix_summary": agent_mix_summary,
        "probe_coverage": probe_coverage,
        "recent_evaluations": evaluated[:10],
        "stack_coverage": dict(sorted(stack_coverage.items())),
        "agent_coverage": dict(sorted(agent_coverage.items())),
        "recommendation": _recommendation(readiness, top_risks),
    }


def _extract_quality(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    decision = row.get("last_owner_decision") or {}
    if not isinstance(decision, dict):
        return None
    delivery_quality = decision.get("delivery_quality")
    if not isinstance(delivery_quality, dict):
        return None
    quality_report = delivery_quality.get("quality_report")
    if not isinstance(quality_report, dict):
        quality_report = {}
    gate = (
        quality_report.get("quality_gate")
        or delivery_quality.get("delivery_quality")
        or delivery_quality.get("quality_gate")
    )
    if not gate:
        return None
    return {
        "delivery_quality": delivery_quality,
        "quality_report": quality_report,
        "quality_gate": str(gate),
        "probe_results": delivery_quality.get("probe_results") or quality_report.get("probe_results") or [],
        "gate_results": quality_report.get("gate_results") or [],
    }


def _build_evaluation_item(row: Dict[str, Any], quality: Dict[str, Any]) -> Dict[str, Any]:
    quality_report = quality["quality_report"]
    gate = quality["quality_gate"]
    required_failed_count = _int_value(quality_report.get("required_failed_count"))
    manual_required_count = _int_value(quality_report.get("manual_required_count"))
    skipped_required_count = _int_value(
        quality_report.get("required_skipped_count", quality_report.get("skipped_required_count"))
    )
    remediation_count = _int_value(quality_report.get("remediation_count"))
    score = quality_report.get("final_quality_score")
    final_score = _optional_int(score)
    is_blocked = gate in BLOCKING_GATES or required_failed_count > 0
    normalized_probe_results = _normalize_probe_results(quality.get("probe_results") or [])
    normalized_gate_results = _normalize_gate_results(quality.get("gate_results") or [])
    probe_summary = _build_probe_summary([*normalized_probe_results, *normalized_gate_results])
    agent_mix = _build_task_agent_mix(row, quality.get("gate_results") or [])
    benchmark_status = _item_benchmark_status(
        gate=gate,
        required_failed_count=required_failed_count,
        manual_required_count=manual_required_count,
        skipped_required_count=skipped_required_count,
        probe_summary=probe_summary,
    )

    return {
        "task_id": str(row.get("task_id") or ""),
        "description": str(row.get("description") or ""),
        "status": str(row.get("status") or "created"),
        "quality_gate": gate,
        "final_quality_score": final_score,
        "generated_quality_score": _optional_int(quality_report.get("generated_quality_score")),
        "required_failed_count": required_failed_count,
        "manual_required_count": manual_required_count,
        "skipped_required_count": skipped_required_count,
        "remediation_count": remediation_count,
        "is_blocked": is_blocked,
        "owner_agent": row.get("owner_agent"),
        "delivery_mode": row.get("delivery_mode") or "legacy",
        "task_types": list(row.get("task_types") or []),
        "probe_results": normalized_probe_results,
        "gate_results": normalized_gate_results,
        "probe_summary": probe_summary,
        "agent_mix": agent_mix,
        "benchmark_status": benchmark_status,
        "audit_trace": {
            "quality_gate": gate,
            "final_quality_score": final_score,
            "remediation_count": remediation_count,
            "required_failed_count": required_failed_count,
            "manual_required_count": manual_required_count,
            "skipped_required_count": skipped_required_count,
            "passed_probe_count": len(probe_summary["passed"]),
            "failed_probe_count": len(probe_summary["failed"]),
        },
        "score_breakdown": quality_report.get("score_breakdown") or {},
        "created_at": _optional_float(row.get("created_at")),
        "updated_at": _optional_float(row.get("updated_at")),
    }


def _task_stacks(row: Dict[str, Any]) -> List[str]:
    stacks: List[str] = []
    for value in row.get("task_types") or []:
        stack = str(value).strip().lower()
        if stack and stack not in stacks:
            stacks.append(stack)
    mode = str(row.get("delivery_mode") or "").strip().lower()
    if mode and mode != "legacy" and mode not in stacks:
        stacks.append(mode)
    return stacks or ["legacy"]


def _task_agents(row: Dict[str, Any]) -> List[str]:
    agents: List[str] = []
    owner = str(row.get("owner_agent") or "").strip()
    if owner and owner != "auto":
        agents.append(owner)
    for value in row.get("allowed_subtask_agents") or []:
        agent_id = str(value).strip()
        if agent_id and agent_id not in agents:
            agents.append(agent_id)
    return agents


def _build_task_agent_mix(row: Dict[str, Any], gate_results: Optional[List[Dict[str, Any]]] = None) -> Dict[str, List[str]]:
    gate_mix = _agent_mix_from_gate_results(gate_results or [])
    if gate_mix:
        return gate_mix
    actual_agents = sorted(_task_agents(row))
    return {
        "actual_agents": actual_agents,
        "local_agents": [agent_id for agent_id in actual_agents if agent_id in LOCAL_AGENT_IDS],
        "cloud_agents": [agent_id for agent_id in actual_agents if agent_id in CLOUD_AGENT_IDS],
    }


def _agent_mix_from_gate_results(gate_results: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    for gate in gate_results:
        if not isinstance(gate, dict):
            continue
        gate_type = str(gate.get("adapter_id") or gate.get("gate_id") or "")
        if gate_type not in {"agent_mix", "gate-agent-mix"}:
            continue
        evidence = gate.get("evidence") or {}
        if not isinstance(evidence, dict):
            continue
        candidates: List[Dict[str, Any]] = []
        if any(key in evidence for key in ("actual_agents", "local_agents", "cloud_agents")):
            candidates.append(evidence)
        for constraint in evidence.get("satisfied_constraints") or []:
            if isinstance(constraint, dict) and isinstance(constraint.get("evidence"), dict):
                candidates.append(constraint["evidence"])
        for candidate in candidates:
            actual_agents = _dedupe_sorted(candidate.get("actual_agents"))
            if not actual_agents:
                continue
            local_agents = _dedupe_sorted(candidate.get("local_agents"))
            cloud_agents = _dedupe_sorted(candidate.get("cloud_agents"))
            return {
                "actual_agents": actual_agents,
                "local_agents": local_agents or [agent_id for agent_id in actual_agents if agent_id in LOCAL_AGENT_IDS],
                "cloud_agents": cloud_agents or [agent_id for agent_id in actual_agents if agent_id in CLOUD_AGENT_IDS],
            }
    return {}


def _dedupe_sorted(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    return sorted({
        str(value).strip()
        for value in values
        if str(value).strip()
    })


def _build_top_risks(
    evaluated: List[Dict[str, Any]],
    *,
    evaluated_count: int,
    passed_count: int,
    blocked_count: int,
    manual_count: int,
    skipped_count: int,
    average_score: Optional[int],
    agent_mix_summary: Dict[str, Any],
) -> List[Dict[str, Any]]:
    risks: List[Dict[str, Any]] = []
    required_failures = sum(item["required_failed_count"] for item in evaluated)
    if required_failures:
        risks.append({
            "kind": "required_gate_failure",
            "severity": "high",
            "count": required_failures,
            "message": f"{required_failures} required quality gate failure(s) block release.",
        })
    if blocked_count and not required_failures:
        risks.append({
            "kind": "blocked_task",
            "severity": "high",
            "count": blocked_count,
            "message": f"{blocked_count} evaluated task(s) are blocked by delivery quality.",
        })
    manual_or_skipped = manual_count + skipped_count
    if manual_or_skipped:
        risks.append({
            "kind": "manual_or_skipped_gate",
            "severity": "medium",
            "count": manual_or_skipped,
            "message": f"{manual_or_skipped} task(s) still need manual or skipped gate resolution.",
        })
    if evaluated_count and passed_count < evaluated_count:
        risks.append({
            "kind": "pass_rate",
            "severity": "medium",
            "count": evaluated_count - passed_count,
            "message": "Not every quality-gated task has passed.",
        })
    if average_score is not None and average_score < 80:
        risks.append({
            "kind": "quality_score",
            "severity": "medium",
            "count": average_score,
            "message": f"Average final quality score is {average_score}, below the release target of 80.",
        })
    if evaluated_count and not agent_mix_summary.get("satisfies_release_mix"):
        missing = agent_mix_summary.get("missing") or []
        risks.append({
            "kind": "agent_mix",
            "severity": "medium",
            "count": len(missing),
            "message": "Release evidence does not yet cover the required local/cloud agent mix.",
        })
    if evaluated_count and evaluated_count < 3:
        risks.append({
            "kind": "sample_size",
            "severity": "low",
            "count": evaluated_count,
            "message": "Fewer than three quality-gated tasks have been evaluated.",
        })
    return risks[:5]


def _release_readiness(
    *,
    evaluated_count: int,
    pass_rate: float,
    blocked_count: int,
    manual_count: int,
    skipped_count: int,
    average_score: Optional[int],
    agent_mix_summary: Dict[str, Any],
) -> str:
    if evaluated_count == 0:
        return "no_evidence"
    if blocked_count > 0 or pass_rate < 0.8:
        return "blocked"
    if (
        evaluated_count < 3
        or manual_count > 0
        or skipped_count > 0
        or average_score is None
        or average_score < 80
        or not agent_mix_summary.get("satisfies_release_mix")
    ):
        return "attention"
    return "ready"


def _build_quality_trend(evaluated: List[Dict[str, Any]]) -> Dict[str, Any]:
    points = [
        {
            "task_id": item["task_id"],
            "score": item["final_quality_score"],
            "quality_gate": item["quality_gate"],
            "updated_at": item.get("updated_at") or item.get("created_at") or 0.0,
        }
        for item in sorted(
            evaluated,
            key=lambda value: value.get("updated_at") or value.get("created_at") or 0,
        )
        if item.get("final_quality_score") is not None
    ]
    if not points:
        return {
            "direction": "no_data",
            "latest_score": None,
            "previous_score": None,
            "delta": None,
            "point_count": 0,
            "points": [],
        }
    latest = points[-1]["score"]
    previous = points[-2]["score"] if len(points) >= 2 else None
    delta = latest - previous if latest is not None and previous is not None else None
    if delta is None:
        direction = "single_point"
    elif delta >= 3:
        direction = "improving"
    elif delta <= -3:
        direction = "regressing"
    else:
        direction = "stable"
    return {
        "direction": direction,
        "latest_score": latest,
        "previous_score": previous,
        "delta": delta,
        "point_count": len(points),
        "points": points[-8:],
    }


def _build_agent_mix_summary(agent_coverage: Counter[str]) -> Dict[str, Any]:
    distinct_agents = sorted(agent_coverage.keys())
    local_agents = [agent_id for agent_id in distinct_agents if agent_id in LOCAL_AGENT_IDS]
    cloud_agents = [agent_id for agent_id in distinct_agents if agent_id in CLOUD_AGENT_IDS]
    missing: List[str] = []
    if len(distinct_agents) < 3:
        missing.append("at least 3 distinct agents")
    if len(local_agents) < 2:
        missing.append("at least 2 local agents")
    if len(cloud_agents) < 1:
        missing.append("at least 1 cloud agent")
    return {
        "distinct_agent_count": len(distinct_agents),
        "local_agent_count": len(local_agents),
        "cloud_agent_count": len(cloud_agents),
        "distinct_agents": distinct_agents,
        "local_agents": local_agents,
        "cloud_agents": cloud_agents,
        "satisfies_release_mix": not missing,
        "missing": missing,
    }


def _normalize_probe_results(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    probes: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        probe_type = _canonical_probe_type(item.get("probe_type") or item.get("type") or item.get("name"))
        if not probe_type:
            continue
        if item.get("passed") is True or str(item.get("status") or "").lower() == "passed":
            status = "passed"
        elif item.get("required") is False and str(item.get("status") or "").lower() in {"skipped", "manual_required"}:
            status = str(item.get("status")).lower()
        elif str(item.get("status") or "").lower() in {"failed", "skipped", "manual_required", "partial"}:
            status = str(item.get("status")).lower()
        elif item.get("passed") is False:
            status = "failed"
        else:
            status = "unknown"
        probes.append({
            "probe_type": probe_type,
            "status": status,
            "required": bool(item.get("required", True)),
        })
    return probes


def _normalize_gate_results(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    gates: List[Dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        probe_type = _canonical_probe_type(
            item.get("adapter_id")
            or item.get("probe_type")
            or item.get("gate_id")
            or item.get("id")
        )
        if not probe_type:
            continue
        status = str(item.get("status") or ("passed" if item.get("passed") is True else "unknown")).lower()
        if status not in {"passed", "failed", "skipped", "manual_required", "partial"}:
            status = "unknown"
        gates.append({
            "probe_type": probe_type,
            "status": status,
            "required": bool(item.get("required", True)),
        })
    return gates


def _build_probe_coverage(evaluated: List[Dict[str, Any]]) -> Dict[str, Any]:
    coverage: Dict[str, Counter[str]] = {
        "passed": Counter(),
        "failed": Counter(),
        "skipped": Counter(),
        "manual_required": Counter(),
        "unknown": Counter(),
    }
    for item in evaluated:
        seen_for_item: set[tuple[str, str]] = set()
        for probe in [*(item.get("probe_results") or []), *(item.get("gate_results") or [])]:
            status = str(probe.get("status") or "unknown")
            probe_type = str(probe.get("probe_type"))
            seen_key = (status, probe_type)
            if seen_key in seen_for_item:
                continue
            seen_for_item.add(seen_key)
            bucket = coverage.get(status, coverage["unknown"])
            bucket[probe_type] += 1
    passed_probe_types = set(coverage["passed"].keys())
    missing_required = sorted(REQUIRED_RELEASE_PROBES - passed_probe_types)
    return {
        "passed": {key: coverage["passed"][key] for key in sorted(coverage["passed"])},
        "failed": {key: coverage["failed"][key] for key in sorted(coverage["failed"])},
        "skipped": {key: coverage["skipped"][key] for key in sorted(coverage["skipped"])},
        "manual_required": {
            key: coverage["manual_required"][key]
            for key in sorted(coverage["manual_required"])
        },
        "unknown": {key: coverage["unknown"][key] for key in sorted(coverage["unknown"])},
        "required_probe_types": sorted(REQUIRED_RELEASE_PROBES),
        "missing_required_probe_types": missing_required,
        "satisfies_release_probe_coverage": not missing_required,
}


def _build_probe_summary(probes: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    summary: Dict[str, set[str]] = {
        "passed": set(),
        "failed": set(),
        "manual_required": set(),
        "skipped": set(),
        "unknown": set(),
    }
    for probe in probes:
        status = str(probe.get("status") or "unknown")
        probe_type = str(probe.get("probe_type") or "").strip()
        if not probe_type:
            continue
        bucket = status if status in summary else "unknown"
        summary[bucket].add(probe_type)
    return {
        key: sorted(values)
        for key, values in summary.items()
    }


def _item_benchmark_status(
    *,
    gate: str,
    required_failed_count: int,
    manual_required_count: int,
    skipped_required_count: int,
    probe_summary: Dict[str, List[str]],
) -> str:
    if gate in BLOCKING_GATES or required_failed_count > 0 or probe_summary.get("failed"):
        return "failed"
    if (
        gate in ATTENTION_GATES
        or manual_required_count > 0
        or skipped_required_count > 0
        or probe_summary.get("manual_required")
        or probe_summary.get("skipped")
    ):
        return "attention"
    if gate in PASSING_GATES:
        return "passed"
    return "unknown"


def _canonical_probe_type(value: Any) -> str:
    probe_type = str(value or "").strip()
    if probe_type.startswith("gate-"):
        probe_type = probe_type[5:]
    return PROBE_TYPE_ALIASES.get(probe_type, probe_type)


def _readiness_check(
    check_id: str,
    status: str,
    label: str,
    message: str,
    *,
    severity: str = "medium",
) -> Dict[str, Any]:
    return {
        "id": check_id,
        "status": status,
        "label": label,
        "message": message,
        "severity": severity,
    }


def _build_readiness_checks(
    *,
    evaluated_count: int,
    pass_rate: float,
    blocked_count: int,
    manual_count: int,
    skipped_count: int,
    average_score: Optional[int],
    quality_trend: Dict[str, Any],
    agent_mix_summary: Dict[str, Any],
    probe_coverage: Dict[str, Any],
) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    checks.append(_readiness_check(
        "evidence_count",
        "passed" if evaluated_count >= 3 else "warning",
        "Evaluated tasks",
        f"{evaluated_count} quality-gated task(s) evaluated.",
        severity="low",
    ))
    checks.append(_readiness_check(
        "pass_rate",
        "passed" if pass_rate >= 0.8 else "failed",
        "Pass rate",
        f"{int(round(pass_rate * 100))}% of evaluated tasks passed quality gates.",
        severity="high",
    ))
    checks.append(_readiness_check(
        "blocking_gates",
        "passed" if blocked_count == 0 else "failed",
        "Blocking gates",
        f"{blocked_count} task(s) have blocking quality gates.",
        severity="high",
    ))
    manual_or_skipped = manual_count + skipped_count
    checks.append(_readiness_check(
        "manual_or_skipped",
        "passed" if manual_or_skipped == 0 else "warning",
        "Manual or skipped gates",
        f"{manual_or_skipped} task(s) need manual or skipped gate review.",
        severity="medium",
    ))
    checks.append(_readiness_check(
        "average_score",
        "passed" if average_score is not None and average_score >= 80 else "warning",
        "Average score",
        f"Average final quality score is {average_score if average_score is not None else 'not available'}.",
        severity="medium",
    ))
    trend_direction = str(quality_trend.get("direction") or "no_data")
    checks.append(_readiness_check(
        "quality_trend",
        "failed" if trend_direction == "regressing" else ("warning" if trend_direction in {"no_data", "single_point"} else "passed"),
        "Quality trend",
        f"Recent quality trend is {trend_direction}.",
        severity="medium",
    ))
    missing_mix = agent_mix_summary.get("missing") or []
    checks.append(_readiness_check(
        "agent_mix",
        "passed" if not missing_mix else "warning",
        "Agent mix",
        "Release evidence covers the required local/cloud agent mix." if not missing_mix else "Missing " + ", ".join(missing_mix) + ".",
        severity="medium",
    ))
    missing_probes = probe_coverage.get("missing_required_probe_types") or []
    checks.append(_readiness_check(
        "probe_coverage",
        "passed" if not missing_probes else "warning",
        "Probe coverage",
        "Required release probes have passing evidence." if not missing_probes else "Missing passing evidence for " + ", ".join(missing_probes) + ".",
        severity="medium",
    ))
    return checks


def _recommendation(readiness: str, risks: List[Dict[str, Any]]) -> str:
    if readiness == "ready":
        return "Release candidate quality is clean across recent evaluated tasks."
    if readiness == "no_evidence":
        return "Run at least three quality-gated E2E tasks before release."
    if risks:
        return risks[0]["message"]
    return "Review recent evaluated tasks before release."


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _int_value(value: Any) -> int:
    parsed = _optional_int(value)
    return parsed if parsed is not None else 0


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
