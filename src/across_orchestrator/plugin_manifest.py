from __future__ import annotations

from contextlib import suppress
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping

from . import __version__
from .paths import (
    COMPONENT_ID,
    cache_home,
    component_data_home,
    contains_protected_user_reference,
    config_home,
    ecosystem_bin_dir,
    ecosystem_home,
    expand_user,
    is_developer_mode,
    is_product_mode,
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
            "evidenceGraph": True,
            "sandboxPolicyEvaluation": True,
            "agentTeamReadiness": True,
            "remoteMcpOAuthTemplate": True,
            "remoteMcpResourceServer": True,
            "mcpTasksProjection": True,
            "a2aTaskDelegation": True,
            "a2aTaskDelegationV2": True,
            "aguiProjection": True,
            "otelGenaiExport": True,
            "agentTeams": True,
            "qualityBenchmarks": True,
            "eventStreaming": True,
            "autopilotMetadataContract": True,
            "autopilotMetadataReflection": True,
            "hostModelDecision": True,
            "agentLoopRuntime": True,
            "agentLoopV2": True,
            "checkpoints": True,
            "humanApproval": True,
            "memoryHooks": True,
            "dynamicLoopPlanning": True,
            "remediationDispatch": True,
            "hostingPlatformAdapters": True,
            "hostNeutralAgentAdapters": True,
            "declarativeAgentAdapters": True,
            "externalAgentPluginRegistry": True,
            "genericAgentPluginSchema": True,
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
            "dataOverride": "ACROSS_ORCHESTRATOR_HOME",
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
                "hostConformance": "POST /host-conformance",
                "loopStart": "POST /loops",
                "loopRun": "POST /loops/{loopId}/run",
                "loopApprove": "POST /loops/{loopId}/actions/{actionId}/approve",
                "loopReject": "POST /loops/{loopId}/actions/{actionId}/reject",
                "loopCancel": "POST /loops/{loopId}/cancel",
                "loopRetryStep": "POST /loops/{loopId}/steps/{stepId}/retry",
                "loopStatus": "GET /loops/{loopId}",
                "loopHealth": "GET /loops/{loopId}/health",
                "loopEvidenceSummary": "GET /loops/{loopId}/evidence-summary",
                "loopEvents": "GET /loops/{loopId}/events",
                "loopAgui": "GET /loops/{loopId}/agui",
                "loopAguiStream": "GET /loops/{loopId}/agui/stream",
                "taskAgui": "GET /tasks/{taskId}/agui",
                "hostModelDecision": "metadata.model_policy.host_model_command",
            },
            "mcp": {
                "transport": "stdio",
                "tools": {
                    "submitTask": "submit_task",
                    "runTask": "run_task",
                    "startAgentLoop": "start_agent_loop",
                    "runAgentLoop": "run_agent_loop",
                    "approveAgentLoopAction": "approve_agent_loop_action",
                    "rejectAgentLoopAction": "reject_agent_loop_action",
                    "cancelAgentLoop": "cancel_agent_loop",
                    "retryAgentLoopStep": "retry_agent_loop_step",
                    "getAgentLoop": "get_agent_loop",
                    "getAgentLoopHealth": "get_agent_loop_health",
                    "getAgentLoopEvents": "get_agent_loop_events",
                    "evaluateSandboxPolicy": "evaluate_sandbox_policy",
                    "evaluateAgentTeamReadiness": "evaluate_agent_team_readiness",
                    "buildEvidenceGraph": "build_evidence_graph",
                    "renderRemoteMcpOauthTemplate": "render_remote_mcp_oauth_template",
                    "createA2aTaskDelegation": "create_a2a_task_delegation",
                    "projectAguiEvents": "project_agui_events",
                    "createAgentTeam": "create_agent_team",
                    "exportOtelGenaiSpans": "export_otel_genai_spans",
                },
            },
            "sdk": {
                "language": "python",
                "hostConformance": "across_orchestrator.host_conformance.evaluate_host_conformance",
            },
        },
        "hostingPlatform": {
            "role": "task-runtime",
            "hostProvides": [
                "registered_agent_containers",
                "agent_execution",
                "credentials",
                "host_model_decision_command",
                "user_permissions",
                "tenant_and_project_context",
            ],
            "pluginProvides": [
                "task_contracts",
                "wave_orchestration",
                "execution_state",
                "agent_adapter_specs",
                "agent_loop_runtime",
                "checkpoints",
                "human_approval_gates",
                "memory_hooks",
                "evidence_bundles",
                "evidence_graph",
                "sandbox_policy_evaluation",
                "agent_team_readiness",
                "remote_mcp_oauth_template",
                "remote_mcp_resource_server",
                "mcp_tasks_projection",
                "a2a_task_delegation",
                "a2a_task_delegation_v2",
                "agui_projection",
                "otel_genai_export",
                "agent_teams",
                "quality_gates",
                "external_agent_plugin_registry",
                "generic_agent_plugin_schema",
            ],
        },
    }


def render_plugin_status(command: str = "across-orchestrator", env: Mapping[str, str] | None = None) -> dict:
    source = env if env is not None else os.environ
    home = ecosystem_home(source)
    plugin_dir = plugin_root(source) / COMPONENT_ID
    manifest_path = plugin_dir / "manifest.json"
    command_path = _resolve_status_command(command, source)
    command_available = Path(command_path).is_file()
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
        "memoryProvider": _memory_provider_status(source),
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


def _resolve_status_command(command: str, source: Mapping[str, str]) -> str:
    if os.path.isabs(command) or os.sep in command:
        if (
            is_product_mode(source)
            and not is_developer_mode(source)
            and contains_protected_user_reference(command, source)
        ):
            return str(ecosystem_bin_dir(source) / Path(command).name)
        candidate = Path(expand_user(command, source))
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else str(ecosystem_bin_dir(source) / Path(command).name)
    for item in str(source.get("PATH") or "").split(os.pathsep):
        if not item:
            continue
        candidate = Path(expand_user(item, source)) / command
        if (
            is_product_mode(source)
            and not is_developer_mode(source)
            and contains_protected_user_reference(str(candidate), source)
        ):
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return str(ecosystem_bin_dir(source) / command)


def _memory_provider_status(source: Mapping[str, str]) -> dict:
    provider = str(source.get("ACROSS_ORCHESTRATOR_MEMORY_PROVIDER") or "").strip().lower()
    if provider in {"none", "disabled", "off", "false", "0"}:
        return {
            "provider": provider,
            "status": "disabled",
            "warnings": [],
        }
    managed_context = ecosystem_bin_dir(source) / "across-context"
    if (
        provider in {"across-context", "across_context"}
        or str(source.get("ACROSS_CONTEXT_COMMAND") or "").strip()
        or (managed_context.is_file() and os.access(managed_context, os.X_OK))
    ):
        from .across_context import diagnose_across_context_command

        return diagnose_across_context_command(
            source,
            recommended_command=str(ecosystem_bin_dir(source) / "across-context"),
        )
    return {
        "provider": provider or "none",
        "status": "disabled",
        "warnings": [],
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


def install_managed_plugin(env: Mapping[str, str] | None = None, *, force: bool = False) -> dict:
    source = env if env is not None else os.environ
    source_root = Path(__file__).resolve().parents[2]
    plugin_dir = plugin_root(source) / COMPONENT_ID
    venv_dir = plugin_dir / "venv"
    wrapper = ecosystem_bin_dir(source) / "across-orchestrator"
    manifest_path = plugin_dir / "manifest.json"
    state_path = plugin_dir / "install-state.json"

    if force:
        shutil.rmtree(venv_dir, ignore_errors=True)

    plugin_dir.mkdir(parents=True, exist_ok=True)
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    entrypoint = venv_dir / "bin" / "across-orchestrator"
    if not entrypoint.is_file() and (source_root / "pyproject.toml").is_file():
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        pip = venv_dir / "bin" / "pip"
        subprocess.run([str(pip), "install", str(source_root)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if entrypoint.is_file():
        wrapper_body = _render_managed_wrapper()
    else:
        wrapper_body = _render_module_wrapper(sys.executable)

    wrapper.write_text(wrapper_body, encoding="utf-8")
    wrapper.chmod(0o755)
    manifest_path.write_text(json.dumps(render_plugin_manifest(str(wrapper)), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    state_path.write_text(
        json.dumps(
            {
                "pluginId": COMPONENT_ID,
                "sourceRoot": str(source_root),
                "pluginDir": str(plugin_dir),
                "wrapper": str(wrapper),
                "venv": str(venv_dir),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "pluginId": COMPONENT_ID,
        "installed": True,
        "pluginDir": str(plugin_dir),
        "wrapper": str(wrapper),
        "venv": str(venv_dir) if entrypoint.is_file() else None,
        "manifestPath": str(manifest_path),
    }


def uninstall_managed_plugin(env: Mapping[str, str] | None = None) -> dict:
    source = env if env is not None else os.environ
    plugin_dir = plugin_root(source) / COMPONENT_ID
    wrapper = ecosystem_bin_dir(source) / "across-orchestrator"
    shutil.rmtree(plugin_dir, ignore_errors=True)
    with suppress(FileNotFoundError):
        wrapper.unlink()
    return {
        "pluginId": COMPONENT_ID,
        "removed": True,
        "pluginDir": str(plugin_dir),
        "wrapper": str(wrapper),
        "preservedData": str(component_data_home(env=source)),
    }


def _render_managed_wrapper() -> str:
    return "\n".join(
        [
            "#!/bin/sh",
            "SCRIPT_DIR=$(CDPATH= cd \"$(dirname \"$0\")\" && pwd)",
            "exec \"$SCRIPT_DIR\"/../plugins/across-orchestrator/venv/bin/across-orchestrator \"$@\"",
            "",
        ]
    )


def _render_module_wrapper(python: str) -> str:
    return "\n".join(
        [
            "#!/bin/sh",
            f"exec {_shell_quote(python)} -m across_orchestrator.cli \"$@\"",
            "",
        ]
    )


def _shell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"
