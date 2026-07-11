import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class AcrossContextProviderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.project = Path(self.tempdir.name) / "project"
        self.context_home = Path(self.tempdir.name) / "context"
        self.orchestrator_home = Path(self.tempdir.name) / "orchestrator"
        self.project.mkdir()
        self.context_home.mkdir()
        self.orchestrator_home.mkdir()
        self.root = Path(__file__).resolve().parents[1]
        self.context_root = self.root.parent / "across-context"
        self._old_orchestrator_home = os.environ.get("ACROSS_ORCHESTRATOR_HOME")
        self._old_context_home = os.environ.get("ACROSS_CONTEXT_HOME")
        self._old_memory_provider = os.environ.get("ACROSS_ORCHESTRATOR_MEMORY_PROVIDER")
        self._old_context_command = os.environ.get("ACROSS_CONTEXT_COMMAND")
        os.environ["ACROSS_ORCHESTRATOR_HOME"] = str(self.orchestrator_home)
        os.environ["ACROSS_CONTEXT_HOME"] = str(self.context_home)

    def tearDown(self):
        if self._old_orchestrator_home is None:
            os.environ.pop("ACROSS_ORCHESTRATOR_HOME", None)
        else:
            os.environ["ACROSS_ORCHESTRATOR_HOME"] = self._old_orchestrator_home
        if self._old_context_home is None:
            os.environ.pop("ACROSS_CONTEXT_HOME", None)
        else:
            os.environ["ACROSS_CONTEXT_HOME"] = self._old_context_home
        if self._old_memory_provider is None:
            os.environ.pop("ACROSS_ORCHESTRATOR_MEMORY_PROVIDER", None)
        else:
            os.environ["ACROSS_ORCHESTRATOR_MEMORY_PROVIDER"] = self._old_memory_provider
        if self._old_context_command is None:
            os.environ.pop("ACROSS_CONTEXT_COMMAND", None)
        else:
            os.environ["ACROSS_CONTEXT_COMMAND"] = self._old_context_command
        self.tempdir.cleanup()

    def run_context(self, *args):
        if not self.context_root.exists():
            self.skipTest("across-context checkout is not available next to across-orchestrator")
        completed = subprocess.run(
            ["node", "src/cli.js", *args],
            cwd=self.context_root,
            env={**os.environ, "ACROSS_CONTEXT_HOME": str(self.context_home)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return completed

    def test_loop_reads_active_and_pinned_memory_and_writes_pending_candidate_through_across_context(self):
        from across_orchestrator.across_context import AcrossContextMemoryProvider
        from across_orchestrator.agent_loop import AgentLoopAdapters, AgentLoopRuntime

        self.run_context(
            "remember",
            "Agent Loop v2 must reuse Across Context active memory.",
            "--scope",
            "project",
            "--project",
            str(self.project),
            "--status",
            "active",
            "--json",
        )
        self.run_context(
            "remember",
            "Agent Loop v2 must also reuse pinned Across Context memory.",
            "--scope",
            "project",
            "--project",
            str(self.project),
            "--status",
            "pinned",
            "--json",
        )
        self.run_context(
            "remember",
            "Agent Loop v2 must not recall pending memory by default.",
            "--scope",
            "project",
            "--project",
            str(self.project),
            "--status",
            "pending",
            "--json",
        )

        command = ["node", str(self.context_root / "src" / "cli.js")]
        runtime = AgentLoopRuntime(
            adapters=AgentLoopAdapters(
                memory_provider=AcrossContextMemoryProvider(
                    command=command,
                    env={**os.environ, "ACROSS_CONTEXT_HOME": str(self.context_home)},
                )
            )
        )
        loop = runtime.start_loop(
            goal="Use Agent Loop v2 memory policy",
            project_root=str(self.project),
            max_turns=8,
        )

        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.status, "completed")
        memory_step = completed.steps[0]
        self.assertEqual(memory_step.action.type, "memory_search")
        self.assertEqual(memory_step.observation.payload["result_count"], 2)
        memory_payload = json.dumps(memory_step.observation.payload)
        self.assertIn("active memory", memory_payload)
        self.assertIn("pinned Across Context memory", memory_payload)
        self.assertNotIn("pending memory by default", memory_payload)

        pending = json.loads(
            self.run_context(
                "pending",
                "--project",
                str(self.project),
                "--json",
            ).stdout
        )
        self.assertEqual(len(pending), 2)
        loop_pending = [item for item in pending if "Use Agent Loop v2 memory policy" in item["text"]]
        self.assertEqual(len(loop_pending), 1)
        self.assertEqual(loop_pending[0]["status"], "pending")

    def test_default_runtime_uses_across_context_when_memory_provider_env_is_enabled(self):
        from across_orchestrator.agent_loop import AgentLoopRuntime

        self.run_context(
            "remember",
            "Default runtime should read Across Context when enabled.",
            "--scope",
            "project",
            "--project",
            str(self.project),
            "--status",
            "active",
            "--json",
        )
        os.environ["ACROSS_ORCHESTRATOR_MEMORY_PROVIDER"] = "across-context"
        os.environ["ACROSS_CONTEXT_COMMAND"] = f"node {self.context_root / 'src' / 'cli.js'}"

        runtime = AgentLoopRuntime()
        loop = runtime.start_loop(
            goal="Default runtime should read Across Context",
            project_root=str(self.project),
            max_turns=8,
        )
        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.steps[0].observation.payload["provider"], "across-context")
        self.assertEqual(completed.steps[0].observation.payload["result_count"], 1)

    def test_default_runtime_uses_across_context_when_context_command_is_configured(self):
        from across_orchestrator.agent_loop import AgentLoopRuntime

        self.run_context(
            "remember",
            "Configured Context command should activate the default provider.",
            "--scope",
            "project",
            "--project",
            str(self.project),
            "--status",
            "active",
            "--json",
        )
        os.environ.pop("ACROSS_ORCHESTRATOR_MEMORY_PROVIDER", None)
        os.environ["ACROSS_CONTEXT_COMMAND"] = f"node {self.context_root / 'src' / 'cli.js'}"

        runtime = AgentLoopRuntime()
        loop = runtime.start_loop(
            goal="Configured Context command should activate the default provider",
            project_root=str(self.project),
            max_turns=8,
        )
        completed = runtime.run_loop(loop.loop_id)

        self.assertEqual(completed.steps[0].observation.payload["provider"], "across-context")
        self.assertEqual(completed.steps[0].observation.payload["result_count"], 1)
        self.assertEqual(completed.steps[0].checkpoint["adapter"], "AcrossContextMemoryProvider")

    def test_product_mode_does_not_execute_development_checkout_context_command(self):
        from across_orchestrator.across_context import AcrossContextMemoryProvider

        dev_command = Path(self.tempdir.name) / "Documents" / "projects" / "across-context" / "bin" / "across-context"
        marker = Path(self.tempdir.name) / "context-command-ran"
        dev_command.parent.mkdir(parents=True)
        dev_command.write_text(
            "#!/bin/sh\n"
            f"touch {marker}\n"
            "printf '{\"results\":[]}\\n'\n",
            encoding="utf-8",
        )
        dev_command.chmod(0o755)

        provider = AcrossContextMemoryProvider(
            command=[str(dev_command)],
            env={
                **os.environ,
                "HOME": self.tempdir.name,
                "ACROSS_ORCHESTRATOR_PRODUCT_MODE": "1",
                "ACROSS_ORCHESTRATOR_MEMORY_PROVIDER": "across-context",
            },
        )

        result = provider.search(query="boundary", project_root=str(self.project))

        self.assertFalse(marker.exists())
        self.assertEqual(result["provider"], "across-context")
        self.assertEqual(result["error"]["status"], "blocked")
        self.assertEqual(result["error"]["command"], ["<protected-user-path>"])
        self.assertIn("development checkout", result["error"]["warnings"][0])
        self.assertNotIn("Documents", json.dumps(result["error"]))

    def test_product_mode_is_forwarded_to_context_cli_environment(self):
        from across_orchestrator.across_context import AcrossContextMemoryProvider

        provider = AcrossContextMemoryProvider(
            command=["across-context"],
            env={
                **os.environ,
                "HOME": self.tempdir.name,
                "ACROSS_ORCHESTRATOR_PRODUCT_MODE": "1",
            },
        )

        self.assertEqual(provider.env["ACROSS_CONTEXT_PRODUCT_MODE"], "1")


if __name__ == "__main__":
    unittest.main()
