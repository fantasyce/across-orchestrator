from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class LLMResponse:
    text: str
    metadata: Dict[str, Any] | None = None


class LLMGateway:
    """Minimal gateway shape for standalone parity tests."""

    async def chat(
        self,
        message: str,
        system_prompt: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        raise RuntimeError("No host LLM gateway is configured.")

