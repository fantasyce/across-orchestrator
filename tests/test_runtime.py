import json
import os
import sys
import tempfile
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
        os.environ["ACROSS_ORCHESTRATOR_HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("ACROSS_ORCHESTRATOR_HOME", None)
        else:
            os.environ["ACROSS_ORCHESTRATOR_HOME"] = self._old_home
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
        self.assertEqual(evidence["agent_loop"]["status"], "completed")
        self.assertEqual(evidence["agent_loop"]["step_count"], 5)
        self.assertEqual(evidence["agent_loop"]["checkpoint_count"], 5)
        self.assertEqual(
            evidence["agent_loop"]["action_types"],
            ["memory_search", "task_dispatch", "quality_gate", "memory_write_candidate", "final_output"],
        )
        self.assertEqual(evidence["agent_loop"]["memory_policy"]["provider"], "across-context")
        self.assertEqual(
            [artifact["path"] for artifact in evidence["artifacts"]],
            ["README.md", "notes/release.md"],
        )
        self.assertTrue(all(artifact["sha256"] for artifact in evidence["artifacts"]))

        quality = runtime.quality_benchmark(task.task_id)
        self.assertEqual(quality["status"], "passed")
        self.assertEqual(quality["required_artifacts"], 2)
        self.assertEqual(quality["present_artifacts"], 2)

        event_types = [event["type"] for event in runtime.list_events(task.task_id)]
        self.assertIn("task.started", event_types)
        self.assertIn("subtask.completed", event_types)
        self.assertIn("task.completed", event_types)

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
