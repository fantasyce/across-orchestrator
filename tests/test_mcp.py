import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def rpc(message_id, method, params=None):
    payload = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


class McpTests(unittest.TestCase):
    def test_mcp_submit_run_and_fetch_evidence(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(__file__).resolve().parents[1]
            project = Path(tempdir) / "project"
            home = Path(tempdir) / "home"
            project.mkdir()
            home.mkdir()
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")
            env["ACROSS_ORCHESTRATOR_HOME"] = str(home)
            messages = [
                rpc(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test"}}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/list"),
                rpc(3, "tools/call", {
                    "name": "submit_task",
                    "arguments": {
                        "goal": "Build MCP demo",
                        "projectRoot": str(project),
                        "deliverables": ["README.md", "web/index.html"],
                        "agent": "demo",
                    },
                }),
            ]
            process = subprocess.run(
                [sys.executable, "-m", "across_orchestrator.cli", "mcp"],
                cwd=root,
                env=env,
                input="\n".join(json.dumps(item) for item in messages) + "\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            responses = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
            self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "Across Orchestrator")
            tool_names = [tool["name"] for tool in responses[1]["result"]["tools"]]
            self.assertIn("submit_task", tool_names)
            submit_text = responses[2]["result"]["content"][0]["text"]
            task_id = json.loads(submit_text)["task_id"]

            run_messages = [
                rpc(1, "initialize", {}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {"name": "run_task", "arguments": {"taskId": task_id}}),
                rpc(3, "tools/call", {"name": "get_evidence_bundle", "arguments": {"taskId": task_id}}),
            ]
            process2 = subprocess.run(
                [sys.executable, "-m", "across_orchestrator.cli", "mcp"],
                cwd=root,
                env=env,
                input="\n".join(json.dumps(item) for item in run_messages) + "\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertEqual(process2.returncode, 0, process2.stderr)
            second = [json.loads(line) for line in process2.stdout.splitlines() if line.strip()]
            self.assertEqual(json.loads(second[1]["result"]["content"][0]["text"])["status"], "completed")
            evidence = json.loads(second[2]["result"]["content"][0]["text"])
            self.assertEqual(evidence["quality"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
