from __future__ import annotations


class ActionCancelledError(RuntimeError):
    """Raised when a running adapter observes a loop cancellation request."""

    def __init__(self, reason: str = "cancelled", *, category: str | None = None):
        self.reason = reason or "cancelled"
        self.category = category
        super().__init__(self.reason)
