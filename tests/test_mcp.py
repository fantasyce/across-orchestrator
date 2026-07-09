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


def agent_plugin_manifest():
    return {
        "schema_version": "across-agent-plugin/1.0",
        "plugin_id": "demo.echo-agent",
        "display_name": "Demo Echo Agent",
        "version": "1.0.0",
        "agent": {"id": "demo-echo", "name": "Demo Echo", "vendor": "tests"},
        "capabilities": [{"id": "echo", "risk": "low"}],
        "entrypoints": {"run": {"command": [sys.executable, "-c", "print('ok')"]}},
        "trust": {"mutation_boundary": "read_only", "secrets_included": False},
        "context": {"pack_id": "demo.echo-agent", "tags": ["demo"]},
    }


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
            self.assertIn("evaluate_sandbox_policy", tool_names)
            self.assertIn("build_evidence_graph", tool_names)
            self.assertIn("evaluate_agent_team_readiness", tool_names)
            self.assertIn("render_remote_mcp_oauth_template", tool_names)
            self.assertIn("create_a2a_task_delegation", tool_names)
            self.assertIn("project_agui_events", tool_names)
            self.assertIn("create_agent_team", tool_names)
            self.assertIn("export_otel_genai_spans", tool_names)
            self.assertIn("validate_external_agent_plugin", tool_names)
            self.assertIn("register_external_agent_plugin", tool_names)
            self.assertIn("list_external_agent_plugins", tool_names)
            self.assertIn("get_external_agent_plugin_health", tool_names)
            submit_tool = next(tool for tool in responses[1]["result"]["tools"] if tool["name"] == "submit_task")
            submit_properties = submit_tool["inputSchema"]["properties"]
            self.assertIn("agentAdapters", submit_properties)
            self.assertIn("agent_adapters", submit_properties)
            resource_uris = [resource["uri"] for resource in responses[2]["result"]["resources"]]
            self.assertIn("across-orchestrator://plugin-manifest", resource_uris)
            self.assertIn("across-orchestrator://agent-loop-schema", resource_uris)
            self.assertIn("across-orchestrator://sandbox-policy", resource_uris)
            self.assertIn("across-orchestrator://external-agent-plugins", resource_uris)
            self.assertIn("across-orchestrator://projection-contracts", resource_uris)
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

    def test_mcp_evaluates_agent_team_readiness(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(__file__).resolve().parents[1]
            home = Path(tempdir) / "home"
            home.mkdir()
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")
            env["ACROSS_ORCHESTRATOR_HOME"] = str(home)
            payload = {
                "pack_id": "plugin-compatibility-lab-v2",
                "host_targets": ["codex", "claude_code", "mcp", "a2a", "across"],
                "runtime_policy": {"promotion": {"human_approval_required": True}},
                "trust_boundary": {"secrets": "not_allowed"},
                "product_card": {
                    "schema_version": "across-workflow-pack-product-card/1.0",
                    "user_problem": "Need a plugin adoption gate.",
                    "job_to_be_done": "Evaluate a plugin before use.",
                    "quickstart": {"cli": "across-autopilot loop run --spec plugin-compatibility-lab-v2 --json"},
                    "market_readiness": {"first_value_artifact": "run://plugin-compatibility-lab/report.md"},
                },
                "protocol_readiness": {
                    "schema_version": "across-workflow-pack-protocol-readiness/1.0",
                    "summary": {"honest_protocol_claims": True},
                    "checks": [{"id": "remote_mcp_http_oauth", "status": "planned"}],
                },
                "trust_receipt": {
                    "schema_version": "across-agent-team-trust-receipt/1.0",
                    "evidence_contract": {
                        "required": ["runtime_policy", "trust_boundary", "host_exports", "evidence_graph", "validation_gates"]
                    },
                },
                "frontier_interop": {
                    "schema_version": "across-workflow-pack-frontier-interop/1.0",
                    "remote_mcp": {"schema_version": "across-remote-mcp-oauth-template/1.0", "oauth_required": True},
                    "a2a": {"schema_version": "across-a2a-task-delegation/2.0"},
                    "observability": {"otel_schema": "across-otel-genai-export/1.0", "otlp_trace_schema": "otlp-traces-json/1.0", "raw_transcripts_included": False},
                    "projections": {
                        "mcp_tasks": {"status": "projection_only"},
                        "a2a": {"status": "passed"},
                        "ag_ui": {"status": "passed"},
                        "remote_mcp_oauth": {"status": "passed"},
                        "otel": {"status": "passed"},
                    },
                },
            }
            messages = [
                rpc(1, "initialize", {}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {"name": "evaluate_agent_team_readiness", "arguments": {"payload": payload}}),
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
            report = json.loads(responses[1]["result"]["content"][0]["text"])
            self.assertEqual(report["schema_version"], "across-agent-team-readiness/1.0")
            self.assertEqual(report["status"], "passed")

    def test_mcp_exposes_frontier_interop_tools(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(__file__).resolve().parents[1]
            home = Path(tempdir) / "home"
            home.mkdir()
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")
            env["ACROSS_ORCHESTRATOR_HOME"] = str(home)
            evidence = {
                "run_id": "run-1",
                "spec_id": "plugin-compatibility-lab-v2",
                "status": "completed",
                "gates": [{"id": "workflow_pack_exports_ready", "status": "passed"}],
            }
            messages = [
                rpc(1, "initialize", {}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {
                    "name": "render_remote_mcp_oauth_template",
                    "arguments": {"config": {"base_url": "https://example.test/mcp", "issuer": "https://issuer.example.test"}},
                }),
                rpc(3, "tools/call", {
                    "name": "create_a2a_task_delegation",
                    "arguments": {"payload": {"goal": "Validate plugin portability", "pack_id": "plugin-compatibility-lab-v2"}},
                }),
                rpc(4, "tools/call", {
                    "name": "export_otel_genai_spans",
                    "arguments": {"payload": evidence},
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
            remote = json.loads(responses[1]["result"]["content"][0]["text"])
            delegated = json.loads(responses[2]["result"]["content"][0]["text"])
            otel = json.loads(responses[3]["result"]["content"][0]["text"])
            self.assertEqual(remote["schema_version"], "across-remote-mcp-oauth-template/1.0")
            self.assertEqual(delegated["schema_version"], "across-a2a-task-delegation/2.0")
            self.assertEqual(delegated["jsonrpc"]["method"], "tasks/send")
            self.assertEqual(otel["schema_version"], "across-otel-genai-export/1.0")
            self.assertEqual(otel["summary"]["eval_case_count"], 1)

    def test_mcp_exposes_agui_projection_and_agent_team_tools(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(__file__).resolve().parents[1]
            home = Path(tempdir) / "home"
            home.mkdir()
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")
            env["ACROSS_ORCHESTRATOR_HOME"] = str(home)
            messages = [
                rpc(1, "initialize", {}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {
                    "name": "project_agui_events",
                    "arguments": {
                        "payload": {
                            "source": "loop",
                            "loop_id": "loop-1",
                            "events": [{"type": "loop.completed", "sequence": 1, "payload": {"status": "completed"}}],
                        }
                    },
                }),
                rpc(3, "tools/call", {
                    "name": "create_agent_team",
                    "arguments": {
                        "payload": {
                            "owner": "owner",
                            "agents": [{"id": "owner"}, {"id": "review-agent", "role": "review"}],
                            "context": {"notes": ["Use NOTES.md handoff only."]},
                        }
                    },
                }),
                rpc(4, "resources/read", {"uri": "across-orchestrator://projection-contracts"}),
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
            agui = json.loads(responses[1]["result"]["content"][0]["text"])
            team = json.loads(responses[2]["result"]["content"][0]["text"])
            contracts = json.loads(responses[3]["result"]["contents"][0]["text"])
            self.assertEqual(agui["schema_version"], "across-agui-projection/1.0")
            self.assertEqual(agui["events"][0]["type"], "task.completed")
            self.assertFalse(agui["summary"]["secrets_included"])
            self.assertEqual(team["schema_version"], "across-agent-team/1.0")
            self.assertEqual(len(team["agents"]), 2)
            self.assertTrue(team["checkpoint_policy"]["independent_session"])
            self.assertEqual(contracts["schema_version"], "across-external-projection/1.0")
            self.assertEqual(contracts["projections"]["ag_ui"]["schema_version"], "across-agui-projection/1.0")

    def test_mcp_exposes_external_agent_plugin_contract(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(__file__).resolve().parents[1]
            home = Path(tempdir) / "home"
            home.mkdir()
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")
            env["ACROSS_ORCHESTRATOR_HOME"] = str(home)
            messages = [
                rpc(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test"}}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                rpc(2, "tools/call", {
                    "name": "validate_external_agent_plugin",
                    "arguments": {"manifest": agent_plugin_manifest()},
                }),
                rpc(3, "tools/call", {
                    "name": "register_external_agent_plugin",
                    "arguments": {"manifest": agent_plugin_manifest()},
                }),
                rpc(4, "tools/call", {"name": "list_external_agent_plugins", "arguments": {}}),
                rpc(5, "tools/call", {"name": "get_external_agent_plugin_health", "arguments": {"agentId": "demo.echo-agent"}}),
                rpc(6, "resources/read", {"uri": "across-orchestrator://external-agent-plugins"}),
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
            normalized = json.loads(responses[1]["result"]["content"][0]["text"])
            self.assertEqual(normalized["schema_version"], "across-agent-plugin/1.0")
            self.assertEqual(normalized["plugin_id"], "demo.echo-agent")
            self.assertEqual(normalized["agent"]["id"], "demo-echo")
            registered = json.loads(responses[2]["result"]["content"][0]["text"])
            self.assertEqual(registered["summary"]["agent_count"], 1)
            self.assertEqual(registered["agents"][0]["plugin_id"], "demo.echo-agent")
            registry = json.loads(responses[3]["result"]["content"][0]["text"])
            self.assertEqual(registry["schema_version"], "across-orchestrator-external-agents/1.0")
            self.assertEqual(registry["summary"]["generic_schema"], "across-agent-plugin/1.0")
            self.assertEqual(registry["summary"]["plugin_count"], 1)
            health = json.loads(responses[4]["result"]["content"][0]["text"])
            self.assertEqual(health["schema_version"], "across-orchestrator-external-agent-health/1.0")
            self.assertEqual(health["summary"]["agent_count"], 1)
            resource = json.loads(responses[5]["result"]["contents"][0]["text"])
            self.assertEqual(resource["schema_version"], "across-orchestrator-external-agents/1.0")
            self.assertEqual(resource["summary"]["plugin_count"], 1)

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
        self.assertIn("loop.budget.exceeded", schema["events"])
        self.assertIn("loop.step.cancelled", schema["events"])
        self.assertIn("loop.step.recovery_decision", schema["events"])
        self.assertIn("loop.step.recovered", schema["events"])
        self.assertIn("get_agent_loop_health", schema["inspectionActions"])
        self.assertIn("get_agent_loop_telemetry", schema["inspectionActions"])
        self.assertIn("healthSummary", schema)
        self.assertIn("recoveryPolicy", schema)
        self.assertIn("eventMetadata", schema)
        self.assertIn("event_id", schema["eventMetadata"]["fields"])
        self.assertIn("sequence", schema["eventMetadata"]["fields"])
        self.assertIn("correlation_id", schema["eventMetadata"]["fields"])
        self.assertEqual(
            schema["cancelCategories"],
            ["user_cancelled", "shutdown", "superseded", "timeout_cancelled", "budget_exceeded"],
        )
        self.assertIn("cancellation_category", schema["healthSummary"]["fields"])
        self.assertIn("require_human", schema["recoveryPolicy"]["supportedActions"])
        self.assertIn("host_release_evidence", schema["evidenceSummary"]["fields"])
        self.assertIn("action_plan", schema["evidenceSummary"]["fields"])
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
        self.assertEqual(schema["hostDeclaredCheckActions"]["phase"], "verify")
        self.assertFalse(schema["hostDeclaredCheckActions"]["sideEffects"])
        self.assertIn("_check", schema["hostDeclaredCheckActions"]["pattern"])
        self.assertEqual(schema["validationContract"]["schemaVersion"], "across-validation-contract/1.0")
        self.assertIn("csv_row_expectation", schema["validationContract"]["checkTypes"])
        self.assertEqual(schema["context"]["heartbeat"], "callable lease renewal hook for long-running dispatch adapters")
        self.assertIn("raise_if_cancelled", schema["context"]["cancellation"])
        self.assertEqual(
            schema["failureTypes"],
            [
                "adapter_error",
                "approval_rejected",
                "budget_exceeded",
                "environment_blocked",
                "lease_expired",
                "max_runtime_exceeded",
                "max_turns_exceeded",
                "quality_failed",
                "timeout",
            ],
        )
        self.assertEqual(schema["telemetry"]["schemaVersion"], "agent-loop-telemetry/1.0")
        self.assertEqual(schema["budgetPolicy"]["schemaVersion"], "agent-loop-budget/1.0")
        self.assertIn("afterSequence", schema["streamResume"]["mcpTool"])
        self.assertIn("schema", schema["metadata"])
        self.assertIn("autopilot", schema["metadata"]["schema"]["properties"])
        self.assertIn("_check", schema["metadata"]["actionPlan"])

    def test_start_agent_loop_tool_schema_documents_host_action_plan_contract(self):
        from across_orchestrator.mcp import tool_definitions

        tools = {tool["name"]: tool for tool in tool_definitions()}
        metadata = tools["start_agent_loop"]["inputSchema"]["properties"]["metadata"]
        action_plan = metadata["properties"]["actionPlan"]
        action_item = action_plan["items"]
        autopilot = metadata["properties"]["autopilot"]

        self.assertIn("business_contract_check", action_plan["description"])
        self.assertIn("turn budget", action_plan["description"])
        self.assertIn("Object actionPlan entries are invalid", action_item["description"])
        self.assertIn("anyOf", action_item)
        self.assertIn("memory_search", action_item["anyOf"][0]["enum"])
        self.assertIn("_check", action_item["anyOf"][1]["pattern"])
        self.assertEqual(autopilot["required"], ["schema_version", "run_id", "evidence_contract"])
        self.assertEqual(autopilot["properties"]["schema_version"]["enum"], ["across-loop-spec/1.0"])
        self.assertEqual(autopilot["properties"]["evidence_contract"]["enum"], ["across-loop-evidence/1.0"])
        validation = metadata["properties"]["validationContract"]
        self.assertEqual(validation["properties"]["schema_version"]["enum"], ["across-validation-contract/1.0"])
        self.assertIn("row_expectations", validation["properties"]["artifacts"]["items"]["properties"])
        self.assertEqual(metadata["properties"]["validation_contract"]["properties"]["check_action"]["pattern"], validation["properties"]["check_action"]["pattern"])
        self.assertEqual(tools["evaluate_sandbox_policy"]["inputSchema"]["required"], ["policy"])
        self.assertEqual(tools["build_evidence_graph"]["inputSchema"]["required"], ["payload"])

    def test_external_agent_plugin_tool_schema_documents_manifest_contract(self):
        from across_orchestrator.mcp import tool_definitions

        tools = {tool["name"]: tool for tool in tool_definitions()}
        validate_manifest = tools["validate_external_agent_plugin"]["inputSchema"]["properties"]["manifest"]
        register_manifest = tools["register_external_agent_plugin"]["inputSchema"]["properties"]["manifest"]

        self.assertEqual(validate_manifest["properties"]["schema_version"]["enum"], ["across-agent-plugin/1.0"])
        self.assertEqual(validate_manifest["properties"]["entrypoints"]["additionalProperties"]["properties"]["command"]["type"], "array")
        self.assertIn(
            "Direct executable argv array",
            validate_manifest["properties"]["entrypoints"]["additionalProperties"]["properties"]["command"]["description"],
        )
        self.assertEqual(validate_manifest["properties"]["capabilities"]["items"]["anyOf"][1]["required"], ["id"])
        self.assertEqual(register_manifest["properties"]["entrypoints"]["additionalProperties"]["properties"]["command"]["type"], "array")

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
                rpc(6, "tools/call", {"name": "get_agent_loop_telemetry", "arguments": {"loopId": loop["loop_id"]}}),
                rpc(7, "tools/call", {"name": "get_agent_loop_events", "arguments": {"loopId": loop["loop_id"], "afterSequence": 1}}),
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
            telemetry = json.loads(second[5]["result"]["content"][0]["text"])
            self.assertEqual(telemetry["schema_version"], "agent-loop-telemetry/1.0")
            resumed_events = json.loads(second[6]["result"]["content"][0]["text"])
            self.assertTrue(resumed_events)
            self.assertTrue(all(event["sequence"] > 1 for event in resumed_events))

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

    def test_stdio_response_redacts_sensitive_values(self):
        from contextlib import redirect_stdout
        from io import StringIO

        from across_orchestrator.mcp import emit_stdio_response

        fake_token = "sk-" + "abcdefghijklmnopqrst"
        output = StringIO()
        with redirect_stdout(output):
            emit_stdio_response(
                99,
                result={
                    "password": "clear-text-password",
                    "notes": f"token {fake_token} should be hidden",
                },
            )

        serialized = output.getvalue()
        payload = json.loads(serialized)
        self.assertEqual(payload["result"]["password"], "[redacted]")
        self.assertIn("[redacted]", payload["result"]["notes"])
        self.assertNotIn("clear-text-password", serialized)
        self.assertNotIn(fake_token, serialized)

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
