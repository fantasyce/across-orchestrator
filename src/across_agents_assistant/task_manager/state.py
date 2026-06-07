import logging
import json
import os
import re
import threading
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from .models import (
    AcceptanceRecord,
    Artifact,
    Task,
    TaskContract,
    SubTask,
    Job,
    JobResult,
    JobStatus,
    TaskStatus,
    TaskType,
    ProgressUpdate,
)
from across_agents_assistant.workspace_hygiene import is_workspace_noise_path

logger = logging.getLogger("across_agents_assistant.task_manager")


def _is_original_business_subtask_id(subtask_id: str) -> bool:
    if subtask_id.endswith("-decompose"):
        return False
    if subtask_id.startswith("st-quality-"):
        return False
    if "-integration-fix" in subtask_id:
        return False
    return re.sub(r"-(?:fix-\d+|v\d+)$", "", subtask_id) == subtask_id


def _canonical_subtask_id(subtask_id: str) -> str:
    """Strip all remediation suffixes to find the canonical business subtask ID.

    Handles ``-fix-N``, ``-vN``, and nested combinations (e.g. ``st-a-v2-fix-1``).
    Mirrors the same helper in ``TaskOrchestrator`` because ``TaskState`` should not
    import it.
    """
    base = subtask_id
    while True:
        new_base = re.sub(r"-(?:fix-\d+|v\d+)$", "", base)
        if new_base == base:
            return base
        base = new_base


def _status_value(status: Any) -> str:
    return str(getattr(status, "value", status) or "").lower()


def _is_remediation_subtask_id(subtask_id: str) -> bool:
    if subtask_id.endswith("-decompose"):
        return False
    if subtask_id.startswith("wave-"):
        return True
    return not _is_original_business_subtask_id(subtask_id)


def _canonical_project_dir(project_dir: Optional[str]) -> Optional[str]:
    if not project_dir:
        return project_dir
    return os.path.realpath(project_dir)


# Optional import for type hints only
try:
    from typing import TYPE_CHECKING
except ImportError:
    TYPE_CHECKING = False

@dataclass
class TaskState:
    """
    Thread-safe in-memory task state management with persistence.

    All access is protected by a reentrant lock to support
    nested calls from the same thread.
    """
    _tasks: Dict[str, Task] = field(default_factory=dict)
    _jobs: Dict[str, Job] = field(default_factory=dict)
    _subtask_to_job: Dict[str, str] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _progress_callbacks: List = field(default_factory=list)
    _is_paused: Dict[str, bool] = field(default_factory=dict)
    _persistence: Optional = field(default=None, repr=False)
    _requirement_manifests: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def _normalize_delivery_task_types(task_types: Optional[List[str]]) -> tuple:
        allowed = {"functional", "artifact"}
        values = []
        for item in task_types or []:
            value = str(item).strip().lower()
            if not value:
                continue
            if value not in allowed:
                raise ValueError(f"Unsupported task type: {value}")
            if value not in values:
                values.append(value)
        if not values:
            return [], "legacy"
        mode = values[0] if len(values) == 1 else "composite"
        return values, mode

    def set_persistence(self, persistence) -> None:
        """Set the persistence service for task state synchronization."""
        self._persistence = persistence

    def _manifest_to_dict(self, manifest: Any) -> Dict[str, Any]:
        """Serialize a RequirementManifest to a plain dict for persistence."""
        if isinstance(manifest, dict):
            return manifest
        import dataclasses
        return {
            "manifest_id": manifest.manifest_id,
            "task_id": manifest.task_id,
            "project_dir": manifest.project_dir,
            "deliverables": [dataclasses.asdict(d) for d in manifest.deliverables],
            "quality_checks": [dataclasses.asdict(c) for c in manifest.quality_checks],
            "created_at": manifest.created_at,
            "updated_at": manifest.updated_at,
        }

    def save_requirement_manifest(self, manifest: Any) -> None:
        """Persist a RequirementManifest and keep an in-memory copy.

        Accepts both a ``RequirementManifest`` dataclass and a plain dict.
        """
        if hasattr(manifest, "__dataclass_fields__"):
            payload = self._manifest_to_dict(manifest)
        else:
            payload = dict(manifest)
        self._requirement_manifests[payload["task_id"]] = payload
        if self._persistence:
            self._persistence.save_requirement_manifest(payload)

    def get_requirement_manifest(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Return the serialized manifest for *task_id*, from persistence or memory."""
        if self._persistence:
            persisted = self._persistence.get_requirement_manifest(task_id)
            if persisted:
                return persisted
        manifest = self._requirement_manifests.get(task_id)
        if manifest:
            return self._manifest_to_dict(manifest)
        return None

    def save_delivery_contract(self, contract: Dict[str, Any]) -> None:
        if self._persistence:
            self._persistence.save_delivery_contract(contract)

    def get_delivery_contract(self, task_id: str) -> Optional[Dict[str, Any]]:
        if not self._persistence:
            return None
        return self._persistence.get_delivery_contract(task_id)

    def _persist_task(self, task: Task) -> None:
        """Persist task to database."""
        if self._persistence is None:
            return
        try:
            self._persistence.save_task({
                'task_id': task.task_id,
                'description': task.description,
                'task_type': task.task_type.value,
                'status': task.status.value,
                'project_dir': task.project_dir,
                'error': task.error,
                'can_handle_directly': task.can_handle_directly,
                'direct_response': task.direct_response,
                'progress': self.get_task_progress(task.task_id),
                'completed_count': sum(
                    1
                    for st in task.subtasks
                    if st.status == JobStatus.COMPLETED and _is_original_business_subtask_id(st.subtask_id)
                ),
                'total_count': len([
                    st for st in task.subtasks if _is_original_business_subtask_id(st.subtask_id)
                ]),
                'owner_agent': task.owner_agent,
                'owner_session_id': task.owner_session_id,
                'allowed_subtask_agents': task.allowed_subtask_agents,
                'owner_state_summary': task.owner_state_summary,
                'last_owner_decision': task.last_owner_decision,
                'task_types': list(getattr(task, 'task_types', []) or []),
                'delivery_mode': getattr(task, 'delivery_mode', 'legacy') or 'legacy',
                'is_paused': self._is_paused.get(task.task_id, False),
                'created_at': task.created_at,
                'updated_at': task.updated_at
            })
        except Exception as e:
            logger.warning(f"Failed to persist task {task.task_id}: {e}")

    def _persist_subtask(self, subtask: SubTask) -> None:
        """Persist subtask to database."""
        if self._persistence is None:
            return
        try:
            self._persistence.save_subtask({
                'subtask_id': subtask.subtask_id,
                'task_id': subtask.task_id,
                'description': subtask.description,
                'agent_id': subtask.agent_id,
                'priority': subtask.priority,
                'status': subtask.status.value,
                'progress': subtask.progress,
                'wave_number': getattr(subtask, 'wave_number', 1),
                'dependencies': subtask.dependencies,
                'error_message': subtask.error_message,
                'output_file': subtask.output_file,
                'duration': subtask.duration,
                'fix_plan': getattr(subtask, 'fix_plan', None),
                'is_fix_round': "-fix-" in subtask.subtask_id,
                'original_subtask_id': subtask.subtask_id.split("-fix-")[0] if "-fix-" in subtask.subtask_id else None,
                'created_at': time.time()
            })
        except Exception as e:
            logger.warning(f"Failed to persist subtask {subtask.subtask_id}: {e}")

    def _persist_job(self, job: Job) -> None:
        """Persist job to database."""
        if self._persistence is None:
            return
        try:
            self._persistence.save_job({
                'job_id': job.job_id,
                'subtask_id': job.subtask_id,
                'agent_id': job.agent_id,
                'task_description': job.task_description,
                'status': job.status.value,
                'progress': job.progress,
                'result': job.result,
                'error': job.error,
                'logs': job.logs,
                'created_at': job.created_at,
                'started_at': getattr(job, 'started_at', None),
                'completed_at': getattr(job, 'completed_at', None),
                'attempt': getattr(job, 'attempt', 0),
                'pinned_session_id': getattr(job, 'pinned_session_id', None),
                'failure_reason': getattr(job, 'failure_reason', None)
            })
        except Exception as e:
            logger.warning(f"Failed to persist job {job.job_id}: {e}")

    def _persist_wave(self, wave) -> None:
        """Persist wave to database."""
        if self._persistence is None:
            return
        try:
            self._persistence.save_wave({
                'wave_id': wave.wave_id,
                'task_id': wave.task_id,
                'wave_number': wave.wave_number,
                'status': wave.status.value,
                'is_blocked': wave.is_blocked,
                'governance_status': getattr(wave, 'governance_status', 'pending'),
                'blocked_by_wave': getattr(wave, 'blocked_by_wave', None),
                'is_revalidating': getattr(wave, 'is_revalidating', False),
                'owner_decision': getattr(wave, 'owner_decision', {}),
            })
        except Exception as e:
            logger.warning(f"Failed to persist wave {wave.wave_id}: {e}")

    def save_task_contract(self, contract: TaskContract) -> None:
        """Persist a task contract if persistence is configured."""
        if self._persistence is None:
            return
        try:
            self._persistence.save_task_contract({
                'contract_id': contract.contract_id,
                'task_id': contract.task_id,
                'subtask_id': contract.subtask_id,
                'wave_number': contract.wave_number,
                'level': contract.level,
                'goal': contract.goal,
                'input_artifact_ids': contract.input_artifact_ids,
                'expected_deliverables': [
                    {
                        'artifact_type': item.artifact_type,
                        'required': item.required,
                        'path_hint': item.path_hint,
                        'description': item.description,
                    }
                    for item in contract.expected_deliverables
                ],
                'acceptance_checks': [
                    {
                        'check_type': item.check_type,
                        'description': item.description,
                        'required': item.required,
                    }
                    for item in contract.acceptance_checks
                ],
                'project_dir': contract.project_dir,
                'context_mode': contract.context_mode,
                'created_at': contract.created_at,
                'updated_at': contract.updated_at,
            })
        except Exception as e:
            logger.warning(f"Failed to persist contract {contract.contract_id}: {e}")

    def get_contract_by_subtask(self, task_id: str, subtask_id: str) -> Optional[Dict[str, Any]]:
        """Get the contract for a specific subtask, or None if not found."""
        if self._persistence is None:
            return None
        try:
            contracts = self._persistence.get_task_contracts(task_id)
            for c in contracts:
                if c.get('subtask_id') == subtask_id:
                    return c
        except Exception as e:
            logger.warning(f"Failed to get contract for {subtask_id}: {e}")
        return None

    def get_task_contracts(self, task_id: str) -> List[Dict[str, Any]]:
        """Get all contracts for a task, ordered by level and created_at."""
        if self._persistence is None:
            return []
        try:
            return self._persistence.get_task_contracts(task_id)
        except Exception as e:
            logger.warning(f"Failed to get contracts for task {task_id}: {e}")
        return []

    def save_artifact_record(self, artifact: Artifact) -> None:
        """Persist an artifact record if persistence is configured."""
        if self._persistence is None:
            return
        try:
            self._persistence.save_artifact_record({
                'artifact_id': artifact.artifact_id,
                'task_id': artifact.task_id,
                'subtask_id': artifact.subtask_id,
                'wave_number': artifact.wave_number,
                'name': artifact.name,
                'artifact_type': artifact.artifact_type,
                'version': artifact.version,
                'status': artifact.status,
                'content_ref': artifact.content_ref,
                'produced_by': artifact.produced_by,
                'schema_version': artifact.schema_version,
                'metadata': artifact.metadata,
                'source_artifact_ids': getattr(artifact, 'source_artifact_ids', []),
                'supersedes_artifact_id': getattr(artifact, 'supersedes_artifact_id', None),
                'superseded_by_artifact_id': getattr(artifact, 'superseded_by_artifact_id', None),
                'created_at': artifact.created_at,
            })
        except Exception as e:
            logger.warning(f"Failed to persist artifact {artifact.artifact_id}: {e}")

    def update_artifact_records_for_subtask(
        self,
        task_id: str,
        subtask_id: str,
        status: str,
        current_status: Optional[str] = None,
    ) -> None:
        """Update persisted artifact lifecycle state for one subtask."""
        if self._persistence is None:
            return
        try:
            self._persistence.update_artifact_records_for_subtask(
                task_id=task_id,
                subtask_id=subtask_id,
                status=status,
                current_status=current_status,
            )
        except Exception as e:
            logger.warning(
                f"Failed to update artifact status for task={task_id} subtask={subtask_id}: {e}"
            )

    def set_task_status(self, task_id: str, status: TaskStatus, error: Optional[str] = None) -> bool:
        """Set task status without mutating subtask runtime state."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.status = status
            if error is not None:
                task.error = error
            task.updated_at = time.time()
            self._persist_task(task)
            return True

    def save_acceptance_record(self, record: AcceptanceRecord) -> None:
        """Persist an acceptance record if persistence is configured."""
        if self._persistence is None:
            return
        try:
            self._persistence.save_acceptance_record({
                'acceptance_id': record.acceptance_id,
                'task_id': record.task_id,
                'subtask_id': record.subtask_id,
                'wave_number': record.wave_number,
                'level': record.level,
                'decision': record.decision,
                'deterministic_passed': record.deterministic_passed,
                'judge_passed': record.judge_passed,
                'failed_checks': record.failed_checks,
                'missing_artifacts': record.missing_artifacts,
                'feedback': record.feedback,
                'root_cause_scope': getattr(record, 'root_cause_scope', 'unknown'),
                'root_cause_wave': getattr(record, 'root_cause_wave', None),
                'root_cause_artifact_ids': getattr(record, 'root_cause_artifact_ids', []),
                'recommended_action': getattr(record, 'recommended_action', 'approve'),
                'preferred_agent': getattr(record, 'preferred_agent', None),
                'owner_session_id': getattr(record, 'owner_session_id', None),
                'created_at': record.created_at,
            })
        except Exception as e:
            logger.warning(f"Failed to persist acceptance record {record.acceptance_id}: {e}")

    def create_task(
        self,
        description: str,
        task_type: TaskType = TaskType.UNKNOWN,
        project_dir: Optional[str] = None,
        owner_agent: Optional[str] = None,
        allowed_subtask_agents: Optional[List[str]] = None,
        task_types: Optional[List[str]] = None,
        delivery_mode: Optional[str] = None,
    ) -> Task:
        with self._lock:
            normalized_task_types, inferred_delivery_mode = self._normalize_delivery_task_types(task_types)
            project_dir = _canonical_project_dir(project_dir)
            task = Task.new(
                description=description,
                task_type=task_type,
                project_dir=project_dir,
                owner_agent=owner_agent,
                allowed_subtask_agents=allowed_subtask_agents,
                task_types=normalized_task_types,
                delivery_mode=delivery_mode or inferred_delivery_mode,
            )
            self._tasks[task.task_id] = task
            self._persist_task(task)
            return task

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def get_all_tasks(self) -> List[Task]:
        with self._lock:
            return list(self._tasks.values())

    def add_subtask(self, task_id: str, description: str, agent_id: str, priority: int = 1, dependencies: List[str] = None, subtask_id: str = None) -> Optional[SubTask]:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None

            # N4 Fix: Duplicate subtask ID protection
            final_subtask_id = subtask_id or f"st-{uuid.uuid4().hex[:8]}"
            if any(st.subtask_id == final_subtask_id for st in task.subtasks):
                logger.error(f"Duplicate subtask_id {final_subtask_id} for task {task_id}, rejecting")
                return None

            subtask = SubTask(
                task_id=task_id,
                subtask_id=final_subtask_id,
                description=description,
                agent_id=agent_id,
                priority=priority,
                dependencies=dependencies or []
            )
            task.subtasks.append(subtask)
            task.updated_at = time.time()
            self._persist_subtask(subtask)
            self._persist_task(task)
            return subtask

    def delete_subtask(self, task_id: str, subtask_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.subtasks = [st for st in task.subtasks if st.subtask_id != subtask_id]
            task.updated_at = time.time()
            if self._persistence:
                try:
                    self._persistence.delete_subtask(subtask_id)
                except Exception as e:
                    logger.warning(f"Failed to delete subtask {subtask_id} from persistence: {e}")
            self._persist_task(task)
            return True

    def update_subtask_status(self, task_id: str, subtask_id: str, status: JobStatus) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            for st in task.subtasks:
                if st.subtask_id == subtask_id:
                    old_status = st.status
                    st.status = status
                    if status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                        st.duration = time.time() - task.created_at
                    self._persist_subtask(st)
                    self._persist_task(task)
                    task.updated_at = time.time()
                    logger.info(f"Subtask {subtask_id} status changed: {old_status} -> {status} (task={task_id})")
                    break
            else:
                return False
        self.update_wave_status(task_id)
        return True

    def get_task_progress(self, task_id: str) -> float:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or not task.subtasks:
                return 0.0
            original = [st for st in task.subtasks if _is_original_business_subtask_id(st.subtask_id)]
            if not original:
                return 0.0
            completed = sum(1 for st in original if st.status == JobStatus.COMPLETED)
            return completed / len(original)

    def create_job(self, subtask: SubTask) -> Job:
        with self._lock:
            job = Job.new(subtask=subtask, agent_id=subtask.agent_id)
            self._jobs[job.job_id] = job
            self._subtask_to_job[subtask.subtask_id] = job.job_id
            self._persist_job(job)
            logger.info(f"Created job {job.job_id} for subtask {subtask.subtask_id} (agent={subtask.agent_id})")
            return job

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def get_job_by_subtask(self, subtask_id: str) -> Optional[Job]:
        with self._lock:
            job_id = self._subtask_to_job.get(subtask_id)
            if job_id:
                return self._jobs.get(job_id)
            return None

    def get_all_jobs(self) -> List[Job]:
        with self._lock:
            return list(self._jobs.values())

    def update_job_progress(self, job_id: str, progress: float, log: Optional[str] = None) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job.progress = min(1.0, max(0.0, progress))
            if log:
                job.logs.append(log)
            # Only auto-transition from PENDING to RUNNING (not from DISPATCHED)
            if job.status == JobStatus.PENDING and progress > 0:
                job.status = JobStatus.RUNNING
                job.started_at = time.time()
            self._persist_job(job)
            return job

    def update_job_status(self, job_id: str, status: JobStatus) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job.status = status
            if status == JobStatus.RUNNING and job.started_at is None:
                job.started_at = time.time()
            self._persist_job(job)
            return job

    def get_jobs_in_status(self, statuses: List[JobStatus]) -> List[Job]:
        """Get all jobs matching any of the given statuses."""
        with self._lock:
            return [j for j in self._jobs.values() if j.status in statuses]

    def complete_job(
        self,
        job_id: str,
        success: bool,
        output: Optional[str] = None,
        error: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> Optional[JobResult]:
        task_id_to_update = None
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job.status = JobStatus.COMPLETED if success else JobStatus.FAILED
            job.completed_at = time.time()
            job.result = output
            job.error = error
            job.result_metadata = dict(metadata or {})
            job.progress = 1.0 if success else job.progress

            duration = None
            if job.started_at and job.completed_at:
                duration = job.completed_at - job.started_at

            for task in self._tasks.values():
                for st in task.subtasks:
                    if st.subtask_id == job.subtask_id:
                        st.status = job.status
                        st.progress = job.progress
                        if not success and error:
                            st.error_message = error
                        if duration is not None:
                            st.duration = duration
                        if success:
                            extracted = self._resolve_output_file(
                                output=output,
                                metadata=job.result_metadata,
                                project_dir=task.project_dir,
                                task_description=st.description,
                            )
                            if extracted:
                                st.output_file = extracted
                        task.updated_at = time.time()
                        task_id_to_update = task.task_id
                        self._persist_subtask(st)
                        self._persist_task(task)
                        logger.info(f"Completed job {job_id} for subtask {job.subtask_id}: success={success}, duration={duration:.1f}s" if duration else f"Completed job {job_id} for subtask {job.subtask_id}: success={success}")

            result = JobResult(
                job_id=job_id,
                success=success,
                output=output,
                error=error,
                duration_sec=duration,
                metadata=job.result_metadata,
            )

            self._persist_job(job)

        if task_id_to_update:
            self.update_wave_status(task_id_to_update)
        return result

    @staticmethod
    def _is_valid_output_file(candidate: str, project_dir: Optional[str] = None) -> bool:
        if not candidate:
            return False
        resolved = os.path.realpath(candidate)
        if not os.path.isfile(resolved):
            return False
        if is_workspace_noise_path(resolved, project_dir):
            return False
        if project_dir:
            project_root = os.path.realpath(project_dir)
            try:
                if os.path.commonpath([project_root, resolved]) != project_root:
                    return False
            except ValueError:
                return False
        return True

    @staticmethod
    def _extract_output_file(
        output: str,
        project_dir: Optional[str] = None,
        task_description: Optional[str] = None,
    ) -> Optional[str]:
        import re

        candidates: List[str] = []
        patterns = [
            r'(?:written to|saved to|created at|created|output file[:\s]+)\s*`?([^`\s]+\.\w+)`?',
            r'`((?:/[^`\n]+|[^`/\n]+\.\w+))`',
            r'(/(?:Users|home|tmp|var|etc)/[^\s`]+\.\w+)',
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, output, re.IGNORECASE):
                candidate = match.group(1).strip().rstrip(".,:;!?)]}")
                if candidate not in candidates:
                    candidates.append(candidate)

        if task_description:
            description_patterns = [
                r'`([^`\n]+\.\w+)`',
                r'([A-Za-z0-9_.-]+\.\w+)',
            ]
            for pattern in description_patterns:
                for match in re.finditer(pattern, task_description):
                    candidate = match.group(1).strip().rstrip(".,:;!?)]}")
                    if candidate not in candidates:
                        candidates.append(candidate)

        for candidate in candidates:
            resolved = candidate
            if not os.path.isabs(resolved):
                if not project_dir:
                    continue
                resolved = os.path.join(project_dir, resolved)
            resolved = os.path.realpath(resolved)
            if TaskState._is_valid_output_file(resolved, project_dir=project_dir):
                return resolved
        return None

    @classmethod
    def _resolve_output_file(
        cls,
        output: Optional[str],
        metadata: Optional[Dict] = None,
        project_dir: Optional[str] = None,
        task_description: Optional[str] = None,
    ) -> Optional[str]:
        metadata = metadata or {}
        for key in ("created_files", "modified_files"):
            for candidate in metadata.get(key, []) or []:
                resolved = candidate
                if not os.path.isabs(resolved):
                    if not project_dir:
                        continue
                    resolved = os.path.join(project_dir, resolved)
                resolved = os.path.realpath(resolved)
                if cls._is_valid_output_file(resolved, project_dir=project_dir):
                    return resolved

        if output:
            return cls._extract_output_file(
                output,
                project_dir=project_dir,
                task_description=task_description,
            )
        return None

    def cancel_task(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            for st in task.subtasks:
                st.status = JobStatus.CANCELLED
                st.error_message = st.error_message or "Cancelled by user"
                self._persist_subtask(st)
                job_id = self._subtask_to_job.get(st.subtask_id)
                if job_id:
                    job = self._jobs.get(job_id)
                    if job and job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                        job.status = JobStatus.CANCELLED
                        job.completed_at = time.time()
                        job.error = job.error or "Cancelled by user"
            task.status = TaskStatus.CANCELLED
            task.error = "Task cancelled by user."
            task.updated_at = time.time()
            self._persist_task(task)
            logger.info(f"Cancelled task {task_id}: {len(task.subtasks)} subtasks affected")
            return True

    def cancel_job(self, job_id: str, error: Optional[str] = None) -> Optional[JobResult]:
        """Cancel a specific job and return the result."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if job.status not in (JobStatus.PENDING, JobStatus.DISPATCHED, JobStatus.RUNNING):
                return None

            job.status = JobStatus.CANCELLED
            job.completed_at = time.time()
            job.error = error or "Cancelled by user"
            job.progress = 0.0
            logger.info(f"Cancelled job {job_id} for subtask {job.subtask_id}: {error or 'Cancelled by user'}")

            # Update subtask status
            for task in self._tasks.values():
                for st in task.subtasks:
                    if st.subtask_id == job.subtask_id:
                        st.status = JobStatus.CANCELLED
                        st.progress = 0.0
                        st.error_message = job.error
                        task.updated_at = time.time()
                        self._persist_subtask(st)
                        self._persist_task(task)

            self._persist_job(job)

            duration = None
            if job.started_at and job.completed_at:
                duration = job.completed_at - job.started_at

            return JobResult(
                job_id=job_id,
                success=False,
                output=None,
                error=job.error,
                duration_sec=duration
            )

    def get_ready_subtasks(self, task_id: str, strict: bool = True) -> List[SubTask]:
        """Get subtasks that are ready to run (pending and dependencies satisfied)."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                logger.warning(f"get_ready_subtasks: task {task_id} not found")
                return []
            ready = []
            for st in task.subtasks:
                if st.status != JobStatus.PENDING:
                    continue
                if strict:
                    deps_satisfied = all(
                        self._get_subtask_status(task_id, dep) == JobStatus.COMPLETED
                        for dep in st.dependencies
                    )
                else:
                    deps_satisfied = all(
                        self._get_subtask_status(task_id, dep) in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED)
                        for dep in st.dependencies
                    )
                if deps_satisfied:
                    ready.append(st)
            logger.info(f"get_ready_subtasks for {task_id}: {len(ready)} ready out of {len(task.subtasks)} total subtasks")
            for st in ready:
                logger.info(f"  Ready: {st.subtask_id} (agent={st.agent_id}, deps={st.dependencies})")
            return ready

    def get_subtask_observability(
        self,
        task_id: str,
        subtask_id: str,
        *,
        strict: bool = True,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Return user-facing observability hints for a subtask.

        This is intentionally derived state. It explains why a pending subtask is
        not ready yet and how long a running job has been active.
        """
        now = time.time() if now is None else now
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return {
                    "waiting_on_dependencies": [],
                    "blocked_reason": None,
                    "running_for_seconds": None,
                }

            subtask = next((st for st in task.subtasks if st.subtask_id == subtask_id), None)
            if not subtask:
                return {
                    "waiting_on_dependencies": [],
                    "blocked_reason": None,
                    "running_for_seconds": None,
                }

            waiting_on = []
            for dep in subtask.dependencies:
                dep_status = self._get_subtask_status(task_id, dep)
                if strict:
                    if dep_status != JobStatus.COMPLETED:
                        waiting_on.append(dep)
                elif dep_status not in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED):
                    waiting_on.append(dep)

            blocked_reason = None
            if subtask.status == JobStatus.PENDING and waiting_on:
                blocked_reason = "waiting_on_dependencies"

            if subtask.status == JobStatus.PENDING:
                wave = next(
                    (w for w in task.waves if w.wave_number == getattr(subtask, "wave_number", 1)),
                    None,
                )
                if wave is not None:
                    governance_status = getattr(wave, "governance_status", None)
                    if getattr(wave, "blocked_by_wave", None):
                        blocked_reason = "blocked_by_prior_wave"
                    elif getattr(wave, "is_revalidating", False):
                        blocked_reason = "wave_revalidating"
                    elif governance_status in {"blocked", "needs_fix"}:
                        blocked_reason = "wave_gate_blocked"

            running_for_seconds = None
            job = self.get_job_by_subtask(subtask_id)
            if job and job.status == JobStatus.RUNNING and job.started_at:
                running_for_seconds = max(0.0, now - job.started_at)

            return {
                "waiting_on_dependencies": waiting_on,
                "blocked_reason": blocked_reason,
                "running_for_seconds": running_for_seconds,
            }

    def is_all_subtasks_terminal(self, task_id: str) -> bool:
        """Check if all subtasks of a task are in a terminal state.

        Terminal states: COMPLETED, FAILED, CANCELLED.
        However, if any original (non-fix) subtask is CANCELLED, the task
        should be considered FAILED (not completed_with_failures), because
        cancelled subtasks were never executed.
        Returns False if no original subtasks exist yet (task still in decomposition/pending).
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or not task.subtasks:
                return False
            # Exclude fix subtasks AND decompose subtask from terminal check
            original = [
                st for st in task.subtasks
                if _is_original_business_subtask_id(st.subtask_id)
            ]
            if not original:
                return False
            terminal_states = {
                JobStatus.COMPLETED.value,
                JobStatus.CANCELLED.value,
                JobStatus.FAILED.value,
            }
            result = all(_status_value(st.status) in terminal_states for st in original)
            if result:
                completed = sum(1 for st in original if _status_value(st.status) == JobStatus.COMPLETED.value)
                failed = sum(1 for st in original if _status_value(st.status) == JobStatus.FAILED.value)
                cancelled = sum(1 for st in original if _status_value(st.status) == JobStatus.CANCELLED.value)
                total = len(original)

                # Issue 31: If any original subtask is CANCELLED, task should be FAILED
                # (not completed_with_failures). Cancelled means never executed.
                has_cancelled = cancelled > 0
                has_failed = failed > 0

                if has_cancelled:
                    logger.warning(
                        f"is_all_subtasks_terminal({task_id}): terminal but {cancelled} subtasks "
                        f"were CANCELLED (never executed). Task should be FAILED, not completed."
                    )
                elif has_failed:
                    logger.info(
                        f"is_all_subtasks_terminal({task_id}): True (original: {completed}/{total} completed, "
                        f"{failed} failed, {cancelled} cancelled) -> completed_with_failures"
                    )
                else:
                    logger.info(
                        f"is_all_subtasks_terminal({task_id}): True (all {completed}/{total} completed)"
                    )
            return result

    def is_all_subtasks_completed(self, task_id: str) -> bool:
        """Check if all original subtasks (those without '-fix-' or '-decompose' in subtask_id) have status COMPLETED.
        Returns False if no original subtasks exist yet (task still in decomposition/pending)."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or not task.subtasks:
                return False
            # Exclude fix subtasks AND decompose subtask from completion check
            original = [
                st for st in task.subtasks
                if _is_original_business_subtask_id(st.subtask_id)
            ]
            if not original:
                return False
            result = all(st.status == JobStatus.COMPLETED for st in original)
            if not result:
                status_counts = {}
                for st in original:
                    status_counts[st.status.value] = status_counts.get(st.status.value, 0) + 1
                logger.info(f"is_all_subtasks_completed({task_id}): {result} (status counts: {status_counts})")
            return result

    def cancel_downstream_subtasks(self, task_id: str, failed_subtask_id: str) -> List[str]:
        """Cancel all subtasks that directly or indirectly depend on failed_subtask_id using BFS.
        Skips fix-round subtasks and never overwrites FAILED/COMPLETED/CANCELLED states."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return []
            dep_map: Dict[str, List[str]] = {}
            for st in task.subtasks:
                if not _is_original_business_subtask_id(st.subtask_id):
                    continue
                for dep in st.dependencies:
                    dep_map.setdefault(dep, []).append(st.subtask_id)
            visited = set()
            queue = deque([failed_subtask_id])
            while queue:
                current = queue.popleft()
                for downstream_id in dep_map.get(current, []):
                    if downstream_id not in visited:
                        visited.add(downstream_id)
                        queue.append(downstream_id)
            cancelled = []
            for st in task.subtasks:
                if (
                    st.subtask_id in visited
                    and _is_original_business_subtask_id(st.subtask_id)
                    and st.status == JobStatus.PENDING
                ):
                    st.status = JobStatus.CANCELLED
                    st.updated_at = time.time()
                    cancelled.append(st.subtask_id)
                    self._persist_subtask(st)
            task.updated_at = time.time()
        if cancelled:
            self.update_wave_status(task_id)
        return cancelled

    def restore_cancelled_downstream(self, task_id: str, completed_subtask_id: str) -> List[str]:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return []
            dep_map: Dict[str, List[str]] = {}
            for st in task.subtasks:
                if not _is_original_business_subtask_id(st.subtask_id):
                    continue
                for dep in st.dependencies:
                    dep_map.setdefault(dep, []).append(st.subtask_id)
            visited = set()
            queue = deque([completed_subtask_id])
            while queue:
                current = queue.popleft()
                for downstream_id in dep_map.get(current, []):
                    if downstream_id not in visited:
                        visited.add(downstream_id)
                        queue.append(downstream_id)
            restored = []
            for st in task.subtasks:
                if (
                    st.subtask_id in visited
                    and _is_original_business_subtask_id(st.subtask_id)
                    and st.status == JobStatus.CANCELLED
                ):
                    st.status = JobStatus.PENDING
                    st.error_message = None
                    st.updated_at = time.time()
                    restored.append(st.subtask_id)
                    self._persist_subtask(st)
            if restored:
                task.updated_at = time.time()
        if restored:
            self.update_wave_status(task_id)
        return restored

    def update_wave_status(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or not task.waves:
                return
            for wave in task.waves:
                if wave.wave_number == 0:
                    continue
                if not wave.subtasks:
                    continue
                statuses = {st.status for st in wave.subtasks}
                old_status = wave.status
                if statuses <= {JobStatus.COMPLETED}:
                    wave.status = JobStatus.COMPLETED
                elif statuses & {JobStatus.RUNNING, JobStatus.DISPATCHED}:
                    wave.status = JobStatus.RUNNING
                elif statuses <= {JobStatus.CANCELLED}:
                    wave.status = JobStatus.CANCELLED
                elif statuses & {JobStatus.PENDING} and not (statuses & {JobStatus.RUNNING, JobStatus.DISPATCHED}):
                    wave.status = JobStatus.PENDING
                elif statuses & {JobStatus.FAILED} and not (statuses & {JobStatus.RUNNING, JobStatus.DISPATCHED, JobStatus.PENDING}):
                    wave.status = JobStatus.FAILED
                if old_status != wave.status:
                    logger.info(f"Wave {wave.wave_number} status changed: {old_status} -> {wave.status} (subtask statuses: {[s.value for s in statuses]})")
                    self._persist_wave(wave)

    def get_task_by_subtask(self, subtask_id: str) -> Optional[Task]:
        """Find the parent task containing a given subtask."""
        with self._lock:
            for task in self._tasks.values():
                for st in task.subtasks:
                    if st.subtask_id == subtask_id:
                        return task
            return None

    def _has_successful_remediation(self, task: "Task", subtask_id: str) -> bool:
        """Check whether any remediation of *subtask_id* (fix or reassign) completed.

        Used by ``_get_subtask_status`` so that downstream dependency checks
        treat a canonical dependency as satisfied when any remediation succeeded.
        """
        canonical_id = _canonical_subtask_id(subtask_id)
        for candidate in task.subtasks:
            if candidate.subtask_id == canonical_id:
                continue
            if _canonical_subtask_id(candidate.subtask_id) != canonical_id:
                continue
            if candidate.status == JobStatus.COMPLETED:
                return True
        return False

    def _get_subtask_status(self, task_id: str, subtask_id: str) -> JobStatus:
        """Effective subtask status, considering successful remediation variants.

        If the original subtask is FAILED or CANCELLED but a fix/reassign
        remediation with the same canonical ID completed, this returns
        COMPLETED so downstream dependency checks can proceed.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return JobStatus.PENDING
            for st in task.subtasks:
                if st.subtask_id == subtask_id:
                    if st.status == JobStatus.COMPLETED:
                        return JobStatus.COMPLETED
                    if st.status in (JobStatus.FAILED, JobStatus.CANCELLED):
                        if self._has_successful_remediation(task, subtask_id):
                            return JobStatus.COMPLETED
                    return st.status
            return JobStatus.PENDING

    def add_progress_callback(self, callback) -> None:
        """Register a callback for progress updates. Callback receives (ProgressUpdate)."""
        with self._lock:
            if callback not in self._progress_callbacks:
                self._progress_callbacks.append(callback)

    def remove_progress_callback(self, callback) -> None:
        """Remove a progress callback."""
        with self._lock:
            if callback in self._progress_callbacks:
                self._progress_callbacks.remove(callback)

    def pause_task(self, task_id: str) -> bool:
        """Pause a task, blocking new dispatches."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            self._is_paused[task_id] = True
            for st in task.subtasks:
                if st.status in (JobStatus.PENDING, JobStatus.DISPATCHED, JobStatus.RUNNING):
                    st.status = JobStatus.PAUSED
                    job_id = self._subtask_to_job.get(st.subtask_id)
                    if job_id:
                        job = self._jobs.get(job_id)
                        if job and job.status in (JobStatus.PENDING, JobStatus.DISPATCHED, JobStatus.RUNNING):
                            job.status = JobStatus.PAUSED
            task.updated_at = time.time()
            return True

    def resume_task(self, task_id: str) -> bool:
        """Resume a paused task, restoring subtasks to PENDING."""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            self._is_paused[task_id] = False
            for st in task.subtasks:
                if st.status == JobStatus.PAUSED:
                    st.status = JobStatus.PENDING
                    job_id = self._subtask_to_job.get(st.subtask_id)
                    if job_id:
                        job = self._jobs.get(job_id)
                        if job and job.status == JobStatus.PAUSED:
                            job.status = JobStatus.PENDING
            task.updated_at = time.time()
            return True

    def is_task_paused(self, task_id: str) -> bool:
        """Check if a task is paused."""
        with self._lock:
            return self._is_paused.get(task_id, False)

    def get_resumable_tasks(self) -> List[Dict[str, Any]]:
        """Get list of tasks that can be resumed from persistence.

        Returns a list of task summaries without restoring them to memory.
        """
        if self._persistence is None:
            return []

        try:
            resumable = []
            task_rows = self._persistence.get_all_tasks()
            for task_row in task_rows:
                task_id = task_row['task_id']
                status = task_row['status']

                # Use derivation-aware filtering: check if subtasks imply a terminal state
                final_status = self.derive_persisted_final_status(task_id)
                if final_status in self.TERMINAL_TASK_STATUSES:
                    # Repair stale persisted status before skipping
                    if final_status != status:
                        self._repair_persisted_terminal_task_status(task_row, final_status)
                    continue

                # Skip terminal and decomposing tasks
                if status in self.TERMINAL_TASK_STATUSES or status == "decomposing":
                    continue

                resumable.append({
                    'task_id': task_id,
                    'description': task_row.get('description', ''),
                    'status': status,
                    'created_at': task_row.get('created_at', 0),
                    'updated_at': task_row.get('updated_at', 0),
                    'project_dir': task_row.get('project_dir'),
                })
            return resumable
        except Exception as e:
            logger.error(f"Failed to get resumable tasks: {e}")
            return []

    def get_tasks_waiting_for_keys(self) -> List[str]:
        task_ids: List[str] = []
        if self._persistence is not None:
            try:
                rows = self._persistence.get_all_tasks()
            except Exception:
                rows = []
            for row in rows:
                decision = row.get("last_owner_decision") or {}
                error = row.get("error") or ""
                if (
                    row.get("status") == TaskStatus.PENDING.value
                    and (
                        decision.get("blocked_reason") == "waiting_for_keys"
                        or "Waiting for API keys" in error
                    )
                ):
                    task_ids.append(row["task_id"])
        with self._lock:
            for task_id, task in self._tasks.items():
                decision = getattr(task, "last_owner_decision", {}) or {}
                if (
                    task.status == TaskStatus.PENDING
                    and decision.get("blocked_reason") == "waiting_for_keys"
                    and task_id not in task_ids
                ):
                    task_ids.append(task_id)
        return task_ids

    def restore_task(self, task_id: str, *, allow_concurrent: bool = False) -> bool:
        """Restore a specific task from persistence to memory.

        Returns True if restored successfully, False otherwise.
        """
        if self._persistence is None:
            return False

        if not allow_concurrent:
            with self._lock:
                for tid, t in self._tasks.items():
                    if t.status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                        logger.warning(f"Cannot restore task {task_id}: task {tid} is already running")
                        return False

        try:
            from .models import Wave

            task_row = self._persistence.get_task(task_id)
            if not task_row:
                logger.warning(f"Task {task_id} not found in persistence")
                return False

            status = task_row['status']
            if status in self.TERMINAL_TASK_STATUSES:
                logger.warning(f"Task {task_id} is in terminal state, cannot restore")
                return False

            # Reconstruct task
            task = Task.new(
                description=task_row['description'],
                task_type=TaskType(task_row.get('task_type', 'unknown')),
                project_dir=_canonical_project_dir(task_row.get('project_dir')),
                owner_agent=task_row.get('owner_agent'),
                allowed_subtask_agents=task_row.get('allowed_subtask_agents') or [],
            )
            task.task_id = task_id
            task.status = TaskStatus(status) if status else TaskStatus.PENDING
            task.error = task_row.get('error')
            task.can_handle_directly = bool(task_row.get('can_handle_directly'))
            task.direct_response = task_row.get('direct_response')
            task.created_at = task_row.get('created_at', time.time())
            task.updated_at = task_row.get('updated_at', time.time())
            task.owner_session_id = task_row.get('owner_session_id')
            task.owner_state_summary = task_row.get('owner_state_summary') or {}
            task.last_owner_decision = task_row.get('last_owner_decision') or {}
            task.owner_agent = task_row.get('owner_agent')
            task.allowed_subtask_agents = task_row.get('allowed_subtask_agents') or []
            task.task_types = task_row.get('task_types') or []
            task.delivery_mode = task_row.get('delivery_mode') or 'legacy'

            # Restore subtasks. Older callers used get_task_subtasks(); the
            # current persistence service exposes get_subtasks().
            get_subtasks = getattr(self._persistence, "get_subtasks", None) or getattr(
                self._persistence,
                "get_task_subtasks",
            )
            subtask_rows = get_subtasks(task_id)
            for st_row in subtask_rows:
                subtask = SubTask(
                    task_id=task_id,
                    subtask_id=st_row['subtask_id'],
                    description=st_row['description'],
                    agent_id=st_row['agent_id'],
                    priority=st_row.get('priority', 1),
                    dependencies=st_row.get('dependencies', [])
                )
                subtask.status = JobStatus(st_row.get('status', 'pending'))
                subtask.progress = st_row.get('progress', 0.0)
                subtask.wave_number = st_row.get('wave_number', 1)
                subtask.error_message = st_row.get('error_message')
                subtask.output_file = st_row.get('output_file')
                subtask.duration = st_row.get('duration')
                if st_row.get('fix_plan'):
                    subtask.fix_plan = st_row['fix_plan']
                task.subtasks.append(subtask)

            # Restore waves
            wave_rows = self._persistence.get_waves(task_id)
            for w_row in wave_rows:
                wave = Wave(
                    wave_id=w_row['wave_id'],
                    task_id=task_id,
                    wave_number=w_row['wave_number']
                )
                wave.status = JobStatus(w_row.get('status', 'pending'))
                wave.is_blocked = bool(w_row.get('is_blocked'))
                wave.governance_status = w_row.get('governance_status', 'pending')
                wave.blocked_by_wave = w_row.get('blocked_by_wave')
                wave.is_revalidating = bool(w_row.get('is_revalidating'))
                wave.owner_decision = w_row.get('owner_decision') or {}
                wave.subtasks = [st for st in task.subtasks if st.wave_number == wave.wave_number]
                task.waves.append(wave)

            # Restore jobs
            for subtask in task.subtasks:
                job_rows = self._persistence.get_jobs_by_subtask(subtask.subtask_id)
                for job_row in job_rows:
                    job = Job(
                        job_id=job_row['job_id'],
                        subtask_id=subtask.subtask_id,
                        agent_id=job_row['agent_id'],
                        task_description=job_row.get('task_description', '')
                    )
                    job.status = JobStatus(job_row.get('status', 'pending'))
                    job.progress = job_row.get('progress', 0.0)
                    job.result = job_row.get('result')
                    job.error = job_row.get('error')
                    job.logs = job_row.get('logs', [])
                    job.created_at = job_row.get('created_at', time.time())
                    job.started_at = job_row.get('started_at')
                    job.completed_at = job_row.get('completed_at')
                    job.attempt = job_row.get('attempt', 0)
                    job.pinned_session_id = job_row.get('pinned_session_id')
                    job.failure_reason = job_row.get('failure_reason')
                    self._jobs[job.job_id] = job
                    self._subtask_to_job[subtask.subtask_id] = job.job_id

            try:
                task.owner_state_summary = self._restore_owner_context(task_id, task)
                task.last_owner_decision = task.owner_state_summary.get('last_owner_decision', {})
            except Exception as e:
                logger.warning(f"Failed to restore owner context for task {task_id}: {e}")

            self._tasks[task_id] = task
            logger.info(f"Restored task {task_id} with {len(task.subtasks)} subtasks, {len(task.waves)} waves")
            return True

        except Exception as e:
            logger.error(f"Failed to restore task {task_id}: {e}")
            return False

    def _restore_owner_context(self, task_id: str, task: Task) -> Dict[str, Any]:
        if self._persistence is None:
            return {}

        acceptance_records = self._persistence.get_acceptance_records(task_id)
        artifact_records = self._persistence.get_artifact_records(task_id)

        if not task.owner_session_id:
            for record in reversed(acceptance_records):
                owner_session_id = record.get('owner_session_id')
                if owner_session_id:
                    task.owner_session_id = owner_session_id
                    break

        artifact_versions: Dict[str, int] = {}
        for artifact in artifact_records:
            name = artifact.get('name') or artifact.get('content_ref') or artifact.get('artifact_id')
            if not name:
                continue
            artifact_versions[name] = max(
                artifact_versions.get(name, 0),
                int(artifact.get('version') or 0),
            )

        wave_statuses = {
            wave.wave_number: {
                'status': wave.status.value if hasattr(wave.status, 'value') else str(wave.status),
                'governance_status': getattr(wave, 'governance_status', 'pending'),
                'blocked_by_wave': getattr(wave, 'blocked_by_wave', None),
                'is_revalidating': getattr(wave, 'is_revalidating', False),
            }
            for wave in task.waves
        }

        return {
            'owner_session_id': task.owner_session_id,
            'recent_acceptance_records': acceptance_records[-10:],
            'artifact_versions': artifact_versions,
            'wave_statuses': wave_statuses,
            'last_owner_decision': acceptance_records[-1] if acceptance_records else task.last_owner_decision,
        }

    def restore_from_persistence(self) -> int:
        """Restore tasks from persistence on startup.

        Issue 46: No longer auto-restores tasks. Tasks must be manually restored
        via restore_task(). This method now only counts resumable tasks for info.

        Returns the number of resumable tasks found.
        """
        resumable = self.get_resumable_tasks()
        count = len(resumable)
        if count > 0:
            logger.info(f"Found {count} resumable tasks in persistence (manual restore required)")
            for t in resumable:
                logger.info(f"  - {t['task_id']}: {t['description'][:50]}...")
        return count

    TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled", "completed_with_failures"}
    ACTIVE_JOB_STATUSES = {"pending", "dispatched", "running"}
    STALE_RUNTIME_STATUSES = {"running", "dispatched"}

    def _is_original_business_subtask_row(self, row: Dict[str, Any]) -> bool:
        subtask_id = row.get("subtask_id", "")
        return _is_original_business_subtask_id(subtask_id)

    def _manifest_missing_required_paths(self, task_row: Dict[str, Any]) -> List[str]:
        """Return required manifest path hints that are absent on disk."""
        if self._persistence is None:
            return []
        get_manifest = getattr(self._persistence, "get_requirement_manifest", None)
        if not callable(get_manifest):
            return []
        manifest = get_manifest(task_row["task_id"])
        if not manifest:
            return []

        project_dir = manifest.get("project_dir") or task_row.get("project_dir")
        missing: List[str] = []
        for deliverable in manifest.get("deliverables", []) or []:
            if not deliverable.get("required", True):
                continue
            path_hint = deliverable.get("path_hint")
            if not path_hint:
                continue
            candidates = self._manifest_candidate_paths(path_hint, project_dir)
            if not any(os.path.isfile(candidate) for candidate in candidates):
                missing.append(path_hint)
        return missing

    def _manifest_candidate_paths(self, path_hint: str, project_dir: Optional[str]) -> List[str]:
        """Return accepted on-disk candidates for a manifest path hint."""
        base = project_dir or ""
        resolved = path_hint if os.path.isabs(path_hint) else os.path.join(base, path_hint)
        candidates = [os.path.realpath(resolved)]
        basename = os.path.basename(path_hint)
        lower = basename.lower()
        if lower == "readme":
            candidates.extend([
                os.path.realpath(os.path.join(base, "README.md")),
                os.path.realpath(os.path.join(base, "README.rst")),
                os.path.realpath(os.path.join(base, "README.txt")),
            ])
        if lower.startswith("test_") and lower.endswith(".py") and "/" not in path_hint:
            candidates.append(os.path.realpath(os.path.join(base, "tests", basename)))
        return list(dict.fromkeys(candidates))

    def derive_persisted_final_status(self, task_id: str) -> Optional[str]:
        """Derive a terminal task status from persisted subtasks.

        Returns a terminal status when the persisted subtask graph is already
        terminal even if the top-level tasks.status is stale. Returns None for
        tasks that are still genuinely resumable or not sufficiently decomposed.
        """
        if self._persistence is None:
            return None

        task_row = self._persistence.get_task(task_id)
        if not task_row:
            return None

        current = task_row.get("status") or ""

        try:
            delivery_contract = self.get_delivery_contract(task_id)
        except Exception:
            delivery_contract = None
        if delivery_contract:
            decision = task_row.get("last_owner_decision") or {}
            if isinstance(decision, str):
                try:
                    decision = json.loads(decision)
                except Exception:
                    decision = {}
            delivery_quality = (decision or {}).get("delivery_quality")
            if not delivery_quality:
                return None
            quality_value = delivery_quality.get("delivery_quality") if isinstance(delivery_quality, dict) else None
            if quality_value == "failed":
                return TaskStatus.FAILED.value
            if quality_value == "partial":
                return TaskStatus.COMPLETED_WITH_FAILURES.value

        if current in self.TERMINAL_TASK_STATUSES:
            return current

        subtask_rows = self._persistence.get_subtasks(task_id) or []
        active_statuses = {
            JobStatus.PENDING.value,
            JobStatus.DISPATCHED.value,
            JobStatus.RUNNING.value,
        }
        active_remediation = [
            row for row in subtask_rows
            if _is_remediation_subtask_id(row.get("subtask_id", ""))
            and row.get("status", "pending") in active_statuses
        ]
        if active_remediation:
            return None

        business = [row for row in subtask_rows if self._is_original_business_subtask_row(row)]
        if not business:
            return None

        statuses = [row.get("status", "pending") for row in business]
        if all(status == JobStatus.COMPLETED.value for status in statuses):
            if self._manifest_missing_required_paths(task_row):
                return TaskStatus.FAILED.value
            return TaskStatus.COMPLETED.value

        terminal_subtask_statuses = {
            JobStatus.COMPLETED.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        }
        if all(status in terminal_subtask_statuses for status in statuses):
            completed = sum(1 for status in statuses if status == JobStatus.COMPLETED.value)
            failed = sum(1 for status in statuses if status == JobStatus.FAILED.value)
            cancelled = sum(1 for status in statuses if status == JobStatus.CANCELLED.value)
            if completed > 0 and failed > 0 and cancelled == 0:
                return TaskStatus.COMPLETED_WITH_FAILURES.value
            return TaskStatus.FAILED.value

        return None

    def _repair_persisted_terminal_task_status(self, task_row: Dict[str, Any], final_status: str) -> bool:
        """Persist derived final task status. Returns True when a row changed."""
        old_status = task_row.get("status")
        if old_status == final_status:
            return False
        task_row["status"] = final_status
        if final_status == TaskStatus.FAILED.value and not task_row.get("error"):
            task_row["error"] = "Recovered terminal status after restart: one or more required subtasks failed or were cancelled."
        elif final_status in {TaskStatus.COMPLETED.value, TaskStatus.COMPLETED_WITH_FAILURES.value}:
            task_row["error"] = None
        self._persistence.save_task(task_row)
        logger.info(
            "Recovered stale persisted task status for %s: %s -> %s",
            task_row.get("task_id"), old_status, final_status,
        )
        return True

    def _archive_stale_terminal_remediation(self, task_id: str, *, reason: str) -> int:
        """Cancel nonterminal remediation rows for an already-terminal task.

        Worker processes do not survive backend restarts. If a parent task is
        already terminal, stale retry/gap-fill rows must not make the task look
        running again after DB-only restoration.
        """
        if self._persistence is None:
            return 0

        nonterminal = {"pending", "dispatched", "running", "paused"}
        archived = 0
        now_timestamp = time.time()
        try:
            subtask_rows = self._persistence.get_subtasks(task_id) or []
        except Exception:
            return 0

        for st_row in subtask_rows:
            subtask_id = st_row.get("subtask_id", "")
            if st_row.get("status") not in nonterminal:
                continue
            if not _is_remediation_subtask_id(subtask_id):
                continue

            st_row["status"] = JobStatus.CANCELLED.value
            st_row["error_message"] = (
                f"Archived stale remediation after {reason}; parent task is already terminal."
            )
            self._persistence.save_subtask(st_row)
            archived += 1

            for job in self._persistence.get_jobs_by_subtask(subtask_id) or []:
                if job.get("status") in nonterminal:
                    job["status"] = JobStatus.FAILED.value
                    job["error"] = (
                        "Backend restarted after the parent task reached a terminal status; "
                        "stale remediation job was archived."
                    )
                    job["failure_reason"] = "orphan_recovery"
                    job["completed_at"] = now_timestamp
                    self._persistence.save_job(job)

        if archived:
            logger.info(
                "Archived %d stale remediation subtask(s) for terminal task %s",
                archived,
                task_id,
            )
        return archived

    def recover_orphaned_persisted_tasks(self, *, reason: str = "backend_restart", auto_resume: bool = False) -> int:
        """Scan persistence for stale RUNNING/DISPATCHED state and mark as orphaned.

        Args:
            reason: Label for log messages.
            auto_resume: If True, stale subtasks become ``pending`` and tasks become
                ``pending`` so they can be resumed automatically.  If False (default),
                the legacy behaviour applies: subtasks → ``paused``, tasks → ``paused``.
                Jobs always become ``failed`` with ``failure_reason=orphan_recovery``
                regardless of *auto_resume*.
        """
        if self._persistence is None:
            return 0

        from datetime import datetime

        STALE_STATUSES = {"running", "dispatched"}
        now_timestamp = time.time()
        recovered = 0

        try:
            all_tasks = self._persistence.get_all_tasks()
        except Exception as exc:
            logger.error("recover_orphaned_persisted_tasks: cannot read tasks: %s", exc)
            return 0

        for task_row in all_tasks:
            task_id = task_row["task_id"]
            task_status = task_row.get("status", "")

            # Terminal derivation: check if subtasks imply a terminal state before
            # touching anything. This prevents rewriting completed/failed history to paused.
            final_status = self.derive_persisted_final_status(task_id)
            if final_status in self.TERMINAL_TASK_STATUSES:
                self._repair_persisted_terminal_task_status(task_row, final_status)
                self._archive_stale_terminal_remediation(task_id, reason=reason)
                continue

            if task_status in self.TERMINAL_TASK_STATUSES:
                self._archive_stale_terminal_remediation(task_id, reason=reason)
                continue

            affected = False
            subtask_rows = self._persistence.get_subtasks(task_id)

            for st_row in subtask_rows:
                st_id = st_row["subtask_id"]
                st_status = st_row.get("status", "")

                # Collect stale jobs for this subtask
                stale_jobs = [
                    j for j in (self._persistence.get_jobs_by_subtask(st_id) or [])
                    if j.get("status", "") in STALE_STATUSES
                ]

                if st_status in STALE_STATUSES or stale_jobs:
                    affected = True

                    if st_status in STALE_STATUSES:
                        st_row["status"] = "pending" if auto_resume else "paused"
                        st_row["error_message"] = (
                            f"Reset to pending after {reason}; previous worker was orphaned."
                            if auto_resume
                            else f"Paused after {reason}; previous worker was orphaned."
                        )
                        self._persistence.save_subtask(st_row)

                    for job in stale_jobs:
                        job["status"] = "failed"
                        job["error"] = (
                            "Backend restarted while this job was running; "
                            "worker process is no longer available."
                        )
                        job["failure_reason"] = "orphan_recovery"
                        job["completed_at"] = now_timestamp
                        self._persistence.save_job(job)

                        j_id = job["job_id"]
                        subtask_of_job = st_row["subtask_id"]
                        logger.info(
                            "Orphan recovery: job %s (subtask %s, task %s): "
                            "%s -> failed (orphan_recovery)",
                            j_id, subtask_of_job, task_id, job.get("status", "?"),
                        )

            if affected:
                task_row["status"] = "pending" if auto_resume else "paused"
                task_row["error"] = (
                    f"Recovered after {reason}; orphaned jobs were reset and task can resume."
                    if auto_resume
                    else f"Paused after {reason} due to orphaned running jobs. Resume the task to continue."
                )
                self._persistence.save_task(task_row)
                logger.info(
                    "Orphan recovery: task %s marked as %s (%d subtask(s) affected)",
                    task_id, task_row["status"],
                    sum(1 for s in subtask_rows if s.get("status", "") in ("pending", "paused")),
                )
                recovered += 1
            else:
                logger.info(
                    "Orphan recovery: task %s (status=%s) has no stale jobs",
                    task_id, task_status,
                )

        if recovered:
            logger.info(
                "Orphan recovery complete: %d task(s) marked as paused",
                recovered,
            )
        else:
            logger.info("Orphan recovery complete: no stale tasks found")

        return recovered
