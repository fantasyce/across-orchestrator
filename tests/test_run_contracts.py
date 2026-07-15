import json
import unittest


class RunContractTests(unittest.TestCase):
    def test_policy_selects_visible_role_model_budget_and_risk_sandbox(self):
        from across_orchestrator.run_contracts import build_execution_policy_contract

        contract = build_execution_policy_contract({
            "run_id": "run-1",
            "role": "independent_reviewer",
            "model_policy": {
                "provider": "host-provider",
                "model": "review-model",
                "api_key": "do-not-keep-this",
            },
            "budget": {"max_model_calls": 2, "max_candidate_repairs": 1, "max_usd": 3.5},
            "actions": ["inspect", "patch"],
        })

        self.assertEqual(contract["schema_version"], "across-execution-policy/1.0")
        self.assertEqual(contract["role"]["id"], "independent_reviewer")
        self.assertEqual(contract["model_policy"]["model"], "review-model")
        self.assertEqual(contract["budget"]["max_model_calls"], 2)
        self.assertEqual(contract["risk"]["profile"], "medium")
        self.assertEqual(contract["sandbox"]["filesystem_policy"]["mode"], "candidate_workspace_only")
        self.assertEqual(contract["sandbox"]["network_policy"]["mode"], "none")
        self.assertTrue(contract["approval"]["required"])
        self.assertNotIn("do-not-keep-this", json.dumps(contract))

    def test_low_risk_policy_cannot_relax_read_only_or_network_none(self):
        from across_orchestrator.run_contracts import select_risk_aware_sandbox_policy

        policy = select_risk_aware_sandbox_policy({
            "risk_profile": "low",
            "sandbox": {"network_policy": "unrestricted_requires_approval", "filesystem_policy": "allowlist"},
        })

        self.assertEqual(policy["network_policy"]["mode"], "none")
        self.assertEqual(policy["filesystem_policy"]["mode"], "read_only")
        self.assertTrue(policy["external_side_effects_blocked"])

    def test_requested_low_risk_cannot_hide_release_or_network_actions(self):
        from across_orchestrator.run_contracts import select_risk_aware_sandbox_policy

        release = select_risk_aware_sandbox_policy({
            "risk_profile": "low",
            "actions": ["publish release"],
        })
        network = select_risk_aware_sandbox_policy({
            "risk_profile": "medium",
            "external_side_effects": ["push remote"],
        })

        self.assertEqual(release["risk_profile"], "release")
        self.assertEqual(network["risk_profile"], "high")
        self.assertTrue(release["external_side_effects_blocked"])
        self.assertTrue(network["external_side_effects_blocked"])

    def test_comparison_covers_verdict_checks_evidence_revision_model_and_budget(self):
        from across_orchestrator.run_contracts import build_run_comparison

        result = build_run_comparison({
            "baseline": {
                "run_id": "run-before",
                "verdict": "blocked",
                "checks": {"tests": "failed", "lint": "passed"},
                "evidence_ids": ["evidence-a"],
                "commit_sha": "a" * 40,
                "model_policy": {"provider": "local", "model": "model-a"},
                "budget": {"model_calls": 1, "max_model_calls": 3},
            },
            "candidate": {
                "run_id": "run-after",
                "verdict": "ready",
                "checks": {"tests": "passed", "lint": "failed", "security": "passed"},
                "evidence_ids": ["evidence-a", "evidence-b"],
                "commit_sha": "b" * 40,
                "model_policy": {"provider": "local", "model": "model-b"},
                "budget": {"model_calls": 2, "max_model_calls": 3},
            },
        })

        self.assertEqual(result["schema_version"], "across-run-comparison/1.0")
        self.assertTrue(result["summary"]["changed"])
        self.assertEqual(result["summary"]["improved_checks"], ["security", "tests"])
        self.assertEqual(result["summary"]["regressed_checks"], ["lint"])
        self.assertEqual(result["changes"]["evidence"]["added"], ["evidence-b"])
        self.assertTrue(result["changes"]["code_revision"]["changed"])
        self.assertTrue(result["changes"]["model_policy"]["changed"])
        self.assertEqual(len(result["comparison_sha256"]), 64)

    def test_public_contracts_drop_embedded_absolute_paths_and_credentials(self):
        from across_orchestrator.run_contracts import build_run_comparison

        private_path = "/" + "Users/example/private/repo"
        temp_path = "/" + "tmp/private.json"
        result = build_run_comparison({
            "baseline": {
                "run_id": "run-before",
                "checks": {f"logs at {private_path}": "failed"},
                "evidence_ids": [f"artifact {temp_path}"],
                "model_policy": {"provider": "token=super-secret-value"},
            },
            "candidate": {"run_id": "run-after", "checks": {"tests": "passed"}},
        })
        serialized = json.dumps(result)

        self.assertNotIn(private_path, serialized)
        self.assertNotIn(temp_path, serialized)
        self.assertNotIn("super-secret-value", serialized)

    def test_replay_is_read_only_and_external_side_effects_need_new_bound_approval(self):
        from across_orchestrator.run_contracts import build_replay_plan

        source = {
            "run_id": "run-source",
            "verdict": "ready",
            "checks": {"tests": "passed"},
            "external_side_effects": ["push"],
        }
        blocked = build_replay_plan({"source": source, "external_side_effects": ["push"]})
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["next_action"], "request_new_approval")
        self.assertFalse(blocked["execution"]["performed"])
        self.assertFalse(blocked["execution"]["automatic_execution_allowed"])

        receipt = {
            "receipt_id": "receipt-1",
            "integrity_status": "verified",
            "decision": "approved",
            "scope": "replay_external_side_effects",
            "proposer_id": "agent-builder",
            "approver_id": "human-reviewer",
            "subject_sha256": blocked["source_snapshot_sha256"],
        }
        ready = build_replay_plan({
            "source": source,
            "external_side_effects": ["push"],
            "renewed_approval": receipt,
        })
        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["renewed_approval"]["verified"])
        self.assertFalse(ready["execution"]["performed"])
        self.assertFalse(ready["execution"]["automatic_execution_allowed"])


if __name__ == "__main__":
    unittest.main()
