from __future__ import annotations

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

