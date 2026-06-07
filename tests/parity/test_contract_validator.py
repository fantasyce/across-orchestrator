import os
import tempfile

import pytest

from across_agents_assistant.task_manager.models import Job, JobStatus, SubTask, Task
from across_agents_assistant.task_manager.orchestration.validator import (
    ContractValidator,
    ValidationError,
    ValidationReport,
    extract_model_fields,
    extract_routers,
    validate_endpoint_coverage,
    validate_model_fields,
    validate_response_format,
    validate_type_consistency,
)


@pytest.fixture
def code_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def write_file(code_dir: str, rel_path: str, content: str) -> str:
    filepath = os.path.join(code_dir, rel_path)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath


class TestExtractRouters:
    def test_extract_routers_basic(self, code_dir):
        write_file(
            code_dir,
            "routes.py",
            """
from fastapi import APIRouter

router = APIRouter()

@router.get("/items")
def list_items():
    return {"items": []}

@router.post("/items")
def create_item():
    return {"id": 1}
""",
        )
        routers = extract_routers(code_dir)
        assert "GET /items" in routers
        assert "POST /items" in routers

    def test_extract_routers_no_router(self, code_dir):
        write_file(
            code_dir,
            "utils.py",
            "def helper(): pass\n",
        )
        routers = extract_routers(code_dir)
        assert routers == {}


class TestExtractModelFields:
    def test_extract_model_fields_basic(self, code_dir):
        write_file(
            code_dir,
            "models.py",
            """
from pydantic import BaseModel
from typing import Optional

class Task(BaseModel):
    task_id: str
    description: str
    position: Optional[int] = None
""",
        )
        models = extract_model_fields(code_dir)
        assert "Task" in models
        assert models["Task"]["task_id"] == "str"
        assert models["Task"]["description"] == "str"
        assert models["Task"]["position"] == "Optional[int]"

    def test_extract_model_fields_no_model(self, code_dir):
        write_file(
            code_dir,
            "plain.py",
            "class Foo: pass\n",
        )
        models = extract_model_fields(code_dir)
        assert "Foo" not in models


class TestValidateEndpointCoverage:
    def test_missing_endpoint(self, code_dir):
        write_file(
            code_dir,
            "routes.py",
            """
from fastapi import APIRouter
router = APIRouter()

@router.get("/items")
def list_items():
    return {"items": []}
""",
        )
        spec = {
            "paths": {
                "/items": {"get": {}},
                "/tasks/{id}/comments": {"get": {}},
            }
        }
        report = validate_endpoint_coverage(spec, code_dir)
        assert not report.passed
        assert any(e.error_type == "missing_endpoint" for e in report.errors)
        assert any(e.target == "/tasks/{id}/comments" for e in report.errors)

    def test_all_endpoints_present(self, code_dir):
        write_file(
            code_dir,
            "routes.py",
            """
from fastapi import APIRouter
router = APIRouter()

@router.get("/items")
def list_items():
    return {"items": []}
""",
        )
        spec = {
            "paths": {
                "/items": {"get": {}},
            }
        }
        report = validate_endpoint_coverage(spec, code_dir)
        assert report.passed
        assert report.errors == []


class TestValidateModelFields:
    def test_missing_field(self, code_dir):
        write_file(
            code_dir,
            "models.py",
            """
from pydantic import BaseModel
from typing import Optional

class Task(BaseModel):
    task_id: str
    description: str
""",
        )
        spec = {
            "components": {
                "schemas": {
                    "Task": {
                        "required": ["task_id", "description", "position"],
                        "properties": {
                            "task_id": {"type": "string"},
                            "description": {"type": "string"},
                            "position": {"type": "integer"},
                        },
                    }
                }
            }
        }
        report = validate_model_fields(spec, code_dir)
        assert not report.passed
        assert any(e.error_type == "missing_field" for e in report.errors)
        assert any(e.target == "Task.position" for e in report.errors)

    def test_all_fields_present(self, code_dir):
        write_file(
            code_dir,
            "models.py",
            """
from pydantic import BaseModel
from typing import Optional

class Task(BaseModel):
    task_id: str
    description: str
    position: Optional[int] = None
""",
        )
        spec = {
            "components": {
                "schemas": {
                    "Task": {
                        "required": ["task_id", "description"],
                        "properties": {
                            "task_id": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    }
                }
            }
        }
        report = validate_model_fields(spec, code_dir)
        assert report.passed
        assert report.errors == []


class TestValidateResponseFormat:
    def test_response_format_mismatch(self, code_dir):
        write_file(
            code_dir,
            "routes.py",
            """
from fastapi import APIRouter
router = APIRouter()

@router.get("/items")
def list_items():
    return {"items": []}
""",
        )
        spec = {
            "paths": {
                "/items": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "success": {"type": "boolean"},
                                                "data": {"type": "object"},
                                            },
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        report = validate_response_format(spec, code_dir)
        assert not report.passed
        assert any(e.error_type == "response_format_mismatch" for e in report.errors)

    def test_response_format_match(self, code_dir):
        write_file(
            code_dir,
            "routes.py",
            """
from fastapi import APIRouter
router = APIRouter()

@router.get("/items")
def list_items():
    return {"success": True, "data": {"items": []}}
""",
        )
        spec = {
            "paths": {
                "/items": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "success": {"type": "boolean"},
                                                "data": {"type": "object"},
                                            },
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        report = validate_response_format(spec, code_dir)
        assert report.passed
        assert report.errors == []


class TestValidateTypeConsistency:
    def test_type_inconsistency(self, code_dir):
        write_file(
            code_dir,
            "models.py",
            """
from pydantic import BaseModel

class Task(BaseModel):
    task_id: str
    count: str
""",
        )
        spec = {
            "components": {
                "schemas": {
                    "Task": {
                        "properties": {
                            "task_id": {"type": "string"},
                            "count": {"type": "integer"},
                        },
                    }
                }
            }
        }
        report = validate_type_consistency(spec, code_dir)
        assert not report.passed
        assert any(e.error_type == "type_inconsistency" for e in report.errors)
        assert any(e.target == "Task.count" for e in report.errors)

    def test_type_consistency_match(self, code_dir):
        write_file(
            code_dir,
            "models.py",
            """
from pydantic import BaseModel
from typing import Optional

class Task(BaseModel):
    task_id: str
    count: int
    done: bool
""",
        )
        spec = {
            "components": {
                "schemas": {
                    "Task": {
                        "properties": {
                            "task_id": {"type": "string"},
                            "count": {"type": "integer"},
                            "done": {"type": "boolean"},
                        },
                    }
                }
            }
        }
        report = validate_type_consistency(spec, code_dir)
        assert report.passed
        assert report.errors == []


class TestContractValidator:
    def test_validate_returns_report(self):
        validator = ContractValidator()
        from across_agents_assistant.task_manager.models import Job, SubTask

        subtask = SubTask(subtask_id="st-1", description="test", agent_id="claude")
        job = Job.new(subtask, agent_id="claude")
        report = validator.validate(job)
        assert isinstance(report, ValidationReport)
        assert report.passed is True

    def test_extract_declared_files_ignores_urls(self):
        text = (
            "Fix the previous error from https://api.minimaxi.com/v1/chat/completions "
            "and create `index.html` under the project directory."
        )

        candidates = ContractValidator._extract_declared_files(text)

        assert "index.html" in candidates
        assert all("minimaxi.com" not in item for item in candidates)

    def test_extract_declared_files_supports_contextual_paths(self):
        text = "Please create file src/app.py and save output to reports/result.json."

        candidates = ContractValidator._extract_declared_files(text)

        assert "src/app.py" in candidates
        assert "reports/result.json" in candidates

    def test_extract_declared_files_ignores_dot_prefixed_project_directory(self):
        text = (
            "All files MUST be written to this directory: "
            "/example-app-home/workspace/e2e-secret-clean "
            "Do NOT create files in any other location. "
            "Create file requirements.txt and save output to .env.example."
        )

        candidates = ContractValidator._extract_declared_files(text)

        assert "/example-app-home" not in candidates
        assert "requirements.txt" in candidates
        assert ".env.example" in candidates

    def test_detects_clarification_without_delivery(self):
        class StateStub:
            def __init__(self, task):
                self.task = task

            def get_task_by_subtask(self, _subtask_id):
                return self.task

        from across_agents_assistant.task_manager.models import Task, SubTask, Job

        task = Task.new("Build backend", project_dir="/tmp/demo")
        subtask = SubTask(
            subtask_id="st-clarify",
            description="Design FastAPI backend",
            agent_id="claude",
            task_id=task.task_id,
        )
        task.subtasks.append(subtask)
        job = Job.new(subtask, agent_id="claude")
        job.result = (
            "Let me ask a few clarifying questions first.\n\n"
            "First question: What should the /api/message endpoint do?\n"
            "- **A.** Simple echo\n"
            "- **B.** Task orchestration\n"
            "Which of these best matches your intent?"
        )

        validator = ContractValidator(StateStub(task))
        report = validator.validate(job)

        assert report.passed is False
        assert any(e.error_type == "non_delivery_clarification" for e in report.errors)

    def test_validate_fails_when_required_contract_deliverable_missing(self, code_dir):
        class PersistenceStub:
            def get_task_contracts(self, _task_id):
                return [{
                    "level": "subtask",
                    "subtask_id": "st-contract",
                    "expected_deliverables": [
                        {
                            "artifact_type": "file",
                            "required": True,
                            "path_hint": "reports/output.json",
                            "description": "Output report must exist",
                        }
                    ],
                    "acceptance_checks": [],
                }]

        class StateStub:
            def __init__(self, task):
                self.task = task
                self._persistence = PersistenceStub()

            def get_task_by_subtask(self, _subtask_id):
                return self.task

        task = Task.new("Generate report", project_dir=code_dir)
        subtask = SubTask(
            subtask_id="st-contract",
            description="Create report",
            agent_id="claude",
            task_id=task.task_id,
        )
        task.subtasks.append(subtask)
        job = Job.new(subtask, agent_id="claude")
        job.result_metadata = {"created_files": []}

        validator = ContractValidator(StateStub(task))
        report = validator.validate(job)

        assert report.passed is False
        assert any(e.error_type == "missing_contract_deliverable" for e in report.errors)

    def test_validate_fails_when_required_contract_acceptance_check_missing(self, code_dir):
        class PersistenceStub:
            def get_task_contracts(self, _task_id):
                return [{
                    "level": "subtask",
                    "subtask_id": "st-contract",
                    "expected_deliverables": [],
                    "acceptance_checks": [
                        {
                            "check_type": "container_config_exists",
                            "required": True,
                            "description": "Dockerfile must exist",
                        }
                    ],
                }]

        class StateStub:
            def __init__(self, task):
                self.task = task
                self._persistence = PersistenceStub()

            def get_task_by_subtask(self, _subtask_id):
                return self.task

        task = Task.new("Containerize service", project_dir=code_dir)
        subtask = SubTask(
            subtask_id="st-contract",
            description="Add container config",
            agent_id="claude",
            task_id=task.task_id,
        )
        task.subtasks.append(subtask)
        job = Job.new(subtask, agent_id="claude")
        job.result_metadata = {"created_files": []}

        validator = ContractValidator(StateStub(task))
        report = validator.validate(job)

        assert report.passed is False
        assert any(e.error_type == "failed_acceptance_check" for e in report.errors)

    def test_html_file_satisfies_frontend_source_contract(self, code_dir):
        class PersistenceStub:
            def get_task_contracts(self, _task_id):
                return [{
                    "level": "subtask",
                    "subtask_id": "st-frontend",
                    "expected_deliverables": [
                        {
                            "artifact_type": "frontend_source",
                            "required": True,
                            "description": "Native HTML/CSS/JS frontend must exist",
                        }
                    ],
                    "acceptance_checks": [
                        {
                            "check_type": "frontend_source_exists",
                            "required": True,
                            "description": "Frontend source exists",
                        }
                    ],
                }]

        class StateStub:
            def __init__(self, task):
                self.task = task
                self._persistence = PersistenceStub()

            def get_task_by_subtask(self, _subtask_id):
                return self.task

        index_path = write_file(code_dir, "frontend/index.html", "<!doctype html><script src=\"app.js\"></script>")
        task = Task.new("Build native frontend", project_dir=code_dir)
        subtask = SubTask(
            subtask_id="st-frontend",
            description="Create native HTML frontend",
            agent_id="hermes",
            task_id=task.task_id,
        )
        task.subtasks.append(subtask)
        job = Job.new(subtask, agent_id="hermes")
        job.result_metadata = {"created_files": [index_path]}

        validator = ContractValidator(StateStub(task))
        report = validator.validate(job)

        assert report.passed is True

    def test_mjs_file_satisfies_api_service_source_contract(self, code_dir):
        class PersistenceStub:
            def get_task_contracts(self, _task_id):
                return [{
                    "level": "subtask",
                    "subtask_id": "st-api",
                    "expected_deliverables": [
                        {
                            "artifact_type": "api_service_source",
                            "required": True,
                            "description": "Node.js API service source must exist",
                        }
                    ],
                    "acceptance_checks": [
                        {
                            "check_type": "api_source_exists",
                            "required": True,
                            "description": "API source exists",
                        }
                    ],
                }]

        class StateStub:
            def __init__(self, task):
                self.task = task
                self._persistence = PersistenceStub()

            def get_task_by_subtask(self, _subtask_id):
                return self.task

        server_path = write_file(code_dir, "api/server.mjs", "import http from 'node:http';\n")
        task = Task.new("Build Node API service", project_dir=code_dir)
        subtask = SubTask(
            subtask_id="st-api",
            description="Create api/server.mjs",
            agent_id="deepseek",
            task_id=task.task_id,
        )
        subtask.output_file = server_path
        task.subtasks.append(subtask)
        job = Job.new(subtask, agent_id="deepseek")
        job.result_metadata = {"created_files": [server_path]}

        report = ContractValidator(StateStub(task)).validate(job)

        assert report.passed is True

    def test_mjs_file_satisfies_test_suite_contract(self, code_dir):
        class PersistenceStub:
            def get_task_contracts(self, _task_id):
                return [{
                    "level": "subtask",
                    "subtask_id": "st-test",
                    "expected_deliverables": [
                        {
                            "artifact_type": "test_suite",
                            "required": True,
                            "description": "Node.js smoke tests must exist",
                        }
                    ],
                    "acceptance_checks": [
                        {
                            "check_type": "test_suite_exists",
                            "required": True,
                            "description": "Test suite exists",
                        }
                    ],
                }]

        class StateStub:
            def __init__(self, task):
                self.task = task
                self._persistence = PersistenceStub()

            def get_task_by_subtask(self, _subtask_id):
                return self.task

        test_path = write_file(code_dir, "tests/e2e-smoke.mjs", "import assert from 'node:assert';\n")
        task = Task.new("Build Node smoke test", project_dir=code_dir)
        subtask = SubTask(
            subtask_id="st-test",
            description="Create tests/e2e-smoke.mjs",
            agent_id="claude",
            task_id=task.task_id,
        )
        subtask.output_file = test_path
        task.subtasks.append(subtask)
        job = Job.new(subtask, agent_id="claude")
        job.result_metadata = {"created_files": [test_path]}

        report = ContractValidator(StateStub(task)).validate(job)

        assert report.passed is True

    def test_workspace_metadata_file_does_not_satisfy_generic_file_deliverable(self, code_dir):
        class PersistenceStub:
            def get_task_contracts(self, _task_id):
                return [{
                    "level": "subtask",
                    "subtask_id": "st-contract",
                    "expected_deliverables": [
                        {
                            "artifact_type": "file",
                            "required": True,
                            "description": "A business deliverable must exist",
                        }
                    ],
                    "acceptance_checks": [],
                }]

        class StateStub:
            def __init__(self, task):
                self.task = task
                self._persistence = PersistenceStub()

            def get_task_by_subtask(self, _subtask_id):
                return self.task

        metadata_path = write_file(code_dir, ".claude/settings.json", "{}")
        task = Task.new("Generate deliverable", project_dir=code_dir)
        subtask = SubTask(
            subtask_id="st-contract",
            description="Create deliverable",
            agent_id="claude",
            task_id=task.task_id,
        )
        task.subtasks.append(subtask)
        job = Job.new(subtask, agent_id="claude")
        job.result_metadata = {"created_files": [metadata_path]}

        validator = ContractValidator(StateStub(task))
        report = validator.validate(job)

        assert report.passed is False
        assert any(e.error_type == "missing_contract_artifact_type" for e in report.errors)


class TestCanonicalContractValidation:
    """Phase 3: fix/reassign validation uses canonical contract, not fix prompt."""

    def test_fix_prompt_declared_files_do_not_pollute_validation(self, tmp_path):
        """Remediation subtask should not extract declared files from the fix prompt."""
        from across_agents_assistant.task_manager.models import (
            DeliverableSpec,
            TaskContract,
        )
        from across_agents_assistant.task_manager.state import TaskState

        class FakePersistence:
            def __init__(self):
                self.contracts = []

            def save_task_contract(self, contract):
                self.contracts.append(dict(contract))

            def get_task_contracts(self, _task_id):
                return list(self.contracts)

            def save_subtask(self, _st):
                pass

            def save_task(self, _t):
                pass

            def save_job(self, _j):
                pass

        state = TaskState()
        state.set_persistence(FakePersistence())
        task = state.create_task("Create tests/test_api.py", project_dir=str(tmp_path))
        state.add_subtask(task.task_id, "Create tests/test_api.py", "deepseek", subtask_id="st-tests")
        fix = state.add_subtask(
            task.task_id,
            "[FIX ROUND 1] previous error: missing /tmp/project/previously-mentioned-file.py [truncated]",
            "deepseek",
            subtask_id="st-tests-fix-1",
        )
        path = tmp_path / "tests" / "test_api.py"
        path.parent.mkdir()
        path.write_text("def test_ok():\n    assert True\n")
        # output_file points to an auxiliary file that exists but is NOT the canonical deliverable
        auxiliary = tmp_path / "run_tests.sh"
        auxiliary.write_text("python3 -m pytest tests/test_api.py\n")
        fix.output_file = str(auxiliary)

        contract = TaskContract.new(
            task_id=task.task_id,
            level="subtask",
            goal="Create tests",
            subtask_id="st-tests",
            project_dir=str(tmp_path),
        )
        contract.expected_deliverables = [
            DeliverableSpec(artifact_type="file", path_hint="tests/test_api.py", required=True)
        ]
        state.save_task_contract(contract)

        job = Job.new(fix, fix.agent_id)
        job.status = JobStatus.COMPLETED
        job.result = "Created tests/test_api.py and run_tests.sh"

        validator = ContractValidator(state)
        report = validator.validate(job)
        # Should pass because the canonical contract path hint (tests/test_api.py) exists,
        # even though the fix prompt contains noisy paths and output_file is auxiliary.
        assert report.passed, f"Validation should pass but got errors: {report.errors}"

    def test_output_file_auxiliary_does_not_override_contract_path_hint(self, tmp_path):
        """When canonical path_hint exists, output_file pointing elsewhere should not fail."""
        from across_agents_assistant.task_manager.models import (
            DeliverableSpec,
            TaskContract,
        )
        from across_agents_assistant.task_manager.state import TaskState

        class FakePersistence:
            def __init__(self):
                self.contracts = []

            def save_task_contract(self, contract):
                self.contracts.append(dict(contract))

            def get_task_contracts(self, _task_id):
                return list(self.contracts)

            def save_subtask(self, _st):
                pass

            def save_task(self, _t):
                pass

            def save_job(self, _j):
                pass

        state = TaskState()
        state.set_persistence(FakePersistence())
        task = state.create_task("Create tests/test_api.py", project_dir=str(tmp_path))
        state.add_subtask(task.task_id, "Create tests/test_api.py", "deepseek", subtask_id="st-tests")
        fix = state.add_subtask(
            task.task_id,
            "[FIX ROUND 1] Create canonical test file",
            "deepseek",
            subtask_id="st-tests-fix-1",
        )

        expected = tmp_path / "tests" / "test_api.py"
        expected.parent.mkdir()
        expected.write_text("def test_ok():\n    assert True\n")
        auxiliary = tmp_path / "run_tests.sh"
        auxiliary.write_text("python3 -m pytest tests/test_api.py\n")
        fix.output_file = str(auxiliary)

        contract = TaskContract.new(
            task_id=task.task_id,
            level="subtask",
            goal="Create tests/test_api.py",
            subtask_id="st-tests",
            project_dir=str(tmp_path),
        )
        contract.expected_deliverables = [
            DeliverableSpec(artifact_type="file", path_hint="tests/test_api.py", required=True)
        ]
        state.save_task_contract(contract)

        job = Job.new(fix, fix.agent_id)
        job.status = JobStatus.COMPLETED
        job.result = "Created tests/test_api.py and run_tests.sh"

        validator = ContractValidator(state)
        report = validator.validate(job)
        assert report.passed, f"Validation should pass but got errors: {report.errors}"

    def test_contract_validator_accepts_test_suite_artifact_type(self, tmp_path):
        from across_agents_assistant.task_manager.models import DeliverableSpec, TaskContract
        from across_agents_assistant.task_manager.state import TaskState

        class FakePersistence:
            def __init__(self):
                self.contracts = []

            def save_task_contract(self, contract):
                self.contracts.append(dict(contract))

            def get_task_contracts(self, _task_id):
                return list(self.contracts)

            def save_subtask(self, _st):
                pass

            def save_task(self, _t):
                pass

            def save_job(self, _j):
                pass

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "conftest.py").write_text("import pytest\n", encoding="utf-8")
        (tmp_path / "tests" / "test_app.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")

        state = TaskState()
        state.set_persistence(FakePersistence())
        task = state.create_task("Create tests", project_dir=str(tmp_path))
        subtask = state.add_subtask(task.task_id, "Create pytest tests", "hermes", subtask_id="st-tests")

        contract = TaskContract.new(
            task_id=task.task_id,
            level="subtask",
            goal="Create pytest tests",
            subtask_id=subtask.subtask_id,
            project_dir=str(tmp_path),
        )
        contract.expected_deliverables = [
            DeliverableSpec(artifact_type="test_suite", required=True),
            DeliverableSpec(artifact_type="file", path_hint="conftest.py", required=True),
        ]
        contract.acceptance_checks = [
            {"check_type": "test_suite_exists", "description": "Tests exist", "required": True},
            {"check_type": "file_exists", "description": "conftest exists", "required": True},
        ]
        state.save_task_contract(contract)

        job = Job.new(subtask, subtask.agent_id)
        job.status = JobStatus.COMPLETED
        job.output_file = str(tmp_path / "tests" / "test_app.py")
        job.result = "Created tests/conftest.py and tests/test_app.py"

        report = ContractValidator(state).validate(job)

        assert report.passed, f"Validation should pass but got errors: {report.errors}"


def test_is_within_project_dir_normalizes_private_tmp_alias():
    project_dir = "/tmp/demo-project"
    candidate = "/private/tmp/demo-project/.claude/settings.json"

    assert ContractValidator._is_within_project_dir(candidate, project_dir) is True

    def test_remediation_contract_cannot_find_path_hint_still_fails(self, tmp_path):
        """If the canonical path_hint doesn't exist, remediation should still fail."""
        from across_agents_assistant.task_manager.models import (
            DeliverableSpec,
            TaskContract,
        )
        from across_agents_assistant.task_manager.state import TaskState

        class FakePersistence:
            def __init__(self):
                self.contracts = []

            def save_task_contract(self, contract):
                self.contracts.append(dict(contract))

            def get_task_contracts(self, _task_id):
                return list(self.contracts)

            def save_subtask(self, _st):
                pass

            def save_task(self, _t):
                pass

            def save_job(self, _j):
                pass

        state = TaskState()
        state.set_persistence(FakePersistence())
        task = state.create_task("Create missing file", project_dir=str(tmp_path))
        state.add_subtask(task.task_id, "Create missing.py", "deepseek", subtask_id="st-missing")
        fix = state.add_subtask(
            task.task_id,
            "Fix missing.py",
            "deepseek",
            subtask_id="st-missing-fix-1",
        )

        contract = TaskContract.new(
            task_id=task.task_id,
            level="subtask",
            goal="Create missing.py",
            subtask_id="st-missing",
            project_dir=str(tmp_path),
        )
        contract.expected_deliverables = [
            DeliverableSpec(artifact_type="file", path_hint="missing.py", required=True)
        ]
        state.save_task_contract(contract)

        job = Job.new(fix, fix.agent_id)
        job.status = JobStatus.COMPLETED
        job.result = "Fixed implementation"

        validator = ContractValidator(state)
        report = validator.validate(job)
        assert not report.passed
        assert any(e.error_type == "missing_contract_deliverable" for e in report.errors)


class TestContractValidatorBarePathResolution:
    def test_contract_validator_resolves_bare_hint_to_unique_nested_file(self, tmp_path):
        from across_agents_assistant.task_manager.models import DeliverableSpec, TaskContract
        from across_agents_assistant.task_manager.state import TaskState

        class FakePersistence:
            def __init__(self):
                self.contracts = []

            def save_task_contract(self, contract):
                self.contracts.append(dict(contract))

            def get_task_contracts(self, _task_id):
                return list(self.contracts)

            def save_subtask(self, _st):
                pass

            def save_task(self, _t):
                pass

            def save_job(self, _j):
                pass

        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "calculator.py").write_text("def add(a, b): return a + b\n")

        state = TaskState()
        state.set_persistence(FakePersistence())
        task = state.create_task("Create calculator.py", project_dir=str(tmp_path))
        subtask = state.add_subtask(task.task_id, "Create calculator.py", "deepseek", subtask_id="st-calc")

        contract = TaskContract.new(
            task_id=task.task_id,
            level="subtask",
            goal="Create calculator",
            subtask_id="st-calc",
            project_dir=str(tmp_path),
        )
        contract.expected_deliverables = [
            DeliverableSpec(artifact_type="api_service_source", required=True, path_hint="calculator.py")
        ]
        state.save_task_contract(contract)

        job = Job.new(subtask, subtask.agent_id)
        job.status = JobStatus.COMPLETED
        job.result = "Created calculator.py"

        validator = ContractValidator(state)
        report = validator.validate(job)
        assert report.passed, f"Validation should pass but got errors: {report.errors}"


class TestContractValidatorGenericFileGuidance:
    def test_pathless_generic_file_contract_does_not_block_planning_subtask(self, tmp_path):
        """A generic pathless file deliverable is LLM guidance, not a deterministic
        filesystem requirement. Final delivery contract acceptance owns hard
        project deliverable checks.
        """
        from across_agents_assistant.task_manager.models import DeliverableSpec, TaskContract
        from across_agents_assistant.task_manager.state import TaskState

        state = TaskState()
        task = state.create_task("Design a todo CLI", project_dir=str(tmp_path))
        subtask = state.add_subtask(
            task.task_id,
            "Design the todo CLI architecture and output a brief spec document.",
            "claude",
            subtask_id="st-design",
        )

        contract = TaskContract.new(
            task_id=task.task_id,
            level="subtask",
            goal=subtask.description,
            subtask_id=subtask.subtask_id,
            project_dir=str(tmp_path),
        )
        contract.expected_deliverables = [
            DeliverableSpec(
                artifact_type="file",
                required=True,
                path_hint=None,
                description="Output file must be produced: design spec",
            )
        ]
        state.save_task_contract(contract)

        job = Job.new(subtask, subtask.agent_id)
        job.status = JobStatus.COMPLETED
        job.result = "Spec: commands are add, list, complete; persistence uses local JSON."

        report = ContractValidator(state).validate(job)

        assert report.passed, f"Validation should pass but got errors: {report.errors}"


class TestContractValidatorRuntimeDataDeliverables:
    def _state_with_contract(self, tmp_path, path_hint: str, description: str):
        from across_agents_assistant.task_manager.models import DeliverableSpec, TaskContract
        from across_agents_assistant.task_manager.state import TaskState

        class FakePersistence:
            def __init__(self):
                self.contracts = []

            def save_task_contract(self, contract):
                self.contracts.append(dict(contract))

            def get_task_contracts(self, _task_id):
                return list(self.contracts)

            def save_task(self, _task):
                pass

            def save_subtask(self, _subtask):
                pass

            def save_job(self, _job):
                pass

        state = TaskState()
        state.set_persistence(FakePersistence())
        task = state.create_task("Build todo CLI", project_dir=str(tmp_path))
        subtask = state.add_subtask(task.task_id, description, "deepseek", subtask_id="st-runtime")
        contract = TaskContract.new(
            task_id=task.task_id,
            level="subtask",
            goal=description,
            subtask_id=subtask.subtask_id,
            project_dir=str(tmp_path),
        )
        contract.expected_deliverables = [
            DeliverableSpec(artifact_type="file", path_hint=path_hint, required=True)
        ]
        state.save_task_contract(contract)
        return state, subtask

    def test_runtime_json_store_hint_is_not_required_as_deliverable(self, tmp_path):
        state, subtask = self._state_with_contract(
            tmp_path,
            "todos.json",
            "Implement todo_cli.py using local JSON persistence, e.g. todos.json, for runtime data.",
        )
        (tmp_path / "todo_cli.py").write_text("print('ok')\n", encoding="utf-8")
        job = Job.new(subtask, subtask.agent_id)
        job.status = JobStatus.COMPLETED
        job.result_metadata = {"created_files": [str(tmp_path / "todo_cli.py")]}

        report = ContractValidator(state).validate(job)

        assert report.passed, f"Runtime data should not block validation: {report.errors}"

    def test_runtime_json_file_persistence_hint_is_not_required_as_deliverable(self, tmp_path):
        state, subtask = self._state_with_contract(
            tmp_path,
            "todos.json",
            "Implement todo_cli.py. Use JSON file persistence (e.g., todos.json).",
        )
        (tmp_path / "todo_cli.py").write_text("print('ok')\n", encoding="utf-8")
        job = Job.new(subtask, subtask.agent_id)
        job.status = JobStatus.COMPLETED
        job.result_metadata = {"created_files": [str(tmp_path / "todo_cli.py")]}

        report = ContractValidator(state).validate(job)

        assert report.passed, f"Runtime data should not block validation: {report.errors}"

    def test_explicit_json_deliverable_is_still_required(self, tmp_path):
        state, subtask = self._state_with_contract(
            tmp_path,
            "config.json",
            "Create required file config.json with default settings.",
        )
        job = Job.new(subtask, subtask.agent_id)
        job.status = JobStatus.COMPLETED

        report = ContractValidator(state).validate(job)

        assert not report.passed
        assert any(error.target == "config.json" for error in report.errors)
