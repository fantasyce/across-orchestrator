# backend/src/across_agents_assistant/approval/executor.py
from __future__ import annotations
import logging
import time
from typing import Dict, Any, Optional

from .models import RiskLevel, ApprovalStatus, ToolExecutionResult, ApprovalRequest
from .service import ApprovalService

logger = logging.getLogger("across_agents_assistant.approval")

class ToolExecutor:
    """
    工具执行器，统一处理工具执行和审批流程。
    """

    def __init__(self, registry, approval_service: Optional[ApprovalService] = None):
        self._registry = registry
        self._approval_service = approval_service

    def check_risk_level(self, tool_name: str) -> RiskLevel:
        """检查工具风险等级"""
        tool = self._registry.get_tool(tool_name)
        if not tool:
            logger.warning(f"未知工具: {tool_name}, 默认 HIGH 风险")
            return RiskLevel.HIGH

        risk_str = tool.risk_level.lower()
        if risk_str == "low":
            return RiskLevel.LOW
        elif risk_str == "medium":
            return RiskLevel.MEDIUM
        else:
            return RiskLevel.HIGH

    def execute_tool(
        self,
        tool_name: str,
        params: Dict[str, Any],
        task_id: str,
        subtask_id: str,
        agent_id: str,
        user_description: str,
        plan_summary: str = "",
        context_sources: Optional[list] = None
    ) -> ToolExecutionResult:
        """
        执行工具。

        Returns:
            ToolExecutionResult - 如果是 LOW 风险或已批准，直接返回执行结果
            如果需要审批，返回 pending 状态的 result
        """
        start_time = time.time()
        risk_level = self.check_risk_level(tool_name)

        logger.info(f"执行工具: {tool_name} 风险等级: {risk_level.value}")

        # 获取工具定义
        tool = self._registry.get_tool(tool_name)
        if not tool:
            return ToolExecutionResult(
                success=False,
                error=f"未知工具: {tool_name}",
                tool_name=tool_name,
                elapsed_sec=time.time() - start_time
            )

        # LOW 风险工具直接执行
        if risk_level == RiskLevel.LOW:
            return self._do_execute(tool_name, tool, params, start_time)

        # MEDIUM/HIGH 风险需要审批
        if self._approval_service:
            # 检查是否在始终允许列表
            if self._approval_service.is_auto_approved(tool_name):
                return self._do_execute(tool_name, tool, params, start_time)

            # 创建审批请求
            request = self._approval_service.create_approval_request(
                task_id=task_id,
                subtask_id=subtask_id,
                agent_id=agent_id,
                tool_name=tool_name,
                tool_params=params,
                risk_level=risk_level,
                description=user_description,
                plan_summary=plan_summary,
                context_sources=context_sources
            )

            # 如果是 ALWAYS_ALLOW 状态（已在白名单），直接执行
            if request.status == ApprovalStatus.ALWAYS_ALLOW:
                return self._do_execute(tool_name, tool, params, start_time)

            # 返回待审批状态
            return ToolExecutionResult(
                success=False,
                output=None,
                tool_name=tool_name,
                elapsed_sec=time.time() - start_time,
                approved_request_id=request.request_id
            )

        # 没有审批服务，默认拒绝 MEDIUM/HIGH 风险
        logger.warning(f"无审批服务，拒绝高风险工具: {tool_name}")
        return ToolExecutionResult(
            success=False,
            error=f"工具 {tool_name} 需要审批，但审批服务未配置",
            tool_name=tool_name,
            elapsed_sec=time.time() - start_time
        )

    def _do_execute(
        self,
        tool_name: str,
        tool,
        params: Dict[str, Any],
        start_time: float
    ) -> ToolExecutionResult:
        """实际执行工具"""
        try:
            logger.info(f"执行工具: {tool_name} 参数: {params}")
            raw_output = tool.handler(**params)
            elapsed = time.time() - start_time
            metadata: Dict[str, Any] = {}
            output = raw_output
            if isinstance(raw_output, dict):
                output = raw_output.get("output")
                metadata = dict(raw_output.get("metadata", {}) or {})

            return ToolExecutionResult(
                success=True,
                output=output,
                tool_name=tool_name,
                elapsed_sec=elapsed,
                metadata=metadata,
            )
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(f"工具执行失败: {tool_name} 错误: {e}")
            return ToolExecutionResult(
                success=False,
                error=str(e),
                tool_name=tool_name,
                elapsed_sec=elapsed
            )

    def execute_approved_request(self, request: ApprovalRequest) -> ToolExecutionResult:
        """执行已批准的请求"""
        if request.status != ApprovalStatus.APPROVED:
            return ToolExecutionResult(
                success=False,
                error=f"请求未批准，状态: {request.status}",
                tool_name=request.tool_name
            )

        tool = self._registry.get_tool(request.tool_name)
        if not tool:
            return ToolExecutionResult(
                success=False,
                error=f"未知工具: {request.tool_name}",
                tool_name=request.tool_name
            )

        start_time = time.time()
        return self._do_execute(request.tool_name, tool, request.tool_params, start_time)
