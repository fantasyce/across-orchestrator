from __future__ import annotations

from pathlib import Path
from typing import Any

SANDBOX_POLICY_SCHEMA = "across-sandbox-policy/1.0"
SANDBOX_EVIDENCE_SCHEMA = "across-sandbox-evidence/1.0"

VALID_NETWORK_MODES = {"none", "adapter_scoped", "allowlist", "unrestricted_requires_approval"}
VALID_FILESYSTEM_MODES = {"read_only", "run_scoped", "candidate_workspace_only", "allowlist"}


def evaluate_sandbox_policy(
    policy: dict[str, Any] | None,
    *,
    command: list[str] | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Evaluate a sandbox policy without executing the command."""

    normalized = normalize_sandbox_policy(policy or {})
    checks: list[dict[str, Any]] = []
    blocked_reasons: list[str] = []

    network_mode = normalized["network_policy"]["mode"]
    filesystem_mode = normalized["filesystem_policy"]["mode"]
    if network_mode not in VALID_NETWORK_MODES:
        blocked_reasons.append(f"unsupported network policy: {network_mode}")
    if filesystem_mode not in VALID_FILESYSTEM_MODES:
        blocked_reasons.append(f"unsupported filesystem policy: {filesystem_mode}")

    checks.append(_check("network_policy", network_mode in VALID_NETWORK_MODES, {"mode": network_mode}))
    checks.append(_check("filesystem_policy", filesystem_mode in VALID_FILESYSTEM_MODES, {"mode": filesystem_mode}))
    checks.append(_check("promotion_block", normalized["promotion"]["merge_release_signing_blocked"] is True))
    checks.append(_check("human_approval", normalized["promotion"]["human_approval_required"] is True))

    model_budget = int(normalized["budget"]["max_model_calls"])
    checks.append(_check("model_budget", model_budget >= 0 and model_budget <= 100, {"max_model_calls": model_budget}))

    if command is not None:
        command_result = _evaluate_command(normalized, command, cwd)
        checks.extend(command_result["checks"])
        blocked_reasons.extend(command_result["blocked_reasons"])

    for check in checks:
        if check["status"] == "blocked" and check["reason"] not in blocked_reasons:
            blocked_reasons.append(check["reason"])

    return {
        "schema_version": SANDBOX_EVIDENCE_SCHEMA,
        "policy_schema_version": normalized["schema_version"],
        "status": "blocked" if blocked_reasons else "passed",
        "policy": normalized,
        "command": command or None,
        "cwd": cwd or None,
        "checks": checks,
        "blocked_reasons": blocked_reasons,
        "execution": {
            "performed": False,
            "reason": "sandbox-probe validates policy and command boundaries only",
        },
    }


def normalize_sandbox_policy(policy: dict[str, Any]) -> dict[str, Any]:
    runtime_policy = dict(policy.get("runtime_policy") or {})
    source = {**runtime_policy, **policy}
    network = _policy_object(source.get("network_policy"), source.get("network") or "none")
    filesystem = _policy_object(source.get("filesystem_policy"), source.get("filesystem") or "read_only")
    budget = dict(source.get("budget") or {})
    promotion = dict(source.get("promotion") or {})
    return {
        "schema_version": SANDBOX_POLICY_SCHEMA,
        "risk_profile": str(source.get("risk_profile") or "low"),
        "network_policy": {
            "mode": str(network.get("mode") or "none"),
            "allowlist": [str(item) for item in network.get("allowlist") or []],
        },
        "filesystem_policy": {
            "mode": str(filesystem.get("mode") or "read_only"),
            "allowlist_roots": [str(item) for item in filesystem.get("allowlist_roots") or filesystem.get("allowlist") or []],
        },
        "budget": {
            "max_model_calls": int(budget.get("max_model_calls") or 0),
            "max_candidate_repairs": int(budget.get("max_candidate_repairs") or 0),
            "max_usd": float(budget.get("max_usd") or 0),
        },
        "promotion": {
            "human_approval_required": promotion.get("human_approval_required") is not False,
            "merge_release_signing_blocked": promotion.get("merge_release_signing_blocked") is not False,
        },
        "workspace_root": str(source.get("workspace_root") or source.get("workspace") or ""),
        "command_allowlist": [_command_key(item) for item in source.get("command_allowlist") or []],
    }


def _evaluate_command(policy: dict[str, Any], command: list[str], cwd: str | None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    blocked_reasons: list[str] = []
    clean_command = [str(item) for item in command if str(item).strip()]
    if not clean_command:
        reason = "command must be a non-empty argv array"
        return {"checks": [_check("command_shape", False, reason=reason)], "blocked_reasons": [reason]}

    allowlist = set(policy.get("command_allowlist") or [])
    command_key = _command_key(clean_command)
    command_allowed = bool(allowlist) and (command_key in allowlist or clean_command[0] in allowlist)
    if not command_allowed:
        reason = f"command is not allowlisted: {command_key}"
        blocked_reasons.append(reason)
    checks.append(_check("command_allowlist", command_allowed, {"command": command_key}, reason=None if command_allowed else reason))

    workspace_root = str(policy.get("workspace_root") or "")
    if cwd and workspace_root:
        cwd_path = Path(cwd).expanduser().resolve()
        root_path = Path(workspace_root).expanduser().resolve()
        inside = cwd_path == root_path or root_path in cwd_path.parents
        if not inside:
            reason = "cwd must stay inside workspace_root"
            blocked_reasons.append(reason)
        checks.append(_check("cwd_boundary", inside, {"cwd": str(cwd_path), "workspace_root": str(root_path)}, reason=None if inside else reason))
    elif cwd and not workspace_root:
        reason = "workspace_root is required when cwd is supplied"
        blocked_reasons.append(reason)
        checks.append(_check("cwd_boundary", False, reason=reason))

    return {"checks": checks, "blocked_reasons": blocked_reasons}


def _policy_object(value: Any, fallback_mode: str) -> dict[str, Any]:
    if isinstance(value, str):
        return {"mode": value}
    if isinstance(value, dict):
        return {**value, "mode": value.get("mode") or fallback_mode}
    return {"mode": fallback_mode}


def _command_key(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value if str(item).strip())
    return str(value)


def _check(check_id: str, passed: bool, details: dict[str, Any] | None = None, *, reason: str | None = None) -> dict[str, Any]:
    return {
        "id": check_id,
        "status": "passed" if passed else "blocked",
        "reason": reason or ("passed" if passed else check_id),
        "details": details or {},
    }
