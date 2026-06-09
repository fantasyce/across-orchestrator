from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Protocol


class DispatcherAdapter(Protocol):
    def add_progress_callback(self, callback: Callable[[Any], None]) -> None:
        ...

    def dispatch_subtask(self, subtask: Any) -> Any:
        ...


class ValidatorAdapter(Protocol):
    def validate(self, job: Any) -> Any:
        ...


class OwnerAgentAdapter(Protocol):
    def decompose_and_assign(self, task: Any, context: dict | None = None) -> Any:
        ...

    def assign_waves(self, task: Any) -> Any:
        ...

    def refresh_decomposition_coverage(self, task: Any) -> Any:
        ...


@dataclass(frozen=True)
class HostAgentDescriptor:
    """Serializable agent-container descriptor supplied by a hosting platform."""

    agent_id: str
    display_name: str
    endpoint: str | None = None
    protocols: tuple[str, ...] = ("sdk",)
    capabilities: tuple[str, ...] = ()
    tenant_id: str | None = None
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["protocols"] = list(self.protocols)
        payload["capabilities"] = list(self.capabilities)
        payload["metadata"] = dict(self.metadata or {})
        return payload


@dataclass(frozen=True)
class HostingPlatformContract:
    """The minimum SDK contract a platform passes into Across Orchestrator."""

    platform_id: str
    agents: tuple[HostAgentDescriptor, ...]
    memory_provider: str | None = None
    approval_mode: str = "host-mediated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform_id": self.platform_id,
            "agents": [agent.to_dict() for agent in self.agents],
            "memory_provider": self.memory_provider,
            "approval_mode": self.approval_mode,
        }


def build_hosting_platform_contract(
    platform_id: str,
    agents: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    memory_provider: str | None = None,
    approval_mode: str = "host-mediated",
) -> HostingPlatformContract:
    descriptors = []
    for item in agents:
        descriptors.append(
            HostAgentDescriptor(
                agent_id=str(item.get("agent_id") or item.get("id") or ""),
                display_name=str(item.get("display_name") or item.get("name") or item.get("agent_id") or item.get("id") or ""),
                endpoint=item.get("endpoint"),
                protocols=tuple(item.get("protocols") or ("sdk",)),
                capabilities=tuple(item.get("capabilities") or ()),
                tenant_id=item.get("tenant_id"),
                metadata=dict(item.get("metadata") or {}),
            )
        )
    return HostingPlatformContract(
        platform_id=platform_id,
        agents=tuple(descriptors),
        memory_provider=memory_provider,
        approval_mode=approval_mode,
    )
