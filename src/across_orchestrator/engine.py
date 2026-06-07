from __future__ import annotations

import time
from typing import Any

from across_agents_assistant.task_manager.models import TaskStatus
from across_agents_assistant.task_manager.orchestration.orchestrator import TaskOrchestrator
from across_agents_assistant.task_manager.state import TaskState

from .host_adapters import DispatcherAdapter, OwnerAgentAdapter, ValidatorAdapter


class MatureOrchestrationEngine:
    """Public wrapper around the transplanted Across Agents Assistant engine.

    Hosts provide agent execution, validation, and owner-agent adapters. The
    extracted engine owns task state, wave governance, contracts, acceptance,
    repair loops, and quality remediation.
    """

    def __init__(
        self,
        *,
        dispatcher: DispatcherAdapter,
        validator: ValidatorAdapter,
        owner_agent: OwnerAgentAdapter,
        state: TaskState | None = None,
    ) -> None:
        self.state = state or TaskState()
        self.dispatcher = dispatcher
        self.validator = validator
        self.owner_agent = owner_agent
        self.orchestrator = TaskOrchestrator(
            self.state,
            dispatcher=dispatcher,
            validator=validator,
            owner_agent=owner_agent,
        )

    def submit_task(
        self,
        goal: str,
        *,
        context: dict[str, Any] | None = None,
        wait_for_decomposition: bool = True,
        timeout: float = 5.0,
    ) -> str:
        task_id = self.orchestrator.submit_task(goal, context=context)
        if wait_for_decomposition:
            self.wait_for_decomposition(task_id, timeout=timeout)
        return task_id

    def wait_for_decomposition(self, task_id: str, *, timeout: float = 5.0) -> None:
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            task = self.state.get_task(task_id)
            if task is None:
                return
            if task.status != TaskStatus.DECOMPOSING:
                return
            if any(not st.subtask_id.endswith("-decompose") for st in task.subtasks):
                return
            time.sleep(0.01)

    def resume_task(self, task: Any) -> None:
        self.orchestrator.resume_task(task)
