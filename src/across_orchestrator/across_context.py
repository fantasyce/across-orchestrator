from __future__ import annotations

from typing import Any, Mapping, Sequence
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from .paths import ecosystem_bin_dir


class AcrossContextMemoryProvider:
    """Subprocess-backed memory provider for the standalone Across Context CLI."""

    def __init__(self, command: Sequence[str] | None = None, env: Mapping[str, str] | None = None, timeout: int = 20):
        source = env if env is not None else os.environ
        configured = command or _command_from_env(source)
        self.command = [str(item) for item in configured]
        self.env = dict(source)
        if _is_product_mode(self.env):
            self.env.setdefault("ACROSS_CONTEXT_PRODUCT_MODE", "1")
        if _is_developer_mode(self.env):
            self.env.setdefault("ACROSS_CONTEXT_DEVELOPER_MODE", "1")
        self.warnings = _command_warnings(self.command, self.env)
        self.disabled_reason = _disabled_reason(self.warnings, self.env)
        self.timeout = timeout

    def search(self, *, query: str, project_root: str, limit: int = 8, status: str | None = None) -> dict[str, Any]:
        context_root = _context_project_root(project_root)
        args = [
            "search",
            query,
            "--project",
            context_root,
            "--limit",
            str(limit),
            "--json",
        ]
        if status:
            args.extend(["--status", status])
        completed = self._run(args)
        if completed["status"] != "ok":
            return {
                "provider": "across-context",
                "result_count": 0,
                "results": [],
                "error": completed,
            }
        payload = _json_or_empty(completed["stdout"])
        results = payload.get("results") or []
        return {
            "provider": "across-context",
            "result_count": len(results),
            "results": results,
            "query": query,
            "project_root": context_root,
        }

    def remember_candidate(self, *, text: str, project_root: str, tags: list[str] | None = None) -> dict[str, Any]:
        context_root = _context_project_root(project_root)
        args = [
            "remember",
            text,
            "--scope",
            "project",
            "--type",
            "session",
            "--project",
            context_root,
            "--status",
            "pending",
            "--auto",
            "--json",
        ]
        for tag in tags or []:
            args.extend(["--tag", tag])
        completed = self._run(args)
        if completed["status"] != "ok":
            return {
                "provider": "across-context",
                "status": "pending",
                "error": completed,
                "candidate": text,
            }
        payload = _json_or_empty(completed["stdout"])
        return {
            "provider": "across-context",
            "memory": payload.get("memory"),
            "status": (payload.get("memory") or {}).get("status", "pending"),
        }

    def _run(self, args: list[str]) -> dict[str, Any]:
        if self.disabled_reason:
            return {
                "status": "blocked",
                "error": self.disabled_reason,
                "command": _diagnostic_command(self.command, self.env),
                "warnings": self.warnings,
            }
        try:
            completed = subprocess.run(
                [*self.command, *args],
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            return {"status": "missing", "error": str(exc), "command": self.command}
        except subprocess.TimeoutExpired as exc:
            return {"status": "timeout", "error": str(exc), "command": self.command}
        if completed.returncode != 0:
            return {
                "status": "failed",
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "command": self.command,
            }
        return {"status": "ok", "stdout": completed.stdout, "stderr": completed.stderr}


def _json_or_empty(text: str) -> dict[str, Any]:
    try:
        return json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}


def _context_project_root(project_root: str) -> str:
    value = str(project_root)
    if value.startswith("/private/var/"):
        return "/var/" + value[len("/private/var/"):]
    return value


def _command_from_env(env: Mapping[str, str]) -> list[str]:
    configured = str(env.get("ACROSS_CONTEXT_COMMAND") or "").strip()
    if configured:
        return shlex.split(configured)
    managed = _managed_across_context_command(env)
    if managed is not None:
        return [str(managed)]
    return ["across-context"]


def _managed_across_context_command(env: Mapping[str, str]) -> Path | None:
    bin_dir = ecosystem_bin_dir(env)
    candidate = bin_dir / "across-context"
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _command_warnings(command: Sequence[str], env: Mapping[str, str] | None = None) -> list[str]:
    source = env if env is not None else os.environ
    expanded = " ".join(_expand_user(str(item), source) for item in command)
    resolved = _resolved_command_path(command, source)
    if (
        _contains_protected_user_reference(expanded, source)
        or _contains_protected_user_reference(resolved or "", source)
        or _protected_path_lookup_candidate(command, source) is not None
    ):
        return [
            "Across Context command resolves to a development checkout; packaged hosts should use the managed "
            "~/.across/bin/across-context wrapper."
        ]
    return []


def _disabled_reason(warnings: Sequence[str], env: Mapping[str, str]) -> str | None:
    if warnings and _is_product_mode(env) and not _is_developer_mode(env):
        return "Across Context command resolves to a development checkout; repair the managed ~/.across/bin/across-context wrapper."
    return None


def _resolved_command_path(command: Sequence[str], env: Mapping[str, str]) -> str | None:
    if not command:
        return None
    first = _expand_user(str(command[0]), env)
    if os.path.isabs(first) or os.sep in first:
        return first
    for item in str(env.get("PATH") or "").split(os.pathsep):
        if not item:
            continue
        candidate = str(Path(_expand_user(item, env)) / first)
        if _is_product_mode(env) and not _is_developer_mode(env) and _contains_protected_user_reference(candidate, env):
            continue
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _protected_path_lookup_candidate(command: Sequence[str], env: Mapping[str, str]) -> str | None:
    if not command or not (_is_product_mode(env) and not _is_developer_mode(env)):
        return None
    first = _expand_user(str(command[0]), env)
    if os.path.isabs(first) or os.sep in first:
        return first if _contains_protected_user_reference(first, env) else None
    for item in str(env.get("PATH") or "").split(os.pathsep):
        if not item:
            continue
        candidate = str(Path(_expand_user(item, env)) / first)
        if _contains_protected_user_reference(candidate, env):
            return candidate
    return None


def _contains_protected_user_reference(value: str, env: Mapping[str, str]) -> bool:
    if not value:
        return False
    expanded = _expand_user(value, env)
    if "/Documents/projects/" in expanded:
        return True
    roots = _protected_user_reference_roots(env)
    if any(_references_path_root(expanded, root) for root in roots):
        return True
    user_home_pattern = r"/" + "Users" + r"/[^/]+"
    return bool(re.search(rf"(?:~|{user_home_pattern})/(Documents|Desktop|Downloads)(?:/|$)", expanded))


def _references_path_root(text: str, root: Path) -> bool:
    root_text = str(root)
    if not root_text:
        return False
    return bool(re.search(re.escape(root_text) + r"(?:/|$)", text))


def _protected_user_reference_roots(env: Mapping[str, str]) -> list[Path]:
    home = Path(_expand_user("~", env))
    return [home / "Documents", home / "Desktop", home / "Downloads"]


def _expand_user(value: str, env: Mapping[str, str]) -> str:
    text = str(value)
    home = str(env.get("HOME") or "").strip()
    if home and (text == "~" or text.startswith("~/")):
        suffix = text[2:] if text.startswith("~/") else ""
        return str(Path(home).expanduser() / suffix)
    return os.path.expanduser(text)


def diagnose_across_context_command(
    env: Mapping[str, str],
    *,
    recommended_command: str = "~/.across/bin/across-context",
) -> dict[str, Any]:
    command = _command_from_env(env)
    warnings = _command_warnings(command, env)
    disabled = _disabled_reason(warnings, env)
    resolved = _resolved_command_path(command, env)
    if disabled and resolved and _contains_protected_user_reference(resolved, env):
        resolved = None
    return {
        "provider": "across-context",
        "status": "needs_repair" if disabled else ("warning" if warnings else "configured"),
        "command": _diagnostic_command(command, env) if disabled else command,
        "resolvedCommand": resolved,
        "warnings": warnings,
        "disabledReason": disabled,
        "recommendedCommand": recommended_command,
    }


def _diagnostic_command(command: Sequence[str], env: Mapping[str, str]) -> list[str]:
    return [
        "<protected-user-path>" if _contains_protected_user_reference(part, env) else str(part)
        for part in command
    ]


def _is_product_mode(env: Mapping[str, str]) -> bool:
    return _truthy(env.get("ACROSS_ORCHESTRATOR_PRODUCT_MODE")) or _truthy(env.get("ACROSS_AGENTS_PRODUCT_MODE"))


def _is_developer_mode(env: Mapping[str, str]) -> bool:
    return _truthy(env.get("ACROSS_ORCHESTRATOR_DEVELOPER_MODE")) or _truthy(env.get("ACROSS_AGENTS_DEVELOPER_MODE"))


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}
