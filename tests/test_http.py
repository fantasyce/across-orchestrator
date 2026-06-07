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


if __name__ == "__main__":
    unittest.main()
