from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Any, List, Optional
import time
import uuid

class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ALWAYS_ALLOW = "always_allow"
    EXPIRED = "expired"

@dataclass
class ApprovalRequest:
    """审批请求"""
    request_id: str
    task_id: str
    subtask_id: str
    agent_id: str
    tool_name: str
    tool_params: Dict[str, Any]
    risk_level: RiskLevel
    description: str
    plan_summary: str = ""
    context_sources: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    status: ApprovalStatus = ApprovalStatus.PENDING

    def is_pending(self) -> bool:
        return self.status == ApprovalStatus.PENDING

    def is_expired(self, timeout_sec: float = 300) -> bool:
        """检查是否过期（默认5分钟）"""
        if self.status != ApprovalStatus.PENDING:
            return False
        return time.time() - self.created_at > timeout_sec

@dataclass
class ToolExecutionResult:
    """工具执行结果"""
    success: bool
    output: Optional[str] = None
    error: Optional[str] = None
    tool_name: str = ""
    elapsed_sec: Optional[float] = None
    approved_request_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
