import json
import os
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

    def test_legacy_default_home_migrates_into_across_data_namespace(self):
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
            self.assertEqual(store.list_task_ids(), ["task-legacy"])

    def test_legacy_default_home_backfills_when_across_data_exists(self):
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

            self.assertEqual(store.list_task_ids(), ["task-current", "task-legacy"])
            self.assertTrue((new_home / "events" / "task-legacy.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
