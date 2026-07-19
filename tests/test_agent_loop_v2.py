import json
import os
import sys
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


class UnifiedFindingStateFailingGate:
    def evaluate(self, *, loop, context):
        return {
            "quality": "unknown",
            "passed": True,
            "finding_state": "blocked",
            "findings": [{
                "id": "browser_e2e",
                "state": "blocked",
                "source_gate": "browser_e2e",
                "summary": "Browser E2E is blocked.",
            }],
            "summary": "Unified finding state blocked promotion.",
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


class CancellableRemediationDispatcher:
    requires_cancel_ack = True

    def __init__(self):
        self.remediation_started = threading.Event()
        self.cancel_seen = threading.Event()
        self.actions = []

    def dispatch(self, *, loop, action_type, context):
        self.actions.append(action_type)
        if action_type != "remediation_dispatch":
            return {
                "dispatch": "completed",
                "adapter": "cancellable-remediation-dispatcher",
            }
        self.remediation_started.set()
        deadline = time.time() + 2
        while time.time() < deadline:
            if context["cancellation"].is_cancelled():
                self.cancel_seen.set()
                context["cancellation"].raise_if_cancelled()
            context["heartbeat"]()
            time.sleep(0.01)
        return {
            "dispatch": "completed",
            "adapter": "cancellable-remediation-dispatcher",
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
        self.across_home = Path(self.tempdir.name) / "across-home"
        self.across_home.mkdir()
        self._old_home = os.environ.get("ACROSS_ORCHESTRATOR_HOME")
        self._old_across_home = os.environ.get("ACROSS_HOME")
        os.environ["ACROSS_ORCHESTRATOR_HOME"] = str(self.home)
        os.environ["ACROSS_HOME"] = str(self.across_home)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("ACROSS_ORCHESTRATOR_HOME", None)
        else:
            os.environ["ACROSS_ORCHESTRATOR_HOME"] = self._old_home
        if self._old_across_home is None:
            os.environ.pop("ACROSS_HOME", None)
        else:
            os.environ["ACROSS_HOME"] = self._old_across_home
        self.tempdir.cleanup()

    def test_agent_loop_uses_adapters_and_branches_to_remediation(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime, DefaultQualityGate

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

    def test_agent_loop_can_dispatch_to_host_model_command(self):
        from across_orchestrator.agent_loop import AgentLoopRuntime

        command = self.project / "host_model.py"
        command.write_text(
            "import json, os, sys\n"
            "blocked = [key for key in os.environ if key.startswith('_PYI') or key.startswith('PYTHON')]\n"
            "if blocked:\n"
            "    raise SystemExit('packaged host environment leaked: ' + ','.join(sorted(blocked)))\n"
            "request = json.loads(sys.stdin.read())\n"
            "print(json.dumps({\n"
            "  'schema_version': 'across-host-model-decision/1.0',\n"
            "  'model_backed': True,\n"
            "  'provider': 'fake-host',\n"
            "  'model': 'fake-loop-engineer',\n"
            "  'decision_hash': 'abc123',\n"
            "  'decision': {'summary': 'Patch candidate docs.', 'patches': [\n"
            "    {'path': 'docs/ITERATION.md', 'mode': 'overwrite', 'content': 'candidate patch'}\n"
            "  ]},\n"
            "  'patches': [{'path': 'docs/ITERATION.md', 'mode': 'overwrite', 'content': 'candidate patch'}]\n"
            "}))\n",
            encoding="utf-8",
        )
        previous_pyi = os.environ.get("_PYI_ARCHIVE_FILE")
        previous_pythonpath = os.environ.get("PYTHONPATH")
        os.environ["_PYI_ARCHIVE_FILE"] = "/Applications/Across Agents Assistant.app/Contents/Resources/backend/backend"
        os.environ["PYTHONPATH"] = "/tmp/packaged-backend-src-that-must-not-leak"
        runtime = AgentLoopRuntime()
        try:
            loop = runtime.start_loop(
                goal="Use host model to plan a candidate patch",
                project_root=str(self.project),
                memory_policy={"read": False, "writeCandidates": False},
                metadata={
                    "actionPlan": ["task_dispatch", "quality_gate", "final_output"],
                    "candidate_workspace": str(self.project),
                    "model_policy": {
                        "required": True,
                        "host_model_command": [sys.executable, str(command)],
                        "allowed_patch_paths": ["docs/ITERATION.md"],
                    },
                },
            )
            completed = runtime.run_loop(loop.loop_id)
        finally:
            if previous_pyi is None:
                os.environ.pop("_PYI_ARCHIVE_FILE", None)
            else:
                os.environ["_PYI_ARCHIVE_FILE"] = previous_pyi
            if previous_pythonpath is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = previous_pythonpath

        self.assertEqual(completed.status, "completed")
        dispatch = next(step for step in completed.steps if step.action.type == "task_dispatch")
        quality = next(step for step in completed.steps if step.action.type == "quality_gate")
        self.assertTrue(dispatch.observation.payload["model_backed"])
        self.assertEqual(dispatch.observation.payload["provider"], "fake-host")
        self.assertEqual(dispatch.observation.payload["patch_count"], 1)
        self.assertTrue(quality.observation.payload["passed"])
        self.assertEqual(quality.observation.payload["model_patch_count"], 1)
        summary = runtime.get_loop_evidence_summary(loop.loop_id)
        self.assertEqual(summary["model_decision"]["provider"], "fake-host")
        self.assertEqual(summary["model_decision"]["patch_paths"], ["docs/ITERATION.md"])

    def test_approval_resume_executes_the_approved_adapter_action(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime, DefaultQualityGate

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

    def test_finding_lifecycle_preserves_failed_and_passing_repair_rounds(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=RemediationDispatcher(),
                quality_gate=FailingThenPassingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Repair a failed browser gate",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
            metadata={"maxRemediationTurns": 1},
        )

        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.finding_state, "pass")
        self.assertEqual(completed.findings[0]["repair_round"], 1)
        self.assertEqual(
            [(item["repair_round"], item["state"], item["source_gate"]) for item in completed.finding_history],
            [(0, "failed", "browser_e2e"), (1, "pass", "quality_gate")],
        )
        events = runtime.list_loop_events(loop.loop_id)
        event_types = [event["type"] for event in events]
        self.assertEqual(event_types.count("loop.findings.updated"), 2)
        self.assertIn("loop.findings.remediation_scheduled", event_types)
        health = runtime.get_loop_health(loop.loop_id)
        self.assertEqual(health["finding_state"], "pass")
        self.assertEqual(health["finding_round_count"], 2)
        summary = runtime.get_loop_evidence_summary(loop.loop_id)
        self.assertEqual(summary["finding_state"], "pass")
        self.assertEqual(summary["finding_lifecycle"]["round_count"], 2)
        self.assertEqual(summary["finding_lifecycle"]["source_gates"], ["browser_e2e", "quality_gate"])

    def test_finding_lifecycle_transitions_exhausted_repairs_to_blocked(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=RemediationDispatcher(),
                quality_gate=AlwaysFailingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Exhaust browser gate repairs",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
            metadata={"maxRemediationTurns": 1},
        )

        failed = runtime.run_loop(loop.loop_id)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.finding_state, "blocked")
        self.assertEqual(failed.findings[0]["state"], "blocked")
        self.assertEqual(failed.findings[0]["repair_round"], 1)
        self.assertEqual(failed.findings[0]["source_gate"], "browser_e2e")
        self.assertEqual(
            [(item["repair_round"], item["state"]) for item in failed.finding_history],
            [(0, "failed"), (1, "failed"), (1, "blocked")],
        )
        events = runtime.list_loop_events(loop.loop_id)
        blocked = next(event for event in events if event["type"] == "loop.findings.blocked")
        self.assertEqual(blocked["payload"]["reason"], "repair_exhausted")
        self.assertEqual(blocked["payload"]["failed_gates"], ["browser_e2e"])
        self.assertEqual(events[-1]["payload"]["finding_state"], "blocked")
        summary = runtime.get_loop_evidence_summary(loop.loop_id)
        self.assertEqual(summary["finding_lifecycle"]["counts_by_state"]["failed"], 2)
        self.assertEqual(summary["finding_lifecycle"]["counts_by_state"]["blocked"], 1)
        quality_check = next(
            check for check in summary["host_release_evidence"]["checks"]
            if check["id"] == "quality_findings"
        )
        self.assertEqual(quality_check["status"], "blocked")

    def test_cancelling_remediation_preserves_failed_finding_round(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        dispatcher = CancellableRemediationDispatcher()
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=dispatcher,
                quality_gate=AlwaysFailingQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Cancel an in-flight repair",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": False, "writeCandidates": False},
            metadata={"maxRemediationTurns": 1},
        )
        results = []

        thread = threading.Thread(target=lambda: results.append(runtime.run_loop(loop.loop_id)))
        thread.start()
        self.assertTrue(dispatcher.remediation_started.wait(timeout=2))
        requested = runtime.cancel_loop(loop.loop_id, reason="cancel repair", cancel_category="user_cancelled")
        thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertTrue(dispatcher.cancel_seen.is_set())
        self.assertEqual(requested.status, "cancelled")
        self.assertEqual(results[0].status, "cancelled")
        self.assertEqual(results[0].finding_state, "failed")
        self.assertEqual(
            [(item["repair_round"], item["state"]) for item in results[0].finding_history],
            [(0, "failed")],
        )
        events = runtime.list_loop_events(loop.loop_id)
        cancelled = next(event for event in events if event["type"] == "loop.findings.remediation_cancelled")
        self.assertEqual(cancelled["payload"]["finding_state"], "failed")
        self.assertEqual(cancelled["payload"]["failed_gates"], ["browser_e2e"])

    def test_unified_finding_state_can_fail_quality_gate(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=RemediationDispatcher(),
                quality_gate=UnifiedFindingStateFailingGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Fail from unified finding state",
            project_root=str(self.project),
            max_turns=6,
            metadata={"maxRemediationTurns": 0},
        )

        failed = runtime.run_loop(loop.loop_id)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error, "quality_gate_failed")
        quality = next(step for step in failed.steps if step.action.type == "quality_gate")
        self.assertEqual(quality.observation.payload["finding_state"], "blocked")
        self.assertEqual(runtime._latest_failed_gates(failed), ["browser_e2e"])

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
        summary = runtime.get_loop_evidence_summary(loop.loop_id)
        self.assertEqual(summary["schema_version"], "0.1")
        self.assertTrue(summary["event_audit"]["sequence_contiguous"])
        self.assertTrue(summary["event_audit"]["event_id_coverage"])
        self.assertEqual(summary["recovery"]["decision_count"], 1)
        self.assertEqual(summary["recovery"]["applied_count"], 1)
        self.assertEqual(summary["recovery"]["decisions"][0]["recovery_action"], "retry")
        self.assertEqual(summary["recovery"]["decisions"][0]["failure_type"], "adapter_error")
        self.assertEqual(summary["recovery"]["recovered_steps"][0]["next_action"], "task_dispatch")
        release_evidence = summary["host_release_evidence"]
        self.assertEqual(release_evidence["readiness"], "attention")
        self.assertEqual(
            [check["id"] for check in release_evidence["checks"]],
            ["event_audit", "action_plan", "capability_routing", "recovery", "memory_candidates", "cancellation", "budget"],
        )
        recovery_check = next(check for check in release_evidence["checks"] if check["id"] == "recovery")
        self.assertEqual(recovery_check["status"], "attention")
        self.assertIn("recovery_applied", [risk["id"] for risk in release_evidence["risks"]])

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
        summary = runtime.get_loop_evidence_summary(loop.loop_id)
        self.assertEqual(summary["host_release_evidence"]["readiness"], "blocked")
        recovery_check = next(check for check in summary["host_release_evidence"]["checks"] if check["id"] == "recovery")
        self.assertEqual(recovery_check["status"], "blocked")
        self.assertIn("recovery_blocked", [risk["id"] for risk in summary["host_release_evidence"]["risks"]])

    def test_host_release_evidence_uses_cancel_category_policy(self):
        from across_orchestrator.agent_loop import (
            CANCEL_CATEGORY_RELEASE_BLOCKING_VALUES,
            CANCEL_CATEGORY_VALUES,
            AgentLoopRuntime,
        )

        runtime = AgentLoopRuntime()
        self.assertEqual(
            tuple(CANCEL_CATEGORY_VALUES),
            ("user_cancelled", "shutdown", "superseded", "timeout_cancelled", "budget_exceeded"),
        )

        for category in CANCEL_CATEGORY_VALUES:
            loop = runtime.start_loop(
                goal=f"Cancel loop with {category}",
                project_root=str(self.project),
                max_turns=3,
                memory_policy={"read": False, "writeCandidates": False},
            )

            runtime.cancel_loop(loop.loop_id, reason=f"{category} requested", cancel_category=category)
            summary = runtime.get_loop_evidence_summary(loop.loop_id)
            release_evidence = summary["host_release_evidence"]
            cancellation_check = next(check for check in release_evidence["checks"] if check["id"] == "cancellation")
            cancellation_risk = next(risk for risk in release_evidence["risks"] if risk["id"] == f"cancelled_{category}")
            expected_status = "blocked" if category in CANCEL_CATEGORY_RELEASE_BLOCKING_VALUES else "attention"

            self.assertEqual(release_evidence["readiness"], expected_status)
            self.assertEqual(cancellation_check["status"], expected_status)
            self.assertEqual(cancellation_check["category"], category)
            self.assertEqual(cancellation_risk["severity"], "high" if expected_status == "blocked" else "medium")

    def test_budget_policy_rejects_excess_concurrent_loop_start(self):
        from across_orchestrator.agent_loop import AgentLoopConcurrencyError, AgentLoopRuntime

        runtime = AgentLoopRuntime()
        runtime.start_loop(
            goal="Hold the first active loop",
            project_root=str(self.project),
            max_turns=8,
            metadata={"agentLoopBudget": {"maxConcurrentLoops": 1}},
        )

        with self.assertRaises(AgentLoopConcurrencyError) as ctx:
            runtime.start_loop(
                goal="Reject the second active loop",
                project_root=str(self.project),
                max_turns=8,
                metadata={"agentLoopBudget": {"maxConcurrentLoops": 1}},
            )

        self.assertEqual(ctx.exception.active_count, 1)
        self.assertEqual(ctx.exception.max_concurrent_loops, 1)

    def test_runtime_budget_stops_loop_with_release_evidence(self):
        from across_orchestrator.agent_loop import AgentLoopRuntime

        runtime = AgentLoopRuntime()
        loop = runtime.start_loop(
            goal="Stop after runtime budget",
            project_root=str(self.project),
            max_turns=8,
            metadata={"agentLoopBudget": {"maxRuntimeSeconds": 0.001}},
        )
        time.sleep(0.01)

        stopped = runtime.run_loop(loop.loop_id)

        self.assertEqual(stopped.status, "stopped")
        self.assertEqual(stopped.error, "max_runtime_exceeded")
        events = runtime.list_loop_events(loop.loop_id)
        self.assertIn("loop.budget.exceeded", [event["type"] for event in events])
        budget_event = next(event for event in events if event["type"] == "loop.budget.exceeded")
        self.assertEqual(budget_event["payload"]["cancel_category"], "budget_exceeded")
        summary = runtime.get_loop_evidence_summary(loop.loop_id)
        budget_check = next(check for check in summary["host_release_evidence"]["checks"] if check["id"] == "budget")
        self.assertEqual(budget_check["status"], "blocked")
        self.assertEqual(summary["host_release_evidence"]["readiness"], "blocked")
        telemetry = runtime.get_loop_telemetry(loop.loop_id)
        self.assertEqual(telemetry["summary"]["cancel_category"], "budget_exceeded")
        self.assertIn(
            "budget_exceeded",
            {metric.get("dimensions", {}).get("cancel_category") for metric in telemetry["metrics"]},
        )

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

    def test_host_declared_check_action_is_recorded_without_side_effects(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(memory_provider=RecordingMemoryProvider())
        )

        loop = runtime.start_loop(
            goal="Record a generic manifest contract check",
            project_root=str(self.project),
            max_turns=8,
            memory_policy={"read": True, "writeCandidates": False},
            metadata={
                "actionPlan": [
                    "memory_search",
                    "manifest_contract_check",
                    "task_dispatch",
                    "quality_gate",
                    "final_output",
                ]
            },
        )

        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            [step.action.type for step in completed.steps],
            ["memory_search", "manifest_contract_check", "task_dispatch", "quality_gate", "final_output"],
        )
        check_step = completed.steps[1]
        self.assertEqual(check_step.phase, "verify")
        self.assertEqual(check_step.observation.payload["mode"], "host_declared_check")
        self.assertFalse(check_step.observation.payload["side_effects"])

    def test_host_declared_check_consumes_generic_validation_contract(self):
        from across_orchestrator.agent_loop import AgentLoopRuntime

        outputs = self.project / "outputs"
        outputs.mkdir()
        (outputs / "risk.csv").write_text(
            "account_id,risk_score,arr_usd,risk_band\n"
            "A-101,100,420000,critical\n"
            "A-107,69,500000,critical\n",
            encoding="utf-8",
        )
        (outputs / "audit.json").write_text('{"loop_id":"loop-test","status":"completed"}', encoding="utf-8")
        (outputs / "memo.md").write_text("A-107 risk score is 69 and the audit passed.\n", encoding="utf-8")
        runtime = AgentLoopRuntime()

        loop = runtime.start_loop(
            goal="Validate a generic artifact contract",
            project_root=str(self.project),
            max_turns=4,
            metadata={
                "actionPlan": ["business_contract_check", "final_output"],
                "validationContract": {
                    "schema_version": "across-validation-contract/1.0",
                    "check_action": "business_contract_check",
                    "artifacts": [
                        {
                            "path": "outputs/risk.csv",
                            "type": "csv",
                            "columns": ["account_id", "risk_score", "arr_usd", "risk_band"],
                            "row_count": 2,
                            "sort": [
                                {"field": "risk_score", "direction": "desc", "numeric": True},
                                {"field": "arr_usd", "direction": "desc", "numeric": True},
                            ],
                            "row_expectations": [
                                {"match": {"account_id": "A-107"}, "expect": {"risk_score": "69", "risk_band": "critical"}},
                            ],
                        },
                        {"path": "outputs/audit.json", "type": "json", "required_keys": ["loop_id", "status"]},
                        {"path": "outputs/memo.md", "type": "markdown", "must_include": ["A-107 risk score is 69"]},
                    ],
                },
            },
        )

        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "completed")
        check_step = completed.steps[0]
        self.assertEqual(check_step.action.type, "business_contract_check")
        self.assertEqual(check_step.status, "completed")
        self.assertEqual(check_step.observation.payload["schema_version"], "across-validation-evidence/1.0")
        self.assertEqual(check_step.observation.payload["status"], "passed")
        self.assertTrue(check_step.observation.payload["passed"])
        self.assertEqual(check_step.observation.payload["failure_count"], 0)
        self.assertEqual(check_step.observation.payload["finding_state"], "pass")
        self.assertEqual(check_step.observation.payload["findings"][0]["schema_version"], "across-autopilot-finding/1.0")
        self.assertEqual(check_step.observation.payload["findings"][0]["source_gate"], "business_contract_check")

    def test_failed_validation_contract_blocks_host_declared_check(self):
        from across_orchestrator.agent_loop import AgentLoopRuntime

        outputs = self.project / "outputs"
        outputs.mkdir()
        (outputs / "risk.csv").write_text(
            "account_id,risk_score,arr_usd,risk_band\n"
            "A-101,100,420000,critical\n"
            "A-107,77,500000,critical\n",
            encoding="utf-8",
        )
        runtime = AgentLoopRuntime()

        loop = runtime.start_loop(
            goal="Block final output when contract fails",
            project_root=str(self.project),
            max_turns=4,
            metadata={
                "actionPlan": ["business_contract_check", "final_output"],
                "validationContract": {
                    "schema_version": "across-validation-contract/1.0",
                    "check_action": "business_contract_check",
                    "artifacts": [
                        {
                            "path": "outputs/risk.csv",
                            "type": "csv",
                            "row_expectations": [
                                {"match": {"account_id": "A-107"}, "expect": {"risk_score": "69"}},
                            ],
                        }
                    ],
                },
            },
        )

        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "failed")
        self.assertEqual(completed.steps[0].status, "failed")
        self.assertEqual(completed.steps[0].observation.payload["status"], "failed")
        self.assertEqual(completed.steps[0].observation.payload["failure_type"], "quality_failed")
        self.assertEqual(completed.steps[0].observation.payload["finding_state"], "failed")
        self.assertEqual(completed.steps[0].observation.payload["failed_gates"], ["csv_row_expectation"])
        self.assertEqual(completed.finding_state, "blocked")
        self.assertEqual(completed.findings[0]["source_gate"], "csv_row_expectation")
        self.assertIn("risk_score expected 69 got 77", json.dumps(completed.steps[0].observation.payload))
        self.assertIsNone(completed.final_output)

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
        self.assertEqual(dispatcher.calls[0]["routing"]["schema_version"], "agent-loop-routing/1.0")
        self.assertIn("reason", dispatcher.calls[0]["routing"])
        self.assertTrue(dispatcher.calls[0]["routing"]["alternatives"])
        remediation_routing = dispatcher.calls[1]["routing"]
        self.assertEqual(
            remediation_routing["source"],
            "metadata.agentCapabilityHints.constraints.requireCapability.remediation_dispatch.browser_e2e",
        )
        self.assertEqual(remediation_routing["matched_gate"], "browser_e2e")
        self.assertEqual(remediation_routing["capability_hint"], "browser_automation")
        self.assertEqual(completed.steps[0].action.payload["routing"]["source"], "metadata.agentCapabilityHints.preferred.task_dispatch")
        summary = runtime.get_loop_evidence_summary(loop.loop_id)
        self.assertEqual(summary["routing"]["routed_action_count"], 2)
        self.assertEqual(summary["routing"]["non_default_route_count"], 2)
        self.assertEqual(summary["routing"]["capability_hint_route_count"], 2)
        self.assertEqual(
            [item["selected_agent"] for item in summary["routing"]["outcomes"]],
            ["builder", "browser-specialist"],
        )
        self.assertEqual(
            summary["routing"]["outcomes"][1]["source"],
            "metadata.agentCapabilityHints.constraints.requireCapability.remediation_dispatch.browser_e2e",
        )
        self.assertEqual(summary["routing"]["outcomes"][1]["matched_gate"], "browser_e2e")
        self.assertEqual(summary["routing"]["outcomes"][1]["capability_hint"], "browser_automation")
        self.assertEqual(summary["routing"]["outcomes"][1]["schema_version"], "agent-loop-routing/1.0")
        self.assertTrue(summary["routing"]["outcomes"][1]["alternatives"])
        release_evidence = summary["host_release_evidence"]
        self.assertEqual(release_evidence["readiness"], "ready")
        routing_check = next(check for check in release_evidence["checks"] if check["id"] == "capability_routing")
        self.assertEqual(routing_check["status"], "passed")
        self.assertEqual(routing_check["routed_action_count"], 2)
        self.assertEqual(routing_check["capability_hint_route_count"], 2)
        memory_check = next(check for check in release_evidence["checks"] if check["id"] == "memory_candidates")
        self.assertEqual(memory_check["status"], "passed")

    def test_loop_telemetry_is_bounded_and_excludes_raw_memory_text(self):
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime, DefaultQualityGate

        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=RecordingMemoryProvider(),
                dispatcher=RemediationDispatcher(),
                quality_gate=DefaultQualityGate(),
            )
        )
        loop = runtime.start_loop(
            goal="Measure loop telemetry",
            project_root=str(self.project),
            max_turns=8,
        )

        completed = runtime.run_loop(loop.loop_id)
        telemetry = runtime.get_loop_telemetry(loop.loop_id)
        raw = json.dumps(telemetry, sort_keys=True)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(telemetry["schema_version"], "agent-loop-telemetry/1.0")
        self.assertEqual(telemetry["loop_id"], loop.loop_id)
        self.assertIn("loop.duration_ms", [item["metric"] for item in telemetry["metrics"]])
        self.assertEqual(telemetry["summary"]["memory_candidate_count"], 1)
        self.assertNotIn("Reuse the existing serial delivery contract", raw)

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
