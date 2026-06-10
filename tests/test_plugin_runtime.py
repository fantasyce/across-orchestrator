import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class PluginRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(__file__).resolve().parents[1]
        self.home = (Path(self.tempdir.name) / "across").resolve()
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(self.root / "src")
        self.env["ACROSS_HOME"] = str(self.home)
        self.env.pop("ACROSS_ORCHESTRATOR_HOME", None)

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

    def test_plugin_status_and_health_use_across_home(self):
        status_result = self.run_cli("plugin-status", "--json")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        status = json.loads(status_result.stdout)

        self.assertEqual(status["pluginId"], "across-orchestrator")
        self.assertEqual(status["paths"]["home"], str(self.home))
        self.assertEqual(status["paths"]["data"], str(self.home / "data" / "across-orchestrator"))
        self.assertIn("sdk", status["protocols"])
        self.assertTrue(status["install"]["installable"])

        health_result = self.run_cli("health", "--json")
        self.assertEqual(health_result.returncode, 0, health_result.stderr)
        health = json.loads(health_result.stdout)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["pluginId"], "across-orchestrator")
        self.assertEqual(health["home"], str(self.home / "data" / "across-orchestrator"))

    def test_plugin_manifest_declares_hosting_platform_contract(self):
        result = self.run_cli("plugin-manifest", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)

        self.assertEqual(manifest["pluginApiVersion"], "2026-06-10")
        self.assertTrue(manifest["capabilities"]["hostingPlatformAdapters"])
        self.assertTrue(manifest["capabilities"]["agentLoopRuntime"])
        self.assertTrue(manifest["capabilities"]["checkpoints"])
        self.assertTrue(manifest["capabilities"]["humanApproval"])
        self.assertTrue(manifest["capabilities"]["memoryHooks"])
        self.assertEqual(manifest["compatibility"]["requiredHostVersion"], ">=0.6.0")
        self.assertEqual(manifest["lifecycle"]["uninstall"]["args"][0], "plugin-uninstall")
        self.assertTrue(manifest["lifecycle"]["uninstall"]["preservesData"])
        self.assertEqual(manifest["permissions"]["network"][0]["host"], "127.0.0.1")
        self.assertEqual(manifest["entrypoints"]["sidecar"]["pluginManifestPath"], "/.well-known/across-plugin.json")
        self.assertIn("registered_agent_containers", manifest["hostingPlatform"]["hostProvides"])
        self.assertIn("evidence_bundles", manifest["hostingPlatform"]["pluginProvides"])
        self.assertIn("agent_loop_runtime", manifest["hostingPlatform"]["pluginProvides"])
        self.assertEqual(manifest["protocols"]["mcp"]["tools"]["startAgentLoop"], "start_agent_loop")

    def test_plugin_uninstall_removes_runtime_not_data(self):
        plugin_dir = self.home / "plugins" / "across-orchestrator"
        wrapper = self.home / "bin" / "across-orchestrator"
        data_dir = self.home / "data" / "across-orchestrator"
        plugin_dir.mkdir(parents=True)
        wrapper.parent.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        (plugin_dir / "manifest.json").write_text("{}", encoding="utf-8")
        wrapper.write_text("#!/bin/sh\n", encoding="utf-8")

        result = self.run_cli("plugin-uninstall", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)

        self.assertTrue(payload["removed"])
        self.assertFalse(plugin_dir.exists())
        self.assertFalse(wrapper.exists())
        self.assertTrue(data_dir.exists())

    def test_hosting_platform_contract_is_serializable(self):
        from across_orchestrator.host_adapters import build_hosting_platform_contract

        contract = build_hosting_platform_contract(
            "enterprise-agent-host",
            [
                {
                    "id": "legal-reviewer",
                    "name": "Legal Reviewer",
                    "endpoint": "https://agents.example.test/legal-reviewer",
                    "protocols": ["a2a", "mcp"],
                    "capabilities": ["contract_review"],
                    "tenant_id": "tenant-a",
                }
            ],
            memory_provider="across-context",
        )
        payload = contract.to_dict()

        self.assertEqual(payload["platform_id"], "enterprise-agent-host")
        self.assertEqual(payload["memory_provider"], "across-context")
        self.assertEqual(payload["agents"][0]["agent_id"], "legal-reviewer")
        self.assertEqual(payload["agents"][0]["protocols"], ["a2a", "mcp"])


if __name__ == "__main__":
    unittest.main()
