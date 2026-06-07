from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List


IGNORED_DIR_NAMES = {
    ".git",
    ".claude",
    ".codex",
    ".cursor",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "htmlcov",
    "node_modules",
    "venv",
}

RUNTIME_DATA_DIR_NAMES = {
    "instance",
    "logs",
    "receipts",
    "tmp",
    "uploads",
}

RUNTIME_FILE_SUFFIXES = {
    ".db",
    ".db-shm",
    ".db-wal",
    ".log",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
}

ROOT_DIAGNOSTIC_PATTERNS = (
    "_*.py",
    "_*.sh",
    "check_*.py",
    "check_*.sh",
    "do_install.py",
    "install_*.py",
    "install_*.sh",
    "install*.py",
    "run_check*.py",
    "run_check*.sh",
    "run_*.py",
    "run_*.sh",
    "run_all_check*.py",
    "run_all_check*.sh",
    "run_direct*.py",
    "run_direct*.sh",
    "run_do_install*.py",
    "run_exec*.py",
    "run_exec*.sh",
    "run_fix*.py",
    "run_full_check*.py",
    "run_install*.py",
    "run_install*.sh",
    "run_list*.py",
    "run_pip*.py",
    "run_pip*.sh",
    "run_simple*.py",
    "run_simple*.sh",
    "run_syntax*.py",
    "run_syntax*.sh",
    "run_test*.py",
    "run_test*.sh",
    "runner.py",
    "setup_*env*.py",
    "setup_*env*.sh",
    "setup_test*.py",
    "setup_test*.sh",
    "cleanup*.py",
    "test_script.py",
    "test_import*.py",
    "test_syntax*.py",
    "tmp_*.py",
    "verify*.py",
)


def _relative_parts(path: str, project_dir: str | None) -> List[str]:
    resolved = os.path.realpath(path)
    if not project_dir:
        return Path(resolved).parts[-1:]
    try:
        rel_path = os.path.relpath(resolved, os.path.realpath(project_dir))
    except ValueError:
        return Path(resolved).parts[-1:]
    if rel_path.startswith(".."):
        return Path(resolved).parts[-1:]
    return [part for part in rel_path.split(os.sep) if part and part != "."]


def is_workspace_noise_path(path: str, project_dir: str | None = None) -> bool:
    """Return True for runtime/cache/diagnostic files that are not deliverables."""
    parts = _relative_parts(path, project_dir)
    if not parts:
        return False

    if any(part in IGNORED_DIR_NAMES or part.endswith(".egg-info") for part in parts[:-1]):
        return True
    if any(part in RUNTIME_DATA_DIR_NAMES for part in parts[:-1]):
        return True

    basename = parts[-1]
    lower = basename.lower()
    if lower == ".ds_store":
        return True
    if any(lower.endswith(suffix) for suffix in RUNTIME_FILE_SUFFIXES):
        return True

    if len(parts) == 1:
        if basename.startswith("__"):
            return False
        return any(fnmatch.fnmatch(basename, pattern) for pattern in ROOT_DIAGNOSTIC_PATTERNS)

    return False


def filtered_workspace_files(paths: Iterable[str], project_dir: str | None) -> List[str]:
    result: List[str] = []
    seen = set()
    for path in paths or []:
        if not path:
            continue
        resolved = os.path.realpath(path)
        if is_workspace_noise_path(resolved, project_dir):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)
    return result


def iter_workspace_noise_files(project_dir: str) -> List[str]:
    """Return all runtime/cache/diagnostic files under *project_dir*."""
    project_root = os.path.realpath(project_dir)
    if not os.path.isdir(project_root):
        return []
    noise: List[str] = []
    for root, _dirnames, filenames in os.walk(project_root):
        for filename in filenames:
            full_path = os.path.realpath(os.path.join(root, filename))
            if is_workspace_noise_path(full_path, project_root):
                noise.append(full_path)
    return sorted(noise)


def scan_workspace_hygiene(
    project_dir: str,
    *,
    max_delivery_files: int = 1000,
    evidence_limit: int = 25,
) -> Dict[str, Any]:
    """Scan a project directory for non-deliverable noise and oversized output."""
    project_root = os.path.realpath(project_dir)
    noise: List[str] = []
    delivery_count = 0
    total_count = 0

    if not os.path.isdir(project_root):
        return {
            "passed": True,
            "total_file_count": 0,
            "delivery_file_count": 0,
            "noise_file_count": 0,
            "noise_evidence": [],
            "max_delivery_files": max_delivery_files,
        }

    for root, dirnames, filenames in os.walk(project_root):
        for filename in filenames:
            full_path = os.path.realpath(os.path.join(root, filename))
            total_count += 1
            if is_workspace_noise_path(full_path, project_root):
                if len(noise) < evidence_limit:
                    noise.append(full_path)
                continue
            delivery_count += 1

    return {
        "passed": not noise and delivery_count <= max_delivery_files,
        "total_file_count": total_count,
        "delivery_file_count": delivery_count,
        "noise_file_count": _count_noise_files(project_root),
        "noise_evidence": noise,
        "max_delivery_files": max_delivery_files,
    }


def _count_noise_files(project_root: str) -> int:
    count = 0
    for root, _dirnames, filenames in os.walk(project_root):
        for filename in filenames:
            if is_workspace_noise_path(os.path.join(root, filename), project_root):
                count += 1
    return count
