import json
import os
import shutil
import subprocess
import sys
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

    def test_loop_reads_active_memory_and_writes_pending_candidate_through_across_context(self):
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
        self.assertEqual(memory_step.observation.payload["result_count"], 1)
        self.assertIn("Agent Loop v2", json.dumps(memory_step.observation.payload))

        pending = json.loads(
            self.run_context(
                "pending",
                "--project",
                str(self.project),
                "--json",
            ).stdout
        )
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "pending")
        self.assertIn("Use Agent Loop v2 memory policy", pending[0]["text"])

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


if __name__ == "__main__":
    unittest.main()
