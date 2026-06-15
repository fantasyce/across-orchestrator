import os
import tempfile
import threading
import time
import unittest
from pathlib import Path


class RecordingMemoryProvider:
    def __init__(self):
        self.searches = []
        self.candidates = []

    def search(self, *, query, project_root, limit=8, status="active"):
        self.searches.append({
            "query": query,
            "project_root": project_root,
            "limit": limit,
            "status": status,
        })
        return {
            "provider": "recording-memory",
            "result_count": 1,
            "results": [
                {
                    "id": "mem-active-1",
                    "text": "Reuse the existing serial delivery contract.",
                    "status": "active",
                }
            ],
        }

    def remember_candidate(self, *, text, project_root, tags=None):
        entry = {
            "id": f"mem-pending-{len(self.candidates) + 1}",
            "text": text,
            "project_root": project_root,
            "tags": list(tags or []),
            "status": "pending",
        }
        self.candidates.append(entry)
        return {"provider": "recording-memory", "memory": entry}


class RemediationDispatcher:
    def __init__(self):
        self.actions = []

    def dispatch(self, *, loop, action_type, context):
        self.actions.append(action_type)
        if action_type == "remediation_dispatch":
            return {
                "dispatch": "completed",
                "adapter": "recording-dispatcher",
                "remediated": True,
                "message": "Applied remediation from failed quality gate.",
            }
        return {
            "dispatch": "completed",
            "adapter": "recording-dispatcher",
            "artifacts": ["README.md"],
            "message": "Initial dispatch produced artifacts.",
        }


class FailingThenPassingQualityGate:
    def __init__(self):
        self.calls = 0

    def evaluate(self, *, loop, context):
        self.calls += 1
        if self.calls == 1:
            return {
                "quality": "failed",
                "passed": False,
                "failed_gates": ["browser_e2e"],
                "summary": "Browser E2E failed before remediation.",
            }
        return {
            "quality": "passed",
            "passed": True,
            "gate_count": 4,
            "summary": "All gates passed after remediation.",
        }


class AlwaysFailingQualityGate:
    def evaluate(self, *, loop, context):
        return {
            "quality": "failed",
            "passed": False,
            "failed_gates": ["browser_e2e"],
            "summary": "Browser E2E still fails.",
        }


class ExplodingDispatcher:
    def dispatch(self, *, loop, action_type, context):
        raise RuntimeError(f"{action_type} adapter unavailable")


class SlowDispatcher:
    def __init__(self):
        self.actions = []
        self._lock = threading.Lock()

    def dispatch(self, *, loop, action_type, context):
        with self._lock:
            self.actions.append(action_type)
        time.sleep(0.05)
        return {
            "dispatch": "completed",
            "adapter": "slow-dispatcher",
            "message": "Slow dispatch completed.",
        }


class AgentLoopV2Tests(unittest.TestCase):
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

    def test_agent_loop_uses_adapters_and_branches_to_remediation(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        memory = RecordingMemoryProvider()
        dispatcher = RemediationDispatcher()
        quality = FailingThenPassingQualityGate()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=memory,
                dispatcher=dispatcher,
                quality_gate=quality,
            )
        )
        loop = runtime.start_loop(
            goal="Ship a serial task with remediation",
            project_root=str(self.project),
            max_turns=8,
        )

        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            [step.action.type for step in completed.steps],
            [
                "memory_search",
                "task_dispatch",
                "quality_gate",
                "remediation_dispatch",
                "quality_gate",
                "memory_write_candidate",
                "final_output",
            ],
        )
        self.assertEqual(dispatcher.actions, ["task_dispatch", "remediation_dispatch"])
        self.assertEqual(quality.calls, 2)
        self.assertEqual(memory.searches[0]["query"], "Ship a serial task with remediation")
        self.assertEqual(memory.candidates[0]["status"], "pending")
        self.assertIn("All gates passed after remediation", completed.final_output)
        self.assertTrue(all(step.checkpoint.get("adapter") for step in completed.steps))
        self.assertIn(
            "loop.next_action.selected",
            [event["type"] for event in runtime.list_loop_events(loop.loop_id)],
        )

    def test_approval_resume_executes_the_approved_adapter_action(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        dispatcher = RemediationDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Dispatch only after approval",
            project_root=str(self.project),
            max_turns=8,
            approval_policy={"requireApprovalFor": ["task_dispatch"]},
        )

        waiting = runtime.run_loop(loop.loop_id)
        self.assertEqual(waiting.status, "awaiting_approval")
        self.assertEqual(waiting.steps[-1].action.type, "task_dispatch")
        self.assertEqual(dispatcher.actions, [])

        approved = runtime.approve_action(waiting.loop_id, waiting.steps[-1].action.action_id)
        self.assertEqual(approved.status, "running")
        self.assertEqual(dispatcher.actions, ["task_dispatch"])

        completed = runtime.run_loop(loop.loop_id)
        self.assertEqual(completed.status, "completed")
        self.assertIn("approval", completed.steps[1].observation.payload)

    def test_quality_failure_after_remediation_budget_remains_failed(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=RemediationDispatcher(),
                quality_gate=AlwaysFailingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Fail when quality never recovers",
            project_root=str(self.project),
            max_turns=8,
            metadata={"maxRemediationTurns": 0},
        )

        failed = runtime.run_loop(loop.loop_id)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error, "quality_gate_failed")
        self.assertIsNone(failed.final_output)
        event_types = [event["type"] for event in runtime.list_loop_events(loop.loop_id)]
        self.assertIn("loop.failed", event_types)
        self.assertNotIn("loop.completed", event_types)

    def test_adapter_exception_fails_loop_instead_of_leaving_it_running(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=ExplodingDispatcher(),
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Record adapter failures",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
        )

        failed = runtime.run_loop(loop.loop_id)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error, "task_dispatch_failed")
        event = runtime.list_loop_events(loop.loop_id)[-1]
        self.assertEqual(event["type"], "loop.failed")
        self.assertEqual(event["payload"]["action_type"], "task_dispatch")

    def test_concurrent_run_loop_executes_dispatch_once(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        dispatcher = SlowDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Run only one dispatcher for concurrent callers",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
        )
        start = threading.Barrier(3)
        results = []

        def run_loop():
            start.wait(timeout=2)
            results.append(runtime.run_loop(loop.loop_id).status)

        threads = [threading.Thread(target=run_loop) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=3)

        final_loop = runtime.get_loop(loop.loop_id)

        self.assertEqual(results, ["completed", "completed"])
        self.assertEqual(dispatcher.actions, ["task_dispatch", "remediation_dispatch"])
        self.assertEqual(dispatcher.actions.count("task_dispatch"), 1)
        self.assertEqual(
            [step.action.type for step in final_loop.steps],
            ["task_dispatch", "quality_gate", "remediation_dispatch", "quality_gate", "final_output"],
        )


if __name__ == "__main__":
    unittest.main()
