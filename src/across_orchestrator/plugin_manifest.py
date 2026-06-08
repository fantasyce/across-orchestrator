from __future__ import annotations

from . import __version__


def render_plugin_manifest(command: str = "across-orchestrator") -> dict:
    return {
        "schemaVersion": "1.0",
        "id": "across-orchestrator",
        "displayName": "Across Orchestrator",
        "kind": "task-runtime",
        "version": __version__,
        "description": "Sidecar-first task orchestration runtime with MCP, CLI, and SDK adapters.",
        "entrypoints": {
            "sidecar": {
                "command": command,
                "args": ["serve", "--host", "127.0.0.1"],
                "healthPath": "/health",
                "agentCardPath": "/.well-known/agent-card.json",
            },
            "mcp": {
                "command": command,
                "args": ["mcp"],
                "transport": "stdio",
            },
            "cli": {
                "command": command,
            },
            "sdk": {
                "pythonModule": "across_orchestrator",
            },
        },
        "paths": {
            "plugin": "~/.across/plugins/across-orchestrator",
            "bin": "~/.across/bin",
            "data": "~/.across/data/across-orchestrator",
            "config": "~/.across/config/across-orchestrator",
            "run": "~/.across/run/across-orchestrator",
            "logs": "~/.across/logs/across-orchestrator",
            "cache": "~/.across/cache/across-orchestrator",
        },
        "environment": {
            "ecosystemHome": "ACROSS_HOME",
            "legacyDataOverride": "ACROSS_ORCHESTRATOR_HOME",
            "pluginRoot": "ACROSS_PLUGIN_HOME",
            "binHome": "ACROSS_BIN_HOME",
        },
        "protocols": {
            "http": {
                "transport": "local-sidecar",
                "taskSubmit": "POST /tasks",
                "taskRun": "POST /tasks/{taskId}/run",
                "taskStatus": "GET /tasks/{taskId}",
                "events": "GET /tasks/{taskId}/events",
            },
            "mcp": {
                "transport": "stdio",
                "tools": True,
            },
            "sdk": {
                "language": "python",
            },
        },
    }
