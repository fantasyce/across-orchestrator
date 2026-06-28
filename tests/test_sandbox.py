import tempfile
import unittest
from pathlib import Path


class SandboxPolicyTests(unittest.TestCase):
    def test_evaluate_read_only_policy_allows_allowlisted_command_inside_workspace(self):
        from across_orchestrator.sandbox import evaluate_sandbox_policy

        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            workspace.mkdir()
            result = evaluate_sandbox_policy(
                {
                    "network_policy": "none",
                    "filesystem_policy": "read_only",
                    "workspace_root": str(workspace),
                    "command_allowlist": ["node --version"],
                    "budget": {"max_model_calls": 0},
                    "promotion": {"human_approval_required": True, "merge_release_signing_blocked": True},
                },
                command=["node", "--version"],
                cwd=str(workspace),
            )

        self.assertEqual(result["schema_version"], "across-sandbox-evidence/1.0")
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["execution"]["performed"])
        self.assertEqual(result["policy"]["network_policy"]["mode"], "none")

    def test_blocks_non_allowlisted_command_and_outside_cwd(self):
        from across_orchestrator.sandbox import evaluate_sandbox_policy

        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            outside = Path(tempdir) / "outside"
            workspace.mkdir()
            outside.mkdir()
            result = evaluate_sandbox_policy(
                {
                    "network_policy": "none",
                    "filesystem_policy": "read_only",
                    "workspace_root": str(workspace),
                    "command_allowlist": ["node --version"],
                },
                command=["python", "-c", "print('x')"],
                cwd=str(outside),
            )

        self.assertEqual(result["status"], "blocked")
        self.assertIn("cwd must stay inside workspace_root", result["blocked_reasons"])
        self.assertTrue(any("command is not allowlisted" in item for item in result["blocked_reasons"]))


if __name__ == "__main__":
    unittest.main()
