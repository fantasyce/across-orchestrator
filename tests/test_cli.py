import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(__file__).resolve().parents[1]
        self.project = Path(self.tempdir.name) / "project"
        self.home = Path(self.tempdir.name) / "home"
        self.project.mkdir()
        self.home.mkdir()
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(self.root / "src")
        self.env["ACROSS_ORCHESTRATOR_HOME"] = str(self.home)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "across_orchestrator.cli", *args],
            cwd=self.root,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_cli_submit_run_and_inspect_task(self):
        submit = self.run_cli(
            "submit",
            "Build a tiny product page",
            "--project",
            str(self.project),
            "--deliverable",
            "README.md",
            "--deliverable",
            "web/index.html",
            "--json",
        )
        self.assertEqual(submit.returncode, 0, submit.stderr)
        task_id = json.loads(submit.stdout)["task_id"]
        self.assertTrue(task_id.startswith("task-"))

        run = self.run_cli("run", task_id, "--json")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["status"], "completed")

        status = self.run_cli("status", task_id, "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["status"], "completed")

        evidence = self.run_cli("evidence", task_id, "--json")
        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        evidence_payload = json.loads(evidence.stdout)
        self.assertEqual(evidence_payload["quality"]["status"], "passed")
        self.assertEqual(len(evidence_payload["artifacts"]), 2)

        quality = self.run_cli("quality", task_id, "--json")
        self.assertEqual(quality.returncode, 0, quality.stderr)
        self.assertEqual(json.loads(quality.stdout)["present_artifacts"], 2)

        events = self.run_cli("events", task_id, "--json")
        self.assertEqual(events.returncode, 0, events.stderr)
        event_types = [event["type"] for event in json.loads(events.stdout)]
        self.assertIn("task.completed", event_types)

    def test_cli_agent_card_is_json(self):
        result = self.run_cli("agent-card", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        card = json.loads(result.stdout)
        self.assertEqual(card["name"], "Across Orchestrator")
        self.assertTrue(card["capabilities"]["taskOrchestration"])

    def test_cli_plugin_manifest_is_json(self):
        result = self.run_cli("plugin-manifest", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual(manifest["id"], "across-orchestrator")
        self.assertEqual(manifest["kind"], "task-runtime")
        self.assertEqual(manifest["entrypoints"]["sidecar"]["command"], "across-orchestrator")
        self.assertEqual(manifest["paths"]["data"], "~/.across/data/across-orchestrator")

    def test_cli_submit_release_e2e_uses_app_grade_engine(self):
        submit = self.run_cli(
            "submit-release-e2e",
            "--project",
            str(self.project),
            "--run-label",
            "cli-test",
            "--json",
        )
        self.assertEqual(submit.returncode, 0, submit.stderr)
        task = json.loads(submit.stdout)
        self.assertEqual(task["contract"]["engine"], "app_grade_release_e2e")

        run = self.run_cli("run", task["task_id"], "--json")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["status"], "completed")

        evidence = self.run_cli("evidence", task["task_id"], "--json")
        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        payload = json.loads(evidence.stdout)
        self.assertEqual(payload["app_grade"]["scenario_id"], "cross_agent_full_delivery_v1")
        self.assertIn(payload["app_grade"]["delivery_quality"], {"passed", "partial"})


if __name__ == "__main__":
    unittest.main()
