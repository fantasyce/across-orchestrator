import copy
import unittest


def simple_goal_contract() -> dict:
    return {
        "schema_version": "across-goal-contract/1.0",
        "goal_id": "goal-task-001",
        "revision": 1,
        "task_id": "task-001",
        "statement": "Ship a verifiable change",
        "success_outcome": "The user can verify the change.",
        "scope": {"includes": ["implementation", "tests"], "excludes": ["release", "promotion"]},
        "acceptance_criteria": [
            {
                "criterion_id": "criterion-36bc8486dd50ddc0",
                "description": "All required tests pass.",
                "required": True,
                "validator_kind": "test_suite",
                "review_policy": "automatic",
                "source": "user_confirmed",
            },
            {
                "criterion_id": "criterion-5691b86a398c721e",
                "description": "Installed application exposes the result.",
                "required": True,
                "validator_kind": "installed_user_journey",
                "review_policy": "human",
                "source": "user_confirmed",
            },
        ],
        "dependencies": [],
        "execution_profile": "orchestrated",
        "source": "user",
        "confirmed_by": "human:user",
        "confirmed_at": "2026-08-28T00:00:00Z",
        "created_at": "2026-08-28T00:00:00Z",
    }


class GoalContractTests(unittest.TestCase):
    def test_normalization_hash_and_criterion_id_match_the_host_contract(self):
        from across_orchestrator.goal_contracts import criterion_id, normalize_goal_contract, stable_goal_hash

        fixture = simple_goal_contract()
        self.assertEqual(normalize_goal_contract(fixture), fixture)
        self.assertEqual(criterion_id("All required tests pass.", "test_suite"), "criterion-36bc8486dd50ddc0")
        self.assertEqual(stable_goal_hash(fixture), "2d6996c43ab0104c3b94f87a2b6030d2d6bab0df1fca777bebba894b21fe83a8")

    def test_invalid_revision_schema_and_duplicate_criterion_fail_closed(self):
        from across_orchestrator.goal_contracts import normalize_goal_contract, stable_goal_hash

        for mutate in (
            lambda value: value.update(revision=0),
            lambda value: value.pop("statement"),
            lambda value: value.update(schema_version="across-goal-contract/2.0"),
            lambda value: value["acceptance_criteria"].append(copy.deepcopy(value["acceptance_criteria"][0])),
        ):
            fixture = simple_goal_contract()
            mutate(fixture)
            with self.assertRaises(ValueError):
                normalize_goal_contract(fixture)

        whitespace = simple_goal_contract()
        whitespace["confirmed_by"] = "   "
        whitespace["confirmed_at"] = "   "
        with self.assertRaises(ValueError):
            normalize_goal_contract(whitespace)
        with self.assertRaisesRegex(ValueError, "integer|canonical JSON"):
            stable_goal_hash({"value": 1e-7})
        self.assertEqual(stable_goal_hash({"value": 1.0}), stable_goal_hash({"value": 1}))
        self.assertEqual(stable_goal_hash({"value": -0.0}), stable_goal_hash({"value": 0}))

    def test_change_proposals_cannot_use_a_management_operation(self):
        from across_orchestrator.goal_contracts import normalize_goal_change_proposal

        proposal = {
            "schema_version": "across-goal-change-proposal/1.0",
            "proposal_id": "proposal-1",
            "goal_id": "goal-task-001",
            "base_goal_revision": 1,
            "proposed_by": "autopilot",
            "reason": "Add review coverage.",
            "operations": [{"op": "confirm", "path": "/confirmed_by", "value": "autopilot"}],
            "impact_summary": {"goal_ids": ["goal-task-001"], "criterion_ids": [], "evidence_ids": [], "requires_revalidation": True},
            "risk_summary": {"level": "medium", "reasons": ["scope_change"]},
            "estimated_cost": {"unit": "agent_turns", "value": 1},
            "alternatives": [],
            "decision_state": "pending",
            "created_at": "2026-08-28T00:05:00Z",
        }

        with self.assertRaisesRegex(ValueError, "operation"):
            normalize_goal_change_proposal(proposal)

        proposal["operations"] = [{"op": "add", "path": "/confirmed_by/agent", "value": "autopilot"}]
        with self.assertRaisesRegex(ValueError, "host-owned"):
            normalize_goal_change_proposal(proposal)


if __name__ == "__main__":
    unittest.main()
