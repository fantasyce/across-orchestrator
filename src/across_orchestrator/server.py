from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import json
import os
import time

from .agent_card import render_agent_card
from .paths import COMPONENT_ID, run_home
from .runtime import OrchestratorRuntime


class OrchestratorHandler(BaseHTTPRequestHandler):
    server_version = "AcrossOrchestrator/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def runtime(self) -> OrchestratorRuntime:
        return self.server.runtime  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/health":
                self.respond({"status": "ok"})
                return
            if path == "/.well-known/agent-card.json":
                self.respond(render_agent_card())
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
            self.respond({"error": "not_found"}, status=404)
        except KeyError as exc:
            self.respond({"error": str(exc)}, status=404)
        except Exception as exc:
            self.respond({"error": str(exc)}, status=500)

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
                )
                self.respond(task.to_dict(), status=201)
                return
            if path == "/release-e2e":
                task = self.runtime.submit_release_e2e_task(
                    project_root=payload.get("projectRoot") or payload.get("project_root") or ".",
                    run_label=payload.get("runLabel") or payload.get("run_label"),
                )
                self.respond(task.to_dict(), status=201)
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "run":
                task = self.runtime.run_task(parts[1])
                self.respond(task.to_dict())
                return
            self.respond({"error": "not_found"}, status=404)
        except KeyError as exc:
            self.respond({"error": str(exc)}, status=404)
        except Exception as exc:
            self.respond({"error": str(exc)}, status=500)

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


class OrchestratorHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int]):
        super().__init__(server_address, OrchestratorHandler)
        self.runtime = OrchestratorRuntime()


def _runtime_info_path(runtime_id: str, runtime_info: str | None = None) -> Path:
    if runtime_info and runtime_info.strip():
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
