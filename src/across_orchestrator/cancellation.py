from __future__ import annotations


class ActionCancelledError(RuntimeError):
    """Raised when a running adapter observes a loop cancellation request."""

    def __init__(self, reason: str = "cancelled"):
        self.reason = reason or "cancelled"
        super().__init__(self.reason)
