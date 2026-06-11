from __future__ import annotations

from typing import Any
import json
import sys

from . import __version__
from .agent_card import render_agent_card
from .agent_loop import AgentLoopRuntime
from .plugin_manifest import render_plugin_manifest, render_plugin_status
from .runtime import OrchestratorRuntime


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "submit_task",
            "description": "Submit a delivery task to Across Orchestrator.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "projectRoot": {"type": "string"},
                    "deliverables": {"type": "array", "items": {"type": "string"}},
                    "agent": {"type": "string", "default": "demo"},
                    "subtasks": {"type": "array", "items": {"type": "object"}},
                    "strictDependency": {"type": "boolean", "default": False},
                    "strict_dependency": {"type": "boolean", "default": False},
                    "taskTypes": {"type": "array", "items": {"type": "string"}},
                    "task_types": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["goal", "projectRoot"],
            },
        },
        {
            "name": "run_task",
            "description": "Run pending subtasks for a task.",
            "inputSchema": {
                "type": "object",
                "properties": {"taskId": {"type": "string"}},
                "required": ["taskId"],
            },
        },
        {
            "name": "submit_release_e2e_task",
            "description": "Submit the app-grade Across Agents Assistant release E2E parity scenario.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "projectRoot": {"type": "string"},
                    "runLabel": {"type": "string"},
                },
                "required": ["projectRoot"],
            },
        },
        {
            "name": "get_task",
            "description": "Fetch task state.",
            "inputSchema": {
                "type": "object",
                "properties": {"taskId": {"type": "string"}},
                "required": ["taskId"],
            },
        },
        {
            "name": "get_evidence_bundle",
            "description": "Fetch task evidence bundle.",
            "inputSchema": {
                "type": "object",
                "properties": {"taskId": {"type": "string"}},
                "required": ["taskId"],
            },
        },
        {
            "name": "get_agent_card",
            "description": "Fetch the Across Orchestrator Agent Card.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "start_agent_loop",
            "description": "Start a durable agent loop run with context, actions, checkpoints, and memory policy.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "projectRoot": {"type": "string"},
                    "agent": {"type": "string", "default": "owner"},
                    "maxTurns": {"type": "integer", "default": 8},
                    "memoryPolicy": {"type": "object"},
                    "approvalPolicy": {"type": "object"},
                },
                "required": ["goal", "projectRoot"],
            },
        },
        {
            "name": "run_agent_loop",
            "description": "Run or continue a durable agent loop until completion, approval wait, or turn budget stop.",
            "inputSchema": {
                "type": "object",
                "properties": {"loopId": {"type": "string"}},
                "required": ["loopId"],
            },
        },
        {
            "name": "get_agent_loop",
            "description": "Fetch durable agent loop state.",
            "inputSchema": {
                "type": "object",
                "properties": {"loopId": {"type": "string"}},
                "required": ["loopId"],
            },
        },
        {
            "name": "get_agent_loop_events",
            "description": "Fetch durable agent loop events.",
            "inputSchema": {
                "type": "object",
                "properties": {"loopId": {"type": "string"}},
                "required": ["loopId"],
            },
        },
    ]


def resource_definitions() -> list[dict[str, Any]]:
    return [
        {
            "uri": "across-orchestrator://agent-card",
            "name": "Across Orchestrator Agent Card",
            "description": "A2A-style task runtime capability card.",
            "mimeType": "application/json",
        },
        {
            "uri": "across-orchestrator://plugin-manifest",
            "name": "Across Orchestrator Plugin Manifest",
            "description": "Across plugin discovery manifest for hosts.",
            "mimeType": "application/json",
        },
        {
            "uri": "across-orchestrator://plugin-status",
            "name": "Across Orchestrator Plugin Status",
            "description": "Resolved local plugin install and runtime status.",
            "mimeType": "application/json",
        },
        {
            "uri": "across-orchestrator://agent-loop-schema",
            "name": "Across Agent Loop Schema",
            "description": "Stable loop run, step, action, observation, checkpoint, and memory-hook contract.",
            "mimeType": "application/json",
        },
    ]


def text_result(payload: Any) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, indent=2, sort_keys=True),
            }
        ]
    }


def agent_loop_schema() -> dict[str, Any]:
    return {
        "schemaVersion": "0.1",
        "entities": ["LoopRun", "LoopStep", "LoopAction", "LoopObservation", "Checkpoint"],
        "status": ["pending", "running", "awaiting_approval", "completed", "stopped", "failed"],
        "actions": ["memory_search", "task_dispatch", "quality_gate", "memory_write_candidate", "final_output"],
        "memoryPolicy": {
            "provider": "across-context",
            "read": "search active memory before planning",
            "writeCandidates": "write durable summaries as pending candidates only",
        },
        "approvalPolicy": {
            "requireApprovalFor": ["tool_call", "task_dispatch", "memory_write_candidate"]
        },
    }


def handle_tool_call(runtime: OrchestratorRuntime, name: str, arguments: dict[str, Any]) -> Any:
    loop_runtime = AgentLoopRuntime(runtime.store)
    if name == "submit_task":
        return runtime.submit_task(
            goal=arguments.get("goal") or "",
            project_root=arguments.get("projectRoot") or arguments.get("project_root") or ".",
            deliverables=arguments.get("deliverables") or ["README.md"],
            agent=arguments.get("agent") or "demo",
            subtasks=arguments.get("subtasks") or None,
            strict_dependency=bool(arguments.get("strictDependency") or arguments.get("strict_dependency")),
            task_types=arguments.get("taskTypes") or arguments.get("task_types") or None,
        ).to_dict()
    if name == "run_task":
        return runtime.run_task(arguments["taskId"]).to_dict()
    if name == "submit_release_e2e_task":
        return runtime.submit_release_e2e_task(
            project_root=arguments.get("projectRoot") or arguments.get("project_root") or ".",
            run_label=arguments.get("runLabel") or arguments.get("run_label"),
        ).to_dict()
    if name == "get_task":
        return runtime.get_task(arguments["taskId"]).to_dict()
    if name == "get_evidence_bundle":
        return runtime.evidence_bundle(arguments["taskId"])
    if name == "get_agent_card":
        return render_agent_card()
    if name == "start_agent_loop":
        return loop_runtime.start_loop(
            goal=arguments.get("goal") or "",
            project_root=arguments.get("projectRoot") or arguments.get("project_root") or ".",
            agent=arguments.get("agent") or "owner",
            max_turns=arguments.get("maxTurns") or arguments.get("max_turns") or 8,
            memory_policy=arguments.get("memoryPolicy") or arguments.get("memory_policy"),
            approval_policy=arguments.get("approvalPolicy") or arguments.get("approval_policy"),
        ).to_dict()
    if name == "run_agent_loop":
        return loop_runtime.run_loop(arguments["loopId"]).to_dict()
    if name == "get_agent_loop":
        return loop_runtime.get_loop(arguments["loopId"]).to_dict()
    if name == "get_agent_loop_events":
        return loop_runtime.list_loop_events(arguments["loopId"])
    raise ValueError(f"Unknown tool: {name}")


def read_resource(uri: str) -> dict[str, Any]:
    if uri == "across-orchestrator://agent-card":
        payload = render_agent_card()
    elif uri == "across-orchestrator://plugin-manifest":
        payload = render_plugin_manifest()
    elif uri == "across-orchestrator://plugin-status":
        payload = render_plugin_status()
    elif uri == "across-orchestrator://agent-loop-schema":
        payload = agent_loop_schema()
    else:
        raise ValueError(f"Unknown resource: {uri}")
    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(payload, indent=2, sort_keys=True),
            }
        ]
    }


def response(message_id: Any, result: Any = None, error: str | None = None) -> dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        payload["error"] = {"code": -32000, "message": error}
    else:
        payload["result"] = result
    return payload


def main() -> int:
    runtime = OrchestratorRuntime()
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request.get("method")
        message_id = request.get("id")
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}, "resources": {"listChanged": False}},
                    "serverInfo": {"name": "Across Orchestrator", "version": __version__},
                }
            elif method == "tools/list":
                result = {"tools": tool_definitions()}
            elif method == "resources/list":
                result = {"resources": resource_definitions()}
            elif method == "resources/read":
                result = read_resource((request.get("params") or {}).get("uri") or "")
            elif method == "tools/call":
                params = request.get("params") or {}
                result = text_result(handle_tool_call(runtime, params.get("name"), params.get("arguments") or {}))
            else:
                raise ValueError(f"Unsupported method: {method}")
            print(json.dumps(response(message_id, result=result)), flush=True)
        except Exception:
            print(json.dumps(response(message_id, error="Across Orchestrator MCP request failed.")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
