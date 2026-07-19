from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_distribution_only_packages_across_orchestrator_namespace():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    package_find = config["tool"]["setuptools"]["packages"]["find"]

    assert package_find["where"] == ["src"]
    assert package_find["include"] == ["across_orchestrator*"]


def test_development_package_metadata_tracks_distribution_version():
    from across_orchestrator import __version__
    from across_orchestrator.server import MCP_SERVER_INFO
    from across_orchestrator.worker_runtime import WORKER_VERSION

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))

    expected_version = pyproject["project"]["version"]

    assert __version__ == expected_version
    assert package["version"] == expected_version
    assert package_lock["version"] == expected_version
    assert package_lock["packages"][""]["version"] == expected_version
    assert WORKER_VERSION == expected_version
    assert MCP_SERVER_INFO["version"] == expected_version


def test_across_orchestrator_production_code_does_not_import_aaa_internals():
    offenders = []
    for path in (ROOT / "src" / "across_orchestrator").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "from across_agents_assistant" in source or "import across_agents_assistant" in source:
            offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == []


def test_repository_does_not_vendor_aaa_namespace_or_parity_tests():
    vendored = []
    for parent in ("src", "tests"):
        vendored.extend(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / parent).rglob("across_agents_assistant")
        )

    parity_imports = []
    aaa_import = re.compile(r"^\s*(?:from|import)\s+across_agents_assistant\b", re.MULTILINE)
    for path in (ROOT / "tests").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if aaa_import.search(source):
            parity_imports.append(path.relative_to(ROOT).as_posix())

    assert vendored == []
    assert parity_imports == []


def test_readme_describes_host_neutral_product_not_migration_snapshot():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    forbidden_phrases = [
        "split out from Across Agents Assistant",
        "transplanted Across Agents Assistant",
        "transplanted `TaskState`",
        "transplanted mature engine",
        "Across Agents Assistant internals",
    ]

    assert [phrase for phrase in forbidden_phrases if phrase in readme] == []


def test_readme_documents_agent_loop_lease_and_routing_contract():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    required_phrases = [
        "actionLeaseSeconds",
        "agentRouting",
        "loop.step.heartbeat",
        "loop.step.lease_expired",
        "loop.cancel_requested",
        "loop.step.cancelled",
        "cancellation",
        "dispatch cancellation guard",
        "loop.dispatch.detached",
        "cancel ack",
        "failure_type",
        "adapter_error",
        "quality_failed",
        "lease_expired",
        "cannot terminate noncooperative in-process Python callbacks",
        "lease_expires_at",
    ]

    assert [phrase for phrase in required_phrases if phrase not in readme] == []


def test_app_grade_payload_is_host_conformance_not_aaa_specific(tmp_path):
    from across_orchestrator.app_grade import build_release_e2e_payload

    payload = build_release_e2e_payload(
        task_id="task-boundary",
        project_root=str(tmp_path),
        run_label="boundary",
    )
    serialized = str(payload)

    assert payload["scenario_id"] == "host_agent_full_delivery_v1"
    assert "Across Agents Assistant" not in serialized
    assert "AAA" not in serialized
    assert payload["request"]["host_boundary"] == "host-provided-agent-adapters"
