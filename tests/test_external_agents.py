import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ExternalAgentPluginRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(__file__).resolve().parents[1]
        self.home = Path(self.tempdir.name) / "home"
        self.home.mkdir()
        self.manifest = Path(self.tempdir.name) / "echo-agent.json"
        self.manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
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

    def test_registry_normalizes_manifest_and_is_secret_free(self):
        from across_orchestrator.external_agents import ExternalAgentRegistry

        registry = ExternalAgentRegistry(home=self.home)
        payload = registry.register_manifest_file(self.manifest)

        self.assertEqual(payload["schema_version"], "across-orchestrator-external-agents/1.0")
        self.assertEqual(payload["summary"]["agent_count"], 1)
        self.assertEqual(payload["summary"]["healthy_agent_count"], 1)
        self.assertFalse(payload["security"]["secrets_included"])
        self.assertFalse(payload["security"]["shell_execution"])
        self.assertEqual(payload["agents"][0]["plugin_id"], "demo.echo-agent")
        self.assertEqual(payload["agents"][0]["trust"]["mutation_boundary"], "read_only")
        self.assertNotIn("command", json.dumps(payload["agents"][0]["entrypoints"]))

    def test_registry_registers_manifest_objects_for_mcp_hosts(self):
        from across_orchestrator.external_agents import ExternalAgentRegistry

        registry = ExternalAgentRegistry(home=self.home)
        payload = registry.register_manifest(_manifest())

        self.assertEqual(payload["summary"]["agent_count"], 1)
        self.assertEqual(payload["summary"]["plugin_count"], 1)
        self.assertEqual(payload["agents"][0]["plugin_id"], "demo.echo-agent")
        self.assertEqual(registry.health_payload("demo.echo")["status"], "passed")

    def test_registry_rejects_incomplete_entrypoints(self):
        from across_orchestrator.external_agents import normalize_agent_plugin_manifest

        manifest = _manifest()
        manifest["entrypoints"] = {"run": {"transport": "stdio"}}

        with self.assertRaisesRegex(ValueError, "entrypoint run must define command or url"):
            normalize_agent_plugin_manifest(manifest)

    def test_cli_register_list_and_health_use_same_contract(self):
        registered = self.run_cli("external-agents", "register", "--manifest", str(self.manifest), "--json")
        self.assertEqual(registered.returncode, 0, registered.stderr)
        self.assertEqual(json.loads(registered.stdout)["summary"]["agent_count"], 1)

        listed = self.run_cli("external-agents", "list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(json.loads(listed.stdout)["agents"][0]["agent_id"], "demo.echo")

        health = self.run_cli("external-agents", "health", "--agent-id", "demo.echo", "--json")
        self.assertEqual(health.returncode, 0, health.stderr)
        payload = json.loads(health.stdout)
        self.assertEqual(payload["schema_version"], "across-orchestrator-external-agent-health/1.0")
        self.assertEqual(payload["status"], "passed")


def _manifest():
    return {
        "schema_version": "across-agent-plugin/1.0",
        "plugin_id": "demo.echo-agent",
        "display_name": "Demo Echo Agent",
        "version": "1.0.0",
        "agent": {"id": "demo.echo", "name": "Demo Echo", "vendor": "local"},
        "protocols": ["stdio"],
        "capabilities": [{"id": "message.echo", "kind": "tool", "risk": "low"}],
        "entrypoints": {
            "run": {"command": [sys.executable, "-m", "json.tool"], "transport": "stdio"},
        },
        "trust": {
            "mutation_boundary": "read_only",
            "requires_human_approval": False,
            "secrets_included": False,
        },
        "context": {"pack_id": "demo.echo"},
        "health": {"status": "passed", "message": "static test health"},
    }


if __name__ == "__main__":
    unittest.main()
