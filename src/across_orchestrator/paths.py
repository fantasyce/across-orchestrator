from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

COMPONENT_ID = "across-orchestrator"


def _env_value(env: Mapping[str, str] | None, key: str) -> str | None:
    source = env if env is not None else os.environ
    value = source.get(key)
    if value and value.strip():
        return value
    return None


def _user_home(env: Mapping[str, str] | None = None) -> Path:
    home = _env_value(env, "HOME")
    return Path(home).expanduser() if home else Path.home()


def ecosystem_home(env: Mapping[str, str] | None = None) -> Path:
    override = _env_value(env, "ACROSS_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (_user_home(env) / ".across").resolve()


def ecosystem_bin_dir(env: Mapping[str, str] | None = None) -> Path:
    override = _env_value(env, "ACROSS_BIN_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return ecosystem_home(env) / "bin"


def plugin_root(env: Mapping[str, str] | None = None) -> Path:
    override = _env_value(env, "ACROSS_PLUGIN_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return ecosystem_home(env) / "plugins"


def component_home(section: str, component_id: str = COMPONENT_ID, env: Mapping[str, str] | None = None) -> Path:
    return ecosystem_home(env) / section / component_id


def component_data_home(component_id: str = COMPONENT_ID, env: Mapping[str, str] | None = None) -> Path:
    return component_home("data", component_id, env)


def config_home(component_id: str = COMPONENT_ID, env: Mapping[str, str] | None = None) -> Path:
    return component_home("config", component_id, env)


def run_home(component_id: str = COMPONENT_ID, env: Mapping[str, str] | None = None) -> Path:
    return component_home("run", component_id, env)


def logs_home(component_id: str = COMPONENT_ID, env: Mapping[str, str] | None = None) -> Path:
    return component_home("logs", component_id, env)


def cache_home(component_id: str = COMPONENT_ID, env: Mapping[str, str] | None = None) -> Path:
    return component_home("cache", component_id, env)


def legacy_default_home(env: Mapping[str, str] | None = None) -> Path:
    return (_user_home(env) / ".across-orchestrator").resolve()
