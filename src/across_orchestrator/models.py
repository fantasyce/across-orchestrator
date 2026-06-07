from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
import time
import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@dataclass
class SubTask:
    subtask_id: str
    goal: str
    path: str
    agent: str = "demo"
    status: str = "pending"
    wave: int = 1
    attempts: int = 0
    error: str | None = None

    @classmethod
    def new(cls, goal: str, path: str, agent: str = "demo") -> "SubTask":
        return cls(subtask_id=new_id("subtask"), goal=goal, path=path, agent=agent)


@dataclass
class Task:
    task_id: str
    goal: str
    project_root: str
    status: str = "pending"
    agent: str = "demo"
    subtasks: list[SubTask] = field(default_factory=list)
    contract: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def new(
        cls,
        goal: str,
        project_root: str,
        deliverables: list[str],
        agent: str = "demo",
    ) -> "Task":
        resolved_root = str(Path(project_root).expanduser().resolve())
        clean_deliverables = [normalize_artifact_path(path) for path in deliverables]
        task = cls(
            task_id=new_id("task"),
            goal=goal,
            project_root=resolved_root,
            agent=agent,
            subtasks=[
                SubTask.new(goal=f"Produce {path} for: {goal}", path=path, agent=agent)
                for path in clean_deliverables
            ],
        )
        task.contract = {
            "contractVersion": "0.1",
            "goal": goal,
            "requiredArtifacts": clean_deliverables,
            "qualityGates": ["required_artifacts_present", "no_artifacts_outside_project"],
        }
        return task

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        subtasks = [SubTask(**item) for item in data.get("subtasks", [])]
        return cls(
            task_id=data["task_id"],
            goal=data["goal"],
            project_root=data["project_root"],
            status=data.get("status", "pending"),
            agent=data.get("agent", "demo"),
            subtasks=subtasks,
            contract=data.get("contract", {}),
            metadata=data.get("metadata", {}),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )


def normalize_artifact_path(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not value:
        raise ValueError("deliverable path cannot be empty")
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ValueError(f"deliverable path must stay inside project root: {path}")
    return "/".join(parts)
