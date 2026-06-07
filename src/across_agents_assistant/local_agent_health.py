from __future__ import annotations

from typing import Dict, Optional

from .agent_ids import LOCAL_CLI_AGENT_IDS


def detect_local_agents() -> Dict[str, dict]:
    return {
        agent_id: {"available": False, "status": "host_adapter_required"}
        for agent_id in LOCAL_CLI_AGENT_IDS
    }


def is_local_agent_available(agent_id: str) -> bool:
    return bool(detect_local_agents().get(agent_id, {}).get("available"))


def get_configured_agent_model(agent_id: str) -> Optional[str]:
    return None


def resolve_local_agent_executable(agent_id: str) -> Optional[str]:
    return None

