from __future__ import annotations

from typing import Any
import hashlib
import json
import re

from .redaction import redact_sensitive_value
from .sandbox import normalize_sandbox_policy


EXECUTION_POLICY_SCHEMA = "across-execution-policy/1.0"
RUN_COMPARISON_SCHEMA = "across-run-comparison/1.0"
REPLAY_PLAN_SCHEMA = "across-replay-plan/1.0"

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "release": 3}
_RELEASE_ACTIONS = {"merge", "publish", "release", "sign", "payment", "production"}
_NETWORK_ACTIONS = {"push", "network", "download", "install", "remote", "webhook"}
_WRITE_ACTIONS = {"write", "patch", "mutate", "repair", "delete", "move", "rename"}
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:^|\s)(?:/(?:Users|home|tmp|var|private|Volumes|opt|etc|usr|Applications)(?:/|$)|[A-Za-z]:\\)"
)


def build_execution_policy_contract(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Render the public role/model/budget and risk-selected sandbox contract.

    The contract intentionally exposes model routing labels and bounded limits,
    never credentials, prompts, transcripts, absolute paths, or environment data.
    """

    source = dict(payload or {})
    role = _clean_text(source.get("role") or source.get("capability_role") or "worker", 80)
    model_source = _object(source.get("model_policy") or source.get("model") or {})
    budget_source = _object(source.get("budget") or {})
    sandbox = select_risk_aware_sandbox_policy(source)
    risk_profile = sandbox["risk_profile"]
    external_side_effects = _normalized_side_effects(source)
    approval_required = risk_profile != "low" or bool(external_side_effects)
    contract = {
        "schema_version": EXECUTION_POLICY_SCHEMA,
        "run_id": _clean_text(source.get("run_id") or source.get("task_id") or source.get("loop_id"), 160),
        "role": {
            "id": role,
            "label": _clean_text(source.get("role_label") or role.replace("_", " ").title(), 100),
            "responsibility": _clean_text(source.get("responsibility"), 240),
        },
        "model_policy": {
            "provider": _clean_text(model_source.get("provider") or model_source.get("provider_id"), 100),
            "model": _clean_text(model_source.get("model") or model_source.get("model_id"), 160),
            "fallback_models": _clean_string_list(model_source.get("fallback_models"), 8, 160),
            "required": bool(model_source.get("required")),
            "host_owned_credentials": True,
            "credentials_included": False,
        },
        "budget": {
            "max_model_calls": _bounded_int(budget_source.get("max_model_calls"), 0, 100, 0),
            "max_candidate_repairs": _bounded_int(budget_source.get("max_candidate_repairs"), 0, 20, 0),
            "max_usd": _bounded_float(budget_source.get("max_usd"), 0, 1000, 0),
            "max_runtime_seconds": _bounded_float(budget_source.get("max_runtime_seconds"), 0, 86400, 0),
        },
        "risk": {
            "profile": risk_profile,
            "reason": sandbox["selection_reason"],
            "external_side_effects": external_side_effects,
        },
        "sandbox": sandbox,
        "approval": {
            "required": approval_required,
            "renewed_approval_required_for_replay": bool(external_side_effects),
            "proposer_approver_separation_required": risk_profile == "release" or bool(external_side_effects),
        },
        "privacy": {
            "credentials_included": False,
            "raw_transcript_included": False,
            "absolute_paths_included": False,
        },
    }
    return _prune(redact_sensitive_value(contract))


def select_risk_aware_sandbox_policy(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    explicit = _clean_text(source.get("risk_profile") or _object(source.get("runtime_policy")).get("risk_profile"), 32)
    actions = _clean_string_list(source.get("actions") or source.get("operations"), 64, 120)
    side_effects = _normalized_side_effects(source)
    inferred, reason = _infer_risk(explicit, actions + side_effects)
    requested_policy = _object(source.get("sandbox") or source.get("runtime_policy") or {})

    defaults = {
        "low": {"network_policy": "none", "filesystem_policy": "read_only"},
        "medium": {"network_policy": "none", "filesystem_policy": "candidate_workspace_only"},
        "high": {"network_policy": "adapter_scoped", "filesystem_policy": "candidate_workspace_only"},
        "release": {"network_policy": "adapter_scoped", "filesystem_policy": "candidate_workspace_only"},
    }[inferred]
    # A caller may make a policy more restrictive. It may not silently relax
    # the risk-selected filesystem or network boundary.
    requested_network = _policy_mode(requested_policy.get("network_policy") or requested_policy.get("network"))
    requested_filesystem = _policy_mode(requested_policy.get("filesystem_policy") or requested_policy.get("filesystem"))
    network = "none" if requested_network == "none" else defaults["network_policy"]
    filesystem = (
        "read_only"
        if requested_filesystem == "read_only"
        else defaults["filesystem_policy"]
    )
    if inferred == "low":
        network = "none"
        filesystem = "read_only"

    normalized = normalize_sandbox_policy({
        "risk_profile": inferred,
        "network_policy": network,
        "filesystem_policy": filesystem,
        "workspace_root": "",
        "budget": _object(source.get("budget")),
        "promotion": {
            "human_approval_required": True,
            "merge_release_signing_blocked": True,
        },
    })
    return {
        "schema_version": normalized["schema_version"],
        "risk_profile": inferred,
        "selection_reason": reason,
        "network_policy": normalized["network_policy"],
        "filesystem_policy": normalized["filesystem_policy"],
        "promotion": normalized["promotion"],
        "execution_mode": "read_only" if filesystem == "read_only" else "isolated_candidate",
        "external_side_effects_blocked": True,
    }


def build_run_comparison(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    baseline = _normalize_run_snapshot(_object(source.get("baseline") or source.get("before")), "baseline")
    candidate = _normalize_run_snapshot(_object(source.get("candidate") or source.get("after")), "candidate")
    checks = _compare_checks(baseline["checks"], candidate["checks"])
    comparison = {
        "schema_version": RUN_COMPARISON_SCHEMA,
        "baseline": baseline,
        "candidate": candidate,
        "changes": {
            "verdict": _change(baseline.get("verdict"), candidate.get("verdict")),
            "checks": checks,
            "evidence": _set_change(baseline.get("evidence_ids") or [], candidate.get("evidence_ids") or []),
            "code_revision": _change(baseline.get("code_revision"), candidate.get("code_revision")),
            "model_policy": _object_change(baseline.get("model_policy") or {}, candidate.get("model_policy") or {}),
            "budget": _budget_change(baseline.get("budget") or {}, candidate.get("budget") or {}),
        },
    }
    changed_sections = [
        key for key, value in comparison["changes"].items()
        if value.get("changed") is True
    ]
    comparison["summary"] = {
        "changed": bool(changed_sections),
        "changed_sections": changed_sections,
        "improved_checks": checks["improved"],
        "regressed_checks": checks["regressed"],
    }
    comparison["comparison_sha256"] = _sha256(comparison)
    return _prune(redact_sensitive_value(comparison))


def build_replay_plan(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    snapshot = _normalize_run_snapshot(_object(source.get("source") or source.get("run")), "source")
    source_sha256 = _sha256(snapshot)
    side_effects = _normalized_side_effects(source) or _clean_string_list(
        _object(source.get("source") or source.get("run")).get("external_side_effects"), 32, 120
    )
    approval = _object(source.get("renewed_approval") or source.get("approval_receipt"))
    approval_valid = _valid_renewed_approval(approval, source_sha256)
    blocked_reasons = []
    if side_effects and not approval_valid:
        blocked_reasons.append("external side effects require a new verified approval bound to this replay snapshot")
    plan = {
        "schema_version": REPLAY_PLAN_SCHEMA,
        "status": "blocked" if blocked_reasons else "ready",
        "mode": "simulation" if not side_effects else "approved_side_effect_plan",
        "source_snapshot": snapshot,
        "source_snapshot_sha256": source_sha256,
        "external_side_effects": side_effects,
        "renewed_approval": {
            "required": bool(side_effects),
            "verified": approval_valid,
            "receipt_id": _clean_text(approval.get("receipt_id"), 160) if approval_valid else None,
        },
        "execution": {
            "performed": False,
            "automatic_execution_allowed": False,
            "side_effects_repeated": False,
        },
        "blocked_reasons": blocked_reasons,
        "next_action": (
            "request_new_approval" if blocked_reasons else
            "review_and_start_explicitly" if side_effects else
            "review_simulation"
        ),
        "privacy": {
            "credentials_included": False,
            "raw_transcript_included": False,
            "absolute_paths_included": False,
        },
    }
    plan["plan_sha256"] = _sha256(plan)
    return _prune(redact_sensitive_value(plan))


def _normalize_run_snapshot(value: dict[str, Any], fallback_id: str) -> dict[str, Any]:
    policy = build_execution_policy_contract({
        "run_id": value.get("run_id") or value.get("task_id") or value.get("loop_id") or fallback_id,
        "role": value.get("role"),
        "model_policy": value.get("model_policy") or value.get("model"),
        "budget": value.get("budget"),
        "risk_profile": value.get("risk_profile"),
        "actions": value.get("actions"),
        "external_side_effects": value.get("external_side_effects"),
    })
    return _prune({
        "run_id": policy.get("run_id") or fallback_id,
        "status": _clean_text(value.get("status"), 48) or "unknown",
        "verdict": _clean_text(value.get("verdict") or _object(value.get("quality")).get("status"), 48) or "unknown",
        "checks": _normalize_checks(value.get("checks") or _object(value.get("quality")).get("checks")),
        "evidence_ids": _evidence_ids(value),
        "code_revision": _clean_text(value.get("code_revision") or value.get("commit_sha") or value.get("revision"), 160),
        "model_policy": policy.get("model_policy"),
        "budget": _public_budget(value.get("budget") or {}),
        "risk": policy.get("risk"),
    })


def _normalize_checks(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {
            _clean_text(key, 120): _check_status(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if _clean_text(key, 120)
        }
    if isinstance(value, list):
        result = {}
        for index, item in enumerate(value):
            entry = _object(item)
            check_id = _clean_text(entry.get("id") or entry.get("name") or f"check-{index + 1}", 120)
            if check_id:
                result[check_id] = _check_status(entry)
        return result
    return {}


def _check_status(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("status") or value.get("state") or value.get("verdict")
    if value is True:
        return "passed"
    if value is False:
        return "failed"
    status = _clean_text(value, 48).lower()
    return status or "unknown"


def _compare_checks(baseline: dict[str, str], candidate: dict[str, str]) -> dict[str, Any]:
    rows = []
    improved = []
    regressed = []
    for check_id in sorted(set(baseline) | set(candidate)):
        before = baseline.get(check_id, "missing")
        after = candidate.get(check_id, "missing")
        classification = "unchanged"
        if before != after:
            if after in {"passed", "ready", "completed"}:
                classification = "improved"
                improved.append(check_id)
            elif before in {"passed", "ready", "completed"}:
                classification = "regressed"
                regressed.append(check_id)
            elif before == "missing":
                classification = "introduced"
            elif after == "missing":
                classification = "removed"
            else:
                classification = "changed"
        rows.append({"id": check_id, "before": before, "after": after, "classification": classification})
    return {
        "changed": any(row["classification"] != "unchanged" for row in rows),
        "items": rows,
        "improved": improved,
        "regressed": regressed,
    }


def _evidence_ids(value: dict[str, Any]) -> list[str]:
    raw = value.get("evidence_ids") or value.get("evidence") or value.get("evidence_refs") or []
    if isinstance(raw, dict):
        raw = list(raw.keys())
    result = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            item = item.get("sha256") or item.get("evidence_sha256") or item.get("id") or item.get("ref")
        clean = _clean_text(item, 200)
        if clean and not clean.startswith("/"):
            result.append(clean)
    return sorted(set(result))[:128]


def _public_budget(value: Any) -> dict[str, Any]:
    budget = _object(value)
    result = {}
    for key in (
        "max_model_calls", "model_calls", "max_candidate_repairs", "candidate_repairs",
        "max_usd", "usd_used", "max_runtime_seconds", "runtime_seconds",
    ):
        raw = budget.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            result[key] = raw
    return result


def _budget_change(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(set(before) | set(after))
    items = []
    for key in keys:
        left, right = before.get(key), after.get(key)
        delta = right - left if isinstance(left, (int, float)) and isinstance(right, (int, float)) else None
        items.append({"id": key, "before": left, "after": right, "delta": delta})
    return {"changed": before != after, "items": items}


def _change(before: Any, after: Any) -> dict[str, Any]:
    return {"changed": before != after, "before": before, "after": after}


def _object_change(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        "changed": _canonical(before) != _canonical(after),
        "before": before,
        "after": after,
    }


def _set_change(before: list[str], after: list[str]) -> dict[str, Any]:
    left, right = set(before), set(after)
    return {
        "changed": left != right,
        "added": sorted(right - left),
        "removed": sorted(left - right),
        "retained": sorted(left & right),
    }


def _valid_renewed_approval(receipt: dict[str, Any], source_sha256: str) -> bool:
    if not receipt:
        return False
    integrity = receipt.get("integrity_status") or receipt.get("verification") or receipt.get("integrity")
    proposer = _clean_text(receipt.get("proposer_id") or receipt.get("proposer"), 160)
    approver = _clean_text(receipt.get("approver_id") or receipt.get("approver"), 160)
    subject = _clean_text(receipt.get("subject_sha256") or receipt.get("target_sha256"), 80)
    decision = _clean_text(receipt.get("decision") or receipt.get("status"), 40).lower()
    scope = _clean_text(receipt.get("scope") or receipt.get("action"), 120).lower()
    return (
        integrity in {True, "verified", "valid"}
        and decision in {"approved", "approve"}
        and bool(proposer and approver and proposer != approver)
        and subject == source_sha256
        and any(token in scope for token in ("replay", "external_side_effect", "run"))
    )


def _normalized_side_effects(source: dict[str, Any]) -> list[str]:
    raw = source.get("external_side_effects") or source.get("side_effects") or []
    return _clean_string_list(raw, 32, 120)


def _infer_risk(explicit: str | None, actions: list[str]) -> tuple[str, str]:
    explicit_value = (explicit or "").lower()
    if explicit_value == "critical":
        explicit_value = "release"
    words = set()
    for action in actions:
        words.update(re.findall(r"[a-z0-9]+", action.lower()))
    if words & _RELEASE_ACTIONS:
        inferred, reason = "release", "release or production side effect detected"
    elif words & _NETWORK_ACTIONS:
        inferred, reason = "high", "external or network side effect detected"
    elif words & _WRITE_ACTIONS:
        inferred, reason = "medium", "workspace mutation detected"
    else:
        inferred, reason = "low", "read-only or no side effects detected"
    if explicit_value not in _RISK_ORDER:
        return inferred, reason
    if _RISK_ORDER[explicit_value] >= _RISK_ORDER[inferred]:
        return explicit_value, "explicit risk profile"
    return inferred, f"risk raised above requested {explicit_value}: {reason}"


def _policy_mode(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("mode")
    return _clean_text(value, 64).lower()


def _clean_string_list(value: Any, limit: int, item_limit: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value:
        clean = _clean_text(item, item_limit)
        if clean and clean not in result:
            result.append(clean)
        if len(result) >= limit:
            break
    return result


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if not text or "\x00" in text:
        return ""
    if text.startswith("/") or _ABSOLUTE_PATH_RE.search(text):
        return ""
    if re.search(r"(api[_-]?key|token|secret|password|credential)\s*[:=]", text, re.I):
        return "[REDACTED]"
    return text[:limit]


def _object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _bounded_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _bounded_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        return max(minimum, min(float(value), maximum))
    except (TypeError, ValueError):
        return default


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _prune(value: Any) -> Any:
    if isinstance(value, list):
        return [_prune(item) for item in value]
    if isinstance(value, dict):
        return {key: _prune(item) for key, item in value.items() if item is not None and item != ""}
    return value
