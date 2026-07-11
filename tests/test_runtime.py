import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.project = Path(self.tempdir.name) / "project"
        self.home = Path(self.tempdir.name) / "home"
        self.project.mkdir()
        self.home.mkdir()
        self._old_home = os.environ.get("ACROSS_ORCHESTRATOR_HOME")
        self._old_allowed_roots = os.environ.get("ACROSS_ORCHESTRATOR_ALLOWED_PROJECT_ROOTS")
        os.environ["ACROSS_ORCHESTRATOR_HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("ACROSS_ORCHESTRATOR_HOME", None)
        else:
            os.environ["ACROSS_ORCHESTRATOR_HOME"] = self._old_home
        if self._old_allowed_roots is None:
            os.environ.pop("ACROSS_ORCHESTRATOR_ALLOWED_PROJECT_ROOTS", None)
        else:
            os.environ["ACROSS_ORCHESTRATOR_ALLOWED_PROJECT_ROOTS"] = self._old_allowed_roots
        self.tempdir.cleanup()

    def test_submit_task_persists_contracts_and_events(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Build a tiny web app",
            project_root=str(self.project),
            deliverables=["README.md", "web/index.html"],
            agent="demo",
        )

        self.assertTrue(task.task_id.startswith("task-"))
        self.assertEqual(task.status, "pending")
        self.assertTrue(task.metadata["agent_loop"]["loop_id"].startswith("loop-"))
        self.assertEqual(task.metadata["agent_loop"]["runtime"], "across-orchestrator")
        self.assertEqual([sub.path for sub in task.subtasks], ["README.md", "web/index.html"])
        self.assertEqual(task.contract["requiredArtifacts"], ["README.md", "web/index.html"])

        loaded = runtime.get_task(task.task_id)
        self.assertEqual(loaded.task_id, task.task_id)
        self.assertEqual(loaded.project_root, str(self.project.resolve()))

        events = runtime.list_events(task.task_id)
        self.assertEqual([event["type"] for event in events], [
            "task.created",
            "contract.created",
            "agent_loop.created",
            "subtask.created",
            "subtask.created",
        ])
        self.assertEqual([event["sequence"] for event in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(event["event_id"].startswith("task-event-") for event in events))
        self.assertTrue(all(event["loop_id"] == task.metadata["agent_loop"]["loop_id"] for event in events))
        self.assertEqual(events[0]["correlation_id"], f"loop:{task.metadata['agent_loop']['loop_id']}")
        subtask_events = [event for event in events if event["type"] == "subtask.created"]
        self.assertTrue(all(event["correlation_id"].startswith("subtask:") for event in subtask_events))

    def test_submit_task_rejects_invalid_project_root(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        with self.assertRaises(ValueError):
            runtime.submit_task(goal="Invalid root", project_root="")
        with self.assertRaises(ValueError):
            runtime.submit_task(goal="Invalid root", project_root="bad\x00root")

    def test_submit_task_enforces_optional_allowed_project_roots(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        outside = Path(self.tempdir.name) / "outside"
        outside.mkdir()
        os.environ["ACROSS_ORCHESTRATOR_ALLOWED_PROJECT_ROOTS"] = str(self.project)
        runtime = OrchestratorRuntime()
        runtime.submit_task(goal="Allowed root", project_root=str(self.project))
        with self.assertRaises(ValueError):
            runtime.submit_task(goal="Blocked root", project_root=str(outside))

    def test_submit_task_preserves_explicit_serial_plan(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Build a serial pipeline",
            project_root=str(self.project),
            deliverables=["docs/contract.json", "api/server.mjs", "README.md"],
            agent="demo",
            strict_dependency=True,
            task_types=["functional"],
            subtasks=[
                {
                    "id": "contract",
                    "description": "Wave 1 contract",
                    "path": "docs/contract.json",
                    "wave": 1,
                    "dependencies": [],
                },
                {
                    "id": "api",
                    "description": "Wave 2 API",
                    "path": "api/server.mjs",
                    "wave": 2,
                    "dependencies": ["contract"],
                },
                {
                    "id": "readme",
                    "description": "Wave 3 evidence",
                    "path": "README.md",
                    "wave": 3,
                    "dependencies": ["api"],
                },
            ],
        )

        self.assertEqual([subtask.wave for subtask in task.subtasks], [1, 2, 3])
        self.assertEqual(task.metadata["task_types"], ["functional"])
        self.assertEqual(task.metadata["delivery_mode"], "functional")
        self.assertEqual(task.contract["requiredArtifacts"], ["docs/contract.json", "api/server.mjs", "README.md"])
        self.assertEqual(task.contract["serialPlan"], True)
        self.assertEqual(task.subtasks[1].dependencies, [task.subtasks[0].subtask_id])
        self.assertEqual(task.subtasks[2].dependencies, [task.subtasks[1].subtask_id])

        completed = runtime.run_task(task.task_id)
        self.assertEqual(completed.status, "completed")
        self.assertTrue((self.project / "docs/contract.json").exists())
        self.assertTrue((self.project / "api/server.mjs").exists())
        self.assertTrue((self.project / "README.md").exists())

    def test_strict_dependency_fills_missing_explicit_subtask_dependencies(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Build a serial pipeline from an explicit plan",
            project_root=str(self.project),
            deliverables=["docs/contract.json", "api/server.mjs", "README.md"],
            agent="demo",
            strict_dependency=True,
            subtasks=[
                {"id": "contract", "description": "Wave 1 contract", "path": "docs/contract.json", "wave": 1},
                {"id": "api", "description": "Wave 2 API", "path": "api/server.mjs", "wave": 2},
                {"id": "readme", "description": "Wave 3 evidence", "path": "README.md", "wave": 3},
            ],
        )

        self.assertEqual(task.contract["serialPlan"], True)
        self.assertEqual(task.subtasks[0].dependencies, [])
        self.assertEqual(task.subtasks[1].dependencies, [task.subtasks[0].subtask_id])
        self.assertEqual(task.subtasks[2].dependencies, [task.subtasks[1].subtask_id])

    def test_generic_serial_task_produces_runnable_reference_delivery(self):
        import subprocess

        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Build a dependency-free serial release pipeline console",
            project_root=str(self.project),
            deliverables=[
                "docs/contract.json",
                "api/server.mjs",
                "web/index.html",
                "web/styles.css",
                "web/app.js",
                "cli/verify.mjs",
                "tests/e2e-serial.mjs",
                "README.md",
                "evidence/summary.json",
            ],
            agent="claude",
            strict_dependency=True,
            task_types=["functional", "artifact"],
            agent_adapters={"*": {"type": "reference"}},
            subtasks=[
                {"id": "contract", "description": "Wave 1 contract", "path": "docs/contract.json", "agent": "claude", "wave": 1},
                {"id": "api", "description": "Wave 2 API reads docs/contract.json", "path": "api/server.mjs", "agent": "deepseek", "wave": 2, "dependencies": ["contract"]},
                {"id": "html", "description": "Wave 3 UI reads API", "path": "web/index.html", "agent": "hermes", "wave": 3, "dependencies": ["api"]},
                {"id": "css", "description": "Wave 3 UI style", "path": "web/styles.css", "agent": "minimax", "wave": 3, "dependencies": ["api"]},
                {"id": "js", "description": "Wave 3 UI behavior", "path": "web/app.js", "agent": "openclaw", "wave": 3, "dependencies": ["api"]},
                {"id": "cli", "description": "Wave 4 CLI verifies exact manifest", "path": "cli/verify.mjs", "agent": "claude", "wave": 4, "dependencies": ["html", "css", "js"]},
                {"id": "e2e", "description": "Wave 5 E2E starts API and runs CLI", "path": "tests/e2e-serial.mjs", "agent": "codex", "wave": 5, "dependencies": ["cli"]},
                {"id": "readme", "description": "Wave 6 README evidence", "path": "README.md", "agent": "deepseek", "wave": 6, "dependencies": ["e2e"]},
                {"id": "summary", "description": "Wave 6 summary evidence", "path": "evidence/summary.json", "agent": "hermes", "wave": 6, "dependencies": ["e2e"]},
            ],
        )

        completed = runtime.run_task(task.task_id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.metadata["task_types"], ["functional", "artifact"])
        self.assertEqual(completed.metadata["delivery_mode"], "composite")
        self.assertNotIn(
            "Generated by Across Orchestrator demo adapter",
            (self.project / "api/server.mjs").read_text(encoding="utf-8"),
        )
        self.assertIn("createServer", (self.project / "api/server.mjs").read_text(encoding="utf-8"))
        cli = subprocess.run(
            ["node", "cli/verify.mjs"],
            cwd=self.project,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr or cli.stdout)
        self.assertIn('"passed":true', cli.stdout.replace(" ", ""))
        e2e = subprocess.run(
            ["node", "tests/e2e-serial.mjs"],
            cwd=self.project,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(e2e.returncode, 0, e2e.stderr or e2e.stdout)
        self.assertIn("e2e serial passed", e2e.stdout)
        quality = runtime.quality_benchmark(task.task_id)
        self.assertEqual(quality["status"], "passed")
        self.assertTrue(quality["gates"]["serial_wave_dependencies"])
        self.assertTrue(quality["gates"]["cli_generic"])
        self.assertTrue(quality["gates"]["api_service"])

    def test_declared_command_agent_adapter_executes_arbitrary_agent(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        agent_script = self.project / "agent_adapter.py"
        agent_script.write_text(
            "\n".join(
                [
                    "import json",
                    "import os",
                    "from pathlib import Path",
                    "subtask = json.loads(os.environ['ACROSS_SUBTASK_JSON'])",
                    "target = Path(subtask['path'])",
                    "target.parent.mkdir(parents=True, exist_ok=True)",
                    "target.write_text(f\"adapter={subtask['agent']} path={subtask['path']}\\n\", encoding='utf-8')",
                    "print(json.dumps({'agent': subtask['agent'], 'path': subtask['path']}))",
                ]
            ),
            encoding="utf-8",
        )

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Run a host-neutral custom agent",
            project_root=str(self.project),
            deliverables=["artifacts/result.txt"],
            agent="custom-agent",
            agent_adapters={
                "custom-agent": {
                    "type": "command",
                    "command": [sys.executable, str(agent_script)],
                    "description": "Unit-test command adapter for any host agent id.",
                }
            },
        )

        self.assertEqual(task.metadata["agent_adapters"]["custom-agent"]["type"], "command")
        completed = runtime.run_task(task.task_id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual((self.project / "artifacts/result.txt").read_text(encoding="utf-8"), "adapter=custom-agent path=artifacts/result.txt\n")
        completed_events = [event for event in runtime.list_events(task.task_id) if event["type"] == "subtask.completed"]
        self.assertEqual(len(completed_events), 1)
        self.assertIn('"agent": "custom-agent"', completed_events[0]["payload"]["result"]["message"])

    def test_run_task_writes_artifacts_and_builds_evidence(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Create release notes",
            project_root=str(self.project),
            deliverables=["README.md", "notes/release.md"],
            agent="demo",
        )

        completed = runtime.run_task(task.task_id)
        self.assertEqual(completed.status, "completed")
        self.assertTrue((self.project / "README.md").exists())
        self.assertTrue((self.project / "notes/release.md").exists())

        evidence = runtime.evidence_bundle(task.task_id)
        self.assertEqual(evidence["task_id"], task.task_id)
        self.assertEqual(evidence["status"], "completed")
        self.assertEqual(evidence["quality"]["status"], "passed")
        self.assertEqual(evidence["quality"]["finding_state"], "pass")
        self.assertEqual(evidence["quality"]["findings"][0]["source_gate"], "required_artifacts")
        self.assertEqual(evidence["agent_loop"]["status"], "completed")
        self.assertEqual(evidence["agent_loop"]["step_count"], 5)
        self.assertEqual(evidence["agent_loop"]["checkpoint_count"], 5)
        self.assertEqual(
            evidence["agent_loop"]["action_types"],
            ["memory_search", "task_dispatch", "quality_gate", "memory_write_candidate", "final_output"],
        )
        self.assertEqual(evidence["agent_loop"]["memory_policy"]["provider"], "across-context")

        loop = runtime.loop_runtime.get_loop(task.metadata["agent_loop"]["loop_id"])
        dispatch_step = next(step for step in loop.steps if step.action.type == "task_dispatch")
        self.assertEqual(dispatch_step.observation.payload["adapter"], "runtime")
        self.assertEqual(dispatch_step.observation.payload["task_id"], task.task_id)
        self.assertEqual(dispatch_step.observation.payload["completed_subtasks"], 2)
        quality_step = next(step for step in loop.steps if step.action.type == "quality_gate")
        self.assertEqual(quality_step.observation.payload["task_id"], task.task_id)
        self.assertEqual(quality_step.observation.payload["status"], "passed")
        final_step = next(step for step in loop.steps if step.action.type == "final_output")
        self.assertEqual(final_step.observation.payload["task_status"], "completed")
        self.assertIn(task.task_id, final_step.observation.payload["final_output"])

        self.assertEqual(
            [artifact["path"] for artifact in evidence["artifacts"]],
            ["README.md", "notes/release.md"],
        )
        self.assertTrue(all(artifact["sha256"] for artifact in evidence["artifacts"]))

        quality = runtime.quality_benchmark(task.task_id)
        self.assertEqual(quality["status"], "passed")
        self.assertEqual(quality["finding_state"], "pass")
        self.assertEqual(quality["required_artifacts"], 2)
        self.assertEqual(quality["present_artifacts"], 2)

        event_types = [event["type"] for event in runtime.list_events(task.task_id)]
        self.assertIn("task.started", event_types)
        self.assertIn("subtask.completed", event_types)
        self.assertIn("task.completed", event_types)

    def test_run_task_fails_when_non_demo_agent_has_no_adapter(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Require an explicit production adapter",
            project_root=str(self.project),
            deliverables=["adapter-required.md"],
            agent="claude",
        )

        completed = runtime.run_task(task.task_id)

        self.assertEqual(completed.status, "failed")
        self.assertFalse((self.project / "adapter-required.md").exists())
        events = runtime.list_events(task.task_id)
        failed = [event for event in events if event["type"] == "task.failed"]
        self.assertTrue(failed)
        self.assertIn("No adapter configured for agent claude", failed[-1]["payload"]["error"])
        self.assertEqual(failed[-1]["payload"]["failure_type"], "adapter_error")
        loop = runtime.loop_runtime.get_loop(task.metadata["agent_loop"]["loop_id"])
        self.assertEqual(loop.status, "failed")
        self.assertEqual(loop.error, "task_dispatch_failed")
        event_types = [event["type"] for event in events]
        self.assertIn("agent_loop.failed", event_types)
        self.assertNotIn("agent_loop.completed", event_types)

    def test_run_task_preserves_root_loop_failure_type_when_syncing_task_status(self):
        from across_orchestrator.agent_loop import LoopAction, LoopObservation, LoopStep
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Preserve terminal failure classification",
            project_root=str(self.project),
            deliverables=["blocked.md"],
            agent="demo",
        )
        loop = runtime.loop_runtime.get_loop(task.metadata["agent_loop"]["loop_id"])
        failure_type = "environment_blocked"
        observation_payload = {
            "action_type": "task_dispatch",
            "error": "task_dispatch missing required toolchain",
            "failure_type": failure_type,
        }
        loop.status = "failed"
        loop.error = "task_dispatch_failed"
        loop.turn_count = 1
        loop.steps.append(
            LoopStep.new(
                loop_id=loop.loop_id,
                turn=1,
                phase="act",
                status="failed",
                action=LoopAction.new("task_dispatch", "Dispatch work through host adapter"),
                observation=LoopObservation.new("failed", observation_payload),
                checkpoint={
                    "loop_id": loop.loop_id,
                    "turn": 1,
                    "action_type": "task_dispatch",
                    "status": "failed",
                    "adapter": "RuntimeLoopDispatcher",
                    "observation_status": "failed",
                    "failure_type": failure_type,
                },
            )
        )
        loop.checkpoint_count = 1
        runtime.store.save_loop(loop)

        failed = runtime.run_task(task.task_id)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.metadata["agent_loop"]["status"], "failed")
        self.assertEqual(failed.metadata["agent_loop"]["error"], "task_dispatch_failed")
        self.assertEqual(failed.metadata["agent_loop"]["failure_type"], failure_type)
        events = runtime.list_events(task.task_id)
        agent_loop_failed = [event for event in events if event["type"] == "agent_loop.failed"]
        task_failed = [event for event in events if event["type"] == "task.failed"]
        self.assertEqual(agent_loop_failed[-1]["payload"]["failure_type"], failure_type)
        self.assertEqual(task_failed[-1]["payload"]["failure_type"], failure_type)

    def test_run_task_event_stream_marks_agent_loop_awaiting_approval(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Require approval before dispatch",
            project_root=str(self.project),
            deliverables=["approval-required.md"],
            agent="demo",
        )
        loop = runtime.loop_runtime.get_loop(task.metadata["agent_loop"]["loop_id"])
        loop.approval_policy = {"requireApprovalFor": ["task_dispatch"]}
        runtime.store.save_loop(loop)

        waiting = runtime.run_task(task.task_id)

        self.assertEqual(waiting.status, "running")
        updated_loop = runtime.loop_runtime.get_loop(loop.loop_id)
        self.assertEqual(updated_loop.status, "awaiting_approval")
        event_types = [event["type"] for event in runtime.list_events(task.task_id)]
        self.assertIn("agent_loop.awaiting_approval", event_types)
        self.assertNotIn("agent_loop.completed", event_types)

    def test_run_task_syncs_cancelled_agent_loop_to_task_status(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Cancel a task while waiting for approval",
            project_root=str(self.project),
            deliverables=["cancelled.md"],
            agent="demo",
        )
        loop = runtime.loop_runtime.get_loop(task.metadata["agent_loop"]["loop_id"])
        loop.approval_policy = {"requireApprovalFor": ["task_dispatch"]}
        runtime.store.save_loop(loop)
        waiting = runtime.run_task(task.task_id)
        runtime.loop_runtime.cancel_loop(loop.loop_id, reason="user cancel")

        cancelled = runtime.run_task(waiting.task_id)

        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.metadata["agent_loop"]["status"], "cancelled")
        events = runtime.list_events(task.task_id)
        cancelled_events = [event for event in events if event["type"] == "task.cancelled"]
        self.assertNotIn("failure_type", cancelled_events[-1]["payload"])
        event_types = [event["type"] for event in events]
        self.assertIn("agent_loop.cancelled", event_types)
        self.assertIn("task.cancelled", event_types)
        self.assertNotIn("agent_loop.completed", event_types)

    def test_run_task_is_idempotent_after_agent_loop_cancellation(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Do not rerun a cancelled task",
            project_root=str(self.project),
            deliverables=["cancel-idempotent.md"],
            agent="demo",
        )
        loop = runtime.loop_runtime.get_loop(task.metadata["agent_loop"]["loop_id"])
        loop.approval_policy = {"requireApprovalFor": ["task_dispatch"]}
        runtime.store.save_loop(loop)
        waiting = runtime.run_task(task.task_id)
        runtime.loop_runtime.cancel_loop(loop.loop_id, reason="user cancel")
        first = runtime.run_task(waiting.task_id)
        first_events = runtime.list_events(task.task_id)

        second = runtime.run_task(first.task_id)
        second_events = runtime.list_events(task.task_id)

        self.assertEqual(first.status, "cancelled")
        self.assertEqual(second.status, "cancelled")
        self.assertEqual(
            [event["type"] for event in second_events],
            [event["type"] for event in first_events],
        )

    def test_cancel_running_command_adapter_terminates_subprocess(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        adapter_script = Path(self.tempdir.name) / "sleeping_adapter.py"
        adapter_script.write_text(
            "\n".join([
                "from pathlib import Path",
                "import signal",
                "import sys",
                "import time",
                "",
                "def stop(signum, frame):",
                "    Path('terminated.flag').write_text('terminated', encoding='utf-8')",
                "    sys.exit(42)",
                "",
                "signal.signal(signal.SIGTERM, stop)",
                "Path('started.flag').write_text('started', encoding='utf-8')",
                "for _ in range(200):",
                "    time.sleep(0.05)",
                "Path('cancelled.md').write_text('should not be written', encoding='utf-8')",
                "print('completed')",
            ]),
            encoding="utf-8",
        )
        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Cancel a running command adapter",
            project_root=str(self.project),
            deliverables=["cancelled.md"],
            agent="worker",
            agent_adapters={"worker": {"type": "command", "command": [sys.executable, str(adapter_script)]}},
        )
        loop_id = task.metadata["agent_loop"]["loop_id"]
        results = []

        def run_task():
            results.append(runtime.run_task(task.task_id))

        thread = threading.Thread(target=run_task)
        thread.start()
        deadline = time.time() + 3
        while time.time() < deadline and not (self.project / "started.flag").exists():
            time.sleep(0.01)
        self.assertTrue((self.project / "started.flag").exists())

        requested = runtime.loop_runtime.cancel_loop(loop_id, reason="user stopped subprocess")

        thread.join(timeout=4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(requested.status, "cancelled")
        self.assertTrue(results)
        self.assertEqual(results[0].status, "cancelled")
        self.assertTrue((self.project / "terminated.flag").exists())
        self.assertFalse((self.project / "cancelled.md").exists())
        loop = runtime.loop_runtime.get_loop(loop_id)
        self.assertEqual(loop.status, "cancelled")
        task_events = [event["type"] for event in runtime.list_events(task.task_id)]
        self.assertIn("subtask.cancelled", task_events)
        self.assertIn("task.cancelled", task_events)
        self.assertIn("agent_loop.cancelled", task_events)

    def test_run_task_syncs_rejected_agent_loop_to_failed_task_status(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Reject a task dispatch approval",
            project_root=str(self.project),
            deliverables=["rejected.md"],
            agent="demo",
        )
        loop = runtime.loop_runtime.get_loop(task.metadata["agent_loop"]["loop_id"])
        loop.approval_policy = {"requireApprovalFor": ["task_dispatch"]}
        runtime.store.save_loop(loop)
        waiting = runtime.run_task(task.task_id)
        action_id = runtime.loop_runtime.get_loop(loop.loop_id).steps[-1].action.action_id
        runtime.loop_runtime.reject_action(loop.loop_id, action_id, reason="not approved")

        failed = runtime.run_task(waiting.task_id)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.metadata["agent_loop"]["status"], "stopped")
        self.assertEqual(failed.metadata["agent_loop"]["error"], "approval_rejected")
        failed_events = [event for event in runtime.list_events(task.task_id) if event["type"] == "task.failed"]
        self.assertEqual(failed_events[-1]["payload"]["failure_type"], "approval_rejected")
        event_types = [event["type"] for event in runtime.list_events(task.task_id)]
        self.assertIn("agent_loop.stopped", event_types)
        self.assertIn("task.failed", event_types)
        self.assertNotIn("agent_loop.completed", event_types)

    def test_run_task_is_idempotent_after_rejected_agent_loop_failure(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Do not rerun a rejected task",
            project_root=str(self.project),
            deliverables=["reject-idempotent.md"],
            agent="demo",
        )
        loop = runtime.loop_runtime.get_loop(task.metadata["agent_loop"]["loop_id"])
        loop.approval_policy = {"requireApprovalFor": ["task_dispatch"]}
        runtime.store.save_loop(loop)
        waiting = runtime.run_task(task.task_id)
        action_id = runtime.loop_runtime.get_loop(loop.loop_id).steps[-1].action.action_id
        runtime.loop_runtime.reject_action(loop.loop_id, action_id, reason="not approved")
        first = runtime.run_task(waiting.task_id)
        first_events = runtime.list_events(task.task_id)

        second = runtime.run_task(first.task_id)
        second_events = runtime.list_events(task.task_id)

        self.assertEqual(first.status, "failed")
        self.assertEqual(second.status, "failed")
        self.assertEqual(
            [event["type"] for event in second_events],
            [event["type"] for event in first_events],
        )

    def test_run_task_normalizes_legacy_stopped_task_status_without_new_events(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Normalize a legacy stopped task",
            project_root=str(self.project),
            deliverables=["legacy-stopped.md"],
            agent="demo",
        )
        task.status = "stopped"
        runtime.store.save_task(task)
        first_events = runtime.list_events(task.task_id)

        loaded = runtime.get_task(task.task_id)
        result = runtime.run_task(task.task_id)
        second_events = runtime.list_events(task.task_id)

        self.assertEqual(loaded.status, "failed")
        self.assertEqual(result.status, "failed")
        self.assertEqual(
            [event["type"] for event in second_events],
            [event["type"] for event in first_events],
        )

    def test_run_task_syncs_max_turns_agent_loop_stop_to_failed_task_status(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Stop before dispatch when max turns are exhausted",
            project_root=str(self.project),
            deliverables=["max-turns.md"],
            agent="demo",
        )
        loop = runtime.loop_runtime.get_loop(task.metadata["agent_loop"]["loop_id"])
        loop.max_turns = 1
        runtime.store.save_loop(loop)

        failed = runtime.run_task(task.task_id)

        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.metadata["agent_loop"]["status"], "stopped")
        self.assertEqual(failed.metadata["agent_loop"]["error"], "max_turns_exceeded")
        failed_events = [event for event in runtime.list_events(task.task_id) if event["type"] == "task.failed"]
        self.assertEqual(failed_events[-1]["payload"]["failure_type"], "max_turns_exceeded")
        event_types = [event["type"] for event in runtime.list_events(task.task_id)]
        self.assertIn("agent_loop.stopped", event_types)
        self.assertIn("task.failed", event_types)
        self.assertNotIn("agent_loop.completed", event_types)

    def test_run_task_is_idempotent_after_agent_loop_completion(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Create an idempotent artifact",
            project_root=str(self.project),
            deliverables=["idempotent.md"],
            agent="demo",
        )
        first = runtime.run_task(task.task_id)
        first_events = runtime.list_events(task.task_id)

        second = runtime.run_task(task.task_id)
        second_events = runtime.list_events(task.task_id)

        self.assertEqual(first.status, "completed")
        self.assertEqual(second.status, "completed")
        self.assertEqual(
            len([event for event in second_events if event["type"] == "subtask.completed"]),
            len([event for event in first_events if event["type"] == "subtask.completed"]),
        )

    def test_standalone_agent_loop_with_deliverables_creates_and_runs_task(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        loop = runtime.loop_runtime.start_loop(
            goal="Produce a standalone loop artifact",
            project_root=str(self.project),
            agent="demo",
            max_turns=4,
            memory_policy={"read": False, "writeCandidates": False},
            metadata={"deliverables": ["standalone/result.md"]},
        )

        completed = runtime.loop_runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            [step.action.type for step in completed.steps],
            ["task_dispatch", "quality_gate", "final_output"],
        )
        dispatch_payload = completed.steps[0].observation.payload
        self.assertEqual(dispatch_payload["adapter"], "runtime")
        self.assertTrue(dispatch_payload["task_id"].startswith("task-"))
        self.assertEqual(dispatch_payload["completed_subtasks"], 1)
        self.assertEqual(completed.steps[1].observation.payload["status"], "passed")
        self.assertEqual(completed.steps[2].observation.payload["task_status"], "completed")
        self.assertTrue((self.project / "standalone/result.md").exists())

        task = runtime.get_task(dispatch_payload["task_id"])
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.metadata["agent_loop"]["loop_id"], loop.loop_id)

    def test_store_files_are_plain_json_and_jsonl(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_task(
            goal="Persist state",
            project_root=str(self.project),
            deliverables=["README.md"],
            agent="demo",
        )

        task_file = self.home / "tasks" / f"{task.task_id}.json"
        event_file = self.home / "events" / f"{task.task_id}.jsonl"
        self.assertTrue(task_file.exists())
        self.assertTrue(event_file.exists())
        self.assertEqual(json.loads(task_file.read_text())["task_id"], task.task_id)
        self.assertGreaterEqual(len(event_file.read_text().splitlines()), 3)

    def test_old_across_orchestrator_home_is_ignored(self):
        with tempfile.TemporaryDirectory() as tempdir:
            from across_orchestrator.store import LocalStore

            across_home = Path(tempdir) / "across"
            legacy_home = Path(tempdir) / ".across-orchestrator"
            legacy_tasks = legacy_home / "tasks"
            legacy_events = legacy_home / "events"
            legacy_tasks.mkdir(parents=True)
            legacy_events.mkdir(parents=True)
            (legacy_tasks / "task-legacy.json").write_text(
                json.dumps({
                    "task_id": "task-legacy",
                    "goal": "Legacy task",
                    "project_root": str(self.project),
                    "deliverables": ["README.md"],
                    "agent": "demo",
                    "subtasks": [],
                    "contract": {},
                    "metadata": {},
                    "status": "pending",
                    "created_at": 1,
                    "updated_at": 1,
                }),
                encoding="utf-8",
            )

            store = LocalStore(env={"HOME": tempdir, "ACROSS_HOME": str(across_home)})

            self.assertEqual(store.home, across_home.resolve() / "data" / "across-orchestrator")
            self.assertEqual(store.list_task_ids(), [])

    def test_old_across_orchestrator_home_does_not_backfill_when_across_data_exists(self):
        with tempfile.TemporaryDirectory() as tempdir:
            from across_orchestrator.store import LocalStore

            across_home = Path(tempdir) / "across"
            new_home = across_home / "data" / "across-orchestrator"
            new_tasks = new_home / "tasks"
            legacy_home = Path(tempdir) / ".across-orchestrator"
            legacy_tasks = legacy_home / "tasks"
            legacy_events = legacy_home / "events"
            new_tasks.mkdir(parents=True)
            legacy_tasks.mkdir(parents=True)
            legacy_events.mkdir(parents=True)
            (new_tasks / "task-current.json").write_text(
                json.dumps({
                    "task_id": "task-current",
                    "goal": "Current task",
                    "project_root": str(self.project),
                    "deliverables": ["README.md"],
                    "agent": "demo",
                    "subtasks": [],
                    "contract": {},
                    "metadata": {},
                    "status": "pending",
                    "created_at": 1,
                    "updated_at": 1,
                }),
                encoding="utf-8",
            )
            (legacy_tasks / "task-legacy.json").write_text(
                json.dumps({
                    "task_id": "task-legacy",
                    "goal": "Legacy task",
                    "project_root": str(self.project),
                    "deliverables": ["README.md"],
                    "agent": "demo",
                    "subtasks": [],
                    "contract": {},
                    "metadata": {},
                    "status": "pending",
                    "created_at": 1,
                    "updated_at": 1,
                }),
                encoding="utf-8",
            )
            (legacy_events / "task-legacy.jsonl").write_text(
                '{"type":"created","task_id":"task-legacy"}\n',
                encoding="utf-8",
            )

            store = LocalStore(env={"HOME": tempdir, "ACROSS_HOME": str(across_home)})

            self.assertEqual(store.list_task_ids(), ["task-current"])
            self.assertFalse((new_home / "events" / "task-legacy.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
