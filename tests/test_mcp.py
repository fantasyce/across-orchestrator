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
            agent_script = project / "mcp_agent_adapter.py"
            agent_script.write_text(
                "\n".join(
                    [
                        "import json",
                        "import os",
                        "from pathlib import Path",
                        "subtask = json.loads(os.environ['ACROSS_SUBTASK_JSON'])",
                        "target = Path(subtask['path'])",
                        "target.parent.mkdir(parents=True, exist_ok=True)",
                        "target.write_text(f\"mcp-adapter={subtask['agent']}\\n\", encoding='utf-8')",
                        "print(json.dumps({'agent': subtask['agent'], 'path': subtask['path']}))",
                    ]
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")
            env["ACROSS_ORCHESTRATOR_HOME"] = str(home)
            messages = [
                rpc(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test"}}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/list"),
                rpc(3, "resources/list"),
                rpc(4, "resources/read", {"uri": "across-orchestrator://plugin-manifest"}),
                rpc(5, "tools/call", {
                    "name": "submit_task",
                    "arguments": {
                        "goal": "Build MCP demo with declared custom agent adapter",
                        "projectRoot": str(project),
                        "deliverables": ["mcp/custom.txt"],
                        "agent": "mcp-custom-agent",
                        "agentAdapters": {
                            "mcp-custom-agent": {
                                "type": "command",
                                "command": [sys.executable, str(agent_script)],
                            }
                        },
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
            self.assertIn("resources", responses[0]["result"]["capabilities"])
            tool_names = [tool["name"] for tool in responses[1]["result"]["tools"]]
            self.assertIn("submit_task", tool_names)
            self.assertIn("start_agent_loop", tool_names)
            self.assertIn("approve_agent_loop_action", tool_names)
            self.assertIn("cancel_agent_loop", tool_names)
            self.assertIn("reject_agent_loop_action", tool_names)
            self.assertIn("retry_agent_loop_step", tool_names)
            self.assertIn("get_agent_loop_health", tool_names)
            submit_tool = next(tool for tool in responses[1]["result"]["tools"] if tool["name"] == "submit_task")
            submit_properties = submit_tool["inputSchema"]["properties"]
            self.assertIn("agentAdapters", submit_properties)
            self.assertIn("agent_adapters", submit_properties)
            resource_uris = [resource["uri"] for resource in responses[2]["result"]["resources"]]
            self.assertIn("across-orchestrator://plugin-manifest", resource_uris)
            self.assertIn("across-orchestrator://agent-loop-schema", resource_uris)
            manifest = json.loads(responses[3]["result"]["contents"][0]["text"])
            self.assertEqual(manifest["id"], "across-orchestrator")
            self.assertTrue(manifest["capabilities"]["agentLoopV2"])
            submit_text = responses[4]["result"]["content"][0]["text"]
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
            self.assertEqual((project / "mcp/custom.txt").read_text(encoding="utf-8"), "mcp-adapter=mcp-custom-agent\n")

    def test_agent_loop_schema_declares_cancelled_terminal_status(self):
        from across_orchestrator.mcp import agent_loop_schema

        schema = agent_loop_schema()

        self.assertIn("cancelled", schema["status"])
        self.assertIn("cancel_agent_loop", schema["controlActions"])

    def test_agent_loop_schema_documents_execution_lease_and_routing_contract(self):
        from across_orchestrator.mcp import agent_loop_schema

        schema = agent_loop_schema()

        self.assertIn("loop.step.heartbeat", schema["events"])
        self.assertIn("loop.step.lease_expired", schema["events"])
        self.assertIn("loop.cancel_requested", schema["events"])
        self.assertIn("loop.dispatch.detached", schema["events"])
        self.assertIn("loop.step.cancelled", schema["events"])
        self.assertIn("loop.step.recovery_decision", schema["events"])
        self.assertIn("loop.step.recovered", schema["events"])
        self.assertIn("get_agent_loop_health", schema["inspectionActions"])
        self.assertIn("healthSummary", schema)
        self.assertIn("recoveryPolicy", schema)
        self.assertIn("eventMetadata", schema)
        self.assertIn("event_id", schema["eventMetadata"]["fields"])
        self.assertIn("sequence", schema["eventMetadata"]["fields"])
        self.assertIn("correlation_id", schema["eventMetadata"]["fields"])
        self.assertEqual(
            schema["cancelCategories"],
            ["user_cancelled", "shutdown", "superseded", "timeout_cancelled"],
        )
        self.assertIn("cancellation_category", schema["healthSummary"]["fields"])
        self.assertIn("require_human", schema["recoveryPolicy"]["supportedActions"])
        self.assertIn("host_release_evidence", schema["evidenceSummary"]["fields"])
        self.assertEqual(schema["evidenceSummary"]["hostReleaseEvidence"]["readiness"], ["ready", "attention", "blocked"])
        self.assertEqual(schema["memoryPolicy"]["candidateSchema"], "agent-loop-memory-candidate/1.0")
        self.assertIn("failure_types", schema["memoryPolicy"]["candidateFields"])
        execution = schema["checkpoint"]["execution"]
        self.assertEqual(
            execution["fields"],
            [
                "lease_id",
                "started_at",
                "heartbeat_at",
                "lease_seconds",
                "lease_expires_at",
                "renewal_count",
                "completed_at",
                "duration_ms",
            ],
        )
        self.assertIn("actionLeaseSeconds", schema["metadata"])
        self.assertIn("agentRouting", schema["metadata"])
        self.assertEqual(schema["context"]["heartbeat"], "callable lease renewal hook for long-running dispatch adapters")
        self.assertIn("raise_if_cancelled", schema["context"]["cancellation"])
        self.assertEqual(
            schema["failureTypes"],
            [
                "adapter_error",
                "approval_rejected",
                "environment_blocked",
                "lease_expired",
                "max_turns_exceeded",
                "quality_failed",
                "timeout",
            ],
        )

    def test_mcp_agent_loop_tools(self):
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
                rpc(1, "initialize", {}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {
                    "name": "start_agent_loop",
                    "arguments": {
                        "goal": "MCP loop scenario",
                        "projectRoot": str(project),
                        "maxTurns": 8,
                        "metadata": {"scenario": "mcp-loop"},
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
            loop = json.loads(responses[1]["result"]["content"][0]["text"])
            self.assertTrue(loop["loop_id"].startswith("loop-"))
            self.assertEqual(loop["metadata"]["scenario"], "mcp-loop")

            run_messages = [
                rpc(1, "initialize", {}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {"name": "run_agent_loop", "arguments": {"loopId": loop["loop_id"]}}),
                rpc(3, "tools/call", {"name": "get_agent_loop_events", "arguments": {"loopId": loop["loop_id"]}}),
                rpc(4, "tools/call", {"name": "get_agent_loop_health", "arguments": {"loopId": loop["loop_id"]}}),
                rpc(5, "tools/call", {"name": "get_agent_loop_evidence_summary", "arguments": {"loopId": loop["loop_id"]}}),
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
            completed = json.loads(second[1]["result"]["content"][0]["text"])
            self.assertEqual(completed["status"], "completed")
            events = json.loads(second[2]["result"]["content"][0]["text"])
            self.assertIn("loop.completed", [event["type"] for event in events])
            health = json.loads(second[3]["result"]["content"][0]["text"])
            self.assertEqual(health["status"], "completed")
            self.assertEqual(health["loop_id"], loop["loop_id"])
            summary = json.loads(second[4]["result"]["content"][0]["text"])
            self.assertEqual(summary["schema_version"], "0.1")
            self.assertEqual(summary["status"], "completed")
            self.assertTrue(summary["event_audit"]["sequence_contiguous"])

    def test_mcp_agent_loop_reports_invalid_action_plan(self):
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
                rpc(1, "initialize", {}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {
                    "name": "start_agent_loop",
                    "arguments": {
                        "goal": "MCP invalid action plan",
                        "projectRoot": str(project),
                        "metadata": {"actionPlan": ["task_dispatch", "unsafe_shell_action"]},
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
            self.assertIn("error", responses[1])
            self.assertIn("unsupported actionPlan entries", responses[1]["error"]["message"])

    def test_mcp_agent_loop_approval_tool(self):
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
                rpc(1, "initialize", {}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {
                    "name": "start_agent_loop",
                    "arguments": {
                        "goal": "MCP approval loop",
                        "projectRoot": str(project),
                        "approvalPolicy": {"requireApprovalFor": ["task_dispatch"]},
                        "maxTurns": 8,
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
            loop = json.loads([json.loads(line) for line in process.stdout.splitlines() if line.strip()][1]["result"]["content"][0]["text"])

            run_messages = [
                rpc(1, "initialize", {}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {"name": "run_agent_loop", "arguments": {"loopId": loop["loop_id"]}}),
            ]
            waiting_process = subprocess.run(
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
            self.assertEqual(waiting_process.returncode, 0, waiting_process.stderr)
            waiting = json.loads([json.loads(line) for line in waiting_process.stdout.splitlines() if line.strip()][1]["result"]["content"][0]["text"])
            self.assertEqual(waiting["status"], "awaiting_approval")
            action_id = waiting["steps"][-1]["action"]["action_id"]

            approve_messages = [
                rpc(1, "initialize", {}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {
                    "name": "approve_agent_loop_action",
                    "arguments": {"loopId": loop["loop_id"], "actionId": action_id},
                }),
            ]
            approved_process = subprocess.run(
                [sys.executable, "-m", "across_orchestrator.cli", "mcp"],
                cwd=root,
                env=env,
                input="\n".join(json.dumps(item) for item in approve_messages) + "\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertEqual(approved_process.returncode, 0, approved_process.stderr)
            approved = json.loads([json.loads(line) for line in approved_process.stdout.splitlines() if line.strip()][1]["result"]["content"][0]["text"])
            self.assertEqual(approved["steps"][-1]["action"]["approval_status"], "approved")

    def test_mcp_agent_loop_control_tools(self):
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
                rpc(1, "initialize", {}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {
                    "name": "start_agent_loop",
                    "arguments": {
                        "goal": "MCP cancel loop",
                        "projectRoot": str(project),
                        "maxTurns": 8,
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
            loop = json.loads([json.loads(line) for line in process.stdout.splitlines() if line.strip()][1]["result"]["content"][0]["text"])

            cancel_messages = [
                rpc(1, "initialize", {}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {
                    "name": "cancel_agent_loop",
                    "arguments": {
                        "loopId": loop["loop_id"],
                        "reason": "mcp user cancelled",
                        "cancelCategory": "user_cancelled",
                    },
                }),
                rpc(3, "tools/call", {"name": "get_agent_loop_health", "arguments": {"loopId": loop["loop_id"]}}),
                rpc(4, "tools/call", {"name": "get_agent_loop_events", "arguments": {"loopId": loop["loop_id"]}}),
            ]
            cancelled_process = subprocess.run(
                [sys.executable, "-m", "across_orchestrator.cli", "mcp"],
                cwd=root,
                env=env,
                input="\n".join(json.dumps(item) for item in cancel_messages) + "\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            self.assertEqual(cancelled_process.returncode, 0, cancelled_process.stderr)
            cancelled = json.loads([json.loads(line) for line in cancelled_process.stdout.splitlines() if line.strip()][1]["result"]["content"][0]["text"])
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertEqual(cancelled["error"], "mcp user cancelled")
            responses = [json.loads(line) for line in cancelled_process.stdout.splitlines() if line.strip()]
            health = json.loads(responses[2]["result"]["content"][0]["text"])
            events = json.loads(responses[3]["result"]["content"][0]["text"])
            self.assertEqual(health["cancellation_category"], "user_cancelled")
            self.assertEqual(
                next(event for event in events if event["type"] == "loop.cancel_requested")["payload"]["cancel_category"],
                "user_cancelled",
            )

    def test_mcp_submit_release_e2e_task(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(__file__).resolve().parents[1]
            project = Path(tempdir) / "release-project"
            home = Path(tempdir) / "home"
            project.mkdir()
            home.mkdir()
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")
            env["ACROSS_ORCHESTRATOR_HOME"] = str(home)
            messages = [
                rpc(1, "initialize", {}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {
                    "name": "submit_release_e2e_task",
                    "arguments": {
                        "projectRoot": str(project),
                        "runLabel": "mcp-test",
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
            task = json.loads(responses[1]["result"]["content"][0]["text"])
            self.assertEqual(task["contract"]["engine"], "app_grade_release_e2e")


if __name__ == "__main__":
    unittest.main()
