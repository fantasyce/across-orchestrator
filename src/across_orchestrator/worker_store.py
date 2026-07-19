from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping
import fcntl
import json
import os
import tempfile

from .paths import component_data_home, expand_user, safe_runtime_override


def default_worker_control_home(env: Mapping[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    explicit = safe_runtime_override("ACROSS_WORKER_CONTROL_HOME", source)
    if explicit:
        return Path(expand_user(explicit, source)).resolve()
    return (component_data_home(env=source) / "worker-control").resolve()


class WorkerControlStore:
    """Small durable JSON store with per-record locks and atomic replacement."""

    _COLLECTIONS = (
        "nodes",
        "enrollments",
        "jobs",
        "leases",
        "events",
        "artifacts",
        "grants",
        "audit",
        "idempotency",
    )

    def __init__(self, home: str | Path | None = None, env: Mapping[str, str] | None = None):
        self.env = env if env is not None else os.environ
        self.home = Path(home).expanduser().resolve() if home else default_worker_control_home(self.env)
        self.locks = self.home / "locks"
        for name in self._COLLECTIONS:
            (self.home / name).mkdir(parents=True, exist_ok=True)
        self.locks.mkdir(parents=True, exist_ok=True)

    def collection_dir(self, collection: str) -> Path:
        if collection not in self._COLLECTIONS:
            raise ValueError("unsupported worker store collection")
        return self.home / collection

    def path(self, collection: str, record_id: str, *, suffix: str = ".json") -> Path:
        clean = _safe_record_id(record_id)
        return self.collection_dir(collection) / f"{clean}{suffix}"

    @contextmanager
    def lock(self, name: str) -> Iterator[None]:
        clean = _safe_record_id(name)
        with (self.locks / f"{clean}.lock").open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def put(self, collection: str, record_id: str, value: Mapping[str, Any]) -> Path:
        target = self.path(collection, record_id)
        with self.lock(f"{collection}-{record_id}"):
            _atomic_write_json(target, dict(value))
        return target

    def get(self, collection: str, record_id: str) -> dict[str, Any] | None:
        target = self.path(collection, record_id)
        if not target.exists():
            return None
        with self.lock(f"{collection}-{record_id}"):
            try:
                value = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"worker store record is unreadable: {collection}/{record_id}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"worker store record must be an object: {collection}/{record_id}")
        return value

    def delete(self, collection: str, record_id: str) -> bool:
        target = self.path(collection, record_id)
        with self.lock(f"{collection}-{record_id}"):
            try:
                target.unlink()
            except FileNotFoundError:
                return False
        return True

    def list(self, collection: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for target in sorted(self.collection_dir(collection).glob("*.json")):
            value = self.get(collection, target.stem)
            if value is not None:
                records.append(value)
        return records

    def append(self, collection: str, record_id: str, value: Mapping[str, Any]) -> int:
        target = self.path(collection, record_id, suffix=".jsonl")
        with self.lock(f"{collection}-{record_id}"):
            sequence = 1
            if target.exists():
                with target.open("r", encoding="utf-8") as reader:
                    sequence = sum(1 for line in reader if line.strip()) + 1
            item = dict(value)
            item.setdefault("sequence", sequence)
            with target.open("a", encoding="utf-8") as writer:
                writer.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
                writer.flush()
                os.fsync(writer.fileno())
        return sequence

    def read_log(self, collection: str, record_id: str) -> list[dict[str, Any]]:
        target = self.path(collection, record_id, suffix=".jsonl")
        if not target.exists():
            return []
        result: list[dict[str, Any]] = []
        with self.lock(f"{collection}-{record_id}"):
            for line in target.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if isinstance(item, dict):
                    result.append(item)
        return result


def _safe_record_id(value: str) -> str:
    clean = str(value or "").strip()
    if not clean or clean in {".", ".."} or "/" in clean or "\\" in clean or len(clean) > 160:
        raise ValueError("unsafe worker store record id")
    return clean


def _atomic_write_json(target: Path, value: Mapping[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
