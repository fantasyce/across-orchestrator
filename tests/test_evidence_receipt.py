import json
import tempfile
import unittest
from pathlib import Path


class EvidenceReceiptTests(unittest.TestCase):
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
