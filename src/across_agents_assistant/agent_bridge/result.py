# backend/src/across_agents_assistant/agent_bridge/result.py
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Any

class ResultStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class SubtaskResult:
    """Result from a single subtask execution."""
    subtask_id: str
    agent_id: str
    status: ResultStatus
    output: Optional[str] = None
    error: Optional[str] = None
    elapsed_sec: Optional[float] = None

    @property
    def is_success(self) -> bool:
        return self.status == ResultStatus.COMPLETED

    @property
    def is_failure(self) -> bool:
        return self.status in (ResultStatus.FAILED, ResultStatus.CANCELLED)

@dataclass
class TaskResult:
    """Aggregated result from multiple subtasks."""
    task_id: str
    subtask_results: List[SubtaskResult] = field(default_factory=list)
    total_subtasks: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_subtask_result(self, result: SubtaskResult) -> None:
        """Add a subtask result."""
        self.subtask_results.append(result)

    @property
    def completed_count(self) -> int:
        return sum(1 for r in self.subtask_results if r.status == ResultStatus.COMPLETED)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.subtask_results if r.status == ResultStatus.FAILED)

    @property
    def has_failures(self) -> bool:
        return self.failed_count > 0

    @property
    def is_complete(self) -> bool:
        if self.total_subtasks > 0:
            return self.completed_count + self.failed_count >= self.total_subtasks
        return False

    @property
    def progress(self) -> float:
        if self.total_subtasks == 0:
            return 0.0
        return len(self.subtask_results) / self.total_subtasks

    def get_summary(self) -> str:
        """Get a human-readable summary of the results."""
        lines = [f"Task {self.task_id}: {self.completed_count}/{self.total_subtasks} completed"]
        for r in self.subtask_results:
            status_icon = "✅" if r.is_success else "❌"
            lines.append(f"  {status_icon} [{r.agent_id}] {r.subtask_id}: {r.output or r.error}")
        return "\n".join(lines)