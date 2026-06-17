from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
from contextlib import contextmanager
import fcntl
import json
import os
import tempfile
import time

from .models import Task
from .paths import component_data_home, expand_user, safe_runtime_override


def default_home(env: Mapping[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    override = safe_runtime_override("ACROSS_ORCHESTRATOR_HOME", source)
    if override:
        return Path(expand_user(override, source)).resolve()
    return component_data_home(env=source)


class LocalStore:
    def __init__(self, home: str | Path | None = None, env: Mapping[str, str] | None = None):
        self.env = env if env is not None else os.environ
        self.home = Path(home).expanduser().resolve() if home else default_home(self.env)
        self.tasks_dir = self.home / "tasks"
        self.events_dir = self.home / "events"
        self.loops_dir = self.home / "loops"
        self.loop_events_dir = self.home / "loop-events"
        self.loop_cancel_requests_dir = self.home / "loop-cancel-requests"
        self.locks_dir = self.home / "locks"
        self.init()

    def init(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.loops_dir.mkdir(parents=True, exist_ok=True)
        self.loop_events_dir.mkdir(parents=True, exist_ok=True)
        self.loop_cancel_requests_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)

    def save_task(self, task: Task) -> None:
        task.updated_at = time.time()
        path = self.tasks_dir / f"{task.task_id}.json"
        _atomic_write_json(path, task.to_dict())

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
        _atomic_write_json(path, loop.to_dict())

    @contextmanager
    def loop_lock(self, loop_id: str, *, blocking: bool = True):
        path = self.locks_dir / f"{loop_id}.lock"
        with path.open("a+", encoding="utf-8") as handle:
            flags = fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            fcntl.flock(handle.fileno(), flags)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

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

    def request_loop_cancel(self, loop_id: str, reason: str | None = None) -> dict[str, Any]:
        request = {
            "loop_id": loop_id,
            "reason": reason or "cancelled",
            "requested_at": time.time(),
        }
        _atomic_write_json(self.loop_cancel_requests_dir / f"{loop_id}.json", request)
        return request

    def load_loop_cancel_request(self, loop_id: str) -> dict[str, Any] | None:
        path = self.loop_cancel_requests_dir / f"{loop_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def clear_loop_cancel_request(self, loop_id: str) -> None:
        path = self.loop_cancel_requests_dir / f"{loop_id}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            return


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        tmp_path = Path(handle.name)
        handle.write(text)
    os.replace(tmp_path, path)
