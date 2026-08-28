import json
import tempfile
import unittest
from pathlib import Path


class EvidenceReceiptTests(unittest.TestCase):
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
            "goal_id": "goal-1",
            "goal_revision": 3,
            "criterion_ids": ["criterion-tests"],
            "task_id": "task-1",
            "run_id": "run-1",
            "attempt_id": "attempt-2",
            "attempt": 2,
            "lease_id": "lease-1",
            "lease_state": "terminal_valid",
            "artifact_digests": {"artifact-1": "b" * 64},
            "input_fingerprint": "a" * 64,
            "validator": {"validator_id": "quality-gate:tests", "method": "receipt_quality_gate"},
        }
        result = bind_evidence_to_criteria(receipt, binding)
        self.assertEqual(result["trust_state"], "verified")
        self.assertEqual(result["verdict"], "verified")
        self.assertEqual(result["receipt_hash"], receipt["receipt_hash"])

        for mutation in (
            lambda value: value.update(goal_revision=2),
            lambda value: value.update(artifact_digests={"artifact-1": "c" * 64}),
            lambda value: value.update(lease_state="expired"),
            lambda value: value.update(verified=True),
        ):
            invalid = dict(binding)
            mutation(invalid)
            with self.assertRaises(ValueError):
                bind_evidence_to_criteria(receipt, invalid)

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
    unittest.main()
