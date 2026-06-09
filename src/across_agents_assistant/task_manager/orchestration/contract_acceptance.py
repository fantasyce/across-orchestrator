from __future__ import annotations

from html.parser import HTMLParser
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

from across_agents_assistant.workspace_hygiene import IGNORED_DIR_NAMES, scan_workspace_hygiene
from across_agents_assistant.agent_ids import LOCAL_CLI_AGENT_IDS
from across_agents_assistant.llm_gateway.provider_registry import get_default_provider_ids

from .project_acceptance import (
    _check_requested_framework_alignment,
    _check_requested_storage_alignment,
    first_existing_candidate,
)
from .quality_gates import QualityGateResult, build_quality_report


LOCAL_AGENT_IDS = set(LOCAL_CLI_AGENT_IDS)
CLOUD_AGENT_IDS = set(get_default_provider_ids())


def _python_probe_executable() -> str:
    """Return a real Python interpreter for project acceptance probes.

    In a PyInstaller bundle ``sys.executable`` points at the packaged backend
    binary, so ``backend -m pytest`` or ``backend -m venv`` is invalid.  Use a
    system Python in that case and keep ``sys.executable`` for source checkouts.
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    for candidate in (
        shutil.which("python3"),
        shutil.which("python"),
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return sys.executable


def _node_probe_executable() -> str | None:
    for candidate in (
        shutil.which("node"),
        "/opt/homebrew/bin/node",
        "/usr/local/bin/node",
        "/usr/bin/node",
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _playwright_node_path(project_dir: str) -> str:
    candidates = [
        os.environ.get("ACROSS_PLAYWRIGHT_NODE_PATH"),
        os.path.join(project_dir, "node_modules"),
        os.path.join(os.getcwd(), "node_modules"),
        os.path.expanduser("~/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"),
        os.path.expanduser("~/.codex/node_modules"),
        "/opt/homebrew/lib/node_modules",
        "/usr/local/lib/node_modules",
    ]
    existing = [path for path in candidates if path and os.path.isdir(path)]
    current = os.environ.get("NODE_PATH")
    if current:
        existing.append(current)
    return os.pathsep.join(existing)


def _scan_for_docker(project_dir: str) -> List[str]:
    matches: List[str] = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
        for filename in files:
            if filename.lower() in {"dockerfile", "containerfile", "docker-compose.yml", "docker-compose.yaml"}:
                matches.append(os.path.realpath(os.path.join(root, filename)))
    return matches


def _scan_for_forbidden_file(project_dir: str, value: str, scope: str = "recursive") -> List[str]:
    target = str(value or "").strip()
    if not target:
        return []
    normalized_target = target.replace("\\", "/").strip("/")
    target_basename = os.path.basename(normalized_target).lower()
    has_path_component = "/" in normalized_target
    matches: List[str] = []

    project_root = os.path.realpath(project_dir)
    if scope in {"project_root", "root"} and not has_path_component:
        root_candidate = os.path.realpath(os.path.join(project_root, normalized_target))
        if os.path.isfile(root_candidate):
            return [root_candidate]
        return []
    if scope in {"project_root", "root", "exact"} and has_path_component:
        exact_candidate = os.path.realpath(os.path.join(project_root, normalized_target))
        if os.path.isfile(exact_candidate):
            return [exact_candidate]
        return []

    ignored_dirs = set(IGNORED_DIR_NAMES)
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        for filename in files:
            full_path = os.path.realpath(os.path.join(root, filename))
            rel_path = os.path.relpath(full_path, project_root).replace("\\", "/")
            if has_path_component:
                if rel_path.lower() == normalized_target.lower():
                    matches.append(full_path)
            elif filename.lower() == target_basename:
                matches.append(full_path)
    return sorted(matches)


def _scan_for_disallowed_files(project_dir: str, allowed_files: List[str]) -> List[str]:
    project_root = os.path.realpath(project_dir)
    allowed = {
        str(path or "").replace("\\", "/").strip("/")
        for path in allowed_files or []
        if str(path or "").strip()
    }
    ignored_dirs = set(IGNORED_DIR_NAMES)
    ignored_names = {".ds_store"}
    disallowed: List[str] = []
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        for filename in files:
            if filename.lower() in ignored_names:
                continue
            full_path = os.path.realpath(os.path.join(root, filename))
            rel_path = os.path.relpath(full_path, project_root).replace("\\", "/")
            if rel_path not in allowed:
                disallowed.append(full_path)
    return sorted(disallowed)


def _scan_for_disallowed_documentation_files(project_dir: str, allowed_files: List[str]) -> List[str]:
    project_root = os.path.realpath(project_dir)
    allowed = {
        str(path or "").replace("\\", "/").strip("/").lower()
        for path in allowed_files or []
        if str(path or "").strip()
    }
    ignored_dirs = set(IGNORED_DIR_NAMES)
    non_documentation_text_files = {"requirements.txt"}
    disallowed: List[str] = []
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        for filename in files:
            lower_filename = filename.lower()
            if lower_filename in non_documentation_text_files:
                continue
            if not lower_filename.endswith((".md", ".rst", ".txt")):
                continue
            full_path = os.path.realpath(os.path.join(root, filename))
            rel_path = os.path.relpath(full_path, project_root).replace("\\", "/").lower()
            if rel_path not in allowed:
                disallowed.append(full_path)
    return sorted(disallowed)


def _scan_for_unrequested_auth(project_dir: str) -> List[str]:
    """Find strong signals that auth/login was added without being requested."""
    project_root = os.path.realpath(project_dir)
    ignored_dirs = set(IGNORED_DIR_NAMES)
    scanned_exts = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".toml", ".txt", ".in"}
    strong_markers = (
        "passlib",
        "bcrypt",
        "python-jose",
        " jose",
        "pyjwt",
        "authlib",
        "oauth2passwordbearer",
        "get_password_hash",
        "verify_password",
        "password_hash",
        "jwt.encode",
        "jwt.decode",
        "create_access_token",
        "authenticate_user",
        "login_for_access_token",
        "tokenurl=",
        "/login",
        "/token",
        "/register",
    )
    suspicious_basenames = {"auth.py", "security.py", "users.py", "user.py"}
    matches: List[str] = []
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        for filename in files:
            full_path = os.path.realpath(os.path.join(root, filename))
            rel_path = os.path.relpath(full_path, project_root).replace("\\", "/")
            lower_rel = rel_path.lower()
            basename = os.path.basename(lower_rel)
            _, ext = os.path.splitext(filename)
            if basename in suspicious_basenames:
                matches.append(full_path)
                continue
            if ext.lower() not in scanned_exts and basename not in {"requirements.txt", "pyproject.toml", "setup.py"}:
                continue
            try:
                content = open(full_path, "r", encoding="utf-8", errors="ignore").read(300000).lower()
            except OSError:
                continue
            if any(marker in content for marker in strong_markers):
                matches.append(full_path)
                continue
            if re.search(r"\b(password|passwd)\b", content) and re.search(r"\b(login|register|token|authenticat|authoriz)\b", content):
                matches.append(full_path)
    return sorted(set(matches))


def _normalize_root(root: str) -> str:
    normalized = str(root or ".").replace("\\", "/").strip()
    if normalized in {"", "."}:
        return "."
    return normalized.strip("/")


def _path_in_allowed_roots(rel_path: str, allowed_roots: List[str]) -> bool:
    if not allowed_roots:
        return True
    normalized_rel = rel_path.replace("\\", "/").strip("/")
    for root in allowed_roots:
        normalized_root = _normalize_root(root)
        if normalized_root == ".":
            return True
        if normalized_rel == normalized_root or normalized_rel.startswith(normalized_root.rstrip("/") + "/"):
            return True
    return False


def _count_group_files(project_dir: str, group: Dict[str, Any]) -> int:
    project_root = os.path.realpath(project_dir)
    allowed_roots = list(group.get("allowed_roots") or [])
    allowed_extensions = {
        str(ext or "").lower()
        for ext in group.get("allowed_extensions") or []
        if str(ext or "").strip()
    }
    ignored_dirs = set(IGNORED_DIR_NAMES)
    count = 0
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in ignored_dirs]
        for filename in files:
            if filename.lower() == ".ds_store":
                continue
            full_path = os.path.realpath(os.path.join(root, filename))
            rel_path = os.path.relpath(full_path, project_root).replace("\\", "/")
            if not _path_in_allowed_roots(rel_path, allowed_roots):
                continue
            if allowed_extensions:
                _, ext = os.path.splitext(filename)
                if ext.lower() not in allowed_extensions:
                    continue
            count += 1
    return count


def _group_entrypoint_exists(project_dir: str, path_hint: str) -> bool:
    candidate = os.path.realpath(os.path.join(project_dir, str(path_hint or "").replace("\\", "/").strip("/")))
    try:
        if os.path.commonpath([os.path.realpath(project_dir), candidate]) != os.path.realpath(project_dir):
            return False
    except ValueError:
        return False
    return os.path.isfile(candidate) or os.path.isdir(candidate)


def _validate_deliverable_groups(project_dir: str, groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    invalid: List[Dict[str, Any]] = []
    for group in groups or []:
        if not group.get("required", True):
            continue
        group_id = group.get("id") or group.get("kind") or "deliverable-group"
        one_of = [str(item) for item in group.get("one_of") or [] if str(item or "").strip()]
        if one_of and not any(_group_entrypoint_exists(project_dir, item) for item in one_of):
            invalid.append({
                "path_hint": " / ".join(one_of),
                "candidate_path_hints": one_of,
                "group_id": group_id,
                "check_type": "deliverable_group_one_of",
                "message": f"Required deliverable group {group_id} needs one of: {', '.join(one_of)}",
            })

        one_of_entrypoints = [
            str(item)
            for item in group.get("one_of_entrypoints") or []
            if str(item or "").strip()
        ]
        if (group.get("kind") == "frontend_source" or group_id == "group-web-ui") and "app/static/index.html" not in one_of_entrypoints:
            one_of_entrypoints.append("app/static/index.html")
        if one_of_entrypoints and not any(_group_entrypoint_exists(project_dir, item) for item in one_of_entrypoints):
            invalid.append({
                "path_hint": " / ".join(one_of_entrypoints),
                "candidate_path_hints": one_of_entrypoints,
                "group_id": group_id,
                "check_type": "deliverable_group_entrypoint",
                "message": f"Required deliverable group {group_id} needs at least one entrypoint.",
            })

        for entrypoint in group.get("required_entrypoints") or []:
            if not _group_entrypoint_exists(project_dir, str(entrypoint)):
                invalid.append({
                    "path_hint": str(entrypoint),
                    "group_id": group_id,
                    "check_type": "deliverable_group_entrypoint",
                    "message": f"Required deliverable group {group_id} is missing entrypoint {entrypoint}.",
                })

        min_file_count = int(group.get("min_file_count") or 0)
        if min_file_count > 0:
            count = _count_group_files(project_dir, group)
            if count < min_file_count:
                invalid.append({
                    "path_hint": group_id,
                    "group_id": group_id,
                    "check_type": "deliverable_group_min_file_count",
                    "message": f"Required deliverable group {group_id} has {count} files; expected at least {min_file_count}.",
                    "actual_count": count,
                    "expected_min": min_file_count,
                })
    return invalid


def _run_pytest(project_dir: str) -> Dict[str, Any]:
    try:
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.setdefault("PYTEST_ADDOPTS", "")
        env["PYTEST_ADDOPTS"] = (env["PYTEST_ADDOPTS"] + " -p no:cacheprovider").strip()
        proc = subprocess.run(
            [_python_probe_executable(), "-m", "pytest", "-p", "no:cacheprovider"],
            cwd=project_dir,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
        return {
            "probe_type": "pytest",
            "passed": proc.returncode == 0,
            "returncode": proc.returncode,
            "output_tail": proc.stdout[-4000:],
            "blocked_by_environment": False,
        }
    except FileNotFoundError as exc:
        return {
            "probe_type": "pytest",
            "passed": False,
            "returncode": None,
            "output_tail": str(exc),
            "blocked_by_environment": True,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "probe_type": "pytest",
            "passed": False,
            "returncode": None,
            "output_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "pytest timed out",
            "blocked_by_environment": False,
        }


def _run_python_install(project_dir: str) -> Dict[str, Any]:
    """Verify Python delivery can be installed in an isolated virtual environment."""
    if not _python_install_metadata_exists(project_dir):
        return {
            "probe_type": "python_install",
            "passed": True,
            "returncode": 0,
            "output_tail": "No pyproject.toml or requirements.txt found; install probe skipped.",
            "blocked_by_environment": False,
            "skipped": True,
        }

    try:
        with tempfile.TemporaryDirectory(prefix="across-install-probe-") as venv_dir:
            env = dict(os.environ)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONPATH"] = (
                project_dir
                if not env.get("PYTHONPATH")
                else project_dir + os.pathsep + env["PYTHONPATH"]
            )
            create_proc = subprocess.run(
                [_python_probe_executable(), "-m", "venv", venv_dir],
                cwd=project_dir,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
            if create_proc.returncode != 0:
                return {
                    "probe_type": "python_install",
                    "passed": False,
                    "returncode": create_proc.returncode,
                    "output_tail": (create_proc.stdout or "")[-4000:],
                    "blocked_by_environment": True,
                    "stage": "venv",
                }

            python_bin = os.path.join(
                venv_dir,
                "Scripts" if os.name == "nt" else "bin",
                "python",
            )
            install_args = _python_install_args(project_dir, python_bin)
            install_proc = subprocess.run(
                install_args,
                cwd=project_dir,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=180,
            )
            return {
                "probe_type": "python_install",
                "passed": install_proc.returncode == 0,
                "returncode": install_proc.returncode,
                "output_tail": (install_proc.stdout or "")[-4000:],
                "blocked_by_environment": False,
                "stage": "install",
                "command": " ".join(install_args[3:]),
            }
    except FileNotFoundError as exc:
        return {
            "probe_type": "python_install",
            "passed": False,
            "returncode": None,
            "output_tail": str(exc),
            "blocked_by_environment": True,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "probe_type": "python_install",
            "passed": False,
            "returncode": None,
            "output_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "python install probe timed out",
            "blocked_by_environment": False,
        }


def _python_install_metadata_exists(project_dir: str) -> bool:
    return os.path.isfile(os.path.join(project_dir, "pyproject.toml")) or os.path.isfile(
        os.path.join(project_dir, "requirements.txt")
    )


def _python_install_args(project_dir: str, python_bin: str) -> List[str]:
    args = [python_bin, "-m", "pip", "install", "--disable-pip-version-check"]
    if os.path.isfile(os.path.join(project_dir, "pyproject.toml")):
        args.extend(["-e", ".[dev]"])
    else:
        args.extend(["-r", os.path.join(project_dir, "requirements.txt")])
    return args


def _discover_python_web_app_candidates(project_dir: str) -> List[str]:
    """Return likely ``module:app`` ASGI entry points for generated Python web apps."""
    project_root = os.path.realpath(project_dir)
    candidates: List[str] = []
    ignored_dirs = set(IGNORED_DIR_NAMES)
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in ignored_dirs and d not in {"tests", "test"}]
        for filename in files:
            if not filename.endswith(".py"):
                continue
            full_path = os.path.realpath(os.path.join(root, filename))
            rel_path = os.path.relpath(full_path, project_root).replace("\\", "/")
            if rel_path.startswith(("tests/", "test/")) or "/tests/" in rel_path:
                continue
            try:
                content = open(full_path, "r", encoding="utf-8", errors="ignore").read(300000)
            except OSError:
                continue
            if "FastAPI(" not in content and "Starlette(" not in content:
                continue
            module = rel_path[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[: -len(".__init__")]
            attrs = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:FastAPI|Starlette)\s*\(", content)
            if not attrs:
                attrs = ["app"]
            for attr in attrs:
                spec = f"{module}:{attr}"
                if spec not in candidates:
                    candidates.append(spec)

    def sort_key(spec: str) -> tuple[int, int, str]:
        module, attr = spec.split(":", 1)
        common_module = 0 if module in {"main", "app.main"} or module.endswith(".main") else 1
        common_attr = 0 if attr == "app" else 1
        return (common_module, common_attr, spec)

    return sorted(candidates, key=sort_key)


def _run_python_web_smoke(project_dir: str, *, require_html_root: bool = False) -> Dict[str, Any]:
    """Verify a generated Python web app imports, starts its lifespan, and serves root/UI."""
    candidates = _discover_python_web_app_candidates(project_dir)
    if not candidates:
        return {
            "probe_type": "python_web_smoke",
            "passed": True,
            "returncode": 0,
            "output_tail": "No FastAPI/Starlette app entry point found; web smoke probe skipped.",
            "blocked_by_environment": False,
            "skipped": True,
        }
    if not _python_install_metadata_exists(project_dir):
        return {
            "probe_type": "python_web_smoke",
            "passed": False,
            "returncode": None,
            "output_tail": "Python web smoke requires pyproject.toml or requirements.txt install metadata.",
            "blocked_by_environment": False,
            "stage": "metadata",
        }

    smoke_source = f"""
import importlib
import sys
import traceback

CANDIDATES = {candidates!r}
REQUIRE_HTML_ROOT = {bool(require_html_root)!r}

errors = []
app = None
used_spec = None
for spec in CANDIDATES:
    try:
        module_name, attr = spec.split(":", 1)
        module = importlib.import_module(module_name)
        app = getattr(module, attr)
        used_spec = spec
        break
    except Exception as exc:
        errors.append(f"{{spec}}: {{type(exc).__name__}}: {{exc}}")

if app is None:
    print("Could not import ASGI app:")
    print("\\n".join(errors))
    sys.exit(1)

try:
    try:
        from fastapi.testclient import TestClient
    except Exception:
        from starlette.testclient import TestClient
except Exception as exc:
    print(f"Could not import TestClient: {{type(exc).__name__}}: {{exc}}")
    sys.exit(1)

try:
    with TestClient(app) as client:
        root = client.get("/")
        root_type = root.headers.get("content-type", "")
        print(f"GET / -> {{root.status_code}} {{root_type}}")
        if root.status_code >= 500:
            print(root.text[:1000])
            sys.exit(1)
        if REQUIRE_HTML_ROOT:
            def is_html_frontend(response):
                response_type = response.headers.get("content-type", "").lower()
                body = response.text.lower()
                htmlish = "<!doctype" in body or "<html" in body or "<body" in body or "<script" in body
                return "text/html" in response_type and htmlish

            frontend_ok = is_html_frontend(root)
            if not frontend_ok:
                for frontend_path in ("/static/index.html", "/index.html"):
                    response = client.get(frontend_path)
                    print(f"GET {{frontend_path}} -> {{response.status_code}} {{response.headers.get('content-type', '')}}")
                    if response.status_code < 500 and is_html_frontend(response):
                        frontend_ok = True
                        break
                    if response.status_code >= 500:
                        print(response.text[:1000])
                        sys.exit(1)
            if not frontend_ok:
                print("Expected GET / or /static/index.html to serve a usable HTML frontend.")
                sys.exit(1)

        schema = client.get("/openapi.json")
        if schema.status_code < 500 and "json" in schema.headers.get("content-type", "").lower():
            paths = schema.json().get("paths", {{}})
            checked = 0
            for path, methods in paths.items():
                if checked >= 8 or path in {{"/", "/openapi.json"}} or "{{" in path:
                    continue
                if not isinstance(methods, dict):
                    continue
                operation = methods.get("get")
                if not isinstance(operation, dict):
                    continue
                params = operation.get("parameters") or []
                if any(param.get("required") for param in params if param.get("in") in {{"query", "header", "cookie"}}):
                    continue
                response = client.get(path)
                checked += 1
                print(f"GET {{path}} -> {{response.status_code}}")
                if response.status_code >= 500:
                    print(response.text[:1000])
                    sys.exit(1)
        print(f"python web smoke ok via {{used_spec}}")
except Exception:
    traceback.print_exc()
    sys.exit(1)
"""

    try:
        with tempfile.TemporaryDirectory(prefix="across-web-smoke-") as probe_dir:
            venv_dir = os.path.join(probe_dir, "venv")
            script_path = os.path.join(probe_dir, "smoke.py")
            with open(script_path, "w", encoding="utf-8") as handle:
                handle.write(smoke_source)

            env = dict(os.environ)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONPATH"] = (
                project_dir
                if not env.get("PYTHONPATH")
                else project_dir + os.pathsep + env["PYTHONPATH"]
            )
            create_proc = subprocess.run(
                [_python_probe_executable(), "-m", "venv", venv_dir],
                cwd=project_dir,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
            if create_proc.returncode != 0:
                return {
                    "probe_type": "python_web_smoke",
                    "passed": False,
                    "returncode": create_proc.returncode,
                    "output_tail": (create_proc.stdout or "")[-4000:],
                    "blocked_by_environment": True,
                    "stage": "venv",
                }

            python_bin = os.path.join(
                venv_dir,
                "Scripts" if os.name == "nt" else "bin",
                "python",
            )
            install_proc = subprocess.run(
                _python_install_args(project_dir, python_bin),
                cwd=project_dir,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=180,
            )
            if install_proc.returncode != 0:
                return {
                    "probe_type": "python_web_smoke",
                    "passed": False,
                    "returncode": install_proc.returncode,
                    "output_tail": (install_proc.stdout or "")[-4000:],
                    "blocked_by_environment": False,
                    "stage": "install",
                }

            probe_deps_proc = subprocess.run(
                [python_bin, "-m", "pip", "install", "--disable-pip-version-check", "httpx"],
                cwd=project_dir,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
            )
            if probe_deps_proc.returncode != 0:
                return {
                    "probe_type": "python_web_smoke",
                    "passed": False,
                    "returncode": probe_deps_proc.returncode,
                    "output_tail": (probe_deps_proc.stdout or "")[-4000:],
                    "blocked_by_environment": False,
                    "stage": "probe_dependencies",
                }

            smoke_proc = subprocess.run(
                [python_bin, script_path],
                cwd=project_dir,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=60,
            )
            return {
                "probe_type": "python_web_smoke",
                "passed": smoke_proc.returncode == 0,
                "returncode": smoke_proc.returncode,
                "output_tail": (smoke_proc.stdout or "")[-4000:],
                "blocked_by_environment": False,
                "stage": "smoke",
                "candidates": candidates,
                "require_html_root": require_html_root,
            }
    except FileNotFoundError as exc:
        return {
            "probe_type": "python_web_smoke",
            "passed": False,
            "returncode": None,
            "output_tail": str(exc),
            "blocked_by_environment": True,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "probe_type": "python_web_smoke",
            "passed": False,
            "returncode": None,
            "output_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "python web smoke probe timed out",
            "blocked_by_environment": False,
        }


STATIC_WEB_ENTRYPOINTS = (
    "index.html",
    "web/index.html",
    "static/index.html",
    "public/index.html",
    "app/static/index.html",
)


VOID_HTML_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

OPTIONAL_END_HTML_TAGS = {
    "p",
    "li",
    "dt",
    "dd",
    "option",
    "tr",
    "td",
    "th",
    "thead",
    "tbody",
    "tfoot",
}


class _StaticHtmlStructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: List[str] = []
        self.errors: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized not in VOID_HTML_TAGS:
            self.stack.append(normalized)

    def handle_startendtag(self, tag: str, attrs: List[tuple[str, str | None]]) -> None:
        return

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in VOID_HTML_TAGS:
            return
        while self.stack and self.stack[-1] in OPTIONAL_END_HTML_TAGS and self.stack[-1] != normalized:
            self.stack.pop()
        if not self.stack:
            self.errors.append(f"unexpected closing tag </{normalized}>")
            return
        if self.stack[-1] == normalized:
            self.stack.pop()
            return
        if normalized in self.stack:
            expected = self.stack[-1]
            self.errors.append(f"mismatched closing tag </{normalized}> while </{expected}> was still open")
            while self.stack:
                popped = self.stack.pop()
                if popped == normalized:
                    break
            return
        self.errors.append(f"unexpected closing tag </{normalized}>")


def _discover_static_web_entrypoint(project_dir: str) -> str | None:
    for rel_path in STATIC_WEB_ENTRYPOINTS:
        candidate = os.path.join(project_dir, rel_path)
        if os.path.isfile(candidate):
            return rel_path
    return None


def _read_static_html(project_dir: str, entrypoint: str) -> str:
    html_path = os.path.join(project_dir, entrypoint)
    return open(html_path, "r", encoding="utf-8", errors="ignore").read(300000)


def _html_local_refs(html: str) -> set[str]:
    refs: set[str] = set()
    for raw_ref in re.findall(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", html, flags=re.IGNORECASE):
        ref = raw_ref.strip()
        if not ref or ref.startswith(("#", "//")):
            continue
        lowered = ref.lower()
        if lowered.startswith(("http://", "https://", "data:", "mailto:", "tel:", "javascript:")):
            continue
        ref_path = ref.split("#", 1)[0].split("?", 1)[0].strip()
        if ref_path:
            refs.add(ref_path.replace("\\", "/").lstrip("./"))
    return refs


def _static_html_structure_failures(project_dir: str, entrypoint: str) -> List[str]:
    try:
        html = _read_static_html(project_dir, entrypoint)
    except OSError as exc:
        return [f"{entrypoint}: {exc}"]
    parser = _StaticHtmlStructureParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        return [f"{entrypoint}: parser error: {exc}"]
    return [f"{entrypoint}: {message}" for message in parser.errors[:8]]


def _static_asset_reference_failures(project_dir: str, entrypoint: str) -> List[str]:
    try:
        html = _read_static_html(project_dir, entrypoint)
    except OSError as exc:
        return [f"{entrypoint}: {exc}"]

    failures: List[str] = []
    entry_dir = os.path.dirname(entrypoint)
    for ref_path in _html_local_refs(html):
        if ref_path.startswith("/"):
            candidate = os.path.join(project_dir, ref_path.lstrip("/"))
        else:
            candidate = os.path.join(project_dir, entry_dir, ref_path)
        if not os.path.isfile(os.path.realpath(candidate)):
            failures.append(ref_path)
    return failures


def _required_static_asset_link_failures(
    project_dir: str,
    entrypoint: str,
    contract: Dict[str, Any] | None,
) -> List[str]:
    if not contract:
        return []
    try:
        html = _read_static_html(project_dir, entrypoint)
    except OSError as exc:
        return [f"{entrypoint}: {exc}"]
    refs = _html_local_refs(html)
    required_assets: List[str] = []
    for deliverable in contract.get("deliverables", []) or []:
        if not deliverable.get("required", True):
            continue
        path_hint = str(deliverable.get("path_hint") or "").strip().replace("\\", "/").lstrip("./")
        if not path_hint.lower().endswith((".css", ".js", ".mjs")):
            continue
        if not _is_static_web_asset_path(path_hint, entrypoint):
            continue
        if os.path.isfile(os.path.join(project_dir, path_hint)):
            required_assets.append(path_hint)
    missing: List[str] = []
    for asset in required_assets:
        asset_key = asset.lstrip("./")
        asset_basename = os.path.basename(asset_key)
        if asset_key not in refs and asset_basename not in {os.path.basename(ref) for ref in refs}:
            missing.append(asset)
    return missing


def _is_static_web_asset_path(path_hint: str, entrypoint: str) -> bool:
    """Return True when a deliverable is expected to be loaded by static HTML.

    Composite tasks can include JavaScript files that are not browser assets,
    such as Node API services, CLI tools, or smoke tests.  The static smoke
    probe should require links for CSS/JS that belong to the web surface only.
    """
    normalized = str(path_hint or "").replace("\\", "/").strip().lstrip("./")
    if not normalized:
        return False
    lowered = normalized.lower()
    if lowered.startswith(("api/", "cli/", "tests/", "test/", "backend/", "server/")):
        return False

    entry_dir = os.path.dirname(entrypoint).replace("\\", "/").strip("/")
    if entry_dir:
        return lowered == entry_dir.lower() or lowered.startswith(entry_dir.lower() + "/")

    if "/" not in normalized:
        return True
    return lowered.startswith(("static/", "public/", "assets/", "web/"))


STATIC_WEB_FEATURE_RULES = [
    {
        "label": "theme toggle",
        "triggers": [r"\btheme\s+toggle\b", r"\blight\s*/\s*dark\b", r"\bdark\s+theme\b"],
        "evidence": [[r"\btheme\b", r"data-theme"], [r"\btoggle\b", r"localstorage", r"prefers-color-scheme", r"\bdark\b"]],
    },
    {
        "label": "agent capability cards",
        "triggers": [r"\bagent\s+capabilit(?:y|ies)\s+cards?\b", r"\bagent\b.{0,40}\bcards?\b", r"\bcapabilit(?:y|ies)\b.{0,40}\bcards?\b"],
        "evidence": [[r"\bagent\b"], [r"capabilit", r"\bcards?\b"], [r"\bcards?\b", r"\.agent-card\b"]],
    },
    {
        "label": "task orchestration timeline",
        "triggers": [r"\btask\s+orchestration\s+timeline\b", r"\borchestration\s+timeline\b", r"\btimeline\b"],
        "evidence": [[r"\btimeline\b"], [r"\borchestrat", r"\btask\b"]],
    },
    {
        "label": "quality checklist",
        "triggers": [r"\bquality\s+checklist\b", r"\bchecklist\b"],
        "evidence": [[r"\bquality\b"], [r"\bchecklist\b", r"\bcheckbox\b", r"\bchecks?\b"]],
    },
    {
        "label": "task detail panel",
        "triggers": [r"\btask\s+detail\b", r"\bdetail\s+panel\b"],
        "evidence": [[r"\btask\b"], [r"\bdetail\b"], [r"\bpanel\b", r"\bdrawer\b", r"\bsection\b"]],
    },
    {
        "label": "keyboard-friendly interactions",
        "triggers": [r"\bkeyboard\b", r"\btab\b.{0,40}\benter\b", r"\benter\b.{0,40}\bescape\b"],
        "evidence": [[r"\bkeydown\b", r"\bkeyup\b", r"\bkeypress\b", r"\btabindex\b", r"\baria-"]],
    },
]


def _read_static_web_source_text(project_dir: str) -> str:
    chunks: List[str] = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
        for filename in files:
            if not filename.lower().endswith((".html", ".css", ".js", ".jsx", ".ts", ".tsx")):
                continue
            path = os.path.join(root, filename)
            try:
                chunks.append(open(path, "r", encoding="utf-8", errors="ignore").read(200000))
            except OSError:
                continue
    return "\n".join(chunks).lower()


def _read_static_script_source_text(project_dir: str, *, preserve_case: bool = False) -> str:
    chunks: List[str] = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
        for filename in files:
            if not filename.lower().endswith((".js", ".jsx", ".ts", ".tsx")):
                continue
            path = os.path.join(root, filename)
            try:
                content = open(path, "r", encoding="utf-8", errors="ignore").read(200000)
                chunks.append(content if preserve_case else content.lower())
            except OSError:
                continue
    joined = "\n".join(chunks)
    return joined if preserve_case else joined.lower()


def _read_static_project_text(project_dir: str) -> str:
    chunks: List[str] = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
        for filename in files:
            if not filename.lower().endswith((".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".md", ".txt")):
                continue
            path = os.path.join(root, filename)
            try:
                chunks.append(open(path, "r", encoding="utf-8", errors="ignore").read(200000))
            except OSError:
                continue
    return "\n".join(chunks).lower()


def _normalize_requirement_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _contains_required_phrase(normalized_text: str, phrase: str) -> bool:
    normalized_phrase = _normalize_requirement_text(phrase)
    return bool(normalized_phrase) and normalized_phrase in normalized_text


def _strip_static_source_comments(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"<!--[\s\S]*?-->", " ", text)
    text = re.sub(r"/\*[\s\S]*?\*/", " ", text)
    lines: List[str] = []
    for line in text.splitlines():
        lines.append(re.sub(r"(?<!:)//.*$", " ", line))
    return "\n".join(lines)


class _VisibleBodyTextParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "title", "template", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__()
        self.in_body = False
        self.skip_depth = 0
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized == "body":
            self.in_body = True
        if self.in_body and normalized in self.SKIP_TAGS:
            self.skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if self.in_body and normalized in self.SKIP_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1
        if normalized == "body":
            self.in_body = False

    def handle_data(self, data: str) -> None:
        if self.in_body and self.skip_depth == 0 and data.strip():
            self.parts.append(data)

    @property
    def text(self) -> str:
        return " ".join(self.parts)


def _static_html_visible_body_text(project_dir: str, entrypoint: str | None) -> str:
    if not entrypoint:
        return ""
    try:
        html = _read_static_html(project_dir, entrypoint)
    except OSError:
        return ""
    parser = _VisibleBodyTextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return ""
    return parser.text


class _StaticHtmlIdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs_list: List[tuple[str, str | None]]) -> None:
        for key, value in attrs_list:
            if key.lower() == "id" and value:
                self.ids.add(value)


def _static_html_ids(project_dir: str, entrypoint: str | None) -> set[str]:
    if not entrypoint:
        return set()
    try:
        html = _read_static_html(project_dir, entrypoint)
    except OSError:
        return set()
    parser = _StaticHtmlIdParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return set()
    return parser.ids


class _StaticSectionTextParser(HTMLParser):
    VOID_TAGS = VOID_HTML_TAGS

    def __init__(self) -> None:
        super().__init__()
        self.sections: List[str] = []
        self.current: List[str] | None = None
        self.depth = 0

    def handle_starttag(self, tag: str, attrs_list: List[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        attrs = {key.lower(): (value or "") for key, value in attrs_list}
        if normalized == "section" and self.current is None:
            self.current = []
            self.depth = 1
            for key in ("aria-label", "id", "class"):
                if attrs.get(key):
                    self.current.append(attrs[key])
        elif self.current is not None and normalized not in self.VOID_TAGS:
            self.depth += 1

        if self.current is not None:
            for key in ("aria-label", "title", "value"):
                if attrs.get(key):
                    self.current.append(attrs[key])

    def handle_data(self, data: str) -> None:
        if self.current is not None and data.strip():
            self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        if tag.lower() in self.VOID_TAGS:
            return
        self.depth -= 1
        if self.depth <= 0:
            self.sections.append(" ".join(self.current))
            self.current = None
            self.depth = 0


def _static_section_texts(project_dir: str, entrypoint: str | None) -> List[str]:
    if not entrypoint:
        return []
    try:
        html = _read_static_html(project_dir, entrypoint)
    except OSError:
        return []
    parser = _StaticSectionTextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return []
    return parser.sections


def _static_display_surface_text(project_dir: str, entrypoint: str | None) -> str:
    """Return non-comment static text that can plausibly become visible UI."""
    parts = [
        _static_html_visible_body_text(project_dir, entrypoint),
        _static_agent_card_text(project_dir, entrypoint),
        _strip_static_source_comments(_read_static_script_source_text(project_dir, preserve_case=True)),
    ]
    return "\n".join(part for part in parts if part)


def _contains_display_phrase(surface_text: str, phrase: str) -> bool:
    normalized_surface = re.sub(r"\s+", " ", str(surface_text or ""))
    normalized_phrase = re.sub(r"\s+", " ", str(phrase or "")).strip()
    return bool(normalized_phrase) and normalized_phrase in normalized_surface


class _StaticCardTextParser(HTMLParser):
    VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__()
        self.cards: List[str] = []
        self.current: List[str] | None = None
        self.depth = 0

    def handle_starttag(self, tag: str, attrs_list: List[tuple[str, str | None]]) -> None:
        attrs = {key.lower(): (value or "") for key, value in attrs_list}
        class_name = attrs.get("class", "")
        is_card = bool(re.search(r"\b(?:agent|llm)-card\b", class_name, flags=re.IGNORECASE))
        if is_card and self.current is None:
            self.current = []
            self.depth = 1
        elif self.current is not None and tag.lower() not in self.VOID_TAGS:
            self.depth += 1

    def handle_data(self, data: str) -> None:
        if self.current is not None and data.strip():
            self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        self.depth -= 1
        if self.depth <= 0:
            self.cards.append(" ".join(self.current))
            self.current = None
            self.depth = 0

    @property
    def text(self) -> str:
        return " ".join(self.cards)


def _static_agent_card_text(project_dir: str, entrypoint: str | None) -> str:
    if not entrypoint:
        return ""
    try:
        html = _read_static_html(project_dir, entrypoint)
    except OSError:
        return ""
    parser = _StaticCardTextParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return ""
    return parser.text


def _static_innerhtml_contexts(source_text: str, anchors: List[str]) -> List[str]:
    semantic = _strip_static_source_comments(source_text).lower()
    normalized_anchors = [anchor.lower() for anchor in anchors if anchor]
    dynamic_anchors = set(normalized_anchors)
    for anchor in normalized_anchors:
        escaped = re.escape(anchor)
        for match in re.finditer(
            r"\b(?:const|let|var)\s+([a-z_$][a-z0-9_$]*)\s*=\s*[^;\n]*" + escaped,
            semantic,
            flags=re.IGNORECASE,
        ):
            dynamic_anchors.add(match.group(1).lower())
    normalized_anchors = sorted(dynamic_anchors)
    contexts: List[str] = []
    for match in re.finditer(r"\binnerhtml\s*=", semantic, flags=re.IGNORECASE):
        near_start = max(0, match.start() - 520)
        near_end = min(len(semantic), match.end() + 360)
        near_context = semantic[near_start:near_end]
        target_context = semantic[near_start:match.end()]
        if not normalized_anchors or any(anchor in near_context for anchor in normalized_anchors):
            if normalized_anchors and not any(anchor in target_context for anchor in normalized_anchors):
                continue
            start = max(0, match.start() - 1400)
            end = min(len(semantic), match.end() + 2600)
            contexts.append(semantic[start:end])
    return contexts


def _static_runtime_visible_text(source_text: str) -> str:
    semantic = _strip_static_source_comments(source_text)
    chunks: List[str] = []
    assignment_pattern = (
        r"\b(?:innerHTML|textContent|innerText)\s*=\s*"
        r"(`[\s\S]{0,1800}?`|'[^']{0,1800}'|\"[^\"]{0,1800}\")"
    )
    for match in re.finditer(assignment_pattern, semantic, flags=re.IGNORECASE):
        chunks.append(match.group(1))
    for match in re.finditer(
        r"\binsertAdjacentHTML\s*\(\s*[^,]+,\s*"
        r"(`[\s\S]{0,1800}?`|'[^']{0,1800}'|\"[^\"]{0,1800}\")",
        semantic,
        flags=re.IGNORECASE,
    ):
        chunks.append(match.group(1))
    return "\n".join(chunks)


def _static_visible_or_runtime_text(project_dir: str, entrypoint: str | None) -> str:
    visible = _static_html_visible_body_text(project_dir, entrypoint)
    runtime = _static_runtime_visible_text(
        _read_static_script_source_text(project_dir, preserve_case=True)
    )
    return f"{visible}\n{runtime}"


def _static_dynamic_overwrite_missing_terms(source_text: str, anchors: List[str], terms: List[str]) -> List[str]:
    contexts = _static_innerhtml_contexts(source_text, anchors)
    if not contexts:
        return []
    missing: List[str] = []
    for term in terms:
        normalized_term = _normalize_requirement_text(term)
        if any(_contains_required_phrase(_normalize_requirement_text(context), term) for context in contexts):
            continue
        if normalized_term == "mcp risk" and any(
            "risk" in _normalize_requirement_text(context)
            for context in contexts
        ):
            continue
        if normalized_term == "matched" and any(
            re.search(r"\b(?:matched|skill|capability)\b", _normalize_requirement_text(context))
            for context in contexts
        ):
            continue
        if normalized_term == "reason" and any(
            "reason" in _normalize_requirement_text(context)
            or "routing reason" in _normalize_requirement_text(context)
            for context in contexts
        ):
            continue
        missing.append(term)
    return missing


def _static_missing_js_dom_id_references(project_dir: str, entrypoint: str | None) -> List[str]:
    """Catch scripts that update ids that are not present in the static entrypoint.

    Static app deliveries commonly wire controls through ``getElementById``.  If the
    target id never exists, the page can load while core panels silently stay stale.
    Dynamic creation is allowed when the same source explicitly assigns that id.
    """
    html_ids = _static_html_ids(project_dir, entrypoint)
    if not html_ids:
        return []

    script_source = _strip_static_source_comments(_read_static_script_source_text(project_dir, preserve_case=True))
    if not script_source:
        return []

    referenced_ids: set[str] = set()
    referenced_ids.update(
        match.group(1)
        for match in re.finditer(
            r"\bgetElementById\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
            script_source,
            flags=re.IGNORECASE,
        )
    )
    referenced_ids.update(
        match.group(1)
        for match in re.finditer(
            r"\bquerySelector\s*\(\s*['\"]#([A-Za-z_][A-Za-z0-9_:\-\.]*)['\"]\s*\)",
            script_source,
            flags=re.IGNORECASE,
        )
    )

    missing: List[str] = []
    for dom_id in sorted(referenced_ids):
        if dom_id in html_ids:
            continue
        escaped = re.escape(dom_id)
        dynamically_created = bool(
            re.search(r"\.id\s*=\s*['\"]" + escaped + r"['\"]", script_source)
            or re.search(
                r"\.setAttribute\s*\(\s*['\"]id['\"]\s*,\s*['\"]" + escaped + r"['\"]\s*\)",
                script_source,
            )
            or re.search(r"\bid\s*:\s*['\"]" + escaped + r"['\"]", script_source)
        )
        if not dynamically_created:
            missing.append(f"runtime DOM target missing: #{dom_id}")
    return missing


def _static_web_interaction_source_failures(source_text: str, description: str) -> List[str]:
    normalized_description = _normalize_requirement_text(description)
    raw = _strip_static_source_comments(source_text)
    lowered = raw.lower()
    failures: List[str] = []

    if (
        _contains_required_phrase(normalized_description, "functional and artifact")
        or (
            _contains_required_phrase(normalized_description, "functional")
            and _contains_required_phrase(normalized_description, "artifact")
            and _contains_required_phrase(normalized_description, "mode")
        )
    ):
        has_functional_artifact_controls = "functional" in lowered and "artifact" in lowered and (
            "role=\"switch\"" in lowered
            or "type=\"checkbox\"" in lowered
            or "type='checkbox'" in lowered
            or "radio" in lowered
        )
        one_way_artifact_reset = bool(
            re.search(r"\bmodeartifact\s*\.\s*checked\s*=\s*false\b", raw, flags=re.IGNORECASE)
            and not re.search(r"\bmodefunctional\s*\.\s*checked\s*=\s*false\b", raw, flags=re.IGNORECASE)
        )
        if has_functional_artifact_controls and one_way_artifact_reset:
            failures.append("functional/artifact mode toggle cannot select Artifact")
        if "contract-tab" in lowered or "role=\"tab\"" in lowered or "role='tab'" in lowered:
            tab_click_handlers = []
            for match in re.finditer(r"addEventListener\s*\(\s*['\"]click['\"]", raw, flags=re.IGNORECASE):
                handler_window = raw[match.start():match.start() + 900]
                tab_click_handlers.append(handler_window)
            tab_handlers = [
                handler for handler in tab_click_handlers
                if "contractstate.mode" in handler.lower() or "tab-functional" in handler.lower() or "tab-artifact" in handler.lower()
            ]
            if tab_handlers and not any(
                re.search(r"aria-selected|classlist\s*\.\s*(?:add|remove|toggle)|\bhidden\b", handler, flags=re.IGNORECASE)
                for handler in tab_handlers
            ):
                failures.append("functional/artifact mode tab does not update active state")

    if "checklist" in lowered and re.search(r"checklist\s*\.\s*addeventlistener\s*\(\s*['\"]click", lowered):
        guards_input = bool(re.search(r"tagname\s*={2,3}\s*['\"]input['\"]", lowered))
        guards_label = bool(re.search(r"tagname\s*={2,3}\s*['\"]label['\"]", lowered))
        if guards_input and not guards_label:
            failures.append("checklist label click double-toggle risk")
        for match in re.finditer(r"checklist\s*\.\s*addeventlistener\s*\(\s*['\"]click['\"]", raw, flags=re.IGNORECASE):
            handler_window = raw[match.start():match.start() + 1100]
            handler_lower = handler_window.lower()
            handles_label_click = bool(
                re.search(r"target\s*\.\s*tagname\s*!={1,2}\s*['\"]label['\"]", handler_lower)
                or re.search(r"target\s*\.\s*tagname\s*={2,3}\s*['\"]label['\"]", handler_lower)
                or re.search(r"\bclosest\s*\(\s*['\"]label['\"]\s*\)", handler_lower)
            )
            suppresses_default = "preventdefault" in handler_lower
            toggles_checkbox = bool(
                re.search(r"\b(?:cb|checkbox|input)\s*\.\s*checked\s*=\s*!\s*(?:cb|checkbox|input)\s*\.\s*checked\b", handler_lower)
                or re.search(r"\b(?:cb|checkbox|input)\s*\.\s*click\s*\(", handler_lower)
            )
            if handles_label_click and suppresses_default and not toggles_checkbox:
                failures.append("checklist label click suppresses checkbox toggle")

    route_evidence_requested = (
        "route evidence" in normalized_description
        or ("evidence panel" in normalized_description and "route" in normalized_description)
    )
    if route_evidence_requested:
        recompute_button_count = len(re.findall(r"\brecompute-btn\b", lowered))
        route_has_recompute = bool(
            re.search(r"route[-_\s]*evidence[\s\S]{0,900}\brecompute-btn\b", raw, flags=re.IGNORECASE)
        )
        binds_first_recompute = bool(
            re.search(r"\bqueryselector\s*\(\s*['\"]\.recompute-btn['\"]\s*\)", lowered)
        )
        if route_has_recompute and recompute_button_count > 2 and binds_first_recompute:
            failures.append("route evidence recompute button may bind first matching control")

        if "reason text" in normalized_description:
            render_chunks = [
                match.group(1)
                for match in re.finditer(
                    r"\b(?:innerhtml|textcontent|innertext)\s*=\s*(`[\s\S]{0,1200}?`|'[^']{0,1200}'|\"[^\"]{0,1200}\")",
                    raw,
                    flags=re.IGNORECASE,
                )
            ]
            route_row_chunks = [
                chunk
                for chunk in render_chunks
                if re.search(r"\b(?:selected|matched|mcp\s*risk|native\s*skill)\b", chunk, flags=re.IGNORECASE)
            ]
            has_separate_reason_update = bool(
                re.search(r"\bev[-_]?reason\b", raw, flags=re.IGNORECASE)
                or re.search(r"textContent\s*=\s*['\"]reason\s*=", raw, flags=re.IGNORECASE)
                or re.search(r"textcontent\s*=\s*reason\b", lowered)
                or re.search(r"\b\w+\s*\.\s*reason\b", raw, flags=re.IGNORECASE)
                or re.search(r"\[\s*['\"]reason['\"]\s*\]", raw, flags=re.IGNORECASE)
                or ("route-reason" in lowered and re.search(r"\breason(?:text)?\b", lowered))
            )
            if route_row_chunks and not any(
                re.search(r"\breason\b|\$\{[^}]*reason[^}]*\}", chunk, flags=re.IGNORECASE)
                for chunk in route_row_chunks
            ) and not has_separate_reason_update:
                failures.append("route evidence runtime row missing: reason")

    return failures


def _static_js_runtime_risk_failures(script_source_text: str) -> List[str]:
    raw = _strip_static_source_comments(script_source_text)
    failures: List[str] = []

    def add_missing_loop_indexes(params: str, body: str) -> None:
        defined_names = set(re.findall(r"\b[A-Za-z_$][A-Za-z0-9_$]*\b", params))
        for loop_name in ("i", "j"):
            if loop_name in defined_names:
                continue
            if re.search(r"\b" + loop_name + r"\b", body):
                message = f"javascript runtime risk: forEach callback uses undefined index variable {loop_name}"
                if message not in failures:
                    failures.append(message)

    lines = raw.splitlines()
    for line_number, line in enumerate(lines):
        lowered_line = line.lower()
        if ".foreach" not in lowered_line or "=>" not in line:
            continue
        inline_params = re.search(r"\.forEach\s*\(\s*(?:async\s*)?\(([^)]*)\)\s*=>", line, flags=re.IGNORECASE)
        if not inline_params:
            continue
        body_lines: List[str] = []
        for next_line in lines[line_number + 1: line_number + 90]:
            stripped = next_line.strip()
            if stripped.startswith("});") or stripped.startswith("})"):
                break
            body_lines.append(next_line)
        add_missing_loop_indexes(inline_params.group(1), "\n".join(body_lines))

    for match in re.finditer(
        r"\.forEach\s*\(\s*(?:async\s*)?\(([^)]*)\)\s*=>\s*\{([\s\S]{0,2600}?)\}\s*\)",
        raw,
        flags=re.IGNORECASE,
    ):
        add_missing_loop_indexes(match.group(1), match.group(2))

    if re.search(r"\b(?:const|let)\s+allCheckboxes\b[\s\S]{0,600}addEventListener\s*\(\s*['\"]keydown", raw, flags=re.IGNORECASE):
        # Alternate emitted shape: declaration is inside keydown callback and a
        # later change callback reuses it, causing a runtime ReferenceError.
        if re.search(r"addEventListener\s*\(\s*['\"]change['\"][\s\S]{0,900}\ballCheckboxes\b", raw, flags=re.IGNORECASE):
            failures.append("javascript runtime risk: change handler uses allCheckboxes outside its scope")
    elif re.search(r"addEventListener\s*\(\s*['\"]keydown['\"][\s\S]{0,900}\b(?:const|let)\s+allCheckboxes\b", raw, flags=re.IGNORECASE):
        if re.search(r"addEventListener\s*\(\s*['\"]change['\"][\s\S]{0,900}\ballCheckboxes\b", raw, flags=re.IGNORECASE):
            failures.append("javascript runtime risk: change handler uses allCheckboxes outside its scope")

    if re.search(r"\.\s*parentnode\s*\.\s*insertbefore\s*\(", raw, flags=re.IGNORECASE):
        failures.append("javascript runtime risk: insertBefore uses parentNode before element is attached")

    if "canvas" in raw.lower() and "createradialgradient" in raw.lower():
        resize_call = re.search(r"\bresizecanvas\s*\(\s*\)\s*;", raw, flags=re.IGNORECASE)
        node_class = re.search(
            r"\bclass\s+([A-Za-z_$][\w$]*)\s*\{[\s\S]{0,800}?\bconstructor\s*\([^)]*\)\s*\{[\s\S]{0,240}?\bthis\.reset\s*\(",
            raw,
            flags=re.IGNORECASE,
        )
        new_node = None
        if node_class:
            new_node = re.search(r"\bnew\s+" + re.escape(node_class.group(1)) + r"\s*\(", raw, flags=re.IGNORECASE)
        has_canvas_resize = bool(
            re.search(
                r"\bfunction\s+resizecanvas\s*\([^)]*\)\s*\{[\s\S]{0,500}?canvas\.(?:width|height)\s*=",
                raw,
                flags=re.IGNORECASE,
            )
        )
        uses_uninitialized_dimensions = bool(
            re.search(r"\b(?:let|var)\s+(?:w\s*,\s*h|width\s*,\s*height)\b", raw, flags=re.IGNORECASE)
            and re.search(r"\b(?:w|h|width|height)\s*[-+*/]", raw, flags=re.IGNORECASE)
        )
        if (
            resize_call
            and new_node
            and new_node.start() < resize_call.start()
            and has_canvas_resize
            and uses_uninitialized_dimensions
        ):
            failures.append("javascript runtime risk: canvas nodes initialized before dimensions")

    return failures


def _split_requested_entities(value: str) -> List[str]:
    cleaned = re.sub(r"\s+and\s+", ",", value or "", flags=re.IGNORECASE)
    entities: List[str] = []
    for item in cleaned.split(","):
        item = re.sub(r"^\s*(?:and|or)\s+", "", item, flags=re.IGNORECASE).strip()
        item = re.sub(r"\s+", " ", item)
        item = item.strip(" .;:-")
        if item and len(item) <= 80:
            entities.append(item)
    return entities


def _requested_include_entities(description: str) -> List[tuple[str, str]]:
    entities: List[tuple[str, str]] = []
    patterns = [
        ("Local Agents", r"\blocal\s+agents?\s+must\s+include\s+([^.\n]+)"),
        ("Cloud LLMs", r"\bcloud\s+llms?\s+must\s+include\s+([^.\n]+)"),
        ("Cloud LLMs", r"\bcloud\s+models?\s+must\s+include\s+([^.\n]+)"),
    ]
    for label, pattern in patterns:
        for match in re.finditer(pattern, description, flags=re.IGNORECASE):
            for entity in _split_requested_entities(match.group(1)):
                entities.append((label, entity))
    return entities


def _requested_agent_routing_entities(description: str) -> List[str]:
    if not re.search(r"\b(agent|llm|routing|route|能力|智能体)\b", description or "", flags=re.IGNORECASE):
        return []
    entities: List[str] = []
    for match in re.finditer(r"\bacross\s+([^.\n]+)", description or "", flags=re.IGNORECASE):
        prefix = (description or "")[max(0, match.start() - 40):match.start()].lower()
        if re.search(r"(?:called|named|titled)\s+[\"'“”]?$", prefix):
            continue
        segment = match.group(1)
        segment = re.split(
            r"\b(?:implement|show|inside|with|using|that|which|for|must|should)\b",
            segment,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        for entity in _split_requested_entities(segment):
            normalized = _normalize_requirement_text(entity)
            if normalized in {"agent", "agents", "local agents", "cloud llms", "llms"}:
                continue
            if re.fullmatch(r"\d+\s+agents?", normalized):
                continue
            entities.append(entity)

    result: List[str] = []
    seen: set[str] = set()
    for entity in entities:
        key = _normalize_requirement_text(entity)
        if key and key not in seen:
            seen.add(key)
            result.append(entity)
    return result


def _requested_static_app_names(description: str) -> List[str]:
    names: List[str] = []
    patterns = [
        r"\b(?:static\s+web\s+app|web\s+app|application|app|site|tool|game)\s+"
        r"(?:called|named)\s+[\"'“”]?([^\"'“”.\n]+?)[\"'“”]?"
        r"(?=\s+(?:inside|in|with|that|which|for|using|to|and|must|should)|[.,;\n]|$)",
        r"(?:名为|叫做)[「《\"'“]?([^」》\"'”。\n]+)[」》\"'”]?",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, description or "", flags=re.IGNORECASE):
            name = re.sub(r"\s+", " ", match.group(1) or "").strip(" .,:;\"'“”")
            if 2 <= len(name) <= 80 and name.lower() not in {"app", "application", "site", "tool", "game"}:
                names.append(name)
    result: List[str] = []
    seen: set[str] = set()
    for name in names:
        key = _normalize_requirement_text(name)
        if key and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def _requested_delivery_report_metrics(description: str) -> List[str]:
    normalized_description = _normalize_requirement_text(description or "")
    if not _contains_required_phrase(normalized_description, "delivery report"):
        return []
    metrics = [
        "generated quality score",
        "final quality score",
        "required gate failures",
        "manual checks",
        "skipped checks",
        "final verdict",
    ]
    return [
        metric for metric in metrics
        if _contains_required_phrase(normalized_description, metric)
    ]


def _requested_native_skill_display_entities(description: str) -> List[str]:
    entities: List[str] = []
    for match in re.finditer(
        r"\b(?:unavailable\s+)?([A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){1,4})\s+native\s+skill\b",
        description or "",
    ):
        entity = re.sub(r"\s+", " ", match.group(1) or "").strip()
        entity = re.sub(
            r"^(?:Add|Build|Create|Display|Expose|Include|Render|Show|Use)\s+",
            "",
            entity,
            flags=re.IGNORECASE,
        ).strip()
        if entity and entity.lower() not in {"native agent"}:
            entities.append(entity)
    result: List[str] = []
    seen: set[str] = set()
    for entity in entities:
        key = _normalize_requirement_text(entity)
        if key and key not in seen:
            seen.add(key)
            result.append(entity)
    return result


def _repair_advice_display_failures(description: str, display_surface: str) -> List[str]:
    normalized_description = _normalize_requirement_text(description)
    if not _contains_required_phrase(normalized_description, "repair advice"):
        return []

    failures: List[str] = []
    entities = _requested_native_skill_display_entities(description)
    if not entities:
        if "repair" not in _normalize_requirement_text(display_surface):
            failures.append("repair advice text")
        return failures

    for entity in entities:
        escaped = re.escape(entity)
        has_nearby_repair = bool(
            re.search(escaped + r"[\s\S]{0,260}\brepair\b", display_surface, flags=re.IGNORECASE)
            or re.search(r"\brepair\b[\s\S]{0,260}" + escaped, display_surface, flags=re.IGNORECASE)
        )
        if not has_nearby_repair:
            failures.append(f"repair advice display text for {entity}")
    return failures


def _is_negative_package_context(context: str, marker: str) -> bool:
    normalized = re.sub(r"\s+", " ", context.lower())
    escaped = re.escape(marker.lower())
    negative_before = re.search(
        r"\b(no|not|never|without|avoid|forbid|forbidden|exclude|omits?|missing|absent)\b.{0,80}"
        + escaped,
        normalized,
    )
    do_not_before = re.search(
        r"\b(do\s+not|don't|does\s+not|is\s+not|are\s+not|not\s+required|not\s+needed|no\s+need\s+to)\b.{0,80}"
        + escaped,
        normalized,
    )
    negative_after = re.search(
        escaped
        + r".{0,80}\b(not\s+required|not\s+needed|is\s+absent|are\s+absent|is\s+not\s+needed|should\s+not\s+exist)\b",
        normalized,
    )
    return bool(negative_before or do_not_before or negative_after)


_PACKAGE_MANAGER_COMMAND_MARKERS = [
    "npm install",
    "npm run",
    "yarn install",
    "pnpm install",
    "bun install",
]


def _line_has_forbidden_package_manager_instruction(line: str) -> str | None:
    for marker in _PACKAGE_MANAGER_COMMAND_MARKERS:
        if re.search(re.escape(marker), line):
            if not _is_negative_package_context(line, marker):
                return marker

    if re.search(r"\bnode_modules\b", line):
        if _is_negative_package_context(line, "node_modules"):
            return None
        if re.search(r"\b(create|install|include|commit|generate|copy|use|require|requires|run)\b", line):
            return "node_modules"
    return None


def _has_forbidden_package_manager_instruction(project_text: str) -> bool:
    for line in project_text.splitlines():
        if _line_has_forbidden_package_manager_instruction(line):
            return True
    return False


def _forbidden_package_manager_instruction_locations(project_dir: str) -> List[str]:
    project_root = os.path.realpath(project_dir)
    locations: List[str] = []
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
        for filename in files:
            if not filename.lower().endswith((".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".md", ".txt")):
                continue
            path = os.path.realpath(os.path.join(root, filename))
            rel_path = os.path.relpath(path, project_root).replace("\\", "/")
            try:
                content = open(path, "r", encoding="utf-8", errors="ignore").read(200000)
            except OSError:
                continue
            for line_number, raw_line in enumerate(content.splitlines(), start=1):
                line = raw_line.lower()
                marker = _line_has_forbidden_package_manager_instruction(line)
                if marker:
                    locations.append(f"{rel_path}:{line_number} ({marker})")
    return locations


def _static_web_explicit_requirement_failures(project_dir: str, task_description: str | None) -> List[str]:
    description = task_description or ""
    normalized_description = _normalize_requirement_text(description)
    if not normalized_description:
        return []

    source_text = _read_static_web_source_text(project_dir)
    script_source_text = _read_static_script_source_text(project_dir)
    project_text = _read_static_project_text(project_dir)
    semantic_source_text = _strip_static_source_comments(source_text)
    entrypoint = _discover_static_web_entrypoint(project_dir)
    try:
        html_text = _read_static_html(project_dir, entrypoint) if entrypoint else ""
    except OSError:
        html_text = ""
    normalized_source = _normalize_requirement_text(semantic_source_text)
    normalized_visible_body = _normalize_requirement_text(
        _static_html_visible_body_text(project_dir, entrypoint)
    )
    display_surface = _static_display_surface_text(project_dir, entrypoint)
    normalized_agent_surface = _normalize_requirement_text(
        _static_agent_card_text(project_dir, entrypoint)
        + "\n"
        + "\n".join(_static_innerhtml_contexts(script_source_text, ["agent", "agents", "llm", "route"]))
    )
    visible_runtime_surface = _static_visible_or_runtime_text(project_dir, entrypoint)
    failures: List[str] = []

    for label, entity in _requested_include_entities(description):
        if not _contains_required_phrase(normalized_source, entity):
            failures.append(f"{label} missing requested item: {entity}")

    for entity in _requested_agent_routing_entities(description):
        if not _contains_required_phrase(normalized_agent_surface, entity):
            failures.append(f"agent routing surface missing requested item: {entity}")
            continue
        if any(char.isupper() for char in entity) and not _contains_display_phrase(display_surface, entity):
            failures.append(f"agent routing display text missing requested item: {entity}")

    for name in _requested_static_app_names(description):
        if not _contains_required_phrase(normalized_visible_body, name):
            failures.append(f"application name: {name} (must be visible in the page body, not only the title)")

    for entity in _requested_native_skill_display_entities(description):
        if not _contains_display_phrase(visible_runtime_surface, entity):
            failures.append(f"native skill display text missing requested item: {entity}")
    failures.extend(_repair_advice_display_failures(description, visible_runtime_surface))

    requested_report_metrics = _requested_delivery_report_metrics(description)
    for metric in requested_report_metrics:
        if not _contains_required_phrase(normalized_source, metric):
            failures.append(f"delivery report metric: {metric}")
    dynamic_missing_metrics = _static_dynamic_overwrite_missing_terms(
        script_source_text,
        [
            "delivery-report",
            "delivery report",
            "reportmetrics",
            "report-metrics",
            "report metrics",
            "reportcontent",
            "report-content",
            "report content",
        ],
        requested_report_metrics,
    )
    for metric in dynamic_missing_metrics:
        failures.append(f"delivery report runtime metric: {metric}")
    failures.extend(_static_missing_js_dom_id_references(project_dir, entrypoint))
    failures.extend(_static_web_interaction_source_failures(source_text, description))
    failures.extend(_static_js_runtime_risk_failures(script_source_text))

    requested_sections = [
        ("Local Agents", "local agents"),
        ("Cloud LLMs", "cloud llms"),
        ("Skill Matrix", "skill matrix"),
        ("Task Composer", "task composer"),
        ("Route Preview", "route preview"),
    ]
    for label, phrase in requested_sections:
        if _contains_required_phrase(normalized_description, phrase) and not _contains_required_phrase(normalized_source, phrase):
            failures.append(f"{label} section")

    raw_source = source_text.lower()
    raw_html = html_text.lower()
    if "priority selector" in normalized_description or "priority selection" in normalized_description:
        if "priority" not in raw_source or not re.search(r"<select\b|<option\b|segmented|radio", raw_source):
            failures.append("priority selector")

    if "strict mode toggle" in normalized_description or "strict mode" in normalized_description:
        strict_control_pattern = (
            r"(?:strict[-\s]*mode|strict).{0,180}"
            r"(?:toggle|type=[\"']checkbox|role=[\"']switch)"
            r"|(?:toggle|type=[\"']checkbox|role=[\"']switch).{0,180}"
            r"(?:strict[-\s]*mode|strict)"
        )
        if not re.search(strict_control_pattern, raw_html, flags=re.IGNORECASE | re.DOTALL):
            failures.append("strict-mode toggle")

    if "recompute" in normalized_description or "recomputes" in normalized_description:
        if "recompute" not in raw_source:
            failures.append("recompute route button")

    route_evidence_requested = (
        "route evidence" in normalized_description
        or ("evidence panel" in normalized_description and "assigned" in normalized_description)
    )
    if route_evidence_requested:
        visible_sections = _static_section_texts(project_dir, entrypoint)
        normalized_sections = [
            _normalize_requirement_text(section)
            for section in visible_sections
        ]
        route_sections = [
            section
            for section in normalized_sections
            if _contains_required_phrase(section, "route evidence")
            or _contains_required_phrase(section, "route preview")
        ]
        if _contains_required_phrase(normalized_description, "route evidence") and not any(
            _contains_required_phrase(section, "route evidence")
            for section in normalized_sections
        ):
            failures.append("route evidence section heading")
        if route_sections and not any("recompute" in section for section in route_sections):
            failures.append("route evidence recompute button inside panel")

        has_route_evidence_panel = (
            "route evidence" in raw_source
            or "routeevidence" in raw_source
            or "route-evidence" in raw_source
        )
        if not has_route_evidence_panel:
            failures.append("route evidence panel")
        route_update_pattern = (
            r"(?:routeevidence|route[-_\s]*evidence|routedetailrows|route[-_\s]*detail[-_\s]*rows?|"
            r"evidencelist|evidence[-_\s]*list|evidence[-_\s]*tbody|evidence[-_\s]*table).{0,240}"
            r"(?:innerhtml|textcontent|appendchild|replacechildren|insertadjacenthtml)"
            r"|(?:innerhtml|textcontent|appendchild|replacechildren|insertadjacenthtml).{0,240}"
            r"(?:routeevidence|route[-_\s]*evidence|routedetailrows|route[-_\s]*detail[-_\s]*rows?|"
            r"evidencelist|evidence[-_\s]*list|evidence[-_\s]*tbody|evidence[-_\s]*table)"
            r"|(?:route[-_\s]*row|routedetailrow|route[-_\s]*detail[-_\s]*row|route[-_\s]*selected[-_\s]*agent|"
            r"route[-_\s]*matched|route[-_\s]*mcp[-_\s]*risk|route[-_\s]*reason).{0,240}"
            r"(?:innerhtml|textcontent|appendchild|replacechildren|insertadjacenthtml)"
            r"|(?:innerhtml|textcontent|appendchild|replacechildren|insertadjacenthtml).{0,240}"
            r"(?:route[-_\s]*row|routedetailrow|route[-_\s]*detail[-_\s]*row|route[-_\s]*selected[-_\s]*agent|"
            r"route[-_\s]*matched|route[-_\s]*mcp[-_\s]*risk|route[-_\s]*reason)"
        )
        if not re.search(route_update_pattern, raw_source, flags=re.IGNORECASE | re.DOTALL):
            failures.append("route evidence update")
        has_route_rationale = bool(
            re.search(r"\b(?:reason|because|matched|matching|assigned|rationale|why|routing decision)\b", raw_source)
            and "skill" in raw_source
        )
        if not has_route_rationale:
            failures.append("route evidence rationale")
        if "selected agent" in normalized_description:
            route_runtime_contexts = _static_innerhtml_contexts(
                script_source_text,
                ["route-evidence", "route evidence", "routeevidence", "evidence-list", "evidence-tbody", "evidence-table", "evidence"],
            )
            selected_agent_surfaces = list(route_sections) + [
                _normalize_requirement_text(context)
                for context in route_runtime_contexts
            ]
            if not any(
                _contains_required_phrase(surface, "selected agent")
                for surface in selected_agent_surfaces
            ):
                failures.append("route evidence label: selected agent")
        required_route_terms: List[str] = []
        if "matched skill" in normalized_description or "matched native skill" in normalized_description:
            required_route_terms.append("matched")
            required_route_terms.append("skill")
        if "mcp risk" in normalized_description:
            required_route_terms.append("mcp risk")
        if "reason text" in normalized_description:
            required_route_terms.append("reason")
        missing_runtime_route_terms = _static_dynamic_overwrite_missing_terms(
            script_source_text,
            ["route-evidence", "route evidence", "routeevidence", "evidence-list", "evidence-tbody", "evidence-table", "evidence"],
            required_route_terms,
        )
        for term in missing_runtime_route_terms:
            failures.append(f"route evidence runtime row missing: {term}")

    if "owner agent" in normalized_description and not _contains_required_phrase(normalized_source, "owner agent"):
        failures.append("owner agent route preview")

    if "worker steps" in normalized_description and "worker" not in raw_source:
        failures.append("worker route steps")

    if "quality gates" in normalized_description:
        has_quality_gate = (
            re.search(r"\bquality\s+gates?\b", raw_source)
            or re.search(r"\bgates?\b.{0,80}\bquality\b", raw_source)
            or re.search(r"\bquality\b.{0,80}\bgates?\b", raw_source)
        )
        if not has_quality_gate:
            failures.append("quality gates")

    if "risk note" in normalized_description and "risk" not in raw_source:
        failures.append("risk note")

    if "three configurable skill chips or toggles" in normalized_description:
        has_skill_toggle = "skill" in raw_source and re.search(r"chip|toggle|type=[\"']checkbox|role=[\"']switch", raw_source)
        if not has_skill_toggle or "native" not in raw_source:
            failures.append("native skill chips or toggles")
        failures.extend(_static_agent_card_skill_control_failures(project_dir, description))

    if "localstorage" in normalized_description:
        persistence_terms = ["localstorage", "priority", "strict", "skill"]
        missing_terms = [term for term in persistence_terms if term not in raw_source]
        if missing_terms:
            failures.append("localStorage persistence missing: " + ", ".join(missing_terms))

    if (
        "responsive layout" in normalized_description
        or "narrow screens" in normalized_description
        or "mobile" in normalized_description
    ):
        failures.extend(_static_web_responsive_layout_failures(project_dir))

    if "no package managers" in normalized_description or "package managers" in normalized_description:
        package_locations = _forbidden_package_manager_instruction_locations(project_dir)
        if package_locations:
            failures.append(
                "forbidden package-manager instructions: "
                + ", ".join(package_locations[:8])
            )

    if "no placeholder lorem ipsum" in normalized_description or "no placeholder" in normalized_description:
        placeholder_markers = ["lorem ipsum", "add your", "todo:", "tbd"]
        if any(marker in project_text for marker in placeholder_markers):
            failures.append("placeholder content")

    return failures


def _static_agent_card_skill_control_failures(project_dir: str, task_description: str) -> List[str]:
    """Check explicit per-agent skill control requirements for static HTML UIs."""
    entrypoint = _discover_static_web_entrypoint(project_dir)
    if not entrypoint:
        return []
    try:
        html = _read_static_html(project_dir, entrypoint)
    except OSError:
        return []

    requested_entities = [
        entity for _, entity in _requested_include_entities(task_description)
        if entity.strip()
    ]
    if not requested_entities:
        return []

    class CardParser(HTMLParser):
        VOID_TAGS = {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }

        def __init__(self) -> None:
            super().__init__()
            self.cards: List[Dict[str, Any]] = []
            self.current: Dict[str, Any] | None = None
            self.depth = 0

        def _count_control(self, attrs: Dict[str, str]) -> bool:
            attr_text = " ".join(f"{key}={value}" for key, value in attrs.items()).lower()
            return bool(
                "data-skill" in attrs
                or attrs.get("type", "").lower() == "checkbox"
                or attrs.get("role", "").lower() == "switch"
                or re.search(r"\b(skill|chip)\b", attr_text)
            )

        def handle_starttag(self, tag: str, attrs_list: List[tuple[str, str | None]]) -> None:
            attrs = {key.lower(): (value or "") for key, value in attrs_list}
            class_name = attrs.get("class", "")
            is_card = bool(re.search(r"\b(?:agent|llm)-card\b", class_name, flags=re.IGNORECASE))
            if is_card and self.current is None:
                self.current = {"text": "", "control_count": 0}
                self.depth = 1
            elif self.current is not None and tag.lower() not in self.VOID_TAGS:
                self.depth += 1
            if self.current is not None and self._count_control(attrs):
                self.current["control_count"] += 1

        def handle_startendtag(self, tag: str, attrs_list: List[tuple[str, str | None]]) -> None:
            attrs = {key.lower(): (value or "") for key, value in attrs_list}
            if self.current is not None and self._count_control(attrs):
                self.current["control_count"] += 1

        def handle_data(self, data: str) -> None:
            if self.current is not None:
                self.current["text"] += " " + data

        def handle_endtag(self, tag: str) -> None:
            if self.current is None:
                return
            self.depth -= 1
            if self.depth <= 0:
                self.cards.append(self.current)
                self.current = None
                self.depth = 0

    parser = CardParser()
    parser.feed(html)

    failures: List[str] = []
    for entity in requested_entities:
        matching_cards = [
            card for card in parser.cards
            if _contains_required_phrase(_normalize_requirement_text(str(card.get("text") or "")), entity)
        ]
        if not matching_cards:
            failures.append(f"agent row {entity} has fewer than three skill controls")
            continue
        control_count = max(int(card.get("control_count") or 0) for card in matching_cards)
        if control_count < 3:
            failures.append(f"agent row {entity} has fewer than three skill controls")
    return failures


def _static_web_responsive_layout_failures(project_dir: str) -> List[str]:
    """Catch common static CSS patterns that cause mobile horizontal overflow."""
    source_text = _read_static_web_source_text(project_dir)
    if not source_text:
        return []

    failures: List[str] = []
    if "@media" not in source_text or "max-width" not in source_text:
        failures.append("responsive narrow-screen media query")

    risky_selectors = [
        ".console",
        ".agents-grid",
        ".skill-matrix",
        ".matrix-grid",
        ".matrix-row",
        ".composer-row",
    ]
    risky_grid_pattern = (
        r"{[^{}]*grid-template-columns\s*:\s*"
        r"(?:repeat\(\s*[23456789]\s*,|[^;{}]*(?:\b1fr\s+1fr\b|auto\s+repeat\())"
    )
    mobile_fix_pattern = r"(?:grid-template-columns\s*:\s*1fr|overflow-x\s*:\s*auto|display\s*:\s*block|flex-direction\s*:\s*column)"
    for selector in risky_selectors:
        selector_pattern = re.escape(selector)
        has_risky_grid = re.search(selector_pattern + r"[^{}]*" + risky_grid_pattern, source_text, flags=re.DOTALL)
        if not has_risky_grid:
            continue
        has_mobile_fix = re.search(
            r"@media[^{]*max-width[\s\S]{0,3000}"
            + selector_pattern
            + r"[\s\S]{0,800}"
            + mobile_fix_pattern,
            source_text,
            flags=re.DOTALL,
        )
        if not has_mobile_fix:
            failures.append(f"responsive mobile rule for {selector}")
    return failures


def _static_web_feature_failures(project_dir: str, task_description: str | None) -> List[str]:
    description = (task_description or "").lower()
    if not description:
        return []
    source_text = _read_static_web_source_text(project_dir)
    failures: List[str] = []
    for rule in STATIC_WEB_FEATURE_RULES:
        if not any(re.search(pattern, description, flags=re.IGNORECASE) for pattern in rule["triggers"]):
            continue
        has_evidence = all(
            any(re.search(pattern, source_text, flags=re.IGNORECASE) for pattern in group)
            for group in rule["evidence"]
        )
        if not has_evidence:
            failures.append(str(rule["label"]))
    return failures


def _run_static_web_smoke(
    project_dir: str,
    task_description: str | None = None,
    contract: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Verify a plain static HTML/CSS/JS delivery can be served by python http.server."""
    entrypoint = _discover_static_web_entrypoint(project_dir)
    if not entrypoint:
        return {
            "probe_type": "static_web_smoke",
            "passed": False,
            "returncode": 1,
            "output_tail": "No static web entrypoint found. Expected index.html, web/index.html, static/index.html, public/index.html, or app/static/index.html.",
            "blocked_by_environment": False,
        }

    html_structure_failures = _static_html_structure_failures(project_dir, entrypoint)
    if html_structure_failures:
        return {
            "probe_type": "static_web_smoke",
            "passed": False,
            "returncode": 1,
            "output_tail": "HTML structure issues: " + ", ".join(html_structure_failures),
            "blocked_by_environment": False,
            "entrypoint": entrypoint,
        }

    missing_assets = _static_asset_reference_failures(project_dir, entrypoint)
    if missing_assets:
        return {
            "probe_type": "static_web_smoke",
            "passed": False,
            "returncode": 1,
            "output_tail": "Missing local static asset references: " + ", ".join(missing_assets[:20]),
            "blocked_by_environment": False,
            "entrypoint": entrypoint,
        }

    missing_required_assets = _required_static_asset_link_failures(project_dir, entrypoint, contract)
    if missing_required_assets:
        return {
            "probe_type": "static_web_smoke",
            "passed": False,
            "returncode": 1,
            "output_tail": "Required static assets are not referenced by the entrypoint: " + ", ".join(missing_required_assets),
            "blocked_by_environment": False,
            "entrypoint": entrypoint,
        }

    feature_failures = _static_web_feature_failures(project_dir, task_description)
    feature_failures.extend(_static_web_explicit_requirement_failures(project_dir, task_description))
    if feature_failures:
        return {
            "probe_type": "static_web_smoke",
            "passed": False,
            "returncode": 1,
            "output_tail": "Missing requested static web feature evidence: " + ", ".join(feature_failures),
            "blocked_by_environment": False,
            "entrypoint": entrypoint,
        }

    smoke_source = f"""
import functools
import http.server
import threading
import urllib.parse
import urllib.request

PROJECT_DIR = {os.path.realpath(project_dir)!r}
ENTRYPOINT = {entrypoint!r}

handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=PROJECT_DIR)
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    url = f"http://127.0.0.1:{{server.server_port}}/" + urllib.parse.quote(ENTRYPOINT)
    with urllib.request.urlopen(url, timeout=5) as response:
        status = response.status
        content_type = response.headers.get("content-type", "")
        body = response.read(300000).decode("utf-8", "ignore").lower()
        print(f"GET /{{ENTRYPOINT}} -> {{status}} {{content_type}}")
        if status >= 400:
            raise SystemExit(1)
        if "<html" not in body and "<!doctype" not in body and "<body" not in body:
            print("Entrypoint did not look like an HTML document.")
            raise SystemExit(1)
finally:
    server.shutdown()
    server.server_close()
"""

    try:
        with tempfile.TemporaryDirectory(prefix="across-static-web-smoke-") as probe_dir:
            script_path = os.path.join(probe_dir, "smoke.py")
            with open(script_path, "w", encoding="utf-8") as handle:
                handle.write(smoke_source)
            env = dict(os.environ)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            proc = subprocess.run(
                [_python_probe_executable(), script_path],
                cwd=project_dir,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=20,
            )
            return {
                "probe_type": "static_web_smoke",
                "passed": proc.returncode == 0,
                "returncode": proc.returncode,
                "output_tail": (proc.stdout or "")[-4000:],
                "blocked_by_environment": False,
                "entrypoint": entrypoint,
            }
    except FileNotFoundError as exc:
        return {
            "probe_type": "static_web_smoke",
            "passed": False,
            "returncode": None,
            "output_tail": str(exc),
            "blocked_by_environment": True,
            "entrypoint": entrypoint,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "probe_type": "static_web_smoke",
            "passed": False,
            "returncode": None,
            "output_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "static web smoke probe timed out",
            "blocked_by_environment": False,
            "entrypoint": entrypoint,
        }


def _run_browser_e2e(
    project_dir: str,
    task_description: str | None = None,
) -> Dict[str, Any]:
    entrypoint = _discover_static_web_entrypoint(project_dir)
    if not entrypoint:
        return {
            "probe_type": "browser_e2e",
            "passed": False,
            "returncode": 1,
            "output_tail": "No static web entrypoint found for browser E2E. Expected index.html or web/index.html.",
            "blocked_by_environment": False,
        }

    node = _node_probe_executable()
    if not node:
        return {
            "probe_type": "browser_e2e",
            "passed": False,
            "returncode": None,
            "output_tail": "Node.js is required for browser E2E but was not found.",
            "blocked_by_environment": True,
            "entrypoint": entrypoint,
        }

    probe_source = r"""
let chromium;
try {
  chromium = require('playwright').chromium;
} catch (err) {
  console.log(JSON.stringify({
    passed: false,
    blockedByEnvironment: true,
    failures: ['Playwright module unavailable: ' + err.message],
    checks: []
  }));
  process.exit(2);
}

const projectDir = __PROJECT_DIR__;
const entrypoint = __ENTRYPOINT__;
const description = (__DESCRIPTION__ || '').toLowerCase();

function needs(pattern) {
  return pattern.test(description);
}

function includesAll(text, terms) {
  const lowered = (text || '').toLowerCase();
  return terms.every(term => lowered.includes(term));
}

function isBrowserEnvironmentError(err) {
  const message = String((err && err.message) || err || '');
  return /Executable doesn't exist|playwright install|Host system is missing dependencies|browserType\.launch/i.test(message);
}

(async () => {
  const failures = [];
  const checks = [];
  const consoleMessages = [];
  const pageErrors = [];
  let browser;

  function record(name, passed, evidence) {
    checks.push({ name, passed, evidence: evidence || {} });
    if (!passed) {
      failures.push(name + (evidence && evidence.message ? ': ' + evidence.message : ''));
    }
  }

  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    page.on('console', msg => {
      if (['error'].includes(msg.type())) {
        consoleMessages.push(msg.type() + ': ' + msg.text());
      }
    });
    page.on('pageerror', err => pageErrors.push(err.stack || err.message));

    await page.goto('file://' + projectDir + '/' + entrypoint, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(1500);

    const bodyText = await page.locator('body').innerText().catch(() => '');
    record('body visible text', bodyText.trim().length > 0, { length: bodyText.length });

    if (needs(/\bcanvas|animation\b/)) {
      const canvasInfo = await page.evaluate(() => {
        const canvas = document.querySelector('canvas');
        if (!canvas) return { present: false, nonBlank: false };
        const ctx = canvas.getContext('2d');
        if (!ctx || canvas.width <= 0 || canvas.height <= 0) {
          return { present: true, nonBlank: false, width: canvas.width, height: canvas.height };
        }
        const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
        const colors = new Set();
        let nonTransparent = 0;
        const stride = Math.max(4, Math.floor(data.length / 6000 / 4) * 4);
        for (let i = 0; i < data.length; i += stride) {
          const r = data[i], g = data[i + 1], b = data[i + 2], a = data[i + 3];
          if (a > 0) nonTransparent++;
          colors.add(`${r},${g},${b},${a}`);
        }
        return {
          present: true,
          nonBlank: nonTransparent > 0 && colors.size > 1,
          width: canvas.width,
          height: canvas.height,
          distinct: colors.size,
          nonTransparent
        };
      }).catch(err => ({ present: false, nonBlank: false, error: err.message }));
      record('canvas renders nonblank pixels', !!canvasInfo.present && !!canvasInfo.nonBlank, canvasInfo);
    }

    if (needs(/\bdelivery\s+report\b/)) {
      const requiredMetrics = [
        'generated quality score',
        'final quality score',
        'required gate failures',
        'manual checks',
        'skipped checks',
        'final verdict'
      ].filter(term => description.includes(term));
      const metricsFound = requiredMetrics.length === 0 || includesAll(bodyText, requiredMetrics);
      record('delivery report metrics visible', metricsFound, { requiredMetrics });
    }

    if (needs(/\b(functional|artifact)\b/) && needs(/\b(mode|radio|toggle)\b/)) {
      const modeResult = await page.evaluate(() => {
        const artifactInput = document.querySelector('#mode-artifact, input[value="artifact"], input[name*="mode"][value*="artifact"]');
        if (artifactInput) {
          artifactInput.click();
          return {
            exists: true,
            selected: !!artifactInput.checked || artifactInput.getAttribute('aria-pressed') === 'true',
            type: artifactInput.tagName
          };
        }
        const candidates = Array.from(document.querySelectorAll('button,[role="tab"],[role="button"],label'));
        const artifactControl = candidates.find(el => /artifact/i.test(el.textContent || ''));
        if (!artifactControl) return { exists: false, selected: false };
        artifactControl.click();
        return {
          exists: true,
          selected: /active|selected|is-active/.test(artifactControl.className || '')
            || artifactControl.getAttribute('aria-selected') === 'true'
            || artifactControl.getAttribute('aria-pressed') === 'true'
        };
      }).catch(err => ({ exists: false, selected: false, error: err.message }));
      record('functional artifact mode selectable', !!modeResult.exists && !!modeResult.selected, modeResult);
    }

    if (needs(/\b(checklist|quality\s+gate)\b/)) {
      const checklistResult = await page.evaluate(() => {
        const checkbox = document.querySelector('.quality-gate-checklist input[type="checkbox"], .checklist input[type="checkbox"], input[type="checkbox"]');
        if (!checkbox) return { exists: false, changed: false };
        const before = checkbox.checked;
        const clickable = checkbox.closest('label')
          || checkbox.closest('.checklist-item')?.querySelector('label,.checklist-label,[role="checkbox"]')
          || checkbox;
        clickable.click();
        const after = checkbox.checked;
        return {
          exists: true,
          before,
          after,
          changed: before !== after,
          localStorageKeys: Object.keys(localStorage).length
        };
      }).catch(err => ({ exists: false, changed: false, error: err.message }));
      record('quality checklist toggles', !!checklistResult.exists && !!checklistResult.changed, checklistResult);
    }

    if (needs(/\broute\s+evidence|recompute\s+route\b/)) {
      const routeResult = await page.evaluate(async () => {
        function textOf(el) {
          return (el && (el.innerText || el.textContent || '')) || '';
        }
        function routeIdentity(el) {
          return [
            el.id || '',
            typeof el.className === 'string' ? el.className : '',
            el.getAttribute && el.getAttribute('aria-label') || '',
            el.getAttribute && el.getAttribute('aria-labelledby') || ''
          ].join(' ');
        }
        function isControl(el) {
          return !!(
            el.closest && el.closest('button,a,input,textarea,select,[role="button"],[role="tab"]')
          );
        }
        function chooseRoutePanel() {
          const containerSelectors = [
            '.route-evidence',
            '.route-evidence-panel',
            '.route-evidence-section',
            '#route-evidence',
            '#routeEvidence',
            '#routeEvidencePanel',
            '.evidence-list',
            '[class*="route"][class*="evidence"]',
            '[id*="route"][id*="evidence"]'
          ];
          for (const selector of containerSelectors) {
            const el = document.querySelector(selector);
            if (el) return el;
          }

          const ariaPanel = Array.from(document.querySelectorAll('[aria-label]')).find(el => {
            const label = el.getAttribute('aria-label') || '';
            return /route evidence/i.test(label) && !isControl(el);
          });
          if (ariaPanel) return ariaPanel;

          const routeHeading = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,[id],[class]')).find(el => {
            const identity = routeIdentity(el);
            return /route evidence/i.test(textOf(el)) || /route[-_\s]*evidence/i.test(identity);
          });
          if (routeHeading) {
            return routeHeading.closest('section,article,main,aside,.panel,.card,[class*="panel"],[class*="route"],[class*="evidence"]')
              || routeHeading.parentElement;
          }

          return null;
        }
        const routeTask = 'Review backend API schema, browser E2E, security privacy, MCP risk, and deployment routing ' + Date.now();
        const panel = chooseRoutePanel() || document.body;
        const beforeInput = panel.innerText || '';
        const taskInput = document.querySelector(
          'textarea, input[name*="task" i], input[id*="task" i], input[name*="description" i], input[id*="description" i]'
        );
        if (taskInput) {
          taskInput.focus();
          taskInput.value = routeTask;
          taskInput.dispatchEvent(new Event('input', { bubbles: true }));
          taskInput.dispatchEvent(new Event('change', { bubbles: true }));
        }
        await new Promise(resolve => setTimeout(resolve, 200));
        const afterInput = panel.innerText || '';
        const scopedButton = Array.from(panel.querySelectorAll ? panel.querySelectorAll('button') : [])
          .find(btn => /recompute route/i.test(btn.textContent || ''));
        const button = scopedButton || Array.from(document.querySelectorAll('button'))
          .find(btn => /recompute route/i.test(btn.textContent || '') || /recompute route/i.test(btn.getAttribute('aria-label') || ''));
        if (button) button.click();
        await new Promise(resolve => setTimeout(resolve, 1300));
        const after = panel.innerText || '';
        const lowered = after.toLowerCase();
        return {
          buttonExists: !!button,
          changed: beforeInput !== afterInput || afterInput !== after,
          hasSelectedAgent: lowered.includes('selected agent') || /(openclaw|hermes|claude code|codex|opencode|cursor agent|openai|anthropic|deepseek|minimax|bailian|moonshot|zhipu|volcengine|google|xai|mistral|groq|cohere|openrouter|together|fireworks)/.test(lowered),
          hasMatched: lowered.includes('matched') || /\b(skill|capability|backend api|schema|research|review)\b/.test(lowered),
          hasMcpRisk: lowered.includes('mcp risk') || /\b(low|medium|high)\b/.test(lowered),
          hasReason: lowered.includes('reason') || /\b(primary|because|domain|quality|discovery|review|integration|specialist)\b/.test(lowered),
          textLength: after.length
        };
      }).catch(err => ({ buttonExists: false, changed: false, error: err.message }));
      record(
        'route evidence recomputes visible rows',
        !!routeResult.buttonExists
          && !!routeResult.changed
          && !!routeResult.hasSelectedAgent
          && !!routeResult.hasMatched
          && !!routeResult.hasMcpRisk
          && !!routeResult.hasReason,
        routeResult
      );
    }

    record('no browser page errors', pageErrors.length === 0, { pageErrors });
    record('no console errors', consoleMessages.length === 0, { consoleMessages });

    const passed = failures.length === 0;
    console.log(JSON.stringify({
      passed,
      blockedByEnvironment: false,
      failures,
      checks,
      consoleMessages,
      pageErrors
    }));
    process.exit(passed ? 0 : 1);
  } catch (err) {
    const blockedByEnvironment = isBrowserEnvironmentError(err);
    const prefix = blockedByEnvironment ? 'browser e2e environment unavailable: ' : 'browser e2e exception: ';
    console.log(JSON.stringify({
      passed: false,
      blockedByEnvironment,
      failures: [prefix + err.message],
      checks,
      consoleMessages,
      pageErrors
    }));
    process.exit(blockedByEnvironment ? 2 : 1);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
})();
"""
    probe_source = (
        probe_source
        .replace("__PROJECT_DIR__", json.dumps(os.path.realpath(project_dir)))
        .replace("__ENTRYPOINT__", json.dumps(entrypoint))
        .replace("__DESCRIPTION__", json.dumps(task_description or ""))
    )

    try:
        with tempfile.TemporaryDirectory(prefix="across-browser-e2e-") as probe_dir:
            script_path = os.path.join(probe_dir, "browser_e2e.js")
            with open(script_path, "w", encoding="utf-8") as handle:
                handle.write(probe_source)
            env = dict(os.environ)
            node_path = _playwright_node_path(project_dir)
            if node_path:
                env["NODE_PATH"] = node_path
            proc = subprocess.run(
                [node, script_path],
                cwd=project_dir,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=45,
            )
    except FileNotFoundError as exc:
        return {
            "probe_type": "browser_e2e",
            "passed": False,
            "returncode": None,
            "output_tail": str(exc),
            "blocked_by_environment": True,
            "entrypoint": entrypoint,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "probe_type": "browser_e2e",
            "passed": False,
            "returncode": None,
            "output_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "browser E2E probe timed out",
            "blocked_by_environment": False,
            "entrypoint": entrypoint,
        }

    output = proc.stdout or ""
    parsed: Dict[str, Any] = {}
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    failures = parsed.get("failures") if isinstance(parsed.get("failures"), list) else []
    output_tail = "; ".join(str(item) for item in failures) or output[-4000:]
    blocked_by_environment = bool(parsed.get("blockedByEnvironment"))
    return {
        "probe_type": "browser_e2e",
        "passed": proc.returncode == 0 and bool(parsed.get("passed")),
        "returncode": proc.returncode,
        "output_tail": output_tail[-4000:],
        "blocked_by_environment": blocked_by_environment,
        "entrypoint": entrypoint,
        "checks": parsed.get("checks") or [],
        "console_messages": parsed.get("consoleMessages") or [],
        "page_errors": parsed.get("pageErrors") or [],
    }


def _should_infer_static_web_probe(
    project_dir: str | None,
    task_types: set[str],
    contract: Dict[str, Any],
    produced_required: List[str],
) -> bool:
    if "functional" not in task_types or not project_dir or not os.path.isdir(project_dir):
        return False
    if not _discover_static_web_entrypoint(project_dir):
        return False
    if any(probe.get("probe_type") == "static_web_smoke" for probe in contract.get("acceptance_probes", []) or []):
        return False
    path_hints = [str(item or "").lower() for item in produced_required]
    deliverable_hints = [
        str(item.get("path_hint") or "").lower()
        for item in contract.get("deliverables", []) or []
        if item.get("required", True)
    ]
    return any(path.endswith(".html") for path in [*path_hints, *deliverable_hints])


def _run_notes_cli_smoke(project_dir: str) -> Dict[str, Any]:
    """Run a user-facing smoke test for the notes CLI contract."""
    generated_files = ["notes.json", "notes_store.json", "todo.json", "e2e_export.md"]

    def cleanup_generated_files() -> None:
        for filename in generated_files:
            path = os.path.realpath(os.path.join(project_dir, filename))
            try:
                if os.path.commonpath([os.path.realpath(project_dir), path]) == os.path.realpath(project_dir) and os.path.isfile(path):
                    os.remove(path)
            except (OSError, ValueError):
                pass

    def parse_created_id(stdout: str) -> str:
        for line in stdout.splitlines():
            text = line.strip()
            if not text:
                continue
            if re.search(r"\b(created|added)\b", text, flags=re.IGNORECASE):
                match = re.search(
                    r"([0-9a-fA-F]{8,}(?:-[0-9a-fA-F]{4}){0,4}|[A-Za-z0-9_-]+)\s*$",
                    text,
                )
                if match:
                    return match.group(1).strip(" .,:;")
        return ""

    commands = [
        ["python3", "notes_cli.py", "add", "Buy milk", "--tag", "home"],
        ["python3", "notes_cli.py", "list"],
        ["python3", "notes_cli.py", "search", "--tag", "home"],
        ["python3", "notes_cli.py", "done", "__ADDED_ID__"],
        ["python3", "notes_cli.py", "list", "--done"],
        ["python3", "notes_cli.py", "export", "e2e_export.md"],
    ]
    output: List[str] = []
    added_id = ""
    try:
        cleanup_generated_files()
        for command in commands:
            actual = [added_id if part == "__ADDED_ID__" else part for part in command]
            proc = subprocess.run(
                actual,
                cwd=project_dir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=30,
            )
            output.append(f"$ {' '.join(actual)}\n{proc.stdout}")
            if proc.returncode != 0:
                return {
                    "probe_type": "notes_cli_smoke",
                    "passed": False,
                    "returncode": proc.returncode,
                    "output_tail": "\n".join(output)[-4000:],
                    "blocked_by_environment": False,
                }
            if command[2] == "add":
                added_id = parse_created_id(proc.stdout)
                if not added_id:
                    return {
                        "probe_type": "notes_cli_smoke",
                        "passed": False,
                        "returncode": 1,
                        "output_tail": "\n".join(output + ["Could not parse created note id."])[-4000:],
                        "blocked_by_environment": False,
                    }
            elif command[2] in {"list", "search"} and "Buy milk" not in proc.stdout:
                return {
                    "probe_type": "notes_cli_smoke",
                    "passed": False,
                    "returncode": 1,
                    "output_tail": "\n".join(output + ["Expected output to contain 'Buy milk'."])[-4000:],
                    "blocked_by_environment": False,
                }
        export_path = os.path.join(project_dir, "e2e_export.md")
        if not os.path.isfile(export_path) or os.path.getsize(export_path) == 0:
            return {
                "probe_type": "notes_cli_smoke",
                "passed": False,
                "returncode": 1,
                "output_tail": "\n".join(output + ["Expected export to write non-empty e2e_export.md."])[-4000:],
                "blocked_by_environment": False,
            }
        return {
            "probe_type": "notes_cli_smoke",
            "passed": True,
            "returncode": 0,
            "output_tail": "\n".join(output)[-4000:],
            "blocked_by_environment": False,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {
            "probe_type": "notes_cli_smoke",
            "passed": False,
            "returncode": None,
            "output_tail": str(exc),
            "blocked_by_environment": isinstance(exc, FileNotFoundError),
        }
    finally:
        cleanup_generated_files()


def _find_api_service_entrypoint(project_dir: str, contract: Dict[str, Any] | None = None) -> str | None:
    contract = contract or {}
    candidates: List[str] = []
    for deliverable in contract.get("deliverables", []) or []:
        path_hint = str(deliverable.get("path_hint") or "").strip().replace("\\", "/")
        if not path_hint:
            continue
        artifact_type = str(deliverable.get("artifact_type") or "").lower()
        if artifact_type == "api_service_source" or path_hint in {"api/server.mjs", "api/server.js", "server.mjs", "server.js"}:
            candidates.append(path_hint)
    candidates.extend(["api/server.mjs", "api/server.js", "server.mjs", "server.js"])
    seen: set[str] = set()
    for relative_path in candidates:
        if relative_path in seen:
            continue
        seen.add(relative_path)
        full_path = os.path.realpath(os.path.join(project_dir, relative_path))
        try:
            if os.path.commonpath([os.path.realpath(project_dir), full_path]) != os.path.realpath(project_dir):
                continue
        except ValueError:
            continue
        if os.path.isfile(full_path):
            return relative_path
    return None


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_json_request(port: int, method: str, path: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        method=method,
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=1.5) as response:
        raw = response.read(200000).decode("utf-8", "ignore")
        return {
            "status": response.status,
            "body": json.loads(raw or "{}"),
        }


def _run_api_service_smoke(project_dir: str, contract: Dict[str, Any] | None = None) -> Dict[str, Any]:
    entrypoint = _find_api_service_entrypoint(project_dir, contract)
    if not entrypoint:
        return {
            "probe_type": "api_service",
            "passed": False,
            "returncode": 1,
            "output_tail": "No API service entrypoint found. Expected api/server.mjs, api/server.js, server.mjs, or server.js.",
            "blocked_by_environment": False,
        }
    node = _node_probe_executable()
    if not node:
        return {
            "probe_type": "api_service",
            "passed": False,
            "returncode": None,
            "output_tail": "Node.js executable was not found for API service smoke.",
            "blocked_by_environment": True,
            "entrypoint": entrypoint,
        }

    port = _free_local_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    proc: subprocess.Popen[str] | None = None
    output: List[str] = []
    try:
        proc = subprocess.Popen(
            [node, entrypoint],
            cwd=project_dir,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        health: Dict[str, Any] | None = None
        deadline = time.time() + 6
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                health = _http_json_request(port, "GET", "/health")
                if health.get("status") == 200:
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
                time.sleep(0.15)
        if not health or health.get("status") != 200 or health.get("body", {}).get("status") != "ok":
            tail = ""
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    tail, _ = proc.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    tail, _ = proc.communicate(timeout=1)
            elif proc and proc.stdout:
                tail = proc.stdout.read(4000)
            return {
                "probe_type": "api_service",
                "passed": False,
                "returncode": proc.poll() if proc else None,
                "output_tail": (tail or "GET /health did not return status ok.")[-4000:],
                "blocked_by_environment": False,
                "entrypoint": entrypoint,
            }

        agents = _http_json_request(port, "GET", "/api/agents")
        route = _http_json_request(port, "POST", "/api/route", {"task": "browser api mcp quality"})
        report = _http_json_request(port, "GET", "/api/report")
        output.extend([
            f"GET /health -> {health.get('status')}",
            f"GET /api/agents -> {agents.get('status')}",
            f"POST /api/route -> {route.get('status')}",
            f"GET /api/report -> {report.get('status')}",
        ])
        agent_body = agents.get("body") or {}
        agent_rows = agent_body.get("agents") if isinstance(agent_body, dict) else None
        route_body = route.get("body") or {}
        report_body = report.get("body") or {}
        failures: List[str] = []
        if not isinstance(agent_rows, list) or len(agent_rows) < 5:
            failures.append("/api/agents must return at least five agents")
        if not any(str(row.get("kind") or "").lower() == "local" for row in agent_rows or []):
            failures.append("/api/agents must include local agents")
        if not any(str(row.get("kind") or "").lower() == "cloud" for row in agent_rows or []):
            failures.append("/api/agents must include cloud LLMs")
        if not (route_body.get("selectedAgent") or route_body.get("selected_agent")):
            failures.append("/api/route must return a selected agent")
        if not (route_body.get("reason") or route_body.get("rationale")):
            failures.append("/api/route must return a reason or rationale")
        if "required_failed_count" not in report_body or not (report_body.get("gateResults") or report_body.get("gate_results")):
            failures.append(
                "/api/report must return readiness metrics and gate results: "
                "required_failed_count, manual_required_count, skipped_required_count, "
                "and gateResults or gate_results. camelCase-only metric keys are insufficient."
            )
        return {
            "probe_type": "api_service",
            "passed": not failures,
            "returncode": 0 if not failures else 1,
            "output_tail": "\n".join(output + failures)[-4000:],
            "blocked_by_environment": False,
            "entrypoint": entrypoint,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {
            "probe_type": "api_service",
            "passed": False,
            "returncode": proc.poll() if proc else None,
            "output_tail": str(exc)[-4000:],
            "blocked_by_environment": isinstance(exc, FileNotFoundError),
            "entrypoint": entrypoint,
        }
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def _run_cli_generic_smoke(project_dir: str, contract: Dict[str, Any] | None = None) -> Dict[str, Any]:
    node = _node_probe_executable()
    script = os.path.join(project_dir, "cli", "quality-check.mjs")
    if not os.path.isfile(script):
        return {
            "probe_type": "cli_generic",
            "passed": False,
            "returncode": 1,
            "output_tail": "No supported CLI smoke entrypoint found. Expected cli/quality-check.mjs.",
            "blocked_by_environment": False,
        }
    if not node:
        return {
            "probe_type": "cli_generic",
            "passed": False,
            "returncode": None,
            "output_tail": "Node.js executable was not found for CLI smoke.",
            "blocked_by_environment": True,
        }
    try:
        proc = subprocess.run(
            [node, "cli/quality-check.mjs"],
            cwd=project_dir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        output = proc.stdout or ""
        passed = proc.returncode == 0
        try:
            parsed = json.loads(output[output.find("{"): output.rfind("}") + 1])
            if isinstance(parsed, dict) and parsed.get("passed") is False:
                passed = False
        except Exception:
            pass
        return {
            "probe_type": "cli_generic",
            "passed": passed,
            "returncode": proc.returncode,
            "output_tail": output[-4000:],
            "blocked_by_environment": False,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {
            "probe_type": "cli_generic",
            "passed": False,
            "returncode": None,
            "output_tail": str(exc),
            "blocked_by_environment": isinstance(exc, FileNotFoundError),
        }


def _agent_mix_constraint_evidence(task: Any) -> Dict[str, Any]:
    actual_agents: List[str] = []
    for subtask in getattr(task, "subtasks", []) or []:
        subtask_id = str(getattr(subtask, "subtask_id", "") or "")
        if subtask_id.endswith("-decompose"):
            continue
        agent_id = str(getattr(subtask, "agent_id", "") or "").strip().lower()
        if not agent_id or agent_id == "owner":
            continue
        status = getattr(getattr(subtask, "status", None), "value", getattr(subtask, "status", None))
        if str(status or "").lower() not in {"completed", "completed_with_failures"}:
            continue
        if agent_id not in actual_agents:
            actual_agents.append(agent_id)

    local_agents = sorted(agent for agent in actual_agents if agent in LOCAL_AGENT_IDS)
    cloud_agents = sorted(agent for agent in actual_agents if agent in CLOUD_AGENT_IDS)
    return {
        "actual_agents": actual_agents,
        "local_agents": local_agents,
        "cloud_agents": cloud_agents,
    }


def _check_agent_mix_constraint(task: Any, constraint: Dict[str, Any]) -> Dict[str, Any] | None:
    value = constraint.get("value") or {}
    if not isinstance(value, dict):
        return None
    min_distinct = int(value.get("min_distinct_agents") or 0)
    min_local = int(value.get("min_local_agents") or 0)
    min_cloud = int(value.get("min_cloud_agents") or 0)

    evidence = _agent_mix_constraint_evidence(task)
    actual_agents = evidence["actual_agents"]
    local_agents = evidence["local_agents"]
    cloud_agents = evidence["cloud_agents"]
    failures: List[str] = []
    if len(actual_agents) < min_distinct:
        failures.append(f"expected at least {min_distinct} distinct agents, saw {len(actual_agents)}")
    if len(local_agents) < min_local:
        failures.append(f"expected at least {min_local} local agents, saw {len(local_agents)}")
    if len(cloud_agents) < min_cloud:
        failures.append(f"expected at least {min_cloud} cloud agents, saw {len(cloud_agents)}")
    if not failures:
        return None
    return {
        "id": constraint.get("id") or "constraint-agent-mix",
        "constraint_type": "agent_mix",
        "value": value,
        "message": "; ".join(failures),
        "evidence": evidence,
    }


def run_delivery_contract_acceptance(
    task: Any,
    contract: Dict[str, Any],
    artifact_records: List[Dict[str, Any]],
    *,
    run_probes: bool = True,
) -> Dict[str, Any]:
    project_dir = getattr(task, "project_dir", None) or contract.get("project_dir")
    task_description = getattr(task, "description", "") or ""
    task_types = set(contract.get("task_types", []) or [])
    missing_required: List[str] = []
    produced_required: List[str] = []
    invalid_required: List[Dict[str, Any]] = []
    failed_constraints: List[Dict[str, Any]] = []
    satisfied_agent_mix_constraints: List[Dict[str, Any]] = []
    evidence_gaps: List[Dict[str, Any]] = []
    probe_results: List[Dict[str, Any]] = []
    hygiene_report: Dict[str, Any] | None = None

    for deliverable in contract.get("deliverables", []) or []:
        if not deliverable.get("required", True):
            continue
        path_hint = deliverable.get("path_hint")
        if not path_hint:
            continue
        resolved = first_existing_candidate(path_hint, project_dir)
        if not resolved or not os.path.isfile(resolved):
            missing_required.append(path_hint)
            continue
        produced_required.append(path_hint)
        try:
            if os.path.getsize(resolved) == 0:
                invalid_required.append({"path_hint": path_hint, "check_type": "file_non_empty", "message": "file is empty"})
        except OSError:
            invalid_required.append({"path_hint": path_hint, "check_type": "file_readable", "message": "file could not be read"})

    if project_dir and os.path.isdir(project_dir):
        invalid_required.extend(
            _validate_deliverable_groups(project_dir, list(contract.get("deliverable_groups") or []))
        )

        framework_result = _check_requested_framework_alignment(
            getattr(task, "description", "") or "",
            project_dir,
        )
        if framework_result and not framework_result.passed:
            invalid_required.append({
                "path_hint": framework_result.evidence.get("path") or "project",
                "check_type": framework_result.check_type,
                "message": framework_result.message,
                "evidence": framework_result.evidence,
            })
        storage_result = _check_requested_storage_alignment(
            getattr(task, "description", "") or "",
            project_dir,
        )
        if storage_result and not storage_result.passed:
            invalid_required.append({
                "path_hint": storage_result.evidence.get("path") or "project",
                "check_type": storage_result.check_type,
                "message": storage_result.message,
                "evidence": storage_result.evidence,
            })

        for constraint in contract.get("constraints", []) or []:
            if constraint.get("constraint_type") == "forbidden_tooling" and constraint.get("value") == "docker":
                docker_paths = _scan_for_docker(project_dir)
                if docker_paths:
                    failed_constraints.append({
                        "id": constraint.get("id") or "constraint-no-docker",
                        "constraint_type": "forbidden_tooling",
                        "value": "docker",
                        "evidence": docker_paths,
                    })
            if constraint.get("constraint_type") == "forbidden_file":
                forbidden_paths = _scan_for_forbidden_file(
                    project_dir,
                    str(constraint.get("value") or ""),
                    str(constraint.get("scope") or "recursive"),
                )
                if forbidden_paths:
                    failed_constraints.append({
                        "id": constraint.get("id") or f"constraint-forbidden-file-{constraint.get('value')}",
                        "constraint_type": "forbidden_file",
                        "value": constraint.get("value"),
                        "evidence": forbidden_paths,
                    })
            if constraint.get("constraint_type") == "allowed_files":
                disallowed_paths = _scan_for_disallowed_files(
                    project_dir,
                    list(constraint.get("value") or []),
                )
                if disallowed_paths:
                    failed_constraints.append({
                        "id": constraint.get("id") or "constraint-allowed-files",
                        "constraint_type": "allowed_files",
                        "value": constraint.get("value") or [],
                        "evidence": disallowed_paths,
                    })
            if constraint.get("constraint_type") == "allowed_documentation_files":
                disallowed_paths = _scan_for_disallowed_documentation_files(
                    project_dir,
                    list(constraint.get("value") or []),
                )
                if disallowed_paths:
                    failed_constraints.append({
                        "id": constraint.get("id") or "constraint-allowed-documentation-files",
                        "constraint_type": "allowed_documentation_files",
                        "value": constraint.get("value") or [],
                        "evidence": disallowed_paths,
                    })
            if constraint.get("constraint_type") == "forbidden_unrequested_auth":
                auth_paths = _scan_for_unrequested_auth(project_dir)
                if auth_paths:
                    failed_constraints.append({
                        "id": constraint.get("id") or "constraint-no-unrequested-auth",
                        "constraint_type": "forbidden_unrequested_auth",
                        "value": constraint.get("value") or "auth",
                        "evidence": auth_paths,
                    })
            if constraint.get("constraint_type") == "agent_mix":
                agent_mix_failure = _check_agent_mix_constraint(task, constraint)
                if agent_mix_failure:
                    failed_constraints.append(agent_mix_failure)
                else:
                    satisfied_agent_mix_constraints.append({
                        "id": constraint.get("id") or "constraint-agent-mix",
                        "constraint_type": "agent_mix",
                        "value": constraint.get("value") or {},
                        "evidence": _agent_mix_constraint_evidence(task),
                    })

        hygiene_report = scan_workspace_hygiene(project_dir)
        if hygiene_report["noise_file_count"] > 0:
            failed_constraints.append({
                "id": "constraint-workspace-hygiene",
                "constraint_type": "workspace_hygiene",
                "value": "runtime_noise",
                "message": (
                    f"Workspace contains {hygiene_report['noise_file_count']} runtime/cache/diagnostic files "
                    "that are not deliverables."
                ),
                "evidence": hygiene_report["noise_evidence"],
                "total_file_count": hygiene_report["total_file_count"],
                "delivery_file_count": hygiene_report["delivery_file_count"],
            })
        if hygiene_report["delivery_file_count"] > hygiene_report["max_delivery_files"]:
            failed_constraints.append({
                "id": "constraint-workspace-file-count",
                "constraint_type": "workspace_hygiene",
                "value": "file_count",
                "message": (
                    f"Workspace contains {hygiene_report['delivery_file_count']} deliverable-like files, "
                    f"above the limit of {hygiene_report['max_delivery_files']}."
                ),
                "evidence": [],
                "total_file_count": hygiene_report["total_file_count"],
                "delivery_file_count": hygiene_report["delivery_file_count"],
            })

    if run_probes:
        for probe in contract.get("acceptance_probes", []) or []:
            if not probe.get("required", True):
                continue
            if probe.get("probe_type") == "python_install" and project_dir:
                result = _run_python_install(project_dir)
                result["id"] = probe.get("id") or "probe-python-install"
                probe_results.append(result)
            if probe.get("probe_type") == "python_web_smoke" and project_dir:
                result = _run_python_web_smoke(
                    project_dir,
                    require_html_root=bool(probe.get("require_html_root")),
                )
                result["id"] = probe.get("id") or "probe-python-web-smoke"
                probe_results.append(result)
            if probe.get("probe_type") == "static_web_smoke" and project_dir:
                result = _run_static_web_smoke(project_dir, task_description, contract)
                result["id"] = probe.get("id") or "probe-static-web-smoke"
                probe_results.append(result)
            if probe.get("probe_type") == "browser_e2e" and project_dir:
                result = _run_browser_e2e(project_dir, task_description)
                result["id"] = probe.get("id") or "probe-browser-e2e"
                probe_results.append(result)
            if probe.get("probe_type") == "api_service" and project_dir:
                result = _run_api_service_smoke(project_dir, contract)
                result["id"] = probe.get("id") or "probe-api-service"
                probe_results.append(result)
            if probe.get("probe_type") == "cli_generic" and project_dir:
                result = _run_cli_generic_smoke(project_dir, contract)
                result["id"] = probe.get("id") or "probe-cli-generic"
                probe_results.append(result)
            if probe.get("probe_type") == "pytest" and project_dir:
                result = _run_pytest(project_dir)
                result["id"] = probe.get("id") or "probe-pytest"
                probe_results.append(result)
            if probe.get("probe_type") == "notes_cli_smoke" and project_dir:
                result = _run_notes_cli_smoke(project_dir)
                result["id"] = probe.get("id") or "probe-notes-cli-smoke"
                probe_results.append(result)
        if _should_infer_static_web_probe(project_dir, task_types, contract, produced_required):
            result = _run_static_web_smoke(project_dir, task_description, contract)
            result["id"] = "probe-static-web-smoke-auto"
            result["inferred"] = True
            probe_results.append(result)

    required_probe_failures = [r for r in probe_results if not r.get("passed")]
    blocked = any(r.get("blocked_by_environment") for r in required_probe_failures)
    required_capabilities = [c for c in contract.get("capabilities", []) or [] if c.get("required", True)]
    if "functional" in task_types and not required_capabilities and not probe_results:
        evidence_gaps.append({
            "check_type": "functional_evidence_required",
            "message": "Functional delivery requires explicit capabilities or runnable acceptance probes.",
        })

    if missing_required or invalid_required or failed_constraints:
        quality = "failed"
    elif required_probe_failures:
        quality = "partial" if blocked else "failed"
    elif evidence_gaps or (required_capabilities and not probe_results):
        quality = "partial"
    else:
        quality = "passed"

    quality_report = build_quality_report(
        task_id=str(getattr(task, "task_id", "") or contract.get("task_id") or ""),
        contract=contract,
        gate_results=_quality_gate_results_from_acceptance(
            missing_required=missing_required,
            invalid_required=invalid_required,
            failed_constraints=failed_constraints,
            satisfied_agent_mix_constraints=satisfied_agent_mix_constraints,
            evidence_gaps=evidence_gaps,
            probe_results=probe_results,
            hygiene_report=hygiene_report,
        ),
    )

    return {
        "delivery_quality": quality,
        "missing_required": missing_required,
        "produced_required": produced_required,
        "invalid_required": invalid_required,
        "failed_constraints": failed_constraints,
        "evidence_gaps": evidence_gaps,
        "probe_results": probe_results,
        "capability_total": len(required_capabilities),
        "capability_evidence_level": "L2" if probe_results and all(r.get("passed") for r in probe_results) else "L1",
        "quality_report": quality_report,
    }


def _quality_gate_results_from_acceptance(
    *,
    missing_required: List[str],
    invalid_required: List[Dict[str, Any]],
    failed_constraints: List[Dict[str, Any]],
    satisfied_agent_mix_constraints: List[Dict[str, Any]],
    evidence_gaps: List[Dict[str, Any]],
    probe_results: List[Dict[str, Any]],
    hygiene_report: Dict[str, Any] | None,
) -> List[QualityGateResult]:
    results: List[QualityGateResult] = []
    if missing_required or invalid_required:
        results.append(QualityGateResult(
            gate_id="gate-artifact-integrity",
            adapter_id="artifact_integrity",
            status="failed",
            required=True,
            summary="Required deliverables are missing or invalid.",
            evidence={
                "missing_required": missing_required,
                "invalid_required": invalid_required,
            },
        ))
    elif missing_required == [] and invalid_required == []:
        results.append(QualityGateResult(
            gate_id="gate-artifact-integrity",
            adapter_id="artifact_integrity",
            status="passed",
            required=True,
            summary="Required deliverables are present and readable.",
        ))

    workspace_failures = [
        item for item in failed_constraints
        if item.get("constraint_type") == "workspace_hygiene"
    ]
    security_failures = [
        item for item in failed_constraints
        if item.get("constraint_type") in {"forbidden_unrequested_auth"}
    ]
    agent_mix_failures = [
        item for item in failed_constraints
        if item.get("constraint_type") == "agent_mix"
    ]
    other_constraints = [
        item for item in failed_constraints
        if item not in workspace_failures and item not in security_failures and item not in agent_mix_failures
    ]
    if workspace_failures:
        results.append(QualityGateResult(
            gate_id="gate-workspace-hygiene",
            adapter_id="workspace_hygiene",
            status="failed",
            required=True,
            summary="Workspace hygiene constraints failed.",
            evidence={"failed_constraints": workspace_failures},
        ))
    elif hygiene_report is not None:
        results.append(QualityGateResult(
            gate_id="gate-workspace-hygiene",
            adapter_id="workspace_hygiene",
            status="passed",
            required=True,
            summary="Workspace hygiene constraints passed.",
            evidence=hygiene_report,
        ))
    if security_failures:
        results.append(QualityGateResult(
            gate_id="gate-security-privacy",
            adapter_id="security_privacy",
            status="failed",
            required=True,
            summary="Security or privacy constraints failed.",
            evidence={"failed_constraints": security_failures},
        ))
    elif failed_constraints or probe_results:
        results.append(QualityGateResult(
            gate_id="gate-security-privacy",
            adapter_id="security_privacy",
            status="passed",
            required=True,
            summary="No security or privacy constraint failures were detected.",
        ))
    if agent_mix_failures:
        results.append(QualityGateResult(
            gate_id="gate-agent-mix",
            adapter_id="agent_mix",
            status="failed",
            required=True,
            summary="Required cross-agent execution mix was not met.",
            evidence={"failed_constraints": agent_mix_failures},
        ))
    elif satisfied_agent_mix_constraints:
        results.append(QualityGateResult(
            gate_id="gate-agent-mix",
            adapter_id="agent_mix",
            status="passed",
            required=True,
            summary="Required cross-agent execution mix was met.",
            evidence={"satisfied_constraints": satisfied_agent_mix_constraints},
        ))
    if other_constraints:
        results.append(QualityGateResult(
            gate_id="gate-constraint-scan",
            adapter_id="artifact_integrity",
            status="failed",
            required=True,
            summary="Required user constraints failed.",
            evidence={"failed_constraints": other_constraints},
        ))

    for probe in probe_results:
        passed = bool(probe.get("passed"))
        blocked_by_environment = bool(probe.get("blocked_by_environment"))
        results.append(QualityGateResult(
            gate_id=str(probe.get("id") or probe.get("probe_type") or "probe"),
            adapter_id=str(probe.get("probe_type") or "probe"),
            status="passed" if passed else ("skipped" if blocked_by_environment else "failed"),
            required=bool(probe.get("required", True)),
            summary=str(probe.get("output_tail") or probe.get("stage") or probe.get("probe_type") or ""),
            evidence={key: value for key, value in probe.items() if key not in {"output_tail"}},
            output_tail=str(probe.get("output_tail") or ""),
            blocked_by_environment=blocked_by_environment,
        ))

    if evidence_gaps:
        results.append(QualityGateResult(
            gate_id="gate-functional-evidence",
            adapter_id="contract_coverage",
            status="manual_required",
            required=True,
            summary="Functional delivery lacks an automatic validation recipe.",
            evidence={"evidence_gaps": evidence_gaps},
        ))
    return results
