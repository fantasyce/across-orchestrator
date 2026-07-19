from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .app_grade import APP_GRADE_RELEASE_E2E_ENGINE, build_release_e2e_payload, run_release_e2e_payload
from .adapters import adapter_for, normalize_agent_adapter_specs
from .agent_loop import (
    AgentLoopAdapters,
    AgentLoopRuntime,
    DefaultFinalizer,
    DefaultQualityGate,
    HostLoopDispatcher,
    normalize_cancel_category,
)
from .cancellation import ActionCancelledError
from .evidence import build_evidence_bundle, build_quality
from .failures import failure_type_for_exception, failure_type_for_loop, failure_type_for_reason
from .findings import failed_gate_ids_from_findings, normalize_quality_report, quality_report_passed
from .models import SubTask, Task
from .planning import ensure_strict_dependency_chain
from .store import LocalStore


TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}


def _resolve_project_root(project_root: str) -> Path:
    text = str(project_root or "").strip()
    if not text or "\x00" in text:
        raise ValueError("project_root must be a non-empty path without null bytes")
    resolved = Path(os.path.realpath(os.path.expanduser(text)))
    allowed_roots = _allowed_project_roots()
    if allowed_roots and not any(resolved == allowed or allowed in resolved.parents for allowed in allowed_roots):
        raise ValueError("project_root is outside ACROSS_ORCHESTRATOR_ALLOWED_PROJECT_ROOTS")
    return resolved


def _allowed_project_roots() -> list[Path]:
    raw = os.environ.get("ACROSS_ORCHESTRATOR_ALLOWED_PROJECT_ROOTS", "")
    roots: list[Path] = []
    for item in raw.split(os.pathsep):
        text = item.strip()
        if not text:
            continue
        if "\x00" in text:
            raise ValueError("ACROSS_ORCHESTRATOR_ALLOWED_PROJECT_ROOTS contains a null byte")
        roots.append(Path(os.path.realpath(os.path.expanduser(text))))
    return roots


class OrchestratorRuntime:
    def __init__(self, store: LocalStore | None = None):
        self.store = store or LocalStore()
        self.loop_runtime = self._agent_loop_runtime()

    def _agent_loop_runtime(self, *, command: list[str] | None = None) -> AgentLoopRuntime:
        return AgentLoopRuntime(self.store, adapters=self._agent_loop_adapters(command=command))

    def _agent_loop_adapters(self, *, command: list[str] | None = None) -> AgentLoopAdapters:
        defaults = AgentLoopRuntime(self.store).adapters
        return AgentLoopAdapters(
            memory_provider=defaults.memory_provider,
            dispatcher=RuntimeLoopDispatcher(self, command=command),
            quality_gate=RuntimeQualityGate(self),
            finalizer=RuntimeFinalizer(self),
        )

    def submit_task(
        self,
        goal: str,
        project_root: str,
        deliverables: list[str] | None = None,
        agent: str = "demo",
        subtasks: list[dict[str, Any]] | None = None,
        strict_dependency: bool = False,
        task_types: list[str] | None = None,
        agent_adapters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        if not goal or not goal.strip():
            raise ValueError("goal is required")
        root = _resolve_project_root(project_root)
        root.mkdir(parents=True, exist_ok=True)
        paths = list(deliverables or ["README.md"])
        if subtasks:
            task = Task.from_plan(
                goal=goal.strip(),
                project_root=str(root),
                subtasks=subtasks,
                deliverables=paths,
                agent=agent,
            )
        else:
            task = Task.new(goal=goal.strip(), project_root=str(root), deliverables=paths, agent=agent)
        if strict_dependency and len(task.subtasks) > 1:
            ensure_strict_dependency_chain(task)
        clean_task_types = _clean_task_types(task_types)
        if clean_task_types:
            task.metadata["task_types"] = clean_task_types
            task.metadata["delivery_mode"] = _delivery_mode_for_task_types(clean_task_types)
        clean_agent_adapters = normalize_agent_adapter_specs(agent_adapters)
        if clean_agent_adapters:
            task.metadata["agent_adapters"] = clean_agent_adapters
        host_metadata = dict(metadata or {})
        if host_metadata:
            task.metadata["host_metadata"] = host_metadata
        execution_contract = host_metadata.get("execution_contract")
        remote_managed = (
            isinstance(execution_contract, dict)
            and str(execution_contract.get("route") or "").strip().lower() == "worker"
        )
        if remote_managed:
            # A Worker-routed task is a durable control-plane parent. The host
            # owns its Worker Job and projects that Job's status/evidence back
            # onto this task; starting the normal agent loop here would execute
            # the same user request a second time on the Coordinator.
            task.metadata["execution_mode"] = "remote_worker"
            task.metadata["agent_loop"] = {
                "runtime": "across-orchestrator",
                "mode": "remote-managed",
                "status": "queued",
            }
            self.store.save_task(task)
            self.store.append_event(task.task_id, "task.created", {"goal": task.goal, "agent": task.agent})
            self.store.append_event(task.task_id, "contract.created", task.contract)
            self.store.append_event(
                task.task_id,
                "task.remote_worker.created",
                {"execution_contract": execution_contract},
            )
            for subtask in task.subtasks:
                self.store.append_event(task.task_id, "subtask.created", subtask.__dict__)
            return task
        loop = self.loop_runtime.start_loop(
            goal=task.goal,
            project_root=str(root),
            agent=agent,
            metadata={"task_id": task.task_id, "task_kind": "delivery", "host_metadata": dict(metadata or {})},
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
        allowed_agents: list[str] | None = None,
    ) -> Task:
        root = _resolve_project_root(project_root)
        root.mkdir(parents=True, exist_ok=True)
        executor_agents = _clean_release_e2e_agents(allowed_agents)
        task = Task.new(
            goal="Run host agent full delivery conformance scenario",
            project_root=str(root),
            deliverables=["README.md"],
            agent=executor_agents[0],
        )
        payload = build_release_e2e_payload(
            task_id=task.task_id,
            project_root=str(root),
            run_label=run_label,
            allowed_agents=executor_agents,
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
        task.subtasks = []
        scenario_id_to_subtask_id: dict[str, str] = {}
        for item in payload["subtasks"]:
            subtask = SubTask.new(
                goal=item["description"],
                path=(item.get("deliverables") or [{}])[0].get("path_hint") or item["id"],
                agent=item["agent"],
                capability_role=item.get("capability_role"),
                wave=int(item.get("wave") or item.get("wave_number") or 1),
                dependencies=[str(dep) for dep in item.get("dependencies") or []],
                priority=int(item.get("priority") or 1),
            )
            task.subtasks.append(subtask)
            scenario_id_to_subtask_id[str(item.get("id") or subtask.path)] = subtask.subtask_id
        _resolve_subtask_dependency_ids(task.subtasks, scenario_id_to_subtask_id)
        task.metadata["app_grade_request"] = payload
        loop = self.loop_runtime.start_loop(
            goal=task.goal,
            project_root=str(root),
            agent=task.agent,
            metadata={"task_id": task.task_id, "task_kind": "host_conformance"},
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
        self.store.append_event(task.task_id, "app_grade.host_conformance.created", {
            "scenario_id": payload["scenario_id"],
            "required_files": task.contract["requiredArtifacts"],
        })
        for subtask in task.subtasks:
            self.store.append_event(task.task_id, "subtask.created", subtask.__dict__)
        return task

    def cancel_task(self, task_id: str, *, reason: str = "cancelled_by_user") -> Task:
        task = self.store.load_task(task_id)
        if task is None:
            raise ValueError("task not found")
        if task.status in TERMINAL_TASK_STATUSES:
            return task
        loop_id = str((task.metadata.get("agent_loop") or {}).get("loop_id") or "")
        if loop_id:
            self.loop_runtime.cancel_loop(loop_id, reason=reason, cancel_category="user")
        task.status = "cancelled"
        task.error = reason
        self.store.save_task(task)
        self.store.append_event(task.task_id, "task.cancelled", {"reason": reason, "source": "host"})
        return task

    def get_task(self, task_id: str) -> Task:
        return self.store.load_task(task_id)

    def list_events(self, task_id: str) -> list[dict]:
        return self.store.list_events(task_id)

    def run_task(self, task_id: str, command: list[str] | None = None) -> Task:
        task = self.store.load_task(task_id)
        if task.status in TERMINAL_TASK_STATUSES:
            return task
        if task.metadata.get("execution_mode") == "remote_worker":
            # Remote-managed tasks can only be advanced by their Worker Job.
            # Keeping this endpoint idempotent also prevents an old client from
            # accidentally launching a local duplicate.
            return task
        self._mark_task_started(task)
        loop = self._run_task_loop(task, command=command)
        if loop is not None:
            return self.store.load_task(task.task_id)
        if task.contract.get("engine") == APP_GRADE_RELEASE_E2E_ENGINE:
            self._dispatch_app_grade_task(task)
            self._evaluate_task_quality(task)
            return self.store.load_task(task.task_id)
        for subtask in sorted(task.subtasks, key=lambda item: (item.wave, item.priority, item.subtask_id)):
            if subtask.status == "completed":
                continue
            self._dispatch_subtask(task, subtask, command=command)
        self._evaluate_task_quality(task)
        return self.store.load_task(task.task_id)

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

    def _dispatch_app_grade_task(self, task: Task) -> dict[str, Any]:
        payload = task.metadata.get("app_grade_request")
        if not payload:
            payload = build_release_e2e_payload(
                task_id=task.task_id,
                project_root=task.project_root,
                allowed_agents=[subtask.agent for subtask in task.subtasks],
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
        self.store.save_task(task)
        self.store.append_event(task.task_id, "app_grade.host_conformance.completed", {
            "delivery_quality": result["delivery_quality"],
            "exact_files": result["exact_files"],
        })
        return result

    def _mark_task_started(self, task: Task) -> None:
        if task.status not in {"running", *TERMINAL_TASK_STATUSES}:
            task.status = "running"
            self.store.save_task(task)
            self.store.append_event(task.task_id, "task.started", {"agent": task.agent})

    def _run_task_loop(self, task: Task, *, command: list[str] | None = None):
        loop_id = str((task.metadata.get("agent_loop") or {}).get("loop_id") or "")
        if not loop_id:
            return None
        loop_runtime = self._agent_loop_runtime(command=command)
        loop = loop_runtime.run_loop(loop_id)
        event_type = {
            "completed": "agent_loop.completed",
            "failed": "agent_loop.failed",
            "awaiting_approval": "agent_loop.awaiting_approval",
            "stopped": "agent_loop.stopped",
            "cancelled": "agent_loop.cancelled",
        }.get(str(loop.status or ""), "agent_loop.updated")
        payload = {
            "loop_id": loop.loop_id,
            "status": loop.status,
            "turn_count": loop.turn_count,
            "checkpoint_count": loop.checkpoint_count,
            "error": loop.error,
            "finding_state": loop.finding_state,
            "failed_gates": failed_gate_ids_from_findings(loop.findings),
            "findings": loop.findings,
        }
        if str(loop.status or "") in {"failed", "stopped"}:
            payload["failure_type"] = failure_type_for_loop(loop)
        self.store.append_event(task.task_id, event_type, payload)
        current = self.store.load_task(task.task_id)
        agent_loop_metadata = {
            "status": loop.status,
            "turn_count": loop.turn_count,
            "checkpoint_count": loop.checkpoint_count,
            "error": loop.error,
            "finding_state": loop.finding_state,
            "failed_gates": failed_gate_ids_from_findings(loop.findings),
            "findings": loop.findings,
        }
        if str(loop.status or "") in {"failed", "stopped"}:
            agent_loop_metadata["failure_type"] = failure_type_for_loop(loop)
        current.metadata.setdefault("agent_loop", {}).update(agent_loop_metadata)
        self._sync_task_status_from_loop(current, loop)
        self.store.save_task(current)
        return loop

    def _sync_task_status_from_loop(self, task: Task, loop: Any) -> None:
        loop_status = str(getattr(loop, "status", "") or "")
        target_status = {
            "cancelled": "cancelled",
            "failed": "failed",
            "stopped": "failed",
        }.get(loop_status)
        if loop_status == "completed" and task.status not in {"completed", "failed", "cancelled"}:
            target_status = "completed"
        if target_status is None or task.status == target_status:
            return
        task.status = target_status
        event_type = {
            "completed": "task.completed",
            "cancelled": "task.cancelled",
        }.get(target_status, "task.failed")
        payload = {
            "loop_id": getattr(loop, "loop_id", None),
            "loop_status": loop_status,
            "error": getattr(loop, "error", None),
            "finding_state": getattr(loop, "finding_state", None),
            "failed_gates": failed_gate_ids_from_findings(getattr(loop, "findings", [])),
            "findings": getattr(loop, "findings", []),
        }
        if target_status == "failed":
            payload["failure_type"] = failure_type_for_loop(loop)
        self.store.append_event(task.task_id, event_type, payload)

    def _dispatch_task_from_loop(
        self,
        task_id: str,
        *,
        command: list[str] | None = None,
        remediation: bool = False,
        cancellation: Any | None = None,
    ) -> dict[str, Any]:
        task = self.store.load_task(task_id)
        self._mark_task_started(task)
        if task.contract.get("engine") == APP_GRADE_RELEASE_E2E_ENGINE:
            result = self._dispatch_app_grade_task(task)
            completed = len(task.subtasks)
            return {
                "dispatch": "completed",
                "adapter": "runtime",
                "task_id": task.task_id,
                "task_status": self.store.load_task(task.task_id).status,
                "completed_subtasks": completed,
                "failed_subtasks": 0,
                "remediation": remediation,
                "app_grade": {
                    "scenario_id": result.get("scenario_id"),
                    "delivery_quality": result.get("delivery_quality"),
                },
            }

        completed = 0
        skipped = 0
        subtask_ids = [
            item.subtask_id
            for item in sorted(task.subtasks, key=lambda item: (item.wave, item.priority, item.subtask_id))
        ]
        for subtask_id in subtask_ids:
            task = self.store.load_task(task.task_id)
            subtask = next((item for item in task.subtasks if item.subtask_id == subtask_id), None)
            if subtask is None:
                skipped += 1
                continue
            if subtask.status == "completed":
                skipped += 1
                continue
            if remediation and subtask.status not in {"failed", "pending", "running"}:
                skipped += 1
                continue
            self._dispatch_subtask(task, subtask, command=command, cancellation=cancellation)
            completed += 1
            task = self.store.load_task(task.task_id)
        failed = len([item for item in task.subtasks if item.status == "failed"])
        return {
            "dispatch": "completed" if failed == 0 else "failed",
            "adapter": "runtime",
            "task_id": task.task_id,
            "task_status": task.status,
            "completed_subtasks": completed,
            "skipped_subtasks": skipped,
            "failed_subtasks": failed,
            "remediation": remediation,
            "project_root": task.project_root,
        }

    def _dispatch_subtask(
        self,
        task: Task,
        subtask: SubTask,
        *,
        command: list[str] | None = None,
        cancellation: Any | None = None,
    ) -> None:
        subtask.status = "running"
        subtask.attempts += 1
        self.store.append_event(task.task_id, "subtask.started", subtask.__dict__)
        try:
            adapter = adapter_for(
                subtask.agent,
                command=command,
                spec=_agent_adapter_spec_for(task, subtask.agent),
            )
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            result = adapter.run(task, subtask, cancellation=cancellation)
            if cancellation is not None:
                cancellation.raise_if_cancelled()
        except ActionCancelledError as exc:
            cancel_category = normalize_cancel_category(exc.category, exc.reason)
            subtask.status = "cancelled"
            subtask.error = exc.reason
            task.status = "cancelled"
            self.store.append_event(
                task.task_id,
                "subtask.cancelled",
                {**subtask.__dict__, "cancel_category": cancel_category},
            )
            self.store.save_task(task)
            self.store.append_event(
                task.task_id,
                "task.cancelled",
                {"error": exc.reason, "cancel_category": cancel_category},
            )
            raise
        except Exception as exc:
            failure_type = failure_type_for_exception(exc)
            subtask.status = "failed"
            subtask.error = str(exc)
            task.status = "failed"
            self.store.append_event(task.task_id, "subtask.failed", {**subtask.__dict__, "failure_type": failure_type})
            self.store.save_task(task)
            self.store.append_event(task.task_id, "task.failed", {
                "error": str(exc),
                "failure_type": failure_type,
            })
            raise
        sandbox_receipt = result.get("sandbox_receipt") if isinstance(result, dict) else None
        if isinstance(sandbox_receipt, dict):
            task.metadata.setdefault("sandbox_executions", []).append({
                "subtask_id": subtask.subtask_id,
                "agent": subtask.agent,
                "receipt": sandbox_receipt,
            })
            self.store.append_event(
                task.task_id,
                "sandbox.execution.completed",
                {
                    "subtask_id": subtask.subtask_id,
                    "agent": subtask.agent,
                    "receipt": sandbox_receipt,
                },
            )
        subtask.status = "completed"
        subtask.error = None
        self.store.append_event(task.task_id, "subtask.completed", {**subtask.__dict__, "result": result})
        self.store.save_task(task)

    def _evaluate_task_quality(self, task: Task, *, repair_round: int = 0) -> dict[str, Any]:
        task = self.store.load_task(task.task_id)
        quality = normalize_quality_report(
            build_quality(task),
            finding_id="task_quality",
            source_gate="task_quality",
            owner=task.agent,
            repair_round=repair_round,
        )
        task.finding_state = quality["finding_state"]
        task.findings = [dict(item) for item in quality["findings"]]
        for item in task.findings:
            history_item = dict(item)
            metadata = dict(history_item.get("metadata") or {})
            metadata["task_id"] = task.task_id
            history_item["metadata"] = metadata
            history_key = (
                history_item.get("id"),
                history_item.get("state"),
                history_item.get("repair_round"),
                history_item.get("source_gate"),
            )
            existing_keys = {
                (
                    existing.get("id"),
                    existing.get("state"),
                    existing.get("repair_round"),
                    existing.get("source_gate"),
                )
                for existing in task.finding_history
            }
            if history_key not in existing_keys:
                task.finding_history.append(history_item)
        task.status = "completed" if quality_report_passed(quality) else "failed"
        self.store.save_task(task)
        payload = dict(quality)
        if not quality_report_passed(quality):
            payload["failure_type"] = failure_type_for_reason("quality_gate_failed")
        self.store.append_event(
            task.task_id,
            "task.completed" if task.status == "completed" else "task.failed",
            payload,
        )
        return quality

    def _create_task_for_loop(self, loop: Any) -> Task:
        metadata = dict(getattr(loop, "metadata", {}) or {})
        deliverables = metadata.get("deliverables") or metadata.get("requiredArtifacts") or metadata.get("required_artifacts")
        if not isinstance(deliverables, list):
            deliverables = ["README.md"]
        raw_subtasks = metadata.get("subtasks")
        subtasks = raw_subtasks if isinstance(raw_subtasks, list) else None
        paths = [str(item) for item in deliverables] or ["README.md"]
        if subtasks:
            task = Task.from_plan(
                goal=loop.goal,
                project_root=loop.project_root,
                subtasks=subtasks,
                deliverables=paths,
                agent=loop.agent,
            )
        else:
            task = Task.new(goal=loop.goal, project_root=loop.project_root, deliverables=paths, agent=loop.agent)
        if bool(metadata.get("strictDependency") or metadata.get("strict_dependency")) and len(task.subtasks) > 1:
            ensure_strict_dependency_chain(task)
        clean_task_types = _clean_task_types(metadata.get("taskTypes") or metadata.get("task_types") or None)
        if clean_task_types:
            task.metadata["task_types"] = clean_task_types
            task.metadata["delivery_mode"] = _delivery_mode_for_task_types(clean_task_types)
        clean_agent_adapters = normalize_agent_adapter_specs(
            metadata.get("agentAdapters") or metadata.get("agent_adapters") or None
        )
        if clean_agent_adapters:
            task.metadata["agent_adapters"] = clean_agent_adapters
        task.metadata["agent_loop"] = {
            "loop_id": loop.loop_id,
            "runtime": "across-orchestrator",
            "mode": "durable",
        }
        self.store.save_task(task)
        self.store.append_event(task.task_id, "task.created", {"goal": task.goal, "agent": task.agent})
        self.store.append_event(task.task_id, "contract.created", task.contract)
        self.store.append_event(task.task_id, "agent_loop.bound", task.metadata["agent_loop"])
        for subtask in task.subtasks:
            self.store.append_event(task.task_id, "subtask.created", subtask.__dict__)
        return task

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
            "finding_state": loop.finding_state,
            "findings": loop.findings,
            "finding_history": loop.finding_history,
            "final_output": loop.final_output,
            "events": self.loop_runtime.list_loop_events(loop.loop_id),
        }


class RuntimeLoopDispatcher:
    """Agent-loop dispatcher that can drive Orchestrator task execution."""

    requires_cancel_ack = True

    def __init__(self, runtime: OrchestratorRuntime, *, command: list[str] | None = None):
        self.runtime = runtime
        self.command = command
        self._fallback = HostLoopDispatcher()

    def dispatch(self, *, loop: Any, action_type: str, context: dict[str, Any]) -> dict[str, Any]:
        source_loop = getattr(loop, "_source_loop", loop)
        task_id = _task_id_from_loop_context(loop, context)
        if not task_id:
            if not _loop_declares_runtime_task(loop):
                return self._fallback.dispatch(loop=loop, action_type=action_type, context=context)
            task = self.runtime._create_task_for_loop(loop)
            source_loop.metadata["task_id"] = task.task_id
            self.runtime.store.save_loop(source_loop)
            task_id = task.task_id
        return self.runtime._dispatch_task_from_loop(
            task_id,
            command=self.command,
            remediation=action_type == "remediation_dispatch",
            cancellation=context.get("cancellation"),
        )


class RuntimeQualityGate:
    """Agent-loop quality gate backed by task evidence and quality benchmarks."""

    def __init__(self, runtime: OrchestratorRuntime):
        self.runtime = runtime
        self._fallback = DefaultQualityGate()

    def evaluate(self, *, loop: Any, context: dict[str, Any]) -> dict[str, Any]:
        task_id = _task_id_from_loop_context(loop, context)
        if not task_id:
            return self._fallback.evaluate(loop=loop, context=context)
        repair_round = sum(1 for step in getattr(loop, "steps", []) if step.action.type == "remediation_dispatch")
        quality = dict(
            self.runtime._evaluate_task_quality(
                self.runtime.store.load_task(task_id),
                repair_round=repair_round,
            )
        )
        status = str(quality.get("status") or "failed")
        quality.update({
            "task_id": task_id,
            "quality": status,
            "passed": quality_report_passed(quality),
            "summary": _quality_summary(quality),
        })
        return quality


class RuntimeFinalizer:
    """Agent-loop finalizer that returns task-aware completion output."""

    def __init__(self, runtime: OrchestratorRuntime):
        self.runtime = runtime
        self._fallback = DefaultFinalizer()

    def finalize(self, *, loop: Any, context: dict[str, Any]) -> dict[str, Any]:
        task_id = _task_id_from_loop_context(loop, context)
        if not task_id:
            return self._fallback.finalize(loop=loop, context=context)
        task = self.runtime.store.load_task(task_id)
        quality = build_quality(task)
        status = str(task.status or "unknown")
        quality_status = str(quality.get("status") or "unknown")
        quality_passed = quality_report_passed(quality)
        return {
            "final_output": f"Task {task.task_id} {status} for: {task.goal}",
            "status": "completed" if status == "completed" else status,
            "task_id": task.task_id,
            "task_status": status,
            "quality": quality_status,
            "finding_state": quality.get("finding_state"),
            "findings": quality.get("findings") or [],
            "passed": status == "completed" and quality_passed,
        }


def _task_id_from_loop_context(loop: Any, context: dict[str, Any]) -> str | None:
    metadata = getattr(loop, "metadata", {}) or {}
    for key in ("task_id", "taskId"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for step in reversed(context.get("steps") or []):
        observation = step.get("observation") if isinstance(step, dict) else None
        payload = observation.get("payload") if isinstance(observation, dict) else None
        if not isinstance(payload, dict):
            continue
        value = payload.get("task_id") or payload.get("taskId")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _loop_declares_runtime_task(loop: Any) -> bool:
    metadata = getattr(loop, "metadata", {}) or {}
    return any(
        key in metadata
        for key in (
            "deliverables",
            "requiredArtifacts",
            "required_artifacts",
            "subtasks",
            "taskTypes",
            "task_types",
            "agentAdapters",
            "agent_adapters",
            "strictDependency",
            "strict_dependency",
        )
    )


def _quality_summary(quality: dict[str, Any]) -> str:
    status = str(quality.get("status") or "unknown")
    missing = quality.get("missing_artifacts") or quality.get("missingArtifacts") or []
    if missing:
        return f"Quality {status}; missing artifacts: {', '.join(str(item) for item in missing)}."
    present = quality.get("present_artifacts")
    required = quality.get("required_artifacts")
    if present is not None and required is not None:
        return f"Quality {status}; {present}/{required} required artifacts present."
    return f"Quality {status}."


def _resolve_subtask_dependency_ids(subtasks: list[SubTask], stable_ids: dict[str, str] | None = None) -> None:
    """Convert stable scenario ids in dependencies into persisted subtask ids."""
    path_to_id = {subtask.path: subtask.subtask_id for subtask in subtasks}
    stem_to_id = {Path(subtask.path).stem: subtask.subtask_id for subtask in subtasks}
    known = {**stem_to_id, **path_to_id, **(stable_ids or {})}
    for subtask in subtasks:
        subtask.dependencies = [known.get(dep, dep) for dep in subtask.dependencies]


def _clean_task_types(task_types: list[str] | None) -> list[str]:
    clean: list[str] = []
    seen: set[str] = set()
    for item in task_types or []:
        value = str(item or "").strip().lower()
        if not value or value in seen:
            continue
        clean.append(value)
        seen.add(value)
    return clean


def _agent_adapter_spec_for(task: Task, agent: str) -> dict[str, Any] | None:
    specs = task.metadata.get("agent_adapters") or {}
    if not isinstance(specs, dict):
        return None
    for key in (agent, "default", "*"):
        spec = specs.get(key)
        if isinstance(spec, dict):
            return spec
    return None


def _clean_release_e2e_agents(allowed_agents: list[str] | None) -> list[str]:
    clean: list[str] = []
    seen: set[str] = set()
    for item in allowed_agents or []:
        value = str(item or "").strip().lower()
        if not value or value in seen or value.endswith("-agent"):
            continue
        clean.append(value)
        seen.add(value)
    return clean or ["openclaw", "hermes", "claude", "deepseek", "minimax"]


def _delivery_mode_for_task_types(task_types: list[str]) -> str:
    if len(task_types) > 1:
        return "composite"
    return task_types[0] if task_types else "artifact"
