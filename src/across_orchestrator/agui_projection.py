from __future__ import annotations

import json
import time
from typing import Any

from .redaction import redact_sensitive_value


AGUI_PROJECTION_SCHEMA = "across-agui-projection/1.0"
AGUI_EVENT_STREAM_SCHEMA = "ag-ui-event-stream/1.0"


EVENT_TYPE_MAP = {
    "task.created": "task.created",
    "task.completed": "task.completed",
    "task.failed": "task.failed",
    "loop.started": "task.created",
    "loop.next_action.selected": "task.step.started",
    "loop.step.started": "task.step.started",
    "loop.step.completed": "task.step.completed",
    "loop.step.cancelled": "task.step.cancelled",
    "loop.approval_required": "task.input.required",
    "loop.action.approved": "task.input.received",
    "loop.action.rejected": "task.failed",
    "loop.completed": "task.completed",
    "loop.failed": "task.failed",
    "loop.cancelled": "task.cancelled",
}


def project_events_to_agui(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Project Across task/loop events into a compact AG-UI-compatible stream."""

    payload = payload or {}
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    source = str(payload.get("source") or payload.get("kind") or "across")
    task_id = str(payload.get("task_id") or payload.get("loop_id") or payload.get("id") or "unknown")
    projected = [_project_event(event, task_id=task_id, source=source, index=index) for index, event in enumerate(events)]
    return {
        "schema_version": AGUI_PROJECTION_SCHEMA,
        "ag_ui_schema": AGUI_EVENT_STREAM_SCHEMA,
        "provider": "across-orchestrator",
        "source": source,
        "task_id": task_id,
        "status": "passed",
        "events": projected,
        "summary": {
            "event_count": len(projected),
            "raw_transcripts_included": False,
            "secrets_included": False,
            "projection_only": True,
        },
    }


def project_event_sse(event: dict[str, Any], *, task_id: str = "unknown", source: str = "across") -> str:
    """Return one SSE chunk carrying an AG-UI projected event."""

    projected = _project_event(event, task_id=task_id, source=source, index=0)
    return f"event: {projected['type']}\ndata: {json.dumps(projected, sort_keys=True)}\n\n"


def _project_event(event: Any, *, task_id: str, source: str, index: int) -> dict[str, Any]:
    raw = event if isinstance(event, dict) else {"type": "message", "payload": event}
    event_type = str(raw.get("type") or "message")
    projected_type = EVENT_TYPE_MAP.get(event_type, "task.updated")
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    sequence = raw.get("sequence")
    safe_payload = redact_sensitive_value(payload)
    title = str(payload.get("summary") or payload.get("action_type") or payload.get("status") or event_type)
    return {
        "id": str(raw.get("event_id") or raw.get("id") or f"{task_id}:agui:{index + 1}"),
        "type": projected_type,
        "source_type": event_type,
        "source": source,
        "task_id": task_id,
        "sequence": sequence if sequence is not None else index + 1,
        "timestamp": raw.get("timestamp") or time.time(),
        "title": redact_sensitive_value(title),
        "payload": safe_payload,
        "ui": {
            "component": "AcrossTaskCard",
            "regions": ["New Task", "Task Detail", "Evidence Center"],
            "external_client_safe": True,
        },
    }
