import hashlib
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path


class SandboxPolicyTests(unittest.TestCase):
    def test_local_provider_only_advertises_kernel_enforced_network_modes(self):
        from across_orchestrator.sandbox import LocalWorkspaceSandboxProvider

        provider = LocalWorkspaceSandboxProvider()
        capabilities = provider.capabilities()

        expected = (
            ["none", "adapter_scoped"]
            if capabilities["native_policy_backend"] == "macos-sandbox-exec"
            else ["none"]
        )
        self.assertEqual(capabilities["network_modes"], expected)
        self.assertNotIn("allowlist", capabilities["network_modes"])
        self.assertNotIn("unrestricted_requires_approval", capabilities["network_modes"])

        expected_filesystem_modes = (
            sorted({"read_only", "run_scoped", "candidate_workspace_only", "allowlist"})
            if capabilities["native_policy_backend"] == "macos-sandbox-exec"
            else ["run_scoped"]
        )
        self.assertEqual(capabilities["filesystem_modes"], expected_filesystem_modes)
        if capabilities["native_policy_backend"] != "macos-sandbox-exec":
            self.assertNotIn("read_only", capabilities["filesystem_modes"])

    def test_network_profiles_keep_none_as_default_and_scope_outbound_to_adapter_mode(self):
        from across_orchestrator.sandbox import (
            SandboxExecutionRequest,
            _macos_sandbox_profile,
            normalize_sandbox_policy,
        )

        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir).resolve()

            def profile_for(policy):
                normalized = normalize_sandbox_policy(
                    {**policy, "workspace_root": str(workspace)}
                )
                request = SandboxExecutionRequest(
                    argv=(sys.executable, "--version"),
                    cwd=workspace,
                    workspace_root=workspace,
                    policy=normalized,
                    timeout_seconds=1,
                    max_output_bytes=1024,
                )
                return normalized, _macos_sandbox_profile(request)

            default_policy, default_profile = profile_for({})
            scoped_policy, scoped_profile = profile_for(
                {"network_policy": "adapter_scoped"}
            )

        self.assertEqual(default_policy["network_policy"]["mode"], "none")
        self.assertIn("(deny network*)", default_profile)
        self.assertNotIn("(allow network-outbound)", default_profile)
        self.assertEqual(scoped_policy["network_policy"]["mode"], "adapter_scoped")
        self.assertIn("(allow network-outbound)", scoped_profile)
        self.assertNotIn("(allow network-inbound)", scoped_profile)

    @unittest.skipUnless(
        sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file(),
        "requires the macOS sandbox-exec kernel policy backend",
    )
    def test_adapter_scoped_allows_localhost_outbound_while_none_blocks_it(self):
        from across_orchestrator.sandbox import execute_sandbox_command

        with tempfile.TemporaryDirectory() as tempdir, socket.socket() as listener:
            workspace = Path(tempdir).resolve()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            command = [
                sys.executable,
                "-c",
                (
                    "import socket; "
                    f"connection = socket.create_connection(('127.0.0.1', {port}), timeout=1); "
                    "connection.close(); print('connected')"
                ),
            ]
            base_policy = {
                "filesystem_policy": "read_only",
                "workspace_root": str(workspace),
                "command_allowlist": [command],
            }

            denied = execute_sandbox_command(
                {**base_policy, "network_policy": "none"},
                command=command,
                cwd=str(workspace),
            )
            allowed = execute_sandbox_command(
                {**base_policy, "network_policy": "adapter_scoped"},
                command=command,
                cwd=str(workspace),
            )

        self.assertEqual(denied["status"], "failed", denied)
        self.assertEqual(denied["policy"]["network_mode"], "none")
        self.assertEqual(denied["enforcement"]["network_policy"], "kernel_enforced")
        self.assertEqual(allowed["status"], "completed", allowed)
        self.assertEqual(allowed["output"]["stdout"], "connected\n")
        self.assertEqual(allowed["policy"]["network_mode"], "adapter_scoped")
        self.assertEqual(allowed["enforcement"]["network_policy"], "kernel_enforced")

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

    def test_execution_blocks_non_allowlisted_command_and_outside_cwd(self):
        from across_orchestrator.sandbox import execute_sandbox_command

        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir) / "workspace"
            outside = Path(tempdir) / "outside"
            workspace.mkdir()
            outside.mkdir()
            result = execute_sandbox_command(
                {
                    "workspace_root": str(workspace),
                    "command_allowlist": ["printf allowed"],
                },
                command=[sys.executable, "-c", "print('blocked')"],
                cwd=str(outside),
            )

        self.assertEqual(result["schema_version"], "across-sandbox-execution/1.0")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["execution"]["performed"])
        self.assertIn("cwd must stay inside workspace_root", result["blocked_reasons"])
        self.assertNotIn("print('blocked')", json.dumps(result))

    def test_local_provider_executes_argv_without_shell(self):
        from across_orchestrator.sandbox import execute_sandbox_command

        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir)
            command = [sys.executable, "-c", "print('sandbox-ok')"]
            result = execute_sandbox_command(
                {
                    "network_policy": "none",
                    "filesystem_policy": "run_scoped",
                    "workspace_root": str(workspace),
                    "command_allowlist": [command],
                },
                command=command,
                cwd=str(workspace),
            )

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["execution"]["exit_code"], 0)
        self.assertEqual(result["output"]["stdout"], "sandbox-ok\n")
        self.assertTrue(result["enforcement"]["argv_without_shell"])
        self.assertNotIn("argv", result["command"])
        receipt_hash = result["receipt_sha256"]
        unhashed = dict(result)
        unhashed.pop("receipt_sha256")
        canonical = json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        self.assertEqual(receipt_hash, hashlib.sha256(canonical).hexdigest())

    def test_local_provider_times_out_and_terminates_process(self):
        from across_orchestrator.sandbox import execute_sandbox_command

        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir)
            command = [sys.executable, "-c", "import time; time.sleep(10)"]
            result = execute_sandbox_command(
                {
                    "filesystem_policy": "run_scoped",
                    "workspace_root": str(workspace),
                    "command_allowlist": [command],
                },
                command=command,
                cwd=str(workspace),
                timeout_seconds=0.05,
            )

        self.assertEqual(result["status"], "timed_out", result)
        self.assertTrue(result["execution"]["performed"])
        self.assertTrue(result["execution"]["timed_out"])
        self.assertEqual(result["execution"]["timeout_kind"], "max_wall")
        self.assertFalse(result["execution"]["refresh_timeout_on_output"])

    def test_local_provider_uses_idle_timeout_when_output_refresh_is_enabled(self):
        from across_orchestrator.sandbox import execute_sandbox_command

        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir)
            command = [sys.executable, "-c", "import time; time.sleep(10)"]
            result = execute_sandbox_command(
                {
                    "filesystem_policy": "run_scoped",
                    "workspace_root": str(workspace),
                    "command_allowlist": [command],
                    "execution": {
                        "timeout_seconds": 0.12,
                        "refresh_timeout_on_output": True,
                        "max_wall_timeout_seconds": 1,
                    },
                },
                command=command,
                cwd=str(workspace),
            )

        self.assertEqual(result["status"], "timed_out", result)
        self.assertTrue(result["execution"]["timed_out"])
        self.assertEqual(result["execution"]["timeout_kind"], "idle")

    def test_local_provider_output_refreshes_idle_timeout(self):
        from across_orchestrator.sandbox import execute_sandbox_command

        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir)
            command = [
                sys.executable,
                "-c",
                (
                    "import sys, time; "
                    "[(print(i, file=sys.stderr, flush=True), time.sleep(0.04)) for i in range(6)]"
                ),
            ]
            result = execute_sandbox_command(
                {
                    "filesystem_policy": "run_scoped",
                    "workspace_root": str(workspace),
                    "command_allowlist": [command],
                    "execution": {
                        "timeout_seconds": 0.12,
                        "refresh_timeout_on_output": True,
                        "max_wall_timeout_seconds": 1,
                    },
                },
                command=command,
                cwd=str(workspace),
            )

        self.assertEqual(result["status"], "completed", result)
        self.assertFalse(result["execution"]["timed_out"])
        self.assertIsNone(result["execution"]["timeout_kind"])
        self.assertEqual(result["output"]["stderr"], "0\n1\n2\n3\n4\n5\n")

    def test_local_provider_max_wall_timeout_caps_continuous_output(self):
        from across_orchestrator.sandbox import execute_sandbox_command

        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir)
            command = [
                sys.executable,
                "-c",
                (
                    "import time; i = 0\n"
                    "while True:\n"
                    " print(i, flush=True); i += 1; time.sleep(0.03)"
                ),
            ]
            result = execute_sandbox_command(
                {
                    "filesystem_policy": "run_scoped",
                    "workspace_root": str(workspace),
                    "command_allowlist": [command],
                    "execution": {
                        "timeout_seconds": 0.12,
                        "refresh_timeout_on_output": True,
                        "max_wall_timeout_seconds": 0.25,
                    },
                },
                command=command,
                cwd=str(workspace),
            )

        self.assertEqual(result["status"], "timed_out", result)
        self.assertEqual(result["execution"]["timeout_kind"], "max_wall")
        self.assertGreater(result["output"]["stdout_bytes"], 0)

    def test_default_timeout_remains_fixed_even_with_continuous_output(self):
        from across_orchestrator.sandbox import execute_sandbox_command

        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir)
            command = [
                sys.executable,
                "-c",
                (
                    "import time; i = 0\n"
                    "while True:\n"
                    " print(i, flush=True); i += 1; time.sleep(0.03)"
                ),
            ]
            result = execute_sandbox_command(
                {
                    "filesystem_policy": "run_scoped",
                    "workspace_root": str(workspace),
                    "command_allowlist": [command],
                },
                command=command,
                cwd=str(workspace),
                timeout_seconds=0.12,
            )

        self.assertEqual(result["status"], "timed_out", result)
        self.assertEqual(result["execution"]["timeout_kind"], "max_wall")
        self.assertGreater(result["output"]["stdout_bytes"], 0)

    def test_max_wall_timeout_must_be_bounded(self):
        from across_orchestrator.sandbox import execute_sandbox_command

        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir)
            command = [sys.executable, "-c", "print('ok')"]
            for invalid_timeout in (3600.01, float("inf"), float("nan")):
                with self.subTest(invalid_timeout=invalid_timeout), self.assertRaisesRegex(
                    ValueError,
                    "max_wall_timeout_seconds",
                ):
                    execute_sandbox_command(
                        {
                            "workspace_root": str(workspace),
                            "command_allowlist": [command],
                            "execution": {"max_wall_timeout_seconds": invalid_timeout},
                        },
                        command=command,
                        cwd=str(workspace),
                    )

    def test_local_provider_truncates_output_while_recording_total_bytes(self):
        from across_orchestrator.sandbox import execute_sandbox_command

        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir)
            command = [sys.executable, "-c", "print('x' * 10000)"]
            result = execute_sandbox_command(
                {
                    "filesystem_policy": "run_scoped",
                    "workspace_root": str(workspace),
                    "command_allowlist": [command],
                },
                command=command,
                cwd=str(workspace),
                max_output_bytes=128,
            )

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(result["output"]["stdout"].encode("utf-8")), 128)
        self.assertGreater(result["output"]["stdout_bytes"], 128)
        self.assertTrue(result["output"]["stdout_truncated"])

    def test_local_provider_enforces_workspace_write_policy(self):
        from across_orchestrator.sandbox import execute_sandbox_command

        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir)
            target = workspace / "created.txt"
            command = [sys.executable, "-c", "from pathlib import Path; Path('created.txt').write_text('ok')"]
            base_policy = {
                "network_policy": "none",
                "workspace_root": str(workspace),
                "command_allowlist": [command],
            }
            blocked = execute_sandbox_command(
                {**base_policy, "filesystem_policy": "read_only"},
                command=command,
                cwd=str(workspace),
            )
            written = execute_sandbox_command(
                {**base_policy, "filesystem_policy": "run_scoped"},
                command=command,
                cwd=str(workspace),
            )

            expected_blocked_status = "failed" if sys.platform == "darwin" else "blocked"
            self.assertEqual(blocked["status"], expected_blocked_status, blocked)
            if sys.platform != "darwin":
                self.assertFalse(blocked["execution"]["performed"])
            self.assertEqual(written["status"], "completed", written)
            self.assertEqual(target.read_text(encoding="utf-8"), "ok")

    def test_runtime_state_roots_allow_only_the_validated_directory(self):
        from across_orchestrator.sandbox import execute_sandbox_command

        with tempfile.TemporaryDirectory() as tempdir:
            base = Path(tempdir).resolve()
            workspace = base / "workspace"
            runtime_state = base / "runtime-state"
            sibling = base / "sibling"
            workspace.mkdir()
            runtime_state.mkdir()
            sibling.mkdir()
            allowed_target = runtime_state / "state.json"
            denied_target = sibling / "state.json"
            command = [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(allowed_target)!r}).write_text('ok'); "
                    f"Path({str(denied_target)!r}).write_text('no')"
                ),
            ]
            result = execute_sandbox_command(
                {
                    "network_policy": "none",
                    "filesystem_policy": {
                        "mode": "read_only",
                        "runtime_state_roots": [str(runtime_state)],
                    },
                    "workspace_root": str(workspace),
                    "command_allowlist": [command],
                },
                command=command,
                cwd=str(workspace),
            )

            if sys.platform == "darwin":
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(allowed_target.read_text(encoding="utf-8"), "ok")
                self.assertFalse(denied_target.exists())
                self.assertEqual(result["policy"]["runtime_state_roots"]["count"], 1)
                self.assertEqual(len(result["policy"]["runtime_state_roots"]["sha256"]), 64)
            else:
                self.assertEqual(result["status"], "blocked", result)
                self.assertFalse(result["execution"]["performed"])
            self.assertNotIn(str(runtime_state), json.dumps(result, sort_keys=True))

    def test_runtime_state_roots_reject_unsafe_or_ambiguous_paths(self):
        from across_orchestrator.sandbox import evaluate_sandbox_policy

        with tempfile.TemporaryDirectory() as tempdir:
            base = Path(tempdir).resolve()
            workspace = base / "workspace"
            runtime_state = base / "runtime-state"
            nested_state = runtime_state / "nested"
            workspace.mkdir()
            nested_state.mkdir(parents=True)
            policy = {
                "workspace_root": str(workspace),
                "filesystem_policy": {
                    "mode": "read_only",
                    "runtime_state_roots": [str(runtime_state), str(nested_state)],
                },
            }
            overlap = evaluate_sandbox_policy(policy)
            workspace_ancestor = evaluate_sandbox_policy(
                {
                    **policy,
                    "filesystem_policy": {
                        "mode": "read_only",
                        "runtime_state_roots": [str(base)],
                    },
                }
            )

        self.assertEqual(overlap["status"], "blocked")
        self.assertIn("runtime_state_roots cannot overlap", overlap["blocked_reasons"])
        self.assertEqual(workspace_ancestor["status"], "blocked")
        self.assertIn("runtime_state_roots cannot contain workspace_root", workspace_ancestor["blocked_reasons"])

    def test_runtime_state_roots_require_an_array_of_absolute_existing_directories(self):
        from across_orchestrator.sandbox import evaluate_sandbox_policy, execute_sandbox_command

        with self.assertRaisesRegex(ValueError, "must be an array"):
            evaluate_sandbox_policy(
                {"filesystem_policy": {"runtime_state_roots": "/tmp/runtime-state"}}
            )

        result = evaluate_sandbox_policy(
            {"filesystem_policy": {"runtime_state_roots": ["relative/runtime-state"]}}
        )
        self.assertEqual(result["status"], "blocked")
        self.assertIn(
            "runtime_state_roots entries must be absolute directory paths",
            result["blocked_reasons"],
        )

        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir).resolve()
            missing = workspace.parent / "private-runtime-state-does-not-exist"
            command = [sys.executable, "-c", "print('not-run')"]
            blocked = execute_sandbox_command(
                {
                    "workspace_root": str(workspace),
                    "filesystem_policy": {
                        "mode": "read_only",
                        "runtime_state_roots": [str(missing)],
                    },
                    "command_allowlist": [command],
                },
                command=command,
                cwd=str(workspace),
            )

        serialized = json.dumps(blocked, sort_keys=True)
        self.assertEqual(blocked["status"], "blocked")
        self.assertNotIn(str(missing), serialized)
        self.assertIn("runtime_state_roots entries must be existing directories", serialized)

    def test_runtime_state_files_grant_only_exact_literal_file(self):
        from across_orchestrator.sandbox import execute_sandbox_command

        with tempfile.TemporaryDirectory() as tempdir:
            base = Path(tempdir).resolve()
            workspace = base / "workspace"
            runtime_state = base / "runtime-state"
            workspace.mkdir()
            runtime_state.mkdir()
            allowed = runtime_state / "allowed.json"
            denied = runtime_state / "denied.json"
            allowed.write_text("before", encoding="utf-8")
            denied.write_text("before", encoding="utf-8")
            command = [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    f"Path({str(allowed)!r}).write_text('allowed'); "
                    f"Path({str(denied)!r}).write_text('denied')"
                ),
            ]
            result = execute_sandbox_command(
                {
                    "network_policy": "none",
                    "filesystem_policy": {
                        "mode": "read_only",
                        "runtime_state_files": [str(allowed)],
                    },
                    "workspace_root": str(workspace),
                    "command_allowlist": [command],
                },
                command=command,
                cwd=str(workspace),
            )

            if sys.platform == "darwin":
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(allowed.read_text(encoding="utf-8"), "allowed")
                self.assertEqual(denied.read_text(encoding="utf-8"), "before")
                summary = result["policy"]["runtime_state_files"]
                self.assertEqual(summary["count"], 1)
                self.assertEqual(len(summary["sha256"]), 64)
            else:
                self.assertEqual(result["status"], "blocked", result)
                self.assertFalse(result["execution"]["performed"])
            self.assertNotIn(str(allowed), json.dumps(result, sort_keys=True))

    def test_runtime_state_files_profile_uses_literal_without_parent_subpath(self):
        from across_orchestrator.sandbox import (
            SandboxExecutionRequest,
            _macos_sandbox_profile,
            normalize_sandbox_policy,
        )

        with tempfile.TemporaryDirectory() as tempdir:
            base = Path(tempdir).resolve()
            workspace = base / "workspace"
            runtime_state = base / "runtime-state"
            workspace.mkdir()
            runtime_state.mkdir()
            state_file = runtime_state / 'state"file.json'
            state_file.write_text("{}", encoding="utf-8")
            policy = normalize_sandbox_policy(
                {
                    "network_policy": "none",
                    "filesystem_policy": {
                        "mode": "read_only",
                        "runtime_state_files": [str(state_file)],
                    },
                    "workspace_root": str(workspace),
                }
            )
            request = SandboxExecutionRequest(
                argv=(sys.executable, "--version"),
                cwd=workspace,
                workspace_root=workspace,
                policy=policy,
                timeout_seconds=1,
                max_output_bytes=1024,
            )
            profile = _macos_sandbox_profile(request)

        quoted_file = str(state_file).replace('"', '\\"')
        self.assertIn(f'(allow file-write* (literal "{quoted_file}"))', profile)
        self.assertNotIn(f'(subpath "{runtime_state}")', profile)

    def test_runtime_state_files_expand_home_paths(self):
        from across_orchestrator.sandbox import evaluate_sandbox_policy

        home_state_file = Path.home() / ".kimi-code" / "session_index.jsonl"
        if not home_state_file.is_file():
            self.skipTest("Kimi session index is not installed")

        result = evaluate_sandbox_policy(
            {"filesystem_policy": {"runtime_state_files": ["~/.kimi-code/session_index.jsonl"]}}
        )

        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["policy"]["filesystem_policy"]["runtime_state_files"], [str(home_state_file.resolve())])

    def test_runtime_state_files_reject_unsafe_or_ambiguous_paths(self):
        from across_orchestrator.sandbox import evaluate_sandbox_policy

        with tempfile.TemporaryDirectory() as tempdir:
            base = Path(tempdir).resolve()
            workspace = base / "workspace"
            runtime_state = base / "runtime-state"
            workspace.mkdir()
            runtime_state.mkdir()
            state_file = runtime_state / "state.json"
            state_file.write_text("{}", encoding="utf-8")
            base_policy = {
                "workspace_root": str(workspace),
                "filesystem_policy": {"mode": "read_only"},
            }

            relative = evaluate_sandbox_policy(
                {
                    **base_policy,
                    "filesystem_policy": {"runtime_state_files": ["state.json"]},
                }
            )
            missing = evaluate_sandbox_policy(
                {
                    **base_policy,
                    "filesystem_policy": {"runtime_state_files": [str(runtime_state / "missing.json")]},
                }
            )
            directory = evaluate_sandbox_policy(
                {
                    **base_policy,
                    "filesystem_policy": {"runtime_state_files": [str(runtime_state)]},
                }
            )
            filesystem_root = evaluate_sandbox_policy(
                {
                    **base_policy,
                    "filesystem_policy": {"runtime_state_files": [str(Path(base.anchor))]},
                }
            )
            symlink = runtime_state / "state-link.json"
            symlink.symlink_to(state_file)
            linked = evaluate_sandbox_policy(
                {
                    **base_policy,
                    "filesystem_policy": {"runtime_state_files": [str(symlink)]},
                }
            )
            hardlink = runtime_state / "state-hardlink.json"
            os.link(state_file, hardlink)
            hardlink_overlap = evaluate_sandbox_policy(
                {
                    **base_policy,
                    "filesystem_policy": {
                        "runtime_state_files": [str(state_file), str(hardlink)]
                    },
                }
            )
            duplicate = evaluate_sandbox_policy(
                {
                    **base_policy,
                    "filesystem_policy": {"runtime_state_files": [str(state_file), str(state_file)]},
                }
            )
            root_overlap = evaluate_sandbox_policy(
                {
                    **base_policy,
                    "filesystem_policy": {
                        "runtime_state_roots": [str(runtime_state)],
                        "runtime_state_files": [str(state_file)],
                    },
                }
            )

        self.assertIn("runtime_state_files entries must be absolute file paths", relative["blocked_reasons"])
        self.assertIn("runtime_state_files entries must be existing regular files", missing["blocked_reasons"])
        self.assertIn("runtime_state_files entries must be existing regular files", directory["blocked_reasons"])
        self.assertIn("runtime_state_files cannot include a filesystem root", filesystem_root["blocked_reasons"])
        self.assertIn("runtime_state_files entries must be existing regular files", linked["blocked_reasons"])
        self.assertIn("runtime_state_files cannot overlap", hardlink_overlap["blocked_reasons"])
        self.assertIn("runtime_state_files cannot overlap", duplicate["blocked_reasons"])
        self.assertIn("runtime_state_files cannot overlap runtime_state_roots", root_overlap["blocked_reasons"])

    def test_runtime_state_files_require_an_array_and_have_a_count_limit(self):
        from across_orchestrator.sandbox import MAX_RUNTIME_STATE_FILES, evaluate_sandbox_policy

        with self.assertRaisesRegex(ValueError, "must be an array"):
            evaluate_sandbox_policy(
                {"filesystem_policy": {"runtime_state_files": "/tmp/state.json"}}
            )

        with self.assertRaisesRegex(ValueError, f"at most {MAX_RUNTIME_STATE_FILES}"):
            evaluate_sandbox_policy(
                {
                    "filesystem_policy": {
                        "runtime_state_files": [f"/tmp/state-{index}.json" for index in range(MAX_RUNTIME_STATE_FILES + 1)]
                    }
                }
            )


if __name__ == "__main__":
    unittest.main()
