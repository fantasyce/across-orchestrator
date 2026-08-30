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
    assert first["state"] == "planned"
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


def test_revalidation_attempt_is_persisted_with_real_queued_jobs(tmp_path):
    import sys
    from across_orchestrator.coordinator import WorkerCoordinator
    from across_orchestrator.goal_graph import InvalidationPlan
    from across_orchestrator.revalidation import create_revalidation_attempt
    from across_orchestrator.worker_protocol import JobManifest
    from across_orchestrator.worker_store import WorkerControlStore

    coordinator = WorkerCoordinator(WorkerControlStore(tmp_path / "worker-control"))
    plan = InvalidationPlan(("source-a",), ("criterion-a",), ("evidence-a",), ("evidence-b",))
    manifest = JobManifest(
        job_id="job-revalidate-a", run_id="run-revalidate", project_id="project-test", task_id="task-test",
        workflow_id="goal-revalidation", idempotency_key="idem-revalidate-a",
        command_argv=(sys.executable, "-c", "print('revalidate')"), required_capabilities={},
        permissions={"network": {"mode": "none"}}, budgets={}, expected_outputs=(),
        goal_id="goal-test", goal_revision=2, goal_node_id="goal-node-a",
        criterion_ids=("criterion-a",), input_fingerprint="a" * 64,
        required_evidence=("test_receipt",),
    )
    attempt = create_revalidation_attempt(coordinator, plan, ["criterion-a"], [manifest])

    assert attempt["state"] == "queued"
    assert attempt["job_ids"] == ["job-revalidate-a"]
    assert coordinator.store.get("revalidation_attempts", attempt["attempt_id"])["attempt_id"] == attempt["attempt_id"]
    assert coordinator.job("job-revalidate-a")["status"] == "queued"


def test_revalidation_rejects_mixed_goal_authority_before_any_write(tmp_path):
    import sys
    import pytest
    from dataclasses import replace
    from across_orchestrator.coordinator import WorkerCoordinator
    from across_orchestrator.goal_graph import InvalidationPlan
    from across_orchestrator.revalidation import create_revalidation_attempt
    from across_orchestrator.worker_protocol import JobManifest
    from across_orchestrator.worker_store import WorkerControlStore

    coordinator = WorkerCoordinator(WorkerControlStore(tmp_path / "worker-control"))
    plan = InvalidationPlan(("source-a",), ("criterion-a", "criterion-b"), ("evidence-a",), ())
    first = JobManifest(
        job_id="job-mixed-a", run_id="run-mixed", project_id="project-test", task_id="task-one",
        workflow_id="goal-revalidation", idempotency_key="idem-mixed-a",
        command_argv=(sys.executable, "-c", "pass"), required_capabilities={}, permissions={}, budgets={},
        expected_outputs=(), goal_id="goal-one", goal_revision=1, goal_node_id="node-a",
        criterion_ids=("criterion-a",), input_fingerprint="a" * 64, required_evidence=("test_receipt",),
    )
    second = replace(
        first, job_id="job-mixed-b", idempotency_key="idem-mixed-b", task_id="task-two",
        goal_id="goal-two", goal_revision=7, criterion_ids=("criterion-b",),
    )
    with pytest.raises(ValueError, match="share one Goal"):
        create_revalidation_attempt(coordinator, plan, ["criterion-a", "criterion-b"], [first, second])
    assert coordinator.store.list("invalidation_plans") == []
    assert coordinator.store.list("revalidation_attempts") == []
    assert coordinator.store.list("jobs") == []
