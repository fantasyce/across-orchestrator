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

    def test_product_source_tree_does_not_contain_aaa_namespace(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(__file__).resolve().parents[1]
            self.assertFalse((root / "src" / "across_agents_assistant").exists())
            self.assertFalse((root / "tests" / "parity_fixtures" / "across_agents_assistant").exists())
            self.assertFalse((root / "tests" / "parity").exists())


if __name__ == "__main__":
    unittest.main()
