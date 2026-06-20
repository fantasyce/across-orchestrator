from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import json
import os
import time

from .agent_card import render_agent_card
from .agent_loop import AgentLoopConcurrencyError, AgentLoopRuntime
from .host_conformance import evaluate_host_conformance
from .paths import COMPONENT_ID, contains_protected_user_reference, is_developer_mode, is_product_mode, run_home
from .plugin_manifest import render_plugin_health, render_plugin_manifest
from .runtime import OrchestratorRuntime


LOOP_STREAM_CLOSING_EVENT_TYPES = {
    "loop.approval_required",
    "loop.completed",
    "loop.failed",
    "loop.stopped",
    "loop.cancelled",
}


class OrchestratorHandler(BaseHTTPRequestHandler):
    server_version = "AcrossOrchestrator/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def runtime(self) -> OrchestratorRuntime:
        return self.server.runtime  # type: ignore[attr-defined]

    @property
    def loop_runtime(self) -> AgentLoopRuntime:
        return self.server.loop_runtime  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/health":
                self.respond(render_plugin_health())
                return
            if path == "/.well-known/agent-card.json":
                self.respond(render_agent_card())
                return
            if path == "/.well-known/across-plugin.json":
                self.respond(render_plugin_manifest())
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) == 2 and parts[0] == "tasks":
                self.respond(self.runtime.get_task(parts[1]).to_dict())
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "events":
                self.respond(self.runtime.list_events(parts[1]))
                return
            if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "events" and parts[3] == "stream":
                self.respond_sse(self.runtime.list_events(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "evidence-bundle":
                self.respond(self.runtime.evidence_bundle(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "quality-benchmark":
                self.respond(self.runtime.quality_benchmark(parts[1]))
                return
            if len(parts) == 2 and parts[0] == "loops":
                self.respond(self.loop_runtime.get_loop(parts[1]).to_dict())
                return
            if len(parts) == 3 and parts[0] == "loops" and parts[2] == "health":
                self.respond(self.loop_runtime.get_loop_health(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "loops" and parts[2] == "evidence-summary":
                self.respond(self.loop_runtime.get_loop_evidence_summary(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "loops" and parts[2] == "telemetry":
                self.respond(self.loop_runtime.get_loop_telemetry(parts[1]))
                return
            if len(parts) == 3 and parts[0] == "loops" and parts[2] == "events":
                self.respond(self.loop_runtime.list_loop_events(parts[1], after_sequence=_query_int(query, "after_sequence")))
                return
            if len(parts) == 4 and parts[0] == "loops" and parts[2] == "events" and parts[3] == "stream":
                after_sequence = _query_int(query, "after_sequence")
                if _query_truthy(query.get("follow", [""])[0]):
                    self.respond_loop_sse(parts[1], after_sequence=after_sequence)
                    return
                self.respond_sse(self.loop_runtime.list_loop_events(parts[1], after_sequence=after_sequence))
                return
            self.respond({"error": "not_found"}, status=404)
        except KeyError:
            self.respond({"error": "not_found"}, status=404)
        except AgentLoopConcurrencyError as exc:
            self.respond(
                {
                    "error": "max_concurrent_loops_exceeded",
                    "active_count": exc.active_count,
                    "max_concurrent_loops": exc.max_concurrent_loops,
                },
                status=409,
            )
        except ValueError as exc:
            self.respond({"error": "bad_request", "detail": str(exc)}, status=400)
        except Exception:
            self.respond({"error": "internal_error"}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            payload = self.read_json()
            if path == "/tasks":
                task = self.runtime.submit_task(
                    goal=payload.get("goal") or payload.get("text") or "",
                    project_root=payload.get("projectRoot") or payload.get("project_root") or ".",
                    deliverables=payload.get("deliverables") or ["README.md"],
                    agent=payload.get("agent") or "demo",
                    subtasks=payload.get("subtasks") or None,
                    strict_dependency=bool(payload.get("strictDependency") or payload.get("strict_dependency")),
                    task_types=payload.get("taskTypes") or payload.get("task_types") or None,
                    agent_adapters=payload.get("agentAdapters") or payload.get("agent_adapters") or None,
                )
                self.respond(task.to_dict(), status=201)
                return
            if path == "/release-e2e":
                task = self.runtime.submit_release_e2e_task(
                    project_root=payload.get("projectRoot") or payload.get("project_root") or ".",
                    run_label=payload.get("runLabel") or payload.get("run_label"),
                    allowed_agents=payload.get("allowedSubtaskAgents")
                    or payload.get("allowed_subtask_agents")
                    or payload.get("agents"),
                )
                self.respond(task.to_dict(), status=201)
                return
            if path == "/host-conformance":
                report = evaluate_host_conformance(payload)
                self.respond(report, status=200 if report["passed"] else 422)
                return
            if path == "/loops":
                loop = self.loop_runtime.start_loop(
                    goal=payload.get("goal") or "",
                    project_root=payload.get("projectRoot") or payload.get("project_root") or ".",
                    agent=payload.get("agent") or "owner",
                    max_turns=payload.get("maxTurns") or payload.get("max_turns") or 8,
                    memory_policy=payload.get("memoryPolicy") or payload.get("memory_policy"),
                    approval_policy=payload.get("approvalPolicy") or payload.get("approval_policy"),
                    metadata=payload.get("metadata"),
                )
                self.respond(loop.to_dict(), status=201)
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "run":
                task = self.runtime.run_task(parts[1])
                self.respond(task.to_dict())
                return
            if len(parts) == 3 and parts[0] == "loops" and parts[2] == "run":
                loop = self.loop_runtime.run_loop(parts[1])
                self.respond(loop.to_dict())
                return
            if len(parts) == 3 and parts[0] == "loops" and parts[2] == "cancel":
                loop = self.loop_runtime.cancel_loop(
                    parts[1],
                    reason=payload.get("reason"),
                    cancel_category=payload.get("cancelCategory") or payload.get("cancel_category"),
                )
                self.respond(loop.to_dict())
                return
            if len(parts) == 5 and parts[0] == "loops" and parts[2] == "actions" and parts[4] == "approve":
                loop = self.loop_runtime.approve_action(parts[1], parts[3])
                self.respond(loop.to_dict())
                return
            if len(parts) == 5 and parts[0] == "loops" and parts[2] == "actions" and parts[4] == "reject":
                loop = self.loop_runtime.reject_action(parts[1], parts[3], reason=payload.get("reason"))
                self.respond(loop.to_dict())
                return
            if len(parts) == 5 and parts[0] == "loops" and parts[2] == "steps" and parts[4] == "retry":
                loop = self.loop_runtime.retry_step(parts[1], parts[3])
                self.respond(loop.to_dict())
                return
            self.respond({"error": "not_found"}, status=404)
        except KeyError:
            self.respond({"error": "not_found"}, status=404)
        except AgentLoopConcurrencyError as exc:
            self.respond(
                {
                    "error": "max_concurrent_loops_exceeded",
                    "active_count": exc.active_count,
                    "max_concurrent_loops": exc.max_concurrent_loops,
                },
                status=409,
            )
        except ValueError as exc:
            self.respond({"error": "bad_request", "detail": str(exc)}, status=400)
        except Exception:
            self.respond({"error": "internal_error"}, status=500)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw)

    def respond(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_sse(self, events: list[dict[str, Any]]) -> None:
        chunks = []
        for event in events:
            chunks.append(f"event: {event.get('type', 'message')}\n")
            chunks.append(f"data: {json.dumps(event, sort_keys=True)}\n\n")
        body = "".join(chunks).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_loop_sse(self, loop_id: str, *, after_sequence: int | None = None) -> None:
        self.loop_runtime.get_loop(loop_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        sent_keys: set[str] = set()
        idle_deadline = time.time() + 30
        while True:
            events = self.loop_runtime.list_loop_events(loop_id, after_sequence=after_sequence)
            new_events = [event for event in events if _event_key(event) not in sent_keys]
            if new_events:
                idle_deadline = time.time() + 30

            closing_seen = False
            for event in new_events:
                sent_keys.add(_event_key(event))
                if not self.write_sse_event(event):
                    return
                if event.get("type") in LOOP_STREAM_CLOSING_EVENT_TYPES:
                    closing_seen = True

            if closing_seen:
                return
            if time.time() >= idle_deadline:
                self.write_sse_comment("idle_timeout")
                return
            time.sleep(0.1)

    def write_sse_event(self, event: dict[str, Any]) -> bool:
        event_type = str(event.get("type") or "message")
        chunk = f"event: {event_type}\ndata: {json.dumps(event, sort_keys=True)}\n\n".encode("utf-8")
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def write_sse_comment(self, comment: str) -> bool:
        try:
            self.wfile.write(f": {comment}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False


class OrchestratorHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int]):
        super().__init__(server_address, OrchestratorHandler)
        self.runtime = OrchestratorRuntime()
        self.loop_runtime = self.runtime.loop_runtime


def _query_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _query_int(query: dict[str, list[str]], key: str) -> int | None:
    raw_values = query.get(key) or []
    if not raw_values or raw_values[0] == "":
        return None
    return int(raw_values[0])


def _event_key(event: dict[str, Any]) -> str:
    return str(event.get("event_id") or event.get("sequence") or json.dumps(event, sort_keys=True))


def _runtime_info_path(runtime_id: str, runtime_info: str | None = None) -> Path:
    if runtime_info and runtime_info.strip():
        if not (
            is_product_mode()
            and not is_developer_mode()
            and contains_protected_user_reference(runtime_info)
        ):
            return Path(runtime_info).expanduser().resolve()
    return run_home() / f"{runtime_id}.json"


def _write_runtime_info(server: OrchestratorHTTPServer, host: str, runtime_id: str, runtime_info: str | None) -> Path:
    actual_host, actual_port = server.server_address[:2]
    endpoint_host = host or actual_host
    path = _runtime_info_path(runtime_id, runtime_info)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "componentId": COMPONENT_ID,
        "runtimeId": runtime_id,
        "pid": os.getpid(),
        "host": endpoint_host,
        "port": actual_port,
        "endpoint": f"http://{endpoint_host}:{actual_port}",
        "transport": "http",
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    runtime_id: str | None = None,
    runtime_info: str | None = None,
) -> None:
    server = OrchestratorHTTPServer((host, port))
    info_path = _write_runtime_info(server, host, runtime_id, runtime_info) if runtime_id else None
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if info_path:
            try:
                info_path.unlink()
            except FileNotFoundError:
                pass
