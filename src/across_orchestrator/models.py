from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any
import time
import uuid


LEGACY_TASK_STATUS_ALIASES = {
    "stopped": "failed",
}


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@dataclass
class SubTask:
    subtask_id: str
    goal: str
    path: str
    agent: str = "demo"
    capability_role: str | None = None
    status: str = "pending"
    wave: int = 1
    dependencies: list[str] = field(default_factory=list)
    priority: int = 1
    attempts: int = 0
    error: str | None = None

    @classmethod
    def new(
        cls,
        goal: str,
        path: str,
        agent: str = "demo",
        *,
        capability_role: str | None = None,
        wave: int = 1,
        dependencies: list[str] | None = None,
        priority: int = 1,
    ) -> "SubTask":
        return cls(
            subtask_id=new_id("subtask"),
            goal=goal,
            path=path,
            agent=agent,
            capability_role=capability_role,
            wave=max(1, int(wave or 1)),
            dependencies=list(dependencies or []),
            priority=max(1, int(priority or 1)),
        )


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

    @classmethod
    def from_plan(
        cls,
        *,
        goal: str,
        project_root: str,
        subtasks: list[dict[str, Any]],
        deliverables: list[str] | None = None,
        agent: str = "demo",
    ) -> "Task":
        resolved_root = str(Path(project_root).expanduser().resolve())
        planned: list[SubTask] = []
        required: list[str] = []
        id_by_spec_id: dict[str, str] = {}

        for index, spec in enumerate(subtasks, start=1):
            path = _path_from_subtask_spec(spec)
            required.append(path)
            spec_id = str(spec.get("id") or spec.get("subtask_id") or f"stage-{index}")
            subtask = SubTask.new(
                goal=str(spec.get("goal") or spec.get("description") or f"Produce {path} for: {goal}"),
                path=path,
                agent=str(spec.get("agent") or agent or "demo"),
                capability_role=str(spec.get("capability_role") or spec.get("role") or "") or None,
                wave=int(spec.get("wave") or spec.get("wave_number") or index),
                dependencies=[str(item) for item in spec.get("dependencies") or []],
                priority=int(spec.get("priority") or index),
            )
            planned.append(subtask)
            id_by_spec_id[spec_id] = subtask.subtask_id

        for subtask in planned:
            subtask.dependencies = [
                id_by_spec_id.get(dep, dep)
                for dep in subtask.dependencies
            ]

        for path in deliverables or []:
            required.append(normalize_artifact_path(path))

        clean_required = list(dict.fromkeys(required))
        task = cls(
            task_id=new_id("task"),
            goal=goal,
            project_root=resolved_root,
            agent=agent,
            subtasks=planned,
        )
        task.contract = {
            "contractVersion": "0.2-plan",
            "goal": goal,
            "requiredArtifacts": clean_required,
            "qualityGates": ["required_artifacts_present", "no_artifacts_outside_project", "serial_wave_dependencies"],
            "serialPlan": True,
        }
        return task

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        subtask_fields = {item.name for item in fields(SubTask)}
        subtasks = []
        for raw in data.get("subtasks", []):
            item = dict(raw)
            if "wave_number" in item and "wave" not in item:
                item["wave"] = item.pop("wave_number")
            item.setdefault("dependencies", [])
            item.setdefault("priority", 1)
            subtasks.append(SubTask(**{key: value for key, value in item.items() if key in subtask_fields}))
        status = str(data.get("status", "pending") or "pending")
        task = cls(
            task_id=data["task_id"],
            goal=data["goal"],
            project_root=data["project_root"],
            status=LEGACY_TASK_STATUS_ALIASES.get(status, status),
            agent=data.get("agent", "demo"),
            subtasks=subtasks,
            contract=data.get("contract", {}),
            metadata=data.get("metadata", {}),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )
        _migrate_legacy_app_grade_agents(task)
        return task


def normalize_artifact_path(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not value:
        raise ValueError("deliverable path cannot be empty")
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ValueError(f"deliverable path must stay inside project root: {path}")
    return "/".join(parts)


def _path_from_subtask_spec(spec: dict[str, Any]) -> str:
    path = spec.get("path") or spec.get("output_file") or spec.get("path_hint")
    if not path:
        for deliverable in spec.get("deliverables") or []:
            path = deliverable.get("path_hint") or deliverable.get("path")
            if path:
                break
    return normalize_artifact_path(str(path or "README.md"))


_APP_GRADE_ENGINE = "app_grade_release_e2e"
_DEFAULT_APP_GRADE_EXECUTORS = ["openclaw", "hermes", "claude", "deepseek", "minimax"]


def _migrate_legacy_app_grade_agents(task: Task) -> None:
    if task.contract.get("engine") != _APP_GRADE_ENGINE:
        return
    executors = _legacy_app_grade_executors(task)
    if task.agent == "app-grade" or task.agent.endswith("-agent"):
        task.agent = executors[0]
    for index, subtask in enumerate(task.subtasks):
        role = _legacy_role_from_agent(subtask.agent)
        if role:
            subtask.capability_role = subtask.capability_role or role
            subtask.agent = executors[index % len(executors)]
    _migrate_legacy_app_grade_payload(task.metadata.get("app_grade_request"), executors)
    _migrate_legacy_app_grade_payload(task.metadata.get("app_grade"), executors, agent_key="agent_id")


def _legacy_app_grade_executors(task: Task) -> list[str]:
    candidates: list[Any] = []
    request = task.metadata.get("app_grade_request")
    if isinstance(request, dict):
        request_body = request.get("request")
        if isinstance(request_body, dict):
            candidates.extend(request_body.get("executor_agents") or [])
    candidates.extend(task.contract.get("executor_agents") or [])
    candidates.extend(subtask.agent for subtask in task.subtasks)
    cleaned: list[str] = []
    for item in candidates:
        value = str(item or "").strip().lower()
        if value and value not in cleaned and not value.endswith("-agent") and value != "app-grade":
            cleaned.append(value)
    return cleaned or list(_DEFAULT_APP_GRADE_EXECUTORS)


def _legacy_role_from_agent(agent: str) -> str | None:
    value = str(agent or "").strip().lower()
    if value.endswith("-agent"):
        return value.removesuffix("-agent")
    return None


def _migrate_legacy_app_grade_payload(payload: Any, executors: list[str], *, agent_key: str = "agent") -> None:
    if not isinstance(payload, dict):
        return
    request = payload.get("request")
    if isinstance(request, dict):
        request["executor_agents"] = executors
    for index, item in enumerate(payload.get("subtasks") or []):
        if not isinstance(item, dict):
            continue
        role = _legacy_role_from_agent(str(item.get(agent_key) or item.get("agent") or ""))
        if role:
            item.setdefault("capability_role", role)
            item[agent_key] = executors[index % len(executors)]
