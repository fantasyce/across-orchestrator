from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .host_adapters import DispatcherAdapter, OwnerAgentAdapter, ValidatorAdapter
from .runtime import OrchestratorRuntime


@dataclass
class HostPlanningSubtask:
    subtask_id: str
    description: str
    agent_id: str = "demo"
    path: str | None = None
    dependencies: list[str] = field(default_factory=list)
    wave: int = 1
    priority: int = 1


@dataclass
class HostPlanningTask:
    task_id: str
    goal: str
    project_root: str
    subtasks: list[Any] = field(default_factory=list)


class RuntimeStateView:
    """Small compatibility view for hosts that need task lookup by id."""

    def __init__(self, runtime: OrchestratorRuntime):
        self._runtime = runtime

    def get_task(self, task_id: str) -> Any | None:
        try:
            return self._runtime.get_task(task_id)
        except KeyError:
            return None


class MatureOrchestrationEngine:
    """Host-neutral wrapper around the standalone Across Orchestrator runtime.

    Hosts provide agent execution, validation, and optional planning adapters.
    Across Orchestrator owns serial plans, task state, contracts, events, and
    evidence without importing host application internals.
    """

    def __init__(
        self,
        *,
        dispatcher: DispatcherAdapter,
        validator: ValidatorAdapter,
        owner_agent: OwnerAgentAdapter,
        runtime: OrchestratorRuntime | None = None,
    ) -> None:
        self.runtime = runtime or OrchestratorRuntime()
        self.state = RuntimeStateView(self.runtime)
        self.dispatcher = dispatcher
        self.validator = validator
        self.owner_agent = owner_agent

    def submit_task(
        self,
        goal: str,
        *,
        context: dict[str, Any] | None = None,
        wait_for_decomposition: bool = True,
        timeout: float = 5.0,
    ) -> str:
        context = context or {}
        project_root = str(context.get("project_root") or context.get("project_dir") or ".")
        deliverables = [str(item) for item in context.get("deliverables") or ["README.md"]]
        agent = str(context.get("agent") or _first_agent(context) or "demo")
        planning_task = HostPlanningTask(
            task_id="host-plan",
            goal=goal,
            project_root=project_root,
        )
        if hasattr(self.owner_agent, "decompose_and_assign"):
            self.owner_agent.decompose_and_assign(planning_task, context=context)
        planned_subtasks = _subtasks_from_context(context, planning_task, deliverables, agent)
        task = self.runtime.submit_task(
            goal=goal,
            project_root=project_root,
            deliverables=deliverables,
            agent=agent,
            subtasks=planned_subtasks,
            strict_dependency=bool(context.get("strict_dependency", True)),
            task_types=context.get("task_types") or None,
        )
        return task.task_id

    def wait_for_decomposition(self, task_id: str, *, timeout: float = 5.0) -> None:
        self.state.get_task(task_id)

    def resume_task(self, task: Any) -> None:
        task_id = str(getattr(task, "task_id", task))
        self.runtime.run_task(task_id)


def _first_agent(context: dict[str, Any]) -> str | None:
    for key in ("allowed_subtask_agents", "agents"):
        values = context.get(key) or []
        for value in values:
            agent = str(value or "").strip()
            if agent:
                return agent
    return None


def _subtasks_from_context(
    context: dict[str, Any],
    planning_task: HostPlanningTask,
    deliverables: list[str],
    agent: str,
) -> list[dict[str, Any]]:
    explicit = context.get("subtasks")
    raw_items = explicit if explicit is not None else planning_task.subtasks
    subtasks: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items or [], start=1):
        spec = _subtask_spec(item, index, deliverables, agent)
        if spec:
            subtasks.append(spec)
    return subtasks


def _subtask_spec(item: Any, index: int, deliverables: list[str], default_agent: str) -> dict[str, Any] | None:
    if isinstance(item, dict):
        get = item.get
    else:
        get = lambda key, default=None: getattr(item, key, default)
    path = get("path") or get("path_hint") or get("output_file")
    if not path and index <= len(deliverables):
        path = deliverables[index - 1]
    if not path:
        return None
    subtask_id = str(get("id") or get("subtask_id") or f"stage-{index}")
    description = str(get("description") or get("goal") or f"Produce {path}.")
    return {
        "id": subtask_id,
        "description": description,
        "path": str(path),
        "agent": str(get("agent") or get("agent_id") or default_agent),
        "wave": int(get("wave") or get("wave_number") or index),
        "priority": int(get("priority") or index),
        "dependencies": [str(dep) for dep in get("dependencies", []) or []],
    }
