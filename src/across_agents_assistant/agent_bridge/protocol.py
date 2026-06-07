from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, Any, Optional
import json
import uuid
import time

class MessageType(str, Enum):
    INVOKE = "invoke"
    RESPONSE = "response"
    HEARTBEAT = "heartbeat"
    CANCEL = "cancel"
    ERROR = "error"

@dataclass
class AgentMessage:
    """Structured message format for Agent Bridge protocol."""
    message_id: str
    message_type: MessageType
    agent_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        """Serialize to JSON string."""
        data = asdict(self)
        data["message_type"] = self.message_type.value
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, json_str: str) -> AgentMessage:
        """Deserialize from JSON string."""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            raise ValueError(f"Invalid JSON: {json_str[:100]}")

        # Validate required fields
        required = ["message_id", "message_type", "agent_id"]
        for field in required:
            if field not in data:
                raise ValueError(f"Missing required field: {field}")

        try:
            data["message_type"] = MessageType(data["message_type"])
        except ValueError:
            raise ValueError(f"Invalid message_type: {data['message_type']}")

        return cls(**data)

    @staticmethod
    def new_invoke(agent_id: str, content: str, context: Optional[Dict[str, Any]] = None, metadata: Optional[Dict[str, Any]] = None) -> AgentMessage:
        """Create a new INVOKE message."""
        return AgentMessage(
            message_id=f"msg-{uuid.uuid4().hex[:8]}",
            message_type=MessageType.INVOKE,
            agent_id=agent_id,
            payload={"content": content, "context": context or {}},
            metadata=metadata or {}
        )

@dataclass
class InvokeRequest:
    """Request to invoke an agent."""
    request_id: str
    agent_id: str
    message: str
    context: Dict[str, Any] = field(default_factory=dict)
    timeout: float = 120.0

    @staticmethod
    def new(agent_id: str, message: str, context: Optional[Dict[str, Any]] = None) -> InvokeRequest:
        return InvokeRequest(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            agent_id=agent_id,
            message=message,
            context=context or {}
        )

@dataclass
class AgentResponse:
    """Response from an agent invocation."""
    message_id: str
    request_id: str
    success: bool
    agent_id: str
    output: Optional[str] = None
    error: Optional[str] = None
    elapsed_sec: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return self.success

    @property
    def is_error(self) -> bool:
        return not self.success