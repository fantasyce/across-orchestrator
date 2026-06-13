import tempfile
from pathlib import Path


def test_ensure_strict_dependency_chain_repairs_missing_links():
    from across_orchestrator.models import Task
    from across_orchestrator.planning import ensure_strict_dependency_chain

    with tempfile.TemporaryDirectory() as tempdir:
        task = Task.from_plan(
            goal="Build a serial plan",
            project_root=str(Path(tempdir) / "project"),
            deliverables=["contract.json", "app.py", "README.md"],
            subtasks=[
                {"id": "contract", "description": "Contract", "path": "contract.json", "wave": 1},
                {"id": "app", "description": "App", "path": "app.py", "wave": 1},
                {"id": "readme", "description": "Readme", "path": "README.md", "wave": 1},
            ],
        )

    ensure_strict_dependency_chain(task)

    assert task.contract["serialPlan"] is True
    assert "serial_wave_dependencies" in task.contract["qualityGates"]
    assert task.subtasks[0].dependencies == []
    assert task.subtasks[1].dependencies == [task.subtasks[0].subtask_id]
    assert task.subtasks[2].dependencies == [task.subtasks[1].subtask_id]
    assert [subtask.wave for subtask in task.subtasks] == [1, 2, 3]
