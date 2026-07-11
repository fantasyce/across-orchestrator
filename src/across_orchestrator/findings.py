from __future__ import annotations

from typing import Any, Mapping


FINDING_SCHEMA_VERSION = "across-autopilot-finding/1.0"
FINDING_STATES = ("pass", "auto_fix_available", "ask_user", "blocked", "no_op", "failed")
_TERMINAL_OK_STATES = {"pass", "no_op"}
_STATE_ALIASES = {
    "ok": "pass",
    "passed": "pass",
    "ready": "pass",
    "completed": "pass",
    "success": "pass",
    "succeeded": "pass",
    "fixed": "pass",
    "auto_fix": "auto_fix_available",
    "autofix": "auto_fix_available",
    "fix_available": "auto_fix_available",
    "needs_fix": "auto_fix_available",
    "needs_user": "ask_user",
    "needs_input": "ask_user",
    "user_input": "ask_user",
    "manual": "ask_user",
    "attention": "ask_user",
    "partial": "ask_user",
    "warning": "ask_user",
    "warn": "ask_user",
    "needs_attention": "ask_user",
    "environment_blocked": "blocked",
    "skipped": "no_op",
    "not_applicable": "no_op",
    "none": "no_op",
    "noop": "no_op",
    "error": "failed",
    "failure": "failed",
    "fail": "failed",
}
_SEVERITY_STATES = {
    "blocker": "blocked",
    "critical": "blocked",
    "error": "blocked",
    "high": "blocked",
    "blocking": "blocked",
    "warn": "no_op",
    "warning": "no_op",
    "medium": "no_op",
    "low": "no_op",
    "info": "no_op",
}


def normalize_finding_state(
    value: Any = None,
    *,
    status: Any = None,
    passed: Any = None,
    severity: Any = None,
) -> str:
    for candidate in (value, status):
        text = _state_token(candidate)
        if text in FINDING_STATES:
            return text
        if text in _STATE_ALIASES:
            return _STATE_ALIASES[text]
    if passed is True:
        return "pass"
    if passed is False:
        return "failed"
    severity_state = _SEVERITY_STATES.get(_state_token(severity))
    return severity_state or "failed"


def normalize_finding(
    finding: Any,
    *,
    index: int = 0,
    defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = dict(finding) if isinstance(finding, Mapping) else {"summary": finding}
    fallback = dict(defaults or {})
    severity = _clean(source.get("severity") or source.get("level") or fallback.get("severity")) or "info"
    explicit_state = source.get("state")
    if explicit_state is None:
        explicit_state = source.get("verdict")
    if explicit_state is None:
        explicit_state = fallback.get("state")
    status = source.get("status")
    if status is None:
        status = fallback.get("status")
    passed = source.get("passed") if "passed" in source else fallback.get("passed")
    state = normalize_finding_state(explicit_state, status=status, passed=passed, severity=severity)
    source_gate = _clean(
        source.get("source_gate")
        or source.get("sourceGate")
        or source.get("gate")
        or fallback.get("source_gate")
    ) or None
    finding_id = _clean(
        source.get("id")
        or source.get("code")
        or source.get("key")
        or source.get("check_id")
        or source.get("checkId")
        or source.get("adapter_id")
        or source.get("adapterId")
        or source.get("gate_id")
        or source.get("gateId")
        or fallback.get("id")
        or source_gate
    ) or f"finding-{index + 1}"
    summary = _clean(
        source.get("summary")
        or source.get("title")
        or source.get("message")
        or source.get("description")
        or fallback.get("summary")
    ) or finding_id
    repair_round = _non_negative_int(
        source.get("repair_round")
        if source.get("repair_round") is not None
        else source.get("repairRound")
        if source.get("repairRound") is not None
        else fallback.get("repair_round"),
        default=0,
    )
    evidence = source.get("evidence") if "evidence" in source else fallback.get("evidence")
    suggested_action = _clean(
        source.get("suggested_action")
        or source.get("suggestedAction")
        or source.get("remediation")
        or fallback.get("suggested_action")
    ) or None
    owner = _clean(
        source.get("owner")
        or source.get("repo")
        or source.get("repository")
        or fallback.get("owner")
    ) or None
    normalized: dict[str, Any] = {
        "schema_version": FINDING_SCHEMA_VERSION,
        "id": finding_id,
        "state": state,
        "severity": severity,
        "summary": summary,
        "evidence": _copy_json_value(evidence),
        "suggested_action": suggested_action,
        "owner": owner,
        "repair_round": repair_round,
        "source_gate": source_gate,
    }

    details = _clean(
        source.get("details")
        or source.get("detail")
        or source.get("body")
        or source.get("excerpt")
        or fallback.get("details")
    )
    if details:
        normalized["details"] = details
    finding_source = _clean(source.get("source") or source.get("adapter") or source.get("check"))
    if finding_source:
        normalized["source"] = finding_source
    file_path = _clean(source.get("file") or source.get("path") or fallback.get("file"))
    if file_path:
        normalized["file"] = file_path
    line = _positive_int(source.get("line") if source.get("line") is not None else fallback.get("line"))
    if line is not None:
        normalized["line"] = line
    refs = _string_list(
        source.get("evidence_refs")
        or source.get("evidenceRefs")
        or source.get("refs")
        or fallback.get("evidence_refs")
    )
    if refs:
        normalized["evidence_refs"] = refs
    metadata = source.get("metadata") if isinstance(source.get("metadata"), Mapping) else fallback.get("metadata")
    if isinstance(metadata, Mapping) and metadata:
        normalized["metadata"] = {
            str(key): _copy_json_value(value)
            for key, value in sorted(metadata.items(), key=lambda item: str(item[0]))
            if value is not None
        }
    return normalized


def normalize_findings(
    findings: Any,
    *,
    defaults: Mapping[str, Any] | None = None,
    sort: bool = True,
) -> list[dict[str, Any]]:
    normalized = [
        normalize_finding(item, index=index, defaults=defaults)
        for index, item in enumerate(_as_list(findings))
    ]
    if not sort:
        return normalized
    state_rank = {state: index for index, state in enumerate(FINDING_STATES)}
    return sorted(
        normalized,
        key=lambda item: (
            state_rank.get(str(item.get("state")), len(state_rank)),
            str(item.get("id") or ""),
            str(item.get("summary") or ""),
        ),
    )


def normalize_quality_report(
    payload: Mapping[str, Any],
    *,
    finding_id: str,
    source_gate: str,
    summary: str | None = None,
    owner: str | None = None,
    repair_round: int | None = None,
) -> dict[str, Any]:
    result = dict(payload)
    round_source = repair_round
    if round_source is None:
        round_source = result.get("repair_round")
    if round_source is None:
        round_source = result.get("repairRound")
    round_number = _non_negative_int(round_source, default=0)
    defaults = {
        "id": finding_id,
        "source_gate": source_gate,
        "summary": summary,
        "owner": owner,
        "repair_round": round_number,
    }
    raw_findings = _first_populated(
        result.get("normalized_findings"),
        result.get("normalizedFindings"),
        result.get("findings"),
        result.get("quality_findings"),
        result.get("qualityFindings"),
    )
    if raw_findings is not None:
        findings = normalize_findings(raw_findings, defaults=defaults)
    else:
        findings = _derive_legacy_findings(result, defaults=defaults)
    if not findings:
        findings = [
            normalize_finding(
                {
                    "id": finding_id,
                    "state": normalize_finding_state(
                        result.get("finding_state"),
                        status=result.get("status") or result.get("quality") or result.get("quality_gate"),
                        passed=result.get("passed"),
                    ),
                    "summary": summary or _clean(result.get("summary")) or finding_id.replace("_", " "),
                    "evidence": _fallback_evidence(result),
                },
                defaults=defaults,
            )
        ]
    if repair_round is not None:
        findings = [{**item, "repair_round": round_number} for item in findings]

    report_signal_present = any(
        key in result
        for key in ("finding_state", "status", "quality", "quality_gate", "passed")
    )
    report_state = (
        normalize_finding_state(
            result.get("finding_state"),
            status=result.get("status") or result.get("quality") or result.get("quality_gate"),
            passed=result.get("passed"),
        )
        if report_signal_present
        else None
    )
    finding_state = aggregate_finding_state(findings, report_state=report_state)
    failed_gates = _merge_unique(
        _string_list(result.get("failed_gates") or result.get("failedGates")),
        failed_gate_ids_from_findings(findings),
    )
    result["repair_round"] = round_number
    result["finding_state"] = finding_state
    result["findings"] = findings
    result["normalized_findings"] = findings
    result["failed_gates"] = failed_gates
    result["source_gates"] = _merge_unique(
        _string_list(result.get("source_gates") or result.get("sourceGates")),
        [str(item.get("source_gate")) for item in findings if item.get("source_gate")],
    )
    return result


def enrich_with_finding_state(
    payload: Mapping[str, Any],
    *,
    finding_id: str,
    source_gate: str,
    summary: str | None = None,
) -> dict[str, Any]:
    return normalize_quality_report(
        payload,
        finding_id=finding_id,
        source_gate=source_gate,
        summary=summary,
    )


def aggregate_finding_state(findings: Any, *, report_state: Any = None) -> str:
    states = [
        normalize_finding_state(item.get("state"))
        for item in _as_list(findings)
        if isinstance(item, Mapping)
    ]
    for state in ("failed", "blocked", "ask_user", "auto_fix_available"):
        if state in states:
            return state
    normalized_report_state = normalize_finding_state(report_state) if report_state is not None else None
    if normalized_report_state not in {None, "pass", "no_op"}:
        return normalized_report_state
    if normalized_report_state == "no_op" and states and all(state == "no_op" for state in states):
        return "no_op"
    if states and all(state in _TERMINAL_OK_STATES for state in states):
        return "pass" if "pass" in states else "no_op"
    return normalized_report_state or "failed"


def quality_report_passed(payload: Mapping[str, Any]) -> bool:
    if payload.get("finding_state") is not None:
        return normalize_finding_state(payload.get("finding_state")) in _TERMINAL_OK_STATES
    if payload.get("findings") is not None or payload.get("normalized_findings") is not None:
        return aggregate_finding_state(payload.get("findings") or payload.get("normalized_findings")) in _TERMINAL_OK_STATES
    if payload.get("passed") is not None:
        return payload.get("passed") is True
    return normalize_finding_state(status=payload.get("status") or payload.get("quality") or payload.get("quality_gate")) in _TERMINAL_OK_STATES


def finding_state_failed(value: Any) -> bool:
    return normalize_finding_state(value) not in _TERMINAL_OK_STATES


def failed_gate_ids_from_findings(findings: Any) -> list[str]:
    gates: list[str] = []
    for item in _as_list(findings):
        if not isinstance(item, Mapping):
            continue
        if not finding_state_failed(item.get("state") or item.get("finding_state")):
            continue
        gate = _clean(item.get("source_gate") or item.get("sourceGate") or item.get("id"))
        if gate and gate not in gates:
            gates.append(gate)
    return gates


def blocked_findings_for_exhaustion(findings: Any, *, repair_round: int) -> list[dict[str, Any]]:
    blocked: list[dict[str, Any]] = []
    for index, item in enumerate(normalize_findings(findings, sort=False)):
        if item.get("state") in _TERMINAL_OK_STATES:
            continue
        metadata = dict(item.get("metadata") or {})
        metadata.update({
            "previous_state": item.get("state"),
            "transition": "repair_exhausted",
        })
        blocked.append(normalize_finding({
            **item,
            "state": "blocked",
            "repair_round": repair_round,
            "suggested_action": item.get("suggested_action") or "Review the exhausted repair with a human owner.",
            "metadata": metadata,
        }, index=index))
    return blocked


def _derive_legacy_findings(result: Mapping[str, Any], *, defaults: Mapping[str, Any]) -> list[dict[str, Any]]:
    gate_results = result.get("gate_results") or result.get("gateResults")
    if isinstance(gate_results, list) and gate_results:
        return normalize_findings([
            {
                **dict(item),
                "id": item.get("adapter_id") or item.get("adapterId") or item.get("gate_id") or item.get("gateId"),
                "source_gate": item.get("adapter_id") or item.get("adapterId") or item.get("gate_id") or item.get("gateId"),
                "summary": item.get("summary") or item.get("message") or f"{item.get('adapter_id') or item.get('gate_id') or 'quality gate'} {item.get('status') or 'unknown'}.",
                "evidence": item.get("evidence") if "evidence" in item else dict(item),
            }
            for item in gate_results
            if isinstance(item, Mapping)
        ], defaults=defaults)

    gates = result.get("gates")
    if isinstance(gates, Mapping) and gates:
        return normalize_findings([
            {
                "id": str(gate),
                "state": "pass" if passed is True else "failed",
                "severity": "info" if passed is True else "error",
                "summary": f"{str(gate).replace('_', ' ')} {'passed' if passed is True else 'failed'}.",
                "source_gate": str(gate),
                "evidence": {"passed": passed},
            }
            for gate, passed in gates.items()
        ], defaults=defaults)

    failures = result.get("failures")
    if failures:
        return normalize_findings([
            {
                **(dict(item) if isinstance(item, Mapping) else {"id": item, "summary": item}),
                "state": (item.get("state") if isinstance(item, Mapping) else None) or "failed",
                "source_gate": (
                    item.get("source_gate")
                    or item.get("sourceGate")
                    or item.get("check_id")
                    if isinstance(item, Mapping)
                    else item
                ),
                "evidence": dict(item) if isinstance(item, Mapping) else {"failure": item},
            }
            for item in _as_list(failures)
        ], defaults=defaults)

    failed_gates = result.get("failed_gates") or result.get("failedGates")
    if failed_gates:
        return normalize_findings([
            {
                "id": gate,
                "state": "failed",
                "severity": "error",
                "summary": f"{str(gate).replace('_', ' ')} failed.",
                "source_gate": gate,
                "evidence": {"failed_gate": gate},
            }
            for gate in _as_list(failed_gates)
        ], defaults=defaults)

    missing = result.get("missing_artifacts") or result.get("missingArtifacts")
    if missing:
        return normalize_findings([
            {
                "id": "required_artifacts_present",
                "state": "failed",
                "severity": "error",
                "summary": "Required artifacts are missing.",
                "source_gate": "required_artifacts_present",
                "evidence": {"missing_artifacts": _as_list(missing)},
                "suggested_action": "Produce the missing required artifacts before promotion.",
            }
        ], defaults=defaults)
    return []


def _fallback_evidence(result: Mapping[str, Any]) -> Any:
    for key in ("evidence", "failures", "failed_gates", "missing_artifacts", "gates", "gate_results"):
        if result.get(key) is not None:
            return _copy_json_value(result.get(key))
    return None


def _first_populated(*values: Any) -> Any:
    for value in values:
        if isinstance(value, list) and value:
            return value
        if isinstance(value, Mapping) and value:
            return value
    return None


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _merge_unique(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for item in group:
            value = _clean(item)
            if value and value not in merged:
                merged.append(value)
    return merged


def _string_list(value: Any) -> list[str]:
    return sorted({_clean(item) for item in _as_list(value) if _clean(item)})


def _state_token(value: Any) -> str:
    return _clean(value).lower().replace("-", "_").replace(" ", "_")


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 1 else None


def _non_negative_int(value: Any, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _copy_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _clean(value: Any) -> str:
    return str(value or "").strip()
