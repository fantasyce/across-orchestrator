from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from ..agent_ids import LOCAL_CLI_AGENT_IDS, normalize_agent_id
from ..llm_gateway.provider_registry import get_default_provider_ids
from .protocol import AgentResponse, InvokeRequest
from .result import SubtaskResult, TaskResult


DEFAULT_AGENTS = [*LOCAL_CLI_AGENT_IDS, *get_default_provider_ids()]


class AgentBridge:
    """Narrow standalone bridge used by the transplanted task dispatcher.

    The desktop app wires this boundary to real local agents, cloud LLMs, and
    tool approvals. The standalone orchestrator keeps the same method shape but
    does not assume those host services exist.
    """

    def __init__(self, local_agent_client: Any, llm_gateway: Any = None, tool_executor: Any = None):
        self._client = local_agent_client
        self._llm_gateway = llm_gateway
        self._tool_executor = tool_executor
        self._task_results: Dict[str, TaskResult] = {}

    def get_agent_ids(self) -> List[str]:
        return list(DEFAULT_AGENTS)

    def is_agent_available(self, agent_id: str) -> bool:
        normalized = normalize_agent_id(agent_id) or agent_id
        return normalized in DEFAULT_AGENTS

    def invoke(
        self,
        agent_id: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        timeout: float = 120.0,
        project_dir: Optional[str] = None,
    ) -> AgentResponse:
        start = time.time()
        normalized = normalize_agent_id(agent_id) or agent_id
        if hasattr(self._client, "invoke"):
            try:
                raw = self._client.invoke(
                    normalized,
                    message,
                    context=context or {},
                    timeout=timeout,
                    project_dir=project_dir,
                )
                output = getattr(raw, "text", None) or getattr(raw, "output", None) or str(raw)
                return AgentResponse(
                    message_id=f"msg-{uuid.uuid4().hex[:8]}",
                    request_id=f"req-{uuid.uuid4().hex[:8]}",
                    success=True,
                    output=output,
                    agent_id=normalized,
                    elapsed_sec=time.time() - start,
                )
            except Exception as exc:
                return AgentResponse(
                    message_id=f"msg-{uuid.uuid4().hex[:8]}",
                    request_id=f"req-{uuid.uuid4().hex[:8]}",
                    success=False,
                    error=str(exc),
                    agent_id=normalized,
                    elapsed_sec=time.time() - start,
                )

        return AgentResponse(
            message_id=f"msg-{uuid.uuid4().hex[:8]}",
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            success=False,
            error="No host agent adapter is configured for standalone execution.",
            agent_id=normalized,
            elapsed_sec=time.time() - start,
        )

    def batch_invoke(self, requests: List[InvokeRequest]) -> List[AgentResponse]:
        return [
            self.invoke(req.agent_id, req.message, req.context, req.timeout)
            for req in requests
        ]

    def create_task_result(self, task_id: str, total_subtasks: int = 0) -> TaskResult:
        result = TaskResult(task_id=task_id, total_subtasks=total_subtasks)
        self._task_results[task_id] = result
        return result

    def get_task_result(self, task_id: str) -> Optional[TaskResult]:
        return self._task_results.get(task_id)

    def add_subtask_result(self, task_result: TaskResult, subtask_result: SubtaskResult) -> None:
        task_result.add_subtask_result(subtask_result)

    def shutdown(self) -> None:
        self._task_results.clear()

