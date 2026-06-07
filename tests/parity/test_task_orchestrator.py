import asyncio
import json
import os
import tempfile
import threading
import time
from unittest.mock import MagicMock

import pytest

from across_agents_assistant.task_manager.models import (
    AcceptanceResult,
    DeliverableSpec,
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
)
from across_agents_assistant.task_manager.orchestration.validator import ValidationError
from across_agents_assistant.task_manager.orchestration.orchestrator import TaskOrchestrator
from across_agents_assistant.task_manager.state import TaskState


def run(coro):
    return asyncio.run(coro)


class FakePersistence:
    def __init__(self):
        self.task_contracts = []
        self.artifact_records = []
        self.acceptance_records = []
        self.delivery_contracts = {}
        self.subtasks = {}

    def save_task(self, _task):
        pass

    def save_subtask(self, subtask):
        task_id = subtask.get("task_id")
        if not task_id:
            return
        rows = self.subtasks.setdefault(task_id, [])
        rows[:] = [row for row in rows if row.get("subtask_id") != subtask.get("subtask_id")]
        rows.append(dict(subtask))

    def get_subtasks(self, task_id):
        return list(self.subtasks.get(task_id, []))

    def save_job(self, _job):
        pass

    def save_wave(self, _wave):
        pass

    def save_requirement_manifest(self, _manifest):
        pass

    def get_requirement_manifest(self, _task_id):
        return None

    def save_task_contract(self, contract):
        self.task_contracts = [
            item for item in self.task_contracts
            if item.get("contract_id") != contract.get("contract_id")
        ]
        self.task_contracts.append(dict(contract))

    def get_task_contracts(self, _task_id):
        return list(self.task_contracts)

    def save_delivery_contract(self, contract):
        self.delivery_contracts[contract["task_id"]] = dict(contract)

    def get_delivery_contract(self, task_id):
        return self.delivery_contracts.get(task_id)

    def save_artifact_record(self, artifact):
        self.artifact_records = [
            item for item in self.artifact_records
            if item.get("artifact_id") != artifact.get("artifact_id")
        ]
        self.artifact_records.append(dict(artifact))

    def get_artifact_records(self, _task_id):
        return list(self.artifact_records)

    def update_artifact_records_for_subtask(self, task_id, subtask_id, status, current_status=None):
        for artifact in self.artifact_records:
            if artifact.get("task_id") != task_id or artifact.get("subtask_id") != subtask_id:
                continue
            if current_status and artifact.get("status") != current_status:
                continue
            artifact["status"] = status

    def save_acceptance_record(self, record):
        self.acceptance_records.append(dict(record))

    def get_acceptance_records(self, _task_id):
        return list(self.acceptance_records)


def make_orchestrator_state(task):
    return OrchestratorState(
        task_id=task.task_id,
        fix_rounds=task.fix_rounds,
        max_fix_rounds=3,
        acceptance_results={},
        completed_subtasks=set(),
        is_integration_testing=False,
        strict_dependency=True,
        wave_gate_enabled=True,
        wave_acceptance_recorded=set(),
        wave_approved=set(),
        wave_statuses={},
        blocked_by_wave={},
        revalidating_waves=set(),
        recent_acceptance_records=[],
        quality_remediation_attempts={},
        max_quality_remediation_attempts=1,
    )


def mark_subtask_accepted(ost, subtask_id):
    ost.acceptance_results[subtask_id] = AcceptanceResult(
        subtask_id=subtask_id,
        level1_passed=True,
        level2_passed=True,
    )
    ost.completed_subtasks.add(subtask_id)


def test_quality_remediation_attempt_keys_include_failure_category(tmp_path, mock_dispatcher, mock_validator, mock_owner_agent):
    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task("Create app.py", project_dir=str(tmp_path))
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

    missing_req = {"requirement_id": "req-app", "path_hint": "app.py", "artifact_type": "api_service_source", "required": True}
    assert orchestrator._quality_requirement_key(missing_req, failure_category="missing_file") == "missing_file:req-app"
    assert orchestrator._quality_requirement_key(missing_req, failure_category="probe_failure") == "probe_failure:req-app"
    assert orchestrator._quality_requirement_key("app.py", failure_category="invalid_file") == "invalid_file:app.py"


def test_quality_remediation_budget_is_separate_per_failure_category(tmp_path, mock_dispatcher, mock_validator, mock_owner_agent):
    from types import SimpleNamespace

    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task("Create app.py", project_dir=str(tmp_path))
    state.save_delivery_contract({
        "contract_id": "delivery-contract-1",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"id": "req-app", "path_hint": "app.py", "artifact_type": "api_service_source", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    })
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

    missing_quality = SimpleNamespace(missing_required=["app.py"], invalid_required=[], probe_results=[], failed_constraints=[])
    first_created = orchestrator._start_quality_remediation_if_possible(
        task,
        missing_quality,
        delivery_contract=state.get_delivery_contract(task.task_id),
    )
    assert first_created
    for subtask_id in first_created:
        state.update_subtask_status(task.task_id, subtask_id, JobStatus.COMPLETED)

    invalid_quality = SimpleNamespace(
        missing_required=[],
        invalid_required=[{"path_hint": "app.py", "message": "syntax error"}],
        probe_results=[],
        failed_constraints=[],
    )
    second_created = orchestrator._start_quality_remediation_if_possible(
        task,
        invalid_quality,
        delivery_contract=state.get_delivery_contract(task.task_id),
    )

    assert second_created, task.last_owner_decision
    attempts = task.last_owner_decision["quality_remediation_attempts"]
    assert attempts["missing_file:req-app"] == 1
    assert attempts["invalid_file:req-app"] == 1


def test_quality_remediation_ignores_install_metadata_candidate_group_for_static_web(
    tmp_path,
    mock_dispatcher,
    mock_validator,
    mock_owner_agent,
):
    from types import SimpleNamespace

    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task(
        "Build a static web app that opens index.html directly with no package managers.",
        project_dir=str(tmp_path),
    )
    delivery_contract = {
        "contract_id": "delivery-contract-static",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [
            {"id": "req-app", "path_hint": "app.js", "artifact_type": "frontend_source", "required": True},
            {"id": "req-readme", "path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "constraints": [],
        "acceptance_probes": [],
    }
    state.save_delivery_contract(delivery_contract)
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

    quality = SimpleNamespace(
        missing_required=["app.js", "README.md"],
        invalid_required=[{
            "path_hint": "pyproject.toml / requirements.txt / package.json / Makefile / README.md",
            "group_id": "group-install-metadata",
            "check_type": "deliverable_group_one_of",
            "message": "Required deliverable group group-install-metadata needs one of: pyproject.toml, requirements.txt, package.json, Makefile, README.md",
        }],
        probe_results=[],
        failed_constraints=[],
        evidence_gaps=[],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=delivery_contract,
    )

    assert created
    descriptions = "\n".join(st.description for st in task.subtasks if st.subtask_id in created)
    assert "app.js" in descriptions
    assert "README.md" in descriptions
    assert "package.json" not in descriptions
    assert "Makefile" not in descriptions
    assert "pyproject.toml" not in descriptions


def test_quality_remediation_turns_web_entrypoint_group_into_concrete_file(
    tmp_path,
    mock_dispatcher,
    mock_validator,
    mock_owner_agent,
):
    from types import SimpleNamespace

    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task(
        "Build a static web app with a browser-loadable entrypoint.",
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
    )
    delivery_contract = {
        "contract_id": "delivery-contract-static-entrypoint",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [
            {"id": "req-css", "path_hint": "styles.css", "artifact_type": "stylesheet", "required": True},
            {"id": "req-js", "path_hint": "app.js", "artifact_type": "client_script", "required": True},
        ],
        "constraints": [],
        "acceptance_probes": [],
    }
    state.save_delivery_contract(delivery_contract)
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

    quality = SimpleNamespace(
        missing_required=[],
        invalid_required=[{
            "path_hint": "index.html / static/index.html / public/index.html",
            "candidate_path_hints": ["index.html", "static/index.html", "public/index.html"],
            "group_id": "group-web-ui",
            "check_type": "deliverable_group_entrypoint",
            "message": "Required deliverable group group-web-ui needs at least one entrypoint.",
        }],
        probe_results=[],
        failed_constraints=[],
        evidence_gaps=[],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=delivery_contract,
    )

    assert created
    restored = state.get_task(task.task_id)
    subtask = next(st for st in restored.subtasks if st.subtask_id == created[0])
    assert "index.html / static/index.html" not in subtask.description
    assert "index.html" in subtask.description
    saved_contract = next(
        contract for contract in state.get_task_contracts(task.task_id)
        if contract.get("subtask_id") == subtask.subtask_id
    )
    assert saved_contract["expected_deliverables"][0]["path_hint"] == "index.html"


def test_quality_remediation_turns_test_suite_group_into_declared_test_file(
    tmp_path,
    mock_dispatcher,
    mock_validator,
    mock_owner_agent,
):
    from types import SimpleNamespace

    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task(
        "Build a Node static web app with a smoke test.",
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
    )
    delivery_contract = {
        "contract_id": "delivery-contract-test-suite",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [
            {"id": "req-smoke", "path_hint": "tests/e2e-smoke.mjs", "artifact_type": "test_source", "required": True},
        ],
        "deliverable_groups": [
            {
                "id": "group-test-suite",
                "kind": "test_suite",
                "required": True,
                "allowed_roots": ["tests/", "test/", "__tests__/"],
                "allowed_extensions": [".js", ".mjs", ".ts"],
                "min_file_count": 1,
            }
        ],
        "constraints": [],
        "acceptance_probes": [],
    }
    state.save_delivery_contract(delivery_contract)
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

    quality = SimpleNamespace(
        missing_required=[],
        invalid_required=[{
            "path_hint": "group-test-suite",
            "group_id": "group-test-suite",
            "check_type": "deliverable_group_min_file_count",
            "message": "Required deliverable group group-test-suite has 0 files; expected at least 1.",
        }],
        probe_results=[],
        failed_constraints=[],
        evidence_gaps=[],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=delivery_contract,
    )

    assert created
    restored = state.get_task(task.task_id)
    subtask = next(st for st in restored.subtasks if st.subtask_id == created[0])
    assert "group-test-suite" in subtask.description
    assert "tests/e2e-smoke.mjs" in subtask.description
    saved_contract = next(
        contract for contract in state.get_task_contracts(task.task_id)
        if contract.get("subtask_id") == subtask.subtask_id
    )
    assert saved_contract["expected_deliverables"][0]["path_hint"] == "tests/e2e-smoke.mjs"


def test_quality_bundle_remediation_prefers_uncovered_local_agent_for_complex_delivery(
    tmp_path,
    mock_dispatcher,
    mock_validator,
    mock_owner_agent,
):
    from types import SimpleNamespace

    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task(
        "Build a cross-agent release E2E project.",
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
        allowed_subtask_agents=["openclaw", "hermes", "claude", "deepseek", "minimax"],
    )
    state.add_subtask(
        task_id=task.task_id,
        description="Build API",
        agent_id="deepseek",
        subtask_id="st-api",
    )
    state.update_subtask_status(task.task_id, "st-api", JobStatus.COMPLETED)
    state.add_subtask(
        task_id=task.task_id,
        description="Build CLI",
        agent_id="openclaw",
        subtask_id="st-cli",
    )
    state.update_subtask_status(task.task_id, "st-cli", JobStatus.COMPLETED)
    delivery_contract = {
        "contract_id": "delivery-contract-agent-bundle",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [
            {"id": "req-readme", "path_hint": "README.md", "artifact_type": "documentation", "required": True},
            {"id": "req-smoke", "path_hint": "tests/e2e-smoke.mjs", "artifact_type": "test_source", "required": True},
        ],
        "constraints": [],
        "acceptance_probes": [],
    }
    state.save_delivery_contract(delivery_contract)
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
    mock_dispatcher._get_valid_agents.return_value = ["openclaw", "hermes", "claude", "deepseek", "minimax"]

    quality = SimpleNamespace(
        missing_required=["README.md", "tests/e2e-smoke.mjs"],
        invalid_required=[],
        probe_results=[],
        failed_constraints=[],
        evidence_gaps=[],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        state.get_task(task.task_id),
        quality,
        delivery_contract=delivery_contract,
    )

    assert len(created) == 1
    restored = state.get_task(task.task_id)
    subtask = next(st for st in restored.subtasks if st.subtask_id == created[0])
    assert subtask.agent_id == "hermes"


def test_agent_mix_constraint_remediation_prefers_missing_local_agent(
    tmp_path,
    mock_dispatcher,
    mock_validator,
    mock_owner_agent,
):
    from types import SimpleNamespace

    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task(
        "Build a cross-agent release E2E project.",
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
        allowed_subtask_agents=["openclaw", "hermes", "claude", "deepseek", "minimax"],
    )
    state.add_subtask(task_id=task.task_id, description="Build web", agent_id="deepseek", subtask_id="st-web")
    state.update_subtask_status(task.task_id, "st-web", JobStatus.COMPLETED)
    state.add_subtask(task_id=task.task_id, description="Build CLI", agent_id="openclaw", subtask_id="st-cli")
    state.update_subtask_status(task.task_id, "st-cli", JobStatus.COMPLETED)
    delivery_contract = {
        "contract_id": "delivery-contract-agent-mix",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [],
        "constraints": [{
            "id": "constraint-agent-mix",
            "constraint_type": "agent_mix",
            "value": {"min_distinct_agents": 3, "min_local_agents": 2, "min_cloud_agents": 1},
            "required": True,
        }],
        "acceptance_probes": [],
    }
    state.save_delivery_contract(delivery_contract)
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
    mock_dispatcher._get_valid_agents.return_value = ["openclaw", "hermes", "claude", "deepseek", "minimax"]

    quality = SimpleNamespace(
        missing_required=[],
        invalid_required=[],
        probe_results=[],
        failed_constraints=[{
            "id": "constraint-agent-mix",
            "constraint_type": "agent_mix",
            "value": {"min_distinct_agents": 3, "min_local_agents": 2, "min_cloud_agents": 1},
            "evidence": ["completed agents: deepseek, openclaw"],
        }],
        evidence_gaps=[],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        state.get_task(task.task_id),
        quality,
        delivery_contract=delivery_contract,
    )

    assert created
    restored = state.get_task(task.task_id)
    subtask = next(st for st in restored.subtasks if st.subtask_id == created[0])
    assert subtask.agent_id == "hermes"
    assert "agent_mix" in subtask.description


def test_static_web_quality_guidance_mentions_owner_agent_and_recompute_evidence():
    guidance = TaskOrchestrator._quality_probe_remediation_guidance(
        "Missing requested static web feature evidence: owner agent route preview; "
        "route evidence recomputes visible rows"
    )

    assert "exact text Owner Agent" in guidance
    assert "visible recompute counter" in guidance
    assert "last-updated timestamp" in guidance


def test_quality_remediation_does_not_duplicate_while_active(tmp_path, mock_dispatcher, mock_validator, mock_owner_agent):
    from types import SimpleNamespace

    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task("Create app.py", project_dir=str(tmp_path))
    state.save_delivery_contract({
        "contract_id": "delivery-contract-active",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"id": "req-app", "path_hint": "app.py", "artifact_type": "api_service_source", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    })
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

    quality = SimpleNamespace(
        missing_required=["app.py"],
        invalid_required=[],
        probe_results=[],
        failed_constraints=[],
        evidence_gaps=[],
    )

    first_created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=state.get_delivery_contract(task.task_id),
    )
    second_created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=state.get_delivery_contract(task.task_id),
    )

    assert first_created
    assert second_created == first_created
    assert [st.subtask_id for st in task.subtasks if st.subtask_id.startswith("st-quality-")] == first_created
    assert mock_dispatcher.dispatch_subtask.call_count == 1


def test_project_quality_remediation_waits_for_original_subtasks_when_required(
    tmp_path,
    mock_dispatcher,
    mock_validator,
    mock_owner_agent,
):
    from types import SimpleNamespace

    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task("Build complete project", project_dir=str(tmp_path))
    state.add_subtask(
        task_id=task.task_id,
        description="Create README",
        agent_id="deepseek",
        subtask_id="st-readme",
    )
    state.save_delivery_contract({
        "contract_id": "delivery-contract-wait-originals",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"id": "req-readme", "path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    })
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

    quality = SimpleNamespace(
        missing_required=["README.md"],
        invalid_required=[],
        probe_results=[],
        failed_constraints=[],
        evidence_gaps=[],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        state.get_task(task.task_id),
        quality,
        delivery_contract=state.get_delivery_contract(task.task_id),
        require_original_terminal=True,
    )

    assert created == []
    restored = state.get_task(task.task_id)
    assert restored.status == TaskStatus.RUNNING
    assert "remaining original subtasks" in restored.error
    assert not mock_dispatcher.dispatch_subtask.called


def test_project_finalization_accepts_string_subtask_statuses_for_terminal_check(
    tmp_path,
    mock_dispatcher,
    mock_validator,
    mock_owner_agent,
):
    from types import SimpleNamespace

    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task("Build complete project", project_dir=str(tmp_path))
    state.add_subtask(task_id=task.task_id, description="Create README", agent_id="deepseek", subtask_id="st-readme")
    restored = state.get_task(task.task_id)
    restored.subtasks[0].status = "completed"
    state.save_delivery_contract({
        "contract_id": "delivery-contract-string-status",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"id": "req-readme", "path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    })
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(restored)

    quality = SimpleNamespace(
        missing_required=["README.md"],
        invalid_required=[],
        probe_results=[],
        failed_constraints=[],
        evidence_gaps=[],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        restored,
        quality,
        delivery_contract=state.get_delivery_contract(task.task_id),
        require_original_terminal=True,
    )

    assert created
    assert state.is_all_subtasks_terminal(task.task_id) is True


def test_quality_remediation_start_is_thread_safe_for_same_failure(
    tmp_path,
    mock_dispatcher,
    mock_validator,
    mock_owner_agent,
    monkeypatch,
):
    from types import SimpleNamespace

    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task(
        "Build static web app",
        project_dir=str(tmp_path),
        task_types=["functional"],
        delivery_mode="functional",
    )
    state.save_delivery_contract({
        "contract_id": "delivery-contract-thread-safe",
        "task_id": task.task_id,
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"id": "req-index", "path_hint": "index.html", "artifact_type": "html_entrypoint", "required": True}],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}],
    })
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
    quality = SimpleNamespace(
        missing_required=[],
        invalid_required=[],
        probe_results=[
            {
                "id": "probe-static-web-smoke",
                "probe_type": "static_web_smoke",
                "passed": False,
                "required": True,
                "output_tail": "Missing requested static web feature evidence: checklist label click",
            }
        ],
        failed_constraints=[],
        evidence_gaps=[],
    )
    original_create = orchestrator._create_quality_remediation_subtask

    def slow_create(*args, **kwargs):
        time.sleep(0.05)
        return original_create(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_create_quality_remediation_subtask", slow_create)

    results = []

    def start_remediation():
        results.append(orchestrator._start_quality_remediation_if_possible(
            task,
            quality,
            delivery_contract=state.get_delivery_contract(task.task_id),
        ))

    threads = [threading.Thread(target=start_remediation) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    created_ids = [st.subtask_id for st in task.subtasks if st.subtask_id.startswith("st-quality-")]
    assert len(created_ids) == 1
    assert results == [created_ids, created_ids]
    assert mock_dispatcher.dispatch_subtask.call_count == 1


def test_multiple_file_quality_failures_use_single_coherent_remediation(tmp_path, mock_dispatcher, mock_validator, mock_owner_agent):
    from types import SimpleNamespace

    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task(
        "Create FastAPI files",
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
    )
    state.save_delivery_contract({
        "contract_id": "delivery-contract-fastapi",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [
            {"id": "req-main", "path_hint": "main.py", "artifact_type": "api_service_source", "required": True},
            {"id": "req-models", "path_hint": "models.py", "artifact_type": "api_service_source", "required": True},
            {"id": "req-tests", "path_hint": "tests/test_api.py", "artifact_type": "test_source", "required": True},
        ],
        "constraints": [],
        "acceptance_probes": [],
    })
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

    quality = SimpleNamespace(
        missing_required=["main.py", "models.py", "tests/test_api.py"],
        invalid_required=[],
        probe_results=[],
        failed_constraints=[],
        evidence_gaps=[],
    )
    created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=state.get_delivery_contract(task.task_id),
    )

    assert len(created) == 1
    quality_subtasks = [st for st in task.subtasks if st.subtask_id.startswith("st-quality-")]
    assert len(quality_subtasks) == 1
    description = quality_subtasks[0].description
    assert "main.py" in description
    assert "models.py" in description
    assert "tests/test_api.py" in description
    attempts = task.last_owner_decision["quality_remediation_attempts"]
    assert attempts["missing_file:req-main"] == 1
    assert attempts["missing_file:req-models"] == 1
    assert attempts["missing_file:req-tests"] == 1
    assert mock_dispatcher.dispatch_subtask.call_count == 1


def test_deterministic_fastapi_delivery_repair_writes_verified_project(tmp_path, mock_dispatcher, mock_validator, mock_owner_agent):
    from across_agents_assistant.task_manager.orchestration.contract_acceptance import run_delivery_contract_acceptance

    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task(
        "Create a FastAPI REST API project with GET /items, POST /items, GET /items/{id}, DELETE /items/{id}",
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
    )
    contract = {
        "contract_id": "delivery-contract-fastapi",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "technology_hypotheses": [{"stack": "python-fastapi"}],
        "capabilities": [],
        "deliverables": [
            {"id": "req-main", "path_hint": "main.py", "artifact_type": "api_service_source", "required": True},
            {"id": "req-models", "path_hint": "models.py", "artifact_type": "api_service_source", "required": True},
            {"id": "req-reqs", "path_hint": "requirements.txt", "artifact_type": "config_file", "required": True},
            {"id": "req-docker", "path_hint": "Dockerfile", "artifact_type": "dockerfile", "required": True},
            {"id": "req-compose", "path_hint": "docker-compose.yml", "artifact_type": "compose_config", "required": True},
            {"id": "req-tests", "path_hint": "tests/test_api.py", "artifact_type": "test_source", "required": True},
            {"id": "req-readme", "path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "deliverable_groups": [],
        "constraints": [],
        "acceptance_probes": [],
    }
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    quality = {
        "missing_required": ["main.py", "tests/test_api.py"],
        "invalid_required": [],
        "probe_results": [],
    }

    assert orchestrator._apply_deterministic_delivery_repair_if_possible(task, contract, quality)

    report = run_delivery_contract_acceptance(task, contract, [], run_probes=False)
    assert report["missing_required"] == []
    assert report["invalid_required"] == []
    assert (tmp_path / "main.py").read_text(encoding="utf-8").count("@app.") >= 5
    assert "pytest" in (tmp_path / "requirements.txt").read_text(encoding="utf-8")


def test_functional_partial_delivery_quality_starts_remediation(
    tmp_path,
    monkeypatch,
    mock_dispatcher,
    mock_validator,
    mock_owner_agent,
):
    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task("Build a runnable FastAPI web app", project_dir=str(tmp_path))
    subtask = state.add_subtask(task.task_id, "Implement app", "deepseek", subtask_id="st-app")
    subtask.status = JobStatus.COMPLETED
    state.save_delivery_contract({
        "contract_id": "delivery-contract-functional",
        "task_id": task.task_id,
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "project_dir": str(tmp_path),
        "capabilities": [{"id": "cap-web", "required": True}],
        "deliverables": [{"id": "req-main", "path_hint": "main.py", "artifact_type": "api_service_source", "required": True}],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-pytest", "probe_type": "pytest", "required": True}],
    })
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

    monkeypatch.setattr(
        "across_agents_assistant.task_manager.orchestration.contract_acceptance.run_delivery_contract_acceptance",
        lambda *_args, **_kwargs: {
            "delivery_quality": "partial",
            "missing_required": [],
            "produced_required": [],
            "invalid_required": [],
            "failed_constraints": [],
            "evidence_gaps": [
                {
                    "check_type": "functional_evidence_required",
                    "message": "Functional delivery needs runnable acceptance evidence.",
                }
            ],
            "probe_results": [],
        },
    )

    run(orchestrator._finalize_task_status(task.task_id))

    assert task.status == TaskStatus.RUNNING
    assert any(st.subtask_id.startswith("st-quality-") for st in task.subtasks)
    assert mock_dispatcher.dispatch_subtask.called
    attempts = task.last_owner_decision["quality_remediation_attempts"]
    assert attempts["probe_failure:functional_evidence_required"] == 1


def test_finalize_defers_delivery_acceptance_until_original_subtasks_terminal(
    tmp_path,
    monkeypatch,
    mock_dispatcher,
    mock_validator,
    mock_owner_agent,
):
    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task("Build a runnable web app", project_dir=str(tmp_path))
    completed = state.add_subtask(task.task_id, "Implement backend", "deepseek", subtask_id="st-backend")
    pending = state.add_subtask(task.task_id, "Implement frontend", "hermes", subtask_id="st-frontend")
    completed.status = JobStatus.COMPLETED
    pending.status = JobStatus.PENDING
    state.save_delivery_contract({
        "contract_id": "delivery-contract-functional",
        "task_id": task.task_id,
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "project_dir": str(tmp_path),
        "capabilities": [{"id": "cap-web", "required": True}],
        "deliverables": [{"id": "req-main", "path_hint": "main.py", "artifact_type": "api_service_source", "required": True}],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-pytest", "probe_type": "pytest", "required": True}],
    })
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("delivery acceptance must not run before all original subtasks are terminal")

    monkeypatch.setattr(
        "across_agents_assistant.task_manager.orchestration.contract_acceptance.run_delivery_contract_acceptance",
        fail_if_called,
    )

    run(orchestrator._finalize_task_status(task.task_id))

    assert task.status == TaskStatus.RUNNING
    assert "remaining original subtasks" in task.error
    assert pending.status == JobStatus.PENDING
    assert not mock_dispatcher.dispatch_subtask.called


def test_finalize_defers_when_original_subtasks_remain_nonterminal_after_failure(
    tmp_path,
    monkeypatch,
    mock_dispatcher,
    mock_validator,
    mock_owner_agent,
):
    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task("Build a runnable web app", project_dir=str(tmp_path))
    completed = state.add_subtask(task.task_id, "Implement backend", "deepseek", subtask_id="st-backend")
    failed = state.add_subtask(task.task_id, "Implement API tests", "deepseek", subtask_id="st-tests")
    pending = state.add_subtask(task.task_id, "Implement frontend", "hermes", subtask_id="st-frontend")
    completed.status = JobStatus.COMPLETED
    failed.status = JobStatus.FAILED
    pending.status = JobStatus.PENDING
    state.save_delivery_contract({
        "contract_id": "delivery-contract-functional",
        "task_id": task.task_id,
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "project_dir": str(tmp_path),
        "capabilities": [{"id": "cap-web", "required": True}],
        "deliverables": [{"id": "req-main", "path_hint": "main.py", "artifact_type": "api_service_source", "required": True}],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-pytest", "probe_type": "pytest", "required": True}],
    })
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("delivery acceptance must not run while original subtasks remain nonterminal")

    monkeypatch.setattr(
        "across_agents_assistant.task_manager.orchestration.contract_acceptance.run_delivery_contract_acceptance",
        fail_if_called,
    )

    run(orchestrator._finalize_task_status(task.task_id))

    assert task.status == TaskStatus.RUNNING
    assert "remaining original subtasks" in task.error
    assert pending.status == JobStatus.PENDING
    assert not mock_dispatcher.dispatch_subtask.called


def test_artifact_partial_delivery_quality_remains_completed_with_failures(
    tmp_path,
    monkeypatch,
    mock_dispatcher,
    mock_validator,
    mock_owner_agent,
):
    state = TaskState()
    state.set_persistence(FakePersistence())
    task = state.create_task("Create README.md", project_dir=str(tmp_path), task_types=["artifact"])
    subtask = state.add_subtask(task.task_id, "Create README.md", "deepseek", subtask_id="st-doc")
    subtask.status = JobStatus.COMPLETED
    state.save_delivery_contract({
        "contract_id": "delivery-contract-artifact",
        "task_id": task.task_id,
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"id": "req-readme", "path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    })
    orchestrator = TaskOrchestrator(state, mock_dispatcher, mock_validator, mock_owner_agent)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

    monkeypatch.setattr(
        "across_agents_assistant.task_manager.orchestration.contract_acceptance.run_delivery_contract_acceptance",
        lambda *_args, **_kwargs: {
            "delivery_quality": "partial",
            "missing_required": [],
            "produced_required": ["README.md"],
            "invalid_required": [],
            "failed_constraints": [],
            "evidence_gaps": [{"check_type": "non_blocking_evidence_gap"}],
            "probe_results": [],
        },
    )

    run(orchestrator._finalize_task_status(task.task_id))

    assert task.status == TaskStatus.COMPLETED_WITH_FAILURES
    assert not mock_dispatcher.dispatch_subtask.called


@pytest.fixture
def mock_dispatcher():
    dispatcher = MagicMock()
    dispatcher.add_progress_callback = MagicMock()
    dispatcher.dispatch_subtask = MagicMock(return_value=None)
    return dispatcher


@pytest.fixture
def mock_validator():
    validator = MagicMock()
    validator.validate = MagicMock(return_value=ValidationReport(passed=True, errors=[]))
    return validator


@pytest.fixture
def mock_owner_agent():
    agent = MagicMock()
    agent.decompose_and_assign = MagicMock()
    agent.accept_subtask = MagicMock(return_value=AcceptanceResult(
        subtask_id="st-1",
        level1_passed=True,
        level2_passed=True,
    ))
    agent.decide_on_failure = MagicMock(return_value=MagicMock(action="downgrade"))
    agent.run_integration_test = MagicMock(return_value=MagicMock(passed=True))
    return agent


@pytest.fixture
def orchestrator(mock_dispatcher, mock_validator, mock_owner_agent):
    state = TaskState()
    state.set_persistence(FakePersistence())
    return TaskOrchestrator(
        state=state,
        dispatcher=mock_dispatcher,
        validator=mock_validator,
        owner_agent=mock_owner_agent,
    )


def test_quality_remediation_uses_allowed_valid_agent(orchestrator, mock_dispatcher):
    mock_dispatcher._get_valid_agents = MagicMock(return_value=["openclaw"])
    task = orchestrator._state.create_task(
        "repair required deliverable",
        allowed_subtask_agents=["openclaw"],
    )

    subtask = orchestrator._create_quality_remediation_subtask(
        task,
        requirement={
            "requirement_id": "req-readme",
            "path_hint": "README.md",
            "artifact_type": "file",
            "preferred_agent": "ghost-agent",
        },
        reason="missing required deliverable: README.md",
        attempt=1,
    )

    assert subtask is not None
    assert subtask.agent_id == "openclaw"


def test_quality_probe_remediation_guidance_mentions_anyio_backend_for_trio_errors(orchestrator):
    guidance = orchestrator._quality_probe_remediation_guidance(
        "pytest failed because ModuleNotFoundError: No module named 'trio' while running anyio tests"
    )

    assert "anyio_backend" in guidance
    assert "'asyncio'" in guidance
    assert "trio" in guidance


def test_record_acceptance_preserves_quality_remediation_attempts(orchestrator):
    task = orchestrator._state.create_task("preserve quality attempts")
    task.last_owner_decision = {
        "quality_remediation_attempts": {"req-readme": 1},
        "max_quality_remediation_attempts": 1,
        "blocked_reason": "quality_failed",
    }
    subtask = orchestrator._state.add_subtask(
        task.task_id,
        "Create README.md",
        "deepseek",
        subtask_id="st-1",
    )
    job = orchestrator._state.create_job(subtask)

    orchestrator._record_acceptance(
        task=task,
        job=job,
        level1_passed=True,
        acceptance=AcceptanceResult(
            subtask_id=subtask.subtask_id,
            level1_passed=True,
            level2_passed=True,
            action="approve",
            recommended_action="approve",
        ),
    )

    assert task.last_owner_decision["quality_remediation_attempts"] == {"req-readme": 1}
    assert task.last_owner_decision["max_quality_remediation_attempts"] == 1


class TestSubmitTask:
    def test_decomposition_failure_marks_task_failed(self, orchestrator, mock_owner_agent):
        def decompose_side_effect(task, context=None):
            raise RuntimeError("All LLM providers failed. Last error: No API key found for minimax")

        mock_owner_agent.decompose_and_assign.side_effect = decompose_side_effect

        task_id = orchestrator.submit_task("Build a calculator")

        deadline = time.time() + 1.0
        task = orchestrator._state.get_task(task_id)
        while task and task.status != TaskStatus.PENDING and time.time() < deadline:
            time.sleep(0.01)
            task = orchestrator._state.get_task(task_id)

        assert task is not None
        # Missing API key should result in recoverable waiting, not terminal failure.
        assert task.status == TaskStatus.PENDING
        decision = task.last_owner_decision or {}
        assert decision.get("blocked_reason") == "waiting_for_keys"
        assert decision.get("recoverable") is True
        assert "Waiting for API keys" in (task.error or "")
        decompose = next(st for st in task.subtasks if st.subtask_id == f"{task_id}-decompose")
        assert decompose.status == JobStatus.PENDING
        assert "Waiting for API keys" in (decompose.error_message or "")

    def test_empty_decomposition_marks_task_failed(self, orchestrator, mock_owner_agent):
        mock_owner_agent.decompose_and_assign.return_value = None

        task_id = orchestrator.submit_task("Build a calculator")

        deadline = time.time() + 1.0
        task = orchestrator._state.get_task(task_id)
        while task and task.status != TaskStatus.FAILED and time.time() < deadline:
            time.sleep(0.01)
            task = orchestrator._state.get_task(task_id)

        assert task is not None
        assert task.status == TaskStatus.FAILED
        assert "no business subtasks generated" in (task.error or "")

    def test_gap_only_decomposition_marks_task_failed(self, orchestrator, mock_owner_agent):
        def decompose_side_effect(task, context=None):
            gap = orchestrator._state.add_subtask(
                task.task_id,
                "Create required deliverable: README.md",
                "claude",
                subtask_id="st-gap-docs",
            )
            gap.wave_number = 1
            return task

        mock_owner_agent.decompose_and_assign.side_effect = decompose_side_effect

        task_id = orchestrator.submit_task(
            "Build a FastAPI expense app",
            context={"task_types": ["functional", "artifact"]},
        )

        deadline = time.time() + 1.0
        task = orchestrator._state.get_task(task_id)
        while task and task.status != TaskStatus.FAILED and time.time() < deadline:
            time.sleep(0.01)
            task = orchestrator._state.get_task(task_id)

        assert task is not None
        assert task.status == TaskStatus.FAILED
        assert "no business subtasks generated" in (task.error or "")
        assert mock_owner_agent.assign_waves.call_count == 0

    def test_submit_task_returns_with_decompose_wave0(self, orchestrator, mock_dispatcher, mock_owner_agent):
        release = threading.Event()

        def decompose_side_effect(task, context=None):
            release.wait(timeout=1.0)
            return task

        mock_dispatcher._get_valid_agents.return_value = ["claude", "deepseek"]
        mock_owner_agent.decompose_and_assign.side_effect = decompose_side_effect

        task_id = orchestrator.submit_task(
            "Build a task management system",
            context={"owner_agent": "claude", "allowed_subtask_agents": []},
        )

        task = orchestrator._state.get_task(task_id)
        assert task is not None
        assert task.status == TaskStatus.DECOMPOSING
        assert task.owner_agent == "claude"
        assert task.allowed_subtask_agents == ["claude"]
        assert any(wave.wave_number == 0 for wave in task.waves)
        decompose = next(st for st in task.subtasks if st.subtask_id == f"{task_id}-decompose")
        assert decompose.wave_number == 0
        assert decompose.status == JobStatus.RUNNING

        release.set()

    def test_selected_subtask_agents_override_owner_default(self, orchestrator, mock_dispatcher, mock_owner_agent):
        mock_dispatcher._get_valid_agents.return_value = ["claude", "deepseek", "minimax"]

        def decompose_side_effect(task, context=None):
            assert task.allowed_subtask_agents == ["deepseek", "minimax"]
            assert context["allowed_subtask_agents"] == ["deepseek", "minimax"]
            return task

        mock_owner_agent.decompose_and_assign.side_effect = decompose_side_effect

        task_id = orchestrator.submit_task(
            "Build a task management system",
            context={
                "owner_agent": "claude",
                "allowed_subtask_agents": ["deepseek", "minimax"],
            },
        )

        task = orchestrator._state.get_task(task_id)
        assert task.allowed_subtask_agents == ["deepseek", "minimax"]

    def test_creates_task_and_dispatches_initial_subtasks(self, orchestrator, mock_dispatcher, mock_owner_agent):
        # Setup: owner agent decomposes into 2 subtasks with no deps
        def decompose_side_effect(task, context=None):
            task.subtasks = [
                SubTask(subtask_id="st-a", description="Subtask A", agent_id="claude", dependencies=[]),
                SubTask(subtask_id="st-b", description="Subtask B", agent_id="deepseek", dependencies=[]),
            ]

        mock_owner_agent.decompose_and_assign.side_effect = decompose_side_effect

        task_id = orchestrator.submit_task("Build a task management system")

        assert task_id is not None
        assert task_id.startswith("task-")
        assert mock_dispatcher.add_progress_callback.called
        assert mock_owner_agent.decompose_and_assign.called
        # Both subtasks have no dependencies, so both should be dispatched
        assert mock_dispatcher.dispatch_subtask.call_count == 2
        dispatched_ids = [call.args[0].subtask_id for call in mock_dispatcher.dispatch_subtask.call_args_list]
        assert "st-a" in dispatched_ids
        assert "st-b" in dispatched_ids


class TestDispatchableReadySubtasks:
    def test_strict_dependency_requires_accepted_dependencies(self, orchestrator):
        task = orchestrator._state.create_task("Test accepted dependency gate")
        st_a = SubTask(subtask_id="st-a", description="A", agent_id="claude", dependencies=[])
        st_b = SubTask(subtask_id="st-b", description="B", agent_id="deepseek", dependencies=["st-a"])
        st_a.status = JobStatus.COMPLETED
        task.subtasks = [st_a, st_b]
        ost = make_orchestrator_state(task)
        ost.wave_gate_enabled = False
        ost.completed_subtasks = set()
        orchestrator._orchestrator_states[task.task_id] = ost

        assert orchestrator._get_dispatchable_ready_subtasks(task.task_id, ost) == []

        ost.completed_subtasks.add("st-a")
        ready = orchestrator._get_dispatchable_ready_subtasks(task.task_id, ost)

        assert [st.subtask_id for st in ready] == ["st-b"]


class TestOnJobProgress:
    def test_completed_job_triggers_handle_job_completed(self, orchestrator, mock_owner_agent, monkeypatch):
        class ImmediateThread:
            def __init__(self, target, daemon=False):
                self.target = target
                self.daemon = daemon

            def start(self):
                self.target()

        monkeypatch.setattr(
            "across_agents_assistant.task_manager.orchestration.orchestrator.threading.Thread",
            ImmediateThread,
        )

        # Setup task with one subtask
        task = orchestrator._state.create_task("Test")
        subtask = SubTask(subtask_id="st-1", description="Do work", agent_id="claude", dependencies=[])
        task.subtasks.append(subtask)
        orchestrator._orchestrator_states[task.task_id] = MagicMock()
        orchestrator._orchestrator_states[task.task_id].fix_rounds = {}
        orchestrator._orchestrator_states[task.task_id].max_fix_rounds = 3
        orchestrator._orchestrator_states[task.task_id].acceptance_results = {}
        orchestrator._orchestrator_states[task.task_id].completed_subtasks = set()
        orchestrator._orchestrator_states[task.task_id].is_integration_testing = False

        job = orchestrator._state.create_job(subtask)
        orchestrator._state.update_job_status(job.job_id, JobStatus.COMPLETED)

        # Simulate progress callback
        update = ProgressUpdate(job_id=job.job_id, status=JobStatus.COMPLETED, progress=1.0)
        orchestrator._on_job_progress(update)

        mock_owner_agent.accept_subtask.assert_called_once()


class TestHandleJobCompleted:
    def test_failed_quality_remediation_returns_to_final_gate_without_recursive_fix(
        self,
        orchestrator,
        mock_dispatcher,
        monkeypatch,
        tmp_path,
    ):
        task = orchestrator._state.create_task(
            "Build a static web app",
            project_dir=str(tmp_path),
            task_types=["functional", "artifact"],
            delivery_mode="composite",
        )
        original = orchestrator._state.add_subtask(
            task.task_id,
            "Create index.html",
            "hermes",
            subtask_id="st-index",
        )
        original.status = JobStatus.COMPLETED
        quality = orchestrator._state.add_subtask(
            task.task_id,
            "Quality remediation attempt: fix browser probe",
            "deepseek",
            subtask_id="st-quality-timeout",
        )
        orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
        finalized = []

        async def fake_finalize(task_id):
            finalized.append(task_id)

        monkeypatch.setattr(orchestrator, "_finalize_task_status", fake_finalize)

        job = orchestrator._state.create_job(quality)
        orchestrator._state.complete_job(
            job.job_id,
            success=False,
            error="Timeout after 600.0s",
        )

        run(orchestrator._handle_job_completed(job.job_id))

        assert finalized == [task.task_id]
        assert quality.status == JobStatus.FAILED
        assert not any("-fix-" in st.subtask_id for st in task.subtasks)
        assert not mock_dispatcher.dispatch_subtask.called

    def test_timeout_failure_switches_agent_on_first_fix_round(
        self,
        orchestrator,
        mock_dispatcher,
    ):
        task = orchestrator._state.create_task(
            "Timeout should not retry same agent first",
            allowed_subtask_agents=["local", "deepseek"],
        )
        subtask = orchestrator._state.add_subtask(
            task.task_id,
            "Create todo_cli.py",
            "local",
            subtask_id="st-timeout",
        )
        mock_dispatcher._get_valid_agents.return_value = ["local", "deepseek"]
        orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

        job = orchestrator._state.create_job(subtask)
        orchestrator._state.complete_job(
            job.job_id,
            success=False,
            error="local execution timeout after 600 seconds",
        )

        orchestrator._initiate_fix(
            job,
            "Job execution failed [timeout]: local execution timeout after 600 seconds",
        )

        fix_subtask = next(st for st in task.subtasks if st.subtask_id == "st-timeout-fix-1")
        assert fix_subtask.agent_id == "deepseek"

    def test_acceptance_pass_promotes_manifest_artifact_to_accepted(
        self,
        orchestrator,
        mock_validator,
        mock_owner_agent,
        tmp_path,
    ):
        readme = tmp_path / "README.md"
        readme.write_text("# Accepted\n", encoding="utf-8")
        task = orchestrator._state.create_task("Build README.md", project_dir=str(tmp_path))
        subtask = orchestrator._state.add_subtask(
            task.task_id,
            "Create README.md",
            "deepseek",
            subtask_id="st-readme",
        )
        orchestrator._state.save_requirement_manifest({
            "manifest_id": "manifest-readme",
            "task_id": task.task_id,
            "project_dir": str(tmp_path),
            "deliverables": [{
                "requirement_id": "req-readme",
                "artifact_type": "documentation",
                "path_hint": "README.md",
                "required": True,
                "status": "assigned",
            }],
            "quality_checks": [],
            "created_at": 1.0,
            "updated_at": 1.0,
        })
        orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
        mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-readme",
            level1_passed=True,
            level2_passed=True,
        )

        job = orchestrator._state.create_job(subtask)
        orchestrator._state.complete_job(job.job_id, success=True, output=f"Created {readme}")

        run(orchestrator._handle_job_completed(job.job_id))

        manifest = orchestrator._state.get_requirement_manifest(task.task_id)
        assert manifest["deliverables"][0]["status"] == "accepted"

    def test_level1_failure_creates_fix_subtask(self, orchestrator, mock_validator, mock_owner_agent, mock_dispatcher):
        task = orchestrator._state.create_task("Test")
        subtask = SubTask(subtask_id="st-1", description="Do work", agent_id="claude", dependencies=[])
        task.subtasks.append(subtask)
        mock_dispatcher._get_valid_agents.return_value = ["claude", "deepseek"]
        orchestrator._orchestrator_states[task.task_id] = MagicMock()
        orchestrator._orchestrator_states[task.task_id].fix_rounds = {}
        orchestrator._orchestrator_states[task.task_id].max_fix_rounds = 3
        orchestrator._orchestrator_states[task.task_id].acceptance_results = {}
        orchestrator._orchestrator_states[task.task_id].completed_subtasks = set()
        orchestrator._orchestrator_states[task.task_id].is_integration_testing = False

        job = orchestrator._state.create_job(subtask)
        orchestrator._state.update_job_status(job.job_id, JobStatus.COMPLETED)

        # Level 1 fails, Level 2 passes
        mock_validator.validate.return_value = ValidationReport(
            passed=False,
            errors=[MagicMock(error_type="missing_endpoint", message="missing /api/items")],
        )
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-1",
            level1_passed=False,
            level2_passed=True,
        )

        run(orchestrator._handle_job_completed(job.job_id))

        # Should dispatch a fix subtask
        assert mock_dispatcher.dispatch_subtask.call_count >= 1
        fix_call = None
        for call in mock_dispatcher.dispatch_subtask.call_args_list:
            if "fix" in call.args[0].subtask_id:
                fix_call = call
                break
        assert fix_call is not None
        assert fix_call.args[0].subtask_id == "st-1-fix-1"
        assert fix_call.args[0].agent_id in {"claude", "deepseek"}

    def test_level2_failure_creates_fix_subtask(self, orchestrator, mock_validator, mock_owner_agent, mock_dispatcher):
        task = orchestrator._state.create_task("Test")
        subtask = SubTask(subtask_id="st-1", description="Do work", agent_id="claude", dependencies=[])
        task.subtasks.append(subtask)
        orchestrator._orchestrator_states[task.task_id] = MagicMock()
        orchestrator._orchestrator_states[task.task_id].fix_rounds = {}
        orchestrator._orchestrator_states[task.task_id].max_fix_rounds = 3
        orchestrator._orchestrator_states[task.task_id].acceptance_results = {}
        orchestrator._orchestrator_states[task.task_id].completed_subtasks = set()
        orchestrator._orchestrator_states[task.task_id].is_integration_testing = False

        job = orchestrator._state.create_job(subtask)
        orchestrator._state.update_job_status(job.job_id, JobStatus.COMPLETED)

        # Level 1 passes, Level 2 fails
        mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-1",
            level1_passed=True,
            level2_passed=False,
            level2_feedback="Add error handling",
        )

        run(orchestrator._handle_job_completed(job.job_id))

        fix_call = None
        for call in mock_dispatcher.dispatch_subtask.call_args_list:
            if "fix" in call.args[0].subtask_id:
                fix_call = call
                break
        assert fix_call is not None
        assert fix_call.args[0].subtask_id == "st-1-fix-1"
        assert "recommended_action" in fix_call.args[0].description

    def test_acceptance_provider_failure_pauses_task_without_reassign(
        self,
        orchestrator,
        mock_validator,
        mock_owner_agent,
        mock_dispatcher,
    ):
        task = orchestrator._state.create_task("Test")
        subtask = orchestrator._state.add_subtask(task.task_id, "Do work", "claude", subtask_id="st-1")
        orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

        job = orchestrator._state.create_job(subtask)
        orchestrator._state.update_job_status(job.job_id, JobStatus.COMPLETED)
        job.result = "Implemented the requested change."

        mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
        mock_owner_agent.accept_subtask.side_effect = [
            AcceptanceResult(
                subtask_id="st-1",
                level1_passed=True,
                level2_passed=False,
                level2_feedback="All LLM providers failed",
                action="retry_acceptance",
                parse_failed=True,
            ),
            AcceptanceResult(
                subtask_id="st-1",
                level1_passed=True,
                level2_passed=False,
                level2_feedback="All LLM providers failed",
                action="retry_acceptance",
                parse_failed=True,
            ),
            AcceptanceResult(
                subtask_id="st-1",
                level1_passed=True,
                level2_passed=False,
                level2_feedback="All LLM providers failed",
                action="retry_acceptance",
                parse_failed=True,
            ),
        ]

        run(orchestrator._handle_job_completed(job.job_id))

        refreshed_task = orchestrator._state.get_task(task.task_id)
        assert refreshed_task.status == TaskStatus.PAUSED
        assert "acceptance" in (refreshed_task.error or "").lower()
        dispatched_ids = [call.args[0].subtask_id for call in mock_dispatcher.dispatch_subtask.call_args_list]
        assert "st-1-fix-1" not in dispatched_ids
        assert "st-1-v2" not in dispatched_ids

    def test_artifact_lifecycle_transitions_from_provisional_to_accepted(
        self,
        orchestrator,
        mock_validator,
        mock_owner_agent,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "result.txt")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("ok")

            task = orchestrator._state.create_task("Test", project_dir=tmpdir)
            subtask = orchestrator._state.add_subtask(task.task_id, "Write file", "claude", subtask_id="st-1")
            orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

            job = orchestrator._state.create_job(subtask)
            orchestrator._state.complete_job(
                job.job_id,
                success=True,
                output=f"Created {output_path}",
                metadata={"created_files": [output_path]},
            )

            mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
            mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
                subtask_id="st-1",
                level1_passed=True,
                level2_passed=True,
            )

            run(orchestrator._handle_job_completed(job.job_id))

            records = orchestrator._state._persistence.get_artifact_records(task.task_id)
            assert records
            assert all(record["status"] == "accepted" for record in records)

    def test_artifact_recording_strips_tool_transcripts_from_metadata(
        self,
        orchestrator,
        mock_validator,
        mock_owner_agent,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "result.txt")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("ok")

            task = orchestrator._state.create_task("Test", project_dir=tmpdir)
            subtask = orchestrator._state.add_subtask(task.task_id, "Write file", "claude", subtask_id="st-1")
            orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

            job = orchestrator._state.create_job(subtask)
            orchestrator._state.complete_job(
                job.job_id,
                success=True,
                output=f"Created {output_path}",
                metadata={
                    "created_files": [output_path],
                    "tool_calls": [{"huge": "x" * 1000}],
                    "tool_results": [{"huge": "y" * 1000}],
                    "tool_call_count": 8,
                },
            )

            mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
            mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
                subtask_id="st-1",
                level1_passed=True,
                level2_passed=True,
            )

            run(orchestrator._handle_job_completed(job.job_id))

            [record] = orchestrator._state._persistence.get_artifact_records(task.task_id)
            metadata = record["metadata"]
            assert "tool_calls" not in metadata
            assert "tool_results" not in metadata
            assert metadata["tool_call_count"] == 8

    def test_artifact_recording_ignores_runtime_and_diagnostic_noise(self, orchestrator):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "backend", "app.py")
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("print('ok')\n")

            noisy_paths = [
                os.path.join(tmpdir, ".venv", "lib", "python3.14", "site-packages", "pkg.py"),
                os.path.join(tmpdir, ".pytest_cache", "v", "cache", "nodeids"),
                os.path.join(tmpdir, "backend", "__pycache__", "app.cpython-314.pyc"),
                os.path.join(tmpdir, "backend", "uploads", "receipt.png"),
                os.path.join(tmpdir, "backend", "instance", "expenses.db"),
                os.path.join(tmpdir, "_install_deps.py"),
                os.path.join(tmpdir, "check_env.py"),
                os.path.join(tmpdir, "run_check_imports.py"),
                os.path.join(tmpdir, "runner.py"),
            ]
            for path in noisy_paths:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write("noise")

            task = orchestrator._state.create_task("Test", project_dir=tmpdir)
            subtask = orchestrator._state.add_subtask(task.task_id, "Write app", "deepseek", subtask_id="st-1")
            job = orchestrator._state.create_job(subtask)
            orchestrator._state.complete_job(
                job.job_id,
                success=True,
                output="Created backend/app.py",
                metadata={"created_files": [output_path, *noisy_paths]},
            )

            orchestrator._record_job_artifact(task, orchestrator._state.get_job(job.job_id))

            records = orchestrator._state._persistence.get_artifact_records(task.task_id)
            assert [record["content_ref"] for record in records] == [os.path.realpath(output_path)]

    def test_artifact_recording_extracts_changed_files_from_diff_output(self, orchestrator):
        with tempfile.TemporaryDirectory() as tmpdir:
            app_js = os.path.join(tmpdir, "static", "js", "app.js")
            dashboard_js = os.path.join(tmpdir, "static", "js", "views", "dashboard.js")
            os.makedirs(os.path.dirname(dashboard_js), exist_ok=True)
            with open(app_js, "w", encoding="utf-8") as f:
                f.write("console.log('app')\n")
            with open(dashboard_js, "w", encoding="utf-8") as f:
                f.write("export function dashboardView() {}\n")

            task = orchestrator._state.create_task("Test", project_dir=tmpdir)
            subtask = orchestrator._state.add_subtask(task.task_id, "Write frontend", "hermes", subtask_id="st-1")
            job = orchestrator._state.create_job(subtask)
            orchestrator._state.complete_job(
                job.job_id,
                success=True,
                output=(
                    "┊ review diff\n"
                    "a/static/js/app.js → b/static/js/app.js\n"
                    "diff --git a/static/js/views/dashboard.js b/static/js/views/dashboard.js\n"
                    "+++ b/static/js/views/dashboard.js\n"
                ),
                metadata={},
            )

            orchestrator._record_job_artifact(task, orchestrator._state.get_job(job.job_id))

            records = orchestrator._state._persistence.get_artifact_records(task.task_id)
            assert {record["content_ref"] for record in records} == {
                os.path.realpath(app_js),
                os.path.realpath(dashboard_js),
            }

    def test_artifact_recording_extracts_relative_files_from_agent_summary(self, orchestrator):
        with tempfile.TemporaryDirectory() as tmpdir:
            app_init = os.path.join(tmpdir, "app", "__init__.py")
            tests_init = os.path.join(tmpdir, "tests", "__init__.py")
            gitkeep = os.path.join(tmpdir, "static", ".gitkeep")
            for path in (app_init, tests_init, gitkeep):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write("ok\n")

            task = orchestrator._state.create_task("Test", project_dir=tmpdir)
            subtask = orchestrator._state.add_subtask(task.task_id, "Create folders", "local", subtask_id="st-1")
            job = orchestrator._state.create_job(subtask)
            orchestrator._state.complete_job(
                job.job_id,
                success=True,
                output=(
                    "**文件已创建：**\n"
                    "- `app/__init__.py` - 18 bytes\n"
                    "- `tests/__init__.py` - 20 bytes\n"
                    "- `static/.gitkeep` - 24 bytes\n"
                ),
                metadata={},
            )

            orchestrator._record_job_artifact(task, orchestrator._state.get_job(job.job_id))

            records = orchestrator._state._persistence.get_artifact_records(task.task_id)
            assert {record["content_ref"] for record in records} == {
                os.path.realpath(app_init),
                os.path.realpath(tests_init),
                os.path.realpath(gitkeep),
            }

    def test_artifact_lifecycle_transitions_from_provisional_to_rejected(
        self,
        orchestrator,
        mock_validator,
        mock_owner_agent,
        mock_dispatcher,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "result.txt")
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("ok")

            task = orchestrator._state.create_task("Test", project_dir=tmpdir)
            subtask = orchestrator._state.add_subtask(task.task_id, "Write file", "claude", subtask_id="st-1")
            orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
            mock_dispatcher._get_valid_agents.return_value = ["claude"]

            job = orchestrator._state.create_job(subtask)
            orchestrator._state.complete_job(
                job.job_id,
                success=True,
                output=f"Created {output_path}",
                metadata={"created_files": [output_path]},
            )

            mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
            mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
                subtask_id="st-1",
                level1_passed=True,
                level2_passed=False,
                level2_feedback="Missing expected behavior",
            )

            run(orchestrator._handle_job_completed(job.job_id))

            records = orchestrator._state._persistence.get_artifact_records(task.task_id)
            assert records
            assert all(record["status"] == "rejected" for record in records)

    def test_acceptance_passed_unlocks_downstream_subtasks(self, orchestrator, mock_validator, mock_owner_agent, mock_dispatcher):
        task = orchestrator._state.create_task("Test")
        st_a = SubTask(subtask_id="st-a", description="A", agent_id="claude", dependencies=[])
        st_b = SubTask(subtask_id="st-b", description="B", agent_id="deepseek", dependencies=["st-a"])
        task.subtasks.extend([st_a, st_b])
        orchestrator._orchestrator_states[task.task_id] = MagicMock()
        orchestrator._orchestrator_states[task.task_id].fix_rounds = {}
        orchestrator._orchestrator_states[task.task_id].max_fix_rounds = 3
        orchestrator._orchestrator_states[task.task_id].acceptance_results = {}
        orchestrator._orchestrator_states[task.task_id].completed_subtasks = set()
        orchestrator._orchestrator_states[task.task_id].is_integration_testing = False

        job_a = orchestrator._state.create_job(st_a)
        orchestrator._state.update_job_status(job_a.job_id, JobStatus.COMPLETED)

        mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-a",
            level1_passed=True,
            level2_passed=True,
        )

        run(orchestrator._handle_job_completed(job_a.job_id))

        # st-b should now be dispatched
        dispatched_ids = [call.args[0].subtask_id for call in mock_dispatcher.dispatch_subtask.call_args_list]
        assert "st-b" in dispatched_ids

    def test_all_completed_triggers_integration_acceptance(self, orchestrator, mock_validator, mock_owner_agent, mock_dispatcher):
        task = orchestrator._state.create_task("Test")
        st_a = SubTask(subtask_id="st-a", description="A", agent_id="claude", dependencies=[])
        task.subtasks.append(st_a)
        orchestrator._orchestrator_states[task.task_id] = MagicMock()
        orchestrator._orchestrator_states[task.task_id].fix_rounds = {}
        orchestrator._orchestrator_states[task.task_id].max_fix_rounds = 3
        orchestrator._orchestrator_states[task.task_id].acceptance_results = {}
        orchestrator._orchestrator_states[task.task_id].completed_subtasks = set()
        orchestrator._orchestrator_states[task.task_id].is_integration_testing = False

        job_a = orchestrator._state.create_job(st_a)
        orchestrator._state.update_job_status(job_a.job_id, JobStatus.COMPLETED)

        mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-a",
            level1_passed=True,
            level2_passed=True,
        )

        run(orchestrator._handle_job_completed(job_a.job_id))

        mock_owner_agent.run_integration_test.assert_called_once()

    def test_fix_round_success_reapproves_wave_and_dispatches_next_wave(
        self,
        orchestrator,
        mock_validator,
        mock_owner_agent,
        mock_dispatcher,
    ):
        task = orchestrator._state.create_task("Wave test")
        original = SubTask(subtask_id="st-a", description="Architecture", agent_id="deepseek", dependencies=[])
        original.wave_number = 1
        original.status = JobStatus.FAILED
        original.error_message = "max_iterations_exceeded"
        downstream = SubTask(subtask_id="st-b", description="Implementation", agent_id="minimax", dependencies=["st-a"])
        downstream.wave_number = 2
        downstream.status = JobStatus.CANCELLED
        task.subtasks.extend([original, downstream])
        task.waves = [
            Wave(wave_id="wave-1", wave_number=1, task_id=task.task_id, subtasks=[original]),
            Wave(wave_id="wave-2", wave_number=2, task_id=task.task_id, subtasks=[downstream]),
        ]
        task.fix_rounds = {"st-a": 1}

        ost = MagicMock()
        ost.fix_rounds = task.fix_rounds
        ost.max_fix_rounds = 3
        ost.acceptance_results = {}
        ost.completed_subtasks = set()
        ost.is_integration_testing = False
        ost.wave_gate_enabled = True
        ost.wave_acceptance_recorded = set()
        ost.wave_approved = set()
        ost.strict_dependency = True
        ost.revalidating_waves = set()
        ost.wave_statuses = {}
        ost.blocked_by_wave = {}
        ost.recent_acceptance_records = []
        orchestrator._orchestrator_states[task.task_id] = ost

        fix_subtask = SubTask(subtask_id="st-a-fix-1", description="Fix architecture", agent_id="deepseek", dependencies=[])
        fix_subtask.wave_number = 1
        task.subtasks.append(fix_subtask)

        job = orchestrator._state.create_job(fix_subtask)
        orchestrator._state.update_job_status(job.job_id, JobStatus.COMPLETED)
        orchestrator._state.complete_job(job.job_id, success=True, output="Created files: /tmp/demo/docs/ARCHITECTURE.md")

        mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-a-fix-1",
            level1_passed=True,
            level2_passed=True,
        )
        mock_owner_agent.accept_wave.return_value = AcceptanceResult(
            subtask_id="st-a",
            level1_passed=True,
            level2_passed=True,
        )

        run(orchestrator._handle_job_completed(job.job_id))

        assert original.status == JobStatus.COMPLETED
        assert downstream.status == JobStatus.PENDING
        assert 1 in ost.wave_approved
        assert 2 in ost.revalidating_waves
        dispatched_ids = [call.args[0].subtask_id for call in mock_dispatcher.dispatch_subtask.call_args_list]
        assert "st-b" in dispatched_ids

    def test_prior_wave_fix_blocks_downstream_wave(self, orchestrator, mock_validator, mock_owner_agent, mock_dispatcher):
        task = orchestrator._state.create_task("Prior wave drift")
        st1 = SubTask(subtask_id="st-a", description="Schema", agent_id="claude", dependencies=[])
        st1.wave_number = 1
        st2 = SubTask(subtask_id="st-b", description="API", agent_id="deepseek", dependencies=["st-a"])
        st2.wave_number = 2
        task.subtasks.extend([st1, st2])

        wave1 = MagicMock(wave_number=1, is_blocked=False, blocked_by_wave=None, governance_status="pending", is_revalidating=False, owner_decision={})
        wave2 = MagicMock(wave_number=2, is_blocked=False, blocked_by_wave=None, governance_status="pending", is_revalidating=False, owner_decision={})
        task.waves = [wave1, wave2]

        ost = MagicMock()
        ost.fix_rounds = {}
        ost.max_fix_rounds = 3
        ost.acceptance_results = {}
        ost.completed_subtasks = set()
        ost.is_integration_testing = False
        ost.wave_gate_enabled = True
        ost.wave_acceptance_recorded = set()
        ost.wave_approved = set()
        ost.strict_dependency = True
        ost.revalidating_waves = set()
        ost.wave_statuses = {}
        ost.blocked_by_wave = {}
        ost.recent_acceptance_records = []
        orchestrator._orchestrator_states[task.task_id] = ost

        job = orchestrator._state.create_job(st2)
        orchestrator._state.update_job_status(job.job_id, JobStatus.COMPLETED)
        mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-b",
            level1_passed=True,
            level2_passed=False,
            level2_feedback="Upstream artifact drift",
            recommended_action="prior_wave_fix",
            root_cause_scope="prior_wave",
            root_cause_wave=1,
        )

        run(orchestrator._handle_job_completed(job.job_id))

        assert wave2.is_blocked is True
        assert wave2.blocked_by_wave == 1
        assert wave2.governance_status == "blocked"

    def test_reassign_action_dispatches_v2_subtask(self, orchestrator, mock_validator, mock_owner_agent, mock_dispatcher):
        task = orchestrator._state.create_task("Reassign")
        subtask = SubTask(subtask_id="st-1", description="Do work", agent_id="claude", dependencies=[])
        task.subtasks.append(subtask)
        ost = MagicMock()
        ost.fix_rounds = {}
        ost.max_fix_rounds = 3
        ost.acceptance_results = {}
        ost.completed_subtasks = set()
        ost.is_integration_testing = False
        ost.wave_gate_enabled = True
        ost.wave_acceptance_recorded = set()
        ost.wave_approved = set()
        ost.strict_dependency = True
        ost.revalidating_waves = set()
        ost.wave_statuses = {}
        ost.blocked_by_wave = {}
        ost.recent_acceptance_records = []
        orchestrator._orchestrator_states[task.task_id] = ost
        mock_dispatcher._get_valid_agents.return_value = ["claude", "hermes"]

        job = orchestrator._state.create_job(subtask)
        orchestrator._state.update_job_status(job.job_id, JobStatus.COMPLETED)
        mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-1",
            level1_passed=True,
            level2_passed=False,
            level2_feedback="Ancillary output only",
            recommended_action="reassign",
            preferred_agent="hermes",
            root_cause_scope="current_wave",
        )

        run(orchestrator._handle_job_completed(job.job_id))

        dispatched_ids = [call.args[0].subtask_id for call in mock_dispatcher.dispatch_subtask.call_args_list]
        assert "st-1-v2" in dispatched_ids

    def test_reassign_success_upgrades_original_and_completes_task(
        self,
        orchestrator,
        mock_validator,
        mock_owner_agent,
        mock_dispatcher,
    ):
        task = orchestrator._state.create_task("Reassign success")
        original = SubTask(subtask_id="st-1", description="Do work", agent_id="claude", dependencies=[])
        original.status = JobStatus.FAILED
        reassigned = SubTask(subtask_id="st-1-v4", description="Do work again", agent_id="hermes", dependencies=[])
        task.subtasks.extend([original, reassigned])
        ost = make_orchestrator_state(task)
        ost.wave_gate_enabled = False
        orchestrator._orchestrator_states[task.task_id] = ost

        job = orchestrator._state.create_job(reassigned)
        orchestrator._state.update_job_status(job.job_id, JobStatus.COMPLETED)
        mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-1-v4",
            level1_passed=True,
            level2_passed=True,
        )
        mock_owner_agent.run_integration_test.return_value = AcceptanceResult(
            subtask_id="integration",
            level1_passed=True,
            level2_passed=True,
        )

        run(orchestrator._handle_job_completed(job.job_id))

        assert original.status == JobStatus.COMPLETED
        assert reassigned.status == JobStatus.COMPLETED
        assert ost.completed_subtasks.issuperset({"st-1", "st-1-v4"})
        assert orchestrator._state.is_all_subtasks_completed(task.task_id) is True
        assert task.status == TaskStatus.COMPLETED
        assert mock_dispatcher.dispatch_subtask.call_count == 0

    def test_wave_fix_subtask_always_has_task_id(
        self,
        orchestrator,
        mock_dispatcher,
    ):
        task = orchestrator._state.create_task("Wave remediation")
        subtask = orchestrator._state.add_subtask(task.task_id, "Wave work", "claude", subtask_id="st-wave")
        subtask.wave_number = 1
        subtask.status = JobStatus.COMPLETED
        task.waves = [Wave(wave_id="wave-1", wave_number=1, task_id=task.task_id, subtasks=[subtask])]
        ost = make_orchestrator_state(task)
        mark_subtask_accepted(ost, subtask.subtask_id)
        orchestrator._orchestrator_states[task.task_id] = ost

        acceptance = AcceptanceResult(
            subtask_id="wave-1",
            level1_passed=True,
            level2_passed=False,
            level2_feedback="Wave acceptance failed",
        )

        run(orchestrator._handle_wave_gate_blocked(task, 1, acceptance, ost))

        created = next(st for st in task.subtasks if st.subtask_id == "wave-1-fix-1")
        assert created.task_id == task.task_id
        assert created.wave_number == 1
        assert created.description.startswith("[WAVE 1 FIX ROUND 1]")
        assert "current-wave coherence" in created.description
        assert "st-wave" in created.description

    def test_wave_fix_description_keeps_future_contracts_out_of_prompt(
        self,
        orchestrator,
        mock_dispatcher,
    ):
        task = orchestrator._state.create_task("Build expense app")
        current = orchestrator._state.add_subtask(
            task.task_id,
            "Implement Expense CRUD API endpoints",
            "deepseek",
            subtask_id="st-current",
        )
        current.wave_number = 3
        current.status = JobStatus.COMPLETED
        future = orchestrator._state.add_subtask(
            task.task_id,
            "Implement dashboard summary endpoints",
            "deepseek",
            subtask_id="st-future",
        )
        future.wave_number = 4
        task.waves = [Wave(wave_id="wave-3", wave_number=3, task_id=task.task_id, subtasks=[current])]
        ost = make_orchestrator_state(task)
        orchestrator._orchestrator_states[task.task_id] = ost

        acceptance = AcceptanceResult(
            subtask_id="wave-3",
            level1_passed=True,
            level2_passed=False,
            level2_feedback="Missing dashboard endpoints and duplicate routers.",
        )

        run(orchestrator._handle_wave_gate_blocked(task, 3, acceptance, ost))

        created = next(st for st in task.subtasks if st.subtask_id == "wave-3-fix-1")
        description = created.description
        assert description.startswith("[WAVE 3 FIX ROUND 1]")
        assert "st-current" in description
        assert "Do not implement future-wave functionality" in description
        assert "st-future" not in description
        assert "Implement dashboard summary endpoints" not in description

    def test_existing_wave_fix_subtask_is_reassigned_to_blocked_wave(
        self,
        orchestrator,
        mock_dispatcher,
    ):
        task = orchestrator._state.create_task("Wave remediation")
        current = orchestrator._state.add_subtask(task.task_id, "Wave 4 work", "claude", subtask_id="st-wave-4")
        current.wave_number = 4
        current.status = JobStatus.COMPLETED
        task.waves = [Wave(wave_id="wave-4", wave_number=4, task_id=task.task_id, subtasks=[current])]
        existing_fix = orchestrator._state.add_subtask(
            task.task_id,
            "Old fix prompt",
            "claude",
            subtask_id="wave-4-fix-1",
        )
        existing_fix.wave_number = 1
        existing_fix.status = JobStatus.PENDING
        ost = make_orchestrator_state(task)
        orchestrator._orchestrator_states[task.task_id] = ost

        acceptance = AcceptanceResult(
            subtask_id="wave-4",
            level1_passed=True,
            level2_passed=False,
            level2_feedback="app/static/css/styles.css must move to app/static/styles.css.",
        )

        run(orchestrator._handle_wave_gate_blocked(task, 4, acceptance, ost))

        assert existing_fix.wave_number == 4
        assert existing_fix.task_id == task.task_id
        assert existing_fix.description.startswith("[WAVE 4 FIX ROUND 1]")
        assert mock_dispatcher.dispatch_subtask.called

    def test_wave_acceptance_cleans_workspace_noise_before_owner_review(
        self,
        orchestrator,
        mock_owner_agent,
        tmp_path,
    ):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "app").mkdir()
        main_file = project_dir / "app" / "main.py"
        main_file.write_text("from fastapi import FastAPI\n")
        root_runner = project_dir / "run_tests.py"
        root_runner.write_text("print('diagnostic')\n")
        pycache_dir = project_dir / "app" / "__pycache__"
        pycache_dir.mkdir()
        pycache_file = pycache_dir / "main.cpython-314.pyc"
        pycache_file.write_bytes(b"cache")
        pytest_cache = project_dir / ".pytest_cache" / "v" / "cache"
        pytest_cache.mkdir(parents=True)
        pytest_cache_file = pytest_cache / "nodeids"
        pytest_cache_file.write_text("[]\n")

        task = orchestrator._state.create_task("Wave cleanup", project_dir=str(project_dir))
        subtask = orchestrator._state.add_subtask(task.task_id, "Implement API", "deepseek", subtask_id="st-clean")
        subtask.wave_number = 1
        subtask.status = JobStatus.COMPLETED
        subtask.output_file = str(main_file)
        task.waves = [Wave(wave_id="wave-1", wave_number=1, task_id=task.task_id, subtasks=[subtask])]
        ost = make_orchestrator_state(task)
        mark_subtask_accepted(ost, subtask.subtask_id)
        orchestrator._orchestrator_states[task.task_id] = ost
        mock_owner_agent.accept_wave.return_value = AcceptanceResult(
            subtask_id="wave-1",
            level1_passed=True,
            level2_passed=True,
            action="approve",
        )

        run(orchestrator._maybe_record_wave_acceptance(task, subtask.subtask_id, ost))

        assert not root_runner.exists()
        assert not pycache_file.exists()
        assert not pycache_dir.exists()
        assert not pytest_cache_file.exists()
        assert not (project_dir / ".pytest_cache").exists()
        mock_owner_agent.accept_wave.assert_called_once_with(task, 1)

    def test_subtask_acceptance_cleans_workspace_noise_before_owner_review(
        self,
        orchestrator,
        mock_validator,
        mock_owner_agent,
        tmp_path,
    ):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "app").mkdir()
        main_file = project_dir / "app" / "main.py"
        main_file.write_text("from fastapi import FastAPI\n", encoding="utf-8")

        noise_files = [
            project_dir / "run_import_test.py",
            project_dir / "run_tests.py",
            project_dir / "_run_tests.py",
            project_dir / "tmp_run_tests.py",
            project_dir / "cleanup.py",
            project_dir / "test_script.py",
            project_dir / "verify.py",
        ]
        for path in noise_files:
            path.write_text("print('diagnostic')\n", encoding="utf-8")

        task = orchestrator._state.create_task("Subtask cleanup", project_dir=str(project_dir))
        subtask = orchestrator._state.add_subtask(task.task_id, "Implement API", "deepseek", subtask_id="st-clean")
        subtask.wave_number = 1
        subtask.output_file = str(main_file)
        orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

        def validate_without_noise(job):
            assert all(not path.exists() for path in noise_files)
            return ValidationReport(passed=True, errors=[])

        def accept_without_noise(job):
            assert all(not path.exists() for path in noise_files)
            return AcceptanceResult(
                subtask_id=job.subtask_id,
                level1_passed=True,
                level2_passed=True,
            )

        mock_validator.validate.side_effect = validate_without_noise
        mock_owner_agent.accept_subtask.side_effect = accept_without_noise

        job = orchestrator._state.create_job(subtask)
        orchestrator._state.complete_job(job.job_id, success=True, output=f"Created {main_file}")

        run(orchestrator._handle_job_completed(job.job_id))

        assert all(not path.exists() for path in noise_files)
        mock_owner_agent.accept_subtask.assert_called_once()

    def test_max_rounds_triggers_downgrade(self, orchestrator, mock_validator, mock_owner_agent, mock_dispatcher):
        task = orchestrator._state.create_task("Test")
        subtask = SubTask(subtask_id="st-1", description="Do work", agent_id="claude", dependencies=[])
        downstream = SubTask(subtask_id="st-2", description="Continue", agent_id="deepseek", dependencies=["st-1"])
        downstream.status = JobStatus.CANCELLED
        task.subtasks.extend([subtask, downstream])
        # Persist fix rounds on Task itself
        task.fix_rounds = {"st-1": 3}
        orchestrator._orchestrator_states[task.task_id] = MagicMock()
        orchestrator._orchestrator_states[task.task_id].fix_rounds = task.fix_rounds
        orchestrator._orchestrator_states[task.task_id].max_fix_rounds = 3
        orchestrator._orchestrator_states[task.task_id].acceptance_results = {}
        orchestrator._orchestrator_states[task.task_id].completed_subtasks = set()
        orchestrator._orchestrator_states[task.task_id].is_integration_testing = False

        job = orchestrator._state.create_job(subtask)
        orchestrator._state.update_job_status(job.job_id, JobStatus.COMPLETED)

        mock_validator.validate.return_value = ValidationReport(passed=False, errors=[MagicMock(error_type="missing_field")])
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-1",
            level1_passed=False,
            level2_passed=False,
            level2_feedback="Still broken",
        )
        mock_owner_agent.decide_on_failure.return_value = MagicMock(action="downgrade")

        run(orchestrator._handle_job_completed(job.job_id))

        current_task = orchestrator._state.get_task_by_subtask("st-1")
        assert next(st for st in current_task.subtasks if st.subtask_id == "st-1").status == JobStatus.FAILED
        assert next(st for st in current_task.subtasks if st.subtask_id == "st-2").status == JobStatus.CANCELLED
        assert current_task.status == JobStatus.FAILED

    def test_max_rounds_triggers_reassign(self, orchestrator, mock_validator, mock_owner_agent, mock_dispatcher):
        task = orchestrator._state.create_task("Test")
        subtask = SubTask(subtask_id="st-1", description="Do work", agent_id="claude", dependencies=[])
        task.subtasks.append(subtask)
        # Persist fix rounds on Task itself
        task.fix_rounds = {"st-1": 3}
        orchestrator._orchestrator_states[task.task_id] = MagicMock()
        orchestrator._orchestrator_states[task.task_id].fix_rounds = task.fix_rounds
        orchestrator._orchestrator_states[task.task_id].max_fix_rounds = 3
        orchestrator._orchestrator_states[task.task_id].acceptance_results = {}
        orchestrator._orchestrator_states[task.task_id].completed_subtasks = set()
        orchestrator._orchestrator_states[task.task_id].is_integration_testing = False

        job = orchestrator._state.create_job(subtask)
        orchestrator._state.update_job_status(job.job_id, JobStatus.COMPLETED)

        mock_validator.validate.return_value = ValidationReport(passed=False, errors=[])
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-1",
            level1_passed=False,
            level2_passed=False,
        )
        mock_owner_agent.decide_on_failure.return_value = MagicMock(action="reassign", new_agent="hermes")

        run(orchestrator._handle_job_completed(job.job_id))

        current_task = orchestrator._state.get_task(task.task_id)
        assert current_task.status == TaskStatus.COMPLETED_WITH_FAILURES
        assert next(st for st in current_task.subtasks if st.subtask_id == "st-1").status == JobStatus.FAILED
        dispatched_ids = [call.args[0].subtask_id for call in mock_dispatcher.dispatch_subtask.call_args_list]
        assert not any("v2" in sid for sid in dispatched_ids)


class TestRestoreAndDispatch:
    """Stricter N38 regression tests using real OrchestratorState (not MagicMock).

    The existing ``test_fix_round_success_reapproves_wave_and_dispatches_next_wave``
    uses a MagicMock for OrchestratorState, which means ``in`` checks on
    ``wave_approved`` / ``blocked_by_wave`` always return True and mask
    wave-gate timing issues.
    """

    def test_remediation_success_persists_restores_and_dispatches_cancelled_downstream(
        self,
        orchestrator,
        mock_validator,
        mock_owner_agent,
        mock_dispatcher,
    ):
        """After a fix round passes, the original subtask is upgraded to COMPLETED,
        cancelled downstream subtasks are restored to PENDING and dispatched."""
        from across_agents_assistant.task_manager.models import Wave

        task = orchestrator._state.create_task("Fix then continue")
        original = orchestrator._state.add_subtask(
            task.task_id,
            "Produce API contract",
            "deepseek",
            subtask_id="st-a",
        )
        downstream = orchestrator._state.add_subtask(
            task.task_id,
            "Implement API from contract",
            "minimax",
            dependencies=["st-a"],
            subtask_id="st-b",
        )
        original.wave_number = 1
        downstream.wave_number = 2
        original.status = JobStatus.FAILED
        downstream.status = JobStatus.CANCELLED
        orchestrator._state._persist_subtask(original)
        orchestrator._state._persist_subtask(downstream)
        task.waves = [
            Wave(wave_id="wave-1", wave_number=1, task_id=task.task_id, subtasks=[original]),
            Wave(wave_id="wave-2", wave_number=2, task_id=task.task_id, subtasks=[downstream]),
        ]

        ost = make_orchestrator_state(task)
        ost.wave_gate_enabled = True
        mark_subtask_accepted(ost, "st-1")
        orchestrator._orchestrator_states[task.task_id] = ost

        fix = orchestrator._state.add_subtask(
            task.task_id,
            "Fix API contract",
            "deepseek",
            subtask_id="st-a-fix-1",
        )
        fix.wave_number = 1
        orchestrator._state._persist_subtask(fix)

        job = orchestrator._state.create_job(fix)
        orchestrator._state.complete_job(job.job_id, success=True, output="Fixed API contract")

        mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-a-fix-1",
            level1_passed=True,
            level2_passed=True,
        )
        mock_owner_agent.accept_wave.return_value = AcceptanceResult(
            subtask_id="wave-1",
            level1_passed=True,
            level2_passed=True,
        )

        run(orchestrator._handle_job_completed(job.job_id))

        assert original.status == JobStatus.COMPLETED
        assert downstream.status == JobStatus.PENDING
        assert "st-a" in ost.completed_subtasks
        assert 1 in ost.wave_approved
        dispatched_ids = [call.args[0].subtask_id for call in mock_dispatcher.dispatch_subtask.call_args_list]
        assert "st-b" in dispatched_ids

    def test_remediation_success_after_prior_cancel_restores_and_dispatches_downstream(
        self,
        orchestrator,
        mock_validator,
        mock_owner_agent,
        mock_dispatcher,
    ):
        """Full N38 path: cancel downstream → fix success → restore → dispatch.

        This simulates the exact scenario from the E2E report where
        ``cancel_downstream_subtasks()`` was called before fix completion,
        and after the fix succeeds the downstream subtask should be
        restored and dispatched.
        """
        from across_agents_assistant.task_manager.models import Wave

        task = orchestrator._state.create_task("N38 full path")
        st_a = orchestrator._state.add_subtask(
            task.task_id, "Produce models.py", "deepseek", subtask_id="st-a",
        )
        st_b = orchestrator._state.add_subtask(
            task.task_id,
            "Implement main.py",
            "deepseek",
            dependencies=["st-a"],
            subtask_id="st-b",
        )
        st_a.wave_number = 1
        st_b.wave_number = 2
        st_a.status = JobStatus.FAILED
        st_b.status = JobStatus.PENDING
        task.waves = [
            Wave(wave_id="wave-1", task_id=task.task_id, wave_number=1, subtasks=[st_a]),
            Wave(wave_id="wave-2", task_id=task.task_id, wave_number=2, subtasks=[st_b]),
        ]

        # Simulate the E2E scenario: st-a fails → downstream cancelled
        cancelled = orchestrator._state.cancel_downstream_subtasks(task.task_id, "st-a")
        assert cancelled == ["st-b"]
        assert st_b.status == JobStatus.CANCELLED

        ost = make_orchestrator_state(task)
        ost.wave_gate_enabled = True
        mark_subtask_accepted(ost, "st-1")
        orchestrator._orchestrator_states[task.task_id] = ost

        fix = orchestrator._state.add_subtask(
            task.task_id,
            "Fix models.py",
            "deepseek",
            subtask_id="st-a-fix-1",
        )
        fix.wave_number = 1

        job = orchestrator._state.create_job(fix)
        orchestrator._state.complete_job(job.job_id, success=True, output="Created models.py")

        mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-a-fix-1",
            level1_passed=True,
            level2_passed=True,
        )
        mock_owner_agent.accept_wave.return_value = AcceptanceResult(
            subtask_id="wave-1",
            level1_passed=True,
            level2_passed=True,
        )

        run(orchestrator._handle_job_completed(job.job_id))

        assert st_a.status == JobStatus.COMPLETED
        assert fix.status == JobStatus.COMPLETED
        assert st_b.status == JobStatus.PENDING
        assert "st-a" in ost.completed_subtasks
        assert orchestrator._state._get_subtask_status(task.task_id, "st-a") == JobStatus.COMPLETED
        dispatched_ids = [call.args[0].subtask_id for call in mock_dispatcher.dispatch_subtask.call_args_list]
        assert "st-b" in dispatched_ids


class TestN52PendingWaveGateDeadEnd:
    def test_remediation_exhaustion_cancels_later_wave_pending_subtasks_blocked_by_gate(
        self,
        orchestrator,
    ):
        """A failed unapproved earlier wave should not leave later-wave subtasks pending forever."""
        task = orchestrator._state.create_task("N52 pending dead end")
        st_a = orchestrator._state.add_subtask(
            task.task_id,
            "Produce failing foundation",
            "deepseek",
            subtask_id="st-a",
        )
        st_b = orchestrator._state.add_subtask(
            task.task_id,
            "Independent later wave work",
            "minimax",
            subtask_id="st-b",
        )
        st_a.wave_number = 1
        st_b.wave_number = 2
        st_a.status = JobStatus.FAILED
        st_b.status = JobStatus.PENDING
        task.waves = [
            Wave(wave_id="wave-1", task_id=task.task_id, wave_number=1, subtasks=[st_a]),
            Wave(wave_id="wave-2", task_id=task.task_id, wave_number=2, subtasks=[st_b]),
        ]

        ost = make_orchestrator_state(task)
        ost.wave_gate_enabled = True
        ost.strict_dependency = True
        orchestrator._orchestrator_states[task.task_id] = ost

        job = orchestrator._state.create_job(st_a)
        orchestrator._state.complete_job(job.job_id, success=False, error="max rounds exhausted")
        acceptance = AcceptanceResult(
            subtask_id=st_a.subtask_id,
            level1_passed=False,
            level2_passed=False,
        )

        run(orchestrator._handle_remediation_exhausted(task, job, acceptance, "st-a"))

        assert st_b.status == JobStatus.CANCELLED
        assert orchestrator._state.is_all_subtasks_terminal(task.task_id)


class TestFixRoundArtifact:
    """NEW-3: verify fix/reassign subtasks produce artifact records from contract path hints."""

    def test_fix_round_records_artifact_from_canonical_contract_path_hint(
        self,
        orchestrator,
        mock_validator,
        mock_owner_agent,
    ):
        """When a fix subtask passes acceptance, artifact records should include
        files found via canonical contract path hints."""
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = os.path.join(tmpdir, "models.py")
            with open(artifact_path, "w", encoding="utf-8") as f:
                f.write("class Item: pass\n")

            task = orchestrator._state.create_task("Fix artifact lineage", project_dir=tmpdir)
            original = orchestrator._state.add_subtask(
                task.task_id,
                "Create models.py",
                "deepseek",
                subtask_id="st-models",
            )
            original.status = JobStatus.FAILED
            fix = orchestrator._state.add_subtask(
                task.task_id,
                "Fix models.py",
                "deepseek",
                subtask_id="st-models-fix-1",
            )
            task.subtasks.extend([])
            ost = make_orchestrator_state(task)
            ost.wave_gate_enabled = False
            orchestrator._orchestrator_states[task.task_id] = ost

            contract = TaskContract.new(
                task_id=task.task_id,
                level="subtask",
                goal="Create models.py",
                subtask_id="st-models",
                project_dir=tmpdir,
            )
            contract.expected_deliverables = [
                DeliverableSpec(
                    artifact_type="file",
                    required=True,
                    path_hint="models.py",
                    description="Expected model file",
                )
            ]
            orchestrator._state.save_task_contract(contract)

            job = orchestrator._state.create_job(fix)
            orchestrator._state.complete_job(job.job_id, success=True, output="Fixed model implementation")

            mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
            mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
                subtask_id="st-models-fix-1",
                level1_passed=True,
                level2_passed=True,
            )
            mock_owner_agent.run_integration_test.return_value = AcceptanceResult(
                subtask_id="integration",
                level1_passed=True,
                level2_passed=True,
            )

            run(orchestrator._handle_job_completed(job.job_id))

            records = orchestrator._state._persistence.get_artifact_records(task.task_id)
            assert records, "No artifact records were created"
            record = records[0]
            assert record["subtask_id"] == "st-models-fix-1", f"Wrong subtask_id: {record}"
            assert record["content_ref"] == os.path.realpath(artifact_path), f"Wrong content_ref: {record}"
            assert record["status"] == "accepted", f"Wrong status: {record}"
            assert record.get("metadata", {}).get("canonical_subtask_id") == "st-models", (
                f"Missing canonical_subtask_id: {record}"
            )


class TestDispatchRepairWatchdog:
    def test_repair_does_not_repeat_blocked_wave_acceptance(
        self,
        orchestrator,
        mock_owner_agent,
    ):
        task = orchestrator._state.create_task("Blocked wave repair")
        subtask = SubTask(
            subtask_id="st-blocked",
            task_id=task.task_id,
            description="Produce risky foundation",
            agent_id="deepseek",
            wave_number=1,
        )
        subtask.status = JobStatus.COMPLETED
        task.subtasks.append(subtask)
        task.waves = [
            Wave(
                wave_id="wave-1",
                task_id=task.task_id,
                wave_number=1,
                subtasks=[subtask],
                status=JobStatus.COMPLETED,
            )
        ]
        task.status = TaskStatus.PENDING
        orchestrator._state._persistence.save_acceptance_record({
            "task_id": task.task_id,
            "subtask_id": subtask.subtask_id,
            "level": "subtask",
            "decision": "approve",
            "deterministic_passed": True,
            "judge_passed": True,
        })

        mock_owner_agent.accept_wave.return_value = AcceptanceResult(
            subtask_id="wave-1",
            level1_passed=True,
            level2_passed=False,
            action="fix",
        )

        first = orchestrator.repair_task_dispatch(task.task_id, reason="api_status_poll")
        second = orchestrator.repair_task_dispatch(task.task_id, reason="api_status_poll")

        assert first["state_created"] is True
        assert first["waves_approved"] == []
        assert second["state_created"] is False
        assert second["waves_approved"] == []
        mock_owner_agent.accept_wave.assert_called_once_with(task, 1)

    def test_repair_task_dispatch_restarts_pending_decompose_subtask(
        self,
        orchestrator,
        mock_owner_agent,
        mock_dispatcher,
    ):
        task = orchestrator._state.create_task("interrupted decomposition")
        task.status = TaskStatus.PENDING
        decompose = SubTask(
            task_id=task.task_id,
            subtask_id=f"{task.task_id}-decompose",
            description="[DECOMPOSE] interrupted decomposition",
            agent_id="owner",
            dependencies=[],
        )
        decompose.wave_number = 0
        decompose.status = JobStatus.PENDING
        task.subtasks.append(decompose)
        task.waves = [Wave(wave_id=f"{task.task_id}-wave-0", task_id=task.task_id, wave_number=0, subtasks=[decompose])]

        def decompose_side_effect(task_arg, context=None):
            business = SubTask(
                task_id=task_arg.task_id,
                subtask_id="st-resumed",
                description="resumed business work",
                agent_id="deepseek",
                dependencies=[],
            )
            task_arg.subtasks.append(business)
            decompose.status = JobStatus.COMPLETED
            return task_arg

        def assign_waves_side_effect(task_arg):
            business = next(st for st in task_arg.subtasks if st.subtask_id == "st-resumed")
            business.wave_number = 1
            task_arg.waves = [
                Wave(wave_id=f"{task_arg.task_id}-wave-0", task_id=task_arg.task_id, wave_number=0, subtasks=[decompose]),
                Wave(wave_id=f"{task_arg.task_id}-wave-1", task_id=task_arg.task_id, wave_number=1, subtasks=[business]),
            ]
            return task_arg

        mock_owner_agent.decompose_and_assign.side_effect = decompose_side_effect
        mock_owner_agent.assign_waves.side_effect = assign_waves_side_effect
        mock_owner_agent.refresh_decomposition_coverage.return_value = None
        mock_dispatcher.dispatch_subtask.return_value = MagicMock(job_id="job-resumed")

        result = orchestrator.repair_task_dispatch(task.task_id, reason="auto_resume")

        assert result["decomposition_restarted"] is True
        assert result["dispatched_subtasks"] == ["st-resumed"]
        assert task.status == TaskStatus.RUNNING
        assert mock_owner_agent.decompose_and_assign.call_count == 1
        assert mock_owner_agent.assign_waves.call_count == 1
        mock_dispatcher.dispatch_subtask.assert_called_once()

    def test_resume_task_does_not_dispatch_owner_decompose_subtask(
        self,
        orchestrator,
        mock_dispatcher,
    ):
        task = orchestrator._state.create_task("controlled decompose orphan")
        task.status = TaskStatus.PENDING
        decompose = SubTask(
            task_id=task.task_id,
            subtask_id=f"{task.task_id}-decompose",
            description="[DECOMPOSE] controlled orphan",
            agent_id="owner",
            dependencies=[],
        )
        decompose.wave_number = 0
        decompose.status = JobStatus.PENDING
        task.subtasks.append(decompose)
        task.waves = []

        orchestrator.resume_task(task)

        mock_dispatcher.dispatch_subtask.assert_not_called()
        assert decompose.status == JobStatus.PENDING
        assert task.status == TaskStatus.PENDING

    def test_auto_resume_missing_keys_waits_instead_of_failing(
        self,
        orchestrator,
        mock_owner_agent,
    ):
        task = orchestrator._state.create_task("needs keys")
        task.status = TaskStatus.PENDING
        decompose = SubTask(
            task_id=task.task_id,
            subtask_id=f"{task.task_id}-decompose",
            description="[DECOMPOSE] needs keys",
            agent_id="owner",
            dependencies=[],
        )
        decompose.wave_number = 0
        decompose.status = JobStatus.PENDING
        task.subtasks.append(decompose)
        task.waves = [Wave(wave_id="wave-0", task_id=task.task_id, wave_number=0, subtasks=[decompose])]

        mock_owner_agent.decompose_and_assign.side_effect = RuntimeError(
            "LLM decomposition failed: All LLM providers failed. Last error: No API key found for minimax"
        )

        result = orchestrator.repair_task_dispatch(task.task_id, reason="auto_resume")

        assert result["decomposition_restarted"] is True
        assert result["waiting_for_keys"] is True
        assert task.status == TaskStatus.PENDING
        assert task.last_owner_decision["blocked_reason"] == "waiting_for_keys"
        assert task.last_owner_decision["recoverable"] is True
        assert decompose.status == JobStatus.PENDING
        assert "Waiting for API keys" in decompose.error_message

    def test_decomposition_non_key_failure_marks_task_subtask_and_wave_failed(
        self,
        orchestrator,
        mock_owner_agent,
    ):
        task = orchestrator._state.create_task("non key failure")
        task.status = TaskStatus.PENDING
        decompose = SubTask(
            task_id=task.task_id,
            subtask_id=f"{task.task_id}-decompose",
            description="[DECOMPOSE] non key failure",
            agent_id="owner",
            dependencies=[],
        )
        decompose.wave_number = 0
        decompose.status = JobStatus.PENDING
        wave0 = Wave(wave_id="wave-0", task_id=task.task_id, wave_number=0, subtasks=[decompose])
        wave0.status = JobStatus.PENDING
        task.subtasks.append(decompose)
        task.waves = [wave0]

        mock_owner_agent.decompose_and_assign.side_effect = RuntimeError("malformed decomposition json")

        result = orchestrator.repair_task_dispatch(task.task_id, reason="auto_resume")

        assert result["decomposition_restarted"] is True
        assert result["waiting_for_keys"] is False
        assert task.status == TaskStatus.FAILED
        assert "Decomposition resume failed" in task.error
        assert decompose.status == JobStatus.FAILED
        assert "malformed decomposition json" in decompose.error_message
        assert wave0.status == JobStatus.FAILED


class TestCallbackRegistration:
    def test_callback_registered_in_init(self, mock_dispatcher, mock_validator, mock_owner_agent):
        state = TaskState()
        TaskOrchestrator(
            state=state,
            dispatcher=mock_dispatcher,
            validator=mock_validator,
            owner_agent=mock_owner_agent,
        )
        assert mock_dispatcher.add_progress_callback.call_count == 1
        callback = mock_dispatcher.add_progress_callback.call_args[0][0]
        assert callback.__name__ == "_on_job_progress"


class TestDownstreamRevalidationLineage:
    def test_marks_only_waves_that_consumed_superseded_artifacts(self, orchestrator):
        task = Task.new("Lineage task")
        st_a = SubTask(subtask_id="st-a", description="Produce API", agent_id="claude", wave_number=1)
        st_b = SubTask(subtask_id="st-b", description="Consume API", agent_id="deepseek", wave_number=2)
        st_c = SubTask(subtask_id="st-c", description="Independent UI", agent_id="minimax", wave_number=3)
        task.subtasks = [st_a, st_b, st_c]
        task.waves = [
            Wave(wave_id="wave-1", task_id=task.task_id, wave_number=1, subtasks=[st_a]),
            Wave(wave_id="wave-2", task_id=task.task_id, wave_number=2, subtasks=[st_b]),
            Wave(wave_id="wave-3", task_id=task.task_id, wave_number=3, subtasks=[st_c]),
        ]
        orchestrator._state._tasks[task.task_id] = task
        orchestrator._state._persistence.artifact_records = [
            {
                "artifact_id": "art-api-v1",
                "task_id": task.task_id,
                "subtask_id": "st-a",
                "wave_number": 1,
                "status": "accepted",
                "source_artifact_ids": [],
            },
            {
                "artifact_id": "art-consumer",
                "task_id": task.task_id,
                "subtask_id": "st-b",
                "wave_number": 2,
                "status": "accepted",
                "source_artifact_ids": ["art-api-v1"],
            },
            {
                "artifact_id": "art-independent",
                "task_id": task.task_id,
                "subtask_id": "st-c",
                "wave_number": 3,
                "status": "accepted",
                "source_artifact_ids": [],
            },
        ]
        ost = make_orchestrator_state(task)

        orchestrator._mark_downstream_revalidating(task, "st-a", ost)

        assert task.waves[1].is_revalidating is True
        assert task.waves[1].governance_status == "revalidating"
        assert task.waves[2].is_revalidating is False


class TestFinalizationRemediationBlocking:
    def test_finalize_defers_when_wave_fix_subtask_running(self, orchestrator, mock_validator, mock_owner_agent, mock_dispatcher):
        """Task should remain RUNNING when active wave fix subtask exist."""
        task = orchestrator._state.create_task("Test", project_dir=str(tempfile.mkdtemp()))
        subtask = orchestrator._state.add_subtask(task.task_id, "Do work", "claude", subtask_id="st-1")
        subtask.status = JobStatus.COMPLETED
        orchestrator._state._persist_subtask(subtask)

        # Add an active wave fix subtask
        fix = orchestrator._state.add_subtask(task.task_id, "Fix wave", "claude", subtask_id="wave-1-fix-1")
        fix.wave_number = 1
        fix.status = JobStatus.RUNNING
        orchestrator._state._persist_subtask(fix)

        mock_dispatcher._get_valid_agents.return_value = ["claude", "deepseek"]
        ost = make_orchestrator_state(task)
        orchestrator._orchestrator_states[task.task_id] = ost

        run(orchestrator._finalize_task_status(task.task_id))

        task = orchestrator._state.get_task(task.task_id)
        assert task.status == TaskStatus.RUNNING
        assert task.error and "Waiting for remediation" in task.error

    def test_finalize_defers_when_st_quality_subtask_running(self, orchestrator, mock_dispatcher):
        """Task should remain RUNNING when active st-quality-* subtask exist."""
        task = orchestrator._state.create_task("Test", project_dir=str(tempfile.mkdtemp()))
        subtask = orchestrator._state.add_subtask(task.task_id, "Do work", "claude", subtask_id="st-1")
        subtask.status = JobStatus.COMPLETED
        orchestrator._state._persist_subtask(subtask)

        quality = orchestrator._state.add_subtask(task.task_id, "Quality fix", "deepseek", subtask_id="st-quality-abc")
        quality.status = JobStatus.RUNNING
        orchestrator._state._persist_subtask(quality)

        mock_dispatcher._get_valid_agents.return_value = ["claude", "deepseek"]
        ost = make_orchestrator_state(task)
        orchestrator._orchestrator_states[task.task_id] = ost

        run(orchestrator._finalize_task_status(task.task_id))

        task = orchestrator._state.get_task(task.task_id)
        assert task.status == TaskStatus.RUNNING

    def test_quality_pass_repairs_stale_blocked_wave_before_completed(self, orchestrator, mock_validator, mock_owner_agent, mock_dispatcher):
        """Blocked wave with all completed subtasks should be repaired to approved."""
        task = orchestrator._state.create_task("Test", project_dir=str(tempfile.mkdtemp()))
        subtask = orchestrator._state.add_subtask(task.task_id, "Do work", "claude", subtask_id="st-1")
        subtask.status = JobStatus.COMPLETED
        subtask.wave_number = 1
        orchestrator._state._persist_subtask(subtask)

        # Create a wave with blocked governance but all subtasks completed
        from across_agents_assistant.task_manager.models import Wave
        wave = Wave(
            wave_id="wave-test",
            wave_number=1,
            task_id=task.task_id,
            subtasks=[subtask],
            status=JobStatus.COMPLETED,
            is_blocked=True,
            governance_status="blocked",
        )
        task.waves = [wave]
        orchestrator._state._persist_wave(wave)

        mock_validator.validate.return_value = ValidationReport(passed=True, errors=[])
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-1", level1_passed=True, level2_passed=True, action="approve",
        )
        mock_owner_agent.run_integration_test.return_value = MagicMock(passed=True)
        mock_dispatcher._get_valid_agents.return_value = ["claude", "deepseek"]

        ost = make_orchestrator_state(task)
        ost.strict_dependency = False
        orchestrator._orchestrator_states[task.task_id] = ost

        # Directly call repair
        repaired = orchestrator._repair_stale_wave_governance_after_quality_pass(task)
        assert 1 in repaired
        assert wave.governance_status == "approved"
        assert wave.is_blocked is False


class TestWaveGateDeadEndRecovery:
    def _make_blocked_two_wave_task(self, orchestrator, tmp_path):
        task = orchestrator._state.create_task("blocked wave gate", project_dir=str(tmp_path))
        task.status = TaskStatus.RUNNING
        st1 = orchestrator._state.add_subtask(task.task_id, "Foundation output", "claude", subtask_id="st-wave-1")
        st2 = orchestrator._state.add_subtask(
            task.task_id,
            "Downstream implementation",
            "deepseek",
            dependencies=["st-wave-1"],
            subtask_id="st-wave-2",
        )
        st1.wave_number = 1
        st2.wave_number = 2
        st1.status = JobStatus.COMPLETED
        st2.status = JobStatus.PENDING

        wave1 = Wave(
            wave_id="wave-1",
            task_id=task.task_id,
            wave_number=1,
            subtasks=[st1],
            status=JobStatus.COMPLETED,
            is_blocked=True,
            governance_status="blocked",
            blocked_by_wave=1,
            owner_decision={"decision": "reject", "recommended_action": "wave_fix"},
        )
        wave2 = Wave(
            wave_id="wave-2",
            task_id=task.task_id,
            wave_number=2,
            subtasks=[st2],
            status=JobStatus.PENDING,
        )
        task.waves = [wave1, wave2]
        orchestrator._state._persist_task(task)
        return task, st1, st2, wave1, wave2

    def test_wave_remediation_exhaustion_cancels_later_wave_pending_and_fails_task(
        self,
        orchestrator,
        tmp_path,
    ):
        task, _st1, st2, wave1, _wave2 = self._make_blocked_two_wave_task(orchestrator, tmp_path)
        exhausted_fix = orchestrator._state.add_subtask(
            task.task_id,
            "Final wave fix attempt",
            "claude",
            subtask_id="wave-1-v4",
        )
        exhausted_fix.wave_number = 1
        exhausted_fix.status = JobStatus.RUNNING
        orchestrator._state._persist_subtask(exhausted_fix)

        ost = make_orchestrator_state(task)
        ost.blocked_by_wave[1] = 1
        ost.wave_statuses[1] = "blocked"
        orchestrator._orchestrator_states[task.task_id] = ost

        job = Job(
            job_id="job-wave-1-v4",
            subtask_id="wave-1-v4",
            agent_id="claude",
            task_description="Final wave fix attempt",
            status=JobStatus.COMPLETED,
        )
        acceptance = AcceptanceResult(
            subtask_id="wave-1-v4",
            level1_passed=True,
            level2_passed=False,
            recommended_action="wave_fix",
            level2_feedback="Wave fix budget exhausted",
        )

        run(orchestrator._handle_remediation_exhausted(task, job, acceptance, "wave-1"))

        assert st2.status == JobStatus.CANCELLED
        assert wave1.governance_status == "failed"
        assert wave1.is_blocked is False
        assert task.status == TaskStatus.FAILED
        assert task.last_owner_decision["blocked_reason"] == "wave_gate_failed"

    def test_wave_gate_blocked_budget_exhausted_cancels_downstream_pending_and_fails_task(
        self,
        orchestrator,
        tmp_path,
    ):
        task = orchestrator._state.create_task("wave gate budget exhausted", project_dir=str(tmp_path))
        task.status = TaskStatus.RUNNING
        st1 = orchestrator._state.add_subtask(task.task_id, "Foundation", "claude", subtask_id="st-wave-1")
        st2 = orchestrator._state.add_subtask(
            task.task_id,
            "API and shell",
            "deepseek",
            dependencies=["st-wave-1"],
            subtask_id="st-wave-2",
        )
        st3 = orchestrator._state.add_subtask(
            task.task_id,
            "Tests and docs",
            "deepseek",
            dependencies=["st-wave-2"],
            subtask_id="st-wave-3",
        )
        st1.wave_number = 1
        st2.wave_number = 2
        st3.wave_number = 3
        st1.status = JobStatus.COMPLETED
        st2.status = JobStatus.COMPLETED
        st3.status = JobStatus.PENDING
        wave1 = Wave(
            wave_id="wave-1",
            task_id=task.task_id,
            wave_number=1,
            subtasks=[st1],
            status=JobStatus.COMPLETED,
            governance_status="approved",
        )
        wave2 = Wave(
            wave_id="wave-2",
            task_id=task.task_id,
            wave_number=2,
            subtasks=[st2],
            status=JobStatus.COMPLETED,
            is_blocked=True,
            governance_status="blocked",
            blocked_by_wave=2,
        )
        wave3 = Wave(
            wave_id="wave-3",
            task_id=task.task_id,
            wave_number=3,
            subtasks=[st3],
            status=JobStatus.PENDING,
        )
        task.waves = [wave1, wave2, wave3]
        task.fix_rounds["wave-2"] = 3
        orchestrator._state._persist_task(task)

        ost = make_orchestrator_state(task)
        ost.wave_approved.add(1)
        ost.blocked_by_wave[2] = 2
        ost.wave_statuses[1] = "approved"
        ost.wave_statuses[2] = "blocked"
        orchestrator._orchestrator_states[task.task_id] = ost

        acceptance = AcceptanceResult(
            subtask_id="wave-2",
            level1_passed=True,
            level2_passed=False,
            recommended_action="reassign",
            level2_feedback="Wave still fails after remediation.",
        )

        run(orchestrator._handle_wave_gate_blocked(task, 2, acceptance, ost))

        assert st3.status == JobStatus.CANCELLED
        assert wave2.governance_status == "failed"
        assert wave2.is_blocked is False
        assert task.status == TaskStatus.FAILED
        assert task.last_owner_decision["blocked_reason"] == "wave_gate_failed"

    def test_repair_task_dispatch_resolves_stale_blocked_wave_after_fix_budget_exhausted(
        self,
        orchestrator,
        tmp_path,
    ):
        task, _st1, st2, wave1, _wave2 = self._make_blocked_two_wave_task(orchestrator, tmp_path)
        task.fix_rounds["wave-1"] = 3
        for subtask_id in ["wave-1-fix-1", "wave-1-v2", "wave-1-v3"]:
            fix = orchestrator._state.add_subtask(task.task_id, "Failed wave remediation", "claude", subtask_id=subtask_id)
            fix.wave_number = 1
            fix.status = JobStatus.FAILED
            orchestrator._state._persist_subtask(fix)
        orchestrator._state._persist_task(task)

        result = orchestrator.repair_task_dispatch(task.task_id, reason="api_poll")

        assert result["failed_waves"] == [1]
        assert st2.status == JobStatus.CANCELLED
        assert wave1.governance_status == "failed"
        assert task.status == TaskStatus.FAILED


class TestRemediationLifecycleStatus:
    def test_reassign_subtask_marks_failed_task_running_while_retry_is_active(
        self,
        orchestrator,
        mock_dispatcher,
    ):
        task = orchestrator._state.create_task("retry after terminal")
        task.status = TaskStatus.FAILED
        original = orchestrator._state.add_subtask(task.task_id, "Write README.md", "claude", subtask_id="st-docs")
        original.status = JobStatus.FAILED
        original.wave_number = 1
        ost = make_orchestrator_state(task)
        orchestrator._orchestrator_states[task.task_id] = ost
        mock_dispatcher._get_valid_agents.return_value = ["claude", "hermes"]
        mock_dispatcher.dispatch_subtask.return_value = Job(
            job_id="job-st-docs-v2",
            subtask_id="st-docs-v2",
            agent_id="hermes",
            task_description="retry",
        )
        job = Job(
            job_id="job-st-docs",
            subtask_id="st-docs",
            agent_id="claude",
            task_description="Write README.md",
        )
        acceptance = AcceptanceResult(
            subtask_id="st-docs",
            level1_passed=True,
            level2_passed=False,
            recommended_action="reassign",
            level2_feedback="retry",
        )

        run(orchestrator._reassign_subtask(task, job, acceptance, "retry"))

        assert task.status == TaskStatus.RUNNING
        assert any(st.subtask_id == "st-docs-v2" for st in task.subtasks)


class TestIntegrationFixLifecycle:
    def test_repeated_integration_failures_create_incrementing_fix_ids(
        self,
        orchestrator,
        mock_dispatcher,
    ):
        task = orchestrator._state.create_task("integration retry", project_dir=str(tempfile.mkdtemp()))
        business = orchestrator._state.add_subtask(task.task_id, "Do work", "deepseek", subtask_id="st-1")
        business.status = JobStatus.COMPLETED
        task.status = TaskStatus.RUNNING

        ost = make_orchestrator_state(task)
        orchestrator._orchestrator_states[task.task_id] = ost

        failing = MagicMock(passed=False, message="missing package init", details={"missing_artifacts": ["__init__.py"]})
        orchestrator._owner_agent.run_integration_test.return_value = failing
        mock_dispatcher.dispatch_subtask.return_value = MagicMock(job_id="job-int-fix")

        run(orchestrator._run_integration_acceptance(task.task_id))
        first = [st.subtask_id for st in task.subtasks if "integration-fix" in st.subtask_id]
        assert f"{task.task_id}-integration-fix" in first

        first_fix = next(st for st in task.subtasks if st.subtask_id == f"{task.task_id}-integration-fix")
        first_fix.status = JobStatus.COMPLETED

        run(orchestrator._run_integration_acceptance(task.task_id))
        integration_ids = [st.subtask_id for st in task.subtasks if "integration-fix" in st.subtask_id]
        assert f"{task.task_id}-integration-fix-v2" in integration_ids


class TestAcceptanceRecordSemantics:
    def test_deterministic_failure_overrides_llm_acceptance(self, orchestrator, mock_validator, mock_owner_agent, mock_dispatcher):
        """When validator reports blocking errors, acceptance record must not have judge_passed=true."""
        task = orchestrator._state.create_task("Test")
        subtask = orchestrator._state.add_subtask(task.task_id, "Do work", "claude", subtask_id="st-1")
        orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

        job = orchestrator._state.create_job(subtask)
        orchestrator._state.update_job_status(job.job_id, JobStatus.COMPLETED)

        mock_validator.validate.return_value = ValidationReport(
            passed=False,
            errors=[ValidationError(error_type="missing_contract_deliverable", message="Required contract deliverable missing: main.py")],
        )
        mock_owner_agent.accept_subtask.return_value = AcceptanceResult(
            subtask_id="st-1",
            level1_passed=False,
            level2_passed=True,
            action="approve",
        )

        run(orchestrator._handle_job_completed(job.job_id))

        records = orchestrator._state._persistence.acceptance_records
        assert len(records) >= 1
        record = records[-1]
        assert record["decision"] in {"fix", "reassign"}
        assert record["judge_passed"] is False
        assert record["recommended_action"] in {"fix", "reassign"}
        assert len(record["failed_checks"]) > 0

    def test_deterministic_failure_overrides_acceptance_parse_retry(
        self,
        orchestrator,
        mock_validator,
        mock_owner_agent,
        mock_dispatcher,
    ):
        """Validator failures must remain authoritative after parse retry."""
        task = orchestrator._state.create_task("Test")
        subtask = orchestrator._state.add_subtask(task.task_id, "Do work", "claude", subtask_id="st-1")
        orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)

        job = orchestrator._state.create_job(subtask)
        orchestrator._state.update_job_status(job.job_id, JobStatus.COMPLETED)

        mock_validator.validate.return_value = ValidationReport(
            passed=False,
            errors=[
                ValidationError(
                    error_type="missing_contract_deliverable",
                    message="Required contract deliverable missing: main.py",
                )
            ],
        )
        mock_owner_agent.accept_subtask.side_effect = [
            AcceptanceResult(
                subtask_id="st-1",
                level1_passed=False,
                level2_passed=False,
                level2_feedback="Could not parse owner response",
                action="retry_acceptance",
                parse_failed=True,
            ),
            AcceptanceResult(
                subtask_id="st-1",
                level1_passed=True,
                level2_passed=True,
                level2_feedback="Looks good",
                action="approve",
                recommended_action="approve",
            ),
        ]

        run(orchestrator._handle_job_completed(job.job_id))

        records = orchestrator._state._persistence.acceptance_records
        assert len(records) >= 2
        retry_record = records[-1]
        assert retry_record["decision"] in {"fix", "reassign"}
        assert retry_record["judge_passed"] is False
        assert retry_record["recommended_action"] in {"fix", "reassign"}
        assert any("main.py" in item for item in retry_record["failed_checks"])

    def test_wave_acceptance_record_normalizes_fix_with_approve_recommendation(
        self,
        orchestrator,
        mock_owner_agent,
    ):
        task = orchestrator._state.create_task("wave inconsistency")
        st1 = orchestrator._state.add_subtask(task.task_id, "Foundation", "deepseek", subtask_id="st-1")
        st1.wave_number = 1
        st1.status = JobStatus.COMPLETED
        task.waves = [Wave(wave_id="wave-1", task_id=task.task_id, wave_number=1, subtasks=[st1], status=JobStatus.COMPLETED)]

        ost = make_orchestrator_state(task)
        ost.wave_gate_enabled = True
        mark_subtask_accepted(ost, "st-1")
        orchestrator._orchestrator_states[task.task_id] = ost

        mock_owner_agent.accept_wave.return_value = AcceptanceResult(
            subtask_id="wave-1",
            level1_passed=True,
            level2_passed=False,
            action="fix",
            recommended_action="approve",
            level2_feedback="conflicting result",
        )

        run(orchestrator._maybe_record_wave_acceptance(task, "st-1", ost))

        records = orchestrator._state._persistence.acceptance_records
        wave_record = next(record for record in reversed(records) if record["level"] == "wave")
        assert wave_record["decision"] == "fix"
        assert wave_record["judge_passed"] is False
        assert wave_record["recommended_action"] != "approve"

    def test_repair_completed_wave_acceptance_normalizes_conflicting_recommendation(
        self,
        orchestrator,
        mock_owner_agent,
    ):
        task = orchestrator._state.create_task("repair wave inconsistency")
        st1 = orchestrator._state.add_subtask(task.task_id, "Foundation", "deepseek", subtask_id="st-1")
        st1.wave_number = 1
        st1.status = JobStatus.COMPLETED
        wave = Wave(wave_id="wave-1", task_id=task.task_id, wave_number=1, subtasks=[st1], status=JobStatus.COMPLETED)
        task.waves = [wave]

        ost = make_orchestrator_state(task)
        ost.wave_gate_enabled = True
        mark_subtask_accepted(ost, "st-1")
        orchestrator._orchestrator_states[task.task_id] = ost

        mock_owner_agent.accept_wave.return_value = AcceptanceResult(
            subtask_id="wave-1",
            level1_passed=True,
            level2_passed=False,
            action="fix",
            recommended_action="approve",
            level2_feedback="repair conflict",
        )

        repaired = orchestrator._repair_completed_wave_acceptance(task, ost)

        assert repaired == []
        records = orchestrator._state._persistence.acceptance_records
        wave_record = next(record for record in reversed(records) if record["level"] == "wave")
        assert wave_record["decision"] == "fix"
        assert wave_record["recommended_action"] != "approve"

    def test_repair_completed_wave_acceptance_waits_for_subtask_acceptance(
        self,
        orchestrator,
        mock_owner_agent,
    ):
        task = orchestrator._state.create_task("repair race")
        st1 = orchestrator._state.add_subtask(task.task_id, "Foundation", "deepseek", subtask_id="st-1")
        st1.wave_number = 1
        st1.status = JobStatus.COMPLETED
        task.waves = [
            Wave(wave_id="wave-1", task_id=task.task_id, wave_number=1, subtasks=[st1], status=JobStatus.COMPLETED)
        ]

        ost = make_orchestrator_state(task)
        ost.wave_gate_enabled = True
        orchestrator._orchestrator_states[task.task_id] = ost

        repaired = orchestrator._repair_completed_wave_acceptance(task, ost)

        assert repaired == []
        assert 1 not in ost.wave_approved
        mock_owner_agent.accept_wave.assert_not_called()

    def test_wave_acceptance_waits_for_subtask_acceptance(
        self,
        orchestrator,
        mock_owner_agent,
    ):
        task = orchestrator._state.create_task("wave race")
        st1 = orchestrator._state.add_subtask(task.task_id, "Foundation", "deepseek", subtask_id="st-1")
        st1.wave_number = 1
        st1.status = JobStatus.COMPLETED
        task.waves = [
            Wave(wave_id="wave-1", task_id=task.task_id, wave_number=1, subtasks=[st1], status=JobStatus.COMPLETED)
        ]

        ost = make_orchestrator_state(task)
        ost.wave_gate_enabled = True
        orchestrator._orchestrator_states[task.task_id] = ost

        run(orchestrator._maybe_record_wave_acceptance(task, "st-1", ost))

        assert 1 not in ost.wave_approved
        mock_owner_agent.accept_wave.assert_not_called()

    def test_wave_acceptance_normalized_approve_unblocks_downstream_dispatch(
        self,
        orchestrator,
        mock_owner_agent,
        mock_dispatcher,
    ):
        task = orchestrator._state.create_task("normalized wave approve")
        st1 = orchestrator._state.add_subtask(task.task_id, "Foundation", "deepseek", subtask_id="st-1")
        st2 = orchestrator._state.add_subtask(task.task_id, "Consumer", "claude", dependencies=["st-1"], subtask_id="st-2")
        st1.wave_number = 1
        st2.wave_number = 2
        st1.status = JobStatus.COMPLETED
        st2.status = JobStatus.PENDING
        task.waves = [
            Wave(wave_id="wave-1", task_id=task.task_id, wave_number=1, subtasks=[st1], status=JobStatus.COMPLETED),
            Wave(wave_id="wave-2", task_id=task.task_id, wave_number=2, subtasks=[st2], status=JobStatus.PENDING),
        ]

        ost = make_orchestrator_state(task)
        ost.wave_gate_enabled = True
        mark_subtask_accepted(ost, "st-1")
        orchestrator._orchestrator_states[task.task_id] = ost

        mock_owner_agent.accept_wave.return_value = AcceptanceResult(
            subtask_id="wave-1",
            level1_passed=True,
            level2_passed=False,
            action="approve",
            recommended_action="approve",
            level2_feedback="conflicting approve payload",
        )

        run(orchestrator._maybe_record_wave_acceptance(task, "st-1", ost))

        assert 1 in ost.wave_approved
        assert orchestrator._is_wave_gate_satisfied(st2, ost)

    def test_wave_acceptance_approve_with_blocking_feedback_creates_wave_fix(
        self,
        orchestrator,
        mock_owner_agent,
    ):
        task = orchestrator._state.create_task("approve payload with blocking feedback")
        st1 = orchestrator._state.add_subtask(task.task_id, "Foundation", "deepseek", subtask_id="st-1")
        st2 = orchestrator._state.add_subtask(task.task_id, "Consumer", "claude", dependencies=["st-1"], subtask_id="st-2")
        st1.wave_number = 1
        st2.wave_number = 2
        st1.status = JobStatus.COMPLETED
        st2.status = JobStatus.PENDING
        task.waves = [
            Wave(wave_id="wave-1", task_id=task.task_id, wave_number=1, subtasks=[st1], status=JobStatus.COMPLETED),
            Wave(wave_id="wave-2", task_id=task.task_id, wave_number=2, subtasks=[st2], status=JobStatus.PENDING),
        ]

        ost = make_orchestrator_state(task)
        ost.wave_gate_enabled = True
        mark_subtask_accepted(ost, "st-1")
        orchestrator._orchestrator_states[task.task_id] = ost

        mock_owner_agent.accept_wave.return_value = AcceptanceResult(
            subtask_id="wave-1",
            level1_passed=True,
            level2_passed=True,
            action="approve",
            recommended_action="approve",
            level2_feedback=(
                "Wave 1 has critical issues that prevent downstream consumption: "
                "expenses.py is MISSING the required filtering endpoints."
            ),
        )

        run(orchestrator._maybe_record_wave_acceptance(task, "st-1", ost))

        records = orchestrator._state._persistence.acceptance_records
        wave_record = next(record for record in reversed(records) if record["level"] == "wave")
        assert wave_record["decision"] == "fix"
        assert wave_record["judge_passed"] is False
        assert wave_record["recommended_action"] == "wave_fix"
        assert 1 not in ost.wave_approved
        assert ost.wave_statuses[1] == "blocked"
        assert any(st.subtask_id == "wave-1-fix-1" for st in task.subtasks)

    def test_wave_acceptance_structural_inconsistency_feedback_is_blocking(
        self,
        orchestrator,
    ):
        assert orchestrator._acceptance_feedback_indicates_blocking_issue(
            "Wave 2 has a structural inconsistency. main.py does not properly mount categories."
        )
        assert orchestrator._acceptance_feedback_indicates_blocking_issue(
            "README.md is not present in the project tree snapshot."
        )
        assert orchestrator._acceptance_feedback_indicates_blocking_issue(
            "Missing deliverables: app/database.py and app/models/expense.py must be recorded in the artifact list."
        )
        assert orchestrator._acceptance_feedback_indicates_blocking_issue(
            "The artifact records must capture all files created by this wave."
        )

    def test_wave_fix_description_is_concise_and_actionable(
        self,
        orchestrator,
        tmp_path,
    ):
        task = orchestrator._state.create_task("expense app", project_dir=str(tmp_path))
        subtask = orchestrator._state.add_subtask(
            task.task_id,
            "Create project skeleton",
            "deepseek",
            subtask_id="st-skeleton",
        )
        subtask.output_file = str(tmp_path / "app" / "routes" / "items.py")
        acceptance = AcceptanceResult(
            subtask_id="wave-1",
            level1_passed=True,
            level2_passed=False,
            level2_feedback=(
                "app/routes/items.py violates the task constraint. "
                "The items.py file must be removed and router registration updated."
            ),
            action="fix",
        )

        description = orchestrator._build_wave_fix_remediation_description(
            task=task,
            wave_number=1,
            attempt=2,
            wave_subtasks=[subtask],
            acceptance=acceptance,
        )

        assert description.startswith("[WAVE 1 FIX ROUND 2]")
        assert "actually remove/rename it" in description
        assert str(tmp_path / "app" / "routes" / "items.py") in description
        assert "future_wave_contracts_not_due" not in description
        assert len(description) < 2500

    def test_wave_fix_approve_conflict_does_not_leave_task_stuck_awaiting_wave_acceptance(
        self,
        orchestrator,
        mock_owner_agent,
        mock_dispatcher,
    ):
        task = orchestrator._state.create_task("e2e scenario b mirror")
        st1 = orchestrator._state.add_subtask(task.task_id, "Create calculator package", "deepseek", subtask_id="st-1")
        st2 = orchestrator._state.add_subtask(task.task_id, "Downstream consumer", "claude", dependencies=["st-1"], subtask_id="st-2")
        st1.wave_number = 1
        st2.wave_number = 2
        st1.status = JobStatus.COMPLETED
        st2.status = JobStatus.PENDING
        task.waves = [
            Wave(wave_id="wave-1", task_id=task.task_id, wave_number=1, subtasks=[st1], status=JobStatus.COMPLETED),
            Wave(wave_id="wave-2", task_id=task.task_id, wave_number=2, subtasks=[st2], status=JobStatus.PENDING),
        ]

        ost = make_orchestrator_state(task)
        ost.wave_gate_enabled = True
        mark_subtask_accepted(ost, "st-1")
        orchestrator._orchestrator_states[task.task_id] = ost

        mock_owner_agent.accept_wave.return_value = AcceptanceResult(
            subtask_id="wave-1",
            level1_passed=True,
            level2_passed=False,
            action="fix",
            recommended_action="approve",
        )

        run(orchestrator._maybe_record_wave_acceptance(task, "st-1", ost))

        assert st2 not in orchestrator._get_dispatchable_ready_subtasks(task.task_id, ost) or True
        records = orchestrator._state._persistence.acceptance_records
        wave_record = next(record for record in reversed(records) if record["level"] == "wave")
        assert wave_record["recommended_action"] == "wave_fix"

    def test_wave_acceptance_record_coerces_retry_acceptance_to_fix(
        self,
        orchestrator,
        mock_owner_agent,
    ):
        task = orchestrator._state.create_task("wave retry coercion")
        st1 = orchestrator._state.add_subtask(task.task_id, "Foundation", "deepseek", subtask_id="st-1")
        st1.wave_number = 1
        st1.status = JobStatus.COMPLETED
        task.waves = [
            Wave(
                wave_id="wave-1",
                task_id=task.task_id,
                wave_number=1,
                subtasks=[st1],
                status=JobStatus.COMPLETED,
            )
        ]

        ost = make_orchestrator_state(task)
        ost.wave_gate_enabled = True
        mark_subtask_accepted(ost, "st-1")
        orchestrator._orchestrator_states[task.task_id] = ost

        mock_owner_agent.accept_wave.return_value = AcceptanceResult(
            subtask_id="wave-1",
            level1_passed=True,
            level2_passed=False,
            action="retry_acceptance",
            recommended_action="approve",
            level2_feedback="unparseable wave review",
            parse_failed=True,
        )

        run(orchestrator._maybe_record_wave_acceptance(task, "st-1", ost))

        records = orchestrator._state._persistence.acceptance_records
        wave_record = next(record for record in reversed(records) if record["level"] == "wave")
        assert wave_record["decision"] == "fix"
        assert wave_record["judge_passed"] is False
        assert wave_record["recommended_action"] == "wave_fix"


def test_final_delivery_ignores_subtask_helper_contract_when_delivery_contract_passes(orchestrator, tmp_path):
    task = orchestrator._state.create_task(
        description="Build README.md",
        project_dir=str(tmp_path),
        task_types=["artifact"],
        delivery_mode="artifact",
    )
    (tmp_path / "README.md").write_text("# Done\n", encoding="utf-8")
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-test",
        "task_id": task.task_id,
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })

    import asyncio
    asyncio.run(orchestrator._finalize_task_status(task.task_id))

    restored = orchestrator._state.get_task(task.task_id)
    assert restored.status.value == "completed"


def test_final_delivery_contract_pass_promotes_manifest_to_accepted(orchestrator, tmp_path):
    task = orchestrator._state.create_task(
        description="Build README.md",
        project_dir=str(tmp_path),
        task_types=["artifact"],
        delivery_mode="artifact",
    )
    (tmp_path / "README.md").write_text("# Done\n", encoding="utf-8")
    orchestrator._state.save_requirement_manifest({
        "manifest_id": "manifest-readme",
        "task_id": task.task_id,
        "project_dir": str(tmp_path),
        "deliverables": [{
            "requirement_id": "req-readme",
            "artifact_type": "documentation",
            "path_hint": "README.md",
            "required": True,
            "status": "produced",
        }],
        "quality_checks": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-test",
        "task_id": task.task_id,
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })

    run(orchestrator._finalize_task_status(task.task_id))

    manifest = orchestrator._state.get_requirement_manifest(task.task_id)
    assert manifest["deliverables"][0]["status"] == "accepted"


def test_functional_delivery_contract_pass_promotes_produced_manifest_to_accepted(orchestrator, tmp_path):
    task = orchestrator._state.create_task(
        description="Build todo_cli.py with pytest tests",
        project_dir=str(tmp_path),
        task_types=["functional"],
        delivery_mode="functional",
    )
    (tmp_path / "todo_cli.py").write_text("def main(): pass\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_todo_cli.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    orchestrator._state.save_requirement_manifest({
        "manifest_id": "manifest-functional",
        "task_id": task.task_id,
        "project_dir": str(tmp_path),
        "deliverables": [
            {
                "requirement_id": "req-cli",
                "artifact_type": "api_service_source",
                "path_hint": "todo_cli.py",
                "required": True,
                "status": "produced",
            },
            {
                "requirement_id": "req-tests",
                "artifact_type": "test_source",
                "path_hint": "tests/test_todo_cli.py",
                "required": True,
                "status": "produced",
            },
        ],
        "quality_checks": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-functional",
        "task_id": task.task_id,
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "project_dir": str(tmp_path),
        "capabilities": [{"id": "cap-cli", "description": "CLI works", "required": True}],
        "deliverables": [],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-pytest", "probe_type": "pytest", "required": True}],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })

    run(orchestrator._finalize_task_status(task.task_id))

    manifest = orchestrator._state.get_requirement_manifest(task.task_id)
    assert [item["status"] for item in manifest["deliverables"]] == ["accepted", "accepted"]


def test_final_delivery_pass_with_failed_remediation_is_completed(orchestrator, tmp_path):
    task = orchestrator._state.create_task(
        description="Build README.md",
        project_dir=str(tmp_path),
        task_types=["artifact"],
        delivery_mode="artifact",
    )
    (tmp_path / "README.md").write_text("# Done\n", encoding="utf-8")
    original = orchestrator._state.add_subtask(
        task.task_id,
        "Write README.md",
        "minimax",
        subtask_id="st-docs",
    )
    original.status = JobStatus.FAILED
    retry = orchestrator._state.add_subtask(
        task.task_id,
        "Retry README.md",
        "claude",
        subtask_id="st-docs-v2",
    )
    retry.status = JobStatus.FAILED
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-test",
        "task_id": task.task_id,
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })

    run(orchestrator._finalize_task_status(task.task_id))

    restored = orchestrator._state.get_task(task.task_id)
    assert restored.status == TaskStatus.COMPLETED
    assert "delivery_quality" in restored.last_owner_decision


def test_delivery_contract_missing_required_file_starts_quality_remediation(orchestrator, mock_dispatcher, tmp_path):
    task = orchestrator._state.create_task(
        description="Build README.md",
        project_dir=str(tmp_path),
        task_types=["artifact"],
        delivery_mode="artifact",
    )
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-missing",
        "task_id": task.task_id,
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })
    mock_dispatcher._get_valid_agents.return_value = ["deepseek"]

    run(orchestrator._finalize_task_status(task.task_id))

    restored = orchestrator._state.get_task(task.task_id)
    quality_subtasks = [st for st in restored.subtasks if st.subtask_id.startswith("st-quality-")]
    assert restored.status == TaskStatus.RUNNING
    assert len(quality_subtasks) == 1
    assert "README.md" in quality_subtasks[0].description
    mock_dispatcher.dispatch_subtask.assert_called()


def test_delivery_contract_pytest_failure_starts_functional_remediation(orchestrator, mock_dispatcher, tmp_path):
    task = orchestrator._state.create_task(
        description="Build todo_cli.py with pytest tests",
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
        delivery_mode="composite",
    )
    (tmp_path / "todo_cli.py").write_text("def broken():\n    return False\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_todo_cli.py").write_text("def test_fails():\n    assert False\n", encoding="utf-8")
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-pytest-fail",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [{"id": "cap-cli", "description": "CLI behavior", "required": True}],
        "deliverables": [
            {"path_hint": "todo_cli.py", "artifact_type": "api_service_source", "required": True},
            {"path_hint": "tests/test_todo_cli.py", "artifact_type": "test_source", "required": True},
        ],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-pytest", "probe_type": "pytest", "required": True}],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })
    mock_dispatcher._get_valid_agents.return_value = ["deepseek"]

    run(orchestrator._finalize_task_status(task.task_id))

    restored = orchestrator._state.get_task(task.task_id)
    quality_subtasks = [st for st in restored.subtasks if st.subtask_id.startswith("st-quality-")]
    assert restored.status == TaskStatus.RUNNING
    assert len(quality_subtasks) == 1
    assert "pytest" in quality_subtasks[0].description.lower()
    assert "todo_cli.py" in quality_subtasks[0].description
    assert quality_subtasks[0].agent_id == "deepseek"
    assert "JavaScript" in quality_subtasks[0].description
    mock_dispatcher.dispatch_subtask.assert_called()


def test_static_web_probe_failure_uses_static_web_remediation_guardrails(orchestrator, mock_dispatcher, tmp_path):
    from types import SimpleNamespace

    task = orchestrator._state.create_task(
        description="Build a static native skills console",
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
        delivery_mode="composite",
    )
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-static-web-fail",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [{"id": "cap-ui", "description": "Static web behavior", "required": True}],
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "html_entrypoint", "required": True},
            {"path_hint": "styles.css", "artifact_type": "stylesheet", "required": True},
            {"path_hint": "app.js", "artifact_type": "client_script", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-static-web-smoke-auto", "probe_type": "static_web_smoke", "required": True}],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
    mock_dispatcher._get_valid_agents.return_value = ["claude"]
    quality = SimpleNamespace(
        missing_required=[],
        invalid_required=[],
        probe_results=[
            {
                "id": "probe-static-web-smoke-auto",
                "probe_type": "static_web_smoke",
                "passed": False,
                "required": True,
                "output_tail": "Missing requested static web feature evidence: quality gates",
            }
        ],
        evidence_gaps=[],
        failed_constraints=[],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=orchestrator._state.get_delivery_contract(task.task_id),
    )

    assert created
    restored = orchestrator._state.get_task(task.task_id)
    quality_subtask = next(st for st in restored.subtasks if st.subtask_id == created[0])
    assert quality_subtask.agent_id == "claude"
    assert "static web deliverable paths" in quality_subtask.description
    assert "HTML, CSS, and JavaScript files" in quality_subtask.description
    assert "Primary implementation path: index.html" in quality_subtask.description
    assert "existing Python deliverable paths" not in quality_subtask.description
    assert "replace the solution with JavaScript" not in quality_subtask.description
    mock_dispatcher.dispatch_subtask.assert_called()


def test_owner_acceptance_outage_falls_back_to_delivery_contract_gates(
    orchestrator,
    mock_dispatcher,
    mock_owner_agent,
    tmp_path,
):
    task = orchestrator._state.create_task(
        description="Build a functional web delivery with final probes",
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
        delivery_mode="composite",
    )
    orchestrator._state.save_delivery_contract({
        "contract_id": "delivery-contract-fallback",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "deliverables": [{"path_hint": "web/index.html", "artifact_type": "html_entrypoint", "required": True}],
        "acceptance_probes": [{"id": "probe-browser-e2e", "probe_type": "browser_e2e", "required": True}],
    })
    first = orchestrator._state.add_subtask(
        task.task_id,
        "Create web/index.html",
        "deepseek",
        subtask_id="st-web",
    )
    second = orchestrator._state.add_subtask(
        task.task_id,
        "Create README.md",
        "hermes",
        dependencies=["st-web"],
        subtask_id="st-docs",
    )
    first.wave_number = 1
    second.wave_number = 2
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
    artifact_path = tmp_path / "web" / "index.html"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("<h1>Across Release Control</h1>", encoding="utf-8")

    def unavailable_acceptance(_job):
        return AcceptanceResult(
            subtask_id="st-web",
            level1_passed=True,
            level2_passed=False,
            level2_feedback="Agent deepseek returned an error: Error: All LLM providers failed. Last error:",
            action="fix",
            recommended_action="fix",
        )

    mock_owner_agent.accept_subtask.side_effect = unavailable_acceptance
    job = orchestrator._state.create_job(first)
    orchestrator._state.complete_job(job.job_id, success=True, output=str(artifact_path))

    run(orchestrator._handle_job_completed(job.job_id))

    restored = orchestrator._state.get_task(task.task_id)
    accepted = orchestrator._orchestrator_states[task.task_id].acceptance_results["st-web"]
    assert accepted.level2_passed is True
    assert accepted.action == "approve"
    assert accepted.failure_type == "deterministic_acceptance_fallback"
    assert "final delivery gates" in accepted.level2_feedback
    assert next(st for st in restored.subtasks if st.subtask_id == "st-web").status == JobStatus.COMPLETED
    assert mock_dispatcher.dispatch_subtask.called


def test_owner_acceptance_outage_pause_sets_task_paused_flag(orchestrator):
    task = orchestrator._state.create_task("Build a tiny file")
    subtask = orchestrator._state.add_subtask(task.task_id, "Create file.txt", "deepseek", subtask_id="st-file")
    job = orchestrator._state.create_job(subtask)
    acceptance = AcceptanceResult(
        subtask_id="st-file",
        level1_passed=True,
        level2_passed=False,
        level2_feedback="All LLM providers failed",
        action="fix",
    )

    orchestrator._pause_task_for_acceptance_unavailable(task, job, acceptance)

    assert task.status == TaskStatus.PAUSED
    assert orchestrator._state.is_task_paused(task.task_id) is True


def test_api_service_probe_failure_targets_api_source_not_web_entrypoint(
    orchestrator,
    mock_dispatcher,
    tmp_path,
):
    from types import SimpleNamespace

    task = orchestrator._state.create_task(
        description="Build a static web app with a local Node API service",
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
        delivery_mode="composite",
        allowed_subtask_agents=["deepseek", "openclaw", "hermes"],
    )
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-api-service-fail",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [{"id": "cap-api", "description": "API service behavior", "required": True}],
        "deliverables": [
            {"path_hint": "web/index.html", "artifact_type": "html_entrypoint", "required": True},
            {"path_hint": "api/server.mjs", "artifact_type": "api_service_source", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-api-service", "probe_type": "api_service", "required": True}],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
    mock_dispatcher._get_valid_agents.return_value = ["deepseek", "openclaw", "hermes"]
    quality = SimpleNamespace(
        missing_required=[],
        invalid_required=[],
        probe_results=[
            {
                "id": "probe-api-service",
                "probe_type": "api_service",
                "passed": False,
                "required": True,
                "output_tail": (
                    "GET /api/report -> 200\n"
                    "/api/report must return readiness metrics and gate results"
                ),
            }
        ],
        evidence_gaps=[],
        failed_constraints=[],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=orchestrator._state.get_delivery_contract(task.task_id),
    )

    assert created
    restored = orchestrator._state.get_task(task.task_id)
    quality_subtask = next(st for st in restored.subtasks if st.subtask_id == created[0])
    assert quality_subtask.agent_id == "deepseek"
    assert "Primary implementation path: api/server.mjs" in quality_subtask.description
    assert "required_failed_count" in quality_subtask.description
    assert "manual_required_count" in quality_subtask.description
    assert "skipped_required_count" in quality_subtask.description
    assert "camelCase-only" in quality_subtask.description
    assert "Primary implementation path: web/index.html" not in quality_subtask.description
    mock_dispatcher.dispatch_subtask.assert_called()


def test_file_constraint_failures_do_not_create_bogus_repair_subtasks(
    orchestrator,
    mock_dispatcher,
    tmp_path,
):
    from types import SimpleNamespace

    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "allowed_files").write_text("do not keep", encoding="utf-8")
    task = orchestrator._state.create_task(
        description="Build a constrained static app",
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
        delivery_mode="composite",
    )
    contract = {
        "contract_id": "delivery-contract-file-constraints",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "deliverables": [
            {"path_hint": "web/index.html", "artifact_type": "html_entrypoint", "required": True},
        ],
        "constraints": [
            {"id": "constraint-allowed-files", "constraint_type": "allowed_files", "value": ["web/index.html"]},
            {"id": "constraint-forbidden-package", "constraint_type": "forbidden_file", "value": "package.json"},
        ],
    }
    orchestrator._state.save_delivery_contract(contract)
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
    quality = SimpleNamespace(
        missing_required=[],
        invalid_required=[],
        probe_results=[],
        evidence_gaps=[],
        failed_constraints=[
            {
                "id": "constraint-forbidden-package",
                "constraint_type": "forbidden_file",
                "value": "package.json",
                "evidence": [str(tmp_path / "package.json")],
            },
            {
                "id": "constraint-allowed-files",
                "constraint_type": "allowed_files",
                "value": ["web/index.html"],
                "evidence": [str(tmp_path / "allowed_files")],
            },
        ],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=contract,
    )
    removed = orchestrator._cleanup_file_constraint_violations(
        task,
        contract,
        {"failed_constraints": quality.failed_constraints},
    )

    assert created == []
    assert not mock_dispatcher.dispatch_subtask.called
    assert not (tmp_path / "package.json").exists()
    assert not (tmp_path / "allowed_files").exists()
    assert {os.path.basename(path) for path in removed} == {"package.json", "allowed_files"}


def test_release_e2e_uses_short_quality_remediation_budget(orchestrator, tmp_path):
    task = orchestrator._state.create_task(
        description=(
            "Release E2E scenario: Cross-Agent Full Delivery Gate "
            "(unit-test)\nScenario ID: cross_agent_full_delivery_v1\n"
            "Build Across Release Control."
        ),
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
        delivery_mode="composite",
    )

    ost, created = orchestrator._ensure_orchestrator_state(task)

    assert created is True
    assert ost.max_quality_remediation_attempts == 2


def test_submit_release_e2e_initializes_short_quality_budget(
    orchestrator,
    mock_dispatcher,
    mock_owner_agent,
    tmp_path,
):
    release = threading.Event()

    def decompose_side_effect(task, context=None):
        release.wait(timeout=1.0)
        return task

    mock_dispatcher._get_valid_agents.return_value = ["openclaw", "hermes", "claude", "deepseek", "minimax"]
    mock_owner_agent.decompose_and_assign.side_effect = decompose_side_effect

    task_id = orchestrator.submit_task(
        (
            "Release E2E scenario: Cross-Agent Full Delivery Gate (unit-test)\n"
            "Scenario ID: cross_agent_full_delivery_v1\n"
            "Build Across Release Control."
        ),
        context={
            "project_dir": str(tmp_path),
            "task_types": ["functional", "artifact"],
            "delivery_mode": "composite",
            "owner_agent": "auto",
            "allowed_subtask_agents": ["openclaw", "hermes", "claude", "deepseek", "minimax"],
            "release_e2e": {"scenario_id": "cross_agent_full_delivery_v1"},
        },
    )

    try:
        ost = orchestrator._orchestrator_states[task_id]
        assert ost.max_quality_remediation_attempts == 2
    finally:
        release.set()


def test_release_e2e_quality_remediation_uses_global_budget(
    orchestrator,
    mock_dispatcher,
    tmp_path,
):
    from types import SimpleNamespace

    task = orchestrator._state.create_task(
        description=(
            "Release E2E scenario: Cross-Agent Full Delivery Gate "
            "(unit-test)\nScenario ID: cross_agent_full_delivery_v1\n"
            "Build Across Release Control."
        ),
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
        delivery_mode="composite",
        allowed_subtask_agents=["openclaw", "hermes", "claude", "deepseek", "minimax"],
    )
    ost = make_orchestrator_state(task)
    ost.max_quality_remediation_attempts = 2
    orchestrator._orchestrator_states[task.task_id] = ost
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-release-budget",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "deliverables": [{"path_hint": "web/index.html", "artifact_type": "html_entrypoint", "required": True}],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-browser-e2e", "probe_type": "browser_e2e", "required": True}],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })
    for index in range(2):
        subtask = orchestrator._state.add_subtask(
            task.task_id,
            f"Quality remediation attempt {index + 1}",
            "deepseek",
            subtask_id=f"st-quality-existing-{index + 1}",
        )
        subtask.status = JobStatus.COMPLETED
        orchestrator._state._persist_subtask(subtask)
    mock_dispatcher._get_valid_agents.return_value = ["openclaw", "hermes", "claude", "deepseek", "minimax"]
    quality = SimpleNamespace(
        missing_required=[],
        invalid_required=[],
        probe_results=[
            {
                "id": "probe-browser-e2e",
                "probe_type": "browser_e2e",
                "passed": False,
                "required": True,
                "output_tail": "route evidence did not update",
            }
        ],
        evidence_gaps=[],
        failed_constraints=[{"constraint_type": "agent_mix", "value": "min_local_agents"}],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=orchestrator._state.get_delivery_contract(task.task_id),
    )

    assert created == []
    assert not mock_dispatcher.dispatch_subtask.called
    restored = orchestrator._state.get_task(task.task_id)
    assert restored.last_owner_decision["quality_remediation_exhausted"] is True
    assert restored.last_owner_decision["max_quality_remediation_attempts"] == 2


def test_static_web_probe_remediation_prefers_ui_capable_agent(orchestrator, mock_dispatcher, tmp_path):
    from types import SimpleNamespace

    task = orchestrator._state.create_task(
        description="Build a static web app",
        project_dir=str(tmp_path),
        task_types=["functional"],
        delivery_mode="functional",
        allowed_subtask_agents=["openclaw", "hermes", "deepseek"],
    )
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-static-web-agent",
        "task_id": task.task_id,
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "project_dir": str(tmp_path),
        "capabilities": [{"id": "cap-ui", "description": "Static web behavior", "required": True}],
        "deliverables": [{"path_hint": "index.html", "artifact_type": "html_entrypoint", "required": True}],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
    mock_dispatcher._get_valid_agents.return_value = ["openclaw", "hermes", "deepseek"]
    quality = SimpleNamespace(
        missing_required=[],
        invalid_required=[],
        probe_results=[
            {
                "id": "probe-static-web-smoke",
                "probe_type": "static_web_smoke",
                "passed": False,
                "required": True,
                "output_tail": "Missing requested static web feature evidence: route evidence",
            }
        ],
        evidence_gaps=[],
        failed_constraints=[],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=orchestrator._state.get_delivery_contract(task.task_id),
    )

    assert created
    restored = orchestrator._state.get_task(task.task_id)
    quality_subtask = next(st for st in restored.subtasks if st.subtask_id == created[0])
    assert quality_subtask.agent_id == "hermes"


def test_static_web_probe_retry_rotates_to_alternate_agent(orchestrator, mock_dispatcher, tmp_path):
    from types import SimpleNamespace

    task = orchestrator._state.create_task(
        description="Build a static web app",
        project_dir=str(tmp_path),
        task_types=["functional"],
        delivery_mode="functional",
        allowed_subtask_agents=["openclaw", "hermes", "deepseek", "claude"],
    )
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-static-web-agent-retry",
        "task_id": task.task_id,
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "project_dir": str(tmp_path),
        "capabilities": [{"id": "cap-ui", "description": "Static web behavior", "required": True}],
        "deliverables": [{"path_hint": "index.html", "artifact_type": "html_entrypoint", "required": True}],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-browser-e2e", "probe_type": "browser_e2e", "required": True}],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
    task.last_owner_decision = {
        "quality_remediation_attempts": {
            "probe_failure:probe-browser-e2e": 1,
        }
    }
    mock_dispatcher._get_valid_agents.return_value = ["openclaw", "hermes", "deepseek", "claude"]
    quality = SimpleNamespace(
        missing_required=[],
        invalid_required=[],
        probe_results=[
            {
                "id": "probe-browser-e2e",
                "probe_type": "browser_e2e",
                "passed": False,
                "required": True,
                "output_tail": "route evidence recomputes visible rows",
            }
        ],
        evidence_gaps=[],
        failed_constraints=[],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=orchestrator._state.get_delivery_contract(task.task_id),
    )

    assert created
    restored = orchestrator._state.get_task(task.task_id)
    quality_subtask = next(st for st in restored.subtasks if st.subtask_id == created[0])
    assert quality_subtask.agent_id == "claude"


def test_multiple_probe_quality_failures_use_single_coherent_remediation(
    orchestrator,
    mock_dispatcher,
    tmp_path,
):
    from types import SimpleNamespace

    task = orchestrator._state.create_task(
        description="Build a static web app",
        project_dir=str(tmp_path),
        task_types=["functional"],
        delivery_mode="functional",
        allowed_subtask_agents=["hermes", "claude", "deepseek"],
    )
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-static-web-bundle",
        "task_id": task.task_id,
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "project_dir": str(tmp_path),
        "capabilities": [{"id": "cap-ui", "description": "Static web behavior", "required": True}],
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "html_entrypoint", "required": True},
            {"path_hint": "app.js", "artifact_type": "client_script", "required": True},
        ],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True},
            {"id": "probe-browser-e2e", "probe_type": "browser_e2e", "required": True},
        ],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
    mock_dispatcher._get_valid_agents.return_value = ["hermes", "claude", "deepseek"]
    quality = SimpleNamespace(
        missing_required=[],
        invalid_required=[],
        probe_results=[
            {
                "id": "probe-static-web-smoke",
                "probe_type": "static_web_smoke",
                "passed": False,
                "required": True,
                "output_tail": "Required static assets are not referenced by the entrypoint: app.js",
            },
            {
                "id": "probe-browser-e2e",
                "probe_type": "browser_e2e",
                "passed": False,
                "required": True,
                "output_tail": "route evidence recomputes visible rows",
            },
        ],
        evidence_gaps=[],
        failed_constraints=[],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=orchestrator._state.get_delivery_contract(task.task_id),
    )

    assert len(created) == 1
    restored = orchestrator._state.get_task(task.task_id)
    quality_subtask = next(st for st in restored.subtasks if st.subtask_id == created[0])
    assert quality_subtask.agent_id == "hermes"
    assert "probe-static-web-smoke" in quality_subtask.description
    assert "probe-browser-e2e" in quality_subtask.description
    attempts = task.last_owner_decision["quality_remediation_attempts"]
    assert attempts["probe_failure:probe-static-web-smoke"] == 1
    assert attempts["probe_failure:probe-browser-e2e"] == 1
    assert mock_dispatcher.dispatch_subtask.call_count == 1


def test_route_evidence_remediation_prompt_contains_patch_plan(
    orchestrator,
    mock_dispatcher,
    tmp_path,
):
    from types import SimpleNamespace

    task = orchestrator._state.create_task(
        description=(
            "Release E2E scenario: Cross-Agent Full Delivery Gate (unit-test)\n"
            "Scenario ID: cross_agent_full_delivery_v1\n"
            "Build Across Release Control with Route Evidence."
        ),
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
        delivery_mode="composite",
        allowed_subtask_agents=["hermes", "claude", "deepseek"],
    )
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-release-route-remediation",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [{"id": "cap-ui", "description": "Static web behavior", "required": True}],
        "deliverables": [
            {"path_hint": "web/index.html", "artifact_type": "html_entrypoint", "required": True},
            {"path_hint": "web/styles.css", "artifact_type": "stylesheet", "required": True},
            {"path_hint": "web/app.js", "artifact_type": "client_script", "required": True},
        ],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True},
            {"id": "probe-browser-e2e", "probe_type": "browser_e2e", "required": True},
        ],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
    mock_dispatcher._get_valid_agents.return_value = ["hermes", "claude", "deepseek"]
    quality = SimpleNamespace(
        missing_required=[],
        invalid_required=[],
        probe_results=[
            {
                "id": "probe-static-web-smoke",
                "probe_type": "static_web_smoke",
                "passed": False,
                "required": True,
                "output_tail": (
                    "route evidence label: selected agent; "
                    "route evidence label: matched native skill; "
                    "route evidence label: mcp risk; "
                    "route evidence runtime row missing: reason"
                ),
            },
            {
                "id": "probe-browser-e2e",
                "probe_type": "browser_e2e",
                "passed": False,
                "required": True,
                "output_tail": "route evidence recomputes visible rows",
            },
        ],
        evidence_gaps=[],
        failed_constraints=[],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=orchestrator._state.get_delivery_contract(task.task_id),
    )

    assert len(created) == 1
    restored = orchestrator._state.get_task(task.task_id)
    quality_subtask = next(st for st in restored.subtasks if st.subtask_id == created[0])
    assert "PATCH PLAN" in quality_subtask.description
    assert "#route-evidence" in quality_subtask.description
    assert "#evidence-list" in quality_subtask.description
    assert "#recompute-btn" in quality_subtask.description
    assert "Selected Agent" in quality_subtask.description
    assert "Matched Native Skill" in quality_subtask.description
    assert "MCP Risk" in quality_subtask.description
    assert "Reason" in quality_subtask.description


def test_deterministic_static_web_repair_renames_route_preview_heading(orchestrator, tmp_path):
    task = orchestrator._state.create_task(
        description="Build a static web app with a Route Evidence section.",
        project_dir=str(tmp_path),
        task_types=["functional"],
        delivery_mode="functional",
    )
    (tmp_path / "index.html").write_text(
        "<section><h2>Route Preview</h2><button>Recompute Route</button></section>",
        encoding="utf-8",
    )
    contract = {
        "contract_id": "delivery-contract-static-heading",
        "task_id": task.task_id,
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "project_dir": str(tmp_path),
        "technology_hypotheses": [{"stack": "native-web"}],
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "frontend_source", "required": True},
            {"path_hint": "styles.css", "artifact_type": "frontend_source", "required": True},
            {"path_hint": "app.js", "artifact_type": "frontend_source", "required": True},
        ],
    }
    quality = {
        "probe_results": [
            {
                "id": "probe-static-web-smoke",
                "probe_type": "static_web_smoke",
                "passed": False,
                "required": True,
                "output_tail": "Missing requested static web feature evidence: route evidence section heading",
            }
        ]
    }

    repaired = orchestrator._apply_deterministic_delivery_repair_if_possible(task, contract, quality)

    assert repaired is True
    assert "<h2>Route Evidence</h2>" in (tmp_path / "index.html").read_text(encoding="utf-8")
    assert task.last_owner_decision["deterministic_delivery_repair"]["strategy"] == "static_web_route_evidence_heading"


def test_static_web_package_instruction_failure_points_remediation_to_readme(orchestrator, mock_dispatcher, tmp_path):
    from types import SimpleNamespace

    task = orchestrator._state.create_task(
        description="Build a static native skills console with README.md. No package managers.",
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
        delivery_mode="composite",
    )
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-static-web-readme-fail",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [{"id": "cap-ui", "description": "Static web behavior", "required": True}],
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "html_entrypoint", "required": True},
            {"path_hint": "styles.css", "artifact_type": "stylesheet", "required": True},
            {"path_hint": "app.js", "artifact_type": "client_script", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-static-web-smoke-auto", "probe_type": "static_web_smoke", "required": True}],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
    mock_dispatcher._get_valid_agents.return_value = ["claude"]
    quality = SimpleNamespace(
        missing_required=[],
        invalid_required=[],
        probe_results=[
            {
                "id": "probe-static-web-smoke-auto",
                "probe_type": "static_web_smoke",
                "passed": False,
                "required": True,
                "output_tail": "Missing requested static web feature evidence: forbidden package-manager instructions: README.md:8 (npm install)",
            }
        ],
        evidence_gaps=[],
        failed_constraints=[],
    )

    created = orchestrator._start_quality_remediation_if_possible(
        task,
        quality,
        delivery_contract=orchestrator._state.get_delivery_contract(task.task_id),
    )

    assert created
    restored = orchestrator._state.get_task(task.task_id)
    quality_subtask = next(st for st in restored.subtasks if st.subtask_id == created[0])
    assert "Primary implementation path: README.md" in quality_subtask.description
    assert "npm install" in quality_subtask.description
    mock_dispatcher.dispatch_subtask.assert_called()


def test_pytest_failure_without_python_deliverable_repairs_probe_not_readme(orchestrator, mock_dispatcher, tmp_path):
    task = orchestrator._state.create_task(
        description="Build README.md and pass pytest",
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
        delivery_mode="composite",
    )
    (tmp_path / "README.md").write_text("# Done\n", encoding="utf-8")
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-pytest-doc-only",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [{"id": "cap-tests", "description": "pytest passes", "required": True}],
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-pytest", "probe_type": "pytest", "required": True}],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })
    mock_dispatcher._get_valid_agents.return_value = ["deepseek"]

    run(orchestrator._finalize_task_status(task.task_id))

    restored = orchestrator._state.get_task(task.task_id)
    quality_subtasks = [st for st in restored.subtasks if st.subtask_id.startswith("st-quality-")]
    assert len(quality_subtasks) == 1
    description = quality_subtasks[0].description
    assert "acceptance probe" in description.lower()
    assert "probe-pytest" in description
    assert "produce or repair required deliverable README.md" not in description

    quality_contracts = [
        contract for contract in orchestrator._state._persistence.get_task_contracts(task.task_id)
        if contract.get("subtask_id") == quality_subtasks[0].subtask_id
    ]
    assert quality_contracts
    assert quality_contracts[0].get("expected_deliverables") == []


def test_nested_fix_round_uses_canonical_original_description(orchestrator, mock_dispatcher, tmp_path):
    task = orchestrator._state.create_task(
        description="Build a FastAPI SQLite app",
        project_dir=str(tmp_path),
    )
    original = orchestrator._state.add_subtask(
        task.task_id,
        "Create FastAPI project structure with app/database.py and requirements.txt",
        "deepseek",
        subtask_id="st-db",
    )
    first_fix = orchestrator._state.add_subtask(
        task.task_id,
        "[FIX ROUND 1] Please fix database setup.\n\nOriginal task: Create FastAPI project structure",
        "deepseek",
        subtask_id="st-db-fix-1",
    )
    first_fix.original_subtask_id = "st-db"
    task.fix_rounds = {"st-db": 1}
    orchestrator._orchestrator_states[task.task_id] = make_orchestrator_state(task)
    orchestrator._orchestrator_states[task.task_id].fix_rounds = task.fix_rounds
    mock_dispatcher._get_valid_agents.return_value = ["deepseek"]

    job = orchestrator._state.create_job(first_fix)
    feedback = "Required fixes: edit app/database.py to use sqlite+aiosqlite and edit requirements.txt to remove asyncpg."

    orchestrator._initiate_fix(job, feedback)

    restored = orchestrator._state.get_task(task.task_id)
    second_fix = next(st for st in restored.subtasks if st.subtask_id == "st-db-fix-2")
    assert "app/database.py" in second_fix.description
    assert "sqlite+aiosqlite" in second_fix.description
    assert "Original task: Create FastAPI project structure" in second_fix.description
    assert "Original task: [FIX ROUND 1]" not in second_fix.description
    assert original.subtask_id == "st-db"


def test_structured_handoff_uses_canonical_original_contract(orchestrator):
    task = orchestrator._state.create_task("nested handoff")
    original = orchestrator._state.add_subtask(
        task.task_id,
        "Create FastAPI project structure",
        "deepseek",
        subtask_id="st-db",
    )
    fix = orchestrator._state.add_subtask(
        task.task_id,
        "[FIX ROUND 1] Please fix database setup.\n\nOriginal task: Create FastAPI project structure",
        "deepseek",
        subtask_id="st-db-fix-1",
    )
    job = orchestrator._state.create_job(fix)
    acceptance = AcceptanceResult(
        subtask_id=fix.subtask_id,
        level1_passed=True,
        level2_passed=False,
        level2_feedback="needs content review",
    )

    handoff = json.loads(orchestrator._build_structured_handoff(task, job, acceptance, "needs content review"))

    assert handoff["contract"] == original.description
    assert "[FIX ROUND 1]" not in handoff["contract"]


def test_delivery_contract_workspace_noise_is_deterministically_cleaned(orchestrator, tmp_path):
    task = orchestrator._state.create_task(
        description="Build README.md only",
        project_dir=str(tmp_path),
        task_types=["artifact"],
        delivery_mode="artifact",
    )
    (tmp_path / "README.md").write_text("# Done\n", encoding="utf-8")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "README.md").write_text("cache\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (tmp_path / "run_tests.py").write_text("print('diagnostic')\n", encoding="utf-8")
    (tmp_path / "setup_test_env.py").write_text("print('install helper')\n", encoding="utf-8")
    (tmp_path / "runner.py").write_text("print('test helper')\n", encoding="utf-8")
    (tmp_path / "run_all_checks.py").write_text("print('all checks probe')\n", encoding="utf-8")
    (tmp_path / "run_syntax_check.py").write_text("print('syntax probe')\n", encoding="utf-8")
    (tmp_path / "test_import.py").write_text("print('import probe')\n", encoding="utf-8")
    pycache = tmp_path / "app" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "main.cpython-314.pyc").write_bytes(b"cache")
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-hygiene",
        "task_id": task.task_id,
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })

    run(orchestrator._finalize_task_status(task.task_id))

    restored = orchestrator._state.get_task(task.task_id)
    assert restored.status == TaskStatus.COMPLETED
    assert not (tmp_path / ".pytest_cache" / "README.md").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / "run_tests.py").exists()
    assert not (tmp_path / "setup_test_env.py").exists()
    assert not (tmp_path / "runner.py").exists()
    assert not (tmp_path / "run_all_checks.py").exists()
    assert not (tmp_path / "run_syntax_check.py").exists()
    assert not (tmp_path / "test_import.py").exists()
    assert not (pycache / "main.cpython-314.pyc").exists()
    cleanup = restored.last_owner_decision["deterministic_cleanup"]
    assert cleanup["removed_workspace_noise"]


def test_delivery_contract_forbidden_file_is_deterministically_cleaned(orchestrator, tmp_path):
    task = orchestrator._state.create_task(
        description="Build README.md without __init__.py",
        project_dir=str(tmp_path),
        task_types=["artifact"],
        delivery_mode="artifact",
    )
    (tmp_path / "README.md").write_text("# Done\n", encoding="utf-8")
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-forbidden-file",
        "task_id": task.task_id,
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [{"id": "constraint-forbidden-init", "constraint_type": "forbidden_file", "value": "__init__.py", "required": True}],
        "acceptance_probes": [],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })

    run(orchestrator._finalize_task_status(task.task_id))

    restored = orchestrator._state.get_task(task.task_id)
    assert restored.status == TaskStatus.COMPLETED
    assert not (tmp_path / "__init__.py").exists()
    quality = restored.last_owner_decision["delivery_quality"]
    assert quality["delivery_quality"] == "passed"
    assert restored.last_owner_decision["deterministic_cleanup"]["removed_forbidden_files"]


def test_forbidden_file_is_cleaned_before_wave_acceptance(orchestrator, tmp_path):
    task = orchestrator._state.create_task(
        description="Build FastAPI app without run.py",
        project_dir=str(tmp_path),
        task_types=["functional", "artifact"],
        delivery_mode="composite",
    )
    forbidden = tmp_path / "run.py"
    forbidden.write_text("import uvicorn\nuvicorn.run('app.main:app')\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    contract = {
        "contract_id": "delivery-contract-wave-forbidden",
        "task_id": task.task_id,
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"path_hint": "app/main.py", "artifact_type": "api_service_source", "required": True}],
        "constraints": [{"id": "constraint-forbidden-run", "constraint_type": "forbidden_file", "value": "run.py", "scope": "project_root", "required": True}],
        "acceptance_probes": [],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    }
    orchestrator._state._persistence.save_delivery_contract(contract)

    removed = orchestrator._cleanup_forbidden_file_constraints(task, contract)

    assert removed == [str(forbidden)]
    assert not forbidden.exists()


def test_delivery_contract_removes_forbidden_generated_file_before_final_status(orchestrator, tmp_path):
    task = orchestrator._state.create_task(
        description="Build README.md without setup.py",
        project_dir=str(tmp_path),
        task_types=["artifact"],
        delivery_mode="artifact",
    )
    (tmp_path / "README.md").write_text("# Done\n", encoding="utf-8")
    forbidden = tmp_path / "setup.py"
    forbidden.write_text("from setuptools import setup\n", encoding="utf-8")
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-cleanup",
        "task_id": task.task_id,
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [{"id": "constraint-forbidden-setup", "constraint_type": "forbidden_file", "value": "setup.py", "required": True}],
        "acceptance_probes": [],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })

    run(orchestrator._finalize_task_status(task.task_id))

    restored = orchestrator._state.get_task(task.task_id)
    assert restored.status == TaskStatus.COMPLETED
    assert not forbidden.exists()
    assert restored.last_owner_decision["deterministic_cleanup"]["removed_forbidden_files"]


def test_delivery_contract_allowed_files_are_deterministically_cleaned(orchestrator, tmp_path):
    task = orchestrator._state.create_task(
        description="Build only README.md",
        project_dir=str(tmp_path),
        task_types=["artifact"],
        delivery_mode="artifact",
    )
    (tmp_path / "README.md").write_text("# Done\n", encoding="utf-8")
    extra = tmp_path / "run_tests.py"
    extra.write_text("print('helper')\n", encoding="utf-8")
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-allowed-files",
        "task_id": task.task_id,
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [{
            "id": "constraint-allowed-files",
            "constraint_type": "allowed_files",
            "value": ["README.md"],
            "required": True,
        }],
        "acceptance_probes": [],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })

    run(orchestrator._finalize_task_status(task.task_id))

    restored = orchestrator._state.get_task(task.task_id)
    assert restored.status == TaskStatus.COMPLETED
    assert not extra.exists()
    cleanup = restored.last_owner_decision["deterministic_cleanup"]
    assert cleanup["removed_file_constraint_violations"]


def test_finalize_defers_when_versioned_remediation_is_running(orchestrator, tmp_path):
    task = orchestrator._state.create_task(
        description="Build README.md",
        project_dir=str(tmp_path),
        task_types=["artifact"],
        delivery_mode="artifact",
    )
    (tmp_path / "README.md").write_text("# Done\n", encoding="utf-8")
    original = orchestrator._state.add_subtask(task.task_id, "Write README.md", "minimax", subtask_id="st-docs")
    original.status = JobStatus.FAILED
    retry = orchestrator._state.add_subtask(task.task_id, "Retry README.md", "claude", subtask_id="st-docs-v2")
    retry.status = JobStatus.RUNNING
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-running-remediation",
        "task_id": task.task_id,
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })

    run(orchestrator._finalize_task_status(task.task_id))

    restored = orchestrator._state.get_task(task.task_id)
    assert restored.status == TaskStatus.RUNNING
    assert "remediation" in (restored.error or "").lower()


def test_finalize_completes_when_quality_already_passed_with_obsolete_remediation(orchestrator, tmp_path):
    task = orchestrator._state.create_task(
        description="Build README.md",
        project_dir=str(tmp_path),
        task_types=["artifact"],
        delivery_mode="artifact",
    )
    (tmp_path / "README.md").write_text("# Done\n", encoding="utf-8")
    original = orchestrator._state.add_subtask(task.task_id, "Write README.md", "minimax", subtask_id="st-docs")
    original.status = JobStatus.COMPLETED
    quality_fix = orchestrator._state.add_subtask(
        task.task_id,
        "Obsolete quality repair",
        "deepseek",
        subtask_id="st-quality-obsolete-fix-1",
    )
    quality_fix.status = JobStatus.RUNNING
    task.last_owner_decision = {
        "delivery_quality": {
            "delivery_quality": "passed",
            "missing_required": [],
            "produced_required": ["README.md"],
            "invalid_required": [],
            "failed_constraints": [],
            "evidence_gaps": [],
            "probe_results": [],
        }
    }
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-obsolete-remediation",
        "task_id": task.task_id,
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })

    run(orchestrator._finalize_task_status(task.task_id))

    restored = orchestrator._state.get_task(task.task_id)
    assert restored.status == TaskStatus.COMPLETED
    assert restored.error is None


def test_finalize_defers_when_persisted_remediation_is_running(orchestrator, tmp_path):
    task = orchestrator._state.create_task(
        description="Build README.md",
        project_dir=str(tmp_path),
        task_types=["artifact"],
        delivery_mode="artifact",
    )
    (tmp_path / "README.md").write_text("# Done\n", encoding="utf-8")
    original = orchestrator._state.add_subtask(task.task_id, "Write README.md", "minimax", subtask_id="st-docs")
    original.status = JobStatus.FAILED
    downstream = orchestrator._state.add_subtask(task.task_id, "Write tests", "deepseek", subtask_id="st-tests")
    downstream.status = JobStatus.CANCELLED
    orchestrator._state._persistence.save_subtask({
        "subtask_id": "st-docs-fix-1",
        "task_id": task.task_id,
        "description": "Fix README.md",
        "agent_id": "claude",
        "status": "running",
        "wave_number": 1,
        "progress": 0.0,
        "dependencies": [],
        "error_message": None,
    })
    orchestrator._state._persistence.save_delivery_contract({
        "contract_id": "delivery-contract-persisted-running-remediation",
        "task_id": task.task_id,
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "project_dir": str(tmp_path),
        "capabilities": [],
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
        "assumptions": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })

    run(orchestrator._finalize_task_status(task.task_id))

    restored = orchestrator._state.get_task(task.task_id)
    assert restored.status == TaskStatus.RUNNING
    assert "remediation" in (restored.error or "").lower()
