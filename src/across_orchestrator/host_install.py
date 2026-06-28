from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .paths import COMPONENT_ID, ecosystem_bin_dir
from .plugin_manifest import install_managed_plugin


def install_agent_host(
    target: str,
    *,
    config_file: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    normalized = str(target or "").strip().lower()
    source = env if env is not None else os.environ
    runtime = install_managed_plugin(source, force=False)
    if normalized in {"codex", "codex-mcp"}:
        return {
            "target": "codex-mcp",
            "command": f"codex mcp add {COMPONENT_ID} -- sh -lc {_shell_quote(_host_mcp_script(source))}",
            "runtime": runtime,
        }
    if normalized in {"claude", "claude-code"}:
        return {
            "target": "claude-code",
            "command": f"claude mcp add -s user {COMPONENT_ID} -- sh -lc {_shell_quote(_host_mcp_script(source))}",
            "runtime": runtime,
        }
    if normalized == "claude-desktop":
        path = Path(config_file or _default_claude_desktop_config_file(env)).expanduser().resolve()
        payload = _read_json_file(path)
        payload["mcpServers"] = {
            **dict(payload.get("mcpServers") or {}),
            COMPONENT_ID: {
                "command": "sh",
                "args": ["-lc", _host_mcp_script(source)],
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"target": "claude-desktop", "path": str(path), "runtime": runtime}
    raise ValueError(f"Unknown install target: {target}")


def _default_claude_desktop_config_file(env: Mapping[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    return Path(source.get("HOME") or str(Path.home())) / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except FileNotFoundError:
        return {}


def _host_mcp_script(env: Mapping[str, str]) -> str:
    command = ecosystem_bin_dir(env) / COMPONENT_ID
    home_default = Path(env.get("HOME") or str(Path.home())).expanduser().resolve() / ".across" / "bin" / COMPONENT_ID
    if command.resolve() == home_default:
        return f'exec "$HOME/.across/bin/{COMPONENT_ID}" mcp'
    return f"exec {_shell_quote(str(command))} mcp"


def _shell_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"
