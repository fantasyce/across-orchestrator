import os
import tempfile
import unittest
from pathlib import Path


class PathTests(unittest.TestCase):
    def test_default_paths_live_under_across_home_namespaces(self):
        with tempfile.TemporaryDirectory() as tempdir:
            from across_orchestrator.paths import (
                component_data_home,
                ecosystem_bin_dir,
                ecosystem_home,
                plugin_root,
                run_home,
            )

            env = {"ACROSS_HOME": tempdir}

            self.assertEqual(ecosystem_home(env), Path(tempdir).resolve())
            self.assertEqual(component_data_home(env=env), Path(tempdir).resolve() / "data" / "across-orchestrator")
            self.assertEqual(plugin_root(env), Path(tempdir).resolve() / "plugins")
            self.assertEqual(ecosystem_bin_dir(env), Path(tempdir).resolve() / "bin")
            self.assertEqual(run_home(env=env), Path(tempdir).resolve() / "run" / "across-orchestrator")

    def test_across_orchestrator_home_remains_explicit_state_override(self):
        with tempfile.TemporaryDirectory() as tempdir:
            from across_orchestrator.store import default_home

            override = Path(tempdir) / "override"
            env = {"ACROSS_HOME": str(Path(tempdir) / "across"), "ACROSS_ORCHESTRATOR_HOME": str(override)}

            self.assertEqual(default_home(env), override.resolve())

    def test_app_grade_compat_paths_do_not_use_legacy_across_agents_home(self):
        with tempfile.TemporaryDirectory() as tempdir:
            from across_agents_assistant.paths import app_home

            old_across_home = os.environ.get("ACROSS_HOME")
            old_agents_home = os.environ.get("ACROSS_AGENTS_HOME")
            os.environ["ACROSS_HOME"] = tempdir
            os.environ.pop("ACROSS_AGENTS_HOME", None)
            try:
                self.assertEqual(
                    app_home(),
                    Path(tempdir).resolve()
                    / "data"
                    / "across-orchestrator"
                    / "compat"
                    / "across-agents-assistant",
                )
            finally:
                if old_across_home is None:
                    os.environ.pop("ACROSS_HOME", None)
                else:
                    os.environ["ACROSS_HOME"] = old_across_home
                if old_agents_home is None:
                    os.environ.pop("ACROSS_AGENTS_HOME", None)
                else:
                    os.environ["ACROSS_AGENTS_HOME"] = old_agents_home


if __name__ == "__main__":
    unittest.main()
