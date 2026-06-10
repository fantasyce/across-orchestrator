import os
import tempfile
import unittest
from pathlib import Path


class AgentLoopRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.project = Path(self.tempdir.name) / "project"
        self.home = Path(self.tempdir.name) / "home"
        self.project.mkdir()
        self.home.mkdir()
        self._old_home = os.environ.get("ACROSS_ORCHESTRATOR_HOME")
        os.environ["ACROSS_ORCHESTRATOR_HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("ACROSS_ORCHESTRATOR_HOME", None)
        else:
            os.environ["ACROSS_ORCHESTRATOR_HOME"] = self._old_home
        self.tempdir.cleanup()

    def test_start_loop_persists_checkpoint_safe_run(self):
        from across_orchestrator.agent_loop import AgentLoopRuntime

        runtime = AgentLoopRuntime()
        loop = runtime.start_loop(
            goal="Build a platform agent handoff plan",
            project_root=str(self.project),
            agent="owner",
            memory_policy={"provider": "across-context", "read": True, "writeCandidates": True},
            max_turns=6,
        )

        self.assertTrue(loop.loop_id.startswith("loop-"))
        self.assertEqual(loop.status, "pending")
        self.assertEqual(loop.goal, "Build a platform agent handoff plan")
        self.assertEqual(loop.project_root, str(self.project.resolve()))
        self.assertEqual(loop.memory_policy["provider"], "across-context")
        self.assertEqual(loop.to_dict()["steps"], [])

        restored = runtime.get_loop(loop.loop_id)
        self.assertEqual(restored.to_dict(), loop.to_dict())
        self.assertEqual(
            [event["type"] for event in runtime.list_loop_events(loop.loop_id)],
            ["loop.started"],
        )

    def test_run_loop_records_memory_action_quality_and_final_output(self):
        from across_orchestrator.agent_loop import AgentLoopRuntime

        runtime = AgentLoopRuntime()
        loop = runtime.start_loop(
            goal="Ship a durable agent loop runtime",
            project_root=str(self.project),
            agent="owner",
            memory_policy={"provider": "across-context", "read": True, "writeCandidates": True},
            max_turns=8,
        )

        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.turn_count, 5)
        self.assertEqual(completed.final_output, "Agent loop completed for: Ship a durable agent loop runtime")
        self.assertEqual(
            [step.action.type for step in completed.steps],
            [
                "memory_search",
                "task_dispatch",
                "quality_gate",
                "memory_write_candidate",
                "final_output",
            ],
        )
        self.assertEqual(completed.steps[0].observation.payload["provider"], "across-context")
        self.assertEqual(completed.steps[2].observation.payload["quality"], "passed")
        self.assertEqual(completed.checkpoint_count, 5)

        events = runtime.list_loop_events(loop.loop_id)
        event_types = [event["type"] for event in events]
        self.assertIn("loop.step.completed", event_types)
        self.assertIn("loop.completed", event_types)

    def test_loop_turn_budget_stops_before_unbounded_execution(self):
        from across_orchestrator.agent_loop import AgentLoopRuntime

        runtime = AgentLoopRuntime()
        loop = runtime.start_loop(
            goal="Run with a strict budget",
            project_root=str(self.project),
            max_turns=2,
        )

        stopped = runtime.run_loop(loop.loop_id)

        self.assertEqual(stopped.status, "stopped")
        self.assertEqual(stopped.error, "max_turns_exceeded")
        self.assertEqual(stopped.turn_count, 2)
        self.assertIsNone(stopped.final_output)
        self.assertEqual([step.action.type for step in stopped.steps], ["memory_search", "task_dispatch"])

    def test_loop_waits_for_human_approval_before_sensitive_action(self):
        from across_orchestrator.agent_loop import AgentLoopRuntime

        runtime = AgentLoopRuntime()
        loop = runtime.start_loop(
            goal="Dispatch a high risk container task",
            project_root=str(self.project),
            approval_policy={"requireApprovalFor": ["task_dispatch"]},
            max_turns=6,
        )

        waiting = runtime.run_loop(loop.loop_id)

        self.assertEqual(waiting.status, "awaiting_approval")
        self.assertEqual(waiting.steps[-1].status, "waiting_approval")
        self.assertTrue(waiting.steps[-1].action.requires_approval)

        approved = runtime.approve_action(waiting.loop_id, waiting.steps[-1].action.action_id)
        self.assertEqual(approved.status, "running")
        completed = runtime.run_loop(loop.loop_id)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.steps[1].observation.payload["approval"], "approved")


if __name__ == "__main__":
    unittest.main()
