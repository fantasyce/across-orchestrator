def _graph():
    return {
        "criteria": {
            "criterion-a": {
                "input_fingerprints": ["source-a"],
                "depends_on": [],
                "evidence_ids": ["evidence-a"],
            },
            "criterion-b": {
                "input_fingerprints": ["source-b"],
                "depends_on": [],
                "evidence_ids": ["evidence-b"],
            },
            "criterion-parent": {
                "input_fingerprints": [],
                "depends_on": ["criterion-a", "criterion-b"],
                "evidence_ids": ["evidence-parent-review"],
            },
        }
    }


def test_invalidation_is_selective_and_deterministic():
    from across_orchestrator.goal_graph import compute_invalidation

    plan = compute_invalidation(_graph(), {"source-a"})
    assert plan.affected_criterion_ids == ("criterion-a", "criterion-parent")
    assert plan.stale_evidence_ids == ("evidence-a", "evidence-parent-review")
    assert plan.preserved_evidence_ids == ("evidence-b",)
    assert compute_invalidation(_graph(), {"source-a"}) == plan


def test_unrelated_fingerprint_preserves_all_evidence():
    from across_orchestrator.goal_graph import compute_invalidation

    plan = compute_invalidation(_graph(), {"source-unrelated"})
    assert plan.affected_criterion_ids == ()
    assert plan.preserved_evidence_ids == (
        "evidence-a",
        "evidence-b",
        "evidence-parent-review",
    )
