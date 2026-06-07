from __future__ import annotations

from typing import List, Optional


class ToolPermissionStore:
    """In-memory permission store for standalone orchestration tests."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path
        self._permissions: dict[str, str] = {}

    def grant_always_allow(self, tool_name: str, session_id: Optional[str] = None) -> bool:
        self._permissions[tool_name] = "always_allow"
        return True

    def set_permission(self, tool_name: str, permission_type: str, session_id: Optional[str] = None) -> bool:
        normalized = permission_type.strip().lower()
        if normalized in {"ask", "ask_every_time", "ask_each_time"}:
            self._permissions.pop(tool_name, None)
            return True
        if normalized not in {"always_allow", "unavailable"}:
            raise ValueError(f"Unsupported permission type: {permission_type}")
        self._permissions[tool_name] = normalized
        return True

    def revoke_permission(self, tool_name: str) -> bool:
        return self._permissions.pop(tool_name, None) is not None

    def get_permission(self, tool_name: str) -> Optional[str]:
        return self._permissions.get(tool_name)

    def is_always_allowed(self, tool_name: str) -> bool:
        return self.get_permission(tool_name) == "always_allow"

    def is_unavailable(self, tool_name: str) -> bool:
        return self.get_permission(tool_name) == "unavailable"

    def list_always_allowed(self) -> List[str]:
        return [name for name, value in self._permissions.items() if value == "always_allow"]

    def list_permissions(self) -> List[dict]:
        return [
            {"tool_name": name, "permission_type": value, "granted_at": None, "granted_by": None}
            for name, value in sorted(self._permissions.items())
        ]

