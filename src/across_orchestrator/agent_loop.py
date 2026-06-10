from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
import time

from .models import new_id
from .store import LocalStore


TERMINAL_LOOP_STATUSES = {"completed", "failed", "stopped"}


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

    def __init__(self, store: LocalStore | None = None):
        self.store = store or LocalStore()

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
        Path(project_root).expanduser().resolve().mkdir(parents=True, exist_ok=True)
        loop = LoopRun.new(
            goal=goal,
            project_root=project_root,
            agent=agent,
            max_turns=max_turns,
            memory_policy=memory_policy,
            approval_policy=approval_policy,
            metadata=metadata,
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
        loop = self.get_loop(loop_id)
        if loop.status in TERMINAL_LOOP_STATUSES:
            return loop
        loop.status = "running"
        loop.error = None
        self.store.save_loop(loop)

        while len(loop.steps) < len(self._planned_action_types(loop)):
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

            action_type = self._planned_action_types(loop)[len(loop.steps)]
            step = self._build_step(loop, action_type)
            loop.turn_count = step.turn
            loop.steps.append(step)
            loop.checkpoint_count = len([item for item in loop.steps if item.checkpoint])
            if step.status == "waiting_approval":
                loop.status = "awaiting_approval"
                self.store.save_loop(loop)
                self.store.append_loop_event(loop.loop_id, "loop.approval_required", step.to_dict() if hasattr(step, "to_dict") else asdict(step))
                return loop
            self.store.append_loop_event(loop.loop_id, "loop.step.completed", asdict(step))
            self.store.save_loop(loop)

        loop.status = "completed"
        loop.final_output = f"Agent loop completed for: {loop.goal}"
        self.store.save_loop(loop)
        self.store.append_loop_event(loop.loop_id, "loop.completed", {
            "final_output": loop.final_output,
            "turn_count": loop.turn_count,
            "checkpoint_count": loop.checkpoint_count,
        })
        return loop

    def approve_action(self, loop_id: str, action_id: str) -> LoopRun:
        loop = self.get_loop(loop_id)
        for step in loop.steps:
            if step.action.action_id != action_id:
                continue
            if step.status != "waiting_approval":
                return loop
            step.action.approval_status = "approved"
            step.status = "completed"
            step.observation = LoopObservation.new("approved", {
                **self._observation_payload(loop, step.action.type),
                "approval": "approved",
            })
            step.checkpoint = self._checkpoint(loop, step.action.type, step.turn)
            step.updated_at = time.time()
            loop.status = "running"
            loop.checkpoint_count = len([item for item in loop.steps if item.checkpoint])
            self.store.save_loop(loop)
            self.store.append_loop_event(loop.loop_id, "loop.action.approved", asdict(step))
            return loop
        raise KeyError(f"Action not found: {action_id}")

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
        return LoopStep.new(
            loop_id=loop.loop_id,
            turn=turn,
            phase=self._phase_for(action_type),
            status="completed",
            action=action,
            observation=LoopObservation.new("completed", self._observation_payload(loop, action_type)),
            checkpoint=self._checkpoint(loop, action_type, turn),
        )

    def _action_title(self, action_type: str) -> str:
        return {
            "memory_search": "Search shared memory before planning",
            "task_dispatch": "Dispatch work through host adapter",
            "quality_gate": "Verify delivery quality",
            "memory_write_candidate": "Prepare pending memory candidate",
            "final_output": "Produce final output",
        }.get(action_type, action_type.replace("_", " ").title())

    def _phase_for(self, action_type: str) -> str:
        return {
            "memory_search": "context",
            "task_dispatch": "act",
            "quality_gate": "verify",
            "memory_write_candidate": "remember",
            "final_output": "final",
        }.get(action_type, "act")

    def _action_payload(self, loop: LoopRun, action_type: str) -> dict[str, Any]:
        if action_type == "memory_search":
            return {"query": loop.goal, "provider": loop.memory_policy.get("provider", "across-context")}
        if action_type == "task_dispatch":
            return {"agent": loop.agent, "project_root": loop.project_root, "host_adapter": "provided-by-host"}
        if action_type == "quality_gate":
            return {"required": ["artifact_integrity", "evidence_bundle", "memory_policy"]}
        if action_type == "memory_write_candidate":
            return {"status": "pending", "provider": loop.memory_policy.get("provider", "across-context")}
        if action_type == "final_output":
            return {"format": "summary"}
        return {}

    def _observation_payload(self, loop: LoopRun, action_type: str) -> dict[str, Any]:
        if action_type == "memory_search":
            return {
                "provider": loop.memory_policy.get("provider", "across-context"),
                "result_count": 0,
                "mode": "active-memory-only",
            }
        if action_type == "task_dispatch":
            return {"dispatch": "accepted", "agent": loop.agent, "adapter": "host"}
        if action_type == "quality_gate":
            return {"quality": "passed", "gate_count": 3}
        if action_type == "memory_write_candidate":
            return {
                "provider": loop.memory_policy.get("provider", "across-context"),
                "status": "pending",
                "candidate": f"Loop summary for {loop.goal}",
            }
        if action_type == "final_output":
            return {"final_output": f"Agent loop completed for: {loop.goal}"}
        return {}

    def _checkpoint(self, loop: LoopRun, action_type: str, turn: int) -> dict[str, Any]:
        return {
            "loop_id": loop.loop_id,
            "turn": turn,
            "action_type": action_type,
            "status": "completed",
        }
