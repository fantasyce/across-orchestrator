import json
import os
import subprocess
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
        allowed_agents = ["openclaw", "hermes", "claude", "deepseek", "minimax"]
        task = runtime.submit_release_e2e_task(
            project_root=str(self.project),
            run_label="runtime-test",
            allowed_agents=allowed_agents,
        )

        self.assertEqual(task.contract["engine"], "app_grade_release_e2e")
        self.assertIn(task.agent, allowed_agents)
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
        self.assertEqual([subtask.wave for subtask in task.subtasks], [1, 2, 3, 4, 5, 6, 7])
        self.assertTrue(task.subtasks[1].dependencies)
        self.assertTrue(all(subtask.agent in allowed_agents for subtask in task.subtasks))
        self.assertTrue(all(not subtask.agent.endswith("-agent") for subtask in task.subtasks))
        self.assertEqual(
            [subtask.capability_role for subtask in task.subtasks],
            ["api", "html", "style", "client", "quality", "smoke", "docs"],
        )

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
        self.assertEqual(app_grade["scenario_id"], "host_agent_full_delivery_v1")
        self.assertTrue(all(item["agent_id"] in allowed_agents for item in app_grade["subtasks"]))
        self.assertEqual(
            [item["capability_role"] for item in app_grade["subtasks"]],
            ["api", "html", "style", "client", "quality", "smoke", "docs"],
        )
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
        if gate_ids["browser_e2e"] == "passed":
            self.assertIn(
                _browser_gate_mode(app_grade),
                {"playwright", "node-dom-shim"},
            )
            self.assertEqual(app_grade["delivery_quality"], "passed")
        else:
            self.assertFalse(_node_playwright_available())
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

    def test_app_grade_payload_rejects_role_names_as_executor_agents(self):
        from across_orchestrator.app_grade import build_release_e2e_payload

        payload = build_release_e2e_payload(
            task_id="task-role-filter",
            project_root=str(self.project),
            allowed_agents=["api-agent", "deepseek"],
        )

        self.assertEqual(payload["request"]["executor_agents"], ["deepseek"])
        self.assertTrue(all(item["agent"] == "deepseek" for item in payload["subtasks"]))
        self.assertEqual(
            [item["capability_role"] for item in payload["subtasks"]],
            ["api", "html", "style", "client", "quality", "smoke", "docs"],
        )

    def test_legacy_app_grade_role_agents_are_migrated_on_load(self):
        from across_orchestrator.models import Task

        task = Task.from_dict({
            "task_id": "task-legacy-role-agents",
            "goal": "legacy app-grade task",
            "project_root": str(self.project),
            "agent": "app-grade",
            "contract": {"engine": "app_grade_release_e2e"},
            "subtasks": [
                {"subtask_id": "subtask-api", "goal": "api", "path": "api/server.mjs", "agent": "api-agent"},
                {"subtask_id": "subtask-html", "goal": "html", "path": "web/index.html", "agent": "html-agent"},
                {"subtask_id": "subtask-style", "goal": "style", "path": "web/styles.css", "agent": "style-agent"},
            ],
        })

        self.assertEqual(task.agent, "openclaw")
        self.assertEqual([subtask.agent for subtask in task.subtasks], ["openclaw", "hermes", "claude"])
        self.assertEqual([subtask.capability_role for subtask in task.subtasks], ["api", "html", "style"])

    def test_browser_gate_has_self_contained_dom_shim_fallback(self):
        from across_orchestrator.app_grade import _browserless_dom_gate, write_release_e2e_reference_artifact

        write_release_e2e_reference_artifact(str(self.project))

        gate = _browserless_dom_gate(self.project)

        self.assertEqual(gate["adapter_id"], "browser_e2e")
        self.assertEqual(gate["status"], "passed")
        self.assertEqual(gate["evidence"]["mode"], "node-dom-shim")


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


def _browser_gate_mode(app_grade: dict) -> str:
    for gate in app_grade["quality_report"]["gate_results"]:
        if gate["adapter_id"] == "browser_e2e":
            return str(gate.get("evidence", {}).get("mode") or "")
    return ""
