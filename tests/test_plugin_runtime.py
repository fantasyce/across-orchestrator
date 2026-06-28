import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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

        manifest_result = self.run_cli("plugin-manifest", "--json")
        self.assertEqual(manifest_result.returncode, 0, manifest_result.stderr)
        manifest = json.loads(manifest_result.stdout)
        self.assertTrue(manifest["capabilities"]["evidenceGraph"])
        self.assertTrue(manifest["capabilities"]["sandboxPolicyEvaluation"])
        self.assertTrue(manifest["capabilities"]["agentTeamReadiness"])
        self.assertTrue(manifest["capabilities"]["remoteMcpOAuthTemplate"])
        self.assertTrue(manifest["capabilities"]["a2aTaskDelegation"])
        self.assertTrue(manifest["capabilities"]["otelGenaiExport"])
        self.assertEqual(
            manifest["protocols"]["mcp"]["tools"]["evaluateAgentTeamReadiness"],
            "evaluate_agent_team_readiness",
        )
        self.assertEqual(
            manifest["protocols"]["mcp"]["tools"]["exportOtelGenaiSpans"],
            "export_otel_genai_spans",
        )
        self.assertIn("evidence_graph", manifest["hostingPlatform"]["pluginProvides"])
        self.assertIn("agent_team_readiness", manifest["hostingPlatform"]["pluginProvides"])
        self.assertIn("remote_mcp_oauth_template", manifest["hostingPlatform"]["pluginProvides"])
        self.assertIn("a2a_task_delegation", manifest["hostingPlatform"]["pluginProvides"])
        self.assertIn("otel_genai_export", manifest["hostingPlatform"]["pluginProvides"])

        health_result = self.run_cli("health", "--json")
        self.assertEqual(health_result.returncode, 0, health_result.stderr)
        health = json.loads(health_result.stdout)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["pluginId"], "across-orchestrator")
        self.assertEqual(health["home"], str(self.home / "data" / "across-orchestrator"))

    def test_install_command_prepares_generic_host_mcp_registrations(self):
        claude_config = self.home / "claude_desktop_config.json"
        claude_config.parent.mkdir(parents=True, exist_ok=True)
        claude_config.write_text(json.dumps({"deploymentMode": "default"}), encoding="utf-8")

        codex_result = self.run_cli("install", "codex-mcp", "--stdout")
        self.assertEqual(codex_result.returncode, 0, codex_result.stderr)
        self.assertIn("codex mcp add across-orchestrator -- sh -lc", codex_result.stdout)
        self.assertIn(str(self.home / "bin" / "across-orchestrator"), codex_result.stdout)

        claude_result = self.run_cli("install", "claude-code", "--stdout")
        self.assertEqual(claude_result.returncode, 0, claude_result.stderr)
        self.assertIn("claude mcp add -s user across-orchestrator -- sh -lc", claude_result.stdout)
        self.assertIn(str(self.home / "bin" / "across-orchestrator"), claude_result.stdout)

        desktop_result = self.run_cli("install", "claude-desktop", "--config-file", str(claude_config), "--json")
        self.assertEqual(desktop_result.returncode, 0, desktop_result.stderr)
        desktop_install = json.loads(desktop_result.stdout)
        self.assertEqual(desktop_install["target"], "claude-desktop")
        self.assertEqual(desktop_install["runtime"]["wrapper"], str(self.home / "bin" / "across-orchestrator"))
        payload = json.loads(claude_config.read_text(encoding="utf-8"))
        self.assertEqual(payload["deploymentMode"], "default")
        self.assertEqual(payload["mcpServers"]["across-orchestrator"]["command"], "sh")
        self.assertEqual(payload["mcpServers"]["across-orchestrator"]["args"], ["-lc", f"exec '{self.home / 'bin' / 'across-orchestrator'}' mcp"])
        self.assertTrue((self.home / "bin" / "across-orchestrator").is_file())
        self.assertTrue((self.home / "plugins" / "across-orchestrator" / "venv" / "bin" / "across-orchestrator").is_file())

    def test_plugin_status_expands_tilde_path_with_env_home(self):
        bin_dir = self.home / "tools"
        command = bin_dir / "across-orchestrator"
        bin_dir.mkdir(parents=True)
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = "~/tools"

        status_result = self.run_cli("plugin-status", "--json")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        status = json.loads(status_result.stdout)

        self.assertEqual(status["command"], str(command))
        self.assertTrue(status["available"])

    def test_plugin_status_warns_when_across_context_command_uses_development_checkout(self):
        self.env["ACROSS_ORCHESTRATOR_MEMORY_PROVIDER"] = "across-context"
        self.env["ACROSS_CONTEXT_COMMAND"] = "node /tmp/Documents/projects/across-context/src/cli.js"

        status_result = self.run_cli("plugin-status", "--json")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        status = json.loads(status_result.stdout)

        memory_provider = status["memoryProvider"]
        self.assertEqual(memory_provider["provider"], "across-context")
        self.assertEqual(memory_provider["status"], "warning")
        self.assertIn("development checkout", memory_provider["warnings"][0])
        self.assertEqual(memory_provider["recommendedCommand"], str(self.home / "bin" / "across-context"))

    def test_plugin_status_enables_memory_provider_when_context_command_is_configured(self):
        command = self.home / "tools" / "across-context"
        command.parent.mkdir(parents=True)
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
        self.env.pop("ACROSS_ORCHESTRATOR_MEMORY_PROVIDER", None)
        self.env["ACROSS_CONTEXT_COMMAND"] = str(command)

        status_result = self.run_cli("plugin-status", "--json")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        status = json.loads(status_result.stdout)

        memory_provider = status["memoryProvider"]
        self.assertEqual(memory_provider["provider"], "across-context")
        self.assertEqual(memory_provider["status"], "configured")
        self.assertEqual(memory_provider["command"], [str(command)])
        self.assertEqual(memory_provider["resolvedCommand"], str(command))

    def test_product_plugin_status_blocks_across_context_development_command(self):
        self.env["ACROSS_ORCHESTRATOR_PRODUCT_MODE"] = "1"
        self.env["ACROSS_ORCHESTRATOR_MEMORY_PROVIDER"] = "across-context"
        self.env["ACROSS_CONTEXT_COMMAND"] = "node /tmp/Documents/projects/across-context/src/cli.js"

        status_result = self.run_cli("plugin-status", "--json")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        status = json.loads(status_result.stdout)

        memory_provider = status["memoryProvider"]
        self.assertEqual(memory_provider["provider"], "across-context")
        self.assertEqual(memory_provider["status"], "needs_repair")
        self.assertEqual(memory_provider["command"], ["node", "<protected-user-path>"])
        self.assertIn("development checkout", memory_provider["warnings"][0])
        self.assertEqual(memory_provider["recommendedCommand"], str(self.home / "bin" / "across-context"))
        self.assertNotIn("Documents", json.dumps(memory_provider))

    def test_product_plugin_status_allows_similarly_named_context_command_directory(self):
        self.env["ACROSS_ORCHESTRATOR_PRODUCT_MODE"] = "1"
        self.env["ACROSS_ORCHESTRATOR_MEMORY_PROVIDER"] = "across-context"
        self.env["HOME"] = str(self.home)
        adjacent_bin = self.home / "DocumentsArchive" / "across-context" / "bin"
        command = adjacent_bin / "across-context"
        adjacent_bin.mkdir(parents=True)
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
        self.env["ACROSS_CONTEXT_COMMAND"] = str(command)

        status_result = self.run_cli("plugin-status", "--json")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        status = json.loads(status_result.stdout)

        memory_provider = status["memoryProvider"]
        self.assertEqual(memory_provider["status"], "configured")
        self.assertEqual(memory_provider["command"], [str(command)])
        self.assertEqual(memory_provider["resolvedCommand"], str(command))
        self.assertEqual(memory_provider["warnings"], [])

    def test_plugin_status_warns_when_default_across_context_command_resolves_to_development_checkout(self):
        dev_bin = self.home / "Documents" / "projects" / "across-context" / "bin"
        dev_command = dev_bin / "across-context"
        dev_bin.mkdir(parents=True)
        dev_command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        dev_command.chmod(0o755)
        self.env["ACROSS_ORCHESTRATOR_MEMORY_PROVIDER"] = "across-context"
        self.env.pop("ACROSS_CONTEXT_COMMAND", None)
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = str(dev_bin)

        status_result = self.run_cli("plugin-status", "--json")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        status = json.loads(status_result.stdout)

        memory_provider = status["memoryProvider"]
        self.assertEqual(memory_provider["provider"], "across-context")
        self.assertEqual(memory_provider["status"], "warning")
        self.assertEqual(memory_provider["resolvedCommand"], str(dev_command))
        self.assertIn("development checkout", memory_provider["warnings"][0])

    def test_plugin_status_prefers_managed_across_context_wrapper_over_path_lookup(self):
        managed_command = self.home / "bin" / "across-context"
        dev_bin = self.home / "Documents" / "projects" / "across-context" / "bin"
        dev_command = dev_bin / "across-context"
        managed_command.parent.mkdir(parents=True)
        dev_bin.mkdir(parents=True)
        managed_command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        dev_command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        managed_command.chmod(0o755)
        dev_command.chmod(0o755)
        self.env["ACROSS_ORCHESTRATOR_MEMORY_PROVIDER"] = "across-context"
        self.env.pop("ACROSS_CONTEXT_COMMAND", None)
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = str(dev_bin)

        status_result = self.run_cli("plugin-status", "--json")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        status = json.loads(status_result.stdout)

        memory_provider = status["memoryProvider"]
        self.assertEqual(memory_provider["status"], "configured")
        self.assertEqual(memory_provider["command"], [str(managed_command)])
        self.assertEqual(memory_provider["resolvedCommand"], str(managed_command))
        self.assertEqual(memory_provider["warnings"], [])

    def test_product_plugin_status_ignores_protected_bin_home_for_managed_context_wrapper(self):
        managed_command = self.home / "bin" / "across-context"
        dev_bin = self.home / "Documents" / "projects" / "across-context" / "bin"
        dev_command = dev_bin / "across-context"
        managed_command.parent.mkdir(parents=True)
        dev_bin.mkdir(parents=True)
        managed_command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        dev_command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        managed_command.chmod(0o755)
        dev_command.chmod(0o755)
        self.env["ACROSS_ORCHESTRATOR_PRODUCT_MODE"] = "1"
        self.env["ACROSS_ORCHESTRATOR_MEMORY_PROVIDER"] = "across-context"
        self.env.pop("ACROSS_CONTEXT_COMMAND", None)
        self.env["HOME"] = str(self.home)
        self.env["ACROSS_BIN_HOME"] = str(dev_bin)
        self.env["PATH"] = str(dev_bin)

        status_result = self.run_cli("plugin-status", "--json")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        status = json.loads(status_result.stdout)

        memory_provider = status["memoryProvider"]
        self.assertEqual(memory_provider["status"], "configured")
        self.assertEqual(memory_provider["command"], [str(managed_command)])
        self.assertEqual(memory_provider["resolvedCommand"], str(managed_command))
        self.assertEqual(memory_provider["warnings"], [])

    def test_product_plugin_status_redacts_protected_context_command_on_path(self):
        dev_bin = self.home / "Documents" / "projects" / "across-context" / "bin"
        dev_command = dev_bin / "across-context"
        dev_bin.mkdir(parents=True)
        dev_command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        dev_command.chmod(0o755)
        self.env["ACROSS_ORCHESTRATOR_PRODUCT_MODE"] = "1"
        self.env["ACROSS_ORCHESTRATOR_MEMORY_PROVIDER"] = "across-context"
        self.env.pop("ACROSS_CONTEXT_COMMAND", None)
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = str(dev_bin)

        status_result = self.run_cli("plugin-status", "--json")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        status = json.loads(status_result.stdout)

        memory_provider = status["memoryProvider"]
        self.assertEqual(memory_provider["status"], "needs_repair")
        self.assertEqual(memory_provider["command"], ["across-context"])
        self.assertIsNone(memory_provider["resolvedCommand"])
        self.assertNotIn("Documents", json.dumps(memory_provider))

    def test_context_diagnostics_skip_protected_path_before_file_probe(self):
        from across_orchestrator.across_context import diagnose_across_context_command

        dev_bin = self.home / "Documents" / "projects" / "across-context" / "bin"
        env = {
            **self.env,
            "HOME": str(self.home),
            "ACROSS_ORCHESTRATOR_PRODUCT_MODE": "1",
            "PATH": str(dev_bin),
        }

        def guarded_is_file(path):
            if "Documents" in str(path):
                raise AssertionError("protected Context PATH was probed")
            return False

        with patch("across_orchestrator.across_context.Path.is_file", guarded_is_file):
            diagnostic = diagnose_across_context_command(env)

        self.assertEqual(diagnostic["status"], "needs_repair")
        self.assertIsNone(diagnostic["resolvedCommand"])

    def test_orchestrator_status_skips_protected_path_before_file_probe(self):
        from across_orchestrator.plugin_manifest import _resolve_status_command

        dev_bin = self.home / "Documents" / "projects" / "across-orchestrator" / "bin"
        env = {
            **self.env,
            "HOME": str(self.home),
            "ACROSS_ORCHESTRATOR_PRODUCT_MODE": "1",
            "PATH": str(dev_bin),
        }

        def guarded_is_file(path):
            if "Documents" in str(path):
                raise AssertionError("protected Orchestrator PATH was probed")
            return False

        with patch("across_orchestrator.plugin_manifest.Path.is_file", guarded_is_file):
            command = _resolve_status_command("across-orchestrator", env)

        self.assertEqual(command, str(self.home / "bin" / "across-orchestrator"))

    def test_product_plugin_status_ignores_protected_orchestrator_command_on_path(self):
        dev_bin = self.home / "Documents" / "projects" / "across-orchestrator" / "bin"
        dev_command = dev_bin / "across-orchestrator"
        dev_bin.mkdir(parents=True)
        dev_command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        dev_command.chmod(0o755)
        self.env["ACROSS_ORCHESTRATOR_PRODUCT_MODE"] = "1"
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = str(dev_bin)

        status_result = self.run_cli("plugin-status", "--json")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        status = json.loads(status_result.stdout)

        self.assertEqual(status["available"], False)
        self.assertEqual(status["command"], str(self.home / "bin" / "across-orchestrator"))
        self.assertNotIn("Documents", json.dumps(status))

    def test_plugin_manifest_declares_hosting_platform_contract(self):
        result = self.run_cli("plugin-manifest", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(result.stdout)

        self.assertEqual(manifest["pluginApiVersion"], "2026-06-10")
        self.assertTrue(manifest["capabilities"]["hostingPlatformAdapters"])
        self.assertTrue(manifest["capabilities"]["agentLoopRuntime"])
        self.assertTrue(manifest["capabilities"]["hostModelDecision"])
        self.assertTrue(manifest["capabilities"]["checkpoints"])
        self.assertTrue(manifest["capabilities"]["humanApproval"])
        self.assertTrue(manifest["capabilities"]["memoryHooks"])
        self.assertEqual(manifest["compatibility"]["requiredHostVersion"], ">=0.6.0")
        self.assertEqual(manifest["lifecycle"]["uninstall"]["args"][0], "plugin-uninstall")
        self.assertTrue(manifest["lifecycle"]["uninstall"]["preservesData"])
        self.assertEqual(manifest["permissions"]["network"][0]["host"], "127.0.0.1")
        self.assertEqual(manifest["entrypoints"]["sidecar"]["pluginManifestPath"], "/.well-known/across-plugin.json")
        self.assertIn("host_model_decision_command", manifest["hostingPlatform"]["hostProvides"])
        self.assertEqual(manifest["protocols"]["http"]["hostModelDecision"], "metadata.model_policy.host_model_command")
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
