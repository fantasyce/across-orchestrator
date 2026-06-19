from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
import json
import os
import threading
import time

from .cancellation import ActionCancelledError
from .failures import failure_type_for_exception, failure_type_for_loop, failure_type_for_reason
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
SUPPORTED_RECOVERY_ACTIONS = {"stop", "retry", "remediation", "require_human"}
RECOVERY_NEXT_ACTION_METADATA_KEY = "_recoveryNextAction"
CANCEL_CATEGORY_VALUES = ("user_cancelled", "shutdown", "superseded", "timeout_cancelled")
CANCEL_CATEGORIES = set(CANCEL_CATEGORY_VALUES)
CANCEL_CATEGORY_RELEASE_BLOCKING_VALUES = frozenset({"shutdown", "timeout_cancelled"})
CANCEL_CATEGORY_ALIASES = {
    "user": "user_cancelled",
    "cancelled": "user_cancelled",
    "user_cancelled": "user_cancelled",
    "shutdown": "shutdown",
    "shutting_down": "shutdown",
    "superseded": "superseded",
    "timeout": "timeout_cancelled",
    "timeout_cancelled": "timeout_cancelled",
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


def normalize_cancel_category(category: Any = None, reason: Any = None) -> str:
    raw = str(category or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw:
        normalized = CANCEL_CATEGORY_ALIASES.get(raw)
        if normalized:
            return normalized
        raise ValueError(f"Unsupported cancel category: {category}")
    reason_text = str(reason or "").strip().lower().replace("-", "_").replace(" ", "_")
    if "shutdown" in reason_text:
        return "shutdown"
    if "superseded" in reason_text:
        return "superseded"
    if "timeout" in reason_text:
        return "timeout_cancelled"
    return "user_cancelled"


def cancel_category_release_status(category: Any = None, reason: Any = None) -> str:
    normalized = normalize_cancel_category(category, reason)
    return "blocked" if normalized in CANCEL_CATEGORY_RELEASE_BLOCKING_VALUES else "attention"


def cancel_category_release_risk_severity(category: Any = None, reason: Any = None) -> str:
    return "high" if cancel_category_release_status(category, reason) == "blocked" else "medium"


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


class LoopCancellationToken:
    """Cooperative cancellation token backed by the durable loop store."""

    def __init__(self, store: LocalStore, loop_id: str):
        self.store = store
        self.loop_id = loop_id
        self._lock = threading.Lock()
        self._cancel_request: dict[str, Any] | None = None

    def request(self) -> dict[str, Any] | None:
        with self._lock:
            if self._cancel_request is not None:
                return dict(self._cancel_request)
        request = self.store.load_loop_cancel_request(self.loop_id)
        if request is None:
            return None
        return self._latch(request)

    def is_cancelled(self) -> bool:
        return self.request() is not None

    def reason(self) -> str:
        request = self.request() or {}
        return str(request.get("reason") or "cancelled")

    def category(self) -> str:
        request = self.request() or {}
        return normalize_cancel_category(request.get("cancel_category"), request.get("reason"))

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise ActionCancelledError(self.reason(), category=self.category())

    def _latch(self, request: dict[str, Any]) -> dict[str, Any]:
        clean = dict(request)
        clean["loop_id"] = self.loop_id
        clean["reason"] = str(clean.get("reason") or "cancelled")
        clean["cancel_category"] = normalize_cancel_category(clean.get("cancel_category"), clean.get("reason"))
        clean.setdefault("requested_at", time.time())
        with self._lock:
            if self._cancel_request is None:
                self._cancel_request = clean
            return dict(self._cancel_request)


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

    def get_loop_health(self, loop_id: str) -> dict[str, Any]:
        loop = self.get_loop(loop_id)
        events = self.store.list_loop_events(loop_id)
        now = time.time()
        pending_approval = self._pending_approval_step(loop)
        current_action_type, current_step_id = self._current_action(loop)
        failure_counts = self._recent_failure_type_counts(events)
        cancel_request = self.store.load_loop_cancel_request(loop_id)
        cancel_category = self._cancel_category_for_health(cancel_request, events)
        last_event_at = events[-1]["timestamp"] if events else loop.created_at

        return {
            "schema_version": "0.1",
            "loop_id": loop.loop_id,
            "status": loop.status,
            "agent": loop.agent,
            "turn_count": loop.turn_count,
            "checkpoint_count": loop.checkpoint_count,
            "current_action_type": current_action_type,
            "current_step_id": current_step_id,
            "pending_approval": self._pending_approval_payload(pending_approval),
            "lease": self._lease_health(loop, events, now),
            "last_event_at": last_event_at,
            "detached_dispatch_count": sum(1 for event in events if event.get("type") == "loop.dispatch.detached"),
            "recent_failure_types": failure_counts,
            "executable_actions": self._executable_actions(loop, pending_approval),
            "cancellation_requested": cancel_request is not None,
            "cancellation_category": cancel_category,
            "cancel_ack_pending": cancel_request is not None and loop.status not in TERMINAL_LOOP_STATUSES,
        }

    def get_loop_evidence_summary(self, loop_id: str) -> dict[str, Any]:
        """Return a compact, read-only evidence summary derived from durable loop state."""
        loop = self.get_loop(loop_id)
        events = self.store.list_loop_events(loop_id)
        recovery_decisions = self._evidence_recovery_decisions(events)
        recovered_steps = self._evidence_recovered_steps(events)
        routing_outcomes = self._evidence_routing_outcomes(loop)
        memory_candidates = self._evidence_memory_candidates(loop)
        event_audit = self._evidence_event_audit(events)
        routing = {
            "outcomes": routing_outcomes,
            "routed_action_count": len(routing_outcomes),
            "non_default_route_count": sum(
                1 for item in routing_outcomes if item.get("source") not in {None, "loop.agent", "action.payload.agent"}
            ),
            "capability_hint_route_count": sum(1 for item in routing_outcomes if item.get("capability_hint")),
        }
        recovery = {
            "decisions": recovery_decisions,
            "recovered_steps": recovered_steps,
            "decision_count": len(recovery_decisions),
            "applied_count": sum(1 for item in recovery_decisions if item.get("applied") is True),
            "blocked_count": sum(1 for item in recovery_decisions if item.get("applied") is False),
        }
        memory_summary = {
            "candidates": memory_candidates,
            "candidate_count": len(memory_candidates),
        }
        cancellation = self._evidence_cancellation(events)

        return {
            "schema_version": "0.1",
            "loop_id": loop.loop_id,
            "status": loop.status,
            "agent": loop.agent,
            "turn_count": loop.turn_count,
            "checkpoint_count": loop.checkpoint_count,
            "final_output_ready": bool(loop.final_output),
            "event_audit": event_audit,
            "routing": routing,
            "recovery": recovery,
            "memory_candidates": memory_summary,
            "cancellation": cancellation,
            "host_release_evidence": self._evidence_host_release_evidence(
                loop=loop,
                event_audit=event_audit,
                routing=routing,
                recovery=recovery,
                memory_candidates=memory_summary,
                cancellation=cancellation,
            ),
        }

    def run_loop(self, loop_id: str) -> LoopRun:
        with self.store.loop_lock(loop_id):
            return self._run_loop_locked(loop_id)

    def _run_loop_locked(self, loop_id: str) -> LoopRun:
        loop = self.get_loop(loop_id)
        if loop.status in TERMINAL_LOOP_STATUSES:
            self.store.clear_loop_cancel_request(loop.loop_id)
            return loop
        cancel_request = self.store.load_loop_cancel_request(loop.loop_id)
        if cancel_request:
            return self._cancel_loop_state(
                loop,
                str(cancel_request.get("reason") or "cancelled"),
                cancel_category=normalize_cancel_category(cancel_request.get("cancel_category"), cancel_request.get("reason")),
            )
        incomplete = self._recover_or_hold_running_action(loop)
        if incomplete is not None:
            return incomplete
        if self._pending_approval_step(loop) is not None:
            loop.status = "awaiting_approval"
            self.store.save_loop(loop)
            return loop
        loop.status = "running"
        loop.error = None
        self.store.save_loop(loop)

        while True:
            cancel_request = self.store.load_loop_cancel_request(loop.loop_id)
            if cancel_request:
                return self._cancel_loop_state(
                    loop,
                    str(cancel_request.get("reason") or "cancelled"),
                    cancel_category=normalize_cancel_category(cancel_request.get("cancel_category"), cancel_request.get("reason")),
                )
            action_type = self._select_next_action(loop)
            if action_type is None:
                if loop.status in TERMINAL_LOOP_STATUSES:
                    return loop
                if loop.status == "awaiting_approval":
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
                    "failure_type": failure_type_for_reason(loop.error),
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
            except ActionCancelledError as exc:
                self._mark_running_step_cancelled(loop, action_type, exc.reason, cancel_category=exc.category)
                return self._cancel_loop_state(loop, exc.reason, cancel_category=exc.category)
            except Exception as exc:
                failed_step = self._mark_running_step_failed(loop, action_type, exc)
                if failed_step is None:
                    failed_step = self._build_failed_step(loop, action_type, exc)
                    loop.steps.append(failed_step)
                loop.turn_count = failed_step.turn
                loop.checkpoint_count = len([item for item in loop.steps if item.checkpoint])
                self.store.save_loop(loop)
                self.store.append_loop_event(loop.loop_id, "loop.step.failed", asdict(failed_step))
                if self._apply_recovery_policy(
                    loop,
                    failed_step,
                    reason=f"{action_type}_failed",
                    error=str(exc),
                    failure_type=str(failed_step.observation.payload.get("failure_type") or ""),
                ):
                    if loop.status == "awaiting_approval":
                        return loop
                    continue
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
            if not any(item.step_id == step.step_id for item in loop.steps):
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

    def cancel_loop(
        self,
        loop_id: str,
        reason: str | None = None,
        *,
        cancel_category: str | None = None,
    ) -> LoopRun:
        loop = self.get_loop(loop_id)
        if loop.status in TERMINAL_LOOP_STATUSES:
            self.store.clear_loop_cancel_request(loop.loop_id)
            return loop
        cancel_reason = reason or "cancelled"
        category = normalize_cancel_category(cancel_category, cancel_reason)
        request = self.store.request_loop_cancel(loop_id, cancel_reason, category=category)
        self.store.append_loop_event(loop_id, "loop.cancel_requested", {
            "reason": cancel_reason,
            "cancel_category": category,
            "requested_at": request["requested_at"],
        })
        try:
            lock = self.store.loop_lock(loop_id, blocking=False)
            with lock:
                loop = self.get_loop(loop_id)
                if loop.status in TERMINAL_LOOP_STATUSES:
                    self.store.clear_loop_cancel_request(loop.loop_id)
                    return loop
                return self._cancel_loop_state(loop, cancel_reason, cancel_category=category)
        except BlockingIOError:
            loop = self.get_loop(loop_id)
            if loop.status not in TERMINAL_LOOP_STATUSES:
                loop.status = "cancelled"
                loop.error = cancel_reason
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
            self._mark_step_running(loop, step, {"approval": "approved", "action_type": step.action.type})
            try:
                observation_payload = self._execute_action(loop, step.action.type)
            except ActionCancelledError as exc:
                self._mark_running_step_cancelled(
                    loop,
                    step.action.type,
                    exc.reason,
                    {"approval": "approved"},
                    cancel_category=exc.category,
                )
                return self._cancel_loop_state(loop, exc.reason, cancel_category=exc.category)
            except Exception as exc:
                failed_step = self._mark_running_step_failed(
                    loop,
                    step.action.type,
                    exc,
                    {"approval": "approved"},
                ) or step
                loop.checkpoint_count = len([item for item in loop.steps if item.checkpoint])
                self.store.save_loop(loop)
                self.store.append_loop_event(loop.loop_id, "loop.action.failed", asdict(failed_step))
                if self._apply_recovery_policy(
                    loop,
                    failed_step,
                    reason=f"{step.action.type}_failed",
                    error=str(exc),
                    failure_type=str(failed_step.observation.payload.get("failure_type") or ""),
                ):
                    return loop
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
            step.checkpoint = self._checkpoint(
                loop,
                step.action.type,
                step.turn,
                step.observation.payload,
                execution=self._complete_execution(step),
            )
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
                    "failure_type": failure_type_for_reason("approval_rejected"),
                    "action_type": step.action.type,
                })
                step.updated_at = time.time()
                loop.status = "stopped"
                loop.error = "approval_rejected"
                self.store.save_loop(loop)
                self.store.append_loop_event(loop.loop_id, "loop.action.rejected", asdict(step))
                self.store.append_loop_event(loop.loop_id, "loop.stopped", {
                    "reason": loop.error,
                    "failure_type": failure_type_for_reason(loop.error),
                    "action_id": action_id,
                    "rejection_reason": reason or "rejected",
                })
                return loop
            raise KeyError(f"Action not found: {action_id}")

    def retry_step(self, loop_id: str, step_id: str) -> LoopRun:
        with self.store.loop_lock(loop_id):
            loop = self.get_loop(loop_id)
            removed = self._rollback_loop_to_step(loop, step_id)
            if not removed:
                raise KeyError(f"Step not found: {step_id}")
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
        event_payload = dict(payload or {})
        loop.status = "failed"
        loop.error = reason
        failure_type = str(event_payload.get("failure_type") or "") or failure_type_for_loop(loop, reason)
        event_payload["failure_type"] = failure_type
        self.store.save_loop(loop)
        self.store.append_loop_event(loop.loop_id, "loop.failed", {
            "reason": reason,
            "error": error,
            **event_payload,
        })
        return loop

    def _apply_recovery_policy(
        self,
        loop: LoopRun,
        failed_step: LoopStep,
        *,
        reason: str,
        error: str,
        failure_type: str | None = None,
    ) -> bool:
        clean_failure_type = failure_type or failure_type_for_loop(loop, reason)
        rule = self._recovery_rule(loop, clean_failure_type)
        if rule is None:
            return False

        recovery_action = rule["action"]
        attempt = self._recovery_attempt_count(loop.loop_id, clean_failure_type, recovery_action) + 1
        max_retries = int(rule["max_retries"])
        applied = recovery_action != "stop" and attempt <= max_retries
        decision_payload = {
            "step_id": failed_step.step_id,
            "action_type": failed_step.action.type,
            "failure_type": clean_failure_type,
            "reason": reason,
            "error": error,
            "recovery_action": recovery_action,
            "attempt": attempt,
            "max_retries": max_retries,
            "applied": applied,
            "source": rule["source"],
        }
        if not applied and recovery_action != "stop":
            decision_payload["blocked_reason"] = "max_retries_exceeded"
        self.store.append_loop_event(loop.loop_id, "loop.step.recovery_decision", decision_payload)

        if not applied:
            return False
        if recovery_action == "retry":
            removed = self._rollback_loop_to_step(loop, failed_step.step_id)
            self.store.save_loop(loop)
            self.store.append_loop_event(loop.loop_id, "loop.step.recovered", {
                **decision_payload,
                "recovered_from_step_id": failed_step.step_id,
                "removed_step_count": len(removed),
                "next_turn": loop.turn_count + 1,
                "next_action": self._peek_next_action(loop),
            })
            return True
        if recovery_action == "remediation":
            loop.status = "running"
            loop.error = None
            loop.final_output = None
            loop.metadata[RECOVERY_NEXT_ACTION_METADATA_KEY] = "remediation_dispatch"
            self.store.save_loop(loop)
            self.store.append_loop_event(loop.loop_id, "loop.step.recovered", {
                **decision_payload,
                "recovered_from_step_id": failed_step.step_id,
                "next_turn": loop.turn_count + 1,
                "next_action": "remediation_dispatch",
            })
            return True
        if recovery_action == "require_human":
            approval_step = self._append_recovery_approval_step(
                loop,
                failed_step,
                failure_type=clean_failure_type,
                reason=reason,
                error=error,
            )
            self.store.append_loop_event(loop.loop_id, "loop.step.recovered", {
                **decision_payload,
                "recovered_from_step_id": failed_step.step_id,
                "approval_step_id": approval_step.step_id,
                "approval_action_id": approval_step.action.action_id,
                "next_turn": approval_step.turn,
                "next_action": approval_step.action.type,
            })
            return True
        return False

    def _recovery_rule(self, loop: LoopRun, failure_type: str) -> dict[str, Any] | None:
        policy = loop.metadata.get("recoveryPolicy", loop.metadata.get("recovery_policy"))
        if not isinstance(policy, dict):
            return None
        by_failure = policy.get("byFailureType", policy.get("by_failure_type", {}))
        raw_rule: Any = None
        source = "metadata.recoveryPolicy.defaultAction"
        if isinstance(by_failure, dict) and failure_type in by_failure:
            raw_rule = by_failure[failure_type]
            source = f"metadata.recoveryPolicy.byFailureType.{failure_type}"
        elif policy.get("defaultAction") is not None or policy.get("default_action") is not None:
            raw_rule = {
                "action": policy.get("defaultAction", policy.get("default_action")),
                "maxRetries": policy.get("maxRetries", policy.get("max_retries")),
            }
        if raw_rule is None:
            return None

        if isinstance(raw_rule, str):
            action = raw_rule
            raw_max_retries = policy.get("maxRetries", policy.get("max_retries"))
        elif isinstance(raw_rule, dict):
            action = raw_rule.get("action", raw_rule.get("type"))
            raw_max_retries = raw_rule.get(
                "maxRetries",
                raw_rule.get("max_retries", policy.get("maxRetries", policy.get("max_retries"))),
            )
        else:
            return None

        recovery_action = str(action or "stop").strip().lower()
        if recovery_action not in SUPPORTED_RECOVERY_ACTIONS:
            return None
        default_max_retries = 0 if recovery_action == "stop" else 1
        try:
            max_retries = max(0, int(raw_max_retries if raw_max_retries is not None else default_max_retries))
        except (TypeError, ValueError):
            max_retries = default_max_retries
        return {"action": recovery_action, "max_retries": max_retries, "source": source}

    def _recovery_attempt_count(self, loop_id: str, failure_type: str, recovery_action: str) -> int:
        count = 0
        for event in self.store.list_loop_events(loop_id):
            if event.get("type") != "loop.step.recovered":
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if payload.get("failure_type") == failure_type and payload.get("recovery_action") == recovery_action:
                count += 1
        return count

    def _rollback_loop_to_step(self, loop: LoopRun, step_id: str) -> list[LoopStep]:
        retry_index = None
        for index, step in enumerate(loop.steps):
            if step.step_id == step_id:
                retry_index = index
                break
        if retry_index is None:
            return []
        removed = loop.steps[retry_index:]
        loop.steps = loop.steps[:retry_index]
        loop.turn_count = loop.steps[-1].turn if loop.steps else 0
        loop.checkpoint_count = len([item for item in loop.steps if item.checkpoint])
        loop.final_output = None
        loop.error = None
        loop.status = "running"
        return removed

    def _append_recovery_approval_step(
        self,
        loop: LoopRun,
        failed_step: LoopStep,
        *,
        failure_type: str,
        reason: str,
        error: str,
    ) -> LoopStep:
        approval_step = LoopStep.new(
            loop_id=loop.loop_id,
            turn=loop.turn_count + 1,
            phase="approval",
            status="waiting_approval",
            action=LoopAction.new(
                failed_step.action.type,
                f"Approve recovery for {failed_step.action.type.replace('_', ' ')}",
                {
                    "recovery": "require_human",
                    "failed_step_id": failed_step.step_id,
                    "failure_type": failure_type,
                    "reason": reason,
                    "error": error,
                    **failed_step.action.payload,
                },
                requires_approval=True,
            ),
            observation=LoopObservation.new("pending", {
                "approval": "required",
                "recovery": "require_human",
                "failed_step_id": failed_step.step_id,
                "failure_type": failure_type,
            }),
            checkpoint={},
        )
        loop.steps.append(approval_step)
        loop.turn_count = approval_step.turn
        loop.status = "awaiting_approval"
        loop.error = None
        loop.final_output = None
        loop.checkpoint_count = len([item for item in loop.steps if item.checkpoint])
        self.store.save_loop(loop)
        self.store.append_loop_event(loop.loop_id, "loop.approval_required", approval_step.to_dict())
        return approval_step

    def _recovery_next_action(self, loop: LoopRun) -> str | None:
        action_type = str(loop.metadata.get(RECOVERY_NEXT_ACTION_METADATA_KEY) or "").strip()
        return action_type if action_type in SUPPORTED_LOOP_ACTION_TYPES else None

    def _cancel_loop_state(
        self,
        loop: LoopRun,
        reason: str,
        *,
        cancel_category: str | None = None,
    ) -> LoopRun:
        cancel_reason = reason or "cancelled"
        category = normalize_cancel_category(cancel_category, cancel_reason)
        cancelled_steps: list[LoopStep] = []
        for step in loop.steps:
            if step.status == "cancelled" and step.observation.status == "cancelled":
                cancelled_steps.append(step)
            elif step.status == "running":
                payload = {
                    "action_type": step.action.type,
                    "reason": cancel_reason,
                    "cancel_category": category,
                }
                step.status = "cancelled"
                step.observation = LoopObservation.new("cancelled", payload)
                step.checkpoint = self._checkpoint(
                    loop,
                    step.action.type,
                    step.turn,
                    payload,
                    status="cancelled",
                    execution=self._complete_execution(step),
                )
                step.updated_at = time.time()
                cancelled_steps.append(step)
            elif step.status == "waiting_approval" and step.action.approval_status == "pending":
                step.action.approval_status = "cancelled"
                step.status = "cancelled"
                step.observation = LoopObservation.new("cancelled", {
                    "approval": "cancelled",
                    "reason": cancel_reason,
                    "cancel_category": category,
                    "action_type": step.action.type,
                })
                step.updated_at = time.time()
                cancelled_steps.append(step)
        loop.status = "cancelled"
        loop.error = cancel_reason
        loop.checkpoint_count = len([item for item in loop.steps if item.checkpoint])
        self.store.save_loop(loop)
        self.store.clear_loop_cancel_request(loop.loop_id)
        for step in cancelled_steps:
            self.store.append_loop_event(loop.loop_id, "loop.step.cancelled", step.to_dict())
        self.store.append_loop_event(loop.loop_id, "loop.cancelled", {
            "reason": loop.error,
            "cancel_category": category,
            "turn_count": loop.turn_count,
            "checkpoint_count": loop.checkpoint_count,
        })
        return loop

    def _current_action(self, loop: LoopRun) -> tuple[str | None, str | None]:
        running = self._latest_running_step(loop)
        if running is not None:
            return running.action.type, running.step_id
        pending = self._pending_approval_step(loop)
        if pending is not None:
            return pending.action.type, pending.step_id
        if loop.status in TERMINAL_LOOP_STATUSES:
            return None, None
        return self._peek_next_action(loop), None

    def _pending_approval_payload(self, step: LoopStep | None) -> dict[str, Any] | None:
        if step is None:
            return None
        return {
            "step_id": step.step_id,
            "action_id": step.action.action_id,
            "action_type": step.action.type,
            "title": step.action.title,
            "approval_status": step.action.approval_status,
        }

    def _cancel_category_for_health(
        self,
        request: dict[str, Any] | None,
        events: list[dict[str, Any]],
    ) -> str | None:
        if request is not None:
            return normalize_cancel_category(request.get("cancel_category"), request.get("reason"))
        for event in reversed(events):
            if event.get("type") not in {"loop.cancel_requested", "loop.cancelled", "loop.step.cancelled"}:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            category = payload.get("cancel_category")
            if category:
                return normalize_cancel_category(category, payload.get("reason"))
        return None

    def _evidence_event_audit(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        sequences = [event.get("sequence") for event in events]
        numeric_sequences = [item for item in sequences if isinstance(item, int)]
        return {
            "event_count": len(events),
            "first_sequence": numeric_sequences[0] if numeric_sequences else None,
            "last_sequence": numeric_sequences[-1] if numeric_sequences else None,
            "sequence_contiguous": numeric_sequences == list(range(1, len(events) + 1)),
            "event_id_coverage": all(bool(event.get("event_id")) for event in events) if events else True,
            "correlation_id_coverage": all(bool(event.get("correlation_id")) for event in events) if events else True,
        }

    def _evidence_recovery_decisions(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        decisions: list[dict[str, Any]] = []
        fields = (
            "step_id",
            "action_type",
            "failure_type",
            "reason",
            "recovery_action",
            "attempt",
            "max_retries",
            "applied",
            "blocked_reason",
            "source",
        )
        for event in events:
            if event.get("type") != "loop.step.recovery_decision":
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            item = self._evidence_event_ref(event)
            for field in fields:
                if field in payload:
                    item[field] = payload[field]
            decisions.append(item)
        return decisions

    def _evidence_recovered_steps(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        fields = (
            "step_id",
            "action_type",
            "failure_type",
            "recovery_action",
            "attempt",
            "recovered_from_step_id",
            "next_action",
            "next_turn",
        )
        for event in events:
            if event.get("type") != "loop.step.recovered":
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            item = self._evidence_event_ref(event)
            for field in fields:
                if field in payload:
                    item[field] = payload[field]
            recovered.append(item)
        return recovered

    def _evidence_routing_outcomes(self, loop: LoopRun) -> list[dict[str, Any]]:
        outcomes: list[dict[str, Any]] = []
        for step in loop.steps:
            if step.action.type not in {"task_dispatch", "remediation_dispatch"}:
                continue
            payload = step.action.payload if isinstance(step.action.payload, dict) else {}
            routing = payload.get("routing") if isinstance(payload.get("routing"), dict) else {}
            item = {
                "step_id": step.step_id,
                "turn": step.turn,
                "action_type": step.action.type,
                "status": step.status,
                "base_agent": routing.get("base_agent") or loop.agent,
                "selected_agent": routing.get("selected_agent") or payload.get("agent"),
                "source": routing.get("source") or "action.payload.agent",
            }
            for field in ("matched_gate", "capability_hint"):
                if routing.get(field):
                    item[field] = routing[field]
            outcomes.append(item)
        return outcomes

    def _evidence_memory_candidates(self, loop: LoopRun) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for step in loop.steps:
            if step.action.type != "memory_write_candidate":
                continue
            action_payload = step.action.payload if isinstance(step.action.payload, dict) else {}
            observation_payload = step.observation.payload if isinstance(step.observation.payload, dict) else {}
            memory = observation_payload.get("memory") if isinstance(observation_payload.get("memory"), dict) else {}
            item = {
                "step_id": step.step_id,
                "turn": step.turn,
                "status": step.status,
                "provider": action_payload.get("provider") or observation_payload.get("provider"),
                "memory_status": memory.get("status") or observation_payload.get("status"),
            }
            if memory.get("id"):
                item["memory_id"] = memory["id"]
            candidates.append({key: value for key, value in item.items() if value is not None})
        return candidates

    def _evidence_cancellation(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        for event in reversed(events):
            if event.get("type") not in {"loop.cancel_requested", "loop.cancelled", "loop.step.cancelled"}:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            return {
                **self._evidence_event_ref(event),
                "requested": True,
                "category": normalize_cancel_category(payload.get("cancel_category"), payload.get("reason")),
                "reason": payload.get("reason"),
            }
        return {"requested": False, "category": None, "reason": None}

    def _evidence_host_release_evidence(
        self,
        *,
        loop: LoopRun,
        event_audit: dict[str, Any],
        routing: dict[str, Any],
        recovery: dict[str, Any],
        memory_candidates: dict[str, Any],
        cancellation: dict[str, Any],
    ) -> dict[str, Any]:
        """Promote compact evidence into host-facing release readiness signals."""
        checks: list[dict[str, Any]] = []
        risks: list[dict[str, Any]] = []
        next_actions: list[str] = []

        def add_check(check_id: str, status: str, summary: str, **details: Any) -> None:
            item = {"id": check_id, "status": status, "summary": summary}
            item.update({key: value for key, value in details.items() if value is not None})
            checks.append(item)

        audit_passed = bool(event_audit.get("sequence_contiguous")) and bool(event_audit.get("event_id_coverage")) and bool(
            event_audit.get("correlation_id_coverage")
        )
        if audit_passed:
            add_check(
                "event_audit",
                "passed",
                f"{event_audit.get('event_count', 0)} events have contiguous sequences and audit identifiers.",
            )
        else:
            add_check(
                "event_audit",
                "blocked",
                "Event audit metadata is incomplete.",
                sequence_contiguous=event_audit.get("sequence_contiguous"),
                event_id_coverage=event_audit.get("event_id_coverage"),
                correlation_id_coverage=event_audit.get("correlation_id_coverage"),
            )
            risks.append(
                {
                    "id": "event_audit_incomplete",
                    "severity": "high",
                    "summary": "Loop events cannot be used as complete release evidence until audit metadata is repaired.",
                }
            )
            next_actions.append("Repair loop event audit metadata before using this loop as release evidence.")

        routed_count = int(routing.get("routed_action_count") or 0)
        non_default_count = int(routing.get("non_default_route_count") or 0)
        capability_hint_count = int(routing.get("capability_hint_route_count") or 0)
        add_check(
            "capability_routing",
            "passed",
            (
                f"{routed_count} dispatch actions recorded; "
                f"{non_default_count} used explicit routing and {capability_hint_count} used capability hints."
            ),
            routed_action_count=routed_count,
            non_default_route_count=non_default_count,
            capability_hint_route_count=capability_hint_count,
        )

        recovery_blocked_count = int(recovery.get("blocked_count") or 0)
        recovery_applied_count = int(recovery.get("applied_count") or 0)
        if recovery_blocked_count:
            add_check(
                "recovery",
                "blocked",
                f"{recovery_blocked_count} recovery decisions blocked loop completion.",
                blocked_count=recovery_blocked_count,
                applied_count=recovery_applied_count,
            )
            risks.append(
                {
                    "id": "recovery_blocked",
                    "severity": "high",
                    "summary": "At least one recovery decision could not be applied.",
                }
            )
            next_actions.append("Review blocked recovery decisions before release.")
        elif recovery_applied_count:
            add_check(
                "recovery",
                "attention",
                f"{recovery_applied_count} recovery decisions were applied successfully.",
                applied_count=recovery_applied_count,
            )
            risks.append(
                {
                    "id": "recovery_applied",
                    "severity": "medium",
                    "summary": "Loop completed with recovery; verify the recovered path is expected.",
                }
            )
            next_actions.append("Review applied recovery decisions for release notes or follow-up work.")
        else:
            add_check("recovery", "passed", "No recovery decisions were needed.", applied_count=0, blocked_count=0)

        candidate_count = int(memory_candidates.get("candidate_count") or 0)
        if candidate_count:
            add_check(
                "memory_candidates",
                "attention",
                f"{candidate_count} structured memory candidates are pending host review.",
                candidate_count=candidate_count,
            )
            risks.append(
                {
                    "id": "memory_review_pending",
                    "severity": "low",
                    "summary": "Structured memory candidates should be reviewed before treating loop evidence as final.",
                }
            )
            next_actions.append("Review pending structured memory candidates in Across Context.")
        else:
            add_check("memory_candidates", "passed", "No structured memory candidates require review.", candidate_count=0)

        if cancellation.get("requested"):
            category = str(cancellation.get("category") or "user_cancelled")
            status = cancel_category_release_status(category)
            add_check(
                "cancellation",
                status,
                f"Loop cancellation was requested with category {category}.",
                category=category,
            )
            risks.append(
                {
                    "id": f"cancelled_{category}",
                    "severity": cancel_category_release_risk_severity(category),
                    "summary": "Cancellation affected loop completion evidence.",
                }
            )
            next_actions.append("Confirm the cancellation category is expected before release.")
        else:
            add_check("cancellation", "passed", "No cancellation request affected this loop.", category=None)

        if any(item["status"] == "blocked" for item in checks):
            readiness = "blocked"
        elif any(item["status"] == "attention" for item in checks):
            readiness = "attention"
        else:
            readiness = "ready"

        return {
            "schema_version": "0.1",
            "readiness": readiness,
            "loop_status": loop.status,
            "checks": checks,
            "risks": risks,
            "risk_count": len(risks),
            "next_actions": list(dict.fromkeys(next_actions)),
        }

    def _evidence_event_ref(self, event: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": event.get("event_id"),
            "sequence": event.get("sequence"),
            "timestamp": event.get("timestamp"),
            "correlation_id": event.get("correlation_id"),
        }

    def _lease_health(self, loop: LoopRun, events: list[dict[str, Any]], now: float) -> dict[str, Any]:
        running = self._latest_running_step(loop)
        execution = self._execution_from_checkpoint(running) if running is not None else {}
        latest_heartbeat = self._latest_event_payload(events, "loop.step.heartbeat")
        heartbeat_at = self._float_or_none(execution.get("heartbeat_at"))
        if heartbeat_at is None:
            heartbeat_at = self._float_or_none(latest_heartbeat.get("heartbeat_at")) if latest_heartbeat else None
        lease_seconds = self._float_or_none(execution.get("lease_seconds"))
        if lease_seconds is None:
            lease_seconds = self._action_lease_seconds(loop)
        expires_at = self._float_or_none(execution.get("lease_expires_at"))
        active = running is not None and loop.status == "running"
        remaining = max(0.0, expires_at - now) if active and expires_at is not None else None
        return {
            "active": active,
            "lease_id": execution.get("lease_id"),
            "lease_seconds": lease_seconds,
            "heartbeat_at": heartbeat_at,
            "expires_at": expires_at,
            "remaining_seconds": remaining,
            "expired": bool(active and expires_at is not None and expires_at <= now),
            "renewal_count": int(execution.get("renewal_count") or 0) if execution else 0,
        }

    def _latest_event_payload(self, events: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
        for event in reversed(events):
            if event.get("type") == event_type and isinstance(event.get("payload"), dict):
                return event["payload"]
        return None

    def _recent_failure_type_counts(self, events: list[dict[str, Any]], *, limit: int = 10) -> dict[str, int]:
        counts: dict[str, int] = {}
        collected = 0
        for event in reversed(events):
            failure_type = self._failure_type_from_event(event)
            if not failure_type:
                continue
            counts[failure_type] = counts.get(failure_type, 0) + 1
            collected += 1
            if collected >= limit:
                break
        return counts

    def _failure_type_from_event(self, event: dict[str, Any]) -> str | None:
        event_type = str(event.get("type") or "")
        if event_type not in {"loop.step.failed", "loop.step.lease_expired", "loop.action.failed", "loop.failed", "loop.stopped"}:
            return None
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        direct = payload.get("failure_type")
        if direct:
            return str(direct)
        observation = payload.get("observation") if isinstance(payload.get("observation"), dict) else {}
        observation_payload = observation.get("payload") if isinstance(observation.get("payload"), dict) else {}
        if observation_payload.get("failure_type"):
            return str(observation_payload["failure_type"])
        checkpoint = payload.get("checkpoint") if isinstance(payload.get("checkpoint"), dict) else {}
        if checkpoint.get("failure_type"):
            return str(checkpoint["failure_type"])
        reason = payload.get("reason") or payload.get("error")
        return failure_type_for_reason(str(reason)) if reason else None

    def _executable_actions(self, loop: LoopRun, pending_approval: LoopStep | None) -> list[str]:
        if loop.status in {"failed", "stopped"}:
            return ["retry"] if loop.steps else []
        if loop.status in {"completed", "cancelled"}:
            return []
        if pending_approval is not None or loop.status == "awaiting_approval":
            actions = ["approve", "reject", "cancel"]
            if loop.steps:
                actions.append("retry")
            return actions
        actions = ["run", "cancel"]
        if loop.steps:
            actions.append("retry")
        return actions

    def _peek_next_action(self, loop: LoopRun) -> str | None:
        recovery_next_action = self._recovery_next_action(loop)
        if recovery_next_action is not None:
            return recovery_next_action
        latest_quality = self._latest_step(loop, "quality_gate")
        latest_dispatch_index = self._latest_action_index(loop, {"task_dispatch", "remediation_dispatch"})
        latest_quality_index = self._latest_action_index(loop, {"quality_gate"})
        if latest_quality_index < latest_dispatch_index:
            return "quality_gate"
        if latest_quality and self._quality_failed(latest_quality):
            if self._action_count(loop, "remediation_dispatch") < self._max_remediation_turns(loop):
                return "remediation_dispatch"
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

    def _select_next_action(self, loop: LoopRun) -> str | None:
        recovery_next_action = self._recovery_next_action(loop)
        if recovery_next_action is not None:
            loop.metadata.pop(RECOVERY_NEXT_ACTION_METADATA_KEY, None)
            self.store.save_loop(loop)
            return recovery_next_action
        action = self._peek_next_action(loop)
        if action is not None:
            return action
        latest_quality = self._latest_step(loop, "quality_gate")
        if latest_quality and self._quality_failed(latest_quality):
            if self._apply_recovery_policy(
                loop,
                latest_quality,
                reason="quality_gate_failed",
                error="quality_gate_failed",
                failure_type=failure_type_for_reason("quality_gate_failed"),
            ):
                if loop.status == "awaiting_approval":
                    return None
                recovery_next_action = self._recovery_next_action(loop)
                if recovery_next_action is not None:
                    loop.metadata.pop(RECOVERY_NEXT_ACTION_METADATA_KEY, None)
                    self.store.save_loop(loop)
                    return recovery_next_action
                return self._peek_next_action(loop)
            loop.status = "failed"
            loop.error = "quality_gate_failed"
            self.store.save_loop(loop)
            self.store.append_loop_event(loop.loop_id, "loop.failed", {
                "reason": loop.error,
                "failure_type": failure_type_for_reason(loop.error),
                "failed_quality": latest_quality.observation.payload,
            })
            return None
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
        step = LoopStep.new(
            loop_id=loop.loop_id,
            turn=turn,
            phase=self._phase_for(action_type),
            status="running",
            action=action,
            observation=LoopObservation.new("running", {"action_type": action_type}),
            checkpoint={},
        )
        self._mark_step_running(loop, step)
        observation_payload = self._execute_action(loop, action_type)
        if action_type == "final_output":
            loop.final_output = observation_payload.get("final_output")
        step.status = "completed"
        step.observation = LoopObservation.new("completed", observation_payload)
        step.checkpoint = self._checkpoint(
            loop,
            action_type,
            turn,
            observation_payload,
            execution=self._complete_execution(step),
        )
        step.updated_at = time.time()
        return step

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
            "failure_type": failure_type_for_exception(exc),
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
            routing = self._agent_routing(loop, action_type)
            return {
                "agent": routing["selected_agent"],
                "project_root": loop.project_root,
                "host_adapter": "provided-by-host",
                "routing": routing,
            }
        if action_type == "remediation_dispatch":
            routing = self._agent_routing(loop, action_type)
            return {
                "agent": routing["selected_agent"],
                "project_root": loop.project_root,
                "host_adapter": "provided-by-host",
                "mode": "remediation",
                "routing": routing,
            }
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
            routing = self._agent_routing(loop, action_type)
            dispatch_loop = replace(loop, agent=routing["selected_agent"])
            setattr(dispatch_loop, "_source_loop", loop)
            cancellation = LoopCancellationToken(self.store, loop.loop_id)
            cancellation.raise_if_cancelled()
            dispatch_context = {**context, "routing": routing, "cancellation": cancellation}
            lease = self._latest_running_execution(loop, action_type)
            if lease:
                dispatch_context["lease"] = lease
                dispatch_context["heartbeat"] = lambda: self._renew_running_step_lease(loop, action_type)
            result = self._dispatch_with_cancellation_guard(
                loop=dispatch_loop,
                action_type=action_type,
                context=dispatch_context,
            )
            cancellation.raise_if_cancelled()
            return result
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

    def _dispatch_with_cancellation_guard(
        self,
        *,
        loop: LoopRun,
        action_type: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        outcome: dict[str, Any] = {}
        done = threading.Event()

        def call_dispatcher() -> None:
            try:
                outcome["result"] = self.adapters.dispatcher.dispatch(
                    loop=loop,
                    action_type=action_type,
                    context=context,
                )
            except Exception as exc:
                outcome["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(
            target=call_dispatcher,
            name=f"across-loop-dispatch-{loop.loop_id}-{action_type}",
            daemon=True,
        )
        worker.start()
        cancellation = context.get("cancellation")
        while not done.wait(0.05):
            if cancellation is not None:
                try:
                    cancellation.raise_if_cancelled()
                except ActionCancelledError as exc:
                    if self._dispatcher_requires_cancel_ack():
                        done.wait()
                        if "error" in outcome:
                            raise outcome["error"]
                        raise ActionCancelledError(exc.reason, category=exc.category)
                    self.store.append_loop_event(loop.loop_id, "loop.dispatch.detached", {
                        "action_type": action_type,
                        "reason": exc.reason,
                        "cancel_category": normalize_cancel_category(exc.category, exc.reason),
                        "dispatcher": self.adapters.dispatcher.__class__.__name__,
                    })
                    raise
        if "error" in outcome:
            raise outcome["error"]
        return dict(outcome.get("result") or {})

    def _dispatcher_requires_cancel_ack(self) -> bool:
        return bool(getattr(self.adapters.dispatcher, "requires_cancel_ack", False))

    def _agent_routing(self, loop: LoopRun, action_type: str) -> dict[str, Any]:
        selected = loop.agent
        source = "loop.agent"
        matched_gate: str | None = None
        static_selected = False
        forbidden_agents = self._capability_hint_forbidden_agents(loop)
        routing = loop.metadata.get("agentRouting", loop.metadata.get("agent_routing"))
        if isinstance(routing, dict):
            route = routing.get(action_type, routing.get("default"))
            if isinstance(route, str):
                candidate = self._clean_agent_id(route)
                if candidate:
                    selected = candidate
                    source = f"metadata.agentRouting.{action_type}"
                    static_selected = True
            elif isinstance(route, dict):
                for gate in self._latest_failed_gates(loop):
                    candidate = self._clean_agent_id(route.get(gate))
                    if candidate:
                        selected = candidate
                        matched_gate = gate
                        source = f"metadata.agentRouting.{action_type}.{gate}"
                        static_selected = True
                        break
                if matched_gate is None:
                    candidate = self._clean_agent_id(route.get("default"))
                    if candidate:
                        selected = candidate
                        source = f"metadata.agentRouting.{action_type}.default"
                        static_selected = True
        if selected in forbidden_agents:
            selected = loop.agent if loop.agent not in forbidden_agents else selected
            source = "metadata.agentCapabilityHints.constraints.forbid"
            static_selected = False
        hint_route = self._agent_capability_hint_route(loop, action_type, forbidden_agents)
        if hint_route and not static_selected:
            selected = hint_route["selected_agent"]
            source = hint_route["source"]
            matched_gate = hint_route.get("matched_gate")
        result = {
            "action_type": action_type,
            "base_agent": loop.agent,
            "selected_agent": selected,
            "source": source,
        }
        if matched_gate:
            result["matched_gate"] = matched_gate
        if hint_route and source == hint_route["source"]:
            result["capability_hint"] = hint_route.get("capability_hint")
        return result

    def _agent_capability_hint_route(
        self,
        loop: LoopRun,
        action_type: str,
        forbidden_agents: set[str],
    ) -> dict[str, Any] | None:
        hints = loop.metadata.get("agentCapabilityHints", loop.metadata.get("agent_capability_hints"))
        if not isinstance(hints, dict):
            return None
        preferred = hints.get("preferred") if isinstance(hints.get("preferred"), dict) else {}
        constraints = hints.get("constraints") if isinstance(hints.get("constraints"), dict) else {}
        required = (
            constraints.get("requireCapability")
            or constraints.get("require_capability")
            if isinstance(constraints, dict)
            else {}
        )
        required = required if isinstance(required, dict) else {}

        queries: list[tuple[str, str, str | None]] = []
        for gate in self._latest_failed_gates(loop):
            for key in (f"{action_type}.{gate}", gate):
                value = required.get(key)
                if value:
                    queries.append((str(value), f"metadata.agentCapabilityHints.constraints.requireCapability.{key}", gate))
        for key in (action_type, "default"):
            value = required.get(key)
            if value:
                queries.append((str(value), f"metadata.agentCapabilityHints.constraints.requireCapability.{key}", None))
        for key in (action_type, "default"):
            value = preferred.get(key)
            if value:
                queries.append((str(value), f"metadata.agentCapabilityHints.preferred.{key}", None))

        for query, source, matched_gate in queries:
            candidate = self._agent_from_capability_registry(hints, query, forbidden_agents)
            if candidate:
                result = {
                    "selected_agent": candidate,
                    "source": source,
                    "capability_hint": query,
                }
                if matched_gate:
                    result["matched_gate"] = matched_gate
                return result
            direct = self._clean_agent_id(query)
            if direct and direct not in forbidden_agents:
                return {
                    "selected_agent": direct,
                    "source": source,
                    "capability_hint": query,
                    **({"matched_gate": matched_gate} if matched_gate else {}),
                }
        return None

    def _agent_from_capability_registry(
        self,
        hints: dict[str, Any],
        query: str,
        forbidden_agents: set[str],
    ) -> str | None:
        registry = hints.get("registry", hints.get("agentRegistry", hints.get("agent_registry", {})))
        agents = registry.get("agents") if isinstance(registry, dict) else hints.get("agents")
        if not isinstance(agents, list):
            return None
        query_token = self._capability_token(query)
        if not query_token:
            return None
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            agent_id = self._clean_agent_id(agent.get("agent_id") or agent.get("agentId") or agent.get("id"))
            if not agent_id or agent_id in forbidden_agents:
                continue
            if query_token in self._agent_capability_tokens(agent):
                return agent_id
        return None

    def _agent_capability_tokens(self, agent: dict[str, Any]) -> set[str]:
        values: list[Any] = [
            agent.get("agent_id"),
            agent.get("agentId"),
            agent.get("id"),
            agent.get("display_name"),
            agent.get("displayName"),
        ]
        for key in (
            "aliases",
            "capabilities",
            "capability_ids",
            "capabilityIds",
            "configured_skill_ids",
            "configuredSkillIds",
            "configured_skill_names",
            "configuredSkillNames",
            "enabled_plugin_ids",
            "enabledPluginIds",
            "enabled_tool_names",
            "enabledToolNames",
            "tags",
        ):
            value = agent.get(key)
            if isinstance(value, list):
                values.extend(value)
            elif value:
                values.append(value)
        return {token for token in (self._capability_token(value) for value in values) if token}

    def _capability_hint_forbidden_agents(self, loop: LoopRun) -> set[str]:
        hints = loop.metadata.get("agentCapabilityHints", loop.metadata.get("agent_capability_hints"))
        if not isinstance(hints, dict):
            return set()
        constraints = hints.get("constraints")
        if not isinstance(constraints, dict):
            return set()
        forbidden = constraints.get("forbid") or constraints.get("forbidden") or []
        if isinstance(forbidden, str):
            forbidden = [forbidden]
        if not isinstance(forbidden, list):
            return set()
        return {agent for agent in (self._clean_agent_id(value) for value in forbidden) if agent}

    def _capability_token(self, value: Any) -> str:
        return "".join(ch for ch in str(value or "").lower() if ch.isalnum())

    def _latest_failed_gates(self, loop: LoopRun) -> list[str]:
        latest_quality = self._latest_step(loop, "quality_gate")
        if latest_quality is None:
            return []
        payload = latest_quality.observation.payload
        failed = payload.get("failed_gates") or payload.get("failedGates") or []
        if not isinstance(failed, list):
            return []
        gates: list[str] = []
        for item in failed:
            value = str(item or "").strip()
            if value:
                gates.append(value)
        return gates

    def _clean_agent_id(self, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    def _checkpoint(
        self,
        loop: LoopRun,
        action_type: str,
        turn: int,
        observation_payload: dict[str, Any] | None = None,
        *,
        status: str = "completed",
        execution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        latest = observation_payload or (loop.steps[-1].observation.payload if loop.steps else {})
        checkpoint = {
            "loop_id": loop.loop_id,
            "turn": turn,
            "action_type": action_type,
            "status": status,
            "adapter": self._adapter_name(action_type),
            "observation_status": latest.get("quality") or latest.get("dispatch") or latest.get("status") or status,
        }
        if latest.get("failure_type"):
            checkpoint["failure_type"] = latest["failure_type"]
        if execution:
            checkpoint["execution"] = execution
        return checkpoint

    def _recover_or_hold_running_action(self, loop: LoopRun) -> LoopRun | None:
        step = self._latest_running_step(loop)
        if step is None:
            return None
        execution = self._execution_from_checkpoint(step)
        lease_expires_at = self._float_or_none(execution.get("lease_expires_at"))
        if lease_expires_at is None or lease_expires_at > time.time():
            loop.status = "running"
            self.store.save_loop(loop)
            return loop

        observation_payload = {
            "action_type": step.action.type,
            "error": "action_lease_expired",
            "failure_type": failure_type_for_reason("action_lease_expired"),
            "lease_id": execution.get("lease_id"),
            "lease_expires_at": lease_expires_at,
        }
        step.status = "failed"
        step.observation = LoopObservation.new("failed", observation_payload)
        step.checkpoint = self._checkpoint(
            loop,
            step.action.type,
            step.turn,
            observation_payload,
            status="failed",
            execution=self._complete_execution(step),
        )
        step.updated_at = time.time()
        loop.turn_count = max(loop.turn_count, step.turn)
        loop.checkpoint_count = len([item for item in loop.steps if item.checkpoint])
        self.store.save_loop(loop)
        self.store.append_loop_event(loop.loop_id, "loop.step.lease_expired", step.to_dict())
        if self._apply_recovery_policy(
            loop,
            step,
            reason="action_lease_expired",
            error="action_lease_expired",
            failure_type=failure_type_for_reason("action_lease_expired"),
        ):
            if loop.status == "awaiting_approval":
                return loop
            return None
        return self._fail_loop(
            loop,
            reason="action_lease_expired",
            error="action_lease_expired",
            payload={
                "step_id": step.step_id,
                "action_type": step.action.type,
                "lease_id": execution.get("lease_id"),
                "lease_expires_at": lease_expires_at,
            },
        )

    def _latest_running_step(self, loop: LoopRun) -> LoopStep | None:
        for step in reversed(loop.steps):
            if step.status == "running":
                return step
        return None

    def _latest_running_execution(self, loop: LoopRun, action_type: str) -> dict[str, Any] | None:
        step = self._latest_running_step(loop)
        if step is None or step.action.type != action_type:
            return None
        execution = self._execution_from_checkpoint(step)
        return execution or None

    def _mark_step_running(
        self,
        loop: LoopRun,
        step: LoopStep,
        observation_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = observation_payload or {"action_type": step.action.type}
        execution = self._start_execution_lease(loop)
        step.status = "running"
        step.observation = LoopObservation.new("running", payload)
        step.checkpoint = self._checkpoint(
            loop,
            step.action.type,
            step.turn,
            {"status": "running", **payload},
            status="running",
            execution=execution,
        )
        step.updated_at = time.time()
        loop.status = "running"
        loop.turn_count = max(loop.turn_count, step.turn)
        if not any(item.step_id == step.step_id for item in loop.steps):
            loop.steps.append(step)
        loop.checkpoint_count = len([item for item in loop.steps if item.checkpoint])
        self.store.save_loop(loop)
        self.store.append_loop_event(loop.loop_id, "loop.step.started", step.to_dict())
        self.store.append_loop_event(loop.loop_id, "loop.step.heartbeat", {
            "step_id": step.step_id,
            "action_type": step.action.type,
            "lease_id": execution["lease_id"],
            "heartbeat_at": execution["heartbeat_at"],
            "lease_expires_at": execution["lease_expires_at"],
            "renewal_count": execution["renewal_count"],
        })
        return execution

    def _renew_running_step_lease(self, loop: LoopRun, action_type: str) -> dict[str, Any]:
        cancel_request = self.store.load_loop_cancel_request(loop.loop_id)
        if cancel_request:
            raise ActionCancelledError(
                str(cancel_request.get("reason") or "cancelled"),
                category=normalize_cancel_category(cancel_request.get("cancel_category"), cancel_request.get("reason")),
            )
        step = self._latest_running_step(loop)
        if step is None or step.action.type != action_type:
            raise RuntimeError(f"No running action lease for {action_type}")
        execution = self._execution_from_checkpoint(step)
        if not execution:
            raise RuntimeError(f"No execution lease for {action_type}")
        now = time.time()
        lease_seconds = self._action_lease_seconds(loop)
        execution["heartbeat_at"] = now
        execution["lease_seconds"] = lease_seconds
        execution["lease_expires_at"] = now + lease_seconds
        execution["renewal_count"] = int(execution.get("renewal_count") or 0) + 1
        step.checkpoint["execution"] = execution
        step.updated_at = now
        self.store.save_loop(loop)
        payload = {
            "step_id": step.step_id,
            "action_type": step.action.type,
            "lease_id": execution["lease_id"],
            "heartbeat_at": execution["heartbeat_at"],
            "lease_expires_at": execution["lease_expires_at"],
            "renewal_count": execution["renewal_count"],
        }
        self.store.append_loop_event(loop.loop_id, "loop.step.heartbeat", payload)
        return payload

    def _mark_running_step_failed(
        self,
        loop: LoopRun,
        action_type: str,
        exc: Exception,
        observation_payload: dict[str, Any] | None = None,
    ) -> LoopStep | None:
        step = self._latest_running_step(loop)
        if step is None or step.action.type != action_type:
            return None
        observation_payload = {
            "action_type": action_type,
            **(observation_payload or {}),
            "error": str(exc),
            "failure_type": failure_type_for_exception(exc),
        }
        step.status = "failed"
        step.observation = LoopObservation.new("failed", observation_payload)
        step.checkpoint = self._checkpoint(
            loop,
            action_type,
            step.turn,
            observation_payload,
            status="failed",
            execution=self._complete_execution(step),
        )
        step.updated_at = time.time()
        return step

    def _mark_running_step_cancelled(
        self,
        loop: LoopRun,
        action_type: str,
        reason: str,
        observation_payload: dict[str, Any] | None = None,
        *,
        cancel_category: str | None = None,
    ) -> LoopStep | None:
        step = self._latest_running_step(loop)
        if step is None or step.action.type != action_type:
            return None
        payload = {
            "action_type": action_type,
            **(observation_payload or {}),
            "reason": reason or "cancelled",
            "cancel_category": normalize_cancel_category(cancel_category, reason),
        }
        step.status = "cancelled"
        step.observation = LoopObservation.new("cancelled", payload)
        step.checkpoint = self._checkpoint(
            loop,
            action_type,
            step.turn,
            payload,
            status="cancelled",
            execution=self._complete_execution(step),
        )
        step.updated_at = time.time()
        return step

    def _start_execution_lease(self, loop: LoopRun) -> dict[str, Any]:
        started_at = time.time()
        lease_seconds = self._action_lease_seconds(loop)
        return {
            "lease_id": new_id("lease"),
            "started_at": started_at,
            "heartbeat_at": started_at,
            "lease_seconds": lease_seconds,
            "lease_expires_at": started_at + lease_seconds,
            "renewal_count": 0,
        }

    def _complete_execution(self, step: LoopStep) -> dict[str, Any]:
        execution = self._execution_from_checkpoint(step)
        completed_at = time.time()
        started_at = self._float_or_none(execution.get("started_at")) or step.created_at
        execution["completed_at"] = completed_at
        execution["duration_ms"] = max(0, int(round((completed_at - started_at) * 1000)))
        return execution

    def _execution_from_checkpoint(self, step: LoopStep) -> dict[str, Any]:
        return dict(step.checkpoint.get("execution") or {})

    def _action_lease_seconds(self, loop: LoopRun) -> float:
        raw = loop.metadata.get("actionLeaseSeconds", loop.metadata.get("action_lease_seconds", 300))
        try:
            return max(0.001, float(raw))
        except (TypeError, ValueError):
            return 300.0

    def _float_or_none(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

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
        return json.dumps(self._memory_candidate_summary(loop), ensure_ascii=False, sort_keys=True)

    def _memory_candidate_summary(self, loop: LoopRun) -> dict[str, Any]:
        decisions: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        commands: list[dict[str, Any]] = []
        failure_types: list[dict[str, Any]] = []
        remediation_outcomes: list[dict[str, Any]] = []
        memory_refs: list[dict[str, Any]] = []
        for step in loop.steps:
            payload = step.observation.payload if isinstance(step.observation.payload, dict) else {}
            decision = {
                "step_id": step.step_id,
                "turn": step.turn,
                "action_type": step.action.type,
                "status": step.status,
            }
            summary = self._memory_step_summary(step)
            if summary:
                decision["summary"] = summary
            decisions.append(decision)

            for artifact in self._memory_artifacts(step, payload):
                artifacts.append(artifact)
            command = self._memory_command(step, payload)
            if command:
                commands.append(command)
            failure_type = payload.get("failure_type") or step.checkpoint.get("failure_type")
            if failure_type:
                failure_types.append({
                    "step_id": step.step_id,
                    "action_type": step.action.type,
                    "failure_type": self._memory_safe_string(failure_type),
                    "gates": self._memory_safe_list(payload.get("failed_gates") or payload.get("failedGates")),
                })
            if step.action.type == "remediation_dispatch":
                remediation_outcomes.append({
                    "step_id": step.step_id,
                    "status": step.status,
                    "summary": summary or "remediation dispatch completed",
                })
            if step.action.type == "memory_search":
                refs = [
                    self._memory_safe_string(item.get("id"))
                    for item in payload.get("results", [])
                    if isinstance(item, dict) and item.get("id")
                ]
                if refs:
                    memory_refs.append({
                        "kind": "pre_loop_search",
                        "step_id": step.step_id,
                        "memory_ids": refs[:12],
                    })
        return {
            "schema_version": "agent-loop-memory-candidate/1.0",
            "loop_id": loop.loop_id,
            "goal": self._memory_safe_string(loop.goal, limit=300),
            "project_root": self._memory_safe_string(loop.project_root, limit=500),
            "outcome": self._memory_candidate_outcome(loop),
            "decisions": decisions,
            "artifacts": artifacts,
            "commands": commands,
            "failure_types": failure_types,
            "remediation_outcomes": remediation_outcomes,
            "memory_refs": memory_refs,
        }

    def _memory_candidate_outcome(self, loop: LoopRun) -> str:
        if loop.status in TERMINAL_LOOP_STATUSES:
            return loop.status
        latest_quality = self._latest_step(loop, "quality_gate")
        if latest_quality is None:
            return loop.status
        return "failed" if self._quality_failed(latest_quality) else "passed"

    def _memory_step_summary(self, step: LoopStep) -> str | None:
        payload = step.observation.payload if isinstance(step.observation.payload, dict) else {}
        for key in ("summary", "message", "final_output", "quality", "dispatch", "status"):
            value = payload.get(key)
            if value:
                return self._memory_safe_string(value)
        return None

    def _memory_artifacts(self, step: LoopStep, payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw_artifacts = payload.get("artifacts") or payload.get("artifact_paths") or payload.get("artifactPaths") or []
        if isinstance(raw_artifacts, str):
            raw_artifacts = [raw_artifacts]
        if not isinstance(raw_artifacts, list):
            return []
        artifacts: list[dict[str, Any]] = []
        for item in raw_artifacts[:20]:
            if isinstance(item, dict):
                path = item.get("path") or item.get("file") or item.get("name")
                kind = item.get("kind") or item.get("type")
                action = item.get("action") or "mentioned"
            else:
                path = item
                kind = None
                action = "mentioned"
            if not path:
                continue
            artifact = {
                "step_id": step.step_id,
                "path": self._memory_safe_string(path, limit=500),
                "action": self._memory_safe_string(action, limit=80),
            }
            if kind:
                artifact["kind"] = self._memory_safe_string(kind, limit=80)
            artifacts.append(artifact)
        return artifacts

    def _memory_command(self, step: LoopStep, payload: dict[str, Any]) -> dict[str, Any] | None:
        command = payload.get("cmd") or payload.get("command")
        if not command:
            return None
        result = {
            "step_id": step.step_id,
            "cmd": self._memory_safe_string(command, limit=300),
        }
        exit_code = payload.get("exit_code", payload.get("exitCode"))
        if exit_code is not None:
            try:
                result["exit_code"] = int(exit_code)
            except (TypeError, ValueError):
                pass
        return result

    def _memory_safe_list(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [self._memory_safe_string(value)]
        if not isinstance(value, list):
            return []
        return [self._memory_safe_string(item) for item in value if item is not None][:20]

    def _memory_safe_string(self, value: Any, *, limit: int = 240) -> str:
        text = " ".join(str(value or "").replace("\x00", " ").split())
        lowered = text.lower()
        blocked = (
            "secret",
            "credential",
            "api key",
            "apikey",
            "token",
            "traceback",
            "stack trace",
            "screenshot",
            "temporary tool error",
        )
        if any(item in lowered for item in blocked):
            return "[redacted]"
        return text[:limit]

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
