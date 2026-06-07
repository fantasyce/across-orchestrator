from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from across_agents_assistant.workspace_hygiene import IGNORED_DIR_NAMES


QUALITY_SCORE_WEIGHTS = {
    "contract_coverage": 20,
    "artifact_integrity": 15,
    "install_build": 15,
    "automated_tests": 15,
    "runtime_smoke": 15,
    "user_e2e": 15,
    "security_privacy": 5,
}

ADAPTER_DIMENSIONS = {
    "workspace_hygiene": "artifact_integrity",
    "security_privacy": "security_privacy",
    "documentation": "contract_coverage",
    "python": "install_build",
    "python_install": "install_build",
    "python_web_smoke": "runtime_smoke",
    "node_web": "install_build",
    "swift_macos": "install_build",
    "static_web": "runtime_smoke",
    "static_web_smoke": "runtime_smoke",
    "api_service": "runtime_smoke",
    "cli_generic": "runtime_smoke",
    "notes_cli_smoke": "runtime_smoke",
    "browser_e2e": "user_e2e",
    "pytest": "automated_tests",
    "contract_coverage": "contract_coverage",
    "agent_mix": "contract_coverage",
    "artifact_integrity": "artifact_integrity",
    "unknown_stack": "contract_coverage",
}

PASSING_STATUSES = {"passed"}
FAILING_STATUSES = {"failed", "error"}
MANUAL_STATUSES = {"manual_required"}


@dataclass(frozen=True)
class QualityGateResult:
    gate_id: str
    adapter_id: str
    status: str
    required: bool = True
    summary: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    output_tail: str = ""
    blocked_by_environment: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProbeAdapter:
    adapter_id: str
    label: str
    description: str
    detector: Callable[[str, Dict[str, Any]], bool]
    required_by_default: bool = True
    manual_required: bool = False

    def applies_to(self, project_dir: str, contract: Optional[Dict[str, Any]] = None) -> bool:
        return bool(self.detector(project_dir, contract or {}))

    def build_gate(self, project_dir: str, contract: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        status = "manual_required" if self.manual_required else "pending"
        return {
            "id": f"gate-{self.adapter_id}",
            "adapter_id": self.adapter_id,
            "label": self.label,
            "description": self.description,
            "required": self.required_by_default,
            "status": status,
            "source": "probe_adapter_registry",
        }


class ProbeAdapterRegistry:
    def __init__(self, adapters: Sequence[ProbeAdapter]):
        self._adapters = list(adapters)

    @classmethod
    def default(cls) -> "ProbeAdapterRegistry":
        return cls([
            ProbeAdapter(
                "workspace_hygiene",
                "Workspace hygiene",
                "Reject runtime caches, local databases, diagnostic scripts, and excessive output files.",
                lambda project_dir, contract: bool(project_dir),
            ),
            ProbeAdapter(
                "security_privacy",
                "Security and privacy scan",
                "Scan for credentials, private paths, auth added without request, and unsafe local data.",
                lambda project_dir, contract: bool(project_dir),
            ),
            ProbeAdapter(
                "documentation",
                "Documentation review",
                "Check requested docs and avoid unrequested documentation sprawl.",
                _has_documentation_signal,
            ),
            ProbeAdapter(
                "python",
                "Python install and tests",
                "Validate Python install metadata, imports, pytest, and web runtime when relevant.",
                _has_python_signal,
            ),
            ProbeAdapter(
                "node_web",
                "Node web build",
                "Validate package metadata, build scripts, and web dev/runtime smoke for Node projects.",
                _has_node_web_signal,
            ),
            ProbeAdapter(
                "swift_macos",
                "Swift/macOS build",
                "Validate SwiftPM/Xcode macOS build shape when a native app is requested.",
                _has_swift_macos_signal,
            ),
            ProbeAdapter(
                "static_web",
                "Static web smoke",
                "Validate HTML entrypoint, static asset references, and browser-loadable UI.",
                _has_static_web_signal,
            ),
            ProbeAdapter(
                "api_service",
                "API service smoke",
                "Validate service entrypoints, health/root behavior, and API startup evidence.",
                _has_api_service_signal,
            ),
            ProbeAdapter(
                "browser_e2e",
                "Browser E2E",
                "Run user-path browser validation and collect screenshots, console, and network evidence.",
                _needs_browser_e2e,
            ),
            ProbeAdapter(
                "cli_generic",
                "CLI smoke",
                "Run golden command probes for command-line deliveries.",
                _has_cli_signal,
            ),
        ])

    def detect_adapters(self, project_dir: str, contract: Optional[Dict[str, Any]] = None) -> List[ProbeAdapter]:
        contract = contract or {}
        detected = [
            adapter
            for adapter in self._adapters
            if adapter.applies_to(project_dir, contract)
        ]
        if _needs_unknown_stack_gate(project_dir, contract, detected):
            detected.append(
                ProbeAdapter(
                    "unknown_stack",
                    "Manual stack validation",
                    "Source project stack is unclear; do not mark delivery complete without an explicit validation recipe.",
                    lambda _project_dir, _contract: True,
                    required_by_default=True,
                    manual_required=True,
                )
            )
        return detected

    def build_gate_plan(self, project_dir: str, contract: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return [
            adapter.build_gate(project_dir, contract or {})
            for adapter in self.detect_adapters(project_dir, contract or {})
        ]


def build_quality_report(
    *,
    task_id: str,
    contract: Optional[Dict[str, Any]],
    gate_results: Iterable[QualityGateResult | Dict[str, Any]],
    generated_gate_results: Optional[Iterable[QualityGateResult | Dict[str, Any]]] = None,
    remediation_count: int = 0,
    external_fix_count: int = 0,
    evidence_bundle: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    final_results = [_coerce_result(item) for item in gate_results]
    generated_results = (
        [_coerce_result(item) for item in generated_gate_results]
        if generated_gate_results is not None
        else final_results
    )
    final_summary = _summarize_results(final_results)
    generated_summary = _summarize_results(generated_results)
    final_score = _score_results(contract or {}, final_results)
    generated_score = _score_results(contract or {}, generated_results)

    if final_summary["required_failed_count"]:
        quality_gate = "failed"
    elif final_summary["manual_required_count"]:
        quality_gate = "manual_required"
    elif final_summary["required_skipped_count"]:
        quality_gate = "partial"
    else:
        quality_gate = "passed"

    can_complete = quality_gate == "passed"
    return {
        "task_id": task_id,
        "quality_gate": quality_gate,
        "can_complete": can_complete,
        "generated_quality_score": generated_score,
        "final_quality_score": final_score,
        "remediation_count": max(0, int(remediation_count or 0)),
        "external_fix_count": max(0, int(external_fix_count or 0)),
        "required_failed_count": final_summary["required_failed_count"],
        "manual_required_count": final_summary["manual_required_count"],
        "required_skipped_count": final_summary["required_skipped_count"],
        "passed_required_count": final_summary["passed_required_count"],
        "total_required_count": final_summary["total_required_count"],
        "generated_required_failed_count": generated_summary["required_failed_count"],
        "score_breakdown": _score_breakdown(contract or {}, final_results),
        "gate_results": [item.to_dict() for item in final_results],
        "evidence_bundle": evidence_bundle or _default_evidence_bundle(task_id, contract or {}, final_results),
    }


def _coerce_result(item: QualityGateResult | Dict[str, Any]) -> QualityGateResult:
    if isinstance(item, QualityGateResult):
        return item
    return QualityGateResult(
        gate_id=str(item.get("gate_id") or item.get("id") or "gate"),
        adapter_id=str(item.get("adapter_id") or item.get("probe_type") or "unknown"),
        status=str(item.get("status") or ("passed" if item.get("passed") else "failed")),
        required=bool(item.get("required", True)),
        summary=str(item.get("summary") or item.get("message") or item.get("output_tail") or ""),
        evidence=dict(item.get("evidence") or {}),
        output_tail=str(item.get("output_tail") or ""),
        blocked_by_environment=bool(item.get("blocked_by_environment", False)),
    )


def _summarize_results(results: Sequence[QualityGateResult]) -> Dict[str, int]:
    required = [item for item in results if item.required]
    return {
        "total_required_count": len(required),
        "passed_required_count": sum(1 for item in required if item.status in PASSING_STATUSES),
        "required_failed_count": sum(1 for item in required if item.status in FAILING_STATUSES),
        "manual_required_count": sum(1 for item in required if item.status in MANUAL_STATUSES),
        "required_skipped_count": sum(1 for item in required if item.status == "skipped"),
    }


def _score_results(contract: Dict[str, Any], results: Sequence[QualityGateResult]) -> int:
    score = sum(_score_breakdown(contract, results).values())
    if any(item.required and item.status in FAILING_STATUSES for item in results):
        score = min(score, 79)
    if any(item.required and item.status in MANUAL_STATUSES for item in results):
        score = min(score, 74)
    if any(item.required and item.status == "skipped" for item in results):
        score = min(score, 84)
    return max(0, min(100, int(score)))


def _score_breakdown(contract: Dict[str, Any], results: Sequence[QualityGateResult]) -> Dict[str, int]:
    dimensions: Dict[str, List[QualityGateResult]] = {key: [] for key in QUALITY_SCORE_WEIGHTS}
    for result in results:
        dimension = ADAPTER_DIMENSIONS.get(result.adapter_id)
        if dimension:
            dimensions.setdefault(dimension, []).append(result)

    breakdown: Dict[str, int] = {}
    for dimension, weight in QUALITY_SCORE_WEIGHTS.items():
        dimension_results = dimensions.get(dimension) or []
        if dimension == "contract_coverage":
            breakdown[dimension] = _score_contract_coverage(contract, dimension_results, weight)
            continue
        if not dimension_results:
            breakdown[dimension] = 0
        elif any(item.required and item.status in FAILING_STATUSES for item in dimension_results):
            breakdown[dimension] = 0
        elif any(item.required and item.status in MANUAL_STATUSES for item in dimension_results):
            breakdown[dimension] = max(0, weight // 3)
        elif any(item.required and item.status == "skipped" for item in dimension_results):
            breakdown[dimension] = max(0, weight // 2)
        elif all(item.status in PASSING_STATUSES for item in dimension_results if item.required):
            breakdown[dimension] = weight
        else:
            breakdown[dimension] = max(0, weight // 2)
    return breakdown


def _score_contract_coverage(
    contract: Dict[str, Any],
    dimension_results: Sequence[QualityGateResult],
    weight: int,
) -> int:
    required_deliverables = [
        item for item in contract.get("deliverables", []) or []
        if item.get("required", True)
    ]
    required_capabilities = [
        item for item in contract.get("capabilities", []) or []
        if item.get("required", True)
    ]
    gate_plan = contract.get("gate_plan", []) or []
    if any(item.required and item.status in FAILING_STATUSES for item in dimension_results):
        return 0
    if any(item.required and item.status in MANUAL_STATUSES for item in dimension_results):
        return max(0, weight // 3)
    if required_deliverables or required_capabilities or gate_plan:
        return weight
    return max(0, weight // 2)


def _default_evidence_bundle(
    task_id: str,
    contract: Dict[str, Any],
    results: Sequence[QualityGateResult],
) -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "contract_id": contract.get("contract_id"),
        "expected_files": [
            item.get("path_hint")
            for item in contract.get("deliverables", []) or []
            if item.get("path_hint")
        ],
        "gate_result_ids": [item.gate_id for item in results],
        "contains_browser_trace": any(item.adapter_id == "browser_e2e" for item in results),
        "contains_security_scan": any(item.adapter_id == "security_privacy" for item in results),
    }


def _needs_unknown_stack_gate(
    project_dir: str,
    contract: Dict[str, Any],
    detected: Sequence[ProbeAdapter],
) -> bool:
    facets = set(str(item) for item in contract.get("delivery_facets", []) or [])
    if "source_project" not in facets and "runnable_app" not in facets:
        return False
    stack_adapter_ids = {
        "python",
        "node_web",
        "swift_macos",
        "static_web",
        "api_service",
        "cli_generic",
    }
    return not any(adapter.adapter_id in stack_adapter_ids for adapter in detected)


def _project_files(project_dir: str, *, max_files: int = 500) -> List[str]:
    if not project_dir or not os.path.isdir(project_dir):
        return []
    project_root = os.path.realpath(project_dir)
    files: List[str] = []
    for root, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [name for name in dirnames if name not in IGNORED_DIR_NAMES]
        for filename in filenames:
            rel_path = os.path.relpath(os.path.join(root, filename), project_root).replace("\\", "/")
            files.append(rel_path)
            if len(files) >= max_files:
                return files
    return files


def _contract_facets(contract: Dict[str, Any]) -> set[str]:
    return {str(item) for item in contract.get("delivery_facets", []) or []}


def _contract_stacks(contract: Dict[str, Any]) -> set[str]:
    return {
        str(item.get("stack"))
        for item in contract.get("technology_hypotheses", []) or []
        if isinstance(item, dict) and item.get("stack")
    }


def _has_documentation_signal(project_dir: str, contract: Dict[str, Any]) -> bool:
    facets = _contract_facets(contract)
    if "documentation" in facets:
        return True
    return any(path.lower().endswith((".md", ".rst", ".txt")) for path in _project_files(project_dir))


def _has_python_signal(project_dir: str, contract: Dict[str, Any]) -> bool:
    stacks = _contract_stacks(contract)
    if {"python", "python-fastapi"} & stacks:
        return True
    files = _project_files(project_dir)
    return any(path.endswith(".py") for path in files) or any(
        os.path.basename(path) in {"pyproject.toml", "requirements.txt", "setup.py"}
        for path in files
    )


def _has_node_web_signal(project_dir: str, contract: Dict[str, Any]) -> bool:
    stacks = _contract_stacks(contract)
    if "node-web" in stacks:
        return True
    files = _project_files(project_dir)
    return "package.json" in files or any(
        path.lower() in {"vite.config.js", "vite.config.ts", "next.config.js", "next.config.mjs"}
        for path in files
    )


def _has_swift_macos_signal(project_dir: str, contract: Dict[str, Any]) -> bool:
    stacks = _contract_stacks(contract)
    facets = _contract_facets(contract)
    if "swift-macos" in stacks or "desktop_app" in facets:
        return True
    files = _project_files(project_dir)
    return "Package.swift" in files or any(path.endswith((".swift", ".xcodeproj")) for path in files)


def _has_static_web_signal(project_dir: str, contract: Dict[str, Any]) -> bool:
    facets = _contract_facets(contract)
    if "web_ui" in facets:
        return True
    files = set(_project_files(project_dir))
    entrypoints = {"index.html", "static/index.html", "public/index.html", "app/static/index.html"}
    return bool(files & entrypoints)


def _has_api_service_signal(project_dir: str, contract: Dict[str, Any]) -> bool:
    facets = _contract_facets(contract)
    if "api_service" in facets:
        return True
    files = _project_files(project_dir)
    if any(path.endswith(".py") and os.path.basename(path) in {"main.py", "app.py", "server.py"} for path in files):
        return _files_contain(project_dir, files, ("FastAPI(", "Starlette(", "Flask(", "APIRouter("))
    if any(os.path.basename(path) in {"server.js", "app.js", "index.js"} for path in files):
        return _files_contain(project_dir, files, ("express(", "createServer(", "fastify("))
    return False


def _needs_browser_e2e(project_dir: str, contract: Dict[str, Any]) -> bool:
    facets = _contract_facets(contract)
    if "web_ui" in facets:
        return True
    return _has_static_web_signal(project_dir, contract) and (
        "runnable_app" in facets or "source_project" in facets
    )


def _has_cli_signal(project_dir: str, contract: Dict[str, Any]) -> bool:
    facets = _contract_facets(contract)
    if "cli_tool" in facets:
        return True
    files = _project_files(project_dir)
    cli_names = ("cli.py", "main.py", "command.py", "bin/")
    if any(path.endswith(cli_names) for path in files):
        return _files_contain(project_dir, files, ("argparse", "click.", "typer.", "if __name__"))
    return False


def _files_contain(project_dir: str, rel_paths: Iterable[str], markers: Sequence[str]) -> bool:
    project_root = os.path.realpath(project_dir)
    for rel_path in rel_paths:
        if not rel_path.lower().endswith((".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".md")):
            continue
        path = os.path.realpath(os.path.join(project_root, rel_path))
        try:
            if os.path.commonpath([project_root, path]) != project_root:
                continue
        except ValueError:
            continue
        try:
            content = open(path, "r", encoding="utf-8", errors="ignore").read(200000)
        except OSError:
            continue
        if any(marker in content for marker in markers):
            return True
    return False
