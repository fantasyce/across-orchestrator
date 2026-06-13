from __future__ import annotations

from typing import Any, Mapping, Sequence
import json
import os
import shlex
import subprocess


class AcrossContextMemoryProvider:
    """Subprocess-backed memory provider for the standalone Across Context CLI."""

    def __init__(self, command: Sequence[str] | None = None, env: Mapping[str, str] | None = None, timeout: int = 20):
        source = env if env is not None else os.environ
        configured = command or _command_from_env(source)
        self.command = [str(item) for item in configured]
        self.warnings = _command_warnings(self.command)
        self.env = dict(source)
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
    return ["across-context"]


def _command_warnings(command: Sequence[str]) -> list[str]:
    expanded = " ".join(os.path.expanduser(str(item)) for item in command)
    if "/Documents/projects/" in expanded:
        return [
            "ACROSS_CONTEXT_COMMAND points at a development checkout; packaged hosts should use the managed "
            "~/.across/bin/across-context wrapper."
        ]
    return []


def diagnose_across_context_command(
    env: Mapping[str, str],
    *,
    recommended_command: str = "~/.across/bin/across-context",
) -> dict[str, Any]:
    command = _command_from_env(env)
    warnings = _command_warnings(command)
    return {
        "provider": "across-context",
        "status": "warning" if warnings else "configured",
        "command": command,
        "warnings": warnings,
        "recommendedCommand": recommended_command,
    }
