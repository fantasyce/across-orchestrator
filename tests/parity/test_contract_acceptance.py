from pathlib import Path
from types import SimpleNamespace

from across_agents_assistant.task_manager.orchestration import contract_acceptance
from across_agents_assistant.task_manager.orchestration.contract_acceptance import run_delivery_contract_acceptance


class FakeTask:
    def __init__(self, project_dir):
        self.task_id = "task-1"
        self.project_dir = str(project_dir)


def test_pytest_probe_disables_cache_and_bytecode(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env", {})
        return SimpleNamespace(returncode=0, stdout="ok")

    monkeypatch.setattr(contract_acceptance.subprocess, "run", fake_run)

    result = contract_acceptance._run_pytest(str(tmp_path))

    assert result["passed"] is True
    assert "-p" in captured["args"]
    assert "no:cacheprovider" in captured["args"]
    assert captured["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "no:cacheprovider" in captured["env"]["PYTEST_ADDOPTS"]


def test_python_probe_executable_uses_system_python_when_packaged(monkeypatch):
    monkeypatch.setattr(contract_acceptance.sys, "frozen", True, raising=False)
    monkeypatch.setattr(contract_acceptance.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "python3" else None)
    monkeypatch.setattr(contract_acceptance.os.path, "isfile", lambda path: path == "/usr/bin/python3")
    monkeypatch.setattr(contract_acceptance.os, "access", lambda path, mode: path == "/usr/bin/python3")

    assert contract_acceptance._python_probe_executable() == "/usr/bin/python3"


def test_static_app_name_across_release_control_is_not_agent_routing_entity():
    description = (
        "Build a dashboard titled Across Release Control. "
        "The app must include Local Agents, Cloud LLMs, and a Route Evidence panel."
    )

    assert "Release Control" not in contract_acceptance._requested_agent_routing_entities(description)


def test_python_install_probe_fails_invalid_pyproject(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools>=64", "wheel"]
build-backend = "setuptools.backends._legacy:_Backend"
""".strip(),
        encoding="utf-8",
    )
    install_calls = []

    def fake_run(args, **kwargs):
        if args[1:3] == ["-m", "venv"]:
            return SimpleNamespace(returncode=0, stdout="venv ok")
        install_calls.append(args)
        return SimpleNamespace(returncode=1, stdout="Cannot import 'setuptools.backends._legacy'")

    monkeypatch.setattr(contract_acceptance.subprocess, "run", fake_run)

    result = contract_acceptance._run_python_install(str(tmp_path))

    assert result["passed"] is False
    assert result["stage"] == "install"
    assert "setuptools.backends._legacy" in result["output_tail"]
    assert install_calls
    assert install_calls[0][-2:] == ["-e", ".[dev]"]


def test_python_install_probe_skips_without_install_metadata(tmp_path):
    result = contract_acceptance._run_python_install(str(tmp_path))

    assert result["passed"] is True
    assert result["skipped"] is True


def test_python_web_smoke_probe_fails_when_frontend_root_is_not_html(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools>=64", "wheel"]
build-backend = "setuptools.build_meta"
""".strip(),
        encoding="utf-8",
    )
    package_dir = tmp_path / "expense_app"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n",
        encoding="utf-8",
    )

    captured_smoke_env = {}

    def fake_run(args, **kwargs):
        if args[1:3] == ["-m", "venv"]:
            return SimpleNamespace(returncode=0, stdout="venv ok")
        if args[1:4] == ["-m", "pip", "install"]:
            return SimpleNamespace(returncode=0, stdout="install ok")
        captured_smoke_env.update(kwargs.get("env", {}))
        return SimpleNamespace(
            returncode=1,
            stdout="GET / -> 200 application/json\nGET /static/index.html -> 404 application/json\nExpected GET / or /static/index.html to serve a usable HTML frontend.",
        )

    monkeypatch.setattr(contract_acceptance.subprocess, "run", fake_run)

    result = contract_acceptance._run_python_web_smoke(str(tmp_path), require_html_root=True)

    assert result["passed"] is False
    assert result["stage"] == "smoke"
    assert result["require_html_root"] is True
    assert "expense_app.main:app" in result["candidates"]
    assert "Expected GET / or /static/index.html" in result["output_tail"]
    assert str(tmp_path) in captured_smoke_env["PYTHONPATH"].split(contract_acceptance.os.pathsep)


def test_python_web_smoke_probe_skips_without_web_app(tmp_path):
    result = contract_acceptance._run_python_web_smoke(str(tmp_path), require_html_root=True)

    assert result["passed"] is True
    assert result["skipped"] is True


def test_artifact_contract_passes_when_required_files_exist(tmp_path):
    (tmp_path / "README.md").write_text("# Usage\n\nRun pytest.\n", encoding="utf-8")
    contract = {
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "capabilities": [],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "passed"
    assert report["missing_required"] == []
    assert report["quality_report"]["quality_gate"] == "passed"
    assert report["quality_report"]["can_complete"] is True


def test_contract_acceptance_quality_report_blocks_failed_probe(monkeypatch, tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    def fake_python_install(project_dir):
        return {
            "probe_type": "python_install",
            "passed": False,
            "returncode": 1,
            "output_tail": "install failed",
            "blocked_by_environment": False,
        }

    monkeypatch.setattr(contract_acceptance, "_run_python_install", fake_python_install)
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-install", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-python-install", "probe_type": "python_install", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "failed"
    assert report["quality_report"]["quality_gate"] == "failed"
    assert report["quality_report"]["can_complete"] is False
    assert report["quality_report"]["required_failed_count"] == 1


def test_workspace_hygiene_fails_on_runtime_noise(tmp_path):
    (tmp_path / "README.md").write_text("# Usage\n\nRun pytest.\n", encoding="utf-8")
    noisy_files = [
        tmp_path / ".venv" / "lib" / "python3.14" / "site-packages" / "pkg.py",
        tmp_path / ".pytest_cache" / "v" / "cache" / "nodeids",
        tmp_path / "expense_app.egg-info" / "PKG-INFO",
        tmp_path / "backend" / "__pycache__" / "app.cpython-314.pyc",
        tmp_path / "backend" / "uploads" / "receipt.png",
        tmp_path / "receipts" / "receipt.png",
        tmp_path / "backend" / "instance" / "expenses.db",
        tmp_path / "_install_deps.py",
        tmp_path / "check_env.py",
        tmp_path / "run_tests.py",
        tmp_path / "run_tests_direct.py",
        tmp_path / "runner.py",
        tmp_path / "run_all_checks.py",
        tmp_path / "run_syntax_check.py",
        tmp_path / "setup_test_env.py",
        tmp_path / "test_import.py",
    ]
    for path in noisy_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("noise", encoding="utf-8")
    contract = {
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "capabilities": [],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "failed"
    hygiene_failures = [
        failure for failure in report["failed_constraints"]
        if failure["constraint_type"] == "workspace_hygiene"
    ]
    assert hygiene_failures
    evidence = "\n".join(hygiene_failures[0]["evidence"])
    assert ".venv" in evidence
    assert "_install_deps.py" in evidence
    assert "run_tests.py" in evidence
    assert "runner.py" in evidence
    assert "run_all_checks.py" in evidence
    assert "run_syntax_check.py" in evidence
    assert "setup_test_env.py" in evidence
    assert "test_import.py" in evidence


def test_contract_acceptance_fails_disallowed_extra_documentation(tmp_path):
    (tmp_path / "README.md").write_text("# App\n", encoding="utf-8")
    (tmp_path / "TESTING.md").write_text("# Tests\n", encoding="utf-8")
    (tmp_path / "SPEC.md").write_text("# Extra spec\n", encoding="utf-8")
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
            {"path_hint": "TESTING.md", "artifact_type": "documentation", "required": True},
        ],
        "capabilities": [],
        "constraints": [
            {
                "constraint_type": "allowed_documentation_files",
                "value": ["README.md", "TESTING.md"],
                "required": True,
            }
        ],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "failed"
    docs_failures = [
        failure for failure in report["failed_constraints"]
        if failure["constraint_type"] == "allowed_documentation_files"
    ]
    assert docs_failures
    assert any(path.endswith("SPEC.md") for path in docs_failures[0]["evidence"])
    assert not any(path.endswith("requirements.txt") for path in docs_failures[0]["evidence"])


def test_allowed_documentation_constraint_keeps_requirements_txt(tmp_path):
    (tmp_path / "README.md").write_text("# App\n", encoding="utf-8")
    (tmp_path / "TESTING.md").write_text("# Tests\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi\npytest\n", encoding="utf-8")
    contract = {
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "deliverables": [
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
            {"path_hint": "TESTING.md", "artifact_type": "documentation", "required": True},
            {"path_hint": "requirements.txt", "artifact_type": "install_metadata", "required": True},
        ],
        "capabilities": [],
        "constraints": [
            {
                "constraint_type": "allowed_documentation_files",
                "value": ["README.md", "TESTING.md"],
                "required": True,
            }
        ],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [], run_probes=False)

    assert report["delivery_quality"] == "passed"
    assert report["failed_constraints"] == []


def test_contract_acceptance_fails_when_required_source_groups_are_missing(tmp_path):
    (tmp_path / "README.md").write_text("# App\n", encoding="utf-8")
    (tmp_path / "TESTING.md").write_text("# Tests\n", encoding="utf-8")
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "delivery_facets": ["source_project", "runnable_app", "web_ui", "api_service", "test_suite"],
        "deliverables": [
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
            {"path_hint": "TESTING.md", "artifact_type": "documentation", "required": True},
        ],
        "deliverable_groups": [
            {
                "id": "group-api-source",
                "kind": "api_service_source",
                "required": True,
                "allowed_roots": ["expense_app/", "app/", "src/", "."],
                "allowed_extensions": [".py"],
                "one_of_entrypoints": ["main.py", "app/main.py", "expense_app/main.py"],
                "min_file_count": 1,
            },
            {
                "id": "group-web-ui",
                "kind": "frontend_source",
                "required": True,
                "allowed_roots": ["static/", "public/", "."],
                "allowed_extensions": [".html", ".css", ".js"],
                "one_of_entrypoints": ["index.html", "static/index.html", "public/index.html"],
                "min_file_count": 1,
            },
            {
                "id": "group-test-suite",
                "kind": "test_suite",
                "required": True,
                "allowed_roots": ["tests/", "test/"],
                "allowed_extensions": [".py"],
                "min_file_count": 1,
            },
        ],
        "capabilities": [{"id": "cap-frontend-loads", "description": "Frontend loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [], run_probes=False)

    assert report["delivery_quality"] == "failed"
    group_failures = [
        item for item in report["invalid_required"]
        if item.get("check_type") in {"deliverable_group_entrypoint", "deliverable_group_min_file_count"}
    ]
    assert group_failures
    assert any(item["group_id"] == "group-api-source" for item in group_failures)
    assert any(item["group_id"] == "group-web-ui" for item in group_failures)
    assert any(item["group_id"] == "group-test-suite" for item in group_failures)


def test_web_ui_group_accepts_app_static_index_for_fastapi_static_apps(tmp_path):
    (tmp_path / "app" / "static").mkdir(parents=True)
    (tmp_path / "app" / "static" / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "deliverable_groups": [
            {
                "id": "group-web-ui",
                "kind": "frontend_source",
                "required": True,
                "allowed_roots": ["static/", "public/", "assets/", "src/", "app/", "."],
                "allowed_extensions": [".html", ".css", ".js"],
                "one_of_entrypoints": ["index.html", "static/index.html", "public/index.html"],
                "min_file_count": 1,
            }
        ],
        "capabilities": [],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [], run_probes=False)

    assert report["invalid_required"] == []


def test_contract_acceptance_fails_fastapi_task_that_uses_flask(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "app.py").write_text(
        "from fastapi import FastAPI\nfrom flask_sqlalchemy import SQLAlchemy\napp = FastAPI()\n",
        encoding="utf-8",
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [{"path_hint": "backend/app.py", "artifact_type": "api_service_source", "required": True}],
        "capabilities": [],
        "constraints": [],
        "acceptance_probes": [],
    }
    task = FakeTask(tmp_path)
    task.description = "Use Python FastAPI and SQLite. Do not use Flask."

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    assert any(item.get("check_type") == "requested_framework_alignment" for item in report["invalid_required"])


def test_contract_acceptance_fails_sqlite_task_that_uses_postgresql_asyncpg(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "database.py").write_text(
        "DATABASE_URL = 'postgresql+asyncpg://postgres:postgres@localhost/db'\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("fastapi\nasyncpg\npsycopg2-binary\n", encoding="utf-8")
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [{"path_hint": "app/database.py", "artifact_type": "api_service_source", "required": True}],
        "capabilities": [],
        "constraints": [],
        "acceptance_probes": [],
    }
    task = FakeTask(tmp_path)
    task.description = "Use Python FastAPI + SQLite. Do not use PostgreSQL."

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    assert any(item.get("check_type") == "requested_storage_alignment" for item in report["invalid_required"])


def test_contract_acceptance_fails_unrequested_auth_artifacts(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "auth.py").write_text(
        "from passlib.context import CryptContext\n"
        "pwd_context = CryptContext(schemes=['bcrypt'])\n"
        "def get_password_hash(password):\n"
        "    return pwd_context.hash(password)\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("fastapi\npasslib[bcrypt]\nbcrypt\n", encoding="utf-8")
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [{"path_hint": "app/auth.py", "artifact_type": "api_service_source", "required": True}],
        "capabilities": [],
        "constraints": [
            {
                "id": "constraint-no-unrequested-auth",
                "constraint_type": "forbidden_unrequested_auth",
                "value": "auth",
                "required": True,
            }
        ],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "failed"
    auth_failures = [
        failure for failure in report["failed_constraints"]
        if failure["constraint_type"] == "forbidden_unrequested_auth"
    ]
    assert auth_failures
    assert any(path.endswith("/app/auth.py") for path in auth_failures[0]["evidence"])


def test_contract_acceptance_treats_slash_file_hint_as_alternatives(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup(name='demo')\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    contract = {
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "deliverables": [{"path_hint": "setup.py/pyproject.toml", "artifact_type": "config_file", "required": True}],
        "capabilities": [],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "passed"
    assert report["missing_required"] == []


def test_allowed_files_constraint_fails_on_extra_business_file(tmp_path):
    (tmp_path / "README.md").write_text("# Usage\n\nRun pytest.\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print('extra')\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    contract = {
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "deliverables": [{"path_hint": "README.md", "artifact_type": "documentation", "required": True}],
        "capabilities": [],
        "constraints": [
            {
                "id": "constraint-allowed-files",
                "constraint_type": "allowed_files",
                "value": ["README.md"],
                "required": True,
            }
        ],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "failed"
    failure = report["failed_constraints"][0]
    assert failure["constraint_type"] == "allowed_files"
    assert any(path.endswith("/main.py") for path in failure["evidence"])
    assert not any(".claude/settings.json" in path for path in failure["evidence"])


def test_no_docker_constraint_fails_when_dockerfile_exists(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [],
        "constraints": [{"constraint_type": "forbidden_tooling", "value": "docker", "required": True}],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "failed"
    assert "constraint-no-docker" in report["failed_constraints"][0]["id"]


def test_functional_contract_pytest_probe_passes(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-smoke", "description": "Smoke behavior", "required": True, "minimum_evidence": "L2"}],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-pytest", "probe_type": "pytest", "command": "pytest", "required": True}],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "passed"
    assert report["probe_results"][0]["passed"] is True


def test_functional_static_web_delivery_infers_smoke_probe(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Agent Dashboard</h1>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text("document.body.dataset.ready = 'true';\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Dashboard\n\nRun `python3 -m http.server`.\n", encoding="utf-8")
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "file", "required": True},
            {"path_hint": "styles.css", "artifact_type": "file", "required": True},
            {"path_hint": "app.js", "artifact_type": "file", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "passed"
    assert report["capability_evidence_level"] == "L2"
    assert report["probe_results"][0]["probe_type"] == "static_web_smoke"
    assert report["probe_results"][0]["inferred"] is True


def test_static_web_smoke_fails_when_required_assets_are_not_linked(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><style>body { font-family: system-ui; }</style></head>
  <body>
    <h1>Agent Dashboard</h1>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { color: white; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text("document.body.dataset.ready = 'true';\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Dashboard\n", encoding="utf-8")
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "file", "required": True},
            {"path_hint": "styles.css", "artifact_type": "file", "required": True},
            {"path_hint": "app.js", "artifact_type": "file", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "failed"
    assert report["probe_results"][0]["probe_type"] == "static_web_smoke"
    assert report["probe_results"][0]["passed"] is False
    assert "Required static assets are not referenced" in report["probe_results"][0]["output_tail"]
    assert "styles.css" in report["probe_results"][0]["output_tail"]
    assert "app.js" in report["probe_results"][0]["output_tail"]


def test_static_web_smoke_accepts_web_directory_entrypoint(tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text(
        """
<!doctype html>
<html>
  <head>
    <link rel="stylesheet" href="styles.css">
    <script defer src="app.js"></script>
  </head>
  <body>
    <main class="console">
      <section class="agent-card"><button data-skill="route">Route</button></section>
      <section class="llm-card"><button data-skill="review">Review</button></section>
    </main>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (web_dir / "styles.css").write_text(".console { display: grid; }\n", encoding="utf-8")
    (web_dir / "app.js").write_text("localStorage.setItem('ready', 'yes');\n", encoding="utf-8")
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "web/index.html", "artifact_type": "file", "required": True},
            {"path_hint": "web/styles.css", "artifact_type": "file", "required": True},
            {"path_hint": "web/app.js", "artifact_type": "file", "required": True},
        ],
        "capabilities": [],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "passed"
    assert report["probe_results"][0]["passed"] is True
    assert report["probe_results"][0]["entrypoint"] == "web/index.html"


def test_static_web_smoke_does_not_require_backend_cli_tests_as_static_assets(tmp_path):
    web_dir = tmp_path / "web"
    api_dir = tmp_path / "api"
    cli_dir = tmp_path / "cli"
    tests_dir = tmp_path / "tests"
    for directory in (web_dir, api_dir, cli_dir, tests_dir):
        directory.mkdir()
    (web_dir / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body><main><h1>Across Release Control</h1></main><script src="app.js"></script></body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (web_dir / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (web_dir / "app.js").write_text("localStorage.setItem('ready', 'yes');\n", encoding="utf-8")
    (api_dir / "server.mjs").write_text("import http from 'node:http';\n", encoding="utf-8")
    (cli_dir / "quality-check.mjs").write_text("console.log(JSON.stringify({ok:true}));\n", encoding="utf-8")
    (tests_dir / "e2e-smoke.mjs").write_text("import assert from 'node:assert';\n", encoding="utf-8")
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "web/index.html", "artifact_type": "file", "required": True},
            {"path_hint": "web/styles.css", "artifact_type": "file", "required": True},
            {"path_hint": "web/app.js", "artifact_type": "file", "required": True},
            {"path_hint": "api/server.mjs", "artifact_type": "file", "required": True},
            {"path_hint": "cli/quality-check.mjs", "artifact_type": "file", "required": True},
            {"path_hint": "tests/e2e-smoke.mjs", "artifact_type": "file", "required": True},
        ],
        "capabilities": [],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "passed"
    assert report["probe_results"][0]["passed"] is True


def test_static_web_smoke_fails_on_obvious_html_structure_errors(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Agent Dashboard</h1>
    <span>OpenClaw</span></span>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text("document.body.dataset.ready = 'true';\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Dashboard\n", encoding="utf-8")
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "file", "required": True},
            {"path_hint": "styles.css", "artifact_type": "file", "required": True},
            {"path_hint": "app.js", "artifact_type": "file", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "failed"
    assert report["probe_results"][0]["probe_type"] == "static_web_smoke"
    assert report["probe_results"][0]["passed"] is False
    assert "HTML structure issues" in report["probe_results"][0]["output_tail"]


def test_static_web_smoke_fails_when_requested_interactions_are_missing(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Across Final Complex E2E</h1>
    <button id="actionBtn">Run Check</button>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text("document.getElementById('actionBtn').click();\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Dashboard\n\nRun `python3 -m http.server`.\n", encoding="utf-8")
    task = FakeTask(tmp_path)
    task.description = (
        "Create a static web app with theme toggle, agent capability cards, "
        "task orchestration timeline, quality checklist, mock task detail panel, "
        "and keyboard-friendly interactions."
    )
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "file", "required": True},
            {"path_hint": "styles.css", "artifact_type": "file", "required": True},
            {"path_hint": "app.js", "artifact_type": "file", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    assert report["probe_results"][0]["probe_type"] == "static_web_smoke"
    assert report["probe_results"][0]["passed"] is False
    assert "theme toggle" in report["probe_results"][0]["output_tail"]
    assert "agent capability cards" in report["probe_results"][0]["output_tail"]


def test_contract_acceptance_runs_required_browser_e2e_probe(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><body><h1>Capability Garden Planner</h1><script src='app.js'></script></body></html>",
        encoding="utf-8",
    )
    (tmp_path / "app.js").write_text("document.body.dataset.ready = 'true';\n", encoding="utf-8")

    def fake_browser_e2e(project_dir, task_description=None):
        return {
            "probe_type": "browser_e2e",
            "passed": False,
            "returncode": 1,
            "output_tail": "no browser page errors: boom",
            "blocked_by_environment": False,
            "entrypoint": "index.html",
        }

    monkeypatch.setattr(contract_acceptance, "_run_browser_e2e", fake_browser_e2e)

    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-browser-e2e", "probe_type": "browser_e2e", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "failed"
    assert report["probe_results"][0]["probe_type"] == "browser_e2e"
    assert report["quality_report"]["quality_gate"] == "failed"
    assert any(
        gate["adapter_id"] == "browser_e2e"
        for gate in report["quality_report"]["gate_results"]
    )


def test_browser_e2e_probe_reports_page_errors(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><body><h1>Capability Garden Planner</h1><canvas></canvas><script src='app.js'></script></body></html>",
        encoding="utf-8",
    )
    (tmp_path / "app.js").write_text("throw new Error('canvas failed');\n", encoding="utf-8")

    monkeypatch.setattr(contract_acceptance, "_node_probe_executable", lambda: "/usr/bin/node")

    def fake_run(args, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout=(
                '{"passed":false,"blockedByEnvironment":false,'
                '"failures":["no browser page errors: canvas failed"],'
                '"checks":[],"consoleMessages":[],"pageErrors":["canvas failed"]}'
            ),
        )

    monkeypatch.setattr(contract_acceptance.subprocess, "run", fake_run)

    result = contract_acceptance._run_browser_e2e(
        str(tmp_path),
        "Build a static web app with a canvas animation.",
    )

    assert result["passed"] is False
    assert result["blocked_by_environment"] is False
    assert "no browser page errors" in result["output_tail"]
    assert result["page_errors"] == ["canvas failed"]


def test_browser_e2e_probe_mutates_task_input_before_route_recompute(monkeypatch, tmp_path):
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><body><textarea id='task-text'></textarea><section class='route-evidence-section'></section><button>Recompute Route</button></body></html>",
        encoding="utf-8",
    )

    monkeypatch.setattr(contract_acceptance, "_node_probe_executable", lambda: "/usr/bin/node")
    captured = {}

    def fake_run(args, **kwargs):
        script_path = args[1]
        captured["source"] = Path(script_path).read_text(encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout='{"passed":true,"blockedByEnvironment":false,"failures":[],"checks":[],"consoleMessages":[],"pageErrors":[]}',
        )

    monkeypatch.setattr(contract_acceptance.subprocess, "run", fake_run)

    result = contract_acceptance._run_browser_e2e(
        str(tmp_path),
        "Build a Route Evidence panel with a Recompute Route button.",
    )

    assert result["passed"] is True
    assert ".route-evidence-section" in captured["source"]
    assert "dispatchEvent(new Event('input'" in captured["source"]
    assert "Review backend API schema, browser E2E" in captured["source"]


def test_static_web_smoke_fails_when_explicit_agent_console_requirements_are_missing(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <section><h2>Local Agents</h2><p>Claude Code</p></section>
    <section><h2>Cloud LLMs</h2><p>Claude Sonnet</p></section>
    <section><h2>Task Composer</h2><textarea></textarea><button>Compose Task</button></section>
    <section><h2>Route Preview</h2><p>No route planned</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "const STORAGE_KEY = 'demo'; localStorage.setItem(STORAGE_KEY, JSON.stringify({skill: true}));\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "# Native Skills Webapp\n\nRun `npm install` then `npm run dev`.\n\n(Add your tech stack details here)\n",
        encoding="utf-8",
    )
    task = FakeTask(tmp_path)
    task.description = (
        "Local Agents must include OpenClaw, Hermes, and Claude Code. "
        "Cloud LLMs must include DeepSeek and MiniMax. "
        "The Task Composer must include a textarea, priority selector, strict-mode toggle, "
        "and a button that recomputes a recommended route without reloading. "
        "Route Preview must show the chosen owner agent, at least two worker steps, "
        "quality gates, and a concise risk note. "
        "Persist the latest composer text, priority, strict mode, and selected skill toggles in localStorage. "
        "Each agent row must show at least three configurable skill chips or toggles. "
        "No package managers. No placeholder Lorem Ipsum."
    )
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "file", "required": True},
            {"path_hint": "styles.css", "artifact_type": "file", "required": True},
            {"path_hint": "app.js", "artifact_type": "file", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "capabilities": [{"id": "cap-web-ui", "description": "Static console loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    output = report["probe_results"][0]["output_tail"]
    assert "Local Agents missing requested item: OpenClaw" in output
    assert "Cloud LLMs missing requested item: DeepSeek" in output
    assert "priority selector" in output
    assert "strict-mode toggle" in output
    assert "quality gates" in output
    assert "forbidden package-manager instructions" in output
    assert "README.md" in output
    assert "placeholder content" in output


def test_static_web_smoke_fails_when_route_evidence_never_updates(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <section><h2>Local Agents</h2>
      <div class="agent-card"><h3>OpenClaw</h3><input data-skill="apple"><input data-skill="native"><input role="switch" type="checkbox"></div>
      <div class="agent-card"><h3>Hermes</h3><input data-skill="p5js"><input data-skill="canvas"><input role="switch" type="checkbox"></div>
    </section>
    <section><h2>Task Composer</h2><textarea></textarea><select><option>Normal</option></select><label><input role="switch" type="checkbox">Strict Mode</label></section>
    <section id="routeEvidencePanel"><h2>Route Evidence</h2><div id="routeEvidence">No route evidence yet</div></section>
    <button id="recomputeRoute">Recompute Route</button>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("@media (max-width: 390px) { body { max-width: 100%; } }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "document.getElementById('recomputeRoute').addEventListener('click', () => renderCanvas());\n"
        "localStorage.setItem('route', JSON.stringify({priority: 'normal', strict: false, skill: 'native'}));\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Static app\n\nOpen index.html directly.\n", encoding="utf-8")
    task = FakeTask(tmp_path)
    task.description = (
        "Create a route evidence panel that shows why a subtask was assigned to an agent "
        "based on native skills. Include a button that recomputes a recommended route without reloading."
    )
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "file", "required": True},
            {"path_hint": "styles.css", "artifact_type": "file", "required": True},
            {"path_hint": "app.js", "artifact_type": "file", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "capabilities": [{"id": "cap-web-ui", "description": "Static console loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    output = report["probe_results"][0]["output_tail"]
    assert "route evidence update" in output
    assert "route evidence rationale" in output


def test_static_web_smoke_requires_requested_app_name_and_delivery_report_metrics(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Across E2E Quality Garden</h1>
    <section><h2>Delivery Report</h2><p>Quality Score: 0/100</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text("document.body.dataset.ready = 'true';\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Across E2E Quality Garden\n\nOpen index.html directly.\n", encoding="utf-8")
    task = FakeTask(tmp_path)
    task.description = (
        "Build a polished static web app called Capability Garden Planner inside the project directory. "
        "The delivery report panel must show generated quality score, final quality score, "
        "required gate failures, manual checks, skipped checks, and final verdict."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [{"path_hint": "index.html", "artifact_type": "file", "required": True}],
        "capabilities": [{"id": "cap-web-ui", "description": "Static console loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    output = report["probe_results"][0]["output_tail"]
    assert "application name: Capability Garden Planner" in output
    assert "delivery report metric: generated quality score" in output
    assert "delivery report metric: final quality score" in output
    assert "delivery report metric: required gate failures" in output
    assert "delivery report metric: manual checks" in output
    assert "delivery report metric: skipped checks" in output
    assert "delivery report metric: final verdict" in output


def test_static_web_smoke_does_not_count_comments_as_delivery_report_metrics(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"><title>Capability Garden Planner</title></head>
  <body>
    <h1>Capability Garden Planner</h1>
    <section><h2>Delivery Report</h2><p>Quality Score: 0/100</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "// Generated quality score, final quality score, required gate failures, manual checks, skipped checks, final verdict.\n"
        "document.body.dataset.ready = 'true';\n",
        encoding="utf-8",
    )
    task = FakeTask(tmp_path)
    task.description = (
        "Build a polished static web app called Capability Garden Planner. "
        "The delivery report panel must show generated quality score, final quality score, "
        "required gate failures, manual checks, skipped checks, and final verdict."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [{"path_hint": "index.html", "artifact_type": "file", "required": True}],
        "capabilities": [{"id": "cap-web-ui", "description": "Static console loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    output = report["probe_results"][0]["output_tail"]
    assert "delivery report metric: generated quality score" in output
    assert "application name: Capability Garden Planner" not in output


def test_static_web_smoke_fails_when_runtime_overwrites_requested_visible_contract(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head>
    <title>Capability Garden Planner</title>
    <link rel="stylesheet" href="styles.css">
  </head>
  <body>
    <h1>Across E2E Quality Garden</h1>
    <section class="agents-grid">
      <div class="agent-card"><h2>Local SWE Agent</h2><label><input type="checkbox"> Backend</label><label><input type="checkbox"> API</label><label><input type="checkbox"> Review</label></div>
      <div class="llm-card"><h2>GPT-4o</h2><label><input type="checkbox"> Reasoning</label><label><input type="checkbox"> Code</label><label><input type="checkbox"> Analysis</label></div>
    </section>
    <section class="route-evidence"><h2>Route Evidence Panel</h2><div class="evidence-list"><div>Selected: OpenClaw - matched: coding native skill</div><div>Reason: static placeholder</div></div></section>
    <section class="delivery-report"><h2>Delivery Report</h2><div class="report-metrics">
      <div>Generated Quality Score</div><div>Final Quality Score</div><div>Required Gate Failures</div>
      <div>Manual Checks</div><div>Skipped Checks</div><div>Final Verdict</div>
    </div></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        """
const agents = ['Local SWE Agent', 'GPT-4o'];
function renderEvidence() {
  document.querySelector('.evidence-list').innerHTML = '<div>Route: POST /api/tasks - Agent: Local SWE Agent</div>';
}
function renderDeliveryMetrics() {
  document.querySelector('.report-metrics').innerHTML =
    '<div>Quality Gate Score</div><div>Tasks Completed</div><div>Quality Gate Verdict</div>';
}
document.querySelector('.route-evidence').insertAdjacentHTML('beforeend', '<button>Recompute Route</button>');
localStorage.setItem('state', JSON.stringify({ priority: 'high', strict: true, skill: 'native' }));
renderEvidence();
renderDeliveryMetrics();
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Capability Garden Planner\n\nOpen index.html directly.\n", encoding="utf-8")
    task = FakeTask(tmp_path)
    task.description = (
        "Build a polished static web app called Capability Garden Planner. "
        "A full-screen canvas animation container visualizes cross-agent routing across "
        "OpenClaw, Hermes, Claude Code, DeepSeek, and MiniMax. "
        "A route evidence panel has a Recompute Route button. Clicking it must update visible rows "
        "containing selected agent, matched skill/native skill, MCP risk, and reason text. "
        "A delivery report panel shows generated quality score, final quality score, "
        "required gate failures, manual checks, skipped checks, and final verdict."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    output = report["probe_results"][0]["output_tail"].lower()
    assert "application name: capability garden planner" in output
    assert "agent routing surface missing requested item: openclaw" in output
    assert "agent routing surface missing requested item: hermes" in output
    assert "delivery report runtime metric: generated quality score" in output
    assert "route evidence runtime row missing: mcp risk" in output


def test_static_web_smoke_fails_on_broken_runtime_ids_and_display_names(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><title>Capability Garden Planner</title><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Capability Garden Planner</h1>
    <section class="agents-grid">
      <article class="agent-card"><h2>openclaw</h2><label><input data-skill="code">Code</label><label><input data-skill="test">Test</label><label><input data-skill="file">File</label></article>
      <article class="agent-card"><h2>hermes</h2><label><input data-skill="design">Design</label><label><input data-skill="native">Native</label><label><input data-skill="test">Test</label></article>
      <article class="llm-card"><h2>claude</h2><label><input data-skill="code">Code</label><label><input data-skill="reason">Reason</label><label><input data-skill="review">Review</label></article>
      <article class="llm-card"><h2>deepseek</h2><label><input data-skill="math">Math</label><label><input data-skill="code">Code</label><label><input data-skill="cost">Cost</label></article>
      <article class="llm-card"><h2>minimax</h2><label><input data-skill="voice">Voice</label><label><input data-skill="fast">Fast</label><label><input data-skill="media">Media</label></article>
    </section>
    <section id="task-contract"><h2>Task Contract Builder</h2><label><input id="mode-functional" type="checkbox" role="switch" checked>Functional</label><label><input id="mode-artifact" type="checkbox" role="switch">Artifact</label><ul id="contract-checklist" class="checklist"><li><input id="gate-1" type="checkbox"><label for="gate-1">E2E smoke test</label></li></ul></section>
    <section id="route-evidence"><h2>Route Evidence</h2><button id="recompute-route-btn">Recompute Route</button><div id="evidence-list">selected agent, matched skill, MCP risk, reason</div></section>
    <section id="delivery-report"><h2>Delivery Report</h2><div>Generated Quality Score</div><div>Final Quality Score</div><div>Required Gate Failures</div><div>Manual Checks</div><div>Skipped Checks</div><div>Final Verdict</div></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        """
const agents = ['openclaw', 'hermes', 'claude', 'deepseek', 'minimax'];
const statQualityScore = document.getElementById('stat-quality-score');
const statFinalScore = document.getElementById('stat-final-score');
function updateContractMode() {
  if (modeFunctional.checked && modeArtifact.checked) {
    modeArtifact.checked = false;
  }
}
checklist.addEventListener('click', (e) => {
  if (e.target.tagName === 'INPUT') return;
  const cb = e.target.closest('li').querySelector('input[type="checkbox"]');
  cb.checked = !cb.checked;
});
document.getElementById('recompute-route-btn').addEventListener('click', () => {
  document.getElementById('evidence-list').textContent = 'selected agent, matched native skill, MCP risk, reason';
});
localStorage.setItem('contract', JSON.stringify({priority: 'normal', strict: true, skill: 'native'}));
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Capability Garden Planner\n\nOpen index.html directly.\n", encoding="utf-8")
    task = FakeTask(tmp_path)
    task.description = (
        "Build a polished static web app called Capability Garden Planner. "
        "A full-screen canvas animation container visualizes cross-agent routing across "
        "OpenClaw, Hermes, Claude Code, DeepSeek, and MiniMax. "
        "An interactive task contract builder has Functional and Artifact modes, a quality gate checklist, "
        "keyboard-accessible controls, and localStorage persistence. "
        "A route evidence panel has a Recompute Route button. Clicking it must update visible rows "
        "containing selected agent, matched skill/native skill, MCP risk, and reason text. "
        "A delivery report panel shows generated quality score, final quality score, "
        "required gate failures, manual checks, skipped checks, and final verdict."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    output = report["probe_results"][0]["output_tail"].lower()
    assert "agent routing display text missing requested item: hermes" in output
    assert "agent routing display text missing requested item: claude code" in output
    assert "runtime dom target missing: #stat-quality-score" in output
    assert "runtime dom target missing: #stat-final-score" in output
    assert "functional/artifact mode toggle cannot select artifact" in output
    assert "checklist label click double-toggle risk" in output


def test_static_web_smoke_fails_on_runtime_js_and_reportcontent_metric_drift(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Capability Garden Planner</h1>
    <div id="canvas-container"></div>
    <section class="agents-grid">
      <div class="agent-card"><h2>OpenClaw</h2><input data-skill="a"><input data-skill="b"><input data-skill="native"></div>
      <div class="agent-card"><h2>Hermes</h2><input data-skill="a"><input data-skill="b"><input data-skill="native"></div>
      <div class="agent-card"><h2>Claude Code</h2><input data-skill="a"><input data-skill="b"><input data-skill="native"></div>
      <div class="llm-card"><h2>DeepSeek</h2><input data-skill="a"><input data-skill="b"><input data-skill="c"></div>
      <div class="llm-card"><h2>MiniMax</h2><input data-skill="a"><input data-skill="b"><input data-skill="c"></div>
    </section>
    <section aria-label="Route evidence panel"><h2>Route Evidence</h2><button id="recompute-route">Recompute Route</button><ul class="evidence-list"><li>selected agent, matched skill, MCP risk, reason</li></ul></section>
    <section aria-label="Delivery report panel"><h2>Delivery Report</h2><div class="report-content"><span>Generated Quality Score</span><span>Final Quality Score</span><span>Required Gate Failures</span><span>Manual Checks</span><span>Skipped Checks</span><span>Final Verdict</span></div></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        """
const pairs = [['a', 'b']];
pairs.forEach(([a, b]) => {
  const t = (i * 0.2 + j * 0.3) % 1;
  console.log(a, b, t);
});
function updateDeliveryReport() {
  document.querySelector('.report-content').innerHTML =
    '<div>Generated Quality Score</div><div>Final Quality Score</div><div>Gate Failures</div><div>Manual Checks</div><div>Skipped Checks</div><div>Verdict</div>';
}
document.getElementById('recompute-route').addEventListener('click', () => {
  document.querySelector('.evidence-list').innerHTML = '<li>selected agent, matched native skill, MCP risk, reason</li>';
});
localStorage.setItem('state', JSON.stringify({priority: 'normal', strict: true, skill: 'native'}));
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Capability Garden Planner\n\nOpen index.html directly.\n", encoding="utf-8")
    task = FakeTask(tmp_path)
    task.description = (
        "Build a polished static web app called Capability Garden Planner. "
        "A full-screen canvas animation container visualizes cross-agent routing across "
        "OpenClaw, Hermes, Claude Code, DeepSeek, and MiniMax. "
        "A route evidence panel has a Recompute Route button. Clicking it must update visible rows "
        "containing selected agent, matched skill/native skill, MCP risk, and reason text. "
        "A delivery report panel shows generated quality score, final quality score, "
        "required gate failures, manual checks, skipped checks, and final verdict. "
        "Persist priority, strict mode, and selected skill toggles in localStorage."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    output = report["probe_results"][0]["output_tail"].lower()
    assert "delivery report runtime metric: required gate failures" in output
    assert "delivery report runtime metric: final verdict" in output
    assert "javascript runtime risk: foreach callback uses undefined index variable i" in output
    assert "javascript runtime risk: foreach callback uses undefined index variable j" in output


def test_static_web_smoke_fails_when_canvas_nodes_initialized_before_resize(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><title>Capability Garden Planner</title><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Capability Garden Planner</h1>
    <canvas id="animation-container"></canvas>
    <section><h2>Delivery Report</h2><p>Generated Quality Score Final Quality Score Required Gate Failures Manual Checks Skipped Checks Final Verdict</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        """
const canvas = document.getElementById('animation-container');
const ctx = canvas.getContext('2d');
let W, H;
class AgentNode {
  constructor(index) {
    this.index = index;
    this.reset();
  }
}
AgentNode.prototype.reset = function() {
  const margin = 120;
  this.x = margin + Math.random() * (W - margin * 2);
  this.y = margin + Math.random() * (H - margin * 2);
  this.radius = 8;
};
AgentNode.prototype.draw = function() {
  const grad = ctx.createRadialGradient(this.x, this.y, 0, this.x, this.y, this.radius * 3);
  grad.addColorStop(0, '#a855f760');
  grad.addColorStop(1, '#a855f700');
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, W, H);
};
const nodes = [];
for (let i = 0; i < 5; i++) nodes.push(new AgentNode(i));
function resizeCanvas() {
  W = window.innerWidth;
  H = window.innerHeight;
  canvas.width = W;
  canvas.height = H;
}
resizeCanvas();
nodes.forEach(node => node.draw());
""".strip(),
        encoding="utf-8",
    )
    task = FakeTask(tmp_path)
    task.description = (
        "Build a static web app called Capability Garden Planner with a full-screen canvas animation "
        "container and a delivery report panel showing generated quality score, final quality score, "
        "required gate failures, manual checks, skipped checks, and final verdict."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    assert "javascript runtime risk: canvas nodes initialized before dimensions" in report["probe_results"][0]["output_tail"].lower()


def test_static_web_smoke_fails_when_delivery_report_runtime_alias_drops_metric(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Capability Garden Planner</h1>
    <section class="delivery-report">
      <h2>Delivery Report</h2>
      <div class="report-metrics">
        <div>Generated Quality Score</div><div>Final Quality Score</div><div>Required Gate Failures</div>
        <div>Manual Checks</div><div>Skipped Checks</div><div>Final Verdict</div>
      </div>
    </section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        """
function updateDeliveryReport() {
  const reportPanel = document.querySelector('.delivery-report');
  let metricsContainer = reportPanel.querySelector('.report-metrics');
  metricsContainer.innerHTML = `
    <div>Generated Quality Score</div>
    <div>Final Quality Score</div>
    <div>Gate Failures</div>
    <div>Manual Checks</div>
    <div>Skipped Checks</div>
    <div>Final Verdict</div>`;
}
updateDeliveryReport();
""".strip(),
        encoding="utf-8",
    )
    task = FakeTask(tmp_path)
    task.description = (
        "Build a polished static web app called Capability Garden Planner. "
        "A delivery report panel shows generated quality score, final quality score, "
        "required gate failures, manual checks, skipped checks, and final verdict."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    assert "delivery report runtime metric: required gate failures" in report["probe_results"][0]["output_tail"].lower()


def test_static_web_smoke_allows_route_table_header_with_dynamic_skill_rows(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><title>Capability Garden Planner</title><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Capability Garden Planner</h1>
    <section class="route-evidence-panel" aria-labelledby="route-heading">
      <h2 id="route-heading">Route Evidence</h2>
      <table class="evidence-table">
        <thead>
          <tr><th>Selected Agent</th><th>Matched</th><th>MCP Risk</th><th>Reason</th></tr>
        </thead>
        <tbody id="evidence-tbody">
          <tr><td>OpenClaw</td><td>Backend API</td><td>LOW</td><td>Primary implementor</td></tr>
        </tbody>
      </table>
      <button id="recompute-route">Recompute Route</button>
    </section>
    <section><h2>Delivery Report</h2><p>Generated Quality Score Final Quality Score Required Gate Failures Manual Checks Skipped Checks Final Verdict</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        """
const rows = [
  { agent: 'OpenClaw', skill: 'Backend API implementation', risk: 'LOW', reason: 'Primary implementor' },
  { agent: 'Hermes', skill: 'Schema design', risk: 'LOW', reason: 'Data model specialist' },
];
function renderRows(items) {
  const tbody = document.getElementById('evidence-tbody');
  tbody.innerHTML = '';
  items.forEach(row => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + row.agent + '</td>' +
      '<td>' + row.skill + '</td>' +
      '<td>' + row.risk + '</td>' +
      '<td>' + row.reason + '</td>';
    tbody.appendChild(tr);
  });
}
document.getElementById('recompute-route').addEventListener('click', () => renderRows(rows.reverse()));
""".strip(),
        encoding="utf-8",
    )
    task = FakeTask(tmp_path)
    task.description = (
        "Build a static web app called Capability Garden Planner with a route evidence panel. "
        "Clicking Recompute Route must update visible rows containing selected agent, matched skill/native skill, "
        "MCP risk, and reason text. A delivery report panel shows generated quality score, final quality score, "
        "required gate failures, manual checks, skipped checks, and final verdict."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "passed"


def test_static_web_smoke_requires_native_skill_display_and_recompute_inside_route_panel(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Capability Garden Planner</h1>
    <section><h2>Local Agents</h2>
      <div class="agent-card"><h3>OpenClaw</h3><input data-skill="a"><input data-skill="b"><input data-skill="native"><p>apple-notes unavailable. Repair by refreshing permissions.</p></div>
      <div class="agent-card"><h3>Hermes</h3><input data-skill="a"><input data-skill="b"><input data-skill="native"></div>
      <div class="agent-card"><h3>Claude Code</h3><input data-skill="a"><input data-skill="b"><input data-skill="native"></div>
    </section>
    <section><h2>Cloud LLMs</h2>
      <div class="llm-card"><h3>DeepSeek</h3><input data-skill="a"><input data-skill="b"><input data-skill="c"></div>
      <div class="llm-card"><h3>MiniMax</h3><input data-skill="a"><input data-skill="b"><input data-skill="c"></div>
    </section>
    <section aria-label="Task Composer"><h2>Task Composer</h2><button>Recompute Route</button><ul class="checklist"><li><input type="checkbox" checked><label>All tests pass</label></li></ul></section>
    <section aria-label="Route Preview"><h2>Route Preview</h2><ul class="evidence-list"><li>selected agent, matched native skill, MCP risk, reason</li></ul></section>
    <section aria-label="Delivery Report"><h2>Delivery Report</h2><p>Generated Quality Score Final Quality Score Required Gate Failures Manual Checks Skipped Checks Final Verdict</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "document.querySelector('.evidence-list').innerHTML = '<li>selected agent, matched native skill, MCP risk, reason</li>'; "
        "localStorage.setItem('state', JSON.stringify({priority: 'normal', strict: true, skill: 'native'}));\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Capability Garden Planner\n\nOpen index.html directly.\n", encoding="utf-8")
    task = FakeTask(tmp_path)
    task.description = (
        "Build a polished static web app called Capability Garden Planner. "
        "A route evidence panel has a Recompute Route button. Clicking it must update visible rows "
        "containing selected agent, matched skill/native skill, MCP risk, and reason text. "
        "Agent capability cards show repair advice for an unavailable Apple Notes native skill. "
        "A delivery report panel shows generated quality score, final quality score, "
        "required gate failures, manual checks, skipped checks, and final verdict."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    output = report["probe_results"][0]["output_tail"].lower()
    assert "native skill display text missing requested item: apple notes" in output
    assert "repair advice display text for apple notes" in output
    assert "route evidence section heading" in output
    assert "route evidence recompute button inside panel" in output


def test_static_web_smoke_fails_when_route_button_is_not_scoped_and_reason_not_rendered(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Capability Garden Planner</h1>
    <section class="task-contract"><h2>Task Contract Builder</h2><button class="recompute-btn">Recompute Route</button></section>
    <section class="route-evidence"><h2>Route Evidence</h2><button class="recompute-btn">Recompute Route</button><div class="evidence-list"></div></section>
    <section class="delivery-report"><h2>Delivery Report</h2><p>Generated Quality Score Final Quality Score Required Gate Failures Manual Checks Skipped Checks Final Verdict</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        """
const recomputeBtn = document.querySelector('.recompute-btn');
const evidenceList = document.querySelector('.evidence-list');
function generateEvidence() {
  const reason = 'Skill match confirmed';
  const row = document.createElement('div');
  const text = document.createElement('span');
  text.textContent = `OpenClaw -> Backend API: selected, matched: Backend API, MCP Risk: Low, Native skill match`;
  row.appendChild(text);
  evidenceList.appendChild(row);
}
recomputeBtn.addEventListener('click', generateEvidence);
generateEvidence();
localStorage.setItem('state', JSON.stringify({priority: 'normal', strict: true, skill: 'native'}));
""".strip(),
        encoding="utf-8",
    )
    task = FakeTask(tmp_path)
    task.description = (
        "Build a polished static web app called Capability Garden Planner. "
        "A route evidence panel has a Recompute Route button. Clicking it must update visible rows "
        "containing selected agent, matched skill/native skill, MCP risk, and reason text. "
        "A delivery report panel shows generated quality score, final quality score, "
        "required gate failures, manual checks, skipped checks, and final verdict."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    output = report["probe_results"][0]["output_tail"].lower()
    assert "route evidence recompute button may bind first matching control" in output
    assert "route evidence runtime row missing: reason" in output


def test_static_web_smoke_requires_selected_agent_label_in_route_evidence(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Capability Garden Planner</h1>
    <section class="route-evidence" aria-label="Route Evidence">
      <h2>Route Evidence</h2>
      <button id="recompute-route">Recompute Route</button>
      <table>
        <thead><tr><th>Agent</th><th>Matched Skill</th><th>MCP Risk</th><th>Reason</th></tr></thead>
        <tbody><tr><td>Hermes</td><td>Task Routing</td><td>Low MCP risk</td><td>Reason text</td></tr></tbody>
      </table>
    </section>
    <section><h2>Delivery Report</h2><p>Generated Quality Score Final Quality Score Required Gate Failures Manual Checks Skipped Checks Final Verdict</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        """
document.getElementById('recompute-route').addEventListener('click', () => {
  document.querySelector('.route-evidence tbody').innerHTML =
    '<tr><td>Claude Code</td><td>Matched native skill</td><td>Medium MCP risk</td><td>Reason text</td></tr>';
});
localStorage.setItem('state', JSON.stringify({priority: 'normal', strict: true, skill: 'native'}));
""".strip(),
        encoding="utf-8",
    )
    task = FakeTask(tmp_path)
    task.description = (
        "Build a static web app called Capability Garden Planner with a route evidence panel. "
        "Clicking Recompute Route must update visible rows containing selected agent, matched skill/native skill, MCP risk, and reason text. "
        "A delivery report panel shows generated quality score, final quality score, required gate failures, manual checks, skipped checks, and final verdict."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    assert "route evidence label: selected agent" in report["probe_results"][0]["output_tail"].lower()


def test_native_skill_display_parser_ignores_leading_request_verb():
    entities = contract_acceptance._requested_native_skill_display_entities(
        "Include Apple Notes native skill repair advice in the task surface."
    )

    assert entities == ["Apple Notes"]


def test_static_web_smoke_allows_static_labels_with_textcontent_value_updates(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Capability Garden Planner</h1>
    <section id="route-evidence-panel" aria-label="Route Evidence">
      <h2>Route Evidence</h2>
      <button id="recompute-route-btn">Recompute Route</button>
      <div id="route-evidence-body">
        <div class="route-row"><span>Selected Agent</span><span class="route-row-value route-selected-agent">Hermes</span></div>
        <div class="route-row"><span>Matched Skill</span><span class="route-row-value route-matched">Task Routing</span></div>
        <div class="route-row"><span>Native Skill</span><span class="route-row-value route-native-skill">Apple Notes</span></div>
        <div class="route-row"><span>MCP Risk</span><span class="route-row-value route-mcp-risk">Low</span></div>
        <div class="route-row"><span>Reason</span><span class="route-row-value route-reason">Matched native skill and low MCP risk.</span></div>
      </div>
    </section>
    <section id="delivery-report">
      <h2>Delivery Report</h2>
      <div><span>Generated Quality Score</span><span id="metric-generated">80</span></div>
      <div><span>Final Quality Score</span><span id="metric-final">78</span></div>
      <div><span>Required Gate Failures</span><span id="metric-failures">0</span></div>
      <div><span>Manual Checks</span><span id="metric-manual">1</span></div>
      <div><span>Skipped Checks</span><span id="metric-skipped">0</span></div>
      <div><span>Final Verdict</span><span id="verdict-value">PASS</span></div>
    </section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        """
function buildRouteEvidence() {
  const values = {
    'route-selected-agent': 'Claude Code',
    'route-matched': 'E2E Validation',
    'route-native-skill': 'Apple Notes',
    'route-mcp-risk': 'Medium',
    'route-reason': 'Reason text: selected agent has the matched native skill and acceptable MCP risk.'
  };
  document.querySelectorAll('#route-evidence-body .route-row-value').forEach(el => {
    const key = Array.from(el.classList).find(cls => values[cls]);
    if (key) el.textContent = values[key];
  });
}
function buildDeliveryReport() {
  const body = document.getElementById('delivery-report');
  if (!body) return;
  document.getElementById('metric-generated').textContent = '88';
  document.getElementById('metric-final').textContent = '84';
  document.getElementById('metric-failures').textContent = '0';
  document.getElementById('metric-manual').textContent = '1';
  document.getElementById('metric-skipped').textContent = '0';
  document.getElementById('verdict-value').textContent = 'PASS';
}
document.getElementById('recompute-route-btn').addEventListener('click', () => {
  buildRouteEvidence();
  buildDeliveryReport();
});
localStorage.setItem('state', JSON.stringify({priority: 'normal', strict: true, skill: 'native'}));
""".strip(),
        encoding="utf-8",
    )
    task = FakeTask(tmp_path)
    task.description = (
        "Build a static web app called Capability Garden Planner with a route evidence panel. "
        "Clicking Recompute Route must update visible rows containing selected agent, matched skill/native skill, MCP risk, and reason text. "
        "A delivery report panel shows generated quality score, final quality score, required gate failures, manual checks, skipped checks, and final verdict."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "passed"


def test_static_web_smoke_fails_when_mode_tabs_only_update_state_not_ui(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Capability Garden Planner</h1>
    <section><h2>Task Contract Builder</h2>
      <div class="contract-tabs" role="tablist">
        <button class="contract-tab is-active" role="tab" id="tab-functional" aria-selected="true">Functional</button>
        <button class="contract-tab" role="tab" id="tab-artifact" aria-selected="false">Artifact</button>
      </div>
      <div class="quality-gate-checklist"><label><input type="checkbox" checked>Tests pass</label></div>
    </section>
    <section><h2>Delivery Report</h2><p>Generated Quality Score Final Quality Score Required Gate Failures Manual Checks Skipped Checks Final Verdict</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        """
let contractState = { mode: 'functional' };
function saveContractState() {
  localStorage.setItem('state', JSON.stringify({priority: 'normal', strict: true, skill: 'native', mode: contractState.mode}));
}
function updateDeliveryReport() {}
document.querySelectorAll('.contract-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    const mode = tab.id === 'tab-functional' ? 'functional' : 'artifact';
    contractState.mode = mode;
    saveContractState();
    updateDeliveryReport();
  });
});
""".strip(),
        encoding="utf-8",
    )
    task = FakeTask(tmp_path)
    task.description = (
        "Build a static web app called Capability Garden Planner with Functional and Artifact modes, "
        "a quality gate checklist, keyboard-accessible controls, localStorage persistence, and a delivery report panel showing "
        "generated quality score, final quality score, required gate failures, manual checks, skipped checks, and final verdict."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    assert "functional/artifact mode tab does not update active state" in report["probe_results"][0]["output_tail"].lower()


def test_static_web_smoke_fails_when_checklist_label_click_suppresses_toggle(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Capability Garden Planner</h1>
    <section><h2>Task Contract Builder</h2>
      <div id="gate-checklist" class="quality-gate-checklist">
        <input type="checkbox" id="check-1" checked>
        <label for="check-1">Collect route evidence</label>
      </div>
    </section>
    <section><h2>Route Evidence</h2><button id="recompute-route">Recompute Route</button><p id="route-output">selected agent, matched skill, MCP risk, reason</p></section>
    <section><h2>Delivery Report</h2><p>Generated Quality Score Final Quality Score Required Gate Failures Manual Checks Skipped Checks Final Verdict</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        """
const checklist = document.getElementById('gate-checklist');
checklist.addEventListener('click', function (e) {
  if (e.target.tagName !== 'LABEL') return;
  e.preventDefault();
  e.stopPropagation();
  const cb = document.getElementById(e.target.getAttribute('for'));
  cb.dispatchEvent(new Event('change', { bubbles: true }));
});
document.getElementById('recompute-route').addEventListener('click', () => {
  document.getElementById('route-output').textContent = 'selected agent, matched native skill, MCP risk, reason';
});
localStorage.setItem('state', JSON.stringify({priority: 'normal', strict: true, skill: 'native'}));
""".strip(),
        encoding="utf-8",
    )
    task = FakeTask(tmp_path)
    task.description = (
        "Build a static web app called Capability Garden Planner with a quality gate checklist, "
        "keyboard-accessible controls, localStorage persistence, a route evidence panel, and a delivery report panel."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    assert "checklist label click suppresses checkbox toggle" in report["probe_results"][0]["output_tail"].lower()


def test_static_web_smoke_fails_when_checklist_change_handler_uses_scoped_variable(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Capability Garden Planner</h1>
    <section><h2>Quality Gate Checklist</h2><ul class="checklist"><li><input type="checkbox" id="gate-one"><label for="gate-one">All tests pass</label></li></ul></section>
    <section><h2>Delivery Report</h2><p>Generated Quality Score Final Quality Score Required Gate Failures Manual Checks Skipped Checks Final Verdict</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        """
const checklistEl = document.querySelector('.checklist');
const checkbox = checklistEl.querySelector('input[type="checkbox"]');
checkbox.addEventListener('keydown', e => {
  const allCheckboxes = Array.from(checklistEl.querySelectorAll('input[type="checkbox"]'));
  allCheckboxes[0].focus();
});
checkbox.addEventListener('change', () => {
  const state = {};
  allCheckboxes.forEach(cb => { state[cb.id] = cb.checked; });
  localStorage.setItem('gates', JSON.stringify(state));
});
localStorage.setItem('state', JSON.stringify({priority: 'normal', strict: true, skill: 'native'}));
""".strip(),
        encoding="utf-8",
    )
    task = FakeTask(tmp_path)
    task.description = (
        "Build a static web app called Capability Garden Planner with a quality gate checklist, "
        "keyboard-accessible controls, localStorage persistence, and a delivery report panel showing "
        "generated quality score, final quality score, required gate failures, manual checks, skipped checks, and final verdict."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    assert "change handler uses allCheckboxes outside its scope" in report["probe_results"][0]["output_tail"]


def test_static_web_smoke_fails_when_insertbefore_uses_detached_parent(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <h1>Capability Garden Planner</h1>
    <section><h2>Agent Capabilities</h2><div id="cards"></div></section>
    <section><h2>Delivery Report</h2><p>Generated Quality Score Final Quality Score Required Gate Failures Manual Checks Skipped Checks Final Verdict</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        """
function nativeSkillRow() {
  const row = document.createElement('div');
  const repair = document.createElement('div');
  repair.textContent = 'Repair Required: Apple Notes';
  row.parentNode.insertBefore(repair, row.nextSibling);
  return row;
}
document.getElementById('cards').appendChild(nativeSkillRow());
""".strip(),
        encoding="utf-8",
    )
    task = FakeTask(tmp_path)
    task.description = (
        "Build a static web app called Capability Garden Planner with agent capability cards "
        "and a delivery report panel showing generated quality score, final quality score, "
        "required gate failures, manual checks, skipped checks, and final verdict."
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [{"id": "cap-web-ui", "description": "Static dashboard loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [
            {"id": "probe-static-web-smoke", "probe_type": "static_web_smoke", "required": True}
        ],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    assert "insertbefore uses parentnode before element is attached" in report["probe_results"][0]["output_tail"].lower()


def test_static_web_smoke_allows_negative_package_manager_mentions(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <section><h2>Local Agents</h2>
      <article class="agent-card"><h3>OpenClaw</h3><button data-skill="planning">Planning</button><button data-skill="coding">Coding</button><button data-skill="native">Native Skill</button></article>
      <article class="agent-card"><h3>Hermes</h3><button data-skill="planning">Planning</button><button data-skill="coding">Coding</button><button data-skill="native">Native Skill</button></article>
      <article class="agent-card"><h3>Claude Code</h3><button data-skill="planning">Planning</button><button data-skill="coding">Coding</button><button data-skill="native">Native Skill</button></article>
    </section>
    <section><h2>Cloud LLMs</h2>
      <article class="llm-card"><h3>DeepSeek</h3><button data-skill="reasoning">Reasoning</button><button data-skill="review">Review</button><button data-skill="native">Native Skill</button></article>
      <article class="llm-card"><h3>MiniMax</h3><button data-skill="voice">Voice</button><button data-skill="review">Review</button><button data-skill="native">Native Skill</button></article>
    </section>
    <section><h2>Skill Matrix</h2><p>Native skill chips and toggles coordinate work.</p></section>
    <section><h2>Task Composer</h2><textarea></textarea><select id="priority"><option>High</option></select><label><input type="checkbox"> Strict mode toggle</label><button>Recompute route</button></section>
    <section><h2>Route Preview</h2><p>Owner agent: OpenClaw</p><p>Worker steps: plan, build, verify</p><p>Quality gates: static smoke and browser review</p><p>Risk note: low risk.</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text(":focus-visible { outline: 2px solid blue; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "localStorage.setItem('routePlannerState', JSON.stringify({priority: 'high', strict: true, skill: 'native'}));\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "# Native Skill Route Planner\n\nOpen `index.html` directly. There is no package manager and no `node_modules` folder.\n",
        encoding="utf-8",
    )
    task = FakeTask(tmp_path)
    task.description = (
        "Local Agents must include OpenClaw, Hermes, and Claude Code. "
        "Cloud LLMs must include DeepSeek and MiniMax. "
        "The Task Composer must include a textarea, priority selector, strict-mode toggle, "
        "and a button that recomputes a recommended route without reloading. "
        "Route Preview must show the chosen owner agent, at least two worker steps, "
        "quality gates, and a concise risk note. "
        "Persist the latest composer text, priority, strict mode, and selected skill toggles in localStorage. "
        "Each agent row must show at least three configurable skill chips or toggles. "
        "No package managers. No placeholder Lorem Ipsum."
    )
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "file", "required": True},
            {"path_hint": "styles.css", "artifact_type": "file", "required": True},
            {"path_hint": "app.js", "artifact_type": "file", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "capabilities": [{"id": "cap-web-ui", "description": "Static console loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "passed"
    assert report["probe_results"][0]["passed"] is True


def test_static_web_smoke_counts_void_input_skill_controls_inside_cards(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <section><h2>Local Agents</h2>
      <article class="agent-card"><h3>OpenClaw</h3><label><input type="checkbox" data-skill="plan">Plan</label><label><input type="checkbox" data-skill="code">Code</label><label><input type="checkbox" data-skill="native">Native Skill</label></article>
      <article class="agent-card"><h3>Hermes</h3><label><input type="checkbox" data-skill="plan">Plan</label><label><input type="checkbox" data-skill="code">Code</label><label><input type="checkbox" data-skill="native">Native Skill</label></article>
      <article class="agent-card"><h3>Claude Code</h3><label><input type="checkbox" data-skill="plan">Plan</label><label><input type="checkbox" data-skill="code">Code</label><label><input type="checkbox" data-skill="native">Native Skill</label></article>
    </section>
    <section><h2>Cloud LLMs</h2>
      <article class="llm-card"><h3>DeepSeek</h3><label><input type="checkbox" data-skill="reason">Reason</label><label><input type="checkbox" data-skill="review">Review</label><label><input type="checkbox" data-skill="native">Native Skill</label></article>
      <article class="llm-card"><h3>MiniMax</h3><label><input type="checkbox" data-skill="voice">Voice</label><label><input type="checkbox" data-skill="review">Review</label><label><input type="checkbox" data-skill="native">Native Skill</label></article>
    </section>
    <section><h2>Skill Matrix</h2><p>Native skill toggles coordinate work.</p></section>
    <section><h2>Task Composer</h2><textarea></textarea><select><option>High</option></select><label><input type="checkbox"> Strict Mode</label><button>Recompute Route</button></section>
    <section><h2>Route Preview</h2><p>Owner Agent: OpenClaw</p><p>Worker steps: plan, build, verify</p><p>Quality gates: browser and static smoke</p><p>Risk note: low risk.</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text(":focus-visible { outline: 2px solid blue; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "localStorage.setItem('routePlannerState', JSON.stringify({priority: 'high', strict: true, skill: 'native'}));\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Native Skill Route Planner\n\nOpen index.html directly.\n", encoding="utf-8")
    task = FakeTask(tmp_path)
    task.description = (
        "Local Agents must include OpenClaw, Hermes, and Claude Code. "
        "Cloud LLMs must include DeepSeek and MiniMax. "
        "Each agent row must show at least three configurable skill chips or toggles. "
        "At least one skill must be native/local-agent specific. "
        "The Task Composer must include a textarea, priority selector, strict-mode toggle, and a button. "
        "Route Preview must show the chosen owner agent, at least two worker steps, quality gates, and a concise risk note. "
        "Persist the latest composer text, priority, strict mode, and selected skill toggles in localStorage."
    )
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "file", "required": True},
            {"path_hint": "styles.css", "artifact_type": "file", "required": True},
            {"path_hint": "app.js", "artifact_type": "file", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "capabilities": [{"id": "cap-web-ui", "description": "Static console loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "passed"
    assert report["probe_results"][0]["passed"] is True


def test_static_web_smoke_requires_named_sections_and_real_strict_toggle(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <section><h2>Tasks & Notes</h2><textarea hidden></textarea></section>
    <section><h2>Local Agents</h2>
      <article class="agent-card"><h3>OpenClaw</h3><input type="checkbox" data-skill="a"><input type="checkbox" data-skill="b"><input type="checkbox" data-skill="native"></article>
      <article class="agent-card"><h3>Hermes</h3><input type="checkbox" data-skill="a"><input type="checkbox" data-skill="b"><input type="checkbox" data-skill="native"></article>
      <article class="agent-card"><h3>Claude Code</h3><input type="checkbox" data-skill="a"><input type="checkbox" data-skill="b"><input type="checkbox" data-skill="native"></article>
    </section>
    <section><h2>Cloud LLMs</h2>
      <article class="llm-card"><h3>DeepSeek</h3><input type="checkbox" data-skill="a"><input type="checkbox" data-skill="b"><input type="checkbox" data-skill="native"></article>
      <article class="llm-card"><h3>MiniMax</h3><input type="checkbox" data-skill="a"><input type="checkbox" data-skill="b"><input type="checkbox" data-skill="native"></article>
    </section>
    <section><h2>Owner Agent Route Preview</h2><p>Worker steps and quality gates with risk note.</p><button>Recompute Route</button></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "'use strict'; localStorage.setItem('settings', JSON.stringify({priority: 'high', strict: true, skill: true}));\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Native Skill Route Planner\n\nOpen index.html directly.\n", encoding="utf-8")
    task = FakeTask(tmp_path)
    task.description = (
        "The first viewport must show sections for Local Agents, Cloud LLMs, Skill Matrix, Task Composer, and Route Preview. "
        "Local Agents must include OpenClaw, Hermes, and Claude Code. Cloud LLMs must include DeepSeek and MiniMax. "
        "Each agent row must show at least three configurable skill chips or toggles. "
        "The Task Composer must include a textarea, priority selector, strict-mode toggle, and a button that recomputes a recommended route. "
        "Route Preview must show the chosen owner agent, at least two worker steps, quality gates, and a concise risk note. "
        "Persist the latest composer text, priority, strict mode, and selected skill toggles in localStorage."
    )
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "file", "required": True},
            {"path_hint": "styles.css", "artifact_type": "file", "required": True},
            {"path_hint": "app.js", "artifact_type": "file", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "capabilities": [{"id": "cap-web-ui", "description": "Static console loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    output = report["probe_results"][0]["output_tail"]
    assert "Skill Matrix section" in output
    assert "Task Composer section" in output
    assert "strict-mode toggle" in output


def test_static_web_smoke_flags_unfixed_multicolumn_mobile_layout(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head>
    <link rel="stylesheet" href="styles.css">
    <style>
      .console { display: grid; grid-template-columns: 1fr 1fr; }
      .agents-grid { display: grid; grid-template-columns: repeat(3, 1fr); }
      .skill-matrix { display: grid; grid-template-columns: auto repeat(3, 1fr); }
    </style>
  </head>
  <body>
    <main class="console">
      <section><h2>Local Agents</h2>
        <article class="agent-card"><h3>OpenClaw</h3><input type="checkbox" data-skill="a"><input type="checkbox" data-skill="b"><input type="checkbox" data-skill="native"></article>
        <article class="agent-card"><h3>Hermes</h3><input type="checkbox" data-skill="a"><input type="checkbox" data-skill="b"><input type="checkbox" data-skill="native"></article>
        <article class="agent-card"><h3>Claude Code</h3><input type="checkbox" data-skill="a"><input type="checkbox" data-skill="b"><input type="checkbox" data-skill="native"></article>
      </section>
      <section><h2>Cloud LLMs</h2>
        <article class="llm-card"><h3>DeepSeek</h3><input type="checkbox" data-skill="a"><input type="checkbox" data-skill="b"><input type="checkbox" data-skill="native"></article>
        <article class="llm-card"><h3>MiniMax</h3><input type="checkbox" data-skill="a"><input type="checkbox" data-skill="b"><input type="checkbox" data-skill="native"></article>
      </section>
      <section class="skill-matrix"><h2>Skill Matrix</h2></section>
      <section><h2>Task Composer</h2><textarea></textarea><select><option>High</option></select><label><input type="checkbox"> Strict Mode</label><button>Recompute Route</button></section>
      <section><h2>Route Preview</h2><p>Owner Agent OpenClaw</p><p>Worker steps include Hermes and Claude Code.</p><p>Quality gates and risk note.</p></section>
    </main>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text(
        "@media (max-width: 480px) { .unused-grid { grid-template-columns: 1fr; } }\n",
        encoding="utf-8",
    )
    (tmp_path / "app.js").write_text(
        "localStorage.setItem('routePlannerState', JSON.stringify({priority: 'high', strict: true, skill: 'native'}));\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Native Skill Route Planner\n\nOpen index.html directly.\n", encoding="utf-8")
    task = FakeTask(tmp_path)
    task.description = (
        "The first viewport must show sections for Local Agents, Cloud LLMs, Skill Matrix, Task Composer, and Route Preview. "
        "Local Agents must include OpenClaw, Hermes, and Claude Code. Cloud LLMs must include DeepSeek and MiniMax. "
        "Each agent row must show at least three configurable skill chips or toggles. "
        "The Task Composer must include a textarea, priority selector, strict-mode toggle, and a button that recomputes a recommended route. "
        "Route Preview must show the chosen owner agent, at least two worker steps, quality gates, and a concise risk note. "
        "Persist the latest composer text, priority, strict mode, and selected skill toggles in localStorage. "
        "Include a responsive layout for narrow screens."
    )
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "file", "required": True},
            {"path_hint": "styles.css", "artifact_type": "file", "required": True},
            {"path_hint": "app.js", "artifact_type": "file", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "capabilities": [{"id": "cap-web-ui", "description": "Static console loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    output = report["probe_results"][0]["output_tail"]
    assert "responsive mobile rule for .console" in output
    assert "responsive mobile rule for .agents-grid" in output
    assert "responsive mobile rule for .skill-matrix" in output


def test_static_web_smoke_fails_when_agent_rows_lack_skill_controls(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <section>
      <h2>Local Agents</h2>
      <div class="agent-card"><span>OpenClaw</span><input type="checkbox"></div>
      <div class="agent-card"><span>Hermes</span><input type="checkbox"></div>
      <div class="agent-card"><span>Claude Code</span><input type="checkbox"></div>
    </section>
    <section>
      <h2>Cloud LLMs</h2>
      <div class="llm-card"><span>DeepSeek</span><input type="checkbox"></div>
      <div class="llm-card"><span>MiniMax</span><input type="checkbox"></div>
    </section>
    <section><h2>Skill Matrix</h2><button class="skill-chip">Native Skill</button></section>
    <section><h2>Task Composer</h2><textarea></textarea><select id="priority"><option>High</option></select><label><input type="checkbox"> Strict Mode</label><button>Recompute Route</button></section>
    <section><h2>Route Preview</h2><p>Owner Agent</p><p>Worker step</p><p>Quality Gate</p><p>Risk note</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text(
        "localStorage.setItem('x', JSON.stringify({priority: 'high', strict: true, skill: true}));\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Native Skill Route Planner\n\nOpen index.html in a browser.\n", encoding="utf-8")
    task = FakeTask(tmp_path)
    task.description = (
        "Local Agents must include OpenClaw, Hermes, and Claude Code. "
        "Cloud LLMs must include DeepSeek and MiniMax. "
        "Each agent row must show at least three configurable skill chips or toggles. "
        "At least one skill must be native/local-agent specific. "
        "The Task Composer must include a textarea, priority selector, strict-mode toggle, "
        "and a button that recomputes a recommended route without reloading. "
        "Route Preview must show the chosen owner agent, at least two worker steps, quality gates, and a concise risk note. "
        "Persist the latest composer text, priority, strict mode, and selected skill toggles in localStorage."
    )
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "file", "required": True},
            {"path_hint": "styles.css", "artifact_type": "file", "required": True},
            {"path_hint": "app.js", "artifact_type": "file", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "capabilities": [{"id": "cap-web-ui", "description": "Static console loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    output = report["probe_results"][0]["output_tail"]
    assert "agent row OpenClaw has fewer than three skill controls" in output
    assert "agent row DeepSeek has fewer than three skill controls" in output
    assert "agent row MiniMax has fewer than three skill controls" in output


def test_static_web_smoke_does_not_treat_aggregate_as_quality_gate(tmp_path):
    (tmp_path / "index.html").write_text(
        """
<!doctype html>
<html>
  <head><link rel="stylesheet" href="styles.css"></head>
  <body>
    <section><h2>Route Preview</h2><p>Aggregate results and report quality.</p><p>Risk note</p></section>
    <script src="app.js"></script>
  </body>
</html>
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body { font-family: system-ui; }\n", encoding="utf-8")
    (tmp_path / "app.js").write_text("document.body.dataset.ready = 'true';\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Route Preview\n\nOpen index.html in a browser.\n", encoding="utf-8")
    task = FakeTask(tmp_path)
    task.description = "Route Preview must show quality gates and a concise risk note."
    contract = {
        "task_types": ["functional", "artifact"],
        "delivery_mode": "composite",
        "deliverables": [
            {"path_hint": "index.html", "artifact_type": "file", "required": True},
            {"path_hint": "styles.css", "artifact_type": "file", "required": True},
            {"path_hint": "app.js", "artifact_type": "file", "required": True},
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
        ],
        "capabilities": [{"id": "cap-web-ui", "description": "Static console loads", "required": True}],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    assert "quality gates" in report["probe_results"][0]["output_tail"]


def test_notes_cli_smoke_probe_fails_when_user_commands_are_missing(tmp_path):
    (tmp_path / "notes_cli.py").write_text(
        """
import argparse
parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest='command', required=True)
add = sub.add_parser('add')
add.add_argument('title')
add.add_argument('--tag', action='append', default=[])
sub.add_parser('list')
search = sub.add_parser('search')
search.add_argument('query')
export = sub.add_parser('export')
args = parser.parse_args()
if args.command == 'add':
    print('Created note: 1')
elif args.command == 'list':
    print('1: Buy milk [home]')
elif args.command == 'search':
    print('1: Buy milk [home]')
else:
    print('# Notes Export')
""",
        encoding="utf-8",
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [{"path_hint": "notes_cli.py", "artifact_type": "api_service_source", "required": True}],
        "capabilities": [{"id": "cap-cli", "description": "CLI behavior", "required": True, "minimum_evidence": "L2"}],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-notes-cli-smoke", "probe_type": "notes_cli_smoke", "required": True}],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "failed"
    assert report["probe_results"][0]["passed"] is False
    assert "search --tag home" in report["probe_results"][0]["output_tail"]


def test_notes_cli_smoke_accepts_uuid_ids_and_cleans_runtime_files(tmp_path):
    (tmp_path / "notes_cli.py").write_text(
        """
import json
import pathlib
import sys

STORE = pathlib.Path('todo.json')

def load():
    return json.loads(STORE.read_text()) if STORE.exists() else []

def save(items):
    STORE.write_text(json.dumps(items))

cmd = sys.argv[1]
if cmd == 'add':
    note = {'id': 'e282609ad547476aad3393116b971b00', 'title': sys.argv[2], 'tags': ['home'], 'done': False}
    save([note])
    print('Note created: e282609ad547476aad3393116b971b00')
elif cmd == 'list':
    for note in load():
        if '--done' not in sys.argv or note['done']:
            print(f"{note['id']}: {note['title']} [home]")
elif cmd == 'search':
    print('e282609ad547476aad3393116b971b00: Buy milk [home]')
elif cmd == 'done':
    notes = load()
    notes[0]['done'] = True
    save(notes)
    print('Done')
elif cmd == 'export':
    pathlib.Path(sys.argv[2]).write_text('# Notes\\n- Buy milk')
    print('Exported')
""",
        encoding="utf-8",
    )
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [{"path_hint": "notes_cli.py", "artifact_type": "api_service_source", "required": True}],
        "capabilities": [{"id": "cap-cli", "description": "CLI behavior", "required": True, "minimum_evidence": "L2"}],
        "constraints": [],
        "acceptance_probes": [{"id": "probe-notes-cli-smoke", "probe_type": "notes_cli_smoke", "required": True}],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "passed"
    assert report["probe_results"][0]["passed"] is True
    assert not (tmp_path / "todo.json").exists()
    assert not (tmp_path / "e2e_export.md").exists()


def test_functional_contract_without_evidence_is_partial_not_passed(tmp_path):
    contract = {
        "task_types": ["functional"],
        "delivery_mode": "functional",
        "deliverables": [],
        "capabilities": [],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "partial"
    assert report["evidence_gaps"]
    assert report["evidence_gaps"][0]["check_type"] == "functional_evidence_required"


def test_forbidden_file_constraint_fails_when_actual_file_exists(tmp_path):
    (tmp_path / "README.md").write_text("# Usage\n", encoding="utf-8")
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    contract = {
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "deliverables": [
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True}
        ],
        "capabilities": [],
        "constraints": [
            {
                "id": "constraint-forbidden-init",
                "constraint_type": "forbidden_file",
                "value": "__init__.py",
                "required": True,
            }
        ],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "failed"
    assert report["produced_required"] == ["README.md"]
    assert report["failed_constraints"]
    failure = report["failed_constraints"][0]
    assert failure["id"] == "constraint-forbidden-init"
    assert failure["constraint_type"] == "forbidden_file"
    assert failure["value"] == "__init__.py"
    assert any(path.endswith("/__init__.py") for path in failure["evidence"])
    assert any(path.endswith("/tests/__init__.py") for path in failure["evidence"])


def test_root_forbidden_init_allows_nested_test_package_marker(tmp_path):
    (tmp_path / "README.md").write_text("# Usage\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    contract = {
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "deliverables": [
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True}
        ],
        "capabilities": [],
        "constraints": [
            {
                "id": "constraint-forbidden-root-init",
                "constraint_type": "forbidden_file",
                "value": "__init__.py",
                "scope": "project_root",
                "required": True,
            }
        ],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "passed"
    assert report["failed_constraints"] == []


def test_positive_dockerfile_is_not_failed_without_forbidden_constraint(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    contract = {
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "deliverables": [
            {"path_hint": "Dockerfile", "artifact_type": "dockerfile", "required": True}
        ],
        "capabilities": [],
        "constraints": [],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(FakeTask(tmp_path), contract, [])

    assert report["delivery_quality"] == "passed"
    assert report["failed_constraints"] == []
    assert report["produced_required"] == ["Dockerfile"]


def test_agent_mix_constraint_fails_when_actual_execution_is_single_agent(tmp_path):
    (tmp_path / "README.md").write_text("# Done\n", encoding="utf-8")
    task = FakeTask(tmp_path)
    task.subtasks = [
        SimpleNamespace(subtask_id="task-1-decompose", agent_id="owner", status="completed"),
        SimpleNamespace(subtask_id="st-api", agent_id="deepseek", status="completed"),
        SimpleNamespace(subtask_id="st-readme", agent_id="deepseek", status="completed"),
    ]
    contract = {
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "deliverables": [
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True}
        ],
        "capabilities": [],
        "constraints": [
            {
                "id": "constraint-agent-mix",
                "constraint_type": "agent_mix",
                "value": {
                    "min_distinct_agents": 3,
                    "min_local_agents": 2,
                    "min_cloud_agents": 1,
                },
                "required": True,
            }
        ],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "failed"
    failure = next(item for item in report["failed_constraints"] if item["constraint_type"] == "agent_mix")
    assert failure["evidence"]["actual_agents"] == ["deepseek"]
    assert "expected at least 3 distinct agents" in failure["message"]
    gate_ids = {gate["gate_id"] for gate in report["quality_report"]["gate_results"]}
    assert "gate-agent-mix" in gate_ids


def test_agent_mix_constraint_passes_with_local_cloud_execution_mix(tmp_path):
    (tmp_path / "README.md").write_text("# Done\n", encoding="utf-8")
    task = FakeTask(tmp_path)
    task.subtasks = [
        SimpleNamespace(subtask_id="task-1-decompose", agent_id="owner", status="completed"),
        SimpleNamespace(subtask_id="st-web", agent_id="hermes", status="completed"),
        SimpleNamespace(subtask_id="st-cli", agent_id="openclaw", status="completed"),
        SimpleNamespace(subtask_id="st-api", agent_id="deepseek", status="completed"),
        SimpleNamespace(subtask_id="st-pending", agent_id="minimax", status="pending"),
    ]
    contract = {
        "task_types": ["artifact"],
        "delivery_mode": "artifact",
        "deliverables": [
            {"path_hint": "README.md", "artifact_type": "documentation", "required": True}
        ],
        "capabilities": [],
        "constraints": [
            {
                "id": "constraint-agent-mix",
                "constraint_type": "agent_mix",
                "value": {
                    "min_distinct_agents": 3,
                    "min_local_agents": 2,
                    "min_cloud_agents": 1,
                },
                "required": True,
            }
        ],
        "acceptance_probes": [],
    }

    report = run_delivery_contract_acceptance(task, contract, [])

    assert report["delivery_quality"] == "passed"
    assert report["failed_constraints"] == []
    agent_mix_gate = next(
        gate for gate in report["quality_report"]["gate_results"]
        if gate["gate_id"] == "gate-agent-mix"
    )
    assert agent_mix_gate["status"] == "passed"
    assert agent_mix_gate["evidence"]["satisfied_constraints"][0]["evidence"]["actual_agents"] == [
        "hermes",
        "openclaw",
        "deepseek",
    ]
