import asyncio
import logging
import os
import threading
import time
from typing import Dict, List, Optional, Callable, Any

from ..local_agent.client import UniversalAgentClient
from ..agent_bridge.bridge import AgentBridge
from ..agent_ids import LOCAL_CLI_AGENT_IDS, normalize_agent_id
from ..agent_bridge.result import SubtaskResult, ResultStatus
from ..approval.service import ApprovalService
from ..approval.executor import ToolExecutor
from ..approval.models import RiskLevel, ApprovalStatus, ApprovalRequest
from ..llm_gateway.provider_registry import get_default_provider_ids
from ..persistence.permissions import ToolPermissionStore
from ..tools.tool_registry import registry
from .models import Job, JobStatus, SubTask, Task, JobResult, ProgressUpdate
from .state import TaskState

logger = logging.getLogger("across_agents_assistant.task_manager")

# Max concurrent jobs to prevent thread explosion
MAX_CONCURRENT_JOBS = 3

class TaskDispatcher:
    """
    Dispatches subtasks to agents and manages job execution.

    Uses a thread pool to execute agent calls without blocking the main async loop.
    Limits concurrent jobs to prevent resource exhaustion.
    """

    def __init__(self, state: TaskState, local_agent_client: UniversalAgentClient,
                 permission_store: Optional[ToolPermissionStore] = None,
                 llm_gateway: Any = None):
        self._state = state
        self._local_agent = local_agent_client
        self._llm_gateway = llm_gateway
        # Use AgentBridge for agent communication (with optional LLMGateway for cloud agents)
        self._approval_service = ApprovalService(permission_store=permission_store)
        self._tool_executor = ToolExecutor(registry, self._approval_service)
        self._agent_bridge = AgentBridge(
            local_agent_client,
            llm_gateway=llm_gateway,
            tool_executor=self._tool_executor,
        )
        self._job_threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._progress_callbacks: List[Callable[[ProgressUpdate], None]] = []
        # Semaphore to limit concurrent job execution
        self._job_semaphore = threading.Semaphore(MAX_CONCURRENT_JOBS)

    def _get_valid_agents(self) -> List[str]:
        available = []
        from ..local_agent_health import detect_local_agents

        local_health = detect_local_agents()
        for agent_id in LOCAL_CLI_AGENT_IDS:
            health = local_health.get(agent_id) or {}
            is_available = bool(health.get("available"))
            reason = health.get("error") or health.get("status") or "unknown"
            logger.info(
                "Agent readiness check: %s -> %s%s",
                agent_id,
                "available" if is_available else "unavailable",
                f" ({reason})" if not is_available else "",
            )
            if is_available:
                available.append(agent_id)
        for llm_id in get_default_provider_ids():
            is_configured = self._is_cloud_llm_configured(llm_id)
            logger.info(f"Cloud LLM availability check: {llm_id} -> {'configured' if is_configured else 'not configured'}")
            if is_configured:
                available.append(llm_id)
        logger.info(f"Valid agents: {available}")
        return available

    def _is_cloud_llm_configured(self, llm_id: str) -> bool:
        """Check if a cloud LLM has a real API key configured."""
        try:
            if not self._llm_gateway:
                return False
            adapter = self._llm_gateway._adapters.get(llm_id)
            if adapter:
                return adapter.is_available()
            return False
        except Exception:
            return False

    @staticmethod
    def _timeout_from_env(env_name: str, default: float) -> float:
        raw = os.environ.get(env_name)
        if raw is None or not raw.strip():
            return default
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        logger.warning("Ignoring invalid timeout value for %s: %r", env_name, raw)
        return default

    def _agent_timeout_for_subtask(self, subtask: SubTask) -> float:
        """Return the bridge timeout for one subtask.

        Quality remediation runs after normal delivery and should fail fast
        enough for the owner loop to continue repairing instead of waiting for
        the full worker budget.
        """
        subtask_id = str(getattr(subtask, "subtask_id", "") or "")
        if subtask_id.startswith("st-quality-"):
            return self._timeout_from_env("ACROSS_AGENTS_QUALITY_REMEDIATION_TIMEOUT", 120.0)
        return self._timeout_from_env("ACROSS_AGENTS_AGENT_TIMEOUT", 600.0)

    def add_progress_callback(self, callback: Callable[[ProgressUpdate], None]) -> None:
        """Add a callback for progress updates."""
        self._progress_callbacks.append(callback)

    def dispatch_subtask(self, subtask: SubTask) -> Optional[Job]:
        """
        Synchronously dispatch a subtask to the appropriate agent.

        Returns the created Job, or None if dispatch failed.
        """
        subtask.agent_id = normalize_agent_id(subtask.agent_id) or subtask.agent_id
        logger.info(f"Dispatching subtask {subtask.subtask_id} to agent {subtask.agent_id}")
        # Validate agent upfront
        valid_agents = self._get_valid_agents()
        if subtask.agent_id not in valid_agents:
            logger.error(f"Agent '{subtask.agent_id}' not available for subtask {subtask.subtask_id}, available: {valid_agents}")
            task = self._state.get_task_by_subtask(subtask.subtask_id)
            if task:
                self._state.update_subtask_status(task.task_id, subtask.subtask_id, JobStatus.FAILED)
            return None

        job = self._state.create_job(subtask)
        logger.info(f"Created job {job.job_id} for subtask {subtask.subtask_id}")

        task = self._state.get_task_by_subtask(subtask.subtask_id)
        if task:
            self._state.update_subtask_status(task.task_id, subtask.subtask_id, JobStatus.DISPATCHED)

        # Transition to DISPATCHED immediately before starting thread
        self._state.update_job_status(job.job_id, JobStatus.DISPATCHED)
        self._notify_progress(job.job_id, JobStatus.DISPATCHED, 0.0, "Dispatched")

        def run_job():
            with self._job_semaphore:
                try:
                    # Transition to RUNNING when agent starts executing
                    self._state.update_job_status(job.job_id, JobStatus.RUNNING)
                    task_for_status = self._state.get_task_by_subtask(subtask.subtask_id)
                    if task_for_status:
                        self._state.update_subtask_status(task_for_status.task_id, subtask.subtask_id, JobStatus.RUNNING)
                    self._notify_progress(job.job_id, JobStatus.RUNNING, 0.0, "Started")

                    # Execute based on agent type
                    if subtask.agent_id in valid_agents:
                        result = self._execute_agent_job(job, subtask, subtask.agent_id)
                    else:
                        result = JobResult(job_id=job.job_id, success=False, error=f"Unknown agent: {subtask.agent_id}")

                    if result.success:
                        self._state.complete_job(
                            job.job_id,
                            success=True,
                            output=result.output,
                            metadata=result.metadata,
                        )
                        self._notify_progress(job.job_id, JobStatus.COMPLETED, 1.0, "Completed")
                    else:
                        self._state.complete_job(
                            job.job_id,
                            success=False,
                            error=result.error,
                            metadata=result.metadata,
                        )
                        self._notify_progress(job.job_id, JobStatus.FAILED, job.progress, f"Failed: {result.error}")

                except Exception as e:
                    logger.error(f"Job {job.job_id} failed with exception: {e}")
                    self._state.complete_job(job.job_id, success=False, error=str(e))
                    self._notify_progress(job.job_id, JobStatus.FAILED, 0.0, f"Error: {e}")
                finally:
                    with self._lock:
                        self._job_threads.pop(job.job_id, None)

        thread = threading.Thread(target=run_job, daemon=True)
        with self._lock:
            self._job_threads[job.job_id] = thread

        thread.start()
        return job

    def _execute_agent_job(self, job: Job, subtask: SubTask, target_agent: str) -> JobResult:
        """Execute a job using the specified agent."""
        logger.info(f"_execute_agent_job: starting job {job.job_id} for subtask {subtask.subtask_id} with agent {target_agent}")
        try:
            self._state.update_job_progress(job.job_id, 0.1, f"Connecting to {target_agent} agent...")
            self._notify_progress(job.job_id, JobStatus.RUNNING, 0.1, "Connecting...")

            # Build context with project_dir if available
            task = self._state.get_task_by_subtask(subtask.subtask_id)
            context = {}
            project_dir = None
            if task and task.project_dir:
                context["project_dir"] = task.project_dir
                project_dir = task.project_dir
            if task:
                context["task_id"] = task.task_id
                allowed_writable_files = self._allowed_writable_files_for_subtask(task, subtask)
                if allowed_writable_files:
                    context["allowed_writable_files"] = allowed_writable_files
                    context["writable_scope_reason"] = "requirement_manifest_assignment"
            context["subtask_id"] = subtask.subtask_id
            context["job_id"] = job.job_id
            context["user_description"] = subtask.description

            agent_timeout = self._agent_timeout_for_subtask(subtask)
            response = self._agent_bridge.invoke(
                agent_id=target_agent,
                message=subtask.description,
                context=context,
                timeout=agent_timeout,
                project_dir=project_dir
            )
            logger.info(f"_execute_agent_job: agent {target_agent} responded for job {job.job_id}, success={response.is_success}, output_len={len(response.output) if response.output else 0}")

            self._state.update_job_progress(job.job_id, 0.9, "Processing response...")
            self._notify_progress(job.job_id, JobStatus.RUNNING, 0.9, "Processing...")

            if response.is_success:
                return JobResult(
                    job_id=job.job_id,
                    success=True,
                    output=response.output,
                    metadata=dict(response.metadata or {}),
                )
            else:
                return JobResult(
                    job_id=job.job_id,
                    success=False,
                    error=response.error,
                    metadata=dict(response.metadata or {}),
                )

        except Exception as e:
            return JobResult(job_id=job.job_id, success=False, error=str(e))

    def _allowed_writable_files_for_subtask(self, task: Task, subtask: SubTask) -> List[str]:
        """Return project-relative files this subtask is explicitly assigned to write.

        The manifest assignment is the strongest signal because it is created
        from the user's requested deliverables after coverage matching.  It lets
        a subtask read dependency outputs while preventing it from creating
        downstream or unrelated files.
        """
        allowed: List[str] = []

        def add(path_hint: Any) -> None:
            normalized = self._normalize_project_relative_path(path_hint)
            if normalized and normalized not in allowed:
                allowed.append(normalized)

        manifest = self._state.get_requirement_manifest(task.task_id)
        for item in (manifest or {}).get("deliverables", []) or []:
            if item.get("assigned_subtask_id") != subtask.subtask_id:
                continue
            add(item.get("path_hint"))

        if allowed:
            return allowed

        contract = self._state.get_contract_by_subtask(task.task_id, subtask.subtask_id)
        for item in (contract or {}).get("expected_deliverables", []) or []:
            artifact_type = str(item.get("artifact_type") or "").lower()
            if artifact_type in {"file", "documentation", "test_source", "api_service_source", "frontend_source"}:
                add(item.get("path_hint"))
        return allowed

    @staticmethod
    def _normalize_project_relative_path(path_hint: Any) -> Optional[str]:
        text = str(path_hint or "").replace("\\", "/").strip()
        if not text:
            return None
        if os.path.isabs(text):
            return None
        normalized = os.path.normpath(text).replace("\\", "/")
        if normalized in {"", "."} or normalized.startswith("../") or normalized == "..":
            return None
        return normalized.strip("/")

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a running job."""
        job = self._state.get_job(job_id)
        if not job:
            return False
        if job.status not in (JobStatus.PENDING, JobStatus.DISPATCHED, JobStatus.RUNNING):
            return False

        result = self._state.cancel_job(job_id, error="Cancelled by user")
        if result:
            self._notify_progress(job_id, JobStatus.CANCELLED, job.progress, "Cancelled")
            return True
        return False

    def get_active_jobs(self) -> List[Job]:
        """Get all currently running jobs."""
        jobs = self._state.get_all_jobs()
        return [j for j in jobs if j.status == JobStatus.RUNNING]

    def get_job(self, job_id: str) -> Optional[Job]:
        """Get a specific job by ID."""
        return self._state.get_job(job_id)

    def recover_orphaned_jobs(self) -> None:
        """Recover jobs that were in DISPATCHED or RUNNING state after a backend restart."""
        orphaned = self._state.get_jobs_in_status([JobStatus.DISPATCHED, JobStatus.RUNNING])
        if not orphaned:
            return

        logger.info(f"Recovering {len(orphaned)} orphaned jobs")

        for job in orphaned:
            if job.status == JobStatus.DISPATCHED:
                # Job was dispatched but agent had not started → re-dispatch safely
                logger.info(f"Re-dispatching orphaned DISPATCHED job {job.job_id}")
                subtask = next(
                    (st for task in self._state.get_all_tasks()
                     for st in task.subtasks if st.subtask_id == job.subtask_id),
                    None
                )
                if subtask:
                    self.dispatch_subtask(subtask)
                else:
                    self._state.complete_job(
                        job.job_id,
                        success=False,
                        error="orphan_recovery: subtask not found"
                    )

            elif job.status == JobStatus.RUNNING:
                if job.pinned_session_id:
                    # Attempt to resume from pinned session
                    logger.info(
                        f"Attempting to resume orphaned RUNNING job {job.job_id} "
                        f"with session {job.pinned_session_id}"
                    )
                    try:
                        # Resume via AgentBridge
                        self._agent_bridge.invoke(
                            agent_id=job.agent_id,
                            message="",
                            context={"resume_session_id": job.pinned_session_id},
                            timeout=30.0
                        )
                        # If resume succeeds, the job continues; otherwise it will fail
                    except Exception as e:
                        logger.error(f"Failed to resume orphaned job {job.job_id}: {e}")
                        job.failure_reason = "orphan_resume_failed"
                        self._state.complete_job(
                            job.job_id,
                            success=False,
                            error=f"orphan_resume_failed: {e}"
                        )
                else:
                    # No pinned session → mark as failed
                    logger.warning(
                        f"Orphaned RUNNING job {job.job_id} has no pinned_session_id, marking FAILED"
                    )
                    job.failure_reason = "orphan_recovery"
                    self._state.complete_job(
                        job.job_id,
                        success=False,
                        error="orphan_recovery: no pinned session available"
                    )

    def _notify_progress(self, job_id: str, status: JobStatus, progress: float, log: Optional[str] = None) -> None:
        """Notify all progress callbacks."""
        update = ProgressUpdate(job_id=job_id, status=status, progress=progress, log=log)
        for callback in self._progress_callbacks:
            try:
                callback(update)
            except Exception as e:
                logger.error(f"Progress callback error: {e}")

    def get_pending_approvals(self) -> List[ApprovalRequest]:
        """获取所有待审批请求"""
        return self._approval_service.get_pending_requests()

    def approve_request(self, request_id: str) -> bool:
        """批准审批请求"""
        return self._approval_service.approve(request_id)

    def reject_request(self, request_id: str) -> bool:
        """拒绝审批请求"""
        return self._approval_service.reject(request_id)

    def always_allow_tool(self, request_id: str) -> bool:
        """始终允许工具"""
        return self._approval_service.always_allow(request_id)

    def add_approval_callback(self, callback) -> None:
        """添加审批状态变化回调"""
        self._approval_service.add_callback(callback)

    def is_tool_auto_approved(self, tool_name: str) -> bool:
        """检查工具是否自动批准"""
        return self._approval_service.is_auto_approved(tool_name)
