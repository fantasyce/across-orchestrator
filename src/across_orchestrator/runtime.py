from __future__ import annotations

from pathlib import Path
from typing import Any

from .app_grade import APP_GRADE_RELEASE_E2E_ENGINE, build_release_e2e_payload, run_release_e2e_payload
from .adapters import adapter_for
from .agent_loop import AgentLoopRuntime
from .evidence import build_evidence_bundle, build_quality
from .models import SubTask, Task
from .store import LocalStore


class OrchestratorRuntime:
    def __init__(self, store: LocalStore | None = None):
        self.store = store or LocalStore()
        self.loop_runtime = AgentLoopRuntime(self.store)

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
        loop = self.loop_runtime.start_loop(
            goal=task.goal,
            project_root=str(root),
            agent=agent,
            metadata={"task_id": task.task_id, "task_kind": "delivery"},
        )
        task.metadata["agent_loop"] = {
            "loop_id": loop.loop_id,
            "runtime": "across-orchestrator",
            "mode": "durable",
        }
        self.store.save_task(task)
        self.store.append_event(task.task_id, "task.created", {"goal": task.goal, "agent": task.agent})
        self.store.append_event(task.task_id, "contract.created", task.contract)
        self.store.append_event(task.task_id, "agent_loop.created", task.metadata["agent_loop"])
        for subtask in task.subtasks:
            self.store.append_event(task.task_id, "subtask.created", subtask.__dict__)
        return task

    def submit_release_e2e_task(
        self,
        project_root: str,
        run_label: str | None = None,
    ) -> Task:
        root = Path(project_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        task = Task.new(
            goal="Run Across Agents Assistant release E2E parity scenario",
            project_root=str(root),
            deliverables=["README.md"],
            agent="app-grade",
        )
        payload = build_release_e2e_payload(
            task_id=task.task_id,
            project_root=str(root),
            run_label=run_label,
        )
        task.goal = payload["request"]["description"]
        task.contract = {
            "contractVersion": "0.4-app-grade",
            "engine": APP_GRADE_RELEASE_E2E_ENGINE,
            "scenarioId": payload["scenario_id"],
            "requiredArtifacts": list(payload["request"]["required_files"]),
            "qualityGates": list(payload["request"]["required_quality_gates"]),
            "requiredAgentMix": dict(payload["request"]["required_agent_mix"]),
        }
        task.subtasks = [
            SubTask.new(
                goal=item["description"],
                path=(item.get("deliverables") or [{}])[0].get("path_hint") or item["id"],
                agent=item["agent"],
            )
            for item in payload["subtasks"]
        ]
        task.metadata["app_grade_request"] = payload
        loop = self.loop_runtime.start_loop(
            goal=task.goal,
            project_root=str(root),
            agent=task.agent,
            metadata={"task_id": task.task_id, "task_kind": "release_e2e"},
        )
        task.metadata["agent_loop"] = {
            "loop_id": loop.loop_id,
            "runtime": "across-orchestrator",
            "mode": "durable",
        }
        self.store.save_task(task)
        self.store.append_event(task.task_id, "task.created", {"goal": task.goal, "agent": task.agent})
        self.store.append_event(task.task_id, "contract.created", task.contract)
        self.store.append_event(task.task_id, "agent_loop.created", task.metadata["agent_loop"])
        self.store.append_event(task.task_id, "app_grade.release_e2e.created", {
            "scenario_id": payload["scenario_id"],
            "required_files": task.contract["requiredArtifacts"],
        })
        for subtask in task.subtasks:
            self.store.append_event(task.task_id, "subtask.created", subtask.__dict__)
        return task

    def get_task(self, task_id: str) -> Task:
        return self.store.load_task(task_id)

    def list_events(self, task_id: str) -> list[dict]:
        return self.store.list_events(task_id)

    def run_task(self, task_id: str, command: list[str] | None = None) -> Task:
        task = self.store.load_task(task_id)
        if task.contract.get("engine") == APP_GRADE_RELEASE_E2E_ENGINE:
            return self._run_app_grade_release_e2e(task)
        task.status = "running"
        self.store.save_task(task)
        self.store.append_event(task.task_id, "task.started", {"agent": task.agent})
        self._run_task_loop(task)
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
        bundle = build_evidence_bundle(task, self.store.list_events(task_id))
        loop_summary = self._agent_loop_summary(task)
        if loop_summary:
            bundle["agent_loop"] = loop_summary
        return bundle

    def quality_benchmark(self, task_id: str) -> dict:
        task = self.store.load_task(task_id)
        return build_quality(task)

    def _run_app_grade_release_e2e(self, task: Task) -> Task:
        task.status = "running"
        self.store.save_task(task)
        self.store.append_event(task.task_id, "task.started", {"agent": task.agent})
        self._run_task_loop(task)
        payload = task.metadata.get("app_grade_request")
        if not payload:
            payload = build_release_e2e_payload(
                task_id=task.task_id,
                project_root=task.project_root,
            )
            task.metadata["app_grade_request"] = payload
        result = run_release_e2e_payload(
            task_id=task.task_id,
            project_root=task.project_root,
            payload=payload,
        )
        for subtask in task.subtasks:
            subtask.status = "completed"
            subtask.error = None
        task.metadata["app_grade"] = result
        task.status = "completed"
        self.store.save_task(task)
        self.store.append_event(task.task_id, "app_grade.release_e2e.completed", {
            "delivery_quality": result["delivery_quality"],
            "exact_files": result["exact_files"],
        })
        self.store.append_event(task.task_id, "task.completed", result["quality_report"])
        return task

    def _run_task_loop(self, task: Task) -> None:
        loop_id = str((task.metadata.get("agent_loop") or {}).get("loop_id") or "")
        if not loop_id:
            return
        loop = self.loop_runtime.run_loop(loop_id)
        self.store.append_event(task.task_id, "agent_loop.completed", {
            "loop_id": loop.loop_id,
            "status": loop.status,
            "turn_count": loop.turn_count,
            "checkpoint_count": loop.checkpoint_count,
        })
        task.metadata["agent_loop"].update({
            "status": loop.status,
            "turn_count": loop.turn_count,
            "checkpoint_count": loop.checkpoint_count,
        })
        self.store.save_task(task)

    def _agent_loop_summary(self, task: Task) -> dict[str, Any] | None:
        loop_id = str((task.metadata.get("agent_loop") or {}).get("loop_id") or "")
        if not loop_id:
            return None
        try:
            loop = self.loop_runtime.get_loop(loop_id)
        except KeyError:
            return {
                "loop_id": loop_id,
                "status": "missing",
                "step_count": 0,
                "checkpoint_count": 0,
                "action_types": [],
            }
        return {
            "loop_id": loop.loop_id,
            "status": loop.status,
            "agent": loop.agent,
            "turn_count": loop.turn_count,
            "step_count": len(loop.steps),
            "checkpoint_count": loop.checkpoint_count,
            "action_types": [step.action.type for step in loop.steps],
            "memory_policy": loop.memory_policy,
            "approval_policy": loop.approval_policy,
            "final_output": loop.final_output,
            "events": self.loop_runtime.list_loop_events(loop.loop_id),
        }
