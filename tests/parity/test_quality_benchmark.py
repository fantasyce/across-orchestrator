from across_agents_assistant.task_manager.orchestration.quality_benchmark import (
    evaluate_delivery_benchmark,
)


def _passing_task_payload():
    return {
        "task_id": "task-good",
        "status": "completed",
        "progress": 1.0,
        "quality_health": {
            "quality_gate": "passed",
            "active_quality_remediation": [],
            "delivery_quality_report": {
                "produced_required": ["index.html", "styles.css", "app.js", "README.md"],
                "missing_required": [],
                "invalid_required": [],
                "failed_constraints": [],
                "probe_results": [
                    {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "passed": True, "required": True},
                    {"id": "probe-browser-e2e", "probe_type": "browser_e2e", "passed": True, "required": True},
                ],
                "quality_report": {
                    "quality_gate": "passed",
                    "final_quality_score": 76,
                    "required_failed_count": 0,
                    "manual_required_count": 0,
                    "required_skipped_count": 0,
                    "gate_results": [
                        {
                            "gate_id": "gate-workspace-hygiene",
                            "adapter_id": "workspace_hygiene",
                            "status": "passed",
                            "required": True,
                            "evidence": {"delivery_file_count": 4, "noise_file_count": 0},
                        }
                    ],
                },
            },
        },
        "delivery_report": {
            "quality_gate": "passed",
            "final_status": "completed",
            "remediation": {
                "attempts_by_requirement": {"probe_failure:probe-static-web-smoke": 1},
                "active_subtasks": [],
            },
            "consistency": {
                "terminal_with_active_remediation": False,
                "has_missing_required": False,
                "has_failed_constraints": False,
            },
        },
    }


def test_delivery_benchmark_passes_when_required_quality_evidence_is_present():
    report = evaluate_delivery_benchmark(
        [_passing_task_payload()],
        benchmark_id="release-0.2.0",
        expected_files=["index.html", "styles.css", "app.js", "README.md"],
        required_probes=["static_web_smoke", "browser_e2e"],
        min_quality_score=70,
        max_remediation_attempts=2,
    )

    assert report["status"] == "passed"
    assert report["summary"]["scenario_count"] == 1
    assert report["summary"]["failed_scenarios"] == 0
    assert report["scenarios"][0]["quality_score"] == 76
    assert report["scenarios"][0]["checks"]["browser_e2e_passed"] is True


def test_delivery_benchmark_fails_when_browser_e2e_or_file_inventory_is_bad():
    payload = _passing_task_payload()
    payload["task_id"] = "task-bad"
    payload["quality_health"]["delivery_quality_report"]["probe_results"][1]["passed"] = False
    payload["quality_health"]["delivery_quality_report"]["produced_required"].append("debug.log")

    report = evaluate_delivery_benchmark(
        [payload],
        benchmark_id="release-0.2.0",
        expected_files=["index.html", "styles.css", "app.js", "README.md"],
        required_probes=["static_web_smoke", "browser_e2e"],
        min_quality_score=70,
        max_remediation_attempts=2,
    )

    assert report["status"] == "failed"
    scenario = report["scenarios"][0]
    assert scenario["checks"]["browser_e2e_passed"] is False
    assert scenario["checks"]["expected_file_inventory"] is False
    assert "browser_e2e" in " ".join(scenario["failures"])
    assert "debug.log" in " ".join(scenario["failures"])


def test_delivery_benchmark_counts_bundled_remediation_subtasks_once():
    payload = _passing_task_payload()
    payload["task_id"] = "task-bundled-remediation"
    payload["delivery_report"]["remediation"]["attempts_by_requirement"] = {
        "probe_failure:probe-static-web-smoke": 2,
        "probe_failure:probe-browser-e2e": 2,
    }
    payload["subtasks"] = [
        {"subtask_id": "st-web", "status": "completed"},
        {"subtask_id": "st-quality-first", "status": "completed"},
        {"subtask_id": "st-quality-second", "status": "completed"},
    ]

    report = evaluate_delivery_benchmark(
        [payload],
        benchmark_id="release-0.3.0",
        expected_files=["index.html", "styles.css", "app.js", "README.md"],
        required_probes=["static_web_smoke", "browser_e2e"],
        min_quality_score=70,
        max_remediation_attempts=2,
    )

    assert report["status"] == "passed"
    scenario = report["scenarios"][0]
    assert scenario["remediation_attempts"] == 2
    assert scenario["checks"]["remediation_budget"] is True
