from __future__ import annotations

import pytest

from across_orchestrator.agent_loop import AgentLoopRuntime
from across_orchestrator.store import LocalStore


def _metadata(run_id: str = "run-1", spec_id: str = "daily-news-brief") -> dict:
    return {
        "autopilot": {
            "run_id": run_id,
            "spec_id": spec_id,
            "schema_version": "across-loop-spec/1.0",
            "evidence_contract": "across-loop-evidence/1.0",
            "candidate_id": "cand-1",
            "candidate_mode": "snapshot",
            "candidate_manifest": "/tmp/candidate-manifest.json",
            "actions_allowed": ["read_only_analysis"],
            "actions_blocked": ["merge_pr", "release_publish"],
            "sandbox": {"root": "/tmp/across-autopilot-sandbox"},
        }
    }


def test_autopilot_metadata_is_reflected_in_status_and_evidence_summary(tmp_path):
    runtime = AgentLoopRuntime(store=LocalStore(home=tmp_path / "store"))

    loop = runtime.start_loop(
        goal="Run Autopilot delegated task",
        project_root=str(tmp_path / "project"),
        metadata=_metadata(),
    )
    loop = runtime.run_loop(loop.loop_id)
    status = runtime.get_loop(loop.loop_id).to_dict()
    summary = runtime.get_loop_evidence_summary(loop.loop_id)

    assert status["metadata"]["autopilot"]["run_id"] == "run-1"
    assert summary["metadata"]["autopilot"]["run_id"] == "run-1"
    assert summary["metadata"]["autopilot"]["spec_id"] == "daily-news-brief"
    assert summary["metadata"]["autopilot"]["candidate_id"] == "cand-1"
    assert summary["metadata"]["autopilot"]["candidate_mode"] == "snapshot"
    assert summary["metadata"]["autopilot"]["evidence_contract"] == "across-loop-evidence/1.0"


def test_autopilot_metadata_missing_required_fields_is_rejected(tmp_path):
    runtime = AgentLoopRuntime(store=LocalStore(home=tmp_path / "store"))

    with pytest.raises(ValueError, match="metadata.autopilot missing required fields"):
        runtime.start_loop(
            goal="Bad metadata",
            project_root=str(tmp_path / "project"),
            metadata={"autopilot": {"run_id": "run-1"}},
        )


def test_autopilot_metadata_wrong_schema_is_rejected(tmp_path):
    runtime = AgentLoopRuntime(store=LocalStore(home=tmp_path / "store"))
    metadata = _metadata()
    metadata["autopilot"]["schema_version"] = "across-loop-spec/2.0"

    with pytest.raises(ValueError, match="metadata.autopilot.schema_version"):
        runtime.start_loop(
            goal="Bad schema",
            project_root=str(tmp_path / "project"),
            metadata=metadata,
        )
