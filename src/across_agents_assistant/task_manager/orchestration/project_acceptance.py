"""Project-level quality acceptance — deterministic checks at integration time.

Phase 4 of the delivery-quality engineering implementation.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from across_agents_assistant.workspace_hygiene import IGNORED_DIR_NAMES

from .requirements import expand_path_hint_alternatives


@dataclass
class ProjectAcceptanceCheckResult:
    check_type: str
    passed: bool
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectAcceptanceReport:
    passed: bool
    results: List[ProjectAcceptanceCheckResult]
    missing_required: List[str] = field(default_factory=list)
    produced_required: List[str] = field(default_factory=list)
    invalid_required: List[Dict[str, Any]] = field(default_factory=list)


def run_project_acceptance(
    task: Any,
    manifest: Optional[Dict[str, Any]],
    artifact_records: List[Dict[str, Any]],
) -> ProjectAcceptanceReport:
    """Run deterministic project-level quality checks against *manifest*.

    Checks performed:
    1. Required manifest files exist on disk inside ``project_dir``.
    2. Files are non-empty.
    3. Python files parse with ``ast.parse``.
    """
    results: List[ProjectAcceptanceCheckResult] = []
    missing_required: List[str] = []
    produced_required: List[str] = []
    invalid_required: List[Dict[str, Any]] = []

    if not manifest:
        return ProjectAcceptanceReport(passed=True, results=results)

    project_dir = task.project_dir

    blocking_failed = False
    framework_result = _check_requested_framework_alignment(
        getattr(task, "description", "") or "",
        project_dir,
    )
    if framework_result:
        results.append(framework_result)
        if not framework_result.passed:
            blocking_failed = True
            invalid_required.append({
                "path_hint": framework_result.evidence.get("path") or "project",
                "check_type": framework_result.check_type,
                "message": framework_result.message,
            })

    storage_result = _check_requested_storage_alignment(
        getattr(task, "description", "") or "",
        project_dir,
    )
    if storage_result:
        results.append(storage_result)
        if not storage_result.passed:
            blocking_failed = True
            invalid_required.append({
                "path_hint": storage_result.evidence.get("path") or "project",
                "check_type": storage_result.check_type,
                "message": storage_result.message,
            })

    for deliverable in manifest.get("deliverables", []):
        if not deliverable.get("required", True):
            continue
        path_hint = deliverable.get("path_hint")
        if not path_hint:
            continue
        resolved = _first_existing_candidate(path_hint, project_dir)
        if resolved and os.path.isfile(resolved):
            produced_required.append(path_hint)
            results.append(
                ProjectAcceptanceCheckResult(
                    check_type="required_file_exists",
                    passed=True,
                    message=f"Found {path_hint}",
                    evidence={"path": resolved},
                )
            )
            # Check non-empty
            try:
                if os.path.getsize(resolved) == 0:
                    blocking_failed = True
                    invalid_required.append({
                        "path_hint": path_hint,
                        "check_type": "file_non_empty",
                        "message": f"{path_hint} exists but is empty",
                    })
                    results.append(
                        ProjectAcceptanceCheckResult(
                            check_type="file_non_empty",
                            passed=False,
                            message=f"{path_hint} exists but is empty",
                            evidence={"path": resolved, "size": 0},
                        )
                    )
            except OSError:
                pass
            # Check Python syntax
            if path_hint.endswith(".py"):
                try:
                    with open(resolved, "r", encoding="utf-8") as f:
                        ast.parse(f.read())
                except SyntaxError as e:
                    blocking_failed = True
                    invalid_required.append({
                        "path_hint": path_hint,
                        "check_type": "python_syntax_valid",
                        "message": f"{path_hint} has syntax error: {e}",
                    })
                    results.append(
                        ProjectAcceptanceCheckResult(
                            check_type="python_syntax_valid",
                            passed=False,
                            message=f"{path_hint} has syntax error: {e}",
                            evidence={"path": resolved, "syntax_error": str(e)},
                        )
                    )
        else:
            missing_required.append(path_hint)
            results.append(
                ProjectAcceptanceCheckResult(
                    check_type="required_file_exists",
                    passed=False,
                    message=f"Missing {path_hint}",
                    evidence=path_resolution_diagnostics(path_hint, project_dir),
                )
            )

    return ProjectAcceptanceReport(
        passed=not missing_required and not blocking_failed,
        results=results,
        missing_required=missing_required,
        produced_required=produced_required,
        invalid_required=invalid_required,
    )


def _check_requested_framework_alignment(
    description: str,
    project_dir: Optional[str],
) -> Optional[ProjectAcceptanceCheckResult]:
    lower = description.lower()
    if "fastapi" not in lower:
        return None
    if re.search(r"\b(flask|django)\b", lower) and not re.search(r"\b(do\s+not|don't|dont|without|no|avoid)\b.{0,80}\b(flask|django)\b", lower):
        return None

    scanned: List[str] = []
    flask_hits: List[str] = []
    fastapi_hits: List[str] = []
    for path in _iter_source_files(project_dir):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read().lower()
        except OSError:
            continue
        scanned.append(path)
        if "fastapi" in content:
            fastapi_hits.append(path)
        if re.search(r"\b(flask|flask_sqlalchemy|flask-sqlalchemy)\b", content):
            flask_hits.append(path)

    if flask_hits:
        return ProjectAcceptanceCheckResult(
            check_type="requested_framework_alignment",
            passed=False,
            message="User requested FastAPI, but generated files reference Flask/Flask-SQLAlchemy.",
            evidence={"path": flask_hits[0], "flask_hits": flask_hits[:10], "fastapi_hits": fastapi_hits[:10]},
        )
    if not fastapi_hits and scanned:
        return ProjectAcceptanceCheckResult(
            check_type="requested_framework_alignment",
            passed=False,
            message="User requested FastAPI, but no generated source or dependency file references FastAPI.",
            evidence={"scanned_count": len(scanned)},
        )
    return ProjectAcceptanceCheckResult(
        check_type="requested_framework_alignment",
        passed=True,
        message="Generated project matches requested FastAPI framework.",
        evidence={"fastapi_hits": fastapi_hits[:10]},
    )


def _check_requested_storage_alignment(
    description: str,
    project_dir: Optional[str],
) -> Optional[ProjectAcceptanceCheckResult]:
    lower = description.lower()
    if "sqlite" not in lower:
        return None

    scanned: List[str] = []
    sqlite_hits: List[str] = []
    postgres_hits: List[str] = []
    for path in _iter_source_files(project_dir):
        if path.lower().endswith((".md", ".rst")):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read().lower()
        except OSError:
            continue
        scanned.append(path)
        if "sqlite" in content or "aiosqlite" in content or "sqlite3" in content:
            sqlite_hits.append(path)
        if re.search(r"\b(postgresql|postgres|asyncpg|psycopg2|psycopg)\b", content):
            postgres_hits.append(path)

    if postgres_hits:
        return ProjectAcceptanceCheckResult(
            check_type="requested_storage_alignment",
            passed=False,
            message="User requested SQLite, but generated files reference PostgreSQL/asyncpg/psycopg.",
            evidence={"path": postgres_hits[0], "postgres_hits": postgres_hits[:10], "sqlite_hits": sqlite_hits[:10]},
        )
    if not sqlite_hits and scanned:
        return ProjectAcceptanceCheckResult(
            check_type="requested_storage_alignment",
            passed=False,
            message="User requested SQLite, but no generated source or dependency file references SQLite.",
            evidence={"scanned_count": len(scanned)},
        )
    return ProjectAcceptanceCheckResult(
        check_type="requested_storage_alignment",
        passed=True,
        message="Generated project matches requested SQLite storage.",
        evidence={"sqlite_hits": sqlite_hits[:10]},
    )


def _iter_source_files(project_dir: Optional[str]) -> List[str]:
    if not project_dir or not os.path.isdir(project_dir):
        return []
    paths: List[str] = []
    for root, dirnames, filenames in os.walk(project_dir):
        dirnames[:] = [name for name in dirnames if name not in METADATA_DIRS]
        for filename in filenames:
            lower = filename.lower()
            if lower.endswith((".py", ".txt", ".toml", ".md")) or lower in {"requirements.txt"}:
                paths.append(os.path.join(root, filename))
    return paths


def _resolve_path(path_hint: str, project_dir: Optional[str]) -> str:
    """Resolve a path hint against *project_dir*."""
    resolved = path_hint if os.path.isabs(path_hint) else os.path.join(project_dir or "", path_hint)
    return os.path.realpath(resolved)


METADATA_DIRS = set(IGNORED_DIR_NAMES)


def candidate_paths_for_hint(path_hint: str, project_dir: Optional[str]) -> List[str]:
    """Return ordered candidate paths for a path hint without checking ambiguity."""
    alternatives = expand_path_hint_alternatives(path_hint or "")
    if len(alternatives) > 1:
        candidates: List[str] = []
        for alternative in alternatives:
            for candidate in candidate_paths_for_hint(alternative, project_dir):
                if candidate not in candidates:
                    candidates.append(candidate)
        return candidates

    resolved = _resolve_path(path_hint, project_dir)
    candidates: List[str] = []
    if os.path.isfile(resolved):
        candidates.append(resolved)
    else:
        candidates.append(resolved)

    basename = os.path.basename(path_hint)
    lower = basename.lower()

    # README aliases
    if lower == "readme":
        for variant in ("README.md", "README.rst", "README.txt"):
            cand = _resolve_path(variant, project_dir)
            if cand not in candidates:
                candidates.append(cand)

    # tests/test_*.py fallback
    if lower.startswith("test_") and lower.endswith(".py") and "/" not in path_hint:
        cand = _resolve_path(f"tests/{basename}", project_dir)
        if cand not in candidates:
            candidates.append(cand)

    # Relative path: search common source directories as package prefixes.
    if "/" in path_hint and not os.path.isabs(path_hint):
        for prefix in ("app", "src", "lib"):
            cand = _resolve_path(f"{prefix}/{path_hint}", project_dir)
            if cand not in candidates:
                candidates.append(cand)

    # Bare basename: search common source directories
    if "/" not in path_hint:
        for prefix in ("app", "src", "lib"):
            cand = _resolve_path(f"{prefix}/{basename}", project_dir)
            if cand not in candidates:
                candidates.append(cand)

        # tests/ prefix for test files
        if lower.startswith("test_") or lower.endswith("_test.py"):
            cand = _resolve_path(f"tests/{basename}", project_dir)
            if cand not in candidates:
                candidates.append(cand)

    return candidates


def _scan_project_for_basename(basename: str, project_dir: str) -> List[str]:
    """Recursively scan project_dir for files matching basename, excluding metadata dirs."""
    matches: List[str] = []
    try:
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in METADATA_DIRS]
            if basename in files:
                matches.append(os.path.realpath(os.path.join(root, basename)))
    except OSError:
        pass
    return matches


def first_existing_candidate(path_hint: str, project_dir: Optional[str]) -> Optional[str]:
    """Return an exact or uniquely inferred existing file path, else None.

    If a bare path_hint has exactly one matching candidate in the project
    tree (after searching common directories), that candidate is accepted.
    If more than one candidate exists, returns None (ambiguous).
    """
    candidates = candidate_paths_for_hint(path_hint, project_dir)
    existing_from_candidates = [c for c in candidates if os.path.isfile(c)]
    if len(expand_path_hint_alternatives(path_hint or "")) > 1 and existing_from_candidates:
        return existing_from_candidates[0]
    if len(existing_from_candidates) == 1:
        return existing_from_candidates[0]
    if len(existing_from_candidates) > 1:
        return None  # ambiguous — don't guess

    # Bare basename: recursive scan as last resort
    if "/" not in path_hint and project_dir and os.path.isdir(project_dir):
        basename = os.path.basename(path_hint)
        recursive = _scan_project_for_basename(basename, project_dir)
        if len(recursive) == 1:
            return recursive[0]

    return None


def path_resolution_diagnostics(path_hint: str, project_dir: Optional[str]) -> Dict[str, Any]:
    """Return candidate_paths and ambiguous_candidates for error evidence."""
    candidates = candidate_paths_for_hint(path_hint, project_dir)
    existing = [c for c in candidates if os.path.isfile(c)]
    ambiguous: List[str] = []

    if len(existing) > 1:
        ambiguous = existing
    elif not existing and "/" not in path_hint and project_dir and os.path.isdir(project_dir):
        basename = os.path.basename(path_hint)
        recursive = _scan_project_for_basename(basename, project_dir)
        if len(recursive) > 1:
            ambiguous = recursive

    return {
        "candidate_paths": candidates,
        "ambiguous_candidates": ambiguous,
    }


def _candidate_paths(path_hint: str, project_dir: Optional[str]) -> List[str]:
    """Return equivalent file paths for a manifest path_hint (backward compat)."""
    return candidate_paths_for_hint(path_hint, project_dir)


def _first_existing_candidate(path_hint: str, project_dir: Optional[str]) -> Optional[str]:
    """Return the first candidate path that exists as a regular file (backward compat)."""
    return first_existing_candidate(path_hint, project_dir)
