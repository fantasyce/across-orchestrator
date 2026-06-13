from __future__ import annotations

from typing import Any, Mapping, Sequence
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path


class AcrossContextMemoryProvider:
    """Subprocess-backed memory provider for the standalone Across Context CLI."""

    def __init__(self, command: Sequence[str] | None = None, env: Mapping[str, str] | None = None, timeout: int = 20):
        source = env if env is not None else os.environ
        configured = command or _command_from_env(source)
        self.command = [str(item) for item in configured]
        self.env = dict(source)
        self.warnings = _command_warnings(self.command, self.env)
        self.timeout = timeout

    def search(self, *, query: str, project_root: str, limit: int = 8, status: str = "active") -> dict[str, Any]:
        context_root = _context_project_root(project_root)
        completed = self._run([
            "search",
            query,
            "--project",
            context_root,
            "--limit",
            str(limit),
            "--status",
            status,
            "--json",
        ])
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
    configured_bin = str(env.get("ACROSS_BIN_HOME") or "").strip()
    if configured_bin:
        bin_dir = Path(_expand_user(configured_bin, env))
    else:
        across_home = str(env.get("ACROSS_HOME") or "").strip()
        if across_home:
            bin_dir = Path(_expand_user(across_home, env)) / "bin"
        else:
            bin_dir = Path(_expand_user("~/.across", env)) / "bin"
    candidate = bin_dir / "across-context"
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _command_warnings(command: Sequence[str], env: Mapping[str, str] | None = None) -> list[str]:
    source = env if env is not None else os.environ
    expanded = " ".join(_expand_user(str(item), source) for item in command)
    resolved = _resolved_command_path(command, source)
    if _contains_protected_user_reference(expanded, source) or _contains_protected_user_reference(resolved or "", source):
        return [
            "Across Context command resolves to a development checkout; packaged hosts should use the managed "
            "~/.across/bin/across-context wrapper."
        ]
    return []


def _resolved_command_path(command: Sequence[str], env: Mapping[str, str]) -> str | None:
    if not command:
        return None
    first = _expand_user(str(command[0]), env)
    if os.path.isabs(first) or os.sep in first:
        return first
    return shutil.which(first, path=str(env.get("PATH") or "")) or None


def _contains_protected_user_reference(value: str, env: Mapping[str, str]) -> bool:
    if not value:
        return False
    expanded = _expand_user(value, env)
    if "/Documents/projects/" in expanded:
        return True
    roots = _protected_user_reference_roots(env)
    if any(str(root) in expanded for root in roots):
        return True
    user_home_pattern = r"/" + "Users" + r"/[^/]+"
    return bool(re.search(rf"(?:~|{user_home_pattern})/(Documents|Desktop|Downloads)(?:/|$)", expanded))


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
    return {
        "provider": "across-context",
        "status": "warning" if warnings else "configured",
        "command": command,
        "resolvedCommand": _resolved_command_path(command, env),
        "warnings": warnings,
        "recommendedCommand": recommended_command,
    }
