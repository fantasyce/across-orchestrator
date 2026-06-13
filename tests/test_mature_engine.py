from unittest.mock import MagicMock


def test_mature_engine_accepts_host_planning_adapter_without_aaa_imports():
    from across_orchestrator.engine import MatureOrchestrationEngine

    dispatcher = MagicMock()
    validator = MagicMock()
    owner_agent = MagicMock()

    def decompose(task, context=None):
        task.subtasks.append({
            "subtask_id": "st-readme",
            "description": "Create README.md",
            "agent_id": "openclaw",
            "path": "README.md",
        })

    owner_agent.decompose_and_assign.side_effect = decompose

    engine = MatureOrchestrationEngine(
        dispatcher=dispatcher,
        validator=validator,
        owner_agent=owner_agent,
    )
    task_id = engine.submit_task(
        "Create README.md",
        context={
            "allowed_subtask_agents": ["openclaw"],
            "strict_dependency": True,
            "enable_wave_gate": True,
        },
    )

    task = engine.state.get_task(task_id)
    assert task is not None
    assert task.status in {"pending", "running"}
    assert any(st.path == "README.md" and st.agent == "openclaw" for st in task.subtasks)
    owner_agent.decompose_and_assign.assert_called_once()
