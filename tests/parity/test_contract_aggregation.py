"""Tests for task-level and wave-level contract aggregation from subtask contracts."""

import os
import tempfile
from unittest.mock import MagicMock

import pytest

from across_agents_assistant.task_manager.models import (
    AcceptanceCheck,
    DeliverableSpec,
    Task,
    TaskContract,
    TaskStatus,
)
from across_agents_assistant.task_manager.orchestration.owner_agent import OwnerAgent
from across_agents_assistant.task_manager.state import TaskState


class FakePersistence:
    def __init__(self):
        self.task_contracts = []
        self.requirement_manifests = {}

    def save_task_contract(self, contract):
        self.task_contracts = [
            item for item in self.task_contracts
            if item.get("contract_id") != contract.get("contract_id")
        ]
        self.task_contracts.append(dict(contract))

    def get_task_contracts(self, _task_id):
        return list(self.task_contracts)

    def save_task(self, _task):
        pass

    def save_subtask(self, _subtask):
        pass

    def save_job(self, _job):
        pass

    def save_wave(self, _wave):
        pass

    def save_requirement_manifest(self, manifest):
        self.requirement_manifests[manifest["task_id"]] = dict(manifest)

    def get_requirement_manifest(self, task_id):
        return self.requirement_manifests.get(task_id)

    def save_artifact_record(self, _artifact):
        pass

    def get_artifact_records(self, _task_id):
        return []

    def save_acceptance_record(self, _record):
        pass

    def get_acceptance_records(self, _task_id):
        return []

    def update_artifact_records_for_subtask(self, task_id, subtask_id, status, current_status=None):
        pass


@pytest.fixture
def state():
    s = TaskState()
    s.set_persistence(FakePersistence())
    return s


@pytest.fixture
def owner_agent(state):
    mock_llm = MagicMock()
    mock_llm.side_effect = lambda system_prompt, message, temperature: MagicMock(
        text='{"subtasks": []}'
    )
    return OwnerAgent(llm_gateway=mock_llm, state=state)


class TestTaskContractAggregation:
    """N45: task-level contract aggregates subtask-specific deliverables."""

    def _make_subtask_contract(self, state, task_id, subtask_id, deliverables, checks):
        contract = TaskContract.new(
            task_id=task_id,
            level="subtask",
            goal=f"subtask {subtask_id}",
            subtask_id=subtask_id,
        )
        contract.expected_deliverables = [
            DeliverableSpec(
                artifact_type=d["artifact_type"],
                required=d.get("required", True),
                path_hint=d.get("path_hint"),
                description=d.get("description", ""),
            )
            for d in deliverables
        ]
        contract.acceptance_checks = [
            AcceptanceCheck(
                check_type=c["check_type"],
                description=c.get("description", ""),
                required=c.get("required", True),
            )
            for c in checks
        ]
        state.save_task_contract(contract)

    def test_aggregates_subtask_deliverables_into_task_contract(self, owner_agent, state):
        """Task contract should contain both inferred items and aggregated subtask deliverables."""
        task = Task(
            task_id="task-n45-1",
            description="Build a FastAPI REST API for user management",
            status=TaskStatus.PENDING,
        )
        state.save_requirement_manifest({
            "manifest_id": "manifest-n45-1",
            "task_id": task.task_id,
            "deliverables": [
                {"requirement_id": "req-models", "artifact_type": "api_service_source", "path_hint": "app/models.py", "required": True},
                {"requirement_id": "req-config", "artifact_type": "api_service_source", "path_hint": "app/config.yaml", "required": True},
                {"requirement_id": "req-main", "artifact_type": "api_service_source", "path_hint": "app/main.py", "required": True},
            ],
            "quality_checks": [],
        })

        # Create subtask contracts with specific deliverables
        self._make_subtask_contract(
            state, task.task_id, "st-models",
            deliverables=[
                {"artifact_type": "api_service_source", "path_hint": "app/models.py",
                 "description": "Pydantic models"},
                {"artifact_type": "api_service_source", "path_hint": "app/config.yaml",
                 "description": "App config"},
            ],
            checks=[
                {"check_type": "models_exist", "description": "Verify model files exist"},
            ],
        )
        self._make_subtask_contract(
            state, task.task_id, "st-routes",
            deliverables=[
                {"artifact_type": "api_service_source", "path_hint": "app/main.py",
                 "description": "FastAPI app entry point"},
            ],
            checks=[
                {"check_type": "routes_exist", "description": "Verify route files exist"},
            ],
        )

        # Create the task contract as _decompose_task does
        task_contract = TaskContract.new(
            task_id=task.task_id,
            level="task",
            goal=task.description,
        )

        # Call _infer_task_contract_requirements first (mocked by testing directly)
        # We don't call the real infer since we don't have the task type matching logic fully mocked
        # Instead, simulate what it does: add an api_service_source deliverable
        task_contract.expected_deliverables.append(
            DeliverableSpec(
                artifact_type="api_service_source",
                required=True,
                description="Backend API service source files must be produced",
            )
        )

        # Now aggregate
        all_contracts = state.get_task_contracts(task.task_id)
        subtask_contracts = [c for c in all_contracts if c.get("level") == "subtask"]
        owner_agent._aggregate_contract_requirements(task_contract, subtask_contracts)

        # Assert: task contract has 3 deliverables (1 inferred + 3 subtask - 1 dedup for api_service_source with no path_hint)
        deliverable_details = [
            (d.artifact_type, d.path_hint or "") for d in task_contract.expected_deliverables
        ]
        assert ("api_service_source", "") in deliverable_details  # inferred
        assert ("api_service_source", "app/models.py") in deliverable_details  # from st-models
        assert ("api_service_source", "app/config.yaml") in deliverable_details  # from st-models
        assert ("api_service_source", "app/main.py") in deliverable_details  # from st-routes

        # Task-level aggregation now keeps task-scope checks conservative and
        # does not lift custom subtask-only validators into the final contract.
        check_types = {c.check_type for c in task_contract.acceptance_checks}
        assert "models_exist" not in check_types
        assert "routes_exist" not in check_types

    def test_deduplicates_by_artifact_type_and_path_hint(self, owner_agent, state):
        """Same artifact_type + path_hint should not be added twice."""
        task = Task(
            task_id="task-n45-2",
            description="Build an API",
            status=TaskStatus.PENDING,
        )
        state.save_requirement_manifest({
            "manifest_id": "manifest-n45-2",
            "task_id": task.task_id,
            "deliverables": [
                {"requirement_id": "req-main", "artifact_type": "api_service_source", "path_hint": "app/main.py", "required": True},
            ],
            "quality_checks": [],
        })

        # Two subtasks with the same deliverable
        self._make_subtask_contract(
            state, task.task_id, "st-1",
            deliverables=[
                {"artifact_type": "api_service_source", "path_hint": "app/main.py",
                 "description": "Main entry point"},
            ],
            checks=[],
        )
        self._make_subtask_contract(
            state, task.task_id, "st-2",
            deliverables=[
                {"artifact_type": "api_service_source", "path_hint": "app/main.py",
                 "description": "Duplicate main entry point"},
            ],
            checks=[],
        )

        task_contract = TaskContract.new(task_id=task.task_id, level="task", goal=task.description)

        all_contracts = state.get_task_contracts(task.task_id)
        subtask_contracts = [c for c in all_contracts if c.get("level") == "subtask"]
        owner_agent._aggregate_contract_requirements(task_contract, subtask_contracts)

        # Should only have one api_service_source/app/main.py
        matches = [
            d for d in task_contract.expected_deliverables
            if d.artifact_type == "api_service_source" and d.path_hint == "app/main.py"
        ]
        assert len(matches) == 1

    def test_handles_empty_subtask_contracts(self, owner_agent, state):
        """Aggregation with no subtask contracts should not change the target contract."""
        task = Task(
            task_id="task-n45-3",
            description="Simple task",
            status=TaskStatus.PENDING,
        )

        task_contract = TaskContract.new(task_id=task.task_id, level="task", goal=task.description)
        task_contract.expected_deliverables.append(
            DeliverableSpec(artifact_type="file", required=True, path_hint="output.txt"),
        )

        owner_agent._aggregate_contract_requirements(task_contract, [])
        assert len(task_contract.expected_deliverables) == 1
        assert task_contract.expected_deliverables[0].path_hint == "output.txt"

    def test_task_level_aggregation_ignores_manifest_external_bootstrap_files(self, owner_agent, state):
        task = Task(
            task_id="task-bootstrap-filter",
            description="Create email validator with app/main.py and README.md",
            status=TaskStatus.PENDING,
            project_dir="/tmp/project",
        )
        state._tasks[task.task_id] = task
        state.save_requirement_manifest({
            "manifest_id": "manifest-bootstrap-filter",
            "task_id": task.task_id,
            "project_dir": task.project_dir,
            "deliverables": [
                {"requirement_id": "req-main", "artifact_type": "api_service_source", "path_hint": "app/main.py", "required": True, "status": "assigned"},
                {"requirement_id": "req-readme", "artifact_type": "documentation", "path_hint": "README.md", "required": True, "status": "assigned"},
            ],
            "quality_checks": [],
        })

        self._make_subtask_contract(
            state, task.task_id, "st-bootstrap",
            deliverables=[
                {"artifact_type": "file", "path_hint": "__init__.py", "description": "bootstrap helper"},
                {"artifact_type": "file", "path_hint": "app/main.py", "description": "real deliverable"},
            ],
            checks=[],
        )

        task_contract = TaskContract.new(task_id=task.task_id, level="task", goal=task.description)
        owner_agent._aggregate_contract_requirements(
            task_contract,
            state.get_task_contracts(task.task_id),
        )

        paths = [d.path_hint for d in task_contract.expected_deliverables]
        assert "app/main.py" in paths
        assert "__init__.py" not in paths


class TestWaveContractAggregation:
    """N44: wave-level contract aggregates only its own wave's subtask deliverables."""

    def _make_subtask_contract(self, state, task_id, subtask_id, deliverables, checks=None):
        contract = TaskContract.new(
            task_id=task_id,
            level="subtask",
            goal=f"subtask {subtask_id}",
            subtask_id=subtask_id,
        )
        contract.expected_deliverables = [
            DeliverableSpec(
                artifact_type=d["artifact_type"],
                required=d.get("required", True),
                path_hint=d.get("path_hint"),
                description=d.get("description", ""),
            )
            for d in deliverables
        ]
        contract.acceptance_checks = [
            AcceptanceCheck(
                check_type=c["check_type"],
                description=c.get("description", ""),
                required=c.get("required", True),
            )
            for c in (checks or [])
        ]
        state.save_task_contract(contract)

    def test_wave_contract_only_contains_its_own_subtask_deliverables(self, owner_agent, state):
        """Wave 1 contract should only include deliverables from wave 1 subtasks, not wave 2."""
        task = Task(
            task_id="task-n44-1",
            description="Build an API",
            status=TaskStatus.PENDING,
        )

        # Wave 1 subtasks produce main.py
        self._make_subtask_contract(
            state, task.task_id, "st-wave1-models",
            deliverables=[
                {"artifact_type": "api_service_source", "path_hint": "app/models.py"},
            ],
        )
        self._make_subtask_contract(
            state, task.task_id, "st-wave1-routes",
            deliverables=[
                {"artifact_type": "api_service_source", "path_hint": "app/main.py"},
            ],
        )

        # Wave 2 subtasks produce Dockerfile
        self._make_subtask_contract(
            state, task.task_id, "st-wave2-docker",
            deliverables=[
                {"artifact_type": "dockerfile", "path_hint": "Dockerfile"},
            ],
        )

        # --- Simulate wave 1 aggregation ---
        wave1_contract = TaskContract.new(
            task_id=task.task_id,
            level="wave",
            goal="Wave 1",
            wave_number=1,
        )

        all_contracts = state.get_task_contracts(task.task_id)
        # Filter to only wave 1 subtasks
        wave1_subtask_ids = {"st-wave1-models", "st-wave1-routes"}
        wave1_sources = [
            c for c in all_contracts
            if c.get("level") == "subtask" and c.get("subtask_id") in wave1_subtask_ids
        ]
        owner_agent._aggregate_contract_requirements(wave1_contract, wave1_sources)

        wave1_paths = {
            (d["artifact_type"], d.get("path_hint") or "")
            for d in [{"artifact_type": d.artifact_type, "path_hint": d.path_hint}
                       for d in wave1_contract.expected_deliverables]
        }
        # Override with proper object access
        wave1_paths = {
            (d.artifact_type, d.path_hint or "")
            for d in wave1_contract.expected_deliverables
        }
        assert ("api_service_source", "app/models.py") in wave1_paths
        assert ("api_service_source", "app/main.py") in wave1_paths
        assert ("dockerfile", "Dockerfile") not in wave1_paths

        # --- Simulate wave 2 aggregation ---
        wave2_contract = TaskContract.new(
            task_id=task.task_id,
            level="wave",
            goal="Wave 2",
            wave_number=2,
        )
        wave2_subtask_ids = {"st-wave2-docker"}
        wave2_sources = [
            c for c in all_contracts
            if c.get("level") == "subtask" and c.get("subtask_id") in wave2_subtask_ids
        ]
        owner_agent._aggregate_contract_requirements(wave2_contract, wave2_sources)

        wave2_paths = {
            (d.artifact_type, d.path_hint or "")
            for d in wave2_contract.expected_deliverables
        }
        assert ("dockerfile", "Dockerfile") in wave2_paths
        assert len(wave2_paths) == 1


class TestIntegrationAcceptancePathHint:
    """Integration acceptance checks concrete file via path_hint."""

    def test_missing_path_hint_file_fails_integration(self, owner_agent, state):
        """Task contract with path_hint for missing file should fail integration."""
        task_id = "task-int-1"

        # Persist a task-level contract with a concrete path_hint
        contract = TaskContract.new(
            task_id=task_id,
            level="task",
            goal="Build config",
        )
        contract.expected_deliverables.append(
            DeliverableSpec(
                artifact_type="file",
                required=True,
                path_hint="config.yaml",
                description="App configuration",
            )
        )
        state.save_task_contract(contract)

        # Create the task
        task = Task(
            task_id=task_id,
            description="Build config",
            status=TaskStatus.PENDING,
            project_dir="/nonexistent/path",
        )

        result = owner_agent.run_integration_test(task)
        assert not result.passed
        assert any("config.yaml" in a for a in result.details.get("missing_artifacts", []))

    def test_existing_path_hint_file_passes_integration(self, owner_agent, state):
        """Task contract with path_hint for existing file should pass integration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            task_id = "task-int-2"

            # Create the file that should satisfy path_hint
            file_path = os.path.join(tmpdir, "config.yaml")
            with open(file_path, "w") as f:
                f.write("key: value\n")

            # Persist a task-level contract with a concrete path_hint
            contract = TaskContract.new(
                task_id=task_id,
                level="task",
                goal="Build config",
            )
            contract.expected_deliverables.append(
                DeliverableSpec(
                    artifact_type="file",
                    required=True,
                    path_hint="config.yaml",
                    description="App configuration",
                )
            )
            state.save_task_contract(contract)

            task = Task(
                task_id=task_id,
                description="Build config",
                status=TaskStatus.PENDING,
                project_dir=tmpdir,
            )
            task.subtasks = []

            result = owner_agent.run_integration_test(task)
            assert result.passed, f"Expected passed, but got missing={result.details.get('missing_artifacts')}"
