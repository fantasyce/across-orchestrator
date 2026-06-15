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

    def test_tilde_overrides_expand_with_passed_home(self):
        with tempfile.TemporaryDirectory() as tempdir:
            from across_orchestrator.paths import ecosystem_home
            from across_orchestrator.store import default_home

            home = Path(tempdir).resolve()
            env = {
                "HOME": str(home),
                "ACROSS_HOME": "~/custom-across",
                "ACROSS_ORCHESTRATOR_HOME": "~/custom-orchestrator",
            }

            self.assertEqual(ecosystem_home(env), home / "custom-across")
            self.assertEqual(default_home(env), home / "custom-orchestrator")

    def test_product_mode_ignores_protected_ecosystem_runtime_roots(self):
        with tempfile.TemporaryDirectory() as tempdir:
            from across_orchestrator.paths import (
                component_data_home,
                ecosystem_bin_dir,
                ecosystem_home,
                plugin_root,
            )
            from across_orchestrator.store import default_home

            home = Path(tempdir).resolve()
            env = {
                "HOME": str(home),
                "ACROSS_ORCHESTRATOR_PRODUCT_MODE": "1",
                "ACROSS_HOME": str(home / "Documents" / "projects" / "across"),
                "ACROSS_PLUGIN_HOME": str(home / "Documents" / "projects" / "plugins"),
                "ACROSS_BIN_HOME": str(home / "Documents" / "projects" / "bin"),
                "ACROSS_ORCHESTRATOR_HOME": str(home / "Documents" / "projects" / "orchestrator-data"),
            }

            self.assertEqual(ecosystem_home(env), home / ".across")
            self.assertEqual(plugin_root(env), home / ".across" / "plugins")
            self.assertEqual(ecosystem_bin_dir(env), home / ".across" / "bin")
            self.assertEqual(component_data_home(env=env), home / ".across" / "data" / "across-orchestrator")
            self.assertEqual(default_home(env), home / ".across" / "data" / "across-orchestrator")

    def test_product_mode_preserves_similarly_named_user_directories(self):
        with tempfile.TemporaryDirectory() as tempdir:
            from across_orchestrator.paths import ecosystem_home

            home = Path(tempdir).resolve()
            adjacent = home / "DocumentsArchive" / "across"
            env = {
                "HOME": str(home),
                "ACROSS_ORCHESTRATOR_PRODUCT_MODE": "1",
                "ACROSS_HOME": str(adjacent),
            }

            self.assertEqual(ecosystem_home(env), adjacent.resolve())

    def test_developer_mode_preserves_protected_ecosystem_runtime_roots(self):
        with tempfile.TemporaryDirectory() as tempdir:
            from across_orchestrator.paths import ecosystem_bin_dir, ecosystem_home, plugin_root
            from across_orchestrator.store import default_home

            home = Path(tempdir)
            env = {
                "HOME": str(home),
                "ACROSS_ORCHESTRATOR_PRODUCT_MODE": "1",
                "ACROSS_ORCHESTRATOR_DEVELOPER_MODE": "1",
                "ACROSS_HOME": str(home / "Documents" / "projects" / "across"),
                "ACROSS_PLUGIN_HOME": str(home / "Documents" / "projects" / "plugins"),
                "ACROSS_BIN_HOME": str(home / "Documents" / "projects" / "bin"),
                "ACROSS_ORCHESTRATOR_HOME": str(home / "Documents" / "projects" / "orchestrator-data"),
            }

            self.assertEqual(ecosystem_home(env), Path(env["ACROSS_HOME"]).resolve())
            self.assertEqual(plugin_root(env), Path(env["ACROSS_PLUGIN_HOME"]).resolve())
            self.assertEqual(ecosystem_bin_dir(env), Path(env["ACROSS_BIN_HOME"]).resolve())
            self.assertEqual(default_home(env), Path(env["ACROSS_ORCHESTRATOR_HOME"]).resolve())

    def test_product_source_tree_does_not_contain_aaa_namespace(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(__file__).resolve().parents[1]
            self.assertFalse((root / "src" / "across_agents_assistant").exists())
            self.assertFalse((root / "tests" / "parity_fixtures" / "across_agents_assistant").exists())
            self.assertFalse((root / "tests" / "parity").exists())


if __name__ == "__main__":
    unittest.main()
