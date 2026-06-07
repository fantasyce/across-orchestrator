"""Tests for contract sanitization — removing contradicting deliverable types."""

from across_agents_assistant.task_manager.models import AcceptanceCheck, DeliverableSpec
from across_agents_assistant.task_manager.orchestration.owner_agent import OwnerAgent


def test_documentation_only_subtask_drops_frontend_source():
    owner = object.__new__(OwnerAgent)

    deliverables = [
        DeliverableSpec("frontend_source", True, None, "Frontend source files must be produced."),
        DeliverableSpec("file", True, "README.md", "Output file must be produced: README.md"),
    ]
    checks = [
        AcceptanceCheck("frontend_source_exists", "Verify frontend exists.", True),
        AcceptanceCheck("file_exists", "Verify file exists.", True),
    ]

    clean_deliverables, clean_checks = owner._sanitize_subtask_contract_specs(
        description="Create README.md with build instructions for the Python project",
        agent_id="hermes",
        deliverables=deliverables,
        checks=checks,
    )

    assert all(d.artifact_type != "frontend_source" for d in clean_deliverables)
    assert all(c.check_type != "frontend_source_exists" for c in clean_checks)
    assert any(d.path_hint == "README.md" for d in clean_deliverables)


def test_frontend_subtask_keeps_frontend_source():
    owner = object.__new__(OwnerAgent)

    deliverables = [
        DeliverableSpec("frontend_source", True, None, "Frontend source files must be produced."),
    ]
    checks = [
        AcceptanceCheck("frontend_source_exists", "Verify frontend exists.", True),
    ]

    clean_deliverables, clean_checks = owner._sanitize_subtask_contract_specs(
        description="Create a React dashboard component with TypeScript",
        agent_id="hermes",
        deliverables=deliverables,
        checks=checks,
    )

    assert any(d.artifact_type == "frontend_source" for d in clean_deliverables)
