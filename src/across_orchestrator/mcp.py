from __future__ import annotations

from typing import Any
import json
import os
import sys

from . import __version__
from .agui_projection import project_events_to_agui
from .agent_card import render_agent_card
from .agent_team import create_agent_team
from .agent_team_readiness import evaluate_agent_team_readiness
from .a2a_delegation import create_a2a_task_delegation
from .agent_loop import CANCEL_CATEGORY_VALUES, HOST_DECLARED_CHECK_ACTION_PATTERN, SUPPORTED_LOOP_ACTION_TYPES
from .evidence_graph import build_evidence_graph_from_payload
from .external_agents import ExternalAgentRegistry, normalize_agent_plugin_manifest
from .otel_export import export_otel_genai_spans
from .plugin_manifest import render_plugin_manifest, render_plugin_status
from .redaction import redact_sensitive_value
from .runtime import OrchestratorRuntime
from .failures import FAILURE_TYPES
from .remote_mcp import render_remote_mcp_oauth_template
from .sandbox import evaluate_sandbox_policy


def _loop_action_plan_item_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "description": (
            "One loop action type. Use built-in actions or host-declared read-only check actions "
            "ending in _check such as business_contract_check. Object actionPlan entries are invalid."
        ),
        "anyOf": [
            {"enum": sorted(SUPPORTED_LOOP_ACTION_TYPES)},
            {"pattern": HOST_DECLARED_CHECK_ACTION_PATTERN.pattern},
        ],
    }


def agent_loop_metadata_schema() -> dict[str, Any]:
    action_plan_schema = {
        "type": "array",
        "items": _loop_action_plan_item_schema(),
        "description": (
            "Optional ordered list of loop action type strings. Duplicates are allowed. "
            "The turn budget is raised to cover declared actions plus implicit post-dispatch quality gates. "
            "Use simple strings only, for example "
            "[\"memory_search\", \"task_dispatch\", \"business_contract_check\", "
            "\"quality_gate\", \"final_output\"]."
        ),
    }
    autopilot_schema = {
        "type": "object",
        "description": "Autopilot evidence contract metadata required when a host supplies Autopilot provenance.",
        "properties": {
            "schema_version": {
                "type": "string",
                "enum": ["across-loop-spec/1.0"],
                "description": "Must be across-loop-spec/1.0.",
            },
            "run_id": {
                "type": "string",
                "description": "Stable host run id for correlating loop evidence.",
            },
            "evidence_contract": {
                "type": "string",
                "enum": ["across-loop-evidence/1.0"],
                "description": "Must be across-loop-evidence/1.0.",
            },
            "tool_packs": {"type": "array", "items": {"type": "string"}},
            "required_tool_packs": {"type": "array", "items": {"type": "string"}},
            "sandbox": {"type": "object"},
        },
        "required": ["schema_version", "run_id", "evidence_contract"],
    }
    validation_contract_schema = {
        "type": "object",
        "description": (
            "Optional across-validation-contract/1.0 artifact contract consumed by host-declared *_check "
            "actions such as business_contract_check. It is generic: hosts provide domain-specific row, "
            "text, or JSON expectations; Orchestrator only performs deterministic artifact checks."
        ),
        "properties": {
            "schema_version": {"type": "string", "enum": ["across-validation-contract/1.0"]},
            "check_action": {
                "type": "string",
                "description": "Host-declared *_check action that should consume this contract.",
                "pattern": HOST_DECLARED_CHECK_ACTION_PATTERN.pattern,
            },
            "artifacts": {
                "type": "array",
                "description": "Required output artifacts and deterministic checks.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "required": {"type": "boolean"},
                        "type": {"type": "string", "enum": ["json", "csv", "markdown", "text"]},
                        "columns": {"type": "array", "items": {"type": "string"}},
                        "row_count": {"type": "integer"},
                        "min_rows": {"type": "integer"},
                        "sort": {"type": "array", "items": {"type": "object"}},
                        "row_expectations": {"type": "array", "items": {"type": "object"}},
                        "required_keys": {"type": "array", "items": {"type": "string"}},
                        "must_include": {"type": "array", "items": {"type": "string"}},
                        "must_not_include": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["path"],
                },
            },
        },
    }
    return {
        "type": "object",
        "description": (
            "Host metadata for Agent Loop. Use metadata.actionPlan to force an explicit action order; "
            "host-declared *_check actions are recorded as read-only verify steps."
        ),
        "properties": {
            "actionPlan": action_plan_schema,
            "action_plan": {**action_plan_schema, "description": "Snake-case alias for actionPlan."},
            "autopilot": autopilot_schema,
            "actionLeaseSeconds": {"type": "number", "description": "Optional per-loop action lease duration in seconds."},
            "action_lease_seconds": {"type": "number", "description": "Snake-case alias for actionLeaseSeconds."},
            "agentRouting": {"type": "object", "description": "Optional routing hints keyed by action type or failed quality gate."},
            "agent_routing": {"type": "object", "description": "Snake-case alias for agentRouting."},
            "agentCapabilityHints": {
                "type": "object",
                "description": "Optional host-provided capability registry and routing hints; no credentials or install paths.",
            },
            "agent_capability_hints": {"type": "object", "description": "Snake-case alias for agentCapabilityHints."},
            "validationContract": validation_contract_schema,
            "validation_contract": {**validation_contract_schema, "description": "Snake-case alias for validationContract."},
            "recoveryPolicy": {"type": "object", "description": "Optional recovery policy keyed by failure type."},
            "recovery_policy": {"type": "object", "description": "Snake-case alias for recoveryPolicy."},
        },
        "additionalProperties": True,
    }


def agent_plugin_manifest_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "description": (
            "across-agent-plugin/1.0 manifest. Keep arrays flat; do not wrap capabilities, "
            "inputs, outputs, or tags in nested arrays."
        ),
        "properties": {
            "schema_version": {"type": "string", "enum": ["across-agent-plugin/1.0"]},
            "plugin_id": {"type": "string", "description": "Stable plugin id. Alias: id."},
            "id": {"type": "string", "description": "Alias for plugin_id."},
            "display_name": {"type": "string"},
            "version": {"type": "string"},
            "agent": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "vendor": {"type": "string"},
                },
            },
            "agent_id": {"type": "string", "description": "Alias for agent.id."},
            "capabilities": {
                "type": "array",
                "description": "Flat capability list. Each item may be a string id or an object with id, kind, risk, description.",
                "items": {
                    "anyOf": [
                        {"type": "string"},
                        {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "kind": {"type": "string"},
                                "risk": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["id"],
                        },
                    ]
                },
            },
            "entrypoints": {
                "type": "object",
                "description": "Map entrypoint names to command or url specs. Use run for dispatch-capable plugins.",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "array",
                            "description": "Direct executable argv array, not a shell string. Example: [\"printf\", \"ready\\n\"].",
                            "items": {"type": "string"},
                        },
                        "url": {"type": "string", "description": "Localhost http URL or https URL."},
                        "transport": {"type": "string", "enum": ["stdio", "http"]},
                        "timeout_seconds": {"type": "integer"},
                    },
                },
            },
            "trust": {
                "type": "object",
                "properties": {
                    "mutation_boundary": {
                        "type": "string",
                        "enum": ["read_only", "candidate_workspace", "host_approved_mutation", "network_only", "manual_only"],
                    },
                    "requires_human_approval": {"type": "boolean"},
                    "secrets_included": {"type": "boolean"},
                    "network_access": {"type": "string"},
                    "credential_boundary": {"type": "string"},
                },
            },
            "context": {
                "type": "object",
                "properties": {
                    "pack_id": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "required": ["schema_version", "plugin_id", "entrypoints"],
    }


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
            "name": "evaluate_sandbox_policy",
            "description": "Evaluate an across-sandbox-policy/1.0 policy and optional argv boundary without executing commands.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "policy": {"type": "object"},
                    "command": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"},
                },
                "required": ["policy"],
            },
        },
        {
            "name": "build_evidence_graph",
            "description": "Build or verify an across-evidence-graph/1.0 graph from task, loop, or Autopilot evidence.",
            "inputSchema": {
                "type": "object",
                "properties": {"payload": {"type": "object"}},
                "required": ["payload"],
            },
        },
        {
            "name": "evaluate_agent_team_readiness",
            "description": "Evaluate whether a Workflow Pack export is market-ready for generic agent-team adoption with product card, trust receipt, and honest protocol readiness.",
            "inputSchema": {
                "type": "object",
                "properties": {"payload": {"type": "object"}},
                "required": ["payload"],
            },
        },
        {
            "name": "render_remote_mcp_oauth_template",
            "description": "Render a secret-free Streamable HTTP/OAuth template for remote MCP deployment planning.",
            "inputSchema": {
                "type": "object",
                "properties": {"config": {"type": "object"}},
            },
        },
        {
            "name": "create_a2a_task_delegation",
            "description": "Create an LF-compatible A2A task, message, artifact, streaming, push notification, and evidence receipt projection without calling a remote agent.",
            "inputSchema": {
                "type": "object",
                "properties": {"payload": {"type": "object"}},
            },
        },
        {
            "name": "project_agui_events",
            "description": "Project Across task or loop events into AG-UI task-card events for external web or desktop clients.",
            "inputSchema": {
                "type": "object",
                "properties": {"payload": {"type": "object"}},
            },
        },
        {
            "name": "create_agent_team",
            "description": "Create a first-class agent-team contract with independent sessions, checkpoints, context refs, and handoff notes.",
            "inputSchema": {
                "type": "object",
                "properties": {"payload": {"type": "object"}},
            },
        },
        {
            "name": "export_otel_genai_spans",
            "description": "Export Across evidence as OTel/GenAI-style spans plus gate-based eval cases without raw transcripts.",
            "inputSchema": {
                "type": "object",
                "properties": {"payload": {"type": "object"}},
                "required": ["payload"],
            },
        },
        {
            "name": "start_agent_loop",
            "description": (
                "Start a durable agent loop run with context, actions, checkpoints, and memory policy. "
                "To force a host action order, pass metadata.actionPlan as a list of action type strings; "
                "host-declared read-only validation stages may use names ending in _check, for example "
                "business_contract_check."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "projectRoot": {"type": "string"},
                    "agent": {"type": "string", "default": "owner"},
                    "maxTurns": {"type": "integer", "default": 8},
                    "memoryPolicy": {"type": "object"},
                    "approvalPolicy": {"type": "object"},
                    "metadata": agent_loop_metadata_schema(),
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
                    "cancelCategory": {
                        "type": "string",
                        "enum": list(CANCEL_CATEGORY_VALUES),
                    },
                    "cancel_category": {
                        "type": "string",
                        "enum": list(CANCEL_CATEGORY_VALUES),
                    },
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
                "properties": {
                    "loopId": {"type": "string"},
                    "afterSequence": {"type": "integer"},
                    "after_sequence": {"type": "integer"},
                },
                "required": ["loopId"],
            },
        },
        {
            "name": "get_agent_loop_evidence_summary",
            "description": "Fetch a compact read-only Agent Loop evidence summary for routing, recovery, memory candidates, and event audit coverage.",
            "inputSchema": {
                "type": "object",
                "properties": {"loopId": {"type": "string"}},
                "required": ["loopId"],
            },
        },
        {
            "name": "get_agent_loop_telemetry",
            "description": "Fetch bounded Agent Loop telemetry metrics without raw observations or memory text.",
            "inputSchema": {
                "type": "object",
                "properties": {"loopId": {"type": "string"}},
                "required": ["loopId"],
            },
        },
        {
            "name": "validate_external_agent_plugin",
            "description": "Validate and normalize an across-agent-plugin/1.0 manifest for generic external agent loading.",
            "inputSchema": {
                "type": "object",
                "properties": {"manifest": agent_plugin_manifest_schema()},
                "required": ["manifest"],
            },
        },
        {
            "name": "register_external_agent_plugin",
            "description": "Validate and persist an across-agent-plugin/1.0 manifest in the generic external agent registry.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "manifest": agent_plugin_manifest_schema(),
                    "probe": {"type": "boolean", "default": False},
                },
                "required": ["manifest"],
            },
        },
        {
            "name": "list_external_agent_plugins",
            "description": "List registered generic external agent plugins from the Orchestrator registry.",
            "inputSchema": {
                "type": "object",
                "properties": {"probe": {"type": "boolean", "default": False}},
            },
        },
        {
            "name": "get_external_agent_plugin_health",
            "description": "Return health status for registered external agent plugins.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agentId": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "probe": {"type": "boolean", "default": False},
                },
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
        {
            "uri": "across-orchestrator://sandbox-policy",
            "name": "Across Sandbox Policy",
            "description": "Host-neutral sandbox policy shape and enforcement semantics.",
            "mimeType": "application/json",
        },
        {
            "uri": "across-orchestrator://external-agent-plugins",
            "name": "External Agent Plugins",
            "description": "Registered across-agent-plugin/1.0 manifests and health-safe public cards.",
            "mimeType": "application/json",
        },
        {
            "uri": "across-orchestrator://projection-contracts",
            "name": "Across Projection Contracts",
            "description": "Projection-only contracts for MCP Tasks, A2A, AG-UI, Remote MCP/OAuth, and OTel.",
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
        "schemaVersion": "0.4",
        "entities": ["LoopRun", "LoopStep", "LoopAction", "LoopObservation", "Checkpoint"],
        "status": ["pending", "running", "awaiting_approval", "completed", "stopped", "failed", "cancelled"],
        "actions": sorted(SUPPORTED_LOOP_ACTION_TYPES),
        "hostDeclaredCheckActions": {
            "pattern": "^[a-z][a-z0-9_]{0,63}_check$",
            "phase": "verify",
            "sideEffects": False,
            "purpose": "Record host/plugin validation stages such as manifest_contract_check without executing arbitrary host commands.",
            "validationContract": "When metadata.validationContract is present and check_action matches, the check evaluates deterministic artifact rules and blocks the loop on failure.",
        },
        "validationContract": {
            "schemaVersion": "across-validation-contract/1.0",
            "evidenceSchemaVersion": "across-validation-evidence/1.0",
            "consumedBy": "host-declared *_check actions such as business_contract_check",
            "checkTypes": [
                "artifact_presence",
                "json_parse",
                "json_required_key",
                "csv_parse",
                "csv_columns",
                "csv_row_count",
                "csv_min_rows",
                "csv_sort_order",
                "csv_row_expectation",
                "text_must_include",
                "text_must_not_include",
            ],
            "failureBehavior": "blocking by default; recoveryPolicy may route quality_failed to remediation.",
        },
        "failureTypes": list(FAILURE_TYPES),
        "controlActions": ["cancel_agent_loop", "approve_agent_loop_action", "reject_agent_loop_action", "retry_agent_loop_step"],
        "inspectionActions": [
            "get_agent_loop",
            "get_agent_loop_health",
            "get_agent_loop_events",
            "get_agent_loop_evidence_summary",
            "get_agent_loop_telemetry",
        ],
        "cancelCategories": list(CANCEL_CATEGORY_VALUES),
        "events": [
            "loop.started",
            "loop.next_action.selected",
            "loop.cancel_requested",
            "loop.dispatch.detached",
            "loop.budget.exceeded",
            "loop.step.started",
            "loop.step.heartbeat",
            "loop.step.completed",
            "loop.step.cancelled",
            "loop.step.lease_expired",
            "loop.step.recovery_decision",
            "loop.step.recovered",
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
        "eventMetadata": {
            "description": (
                "Every newly appended loop event carries stable audit metadata in addition to "
                "type, loop_id, timestamp, and payload; task events emitted from bound tasks "
                "carry the same event_id, sequence, and correlation_id contract."
            ),
            "fields": [
                "event_id",
                "sequence",
                "correlation_id",
                "step_id",
                "action_id",
                "task_id",
                "causation_id",
            ],
            "correlationId": (
                "step:{step_id}, action:{action_id}, task:{task_id}, or loop:{loop_id}, "
                "in that precedence order."
            ),
            "sequence": "Monotonic per-loop append order for durable JSONL events.",
            "causationId": (
                "Optional passthrough when a future producer includes causation_id, causationId, "
                "parent_event_id, or parentEventId in the payload."
            ),
        },
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
            "routing": (
                "dispatch context block with schema agent-loop-routing/1.0, selected_agent, "
                "base_agent, source, reason, alternatives, and optional matched_gate"
            ),
            "heartbeat": "callable lease renewal hook for long-running dispatch adapters",
            "cancellation": "cooperative token with is_cancelled(), reason(), and raise_if_cancelled() for running dispatch adapters",
        },
        "memoryPolicy": {
            "provider": "across-context",
            "read": "search active memory before planning",
            "writeCandidates": "write durable summaries as pending candidates only",
            "candidateSchema": "agent-loop-memory-candidate/1.0",
            "candidateFields": [
                "loop_id",
                "goal",
                "outcome",
                "decisions",
                "artifacts",
                "commands",
                "failure_types",
                "remediation_outcomes",
                "memory_refs",
            ],
        },
        "recoveryPolicy": {
            "metadataKey": "recoveryPolicy",
            "byFailureTypeKey": "byFailureType",
            "supportedActions": ["stop", "retry", "remediation", "require_human"],
            "events": ["loop.step.recovery_decision", "loop.step.recovered"],
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
                "cancellation_category",
            ],
        },
        "evidenceSummary": {
            "description": (
                "Read-only compact loop evidence derived from durable state; it exposes routing outcomes, "
                "recovery decisions, memory candidate counts, cancellation category, event audit coverage, "
                "action plan completion, and host release evidence without raw logs, transcripts, memory text, or stack traces."
            ),
            "schemaVersion": "0.1",
            "fields": [
                "event_audit",
                "routing",
                "recovery",
                "memory_candidates",
                "action_plan",
                "cancellation",
                "host_release_evidence",
            ],
            "hostReleaseEvidence": {
                "description": "High-level readiness, checks, risks, and next actions derived from compact evidence.",
                "readiness": ["ready", "attention", "blocked"],
                "checkStatuses": ["passed", "attention", "blocked"],
            },
        },
        "telemetry": {
            "schemaVersion": "agent-loop-telemetry/1.0",
            "description": "Bounded aggregate loop metrics; excludes prompts, raw memory text, stack traces, logs, and local absolute paths.",
            "metrics": [
                "loop.duration_ms",
                "loop.turn_count",
                "loop.event_count",
                "loop.routing.outcome_count",
                "loop.routing.capability_hint_route_count",
                "loop.routing.non_default_route_count",
                "loop.recovery.decision_count",
                "loop.recovery.applied_count",
                "loop.memory_candidate.produced_count",
                "loop.cancellation.requested_count",
                "loop.budget.turns_remaining",
            ],
        },
        "streamResume": {
            "events": "GET /loops/{loop_id}/events?after_sequence=N",
            "stream": "GET /loops/{loop_id}/events/stream?follow=true&after_sequence=N",
            "mcpTool": "get_agent_loop_events accepts afterSequence.",
        },
        "budgetPolicy": {
            "schemaVersion": "agent-loop-budget/1.0",
            "metadataKeys": ["agentLoopBudget", "agent_loop_budget", "budget"],
            "fields": ["maxConcurrentLoops", "maxTurnsPerLoop", "maxRuntimeSeconds"],
            "budgetExceededCategory": "budget_exceeded",
        },
        "approvalPolicy": {
            "requireApprovalFor": ["tool_call", "task_dispatch", "memory_write_candidate"]
        },
        "metadata": {
            "schema": agent_loop_metadata_schema(),
            "actionPlan": "optional ordered list of supported action type strings; duplicates are allowed; host-declared *_check actions are read-only verify steps; turn budget is raised to the minimum needed for declared actions plus implicit post-dispatch quality gates",
            "action_plan": "snake-case alias for actionPlan",
            "autopilot": "optional Autopilot provenance object; when present, schema_version must be across-loop-spec/1.0 and evidence_contract must be across-loop-evidence/1.0",
            "actionLeaseSeconds": "optional per-loop action lease duration in seconds; default is 300",
            "action_lease_seconds": "snake-case alias for actionLeaseSeconds",
            "agentRouting": "optional mapping from action type or failed quality gate to selected dispatch agent",
            "agent_routing": "snake-case alias for agentRouting",
            "agentCapabilityHints": "optional host-provided capability registry and routing hints; contains no credentials or install paths",
            "agent_capability_hints": "snake-case alias for agentCapabilityHints",
            "recoveryPolicy": "optional recovery policy keyed by failure_type; default behavior remains fail-fast",
            "recovery_policy": "snake-case alias for recoveryPolicy",
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
    if name == "evaluate_sandbox_policy":
        return evaluate_sandbox_policy(
            arguments.get("policy") or {},
            command=arguments.get("command"),
            cwd=arguments.get("cwd"),
        )
    if name == "build_evidence_graph":
        return build_evidence_graph_from_payload(arguments.get("payload") or {})
    if name == "evaluate_agent_team_readiness":
        return evaluate_agent_team_readiness(arguments.get("payload") or {})
    if name == "render_remote_mcp_oauth_template":
        return render_remote_mcp_oauth_template(arguments.get("config") or {})
    if name == "create_a2a_task_delegation":
        return create_a2a_task_delegation(arguments.get("payload") or {})
    if name == "project_agui_events":
        return project_events_to_agui(arguments.get("payload") or {})
    if name == "create_agent_team":
        return create_agent_team(arguments.get("payload") or {})
    if name == "export_otel_genai_spans":
        return export_otel_genai_spans(arguments.get("payload") or {})
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
        return loop_runtime.cancel_loop(
            arguments["loopId"],
            reason=arguments.get("reason"),
            cancel_category=arguments.get("cancelCategory") or arguments.get("cancel_category"),
        ).to_dict()
    if name == "retry_agent_loop_step":
        return loop_runtime.retry_step(arguments["loopId"], arguments["stepId"]).to_dict()
    if name == "get_agent_loop":
        return loop_runtime.get_loop(arguments["loopId"]).to_dict()
    if name == "get_agent_loop_health":
        return loop_runtime.get_loop_health(arguments["loopId"])
    if name == "get_agent_loop_events":
        return loop_runtime.list_loop_events(
            arguments["loopId"],
            after_sequence=arguments.get("afterSequence") or arguments.get("after_sequence"),
        )
    if name == "get_agent_loop_evidence_summary":
        return loop_runtime.get_loop_evidence_summary(arguments["loopId"])
    if name == "get_agent_loop_telemetry":
        return loop_runtime.get_loop_telemetry(arguments["loopId"])
    if name == "validate_external_agent_plugin":
        return normalize_agent_plugin_manifest(arguments.get("manifest") or {})
    if name == "register_external_agent_plugin":
        return ExternalAgentRegistry().register_manifest(
            arguments.get("manifest") or {},
            probe=bool(arguments.get("probe")),
        )
    if name == "list_external_agent_plugins":
        return ExternalAgentRegistry().registry_payload(probe=bool(arguments.get("probe")))
    if name == "get_external_agent_plugin_health":
        return ExternalAgentRegistry().health_payload(
            arguments.get("agentId") or arguments.get("agent_id"),
            probe=bool(arguments.get("probe")),
        )
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
    elif uri == "across-orchestrator://sandbox-policy":
        payload = {
            "schema_version": "across-sandbox-policy/1.0",
            "network_policy": ["none", "adapter_scoped", "allowlist", "unrestricted_requires_approval"],
            "filesystem_policy": ["read_only", "run_scoped", "candidate_workspace_only", "allowlist"],
            "promotion": {
                "human_approval_required": True,
                "merge_release_signing_blocked": True,
            },
            "command_execution": "Commands are never executed by evaluate_sandbox_policy; argv is checked against command_allowlist and workspace_root only.",
        }
    elif uri == "across-orchestrator://external-agent-plugins":
        payload = ExternalAgentRegistry().registry_payload()
    elif uri == "across-orchestrator://projection-contracts":
        payload = {
            "schema_version": "across-external-projection/1.0",
            "runtime_source_of_truth": "across-orchestrator-run-store",
            "projection_only": True,
            "projections": {
                "mcp_tasks": {"schema_version": "across-async-task/1.0", "status": "projection_only"},
                "a2a": {"schema_version": "across-a2a-task-delegation/2.0", "status": "passed"},
                "ag_ui": {"schema_version": "across-agui-projection/1.0", "status": "passed"},
                "remote_mcp_oauth": {"schema_version": "across-remote-mcp-oauth-template/1.0", "status": "passed"},
                "otel": {"schema_version": "across-otel-genai-export/1.0", "status": "passed"},
            },
            "boundaries": {
                "host_credentials": "host_owned",
                "raw_transcripts_included": False,
                "secrets_included": False,
            },
        }
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


def emit_stdio_response(message_id: Any, result: Any = None, error: str | None = None, stdout_fd: int | None = None) -> None:
    payload = redact_sensitive_value(response(message_id, result=result, error=error))
    target_fd = sys.stdout.fileno() if stdout_fd is None else stdout_fd
    os.write(target_fd, json.dumps(payload).encode("utf-8", errors="replace"))
    os.write(target_fd, b"\n")


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
            emit_stdio_response(message_id, result=result)
        except ValueError as exc:
            emit_stdio_response(message_id, error=str(exc))
        except Exception:
            emit_stdio_response(message_id, error="Across Orchestrator MCP request failed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
