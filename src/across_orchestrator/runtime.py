from __future__ import annotations

from pathlib import Path

from .adapters import adapter_for
from .evidence import build_evidence_bundle, build_quality
from .models import Task
from .store import LocalStore


class OrchestratorRuntime:
    def __init__(self, store: LocalStore | None = None):
        self.store = store or LocalStore()

    def submit_task(
        self,
        goal: str,
        project_root: str,
        deliverables: list[str] | None = None,
        agent: str = "demo",
    ) -> Task:
        if not goal or not goal.strip():
            raise ValueError("goal is required")
        root = Path(project_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        paths = list(deliverables or ["README.md"])
        task = Task.new(goal=goal.strip(), project_root=str(root), deliverables=paths, agent=agent)
        self.store.save_task(task)
        self.store.append_event(task.task_id, "task.created", {"goal": task.goal, "agent": task.agent})
        self.store.append_event(task.task_id, "contract.created", task.contract)
        for subtask in task.subtasks:
            self.store.append_event(task.task_id, "subtask.created", subtask.__dict__)
        return task

    def get_task(self, task_id: str) -> Task:
        return self.store.load_task(task_id)

    def list_events(self, task_id: str) -> list[dict]:
        return self.store.list_events(task_id)

    def run_task(self, task_id: str, command: list[str] | None = None) -> Task:
        task = self.store.load_task(task_id)
        task.status = "running"
        self.store.save_task(task)
        self.store.append_event(task.task_id, "task.started", {"agent": task.agent})
        for subtask in task.subtasks:
            if subtask.status == "completed":
                continue
            adapter = adapter_for(subtask.agent, command=command)
            subtask.status = "running"
            subtask.attempts += 1
            self.store.append_event(task.task_id, "subtask.started", subtask.__dict__)
            try:
                result = adapter.run(task, subtask)
                subtask.status = "completed"
                subtask.error = None
                self.store.append_event(task.task_id, "subtask.completed", {**subtask.__dict__, "result": result})
            except Exception as exc:
                subtask.status = "failed"
                subtask.error = str(exc)
                task.status = "failed"
                self.store.append_event(task.task_id, "subtask.failed", subtask.__dict__)
                self.store.save_task(task)
                self.store.append_event(task.task_id, "task.failed", {"error": str(exc)})
                return task
            self.store.save_task(task)

        quality = build_quality(task)
        task.status = "completed" if quality["status"] == "passed" else "failed"
        self.store.save_task(task)
        self.store.append_event(
            task.task_id,
            "task.completed" if task.status == "completed" else "task.failed",
            quality,
        )
        return task

    def evidence_bundle(self, task_id: str) -> dict:
        task = self.store.load_task(task_id)
        return build_evidence_bundle(task, self.store.list_events(task_id))

    def quality_benchmark(self, task_id: str) -> dict:
        task = self.store.load_task(task_id)
        return build_quality(task)
