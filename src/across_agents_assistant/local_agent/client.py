from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..paths import app_subdir


def default_local_agent_workspace() -> Path:
    return app_subdir("workspace")


@dataclass
class LocalAgentReply:
    text: str
    session_id: Optional[str] = None
    elapsed_sec: float = 0.0
    requires_approval: bool = False
    approval_request: Optional[dict] = None


class UniversalAgentClient:
    """Host-provided local agent client placeholder.

    A real app host can pass an object with an ``invoke`` method into the
    dispatcher. Standalone tests intentionally avoid launching local CLIs.
    """

    def __init__(self, manager: Any = None):
        self.manager = manager

    def initialize(self) -> None:
        return None

    def cancel(self, session_id: str) -> bool:
        return False

