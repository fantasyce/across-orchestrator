import time

from across_agents_assistant.agent_bridge.protocol import AgentResponse
from across_agents_assistant.task_manager.dispatcher import TaskDispatcher
from across_agents_assistant.task_manager.models import JobResult, JobStatus
from across_agents_assistant.task_manager.state import TaskState


def test_dispatch_subtask_marks_parent_subtask_running_before_execution():
    state = TaskState()
    task = state.create_task("Build backend service")
    subtask = state.add_subtask(task.task_id, "Implement FastAPI backend", "deepseek")
    dispatcher = TaskDispatcher(state, local_agent_client=object())
    observed = {}

    def fake_execute(job, current_subtask, agent_id):
        current_task = state.get_task(task.task_id)
        observed["job_status"] = state.get_job(job.job_id).status
        observed["subtask_status"] = next(
            st.status for st in current_task.subtasks if st.subtask_id == current_subtask.subtask_id
        )
        return JobResult(job_id=job.job_id, success=True, output="done")

    dispatcher._get_valid_agents = lambda: ["deepseek"]
    dispatcher._execute_agent_job = fake_execute

    job = dispatcher.dispatch_subtask(subtask)
    assert job is not None

    deadline = time.time() + 2.0
    while time.time() < deadline:
        current_job = state.get_job(job.job_id)
        if current_job and current_job.status == JobStatus.COMPLETED:
            break
        time.sleep(0.01)

    assert observed["job_status"] == JobStatus.RUNNING
    assert observed["subtask_status"] == JobStatus.RUNNING
    assert state.get_job(job.job_id).status == JobStatus.COMPLETED
    assert state.get_task(task.task_id).subtasks[0].status == JobStatus.COMPLETED


def test_execute_agent_job_passes_manifest_assigned_writable_files_only():
    state = TaskState()
    task = state.create_task("Build exact release files", project_dir="/tmp/project")
    app_subtask = state.add_subtask(task.task_id, "Implement web/app.js", "deepseek", subtask_id="st-app")
    state.save_requirement_manifest({
        "manifest_id": "manifest-test",
        "task_id": task.task_id,
        "project_dir": task.project_dir,
        "deliverables": [
            {
                "requirement_id": "req-app",
                "artifact_type": "file",
                "required": True,
                "path_hint": "web/app.js",
                "assigned_subtask_id": "st-app",
            },
            {
                "requirement_id": "req-cli",
                "artifact_type": "file",
                "required": True,
                "path_hint": "cli/quality-check.mjs",
                "assigned_subtask_id": "st-cli",
            },
        ],
        "quality_checks": [],
        "created_at": 1.0,
        "updated_at": 1.0,
    })
    dispatcher = TaskDispatcher(state, local_agent_client=object())
    job = state.create_job(app_subtask)
    captured = {}

    class FakeBridge:
        def invoke(self, **kwargs):
            captured.update(kwargs)
            return AgentResponse(
                message_id="msg-test",
                request_id="req-test",
                success=True,
                output="done",
                agent_id=kwargs["agent_id"],
            )

    dispatcher._agent_bridge = FakeBridge()

    result = dispatcher._execute_agent_job(job, app_subtask, "deepseek")

    assert result.success is True
    assert captured["context"]["allowed_writable_files"] == ["web/app.js"]
    assert "cli/quality-check.mjs" not in captured["context"]["allowed_writable_files"]


def test_execute_agent_job_uses_short_timeout_for_quality_remediation(monkeypatch):
    monkeypatch.setenv("ACROSS_AGENTS_AGENT_TIMEOUT", "600")
    monkeypatch.delenv("ACROSS_AGENTS_QUALITY_REMEDIATION_TIMEOUT", raising=False)

    state = TaskState()
    task = state.create_task("Repair final quality gate", project_dir="/tmp/project")
    subtask = state.add_subtask(
        task.task_id,
        "Quality remediation attempt 1",
        "hermes",
        subtask_id="st-quality-browser-e2e",
    )
    dispatcher = TaskDispatcher(state, local_agent_client=object())
    job = state.create_job(subtask)
    captured = {}

    class FakeBridge:
        def invoke(self, **kwargs):
            captured.update(kwargs)
            return AgentResponse(
                message_id="msg-quality",
                request_id="req-quality",
                success=True,
                output="done",
                agent_id=kwargs["agent_id"],
            )

    dispatcher._agent_bridge = FakeBridge()

    result = dispatcher._execute_agent_job(job, subtask, "hermes")

    assert result.success is True
    assert captured["timeout"] == 120.0


def test_allowed_writable_files_fall_back_to_subtask_contract(tmp_path):
    state = TaskState()
    task = state.create_task("Build README", project_dir=str(tmp_path))
    subtask = state.add_subtask(task.task_id, "Write docs", "deepseek", subtask_id="st-doc")

    class FakePersistence:
        def get_requirement_manifest(self, _task_id):
            return None

        def get_task_contracts(self, _task_id):
            return [{
                "subtask_id": "st-doc",
                "expected_deliverables": [
                    {"artifact_type": "documentation", "path_hint": "README.md"},
                    {"artifact_type": "frontend_source", "path_hint": None},
                ],
            }]

    state.set_persistence(FakePersistence())
    dispatcher = TaskDispatcher(state, local_agent_client=object())

    assert dispatcher._allowed_writable_files_for_subtask(task, subtask) == ["README.md"]
