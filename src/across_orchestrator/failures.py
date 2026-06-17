from __future__ import annotations

import subprocess
from typing import Any


FAILURE_TYPES = [
    "adapter_error",
    "approval_rejected",
    "environment_blocked",
    "lease_expired",
    "max_turns_exceeded",
    "quality_failed",
    "timeout",
]


def failure_type_for_reason(reason: str | None) -> str:
    value = str(reason or "").strip()
    if value == "action_lease_expired":
        return "lease_expired"
    if value == "quality_gate_failed":
        return "quality_failed"
    if value == "approval_rejected":
        return "approval_rejected"
    if value == "max_turns_exceeded":
        return "max_turns_exceeded"
    if value in FAILURE_TYPES:
        return value
    return "adapter_error"


def failure_type_for_exception(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    if bool(getattr(exc, "blocked_by_environment", False)):
        return "environment_blocked"
    return "adapter_error"


def failure_type_for_loop(loop: Any, fallback_reason: str | None = None) -> str:
    for step in reversed(list(getattr(loop, "steps", []) or [])):
        failure_type = _failure_type_from_step(step)
        if failure_type:
            return failure_type
    return failure_type_for_reason(fallback_reason if fallback_reason is not None else getattr(loop, "error", None))


def _failure_type_from_step(step: Any) -> str | None:
    observation = getattr(step, "observation", None)
    observation_payload = getattr(observation, "payload", {}) or {}
    failure_type = observation_payload.get("failure_type")
    if failure_type:
        return str(failure_type)
    checkpoint = getattr(step, "checkpoint", {}) or {}
    checkpoint_failure_type = checkpoint.get("failure_type")
    if checkpoint_failure_type:
        return str(checkpoint_failure_type)
    return None
