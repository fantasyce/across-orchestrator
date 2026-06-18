from __future__ import annotations

from typing import Any
import json
import sys

from . import __version__
from .agent_card import render_agent_card
from .plugin_manifest import render_plugin_manifest, render_plugin_status
from .runtime import OrchestratorRuntime
from .failures import FAILURE_TYPES


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
                    "agentAdapters": {
                        "type": "object",
                        "additionalProperties": {"type": "object"},
                        "description": "Map agent ids to adapter specs. Supported types: command, demo, reference.",
                    },
                    "agent_adapters": {
                        "type": "object",
                        "additionalProperties": {"type": "object"},
                        "description": "Snake-case alias for agentAdapters.",
                    },
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
            "description": "Submit the app-grade host agent full delivery conformance scenario.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "projectRoot": {"type": "string"},
                    "runLabel": {"type": "string"},
                    "allowedSubtaskAgents": {"type": "array", "items": {"type": "string"}},
                    "allowed_subtask_agents": {"type": "array", "items": {"type": "string"}},
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
                    "metadata": {"type": "object"},
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
            "name": "approve_agent_loop_action",
            "description": "Approve a pending durable agent loop action and execute the approved adapter step.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "loopId": {"type": "string"},
                    "actionId": {"type": "string"},
                },
                "required": ["loopId", "actionId"],
            },
        },
        {
            "name": "reject_agent_loop_action",
            "description": "Reject a pending durable agent loop action and stop the loop safely.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "loopId": {"type": "string"},
                    "actionId": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["loopId", "actionId"],
            },
        },
        {
            "name": "cancel_agent_loop",
            "description": "Cancel a pending or running durable agent loop.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "loopId": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["loopId"],
            },
        },
        {
            "name": "retry_agent_loop_step",
            "description": "Retry an agent loop by rewinding from a selected step.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "loopId": {"type": "string"},
                    "stepId": {"type": "string"},
                },
                "required": ["loopId", "stepId"],
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
            "name": "get_agent_loop_health",
            "description": "Fetch a read-only health summary for a durable agent loop.",
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
        "schemaVersion": "0.3",
        "entities": ["LoopRun", "LoopStep", "LoopAction", "LoopObservation", "Checkpoint"],
        "status": ["pending", "running", "awaiting_approval", "completed", "stopped", "failed", "cancelled"],
        "actions": ["memory_search", "task_dispatch", "quality_gate", "remediation_dispatch", "memory_write_candidate", "final_output"],
        "failureTypes": list(FAILURE_TYPES),
        "controlActions": ["cancel_agent_loop", "approve_agent_loop_action", "reject_agent_loop_action", "retry_agent_loop_step"],
        "inspectionActions": ["get_agent_loop", "get_agent_loop_health", "get_agent_loop_events"],
        "events": [
            "loop.started",
            "loop.next_action.selected",
            "loop.cancel_requested",
            "loop.dispatch.detached",
            "loop.step.started",
            "loop.step.heartbeat",
            "loop.step.completed",
            "loop.step.cancelled",
            "loop.step.lease_expired",
            "loop.approval_required",
            "loop.action.approved",
            "loop.action.rejected",
            "loop.action.failed",
            "loop.step.retry_requested",
            "loop.completed",
            "loop.stopped",
            "loop.failed",
            "loop.cancelled",
        ],
        "checkpoint": {
            "execution": {
                "description": "Optional execution lease block on running, completed, and failed action checkpoints.",
                "fields": [
                    "lease_id",
                    "started_at",
                    "heartbeat_at",
                    "lease_seconds",
                    "lease_expires_at",
                    "renewal_count",
                    "completed_at",
                    "duration_ms",
                ],
            }
        },
        "context": {
            "routing": "dispatch context block describing selected_agent, base_agent, source, and optional matched_gate",
            "heartbeat": "callable lease renewal hook for long-running dispatch adapters",
            "cancellation": "cooperative token with is_cancelled(), reason(), and raise_if_cancelled() for running dispatch adapters",
        },
        "memoryPolicy": {
            "provider": "across-context",
            "read": "search active memory before planning",
            "writeCandidates": "write durable summaries as pending candidates only",
        },
        "healthSummary": {
            "description": "Read-only loop health snapshot; computing it must not mutate loop state or append events.",
            "fields": [
                "status",
                "current_action_type",
                "pending_approval",
                "lease",
                "detached_dispatch_count",
                "recent_failure_types",
                "executable_actions",
                "cancellation_requested",
            ],
        },
        "approvalPolicy": {
            "requireApprovalFor": ["tool_call", "task_dispatch", "memory_write_candidate"]
        },
        "metadata": {
            "actionPlan": "optional ordered list of supported action types; duplicates are allowed",
            "action_plan": "snake-case alias for actionPlan",
            "actionLeaseSeconds": "optional per-loop action lease duration in seconds; default is 300",
            "action_lease_seconds": "snake-case alias for actionLeaseSeconds",
            "agentRouting": "optional mapping from action type or failed quality gate to selected dispatch agent",
            "agent_routing": "snake-case alias for agentRouting",
        },
    }


def handle_tool_call(runtime: OrchestratorRuntime, name: str, arguments: dict[str, Any]) -> Any:
    loop_runtime = runtime.loop_runtime
    if name == "submit_task":
        return runtime.submit_task(
            goal=arguments.get("goal") or "",
            project_root=arguments.get("projectRoot") or arguments.get("project_root") or ".",
            deliverables=arguments.get("deliverables") or ["README.md"],
            agent=arguments.get("agent") or "demo",
            subtasks=arguments.get("subtasks") or None,
            strict_dependency=bool(arguments.get("strictDependency") or arguments.get("strict_dependency")),
            task_types=arguments.get("taskTypes") or arguments.get("task_types") or None,
            agent_adapters=arguments.get("agentAdapters") or arguments.get("agent_adapters") or None,
        ).to_dict()
    if name == "run_task":
        return runtime.run_task(arguments["taskId"]).to_dict()
    if name == "submit_release_e2e_task":
        return runtime.submit_release_e2e_task(
            project_root=arguments.get("projectRoot") or arguments.get("project_root") or ".",
            run_label=arguments.get("runLabel") or arguments.get("run_label"),
            allowed_agents=arguments.get("allowedSubtaskAgents") or arguments.get("allowed_subtask_agents"),
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
            metadata=arguments.get("metadata"),
        ).to_dict()
    if name == "run_agent_loop":
        return loop_runtime.run_loop(arguments["loopId"]).to_dict()
    if name == "approve_agent_loop_action":
        return loop_runtime.approve_action(arguments["loopId"], arguments["actionId"]).to_dict()
    if name == "reject_agent_loop_action":
        return loop_runtime.reject_action(
            arguments["loopId"],
            arguments["actionId"],
            reason=arguments.get("reason"),
        ).to_dict()
    if name == "cancel_agent_loop":
        return loop_runtime.cancel_loop(arguments["loopId"], reason=arguments.get("reason")).to_dict()
    if name == "retry_agent_loop_step":
        return loop_runtime.retry_step(arguments["loopId"], arguments["stepId"]).to_dict()
    if name == "get_agent_loop":
        return loop_runtime.get_loop(arguments["loopId"]).to_dict()
    if name == "get_agent_loop_health":
        return loop_runtime.get_loop_health(arguments["loopId"])
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
        except ValueError as exc:
            print(json.dumps(response(message_id, error=str(exc))), flush=True)
        except Exception:
            print(json.dumps(response(message_id, error="Across Orchestrator MCP request failed.")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
