from __future__ import annotations

import json
import os
import threading
import uuid
from typing import Dict, List, Optional

from across_agents_assistant.task_manager.models import Artifact


class ArtifactStore:
    """In-memory (with optional file-based) artifact storage."""

    def __init__(self, storage_dir: Optional[str] = None) -> None:
        self._storage_dir = storage_dir
        self._artifacts: Dict[str, Artifact] = {}
        self._task_artifacts: Dict[str, List[str]] = {}
        self._lock = threading.RLock()

        if self._storage_dir is not None:
            os.makedirs(self._storage_dir, exist_ok=True)

    def store(self, artifact: Artifact) -> str:
        with self._lock:
            if not artifact.artifact_id:
                artifact.artifact_id = f"art-{uuid.uuid4().hex[:8]}"

            self._artifacts[artifact.artifact_id] = artifact
            self._task_artifacts.setdefault(artifact.task_id, []).append(
                artifact.artifact_id
            )

            if self._storage_dir is not None:
                filepath = os.path.join(
                    self._storage_dir, f"{artifact.artifact_id}.json"
                )
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(self._artifact_to_dict(artifact), f, indent=2)

            return artifact.artifact_id

    def get_latest(self, artifact_type: str, task_id: str) -> Optional[Artifact]:
        with self._lock:
            art_ids = self._task_artifacts.get(task_id, [])
            candidates = [
                self._artifacts[aid]
                for aid in art_ids
                if self._artifacts[aid].artifact_type == artifact_type
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda a: a.created_at)

    def get_consumers(self, artifact_id: str) -> List[str]:
        with self._lock:
            artifact = self._artifacts.get(artifact_id)
            if artifact is None:
                return []
            return list(artifact.consumed_by)

    def notify_consumers(self, artifact_id: str) -> List[str]:
        with self._lock:
            artifact = self._artifacts.get(artifact_id)
            if artifact is None:
                return []
            return list(artifact.consumed_by)

    def get_by_task(self, task_id: str) -> List[Artifact]:
        with self._lock:
            art_ids = self._task_artifacts.get(task_id, [])
            return [self._artifacts[aid] for aid in art_ids if aid in self._artifacts]

    @staticmethod
    def _artifact_to_dict(artifact: Artifact) -> dict:
        return {
            "artifact_id": artifact.artifact_id,
            "artifact_type": artifact.artifact_type,
            "produced_by": artifact.produced_by,
            "task_id": artifact.task_id,
            "subtask_id": artifact.subtask_id,
            "content_ref": artifact.content_ref,
            "consumed_by": artifact.consumed_by,
            "schema_version": artifact.schema_version,
            "created_at": artifact.created_at,
        }
