import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class AppGradeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.project = Path(self.tempdir.name) / "release-project"
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

    def test_release_e2e_task_runs_mature_quality_path(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        runtime = OrchestratorRuntime()
        task = runtime.submit_release_e2e_task(
            project_root=str(self.project),
            run_label="runtime-test",
        )

        self.assertEqual(task.contract["engine"], "app_grade_release_e2e")
        self.assertEqual(task.contract["requiredArtifacts"], [
            "README.md",
            "web/index.html",
            "web/styles.css",
            "web/app.js",
            "api/server.mjs",
            "cli/quality-check.mjs",
            "tests/e2e-smoke.mjs",
        ])
        self.assertGreaterEqual(len(task.subtasks), 7)

        completed = runtime.run_task(task.task_id)
        self.assertEqual(completed.status, "completed")

        files = sorted(
            path.relative_to(self.project).as_posix()
            for path in self.project.rglob("*")
            if path.is_file()
        )
        self.assertEqual(files, sorted(task.contract["requiredArtifacts"]))

        smoke = subprocess.run(
            ["node", "tests/e2e-smoke.mjs"],
            cwd=self.project,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(smoke.returncode, 0, smoke.stdout)

        cli = subprocess.run(
            ["node", "cli/quality-check.mjs"],
            cwd=self.project,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(cli.returncode, 0, cli.stdout)
        self.assertTrue(json.loads(cli.stdout)["passed"])

        evidence = runtime.evidence_bundle(task.task_id)
        app_grade = evidence["app_grade"]
        self.assertEqual(app_grade["scenario_id"], "cross_agent_full_delivery_v1")
        self.assertIn(app_grade["delivery_quality"], {"passed", "partial"})
        self.assertEqual(app_grade["quality_report"]["task_id"], task.task_id)
        gate_ids = {
            gate["adapter_id"]: gate["status"]
            for gate in app_grade["quality_report"]["gate_results"]
        }
        self.assertEqual(gate_ids["static_web_smoke"], "passed")
        self.assertEqual(gate_ids["api_service"], "passed")
        self.assertEqual(gate_ids["cli_generic"], "passed")
        self.assertIn("browser_e2e", gate_ids)
        self.assertEqual(gate_ids["agent_mix"], "passed")
        if _node_playwright_available():
            self.assertEqual(gate_ids["browser_e2e"], "passed")
            self.assertEqual(app_grade["delivery_quality"], "passed")
        else:
            self.assertIn(gate_ids["browser_e2e"], {"partial", "failed", "skipped"})

    def test_release_e2e_repairs_dirty_workspace_to_exact_manifest(self):
        from across_orchestrator.runtime import OrchestratorRuntime

        (self.project / "unexpected.log").write_text("noise", encoding="utf-8")
        (self.project / "web").mkdir()
        (self.project / "web" / "index.html").write_text("", encoding="utf-8")

        runtime = OrchestratorRuntime()
        task = runtime.submit_release_e2e_task(
            project_root=str(self.project),
            run_label="dirty-workspace",
        )
        runtime.run_task(task.task_id)

        evidence = runtime.evidence_bundle(task.task_id)
        self.assertEqual(evidence["app_grade"]["exact_files"], sorted(task.contract["requiredArtifacts"]))
        self.assertNotIn("unexpected.log", evidence["app_grade"]["exact_files"])
        self.assertEqual(evidence["app_grade"]["quality_report"]["required_failed_count"], 0)


def _node_playwright_available() -> bool:
    probe = subprocess.run(
        [
            "node",
            "-e",
            (
                "const { chromium } = require('playwright');"
                "(async () => { const browser = await chromium.launch({ headless: true });"
                "await browser.close(); })().catch(() => process.exit(1));"
            ),
        ],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return probe.returncode == 0
