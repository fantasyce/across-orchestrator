from across_agents_assistant.task_manager.orchestration.delivery_contract import (
    build_owner_delivery_contract,
    normalize_delivery_task_types,
)
from across_agents_assistant.task_manager.orchestration.requirements import (
    extract_requirement_manifest,
    extract_required_path_hints,
)
from across_agents_assistant.task_manager.state import TaskState


def test_normalize_delivery_task_types_composite():
    assert normalize_delivery_task_types(["functional", "artifact"]) == (["functional", "artifact"], "composite")


def test_contract_includes_probe_adapter_plan_for_static_web(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")

    contract = build_owner_delivery_contract(
        task_id="task-static",
        description="Build a runnable static web page with browser interaction.",
        task_types=["functional"],
        project_dir=str(tmp_path),
        manifest={"deliverables": [{"path_hint": "index.html", "artifact_type": "frontend_source", "required": True}]},
    )

    adapter_ids = {gate["adapter_id"] for gate in contract["probe_adapter_plan"]}
    assert "static_web" in adapter_ids
    assert "browser_e2e" in adapter_ids


def test_contract_unknown_source_project_requires_manual_validation_recipe(tmp_path):
    (tmp_path / "README.md").write_text("# Unknown stack\n", encoding="utf-8")

    contract = build_owner_delivery_contract(
        task_id="task-unknown",
        description="Build a runnable local app, but no technology stack is specified.",
        task_types=["functional"],
        project_dir=str(tmp_path),
        manifest={"deliverables": []},
    )

    unknown_gates = [
        gate for gate in contract["probe_adapter_plan"]
        if gate["adapter_id"] == "unknown_stack"
    ]
    assert unknown_gates
    assert unknown_gates[0]["status"] == "manual_required"


def test_contract_turns_no_docker_into_constraint_not_deliverable():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-readme", "artifact_type": "documentation", "path_hint": "README.md", "required": True}
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="Build a todo app. Do not use Docker or container tooling.",
        task_types=["functional", "artifact"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    assert contract["delivery_mode"] == "composite"
    assert any(c["constraint_type"] == "forbidden_tooling" and c["value"] == "docker" for c in contract["constraints"])
    assert not any(d["artifact_type"] == "dockerfile" for d in contract["deliverables"])


def test_contract_turns_no_dockerfile_into_constraint_not_deliverable():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-readme", "artifact_type": "documentation", "path_hint": "README.md", "required": True}
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="Create README.md only. Do not create Dockerfile, setup.py, or container tooling.",
        task_types=["artifact"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    assert any(c["constraint_type"] == "forbidden_tooling" and c["value"] == "docker" for c in contract["constraints"])
    assert not any(d["artifact_type"] == "dockerfile" for d in contract["deliverables"])


def test_contract_turns_chinese_no_dockerfile_into_constraint_not_deliverable():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-readme", "artifact_type": "documentation", "path_hint": "README.md", "required": True},
            {"requirement_id": "req-docker", "artifact_type": "dockerfile", "path_hint": "Dockerfile", "required": True},
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="实现 FastAPI + SQLite Web 应用，不得创建 Dockerfile/docker-compose。",
        task_types=["functional", "artifact"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    assert any(c["constraint_type"] == "forbidden_tooling" and c["value"] == "docker" for c in contract["constraints"])
    assert not any(d["artifact_type"] == "dockerfile" for d in contract["deliverables"])


def test_contract_records_chinese_allowed_documentation_files():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-readme", "artifact_type": "documentation", "path_hint": "README.md", "required": True},
            {"requirement_id": "req-testing", "artifact_type": "documentation", "path_hint": "TESTING.md", "required": True},
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="文档只需要 README.md 和 TESTING.md，不要生成大量额外文档。",
        task_types=["functional", "artifact"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    assert any(
        c["constraint_type"] == "allowed_documentation_files"
        and c["value"] == ["README.md", "TESTING.md"]
        for c in contract["constraints"]
    )


def test_contract_records_chinese_except_clause_allowed_documentation_files():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-readme", "artifact_type": "documentation", "path_hint": "README.md", "required": True},
            {"requirement_id": "req-testing", "artifact_type": "documentation", "path_hint": "TESTING.md", "required": True},
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="除 README.md 和 TESTING.md 外不要增加额外文档。",
        task_types=["functional", "artifact"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    assert any(
        c["constraint_type"] == "allowed_documentation_files"
        and c["value"] == ["README.md", "TESTING.md"]
        for c in contract["constraints"]
    )


def test_functional_contract_forbids_auth_when_not_requested():
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="Build a personal expense FastAPI SQLite web app with native HTML frontend.",
        task_types=["functional", "artifact"],
        project_dir="/tmp/project",
        manifest={"deliverables": []},
    )

    assert any(
        c["constraint_type"] == "forbidden_unrequested_auth"
        for c in contract["constraints"]
    )


def test_functional_contract_allows_auth_when_explicitly_requested():
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="Build a FastAPI app with user login and JWT authentication.",
        task_types=["functional"],
        project_dir="/tmp/project",
        manifest={"deliverables": []},
    )

    assert not any(
        c["constraint_type"] == "forbidden_unrequested_auth"
        for c in contract["constraints"]
    )


def test_contract_records_forbidden_runner_scripts_when_explicitly_banned():
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description=(
            "Build a FastAPI app. Do not create runner scripts, run_tests scripts, "
            "or setup_test_env scripts."
        ),
        task_types=["functional", "artifact"],
        project_dir="/tmp/project",
        manifest={"deliverables": []},
    )

    forbidden = {
        c["value"]: c
        for c in contract["constraints"]
        if c["constraint_type"] == "forbidden_file"
    }
    assert {"run.py", "runner.py", "run_tests.py", "setup_test_env.py"} <= set(forbidden)
    assert forbidden["run.py"]["scope"] == "project_root"


def test_contract_does_not_forbid_runtime_data_when_only_not_final_deliverable():
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description=(
            "Build a Python todo CLI. Store data in TODO_FILE, defaulting to todo.json. "
            "Do not treat todo.json as a final deliverable; it is runtime data only."
        ),
        task_types=["functional"],
        project_dir="/tmp/project",
        manifest={"deliverables": []},
    )

    assert not any(
        c["constraint_type"] == "forbidden_file" and c["value"] == "todo.json"
        for c in contract["constraints"]
    )


def test_contract_filters_forbidden_manifest_deliverables_and_records_file_constraints():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-readme", "artifact_type": "documentation", "path_hint": "README.md", "required": True},
            {"requirement_id": "req-setup", "artifact_type": "config_file", "path_hint": "setup.py", "required": True},
            {"requirement_id": "req-init", "artifact_type": "api_service_source", "path_hint": "__init__.py", "required": True},
            {"requirement_id": "req-docker", "artifact_type": "dockerfile", "path_hint": "Dockerfile", "required": True},
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description=(
            "Create exactly one required file named README.md. "
            "Do not create Dockerfile, setup.py, package files, __init__.py, or container tooling."
        ),
        task_types=["artifact"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    assert [item["path_hint"] for item in contract["deliverables"]] == ["README.md"]
    forbidden_file_values = {
        c["value"] for c in contract["constraints"]
        if c["constraint_type"] == "forbidden_file"
    }
    assert {"Dockerfile", "setup.py", "__init__.py"} <= forbidden_file_values
    assert any(c["constraint_type"] == "forbidden_tooling" and c["value"] == "docker" for c in contract["constraints"])


def test_contract_records_allowed_files_when_user_requests_exact_file_set():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-readme", "artifact_type": "documentation", "path_hint": "README.md", "required": True}
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="Create exactly one required file named README.md. Do not create any other files.",
        task_types=["artifact"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    allowed = [
        c for c in contract["constraints"]
        if c["constraint_type"] == "allowed_files"
    ]
    assert allowed
    assert allowed[0]["value"] == ["README.md"]


def test_exact_static_web_contract_keeps_index_when_no_build_step_is_forbidden():
    description = (
        "Build a static web app called Release Evaluation Cockpit. "
        "It must open directly from index.html with no build step. "
        "Deliver exactly these files and no others: index.html, styles.css, app.js, README.md."
    )

    assert extract_required_path_hints(description) == [
        "index.html",
        "styles.css",
        "app.js",
        "README.md",
    ]

    manifest = extract_requirement_manifest("task-static", description, "/tmp/project")
    manifest_payload = TaskState()._manifest_to_dict(manifest)
    contract = build_owner_delivery_contract(
        task_id="task-static",
        description=description,
        task_types=["functional", "artifact"],
        project_dir="/tmp/project",
        manifest=manifest_payload,
    )

    assert [item["path_hint"] for item in contract["deliverables"]] == [
        "index.html",
        "styles.css",
        "app.js",
        "README.md",
    ]
    allowed = [
        c for c in contract["constraints"]
        if c["constraint_type"] == "allowed_files"
    ]
    assert allowed
    assert allowed[0]["value"] == ["index.html", "styles.css", "app.js", "README.md"]


def test_release_e2e_contract_ignores_absolute_project_directory_path():
    project_dir = "/private/tmp/across-v040-e2e.run123"
    description = (
        "Build a dependency-free console in this exact project directory:\n"
        f"{project_dir}\n\n"
        "Deliver exactly these files and no others:\n"
        "- README.md\n"
        "- web/index.html\n"
        "- web/styles.css\n"
        "- web/app.js\n"
        "- api/server.mjs\n"
        "- cli/quality-check.mjs\n"
        "- tests/e2e-smoke.mjs\n"
    )

    paths = extract_required_path_hints(description)

    assert "private/tmp/across-v040-e2e.run123" not in paths
    assert set(paths) == {
        "README.md",
        "web/index.html",
        "web/styles.css",
        "web/app.js",
        "api/server.mjs",
        "cli/quality-check.mjs",
        "tests/e2e-smoke.mjs",
    }
    assert len(paths) == 7


def test_release_e2e_contract_ignores_tmp_project_directory_path():
    project_dir = "/tmp/across-remaining-e2e.PYXfyz"
    description = (
        "Build a dependency-free console in this exact project directory:\n"
        f"{project_dir}\n\n"
        "Deliver exactly these files and no others:\n"
        "- README.md\n"
        "- web/index.html\n"
        "- web/styles.css\n"
        "- web/app.js\n"
        "- api/server.mjs\n"
        "- cli/quality-check.mjs\n"
        "- tests/e2e-smoke.mjs\n"
    )

    paths = extract_required_path_hints(description)

    assert "tmp/across-remaining-e2e.PYXfyz" not in paths
    assert set(paths) == {
        "README.md",
        "web/index.html",
        "web/styles.css",
        "web/app.js",
        "api/server.mjs",
        "cli/quality-check.mjs",
        "tests/e2e-smoke.mjs",
    }
    assert len(paths) == 7


def test_functional_contract_records_allowed_files_when_user_requests_exact_file_set():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-index", "artifact_type": "frontend_source", "path_hint": "index.html", "required": True},
            {"requirement_id": "req-css", "artifact_type": "frontend_source", "path_hint": "styles.css", "required": True},
            {"requirement_id": "req-js", "artifact_type": "frontend_source", "path_hint": "app.js", "required": True},
            {"requirement_id": "req-readme", "artifact_type": "documentation", "path_hint": "README.md", "required": True},
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-static",
        description=(
            "Build a static web app. "
            "Create exactly these files: index.html, styles.css, app.js, README.md."
        ),
        task_types=["functional"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    allowed = [
        c for c in contract["constraints"]
        if c["constraint_type"] == "allowed_files"
    ]
    assert allowed
    assert allowed[0]["value"] == ["index.html", "styles.css", "app.js", "README.md"]


def test_contract_does_not_forbid_static_entrypoint_when_no_package_managers():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-index", "artifact_type": "frontend_source", "path_hint": "index.html", "required": True},
            {"requirement_id": "req-css", "artifact_type": "frontend_source", "path_hint": "styles.css", "required": True},
            {"requirement_id": "req-js", "artifact_type": "frontend_source", "path_hint": "app.js", "required": True},
            {"requirement_id": "req-readme", "artifact_type": "documentation", "path_hint": "README.md", "required": True},
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-static",
        description=(
            "It must run by opening index.html directly, with no package managers, "
            "no external CDN, and no generated dependencies.\n\n"
            "Create exactly these files: index.html, styles.css, app.js, README.md."
        ),
        task_types=["functional", "artifact"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    deliverable_paths = {item["path_hint"] for item in contract["deliverables"]}
    forbidden_file_values = {
        c["value"] for c in contract["constraints"]
        if c["constraint_type"] == "forbidden_file"
    }

    assert {"index.html", "styles.css", "app.js", "README.md"} <= deliverable_paths
    assert "index.html" not in forbidden_file_values
    assert not any(group["id"] == "group-install-metadata" for group in contract["deliverable_groups"])


def test_contract_keeps_static_entrypoint_when_no_build_step_and_small_deliverable_list():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-index", "artifact_type": "frontend_source", "path_hint": "index.html", "required": True},
            {"requirement_id": "req-css", "artifact_type": "frontend_source", "path_hint": "styles.css", "required": True},
            {"requirement_id": "req-js", "artifact_type": "frontend_source", "path_hint": "app.js", "required": True},
            {"requirement_id": "req-readme", "artifact_type": "documentation", "path_hint": "README.md", "required": True},
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-static",
        description=(
            "Build a static web app called Delivery Benchmark Command Center. "
            "It must open directly from index.html with no build step. "
            "Keep the deliverable small: index.html, styles.css, app.js, and README.md only."
        ),
        task_types=["functional", "artifact"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    deliverable_paths = {item["path_hint"] for item in contract["deliverables"]}
    forbidden_file_values = {
        c["value"] for c in contract["constraints"]
        if c["constraint_type"] == "forbidden_file"
    }
    allowed_files = [
        c["value"] for c in contract["constraints"]
        if c["constraint_type"] == "allowed_files"
    ]

    assert {"index.html", "styles.css", "app.js", "README.md"} <= deliverable_paths
    assert "index.html" not in forbidden_file_values
    assert allowed_files == [["index.html", "styles.css", "app.js", "README.md"]]


def test_documentation_only_artifact_contract_does_not_require_install_metadata():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-readme", "artifact_type": "documentation", "path_hint": "README.md", "required": True}
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="Create exactly one required file named README.md. Do not create any other files.",
        task_types=["artifact"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    assert "source_project" not in contract["delivery_facets"]
    assert "runnable_app" not in contract["delivery_facets"]
    assert not any(group["id"] == "group-install-metadata" for group in contract["deliverable_groups"])


def test_artifact_only_python_file_contract_does_not_require_functional_groups():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-hello", "artifact_type": "api_service_source", "path_hint": "hello.py", "required": True}
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="Create a simple hello.py that prints 'Hello from E2E test'",
        task_types=["artifact"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    assert contract["delivery_facets"] == []
    assert contract["deliverable_groups"] == []
    assert contract["acceptance_probes"] == []


def test_contract_marks_root_init_constraint_when_user_says_root_init():
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description=(
            "Build a todo CLI with tests/test_todo_cli.py. "
            "Do not create setup.py, package files, or root __init__.py."
        ),
        task_types=["functional", "artifact"],
        project_dir="/tmp/project",
        manifest={"deliverables": []},
    )

    init_constraints = [
        c for c in contract["constraints"]
        if c["constraint_type"] == "forbidden_file" and c["value"] == "__init__.py"
    ]
    assert init_constraints
    assert init_constraints[0]["scope"] == "project_root"


def test_contract_does_not_turn_positive_docker_requirement_into_forbidden_constraint():
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="Build a service. Docker is required for packaging and local deployment.",
        task_types=["artifact"],
        project_dir="/tmp/project",
        manifest={"deliverables": []},
    )

    assert not any(c["constraint_type"] == "forbidden_tooling" and c["value"] == "docker" for c in contract["constraints"])


def test_functional_contract_extracts_core_behavior_capabilities():
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="Build a todo manager. It must support add, list, complete, local JSON persistence, and duplicate ID rejection. Include pytest tests.",
        task_types=["functional"],
        project_dir="/tmp/project",
        manifest={"deliverables": []},
    )

    capability_text = " ".join(item["description"].lower() for item in contract["capabilities"])
    assert "add" in capability_text
    assert "list" in capability_text
    assert "complete" in capability_text
    assert "persists" in capability_text
    assert "duplicate" in capability_text
    assert contract["acceptance_probes"]


def test_python_functional_contract_adds_install_probe():
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="Build a FastAPI app with pytest tests.",
        task_types=["functional"],
        project_dir="/tmp/project",
        manifest={
            "deliverables": [
                {"path_hint": "expense_app/main.py", "artifact_type": "api_service_source", "required": True}
            ]
        },
    )

    probe_types = [probe["probe_type"] for probe in contract["acceptance_probes"]]
    assert "python_install" in probe_types
    assert probe_types.index("python_install") < probe_types.index("pytest")


def test_python_web_app_contract_adds_runtime_smoke_probe_with_html_root():
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="Build a FastAPI SQLite web app with native HTML frontend and pytest tests.",
        task_types=["functional"],
        project_dir="/tmp/project",
        manifest={
            "deliverables": [
                {"path_hint": "expense_app/main.py", "artifact_type": "api_service_source", "required": True},
                {"path_hint": "index.html", "artifact_type": "frontend_source", "required": True},
            ]
        },
    )

    probe_types = [probe["probe_type"] for probe in contract["acceptance_probes"]]
    web_probe = next(probe for probe in contract["acceptance_probes"] if probe["probe_type"] == "python_web_smoke")
    assert probe_types.index("python_install") < probe_types.index("python_web_smoke")
    assert probe_types.index("python_web_smoke") < probe_types.index("pytest")
    assert web_probe["require_html_root"] is True


def test_static_webapp_with_macos_aesthetic_does_not_require_desktop_or_api_contract():
    description = (
        "Build a small but complete static web app for validating native-skill task delivery. "
        "Create exactly these files and no dependency folders or build outputs: "
        "index.html, styles.css, app.js, README.md. Product: Native Skill Route Planner. "
        "Include a dark macOS productivity app aesthetic, a task composer, routing summary, "
        "localStorage persistence, and a manual verification checklist. It is a web page only. "
        "Do not build a packaged macOS .app bundle, Swift app, backend API service, test suite, "
        "package.json, node_modules, dist, .git, screenshots, databases, or cache folders."
    )
    contract = build_owner_delivery_contract(
        task_id="task-static-web",
        description=description,
        task_types=["functional", "artifact"],
        project_dir="/tmp/project",
        manifest={
            "deliverables": [
                {"path_hint": "index.html", "artifact_type": "frontend_source", "required": True},
                {"path_hint": "styles.css", "artifact_type": "frontend_source", "required": True},
                {"path_hint": "app.js", "artifact_type": "frontend_source", "required": True},
                {"path_hint": "README.md", "artifact_type": "documentation", "required": True},
            ]
        },
    )

    assert "web_ui" in contract["delivery_facets"]
    assert "desktop_app" not in contract["delivery_facets"]
    assert "api_service" not in contract["delivery_facets"]
    assert "test_suite" not in contract["delivery_facets"]
    assert "cap-api" not in {item["id"] for item in contract["capabilities"]}
    assert {item["stack"] for item in contract["technology_hypotheses"]} == {"native-web"}
    assert not any(item["artifact_type"] == "macos_app_bundle" for item in contract["deliverables"])
    assert not any(group["id"] == "group-api-source" for group in contract["deliverable_groups"])
    assert not any(group["id"] == "group-test-suite" for group in contract["deliverable_groups"])
    probe_types = {probe["probe_type"] for probe in contract["acceptance_probes"]}
    assert probe_types == {"static_web_smoke", "browser_e2e"}
    browser_probe = next(probe for probe in contract["acceptance_probes"] if probe["probe_type"] == "browser_e2e")
    assert browser_probe["required"] is True
    assert browser_probe["minimum_evidence"] == "L3"
    capability_probe_ids = {
        probe_id
        for capability in contract["capabilities"]
        for probe_id in capability.get("acceptance_probe_ids", [])
    }
    assert "probe-static-web-smoke" in capability_probe_ids
    assert "probe-browser-e2e" in capability_probe_ids
    assert "probe-python-web-smoke" not in capability_probe_ids
    assert "probe-pytest" not in capability_probe_ids


def test_interactive_static_web_contract_requires_browser_e2e_probe():
    contract = build_owner_delivery_contract(
        task_id="task-static-interactive",
        description=(
            "Build a static web app called Capability Garden Planner with a canvas animation, "
            "Functional and Artifact mode toggles, localStorage persistence, route evidence, "
            "and a Recompute Route button."
        ),
        task_types=["functional"],
        project_dir="/tmp/project",
        manifest={
            "deliverables": [
                {"path_hint": "index.html", "artifact_type": "frontend_source", "required": True},
                {"path_hint": "styles.css", "artifact_type": "frontend_source", "required": True},
                {"path_hint": "app.js", "artifact_type": "frontend_source", "required": True},
            ]
        },
    )

    probe_types = [probe["probe_type"] for probe in contract["acceptance_probes"]]
    gate_by_type = {gate["gate_type"]: gate for gate in contract["gate_plan"]}

    assert "static_web_smoke" in probe_types
    assert "browser_e2e" in probe_types
    assert probe_types.index("static_web_smoke") < probe_types.index("browser_e2e")
    assert gate_by_type["browser_e2e"]["required"] is True
    assert gate_by_type["browser_e2e"]["probe_type"] == "browser_e2e"


def test_complex_webapp_contract_records_facets_groups_and_capability_matrix():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-readme", "artifact_type": "documentation", "path_hint": "README.md", "required": True},
            {"requirement_id": "req-testing", "artifact_type": "documentation", "path_hint": "TESTING.md", "required": True},
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-expenses",
        description=(
            "Build a local personal expense and receipt tracking WebApp with FastAPI and SQLite. "
            "Include expense CRUD APIs, CSV import, receipt upload, filters by month/category/merchant, "
            "monthly and category dashboard summaries, native HTML/CSS/JavaScript frontend, and pytest tests. "
            "Keep only README.md and TESTING.md as documentation. Do not add login, auth, JWT, Docker, or passwords."
        ),
        task_types=["functional", "artifact"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    assert contract["contract_version"] == "2.0"
    assert {
        "source_project",
        "runnable_app",
        "web_ui",
        "api_service",
        "local_storage",
        "test_suite",
        "documentation",
    } <= set(contract["delivery_facets"])
    assert {"python-fastapi", "native-web", "sqlite"} <= {
        item["stack"] for item in contract["technology_hypotheses"]
    }
    group_ids = {group["id"] for group in contract["deliverable_groups"]}
    assert {
        "group-api-source",
        "group-web-ui",
        "group-test-suite",
        "group-install-metadata",
        "group-docs",
    } <= group_ids
    capability_ids = {item["id"] for item in contract["capabilities"]}
    assert {
        "cap-expense-create",
        "cap-expense-list",
        "cap-expense-update",
        "cap-expense-delete",
        "cap-csv-import",
        "cap-receipt-upload",
        "cap-dashboard-summary",
        "cap-frontend-loads",
        "cap-no-auth",
        "cap-docs-limited",
    } <= capability_ids
    gate_ids = {gate["id"] for gate in contract["gate_plan"]}
    assert {"gate-install", "gate-runtime-smoke", "gate-tests", "gate-workspace-hygiene"} <= gate_ids


def test_chinese_expense_webapp_contract_expands_capability_matrix():
    contract = build_owner_delivery_contract(
        task_id="task-expenses-cn",
        description=(
            "构建一个本地可运行的个人支出和票据追踪 WebApp。"
            "使用 Python 3.11 + FastAPI + SQLite，实现支出 CRUD API。"
            "前端必须是原生 HTML/CSS/JavaScript 单页应用，能新增、编辑、删除支出，"
            "按月份/分类/商户关键字过滤，显示总金额、分类汇总和最近支出列表。"
            "支持 CSV 导入支出，支持票据文件上传。不要实现认证、登录、用户、密码、JWT 或 OAuth。"
            "文档只需要 README.md 和 TESTING.md。"
        ),
        task_types=["functional", "artifact"],
        project_dir="/tmp/project",
        manifest={
            "deliverables": [
                {"requirement_id": "req-readme", "artifact_type": "documentation", "path_hint": "README.md", "required": True},
                {"requirement_id": "req-testing", "artifact_type": "documentation", "path_hint": "TESTING.md", "required": True},
            ]
        },
    )

    capability_ids = {item["id"] for item in contract["capabilities"]}
    assert {
        "cap-expense-create",
        "cap-expense-list",
        "cap-expense-update",
        "cap-expense-delete",
        "cap-filter-by-month",
        "cap-filter-by-category",
        "cap-filter-by-merchant",
        "cap-csv-import",
        "cap-receipt-upload",
        "cap-dashboard-summary",
        "cap-dashboard-category-breakdown",
        "cap-frontend-loads",
        "cap-no-auth",
        "cap-docs-limited",
    } <= capability_ids


def test_functional_contract_does_not_extract_generic_completion_words_as_capability():
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description=(
            "Write a complete README explaining installation and usage. "
            "The task is documentation only; do not implement a todo completion feature."
        ),
        task_types=["functional"],
        project_dir="/tmp/project",
        manifest={"deliverables": []},
    )

    capability_ids = {item["id"] for item in contract["capabilities"]}
    assert "cap-complete" not in capability_ids


def test_functional_contract_records_extraction_diagnostics_for_review():
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="Build a todo CLI with add and list commands, but no complete command.",
        task_types=["functional"],
        project_dir="/tmp/project",
        manifest={"deliverables": []},
    )

    diagnostics = contract.get("extraction_diagnostics")
    assert diagnostics
    assert "cap-add" in diagnostics["included_capability_ids"]
    assert "cap-list" in diagnostics["included_capability_ids"]
    assert "cap-complete" in diagnostics["excluded_capability_ids"]


def test_notes_cli_contract_adds_user_facing_smoke_probe():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-cli", "artifact_type": "api_service_source", "path_hint": "notes_cli.py", "required": True},
            {"requirement_id": "req-test", "artifact_type": "test_source", "path_hint": "test_notes.py", "required": True},
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description=(
            "Create notes_cli.py with commands add, list, done, delete, search, and export. "
            "Search finds by title/tag and export writes Markdown."
        ),
        task_types=["functional", "artifact"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    probe_types = {probe["probe_type"] for probe in contract["acceptance_probes"]}
    assert "notes_cli_smoke" in probe_types


def test_artifact_contract_uses_manifest_deliverables():
    manifest = {
        "deliverables": [
            {"requirement_id": "req-src", "artifact_type": "api_service_source", "path_hint": "app/todo.py", "required": True},
            {"requirement_id": "req-doc", "artifact_type": "documentation", "path_hint": "README.md", "required": True},
        ]
    }
    contract = build_owner_delivery_contract(
        task_id="task-1",
        description="Produce app/todo.py and README.md.",
        task_types=["artifact"],
        project_dir="/tmp/project",
        manifest=manifest,
    )

    paths = {item["path_hint"] for item in contract["deliverables"]}
    assert paths == {"app/todo.py", "README.md"}
    assert contract["capabilities"] == []
