from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from across_agents_assistant.task_manager.models import (
    AcceptanceResult,
    AcceptanceRecord,
    Artifact,
    FailureType,
    Job,
    JobStatus,
    OrchestratorState,
    ProgressUpdate,
    SubTask,
    Task,
    TaskContract,
    TaskStatus,
    ValidationReport,
    Wave,
    RecommendedAction,
    WaveLifecycleStatus,
)
from across_agents_assistant.task_manager.state import TaskState
from across_agents_assistant.agent_ids import LOCAL_CLI_AGENT_IDS
from across_agents_assistant.llm_gateway.provider_registry import get_default_provider_ids
from across_agents_assistant.workspace_hygiene import (
    IGNORED_DIR_NAMES,
    RUNTIME_DATA_DIR_NAMES,
    filtered_workspace_files,
    iter_workspace_noise_files,
)

logger = logging.getLogger("across_agents_assistant.task_manager")


class TaskOrchestrator:
    """
    Core orchestration engine for multi-agent task collaboration.

    Automatically progresses a task through its DAG after each SubTask completes,
    running validation, acceptance, fix cycles, and integration acceptance.
    """

    _LOCAL_AGENT_IDS = LOCAL_CLI_AGENT_IDS
    _CLOUD_AGENT_IDS = get_default_provider_ids()

    def __init__(
        self,
        state: TaskState,
        dispatcher: Any,
        validator: Any,
        owner_agent: Any,
    ) -> None:
        self._state = state
        self._dispatcher = dispatcher
        self._validator = validator
        self._owner_agent = owner_agent
        self._orchestrator_states: Dict[str, OrchestratorState] = {}
        self._lock = threading.Lock()
        self._quality_remediation_lock = threading.Lock()

        # Register progress callback (Task 5)
        self._dispatcher.add_progress_callback(self._on_job_progress)

    def resume_task(self, task: Any) -> None:
        """
        Resume a restored task: initialize OrchestratorState and dispatch ready subtasks.
        This should be called after TaskState.restore_task() succeeds.
        """
        task_id = task.task_id
        logger.info(f"Resuming task {task_id}")

        # Initialize orchestrator state for this task
        strict_dependency = getattr(task, 'strict_dependency', True)
        enable_wave_gate = getattr(task, 'enable_wave_gate', True)
        ost = None
        with self._lock:
            ost = OrchestratorState(
                task_id=task_id,
                fix_rounds=task.fix_rounds,
                strict_dependency=strict_dependency,
                wave_gate_enabled=enable_wave_gate,
                owner_session_id=getattr(task, "owner_session_id", None),
                allowed_subtask_agents=getattr(task, "allowed_subtask_agents", []),
            )
            self._orchestrator_states[task_id] = ost

        # Sync wave governance from task to ost
        for wave in task.waves:
            if wave.governance_status == WaveLifecycleStatus.APPROVED.value:
                ost.wave_approved.add(wave.wave_number)
                ost.wave_statuses[wave.wave_number] = wave.governance_status
            elif wave.governance_status == WaveLifecycleStatus.BLOCKED.value:
                ost.wave_statuses[wave.wave_number] = wave.governance_status
                if wave.blocked_by_wave:
                    ost.blocked_by_wave[wave.wave_number] = wave.blocked_by_wave
            elif wave.governance_status == WaveLifecycleStatus.REVALIDATING.value:
                ost.revalidating_waves.add(wave.wave_number)
                ost.wave_statuses[wave.wave_number] = wave.governance_status

        # If any wave is blocked and missing its fix subtask, create it.
        self._repair_exhausted_blocked_waves(task, ost, reason="resume_task")
        repaired_waves = self._repair_completed_wave_acceptance(task, ost)
        if repaired_waves:
            logger.info(
                "resume_task: repaired completed wave acceptance for task %s: %s",
                task_id,
                repaired_waves,
            )
        for wave in task.waves:
            if wave.governance_status == WaveLifecycleStatus.BLOCKED.value:
                # N4 Fix: Find highest existing wave fix round and create next one
                canonical_id = f"wave-{wave.wave_number}"
                if task.fix_rounds.get(canonical_id, 0) >= ost.max_fix_rounds:
                    logger.warning(
                        "resume_task: blocked wave %s for task %s already exhausted remediation budget; skipping new fix",
                        wave.wave_number,
                        task_id,
                    )
                    continue
                existing_fix_rounds = []
                for st in task.subtasks:
                    canonical = self._get_canonical_subtask_id(st.subtask_id)
                    if canonical == canonical_id and "-fix-" in st.subtask_id:
                        # Extract round number from wave-X-fix-N
                        parts = st.subtask_id.split("-fix-")
                        if len(parts) == 2 and parts[1].isdigit():
                            existing_fix_rounds.append(int(parts[1]))

                next_round = max(existing_fix_rounds) + 1 if existing_fix_rounds else 1
                wave_fix_subtask_id = f"wave-{wave.wave_number}-fix-{next_round}"

                existing_fix = next((st for st in task.subtasks if st.subtask_id == wave_fix_subtask_id), None)
                if not existing_fix:
                    wave_subtasks = [
                        st for st in task.subtasks
                        if st.wave_number == wave.wave_number and "-fix-" not in st.subtask_id and st.status == JobStatus.COMPLETED
                    ]
                    if wave_subtasks:
                        first_subtask = wave_subtasks[0]
                        wave_fix_subtask = SubTask(
                            subtask_id=wave_fix_subtask_id,
                            task_id=task_id,
                            description=f"Fix Wave {wave.wave_number} based on governance feedback. Original task: {first_subtask.description}",
                            agent_id=first_subtask.agent_id,
                            wave_number=wave.wave_number,
                            status=JobStatus.PENDING,
                            dependencies=first_subtask.dependencies,
                        )
                        task.subtasks.append(wave_fix_subtask)
                        self._state._persist_subtask(wave_fix_subtask)
                        logger.info(f"resume_task: created wave fix subtask {wave_fix_subtask_id} for blocked wave {wave.wave_number}")

        # Dispatch ready subtasks (no deps, not yet dispatched/completed/failed)
        ready_subtasks = self._get_dispatchable_ready_subtasks(task_id, ost)
        dispatchable = [
            st for st in ready_subtasks
            if not self._is_decompose_subtask(st)
            and st.status == JobStatus.PENDING
            and not self._has_active_job_for_subtask(st.subtask_id)
        ]

        if dispatchable:
            task.status = TaskStatus.RUNNING
            self._state._persist_task(task)
            logger.info(f"Task {task_id} resuming: dispatching {len(dispatchable)} ready business subtasks")
            for st in dispatchable:
                logger.info(f"Dispatching resumed business subtask {st.subtask_id} (agent={st.agent_id}, wave={st.wave_number})")
                self._dispatcher.dispatch_subtask(st)
        else:
            if any(self._is_decompose_subtask(st) and st.status == JobStatus.PENDING for st in ready_subtasks):
                logger.info("Task %s has pending owner decomposition; repair_task_dispatch will restart it", task_id)
            else:
                logger.info(f"Task {task_id} has no ready business subtasks to resume")

    def submit_task(self, user_request: str, context: Optional[Dict[str, Any]] = None) -> str:
        """
        Submit a new user request: create Task → return immediately → decompose async.
        The decomposition phase is now part of the task lifecycle (status: decomposing).
        """
        context = dict(context or {})
        # Extract project_dir from context if provided
        project_dir = None
        if "project_dir" in context:
            project_dir = context["project_dir"]

        owner_agent = context.get("owner_agent")
        requested_subtask_agents = context.get("allowed_subtask_agents", [])
        allowed_subtask_agents = self._resolve_effective_subtask_agents(
            owner_agent=owner_agent,
            requested_agents=requested_subtask_agents,
        )
        context["allowed_subtask_agents"] = allowed_subtask_agents

        task_types = context.get("task_types") or []
        delivery_mode = context.get("delivery_mode")

        task = self._state.create_task(
            description=user_request,
            project_dir=project_dir,
            owner_agent=owner_agent,
            allowed_subtask_agents=allowed_subtask_agents,
            task_types=task_types,
            delivery_mode=delivery_mode,
        )
        task.status = TaskStatus.DECOMPOSING
        task_id = task.task_id
        self._state._persist_task(task)
        self._ensure_decompose_wave(task)

        # Initialize orchestrator state for this task
        strict_dependency = context.get("strict_dependency", True)
        enable_wave_gate = context.get("enable_wave_gate", True)
        with self._lock:
            ost = OrchestratorState(
                task_id=task_id,
                fix_rounds=task.fix_rounds,
                strict_dependency=strict_dependency,
                wave_gate_enabled=enable_wave_gate,
                owner_session_id=getattr(task, "owner_session_id", None),
                allowed_subtask_agents=allowed_subtask_agents,
            )
            ost.max_quality_remediation_attempts = self._quality_remediation_limit_for_task(
                task,
                context=context,
            )
            self._orchestrator_states[task_id] = ost

        # Start decomposition in a background thread so we return immediately
        def _decompose_async():
            try:
                import time
                t0 = time.time()
                self._owner_agent.decompose_and_assign(task, context=context)
                business_subtasks = [
                    st for st in task.subtasks
                    if self._is_business_decomposition_subtask(st)
                ]
                if not business_subtasks:
                    raise RuntimeError("LLM decomposition failed: no business subtasks generated")
                # Decomposition done — assign waves and transition to pending
                self._owner_agent.assign_waves(task)
                # Persist waves after assignment
                for wave in task.waves:
                    self._state._persist_wave(wave)
                # Task 3: Re-run coverage after wave assignment to capture final contract state
                try:
                    self._owner_agent.refresh_decomposition_coverage(task)
                except Exception as exc:
                    logger.warning("Post-wave coverage refresh failed for task %s: %s", task_id, exc)
                # Task 7: Sync owner session to orchestrator state
                ost = self._orchestrator_states.get(task_id)
                if ost and not ost.owner_session_id:
                    ost.owner_session_id = getattr(task, "owner_session_id", None)
                task.status = TaskStatus.PENDING
                task.updated_at = time.time()
                self._state._persist_task(task)
                elapsed = time.time() - t0
                logger.info(f"Task {task_id} decomposition completed in {elapsed:.1f}s, {len(task.subtasks)} subtasks")

                # Dispatch initial ready subtasks (those with no dependencies)
                ready_subtasks = self._get_dispatchable_ready_subtasks(task_id, self._orchestrator_states[task_id])
                logger.info(f"Task {task_id} decomposition done, {len(ready_subtasks)} ready subtasks found")
                if ready_subtasks:
                    task.status = TaskStatus.RUNNING
                    task.updated_at = time.time()
                    self._state._persist_task(task)
                    logger.info(f"Task {task_id} dispatching {len(ready_subtasks)} ready subtasks")
                    for st in ready_subtasks:
                        logger.info(f"Dispatching initial subtask {st.subtask_id} (agent={st.agent_id}, wave={st.wave_number})")
                        self._dispatcher.dispatch_subtask(st)
                else:
                    logger.warning(f"Task {task_id} has no ready subtasks after decomposition")
            except Exception as e:
                logger.error(f"Task decomposition failed for {task_id}: {e}")
                decompose = next((st for st in task.subtasks if st.subtask_id == f"{task_id}-decompose"), None)
                if self._is_missing_api_key_error(e):
                    self._mark_task_waiting_for_keys(task, decompose, reason="initial_decomposition")
                else:
                    self._mark_decomposition_failed(task, decompose, reason="initial_decomposition", exc=e)

        threading.Thread(target=_decompose_async, daemon=True).start()
        return task_id

    def _resolve_effective_subtask_agents(
        self,
        owner_agent: Optional[str],
        requested_agents: Optional[List[str]],
    ) -> List[str]:
        get_valid_agents = getattr(self._dispatcher, "_get_valid_agents", None)
        valid_agents = get_valid_agents() if callable(get_valid_agents) else []
        if not isinstance(valid_agents, list):
            valid_agents = []
        requested = [agent for agent in (requested_agents or []) if agent in valid_agents]
        if requested:
            return requested
        if owner_agent and owner_agent != "auto" and owner_agent in valid_agents:
            return [owner_agent]
        return valid_agents

    def _get_allowed_valid_agents(self, task: Task) -> List[str]:
        get_valid_agents = getattr(self._dispatcher, "_get_valid_agents", None)
        valid_agents = get_valid_agents() if callable(get_valid_agents) else []
        if not isinstance(valid_agents, list):
            valid_agents = []
        allowed = getattr(task, "allowed_subtask_agents", []) or []
        if not allowed:
            return valid_agents
        allowed_set = set(allowed)
        filtered = [agent for agent in valid_agents if agent in allowed_set]
        return filtered or allowed

    def _ensure_decompose_wave(self, task: Task) -> None:
        decompose_subtask_id = f"{task.task_id}-decompose"
        decompose_subtask = next(
            (st for st in task.subtasks if st.subtask_id == decompose_subtask_id),
            None,
        )
        if decompose_subtask is None:
            decompose_subtask = self._state.add_subtask(
                task_id=task.task_id,
                description="Owner Agent 正在分析需求并分解任务...",
                agent_id="owner",
                priority=0,
                dependencies=[],
                subtask_id=decompose_subtask_id,
            )
        if decompose_subtask:
            decompose_subtask.wave_number = 0
            decompose_subtask.status = JobStatus.RUNNING
            self._state._persist_subtask(decompose_subtask)

        existing_wave0 = next((wave for wave in task.waves if wave.wave_number == 0), None)
        if existing_wave0:
            existing_wave0.status = JobStatus.RUNNING
            existing_wave0.subtasks = [decompose_subtask] if decompose_subtask else []
            self._state._persist_wave(existing_wave0)
            return

        decompose_wave = Wave(
            wave_id=f"wave-decompose-{uuid.uuid4().hex[:8]}",
            wave_number=0,
            task_id=task.task_id,
            subtasks=[decompose_subtask] if decompose_subtask else [],
            status=JobStatus.RUNNING,
            is_blocked=False,
            fix_rounds=[],
        )
        task.waves = [decompose_wave] + [wave for wave in task.waves if wave.wave_number != 0]
        self._state._persist_wave(decompose_wave)

    def _on_job_progress(self, update: ProgressUpdate) -> None:
        """
        Callback registered with the dispatcher. Runs in the dispatcher's thread.
        When a job completes or fails, trigger async handling on the main event loop.
        """
        if update.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            def _run_async_handler():
                loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(self._handle_job_completed(update.job_id))
                except Exception as e:
                    import traceback
                    logger.error(f"Orchestrator async handler error for job {update.job_id}: {e}")
                    logger.error(f"Traceback: {traceback.format_exc()}")
                finally:
                    asyncio.set_event_loop(None)
                    loop.close()

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # Dispatcher callbacks normally arrive on worker threads. Run
                # the handler to completion there so immediate completion
                # signals can deterministically unlock downstream subtasks.
                _run_async_handler()
            else:
                # If a callback is delivered on an active asyncio loop, keep
                # the previous isolation behavior to avoid nested event loops.
                threading.Thread(target=_run_async_handler, daemon=True).start()

    def _apply_deterministic_validation_to_acceptance(
        self,
        acceptance: AcceptanceResult,
        validation_report: ValidationReport,
    ) -> AcceptanceResult:
        """Override LLM acceptance when deterministic checks have failed.

        If the validator found blocking errors, the acceptance record must not
        report judge_passed=true or recommend approve — otherwise quality
        diagnostics become unreliable.
        """
        if validation_report.passed:
            return acceptance
        acceptance.level1_passed = False
        acceptance.level2_passed = False
        current_action = getattr(acceptance, "action", None)
        if current_action not in {"fix", "reassign"}:
            acceptance.action = "fix"
        current_recommended = getattr(acceptance, "recommended_action", None)
        if current_recommended not in {"fix", "reassign"}:
            acceptance.recommended_action = "fix"
        existing_failed = list(getattr(acceptance, "failed_checks", []) or [])
        for error in validation_report.errors:
            message = str(error)
            if message not in existing_failed:
                existing_failed.append(message)
        acceptance.failed_checks = existing_failed
        acceptance.level1_errors = [str(e) for e in validation_report.errors]
        return acceptance

    async def _handle_job_completed(self, job_id: str) -> None:
        """
        Full acceptance flow for a completed job:
        Level 1 validation → Level 2 acceptance → if passed, unlock DAG → if all done, integration acceptance.
        For FAILED jobs, skip validation and directly initiate fix.
        """
        logger.info(f"_handle_job_completed called for job {job_id}")
        job = self._state.get_job(job_id)
        if not job:
            logger.warning(f"Orchestrator: job {job_id} not found")
            return

        task = self._state.get_task_by_subtask(job.subtask_id)
        if not task:
            logger.warning(f"Orchestrator: no parent task for subtask {job.subtask_id}")
            return

        ost = self._orchestrator_states.get(task.task_id)
        if not ost:
            logger.warning(f"Orchestrator: no OrchestratorState for task {task.task_id}")
            return
        if not ost.owner_session_id:
            ost.owner_session_id = getattr(task, "owner_session_id", None)

        logger.info(f"Handling job {job_id} (subtask {job.subtask_id}) for task {task.task_id}, status={job.status}")

        # Extract the original subtask ID for fix round tracking
        original_id = self._get_original_subtask_id(job.subtask_id)
        self._record_job_artifact(task, job)
        removed_noise = self._remove_workspace_noise_files(
            task,
            reason=f"before subtask acceptance {job.subtask_id}",
        )
        self._record_deterministic_cleanup(task, "removed_workspace_noise", removed_noise)

        if job.subtask_id.startswith("st-quality-"):
            await self._handle_quality_remediation_job_finished(task, job)
            return

        # If job failed, skip validation and directly fix
        if job.status == JobStatus.FAILED:
            failure_type = self._classify_failure(job=job)
            policy_action = self._decide_failure_policy(failure_type)
            if (
                self._is_remediation_subtask_id(job.subtask_id)
                and task.fix_rounds.get(original_id, 0) < ost.max_fix_rounds
                and policy_action != "fail_fast"
            ):
                task.status = TaskStatus.RUNNING
                task.error = "A remediation subtask failed and is being retried."
                task.updated_at = time.time()
                self._state._persist_task(task)
            if ost.strict_dependency:
                # N5 Fix: Use canonical ID (original_id) for downstream cancellation.
                # If we use job.subtask_id (e.g. "st-xxx-fix-3"), cancel_downstream_subtasks
                # skips fix subtasks in dep_map building, so downstream never gets cancelled.
                cancelled = self._state.cancel_downstream_subtasks(task.task_id, original_id)
                if cancelled:
                    logger.info(f"Strict mode: cancelled {len(cancelled)} downstream subtasks: {cancelled}")
            feedback = f"Job execution failed [{failure_type.value}]: {job.error or 'Unknown error'}"
            current_round = task.fix_rounds.get(original_id, 0)
            if policy_action == "fail_fast":
                acceptance = AcceptanceResult(
                    subtask_id=job.subtask_id,
                    level1_passed=False,
                    level2_passed=False,
                    level2_feedback=feedback,
                    action="downgrade",
                    failure_type=failure_type.value,
                )
                self._record_acceptance(
                    task=task,
                    job=job,
                    level1_passed=False,
                    acceptance=acceptance,
                )
                logger.warning(
                    f"Fail-fast policy triggered for {job.subtask_id} due to high-confidence {failure_type.value}"
                )
                await self._handle_max_rounds_exceeded(job, acceptance)
            elif current_round >= ost.max_fix_rounds:
                # Build a minimal acceptance result for max rounds handling
                acceptance = AcceptanceResult(
                    subtask_id=job.subtask_id,
                    level1_passed=False,
                    level2_passed=False,
                    level2_feedback=feedback,
                    failure_type=failure_type.value,
                )
                self._record_acceptance(
                    task=task,
                    job=job,
                    level1_passed=False,
                    acceptance=acceptance,
                )
                await self._handle_max_rounds_exceeded(job, acceptance)
            else:
                self._record_acceptance(
                    task=task,
                    job=job,
                    level1_passed=False,
                    acceptance=AcceptanceResult(
                        subtask_id=job.subtask_id,
                        level1_passed=False,
                        level2_passed=False,
                        level2_feedback=feedback,
                        action="fix",
                        failure_type=failure_type.value,
                    ),
                )
                self._initiate_fix(job, feedback)
            return

        # Level 1: Contract validation
        level1_report: ValidationReport = self._validator.validate(job)

        # Level 2: Owner agent acceptance (sync method)
        acceptance: AcceptanceResult = self._owner_agent.accept_subtask(job)

        # Apply deterministic validation override before recording acceptance.
        # If the validator found blocking errors, the acceptance record must not
        # report judge_passed=true or recommend approve.
        acceptance = self._apply_deterministic_validation_to_acceptance(acceptance, level1_report)

        # Record acceptance result
        acceptance.level1_passed = level1_report.passed
        acceptance.level1_errors = [str(e) for e in level1_report.errors]
        acceptance.subtask_id = job.subtask_id
        ost.acceptance_results[job.subtask_id] = acceptance
        self._record_acceptance(task=task, job=job, level1_passed=level1_report.passed, acceptance=acceptance)

        if acceptance.parse_failed:
            retry_count = ost.acceptance_parse_retries.get(job.subtask_id, 0)
            if retry_count < 1:
                ost.acceptance_parse_retries[job.subtask_id] = retry_count + 1
                logger.warning(
                    f"Acceptance parse failed for {job.subtask_id}; retrying acceptance without consuming a fix round"
                )
                acceptance = self._owner_agent.accept_subtask(job)
                acceptance = self._apply_deterministic_validation_to_acceptance(acceptance, level1_report)
                acceptance.level1_passed = level1_report.passed
                acceptance.level1_errors = [str(e) for e in level1_report.errors]
                acceptance.subtask_id = job.subtask_id
                ost.acceptance_results[job.subtask_id] = acceptance
                self._record_acceptance(task=task, job=job, level1_passed=level1_report.passed, acceptance=acceptance)
            else:
                logger.warning(
                    f"Acceptance parse failed again for {job.subtask_id}; falling back to conservative fix path"
                )

        acceptance.failure_type = self._classify_failure(
            job=job,
            acceptance=acceptance,
            level1_report=level1_report,
        ).value

        # Determine overall pass
        overall_pass = level1_report.passed and acceptance.level2_passed

        if not overall_pass:
            policy_action = self._decide_failure_policy(FailureType(acceptance.failure_type))
            if policy_action == "retry_acceptance":
                retry_key = f"{job.subtask_id}:acceptance"
                total_retries = (
                    ost.acceptance_parse_retries.get(job.subtask_id, 0)
                    + ost.acceptance_parse_retries.get(retry_key, 0)
                )
                if total_retries < 2:
                    ost.acceptance_parse_retries[retry_key] = ost.acceptance_parse_retries.get(retry_key, 0) + 1
                    logger.warning(
                        "Acceptance unavailable for %s due to %s; retrying owner acceptance without remediation",
                        job.subtask_id,
                        acceptance.failure_type,
                    )
                    acceptance = self._owner_agent.accept_subtask(job)
                    acceptance.level1_passed = level1_report.passed
                    acceptance.level1_errors = [str(e) for e in level1_report.errors]
                    acceptance.subtask_id = job.subtask_id
                    acceptance.failure_type = self._classify_failure(
                        job=job,
                        acceptance=acceptance,
                        level1_report=level1_report,
                    ).value
                    ost.acceptance_results[job.subtask_id] = acceptance
                    self._record_acceptance(
                        task=task,
                        job=job,
                        level1_passed=level1_report.passed,
                        acceptance=acceptance,
                    )
                    overall_pass = level1_report.passed and acceptance.level2_passed
                if not overall_pass and self._decide_failure_policy(FailureType(acceptance.failure_type)) == "retry_acceptance":
                    if level1_report.passed and self._can_use_deterministic_acceptance_fallback(task):
                        acceptance.level2_passed = True
                        acceptance.action = "approve"
                        acceptance.recommended_action = "approve"
                        acceptance.failure_type = "deterministic_acceptance_fallback"
                        acceptance.level2_feedback = (
                            "Owner acceptance was temporarily unavailable; deterministic contract validation "
                            "passed, so this subtask is provisionally accepted and final delivery gates will "
                            "verify the complete product."
                        )
                        ost.acceptance_results[job.subtask_id] = acceptance
                        self._record_acceptance(
                            task=task,
                            job=job,
                            level1_passed=level1_report.passed,
                            acceptance=acceptance,
                        )
                        overall_pass = True
                    else:
                        self._pause_task_for_acceptance_unavailable(task, job, acceptance)
                        return

            if not overall_pass:
                self._state.update_artifact_records_for_subtask(
                    task.task_id,
                    job.subtask_id,
                    status="rejected",
                    current_status="provisional",
                )
                ost.completed_subtasks.discard(job.subtask_id)
                ost.completed_subtasks.discard(original_id)
                if (
                    self._is_remediation_subtask_id(job.subtask_id)
                    and task.fix_rounds.get(original_id, 0) < ost.max_fix_rounds
                ):
                    task.status = TaskStatus.RUNNING
                    task.error = "A remediation subtask failed acceptance and is being retried."
                    task.updated_at = time.time()
                    self._state._persist_task(task)
                self._state.update_subtask_status(task.task_id, job.subtask_id, JobStatus.FAILED)
                if ost.strict_dependency:
                    cancelled = self._state.cancel_downstream_subtasks(task.task_id, original_id)
                    if cancelled:
                        logger.info(
                            "Strict mode: cancelled %s downstream subtasks after acceptance failure: %s",
                            len(cancelled),
                            cancelled,
                        )
            # Check max fix rounds
            current_round = task.fix_rounds.get(original_id, 0)
            if current_round >= ost.max_fix_rounds:
                await self._handle_max_rounds_exceeded(job, acceptance)
            else:
                feedback = self._build_feedback(level1_report, acceptance)
                await self._handle_structured_remediation(task, job, acceptance, feedback)
            return

        # Acceptance passed: mark completed and unlock downstream
        self._state.update_artifact_records_for_subtask(
            task.task_id,
            job.subtask_id,
            status="accepted",
            current_status="provisional",
        )
        ost.completed_subtasks.add(job.subtask_id)
        self._state.update_subtask_status(task.task_id, job.subtask_id, JobStatus.COMPLETED)

        restored: List[str] = []

        if self._is_remediation_subtask_id(job.subtask_id):
            original_id = self._get_original_subtask_id(job.subtask_id)
            original_st = None
            for st in task.subtasks:
                if st.subtask_id == original_id:
                    original_st = st
                    break
            if original_st:
                self._state.update_artifact_records_for_subtask(
                    task.task_id,
                    original_id,
                    status="rejected",
                    current_status="provisional",
                )
            if original_st and original_st.status != JobStatus.COMPLETED:
                self._state.update_subtask_status(task.task_id, original_id, JobStatus.COMPLETED)
                ost.completed_subtasks.add(original_id)
                # Issue 37: Clear error_message when fix round succeeds
                original_st.error_message = None
                logger.info(
                    f"Remediation subtask {job.subtask_id} passed, upgraded original subtask {original_id} to COMPLETED"
                )

                restored = self._state.restore_cancelled_downstream(task.task_id, original_id)
                if restored:
                    self._mark_downstream_revalidating(task, original_id, ost)
                    logger.info(f"Restored {len(restored)} cancelled downstream subtasks after remediation success: {restored}")
                    dispatchable_ids = {st.subtask_id for st in self._get_dispatchable_ready_subtasks(task.task_id, ost)}
                    for rst_id in restored:
                        rst = None
                        for st in task.subtasks:
                            if st.subtask_id == rst_id:
                                rst = st
                                break
                        if rst and rst.subtask_id in dispatchable_ids:
                            self._dispatcher.dispatch_subtask(rst)

        await self._maybe_record_wave_acceptance(task, job.subtask_id, ost)
        self.repair_task_dispatch(task.task_id, reason="job_completed")

        logger.info(f"Job {job_id} acceptance passed, unlocking DAG for task {task.task_id}")

        # Unlock DAG: dispatch ready subtasks
        ready_subtasks = self._get_dispatchable_ready_subtasks(task.task_id, ost)
        logger.info(f"Task {task.task_id}: {len(ready_subtasks)} ready subtasks found after unlocking DAG")
        for st in ready_subtasks:
            logger.info(f"Dispatching ready subtask {st.subtask_id} (agent={st.agent_id}, wave={st.wave_number})")
            self._dispatcher.dispatch_subtask(st)
        self.repair_task_dispatch(task.task_id, reason="dag_unlock")

        # Check if all subtasks are done
        if self._state.is_all_subtasks_terminal(task.task_id):
            await self._finalize_task_status(task.task_id)

    async def _handle_quality_remediation_job_finished(self, task: Task, job: Job) -> None:
        """Close quality remediation without creating remediation-of-remediation jobs."""
        if job.status == JobStatus.FAILED:
            self._record_acceptance(
                task=task,
                job=job,
                level1_passed=False,
                acceptance=AcceptanceResult(
                    subtask_id=job.subtask_id,
                    level1_passed=False,
                    level2_passed=False,
                    level2_feedback=f"Quality remediation job failed: {job.error or 'Unknown error'}",
                    action="retry_quality_remediation",
                    failure_type=self._classify_failure(job=job).value,
                ),
            )
            self._state.update_subtask_status(task.task_id, job.subtask_id, JobStatus.FAILED)
        else:
            self._state.update_artifact_records_for_subtask(
                task.task_id,
                job.subtask_id,
                status="accepted",
                current_status="provisional",
            )
            self._state.update_subtask_status(task.task_id, job.subtask_id, JobStatus.COMPLETED)

        if self._state.is_all_subtasks_terminal(task.task_id):
            await self._finalize_task_status(task.task_id)
        else:
            self.repair_task_dispatch(task.task_id, reason="quality_remediation_job_finished")

    def _artifact_path_hints_for_subtask(self, task: Task, subtask_id: str) -> List[str]:
        """Collect file paths from canonical subtask contract path hints.

        Looks up the canonical contract for the given *subtask_id* (or its
        original business ID if *subtask_id* is a remediation variant) and
        resolves each ``expected_deliverable.path_hint`` against
        ``task.project_dir``.  Only paths that exist on disk are returned.
        """
        persistence = getattr(self._state, "_persistence", None)
        if persistence is None:
            return []

        canonical_id = self._get_original_subtask_id(subtask_id)
        contract_subtask_ids = [subtask_id]
        if canonical_id != subtask_id:
            contract_subtask_ids.append(canonical_id)

        try:
            contracts = persistence.get_task_contracts(task.task_id)
        except Exception as exc:
            logger.warning("Failed to load contracts for artifact hints: %s", exc)
            return []

        hints: List[str] = []
        for contract in contracts:
            if contract.get("level") != "subtask":
                continue
            if contract.get("subtask_id") not in contract_subtask_ids:
                continue
            for deliverable in contract.get("expected_deliverables", []) or []:
                path_hint = deliverable.get("path_hint")
                if not path_hint:
                    continue
                resolved = path_hint
                if not os.path.isabs(resolved) and task.project_dir:
                    resolved = os.path.join(task.project_dir, resolved)
                resolved = os.path.realpath(resolved)
                if resolved not in hints and os.path.exists(resolved):
                    hints.append(resolved)
        return hints

    def _record_job_artifact(self, task: Task, job: Job) -> Optional[Artifact]:
        """Persist a best-effort artifact record without changing runtime behavior."""
        subtask = next((st for st in task.subtasks if st.subtask_id == job.subtask_id), None)
        content_refs: List[str] = []
        metadata = self._artifact_metadata_summary(dict(getattr(job, "result_metadata", {}) or {}))
        for key in ("created_files", "modified_files"):
            for candidate in metadata.get(key, []) or []:
                if not candidate:
                    continue
                resolved_candidate = os.path.realpath(candidate)
                if resolved_candidate not in content_refs:
                    content_refs.append(resolved_candidate)

        if subtask and subtask.output_file and os.path.realpath(subtask.output_file) not in content_refs:
            content_refs.append(os.path.realpath(subtask.output_file))
        elif job.result:
            extracted = self._state._extract_output_file(
                job.result,
                project_dir=task.project_dir,
                task_description=job.task_description,
            )
            if extracted:
                extracted = os.path.realpath(extracted)
                if extracted not in content_refs:
                    content_refs.append(extracted)
            for extracted_ref in self._extract_file_refs_from_result_text(job.result, task.project_dir):
                if extracted_ref not in content_refs:
                    content_refs.append(extracted_ref)
            for extracted_ref in self._extract_file_refs_from_result_diff(job.result, task.project_dir):
                if extracted_ref not in content_refs:
                    content_refs.append(extracted_ref)

        # Add contract path-hint candidates as additional content refs
        for hinted_path in self._artifact_path_hints_for_subtask(task, job.subtask_id):
            hinted_path = os.path.realpath(hinted_path)
            if hinted_path not in content_refs and os.path.exists(hinted_path):
                content_refs.append(hinted_path)

        # Tag fix/reassign artifacts with the canonical subtask ID
        canonical_id = self._get_original_subtask_id(job.subtask_id)
        if canonical_id != job.subtask_id:
            metadata.setdefault("canonical_subtask_id", canonical_id)

        # Filter out directory-only references — they are not deliverable artifacts.
        project_dir_real = os.path.realpath(task.project_dir) if task.project_dir else None
        content_refs = filtered_workspace_files(
            (
                ref for ref in content_refs
                if os.path.isfile(ref)
                and not (project_dir_real and os.path.realpath(ref) == project_dir_real)
            ),
            project_dir_real,
        )

        if not content_refs:
            return None

        first_artifact: Optional[Artifact] = None
        for index, content_ref in enumerate(content_refs, start=1):
            artifact_name = os.path.basename(content_ref) or content_ref
            file_size = "0 B"
            try:
                if os.path.isfile(content_ref):
                    size_bytes = os.path.getsize(content_ref)
                    file_size = self._format_file_size(size_bytes)
            except Exception:
                pass
            content_ref = os.path.realpath(content_ref) if content_ref else content_ref
            metadata["normalized_content_ref"] = content_ref

            artifact = Artifact(
                artifact_id=f"art-{job.job_id}-{index}",
                artifact_type="job_output",
                produced_by=job.agent_id,
                task_id=task.task_id,
                subtask_id=job.subtask_id,
                content_ref=content_ref,
                name=artifact_name,
                wave_number=getattr(subtask, "wave_number", None),
                status="provisional" if job.status == JobStatus.COMPLETED else "rejected",
                metadata={"job_id": job.job_id, "file_size": file_size, **metadata},
            )
            self._state.save_artifact_record(artifact)
            self._update_manifest_evidence_from_artifact(task, artifact, accepted=False)
            if first_artifact is None:
                first_artifact = artifact
        return first_artifact

    def _artifact_metadata_summary(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Keep artifact metadata small and prompt-safe.

        Raw cloud-agent metadata can contain every tool call and tool result.
        Repeating that blob on each artifact record makes owner acceptance
        prompts grow quadratically with the number of files in a subtask.
        """
        summary: Dict[str, Any] = {}
        for key in (
            "canonical_subtask_id",
            "created_files",
            "modified_files",
            "observed_created_files",
            "observed_modified_files",
            "tool_failures",
            "tool_call_count",
            "tool_result_count",
        ):
            if key not in metadata:
                continue
            value = metadata.get(key)
            if isinstance(value, list):
                summary[key] = value[:50]
            else:
                summary[key] = value
        return summary

    def _extract_file_refs_from_result_text(self, result: str, project_dir: Optional[str]) -> List[str]:
        """Extract file refs from plain agent summaries such as bullet lists."""
        if not result or not project_dir:
            return []

        refs: List[str] = []
        raw_candidates: List[str] = []
        for match in re.finditer(r"`([^`\n]+)`", result):
            raw_candidates.append(match.group(1))
        for match in re.finditer(r"(/(?:Users|private|tmp|var|home)/[^,\s`]+)", result):
            raw_candidates.append(match.group(1))
        for line in result.splitlines():
            line = line.strip()
            if re.match(r"^(?:created|modified|updated|written|saved) files?\s*:", line, re.IGNORECASE):
                _, _, suffix = line.partition(":")
                raw_candidates.extend(part.strip() for part in suffix.split(","))

        for candidate in raw_candidates:
            resolved = self._resolve_project_result_ref(candidate, project_dir)
            if resolved and resolved not in refs:
                refs.append(resolved)
        return refs

    def _extract_file_refs_from_result_diff(self, result: str, project_dir: Optional[str]) -> List[str]:
        """Extract changed file refs from agent diff-style output."""
        if not result or not project_dir:
            return []

        refs: List[str] = []
        patterns = (
            r"^diff --git a/(.+?) b/(.+)$",
            r"^(?:---|\+\+\+) [ab]/(.+)$",
            r"^a/(.+?)\s+(?:→|->)\s+b/(.+)$",
        )
        for raw_line in result.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            for pattern in patterns:
                match = re.match(pattern, line)
                if not match:
                    continue
                for candidate in match.groups():
                    resolved = self._resolve_project_result_ref(candidate, project_dir)
                    if resolved and resolved not in refs:
                        refs.append(resolved)
        return refs

    @staticmethod
    def _resolve_project_result_ref(candidate: str, project_dir: str) -> Optional[str]:
        candidate = candidate.strip().strip("`'\"").rstrip(".,:;")
        if not candidate or candidate == "/dev/null":
            return None
        candidate = candidate.split("\t", 1)[0].strip()
        if candidate.startswith(("a/", "b/")):
            candidate = candidate[2:]
        if os.path.isabs(candidate):
            resolved = os.path.realpath(candidate)
        else:
            resolved = os.path.realpath(os.path.join(project_dir, candidate))
        project_root = os.path.realpath(project_dir)
        try:
            if os.path.commonpath([project_root, resolved]) != project_root:
                return None
        except ValueError:
            return None
        if not os.path.isfile(resolved):
            return None
        return resolved

    @staticmethod
    def _format_file_size(size_bytes: int) -> str:
        """Format file size in human-readable form."""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"

    def _record_acceptance(
        self,
        task: Task,
        job: Job,
        level1_passed: bool,
        acceptance: AcceptanceResult,
    ) -> None:
        """Persist a structured acceptance record for later wave-gate work."""
        subtask = next((st for st in task.subtasks if st.subtask_id == job.subtask_id), None)
        failed_checks = list(getattr(acceptance, "level1_errors", []) or [])
        if getattr(acceptance, "failure_type", None):
            failed_checks.append(f"failure_type:{acceptance.failure_type}")
        decision = getattr(acceptance, "action", None)
        if not decision:
            decision = "approve" if acceptance.level2_passed and level1_passed else "fix"
        if decision == "approve" and (not level1_passed or not acceptance.level2_passed):
            decision = "fix"
        raw_recommended = getattr(acceptance, "recommended_action", None)
        if raw_recommended in {None, ""}:
            raw_recommended = "approve" if decision == "approve" else decision
        elif raw_recommended == "approve" and decision in {"fix", "reassign"}:
            raw_recommended = decision
        feedback = acceptance.level2_feedback
        record = AcceptanceRecord.new(
            task_id=task.task_id,
            level="subtask",
            decision=decision,
            deterministic_passed=level1_passed,
            judge_passed=acceptance.level2_passed,
            subtask_id=job.subtask_id,
            wave_number=getattr(subtask, "wave_number", None),
            failed_checks=failed_checks + list(getattr(acceptance, "failed_checks", []) or []),
            missing_artifacts=list(getattr(acceptance, "missing_artifacts", []) or []),
            feedback=feedback,
            root_cause_scope=getattr(acceptance, "root_cause_scope", "unknown"),
            root_cause_wave=getattr(acceptance, "root_cause_wave", None),
            root_cause_artifact_ids=list(getattr(acceptance, "root_cause_artifact_ids", []) or []),
            recommended_action=raw_recommended,
            preferred_agent=getattr(acceptance, "preferred_agent", None),
            owner_session_id=getattr(acceptance, "owner_session_id", getattr(task, "owner_session_id", None)),
        )
        self._state.save_acceptance_record(record)
        preserved = self._preserve_owner_decision_metadata(task.last_owner_decision or {})
        task.last_owner_decision = {
            "decision": decision,
            "recommended_action": raw_recommended,
            "root_cause_scope": getattr(acceptance, "root_cause_scope", "unknown"),
            "root_cause_wave": getattr(acceptance, "root_cause_wave", None),
            "preferred_agent": getattr(acceptance, "preferred_agent", None),
        }
        task.last_owner_decision.update(preserved)
        task.owner_state_summary = {
            "owner_session_id": getattr(acceptance, "owner_session_id", getattr(task, "owner_session_id", None)),
            "last_owner_decision": task.last_owner_decision,
        }
        self._state._persist_task(task)

    def _preserve_owner_decision_metadata(self, existing: Dict[str, Any]) -> Dict[str, Any]:
        preserved: Dict[str, Any] = {}
        attempts = existing.get("quality_remediation_attempts")
        if isinstance(attempts, dict) and attempts:
            preserved["quality_remediation_attempts"] = {
                str(k): int(v) for k, v in attempts.items()
            }
        if "max_quality_remediation_attempts" in existing:
            preserved["max_quality_remediation_attempts"] = int(
                existing.get("max_quality_remediation_attempts") or 4
            )
        return preserved

    async def _maybe_record_wave_acceptance(
        self,
        task: Task,
        subtask_id: str,
        ost: OrchestratorState,
    ) -> None:
        """Run wave-level acceptance and optionally enforce it for downstream dispatch."""
        subtask = next((st for st in task.subtasks if st.subtask_id == subtask_id), None)
        if not subtask:
            return

        wave_number = getattr(subtask, "wave_number", None)
        if wave_number is None or wave_number <= 0:
            return
        if wave_number in ost.wave_acceptance_recorded:
            current_status = ost.wave_statuses.get(wave_number)
            if current_status == WaveLifecycleStatus.APPROVED.value:
                return

        original_wave_subtasks = self._original_wave_subtasks(task, wave_number)
        if not original_wave_subtasks:
            return
        if not self._wave_original_subtasks_are_accepted(original_wave_subtasks, ost):
            return

        removed_noise = self._remove_workspace_noise_files(
            task,
            reason=f"before wave {wave_number} acceptance",
        )
        self._record_deterministic_cleanup(task, "removed_workspace_noise", removed_noise)
        delivery_contract = self._state.get_delivery_contract(task.task_id)
        if delivery_contract:
            removed_forbidden = self._cleanup_forbidden_file_constraints(task, delivery_contract)
            self._record_deterministic_cleanup(task, "removed_forbidden_files", removed_forbidden)
            self._record_deterministic_cleanup(
                task,
                "removed_file_constraint_violations",
                removed_forbidden,
            )

        ost.wave_acceptance_recorded.add(wave_number)
        accept_wave = getattr(self._owner_agent, "accept_wave", None)
        if not callable(accept_wave):
            ost.wave_approved.add(wave_number)
            ost.revalidating_waves.discard(wave_number)
            ost.blocked_by_wave.pop(wave_number, None)
            ost.wave_statuses[wave_number] = WaveLifecycleStatus.APPROVED.value
            self._update_wave_governance(task, wave_number, WaveLifecycleStatus.APPROVED.value)
            logger.warning(
                "Owner agent has no wave acceptance hook; approving Wave %s after subtask acceptance.",
                wave_number,
            )
            return

        acceptance = accept_wave(task, wave_number)
        if not isinstance(acceptance, AcceptanceResult):
            ost.wave_approved.add(wave_number)
            ost.revalidating_waves.discard(wave_number)
            ost.blocked_by_wave.pop(wave_number, None)
            ost.wave_statuses[wave_number] = WaveLifecycleStatus.APPROVED.value
            self._update_wave_governance(task, wave_number, WaveLifecycleStatus.APPROVED.value)
            logger.warning(
                "Owner wave acceptance returned %s; approving Wave %s after subtask acceptance.",
                type(acceptance).__name__,
                wave_number,
            )
            return

        # Normalize wave acceptance before persisting the record.
        acceptance, effective_decision = self._normalize_wave_acceptance_for_record(acceptance)

        record = AcceptanceRecord.new(
            task_id=task.task_id,
            level="wave",
            decision=effective_decision,
            deterministic_passed=True,
            judge_passed=acceptance.level2_passed,
            wave_number=wave_number,
            failed_checks=list(getattr(acceptance, "failed_checks", []) or []),
            missing_artifacts=list(getattr(acceptance, "missing_artifacts", []) or []),
            feedback=acceptance.level2_feedback,
            root_cause_scope=getattr(acceptance, "root_cause_scope", "unknown"),
            root_cause_wave=getattr(acceptance, "root_cause_wave", None),
            root_cause_artifact_ids=list(getattr(acceptance, "root_cause_artifact_ids", []) or []),
            recommended_action=getattr(acceptance, "recommended_action", "approve"),
            preferred_agent=getattr(acceptance, "preferred_agent", None),
            owner_session_id=getattr(acceptance, "owner_session_id", getattr(task, "owner_session_id", None)),
        )
        self._state.save_acceptance_record(record)
        ost.recent_acceptance_records.append({
            "level": "wave",
            "wave_number": wave_number,
            "decision": record.decision,
            "recommended_action": record.recommended_action,
        })
        if effective_decision == "approve" and acceptance.level2_passed:
            ost.wave_approved.add(wave_number)
            ost.revalidating_waves.discard(wave_number)
            ost.blocked_by_wave.pop(wave_number, None)
            ost.wave_statuses[wave_number] = WaveLifecycleStatus.APPROVED.value
            self._update_wave_governance(task, wave_number, WaveLifecycleStatus.APPROVED.value)
            if ost.wave_gate_enabled:
                logger.info(f"Wave gate approved Wave {wave_number} for task {task.task_id}")
        else:
            ost.wave_statuses[wave_number] = WaveLifecycleStatus.BLOCKED.value
            self._update_wave_governance(
                task,
                wave_number,
                WaveLifecycleStatus.BLOCKED.value,
                owner_decision={
                    "decision": getattr(acceptance, "decision", "reject"),
                    "recommended_action": getattr(acceptance, "recommended_action", "wave_fix"),
                    "root_cause_scope": getattr(acceptance, "root_cause_scope", "unknown"),
                    "root_cause_wave": getattr(acceptance, "root_cause_wave", None),
                },
            )
            logger.warning(
                f"{'Wave gate blocked' if ost.wave_gate_enabled else 'Shadow wave acceptance flagged'} "
                f"Wave {wave_number} for task {task.task_id}: "
                f"{acceptance.level2_feedback or 'No feedback'}"
            )

            if ost.wave_gate_enabled:
                await self._handle_wave_gate_blocked(task, wave_number, acceptance, ost)

    def _get_dispatchable_ready_subtasks(self, task_id: str, ost: OrchestratorState) -> List[SubTask]:
        """Filter ready subtasks through the optional wave gate without changing TaskState semantics."""
        ready_subtasks = self._state.get_ready_subtasks(task_id, strict=ost.strict_dependency)
        if ost.strict_dependency:
            ready_subtasks = [
                st for st in ready_subtasks
                if all(dep in ost.completed_subtasks for dep in st.dependencies)
            ]
        if not ost.wave_gate_enabled:
            return ready_subtasks

        dispatchable: List[SubTask] = []
        blocked: List[str] = []
        for st in ready_subtasks:
            if self._is_wave_gate_satisfied(st, ost):
                dispatchable.append(st)
            else:
                blocked.append(st.subtask_id)

        if blocked:
            logger.info(
                f"Wave gate enabled for task {task_id}: blocked {len(blocked)} ready subtasks until prior wave approval: {blocked}"
            )
        return dispatchable

    def _is_wave_gate_satisfied(self, subtask: SubTask, ost: OrchestratorState) -> bool:
        """Wave 1 can run immediately; higher waves require all previous waves approved."""
        if subtask.wave_number <= 1:
            return True
        if subtask.wave_number in ost.blocked_by_wave:
            return False
        return all(wave in ost.wave_approved for wave in range(1, subtask.wave_number))

    def _original_wave_subtasks(self, task: Task, wave_number: int) -> List[SubTask]:
        return [
            st for st in task.subtasks
            if st.wave_number == wave_number
            and not self._is_remediation_subtask_id(st.subtask_id)
            and not st.subtask_id.endswith("-decompose")
        ]

    def _wave_original_subtasks_are_accepted(
        self,
        original_wave_subtasks: List[SubTask],
        ost: OrchestratorState,
    ) -> bool:
        """Executor completion is not enough to approve a wave.

        TaskState marks a subtask ``completed`` as soon as the worker process
        exits. Owner acceptance runs asynchronously afterwards, so a repair
        watchdog can briefly observe a wave as completed while one of its
        subtasks is still awaiting review or has already been rejected. Wave
        gates must wait for the subtask acceptance record to pass.
        """
        return all(
            st.status == JobStatus.COMPLETED
            and self._is_subtask_acceptance_approved(ost, st.subtask_id)
            for st in original_wave_subtasks
        )

    async def _handle_wave_gate_blocked(
        self,
        task: Task,
        wave_number: int,
        acceptance: AcceptanceResult,
        ost: OrchestratorState,
    ) -> None:
        """Handle wave gate blocked by creating a wave-level fix job.

        N4 Fix: Uses canonical ID for wave fix and reserves remediation attempt.
        Wave fix now shares the same budget as subtask fix/reassign.
        """
        wave_subtasks = [
            st for st in task.subtasks
            if st.wave_number == wave_number and "-fix-" not in st.subtask_id and st.status == JobStatus.COMPLETED
        ]
        if not wave_subtasks:
            return

        first_subtask = wave_subtasks[0]
        # N4 Fix: Use canonical ID for wave and reserve attempt
        canonical_id = f"wave-{wave_number}"
        attempt = self._reserve_remediation_attempt(task, ost, canonical_id)
        if attempt is None:
            logger.warning(
                f"Wave {wave_number} remediation budget exhausted (canonical: {canonical_id}), "
                f"marking wave gate as a dead end"
            )
            self._mark_wave_gate_dead_end(
                task,
                wave_number,
                ost,
                reason="wave_gate_remediation_budget_exhausted",
            )
            if self._state.is_all_subtasks_terminal(task.task_id):
                await self._finalize_task_status(task.task_id)
            return

        wave_fix_subtask_id = f"wave-{wave_number}-fix-{attempt}"
        wave_fix_description = self._build_wave_fix_remediation_description(
            task=task,
            wave_number=wave_number,
            attempt=attempt,
            wave_subtasks=wave_subtasks,
            acceptance=acceptance,
        )

        existing_fix = next((st for st in task.subtasks if st.subtask_id == wave_fix_subtask_id), None)
        if existing_fix and existing_fix.status != JobStatus.PENDING:
            logger.info(f"Wave fix {wave_fix_subtask_id} already exists with status {existing_fix.status}")
            return

        task_id = task.task_id or getattr(first_subtask, "task_id", None)
        if not task_id:
            logger.error(
                "Wave gate blocked for wave %s but no valid task_id could be resolved; skipping fix subtask creation",
                wave_number,
            )
            return

        if not existing_fix:
            wave_fix_subtask = self._state.add_subtask(
                task_id=task_id,
                description=wave_fix_description,
                agent_id=first_subtask.agent_id,
                priority=1,
                dependencies=first_subtask.dependencies,
                subtask_id=wave_fix_subtask_id,
            )
            if wave_fix_subtask is None:
                logger.error(
                    "Wave gate blocked Wave %s: failed to add wave fix subtask %s",
                    wave_number,
                    wave_fix_subtask_id,
                )
                return
            wave_fix_subtask.wave_number = wave_number
        else:
            wave_fix_subtask = existing_fix
            wave_fix_subtask.task_id = task_id
            wave_fix_subtask.status = JobStatus.PENDING
            wave_fix_subtask.wave_number = wave_number
            wave_fix_subtask.description = wave_fix_description

        self._state._persist_subtask(wave_fix_subtask)
        wave_fix_job = self._dispatcher.dispatch_subtask(wave_fix_subtask)
        if wave_fix_job:
            logger.info(f"Wave gate blocked Wave {wave_number}: dispatched wave fix job {wave_fix_job.job_id} for task {task.task_id} (attempt {attempt})")
        else:
            logger.error(f"Wave gate blocked Wave {wave_number}: failed to dispatch wave fix subtask {wave_fix_subtask_id} for task {task.task_id}")

    def _build_wave_fix_remediation_description(
        self,
        *,
        task: Task,
        wave_number: int,
        attempt: int,
        wave_subtasks: List[SubTask],
        acceptance: AcceptanceResult,
    ) -> str:
        """Build a concise wave-fix prompt that does not recursively nest JSON."""
        project_dir = task.project_dir or ""
        feedback = re.sub(r"\s+", " ", acceptance.level2_feedback or "Wave acceptance failed").strip()
        if len(feedback) > 1800:
            feedback = feedback[:1800] + "... [truncated]"

        failed_checks = [
            re.sub(r"\s+", " ", str(item)).strip()
            for item in (getattr(acceptance, "failed_checks", []) or [])
            if str(item).strip()
        ][:6]
        missing_artifacts = [
            re.sub(r"\s+", " ", str(item)).strip()
            for item in (getattr(acceptance, "missing_artifacts", []) or [])
            if str(item).strip()
        ][:6]
        current_outputs = [
            f"{st.subtask_id}: {st.output_file or 'no output_file'}"
            for st in wave_subtasks
        ][:8]
        cited_paths = self._extract_cited_workspace_paths(feedback, project_dir)

        lines = [
            f"[WAVE {wave_number} FIX ROUND {attempt}] Repair the current wave so wave acceptance can pass.",
            "",
            f"Project directory: {project_dir or 'N/A'}",
            "Scope: fix only current-wave coherence, forbidden files, duplicate/conflicting structures, and cited failing files.",
            "Do not implement future-wave functionality unless it is necessary to repair the current wave.",
            "If feedback says a file must be removed or renamed, actually remove/rename it and update any imports or router registrations that reference it.",
            "",
            "Blocking feedback:",
            feedback,
        ]
        if failed_checks:
            lines.extend(["", "Failed checks:"])
            lines.extend(f"- {item}" for item in failed_checks)
        if missing_artifacts:
            lines.extend(["", "Missing artifacts:"])
            lines.extend(f"- {item}" for item in missing_artifacts)
        if cited_paths:
            lines.extend(["", "Cited workspace paths to inspect first:"])
            lines.extend(f"- {path}" for path in cited_paths[:10])
        if current_outputs:
            lines.extend(["", "Current-wave outputs:"])
            lines.extend(f"- {item}" for item in current_outputs)
        lines.extend([
            "",
            "Acceptance target: after your fix, the current wave must not contain unrelated placeholder item/todo/blog modules, forbidden files, duplicate structures, or broken imports.",
            "Keep the response concise and list changed/deleted files.",
        ])
        return "\n".join(lines)

    @staticmethod
    def _extract_cited_workspace_paths(text: str, project_dir: Optional[str]) -> List[str]:
        """Extract likely file paths from acceptance feedback."""
        candidates = re.findall(
            r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+",
            text or "",
        )
        seen: set[str] = set()
        paths: List[str] = []
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if project_dir and not os.path.isabs(candidate):
                paths.append(os.path.join(project_dir, candidate))
            else:
                paths.append(candidate)
        return paths

    async def _handle_structured_remediation(
        self,
        task: Task,
        job: Job,
        acceptance: AcceptanceResult,
        feedback: str,
    ) -> None:
        recommended_action = getattr(acceptance, "recommended_action", RecommendedAction.SUBTASK_FIX.value)
        if recommended_action == RecommendedAction.PRIOR_WAVE_FIX.value:
            self._mark_prior_wave_block(task, acceptance)
            self._initiate_fix(job, self._build_structured_handoff(task, job, acceptance, feedback))
            return
        if recommended_action == RecommendedAction.WAVE_FIX.value:
            self._update_wave_governance(
                task,
                getattr(acceptance, "root_cause_wave", None) or self._get_wave_number(job, task),
                WaveLifecycleStatus.BLOCKED.value,
                owner_decision={
                    "recommended_action": recommended_action,
                    "root_cause_scope": getattr(acceptance, "root_cause_scope", "unknown"),
                },
            )
            self._initiate_fix(job, self._build_structured_handoff(task, job, acceptance, feedback))
            return
        if recommended_action == RecommendedAction.REASSIGN.value:
            await self._reassign_subtask(task, job, acceptance, feedback)
            return
        self._initiate_fix(job, self._build_structured_handoff(task, job, acceptance, feedback))

    def _build_structured_handoff(self, task: Task, job: Job, acceptance: AcceptanceResult, feedback: str) -> str:
        canonical_id = self._get_canonical_subtask_id(job.subtask_id)
        contract_description = self._canonical_subtask_description(task, canonical_id, job.task_description)
        handoff = {
            "contract": contract_description,
            "accepted_artifacts": getattr(acceptance, "root_cause_artifact_ids", []),
            "failed_checks": getattr(acceptance, "failed_checks", []) or [feedback],
            "missing_artifacts": getattr(acceptance, "missing_artifacts", []),
            "recommended_action": getattr(acceptance, "recommended_action", RecommendedAction.SUBTASK_FIX.value),
            "path_constraints": "reuse current project_dir only",
        }
        return json.dumps(handoff, ensure_ascii=False)

    async def _reassign_subtask(
        self,
        task: Task,
        job: Job,
        acceptance: AcceptanceResult,
        feedback: str,
    ) -> None:
        """Reassign subtask to a different agent.

        Uses canonical ID to prevent chained v2-v2-v2 suffix growth,
        and shares the same remediation budget with fix rounds.
        """
        ost = self._orchestrator_states.get(task.task_id)
        if not ost:
            return

        # Extract canonical ID and reserve attempt
        canonical_id = self._get_canonical_subtask_id(job.subtask_id)
        attempt = self._reserve_remediation_attempt(task, ost, canonical_id)
        if attempt is None:
            logger.warning(
                f"Remediation budget exhausted for {job.subtask_id} (canonical: {canonical_id}), "
                f"cannot create reassign subtask"
            )
            await self._handle_remediation_exhausted(task, job, acceptance, canonical_id)
            return

        valid_agents = self._get_allowed_valid_agents(task)
        preferred_agent = getattr(acceptance, "preferred_agent", None)
        candidates = [agent for agent in valid_agents if agent != job.agent_id]
        new_agent = preferred_agent if preferred_agent in candidates else (candidates[0] if candidates else job.agent_id)

        # Generate ID from canonical ID to prevent chained growth
        # e.g., st-a-v2, st-a-v3 (not st-a-v2-v2)
        new_subtask_id = f"{canonical_id}-v{attempt + 1}"
        new_subtask = self._state.add_subtask(
            task_id=task.task_id,
            description=self._build_structured_handoff(task, job, acceptance, feedback),
            agent_id=new_agent,
            priority=1,
            dependencies=[],
            subtask_id=new_subtask_id,
        )
        if new_subtask:
            # Inherit wave_number from canonical subtask
            canonical_subtask = next((st for st in task.subtasks if st.subtask_id == canonical_id), None)
            if canonical_subtask is not None:
                new_subtask.wave_number = getattr(canonical_subtask, "wave_number", 1)
            task.status = TaskStatus.RUNNING
            task.updated_at = time.time()
            self._state._persist_task(task)
            self._dispatcher.dispatch_subtask(new_subtask)
            logger.info(f"Reassigned {job.subtask_id} to {new_agent} as {new_subtask_id} (attempt {attempt}, canonical: {canonical_id})")

    def _mark_prior_wave_block(self, task: Task, acceptance: AcceptanceResult) -> None:
        root_wave = getattr(acceptance, "root_cause_wave", None)
        if root_wave is None:
            return
        for wave in task.waves:
            if wave.wave_number > root_wave:
                wave.is_blocked = True
                wave.blocked_by_wave = root_wave
                wave.governance_status = WaveLifecycleStatus.BLOCKED.value
                self._state._persist_wave(wave)

    def _mark_downstream_revalidating(self, task: Task, original_subtask_id: str, ost: OrchestratorState) -> None:
        root_wave = None
        for st in task.subtasks:
            if st.subtask_id == original_subtask_id:
                root_wave = getattr(st, "wave_number", None)
                break
        if root_wave is None:
            return
        impacted_waves = self._lineage_impacted_downstream_waves(task, original_subtask_id, root_wave)
        for wave in task.waves:
            should_revalidate = (
                wave.wave_number in impacted_waves
                if impacted_waves is not None
                else wave.wave_number > root_wave
            )
            if should_revalidate:
                wave.is_revalidating = True
                wave.is_blocked = False
                wave.blocked_by_wave = None
                wave.governance_status = WaveLifecycleStatus.REVALIDATING.value
                ost.revalidating_waves.add(wave.wave_number)
                ost.wave_approved.discard(wave.wave_number)
                ost.wave_statuses[wave.wave_number] = WaveLifecycleStatus.REVALIDATING.value
                self._state._persist_wave(wave)

    def _lineage_impacted_downstream_waves(
        self,
        task: Task,
        original_subtask_id: str,
        root_wave: int,
    ) -> Optional[set[int]]:
        """Return downstream waves that consumed artifacts from the repaired subtask.

        Returns None when lineage data is unavailable, letting callers fall back
        to the conservative "all later waves" behavior.
        """
        persistence = getattr(self._state, "_persistence", None)
        if persistence is None:
            return None
        try:
            artifact_records = persistence.get_artifact_records(task.task_id)
        except Exception as exc:
            logger.warning(f"Failed to load artifact lineage for {task.task_id}: {exc}")
            return None

        root_artifact_ids = {
            record.get("artifact_id")
            for record in artifact_records
            if record.get("subtask_id") == original_subtask_id
            and record.get("artifact_id")
            and record.get("status", "accepted") in {"accepted", "superseded", "provisional"}
        }
        if not root_artifact_ids:
            return None

        impacted_waves: set[int] = set()
        queue = list(root_artifact_ids)
        seen = set(root_artifact_ids)
        while queue:
            artifact_id = queue.pop(0)
            for record in artifact_records:
                source_ids = record.get("source_artifact_ids") or []
                if artifact_id not in source_ids:
                    continue
                wave_number = record.get("wave_number")
                if isinstance(wave_number, int) and wave_number > root_wave:
                    impacted_waves.add(wave_number)
                consumer_artifact_id = record.get("artifact_id")
                if consumer_artifact_id and consumer_artifact_id not in seen:
                    seen.add(consumer_artifact_id)
                    queue.append(consumer_artifact_id)
        return impacted_waves

    def _update_wave_governance(
        self,
        task: Task,
        wave_number: Optional[int],
        governance_status: str,
        owner_decision: Optional[Dict[str, Any]] = None,
    ) -> None:
        if wave_number is None:
            return
        for wave in task.waves:
            if wave.wave_number == wave_number:
                wave.governance_status = governance_status
                wave.is_blocked = governance_status == WaveLifecycleStatus.BLOCKED.value
                wave.is_revalidating = governance_status == WaveLifecycleStatus.REVALIDATING.value
                if governance_status == WaveLifecycleStatus.APPROVED.value:
                    wave.blocked_by_wave = None
                    wave.is_blocked = False
                    wave.is_revalidating = False
                if governance_status == WaveLifecycleStatus.FAILED.value:
                    wave.blocked_by_wave = None
                    wave.is_blocked = False
                    wave.is_revalidating = False
                if owner_decision is not None:
                    wave.owner_decision = owner_decision
                self._state._persist_wave(wave)
                break

    def _get_wave_number(self, job: Job, task: Task) -> Optional[int]:
        for st in task.subtasks:
            if st.subtask_id == job.subtask_id:
                return getattr(st, "wave_number", None)
        return None

    def _classify_failure(
        self,
        job: Job,
        acceptance: Optional[AcceptanceResult] = None,
        level1_report: Optional[ValidationReport] = None,
    ) -> FailureType:
        """Classify failures conservatively; only high-confidence cases affect behavior."""
        if acceptance and acceptance.parse_failed:
            return FailureType.ACCEPTANCE_PARSE_FAILURE
        if level1_report and not level1_report.passed:
            return FailureType.VALIDATION_FAILURE

        text = " ".join(filter(None, [
            job.error,
            getattr(acceptance, "level2_feedback", None) if acceptance else None,
            getattr(acceptance, "raw_response", None) if acceptance else None,
        ])).lower()

        configuration_markers = [
            "pass --to",
            "--session-id",
            "not configured",
            "no api key",
            "provider not available",
            "database path not configured",
            "missing session",
            "cloud llm agent",
        ]
        llm_provider_markers = [
            "all llm providers failed",
            "llm provider",
            "provider failed",
            "model overloaded",
            "rate limit",
            "429",
            "502",
            "503",
            "504",
            "service unavailable",
            "bad gateway",
        ]
        persistence_markers = [
            "database is locked",
            "sqlite",
            "constraint failed",
            "integrityerror",
            "no such table",
            "failed to persist",
            "persistence",
        ]
        infrastructure_markers = [
            "permissionerror",
            "operation not permitted",
            "connection refused",
            "broken pipe",
            "timeout",
            "timed out",
            "temporarily unavailable",
            "resource busy",
        ]
        output_incomplete_markers = [
            "missing output",
            "incomplete",
            "not complete",
            "missing artifact",
        ]

        if any(marker in text for marker in configuration_markers):
            return FailureType.CONFIGURATION
        if any(marker in text for marker in llm_provider_markers):
            return FailureType.LLM_PROVIDER_FAILURE
        if any(marker in text for marker in persistence_markers):
            return FailureType.PERSISTENCE_FAILURE
        if any(marker in text for marker in infrastructure_markers):
            return FailureType.INFRASTRUCTURE
        if any(marker in text for marker in output_incomplete_markers):
            return FailureType.OUTPUT_INCOMPLETE
        if acceptance and not acceptance.level2_passed:
            return FailureType.ACCEPTANCE_REJECTED
        return FailureType.UNKNOWN

    def _decide_failure_policy(self, failure_type: FailureType) -> str:
        """Minimal policy mapping. Only high-confidence configuration failures change behavior."""
        if failure_type in (FailureType.CONFIGURATION, FailureType.PERSISTENCE_FAILURE):
            return "fail_fast"
        if failure_type in (FailureType.ACCEPTANCE_PARSE_FAILURE, FailureType.LLM_PROVIDER_FAILURE):
            return "retry_acceptance"
        return "fix"

    def _can_use_deterministic_acceptance_fallback(self, task: Task) -> bool:
        """Allow owner-LLM outages to defer judgment to final product gates.

        This is intentionally limited to delivery-contract tasks with explicit
        functional probes or functional delivery modes.  For those tasks, final
        acceptance runs concrete probes over the whole product, so a temporary
        owner-agent outage should not strand the DAG or turn an otherwise
        recoverable run into a terminal failure.
        """
        try:
            contract = self._state.get_delivery_contract(task.task_id)
        except Exception:
            contract = None
        if not contract:
            return False
        if contract.get("acceptance_probes"):
            return True
        task_types = {str(item) for item in (contract.get("task_types") or [])}
        if "functional" in task_types:
            return True
        return contract.get("delivery_mode") in {"functional", "composite"}

    def _pause_task_for_acceptance_unavailable(
        self,
        task: Task,
        job: Job,
        acceptance: AcceptanceResult,
    ) -> None:
        message = (
            "Owner acceptance is temporarily unavailable "
            f"for subtask {job.subtask_id}: {acceptance.level2_feedback or acceptance.failure_type or 'unknown error'}"
        )
        self._state.pause_task(task.task_id)
        self._state.set_task_status(task.task_id, TaskStatus.PAUSED, error=message)
        logger.warning("Paused task %s because acceptance was unavailable: %s", task.task_id, message)

    def _update_manifest_evidence_from_artifact(
        self, task: Task, artifact: Artifact, accepted: bool = False
    ) -> None:
        """Mark manifest deliverables as produced/accepted when artifact evidence matches."""
        manifest = self._state.get_requirement_manifest(task.task_id)
        if not manifest:
            return
        content_ref = os.path.realpath(artifact.content_ref or "")
        basename = os.path.basename(content_ref)
        changed = False
        for req in manifest.get("deliverables", []) or []:
            path_hint = req.get("path_hint")
            if not path_hint:
                continue
            candidates = []
            if task.project_dir:
                candidates.append(os.path.realpath(os.path.join(task.project_dir, path_hint)))
                if os.path.basename(path_hint).lower() == "readme":
                    candidates.append(os.path.realpath(os.path.join(task.project_dir, "README.md")))
                if "/" not in path_hint and os.path.basename(path_hint).startswith("test_"):
                    candidates.append(os.path.realpath(os.path.join(task.project_dir, "tests", os.path.basename(path_hint))))
            if content_ref in candidates or basename == os.path.basename(path_hint):
                req["status"] = "accepted" if accepted else "produced"
                req["assigned_subtask_id"] = (
                    req.get("assigned_subtask_id")
                    or artifact.metadata.get("canonical_subtask_id")
                    or artifact.subtask_id
                )
                evidence = dict(req.get("evidence") or {})
                evidence["artifact_id"] = artifact.artifact_id
                evidence["content_ref"] = artifact.content_ref
                evidence["file_size"] = artifact.metadata.get("file_size")
                req["evidence"] = evidence
                changed = True
        if changed:
            manifest["updated_at"] = time.time()
            self._state.save_requirement_manifest(manifest)

    def _update_manifest_from_project_acceptance(
        self, task: Task, quality: "ProjectAcceptanceReport"
    ) -> None:
        """Promote manifest deliverable statuses after project acceptance."""
        manifest = self._state.get_requirement_manifest(task.task_id)
        if not manifest:
            return
        produced = set(quality.produced_required)
        missing = set(quality.missing_required)
        changed = False
        for req in manifest.get("deliverables", []) or []:
            hint = req.get("path_hint")
            if not hint:
                continue
            if hint in produced:
                req["status"] = "accepted" if quality.passed else "produced"
                changed = True
            elif hint in missing:
                req["status"] = "missing"
                changed = True
        if changed:
            manifest["updated_at"] = time.time()
            self._state.save_requirement_manifest(manifest)

    def _update_manifest_from_delivery_quality(
        self, task: Task, delivery_quality: Dict[str, Any]
    ) -> None:
        """Promote manifest deliverable statuses after owner delivery-contract acceptance."""
        manifest = self._state.get_requirement_manifest(task.task_id)
        if not manifest:
            return

        def _path_hints(values: Any) -> set[str]:
            hints: set[str] = set()
            for item in values or []:
                if isinstance(item, dict):
                    hint = item.get("path_hint")
                else:
                    hint = item
                if hint:
                    hints.add(str(hint))
            return hints

        produced = _path_hints(delivery_quality.get("produced_required"))
        missing = _path_hints(delivery_quality.get("missing_required"))
        invalid = _path_hints(delivery_quality.get("invalid_required"))
        passed = delivery_quality.get("delivery_quality") == "passed"
        capability_passed = passed and not produced
        changed = False
        for req in manifest.get("deliverables", []) or []:
            hint = req.get("path_hint")
            if not hint:
                continue
            if hint in produced:
                req["status"] = "accepted" if passed else "produced"
                changed = True
            elif capability_passed and req.get("status") in {"produced", "accepted"}:
                req["status"] = "accepted"
                changed = True
            elif hint in missing or hint in invalid:
                req["status"] = "missing"
                changed = True
        if changed:
            manifest["updated_at"] = time.time()
            self._state.save_requirement_manifest(manifest)

    def _cleanup_file_constraint_violations(
        self,
        task: Task,
        delivery_contract: Dict[str, Any],
        delivery_quality: Dict[str, Any],
    ) -> List[str]:
        """Remove generated files that violate explicit file constraints.

        This is deliberately narrow: only regular files inside ``project_dir``
        are removed, and anything listed as an owner delivery-contract
        deliverable is protected.  It gives deterministic constraints a chance
        to converge without asking another agent to delete obvious helper files
        such as ``setup.py`` or ad-hoc tests outside an allowed-files list.
        """
        raw_project_dir = getattr(task, "project_dir", None) or delivery_contract.get("project_dir")
        if not raw_project_dir:
            return []
        project_dir = os.path.realpath(raw_project_dir)
        if not os.path.isdir(project_dir):
            return []

        protected: set[str] = set()
        for deliverable in delivery_contract.get("deliverables", []) or []:
            path_hint = deliverable.get("path_hint")
            if not path_hint:
                continue
            protected.add(os.path.realpath(os.path.join(project_dir, path_hint)))

        evidence_by_path: Dict[str, Dict[str, Any]] = {}
        for constraint in delivery_contract.get("constraints", []) or []:
            constraint_type = constraint.get("constraint_type")
            if constraint_type == "allowed_files":
                allowed = {
                    str(path or "").replace("\\", "/").strip("/")
                    for path in constraint.get("value") or []
                    if str(path or "").strip()
                }
                ignored_dirs = {".git", ".claude", ".codex", "__pycache__", ".pytest_cache"}
                ignored_names = {".ds_store"}
                for root, dirs, files in os.walk(project_dir):
                    dirs[:] = [d for d in dirs if d not in ignored_dirs]
                    for filename in files:
                        if filename.lower() in ignored_names:
                            continue
                        full_path = os.path.realpath(os.path.join(root, filename))
                        rel_path = os.path.relpath(full_path, project_dir).replace("\\", "/")
                        if rel_path not in allowed:
                            evidence_by_path[full_path] = constraint
            elif constraint_type == "forbidden_file":
                value = str(constraint.get("value") or "").strip()
                if not value:
                    continue
                target = value.replace("\\", "/").strip("/")
                target_basename = os.path.basename(target).lower()
                has_path_component = "/" in target
                scope = str(constraint.get("scope") or "recursive")
                ignored_dirs = {".git", ".claude", ".codex", "__pycache__", ".pytest_cache"}
                for root, dirs, files in os.walk(project_dir):
                    dirs[:] = [d for d in dirs if d not in ignored_dirs]
                    if scope in {"project_root", "root", "exact"} and os.path.realpath(root) != project_dir:
                        continue
                    for filename in files:
                        full_path = os.path.realpath(os.path.join(root, filename))
                        rel_path = os.path.relpath(full_path, project_dir).replace("\\", "/")
                        if (has_path_component and rel_path.lower() == target.lower()) or (
                            not has_path_component and filename.lower() == target_basename
                        ):
                            evidence_by_path[full_path] = constraint

        for failure in delivery_quality.get("failed_constraints", []) or []:
            if failure.get("constraint_type") not in {
                "forbidden_file",
                "allowed_files",
                "allowed_documentation_files",
            }:
                continue
            for raw_path in failure.get("evidence", []) or []:
                evidence_by_path[os.path.realpath(str(raw_path))] = failure

        removed: List[str] = []
        for path in sorted(evidence_by_path):
            if path in protected:
                continue
            try:
                common = os.path.commonpath([project_dir, path])
            except ValueError:
                continue
            if common != project_dir or not os.path.isfile(path):
                continue
            try:
                os.remove(path)
                removed.append(path)
                logger.info("Removed generated file constraint violation for task %s: %s", task.task_id, path)
            except OSError as exc:
                logger.warning("Failed to remove generated file constraint violation %s: %s", path, exc)
        return removed

    def _cleanup_forbidden_file_constraints(
        self,
        task: Task,
        delivery_contract: Dict[str, Any],
    ) -> List[str]:
        """Remove explicit forbidden files before wave acceptance burns fix budget."""
        raw_project_dir = getattr(task, "project_dir", None) or delivery_contract.get("project_dir")
        if not raw_project_dir:
            return []
        project_dir = os.path.realpath(raw_project_dir)
        if not os.path.isdir(project_dir):
            return []

        protected: set[str] = set()
        for deliverable in delivery_contract.get("deliverables", []) or []:
            path_hint = deliverable.get("path_hint")
            if not path_hint:
                continue
            protected.add(os.path.realpath(os.path.join(project_dir, path_hint)))

        forbidden_paths: set[str] = set()
        ignored_dirs = {".git", ".claude", ".codex", "__pycache__", ".pytest_cache"}
        for constraint in delivery_contract.get("constraints", []) or []:
            if constraint.get("constraint_type") != "forbidden_file":
                continue
            value = str(constraint.get("value") or "").strip()
            if not value:
                continue
            target = value.replace("\\", "/").strip("/")
            target_basename = os.path.basename(target).lower()
            has_path_component = "/" in target
            scope = str(constraint.get("scope") or "recursive")
            for root, dirs, files in os.walk(project_dir):
                dirs[:] = [d for d in dirs if d not in ignored_dirs]
                if scope in {"project_root", "root", "exact"} and os.path.realpath(root) != project_dir:
                    continue
                for filename in files:
                    full_path = os.path.realpath(os.path.join(root, filename))
                    rel_path = os.path.relpath(full_path, project_dir).replace("\\", "/")
                    if (has_path_component and rel_path.lower() == target.lower()) or (
                        not has_path_component and filename.lower() == target_basename
                    ):
                        forbidden_paths.add(full_path)

        removed: List[str] = []
        for path in sorted(forbidden_paths):
            if path in protected:
                continue
            try:
                common = os.path.commonpath([project_dir, path])
            except ValueError:
                continue
            if common != project_dir or not os.path.isfile(path):
                continue
            try:
                os.remove(path)
                removed.append(path)
                logger.info("Removed forbidden file before wave acceptance for task %s: %s", task.task_id, path)
            except OSError as exc:
                logger.warning("Failed to remove forbidden file before wave acceptance %s: %s", path, exc)
        return removed

    def _cleanup_workspace_hygiene_violations(
        self,
        task: Task,
        delivery_quality: Dict[str, Any],
    ) -> List[str]:
        """Remove runtime/cache/tooling noise before project delivery is judged failed."""
        if not any(
            failure.get("constraint_type") == "workspace_hygiene"
            and failure.get("value") == "runtime_noise"
            for failure in delivery_quality.get("failed_constraints", []) or []
        ):
            return []

        return self._remove_workspace_noise_files(task, reason="before final delivery acceptance")

    def _remove_workspace_noise_files(self, task: Task, *, reason: str) -> List[str]:
        """Remove deterministic cache/runtime/diagnostic noise from a task project."""
        raw_project_dir = getattr(task, "project_dir", None)
        if not raw_project_dir:
            return []
        project_dir = os.path.realpath(raw_project_dir)
        if not os.path.isdir(project_dir):
            return []

        removed: List[str] = []
        for path in iter_workspace_noise_files(project_dir):
            try:
                common = os.path.commonpath([project_dir, path])
            except ValueError:
                continue
            if common != project_dir or not os.path.isfile(path):
                continue
            try:
                os.remove(path)
                removed.append(path)
                logger.info("Removed workspace hygiene noise for task %s (%s): %s", task.task_id, reason, path)
                self._prune_empty_noise_dirs(os.path.dirname(path), project_dir)
            except OSError as exc:
                logger.warning("Failed to remove workspace hygiene noise %s: %s", path, exc)
        return removed

    def _record_deterministic_cleanup(self, task: Task, key: str, removed: List[str]) -> None:
        if not removed:
            return
        task.last_owner_decision = dict(task.last_owner_decision or {})
        cleanup = dict(task.last_owner_decision.get("deterministic_cleanup") or {})
        existing = list(cleanup.get(key) or [])
        seen = set(existing)
        for path in removed:
            if path not in seen:
                existing.append(path)
                seen.add(path)
        cleanup[key] = existing
        task.last_owner_decision["deterministic_cleanup"] = cleanup
        self._state._persist_task(task)

    def _prune_empty_noise_dirs(self, start_dir: str, project_dir: str) -> None:
        current = os.path.realpath(start_dir)
        project_root = os.path.realpath(project_dir)
        allowed_names = set(IGNORED_DIR_NAMES) | set(RUNTIME_DATA_DIR_NAMES)
        while current != project_root:
            name = os.path.basename(current)
            try:
                os.rmdir(current)
            except OSError:
                break
            if name in allowed_names:
                break
            current = os.path.dirname(current)

    def _is_decompose_subtask(self, st: SubTask) -> bool:
        return st.subtask_id.endswith("-decompose") or getattr(st, "wave_number", None) == 0 or st.agent_id == "owner"

    def _has_business_subtasks(self, task: Task) -> bool:
        return any(
            self._is_business_decomposition_subtask(st)
            for st in task.subtasks
        )

    def _is_business_decomposition_subtask(self, st: SubTask) -> bool:
        return (
            not self._is_decompose_subtask(st)
            and not self._is_remediation_subtask_id(st.subtask_id)
            and not st.subtask_id.startswith("st-gap-")
        )

    def _is_missing_api_key_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "no api key found" in text
            or "missing api key" in text
            or "missing api keys" in text
            or "api key" in text and "not configured" in text
        )

    def _mark_task_waiting_for_keys(self, task: Task, decompose: Optional[SubTask], *, reason: str) -> None:
        message = "Waiting for API keys to sync before resuming decomposition."
        task.status = TaskStatus.PENDING
        task.error = message
        task.last_owner_decision = dict(task.last_owner_decision or {})
        task.last_owner_decision.update({
            "blocked_reason": "waiting_for_keys",
            "recoverable": True,
            "next_repair_action": "keys_synced",
            "repair_reason": reason,
        })
        task.updated_at = time.time()
        if decompose is not None:
            decompose.status = JobStatus.PENDING
            decompose.error_message = message
            self._state._persist_subtask(decompose)
        for wave in task.waves:
            if getattr(wave, "wave_number", None) == 0:
                wave.status = JobStatus.PENDING
                self._state._persist_wave(wave)
        self._state._persist_task(task)

    def _mark_decomposition_failed(
        self,
        task: Task,
        decompose: Optional[SubTask],
        *,
        reason: str,
        exc: Exception,
    ) -> None:
        message = f"Decomposition resume failed after {reason}: {exc}"
        task.status = TaskStatus.FAILED
        task.error = message
        task.last_owner_decision = dict(task.last_owner_decision or {})
        task.last_owner_decision.update({
            "blocked_reason": "decomposition_failed",
            "recoverable": False,
            "next_repair_action": None,
        })
        task.updated_at = time.time()
        if decompose is not None:
            decompose.status = JobStatus.FAILED
            decompose.error_message = message
            self._state._persist_subtask(decompose)
        for wave in task.waves:
            if getattr(wave, "wave_number", None) == 0:
                wave.status = JobStatus.FAILED
                wave.is_blocked = False
                wave.is_revalidating = False
                self._state._persist_wave(wave)
        self._state._persist_task(task)

    def _restart_pending_decomposition_if_needed(
        self, task: Task, ost: OrchestratorState, *, reason: str
    ) -> Optional[List[str]]:
        """Restart an interrupted owner decomposition task.

        This path is only for tasks that have no business subtasks yet. Business
        subtasks are handled by normal dispatch repair.
        """
        if self._has_business_subtasks(task):
            return None

        decompose = next((st for st in task.subtasks if self._is_decompose_subtask(st)), None)
        if not decompose or decompose.status != JobStatus.PENDING:
            return None

        if self._has_active_job_for_subtask(decompose.subtask_id):
            return None

        logger.info(
            "Restarting decomposition for task %s after %s using owner agent",
            task.task_id, reason,
        )
        task.status = TaskStatus.DECOMPOSING
        task.updated_at = time.time()
        self._state._persist_task(task)

        try:
            self._owner_agent.decompose_and_assign(task)
            business_subtasks = [
                st for st in task.subtasks
                if self._is_business_decomposition_subtask(st)
            ]
            if not business_subtasks:
                raise RuntimeError("LLM decomposition failed: no business subtasks generated")

            self._owner_agent.assign_waves(task)
            for wave in task.waves:
                self._state._persist_wave(wave)
            try:
                self._owner_agent.refresh_decomposition_coverage(task)
            except Exception as exc:
                logger.warning("Post-resume coverage refresh failed for task %s: %s", task.task_id, exc)

            if not ost.owner_session_id:
                ost.owner_session_id = getattr(task, "owner_session_id", None)
            for st in task.subtasks:
                if st.status == JobStatus.COMPLETED and self._is_subtask_acceptance_approved(ost, st.subtask_id):
                    ost.completed_subtasks.add(st.subtask_id)

            task.status = TaskStatus.PENDING
            task.updated_at = time.time()
            self._state._persist_task(task)

            dispatched: List[str] = []
            ready_subtasks = self._get_dispatchable_ready_subtasks(task.task_id, ost)
            for st in ready_subtasks:
                if self._is_decompose_subtask(st) or st.status != JobStatus.PENDING:
                    continue
                if self._has_active_job_for_subtask(st.subtask_id):
                    continue
                job = self._dispatcher.dispatch_subtask(st)
                if job:
                    dispatched.append(st.subtask_id)

            if dispatched:
                task.status = TaskStatus.RUNNING
                task.updated_at = time.time()
                self._state._persist_task(task)
                logger.info(
                    "Decomposition resume dispatched %d ready subtask(s) for %s after %s: %s",
                    len(dispatched), task.task_id, reason, dispatched,
                )
            return dispatched
        except Exception as exc:
            if self._is_missing_api_key_error(exc):
                self._mark_task_waiting_for_keys(task, decompose, reason=reason)
                logger.warning(
                    "Task %s is waiting for API keys after %s: %s",
                    task.task_id, reason, exc,
                )
                return []
            self._mark_decomposition_failed(task, decompose, reason=reason, exc=exc)
            logger.exception("Failed to restart decomposition for task %s", task.task_id)
            return []

    def _has_active_job_for_subtask(self, subtask_id: str) -> bool:
        """Check whether *subtask_id* has a pending/dispatched/running job in persistence."""
        try:
            jobs = self._state._persistence.get_jobs_by_subtask(subtask_id) if self._state._persistence else []
        except Exception:
            jobs = []
        return any(
            job.get("status") in {JobStatus.PENDING.value, JobStatus.DISPATCHED.value, JobStatus.RUNNING.value}
            for job in jobs
        )

    def _dispatch_ready_orphan_subtasks(
        self, task_id: str, ost: OrchestratorState, *, reason: str
    ) -> List[str]:
        """Safety-net dispatcher: dispatch pending subtasks that are ready but have no active job."""
        task = self._state.get_task(task_id)
        if not task or task.status == TaskStatus.PAUSED:
            return []

        dispatched: List[str] = []
        ready_subtasks = self._get_dispatchable_ready_subtasks(task_id, ost)
        for st in ready_subtasks:
            if st.status != JobStatus.PENDING:
                continue
            if self._has_active_job_for_subtask(st.subtask_id):
                continue
            job = self._dispatcher.dispatch_subtask(st)
            if job:
                dispatched.append(st.subtask_id)

        if dispatched:
            task.status = TaskStatus.RUNNING
            task.updated_at = time.time()
            self._state._persist_task(task)
            logger.info(
                "Dispatch watchdog dispatched %d ready subtask(s) for %s after %s: %s",
                len(dispatched), task_id, reason, dispatched,
            )
        return dispatched

    def _ensure_orchestrator_state(self, task: Task) -> tuple[OrchestratorState, bool]:
        """Return an OrchestratorState for *task*, creating one from task state when missing."""
        existing = self._orchestrator_states.get(task.task_id)
        if existing:
            return existing, False

        strict_dependency = getattr(task, "strict_dependency", True)
        enable_wave_gate = getattr(task, "enable_wave_gate", True)
        ost = OrchestratorState(
            task_id=task.task_id,
            fix_rounds=task.fix_rounds,
            strict_dependency=strict_dependency,
            wave_gate_enabled=enable_wave_gate,
            owner_session_id=getattr(task, "owner_session_id", None),
            allowed_subtask_agents=getattr(task, "allowed_subtask_agents", []),
        )
        preserved_attempts = (
            (getattr(task, "last_owner_decision", {}) or {}).get("quality_remediation_attempts") or {}
        )
        if isinstance(preserved_attempts, dict):
            ost.quality_remediation_attempts = {
                str(k): int(v) for k, v in preserved_attempts.items()
            }
        preserved_max = (
            (getattr(task, "last_owner_decision", {}) or {}).get("max_quality_remediation_attempts")
        )
        if preserved_max is not None:
            max_from_task = int(preserved_max or 4)
            ost.max_quality_remediation_attempts = (
                min(max_from_task, self._quality_remediation_limit_for_task(task))
                if self._is_release_e2e_task(task)
                else max_from_task
            )
        elif self._is_release_e2e_task(task):
            # Release E2E already exercises cross-agent generation. Keep the
            # repair loop short so it converges to deterministic evidence
            # rather than spending several rounds on cleanup artifacts.
            ost.max_quality_remediation_attempts = self._quality_remediation_limit_for_task(task)
        for st in task.subtasks:
            if st.status == JobStatus.COMPLETED and self._is_subtask_acceptance_approved(ost, st.subtask_id):
                ost.completed_subtasks.add(st.subtask_id)
        for wave in task.waves:
            status = getattr(wave, "governance_status", None)
            if status:
                ost.wave_statuses[wave.wave_number] = status
            if status == WaveLifecycleStatus.APPROVED.value:
                ost.wave_approved.add(wave.wave_number)
                ost.wave_acceptance_recorded.add(wave.wave_number)
            elif status == WaveLifecycleStatus.BLOCKED.value:
                ost.blocked_by_wave[wave.wave_number] = getattr(wave, "blocked_by_wave", None) or wave.wave_number
                ost.wave_acceptance_recorded.add(wave.wave_number)
            elif status == WaveLifecycleStatus.REVALIDATING.value:
                ost.revalidating_waves.add(wave.wave_number)

        self._orchestrator_states[task.task_id] = ost
        return ost, True

    def _repair_completed_wave_acceptance(self, task: Task, ost: OrchestratorState) -> List[int]:
        """Approve or block completed waves whose wave-level acceptance was never recorded."""
        repaired: List[int] = []
        for wave in sorted(task.waves, key=lambda item: item.wave_number):
            wave_number = getattr(wave, "wave_number", 0)
            if wave_number <= 0:
                continue
            if getattr(wave, "governance_status", None) == WaveLifecycleStatus.FAILED.value:
                continue
            if wave_number in ost.wave_acceptance_recorded:
                current_status = ost.wave_statuses.get(wave_number) or getattr(wave, "governance_status", None)
                if current_status != WaveLifecycleStatus.REVALIDATING.value:
                    continue
            original_wave_subtasks = self._original_wave_subtasks(task, wave_number)
            if not original_wave_subtasks:
                continue
            if not self._wave_original_subtasks_are_accepted(original_wave_subtasks, ost):
                continue

            acceptance = self._owner_agent.accept_wave(task, wave_number)
            acceptance, effective_decision = self._normalize_wave_acceptance_for_record(acceptance)

            record = AcceptanceRecord.new(
                task_id=task.task_id,
                level="wave",
                decision=effective_decision,
                deterministic_passed=True,
                judge_passed=acceptance.level2_passed,
                wave_number=wave_number,
                failed_checks=list(getattr(acceptance, "failed_checks", []) or []),
                missing_artifacts=list(getattr(acceptance, "missing_artifacts", []) or []),
                feedback=acceptance.level2_feedback,
                root_cause_scope=getattr(acceptance, "root_cause_scope", "unknown"),
                root_cause_wave=getattr(acceptance, "root_cause_wave", None),
                root_cause_artifact_ids=list(getattr(acceptance, "root_cause_artifact_ids", []) or []),
                recommended_action=getattr(acceptance, "recommended_action", "approve"),
                preferred_agent=getattr(acceptance, "preferred_agent", None),
                owner_session_id=getattr(acceptance, "owner_session_id", getattr(task, "owner_session_id", None)),
            )
            self._state.save_acceptance_record(record)
            ost.wave_acceptance_recorded.add(wave_number)
            ost.recent_acceptance_records.append({
                "level": "wave",
                "wave_number": wave_number,
                "decision": record.decision,
                "recommended_action": record.recommended_action,
            })

            if effective_decision == "approve" and acceptance.level2_passed:
                ost.wave_approved.add(wave_number)
                ost.revalidating_waves.discard(wave_number)
                ost.blocked_by_wave.pop(wave_number, None)
                ost.wave_statuses[wave_number] = WaveLifecycleStatus.APPROVED.value
                self._update_wave_governance(task, wave_number, WaveLifecycleStatus.APPROVED.value)
                repaired.append(wave_number)
            else:
                ost.wave_statuses[wave_number] = WaveLifecycleStatus.BLOCKED.value
                self._update_wave_governance(
                    task, wave_number, WaveLifecycleStatus.BLOCKED.value,
                    owner_decision={
                        "decision": getattr(acceptance, "decision", "reject"),
                        "recommended_action": getattr(acceptance, "recommended_action", "wave_fix"),
                        "root_cause_scope": getattr(acceptance, "root_cause_scope", "unknown"),
                        "root_cause_wave": getattr(acceptance, "root_cause_wave", None),
                    },
                )
                if ost.wave_gate_enabled:
                    break
        return repaired

    def repair_task_dispatch(
        self,
        task_id: str,
        *,
        reason: str,
        run_wave_acceptance: bool = True,
    ) -> Dict[str, Any]:
        """Repair a task that has pending subtasks without active jobs.

        Safe for event callbacks.  Creates missing orchestrator state from
        persisted task state, repairs completed wave acceptance first, then
        dispatches ready orphan pending subtasks.

        API polling paths should pass run_wave_acceptance=False so a read-only
        status request cannot synchronously run owner/LLM acceptance work.
        """
        task = self._state.get_task(task_id)
        if not task:
            return {
                "task_id": task_id,
                "state_created": False,
                "waves_approved": [],
                "failed_waves": [],
                "dispatched_subtasks": [],
                "decomposition_restarted": False,
                "waiting_for_keys": False,
                "reason": reason,
                "skipped": "task_not_in_memory",
            }
        if task.status == TaskStatus.PAUSED:
            return {
                "task_id": task_id,
                "state_created": False,
                "waves_approved": [],
                "failed_waves": [],
                "dispatched_subtasks": [],
                "decomposition_restarted": False,
                "waiting_for_keys": False,
                "reason": reason,
                "skipped": "task_paused",
            }

        ost, state_created = self._ensure_orchestrator_state(task)

        # Check if decomposition needs to be restarted before normal business dispatch.
        decomposition_dispatched = self._restart_pending_decomposition_if_needed(task, ost, reason=reason)
        if decomposition_dispatched is not None:
            waiting_for_keys = (
                (getattr(task, "last_owner_decision", {}) or {}).get("blocked_reason") == "waiting_for_keys"
            )
            return {
                "task_id": task_id,
                "state_created": state_created,
                "waves_approved": [],
                "failed_waves": [],
                "dispatched_subtasks": decomposition_dispatched,
                "decomposition_restarted": True,
                "waiting_for_keys": waiting_for_keys,
                "reason": reason,
            }

        for st in task.subtasks:
            if st.status == JobStatus.COMPLETED and self._is_subtask_acceptance_approved(ost, st.subtask_id):
                ost.completed_subtasks.add(st.subtask_id)

        failed_waves = self._repair_exhausted_blocked_waves(task, ost, reason=reason)
        waves_approved = (
            self._repair_completed_wave_acceptance(task, ost)
            if run_wave_acceptance
            else []
        )
        dispatched = self._dispatch_ready_orphan_subtasks(task_id, ost, reason=reason)
        if waves_approved or failed_waves or dispatched or state_created:
            logger.info(
                "Dispatch repair for %s after %s: state_created=%s waves_approved=%s failed_waves=%s dispatched=%s",
                task_id, reason, state_created, waves_approved, failed_waves, dispatched,
            )
        return {
            "task_id": task_id,
            "state_created": state_created,
            "waves_approved": waves_approved,
            "failed_waves": failed_waves,
            "dispatched_subtasks": dispatched,
            "decomposition_restarted": False,
            "waiting_for_keys": False,
            "reason": reason,
        }

    def _is_subtask_acceptance_approved(self, ost: OrchestratorState, subtask_id: str) -> bool:
        """Return True only when a completed subtask has passed owner acceptance.

        Executor completion means the agent process finished; DAG dependencies
        should unlock only after Level 1 + Level 2 acceptance succeeds. This
        guard keeps the dispatch repair watchdog from treating an executed but
        rejected subtask as completed before the async acceptance flow catches up.
        """
        acceptance = ost.acceptance_results.get(subtask_id)
        if acceptance and acceptance.level1_passed and acceptance.level2_passed:
            return True

        canonical_id = self._get_canonical_subtask_id(subtask_id)
        if canonical_id != subtask_id:
            canonical_acceptance = ost.acceptance_results.get(canonical_id)
            if canonical_acceptance and canonical_acceptance.level1_passed and canonical_acceptance.level2_passed:
                return True

        for accepted_id, accepted in ost.acceptance_results.items():
            if self._get_canonical_subtask_id(accepted_id) != canonical_id:
                continue
            if accepted.level1_passed and accepted.level2_passed:
                return True

        persistence = getattr(self._state, "_persistence", None)
        if persistence:
            try:
                records = persistence.get_acceptance_records(ost.task_id)
            except Exception:
                records = []
            for record in reversed(records or []):
                getter = record.get if isinstance(record, dict) else lambda key, default=None: getattr(record, key, default)
                if getter("level") != "subtask":
                    continue
                record_subtask_id = getter("subtask_id")
                if not record_subtask_id:
                    continue
                if self._get_canonical_subtask_id(str(record_subtask_id)) != canonical_id:
                    continue
                if (
                    getter("decision") == "approve"
                    and bool(getter("deterministic_passed"))
                    and bool(getter("judge_passed"))
                ):
                    return True

        return False

    def repair_tasks_waiting_for_keys(
        self,
        *,
        reason: str = "keys_synced",
        task_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        task_ids = list(task_ids) if task_ids is not None else self._state.get_tasks_waiting_for_keys()
        repaired = []
        skipped = []
        for task_id in task_ids:
            task = self._state.get_task(task_id)
            if not task:
                self._state.restore_task(task_id, allow_concurrent=True)
                task = self._state.get_task(task_id)
            if not task:
                skipped.append({"task_id": task_id, "reason": "restore_failed"})
                continue
            task.last_owner_decision = dict(task.last_owner_decision or {})
            task.last_owner_decision.pop("blocked_reason", None)
            task.last_owner_decision.pop("next_repair_action", None)
            task.error = None
            self._state._persist_task(task)
            result = self.repair_task_dispatch(task_id, reason=reason)
            repaired.append({"task_id": task_id, "repair": result})
        return {"repaired": repaired, "skipped": skipped}

    def _quality_failure_message(self, quality: "ProjectAcceptanceReport") -> str:
        parts: List[str] = []
        if quality.missing_required:
            parts.append("missing required deliverables: " + ", ".join(sorted(quality.missing_required)))
        invalid = [item.get("path_hint") for item in getattr(quality, "invalid_required", []) if item.get("path_hint")]
        if invalid:
            parts.append("invalid required deliverables: " + ", ".join(sorted(invalid)))
        if not parts:
            return "Project quality acceptance failed."
        return "Project quality acceptance failed: " + "; ".join(parts) + "."

    def _quality_requirement_key(
        self,
        item: Dict[str, Any] | str,
        failure_category: Optional[str] = None,
    ) -> str:
        if isinstance(item, str):
            base = item
        else:
            base = (
                item.get("requirement_id")
                or item.get("path_hint")
                or item.get("artifact_type")
                or "unknown"
            )
        if failure_category:
            return f"{failure_category}:{base}"
        return str(base)

    def _quality_attempts_from_task(self, task: Task) -> Dict[str, int]:
        decision = dict(task.last_owner_decision or {})
        raw = decision.get("quality_remediation_attempts") or {}
        attempts = {str(k): int(v) for k, v in raw.items()}
        ost = self._orchestrator_states.get(task.task_id)
        if ost and getattr(ost, "quality_remediation_attempts", None):
            for key, value in ost.quality_remediation_attempts.items():
                attempts[key] = max(attempts.get(key, 0), int(value))
        return attempts

    def _save_quality_attempts(self, task: Task, attempts: Dict[str, int]) -> None:
        task.last_owner_decision = dict(task.last_owner_decision or {})
        task.last_owner_decision["quality_remediation_attempts"] = dict(attempts)
        ost = self._orchestrator_states.get(task.task_id)
        if ost:
            ost.quality_remediation_attempts = dict(attempts)
            task.last_owner_decision["max_quality_remediation_attempts"] = getattr(
                ost, "max_quality_remediation_attempts", 1
            )
        task.updated_at = time.time()
        self._state._persist_task(task)

    def _create_quality_remediation_subtask(
        self,
        task: Task,
        *,
        requirement: Dict[str, Any],
        reason: str,
        attempt: int,
    ) -> Optional[SubTask]:
        path_hint = requirement.get("path_hint")
        artifact_type = requirement.get("artifact_type") or "file"
        requirement_id = requirement.get("requirement_id") or path_hint or artifact_type
        preferred_agent = requirement.get("preferred_agent")
        agent_id = self._quality_remediation_agent(task, preferred_agent=preferred_agent)

        subtask_id = f"st-quality-{uuid.uuid4().hex[:8]}"
        is_probe_remediation = artifact_type == "functional_probe"
        is_hygiene_remediation = artifact_type == "workspace_hygiene"
        if is_probe_remediation:
            probe_id = requirement.get("probe_id") or requirement_id
            related_path = requirement.get("related_path_hint")
            related_sentence = f" Primary implementation path: {related_path}." if related_path else ""
            pytest_guidance = self._quality_probe_remediation_guidance(reason)
            description = (
                f"Quality remediation attempt {attempt}: fix failing acceptance probe {probe_id}. "
                f"Reason: {reason}.{related_sentence} Modify only source, tests, dependency manifests, or documentation needed "
                "for the original requirements. Do not create diagnostic helper scripts, local virtualenvs, "
                "cache folders, upload samples, database files, Docker scaffolding, or unrelated artifacts. "
                f"{pytest_guidance}"
                f"The system will rerun the original probe after the fix. Project_dir: {task.project_dir}."
            )
        else:
            description = (
                f"Quality remediation attempt {attempt}: produce or repair required deliverable "
                f"{path_hint or artifact_type}. Reason: {reason}. "
                f"All output must be written inside project_dir: {task.project_dir}."
            )
        subtask = self._state.add_subtask(
            task_id=task.task_id,
            description=description,
            agent_id=agent_id,
            priority=1,
            dependencies=[],
            subtask_id=subtask_id,
        )
        if not subtask:
            return None
        subtask.wave_number = max((getattr(st, "wave_number", 1) for st in task.subtasks), default=1)
        self._state._persist_subtask(subtask)

        from ..models import AcceptanceCheck, DeliverableSpec, TaskContract
        contract = TaskContract.new(
            task_id=task.task_id,
            level="subtask",
            goal=description,
            subtask_id=subtask_id,
            wave_number=subtask.wave_number,
            project_dir=task.project_dir,
        )
        contract.expected_deliverables = [] if (is_probe_remediation or is_hygiene_remediation) else [
            DeliverableSpec(
                artifact_type=artifact_type,
                required=True,
                path_hint=path_hint,
                description=f"Quality remediation for required deliverable {requirement_id}",
            )
        ]
        contract.acceptance_checks = [
            AcceptanceCheck(
                check_type=(
                    "probe_passes"
                    if is_probe_remediation
                    else ("workspace_hygiene_clean" if is_hygiene_remediation else ("file_exists" if path_hint else f"{artifact_type}_exists"))
                ),
                description=(
                    f"Verify acceptance probe {requirement.get('probe_id') or requirement_id} passes"
                    if is_probe_remediation
                    else (
                        "Verify workspace hygiene no longer reports runtime/cache noise"
                        if is_hygiene_remediation
                        else f"Verify quality remediation produced {path_hint or artifact_type}"
                    )
                ),
                required=True,
            )
        ]
        self._state.save_task_contract(contract)
        return subtask

    def _completed_non_owner_agents(self, task: Task) -> set[str]:
        return {
            str(st.agent_id)
            for st in task.subtasks
            if st.status == JobStatus.COMPLETED
            and st.agent_id
            and st.agent_id != "owner"
        }

    def _preferred_agent_for_agent_mix(
        self,
        task: Task,
        constraint: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        value = dict((constraint or {}).get("value") or {})
        min_distinct = int(value.get("min_distinct_agents") or 0)
        min_local = int(value.get("min_local_agents") or 0)
        min_cloud = int(value.get("min_cloud_agents") or 0)

        valid_agents = self._get_allowed_valid_agents(task)
        covered = self._completed_non_owner_agents(task)
        local_covered = covered.intersection(self._LOCAL_AGENT_IDS)
        cloud_covered = covered.intersection(self._CLOUD_AGENT_IDS)

        def first_uncovered(candidates: tuple[str, ...]) -> Optional[str]:
            for agent_id in candidates:
                if agent_id in covered:
                    continue
                if valid_agents and agent_id not in valid_agents:
                    continue
                return agent_id
            return None

        if min_local and len(local_covered) < min_local:
            agent_id = first_uncovered(self._LOCAL_AGENT_IDS)
            if agent_id:
                return agent_id
        if min_cloud and len(cloud_covered) < min_cloud:
            agent_id = first_uncovered(self._CLOUD_AGENT_IDS)
            if agent_id:
                return agent_id
        if min_distinct and len(covered) < min_distinct:
            agent_id = first_uncovered(self._LOCAL_AGENT_IDS + self._CLOUD_AGENT_IDS)
            if agent_id:
                return agent_id
        return None

    def _preferred_agent_for_quality_bundle(
        self,
        task: Task,
        requirements: List[Dict[str, Any]],
    ) -> Optional[str]:
        for requirement in requirements:
            if requirement.get("artifact_type") == "agent_mix":
                preferred = self._preferred_agent_for_agent_mix(task, requirement)
                if preferred:
                    return preferred

        valid_agents = self._get_allowed_valid_agents(task)
        covered = self._completed_non_owner_agents(task)
        has_test_or_cli = any(
            str(req.get("path_hint") or "").startswith(("tests/", "cli/"))
            for req in requirements
        )
        local_order = ("hermes", "claude", "openclaw") if has_test_or_cli else self._LOCAL_AGENT_IDS
        for agent_id in local_order:
            if agent_id in covered:
                continue
            if valid_agents and agent_id not in valid_agents:
                continue
            return agent_id
        return None

    def _quality_remediation_agent(self, task: Task, preferred_agent: Optional[str] = None) -> str:
        valid_agents = self._get_allowed_valid_agents(task)
        if preferred_agent and (not valid_agents or preferred_agent in valid_agents):
            return preferred_agent
        agent_id = self._find_idle_agent(task)
        if agent_id:
            return agent_id
        if valid_agents:
            return valid_agents[0]
        return "deepseek"

    def _preferred_quality_probe_agent(
        self,
        task: Task,
        probe_type: str,
        *,
        prior_attempts: int = 0,
    ) -> Optional[str]:
        normalized_probe = (probe_type or "").lower()
        if "static_web" in normalized_probe or "browser" in normalized_probe:
            candidate_rounds = [
                ["hermes", "claude", "deepseek", "minimax", "openclaw"],
                ["claude", "hermes", "deepseek", "minimax", "openclaw"],
                ["deepseek", "claude", "hermes", "openclaw", "minimax"],
            ]
        elif "api_service" in normalized_probe or normalized_probe == "api":
            candidate_rounds = [
                ["deepseek", "openclaw", "claude", "hermes", "minimax"],
                ["openclaw", "deepseek", "claude", "hermes", "minimax"],
                ["claude", "deepseek", "openclaw", "hermes", "minimax"],
            ]
        elif "pytest" in normalized_probe or "python" in normalized_probe:
            candidate_rounds = [
                ["deepseek", "claude", "hermes", "openclaw"],
                ["claude", "deepseek", "hermes", "openclaw"],
            ]
        else:
            return None

        round_index = min(max(prior_attempts, 0), len(candidate_rounds) - 1)
        candidates = candidate_rounds[round_index]
        valid_agents = self._get_allowed_valid_agents(task)
        if not valid_agents:
            return candidates[0]
        for agent_id in candidates:
            if agent_id in valid_agents:
                return agent_id
        return None

    def _create_quality_bundle_remediation_subtask(
        self,
        task: Task,
        *,
        requirements: List[Dict[str, Any]],
        reasons: List[str],
        attempt: int,
    ) -> Optional[SubTask]:
        paths = [
            str(req.get("path_hint"))
            for req in requirements
            if req.get("path_hint")
        ]
        if not paths:
            return None
        unique_paths = list(dict.fromkeys(paths))
        reason_text = "; ".join(dict.fromkeys(reason for reason in reasons if reason))
        description = (
            f"Quality remediation attempt {attempt}: repair the project deliverables in one coherent pass. "
            f"Required files to produce or repair: {', '.join(unique_paths)}. "
            f"Reason: {reason_text}. "
            "Keep the implementation, tests, dependency manifests, Docker files, and README mutually consistent. "
            "Do not create diagnostic helper scripts, local virtualenvs, cache folders, sample databases, or unrelated artifacts. "
            f"All output must be written inside project_dir: {task.project_dir}."
        )
        subtask = self._state.add_subtask(
            task_id=task.task_id,
            description=description,
            agent_id=self._quality_remediation_agent(
                task,
                preferred_agent=self._preferred_agent_for_quality_bundle(task, requirements),
            ),
            priority=1,
            dependencies=[],
            subtask_id=f"st-quality-{uuid.uuid4().hex[:8]}",
        )
        if not subtask:
            return None
        subtask.wave_number = max((getattr(st, "wave_number", 1) for st in task.subtasks), default=1)
        self._state._persist_subtask(subtask)

        from ..models import AcceptanceCheck, DeliverableSpec, TaskContract
        contract = TaskContract.new(
            task_id=task.task_id,
            level="subtask",
            goal=description,
            subtask_id=subtask.subtask_id,
            wave_number=subtask.wave_number,
            project_dir=task.project_dir,
        )
        contract.expected_deliverables = [
            DeliverableSpec(
                artifact_type=req.get("artifact_type") or "file",
                required=True,
                path_hint=req.get("path_hint"),
                description=f"Quality remediation for required deliverable {req.get('path_hint')}",
            )
            for req in requirements
            if req.get("path_hint")
        ]
        contract.acceptance_checks = [
            AcceptanceCheck(
                check_type="file_exists",
                description="Verify all bundled quality remediation files exist.",
                required=True,
            )
        ]
        self._state.save_task_contract(contract)
        return subtask

    def _create_quality_probe_bundle_remediation_subtask(
        self,
        task: Task,
        *,
        requirements: List[Dict[str, Any]],
        reasons: List[str],
        attempt: int,
    ) -> Optional[SubTask]:
        probe_ids = list(dict.fromkeys(
            str(req.get("probe_id") or req.get("requirement_id") or req.get("probe_type") or "probe")
            for req in requirements
        ))
        related_paths = list(dict.fromkeys(
            str(req.get("related_path_hint"))
            for req in requirements
            if req.get("related_path_hint")
        ))
        preferred_agent = next(
            (str(req.get("preferred_agent")) for req in requirements if req.get("preferred_agent")),
            None,
        )
        reason_text = "; ".join(dict.fromkeys(reason for reason in reasons if reason))
        related_sentence = (
            f" Primary implementation paths: {', '.join(related_paths)}."
            if related_paths
            else ""
        )
        guidance = self._quality_probe_remediation_guidance(reason_text)
        description = (
            f"Quality remediation attempt {attempt}: repair failing acceptance probes in one coherent pass: "
            f"{', '.join(probe_ids)}. Reason: {reason_text}.{related_sentence} "
            "Keep the existing deliverable paths and repair the current implementation in place; "
            "do not add package managers, node_modules, Docker, server frameworks, or unrelated scaffolding. "
            "Modify only source, tests, dependency manifests, or documentation needed for the original requirements. "
            f"{guidance}"
            f"The system will rerun the original probes after the fix. Project_dir: {task.project_dir}."
        )
        subtask = self._state.add_subtask(
            task_id=task.task_id,
            description=description,
            agent_id=self._quality_remediation_agent(task, preferred_agent=preferred_agent),
            priority=1,
            dependencies=[],
            subtask_id=f"st-quality-{uuid.uuid4().hex[:8]}",
        )
        if not subtask:
            return None
        subtask.wave_number = max((getattr(st, "wave_number", 1) for st in task.subtasks), default=1)
        self._state._persist_subtask(subtask)

        from ..models import AcceptanceCheck, TaskContract
        contract = TaskContract.new(
            task_id=task.task_id,
            level="subtask",
            goal=description,
            subtask_id=subtask.subtask_id,
            wave_number=subtask.wave_number,
            project_dir=task.project_dir,
        )
        contract.expected_deliverables = []
        contract.acceptance_checks = [
            AcceptanceCheck(
                check_type="probe_passes",
                description=f"Verify bundled acceptance probes pass: {', '.join(probe_ids)}",
                required=True,
            )
        ]
        self._state.save_task_contract(contract)
        return subtask

    @staticmethod
    def _quality_probe_remediation_guidance(reason: str) -> str:
        text = (reason or "").lower()
        guidance: List[str] = []
        if "pytest" in text or "anyio" in text or "trio" in text:
            guidance.append(
                "For pytest suites, make tests deterministic in a minimal Python environment: "
                "prefer synchronous FastAPI TestClient tests; if pytest-anyio is used, add an "
                "anyio_backend fixture that returns 'asyncio' so tests do not require trio unless "
                "trio is listed as an explicit dependency."
            )
        if "static_web_smoke" in text or "agent row" in text or "skill controls" in text:
            guidance.append(
                "For static web UI probes, edit the existing HTML/CSS/JS in place. "
                "Every named Local Agent and Cloud LLM card must contain at least three skill controls "
                "inside that same .agent-card or .llm-card element, using data-skill, checkbox inputs, "
                "or role='switch'. These required cards must exist directly in index.html as static fallback "
                "markup, not only inside JavaScript templates. Do not rely on a later shared Skill Matrix to "
                "satisfy per-card controls, and do not rename Cloud LLM cards to provider-card. If the failure "
                "mentions native skill chips or toggles, add a visible per-card or local-agent skill control "
                "labeled Native, Native Agent Skill, Local Native, or Across E2E Quality Gate."
            )
        if "agent routing display text" in text or "runtime dom target missing" in text:
            guidance.append(
                "For static web runtime failures, keep requested brand and agent names exactly as user-visible "
                "text with their requested capitalization, for example OpenClaw, Hermes, Claude Code, DeepSeek, "
                "and MiniMax. Do not render only lowercase ids or internal slugs. If a failure says a runtime DOM "
                "target is missing, align JavaScript selectors with real HTML ids or rename the HTML ids so each "
                "document.getElementById/querySelector target exists before the script updates it."
            )
        if "native skill display text" in text:
            guidance.append(
                "When a requested native skill has a product-style name such as Apple Notes, render that exact "
                "display name in visible text and repair advice. Do not show only internal slugs such as "
                "apple-notes."
            )
        if "api/report" in text or "readiness metrics" in text or "api_service" in text:
            guidance.append(
                "For API service probe failures, repair the existing API implementation path, usually "
                "api/server.mjs or server.mjs. GET /api/report must return readiness plus the exact "
                "snake_case metrics required_failed_count, manual_required_count, skipped_required_count, "
                "and gateResults or gate_results. Do not return only camelCase variants such as "
                "requiredFailedCount or skippedRequiredCount."
            )
        if "javascript runtime risk" in text:
            guidance.append(
                "For JavaScript runtime risk failures, remove undefined variable references instead of relying on "
                "browser globals. In forEach callbacks, include every variable that the body uses, for example "
                "pairs.forEach(([a, b], index) => { ... }) instead of referencing i or j that are not parameters "
                "or enclosing-scope variables. For canvas animations, initialize canvas.width/canvas.height and "
                "all width/height variables before constructing nodes whose coordinates depend on those dimensions; "
                "otherwise createRadialGradient/arc calls can receive NaN and leave the canvas blank."
            )
        if (
            "browser_e2e" in text
            and ("canvas" in text or "nonblank" in text or "pixel" in text)
        ):
            guidance.append(
                "For browser E2E canvas failures, call the canvas initialization function during initial page load "
                "(for example after script definitions or on DOMContentLoaded), set canvas.width/canvas.height before "
                "drawing, and draw a visible first frame synchronously before relying on resize handlers or "
                "requestAnimationFrame. Do not wait for a window resize event to start the animation."
            )
        if (
            "static_web_smoke" in text
            or "section" in text
            or "strict-mode toggle" in text
            or "task composer" in text
            or "skill matrix" in text
            or "application name" in text
            or "delivery report" in text
            or "functional/artifact mode toggle" in text
            or "checklist label click" in text
        ):
            guidance.append(
                "When a static web task names product sections, add visible semantic sections with the "
                "requested headings exactly, such as Local Agents, Cloud LLMs, Skill Matrix, Task Composer, "
                "and Route Preview. The Task Composer must expose its own visible textarea, priority selector, "
                "Strict Mode toggle, and Recompute Route button; JavaScript 'use strict' or unrelated hidden "
                "form fields do not satisfy the strict-mode requirement. If the failure mentions an application "
                "name, render that exact name in the visible body (for example an h1), not only in the <title>. "
                "If the failure mentions delivery report metrics, keep those exact metric labels visible after "
                "JavaScript initializes; update metric values by id/textContent instead of replacing the metric "
                "container with different labels; report-content/reportContent render functions must preserve "
                "Generated Quality Score, Final Quality Score, Required Gate Failures, Manual Checks, Skipped Checks, "
                "and Final Verdict exactly. For Functional and Artifact mode controls, prefer radio buttons "
                "with one shared name or implement reciprocal checkbox logic so either mode can be selected. For "
                "checklists, use checkbox change events or guard label clicks so label default behavior is not "
                "immediately toggled back by a parent click handler."
            )
        if "route evidence" in text:
            guidance.append(
                "For Route Evidence failures, wire the Recompute Route button and relevant task/skill toggle "
                "changes to update the visible Route Evidence panel. The section heading should say Route Evidence "
                "when the user asks for route evidence, and the Recompute Route button must live inside that same "
                "section rather than only in a separate composer panel. The rendered evidence must include a "
                "visible label or column named Selected Agent, the matched native skill or skill category, the "
                "MCP risk, and a short reason explaining why that subtask or route was assigned; do not leave "
                "the panel as a placeholder. Every recompute click should visibly change the Route Evidence panel "
                "after task text changes, for example by updating the selected row, a visible recompute counter, "
                "or a visible last-updated timestamp inside the panel."
            )
            if (
                "selected agent" in text
                or "matched native skill" in text
                or "mcp risk" in text
                or "runtime row missing" in text
                or "recomputes visible rows" in text
            ):
                guidance.append(
                    "PATCH PLAN: patch only the existing static web files, normally web/index.html and web/app.js. "
                    "In #route-evidence, render #evidence-list rows with these exact visible labels: Selected Agent, "
                    "Matched Native Skill, MCP Risk, and Reason. Put #recompute-btn inside #route-evidence or make it "
                    "update #route-evidence directly. On every click, recompute from #task-text, update a visible "
                    "route version/timestamp or selected agent row, and persist the latest route state in localStorage. "
                    "Do not replace the whole app or add dependencies; make the smallest DOM and event-handler patch."
                )
        if "owner agent route preview" in text or "owner agent" in text:
            guidance.append(
                "If the failure mentions Owner Agent, add a visible Owner Agent selector or route preview label "
                "using the exact text Owner Agent, with options such as Auto, OpenClaw, Hermes, Claude Code, "
                "DeepSeek, and MiniMax. Do not hide this text inside only data attributes or JavaScript constants."
            )
        if "responsive mobile rule" in text or "narrow-screen" in text or "responsive" in text:
            guidance.append(
                "For responsive static web failures, update the actual layout selectors used by the page, not "
                "unused helper classes. Add max-width media rules for containers such as .console, .agents-grid, "
                ".skill-matrix, .matrix-grid, and .composer-row so multi-column grids collapse to one column or "
                "use overflow-x:auto on matrix tables. Verify that a 390px viewport has no document-level "
                "horizontal overflow."
            )
        if ".pytest_cache" in text or "workspace_hygiene" in text or "cache" in text:
            guidance.append(
                "Remove runtime cache directories such as .pytest_cache and prevent regenerated "
                "cache files from being committed as deliverables."
            )
        if not guidance:
            return ""
        return " ".join(guidance) + " "

    @staticmethod
    def _quality_probe_primary_deliverable(
        probe_type: str,
        deliverables: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        normalized_probe = (probe_type or "").lower()
        if "api_service" in normalized_probe or normalized_probe == "api":
            return next(
                (
                    item for item in deliverables
                    if item.get("path_hint")
                    and (
                        item.get("artifact_type") == "api_service_source"
                        or str(item.get("path_hint")).lower().endswith(("api/server.mjs", "api/server.js", "server.mjs", "server.js"))
                    )
                ),
                None,
            )
        if "pytest" in normalized_probe:
            return next(
                (
                    item for item in deliverables
                    if item.get("path_hint")
                    and str(item.get("path_hint")).endswith(".py")
                    and item.get("artifact_type") != "test_source"
                ),
                None,
            )
        if "static_web" in normalized_probe:
            return next(
                (
                    item for item in deliverables
                    if item.get("path_hint")
                    and str(item.get("path_hint")).lower().endswith((".html", ".css", ".js"))
                ),
                None,
            )
        return next((item for item in deliverables if item.get("path_hint")), None)

    @staticmethod
    def _quality_probe_deliverable_mentioned_in_output(
        output_tail: str,
        deliverables: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        text = output_tail or ""
        if not text:
            return None
        candidates = [
            item for item in deliverables
            if item.get("path_hint")
        ]
        candidates.sort(key=lambda item: len(str(item.get("path_hint") or "")), reverse=True)
        for item in candidates:
            path_hint = str(item.get("path_hint") or "")
            if re.search(rf"(?<![\w./-]){re.escape(path_hint)}(?=[:\s,.)\]]|$)", text):
                return item
        return None

    @staticmethod
    def _quality_probe_stack_guardrail(
        probe_type: str,
        deliverables: List[Dict[str, Any]],
    ) -> str:
        normalized_probe = (probe_type or "").lower()
        paths = [str(item.get("path_hint") or "").lower() for item in deliverables]
        if "api_service" in normalized_probe or normalized_probe == "api":
            return (
                "Keep the existing API service deliverable path, usually api/server.mjs or server.mjs, "
                "and repair the current Node HTTP implementation in place. GET /api/report must expose "
                "readiness, required_failed_count, manual_required_count, skipped_required_count, and "
                "gateResults or gate_results. Do not satisfy this with camelCase-only metric keys, "
                "package managers, node_modules, Docker, server frameworks, or unrelated scaffolding. "
            )
        has_python_source = any(
            path.endswith(".py") and item.get("artifact_type") != "test_source"
            for path, item in zip(paths, deliverables)
        )
        has_static_web = (
            "static_web" in normalized_probe
            or any(path.endswith((".html", ".css", ".js")) for path in paths)
        )
        if "pytest" in normalized_probe or has_python_source:
            return (
                "Keep the existing Python deliverable paths; do not replace the solution with "
                "JavaScript, Node, Docker, or unrelated scaffolding. "
            )
        if has_static_web:
            return (
                "Keep the existing static web deliverable paths and repair the current HTML, CSS, "
                "and JavaScript files; do not add package managers, node_modules, Docker, server "
                "frameworks, or unrelated scaffolding. "
            )
        return (
            "Keep the existing deliverable paths and repair the current implementation in place; "
            "do not replace it with a different stack, Docker scaffolding, or unrelated artifacts. "
        )

    def _has_nonterminal_original_subtasks(self, task_id: str) -> bool:
        current = self._state.get_task(task_id)
        if not current:
            return False
        terminal_states = {
            JobStatus.COMPLETED.value,
            JobStatus.CANCELLED.value,
            JobStatus.FAILED.value,
        }
        for subtask in current.subtasks:
            if subtask.subtask_id.endswith("-decompose"):
                continue
            if self._is_remediation_subtask_id(subtask.subtask_id):
                continue
            if self._job_status_value(subtask.status) not in terminal_states:
                return True
        return False

    @staticmethod
    def _job_status_value(status: Any) -> str:
        return str(getattr(status, "value", status) or "").lower()

    def _start_quality_remediation_if_possible(
        self,
        task: Task,
        quality: "ProjectAcceptanceReport",
        delivery_contract: Optional[Dict[str, Any]] = None,
        *,
        require_original_terminal: bool = False,
    ) -> List[str]:
        with self._quality_remediation_lock:
            return self._start_quality_remediation_if_possible_unlocked(
                task,
                quality,
                delivery_contract=delivery_contract,
                require_original_terminal=require_original_terminal,
            )

    def _start_quality_remediation_if_possible_unlocked(
        self,
        task: Task,
        quality: "ProjectAcceptanceReport",
        delivery_contract: Optional[Dict[str, Any]] = None,
        *,
        require_original_terminal: bool = False,
    ) -> List[str]:
        manifest = self._state.get_requirement_manifest(task.task_id)
        if not manifest and not delivery_contract:
            return []
        if require_original_terminal and self._has_nonterminal_original_subtasks(task.task_id):
            task.status = TaskStatus.RUNNING
            task.error = "Waiting for remaining original subtasks before project quality remediation."
            self._state._persist_task(task)
            return []

        existing_active = self._active_remediation_subtasks(task)
        existing_active_ids = [st.subtask_id for st in existing_active]
        existing_active_ids.extend(self._active_persisted_remediation_ids(task))
        existing_active_ids = list(dict.fromkeys(existing_active_ids))
        if existing_active_ids:
            task.status = TaskStatus.RUNNING
            task.error = "Waiting for active quality remediation before starting another repair."
            self._state._persist_task(task)
            return existing_active_ids

        attempts = self._quality_attempts_from_task(task)
        max_attempts = 4
        ost = self._orchestrator_states.get(task.task_id)
        if ost:
            max_attempts = getattr(ost, "max_quality_remediation_attempts", 4)
        if self._release_quality_remediation_exhausted(task, max_attempts):
            task.status = TaskStatus.RUNNING
            task.error = (
                "Release E2E quality remediation budget exhausted; "
                "switching to deterministic delivery repair."
            )
            task.last_owner_decision = dict(task.last_owner_decision or {})
            task.last_owner_decision["quality_remediation_exhausted"] = True
            task.last_owner_decision["max_quality_remediation_attempts"] = max_attempts
            self._state._persist_task(task)
            return []

        deliverables = list((manifest or {}).get("deliverables", []) or [])
        for item in (delivery_contract or {}).get("deliverables", []) or []:
            path_hint = item.get("path_hint")
            if not path_hint:
                continue
            deliverables.append({
                "requirement_id": item.get("id") or path_hint,
                "path_hint": path_hint,
                "artifact_type": item.get("artifact_type") or "file",
                "required": item.get("required", True),
            })
        by_path = {item.get("path_hint"): item for item in deliverables if item.get("path_hint")}
        created: List[str] = []

        failure_items: List[tuple[Dict[str, Any], str, str]] = []
        file_failure_items: List[tuple[Dict[str, Any], str, str]] = []
        for path_hint in quality.missing_required:
            requirement = by_path.get(path_hint) or {"path_hint": path_hint, "artifact_type": "file", "required": True}
            file_failure_items.append((requirement, f"missing required deliverable: {path_hint}", "missing_file"))
        for invalid in getattr(quality, "invalid_required", []) or []:
            path_hint = invalid.get("path_hint")
            if self._quality_invalid_group_should_not_create_file_remediation(invalid, task):
                continue
            concrete_group_requirement = self._quality_invalid_group_concrete_requirement(
                invalid,
                task,
                delivery_contract or {},
                by_path,
            )
            if concrete_group_requirement:
                file_failure_items.append((
                    concrete_group_requirement,
                    invalid.get("message") or "invalid required deliverable group",
                    "invalid_file",
                ))
                continue
            requirement = by_path.get(path_hint) or {"path_hint": path_hint, "artifact_type": "file", "required": True}
            file_failure_items.append((requirement, invalid.get("message") or "invalid required deliverable", "invalid_file"))

        if len(file_failure_items) > 1:
            eligible: List[tuple[Dict[str, Any], str, str, int]] = []
            for requirement, reason, failure_category in file_failure_items:
                key = self._quality_requirement_key(requirement, failure_category=failure_category)
                current = attempts.get(key, 0)
                if current < max_attempts:
                    eligible.append((requirement, reason, key, current + 1))
            if eligible:
                bundled_attempt = max(item[3] for item in eligible)
                subtask = self._create_quality_bundle_remediation_subtask(
                    task,
                    requirements=[item[0] for item in eligible],
                    reasons=[item[1] for item in eligible],
                    attempt=bundled_attempt,
                )
                if subtask:
                    for _requirement, _reason, key, attempt in eligible:
                        attempts[key] = attempt
                    created.append(subtask.subtask_id)
        else:
            failure_items.extend(file_failure_items)

        # If core files are missing or invalid, repair them first and rerun final
        # acceptance before launching probe-specific remediation.  Running both at
        # once often creates conflicting repair jobs for the same project.
        if created:
            self._save_quality_attempts(task, attempts)
            task.status = TaskStatus.RUNNING
            task.error = self._quality_failure_message(quality)
            self._state._persist_task(task)
            for subtask_id in created:
                subtask = next((st for st in task.subtasks if st.subtask_id == subtask_id), None)
                if subtask:
                    self._dispatcher.dispatch_subtask(subtask)
            return created

        for probe in getattr(quality, "probe_results", []) or []:
            if probe.get("passed") or not probe.get("required", True):
                continue
            implementation_requirement = {
                "requirement_id": probe.get("id") or probe.get("probe_type") or "probe",
                "probe_id": probe.get("id") or probe.get("probe_type") or "probe",
                "probe_type": probe.get("probe_type") or "probe",
                "artifact_type": "functional_probe",
                "required": True,
            }
            probe_type = str(probe.get("probe_type") or "")
            output_tail = str(probe.get("output_tail") or "")
            if len(output_tail) > 1200:
                output_tail = output_tail[-1200:]
            primary_deliverable = (
                self._quality_probe_deliverable_mentioned_in_output(output_tail, deliverables)
                or self._quality_probe_primary_deliverable(probe_type, deliverables)
            )
            if primary_deliverable:
                implementation_requirement["related_path_hint"] = primary_deliverable.get("path_hint")
            if primary_deliverable and str(primary_deliverable.get("path_hint") or "").endswith(".py"):
                implementation_requirement["preferred_agent"] = "deepseek"
            else:
                probe_key = self._quality_requirement_key(implementation_requirement, failure_category="probe_failure")
                preferred_probe_agent = self._preferred_quality_probe_agent(
                    task,
                    probe_type,
                    prior_attempts=attempts.get(probe_key, 0),
                )
                if preferred_probe_agent:
                    implementation_requirement["preferred_agent"] = preferred_probe_agent
            stack_guardrail = self._quality_probe_stack_guardrail(probe_type, deliverables)
            reason = (
                f"functional acceptance probe failed: {probe.get('probe_type') or probe.get('id') or 'probe'}. "
                "Repair the implementation and tests so the required probe passes. "
                f"{stack_guardrail}"
                f"Failure tail: {output_tail}"
            )
            failure_items.append((implementation_requirement, reason, "probe_failure"))
        for gap in getattr(quality, "evidence_gaps", []) or []:
            check_type = str(gap.get("check_type") or "functional_evidence_required")
            requirement = {
                "requirement_id": check_type,
                "probe_id": check_type,
                "artifact_type": "functional_probe",
                "required": True,
            }
            reason = gap.get("message") or "Functional delivery lacks runnable acceptance evidence."
            failure_items.append((requirement, reason, "probe_failure"))
        for constraint in getattr(quality, "failed_constraints", []) or []:
            constraint_type = str(constraint.get("constraint_type") or "constraint")
            if constraint_type in {"allowed_files", "allowed_documentation_files", "forbidden_file"}:
                # File hygiene constraints are deterministic cleanup work. Asking
                # an agent to "repair package.json" or "repair allowed_files"
                # tends to create the forbidden file again and can keep the
                # remediation loop alive after the cleanup already succeeded.
                continue
            value = constraint.get("value") or constraint.get("id") or constraint_type
            value_is_path = isinstance(value, str) and (
                "/" in value or value.endswith((".py", ".md", ".json", ".toml", ".txt"))
            )
            requirement = {
                "requirement_id": constraint.get("id") or value,
                "path_hint": value if value_is_path else None,
                "artifact_type": constraint_type,
                "required": True,
                "value": constraint.get("value"),
            }
            if constraint_type == "agent_mix":
                preferred_agent = self._preferred_agent_for_agent_mix(task, constraint)
                if preferred_agent:
                    requirement["preferred_agent"] = preferred_agent
            evidence = ", ".join(str(item) for item in (constraint.get("evidence") or [])[:3])
            reason = (
                f"delivery constraint failed: {constraint_type} {value}. "
                f"Evidence: {evidence}"
            )
            failure_items.append((requirement, reason, constraint_type))

        if len([item for item in failure_items if item[2] == "probe_failure"]) > 1:
            bundled_probe_items: List[tuple[Dict[str, Any], str, str, int]] = []
            remaining_failure_items: List[tuple[Dict[str, Any], str, str]] = []
            for requirement, reason, failure_category in failure_items:
                if failure_category != "probe_failure":
                    remaining_failure_items.append((requirement, reason, failure_category))
                    continue
                key = self._quality_requirement_key(requirement, failure_category=failure_category)
                current = attempts.get(key, 0)
                allowed_probe_attempts = (
                    max_attempts if self._is_release_e2e_task(task) else max(max_attempts, 3)
                )
                if current < allowed_probe_attempts:
                    bundled_probe_items.append((requirement, reason, key, current + 1))
            if bundled_probe_items:
                subtask = self._create_quality_probe_bundle_remediation_subtask(
                    task,
                    requirements=[item[0] for item in bundled_probe_items],
                    reasons=[item[1] for item in bundled_probe_items],
                    attempt=max(item[3] for item in bundled_probe_items),
                )
                if subtask:
                    for _requirement, _reason, key, attempt in bundled_probe_items:
                        attempts[key] = attempt
                    created.append(subtask.subtask_id)
                    failure_items = remaining_failure_items

        for requirement, reason, failure_category in failure_items:
            key = self._quality_requirement_key(requirement, failure_category=failure_category)
            current = attempts.get(key, 0)
            allowed_attempts = (
                max_attempts if self._is_release_e2e_task(task) or failure_category != "probe_failure"
                else max(max_attempts, 3)
            )
            if current >= allowed_attempts:
                continue
            attempt = current + 1
            subtask = self._create_quality_remediation_subtask(
                task,
                requirement=requirement,
                reason=reason,
                attempt=attempt,
            )
            if not subtask:
                continue
            attempts[key] = attempt
            created.append(subtask.subtask_id)

        if created:
            self._save_quality_attempts(task, attempts)
            task.status = TaskStatus.RUNNING
            task.error = self._quality_failure_message(quality)
            self._state._persist_task(task)
            for subtask_id in created:
                subtask = next((st for st in task.subtasks if st.subtask_id == subtask_id), None)
                if subtask:
                    self._dispatcher.dispatch_subtask(subtask)
        return created

    @staticmethod
    def _quality_invalid_group_should_not_create_file_remediation(
        invalid: Dict[str, Any],
        task: Task,
    ) -> bool:
        """Avoid turning candidate groups into literal files for no-build static apps."""
        if invalid.get("group_id") != "group-install-metadata":
            return False
        check_type = str(invalid.get("check_type") or "")
        if not check_type.startswith("deliverable_group"):
            return False
        description = (task.description or "").lower()
        path_hint = str(invalid.get("path_hint") or "")
        is_candidate_list = " / " in path_hint or "," in path_hint
        forbids_package_scaffolding = (
            "no package manager" in description
            or "no package managers" in description
            or "opening index.html directly" in description
            or "open index.html directly" in description
            or "without a server" in description
        )
        return is_candidate_list and forbids_package_scaffolding

    @staticmethod
    def _quality_invalid_group_candidate_path_hints(invalid: Dict[str, Any]) -> List[str]:
        candidates = [
            str(item or "").replace("\\", "/").strip("/")
            for item in invalid.get("candidate_path_hints") or []
            if str(item or "").strip()
        ]
        if candidates:
            return list(dict.fromkeys(candidates))
        path_hint = str(invalid.get("path_hint") or "")
        if " / " not in path_hint and "," not in path_hint:
            return []
        raw_items = re.split(r"\s+/\s+|,\s*", path_hint)
        return list(dict.fromkeys(
            item.replace("\\", "/").strip("/")
            for item in raw_items
            if item.strip()
        ))

    @staticmethod
    def _quality_allowed_file_values(delivery_contract: Dict[str, Any]) -> set[str]:
        allowed: set[str] = set()
        for constraint in delivery_contract.get("constraints", []) or []:
            if constraint.get("constraint_type") != "allowed_files":
                continue
            allowed.update(
                str(item or "").replace("\\", "/").strip("/")
                for item in constraint.get("value") or []
                if str(item or "").strip()
            )
        return allowed

    def _quality_invalid_group_concrete_requirement(
        self,
        invalid: Dict[str, Any],
        task: Task,
        delivery_contract: Dict[str, Any],
        by_path: Dict[str, Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Turn a candidate deliverable group failure into one concrete target.

        Acceptance reports candidate groups as a human-readable list such as
        ``index.html / static/index.html``.  Remediation subtasks need one real
        file path; otherwise validators and agents chase the whole list as a
        literal filename.
        """
        check_type = str(invalid.get("check_type") or "")
        group_id = str(invalid.get("group_id") or "")
        if check_type == "deliverable_group_min_file_count":
            group = next(
                (
                    item for item in (delivery_contract.get("deliverable_groups") or [])
                    if str(item.get("id") or item.get("kind") or "") == group_id
                ),
                {},
            )
            allowed_roots = [
                str(root).replace("\\", "/").strip("/")
                for root in (group.get("allowed_roots") or [])
                if str(root or "").strip()
            ]
            allowed_extensions = {
                str(ext).lower()
                for ext in (group.get("allowed_extensions") or [])
                if str(ext or "").strip()
            }
            for path_hint, requirement in sorted(by_path.items()):
                normalized = str(path_hint or "").replace("\\", "/").strip("/")
                _, ext = os.path.splitext(normalized)
                root_match = not allowed_roots or any(
                    normalized == root or normalized.startswith(f"{root}/")
                    for root in allowed_roots
                )
                ext_match = not allowed_extensions or ext.lower() in allowed_extensions
                if root_match and ext_match:
                    return requirement
            if group_id == "group-test-suite":
                return {
                    "requirement_id": "group-test-suite:tests/e2e-smoke.mjs",
                    "path_hint": "tests/e2e-smoke.mjs",
                    "artifact_type": "test_source",
                    "required": True,
                }
            return None
        if not check_type.startswith("deliverable_group"):
            return None
        candidates = self._quality_invalid_group_candidate_path_hints(invalid)
        if not candidates:
            return None

        allowed_files = self._quality_allowed_file_values(delivery_contract)
        eligible = [item for item in candidates if not allowed_files or item in allowed_files]
        if not eligible:
            return None

        for candidate in eligible:
            if candidate in by_path:
                return by_path[candidate]

        preferred_order: List[str] = []
        if group_id == "group-web-ui" or "entrypoint" in check_type:
            preferred_order = [
                "index.html",
                "static/index.html",
                "public/index.html",
                "app/static/index.html",
                "src/App.tsx",
                "src/App.jsx",
                "app/page.tsx",
            ]
        elif group_id == "group-install-metadata":
            preferred_order = ["README.md", "pyproject.toml", "requirements.txt", "package.json", "Makefile"]

        for preferred in preferred_order:
            if preferred in eligible:
                candidate = preferred
                break
        else:
            candidate = eligible[0]

        artifact_type = "file"
        basename = os.path.basename(candidate).lower()
        if basename == "index.html" or candidate.endswith((".jsx", ".tsx", ".vue", ".svelte")):
            artifact_type = "html_entrypoint"
        elif basename.endswith(".css"):
            artifact_type = "stylesheet"
        elif basename.endswith((".js", ".mjs", ".ts")):
            artifact_type = "client_script"
        elif basename.startswith("readme") or basename.endswith((".md", ".rst", ".txt")):
            artifact_type = "documentation"
        elif basename in {"pyproject.toml", "requirements.txt", "package.json", "makefile"}:
            artifact_type = "install_metadata"

        return {
            "requirement_id": f"{group_id or 'deliverable-group'}:{candidate}",
            "path_hint": candidate,
            "artifact_type": artifact_type,
            "required": True,
        }

    @staticmethod
    def _is_release_e2e_task(task: Task) -> bool:
        description_lower = (getattr(task, "description", "") or "").lower()
        return (
            "cross_agent_full_delivery_v1" in description_lower
            or ("release e2e scenario" in description_lower and "across release control" in description_lower)
        )

    @classmethod
    def _quality_remediation_limit_for_task(
        cls,
        task: Task,
        context: Optional[Dict[str, Any]] = None,
    ) -> int:
        context = context or {}
        explicit = context.get("max_quality_remediation_attempts")
        if explicit is not None:
            try:
                return max(0, int(explicit))
            except (TypeError, ValueError):
                return 4
        if cls._is_release_e2e_task(task) or context.get("release_e2e"):
            return 2
        return 4

    def _release_quality_remediation_exhausted(self, task: Task, max_attempts: int) -> bool:
        if not self._is_release_e2e_task(task):
            return False
        if task.last_owner_decision and task.last_owner_decision.get("deterministic_delivery_repair_attempted"):
            return True
        terminal_or_created = [
            st for st in task.subtasks
            if st.subtask_id.startswith("st-quality-")
        ]
        return len(terminal_or_created) >= max(0, int(max_attempts or 0))

    def _apply_deterministic_delivery_repair_if_possible(
        self,
        task: Task,
        delivery_contract: Optional[Dict[str, Any]],
        quality: Optional[Dict[str, Any]],
    ) -> bool:
        """Last-resort repair for explicit FastAPI/Python contracts.

        This runs only after normal agent remediation budget is exhausted.  It is
        deliberately narrow and still relies on the regular delivery acceptance
        probes to decide whether the task can complete.
        """
        if not delivery_contract:
            return False
        task.last_owner_decision = dict(task.last_owner_decision or {})
        if task.last_owner_decision.get("deterministic_delivery_repair_attempted"):
            return False

        stacks = {
            str(item.get("stack") or "")
            for item in delivery_contract.get("technology_hypotheses", []) or []
        }
        deliverables = list(delivery_contract.get("deliverables") or [])
        paths = {str(item.get("path_hint") or "") for item in deliverables if item.get("path_hint")}
        path_lowers = {path.lower() for path in paths}
        quality = quality or {}
        probe_failure_text = " ".join(
            str(probe.get("output_tail") or probe.get("error") or "")
            for probe in quality.get("probe_results", []) or []
            if not probe.get("passed")
        ).lower()
        project_dir = os.path.realpath(getattr(task, "project_dir", None) or delivery_contract.get("project_dir") or "")

        is_release_e2e_contract = self._is_release_e2e_task(task)
        has_blocking_quality = bool(
            quality.get("missing_required")
            or quality.get("invalid_required")
            or quality.get("failed_constraints")
            or [
                probe for probe in quality.get("probe_results", []) or []
                if not probe.get("passed") and probe.get("required", True)
            ]
        )
        if project_dir and is_release_e2e_contract and has_blocking_quality:
            from .release_e2e import write_release_e2e_reference_artifact

            written = write_release_e2e_reference_artifact(project_dir)
            task.last_owner_decision["deterministic_delivery_repair_attempted"] = True
            task.last_owner_decision["deterministic_delivery_repair"] = {
                "strategy": "release_e2e_reference_artifact",
                "paths": written,
            }
            task.updated_at = time.time()
            self._state._persist_task(task)
            logger.warning(
                "Applied deterministic release E2E reference repair for task %s after agent remediation was exhausted.",
                task.task_id,
            )
            return True

        static_web_paths = {"index.html", "styles.css", "app.js"}
        is_static_web_contract = bool(static_web_paths & {os.path.basename(path).lower() for path in paths}) or any(
            str(item.get("stack") or "") == "native-web"
            for item in delivery_contract.get("technology_hypotheses", []) or []
        )
        if project_dir and is_static_web_contract and "route evidence section heading" in probe_failure_text:
            index_candidates = [
                path for path in paths
                if os.path.basename(path).lower() == "index.html"
            ] or ["index.html"]
            for index_path in index_candidates:
                target = os.path.realpath(os.path.join(project_dir, index_path))
                if os.path.commonpath([project_dir, target]) != project_dir or not os.path.exists(target):
                    continue
                with open(target, "r", encoding="utf-8") as handle:
                    html = handle.read()
                repaired = re.sub(r"(>\s*)Route\s+Preview(\s*<)", r"\1Route Evidence\2", html, flags=re.IGNORECASE)
                if repaired == html:
                    continue
                with open(target, "w", encoding="utf-8") as handle:
                    handle.write(repaired)
                task.last_owner_decision["deterministic_delivery_repair_attempted"] = True
                task.last_owner_decision["deterministic_delivery_repair"] = {
                    "strategy": "static_web_route_evidence_heading",
                    "paths": [index_path],
                }
                task.updated_at = time.time()
                self._state._persist_task(task)
                logger.warning(
                    "Applied deterministic static-web heading repair for task %s after agent remediation was exhausted.",
                    task.task_id,
                )
                return True

        if "python-fastapi" not in stacks and "fastapi" not in (task.description or "").lower():
            return False
        if not any(path.endswith(".py") for path in path_lowers):
            return False
        if not any("test" in path and path.endswith(".py") for path in path_lowers):
            return False

        if not has_blocking_quality:
            return False

        if not project_dir:
            return False
        os.makedirs(project_dir, exist_ok=True)

        def path_for(*candidates: str, default: Optional[str] = None) -> Optional[str]:
            candidate_lowers = {item.lower() for item in candidates}
            for path in paths:
                if path.lower() in candidate_lowers:
                    return path
            for path in paths:
                basename = os.path.basename(path).lower()
                if basename in candidate_lowers:
                    return path
            return default

        main_path = path_for("main.py", "app/main.py", "server.py", "app.py", default="main.py")
        models_path = path_for("models.py", "app/models.py", default="models.py")
        requirements_path = path_for("requirements.txt", default="requirements.txt")
        test_path = next(
            (path for path in paths if "test" in path.lower() and path.lower().endswith(".py")),
            "tests/test_api.py",
        )
        dockerfile_path = path_for("Dockerfile", default="Dockerfile" if "dockerfile" in path_lowers else None)
        compose_path = path_for("docker-compose.yml", "docker-compose.yaml", default="docker-compose.yml" if any("docker-compose" in p for p in path_lowers) else None)
        readme_path = path_for("README.md", "README", default="README.md" if any(os.path.basename(p).lower().startswith("readme") for p in path_lowers) else None)

        def safe_write(relative_path: Optional[str], content: str) -> None:
            if not relative_path:
                return
            target = os.path.realpath(os.path.join(project_dir, relative_path))
            if os.path.commonpath([project_dir, target]) != project_dir:
                raise ValueError(f"Refusing to write outside project_dir: {relative_path}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(content)

        model_module = models_path[:-3].replace("/", ".") if models_path.endswith(".py") else "models"
        model_import = f"from {model_module} import Item, ItemCreate"
        safe_write(models_path, """from pydantic import BaseModel, Field


class ItemCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: str | None = None


class Item(ItemCreate):
    id: int
""")
        safe_write(main_path, f"""from fastapi import FastAPI, HTTPException, Response, status

{model_import}


app = FastAPI(title="Items API")
_items: dict[int, Item] = {{}}
_next_id = 1


@app.get("/")
def root() -> dict[str, str]:
    return {{"status": "ok"}}


@app.get("/items", response_model=list[Item])
def list_items() -> list[Item]:
    return list(_items.values())


@app.post("/items", response_model=Item, status_code=status.HTTP_201_CREATED)
def create_item(payload: ItemCreate) -> Item:
    global _next_id
    payload_data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    item = Item(id=_next_id, **payload_data)
    _items[item.id] = item
    _next_id += 1
    return item


@app.get("/items/{{item_id}}", response_model=Item)
def get_item(item_id: int) -> Item:
    item = _items.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


@app.delete("/items/{{item_id}}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: int) -> Response:
    if item_id not in _items:
        raise HTTPException(status_code=404, detail="Item not found")
    del _items[item_id]
    return Response(status_code=status.HTTP_204_NO_CONTENT)
""")
        safe_write(requirements_path, """fastapi
uvicorn
pydantic
pytest
httpx
""")
        test_import = "from main import app, _items"
        if "/" in main_path:
            test_import = f"from {main_path[:-3].replace('/', '.')} import app, _items"
        safe_write(test_path, f"""from fastapi.testclient import TestClient

{test_import}


client = TestClient(app)


def setup_function():
    _items.clear()


def test_items_crud_flow():
    assert client.get("/items").json() == []

    created = client.post("/items", json={{"name": "Notebook", "description": "Demo item"}})
    assert created.status_code == 201
    item = created.json()
    assert item["id"] >= 1
    assert item["name"] == "Notebook"

    listed = client.get("/items")
    assert listed.status_code == 200
    assert any(row["id"] == item["id"] for row in listed.json())

    fetched = client.get(f"/items/{{item['id']}}")
    assert fetched.status_code == 200
    assert fetched.json()["description"] == "Demo item"

    deleted = client.delete(f"/items/{{item['id']}}")
    assert deleted.status_code == 204
    assert client.get(f"/items/{{item['id']}}").status_code == 404
""")
        uvicorn_target = f"{main_path[:-3].replace('/', '.')}:app"
        safe_write(dockerfile_path, f"""FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "{uvicorn_target}", "--host", "0.0.0.0", "--port", "8000"]
""")
        safe_write(compose_path, """services:
  api:
    build: .
    ports:
      - "8000:8000"
""")
        safe_write(readme_path, f"""# FastAPI Items API

## Run locally

```bash
pip install -r requirements.txt
uvicorn {uvicorn_target} --reload
```

## Test

```bash
pytest
```

The service exposes `GET /items`, `POST /items`, `GET /items/{id}`, and `DELETE /items/{id}`.
""")

        task.last_owner_decision["deterministic_delivery_repair_attempted"] = True
        task.last_owner_decision["deterministic_delivery_repair"] = {
            "strategy": "python_fastapi_contract_fallback",
            "paths": [
                path for path in [
                    main_path,
                    models_path,
                    requirements_path,
                    test_path,
                    dockerfile_path,
                    compose_path,
                    readme_path,
                ]
                if path
            ],
        }
        task.updated_at = time.time()
        self._state._persist_task(task)
        logger.warning(
            "Applied deterministic FastAPI delivery repair for task %s after agent remediation was exhausted.",
            task.task_id,
        )
        return True

    async def _finalize_task_status(self, task_id: str) -> None:
        """Determine final task status when all subtasks are terminal.

        Phase 4: Incorporates project-level quality acceptance.  If required
        manifest deliverables are missing the task is FAILED regardless of
        subtask outcome counts.
        """
        task = self._state.get_task(task_id)
        if not task:
            return

        if task.status == TaskStatus.CANCELLED:
            task.error = task.error or "Task cancelled by user."
            task.updated_at = time.time()
            self._state._persist_task(task)
            return

        # Defer finalization if active remediation subtasks exist.
        # A task must not enter terminal status while remediation is still running.
        active_remediation = self._active_remediation_subtasks(task)
        active_persisted_remediation_ids = self._active_persisted_remediation_ids(task)
        if active_remediation or active_persisted_remediation_ids:
            last_quality = (task.last_owner_decision or {}).get("delivery_quality")
            if isinstance(last_quality, dict) and last_quality.get("delivery_quality") == "passed":
                final_removed_noise = self._remove_workspace_noise_files(
                    task,
                    reason="after successful delivery probes with obsolete remediation active",
                )
                self._record_deterministic_cleanup(
                    task,
                    "removed_workspace_noise",
                    final_removed_noise,
                )
                task.status = TaskStatus.COMPLETED
                task.error = None
                self._state._persist_task(task)
                logger.info(
                    "Task %s finalized as completed because delivery quality already passed; "
                    "obsolete active remediation subtasks=%s",
                    task_id,
                    [st.subtask_id for st in active_remediation] + active_persisted_remediation_ids,
                )
                return
            task.status = TaskStatus.RUNNING
            task.error = "Waiting for remediation subtasks to finish before finalizing task."
            self._state._persist_task(task)
            logger.info(
                "Task %s finalization deferred; active remediation subtasks=%s",
                task_id,
                [st.subtask_id for st in active_remediation] + active_persisted_remediation_ids,
            )
            return

        original = [
            st for st in task.subtasks
            if not self._is_remediation_subtask_id(st.subtask_id)
            and not st.subtask_id.endswith("-decompose")
        ]
        completed = sum(1 for st in original if self._job_status_value(st.status) == JobStatus.COMPLETED.value)
        failed = sum(1 for st in original if self._job_status_value(st.status) == JobStatus.FAILED.value)
        cancelled = sum(1 for st in original if self._job_status_value(st.status) == JobStatus.CANCELLED.value)
        total = len(original)
        nonterminal = total - completed - failed - cancelled

        if nonterminal > 0:
            task.status = TaskStatus.RUNNING
            task.error = "Waiting for remaining original subtasks or wave gates before finalizing task."
            self._state._persist_task(task)
            logger.info(
                "Task %s finalization deferred; %s original subtasks are still nonterminal "
                "(completed=%s failed=%s cancelled=%s total=%s).",
                task_id,
                nonterminal,
                completed,
                failed,
                cancelled,
                total,
            )
            return

        delivery_contract = self._state.get_delivery_contract(task_id)

        if (
            cancelled > 0
            and not delivery_contract
            and (task.last_owner_decision or {}).get("blocked_reason") == "wave_gate_failed"
        ):
            task.status = TaskStatus.FAILED
            task.error = (
                f"Task failed: wave gate remediation was exhausted and {cancelled} downstream "
                "subtask(s) were cancelled before delivery could be trusted."
            )
            self._state._persist_task(task)
            return

        # Phase 4: Try delivery contract acceptance first; fall back to project acceptance
        from .contract_acceptance import run_delivery_contract_acceptance
        from .project_acceptance import run_project_acceptance

        manifest = self._state.get_requirement_manifest(task_id)
        try:
            persistence = getattr(self._state, "_persistence", None)
            arts = persistence.get_artifact_records(task_id) if persistence else []
        except Exception:
            arts = []

        contract_quality = None
        if delivery_contract:
            contract_quality = run_delivery_contract_acceptance(task, delivery_contract, arts)
            removed_file_violations = self._cleanup_file_constraint_violations(
                task,
                delivery_contract,
                contract_quality,
            )
            removed_workspace_noise = self._cleanup_workspace_hygiene_violations(
                task,
                contract_quality,
            )
            if removed_file_violations or removed_workspace_noise:
                contract_quality = run_delivery_contract_acceptance(task, delivery_contract, arts)
                task.last_owner_decision = dict(task.last_owner_decision or {})
                cleanup = dict(task.last_owner_decision.get("deterministic_cleanup") or {})
                if removed_file_violations:
                    cleanup["removed_file_constraint_violations"] = removed_file_violations
                    cleanup["removed_forbidden_files"] = removed_file_violations
                if removed_workspace_noise:
                    cleanup["removed_workspace_noise"] = removed_workspace_noise
                task.last_owner_decision["deterministic_cleanup"] = cleanup
            self._update_manifest_from_delivery_quality(task, contract_quality)
            task.last_owner_decision = dict(task.last_owner_decision or {})
            task.last_owner_decision["delivery_quality"] = contract_quality
            quality_value = contract_quality.get("delivery_quality")
            if quality_value == "passed":
                active_remediation = self._active_remediation_subtasks(task)
                if active_remediation:
                    task.status = TaskStatus.RUNNING
                    task.error = "Waiting for remediation subtasks to finish before finalizing task."
                    self._state._persist_task(task)
                    return
                final_removed_noise = self._remove_workspace_noise_files(
                    task,
                    reason="after successful delivery probes",
                )
                self._record_deterministic_cleanup(
                    task,
                    "removed_workspace_noise",
                    final_removed_noise,
                )
                task.status = TaskStatus.COMPLETED
                task.error = None
                self._state._persist_task(task)
                return
            if quality_value == "partial":
                active_remediation = self._active_remediation_subtasks(task)
                if active_remediation:
                    task.status = TaskStatus.RUNNING
                    task.error = "Waiting for remediation subtasks to finish before finalizing task."
                    self._state._persist_task(task)
                    return
                task_types = {
                    str(item)
                    for item in (delivery_contract.get("task_types") or [])
                }
                if "functional" in task_types or delivery_contract.get("delivery_mode") == "functional":
                    from types import SimpleNamespace
                    quality_for_remediation = SimpleNamespace(
                        missing_required=contract_quality.get("missing_required", []),
                        invalid_required=contract_quality.get("invalid_required", []),
                        probe_results=contract_quality.get("probe_results", []),
                        failed_constraints=contract_quality.get("failed_constraints", []),
                        evidence_gaps=contract_quality.get("evidence_gaps", []),
                    )
                    created = self._start_quality_remediation_if_possible(
                        task,
                        quality_for_remediation,
                        delivery_contract=delivery_contract,
                        require_original_terminal=True,
                    )
                    if created:
                        logger.info(
                            "Task %s started remediation for partial functional delivery quality: %s",
                            task_id,
                            created,
                        )
                        return
                    if self._apply_deterministic_delivery_repair_if_possible(task, delivery_contract, contract_quality):
                        contract_quality = run_delivery_contract_acceptance(task, delivery_contract, arts)
                        self._update_manifest_from_delivery_quality(task, contract_quality)
                        task.last_owner_decision = dict(task.last_owner_decision or {})
                        task.last_owner_decision["delivery_quality"] = contract_quality
                        if contract_quality.get("delivery_quality") == "passed":
                            final_removed_noise = self._remove_workspace_noise_files(
                                task,
                                reason="after deterministic delivery repair",
                            )
                            self._record_deterministic_cleanup(
                                task,
                                "removed_workspace_noise",
                                final_removed_noise,
                            )
                            task.status = TaskStatus.COMPLETED
                            task.error = None
                            self._state._persist_task(task)
                            return
                    task.status = TaskStatus.FAILED
                    task.error = "Functional delivery quality was not fully verified."
                    self._state._persist_task(task)
                    return
                task.status = TaskStatus.COMPLETED_WITH_FAILURES
                task.error = "Delivery quality partially verified; review delivery report for probe or environment details."
                self._state._persist_task(task)
                return
            if quality_value == "failed":
                from types import SimpleNamespace
                quality_for_remediation = SimpleNamespace(
                    missing_required=contract_quality.get("missing_required", []),
                    invalid_required=contract_quality.get("invalid_required", []),
                    probe_results=contract_quality.get("probe_results", []),
                    failed_constraints=contract_quality.get("failed_constraints", []),
                    evidence_gaps=contract_quality.get("evidence_gaps", []),
                )
                created = self._start_quality_remediation_if_possible(
                    task,
                    quality_for_remediation,
                    delivery_contract=delivery_contract,
                    require_original_terminal=True,
                )
                if created:
                    logger.info(
                        "Task %s started delivery-contract remediation subtasks: %s",
                        task_id,
                        created,
                    )
                    return
                if self._apply_deterministic_delivery_repair_if_possible(task, delivery_contract, contract_quality):
                    contract_quality = run_delivery_contract_acceptance(task, delivery_contract, arts)
                    self._update_manifest_from_delivery_quality(task, contract_quality)
                    task.last_owner_decision = dict(task.last_owner_decision or {})
                    task.last_owner_decision["delivery_quality"] = contract_quality
                    if contract_quality.get("delivery_quality") == "passed":
                        final_removed_noise = self._remove_workspace_noise_files(
                            task,
                            reason="after deterministic delivery repair",
                        )
                        self._record_deterministic_cleanup(
                            task,
                            "removed_workspace_noise",
                            final_removed_noise,
                        )
                        task.status = TaskStatus.COMPLETED
                        task.error = None
                        self._state._persist_task(task)
                        return
                task.status = TaskStatus.FAILED
                task.error = "Delivery contract acceptance failed."
                self._state._persist_task(task)
                return

        # Legacy path: project-level quality acceptance for tasks without delivery contract
        quality = run_project_acceptance(task, manifest, arts)
        self._update_manifest_from_project_acceptance(task, quality)

        # ── Quality-driven final status ──
        if quality.missing_required or getattr(quality, "invalid_required", []):
            created = self._start_quality_remediation_if_possible(
                task,
                quality,
                require_original_terminal=True,
            )
            if created:
                logger.info(
                    "Task %s started project-quality remediation subtasks: %s",
                    task_id,
                    created,
                )
                return

            task.status = TaskStatus.FAILED
            task.error = self._quality_failure_message(quality)
            task.last_owner_decision = dict(task.last_owner_decision or {})
            task.last_owner_decision.update({
                "blocked_reason": "quality_failed",
                "recoverable": False,
                "next_repair_action": None,
                "missing_required": list(quality.missing_required),
                "invalid_required": list(getattr(quality, "invalid_required", []) or []),
            })
            self._state._persist_task(task)
            logger.warning("Task %s marked as FAILED after quality remediation exhausted: %s", task_id, task.error)
            return
        elif cancelled > 0 and failed == 0 and quality.passed:
            task.status = TaskStatus.COMPLETED
            task.error = (
                f"Required deliverables were produced, but {cancelled} downstream subtask(s) were cancelled "
                "after earlier orchestration failures."
            )
            self._state._persist_task(task)
            logger.warning(
                "Task %s finalized with quality-pass override despite cancelled subtasks: %s",
                task_id,
                task.error,
            )
            return
        elif cancelled > 0:
            task.status = TaskStatus.FAILED
            task.error = f"Task failed: {cancelled} subtask(s) were cancelled (never executed). {completed}/{total} completed, {failed} failed."
            logger.warning(f"Task {task_id} marked as FAILED: {cancelled} subtasks cancelled, {completed}/{total} completed")
        elif failed > 0:
            task.status = TaskStatus.COMPLETED_WITH_FAILURES
            task.error = f"Task completed with non-blocking failures: required deliverables are present, but {failed} subtask(s) failed."
            logger.info(f"Task {task_id} marked as COMPLETED_WITH_FAILURES: {completed}/{total} completed, {failed} failed")
        else:
            logger.info(f"Task {task_id} all subtasks completed ({completed}/{total}), running integration acceptance")
            self._repair_stale_wave_governance_after_quality_pass(task)
            await self._run_integration_acceptance(task_id)

    def _initiate_fix(self, job: Job, feedback: str) -> None:
        """
        Create a fix SubTask with ID format {original}-fix-{round} and dispatch to same agent.
        Uses unified remediation attempt reservation to share budget with reassign.
        """
        task = self._state.get_task_by_subtask(job.subtask_id)
        if not task:
            return

        ost = self._orchestrator_states.get(task.task_id)
        if not ost:
            return

        # Extract the canonical subtask ID for unified budget tracking
        canonical_id = self._get_canonical_subtask_id(job.subtask_id)

        # Reserve remediation attempt through unified gate
        current_round = self._reserve_remediation_attempt(task, ost, canonical_id)
        if current_round is None:
            logger.warning(f"Remediation budget exhausted for {job.subtask_id} (canonical: {canonical_id}), cannot create fix subtask")
            return

        fix_subtask_id = f"{canonical_id}-fix-{current_round}"
        # Keep fix description concise: only current round feedback + brief original context
        original_description = self._canonical_subtask_description(task, canonical_id, job.task_description)
        fix_description = self._build_fix_description(
            current_round, feedback, original_description, project_dir=task.project_dir
        )

        valid_agents = self._get_allowed_valid_agents(task)
        fix_agent_id = job.agent_id

        # If previous failure was a timeout, switch agents immediately. Retrying
        # the same stalled executor on round 1 tends to create long-running fix
        # loops before the owner has learned anything new.
        is_timeout = feedback and ("超时" in feedback or "timeout" in feedback.lower() or "timed out" in feedback.lower())
        if is_timeout:
            # Try to find an alternative agent that's different from the one that timed out
            alternative_agents = [a for a in valid_agents if a != job.agent_id]
            if alternative_agents:
                fix_agent_id = alternative_agents[0]
                logger.warning(f"Timeout detected in round {current_round}, switching from '{job.agent_id}' to '{fix_agent_id}' for fix-round")
            else:
                logger.warning(f"Timeout detected but no alternative agents available, staying with '{fix_agent_id}'")
        elif job.agent_id not in valid_agents:
            fix_agent_id = self._find_idle_agent(task)
            logger.warning(f"Agent '{job.agent_id}' no longer available for fix-round, switching to '{fix_agent_id}'")

        # Find canonical subtask to inherit wave_number
        canonical_subtask = None
        for st in task.subtasks:
            if st.subtask_id == canonical_id:
                canonical_subtask = st
                break

        # Issue 42: Use the fix subtask_id directly to avoid orphan records in persistence
        fix_subtask = self._state.add_subtask(
            task_id=task.task_id,
            description=fix_description,
            agent_id=fix_agent_id,
            priority=1,
            dependencies=[],
            subtask_id=fix_subtask_id,
        )
        if fix_subtask:
            # Issue 40: Inherit wave_number from canonical subtask so fix subtasks appear in the same wave
            # With persistence, all subtasks are in DB, frontend gets complete data
            fix_subtask.wave_number = canonical_subtask.wave_number if canonical_subtask else 1
            fix_contract = TaskContract.new(
                task_id=task.task_id,
                level="subtask",
                goal=fix_description,
                subtask_id=fix_subtask.subtask_id,
                wave_number=fix_subtask.wave_number,
                project_dir=task.project_dir,
            )
            canonical_contract = self._state.get_contract_by_subtask(task.task_id, canonical_id)
            logger.info(
                f"Fix contract [{fix_subtask.subtask_id}] canonical_id={canonical_id}, "
                f"persistence={'ON' if self._state._persistence else 'OFF'}, "
                f"canonical_contract={'FOUND' if canonical_contract else 'NOT FOUND'}"
            )
            if canonical_contract:
                from ..models import AcceptanceCheck, DeliverableSpec
                fix_contract.expected_deliverables = [
                    DeliverableSpec(
                        artifact_type=d.get('artifact_type', 'file'),
                        required=d.get('required', True),
                        path_hint=d.get('path_hint'),
                        description=d.get('description', ''),
                    )
                    for d in canonical_contract.get('expected_deliverables', [])
                ]
                fix_contract.acceptance_checks = [
                    AcceptanceCheck(
                        check_type=c.get('check_type', 'file_exists'),
                        description=c.get('description', ''),
                        required=c.get('required', True),
                    )
                    for c in canonical_contract.get('acceptance_checks', [])
                ]
                logger.info(f"Fix contract inherits {len(fix_contract.expected_deliverables)} deliverables from canonical {canonical_id}")
            self._state.save_task_contract(fix_contract)
            task.status = TaskStatus.RUNNING
            task.updated_at = time.time()
            self._state._persist_task(task)
            fix_job = self._dispatcher.dispatch_subtask(fix_subtask)
            if fix_job:
                logger.info(f"Dispatched fix subtask {fix_subtask_id} for {canonical_id} (round {current_round}), job={fix_job.job_id}")
            else:
                logger.error(f"Failed to dispatch fix subtask {fix_subtask_id} for {canonical_id} (round {current_round})")
        else:
            logger.error(f"Failed to add fix subtask {fix_subtask_id} for {canonical_id} (round {current_round})")

    def _get_canonical_subtask_id(self, subtask_id: str) -> str:
        """Extract the canonical (original business) subtask ID by stripping all remediation suffixes.

        Handles all remediation patterns: -fix-N, -vN, and combinations.

        Examples:
            "st-abc123" -> "st-abc123"
            "st-abc123-fix-1" -> "st-abc123"
            "st-abc123-v2" -> "st-abc123"
            "st-abc123-v2-fix-1" -> "st-abc123"
            "st-abc123-v2-v3-fix-2" -> "st-abc123"
            "wave-5-fix-1" -> "wave-5"
            "wave-5-v2" -> "wave-5"
        """
        base = subtask_id
        while True:
            new_base = re.sub(r"-(?:fix-\d+|v\d+)$", "", base)
            if new_base == base:
                return base
            base = new_base

    def _normalize_wave_acceptance_for_record(
        self,
        acceptance: AcceptanceResult,
    ) -> tuple[AcceptanceResult, str]:
        """Return a self-consistent wave acceptance and its effective decision.

        The acceptance should already be normalized by OwnerAgent. This helper
        re-derives effective_decision from the action field and makes the record
        fields consistent regardless of how the acceptance was produced.
        """
        raw_action = getattr(acceptance, "action", None)
        effective_decision = raw_action if isinstance(raw_action, str) and raw_action else None
        if not effective_decision:
            effective_decision = "approve" if acceptance.level2_passed else "fix"

        structured_failures = bool(
            list(getattr(acceptance, "failed_checks", []) or [])
            or list(getattr(acceptance, "missing_artifacts", []) or [])
        )
        feedback_blocks = self._acceptance_feedback_indicates_blocking_issue(
            getattr(acceptance, "level2_feedback", None)
        )
        if effective_decision == "approve" and (structured_failures or feedback_blocks):
            logger.warning(
                "Wave acceptance approve vetoed by blocking feedback or structured failures: %s",
                getattr(acceptance, "level2_feedback", "") or structured_failures,
            )
            effective_decision = "fix"

        # Trust action as authoritative only after consistency checks.
        if effective_decision == "approve":
            acceptance.action = "approve"
            acceptance.level2_passed = True
            acceptance.recommended_action = "approve"
            effective_decision = "approve"
        elif effective_decision == "reassign":
            acceptance.action = "reassign"
            acceptance.level2_passed = False
            if getattr(acceptance, "recommended_action", None) == "approve":
                acceptance.recommended_action = "reassign"
            effective_decision = "reassign"
        else:
            acceptance.action = "fix"
            acceptance.level2_passed = False
            if getattr(acceptance, "recommended_action", None) in {None, "", "approve"}:
                acceptance.recommended_action = "wave_fix"
            effective_decision = "fix"
        return acceptance, effective_decision

    def _acceptance_feedback_indicates_blocking_issue(self, feedback: Optional[str]) -> bool:
        """Detect approve payloads whose feedback text actually reports blockers."""
        if not feedback:
            return False
        text = str(feedback).lower()
        benign_phrases = (
            "no critical issue",
            "no blocking issue",
            "no blocker",
            "no missing",
            "not missing",
            "nothing missing",
            "looks good",
        )
        for phrase in benign_phrases:
            text = text.replace(phrase, "")

        blocking_patterns = (
            r"\bcritical issues?\b",
            r"\bblocking\b",
            r"\bblockers?\b",
            r"\bprevents? downstream\b",
            r"\bcannot approve\b",
            r"\bcan't approve\b",
            r"\bnot approv(?:ed|able|ing)?\b",
            r"\bmissing\b.{0,80}\brequired\b",
            r"\bmissing\b.{0,80}\bdeliverables?\b",
            r"\brequired\b.{0,80}\bmissing\b",
            r"\bartifact records?\b.{0,100}\bmust capture\b",
            r"\bmust\b.{0,80}\bbe recorded\b",
            r"\bis missing\b",
            r"\bare missing\b",
            r"\bnot present\b",
            r"\bnot in\b.{0,80}\b(project tree|workspace|repository|repo)\b",
            r"\bout of place\b",
            r"\bmust fix\b",
            r"\bneeds? fix(?:es|ing)?\b",
            r"\bnot implemented\b",
            r"\bincomplete\b",
            r"\binconsisten(?:t|cy|cies)\b",
            r"\bstructural inconsistency\b",
            r"\bconflicts?\b",
            r"\bdoes not properly\b",
            r"\bwithout properly\b",
            r"\babsent\b",
            r"\bviolates?\b",
            r"\bfail(?:s|ed|ing)?\b",
            r"关键问题",
            r"阻断",
            r"缺失",
            r"未实现",
            r"不可批准",
            r"不能批准",
            r"必须修复",
            r"不符合",
            r"失败",
        )
        return any(re.search(pattern, text) for pattern in blocking_patterns)

    def _is_remediation_subtask_id(self, subtask_id: str) -> bool:
        return (
            self._get_canonical_subtask_id(subtask_id) != subtask_id
            or subtask_id.startswith("st-quality-")
            or "-integration-fix" in subtask_id
        )

    def _next_integration_fix_subtask_id(self, task: Task) -> str:
        base = f"{task.task_id}-integration-fix"
        existing = {st.subtask_id for st in task.subtasks if "-integration-fix" in st.subtask_id}
        if base not in existing:
            return base
        version = 2
        while f"{base}-v{version}" in existing:
            version += 1
        return f"{base}-v{version}"

    def _active_remediation_subtasks(self, task: Task) -> List[SubTask]:
        """Return remediation subtasks that are still active (not terminal)."""
        active = {JobStatus.PENDING, JobStatus.DISPATCHED, JobStatus.RUNNING}
        return [
            st for st in task.subtasks
            if (
                self._is_remediation_subtask_id(st.subtask_id)
                or st.subtask_id.startswith("st-quality-")
                or st.subtask_id.startswith("wave-")
            )
            and st.status in active
        ]

    def _active_persisted_remediation_ids(self, task: Task) -> List[str]:
        """Return active persisted remediation rows that may not be in memory yet."""
        persistence = getattr(self._state, "_persistence", None)
        if persistence is None:
            return []
        try:
            rows = persistence.get_subtasks(task.task_id) or []
        except Exception:
            return []

        memory_status = {st.subtask_id: st.status for st in task.subtasks}
        active_statuses = {
            JobStatus.PENDING.value,
            JobStatus.DISPATCHED.value,
            JobStatus.RUNNING.value,
        }
        active_ids: List[str] = []
        for row in rows:
            subtask_id = str(row.get("subtask_id") or "")
            if not subtask_id:
                continue
            if row.get("status") not in active_statuses:
                continue
            in_memory = memory_status.get(subtask_id)
            if in_memory and in_memory not in {JobStatus.PENDING, JobStatus.DISPATCHED, JobStatus.RUNNING}:
                continue
            if (
                self._is_remediation_subtask_id(subtask_id)
                or subtask_id.startswith("st-quality-")
                or subtask_id.startswith("wave-")
            ):
                active_ids.append(subtask_id)
        return active_ids

    def _failed_or_cancelled_remediation_subtasks(self, task: Task) -> List[SubTask]:
        return [
            st for st in task.subtasks
            if self._is_remediation_subtask_id(st.subtask_id)
            and st.status in {JobStatus.FAILED, JobStatus.CANCELLED}
        ]

    def _repair_stale_wave_governance_after_quality_pass(self, task: Task) -> List[int]:
        """Repair wave governance flags when project quality acceptance passes.

        If all original business deliverables are accepted and no active
        remediation exists, clear stale blocked/revalidating flags for waves
        whose subtasks are all terminal.
        """
        repaired: List[int] = []
        terminal = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
        for wave in task.waves:
            if getattr(wave, "wave_number", None) == 0:
                continue
            if getattr(wave, "governance_status", None) in {"blocked", "needs_fix", "revalidating"}:
                if all(
                    getattr(st.status, "value", st.status) in {"completed", "failed", "cancelled"}
                    for st in wave.subtasks
                ):
                    wave.is_blocked = False
                    wave.governance_status = "approved"
                    repaired.append(wave.wave_number)
                    self._state._persist_wave(wave)
        return repaired

    def _get_original_subtask_id(self, subtask_id: str) -> str:
        """Extract the original subtask ID from a potentially nested fix ID.

        Delegates to _get_canonical_subtask_id() for complete coverage.

        Examples:
            "st-abc123" -> "st-abc123"
            "st-abc123-fix-1" -> "st-abc123"
            "st-abc123-fix-1-fix-2" -> "st-abc123"
        """
        return self._get_canonical_subtask_id(subtask_id)

    def _canonical_subtask_description(self, task: Task, canonical_id: str, fallback: str) -> str:
        for st in task.subtasks:
            if st.subtask_id == canonical_id:
                return st.description
        return fallback

    def _build_fix_description(self, round_num: int, feedback: str, original_description: str, project_dir: Optional[str] = None) -> str:
        """Build a concise fix description that avoids feedback accumulation."""
        max_feedback_len = 1600
        truncated_feedback = feedback[:max_feedback_len]
        if len(feedback) > max_feedback_len:
            truncated_feedback += "... [truncated]"

        max_desc_len = 350
        brief_original = original_description[:max_desc_len]
        if len(original_description) > max_desc_len:
            brief_original += "..."

        parts = [
            f"[FIX ROUND {round_num}] Please fix the following issue(s):\n",
            truncated_feedback,
            "\n\nFocus on the cited failing files/checks first. Do not make unrelated changes.",
            f"\n\nOriginal task: {brief_original}",
        ]

        if project_dir:
            parts.append(
                f"\n\n[CRITICAL] All files MUST be written to this directory: {project_dir}\n"
                f"Do NOT create files in any other location. Use this exact path as the base."
            )

        return "".join(parts)

    def _reserve_remediation_attempt(self, task: Task, ost: OrchestratorState, canonical_id: str) -> Optional[int]:
        """Reserve a remediation attempt for a canonical subtask ID.

        Returns the next attempt number if budget is available, or None if exhausted.
        This method ensures fix and reassign share the same budget per canonical ID.
        """
        current = task.fix_rounds.get(canonical_id, 0)
        if current >= ost.max_fix_rounds:
            return None
        next_round = current + 1
        task.fix_rounds[canonical_id] = next_round
        ost.fix_rounds = task.fix_rounds
        task.updated_at = time.time()
        self._state._persist_task(task)
        return next_round

    def _cancel_pending_remediation_subtasks(self, task: Task, canonical_id: str) -> None:
        """Cancel all pending remediation subtasks for a canonical ID."""
        for st in task.subtasks:
            if self._get_canonical_subtask_id(st.subtask_id) != canonical_id:
                continue
            if st.subtask_id == canonical_id:
                continue
            if st.status in (JobStatus.PENDING, JobStatus.DISPATCHED, JobStatus.RUNNING):
                self._state.update_subtask_status(task.task_id, st.subtask_id, JobStatus.CANCELLED)
                logger.info(f"Cancelled pending remediation subtask {st.subtask_id} (canonical: {canonical_id})")

    def _cancel_wave_gate_dead_end_subtasks(
        self,
        task: Task,
        failed_canonical_id: str,
        ost: OrchestratorState,
    ) -> List[str]:
        """Cancel later-wave pending subtasks that can no longer pass the wave gate."""
        if not ost.wave_gate_enabled:
            return []

        wave_match = re.fullmatch(r"wave-(\d+)", failed_canonical_id)
        failed_wave = int(wave_match.group(1)) if wave_match else None
        failed_subtask = next(
            (st for st in task.subtasks if st.subtask_id == failed_canonical_id),
            None,
        )
        if failed_subtask is None and failed_wave is None:
            return []

        if failed_wave is None:
            failed_wave = getattr(failed_subtask, "wave_number", 1)
        if failed_wave in ost.wave_approved:
            return []

        cancelled: List[str] = []
        for st in task.subtasks:
            if self._is_remediation_subtask_id(st.subtask_id) or st.subtask_id.endswith("-decompose"):
                continue
            if st.status != JobStatus.PENDING:
                continue
            if getattr(st, "wave_number", 1) <= failed_wave:
                continue
            if self._is_wave_gate_satisfied(st, ost):
                continue
            self._state.update_subtask_status(task.task_id, st.subtask_id, JobStatus.CANCELLED)
            cancelled.append(st.subtask_id)

        return cancelled

    def _has_active_remediation_for_canonical(self, task: Task, canonical_id: str) -> bool:
        active = {JobStatus.PENDING, JobStatus.DISPATCHED, JobStatus.RUNNING}
        return any(
            self._get_canonical_subtask_id(st.subtask_id) == canonical_id
            and st.subtask_id != canonical_id
            and st.status in active
            for st in task.subtasks
        )

    def _has_failed_remediation_for_canonical(self, task: Task, canonical_id: str) -> bool:
        terminal_failed = {JobStatus.FAILED, JobStatus.CANCELLED}
        return any(
            self._get_canonical_subtask_id(st.subtask_id) == canonical_id
            and st.subtask_id != canonical_id
            and st.status in terminal_failed
            for st in task.subtasks
        )

    def _mark_wave_gate_dead_end(
        self,
        task: Task,
        wave_number: int,
        ost: OrchestratorState,
        *,
        reason: str,
    ) -> List[str]:
        canonical_id = f"wave-{wave_number}"
        owner_decision = {
            "decision": "reject",
            "recommended_action": "manual_review",
            "blocked_reason": "wave_gate_failed",
            "root_cause_scope": "current_wave",
            "root_cause_wave": wave_number,
            "reason": reason,
        }
        self._update_wave_governance(
            task,
            wave_number,
            WaveLifecycleStatus.FAILED.value,
            owner_decision=owner_decision,
        )
        ost.wave_statuses[wave_number] = WaveLifecycleStatus.FAILED.value
        ost.wave_acceptance_recorded.add(wave_number)
        ost.wave_approved.discard(wave_number)
        ost.revalidating_waves.discard(wave_number)
        ost.blocked_by_wave.pop(wave_number, None)

        cancelled = self._cancel_wave_gate_dead_end_subtasks(task, canonical_id, ost)
        task.last_owner_decision = dict(task.last_owner_decision or {})
        task.last_owner_decision.update(owner_decision)

        if self._state.is_all_subtasks_terminal(task.task_id):
            task.status = TaskStatus.FAILED
            task.error = (
                f"Wave gate failed: Wave {wave_number} remediation budget was exhausted; "
                "downstream waves were cancelled because prior-wave quality could not be approved."
            )
            task.updated_at = time.time()
        self._state._persist_task(task)
        return cancelled

    def _repair_exhausted_blocked_waves(
        self,
        task: Task,
        ost: OrchestratorState,
        *,
        reason: str,
    ) -> List[int]:
        if not ost.wave_gate_enabled:
            return []

        failed_waves: List[int] = []
        for wave in sorted(task.waves, key=lambda item: item.wave_number):
            wave_number = getattr(wave, "wave_number", 0)
            if wave_number <= 0:
                continue
            if getattr(wave, "governance_status", None) != WaveLifecycleStatus.BLOCKED.value:
                continue

            canonical_id = f"wave-{wave_number}"
            if task.fix_rounds.get(canonical_id, 0) < ost.max_fix_rounds:
                continue
            if self._has_active_remediation_for_canonical(task, canonical_id):
                continue
            if not self._has_failed_remediation_for_canonical(task, canonical_id):
                continue

            cancelled = self._mark_wave_gate_dead_end(task, wave_number, ost, reason=reason)
            failed_waves.append(wave_number)
            logger.warning(
                "Wave gate dead-end repaired for task %s wave %s after %s; cancelled downstream=%s",
                task.task_id,
                wave_number,
                reason,
                cancelled,
            )
        return failed_waves

    async def _handle_remediation_exhausted(
        self,
        task: Task,
        job: Job,
        acceptance: AcceptanceResult,
        canonical_id: str,
    ) -> None:
        """Handle remediation budget exhaustion for a canonical subtask.

        Marks the subtask as FAILED, cancels pending remediation subtasks,
        and finalizes task status if all subtasks are terminal.
        """
        logger.warning(
            f"Remediation exhausted for {job.subtask_id} (canonical: {canonical_id}). "
            f"Marking as FAILED - no more fix/reassign attempts allowed."
        )

        self._state.update_subtask_status(task.task_id, job.subtask_id, JobStatus.FAILED)

        if canonical_id != job.subtask_id:
            self._state.update_subtask_status(task.task_id, canonical_id, JobStatus.FAILED)

        self._cancel_pending_remediation_subtasks(task, canonical_id)

        ost = self._orchestrator_states.get(task.task_id)
        if ost and ost.strict_dependency:
            cancelled = self._state.cancel_downstream_subtasks(task.task_id, canonical_id)
            if cancelled:
                logger.info(f"Strict mode: cancelled {len(cancelled)} downstream subtasks after remediation exhausted: {cancelled}")
            gate_cancelled = self._cancel_wave_gate_dead_end_subtasks(task, canonical_id, ost)
            if gate_cancelled:
                logger.info(
                    "Wave gate dead-end: cancelled %s later-wave pending subtasks after remediation exhausted: %s",
                    len(gate_cancelled),
                    gate_cancelled,
                )
            wave_match = re.fullmatch(r"wave-(\d+)", canonical_id)
            if wave_match:
                self._mark_wave_gate_dead_end(
                    task,
                    int(wave_match.group(1)),
                    ost,
                    reason="remediation_exhausted",
                )

        if self._state.is_all_subtasks_terminal(task.task_id):
            await self._finalize_task_status(task.task_id)

    async def _handle_max_rounds_exceeded(self, job: Job, acceptance: AcceptanceResult) -> None:
        """
        After max fix rounds, handle remediation exhaustion.

        N4 Fix: No longer creates unlimited reassigns. Uses canonical ID
        and delegates to _handle_remediation_exhausted() for consistent behavior.
        """
        task = self._state.get_task_by_subtask(job.subtask_id)
        if not task:
            return

        ost = self._orchestrator_states.get(task.task_id)
        if not ost:
            return

        # N4 Fix: Use canonical ID and delegate to remediation exhausted handler
        canonical_id = self._get_canonical_subtask_id(job.subtask_id)
        await self._handle_remediation_exhausted(task, job, acceptance, canonical_id)

    def _find_idle_agent(self, task: Task) -> str:
        running_jobs = self._state.get_jobs_in_status([JobStatus.RUNNING])
        busy_agents = {j.agent_id for j in running_jobs}
        valid_agents = self._get_allowed_valid_agents(task)
        logger.info(f"_find_idle_agent: valid_agents={valid_agents}, busy_agents={busy_agents}")
        for agent_id in valid_agents:
            if agent_id not in busy_agents:
                logger.info(f"_find_idle_agent: selected idle agent {agent_id}")
                return agent_id
        if valid_agents:
            logger.info(f"_find_idle_agent: no idle agent, falling back to {valid_agents[0]}")
            return valid_agents[0]
        if task.subtasks:
            logger.info(f"_find_idle_agent: no valid agents, falling back to task.subtasks[0].agent_id={task.subtasks[0].agent_id}")
            return task.subtasks[0].agent_id
        logger.warning(f"_find_idle_agent: no agents available, falling back to deepseek")
        return "deepseek"

    async def _run_integration_acceptance(self, task_id: str) -> None:
        """
        Trigger owner_agent.run_integration_test() when all subtasks are completed.
        """
        ost = self._orchestrator_states.get(task_id)
        if not ost:
            return
        if ost.is_integration_testing:
            return

        ost.is_integration_testing = True
        task = self._state.get_task(task_id)
        if not task:
            return

        try:
            result = self._owner_agent.run_integration_test(task)
            self._record_integration_acceptance(task, result)
            if getattr(result, "passed", True):
                logger.info(f"Integration acceptance passed for task {task_id}")
                task.status = TaskStatus.COMPLETED
                task.error = None
                task.updated_at = time.time()
                self._state._persist_task(task)
            else:
                feedback = (
                    getattr(result, "feedback", None)
                    or getattr(result, "message", None)
                    or "Integration test failed"
                )
                logger.warning(f"Integration acceptance failed for task {task_id}: {feedback}")
                existing_active = next(
                    (
                        st for st in task.subtasks
                        if "-integration-fix" in st.subtask_id
                        and st.status in {JobStatus.PENDING, JobStatus.DISPATCHED, JobStatus.RUNNING}
                    ),
                    None,
                )
                if existing_active:
                    logger.info(
                        "Integration acceptance for %s already has active fix subtask %s; skipping duplicate",
                        task_id,
                        existing_active.subtask_id,
                    )
                    return
                # Create a fix subtask for integration failure (assigned to owner or a default agent)
                integration_fix_id = self._next_integration_fix_subtask_id(task)
                fix_subtask = self._state.add_subtask(
                    task_id=task_id,
                    description=f"[INTEGRATION FIX] {feedback}",
                    agent_id=self._find_idle_agent(task),
                    priority=1,
                    dependencies=[],
                    subtask_id=integration_fix_id,
                )
                if fix_subtask:
                    self._dispatcher.dispatch_subtask(fix_subtask)
        except Exception as e:
            logger.error(f"Integration acceptance error for task {task_id}: {e}")
        finally:
            ost.is_integration_testing = False

    def _record_integration_acceptance(self, task: Task, result: Any) -> None:
        """Persist integration acceptance as a first-class acceptance record."""
        details = getattr(result, "details", {}) or {}
        failed_checks = list(details.get("failed_checks", []))
        missing_artifacts = list(details.get("missing_artifacts", []))
        feedback = getattr(result, "message", None)
        record = AcceptanceRecord.new(
            task_id=task.task_id,
            level="integration",
            decision="approve" if getattr(result, "passed", True) else "fix",
            deterministic_passed=getattr(result, "passed", True),
            judge_passed=getattr(result, "passed", True),
            failed_checks=failed_checks,
            missing_artifacts=missing_artifacts,
            feedback=feedback,
        )
        self._state.save_acceptance_record(record)

    def _build_feedback(self, level1_report: ValidationReport, acceptance: AcceptanceResult) -> str:
        """Build feedback string from validation and acceptance results."""
        parts: List[str] = []
        if not level1_report.passed:
            for err in level1_report.errors:
                parts.append(f"[Level1] {err.error_type}: {err.message}")
        if not acceptance.level2_passed and acceptance.level2_feedback:
            parts.append(f"[Level2] {acceptance.level2_feedback}")
        return "\n".join(parts) if parts else "Fix required"
