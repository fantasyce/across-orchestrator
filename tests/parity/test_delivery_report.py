"""Tests for build_delivery_report."""

from types import SimpleNamespace

from across_agents_assistant.task_manager.models import Task, TaskStatus
from across_agents_assistant.task_manager.orchestration.delivery_report import build_delivery_report


def test_delivery_report_passed_summary():
    task = Task.new("build project")
    task.status = TaskStatus.COMPLETED
    manifest = {
        "deliverables": [
            {"requirement_id": "req-main", "path_hint": "main.py", "required": True, "status": "accepted"}
        ]
    }
    report = build_delivery_report(
        task=task,
        manifest=manifest,
        artifact_records=[],
        acceptance_records=[],
        quality_health={"quality_gate": "passed", "manifest_accepted": 1},
    )
    assert report["summary"] == "All required deliverables were produced and accepted."
    assert report["required_total"] == 1
    assert report["accepted_total"] == 1


def test_delivery_report_failed_lists_missing_required():
    task = Task.new("build project")
    task.status = TaskStatus.FAILED
    manifest = {
        "deliverables": [
            {"requirement_id": "req-readme", "path_hint": "README.md", "required": True, "status": "missing"}
        ]
    }
    report = build_delivery_report(
        task=task,
        manifest=manifest,
        artifact_records=[],
        acceptance_records=[],
        quality_health={"quality_gate": "failed", "manifest_accepted": 0, "next_repair_action": "quality_remediation"},
    )
    assert report["missing_required"] == ["README.md"]
    assert report["next_action"] == "quality_remediation"


def test_delivery_report_marks_attempted_when_active_quality_subtask_exists_without_attempt_metadata():
    task = SimpleNamespace(
        task_id="task-quality",
        status="running",
        last_owner_decision={},
        subtasks=[
            SimpleNamespace(subtask_id="st-quality-abc", status="running")
        ],
    )

    report = build_delivery_report(
        task=task,
        manifest={"deliverables": [{"path_hint": "README.md", "required": True, "status": "missing"}]},
        artifact_records=[],
        acceptance_records=[],
        quality_health={"quality_gate": "failed", "manifest_accepted": 0, "next_repair_action": "await_quality_remediation"},
    )

    assert report["remediation"]["attempted"] is True
    assert report["remediation"]["active_subtasks"] == ["st-quality-abc"]


def test_delivery_report_attempted_false_when_no_quality_subtasks_and_no_attempts():
    task = SimpleNamespace(
        task_id="task-no-quality",
        status="running",
        last_owner_decision={},
        subtasks=[],
    )

    report = build_delivery_report(
        task=task,
        manifest={"deliverables": [{"path_hint": "main.py", "required": True, "status": "accepted"}]},
        artifact_records=[],
        acceptance_records=[],
        quality_health={"quality_gate": "passed", "manifest_accepted": 1},
    )

    assert report["remediation"]["attempted"] is False


def test_delivery_report_consistency_active_remediation_and_running():
    task = SimpleNamespace(
        task_id="task-consistency",
        status="running",
        last_owner_decision={},
        subtasks=[
            SimpleNamespace(subtask_id="st-quality-abc", status="running")
        ],
    )

    report = build_delivery_report(
        task=task,
        manifest={"deliverables": [{"path_hint": "main.py", "required": True, "status": "missing"}]},
        artifact_records=[],
        acceptance_records=[],
        quality_health={"quality_gate": "failed", "manifest_accepted": 0},
    )

    assert report["consistency"]["has_active_quality_remediation"] is True
    assert report["consistency"]["has_missing_required"] is True
    assert report["consistency"]["is_terminal"] is False
    assert report["consistency"]["terminal_with_active_remediation"] is False


def test_delivery_report_consistency_terminal_with_active_remediation():
    task = SimpleNamespace(
        task_id="task-terminal-remediation",
        status="completed",
        last_owner_decision={},
        subtasks=[
            SimpleNamespace(subtask_id="st-quality-abc", status="running")
        ],
    )

    report = build_delivery_report(
        task=task,
        manifest={"deliverables": [{"path_hint": "main.py", "required": True, "status": "accepted"}]},
        artifact_records=[],
        acceptance_records=[],
        quality_health={"quality_gate": "passed", "manifest_accepted": 1},
    )

    assert report["consistency"]["is_terminal"] is True
    assert report["consistency"]["terminal_with_active_remediation"] is True


def test_delivery_report_consistency_terminal_clean():
    task = SimpleNamespace(
        task_id="task-clean",
        status="completed",
        last_owner_decision={},
        subtasks=[],
    )

    report = build_delivery_report(
        task=task,
        manifest={"deliverables": [{"path_hint": "main.py", "required": True, "status": "accepted"}]},
        artifact_records=[],
        acceptance_records=[],
        quality_health={"quality_gate": "passed", "manifest_accepted": 1},
    )

    assert report["consistency"]["is_terminal"] is True
    assert report["consistency"]["has_active_quality_remediation"] is False
    assert report["consistency"]["has_missing_required"] is False
    assert report["consistency"]["terminal_with_active_remediation"] is False


def test_delivery_report_uses_computed_final_status_override():
    task = SimpleNamespace(
        task_id="task-override",
        status="running",
        last_owner_decision={},
        subtasks=[],
    )

    report = build_delivery_report(
        task=task,
        manifest={"deliverables": [{"path_hint": "main.py", "required": True, "status": "accepted"}]},
        artifact_records=[],
        acceptance_records=[],
        quality_health={"quality_gate": "passed", "manifest_accepted": 1},
        final_status="completed",
    )

    assert report["final_status"] == "completed"
    assert report["consistency"]["is_terminal"] is True


def test_delivery_report_failed_with_passed_quality_uses_warning_summary():
    task = SimpleNamespace(
        task_id="task-failed-pass",
        status="failed",
        last_owner_decision={},
        subtasks=[],
    )

    report = build_delivery_report(
        task=task,
        manifest={"deliverables": [{"path_hint": "main.py", "required": True, "status": "accepted"}]},
        artifact_records=[],
        acceptance_records=[],
        quality_health={"quality_gate": "passed", "manifest_accepted": 1},
    )

    assert report["final_status"] == "failed"
    assert "still failed" in report["summary"]


def test_delivery_report_consistency_includes_failed_constraints():
    task = SimpleNamespace(
        task_id="task-forbidden",
        status="failed",
        last_owner_decision={
            "delivery_quality": {
                "delivery_quality": "failed",
                "missing_required": [],
                "invalid_required": [],
                "failed_constraints": [
                    {"constraint_type": "forbidden_file", "value": "__init__.py", "evidence": ["/tmp/project/__init__.py"]}
                ],
            }
        },
        subtasks=[],
    )

    report = build_delivery_report(
        task=task,
        manifest={"deliverables": []},
        artifact_records=[],
        acceptance_records=[],
        quality_health={
            "quality_gate": "failed",
            "manifest_accepted": 0,
            "next_repair_action": "quality_remediation",
        },
        final_status="failed",
    )

    assert report["quality_gate"] == "failed"
    assert report["consistency"]["has_failed_constraints"] is True
    assert report["failed_constraints"][0]["value"] == "__init__.py"


def test_delivery_report_uses_quality_health_delivery_report_when_owner_decision_missing():
    task = SimpleNamespace(
        task_id="task-forbidden-fallback",
        status="failed",
        last_owner_decision={},
        subtasks=[],
    )

    report = build_delivery_report(
        task=task,
        manifest={"deliverables": []},
        artifact_records=[],
        acceptance_records=[],
        quality_health={
            "quality_gate": "failed",
            "manifest_accepted": 0,
            "delivery_quality_report": {
                "delivery_quality": "failed",
                "failed_constraints": [
                    {
                        "constraint_type": "forbidden_file",
                        "value": "__init__.py",
                        "evidence": ["/tmp/project/__init__.py"],
                    }
                ],
            },
        },
        final_status="failed",
    )

    assert report["consistency"]["has_failed_constraints"] is True
    assert report["failed_constraints"][0]["constraint_type"] == "forbidden_file"
    assert report["failed_constraints"][0]["value"] == "__init__.py"


def test_delivery_report_exposes_structured_quality_report():
    task = SimpleNamespace(
        task_id="task-quality-report",
        status="completed",
        last_owner_decision={},
        subtasks=[],
    )

    report = build_delivery_report(
        task=task,
        manifest={"deliverables": [{"path_hint": "index.html", "required": True, "status": "accepted"}]},
        artifact_records=[],
        acceptance_records=[],
        quality_health={
            "quality_gate": "passed",
            "manifest_accepted": 1,
            "delivery_quality_report": {
                "delivery_quality": "passed",
                "quality_report": {
                    "quality_gate": "passed",
                    "generated_quality_score": 82,
                    "final_quality_score": 96,
                },
            },
        },
        final_status="completed",
    )

    assert report["quality_report"]["final_quality_score"] == 96
    assert report["quality_report"]["generated_quality_score"] == 82
