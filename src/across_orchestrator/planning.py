from __future__ import annotations

from .models import SubTask, Task


def ensure_strict_dependency_chain(task: Task) -> None:
    """Fill missing dependency links so strict tasks form a real serial chain."""
    task.contract["serialPlan"] = True
    quality_gates = list(task.contract.get("qualityGates") or [])
    if "serial_wave_dependencies" not in quality_gates:
        quality_gates.append("serial_wave_dependencies")
    task.contract["qualityGates"] = quality_gates

    previous: SubTask | None = None
    for index, subtask in enumerate(task.subtasks, start=1):
        subtask.priority = max(1, int(subtask.priority or index))
        if previous is None:
            subtask.dependencies = []
        elif not subtask.dependencies:
            subtask.dependencies = [previous.subtask_id]
            if subtask.wave <= previous.wave:
                subtask.wave = previous.wave + 1
        previous = subtask
