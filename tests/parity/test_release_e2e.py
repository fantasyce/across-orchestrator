from dataclasses import asdict
from types import SimpleNamespace

import pytest

from across_agents_assistant.task_manager.orchestration import contract_acceptance
from across_agents_assistant.task_manager.orchestration.release_e2e import (
    RELEASE_E2E_SCENARIO_ID,
    build_release_e2e_scenarios,
    build_release_e2e_subtasks,
    build_release_e2e_task_request,
    write_release_e2e_reference_artifact,
)
from across_agents_assistant.task_manager.orchestration.delivery_contract import (
    build_owner_delivery_contract,
)
from across_agents_assistant.task_manager.orchestration.requirements import (
    extract_requirement_manifest,
    extract_required_path_hints,
)


def test_release_e2e_scenario_is_cross_stack_and_cross_agent():
    scenarios = build_release_e2e_scenarios()

    scenario = next(item for item in scenarios if item["id"] == RELEASE_E2E_SCENARIO_ID)

    assert scenario["complexity_score"] >= 90
    assert {"openclaw", "hermes", "claude"}.issubset(set(scenario["local_agents"]))
    assert {"deepseek", "minimax"}.issubset(set(scenario["cloud_agents"]))
    assert scenario["required_quality_gates"] == [
        "workspace_hygiene",
        "security_privacy",
        "static_web",
        "api_service",
        "cli_generic",
        "browser_e2e",
    ]
    assert scenario["required_files"] == [
        "README.md",
        "web/index.html",
        "web/styles.css",
        "web/app.js",
        "api/server.mjs",
        "cli/quality-check.mjs",
        "tests/e2e-smoke.mjs",
    ]
    assert scenario["required_agent_mix"] == {
        "min_distinct_agents": 3,
        "min_local_agents": 2,
        "min_cloud_agents": 1,
    }


def test_release_e2e_task_request_forces_exact_deliverables_and_verification(tmp_path):
    project_dir = tmp_path / "release-e2e"

    request = build_release_e2e_task_request(
        scenario_id=RELEASE_E2E_SCENARIO_ID,
        project_dir=str(project_dir),
        run_label="unit",
    )

    description = request["description"]
    assert request["project_dir"] == str(project_dir)
    assert request["task_types"] == ["functional", "artifact"]
    assert request["strict_dependency"] is True
    assert request["enable_wave_gate"] is True
    assert request["required_agent_mix"] == {
        "min_distinct_agents": 3,
        "min_local_agents": 2,
        "min_cloud_agents": 1,
    }
    assert request["owner_agent"] == "auto"
    assert {"openclaw", "hermes", "claude", "deepseek", "minimax"}.issubset(
        set(request["allowed_subtask_agents"])
    )
    assert "Do not create any other files" in description
    assert "Node.js built-in http server" in description
    assert "browser E2E" in description
    assert "native skill" in description.lower()
    assert "Local Agents must include OpenClaw, Hermes, and Claude Code" in description
    assert "Cloud LLMs must include DeepSeek and MiniMax" in description
    assert "Required agent execution mix" in description
    assert "At least 3 distinct non-owner agents" in description
    assert "At least 2 local agents" in description
    assert "At least 1 cloud LLM" in description
    assert "Selected Agent, Matched Native Skill, MCP Risk, and Reason" in description
    assert "Generated Quality Score, Final Quality Score, Required Gate Failures" in description
    assert "Execution Timeline" in description
    assert "Remediation Trace" in description
    assert "MCP Safety Audit" in description
    assert "Native Skill Routing Evidence" in description
    assert "Scenario ID: cross_agent_full_delivery_v1" in description
    assert "process.env.PORT" in description
    assert "./styles.css" in description
    assert "./app.js" in description
    assert "api-results" in description
    assert "quality-gates" in description
    assert "Owner Agent Route Preview" in description
    assert "Selected Agent, Matched Native Skill, MCP Risk, and Reason" in description
    assert "Final Verdict" in description
    assert "avoid a section that lists scanner/security terms" in description
    for relative_path in request["required_files"]:
        assert relative_path in description


def test_release_e2e_manifest_extraction_keeps_only_required_files(tmp_path):
    request = build_release_e2e_task_request(
        scenario_id=RELEASE_E2E_SCENARIO_ID,
        project_dir=str(tmp_path / "release-e2e"),
        run_label="manifest",
    )

    extracted = extract_required_path_hints(request["description"])
    assert set(extracted) == set(request["required_files"])
    assert len(extracted) == len(request["required_files"])


def test_release_e2e_deterministic_decomposition_matches_exact_manifest():
    subtasks = build_release_e2e_subtasks(["openclaw", "hermes", "claude", "deepseek", "minimax"])

    delivered = [
        item["path_hint"]
        for subtask in subtasks
        for item in subtask["deliverables"]
    ]
    agents = {subtask["agent"] for subtask in subtasks}

    assert delivered == [
        "api/server.mjs",
        "web/index.html",
        "web/styles.css",
        "web/app.js",
        "cli/quality-check.mjs",
        "tests/e2e-smoke.mjs",
        "README.md",
    ]
    assert {"deepseek", "hermes", "openclaw"}.issubset(agents)
    assert all("FastAPI" not in subtask["description"] for subtask in subtasks)
    assert all("pyproject.toml" not in subtask["description"] for subtask in subtasks)


def test_release_e2e_task_request_is_not_python_specific(tmp_path):
    request = build_release_e2e_task_request(
        scenario_id=RELEASE_E2E_SCENARIO_ID,
        project_dir=str(tmp_path / "release-e2e"),
    )

    description = request["description"].lower()
    assert "python" not in description
    assert "node" in description
    assert "web/index.html" in description
    assert "api/server.mjs" in description


def test_release_e2e_delivery_contract_accepts_node_mjs_entrypoints(tmp_path):
    request = build_release_e2e_task_request(
        scenario_id=RELEASE_E2E_SCENARIO_ID,
        project_dir=str(tmp_path / "release-e2e"),
        run_label="contract",
    )
    manifest_obj = extract_requirement_manifest(
        "task-release-e2e",
        request["description"],
        request["project_dir"],
    )
    manifest = {
        "deliverables": [asdict(item) for item in manifest_obj.deliverables],
        "quality_checks": [asdict(item) for item in manifest_obj.quality_checks],
    }

    contract = build_owner_delivery_contract(
        task_id="task-release-e2e",
        description=request["description"],
        task_types=request["task_types"],
        project_dir=request["project_dir"],
        manifest=manifest,
    )

    groups = {group["id"]: group for group in contract["deliverable_groups"]}
    assert ".mjs" in groups["group-api-source"]["allowed_extensions"]
    assert "api/server.mjs" in groups["group-api-source"]["one_of_entrypoints"]
    assert "web/index.html" in groups["group-web-ui"]["one_of_entrypoints"]
    assert ".mjs" in groups["group-test-suite"]["allowed_extensions"]
    probe_types = {probe["probe_type"] for probe in contract["acceptance_probes"]}
    assert "pytest" not in probe_types
    assert {"static_web_smoke", "browser_e2e", "api_service", "cli_generic"} <= probe_types
    agent_mix = [
        constraint for constraint in contract["constraints"]
        if constraint["constraint_type"] == "agent_mix"
    ]
    assert agent_mix
    assert agent_mix[0]["value"] == request["required_agent_mix"]


def test_release_e2e_reference_artifact_passes_automatic_probes(monkeypatch, tmp_path):
    if not contract_acceptance._node_probe_executable():
        pytest.skip("Node.js is required for release E2E API and CLI probes")

    request = build_release_e2e_task_request(
        scenario_id=RELEASE_E2E_SCENARIO_ID,
        project_dir=str(tmp_path),
        run_label="reference",
    )
    manifest_obj = extract_requirement_manifest(
        "task-release-reference",
        request["description"],
        request["project_dir"],
    )
    manifest = {
        "deliverables": [asdict(item) for item in manifest_obj.deliverables],
        "quality_checks": [asdict(item) for item in manifest_obj.quality_checks],
    }
    contract = build_owner_delivery_contract(
        task_id="task-release-reference",
        description=request["description"],
        task_types=request["task_types"],
        project_dir=request["project_dir"],
        manifest=manifest,
    )
    written = write_release_e2e_reference_artifact(str(tmp_path))

    class FakeTask:
        task_id = "task-release-reference"
        project_dir = str(tmp_path)
        subtasks = [
            SimpleNamespace(subtask_id="st-openclaw", agent_id="openclaw", status="completed"),
            SimpleNamespace(subtask_id="st-hermes", agent_id="hermes", status="completed"),
            SimpleNamespace(subtask_id="st-deepseek", agent_id="deepseek", status="completed"),
        ]

    monkeypatch.setattr(
        contract_acceptance,
        "_run_browser_e2e",
        lambda project_dir, task_description=None: {
            "probe_type": "browser_e2e",
            "passed": True,
            "returncode": 0,
            "output_tail": "browser ok",
            "blocked_by_environment": False,
        },
    )

    report = contract_acceptance.run_delivery_contract_acceptance(FakeTask(), contract, [])

    assert sorted(written) == sorted(request["required_files"])
    assert report["delivery_quality"] == "passed"
    gate_types = {gate["adapter_id"]: gate["status"] for gate in report["quality_report"]["gate_results"]}
    assert gate_types["static_web_smoke"] == "passed"
    assert gate_types["api_service"] == "passed"
    assert gate_types["cli_generic"] == "passed"
