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
        self.env["ACROSS_HOME"] = str(Path(self.tempdir.name) / "across-home")
        self.env["ACROSS_CONTEXT_HOME"] = str(Path(self.tempdir.name) / "context-home")
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

    def test_cli_json_output_redacts_sensitive_payloads(self):
        fake_token = "sk-" + "abcdefghijklmnopqrst"
        payload = {
            "run_id": "run-secret-safe",
            "status": "passed",
            "quality": {
                "password": "clear-text-password",
                "notes": f"token {fake_token} should be hidden",
            },
        }
        result = self.run_cli("evidence-graph", "--payload-json", json.dumps(payload), "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("clear-text-password", result.stdout)
        self.assertNotIn(fake_token, result.stdout)
        parsed = json.loads(result.stdout)
        quality_node = next(item for item in parsed["nodes"] if item["type"] == "quality")
        self.assertEqual(quality_node["payload"]["password"], "[redacted]")
        self.assertEqual(quality_node["payload"]["notes"], "token [redacted] should be hidden")

    def test_cli_exposes_policy_comparison_and_read_only_replay_contracts(self):
        policy = self.run_cli(
            "execution-policy",
            "--payload-json",
            json.dumps({"role": "reviewer", "actions": ["inspect"], "budget": {"max_model_calls": 0}}),
            "--json",
        )
        self.assertEqual(policy.returncode, 0, policy.stderr)
        self.assertEqual(json.loads(policy.stdout)["schema_version"], "across-execution-policy/1.0")

        comparison = self.run_cli(
            "run-compare",
            "--payload-json",
            json.dumps({"baseline": {"verdict": "blocked"}, "candidate": {"verdict": "ready"}}),
            "--json",
        )
        self.assertEqual(comparison.returncode, 0, comparison.stderr)
        self.assertTrue(json.loads(comparison.stdout)["changes"]["verdict"]["changed"])

        replay = self.run_cli(
            "replay-plan",
            "--payload-json",
            json.dumps({"source": {"run_id": "run-1"}, "external_side_effects": ["push"]}),
            "--json",
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replay_payload = json.loads(replay.stdout)
        self.assertEqual(replay_payload["status"], "blocked")
        self.assertFalse(replay_payload["execution"]["performed"])

    def test_cli_submit_preserves_explicit_serial_plan(self):
        subtasks = [
            {
                "id": "stage-design",
                "description": "Create the release design note",
                "path": "README.md",
                "agent": "openclaw",
                "wave": 1,
                "priority": 1,
            },
            {
                "id": "stage-implement",
                "description": "Implement after the design note is complete",
                "path": "web/index.html",
                "agent": "hermes",
                "wave": 2,
                "priority": 2,
                "dependencies": ["stage-design"],
            },
        ]

        submit = self.run_cli(
            "submit",
            "Build a serial release validation chain",
            "--project",
            str(self.project),
            "--deliverable",
            "README.md",
            "--deliverable",
            "web/index.html",
            "--agent",
            "openclaw",
            "--strict-dependency",
            "--subtasks-json",
            json.dumps(subtasks),
            "--task-type",
            "artifact",
            "--json",
        )

        self.assertEqual(submit.returncode, 0, submit.stderr)
        task = json.loads(submit.stdout)
        self.assertEqual(task["metadata"]["task_types"], ["artifact"])
        self.assertEqual(task["contract"]["serialPlan"], True)
        self.assertEqual([item["wave"] for item in task["subtasks"]], [1, 2])
        self.assertEqual(task["subtasks"][1]["dependencies"], [task["subtasks"][0]["subtask_id"]])

    def test_cli_evaluates_agent_team_readiness_payload(self):
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
                "a2a": {"schema_version": "across-a2a-task-delegation/1.0"},
                "observability": {"otel_schema": "across-otel-genai-export/1.0", "otlp_trace_schema": "otlp-traces-json/1.0", "raw_transcripts_included": False},
            },
        }

        result = self.run_cli("agent-team-readiness", "--payload-json", json.dumps(payload), "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema_version"], "across-agent-team-readiness/1.0")
        self.assertEqual(report["status"], "passed")

    def test_cli_declared_agent_adapter_executes_arbitrary_agent(self):
        agent_script = self.project / "cli_agent_adapter.py"
        agent_script.write_text(
            "\n".join(
                [
                    "import json",
                    "import os",
                    "from pathlib import Path",
                    "subtask = json.loads(os.environ['ACROSS_SUBTASK_JSON'])",
                    "target = Path(subtask['path'])",
                    "target.parent.mkdir(parents=True, exist_ok=True)",
                    "target.write_text(f\"cli-adapter={subtask['agent']}\\n\", encoding='utf-8')",
                    "print(json.dumps({'agent': subtask['agent'], 'path': subtask['path']}))",
                ]
            ),
            encoding="utf-8",
        )

        submit = self.run_cli(
            "submit",
            "Run a CLI-declared custom agent",
            "--project",
            str(self.project),
            "--deliverable",
            "cli/out.txt",
            "--agent",
            "cli-custom-agent",
            "--agent-adapters-json",
            json.dumps(
                {
                    "cli-custom-agent": {
                        "type": "command",
                        "command": [sys.executable, str(agent_script)],
                    }
                }
            ),
            "--json",
        )

        self.assertEqual(submit.returncode, 0, submit.stderr)
        task = json.loads(submit.stdout)
        self.assertEqual(task["metadata"]["agent_adapters"]["cli-custom-agent"]["type"], "command")

        run = self.run_cli("run", task["task_id"], "--json")

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["status"], "completed")
        self.assertEqual((self.project / "cli/out.txt").read_text(encoding="utf-8"), "cli-adapter=cli-custom-agent\n")

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
        self.assertTrue(manifest["capabilities"]["agentLoopRuntime"])
        self.assertEqual(manifest["entrypoints"]["sidecar"]["command"], "across-orchestrator")
        self.assertEqual(manifest["paths"]["data"], "~/.across/data/across-orchestrator")
        self.assertEqual(manifest["protocols"]["http"]["loopStart"], "POST /loops")
        self.assertEqual(manifest["protocols"]["http"]["hostConformance"], "POST /host-conformance")

    def test_cli_sandbox_probe_and_evidence_graph(self):
        policy = {
            "network_policy": "none",
            "filesystem_policy": "read_only",
            "workspace_root": str(self.project),
            "command_allowlist": ["node --version"],
        }
        sandbox = self.run_cli(
            "sandbox-probe",
            "--policy-json",
            json.dumps(policy),
            "--command-json",
            json.dumps(["node", "--version"]),
            "--cwd",
            str(self.project),
            "--json",
        )
        self.assertEqual(sandbox.returncode, 0, sandbox.stderr)
        sandbox_payload = json.loads(sandbox.stdout)
        self.assertEqual(sandbox_payload["schema_version"], "across-sandbox-evidence/1.0")
        self.assertEqual(sandbox_payload["status"], "passed")

        graph = self.run_cli(
            "evidence-graph",
            "--payload-json",
            json.dumps({
                "schema_version": "across-loop-evidence/1.0",
                "run_id": "run-1",
                "spec_id": "plugin-compatibility-lab-v2",
                "status": "completed",
                "actions": [{"id": "workflow_pack_export", "status": "passed"}],
            }),
            "--json",
        )
        self.assertEqual(graph.returncode, 0, graph.stderr)
        graph_payload = json.loads(graph.stdout)
        self.assertEqual(graph_payload["schema_version"], "across-evidence-graph/1.0")
        self.assertTrue(any(node["id"] == "action:workflow_pack_export" for node in graph_payload["nodes"]))

    def test_cli_host_conformance_validates_external_host_contract(self):
        contract_path = self.project / "host-contract.json"
        contract_path.write_text(
            json.dumps(
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
                }
            ),
            encoding="utf-8",
        )

        result = self.run_cli("host-conformance", "--contract", str(contract_path), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["passed"])
        self.assertEqual(report["host"]["platformId"], "generic-agent-host")
        self.assertNotIn("Across Agents Assistant", result.stdout)
        self.assertNotIn("Documents/projects", result.stdout)

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
        self.assertEqual(payload["app_grade"]["scenario_id"], "host_agent_full_delivery_v1")
        self.assertIn(payload["app_grade"]["delivery_quality"], {"passed", "partial"})

    def test_cli_agent_loop_lifecycle(self):
        start = self.run_cli(
            "loop-start",
            "Coordinate platform agents",
            "--project",
            str(self.project),
            "--agent",
            "owner",
            "--max-turns",
            "8",
            "--json",
        )
        self.assertEqual(start.returncode, 0, start.stderr)
        loop = json.loads(start.stdout)
        self.assertTrue(loop["loop_id"].startswith("loop-"))
        self.assertEqual(loop["status"], "pending")

        run = self.run_cli("loop-run", loop["loop_id"], "--json")
        self.assertEqual(run.returncode, 0, run.stderr)
        completed = json.loads(run.stdout)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["steps"][0]["action"]["type"], "memory_search")
        self.assertEqual(completed["checkpoint_count"], 5)

        status = self.run_cli("loop-status", loop["loop_id"], "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["final_output"], "Agent loop completed for: Coordinate platform agents")

        health = self.run_cli("loop-health", loop["loop_id"], "--json")
        self.assertEqual(health.returncode, 0, health.stderr)
        health_payload = json.loads(health.stdout)
        self.assertEqual(health_payload["status"], "completed")
        self.assertEqual(health_payload["loop_id"], loop["loop_id"])
        self.assertEqual(health_payload["recent_failure_types"], {})

        summary = self.run_cli("loop-evidence-summary", loop["loop_id"], "--json")
        self.assertEqual(summary.returncode, 0, summary.stderr)
        summary_payload = json.loads(summary.stdout)
        self.assertEqual(summary_payload["schema_version"], "0.1")
        self.assertEqual(summary_payload["status"], "completed")
        self.assertTrue(summary_payload["event_audit"]["sequence_contiguous"])
        self.assertEqual(summary_payload["host_release_evidence"]["readiness"], "attention")
        self.assertEqual(
            next(
                check
                for check in summary_payload["host_release_evidence"]["checks"]
                if check["id"] == "memory_candidates"
            )["status"],
            "attention",
        )

        events = self.run_cli("loop-events", loop["loop_id"], "--json")
        self.assertEqual(events.returncode, 0, events.stderr)
        self.assertIn("loop.completed", [event["type"] for event in json.loads(events.stdout)])

        resumed_events = self.run_cli("loop-events", loop["loop_id"], "--after-sequence", "1", "--json")
        self.assertEqual(resumed_events.returncode, 0, resumed_events.stderr)
        resumed_payload = json.loads(resumed_events.stdout)
        self.assertTrue(resumed_payload)
        self.assertTrue(all(event["sequence"] > 1 for event in resumed_payload))

        telemetry = self.run_cli("loop-telemetry", loop["loop_id"], "--json")
        self.assertEqual(telemetry.returncode, 0, telemetry.stderr)
        telemetry_payload = json.loads(telemetry.stdout)
        self.assertEqual(telemetry_payload["schema_version"], "agent-loop-telemetry/1.0")
        self.assertEqual(telemetry_payload["loop_id"], loop["loop_id"])

    def test_cli_agent_loop_approval_lifecycle(self):
        start = self.run_cli(
            "loop-start",
            "Coordinate approval-gated platform agents",
            "--project",
            str(self.project),
            "--agent",
            "owner",
            "--max-turns",
            "8",
            "--require-approval-for",
            "task_dispatch",
            "--json",
        )
        self.assertEqual(start.returncode, 0, start.stderr)
        loop = json.loads(start.stdout)

        waiting = self.run_cli("loop-run", loop["loop_id"], "--json")
        self.assertEqual(waiting.returncode, 0, waiting.stderr)
        waiting_payload = json.loads(waiting.stdout)
        self.assertEqual(waiting_payload["status"], "awaiting_approval")
        action_id = waiting_payload["steps"][-1]["action"]["action_id"]

        approved = self.run_cli("loop-approve", loop["loop_id"], action_id, "--json")
        self.assertEqual(approved.returncode, 0, approved.stderr)
        self.assertEqual(json.loads(approved.stdout)["steps"][-1]["action"]["approval_status"], "approved")

        completed = self.run_cli("loop-run", loop["loop_id"], "--json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "completed")

    def test_cli_agent_loop_accepts_memory_policy_and_metadata_json(self):
        start = self.run_cli(
            "loop-start",
            "Coordinate metadata-aware loop",
            "--project",
            str(self.project),
            "--memory-policy-json",
            json.dumps({"read": False, "writeCandidates": False, "limit": 3}),
            "--metadata-json",
            json.dumps({"maxRemediationTurns": 2, "scenario": "cli-json"}),
            "--json",
        )

        self.assertEqual(start.returncode, 0, start.stderr)
        loop = json.loads(start.stdout)
        self.assertEqual(loop["memory_policy"]["read"], False)
        self.assertEqual(loop["memory_policy"]["writeCandidates"], False)
        self.assertEqual(loop["memory_policy"]["limit"], 3)
        self.assertEqual(loop["metadata"]["scenario"], "cli-json")

        run = self.run_cli("loop-run", loop["loop_id"], "--json")
        self.assertEqual(run.returncode, 0, run.stderr)
        completed = json.loads(run.stdout)
        self.assertEqual(
            [step["action"]["type"] for step in completed["steps"]],
            ["task_dispatch", "quality_gate", "final_output"],
        )

    def test_cli_loop_start_rejects_invalid_action_plan_without_traceback(self):
        invalid = self.run_cli(
            "loop-start",
            "Reject invalid plan",
            "--project",
            str(self.project),
            "--metadata-json",
            json.dumps({"actionPlan": ["task_dispatch", "unsafe_shell_action"]}),
            "--json",
        )

        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("unsupported actionPlan entries", invalid.stderr)
        self.assertNotIn("Traceback", invalid.stderr)

    def test_cli_agent_loop_control_actions(self):
        cancel_start = self.run_cli(
            "loop-start",
            "Cancel from CLI",
            "--project",
            str(self.project),
            "--json",
        )
        self.assertEqual(cancel_start.returncode, 0, cancel_start.stderr)
        cancel_loop = json.loads(cancel_start.stdout)

        cancelled = self.run_cli(
            "loop-cancel",
            cancel_loop["loop_id"],
            "--reason",
            "no longer needed",
            "--category",
            "superseded",
            "--json",
        )

        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertEqual(json.loads(cancelled.stdout)["status"], "cancelled")
        cancel_events = self.run_cli("loop-events", cancel_loop["loop_id"], "--json")
        self.assertEqual(cancel_events.returncode, 0, cancel_events.stderr)
        self.assertEqual(
            next(event for event in json.loads(cancel_events.stdout) if event["type"] == "loop.cancel_requested")["payload"]["cancel_category"],
            "superseded",
        )

        reject_start = self.run_cli(
            "loop-start",
            "Reject from CLI",
            "--project",
            str(self.project),
            "--approval-policy-json",
            json.dumps({"requireApprovalFor": ["task_dispatch"]}),
            "--json",
        )
        self.assertEqual(reject_start.returncode, 0, reject_start.stderr)
        reject_loop = json.loads(reject_start.stdout)
        waiting = self.run_cli("loop-run", reject_loop["loop_id"], "--json")
        self.assertEqual(waiting.returncode, 0, waiting.stderr)
        action_id = json.loads(waiting.stdout)["steps"][-1]["action"]["action_id"]

        rejected = self.run_cli(
            "loop-reject",
            reject_loop["loop_id"],
            action_id,
            "--reason",
            "needs review",
            "--json",
        )

        self.assertEqual(rejected.returncode, 0, rejected.stderr)
        rejected_loop = json.loads(rejected.stdout)
        self.assertEqual(rejected_loop["status"], "stopped")
        self.assertEqual(rejected_loop["steps"][-1]["action"]["approval_status"], "rejected")

        retry_start = self.run_cli(
            "loop-start",
            "Retry from CLI",
            "--project",
            str(self.project),
            "--memory-policy-json",
            json.dumps({"read": False, "writeCandidates": False}),
            "--json",
        )
        self.assertEqual(retry_start.returncode, 0, retry_start.stderr)
        retry_loop = json.loads(retry_start.stdout)
        completed = self.run_cli("loop-run", retry_loop["loop_id"], "--json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        completed_loop = json.loads(completed.stdout)
        quality_step = next(step for step in completed_loop["steps"] if step["action"]["type"] == "quality_gate")

        rewound = self.run_cli("loop-retry", retry_loop["loop_id"], quality_step["step_id"], "--json")

        self.assertEqual(rewound.returncode, 0, rewound.stderr)
        self.assertEqual(json.loads(rewound.stdout)["status"], "running")
        self.assertEqual(
            [step["action"]["type"] for step in json.loads(rewound.stdout)["steps"]],
            ["task_dispatch"],
        )


if __name__ == "__main__":
    unittest.main()
