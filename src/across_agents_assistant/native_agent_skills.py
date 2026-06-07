from __future__ import annotations

from typing import Any, Dict


def is_native_skill_available(skill: Dict[str, Any]) -> bool:
    status = str(skill.get("status") or "").strip().lower()
    availability = str(skill.get("availability") or "").strip().lower()
    return status not in {"blocked", "disabled", "error", "failed", "missing", "not_ready", "unavailable"} and availability != "unavailable"

