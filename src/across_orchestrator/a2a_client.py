from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
import json
import socket


class A2AClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class A2ATaskResult:
    task_id: str
    state: str
    events: tuple[dict[str, Any], ...]
    artifacts: tuple[dict[str, Any], ...]


class A2AAgentClient:
    """Small bounded LF A2A client; remote Agents remain distinct from Workers."""

    def __init__(self, endpoint: str, *, timeout_seconds: float = 30, max_response_bytes: int = 8 * 1024 * 1024):
        self.endpoint = _safe_endpoint(endpoint)
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 300.0))
        self.max_response_bytes = max(1024, min(int(max_response_bytes), 64 * 1024 * 1024))

    def agent_card(self) -> dict[str, Any]:
        parsed = urlparse(self.endpoint)
        card_url = f"{parsed.scheme}://{parsed.netloc}/.well-known/agent-card.json"
        card = self._json_request(card_url, method="GET")
        if not isinstance(card.get("name"), str) or not isinstance(card.get("capabilities"), Mapping):
            raise A2AClientError("remote Agent Card is invalid")
        return card

    def send_task(self, *, task_id: str, text: str, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
        result = self._rpc(
            "tasks/send",
            {"id": task_id, "message": {"role": "user", "parts": [{"kind": "text", "text": str(text)}]}, "metadata": dict(metadata or {})},
            request_id=task_id,
        )
        task = result.get("result") if isinstance(result.get("result"), Mapping) else result
        if str(task.get("id") or "") != task_id:
            raise A2AClientError("remote Agent returned a mismatched task identity")
        return dict(task)

    def stream_task(self, *, task_id: str, text: str, metadata: Mapping[str, Any] | None = None) -> A2ATaskResult:
        payload = {
            "jsonrpc": "2.0",
            "id": task_id,
            "method": "tasks/sendSubscribe",
            "params": {"id": task_id, "message": {"role": "user", "parts": [{"kind": "text", "text": str(text)}]}, "metadata": dict(metadata or {})},
        }
        request = Request(self.endpoint, data=json.dumps(payload, separators=(",", ":")).encode(), method="POST", headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
        events: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        total = 0
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                for raw in response:
                    total += len(raw)
                    if total > self.max_response_bytes:
                        raise A2AClientError("A2A event stream exceeded its response limit")
                    line = raw.decode("utf-8", errors="strict").strip()
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    value = json.loads(line)
                    event = value.get("result") if isinstance(value, Mapping) and isinstance(value.get("result"), Mapping) else value
                    if not isinstance(event, Mapping):
                        raise A2AClientError("A2A stream event is invalid")
                    if str(event.get("taskId") or event.get("task_id") or event.get("id") or task_id) != task_id:
                        raise A2AClientError("A2A stream crossed task identities")
                    record = dict(event)
                    events.append(record)
                    artifact = record.get("artifact")
                    if isinstance(artifact, Mapping):
                        artifacts.append(dict(artifact))
        except (HTTPError, URLError, socket.timeout, UnicodeError, json.JSONDecodeError) as exc:
            raise A2AClientError(f"A2A stream failed: {type(exc).__name__}") from exc
        state = _terminal_state(events)
        return A2ATaskResult(task_id=task_id, state=state, events=tuple(events), artifacts=tuple(artifacts))

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        response = self._rpc("tasks/cancel", {"id": task_id}, request_id=f"cancel-{task_id}")
        result = response.get("result") if isinstance(response.get("result"), Mapping) else response
        if str(result.get("id") or result.get("taskId") or "") != task_id:
            raise A2AClientError("remote Agent returned a mismatched cancellation identity")
        state = str(result.get("state") or result.get("status", {}).get("state") or "")
        if state not in {"canceled", "cancelled"}:
            raise A2AClientError("remote Agent did not confirm cancellation")
        return dict(result)

    def _rpc(self, method: str, params: Mapping[str, Any], *, request_id: str) -> dict[str, Any]:
        response = self._json_request(
            self.endpoint,
            method="POST",
            payload={"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)},
        )
        if response.get("error"):
            raise A2AClientError("remote Agent returned an A2A error")
        return response

    def _json_request(self, url: str, *, method: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        request = Request(url, data=body, method=method, headers={"Accept": "application/json", "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.max_response_bytes + 1)
        except (HTTPError, URLError, socket.timeout) as exc:
            raise A2AClientError(f"A2A request failed: {type(exc).__name__}") from exc
        if len(raw) > self.max_response_bytes:
            raise A2AClientError("A2A response exceeded its size limit")
        try:
            value = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise A2AClientError("A2A response was not valid JSON") from exc
        if not isinstance(value, dict):
            raise A2AClientError("A2A response must be an object")
        return value


def _safe_endpoint(value: str) -> str:
    parsed = urlparse(str(value).strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise A2AClientError("A2A endpoint must be a credential-free HTTPS URL")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise A2AClientError("non-loopback A2A endpoints require HTTPS")
    return str(value).strip()


def _terminal_state(events: Iterable[Mapping[str, Any]]) -> str:
    state = "submitted"
    for event in events:
        candidate = event.get("state")
        if not candidate and isinstance(event.get("status"), Mapping):
            candidate = event["status"].get("state")
        if candidate:
            state = str(candidate)
    if state not in {"completed", "failed", "canceled", "cancelled", "input-required"}:
        raise A2AClientError("A2A stream ended without a terminal state")
    return state
