from __future__ import annotations

import re
from typing import Any


SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|apikey|credential|client[_-]?secret|private[_-]?key)",
    re.IGNORECASE,
)
SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|xox[baprs]-[A-Za-z0-9-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
SAFE_SENSITIVE_FIELD_VALUES = {False, None, "", 0, "false", "none", "not_allowed", "disabled", "not_included"}


def is_safe_sensitive_field_value(value: Any) -> bool:
    try:
        return value in SAFE_SENSITIVE_FIELD_VALUES
    except TypeError:
        return False


def redact_sensitive_value(value: Any) -> Any:
    """Return a JSON-safe copy with credentials and raw secret-looking values removed."""

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SENSITIVE_KEY_RE.search(key_text) and not is_safe_sensitive_field_value(item):
                redacted[key_text] = "[redacted]"
            else:
                redacted[key_text] = redact_sensitive_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive_value(item) for item in value]
    if isinstance(value, str):
        return SENSITIVE_VALUE_RE.sub("[redacted]", value)
    return value
