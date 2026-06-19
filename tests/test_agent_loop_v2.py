import json
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


class AgentRoutingDispatcher:
    def __init__(self):
        self.calls = []

    def dispatch(self, *, loop, action_type, context):
        self.calls.append({
            "action_type": action_type,
            "agent": loop.agent,
            "routing": context.get("routing"),
        })
        return {
            "dispatch": "completed",
            "adapter": "agent-routing-dispatcher",
            "agent": loop.agent,
            "message": f"{action_type} routed to {loop.agent}.",
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


class FailingOnceDispatcher:
    def __init__(self):
        self.actions = []

    def dispatch(self, *, loop, action_type, context):
        self.actions.append(action_type)
        if len(self.actions) == 1:
            raise RuntimeError(f"{action_type} adapter unavailable")
        return {
            "dispatch": "completed",
            "adapter": "failing-once-dispatcher",
            "message": "Dispatch succeeded after recovery retry.",
        }


class EnvironmentBlockedError(RuntimeError):
    blocked_by_environment = True


class TimeoutDispatcher:
    def dispatch(self, *, loop, action_type, context):
        raise TimeoutError(f"{action_type} timed out")


class EnvironmentBlockedDispatcher:
    def dispatch(self, *, loop, action_type, context):
        raise EnvironmentBlockedError(f"{action_type} missing required toolchain")


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


class InspectingDispatcher:
    def __init__(self):
        self.runtime = None
        self.persisted_during_dispatch = None

    def dispatch(self, *, loop, action_type, context):
        if self.persisted_during_dispatch is None:
            self.persisted_during_dispatch = self.runtime.get_loop(loop.loop_id).to_dict()
        return {
            "dispatch": "completed",
            "adapter": "inspecting-dispatcher",
            "message": "Inspected persisted loop state during dispatch.",
        }


class HeartbeatRenewingDispatcher:
    def __init__(self):
        self.runtime = None
        self.before = None
        self.after = None
        self.renewal = None

    def dispatch(self, *, loop, action_type, context):
        self.before = self.runtime.get_loop(loop.loop_id).steps[-1].checkpoint["execution"]
        time.sleep(0.01)
        self.renewal = context["heartbeat"]()
        self.after = self.runtime.get_loop(loop.loop_id).steps[-1].checkpoint["execution"]
        return {
            "dispatch": "completed",
            "adapter": "heartbeat-renewing-dispatcher",
            "message": "Renewed dispatch heartbeat.",
        }


class CancellableDispatcher:
    def __init__(self):
        self.started = threading.Event()
        self.cancel_seen = threading.Event()
        self.actions = []

    def dispatch(self, *, loop, action_type, context):
        self.actions.append(action_type)
        self.started.set()
        deadline = time.time() + 2
        while time.time() < deadline:
            if context["cancellation"].is_cancelled():
                self.cancel_seen.set()
                context["cancellation"].raise_if_cancelled()
            context["heartbeat"]()
            time.sleep(0.01)
        return {
            "dispatch": "completed",
            "adapter": "cancellable-dispatcher",
            "message": "Cancellation was not observed.",
        }


class NonCooperativeHangingDispatcher:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.actions = []

    def dispatch(self, *, loop, action_type, context):
        self.actions.append(action_type)
        self.started.set()
        self.release.wait(timeout=5)
        return {
            "dispatch": "completed",
            "adapter": "non-cooperative-hanging-dispatcher",
            "message": "Returned after test cleanup.",
        }


class LateCheckingDetachedDispatcher:
    def __init__(self):
        self.started = threading.Event()
        self.allow_late_check = threading.Event()
        self.finished = threading.Event()
        self.worker_saw_cancel = False
        self.actions = []

    def dispatch(self, *, loop, action_type, context):
        self.actions.append(action_type)
        self.started.set()
        self.allow_late_check.wait(timeout=2)
        self.worker_saw_cancel = context["cancellation"].is_cancelled()
        if self.worker_saw_cancel:
            self.finished.set()
            context["cancellation"].raise_if_cancelled()
        late_write = Path(loop.project_root) / "late-write.txt"
        late_write.write_text("dispatch continued after cancellation", encoding="utf-8")
        self.finished.set()
        return {
            "dispatch": "completed",
            "adapter": "late-checking-detached-dispatcher",
            "message": "Late cancellation was not observed.",
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
        candidate = json.loads(memory.candidates[0]["text"])
        self.assertEqual(candidate["schema_version"], "agent-loop-memory-candidate/1.0")
        self.assertEqual(candidate["outcome"], "passed")
        self.assertEqual(candidate["goal"], "Ship a serial task with remediation")
        self.assertIn("memory_search", [item["action_type"] for item in candidate["decisions"]])
        self.assertEqual(candidate["memory_refs"][0]["memory_ids"], ["mem-active-1"])
        self.assertEqual(candidate["remediation_outcomes"][0]["status"], "completed")
        self.assertNotIn("traceback", json.dumps(candidate).lower())
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

    def test_approved_action_persists_running_step_lease_before_adapter_returns(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        dispatcher = InspectingDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        dispatcher.runtime = runtime
        loop = runtime.start_loop(
            goal="Persist approved action leases",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
            approval_policy={"requireApprovalFor": ["task_dispatch"]},
        )
        waiting = runtime.run_loop(loop.loop_id)

        approved = runtime.approve_action(waiting.loop_id, waiting.steps[-1].action.action_id)

        persisted = dispatcher.persisted_during_dispatch
        self.assertEqual(persisted["status"], "running")
        self.assertEqual(len(persisted["steps"]), 1)
        running_step = persisted["steps"][0]
        self.assertEqual(running_step["action"]["approval_status"], "approved")
        self.assertEqual(running_step["status"], "running")
        self.assertEqual(running_step["observation"]["status"], "running")
        execution = running_step["checkpoint"]["execution"]
        self.assertTrue(execution["lease_id"].startswith("lease-"))
        self.assertGreater(execution["lease_expires_at"], execution["heartbeat_at"])

        self.assertEqual(approved.steps[0].status, "completed")
        self.assertEqual(approved.steps[0].observation.payload["approval"], "approved")
        self.assertEqual(approved.steps[0].checkpoint["execution"]["lease_id"], execution["lease_id"])
        event_types = [event["type"] for event in runtime.list_loop_events(loop.loop_id)]
        self.assertIn("loop.step.started", event_types)
        self.assertIn("loop.step.heartbeat", event_types)

    def test_approved_action_exception_records_failed_execution_lease(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=ExplodingDispatcher(),
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Fail an approved action with lease evidence",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
            approval_policy={"requireApprovalFor": ["task_dispatch"]},
        )
        waiting = runtime.run_loop(loop.loop_id)

        failed = runtime.approve_action(waiting.loop_id, waiting.steps[-1].action.action_id)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error, "task_dispatch_failed")
        self.assertEqual(failed.steps[0].status, "failed")
        self.assertEqual(failed.steps[0].action.approval_status, "approved")
        self.assertEqual(failed.steps[0].observation.payload["approval"], "approved")
        self.assertEqual(failed.steps[0].observation.payload["error"], "task_dispatch adapter unavailable")
        self.assertEqual(failed.steps[0].observation.payload["failure_type"], "adapter_error")
        self.assertEqual(failed.steps[0].checkpoint["failure_type"], "adapter_error")
        execution = failed.steps[0].checkpoint["execution"]
        self.assertTrue(execution["lease_id"].startswith("lease-"))
        self.assertGreaterEqual(execution["duration_ms"], 0)
        events = runtime.list_loop_events(loop.loop_id)
        event_types = [event["type"] for event in events]
        self.assertIn("loop.step.heartbeat", event_types)
        self.assertIn("loop.action.failed", event_types)
        self.assertEqual(events[-1]["payload"]["failure_type"], "adapter_error")

    def test_run_loop_does_not_advance_past_pending_approval(self):
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
            goal="Do not bypass approval",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
            approval_policy={"requireApprovalFor": ["task_dispatch"]},
        )

        waiting = runtime.run_loop(loop.loop_id)
        rerun = runtime.run_loop(loop.loop_id)

        self.assertEqual(rerun.status, "awaiting_approval")
        self.assertEqual(len(rerun.steps), len(waiting.steps))
        self.assertEqual(rerun.steps[-1].status, "waiting_approval")
        self.assertEqual(dispatcher.actions, [])

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
        events = runtime.list_loop_events(loop.loop_id)
        event_types = [event["type"] for event in events]
        self.assertIn("loop.failed", event_types)
        self.assertNotIn("loop.completed", event_types)
        self.assertEqual(events[-1]["payload"]["failure_type"], "quality_failed")

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
        self.assertEqual(len(failed.steps), 1)
        self.assertEqual(failed.steps[0].action.type, "task_dispatch")
        self.assertEqual(failed.steps[0].status, "failed")
        self.assertEqual(failed.steps[0].observation.status, "failed")
        self.assertEqual(failed.steps[0].observation.payload["error"], "task_dispatch adapter unavailable")
        self.assertEqual(failed.steps[0].observation.payload["failure_type"], "adapter_error")
        self.assertEqual(failed.steps[0].checkpoint["status"], "failed")
        self.assertEqual(failed.steps[0].checkpoint["failure_type"], "adapter_error")
        event = runtime.list_loop_events(loop.loop_id)[-1]
        self.assertEqual(event["type"], "loop.failed")
        self.assertEqual(event["payload"]["action_type"], "task_dispatch")
        self.assertEqual(event["payload"]["failure_type"], "adapter_error")

    def test_dispatch_failure_type_propagates_to_loop_failed_event(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        cases = [
            (TimeoutDispatcher(), "timeout"),
            (EnvironmentBlockedDispatcher(), "environment_blocked"),
        ]
        for dispatcher, expected_failure_type in cases:
            with self.subTest(failure_type=expected_failure_type):
                runtime = AgentLoopRuntime(
                    adapters=AgentLoopAdapters(
                        memory_provider=RecordingMemoryProvider(),
                        dispatcher=dispatcher,
                        quality_gate=FailingThenPassingQualityGate(),
                    )
                )
                loop = runtime.start_loop(
                    goal=f"Preserve {expected_failure_type} terminal classification",
                    project_root=str(self.project),
                    max_turns=8,
                    memory_policy={"read": False, "writeCandidates": False},
                )

                failed = runtime.run_loop(loop.loop_id)

                self.assertEqual(failed.status, "failed")
                self.assertEqual(failed.error, "task_dispatch_failed")
                self.assertEqual(failed.steps[0].observation.payload["failure_type"], expected_failure_type)
                self.assertEqual(failed.steps[0].checkpoint["failure_type"], expected_failure_type)
                event = runtime.list_loop_events(loop.loop_id)[-1]
                self.assertEqual(event["type"], "loop.failed")
                self.assertEqual(event["payload"]["failure_type"], expected_failure_type)

    def test_recovery_policy_retries_adapter_failure_once(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        dispatcher = FailingOnceDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Retry adapter failure once",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
            metadata={
                "recoveryPolicy": {
                    "byFailureType": {
                        "adapter_error": {"action": "retry", "maxRetries": 1}
                    },
                    "defaultAction": "stop",
                }
            },
        )

        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(dispatcher.actions[:2], ["task_dispatch", "task_dispatch"])
        self.assertEqual(completed.steps[0].action.type, "task_dispatch")
        self.assertEqual(completed.steps[0].status, "completed")
        events = runtime.list_loop_events(loop.loop_id)
        event_types = [event["type"] for event in events]
        self.assertIn("loop.step.recovery_decision", event_types)
        self.assertIn("loop.step.recovered", event_types)
        recovered = [event for event in events if event["type"] == "loop.step.recovered"]
        self.assertEqual(recovered[0]["payload"]["recovery_action"], "retry")
        self.assertEqual(recovered[0]["payload"]["failure_type"], "adapter_error")

    def test_recovery_policy_stops_after_retry_budget_is_exhausted(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        dispatcher = ExplodingDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Stop after one adapter retry",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
            metadata={
                "recoveryPolicy": {
                    "byFailureType": {
                        "adapter_error": {"action": "retry", "maxRetries": 1}
                    },
                    "defaultAction": "stop",
                }
            },
        )

        failed = runtime.run_loop(loop.loop_id)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error, "task_dispatch_failed")
        events = runtime.list_loop_events(loop.loop_id)
        decisions = [event for event in events if event["type"] == "loop.step.recovery_decision"]
        recovered = [event for event in events if event["type"] == "loop.step.recovered"]
        self.assertEqual(len(decisions), 2)
        self.assertEqual(len(recovered), 1)
        self.assertFalse(decisions[-1]["payload"]["applied"])
        self.assertEqual(decisions[-1]["payload"]["blocked_reason"], "max_retries_exceeded")

    def test_recovery_policy_schedules_remediation_for_quality_failure(self):
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
            goal="Recover failed quality with remediation",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
            metadata={
                "maxRemediationTurns": 0,
                "recoveryPolicy": {
                    "byFailureType": {
                        "quality_failed": {"action": "remediation", "maxRetries": 1}
                    },
                    "defaultAction": "stop",
                },
            },
        )

        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(dispatcher.actions, ["task_dispatch", "remediation_dispatch"])
        self.assertEqual(
            [step.action.type for step in completed.steps],
            ["task_dispatch", "quality_gate", "remediation_dispatch", "quality_gate", "final_output"],
        )
        recovered = [
            event for event in runtime.list_loop_events(loop.loop_id)
            if event["type"] == "loop.step.recovered"
        ]
        self.assertEqual(recovered[0]["payload"]["recovery_action"], "remediation")
        self.assertEqual(recovered[0]["payload"]["next_action"], "remediation_dispatch")

    def test_recovery_policy_requires_human_for_adapter_failure(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=ExplodingDispatcher(),
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Hold adapter failure for human recovery",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
            metadata={
                "recoveryPolicy": {
                    "byFailureType": {
                        "adapter_error": {"action": "require_human", "maxRetries": 1}
                    },
                    "defaultAction": "stop",
                }
            },
        )

        waiting = runtime.run_loop(loop.loop_id)

        self.assertEqual(waiting.status, "awaiting_approval")
        self.assertEqual([step.status for step in waiting.steps], ["failed", "waiting_approval"])
        self.assertEqual(waiting.steps[-1].action.type, "task_dispatch")
        self.assertEqual(waiting.steps[-1].action.approval_status, "pending")
        events = runtime.list_loop_events(loop.loop_id)
        event_types = [event["type"] for event in events]
        self.assertIn("loop.step.recovery_decision", event_types)
        self.assertIn("loop.step.recovered", event_types)
        self.assertIn("loop.approval_required", event_types)

    def test_dispatch_action_persists_running_step_lease_before_adapter_returns(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        dispatcher = InspectingDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        dispatcher.runtime = runtime
        loop = runtime.start_loop(
            goal="Persist running action leases",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
        )

        completed = runtime.run_loop(loop.loop_id)

        persisted = dispatcher.persisted_during_dispatch
        self.assertEqual(persisted["status"], "running")
        self.assertEqual(len(persisted["steps"]), 1)
        running_step = persisted["steps"][0]
        self.assertEqual(running_step["action"]["type"], "task_dispatch")
        self.assertEqual(running_step["status"], "running")
        self.assertEqual(running_step["checkpoint"]["status"], "running")
        execution = running_step["checkpoint"]["execution"]
        self.assertTrue(execution["lease_id"].startswith("lease-"))
        self.assertGreater(execution["lease_expires_at"], execution["heartbeat_at"])
        self.assertEqual(execution["heartbeat_at"], execution["started_at"])

        completed_step = completed.steps[0]
        self.assertEqual(completed_step.status, "completed")
        self.assertEqual(completed_step.checkpoint["execution"]["lease_id"], execution["lease_id"])
        self.assertGreaterEqual(completed_step.checkpoint["execution"]["duration_ms"], 0)
        event_types = [event["type"] for event in runtime.list_loop_events(loop.loop_id)]
        self.assertIn("loop.step.started", event_types)
        self.assertIn("loop.step.heartbeat", event_types)

    def test_dispatch_adapter_can_renew_running_step_lease(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        dispatcher = HeartbeatRenewingDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        dispatcher.runtime = runtime
        loop = runtime.start_loop(
            goal="Renew long running dispatch lease",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
            metadata={"actionLeaseSeconds": 1},
        )

        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(dispatcher.before["lease_id"], dispatcher.after["lease_id"])
        self.assertEqual(dispatcher.renewal["lease_id"], dispatcher.before["lease_id"])
        self.assertGreater(dispatcher.after["heartbeat_at"], dispatcher.before["heartbeat_at"])
        self.assertGreater(dispatcher.after["lease_expires_at"], dispatcher.before["lease_expires_at"])
        self.assertEqual(dispatcher.after["renewal_count"], 1)
        heartbeat_events = [
            event for event in runtime.list_loop_events(loop.loop_id)
            if event["type"] == "loop.step.heartbeat"
        ]
        self.assertGreaterEqual(len(heartbeat_events), 2)
        renewed_events = [
            event for event in heartbeat_events
            if event["payload"]["lease_id"] == dispatcher.renewal["lease_id"]
        ]
        self.assertEqual(renewed_events[-1]["payload"]["renewal_count"], 1)

    def test_stale_running_action_lease_fails_without_reexecuting_dispatcher(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime, LoopAction, LoopObservation, LoopStep

        dispatcher = RemediationDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Recover an expired running action",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
        )
        started_at = time.time() - 60
        running_step = LoopStep.new(
            loop_id=loop.loop_id,
            turn=1,
            phase="act",
            status="running",
            action=LoopAction.new("task_dispatch", "Dispatch work through host adapter"),
            observation=LoopObservation.new("running", {"action_type": "task_dispatch"}),
            checkpoint={
                "loop_id": loop.loop_id,
                "turn": 1,
                "action_type": "task_dispatch",
                "status": "running",
                "adapter": "RemediationDispatcher",
                "observation_status": "running",
                "execution": {
                    "lease_id": "lease-expired",
                    "started_at": started_at,
                    "heartbeat_at": started_at,
                    "lease_seconds": 0.01,
                    "lease_expires_at": started_at + 0.01,
                },
            },
        )
        loop.status = "running"
        loop.turn_count = 1
        loop.steps.append(running_step)
        loop.checkpoint_count = 1
        runtime.store.save_loop(loop)

        recovered = runtime.run_loop(loop.loop_id)

        self.assertEqual(recovered.status, "failed")
        self.assertEqual(recovered.error, "action_lease_expired")
        self.assertEqual(dispatcher.actions, [])
        self.assertEqual(recovered.steps[0].status, "failed")
        self.assertEqual(recovered.steps[0].observation.status, "failed")
        self.assertEqual(recovered.steps[0].observation.payload["error"], "action_lease_expired")
        self.assertEqual(recovered.steps[0].observation.payload["failure_type"], "lease_expired")
        self.assertEqual(recovered.steps[0].checkpoint["status"], "failed")
        self.assertEqual(recovered.steps[0].checkpoint["failure_type"], "lease_expired")
        events = runtime.list_loop_events(loop.loop_id)
        event_types = [event["type"] for event in events]
        self.assertIn("loop.step.lease_expired", event_types)
        self.assertIn("loop.failed", event_types)
        self.assertEqual(events[-1]["payload"]["failure_type"], "lease_expired")

    def test_recovery_policy_retries_expired_running_action_lease(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime, LoopAction, LoopObservation, LoopStep

        dispatcher = RemediationDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Retry an expired running action",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
            metadata={
                "recoveryPolicy": {
                    "byFailureType": {
                        "lease_expired": {"action": "retry", "maxRetries": 1}
                    },
                    "defaultAction": "stop",
                }
            },
        )
        started_at = time.time() - 60
        running_step = LoopStep.new(
            loop_id=loop.loop_id,
            turn=1,
            phase="act",
            status="running",
            action=LoopAction.new("task_dispatch", "Dispatch work through host adapter"),
            observation=LoopObservation.new("running", {"action_type": "task_dispatch"}),
            checkpoint={
                "loop_id": loop.loop_id,
                "turn": 1,
                "action_type": "task_dispatch",
                "status": "running",
                "adapter": "RemediationDispatcher",
                "observation_status": "running",
                "execution": {
                    "lease_id": "lease-expired",
                    "started_at": started_at,
                    "heartbeat_at": started_at,
                    "lease_seconds": 0.01,
                    "lease_expires_at": started_at + 0.01,
                },
            },
        )
        loop.status = "running"
        loop.turn_count = 1
        loop.steps.append(running_step)
        loop.checkpoint_count = 1
        runtime.store.save_loop(loop)

        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(dispatcher.actions, ["task_dispatch", "remediation_dispatch"])
        self.assertEqual(completed.steps[0].action.type, "task_dispatch")
        self.assertEqual(completed.steps[0].status, "completed")
        events = runtime.list_loop_events(loop.loop_id)
        event_types = [event["type"] for event in events]
        self.assertIn("loop.step.lease_expired", event_types)
        self.assertIn("loop.step.recovery_decision", event_types)
        self.assertIn("loop.step.recovered", event_types)
        self.assertEqual(
            [event for event in events if event["type"] == "loop.step.recovered"][0]["payload"]["failure_type"],
            "lease_expired",
        )

    def test_invalid_host_action_plan_is_rejected_instead_of_silently_ignored(self):
        from across_orchestrator.agent_loop import AgentLoopRuntime

        runtime = AgentLoopRuntime()

        with self.assertRaises(ValueError) as exc:
            runtime.start_loop(
                goal="Reject invalid host action plan",
                project_root=str(self.project),
                metadata={"actionPlan": ["task_dispatch", "unsafe_shell_action", "final_output"]},
            )

        self.assertIn("unsupported actionPlan entries", str(exc.exception))

    def test_host_action_plan_continues_after_inserted_remediation_steps(self):
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
            goal="Continue a host plan after remediation",
            project_root=str(self.project),
            max_turns=10,
            memory_policy={"read": False, "writeCandidates": False},
            metadata={
                "actionPlan": [
                    "task_dispatch",
                    "quality_gate",
                    "task_dispatch",
                    "quality_gate",
                    "final_output",
                ],
                "maxRemediationTurns": 1,
            },
        )

        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            [step.action.type for step in completed.steps],
            [
                "task_dispatch",
                "quality_gate",
                "remediation_dispatch",
                "quality_gate",
                "task_dispatch",
                "quality_gate",
                "final_output",
            ],
        )
        self.assertEqual(dispatcher.actions, ["task_dispatch", "remediation_dispatch", "task_dispatch"])

    def test_remediation_dispatch_can_route_agent_from_failed_quality_gate(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        dispatcher = AgentRoutingDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Route remediation to a specialist",
            project_root=str(self.project),
            agent="owner",
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
            metadata={
                "agentRouting": {
                    "task_dispatch": "builder",
                    "remediation_dispatch": {
                        "browser_e2e": "browser-specialist",
                        "default": "repair-generalist",
                    },
                }
            },
        )

        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            [(call["action_type"], call["agent"]) for call in dispatcher.calls],
            [("task_dispatch", "builder"), ("remediation_dispatch", "browser-specialist")],
        )
        remediation_call = dispatcher.calls[1]
        self.assertEqual(remediation_call["routing"]["selected_agent"], "browser-specialist")
        self.assertEqual(remediation_call["routing"]["matched_gate"], "browser_e2e")
        self.assertEqual(completed.steps[0].action.payload["agent"], "builder")
        self.assertEqual(completed.steps[2].action.payload["agent"], "browser-specialist")

    def test_dispatch_can_route_agent_from_capability_hints_registry(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        dispatcher = AgentRoutingDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Route via host capability hints",
            project_root=str(self.project),
            agent="owner",
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
            metadata={
                "agentCapabilityHints": {
                    "preferred": {
                        "task_dispatch": "implementation",
                    },
                    "constraints": {
                        "requireCapability": {
                            "remediation_dispatch.browser_e2e": "browser_automation",
                        },
                    },
                    "registry": {
                        "agents": [
                            {
                                "agent_id": "builder",
                                "aliases": ["implementation"],
                                "capabilities": ["general_execution"],
                            },
                            {
                                "agent_id": "browser-specialist",
                                "aliases": ["browser-specialist"],
                                "capabilities": ["browser_automation", "frontend_design"],
                            },
                        ],
                    },
                }
            },
        )

        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            [(call["action_type"], call["agent"]) for call in dispatcher.calls],
            [("task_dispatch", "builder"), ("remediation_dispatch", "browser-specialist")],
        )
        self.assertEqual(dispatcher.calls[0]["routing"]["source"], "metadata.agentCapabilityHints.preferred.task_dispatch")
        remediation_routing = dispatcher.calls[1]["routing"]
        self.assertEqual(
            remediation_routing["source"],
            "metadata.agentCapabilityHints.constraints.requireCapability.remediation_dispatch.browser_e2e",
        )
        self.assertEqual(remediation_routing["matched_gate"], "browser_e2e")
        self.assertEqual(remediation_routing["capability_hint"], "browser_automation")

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

    def test_cancel_running_loop_interrupts_cooperative_dispatch_adapter(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        dispatcher = CancellableDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Cancel a running dispatch adapter",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
        )
        results = []

        def run_loop():
            results.append(runtime.run_loop(loop.loop_id))

        thread = threading.Thread(target=run_loop)
        thread.start()
        self.assertTrue(dispatcher.started.wait(timeout=2))

        requested = runtime.cancel_loop(
            loop.loop_id,
            reason="stop running adapter",
            cancel_category="shutdown",
        )

        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(dispatcher.cancel_seen.is_set())
        self.assertEqual(requested.status, "cancelled")
        self.assertEqual(results[0].status, "cancelled")
        self.assertEqual(results[0].error, "stop running adapter")
        self.assertEqual(results[0].steps[0].status, "cancelled")
        self.assertEqual(results[0].steps[0].observation.payload["reason"], "stop running adapter")
        self.assertEqual(results[0].steps[0].observation.payload["cancel_category"], "shutdown")
        self.assertEqual(dispatcher.actions, ["task_dispatch"])
        final_loop = runtime.get_loop(loop.loop_id)
        self.assertEqual(final_loop.status, "cancelled")
        self.assertEqual([step.action.type for step in final_loop.steps], ["task_dispatch"])
        events = runtime.list_loop_events(loop.loop_id)
        event_types = [event["type"] for event in events]
        self.assertIn("loop.cancel_requested", event_types)
        self.assertIn("loop.cancelled", event_types)
        self.assertEqual(events[-1]["payload"]["cancel_category"], "shutdown")

    def test_cancel_running_loop_releases_runtime_from_noncooperative_dispatcher(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        dispatcher = NonCooperativeHangingDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Cancel a noncooperative dispatch adapter",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
        )
        results = []

        def run_loop():
            results.append(runtime.run_loop(loop.loop_id))

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
        self.assertTrue(dispatcher.started.wait(timeout=2))

        requested = runtime.cancel_loop(loop.loop_id, reason="stop noncooperative adapter")

        try:
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(requested.status, "cancelled")
            self.assertTrue(results)
            self.assertEqual(results[0].status, "cancelled")
            self.assertEqual(results[0].error, "stop noncooperative adapter")
            self.assertEqual(results[0].steps[0].status, "cancelled")
        finally:
            dispatcher.release.set()
            thread.join(timeout=2)

    def test_cancelled_dispatch_token_stays_latched_for_detached_worker(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        dispatcher = LateCheckingDetachedDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Latch cancellation after main loop detaches",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
        )
        results = []

        def run_loop():
            results.append(runtime.run_loop(loop.loop_id))

        thread = threading.Thread(target=run_loop)
        thread.start()
        self.assertTrue(dispatcher.started.wait(timeout=2))

        requested = runtime.cancel_loop(loop.loop_id, reason="late cancellation")

        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(requested.status, "cancelled")
        self.assertTrue(results)
        self.assertEqual(results[0].status, "cancelled")

        dispatcher.allow_late_check.set()
        self.assertTrue(dispatcher.finished.wait(timeout=2))
        self.assertTrue(dispatcher.worker_saw_cancel)
        self.assertFalse((self.project / "late-write.txt").exists())
        event_types = [event["type"] for event in runtime.list_loop_events(loop.loop_id)]
        self.assertIn("loop.dispatch.detached", event_types)


if __name__ == "__main__":
    unittest.main()
