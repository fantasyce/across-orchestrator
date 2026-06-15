from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

COMPONENT_ID = "across-orchestrator"
_TRUTHY_VALUES = {"1", "true", "yes", "on", "y"}


def _env_value(env: Mapping[str, str] | None, key: str) -> str | None:
    source = env if env is not None else os.environ
    value = source.get(key)
    if value and value.strip():
        return value
    return None


def _user_home(env: Mapping[str, str] | None = None) -> Path:
    home = _env_value(env, "HOME")
    return Path(home).expanduser() if home else Path.home()


def expand_user(value: str, env: Mapping[str, str] | None = None) -> str:
    text = str(value or "")
    if text == "~":
        return str(_user_home(env))
    if text.startswith("~/"):
        return str(_user_home(env) / text[2:])
    return os.path.expanduser(text)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUTHY_VALUES


def _is_product_mode(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return _truthy(source.get("ACROSS_ORCHESTRATOR_PRODUCT_MODE")) or _truthy(source.get("ACROSS_AGENTS_PRODUCT_MODE"))


def is_product_mode(env: Mapping[str, str] | None = None) -> bool:
    return _is_product_mode(env)


def _is_developer_mode(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return _truthy(source.get("ACROSS_ORCHESTRATOR_DEVELOPER_MODE")) or _truthy(source.get("ACROSS_AGENTS_DEVELOPER_MODE"))


def is_developer_mode(env: Mapping[str, str] | None = None) -> bool:
    return _is_developer_mode(env)


def _protected_user_roots(env: Mapping[str, str] | None = None) -> list[Path]:
    home = _user_home(env)
    return [home / "Documents", home / "Desktop", home / "Downloads"]


def _contains_protected_user_reference(value: str, env: Mapping[str, str] | None = None) -> bool:
    text = expand_user(str(value or ""), env)
    if not text:
        return False
    if any(_references_path_root(text, root) for root in _protected_user_roots(env)):
        return True
    user_home_pattern = r"/" + "Users" + r"/[^/]+"
    return bool(re.search(rf"(?:~|{user_home_pattern})/(Documents|Desktop|Downloads)(?:/|$)", text))


def _references_path_root(text: str, root: Path) -> bool:
    root_text = str(root)
    if not root_text:
        return False
    return bool(re.search(re.escape(root_text) + r"(?:/|$)", text))


def contains_protected_user_reference(value: str, env: Mapping[str, str] | None = None) -> bool:
    return _contains_protected_user_reference(value, env)


def safe_runtime_override(name: str, env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    value = _env_value(source, name)
    if not value:
        return None
    if not _is_product_mode(source) or _is_developer_mode(source):
        return value
    return None if _contains_protected_user_reference(value, source) else value


def ecosystem_home(env: Mapping[str, str] | None = None) -> Path:
    override = safe_runtime_override("ACROSS_HOME", env)
    if override:
        return Path(expand_user(override, env)).resolve()
    return (_user_home(env) / ".across").resolve()


def ecosystem_bin_dir(env: Mapping[str, str] | None = None) -> Path:
    override = safe_runtime_override("ACROSS_BIN_HOME", env)
    if override:
        return Path(expand_user(override, env)).resolve()
    return ecosystem_home(env) / "bin"


def plugin_root(env: Mapping[str, str] | None = None) -> Path:
    override = safe_runtime_override("ACROSS_PLUGIN_HOME", env)
    if override:
        return Path(expand_user(override, env)).resolve()
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
