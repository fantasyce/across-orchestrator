from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol
import os
import time

from .models import new_id
from .store import LocalStore


TERMINAL_LOOP_STATUSES = {"completed", "failed", "stopped", "cancelled"}
SUPPORTED_LOOP_ACTION_TYPES = {
    "memory_search",
    "task_dispatch",
    "remediation_dispatch",
    "quality_gate",
    "memory_write_candidate",
    "final_output",
}


class MemoryProvider(Protocol):
    def search(self, *, query: str, project_root: str, limit: int = 8, status: str = "active") -> dict[str, Any]:
        ...

    def remember_candidate(self, *, text: str, project_root: str, tags: list[str] | None = None) -> dict[str, Any]:
        ...


class LoopDispatcher(Protocol):
    def dispatch(self, *, loop: "LoopRun", action_type: str, context: dict[str, Any]) -> dict[str, Any]:
        ...


class QualityGate(Protocol):
    def evaluate(self, *, loop: "LoopRun", context: dict[str, Any]) -> dict[str, Any]:
        ...


class Finalizer(Protocol):
    def finalize(self, *, loop: "LoopRun", context: dict[str, Any]) -> dict[str, Any]:
        ...


class NullMemoryProvider:
    def search(self, *, query: str, project_root: str, limit: int = 8, status: str = "active") -> dict[str, Any]:
        return {
            "provider": "across-context",
            "query": query,
            "project_root": project_root,
            "result_count": 0,
            "results": [],
            "mode": "memory-provider-not-configured",
        }

    def remember_candidate(self, *, text: str, project_root: str, tags: list[str] | None = None) -> dict[str, Any]:
        return {
            "provider": "across-context",
            "memory": {
                "id": None,
                "text": text,
                "project_root": project_root,
                "tags": list(tags or []),
                "status": "pending",
            },
            "mode": "memory-provider-not-configured",
        }


class HostLoopDispatcher:
    def dispatch(self, *, loop: "LoopRun", action_type: str, context: dict[str, Any]) -> dict[str, Any]:
        if action_type == "remediation_dispatch":
            return {
                "dispatch": "completed",
                "agent": loop.agent,
                "adapter": "host",
                "remediation": True,
                "message": "Host remediation adapter accepted the loop action.",
            }
        return {
            "dispatch": "completed",
            "agent": loop.agent,
            "adapter": "host",
            "project_root": loop.project_root,
            "message": "Host dispatch adapter accepted the loop action.",
        }


class DefaultQualityGate:
    def evaluate(self, *, loop: "LoopRun", context: dict[str, Any]) -> dict[str, Any]:
        required = list((loop.metadata.get("quality_gates") or ["artifact_integrity", "evidence_bundle", "memory_policy"]))
        return {
            "quality": "passed",
            "passed": True,
            "gate_count": len(required),
            "required": required,
            "summary": "Default quality gate passed.",
        }


class DefaultFinalizer:
    def finalize(self, *, loop: "LoopRun", context: dict[str, Any]) -> dict[str, Any]:
        quality_summary = ""
        for step in reversed(loop.steps):
            if step.action.type == "quality_gate":
                summary = step.observation.payload.get("summary")
                if summary and summary != "Default quality gate passed.":
                    quality_summary = f" {summary}"
                break
        return {
            "final_output": (
                f"Agent loop completed for: {loop.goal}.{quality_summary}"
                if quality_summary
                else f"Agent loop completed for: {loop.goal}"
            ),
            "status": "completed",
        }


@dataclass
class AgentLoopAdapters:
    memory_provider: MemoryProvider = field(default_factory=NullMemoryProvider)
    dispatcher: LoopDispatcher = field(default_factory=HostLoopDispatcher)
    quality_gate: QualityGate = field(default_factory=DefaultQualityGate)
    finalizer: Finalizer = field(default_factory=DefaultFinalizer)


@dataclass
class LoopAction:
    action_id: str
    type: str
    title: str
    payload: dict[str, Any] = field(default_factory=dict)
    requires_approval: bool = False
    approval_status: str | None = None

    @classmethod
    def new(
        cls,
        action_type: str,
        title: str,
        payload: dict[str, Any] | None = None,
        *,
        requires_approval: bool = False,
    ) -> "LoopAction":
        return cls(
            action_id=new_id("action"),
            type=action_type,
            title=title,
            payload=payload or {},
            requires_approval=requires_approval,
            approval_status="pending" if requires_approval else None,
        )


@dataclass
class LoopObservation:
    observation_id: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(cls, status: str, payload: dict[str, Any] | None = None) -> "LoopObservation":
        return cls(observation_id=new_id("observation"), status=status, payload=payload or {})


@dataclass
class LoopStep:
    step_id: str
    loop_id: str
    turn: int
    phase: str
    status: str
    action: LoopAction
    observation: LoopObservation
    checkpoint: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def new(
        cls,
        *,
        loop_id: str,
        turn: int,
        phase: str,
        status: str,
        action: LoopAction,
        observation: LoopObservation,
        checkpoint: dict[str, Any] | None = None,
    ) -> "LoopStep":
        return cls(
            step_id=new_id("step"),
            loop_id=loop_id,
            turn=turn,
            phase=phase,
            status=status,
            action=action,
            observation=observation,
            checkpoint=checkpoint or {},
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopStep":
        return cls(
            step_id=data["step_id"],
            loop_id=data["loop_id"],
            turn=int(data["turn"]),
            phase=data.get("phase", "act"),
            status=data.get("status", "completed"),
            action=LoopAction(**data["action"]),
            observation=LoopObservation(**data["observation"]),
            checkpoint=dict(data.get("checkpoint") or {}),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LoopRun:
    loop_id: str
    goal: str
    project_root: str
    status: str = "pending"
    agent: str = "owner"
    max_turns: int = 8
    turn_count: int = 0
    memory_policy: dict[str, Any] = field(default_factory=dict)
    approval_policy: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    steps: list[LoopStep] = field(default_factory=list)
    checkpoint_count: int = 0
    final_output: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def new(
        cls,
        *,
        goal: str,
        project_root: str,
        agent: str = "owner",
        max_turns: int = 8,
        memory_policy: dict[str, Any] | None = None,
        approval_policy: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "LoopRun":
        root = str(Path(project_root).expanduser().resolve())
        return cls(
            loop_id=new_id("loop"),
            goal=goal.strip(),
            project_root=root,
            agent=agent or "owner",
            max_turns=max(1, int(max_turns or 8)),
            memory_policy={
                "provider": "across-context",
                "read": True,
                "writeCandidates": True,
                **(memory_policy or {}),
            },
            approval_policy={
                "requireApprovalFor": [],
                **(approval_policy or {}),
            },
            metadata=metadata or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopRun":
        return cls(
            loop_id=data["loop_id"],
            goal=data["goal"],
            project_root=data["project_root"],
            status=data.get("status", "pending"),
            agent=data.get("agent", "owner"),
            max_turns=int(data.get("max_turns", 8)),
            turn_count=int(data.get("turn_count", 0)),
            memory_policy=dict(data.get("memory_policy") or {}),
            approval_policy=dict(data.get("approval_policy") or {}),
            metadata=dict(data.get("metadata") or {}),
            steps=[LoopStep.from_dict(item) for item in data.get("steps", [])],
            checkpoint_count=int(data.get("checkpoint_count", 0)),
            final_output=data.get("final_output"),
            error=data.get("error"),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )


class AgentLoopRuntime:
    """Durable agent-loop runtime used by CLI, HTTP, MCP, and host SDK adapters."""

    def __init__(self, store: LocalStore | None = None, adapters: AgentLoopAdapters | None = None):
        self.store = store or LocalStore()
        self.adapters = adapters or default_agent_loop_adapters()

    def start_loop(
        self,
        *,
        goal: str,
        project_root: str,
        agent: str = "owner",
        max_turns: int = 8,
        memory_policy: dict[str, Any] | None = None,
        approval_policy: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LoopRun:
        if not goal or not goal.strip():
            raise ValueError("goal is required")
        clean_metadata = self._validated_metadata(metadata)
        Path(project_root).expanduser().resolve().mkdir(parents=True, exist_ok=True)
        loop = LoopRun.new(
            goal=goal,
            project_root=project_root,
            agent=agent,
            max_turns=max_turns,
            memory_policy=memory_policy,
            approval_policy=approval_policy,
            metadata=clean_metadata,
        )
        self.store.save_loop(loop)
        self.store.append_loop_event(loop.loop_id, "loop.started", {
            "goal": loop.goal,
            "agent": loop.agent,
            "memory_policy": loop.memory_policy,
        })
        return loop

    def get_loop(self, loop_id: str) -> LoopRun:
        return LoopRun.from_dict(self.store.load_loop_dict(loop_id))

    def list_loop_events(self, loop_id: str) -> list[dict[str, Any]]:
        return self.store.list_loop_events(loop_id)

    def run_loop(self, loop_id: str) -> LoopRun:
        with self.store.loop_lock(loop_id):
            return self._run_loop_locked(loop_id)

    def _run_loop_locked(self, loop_id: str) -> LoopRun:
        loop = self.get_loop(loop_id)
        if loop.status in TERMINAL_LOOP_STATUSES:
            return loop
        if self._pending_approval_step(loop) is not None:
            loop.status = "awaiting_approval"
            self.store.save_loop(loop)
            return loop
        loop.status = "running"
        loop.error = None
        self.store.save_loop(loop)

        while True:
            action_type = self._select_next_action(loop)
            if action_type is None:
                if loop.status in TERMINAL_LOOP_STATUSES:
                    return loop
                loop.status = "completed"
                if loop.final_output is None:
                    loop.final_output = f"Agent loop completed for: {loop.goal}"
                self.store.save_loop(loop)
                self.store.append_loop_event(loop.loop_id, "loop.completed", {
                    "final_output": loop.final_output,
                    "turn_count": loop.turn_count,
                    "checkpoint_count": loop.checkpoint_count,
                })
                return loop

            if loop.turn_count >= loop.max_turns:
                loop.status = "stopped"
                loop.error = "max_turns_exceeded"
                self.store.save_loop(loop)
                self.store.append_loop_event(loop.loop_id, "loop.stopped", {
                    "reason": loop.error,
                    "turn_count": loop.turn_count,
                    "max_turns": loop.max_turns,
                })
                return loop

            self.store.append_loop_event(loop.loop_id, "loop.next_action.selected", {
                "action_type": action_type,
                "turn": loop.turn_count + 1,
                "reason": self._next_action_reason(loop, action_type),
            })
            try:
                step = self._build_step(loop, action_type)
            except Exception as exc:
                failed_step = self._build_failed_step(loop, action_type, exc)
                loop.turn_count = failed_step.turn
                loop.steps.append(failed_step)
                loop.checkpoint_count = len([item for item in loop.steps if item.checkpoint])
                self.store.save_loop(loop)
                self.store.append_loop_event(loop.loop_id, "loop.step.failed", asdict(failed_step))
                return self._fail_loop(
                    loop,
                    reason=f"{action_type}_failed",
                    error=str(exc),
                    payload={
                        "action_type": action_type,
                        "turn": failed_step.turn,
                        "step_id": failed_step.step_id,
                    },
                )
            loop.turn_count = step.turn
            loop.steps.append(step)
            loop.checkpoint_count = len([item for item in loop.steps if item.checkpoint])
            if step.status == "waiting_approval":
                loop.status = "awaiting_approval"
                self.store.save_loop(loop)
                self.store.append_loop_event(loop.loop_id, "loop.approval_required", step.to_dict())
                return loop
            self.store.append_loop_event(loop.loop_id, "loop.step.completed", asdict(step))
            self.store.save_loop(loop)

    def approve_action(self, loop_id: str, action_id: str) -> LoopRun:
        with self.store.loop_lock(loop_id):
            return self._approve_action_locked(loop_id, action_id)

    def cancel_loop(self, loop_id: str, reason: str | None = None) -> LoopRun:
        with self.store.loop_lock(loop_id):
            loop = self.get_loop(loop_id)
            if loop.status in TERMINAL_LOOP_STATUSES:
                return loop
            cancel_reason = reason or "cancelled"
            for step in loop.steps:
                if step.status == "waiting_approval" and step.action.approval_status == "pending":
                    step.action.approval_status = "cancelled"
                    step.status = "cancelled"
                    step.observation = LoopObservation.new("cancelled", {
                        "approval": "cancelled",
                        "reason": cancel_reason,
                        "action_type": step.action.type,
                    })
                    step.updated_at = time.time()
            loop.status = "cancelled"
            loop.error = cancel_reason
            self.store.save_loop(loop)
            self.store.append_loop_event(loop.loop_id, "loop.cancelled", {
                "reason": loop.error,
                "turn_count": loop.turn_count,
                "checkpoint_count": loop.checkpoint_count,
            })
            return loop

    def _approve_action_locked(self, loop_id: str, action_id: str) -> LoopRun:
        loop = self.get_loop(loop_id)
        if loop.status in TERMINAL_LOOP_STATUSES:
            return loop
        for step in loop.steps:
            if step.action.action_id != action_id:
                continue
            if step.status != "waiting_approval":
                return loop
            step.action.approval_status = "approved"
            try:
                observation_payload = self._execute_action(loop, step.action.type)
            except Exception as exc:
                step.status = "failed"
                step.observation = LoopObservation.new("failed", {
                    "approval": "approved",
                    "action_type": step.action.type,
                    "error": str(exc),
                })
                step.checkpoint = self._checkpoint(
                    loop,
                    step.action.type,
                    step.turn,
                    step.observation.payload,
                    status="failed",
                )
                step.updated_at = time.time()
                loop.checkpoint_count = len([item for item in loop.steps if item.checkpoint])
                self.store.save_loop(loop)
                self.store.append_loop_event(loop.loop_id, "loop.action.failed", asdict(step))
                return self._fail_loop(
                    loop,
                    reason=f"{step.action.type}_failed",
                    error=str(exc),
                    payload={"action_id": action_id, "action_type": step.action.type},
                )
            step.status = "completed"
            step.observation = LoopObservation.new("approved", {
                **observation_payload,
                "approval": "approved",
            })
            step.checkpoint = self._checkpoint(loop, step.action.type, step.turn, step.observation.payload)
            step.updated_at = time.time()
            loop.status = "running"
            loop.checkpoint_count = len([item for item in loop.steps if item.checkpoint])
            if step.action.type == "final_output":
                loop.final_output = step.observation.payload.get("final_output")
            self.store.save_loop(loop)
            self.store.append_loop_event(loop.loop_id, "loop.action.approved", asdict(step))
            self.store.append_loop_event(loop.loop_id, "loop.step.completed", asdict(step))
            return loop
        raise KeyError(f"Action not found: {action_id}")

    def _validated_metadata(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        clean = dict(metadata or {})
        raw_plan = clean.get("actionPlan")
        if raw_plan is None:
            raw_plan = clean.get("action_plan")
        if raw_plan is None:
            return clean
        if not isinstance(raw_plan, list):
            raise ValueError("actionPlan must be a list of supported action types")
        invalid = [str(item or "").strip() for item in raw_plan if str(item or "").strip() not in SUPPORTED_LOOP_ACTION_TYPES]
        if invalid:
            raise ValueError(f"unsupported actionPlan entries: {', '.join(invalid)}")
        return clean

    def reject_action(self, loop_id: str, action_id: str, reason: str | None = None) -> LoopRun:
        with self.store.loop_lock(loop_id):
            loop = self.get_loop(loop_id)
            if loop.status in TERMINAL_LOOP_STATUSES:
                return loop
            for step in loop.steps:
                if step.action.action_id != action_id:
                    continue
                if step.status != "waiting_approval":
                    return loop
                step.action.approval_status = "rejected"
                step.status = "rejected"
                step.observation = LoopObservation.new("rejected", {
                    "approval": "rejected",
                    "reason": reason or "rejected",
                    "action_type": step.action.type,
                })
                step.updated_at = time.time()
                loop.status = "stopped"
                loop.error = "approval_rejected"
                self.store.save_loop(loop)
                self.store.append_loop_event(loop.loop_id, "loop.action.rejected", asdict(step))
                self.store.append_loop_event(loop.loop_id, "loop.stopped", {
                    "reason": loop.error,
                    "action_id": action_id,
                    "rejection_reason": reason or "rejected",
                })
                return loop
            raise KeyError(f"Action not found: {action_id}")

    def retry_step(self, loop_id: str, step_id: str) -> LoopRun:
        with self.store.loop_lock(loop_id):
            loop = self.get_loop(loop_id)
            retry_index = None
            for index, step in enumerate(loop.steps):
                if step.step_id == step_id:
                    retry_index = index
                    break
            if retry_index is None:
                raise KeyError(f"Step not found: {step_id}")

            removed = loop.steps[retry_index:]
            loop.steps = loop.steps[:retry_index]
            loop.turn_count = loop.steps[-1].turn if loop.steps else 0
            loop.checkpoint_count = len([item for item in loop.steps if item.checkpoint])
            loop.final_output = None
            loop.error = None
            loop.status = "running"
            self.store.save_loop(loop)
            self.store.append_loop_event(loop.loop_id, "loop.step.retry_requested", {
                "step_id": step_id,
                "action_type": removed[0].action.type,
                "removed_step_count": len(removed),
                "next_turn": loop.turn_count + 1,
            })
            return loop

    def _fail_loop(
        self,
        loop: LoopRun,
        *,
        reason: str,
        error: str,
        payload: dict[str, Any] | None = None,
    ) -> LoopRun:
        loop.status = "failed"
        loop.error = reason
        self.store.save_loop(loop)
        self.store.append_loop_event(loop.loop_id, "loop.failed", {
            "reason": reason,
            "error": error,
            **(payload or {}),
        })
        return loop

    def _select_next_action(self, loop: LoopRun) -> str | None:
        latest_quality = self._latest_step(loop, "quality_gate")
        latest_dispatch_index = self._latest_action_index(loop, {"task_dispatch", "remediation_dispatch"})
        latest_quality_index = self._latest_action_index(loop, {"quality_gate"})
        if latest_quality_index < latest_dispatch_index:
            return "quality_gate"
        if latest_quality and self._quality_failed(latest_quality):
            if self._action_count(loop, "remediation_dispatch") < self._max_remediation_turns(loop):
                return "remediation_dispatch"
            loop.status = "failed"
            loop.error = "quality_gate_failed"
            self.store.save_loop(loop)
            self.store.append_loop_event(loop.loop_id, "loop.failed", {
                "reason": loop.error,
                "failed_quality": latest_quality.observation.payload,
            })
            return None

        action_plan = self._host_action_plan(loop)
        if action_plan:
            plan_index = self._host_action_plan_progress(loop, action_plan)
            if plan_index < len(action_plan):
                return action_plan[plan_index]
            return None

        if bool(loop.memory_policy.get("read", True)) and not self._has_action(loop, "memory_search"):
            return "memory_search"
        if not self._has_action(loop, "task_dispatch"):
            return "task_dispatch"
        if latest_quality_index < latest_dispatch_index:
            return "quality_gate"
        if bool(loop.memory_policy.get("writeCandidates", True)) and not self._has_action_after(loop, "memory_write_candidate", latest_quality_index):
            return "memory_write_candidate"
        if not self._has_action(loop, "final_output"):
            return "final_output"
        return None

    def _next_action_reason(self, loop: LoopRun, action_type: str) -> str:
        return {
            "memory_search": "memory policy requires context before dispatch",
            "task_dispatch": "no dispatch observation exists for this loop",
            "quality_gate": "latest dispatch requires verification",
            "remediation_dispatch": "latest quality gate failed and remediation budget remains",
            "memory_write_candidate": "quality passed and memory policy allows pending summaries",
            "final_output": "loop has enough evidence to produce final output",
        }.get(action_type, "selected by host action plan" if self._host_action_plan(loop) else "selected by loop policy")

    def _planned_action_types(self, loop: LoopRun) -> list[str]:
        actions: list[str] = []
        if bool(loop.memory_policy.get("read", True)):
            actions.append("memory_search")
        actions.extend(["task_dispatch", "quality_gate"])
        if bool(loop.memory_policy.get("writeCandidates", True)):
            actions.append("memory_write_candidate")
        actions.append("final_output")
        return actions

    def _build_step(self, loop: LoopRun, action_type: str) -> LoopStep:
        turn = loop.turn_count + 1
        requires_approval = action_type in set(loop.approval_policy.get("requireApprovalFor") or [])
        action = LoopAction.new(
            action_type,
            self._action_title(action_type),
            self._action_payload(loop, action_type),
            requires_approval=requires_approval,
        )
        if requires_approval:
            return LoopStep.new(
                loop_id=loop.loop_id,
                turn=turn,
                phase="approval",
                status="waiting_approval",
                action=action,
                observation=LoopObservation.new("pending", {"approval": "required"}),
                checkpoint={},
            )
        observation_payload = self._execute_action(loop, action_type)
        if action_type == "final_output":
            loop.final_output = observation_payload.get("final_output")
        return LoopStep.new(
            loop_id=loop.loop_id,
            turn=turn,
            phase=self._phase_for(action_type),
            status="completed",
            action=action,
            observation=LoopObservation.new("completed", observation_payload),
            checkpoint=self._checkpoint(loop, action_type, turn, observation_payload),
        )

    def _build_failed_step(self, loop: LoopRun, action_type: str, exc: Exception) -> LoopStep:
        turn = loop.turn_count + 1
        action = LoopAction.new(
            action_type,
            self._action_title(action_type),
            self._action_payload(loop, action_type),
        )
        observation_payload = {
            "action_type": action_type,
            "error": str(exc),
        }
        return LoopStep.new(
            loop_id=loop.loop_id,
            turn=turn,
            phase=self._phase_for(action_type),
            status="failed",
            action=action,
            observation=LoopObservation.new("failed", observation_payload),
            checkpoint=self._checkpoint(loop, action_type, turn, observation_payload, status="failed"),
        )

    def _action_title(self, action_type: str) -> str:
        return {
            "memory_search": "Search shared memory before planning",
            "task_dispatch": "Dispatch work through host adapter",
            "remediation_dispatch": "Dispatch remediation through host adapter",
            "quality_gate": "Verify delivery quality",
            "memory_write_candidate": "Prepare pending memory candidate",
            "final_output": "Produce final output",
        }.get(action_type, action_type.replace("_", " ").title())

    def _phase_for(self, action_type: str) -> str:
        return {
            "memory_search": "context",
            "task_dispatch": "act",
            "remediation_dispatch": "act",
            "quality_gate": "verify",
            "memory_write_candidate": "remember",
            "final_output": "final",
        }.get(action_type, "act")

    def _action_payload(self, loop: LoopRun, action_type: str) -> dict[str, Any]:
        if action_type == "memory_search":
            return {"query": loop.goal, "provider": loop.memory_policy.get("provider", "across-context")}
        if action_type == "task_dispatch":
            return {"agent": loop.agent, "project_root": loop.project_root, "host_adapter": "provided-by-host"}
        if action_type == "remediation_dispatch":
            return {"agent": loop.agent, "project_root": loop.project_root, "host_adapter": "provided-by-host", "mode": "remediation"}
        if action_type == "quality_gate":
            return {"required": ["artifact_integrity", "evidence_bundle", "memory_policy"]}
        if action_type == "memory_write_candidate":
            return {"status": "pending", "provider": loop.memory_policy.get("provider", "across-context")}
        if action_type == "final_output":
            return {"format": "summary"}
        return {}

    def _execute_action(self, loop: LoopRun, action_type: str) -> dict[str, Any]:
        context = self._context(loop)
        if action_type == "memory_search":
            return self.adapters.memory_provider.search(
                query=loop.goal,
                project_root=loop.project_root,
                limit=int(loop.memory_policy.get("limit") or 8),
                status=str(loop.memory_policy.get("readStatus") or "active"),
            )
        if action_type in {"task_dispatch", "remediation_dispatch"}:
            return self.adapters.dispatcher.dispatch(loop=loop, action_type=action_type, context=context)
        if action_type == "quality_gate":
            return self.adapters.quality_gate.evaluate(loop=loop, context=context)
        if action_type == "memory_write_candidate":
            return self.adapters.memory_provider.remember_candidate(
                text=self._memory_candidate_text(loop),
                project_root=loop.project_root,
                tags=["agent-loop", loop.loop_id],
            )
        if action_type == "final_output":
            return self.adapters.finalizer.finalize(loop=loop, context=context)
        return {}

    def _checkpoint(
        self,
        loop: LoopRun,
        action_type: str,
        turn: int,
        observation_payload: dict[str, Any] | None = None,
        *,
        status: str = "completed",
    ) -> dict[str, Any]:
        latest = observation_payload or (loop.steps[-1].observation.payload if loop.steps else {})
        return {
            "loop_id": loop.loop_id,
            "turn": turn,
            "action_type": action_type,
            "status": status,
            "adapter": self._adapter_name(action_type),
            "observation_status": latest.get("quality") or latest.get("dispatch") or latest.get("status") or status,
        }

    def _host_action_plan(self, loop: LoopRun) -> list[str]:
        raw_plan = loop.metadata.get("actionPlan")
        if raw_plan is None:
            raw_plan = loop.metadata.get("action_plan")
        if not isinstance(raw_plan, list):
            return []
        plan: list[str] = []
        for item in raw_plan:
            action_type = str(item or "").strip()
            if action_type in SUPPORTED_LOOP_ACTION_TYPES:
                plan.append(action_type)
        return plan

    def _host_action_plan_progress(self, loop: LoopRun, action_plan: list[str]) -> int:
        plan_index = 0
        for step in loop.steps:
            if plan_index >= len(action_plan):
                break
            if step.action.type == action_plan[plan_index]:
                plan_index += 1
        return plan_index

    def _pending_approval_step(self, loop: LoopRun) -> LoopStep | None:
        for step in loop.steps:
            if step.status == "waiting_approval" and step.action.approval_status == "pending":
                return step
        return None

    def _context(self, loop: LoopRun) -> dict[str, Any]:
        return {
            "loop_id": loop.loop_id,
            "goal": loop.goal,
            "project_root": loop.project_root,
            "steps": [step.to_dict() for step in loop.steps],
            "memory": [step.observation.payload for step in loop.steps if step.action.type == "memory_search"],
            "quality": [step.observation.payload for step in loop.steps if step.action.type == "quality_gate"],
        }

    def _memory_candidate_text(self, loop: LoopRun) -> str:
        quality = self._latest_step(loop, "quality_gate")
        quality_summary = ""
        if quality:
            quality_summary = str(quality.observation.payload.get("summary") or quality.observation.payload.get("quality") or "")
        return f"Agent loop completed for {loop.goal}. {quality_summary}".strip()

    def _adapter_name(self, action_type: str) -> str:
        if action_type == "memory_search" or action_type == "memory_write_candidate":
            return self.adapters.memory_provider.__class__.__name__
        if action_type in {"task_dispatch", "remediation_dispatch"}:
            return self.adapters.dispatcher.__class__.__name__
        if action_type == "quality_gate":
            return self.adapters.quality_gate.__class__.__name__
        if action_type == "final_output":
            return self.adapters.finalizer.__class__.__name__
        return "unknown"

    def _latest_step(self, loop: LoopRun, action_type: str) -> LoopStep | None:
        for step in reversed(loop.steps):
            if step.action.type == action_type:
                return step
        return None

    def _has_action(self, loop: LoopRun, action_type: str) -> bool:
        return any(step.action.type == action_type for step in loop.steps)

    def _has_action_after(self, loop: LoopRun, action_type: str, index: int) -> bool:
        return any(step.action.type == action_type for step in loop.steps[index + 1:])

    def _action_count(self, loop: LoopRun, action_type: str) -> int:
        return sum(1 for step in loop.steps if step.action.type == action_type)

    def _latest_action_index(self, loop: LoopRun, action_types: set[str]) -> int:
        for index in range(len(loop.steps) - 1, -1, -1):
            if loop.steps[index].action.type in action_types:
                return index
        return -1

    def _quality_failed(self, step: LoopStep) -> bool:
        payload = step.observation.payload
        if payload.get("passed") is False:
            return True
        return str(payload.get("quality") or "").lower() in {"failed", "error", "blocked"}

    def _max_remediation_turns(self, loop: LoopRun) -> int:
        value = loop.metadata.get("maxRemediationTurns", loop.metadata.get("max_remediation_turns", 1))
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 1


def default_agent_loop_adapters(env: dict[str, str] | None = None) -> AgentLoopAdapters:
    source = env or os.environ
    provider = str(source.get("ACROSS_ORCHESTRATOR_MEMORY_PROVIDER") or "").strip().lower()
    if provider in {"across-context", "across_context"}:
        from .across_context import AcrossContextMemoryProvider

        return AgentLoopAdapters(memory_provider=AcrossContextMemoryProvider(env=source))
    return AgentLoopAdapters()
