from __future__ import annotations

from . import __version__


def render_agent_card() -> dict:
    return {
        "name": "Across Orchestrator",
        "version": __version__,
        "description": "Local-first task orchestration runtime for agent-to-agent delivery work.",
        "url": "https://github.com/fantasyce/across-orchestrator",
        "capabilities": {
            "taskOrchestration": True,
            "agentLoopRuntime": True,
            "agentLoopV2": True,
            "dynamicLoopPlanning": True,
            "checkpoints": True,
            "humanApproval": True,
            "actionApprovalResume": True,
            "remediationDispatch": True,
            "loopCancellation": True,
            "loopActionRejection": True,
            "loopStepRetry": True,
            "memoryHooks": True,
            "agentLoopMemoryHooksV2": True,
            "contracts": True,
            "artifacts": True,
            "evidenceBundles": True,
            "qualityBenchmarks": True,
            "eventStreaming": True,
            "autopilotMetadataContract": True,
            "autopilotMetadataReflection": True,
            "hostModelDecision": True,
            "hostNeutralAgentAdapters": True,
            "declarativeAgentAdapters": True,
            "externalAgentPluginRegistry": True,
            "genericAgentPluginSchema": True,
            "localFirst": True,
        },
        "protocols": {
            "a2a": {
                "agentCard": "/.well-known/agent-card.json",
                "tasks": True,
                "artifacts": True,
                "statusEvents": True,
            },
            "mcp": {
                "transport": "stdio",
                "command": "across-orchestrator",
                "args": ["mcp"],
                "tools": True,
                "approveAgentLoopAction": True,
                "rejectAgentLoopAction": True,
                "cancelAgentLoop": True,
                "retryAgentLoopStep": True,
            },
            "http": {
                "transport": "local-sidecar",
                "command": "across-orchestrator",
                "args": ["serve", "--host", "127.0.0.1"],
                "health": "/health",
                "loopApprove": "/loops/{loop_id}/actions/{action_id}/approve",
                "loopReject": "/loops/{loop_id}/actions/{action_id}/reject",
                "loopCancel": "/loops/{loop_id}/cancel",
                "loopRetryStep": "/loops/{loop_id}/steps/{step_id}/retry",
                "loopHealth": "/loops/{loop_id}/health",
                "loopEvidenceSummary": "/loops/{loop_id}/evidence-summary",
                "autopilotMetadata": "metadata.autopilot",
                "hostModelDecision": "metadata.model_policy.host_model_command",
            },
        },
        "skills": [
            {
                "id": "agent-loop-runtime",
                "name": "Agent Loop Runtime v2",
                "description": "Run durable goal-action-observation loops with dynamic planning, checkpoints, approval gates, remediation dispatch, and memory hooks.",
            },
            {
                "id": "task-orchestration",
                "name": "Task Orchestration",
                "description": "Submit, run, pause, inspect, and verify multi-artifact delivery tasks.",
            },
            {
                "id": "agent-adapter-specs",
                "name": "Declarative Agent Adapters",
                "description": "Bind arbitrary host agent ids to explicit command, demo, or reference execution adapters.",
            },
            {
                "id": "external-agent-plugins",
                "name": "External Agent Plugin Registry",
                "description": "Register, validate, and expose generic across-agent-plugin/1.0 manifests for arbitrary local or sidecar agents.",
            },
            {
                "id": "delivery-contracts",
                "name": "Delivery Contracts",
                "description": "Track required artifacts and quality gates as explicit task contracts.",
            },
            {
                "id": "evidence-bundles",
                "name": "Evidence Bundles",
                "description": "Export task status, contract, artifacts, quality, and event history.",
            },
            {
                "id": "autopilot-metadata-contract",
                "name": "Autopilot Metadata Contract",
                "description": "Validate and reflect LoopSpec run metadata for Across Autopilot without creating a separate execution state surface.",
            },
            {
                "id": "host-model-decision",
                "name": "Host Model Decision Boundary",
                "description": "Request model-backed loop decisions through a host-declared JSON command while keeping model credentials with the host.",
            },
        ],
        "storage": {
            "defaultHome": "~/.across/data/across-orchestrator",
            "overrideEnv": "ACROSS_ORCHESTRATOR_HOME",
            "acrossHomeEnv": "ACROSS_HOME",
        },
    }
