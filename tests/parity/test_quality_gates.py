from pathlib import Path

from across_agents_assistant.task_manager.orchestration.quality_gates import (
    ProbeAdapterRegistry,
    QualityGateResult,
    build_quality_report,
)


def test_quality_report_blocks_completion_on_required_failure():
    report = build_quality_report(
        task_id="task-quality",
        contract={
            "capabilities": [{"id": "cap-browser", "required": True}],
            "deliverables": [{"id": "del-index", "required": True}],
        },
        gate_results=[
            QualityGateResult(
                gate_id="gate-browser",
                adapter_id="browser_e2e",
                status="failed",
                required=True,
                summary="Create expense flow failed",
            )
        ],
    )

    assert report["quality_gate"] == "failed"
    assert report["can_complete"] is False
    assert report["required_failed_count"] == 1
    assert report["final_quality_score"] < 80


def test_quality_report_distinguishes_generated_and_final_scores_after_remediation():
    report = build_quality_report(
        task_id="task-quality",
        contract={
            "capabilities": [{"id": "cap-install", "required": True}],
            "deliverables": [{"id": "del-app", "required": True}],
        },
        gate_results=[
            QualityGateResult(
                gate_id="gate-install",
                adapter_id="python",
                status="passed",
                required=True,
                summary="Install succeeded after repair",
            )
        ],
        generated_gate_results=[
            QualityGateResult(
                gate_id="gate-install",
                adapter_id="python",
                status="failed",
                required=True,
                summary="Initial install failed",
            )
        ],
        remediation_count=1,
    )

    assert report["quality_gate"] == "passed"
    assert report["can_complete"] is True
    assert report["generated_quality_score"] < report["final_quality_score"]
    assert report["remediation_count"] == 1


def test_probe_registry_detects_cross_stack_project_facets(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "static").mkdir()
    (tmp_path / "app" / "static" / "index.html").write_text("<html></html>", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")

    registry = ProbeAdapterRegistry.default()
    adapters = registry.detect_adapters(
        str(tmp_path),
        contract={"delivery_facets": ["source_project", "web_ui", "api_service", "documentation"]},
    )

    adapter_ids = {adapter.adapter_id for adapter in adapters}
    assert {"workspace_hygiene", "security_privacy", "documentation", "python", "static_web", "api_service", "browser_e2e"} <= adapter_ids


def test_probe_registry_does_not_mark_unknown_stack_as_validated(tmp_path: Path):
    (tmp_path / "README.md").write_text("# Notes only\n", encoding="utf-8")

    registry = ProbeAdapterRegistry.default()
    gate_plan = registry.build_gate_plan(
        str(tmp_path),
        contract={"delivery_facets": ["source_project"]},
    )

    gate_ids = {gate["adapter_id"] for gate in gate_plan}
    assert "unknown_stack" in gate_ids
    assert any(gate["status"] == "manual_required" for gate in gate_plan)
