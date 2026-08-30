import json
import tempfile
from pathlib import Path
from unittest import TestCase, main, mock


class EvidenceReceiptTests(TestCase):
    def test_git_commit_sha_reads_linked_worktree_metadata_without_spawning_git(self):
        from across_orchestrator.evidence import _git_commit_sha

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            common = root / "repo" / ".git"
            admin = common / "worktrees" / "acceptance"
            worktree = root / "acceptance"
            nested = worktree / "fixture"
            admin.mkdir(parents=True)
            nested.mkdir(parents=True)
            (worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
            (admin / "HEAD").write_text("ref: refs/heads/codex/goal-contract-v1\n", encoding="utf-8")
            (admin / "commondir").write_text("../..\n", encoding="utf-8")
            ref = common / "refs" / "heads" / "codex" / "goal-contract-v1"
            ref.parent.mkdir(parents=True)
            ref.write_text("a" * 40 + "\n", encoding="utf-8")

            with mock.patch("across_orchestrator.evidence.subprocess.run") as run:
                self.assertEqual(_git_commit_sha(worktree), "a" * 40)

            run.assert_not_called()

            with mock.patch("across_orchestrator.evidence.subprocess.run") as run:
                self.assertEqual(_git_commit_sha(nested), "")

            run.assert_not_called()

    def test_goal_evidence_binding_verifies_receipt_revision_artifacts_and_validator(self):
        from across_orchestrator.evidence import bind_evidence_to_criteria
        from across_orchestrator.worker_protocol import payload_hash

        receipt = {
            "schema_version": "across-worker-evidence/1.0",
            "run_id": "run-1",
            "job_id": "job-1",
            "attempt": 2,
            "lease_id": "lease-1",
            "goal_id": "goal-1",
            "goal_revision": 3,
            "goal_node_id": "goal-node-build",
            "criterion_ids": ["criterion-tests"],
            "input_fingerprint": "a" * 64,
            "terminal_state": "completed",
            "artifacts": [{"artifact_id": "artifact-1", "sha256": "b" * 64}],
            "quality_gates": {"tests": "passed"},
        }
        receipt["receipt_hash"] = payload_hash(receipt)
        binding = {
            "schema_version": "across-goal-evidence-binding/1.0",
            "evidence_id": "evidence-1",
            "criterion_ids": ["criterion-tests"],
            "artifact_digests": {"artifact-1": "b" * 64},
        }
        receipt["task_id"] = "task-1"
        receipt["receipt_hash"] = payload_hash({key: value for key, value in receipt.items() if key != "receipt_hash"})
        authority = {
            "goal_id": "goal-1",
            "goal_revision": 3,
            "task_id": "task-1",
            "run_id": "run-1",
            "job_id": "job-1",
            "attempt": 2,
            "lease_id": "lease-1",
            "lease_state": "terminal_valid",
            "input_fingerprint": "a" * 64,
            "registered_validator_ids": ["quality-gate:tests"],
            "validator_results": {
                "criterion-tests": {
                    "validator_id": "quality-gate:tests",
                    "method": "host_quality_gate",
                    "status": "passed",
                }
            },
        }
        result = bind_evidence_to_criteria(receipt, binding, authority=authority)
        self.assertEqual(result["trust_state"], "verified")
        self.assertEqual(result["verdict"], "verified")
        self.assertEqual(result["receipt_hash"], receipt["receipt_hash"])

        for mutation in (
            lambda value: value[0].update(artifact_digests={"artifact-1": "c" * 64}),
            lambda value: value[1].update(goal_revision=2),
            lambda value: value[1].update(lease_state="expired"),
            lambda value: value[1].update(task_id="task-foreign"),
            lambda value: value[1].update(validator_results={"criterion-tests": {"validator_id": "self_report", "method": "worker", "status": "passed"}}),
        ):
            invalid_binding = dict(binding)
            invalid_authority = {**authority, "validator_results": dict(authority["validator_results"])}
            mutation((invalid_binding, invalid_authority))
            with self.assertRaises(ValueError):
                bind_evidence_to_criteria(receipt, invalid_binding, authority=invalid_authority)

    def test_hash_is_stable_and_payload_is_secret_free(self):
        from across_orchestrator.evidence import build_evidence_receipt

        with tempfile.TemporaryDirectory() as tempdir:
            payload = {
                "workspace": {
                    "root": tempdir,
                    "commit_sha": "a" * 40,
                    "workspace_id": "candidate-1",
                },
                "sandbox_receipt": {
                    "schema_version": "across-sandbox-execution/1.0",
                    "status": "completed",
                    "environment": {"API_TOKEN": "do-not-keep-this"},
                    "output": {
                        "stdout": "sk-abcdefghijklmnop",
                        "stderr": "",
                        "stdout_bytes": 19,
                    },
                },
                "validations": [{"id": "tests", "status": "passed", "api_key": "do-not-keep-this"}],
                "artifacts": [{"path": str(Path(tempdir) / "report.json"), "content": "private report body"}],
                "provenance": {"producer": "unit-test", "authorization": "Bearer do-not-keep-this"},
            }
            first = build_evidence_receipt(payload)
            second = build_evidence_receipt(payload)

        self.assertEqual(first["schema_version"], "across-evidence-receipt/1.0")
        self.assertEqual(first["verdict"], "needs_review")
        self.assertEqual(first["evidence_sha256"], second["evidence_sha256"])
        self.assertEqual(first, second)
        self.assertEqual(first["artifacts"][0]["path"], "report.json")
        serialized = json.dumps(first, sort_keys=True)
        self.assertNotIn("do-not-keep-this", serialized)
        self.assertNotIn("sk-abcdefghijklmnop", serialized)
        self.assertNotIn(tempdir, serialized)
        self.assertNotIn("private report body", serialized)
        self.assertIn("stdout_sha256", first["sandbox_receipt"]["output"])
        self.assertEqual(len(first["evidence_sha256"]), 64)

    def test_verdict_requires_successful_execution_validation_and_enforcement(self):
        from across_orchestrator.evidence import build_evidence_receipt

        with tempfile.TemporaryDirectory() as tempdir:
            base = {
                "workspace": {"root": tempdir, "commit_sha": "b" * 40},
                "sandbox_receipt": {
                    "status": "completed",
                    "enforcement": {
                        "workspace_boundary": "kernel_enforced",
                        "filesystem_policy": "kernel_enforced",
                        "network_policy": "kernel_enforced",
                    },
                },
                "validations": [{"status": "passed"}],
            }
            self.assertEqual(build_evidence_receipt(base)["verdict"], "ready")
            base["validations"] = [{"status": "failed"}]
            self.assertEqual(build_evidence_receipt(base)["verdict"], "blocked")


if __name__ == "__main__":
    main()
