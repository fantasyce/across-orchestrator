from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Mapping

from . import __version__
from .paths import (
    COMPONENT_ID,
    cache_home,
    component_data_home,
    config_home,
    ecosystem_bin_dir,
    ecosystem_home,
    logs_home,
    plugin_root,
    run_home,
)
from .store import LocalStore


def render_plugin_manifest(command: str = "across-orchestrator") -> dict:
    return {
        "schemaVersion": "1.0",
        "pluginApiVersion": "2026-06-10",
        "id": "across-orchestrator",
        "displayName": "Across Orchestrator",
        "kind": "task-runtime",
        "version": __version__,
        "description": "Sidecar-first task orchestration runtime with MCP, CLI, and SDK adapters.",
        "capabilities": {
            "taskOrchestration": True,
            "contracts": True,
            "evidenceBundles": True,
            "qualityBenchmarks": True,
            "eventStreaming": True,
            "hostingPlatformAdapters": True,
            "localFirst": True,
        },
        "compatibility": {
            "requiredHostVersion": ">=0.6.0",
            "pluginApiVersion": "2026-06-10",
            "compatiblePluginApiVersions": ["2026-06-10"],
        },
        "permissions": {
            "filesystem": [
                {"path": "~/.across/data/across-orchestrator", "access": "read-write", "reason": "Task state, events, and evidence"},
                {"path": "~/.across/plugins/across-orchestrator", "access": "read", "reason": "Managed plugin runtime"},
                {"path": "~/.across/run/across-orchestrator", "access": "read-write", "reason": "Local sidecar runtime metadata"},
            ],
            "network": [
                {"host": "127.0.0.1", "reason": "Local HTTP sidecar only"}
            ],
            "secrets": [],
        },
        "diagnostics": {
            "startupSafe": True,
            "startsProcess": False,
            "statusCommandSafeAtStartup": True,
            "healthMayInitializeStore": True,
        },
        "lifecycle": {
            "install": {
                "hostManaged": True,
                "strategy": "python-venv",
                "idempotent": True,
            },
            "upgrade": {
                "hostManaged": True,
                "strategy": "reinstall",
            },
            "repair": {
                "hostManaged": True,
                "strategy": "reinstall",
            },
            "uninstall": {
                "hostManaged": True,
                "command": command,
                "args": ["plugin-uninstall", "--json"],
                "removesRuntime": True,
                "preservesData": True,
            },
        },
        "entrypoints": {
            "sidecar": {
                "command": command,
                "args": ["serve", "--host", "127.0.0.1"],
                "healthPath": "/health",
                "agentCardPath": "/.well-known/agent-card.json",
                "pluginManifestPath": "/.well-known/across-plugin.json",
            },
            "mcp": {
                "command": command,
                "args": ["mcp"],
                "transport": "stdio",
            },
            "cli": {
                "command": command,
            },
            "status": {
                "command": command,
                "args": ["plugin-status", "--json"],
            },
            "health": {
                "command": command,
                "args": ["health", "--json"],
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
        "hostingPlatform": {
            "role": "task-runtime",
            "hostProvides": [
                "registered_agent_containers",
                "agent_execution",
                "credentials",
                "user_permissions",
                "tenant_and_project_context",
            ],
            "pluginProvides": [
                "task_contracts",
                "wave_orchestration",
                "execution_state",
                "evidence_bundles",
                "quality_gates",
            ],
        },
    }


def render_plugin_status(command: str = "across-orchestrator", env: Mapping[str, str] | None = None) -> dict:
    source = env if env is not None else os.environ
    home = ecosystem_home(source)
    plugin_dir = plugin_root(source) / COMPONENT_ID
    manifest_path = plugin_dir / "manifest.json"
    command_path = shutil.which(command, path=source.get("PATH")) or str(ecosystem_bin_dir(source) / command)
    command_available = Path(command_path).is_file() or shutil.which(command, path=source.get("PATH")) is not None
    manifest_exists = manifest_path.is_file()
    installed = manifest_exists or command_available
    store = LocalStore(env=source)
    return {
        "pluginId": COMPONENT_ID,
        "status": "installed" if installed else "not_installed",
        "installed": installed,
        "available": command_available,
        "command": command_path,
        "commandExists": command_available,
        "manifestPath": str(manifest_path),
        "manifestExists": manifest_exists,
        "dataPath": str(component_data_home(env=source)),
        "taskCount": len(store.list_task_ids()),
        "paths": {
            "home": str(home),
            "plugin": str(plugin_dir),
            "bin": str(ecosystem_bin_dir(source)),
            "data": str(component_data_home(env=source)),
            "config": str(config_home(env=source)),
            "run": str(run_home(env=source)),
            "logs": str(logs_home(env=source)),
            "cache": str(cache_home(env=source)),
        },
        "protocols": ["http", "mcp", "cli", "sdk"],
        "install": {
            "installable": True,
            "command": "python3 -m pip install across-orchestrator",
            "installDir": str(plugin_dir),
        },
        "lifecycle": {
            "actions": ["install", "upgrade", "repair", "uninstall"],
            "preservesDataOnUninstall": True,
        },
    }


def render_plugin_health(env: Mapping[str, str] | None = None) -> dict:
    source = env if env is not None else os.environ
    store = LocalStore(env=source)
    return {
        "status": "ok",
        "pluginId": COMPONENT_ID,
        "home": str(store.home),
        "taskCount": len(store.list_task_ids()),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def uninstall_managed_plugin(env: Mapping[str, str] | None = None) -> dict:
    source = env if env is not None else os.environ
    plugin_dir = plugin_root(source) / COMPONENT_ID
    wrapper = ecosystem_bin_dir(source) / "across-orchestrator"
    shutil.rmtree(plugin_dir, ignore_errors=True)
    try:
        wrapper.unlink()
    except FileNotFoundError:
        pass
    return {
        "pluginId": COMPONENT_ID,
        "removed": True,
        "pluginDir": str(plugin_dir),
        "wrapper": str(wrapper),
        "preservedData": str(component_data_home(env=source)),
    }
