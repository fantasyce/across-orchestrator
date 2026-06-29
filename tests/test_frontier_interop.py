import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class FrontierInteropTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(__file__).resolve().parents[1]
        self.home = Path(self.tempdir.name) / "home"
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

    def test_cli_renders_remote_mcp_oauth_template(self):
        result = self.run_cli(
            "remote-mcp-oauth-template",
            "--config-json",
            json.dumps({"base_url": "https://example.test/mcp", "issuer": "https://issuer.example.test"}),
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], "across-remote-mcp-oauth-template/1.0")
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["transport"]["type"], "streamable_http")
        self.assertFalse(payload["authorization"]["secrets_embedded"])

    def test_cli_creates_a2a_delegation_envelope(self):
        result = self.run_cli(
            "a2a-delegation",
            "--payload-json",
            json.dumps({"goal": "Validate a generic agent plugin", "pack_id": "plugin-compatibility-lab-v2"}),
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], "across-a2a-task-delegation/2.0")
        self.assertIn("across-a2a-task-delegation/1.0", payload["compatible_schema_versions"])
        self.assertEqual(payload["jsonrpc"]["method"], "tasks/send")
        self.assertEqual(payload["streaming"]["method"], "tasks/sendSubscribe")
        self.assertEqual(payload["push_notification"]["method"], "tasks/pushNotificationConfig/set")
        self.assertTrue(payload["agent_card"]["capabilities"]["streaming"])
        self.assertEqual(payload["task"]["metadata"]["pack_id"], "plugin-compatibility-lab-v2")
        self.assertTrue(payload["evidence_receipt"]["required"])
        self.assertTrue(payload["artifacts"])

    def test_cli_projects_agui_events(self):
        result = self.run_cli(
            "agui-projection",
            "--payload-json",
            json.dumps(
                {
                    "source": "loop",
                    "loop_id": "loop-1",
                    "events": [
                        {
                            "type": "loop.started",
                            "event_id": "evt-1",
                            "sequence": 1,
                            "payload": {"summary": "started", "token": "sk-abcdefghijklmnop"},
                        },
                        {"type": "loop.completed", "event_id": "evt-2", "sequence": 2, "payload": {"status": "completed"}},
                    ],
                }
            ),
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], "across-agui-projection/1.0")
        self.assertEqual(payload["ag_ui_schema"], "ag-ui-event-stream/1.0")
        self.assertEqual([event["type"] for event in payload["events"]], ["task.created", "task.completed"])
        self.assertEqual(payload["events"][0]["payload"]["token"], "[redacted]")
        self.assertFalse(payload["summary"]["secrets_included"])

    def test_cli_creates_agent_team_contract(self):
        result = self.run_cli(
            "agent-team",
            "--payload-json",
            json.dumps(
                {
                    "owner_agent": "owner",
                    "agents": [
                        {"id": "owner", "role": "owner"},
                        {"id": "reviewer", "role": "review", "context_refs": ["NOTES.md"]},
                    ],
                    "handoffs": [{"from": "owner", "to": "reviewer", "artifact": {"path": "NOTES.md"}}],
                }
            ),
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], "across-agent-team/1.0")
        self.assertEqual(payload["owner_agent"], "owner")
        self.assertEqual(len(payload["agents"]), 2)
        self.assertTrue(payload["checkpoint_policy"]["independent_session"])
        self.assertEqual(payload["handoffs"][0]["artifact"]["path"], "NOTES.md")

    def test_cli_exports_otel_genai_spans_and_eval_cases(self):
        evidence = {
            "run_id": "run-1-sk-abcdefghijklmnop",
            "spec_id": "plugin-compatibility-lab-v2",
            "status": "completed",
            "actions": [{"id": "workflow_pack_export", "status": "passed"}],
            "gates": [{"id": "workflow_pack_exports_ready", "status": "passed"}],
        }

        result = self.run_cli("otel-export", "--payload-json", json.dumps(evidence), "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema_version"], "across-otel-genai-export/1.0")
        self.assertGreaterEqual(payload["summary"]["span_count"], 4)
        self.assertEqual(payload["summary"]["eval_case_count"], 1)
        self.assertFalse(payload["summary"]["raw_transcripts_included"])
        self.assertNotIn("sk-abcdefghijklmnop", json.dumps(payload))
        self.assertEqual(payload["otlp"]["schema_version"], "otlp-traces-json/1.0")
        self.assertEqual(len(payload["otlp"]["resourceSpans"]), 1)
        self.assertGreaterEqual(len(payload["otlp"]["resourceSpans"][0]["scopeSpans"][0]["spans"]), 4)

    def test_cli_writes_otlp_trace_file(self):
        evidence = {
            "run_id": "run-otlp",
            "spec_id": "plugin-compatibility-lab-v2",
            "status": "completed",
            "actions": [{"id": "workflow_pack_export", "status": "passed"}],
            "gates": [{"id": "workflow_pack_exports_ready", "status": "passed"}],
        }
        target = Path(self.tempdir.name) / "otel" / "traces.json"

        result = self.run_cli(
            "otel-export",
            "--payload-json",
            json.dumps(evidence),
            "--otlp-file",
            str(target),
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["otlp_file"], str(target))
        written = json.loads(target.read_text())
        self.assertEqual(written["schema_version"], "otlp-traces-json/1.0")
        self.assertEqual(len(written["resourceSpans"]), 1)


if __name__ == "__main__":
    unittest.main()
