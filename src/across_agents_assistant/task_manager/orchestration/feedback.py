from __future__ import annotations

import threading
import uuid
from typing import Dict, List, Optional

from across_agents_assistant.task_manager.models import Feedback


class FeedbackChannel:
    """In-memory feedback routing and resolution."""

    def __init__(self) -> None:
        self._feedbacks: Dict[str, Feedback] = {}
        self._pending_by_agent: Dict[str, List[str]] = {}
        self._lock = threading.RLock()

    def submit(self, feedback: Feedback) -> str:
        with self._lock:
            if not feedback.feedback_id:
                feedback.feedback_id = f"fb-{uuid.uuid4().hex[:8]}"

            self._feedbacks[feedback.feedback_id] = feedback
            self._pending_by_agent.setdefault(feedback.to_agent, []).append(
                feedback.feedback_id
            )
            return feedback.feedback_id

    def get_pending_for_agent(self, agent_id: str) -> List[Feedback]:
        with self._lock:
            fb_ids = self._pending_by_agent.get(agent_id, [])
            return [
                self._feedbacks[fid]
                for fid in fb_ids
                if fid in self._feedbacks
            ]

    def route_to_owner(self, feedback: Feedback) -> bool:
        with self._lock:
            if feedback.feedback_id not in self._feedbacks:
                return False
            return True

    def get_all_pending(self) -> List[Feedback]:
        with self._lock:
            result: List[Feedback] = []
            for fb_ids in self._pending_by_agent.values():
                for fid in fb_ids:
                    if fid in self._feedbacks:
                        result.append(self._feedbacks[fid])
            return result

    def resolve(self, feedback_id: str) -> bool:
        with self._lock:
            feedback = self._feedbacks.get(feedback_id)
            if feedback is None:
                return False

            del self._feedbacks[feedback_id]

            agent_pending = self._pending_by_agent.get(feedback.to_agent, [])
            if feedback_id in agent_pending:
                agent_pending.remove(feedback_id)
                if not agent_pending:
                    del self._pending_by_agent[feedback.to_agent]

            return True
