import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class CommandAgentAdapterTests(unittest.TestCase):
    def test_adapter_passes_streaming_timeout_policy(self):
        from across_orchestrator.adapters import adapter_for

        adapter = adapter_for(
            "worker",
            spec={
                "type": "command",
                "command": ["worker", "run"],
                "timeoutSeconds": 15,
                "refreshTimeoutOnOutput": True,
                "maxWallTimeoutSeconds": 120,
            },
        )
        receipt = {
            "status": "completed",
            "execution": {"timed_out": False, "timeout_kind": None},
            "output": {"stdout": "done\n", "stderr": ""},
        }

        with tempfile.TemporaryDirectory() as tempdir, patch(
            "across_orchestrator.adapters.execute_sandbox_command",
            return_value=receipt,
        ) as execute:
            task = SimpleNamespace(
                project_root=str(Path(tempdir)),
                to_dict=lambda: {"project_root": str(Path(tempdir))},
            )
            subtask = SimpleNamespace(path="result.txt", goal="run")
            result = adapter.run(task, subtask)

        self.assertEqual(result["message"], "done")
        kwargs = execute.call_args.kwargs
        self.assertEqual(kwargs["timeout_seconds"], 15)
        self.assertTrue(kwargs["refresh_timeout_on_output"])
        self.assertEqual(kwargs["max_wall_timeout_seconds"], 120)
        self.assertEqual(
            execute.call_args.args[0]["execution"],
            {
                "timeout_seconds": 15,
                "refresh_timeout_on_output": True,
                "max_wall_timeout_seconds": 120,
            },
        )


if __name__ == "__main__":
    unittest.main()
