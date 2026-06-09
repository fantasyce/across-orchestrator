from __future__ import annotations

import os
from pathlib import Path


def _ecosystem_home() -> Path:
    override = os.environ.get("ACROSS_HOME")
    if override and override.strip():
        return Path(override).expanduser().resolve()
    return (Path.home() / ".across").resolve()


def app_home() -> Path:
    """Return the compatibility data root for transplanted app-grade modules."""
    override = os.environ.get("ACROSS_AGENTS_HOME")
    if override and override.strip():
        return Path(override).expanduser().resolve()
    return _ecosystem_home() / "data" / "across-orchestrator" / "compat" / "across-agents-assistant"


def ensure_app_home() -> Path:
    root = app_home()
    root.mkdir(parents=True, exist_ok=True)
    return root


def app_subdir(name: str) -> Path:
    path = ensure_app_home() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_file(name: str) -> Path:
    return ensure_app_home() / name


def log_dir() -> Path:
    return app_subdir("logs")


def run_dir() -> Path:
    return app_subdir("run")


def tmp_dir() -> Path:
    return app_subdir("tmp")


def backend_socket_path() -> str:
    return str(run_dir() / "across-agents.sock")


def speech_socket_path() -> str:
    return str(run_dir() / "speech_cli.sock")
