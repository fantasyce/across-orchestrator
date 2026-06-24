from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from .store import LocalStore, _atomic_write_json


AGENT_PLUGIN_SCHEMA_VERSION = "across-agent-plugin/1.0"
EXTERNAL_AGENT_REGISTRY_SCHEMA_VERSION = "across-orchestrator-external-agents/1.0"
EXTERNAL_AGENT_HEALTH_SCHEMA_VERSION = "across-orchestrator-external-agent-health/1.0"

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")
_TRUST_BOUNDARIES = {
    "read_only",
    "candidate_workspace",
    "host_approved_mutation",
    "network_only",
    "manual_only",
}


class ExternalAgentRegistry:
    def __init__(self, *, store: LocalStore | None = None, home: str | Path | None = None, env: Mapping[str, str] | None = None):
        self.store = store or LocalStore(home=home, env=env)
        self.registry_dir = self.store.home / "external-agents"
        self.registry_dir.mkdir(parents=True, exist_ok=True)

    def validate_manifest_file(self, path: str | Path) -> dict[str, Any]:
        manifest_path = Path(path).expanduser().resolve()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        return normalize_agent_plugin_manifest(payload, source_path=manifest_path)

    def register_manifest_file(self, path: str | Path) -> dict[str, Any]:
        manifest = self.validate_manifest_file(path)
        return self.register_manifest(manifest)

    def register_manifest(self, manifest: Mapping[str, Any], *, probe: bool = False) -> dict[str, Any]:
        manifest = normalize_agent_plugin_manifest(manifest)
        target = self._manifest_path(str(manifest["plugin_id"]))
        _atomic_write_json(target, manifest)
        return self.registry_payload(self.list_manifests(), probe=probe)

    def list_manifests(self) -> list[dict[str, Any]]:
        manifests: list[dict[str, Any]] = []
        for path in sorted(self.registry_dir.glob("*.json")):
            try:
                manifests.append(normalize_agent_plugin_manifest(json.loads(path.read_text(encoding="utf-8")), source_path=path))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        return manifests

    def registry_payload(self, manifests: list[Mapping[str, Any]] | None = None, *, probe: bool = False) -> dict[str, Any]:
        return render_external_agent_registry(manifests or self.list_manifests(), probe=probe)

    def health_payload(self, agent_id: str | None = None, *, probe: bool = False) -> dict[str, Any]:
        manifests = self.list_manifests()
        if agent_id:
            manifests = [
                manifest
                for manifest in manifests
                if manifest.get("plugin_id") == agent_id or manifest.get("agent", {}).get("id") == agent_id
            ]
        return render_external_agent_health(manifests, probe=probe)

    def _manifest_path(self, plugin_id: str) -> Path:
        safe_id = _SAFE_ID.sub("-", plugin_id).strip(".-") or "agent-plugin"
        return self.registry_dir / f"{safe_id}.json"


def normalize_agent_plugin_manifest(payload: Mapping[str, Any], *, source_path: str | Path | None = None) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("agent plugin manifest must be a JSON object")
    schema = str(payload.get("schema_version") or payload.get("schemaVersion") or "").strip()
    if schema and schema != AGENT_PLUGIN_SCHEMA_VERSION:
        raise ValueError(f"unsupported agent plugin schema: {schema}")
    plugin_id = _required_text(payload.get("plugin_id") or payload.get("id"), "plugin_id")
    agent = _dict(payload.get("agent"))
    agent_id = _required_text(agent.get("id") or payload.get("agent_id") or plugin_id, "agent.id")
    capabilities = [_capability(item) for item in _list(payload.get("capabilities"))][:24]
    entrypoints = _entrypoints(_dict(payload.get("entrypoints")))
    trust = _trust(payload.get("trust"))
    protocols = _protocols(payload.get("protocols"), entrypoints)
    health = _health(payload.get("health"))
    context = _dict(payload.get("context"))
    return {
        "schema_version": AGENT_PLUGIN_SCHEMA_VERSION,
        "plugin_id": plugin_id,
        "display_name": str(payload.get("display_name") or payload.get("displayName") or agent.get("name") or plugin_id),
        "version": str(payload.get("version") or "0.0.0"),
        "kind": str(payload.get("kind") or "agent-plugin"),
        "agent": {
            "id": agent_id,
            "name": str(agent.get("name") or payload.get("display_name") or plugin_id),
            "vendor": str(agent.get("vendor") or payload.get("vendor") or "unknown"),
        },
        "description": str(payload.get("description") or ""),
        "protocols": protocols,
        "capabilities": capabilities,
        "entrypoints": entrypoints,
        "trust": trust,
        "context": {
            "pack_id": str(context.get("pack_id") or context.get("packId") or plugin_id),
            "tags": [str(item) for item in _list(context.get("tags"))[:12]],
        },
        "health": health,
        "source": {"path": str(source_path) if source_path else None},
    }


def render_external_agent_registry(manifests: list[Mapping[str, Any]], *, probe: bool = False) -> dict[str, Any]:
    normalized = [normalize_agent_plugin_manifest(manifest) for manifest in manifests]
    agents = [_agent_card(manifest, probe=probe) for manifest in normalized]
    healthy_count = sum(1 for item in agents if item["health"]["status"] == "passed")
    return {
        "schema_version": EXTERNAL_AGENT_REGISTRY_SCHEMA_VERSION,
        "owner": "across-orchestrator",
        "status": "passed" if agents and healthy_count == len(agents) else "attention" if agents else "unavailable",
        "summary": {
            "agent_count": len(agents),
            "healthy_agent_count": healthy_count,
            "plugin_count": len(normalized),
            "generic_schema": AGENT_PLUGIN_SCHEMA_VERSION,
        },
        "agents": agents,
        "security": {
            "secrets_included": False,
            "commands_run_only_on_probe": True,
            "shell_execution": False,
        },
    }


def render_external_agent_health(manifests: list[Mapping[str, Any]], *, probe: bool = False) -> dict[str, Any]:
    normalized = [normalize_agent_plugin_manifest(manifest) for manifest in manifests]
    results = [
        {
            "plugin_id": manifest["plugin_id"],
            "agent_id": manifest["agent"]["id"],
            "health": _probe_health(manifest, probe=probe),
        }
        for manifest in normalized
    ]
    failed = [item for item in results if item["health"]["status"] == "failed"]
    attention = [item for item in results if item["health"]["status"] != "passed"]
    return {
        "schema_version": EXTERNAL_AGENT_HEALTH_SCHEMA_VERSION,
        "owner": "across-orchestrator",
        "status": "failed" if failed else "attention" if attention else "passed",
        "summary": {
            "agent_count": len(results),
            "healthy_agent_count": sum(1 for item in results if item["health"]["status"] == "passed"),
            "probed": bool(probe),
        },
        "results": results,
    }


def _agent_card(manifest: Mapping[str, Any], *, probe: bool) -> dict[str, Any]:
    return {
        "plugin_id": manifest["plugin_id"],
        "agent_id": manifest["agent"]["id"],
        "name": manifest["agent"]["name"],
        "display_name": manifest["display_name"],
        "version": manifest["version"],
        "vendor": manifest["agent"]["vendor"],
        "protocols": manifest["protocols"],
        "capabilities": [
            {
                "id": item["id"],
                "kind": item["kind"],
                "risk": item["risk"],
            }
            for item in _list(manifest.get("capabilities"))
        ],
        "entrypoints": {name: {"configured": True} for name in _dict(manifest.get("entrypoints")).keys()},
        "trust": manifest["trust"],
        "context": manifest["context"],
        "health": _probe_health(manifest, probe=probe),
    }


def _probe_health(manifest: Mapping[str, Any], *, probe: bool) -> dict[str, Any]:
    health = _dict(manifest.get("health"))
    static_status = str(health.get("status") or health.get("static_status") or "").strip()
    if static_status:
        return {
            "status": _normalize_status(static_status),
            "mode": "static",
            "checked_at": _now(),
            "message": str(health.get("message") or "static health"),
        }
    health_entry = _dict(_dict(manifest.get("entrypoints")).get("health"))
    if not probe:
        return {
            "status": "attention" if health_entry else "passed",
            "mode": "configured" if health_entry else "not_configured",
            "checked_at": _now(),
            "message": "health probe not requested" if health_entry else "no health command required",
        }
    if not health_entry:
        return {"status": "passed", "mode": "not_configured", "checked_at": _now(), "message": "no health command required"}
    command = _command(health_entry)
    timeout = min(max(int(health_entry.get("timeout_seconds") or 5), 1), 15)
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "failed", "mode": "probe", "checked_at": _now(), "message": str(exc)[:200]}
    return {
        "status": "passed" if result.returncode == 0 else "failed",
        "mode": "probe",
        "checked_at": _now(),
        "exit_code": result.returncode,
        "message": (result.stdout or result.stderr or "").strip()[:200],
    }


def _capability(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"id": value, "kind": "agent_capability", "risk": "low"}
    item = _dict(value)
    capability_id = _required_text(item.get("id"), "capability.id")
    return {
        "id": capability_id,
        "kind": str(item.get("kind") or "agent_capability"),
        "risk": str(item.get("risk") or "low"),
        "description": str(item.get("description") or ""),
    }


def _entrypoints(value: Mapping[str, Any]) -> dict[str, Any]:
    entrypoints: dict[str, Any] = {}
    for name, item in value.items():
        if not isinstance(item, Mapping):
            raise ValueError(f"entrypoint {name} must be an object")
        item_dict = _dict(item)
        if "command" in item_dict:
            entrypoints[str(name)] = {
                "command": _command(item_dict),
                "transport": str(item_dict.get("transport") or "stdio"),
                "timeout_seconds": int(item_dict.get("timeout_seconds") or 5),
            }
        elif "url" in item_dict:
            url = str(item_dict.get("url") or "")
            if not url.startswith(("http://127.0.0.1", "http://localhost", "https://")):
                raise ValueError(f"entrypoint {name} url must be localhost or https")
            entrypoints[str(name)] = {"url": url, "transport": str(item_dict.get("transport") or "http")}
        else:
            raise ValueError(f"entrypoint {name} must define command or url")
    return entrypoints


def _command(value: Mapping[str, Any]) -> list[str]:
    raw_command = value.get("command")
    if isinstance(raw_command, list):
        command = [str(item) for item in raw_command]
    else:
        command = [str(raw_command or "")]
        command.extend(str(item) for item in _list(value.get("args")))
    command = [item for item in command if item]
    if not command:
        raise ValueError("entrypoint command is required")
    if any(item in {"sh", "bash", "zsh", "fish"} for item in command[:1]) and any(flag in command for flag in ["-c", "-lc", "-ic"]):
        raise ValueError("agent plugin entrypoints must not use shell command strings")
    return command[:24]


def _trust(value: Any) -> dict[str, Any]:
    item = _dict(value)
    boundary = str(item.get("mutation_boundary") or item.get("boundary") or "read_only")
    if boundary not in _TRUST_BOUNDARIES:
        raise ValueError(f"unsupported mutation boundary: {boundary}")
    return {
        "mutation_boundary": boundary,
        "requires_human_approval": bool(item.get("requires_human_approval", boundary != "read_only")),
        "secrets_included": bool(item.get("secrets_included", False)),
        "network_access": str(item.get("network_access") or "host_policy"),
        "credential_boundary": str(item.get("credential_boundary") or "host_owned"),
    }


def _protocols(value: Any, entrypoints: Mapping[str, Any]) -> list[str]:
    if isinstance(value, Mapping):
        protocols = [str(key) for key, enabled in value.items() if enabled]
    else:
        protocols = [str(item) for item in _list(value)]
    for entrypoint in entrypoints.values():
        transport = str(_dict(entrypoint).get("transport") or "").strip()
        if transport and transport not in protocols:
            protocols.append(transport)
    return sorted(set(protocols or ["agent-plugin"]))


def _health(value: Any) -> dict[str, Any]:
    item = _dict(value)
    status = str(item.get("status") or item.get("static_status") or "").strip()
    return {
        "status": _normalize_status(status) if status else None,
        "message": str(item.get("message") or ""),
    }


def _normalize_status(status: str) -> str:
    text = str(status or "").strip().lower()
    if text in {"ok", "ready", "healthy", "passed", "active"}:
        return "passed"
    if text in {"failed", "error", "unhealthy"}:
        return "failed"
    return "attention"


def _required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
