from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import json
import os
import time

from .models import Task
from .paths import component_data_home


def default_home(env: Mapping[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    override = source.get("ACROSS_ORCHESTRATOR_HOME")
    if override and override.strip():
        return Path(override).expanduser().resolve()
    return component_data_home(env=source)


class LocalStore:
    def __init__(self, home: str | Path | None = None, env: Mapping[str, str] | None = None):
        self.env = env if env is not None else os.environ
        self.home = Path(home).expanduser().resolve() if home else default_home(self.env)
        self.tasks_dir = self.home / "tasks"
        self.events_dir = self.home / "events"
        self.loops_dir = self.home / "loops"
        self.loop_events_dir = self.home / "loop-events"
        self.init()

    def init(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.loops_dir.mkdir(parents=True, exist_ok=True)
        self.loop_events_dir.mkdir(parents=True, exist_ok=True)

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

    def save_loop(self, loop: Any) -> None:
        loop.updated_at = time.time()
        path = self.loops_dir / f"{loop.loop_id}.json"
        path.write_text(json.dumps(loop.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

    def load_loop_dict(self, loop_id: str) -> dict[str, Any]:
        path = self.loops_dir / f"{loop_id}.json"
        if not path.exists():
            raise KeyError(f"Loop not found: {loop_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list_loop_ids(self) -> list[str]:
        return sorted(path.stem for path in self.loops_dir.glob("loop-*.json"))

    def append_loop_event(self, loop_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "type": event_type,
            "loop_id": loop_id,
            "timestamp": time.time(),
            "payload": payload or {},
        }
        path = self.loop_events_dir / f"{loop_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        return event

    def list_loop_events(self, loop_id: str) -> list[dict[str, Any]]:
        path = self.loop_events_dir / f"{loop_id}.jsonl"
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events
