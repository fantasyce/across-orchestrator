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


def test_revalidation_rejects_payload_dependent_batch_before_any_write(tmp_path):
    import sys
    import pytest
    from hashlib import sha256
    from across_orchestrator.coordinator import WorkerCoordinator
    from across_orchestrator.goal_graph import InvalidationPlan
    from across_orchestrator.revalidation import create_revalidation_attempt
    from across_orchestrator.worker_protocol import JobManifest
    from across_orchestrator.worker_store import WorkerControlStore

    coordinator = WorkerCoordinator(WorkerControlStore(tmp_path / "worker-control"))
    plan = InvalidationPlan(("source-a",), ("criterion-a",), ("evidence-a",), ())
    manifest = JobManifest(
        job_id="job-payload-batch", run_id="run-payload", project_id="project-test", task_id="task-test",
        workflow_id="goal-revalidation", idempotency_key="idem-payload-batch",
        command_argv=(sys.executable, "-c", "pass"), required_capabilities={}, permissions={}, budgets={},
        input_artifacts=({"logical_name": "input.json", "sha256": sha256(b"{}").hexdigest()},),
        expected_outputs=(), goal_id="goal-payload", goal_revision=1, goal_node_id="node-a",
        criterion_ids=("criterion-a",), input_fingerprint="f" * 64, required_evidence=("test_receipt",),
    )
    with pytest.raises(ValueError, match="input artifacts"):
        create_revalidation_attempt(coordinator, plan, ["criterion-a"], [manifest])
    assert coordinator.store.list("invalidation_plans") == []
    assert coordinator.store.list("revalidation_attempts") == []
    assert coordinator.store.list("jobs") == []


def _host_revalidation_request():
    return {
        "schema_version": "across-goal-revalidation-request/1.1",
        "graph": {
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
            }
        },
        "changed_fingerprints": ["source-a"],
        "criterion_ids": ["criterion-a"],
        "prior_attempt_number": 3,
        "goal_id": "goal-host",
        "goal_revision": 4,
        "task_id": "task-host",
        "input_fingerprint": "f" * 64,
    }


def _host_receipt(attempt):
    from across_orchestrator.worker_protocol import payload_hash

    unsigned = {
        "schema_version": "across-goal-host-validation-evidence/1.1",
        "attempt_id": attempt["attempt_id"],
        "goal_id": attempt["goal_id"],
        "goal_revision": attempt["goal_revision"],
        "task_id": attempt["task_id"],
        "criterion_ids": attempt["criterion_ids"],
        "artifact_digests": {"task-result.md": "a" * 64},
        "input_fingerprint": attempt["input_fingerprint"],
        "validator_id": "aaa-host-validator",
        "verdict": "verified",
    }
    return {**unsigned, "receipt_hash": payload_hash(unsigned)}


def test_revalidation_11_plan_is_pure_deterministic_and_criterion_scoped():
    from across_orchestrator.revalidation import plan_revalidation

    request = _host_revalidation_request()
    first = plan_revalidation(request)
    second = plan_revalidation(request)

    assert first == second
    assert first["schema_version"] == "across-goal-revalidation-plan/1.1"
    assert first["criterion_ids"] == ["criterion-a"]
    assert first["supersedes_evidence_ids"] == ["evidence-a"]
    assert first["preserved_evidence_ids"] == ["evidence-b"]
    assert first["attempt_number"] == 4
    assert len(first["plan_hash"]) == 64


def test_host_revalidation_start_is_durable_idempotent_and_creates_no_worker_job(tmp_path):
    from across_orchestrator.coordinator import WorkerCoordinator
    from across_orchestrator.revalidation import plan_revalidation, start_revalidation_attempt
    from across_orchestrator.worker_store import WorkerControlStore

    coordinator = WorkerCoordinator(WorkerControlStore(tmp_path / "worker-control"), clock=lambda: 42.0)
    request = _host_revalidation_request()
    plan = plan_revalidation(request)
    start = {
        **request,
        "execution_mode": "host_validation",
        "idempotency_key": "host-validation-goal-host-r4",
        "plan_hash": plan["plan_hash"],
    }

    first = start_revalidation_attempt(coordinator, start)
    second = start_revalidation_attempt(coordinator, start)

    assert first == second
    assert first["schema_version"] == "across-goal-revalidation-attempt/1.1"
    assert first["execution_mode"] == "host_validation"
    assert first["state"] == "awaiting_host_evidence"
    assert first["job_ids"] == []
    assert coordinator.store.get("revalidation_attempts", first["attempt_id"]) == first
    assert len(coordinator.store.list("invalidation_plans")) == 1
    assert coordinator.store.list("jobs") == []


def test_host_revalidation_rejects_worker_manifests_before_any_write(tmp_path):
    import pytest
    from across_orchestrator.coordinator import WorkerCoordinator
    from across_orchestrator.revalidation import plan_revalidation, start_revalidation_attempt
    from across_orchestrator.worker_store import WorkerControlStore

    coordinator = WorkerCoordinator(WorkerControlStore(tmp_path / "worker-control"))
    request = _host_revalidation_request()
    plan = plan_revalidation(request)
    with pytest.raises(ValueError, match="must not include job_manifests"):
        start_revalidation_attempt(
            coordinator,
            {
                **request,
                "execution_mode": "host_validation",
                "idempotency_key": "host-validation-with-worker-manifest",
                "plan_hash": plan["plan_hash"],
                "job_manifests": [{}],
            },
        )
    assert coordinator.store.list("invalidation_plans") == []
    assert coordinator.store.list("revalidation_attempts") == []
    assert coordinator.store.list("jobs") == []


def test_worker_revalidation_requires_exact_manifest_and_replays_idempotently(tmp_path):
    import sys
    import pytest
    from across_orchestrator.coordinator import WorkerCoordinator
    from across_orchestrator.revalidation import plan_revalidation, start_revalidation_attempt
    from across_orchestrator.worker_protocol import JobManifest
    from across_orchestrator.worker_store import WorkerControlStore

    coordinator = WorkerCoordinator(WorkerControlStore(tmp_path / "worker-control"))
    request = _host_revalidation_request()
    plan = plan_revalidation(request)
    base = {
        **request,
        "execution_mode": "worker_reexecution",
        "idempotency_key": "worker-reexecution-goal-host-r4",
        "plan_hash": plan["plan_hash"],
    }
    with pytest.raises(ValueError, match="one revalidation Job manifest"):
        start_revalidation_attempt(coordinator, base)
    assert coordinator.store.list("revalidation_attempts") == []

    manifest = JobManifest(
        job_id="job-worker-revalidation-a",
        run_id="run-worker-revalidation",
        project_id="project-test",
        task_id="task-host",
        workflow_id="goal-revalidation",
        idempotency_key="job-worker-revalidation-a",
        command_argv=(sys.executable, "-c", "print('revalidate')"),
        required_capabilities={},
        permissions={"network": {"mode": "none"}},
        budgets={},
        expected_outputs=(),
        goal_id="goal-host",
        goal_revision=4,
        goal_node_id="criterion-a",
        criterion_ids=("criterion-a",),
        input_fingerprint="f" * 64,
        required_evidence=("test_receipt",),
    )
    start = {**base, "job_manifests": [manifest.to_dict()]}
    first = start_revalidation_attempt(coordinator, start)
    second = start_revalidation_attempt(coordinator, start)

    assert first == second
    assert first["execution_mode"] == "worker_reexecution"
    assert first["state"] == "queued"
    assert first["job_ids"] == ["job-worker-revalidation-a"]
    assert coordinator.store.get("jobs", "job-worker-revalidation-a")["status"] == "queued"


def test_host_revalidation_completion_requires_hash_valid_bound_receipt_and_is_idempotent(tmp_path):
    import pytest
    from across_orchestrator.coordinator import WorkerCoordinator
    from across_orchestrator.revalidation import (
        complete_host_revalidation_attempt,
        plan_revalidation,
        start_revalidation_attempt,
    )
    from across_orchestrator.worker_store import WorkerControlStore

    coordinator = WorkerCoordinator(WorkerControlStore(tmp_path / "worker-control"), clock=lambda: 42.0)
    request = _host_revalidation_request()
    plan = plan_revalidation(request)
    attempt = start_revalidation_attempt(
        coordinator,
        {
            **request,
            "execution_mode": "host_validation",
            "idempotency_key": "host-validation-goal-host-r4",
            "plan_hash": plan["plan_hash"],
        },
    )
    bad_receipt = {**_host_receipt(attempt), "receipt_hash": "0" * 64}
    with pytest.raises(ValueError, match="receipt hash"):
        complete_host_revalidation_attempt(
            coordinator,
            {"attempt_id": attempt["attempt_id"], "receipt": bad_receipt},
        )
    assert coordinator.store.get("revalidation_attempts", attempt["attempt_id"])["state"] == "awaiting_host_evidence"

    receipt = _host_receipt(attempt)
    first = complete_host_revalidation_attempt(
        coordinator,
        {"attempt_id": attempt["attempt_id"], "receipt": receipt},
    )
    second = complete_host_revalidation_attempt(
        coordinator,
        {"attempt_id": attempt["attempt_id"], "receipt": receipt},
    )

    assert first == second
    assert first["state"] == "completed"
    assert first["evidence_receipt_hash"] == receipt["receipt_hash"]
    assert first["replacement_evidence_ids"] == [f"evidence-{receipt['receipt_hash'][:24]}"]


def test_revalidation_idempotency_key_rejects_changed_authority_without_partial_write(tmp_path):
    import pytest
    from across_orchestrator.coordinator import WorkerCoordinator
    from across_orchestrator.revalidation import plan_revalidation, start_revalidation_attempt
    from across_orchestrator.worker_store import WorkerControlStore

    coordinator = WorkerCoordinator(WorkerControlStore(tmp_path / "worker-control"))
    request = _host_revalidation_request()
    plan = plan_revalidation(request)
    start = {
        **request,
        "execution_mode": "host_validation",
        "idempotency_key": "host-validation-goal-host-r4",
        "plan_hash": plan["plan_hash"],
    }
    first = start_revalidation_attempt(coordinator, start)
    conflicting = {**start, "task_id": "task-other"}
    conflicting["plan_hash"] = plan_revalidation(conflicting)["plan_hash"]

    with pytest.raises(ValueError, match="idempotency key conflicts"):
        start_revalidation_attempt(coordinator, conflicting)

    assert len(coordinator.store.list("revalidation_attempts")) == 1
    assert coordinator.store.get("revalidation_attempts", first["attempt_id"]) == first
    assert len(coordinator.store.list("invalidation_plans")) == 1
