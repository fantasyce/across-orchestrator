from unittest.mock import MagicMock


def test_mature_engine_exposes_transplanted_task_orchestrator():
    from across_agents_assistant.task_manager.models import SubTask
    from across_orchestrator.engine import MatureOrchestrationEngine

    dispatcher = MagicMock()
    dispatcher.add_progress_callback = MagicMock()
    validator = MagicMock()
    owner_agent = MagicMock()

    def decompose(task, context=None):
        task.subtasks.append(
            SubTask(
                subtask_id="st-readme",
                task_id=task.task_id,
                description="Create README.md",
                agent_id="openclaw",
            )
        )

    owner_agent.decompose_and_assign.side_effect = decompose
    owner_agent.assign_waves.return_value = None
    owner_agent.refresh_decomposition_coverage.return_value = None
    dispatcher._get_valid_agents.return_value = ["openclaw"]

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
    assert task.status.value in {"pending", "running"}
    assert any(st.subtask_id == "st-readme" for st in task.subtasks)
    dispatcher.add_progress_callback.assert_called_once()
