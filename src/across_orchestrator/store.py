from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os
import time

from .models import Task


def default_home() -> Path:
    override = os.environ.get("ACROSS_ORCHESTRATOR_HOME")
    if override and override.strip():
        return Path(override).expanduser().resolve()
    return Path.home() / ".across-orchestrator"


class LocalStore:
    def __init__(self, home: str | Path | None = None):
        self.home = Path(home).expanduser().resolve() if home else default_home()
        self.tasks_dir = self.home / "tasks"
        self.events_dir = self.home / "events"
        self.init()

    def init(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)

    def save_task(self, task: Task) -> None:
        task.updated_at = time.time()
        path = self.tasks_dir / f"{task.task_id}.json"
        path.write_text(json.dumps(task.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

    def load_task(self, task_id: str) -> Task:
        path = self.tasks_dir / f"{task_id}.json"
        if not path.exists():
            raise KeyError(f"Task not found: {task_id}")
        return Task.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_task_ids(self) -> list[str]:
        return sorted(path.stem for path in self.tasks_dir.glob("task-*.json"))

    def append_event(self, task_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "type": event_type,
            "task_id": task_id,
            "timestamp": time.time(),
            "payload": payload or {},
        }
        path = self.events_dir / f"{task_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        return event

    def list_events(self, task_id: str) -> list[dict[str, Any]]:
        path = self.events_dir / f"{task_id}.jsonl"
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events
