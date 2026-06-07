from across_agents_assistant.task_manager.orchestration.release_evaluation import (
    build_release_evaluation_summary,
)


def _task(
    task_id: str,
    *,
    status: str = "completed",
    gate: str = "passed",
    score: int = 90,
    task_types: list[str] | None = None,
    delivery_mode: str = "functional",
    owner_agent: str = "hermes",
    allowed_subtask_agents: list[str] | None = None,
    remediation_count: int = 0,
    required_failed_count: int = 0,
    manual_required_count: int = 0,
    skipped_required_count: int = 0,
    updated_at: float | None = None,
    probe_results: list[dict] | None = None,
    gate_results: list[dict] | None = None,
):
    return {
        "task_id": task_id,
        "description": f"Task {task_id}",
        "status": status,
        "task_types": task_types or ["functional"],
        "delivery_mode": delivery_mode,
        "owner_agent": owner_agent,
        "allowed_subtask_agents": ["openclaw", "deepseek"] if allowed_subtask_agents is None else allowed_subtask_agents,
        "created_at": updated_at or 1.0,
        "updated_at": updated_at or 1.0,
        "last_owner_decision": {
            "delivery_quality": {
                "delivery_quality": gate,
                "probe_results": probe_results or [],
                "quality_report": {
                    "quality_gate": gate,
                    "final_quality_score": score,
                    "generated_quality_score": max(score - 8, 0),
                    "remediation_count": remediation_count,
                    "required_failed_count": required_failed_count,
                    "manual_required_count": manual_required_count,
                    "required_skipped_count": skipped_required_count,
                    "score_breakdown": {
                        "contract_coverage": 20,
                        "runtime_smoke": 15,
                        "user_e2e": 15,
                    },
                    "gate_results": gate_results or [],
                },
            }
        },
    }


def test_release_evaluation_reports_no_evidence_without_quality_reports():
    summary = build_release_evaluation_summary([
        {
            "task_id": "task-legacy",
            "description": "Legacy task",
            "status": "completed",
            "task_types": ["functional"],
            "delivery_mode": "legacy",
            "last_owner_decision": {},
        }
    ])

    assert summary["release_readiness"] == "no_evidence"
    assert summary["evaluated_task_count"] == 0
    assert summary["recommendation"] == "Run at least three quality-gated E2E tasks before release."


def test_release_evaluation_marks_ready_when_recent_quality_is_clean():
    summary = build_release_evaluation_summary([
        _task("task-a", score=91, owner_agent="hermes", allowed_subtask_agents=["deepseek"]),
        _task("task-b", score=88, owner_agent="openclaw", allowed_subtask_agents=["claude", "minimax"]),
        _task(
            "task-c",
            score=94,
            task_types=["artifact"],
            delivery_mode="artifact",
            owner_agent="minimax",
            allowed_subtask_agents=[],
        ),
    ])

    assert summary["release_readiness"] == "ready"
    assert summary["evaluated_task_count"] == 3
    assert summary["passed_task_count"] == 3
    assert summary["blocked_task_count"] == 0
    assert summary["pass_rate"] == 1.0
    assert summary["average_final_quality_score"] == 91
    assert summary["agent_coverage"]["hermes"] == 1
    assert summary["agent_coverage"]["deepseek"] == 1
    assert summary["stack_coverage"]["functional"] == 2
    assert summary["stack_coverage"]["artifact"] == 1
    assert summary["agent_mix_summary"]["satisfies_release_mix"] is True
    assert {check["id"]: check["status"] for check in summary["readiness_checks"]}["agent_mix"] == "passed"


def test_release_evaluation_blocks_on_required_gate_failure():
    summary = build_release_evaluation_summary([
        _task("task-good", score=90),
        _task(
            "task-bad",
            gate="failed",
            score=52,
            required_failed_count=1,
            remediation_count=2,
        ),
    ])

    assert summary["release_readiness"] == "blocked"
    assert summary["blocked_task_count"] == 1
    assert summary["total_remediation_count"] == 2
    assert summary["gate_breakdown"]["failed"] == 1
    assert summary["top_risks"][0]["kind"] == "required_gate_failure"
    assert summary["recent_evaluations"][0]["task_id"] == "task-good"


def test_release_evaluation_flags_manual_or_skipped_gates_as_attention():
    summary = build_release_evaluation_summary([
        _task("task-a", score=86, manual_required_count=1),
        _task("task-b", score=82, skipped_required_count=1),
        _task("task-c", score=81),
    ])

    assert summary["release_readiness"] == "attention"
    assert summary["manual_task_count"] == 1
    assert summary["skipped_task_count"] == 1
    assert any(risk["kind"] == "manual_or_skipped_gate" for risk in summary["top_risks"])


def test_release_evaluation_reports_quality_trend_probe_coverage_and_checklist():
    summary = build_release_evaluation_summary([
        _task(
            "task-a",
            score=82,
            updated_at=1.0,
            owner_agent="hermes",
            allowed_subtask_agents=["openclaw", "deepseek"],
            probe_results=[
                {"probe_type": "static_web", "passed": True},
                {"probe_type": "browser_e2e", "passed": True},
            ],
        ),
        _task(
            "task-b",
            score=88,
            updated_at=2.0,
            owner_agent="openclaw",
            allowed_subtask_agents=["claude", "minimax"],
            probe_results=[
                {"probe_type": "api_service", "passed": True},
                {"probe_type": "cli_generic", "passed": True},
            ],
        ),
        _task(
            "task-c",
            score=94,
            updated_at=3.0,
            owner_agent="claude",
            allowed_subtask_agents=["hermes", "deepseek"],
            probe_results=[
                {"probe_type": "workspace_hygiene", "passed": True},
                {"probe_type": "security_privacy", "passed": True},
            ],
        ),
    ])

    assert summary["quality_trend"] == {
        "direction": "improving",
        "latest_score": 94,
        "previous_score": 88,
        "delta": 6,
        "point_count": 3,
        "points": [
            {"task_id": "task-a", "score": 82, "quality_gate": "passed", "updated_at": 1.0},
            {"task_id": "task-b", "score": 88, "quality_gate": "passed", "updated_at": 2.0},
            {"task_id": "task-c", "score": 94, "quality_gate": "passed", "updated_at": 3.0},
        ],
    }
    assert summary["probe_coverage"]["passed"]["browser_e2e"] == 1
    assert summary["probe_coverage"]["passed"]["api_service"] == 1
    checks = {check["id"]: check for check in summary["readiness_checks"]}
    assert checks["quality_trend"]["status"] == "passed"
    assert checks["probe_coverage"]["status"] == "passed"
    assert checks["agent_mix"]["status"] == "passed"


def test_release_evaluation_marks_agent_mix_gap_as_attention():
    summary = build_release_evaluation_summary([
        _task("task-a", score=91, owner_agent="hermes", allowed_subtask_agents=[]),
        _task("task-b", score=89, owner_agent="hermes", allowed_subtask_agents=[]),
        _task("task-c", score=90, owner_agent="hermes", allowed_subtask_agents=[]),
    ])

    assert summary["release_readiness"] == "attention"
    assert summary["agent_mix_summary"]["satisfies_release_mix"] is False
    assert summary["agent_mix_summary"]["missing"] == [
        "at least 3 distinct agents",
        "at least 2 local agents",
        "at least 1 cloud agent",
    ]
    checks = {check["id"]: check for check in summary["readiness_checks"]}
    assert checks["agent_mix"]["status"] == "warning"
    assert any(risk["kind"] == "agent_mix" for risk in summary["top_risks"])


def test_release_evaluation_counts_quality_gate_results_toward_probe_coverage():
    summary = build_release_evaluation_summary([
        _task(
            "task-a",
            score=90,
            owner_agent="hermes",
            allowed_subtask_agents=["openclaw", "deepseek"],
            probe_results=[
                {"probe_type": "static_web_smoke", "passed": True},
                {"probe_type": "browser_e2e", "passed": True},
            ],
            gate_results=[
                {"adapter_id": "workspace_hygiene", "status": "passed", "required": True},
            ],
        ),
        _task(
            "task-b",
            score=88,
            owner_agent="openclaw",
            allowed_subtask_agents=["claude", "minimax"],
            probe_results=[
                {"probe_type": "api_service", "passed": True},
                {"probe_type": "cli_generic", "passed": True},
            ],
            gate_results=[
                {"adapter_id": "security_privacy", "status": "passed", "required": True},
            ],
        ),
        _task(
            "task-c",
            score=92,
            owner_agent="claude",
            allowed_subtask_agents=["deepseek"],
        ),
    ])

    assert summary["probe_coverage"]["passed"]["static_web"] == 1
    assert summary["probe_coverage"]["passed"]["workspace_hygiene"] == 1
    assert summary["probe_coverage"]["passed"]["security_privacy"] == 1
    assert summary["probe_coverage"]["missing_required_probe_types"] == []
    checks = {check["id"]: check for check in summary["readiness_checks"]}
    assert checks["probe_coverage"]["status"] == "passed"


def test_release_evaluation_recent_items_include_auditable_detail_fields():
    summary = build_release_evaluation_summary([
        _task(
            "task-a",
            score=90,
            owner_agent="hermes",
            allowed_subtask_agents=["openclaw", "deepseek", "minimax"],
            remediation_count=1,
            probe_results=[
                {"probe_type": "static_web_smoke", "passed": True, "required": True},
                {"probe_type": "browser_e2e", "passed": False, "required": True},
                {"probe_type": "manual_review", "status": "manual_required", "required": True},
            ],
            gate_results=[
                {"adapter_id": "workspace_hygiene", "status": "passed", "required": True},
                {
                    "adapter_id": "agent_mix",
                    "status": "passed",
                    "required": True,
                    "evidence": {
                        "satisfied_constraints": [
                            {
                                "evidence": {
                                    "actual_agents": ["hermes", "openclaw", "deepseek"],
                                    "local_agents": ["hermes", "openclaw"],
                                    "cloud_agents": ["deepseek"],
                                }
                            }
                        ]
                    },
                },
            ],
        )
    ])

    recent = summary["recent_evaluations"][0]
    assert recent["benchmark_status"] == "failed"
    assert recent["probe_summary"] == {
        "passed": ["agent_mix", "static_web", "workspace_hygiene"],
        "failed": ["browser_e2e"],
        "manual_required": ["manual_review"],
        "skipped": [],
        "unknown": [],
    }
    assert recent["agent_mix"] == {
        "actual_agents": ["deepseek", "hermes", "openclaw"],
        "local_agents": ["hermes", "openclaw"],
        "cloud_agents": ["deepseek"],
    }
    assert recent["audit_trace"]["remediation_count"] == 1
    assert recent["audit_trace"]["required_failed_count"] == 0
