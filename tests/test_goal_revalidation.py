def test_revalidation_always_creates_a_new_attempt_and_keeps_old_evidence_refs():
    from across_orchestrator.goal_graph import InvalidationPlan
    from across_orchestrator.revalidation import build_revalidation_attempt

    plan = InvalidationPlan(
        changed_fingerprints=("source-a",),
        affected_criterion_ids=("criterion-a", "criterion-parent"),
        stale_evidence_ids=("evidence-a", "evidence-parent-review"),
        preserved_evidence_ids=("evidence-b",),
        stale_evidence_by_criterion=(
            ("criterion-a", ("evidence-a",)),
            ("criterion-parent", ("evidence-parent-review",)),
        ),
    )
    first = build_revalidation_attempt(plan, ["criterion-a"])
    second = build_revalidation_attempt(plan, ["criterion-a"])
    assert first["attempt_id"] != second["attempt_id"]
    assert first["attempt_number"] == 1
    assert first["criterion_ids"] == ["criterion-a"]
    assert first["supersedes_evidence_ids"] == ["evidence-a"]
    assert first["preserved_evidence_ids"] == ["evidence-b"]


def test_revalidation_rejects_unaffected_criterion():
    import pytest
    from across_orchestrator.goal_graph import InvalidationPlan
    from across_orchestrator.revalidation import build_revalidation_attempt

    plan = InvalidationPlan(("source-a",), ("criterion-a",), ("evidence-a",), ("evidence-b",))
    with pytest.raises(ValueError, match="affected"):
        build_revalidation_attempt(plan, ["criterion-b"])
