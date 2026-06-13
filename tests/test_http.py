import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib import request


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(__file__).resolve().parents[1]
        self.project = Path(self.tempdir.name) / "project"
        self.home = Path(self.tempdir.name) / "home"
        self.project.mkdir()
        self.home.mkdir()
        self.port = free_port()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.root / "src")
        env["ACROSS_ORCHESTRATOR_HOME"] = str(self.home)
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "across_orchestrator.cli",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=self.root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.base = f"http://127.0.0.1:{self.port}"
        self.wait_for_health()

    def tearDown(self):
        self.process.terminate()
        try:
            self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate(timeout=5)
        self.tempdir.cleanup()

    def wait_for_health(self):
        deadline = time.time() + 10
        last_error = None
        while time.time() < deadline:
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate(timeout=1)
                raise AssertionError(f"server exited early\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
            try:
                payload = self.get("/health")
                if payload["status"] == "ok":
                    return
            except Exception as exc:
                last_error = exc
            time.sleep(0.1)
        raise AssertionError(f"server did not start: {last_error}")

    def get(self, path):
        with request.urlopen(self.base + path, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_http_submit_run_and_fetch_evidence(self):
        plugin_manifest = self.get("/.well-known/across-plugin.json")
        self.assertEqual(plugin_manifest["id"], "across-orchestrator")
        self.assertEqual(plugin_manifest["entrypoints"]["sidecar"]["healthPath"], "/health")
        self.assertTrue(plugin_manifest["capabilities"]["agentLoopRuntime"])
        self.assertEqual(plugin_manifest["protocols"]["http"]["hostConformance"], "POST /host-conformance")

        card = self.get("/.well-known/agent-card.json")
        self.assertEqual(card["name"], "Across Orchestrator")

        task = self.post(
            "/tasks",
            {
                "goal": "Build docs",
                "projectRoot": str(self.project),
                "deliverables": ["README.md", "docs/usage.md"],
                "agent": "demo",
            },
        )
        task_id = task["task_id"]

        completed = self.post(f"/tasks/{task_id}/run", {})
        self.assertEqual(completed["status"], "completed")

        status = self.get(f"/tasks/{task_id}")
        self.assertEqual(status["status"], "completed")

        evidence = self.get(f"/tasks/{task_id}/evidence-bundle")
        self.assertEqual(evidence["quality"]["status"], "passed")

        quality = self.get(f"/tasks/{task_id}/quality-benchmark")
        self.assertEqual(quality["present_artifacts"], 2)

        events = self.get(f"/tasks/{task_id}/events")
        self.assertIn("task.completed", [event["type"] for event in events])

        stream = request.urlopen(self.base + f"/tasks/{task_id}/events/stream", timeout=5)
        body = stream.read().decode("utf-8")
        self.assertIn("event: task.completed", body)

    def test_http_agent_loop_lifecycle(self):
        loop = self.post(
            "/loops",
            {
                "goal": "Run platform loop over hosted agents",
                "projectRoot": str(self.project),
                "agent": "owner",
                "maxTurns": 8,
            },
        )
        loop_id = loop["loop_id"]
        self.assertTrue(loop_id.startswith("loop-"))
        self.assertEqual(loop["status"], "pending")

        completed = self.post(f"/loops/{loop_id}/run", {})
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            [step["action"]["type"] for step in completed["steps"]],
            ["memory_search", "task_dispatch", "quality_gate", "memory_write_candidate", "final_output"],
        )

        status = self.get(f"/loops/{loop_id}")
        self.assertEqual(status["final_output"], "Agent loop completed for: Run platform loop over hosted agents")

        events = self.get(f"/loops/{loop_id}/events")
        self.assertIn("loop.completed", [event["type"] for event in events])

    def test_http_host_conformance_validates_external_host_contract(self):
        report = self.post(
            "/host-conformance",
            {
                "platform_id": "generic-agent-host",
                "agents": [
                    {
                        "agent_id": "planner",
                        "display_name": "Planner",
                        "endpoint": "http://127.0.0.1:9910/agents/planner",
                        "protocols": ["http", "mcp"],
                        "capabilities": ["planning"],
                        "tenant_id": "tenant-a",
                    }
                ],
                "memory_provider": "across-context",
                "credentials_provider": "host-keychain",
                "permissions_provider": "host-policy",
                "project_context": {
                    "project_id": "project-a",
                    "workspace_root": "~/.across/workspaces/project-a",
                },
            },
        )
        self.assertTrue(report["passed"])
        self.assertEqual(report["host"]["platformId"], "generic-agent-host")
        self.assertEqual(report["missingHostProvides"], [])

    def test_http_agent_loop_approval_lifecycle(self):
        loop = self.post(
            "/loops",
            {
                "goal": "Run approval-gated platform loop",
                "projectRoot": str(self.project),
                "agent": "owner",
                "maxTurns": 8,
                "approvalPolicy": {"requireApprovalFor": ["task_dispatch"]},
            },
        )
        waiting = self.post(f"/loops/{loop['loop_id']}/run", {})
        self.assertEqual(waiting["status"], "awaiting_approval")
        action_id = waiting["steps"][-1]["action"]["action_id"]

        approved = self.post(f"/loops/{loop['loop_id']}/actions/{action_id}/approve", {})
        self.assertEqual(approved["steps"][-1]["action"]["approval_status"], "approved")

        completed = self.post(f"/loops/{loop['loop_id']}/run", {})
        self.assertEqual(completed["status"], "completed")

    def test_http_submit_release_e2e(self):
        task = self.post(
            "/release-e2e",
            {
                "projectRoot": str(self.project),
                "runLabel": "http-test",
            },
        )
        self.assertEqual(task["contract"]["engine"], "app_grade_release_e2e")

        completed = self.post(f"/tasks/{task['task_id']}/run", {})
        self.assertEqual(completed["status"], "completed")

        evidence = self.get(f"/tasks/{task['task_id']}/evidence-bundle")
        self.assertEqual(evidence["app_grade"]["scenario_id"], "host_agent_full_delivery_v1")
        self.assertIn(evidence["app_grade"]["delivery_quality"], {"passed", "partial"})

    def test_sidecar_runtime_info_is_written_under_across_run_home(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(__file__).resolve().parents[1]
            port = free_port()
            across_home = Path(tempdir) / "across"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")
            env["ACROSS_HOME"] = str(across_home)
            env.pop("ACROSS_ORCHESTRATOR_HOME", None)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "across_orchestrator.cli",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--runtime-id",
                    "unit-host",
                ],
                cwd=root,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                base = f"http://127.0.0.1:{port}"
                deadline = time.time() + 10
                while time.time() < deadline:
                    try:
                        with request.urlopen(base + "/health", timeout=1) as response:
                            if json.loads(response.read().decode("utf-8"))["status"] == "ok":
                                break
                    except Exception:
                        time.sleep(0.1)
                else:
                    self.fail("sidecar did not become healthy")

                runtime_info = across_home / "run" / "across-orchestrator" / "unit-host.json"
                payload = json.loads(runtime_info.read_text(encoding="utf-8"))
                self.assertEqual(payload["componentId"], "across-orchestrator")
                self.assertEqual(payload["runtimeId"], "unit-host")
                self.assertEqual(payload["endpoint"], base)
                self.assertEqual(payload["transport"], "http")
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
