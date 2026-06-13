from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .plugin_manifest import render_plugin_manifest


HOST_PROVIDE_CHECKS = {
    "registered_agent_containers": "Host contract must include at least one registered agent.",
    "agent_execution": "Host contract must expose at least one executable agent protocol.",
    "credentials": "Host contract must declare the credential provider used for agent execution.",
    "user_permissions": "Host contract must declare the permission provider used for guarded actions.",
    "tenant_and_project_context": "Host contract must include project context for scoped task state.",
}


PLUGIN_PROVIDE_CAPABILITIES = {
    "task_contracts": "contracts",
    "wave_orchestration": "taskOrchestration",
    "execution_state": "eventStreaming",
    "agent_loop_runtime": "agentLoopRuntime",
    "checkpoints": "checkpoints",
    "human_approval_gates": "humanApproval",
    "memory_hooks": "memoryHooks",
    "evidence_bundles": "evidenceBundles",
    "quality_gates": "qualityBenchmarks",
}


def load_host_contract(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def evaluate_host_conformance(
    host_contract: Mapping[str, Any] | Any,
    *,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    contract = _as_mapping(host_contract)
    plugin_manifest = dict(manifest or render_plugin_manifest())
    hosting_platform = dict(plugin_manifest.get("hostingPlatform") or {})
    host_provides = [str(item) for item in hosting_platform.get("hostProvides") or []]
    plugin_provides = [str(item) for item in hosting_platform.get("pluginProvides") or []]
    capabilities = dict(plugin_manifest.get("capabilities") or {})

    missing_host_provides = [
        item for item in host_provides
        if item in HOST_PROVIDE_CHECKS and not _host_provide_satisfied(item, contract)
    ]
    unsupported_plugin_provides = [
        item for item in plugin_provides
        if not capabilities.get(PLUGIN_PROVIDE_CAPABILITIES.get(item, item), False)
    ]
    missing_protocols = [
        name for name, enabled in _protocol_report(plugin_manifest).items()
        if not enabled
    ]
    errors = [
        HOST_PROVIDE_CHECKS[item]
        for item in missing_host_provides
    ]
    errors.extend(
        f"Plugin manifest declares {item} without the backing capability."
        for item in unsupported_plugin_provides
    )
    errors.extend(f"Plugin manifest is missing {item} protocol metadata." for item in missing_protocols)

    agents = _agents(contract)
    return {
        "passed": not errors,
        "pluginId": str(plugin_manifest.get("id") or ""),
        "pluginApiVersion": str(plugin_manifest.get("pluginApiVersion") or ""),
        "manifestCompatibility": dict(plugin_manifest.get("compatibility") or {}),
        "host": {
            "platformId": str(contract.get("platform_id") or contract.get("platformId") or ""),
            "agentCount": len(agents),
            "approvalMode": str(contract.get("approval_mode") or contract.get("approvalMode") or ""),
            "memoryProvider": contract.get("memory_provider") or contract.get("memoryProvider"),
        },
        "protocols": _protocol_report(plugin_manifest),
        "hostProvides": host_provides,
        "pluginProvides": plugin_provides,
        "missingHostProvides": missing_host_provides,
        "unsupportedPluginProvides": unsupported_plugin_provides,
        "errors": errors,
        "warnings": _warnings(contract),
    }


def _as_mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    raise TypeError("host_contract must be a mapping or expose to_dict()")


def _agents(contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    agents = contract.get("agents") or []
    if not isinstance(agents, list):
        return []
    return [dict(item) for item in agents if isinstance(item, Mapping)]


def _host_provide_satisfied(item: str, contract: Mapping[str, Any]) -> bool:
    agents = _agents(contract)
    if item == "registered_agent_containers":
        return bool(agents) and all(_agent_id(agent) and _agent_name(agent) for agent in agents)
    if item == "agent_execution":
        return any(_agent_protocols(agent) for agent in agents)
    if item == "credentials":
        return bool(contract.get("credentials_provider") or contract.get("credentialsProvider") or contract.get("credentials"))
    if item == "user_permissions":
        return bool(contract.get("permissions_provider") or contract.get("permissionsProvider") or contract.get("permissions"))
    if item == "tenant_and_project_context":
        context = contract.get("project_context") or contract.get("projectContext")
        return isinstance(context, Mapping) and bool(context)
    return True


def _agent_id(agent: Mapping[str, Any]) -> str:
    return str(agent.get("agent_id") or agent.get("id") or "").strip()


def _agent_name(agent: Mapping[str, Any]) -> str:
    return str(agent.get("display_name") or agent.get("name") or _agent_id(agent)).strip()


def _agent_protocols(agent: Mapping[str, Any]) -> list[str]:
    protocols = agent.get("protocols") or []
    if isinstance(protocols, str):
        protocols = [protocols]
    return [str(item).strip() for item in protocols if str(item).strip()]


def _protocol_report(manifest: Mapping[str, Any]) -> dict[str, bool]:
    protocols = dict(manifest.get("protocols") or {})
    entrypoints = dict(manifest.get("entrypoints") or {})
    return {
        "http": bool(protocols.get("http") and entrypoints.get("sidecar")),
        "mcp": bool(protocols.get("mcp") and entrypoints.get("mcp")),
        "sdk": bool(protocols.get("sdk") and entrypoints.get("sdk")),
    }


def _warnings(contract: Mapping[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not (contract.get("memory_provider") or contract.get("memoryProvider")):
        warnings.append("Host contract does not declare a memory provider; memory hooks will use plugin defaults.")
    agents_without_tenant = [
        _agent_id(agent)
        for agent in _agents(contract)
        if not (agent.get("tenant_id") or agent.get("tenantId"))
    ]
    if agents_without_tenant:
        warnings.append("Some host agents do not declare tenant scope.")
    return warnings
