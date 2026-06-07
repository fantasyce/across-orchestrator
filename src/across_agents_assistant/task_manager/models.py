from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Dict, Any, Set
import uuid
import time

class JobStatus(str, Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"

class TaskType(str, Enum):
    RESEARCH = "research"
    CODE_REVIEW = "code_review"
    AUTOMATION = "automation"
    SIMPLE_QA = "simple_qa"
    UNKNOWN = "unknown"


class DeliveryTaskType(str, Enum):
    FUNCTIONAL = "functional"
    ARTIFACT = "artifact"


class DeliveryMode(str, Enum):
    FUNCTIONAL = "functional"
    ARTIFACT = "artifact"
    COMPOSITE = "composite"
    LEGACY = "legacy"


class FailureType(str, Enum):
    CONFIGURATION = "configuration"
    INFRASTRUCTURE = "infrastructure"
    LLM_PROVIDER_FAILURE = "llm_provider_failure"
    PERSISTENCE_FAILURE = "persistence_failure"
    CAPABILITY_MISMATCH = "capability_mismatch"
    OUTPUT_INCOMPLETE = "output_incomplete"
    VALIDATION_FAILURE = "validation_failure"
    ACCEPTANCE_PARSE_FAILURE = "acceptance_parse_failure"
    ACCEPTANCE_REJECTED = "acceptance_rejected"
    INTEGRATION_FAILURE = "integration_failure"
    UNKNOWN = "unknown"


class RootCauseScope(str, Enum):
    CURRENT_SUBTASK = "current_subtask"
    CURRENT_WAVE = "current_wave"
    PRIOR_WAVE = "prior_wave"
    INTEGRATION = "integration"
    UNKNOWN = "unknown"


class RecommendedAction(str, Enum):
    APPROVE = "approve"
    SUBTASK_FIX = "subtask_fix"
    WAVE_FIX = "wave_fix"
    PRIOR_WAVE_FIX = "prior_wave_fix"
    REASSIGN = "reassign"
    MANUAL_REVIEW = "manual_review"
    DOWNSTREAM_REVALIDATION = "downstream_revalidation"


class WaveLifecycleStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    APPROVED = "approved"
    BLOCKED = "blocked"
    REVALIDATING = "revalidating"
    FAILED = "failed"

@dataclass
class SubTask:
    subtask_id: str
    description: str
    agent_id: str
    priority: int = 1
    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0
    dependencies: List[str] = field(default_factory=list)
    wave_number: int = 1
    error_message: Optional[str] = None
    output_file: Optional[str] = None
    duration: Optional[float] = None
    task_id: Optional[str] = None

@dataclass
class FixRound:
    round_number: int
    status: JobStatus
    agent_id: str
    fix_description: str
    error_summary: Optional[str] = None

@dataclass
class Wave:
    wave_id: str
    wave_number: int
    task_id: Optional[str] = None
    subtasks: List[SubTask] = field(default_factory=list)
    status: JobStatus = JobStatus.PENDING
    is_blocked: bool = False
    fix_rounds: List[FixRound] = field(default_factory=list)
    governance_status: str = WaveLifecycleStatus.PENDING.value
    blocked_by_wave: Optional[int] = None
    is_revalidating: bool = False
    owner_decision: Dict[str, Any] = field(default_factory=dict)

class TaskStatus(str, Enum):
    DECOMPOSING = "decomposing"
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


@dataclass
class Task:
    task_id: str
    description: str
    task_type: TaskType = TaskType.UNKNOWN
    subtasks: List[SubTask] = field(default_factory=list)
    waves: List[Wave] = field(default_factory=list)
    can_handle_directly: bool = False
    direct_response: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    fix_rounds: Dict[str, int] = field(default_factory=dict)
    project_dir: Optional[str] = None
    error: Optional[str] = None
    status: TaskStatus = TaskStatus.DECOMPOSING
    owner_session_id: Optional[str] = None
    owner_state_summary: Dict[str, Any] = field(default_factory=dict)
    last_owner_decision: Dict[str, Any] = field(default_factory=dict)
    owner_agent: Optional[str] = None
    allowed_subtask_agents: List[str] = field(default_factory=list)
    task_types: List[str] = field(default_factory=list)
    delivery_mode: str = DeliveryMode.LEGACY.value

    @staticmethod
    def new(
        description: str,
        task_type: TaskType = TaskType.UNKNOWN,
        project_dir: Optional[str] = None,
        owner_agent: Optional[str] = None,
        allowed_subtask_agents: Optional[List[str]] = None,
        task_types: Optional[List[str]] = None,
        delivery_mode: Optional[str] = None,
    ) -> Task:
        return Task(
            task_id=f"task-{uuid.uuid4().hex[:8]}",
            description=description,
            task_type=task_type,
            project_dir=project_dir,
            owner_agent=owner_agent,
            allowed_subtask_agents=list(allowed_subtask_agents or []),
            task_types=list(task_types or []),
            delivery_mode=delivery_mode or DeliveryMode.LEGACY.value,
        )

@dataclass
class Job:
    job_id: str
    subtask_id: str
    agent_id: str
    task_description: str
    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0
    logs: List[str] = field(default_factory=list)
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    attempt: int = 0
    pinned_session_id: Optional[str] = None
    failure_reason: Optional[str] = None
    result_metadata: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def new(subtask: SubTask, agent_id: str) -> Job:
        return Job(
            job_id=f"job-{uuid.uuid4().hex[:8]}",
            subtask_id=subtask.subtask_id,
            agent_id=agent_id,
            task_description=subtask.description
        )

@dataclass
class JobResult:
    job_id: str
    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    duration_sec: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ProgressUpdate:
    job_id: str
    status: JobStatus
    progress: float
    log: Optional[str] = None


@dataclass
class OrchestratorState:
    task_id: str
    fix_rounds: Dict[str, int]
    max_fix_rounds: int = 3
    acceptance_results: Dict[str, Any] = field(default_factory=dict)
    acceptance_parse_retries: Dict[str, int] = field(default_factory=dict)
    wave_acceptance_recorded: Set[int] = field(default_factory=set)
    wave_approved: Set[int] = field(default_factory=set)
    completed_subtasks: Set[str] = field(default_factory=set)
    is_integration_testing: bool = False
    strict_dependency: bool = True
    wave_gate_enabled: bool = True
    owner_session_id: Optional[str] = None
    wave_statuses: Dict[int, str] = field(default_factory=dict)
    blocked_by_wave: Dict[int, int] = field(default_factory=dict)
    revalidating_waves: Set[int] = field(default_factory=set)
    recent_acceptance_records: List[Dict[str, Any]] = field(default_factory=list)
    quality_remediation_attempts: Dict[str, int] = field(default_factory=dict)
    max_quality_remediation_attempts: int = 4
    artifact_versions: Dict[str, int] = field(default_factory=dict)
    allowed_subtask_agents: List[str] = field(default_factory=list)


@dataclass
class AcceptanceResult:
    subtask_id: str
    level1_passed: bool
    level2_passed: bool
    level1_errors: List[str] = field(default_factory=list)
    level2_feedback: Optional[str] = None
    fix_round: int = 0
    action: str = "approve"
    parse_failed: bool = False
    raw_response: Optional[str] = None
    failure_type: Optional[str] = None
    decision: str = "approve"
    root_cause_scope: str = RootCauseScope.UNKNOWN.value
    root_cause_wave: Optional[int] = None
    root_cause_artifact_ids: List[str] = field(default_factory=list)
    recommended_action: str = RecommendedAction.APPROVE.value
    preferred_agent: Optional[str] = None
    failed_checks: List[str] = field(default_factory=list)
    missing_artifacts: List[str] = field(default_factory=list)
    owner_session_id: Optional[str] = None
    investigation_level: int = 0


@dataclass
class ValidationReport:
    passed: bool
    errors: List[Any] = field(default_factory=list)


@dataclass
class DeliverableSpec:
    artifact_type: str
    required: bool = True
    path_hint: Optional[str] = None
    description: str = ""


@dataclass
class AcceptanceCheck:
    check_type: str
    description: str
    required: bool = True


@dataclass
class RequirementDeliverable:
    requirement_id: str
    artifact_type: str
    required: bool = True
    path_hint: Optional[str] = None
    description: str = ""
    source: str = "user_request"
    assigned_subtask_id: Optional[str] = None
    status: str = "unassigned"
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RequirementManifest:
    manifest_id: str
    task_id: str
    project_dir: Optional[str] = None
    deliverables: List[RequirementDeliverable] = field(default_factory=list)
    quality_checks: List[AcceptanceCheck] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @staticmethod
    def new(task_id: str, project_dir: Optional[str] = None) -> "RequirementManifest":
        return RequirementManifest(
            manifest_id=f"manifest-{uuid.uuid4().hex[:8]}",
            task_id=task_id,
            project_dir=project_dir,
        )


@dataclass
class TaskContract:
    contract_id: str
    task_id: str
    level: str
    goal: str
    subtask_id: Optional[str] = None
    wave_number: Optional[int] = None
    input_artifact_ids: List[str] = field(default_factory=list)
    expected_deliverables: List[DeliverableSpec] = field(default_factory=list)
    acceptance_checks: List[AcceptanceCheck] = field(default_factory=list)
    project_dir: Optional[str] = None
    context_mode: str = "summary"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @staticmethod
    def new(
        task_id: str,
        level: str,
        goal: str,
        subtask_id: Optional[str] = None,
        wave_number: Optional[int] = None,
        project_dir: Optional[str] = None,
        context_mode: str = "summary",
    ) -> "TaskContract":
        return TaskContract(
            contract_id=f"contract-{uuid.uuid4().hex[:8]}",
            task_id=task_id,
            level=level,
            goal=goal,
            subtask_id=subtask_id,
            wave_number=wave_number,
            project_dir=project_dir,
            context_mode=context_mode,
        )


@dataclass
class Artifact:
    artifact_id: str
    artifact_type: str
    produced_by: str
    task_id: str
    subtask_id: str
    content_ref: str
    consumed_by: List[str] = field(default_factory=list)
    schema_version: str = "1.0"
    name: Optional[str] = None
    wave_number: Optional[int] = None
    version: int = 1
    status: str = "accepted"
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_artifact_ids: List[str] = field(default_factory=list)
    supersedes_artifact_id: Optional[str] = None
    superseded_by_artifact_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class ArtifactLineage:
    artifact_id: str
    source_artifact_ids: List[str] = field(default_factory=list)
    supersedes_artifact_id: Optional[str] = None
    superseded_by_artifact_id: Optional[str] = None


@dataclass
class OwnerDecision:
    decision: str
    root_cause_scope: str = RootCauseScope.UNKNOWN.value
    root_cause_wave: Optional[int] = None
    root_cause_artifact_ids: List[str] = field(default_factory=list)
    recommended_action: str = RecommendedAction.APPROVE.value
    preferred_agent: Optional[str] = None
    failed_checks: List[str] = field(default_factory=list)
    missing_artifacts: List[str] = field(default_factory=list)
    summary: Optional[str] = None
    owner_session_id: Optional[str] = None
    investigation_level: int = 0


@dataclass
class Feedback:
    feedback_id: str
    feedback_type: str
    from_agent: str
    to_agent: str
    target: str
    observed: str
    expected: str
    severity: str = "warning"


@dataclass
class AcceptanceRecord:
    acceptance_id: str
    task_id: str
    level: str
    decision: str
    deterministic_passed: bool
    judge_passed: bool
    subtask_id: Optional[str] = None
    wave_number: Optional[int] = None
    failed_checks: List[str] = field(default_factory=list)
    missing_artifacts: List[str] = field(default_factory=list)
    feedback: Optional[str] = None
    root_cause_scope: str = RootCauseScope.UNKNOWN.value
    root_cause_wave: Optional[int] = None
    root_cause_artifact_ids: List[str] = field(default_factory=list)
    recommended_action: str = RecommendedAction.APPROVE.value
    preferred_agent: Optional[str] = None
    owner_session_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def new(
        task_id: str,
        level: str,
        decision: str,
        deterministic_passed: bool,
        judge_passed: bool,
        subtask_id: Optional[str] = None,
        wave_number: Optional[int] = None,
        failed_checks: Optional[List[str]] = None,
        missing_artifacts: Optional[List[str]] = None,
        feedback: Optional[str] = None,
        root_cause_scope: str = RootCauseScope.UNKNOWN.value,
        root_cause_wave: Optional[int] = None,
        root_cause_artifact_ids: Optional[List[str]] = None,
        recommended_action: str = RecommendedAction.APPROVE.value,
        preferred_agent: Optional[str] = None,
        owner_session_id: Optional[str] = None,
    ) -> "AcceptanceRecord":
        return AcceptanceRecord(
            acceptance_id=f"acc-{uuid.uuid4().hex[:8]}",
            task_id=task_id,
            level=level,
            decision=decision,
            deterministic_passed=deterministic_passed,
            judge_passed=judge_passed,
            subtask_id=subtask_id,
            wave_number=wave_number,
            failed_checks=failed_checks or [],
            missing_artifacts=missing_artifacts or [],
            feedback=feedback,
            root_cause_scope=root_cause_scope,
            root_cause_wave=root_cause_wave,
            root_cause_artifact_ids=root_cause_artifact_ids or [],
            recommended_action=recommended_action,
            preferred_agent=preferred_agent,
            owner_session_id=owner_session_id,
        )
