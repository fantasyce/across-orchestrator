import pytest
from unittest.mock import MagicMock

from across_agents_assistant.task_manager.models import (
    AcceptanceCheck,
    DeliverableSpec,
    Job,
    JobStatus,
    SubTask,
    Task,
    TaskContract,
)
from across_agents_assistant.task_manager.state import TaskState
from across_agents_assistant.task_manager.orchestration.owner_agent import OwnerAgent
from across_agents_assistant.task_manager.orchestration.release_e2e import (
    RELEASE_E2E_SCENARIO_ID,
    build_release_e2e_task_request,
)


class MockLLMResponse:
    def __init__(self, text: str):
        self.text = text


class MockLLMGateway:
    def __init__(self, response_text: str):
        self._response_text = response_text
        self.calls = []

    def __call__(self, system_prompt: str, message: str, temperature: float):
        self.calls.append({
            "system_prompt": system_prompt,
            "message": message,
            "temperature": temperature,
        })
        return MockLLMResponse(self._response_text)


AVAILABLE_AGENT_IDS = ["claude", "deepseek", "hermes", "minimax", "openclaw"]


def test_parse_json_response_ignores_thinking_block_before_acceptance_json(fake_task_state):
    owner = OwnerAgent(MockLLMGateway("{}"), fake_task_state)
    response_text = """
<think>
Let me inspect the previous attempts first.
The draft includes an object-shaped note: {"not": "the decision"}.
</think>

{"passed": true, "feedback": "Verified deterministic evidence.", "action": "approve"}
"""

    parsed = owner._parse_json_response(response_text)

    assert parsed == {
        "passed": True,
        "feedback": "Verified deterministic evidence.",
        "action": "approve",
    }


def test_parse_json_response_extracts_first_balanced_json_object(fake_task_state):
    owner = OwnerAgent(MockLLMGateway("{}"), fake_task_state)
    response_text = """
Review complete.
{"passed": false, "feedback": "Missing api/server.mjs", "action": "fix"}
Additional prose after the JSON should be ignored.
"""

    parsed = owner._parse_json_response(response_text)

    assert parsed["passed"] is False
    assert parsed["action"] == "fix"
    assert parsed["feedback"] == "Missing api/server.mjs"


def _dag_subtasks(subtasks):
    return [st for st in subtasks if not st.subtask_id.endswith("-decompose")]


def _all_agents():
    return [
        {"id": "claude", "name": "Claude Code", "characteristics": "architecture"},
        {"id": "deepseek", "name": "DeepSeek", "characteristics": "backend"},
        {"id": "hermes", "name": "Hermes", "characteristics": "frontend"},
        {"id": "minimax", "name": "MiniMax", "characteristics": "devops"},
        {"id": "openclaw", "name": "OpenClaw", "characteristics": "general"},
    ]


class TestDecomposeAndAssign:
    def test_creates_subtasks_with_correct_agents(self):
        state = TaskState()
        task = state.create_task("Build a task management system")

        llm_response = """{"subtasks": [
            {"id": "arch", "description": "Design OpenAPI spec and DB schema", "agent": "claude", "priority": 1, "dependencies": []},
            {"id": "backend", "description": "Implement FastAPI backend with Pydantic models", "agent": "deepseek", "priority": 2, "dependencies": ["arch"]},
            {"id": "frontend", "description": "Build React UI with TypeScript components", "agent": "hermes", "priority": 2, "dependencies": ["arch"]},
            {"id": "devops", "description": "Setup Docker and nginx deployment", "agent": "minimax", "priority": 3, "dependencies": ["backend", "frontend"]}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(task)

        dag_subtasks = _dag_subtasks(result.subtasks)

        assert len(dag_subtasks) == 4
        assert dag_subtasks[0].agent_id == "claude"
        assert dag_subtasks[1].agent_id == "deepseek"
        assert dag_subtasks[2].agent_id == "hermes"
        assert dag_subtasks[3].agent_id == "minimax"

    def test_context_allowed_subtask_agents_limits_assignment_pool(self):
        state = TaskState()
        task = state.create_task(
            "Build a task management system",
            owner_agent="claude",
            allowed_subtask_agents=["deepseek"],
        )

        llm_response = """{"subtasks": [
            {"id": "arch", "description": "Design OpenAPI spec and DB schema", "agent": "claude", "priority": 1, "dependencies": []},
            {"id": "backend", "description": "Implement FastAPI backend", "agent": "deepseek", "priority": 2, "dependencies": ["arch"]}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(
            task,
            context={"owner_agent": "claude", "allowed_subtask_agents": ["deepseek"]},
        )

        dag_subtasks = _dag_subtasks(result.subtasks)
        assert {st.agent_id for st in dag_subtasks} == {"deepseek"}
        assert "claude" not in llm.calls[0]["system_prompt"]

    def test_gap_fill_respects_allowed_subtask_agents(self, tmp_path):
        from across_agents_assistant.task_manager.orchestration.coverage import CoverageGap

        state = TaskState()
        task = state.create_task(
            "Build a static web app",
            project_dir=str(tmp_path),
            owner_agent="claude",
            allowed_subtask_agents=["claude"],
            task_types=["functional", "artifact"],
        )
        owner = OwnerAgent(MockLLMGateway('{"subtasks": []}'), state)
        owner._get_available_agents = _all_agents
        owner._is_agent_available = lambda agent_id: True

        owner._create_gap_fill_subtask(
            task,
            CoverageGap(
                requirement_id="req-app-js",
                path_hint="app.js",
                artifact_type="frontend_source",
                reason="required_deliverable_unassigned",
            ),
        )

        gap_subtasks = [st for st in task.subtasks if st.subtask_id.startswith("st-gap-")]
        assert len(gap_subtasks) == 1
        assert gap_subtasks[0].agent_id == "claude"

    def test_readme_gap_fill_inherits_static_web_constraints(self, tmp_path):
        from across_agents_assistant.task_manager.orchestration.coverage import CoverageGap

        state = TaskState()
        task = state.create_task(
            "Build a static web app with index.html, styles.css, app.js, and README.md. "
            "It must run by opening index.html directly. No package managers.",
            project_dir=str(tmp_path),
            owner_agent="claude",
            allowed_subtask_agents=["claude"],
            task_types=["functional", "artifact"],
        )
        owner = OwnerAgent(MockLLMGateway('{"subtasks": []}'), state)
        owner._get_available_agents = _all_agents
        owner._is_agent_available = lambda agent_id: True

        owner._create_gap_fill_subtask(
            task,
            CoverageGap(
                requirement_id="req-readme",
                path_hint="README.md",
                artifact_type="documentation",
                reason="required_deliverable_unassigned",
            ),
        )

        gap_subtask = next(st for st in task.subtasks if st.subtask_id.startswith("st-gap-"))
        assert "README.md" in gap_subtask.description
        assert "Open index.html directly" in gap_subtask.description
        assert "Do NOT include npm install" in gap_subtask.description
        assert "No package managers" in gap_subtask.description

    def test_missing_llm_path_hint_is_repaired_from_subtask_description(self, tmp_path):
        state = TaskState()
        state.set_persistence(InMemoryPersistence())
        task = state.create_task(
            "Create exactly these files: index.html, app.js.",
            project_dir=str(tmp_path),
            task_types=["functional", "artifact"],
        )

        llm_response = """{"subtasks": [
            {
                "id": "script",
                "description": "Create app.js with localStorage persistence and route preview updates",
                "agent": "claude",
                "priority": 1,
                "dependencies": [],
                "expected_deliverables": [
                    {"artifact_type": "file", "required": true, "description": "Script file"}
                ]
            }
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(
            task,
            context={"owner_agent": "claude", "allowed_subtask_agents": ["claude"]},
        )

        contracts = state.get_task_contracts(task.task_id)
        script_contract = next(
            contract for contract in contracts
            if contract.get("subtask_id")
            and not str(contract.get("subtask_id")).startswith("st-gap-")
        )
        deliverables = script_contract.get("expected_deliverables") or []
        assert any(item.get("path_hint") == "app.js" for item in deliverables)
        assert not any(st.subtask_id.startswith("st-gap-") and "app.js" in st.description for st in _dag_subtasks(result.subtasks))

    def test_release_e2e_node_api_contract_keeps_server_path_and_needs_no_gap(self, tmp_path):
        state = TaskState()
        state.set_persistence(InMemoryPersistence())
        request = build_release_e2e_task_request(
            scenario_id=RELEASE_E2E_SCENARIO_ID,
            project_dir=str(tmp_path / "release-e2e"),
            run_label="owner-coverage",
        )
        task = state.create_task(
            request["description"],
            project_dir=request["project_dir"],
            owner_agent=request["owner_agent"],
            allowed_subtask_agents=request["allowed_subtask_agents"],
            task_types=request["task_types"],
        )
        task.strict_dependency = request["strict_dependency"]
        task.enable_wave_gate = request["enable_wave_gate"]
        task.required_agent_mix = request["required_agent_mix"]

        owner = OwnerAgent(MockLLMGateway('{"subtasks": []}'), state)
        owner._get_available_agents = lambda: [
            {"id": "claude", "name": "Claude Code", "characteristics": "architecture"},
            {"id": "deepseek", "name": "DeepSeek", "characteristics": "backend"},
            {"id": "hermes", "name": "Hermes", "characteristics": "frontend"},
            {"id": "minimax", "name": "MiniMax", "characteristics": "devops"},
            {"id": "openclaw", "name": "OpenClaw", "characteristics": "general"},
        ]

        result = owner.decompose_and_assign(
            task,
            context={
                "release_e2e": {"scenario_id": RELEASE_E2E_SCENARIO_ID},
                "owner_agent": request["owner_agent"],
                "allowed_subtask_agents": request["allowed_subtask_agents"],
                "task_types": request["task_types"],
            },
        )

        api_contract = next(
            contract for contract in state.get_task_contracts(task.task_id)
            if contract.get("subtask_id")
            and "Create api/server.mjs" in str(contract.get("goal") or "")
        )
        deliverables = api_contract.get("expected_deliverables") or []
        gap_descriptions = [
            st.description for st in _dag_subtasks(result.subtasks)
            if st.subtask_id.startswith("st-gap-")
        ]

        assert any(item.get("path_hint") == "api/server.mjs" for item in deliverables)
        assert not any("api/server.mjs" in description for description in gap_descriptions)

    def test_agent_capability_context_guides_decomposition_and_worker_prompt(self, tmp_path):
        state = TaskState()
        task = state.create_task(
            "Build an accessible settings page",
            project_dir=str(tmp_path),
            task_types=["functional"],
        )

        llm_response = """{"subtasks": [
            {"id": "frontend", "description": "Implement the SwiftUI settings page", "agent": "hermes", "priority": 1, "dependencies": []}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(
            task,
            context={
                "task_types": ["functional"],
                "agent_capabilities": {
                    "profiles": {
                        "hermes": {
                            "agent_id": "hermes",
                            "enabled_skill_ids": ["frontend_design"],
                            "enabled_plugin_ids": ["filesystem"],
                            "enabled_tool_names": ["read_file", "write_file"],
                            "custom_instructions": "Prefer keyboard-first interaction and compact controls.",
                            "strict_tool_scope": True,
                        }
                    },
                    "skills": [
                        {
                            "id": "frontend_design",
                            "name": "Frontend product design",
                            "description": "Design and implement polished interfaces.",
                            "prompt_hint": "Use the app design language and verify responsive layout.",
                            "tags": ["frontend"],
                        }
                    ],
                    "native_skills": {
                        "hermes": [
                            {
                                "id": "swiftui-layout-review",
                                "name": "SwiftUI Layout Review",
                                "status": "enabled",
                                "source": "hermes",
                            }
                        ]
                    },
                    "prompt": "- hermes: skills=Frontend product design; plugins=filesystem; tools=read_file, write_file",
                },
            },
        )

        subtask = _dag_subtasks(result.subtasks)[0]

        assert "Configured Agent Capabilities" in llm.calls[0]["system_prompt"]
        assert "Frontend product design" in llm.calls[0]["system_prompt"]
        assert "SwiftUI Layout Review" in llm.calls[0]["system_prompt"]
        assert "[AGENT CAPABILITY PROFILE]" in subtask.description
        assert "Native skills: SwiftUI Layout Review" in subtask.description
        assert "filesystem" in subtask.description
        assert "read_file, write_file" in subtask.description
        assert "Prefer keyboard-first interaction" in subtask.description
        assert "Only use the listed plugins/tools" in subtask.description

    def test_native_skill_context_reaches_worker_without_profile(self, tmp_path):
        state = TaskState()
        task = state.create_task(
            "Review the SwiftUI sidebar layout.",
            project_dir=str(tmp_path),
            task_types=["functional"],
        )

        llm_response = """{"subtasks": [
            {"id": "review", "description": "Review sidebar layout", "agent": "hermes", "priority": 1, "dependencies": []}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(
            task,
            context={
                "task_types": ["functional"],
                "agent_capabilities": {
                    "native_skills": {
                        "hermes": [
                            {
                                "id": "swiftui-layout-review",
                                "name": "SwiftUI Layout Review",
                                "status": "enabled",
                                "source": "hermes",
                            }
                        ]
                    }
                },
            },
        )

        subtask = _dag_subtasks(result.subtasks)[0]

        assert "Configured Agent Capabilities" in llm.calls[0]["system_prompt"]
        assert "SwiftUI Layout Review" in llm.calls[0]["system_prompt"]
        assert "[AGENT CAPABILITY PROFILE]" in subtask.description
        assert "Native skills: SwiftUI Layout Review" in subtask.description

    def test_native_skill_match_can_route_to_matching_local_agent(self, tmp_path):
        state = TaskState()
        task = state.create_task(
            "Audit keyboard accessibility and screen reader behavior for the settings workflow.",
            project_dir=str(tmp_path),
            owner_agent="claude",
            allowed_subtask_agents=["claude", "hermes", "openclaw"],
            task_types=["functional"],
        )

        llm_response = """{"subtasks": [
            {"id": "a11y-review", "description": "Review keyboard accessibility and screen reader behavior for the settings workflow", "agent": "claude", "priority": 1, "dependencies": []}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(
            task,
            context={
                "owner_agent": "claude",
                "allowed_subtask_agents": ["claude", "hermes", "openclaw"],
                "task_types": ["functional"],
                "agent_capabilities": {
                    "native_skills": {
                        "hermes": [
                            {
                                "id": "accessibility-review",
                                "name": "Accessibility Review",
                                "description": "Review keyboard navigation, screen reader behavior, focus order, and UI accessibility.",
                                "status": "enabled",
                                "source": "hermes",
                            }
                        ]
                    }
                },
            },
        )

        subtask = _dag_subtasks(result.subtasks)[0]

        assert subtask.agent_id == "hermes"
        assert "[ROUTING DECISION]" in subtask.description
        assert "Native skill match: Accessibility Review" in subtask.description
        assert "keyboard" in subtask.description

    def test_native_skill_removal_changes_assignment_back_to_default(self, tmp_path):
        def _run_with_native_skills(native_skills):
            state = TaskState()
            task = state.create_task(
                "Review keyboard accessibility and screen reader behavior for the settings workflow.",
                project_dir=str(tmp_path),
                owner_agent="claude",
                allowed_subtask_agents=["claude", "hermes", "openclaw"],
                task_types=["functional"],
            )

            llm_response = """{"subtasks": [
                {"id": "a11y-review", "description": "Review keyboard accessibility and screen reader behavior for the settings workflow", "agent": "claude", "priority": 1, "dependencies": []}
            ]}"""

            llm = MockLLMGateway(llm_response)
            owner = OwnerAgent(llm, state)
            owner._get_available_agents = _all_agents
            result = owner.decompose_and_assign(
                task,
                context={
                    "owner_agent": "claude",
                    "allowed_subtask_agents": ["claude", "hermes", "openclaw"],
                    "task_types": ["functional"],
                    "agent_capabilities": {"native_skills": native_skills},
                },
            )
            return _dag_subtasks(result.subtasks)[0]

        routed_with_skill = _run_with_native_skills(
            {
                "hermes": [
                    {
                        "id": "accessibility-review",
                        "name": "Accessibility Review",
                        "description": "Review keyboard navigation, screen reader behavior, focus order, and UI accessibility.",
                        "status": "enabled",
                        "source": "hermes",
                    }
                ]
            }
        )
        routed_without_skill = _run_with_native_skills({})

        assert routed_with_skill.agent_id == "hermes"
        assert routed_without_skill.agent_id == "claude"
        assert "[ROUTING DECISION]" not in routed_without_skill.description

    def test_macos_aesthetic_css_subtask_does_not_require_packaged_app(self, tmp_path):
        state = TaskState()
        owner = OwnerAgent(MockLLMGateway("{}"), state)

        deliverables, checks = owner._infer_subtask_deliverables(
            (
                "Create styles.css with a dark macOS productivity app aesthetic. "
                "Style agent cards with hover states, skill toggles with visual on/off states, "
                "a compact task composer layout, and a routing summary panel with monospace font."
            ),
            "claude",
            str(tmp_path),
        )

        assert not any(item.artifact_type == "macos_app_bundle" for item in deliverables)
        assert not any(item.check_type == "packaged_app_exists" for item in checks)
        assert any(item.path_hint == "styles.css" for item in deliverables)

    def test_planning_only_component_subtask_is_skipped(self, tmp_path):
        state = TaskState()
        task = state.create_task(
            "Build a static dashboard in index.html, styles.css, and app.js.",
            project_dir=str(tmp_path),
            task_types=["functional", "artifact"],
        )

        llm_response = """{"subtasks": [
            {"id": "plan", "description": "Plan the dashboard component architecture, card layout, timeline structure, and interaction state model", "agent": "claude", "priority": 1, "dependencies": []},
            {"id": "ui", "description": "Create index.html, styles.css, and app.js for the dashboard", "agent": "hermes", "priority": 2, "dependencies": ["plan"]}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(task)
        dag_subtasks = _dag_subtasks(result.subtasks)

        descriptions = [st.description for st in dag_subtasks]
        assert not any("Plan the dashboard component architecture" in desc for desc in descriptions)
        ui_subtask = next(st for st in dag_subtasks if "Create index.html" in st.description)
        assert ui_subtask.dependencies == []

    def test_empty_functional_decomposition_uses_implementation_fallback(self, tmp_path):
        state = TaskState()
        task = state.create_task(
            "Build a FastAPI SQLite web app with frontend and pytest tests",
            project_dir=str(tmp_path),
            task_types=["functional", "artifact"],
        )

        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(task, context={"task_types": ["functional", "artifact"]})
        dag_subtasks = _dag_subtasks(result.subtasks)

        assert any("FastAPI + SQLite project skeleton" in st.description for st in dag_subtasks)
        assert any("CSV import" in st.description for st in dag_subtasks)
        assert any("README.md and TESTING.md" in st.description for st in dag_subtasks)
        assert not any(st.subtask_id.startswith("st-gap-") for st in dag_subtasks)
        assert any("tests/test_api.py" in st.description or "pytest coverage" in st.description for st in dag_subtasks)

    def test_limited_documentation_scope_filters_design_doc_subtasks(self, tmp_path):
        state = TaskState()
        task = state.create_task(
            "Build a FastAPI SQLite web app. 文档只需要 README.md 和 TESTING.md，不要生成大量额外文档。",
            project_dir=str(tmp_path),
            task_types=["functional", "artifact"],
        )

        llm_response = """{"subtasks": [
            {"id": "design", "description": "分析需求并设计数据库 schema 和 API 结构", "agent": "claude", "priority": 1, "dependencies": [], "deliverables": [
                {"artifact_type": "documentation", "path_hint": "DATABASE_SCHEMA.md", "required": true},
                {"artifact_type": "documentation", "path_hint": "API_STRUCTURE.md", "required": true}
            ]},
            {"id": "backend", "description": "实现 FastAPI 后端 CRUD API", "agent": "deepseek", "priority": 2, "dependencies": ["design"], "deliverables": [
                {"artifact_type": "api_service_source", "path_hint": "app/main.py", "required": true}
            ]},
            {"id": "docs", "description": "创建 README.md 和 TESTING.md 文档", "agent": "openclaw", "priority": 3, "dependencies": ["backend"], "deliverables": [
                {"artifact_type": "documentation", "path_hint": "README.md", "required": true},
                {"artifact_type": "documentation", "path_hint": "TESTING.md", "required": true}
            ]}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(task, context={"task_types": ["functional", "artifact"]})
        dag_subtasks = _dag_subtasks(result.subtasks)

        assert not any("DATABASE_SCHEMA.md" in st.description for st in dag_subtasks)
        assert not any("API_STRUCTURE.md" in st.description for st in dag_subtasks)
        assert not any("分析需求并设计数据库 schema" in st.description for st in dag_subtasks)
        backend = next(st for st in dag_subtasks if "实现 FastAPI 后端 CRUD API" in st.description)
        assert backend.dependencies == []
        assert any("README.md 和 TESTING.md" in st.description for st in dag_subtasks)

    def test_global_negative_constraints_are_added_to_worker_prompts(self, tmp_path):
        state = TaskState()
        task = state.create_task(
            "Build an expense tracker. 不要实现登录、注册、JWT、密码。除 README.md 和 TESTING.md 外不要增加额外文档。不要创建 run.py。",
            project_dir=str(tmp_path),
            task_types=["functional", "artifact"],
        )

        llm_response = """{"subtasks": [
            {"id": "models", "description": "创建 Pydantic 模型和数据访问层", "agent": "deepseek", "priority": 1, "dependencies": [], "deliverables": [
                {"artifact_type": "api_service_source", "path_hint": "models.py", "required": true}
            ]}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(task, context={"task_types": ["functional", "artifact"]})
        subtask = _dag_subtasks(result.subtasks)[0]

        assert "Do NOT implement authentication" in subtask.description
        assert "only create README.md, TESTING.md" in subtask.description
        assert "run.py" in subtask.description

    def test_validation_only_subtasks_are_filtered_from_functional_decomposition(self, tmp_path):
        state = TaskState()
        task = state.create_task(
            "Build a FastAPI SQLite web app with pytest tests",
            project_dir=str(tmp_path),
            task_types=["functional", "artifact"],
        )

        llm_response = """{"subtasks": [
            {"id": "backend", "description": "Implement FastAPI backend CRUD APIs", "agent": "deepseek", "priority": 1, "dependencies": [], "deliverables": [
                {"artifact_type": "api_service_source", "path_hint": "app/main.py", "required": true}
            ]},
            {"id": "tests", "description": "Write pytest tests covering API CRUD behavior", "agent": "deepseek", "priority": 2, "dependencies": ["backend"], "deliverables": [
                {"artifact_type": "test_source", "path_hint": "tests/test_api.py", "required": true}
            ]},
            {"id": "final-validation", "description": "Run the application, execute tests, verify all endpoints work correctly, fix any issues found", "agent": "openclaw", "priority": 3, "dependencies": ["tests"], "deliverables": []},
            {"id": "docs", "description": "Create README.md and TESTING.md documentation", "agent": "openclaw", "priority": 4, "dependencies": ["final-validation"], "deliverables": [
                {"artifact_type": "documentation", "path_hint": "README.md", "required": true},
                {"artifact_type": "documentation", "path_hint": "TESTING.md", "required": true}
            ]}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(task, context={"task_types": ["functional", "artifact"]})
        dag_subtasks = _dag_subtasks(result.subtasks)

        assert not any("Run the application" in st.description for st in dag_subtasks)
        tests = next(st for st in dag_subtasks if "Write pytest tests" in st.description)
        docs = next(st for st in dag_subtasks if "README.md and TESTING.md" in st.description)
        assert docs.dependencies == [tests.subtask_id]
        assert any("Write pytest tests" in st.description for st in dag_subtasks)

    def test_structure_only_subtasks_are_filtered_from_functional_decomposition(self, tmp_path):
        state = TaskState()
        task = state.create_task(
            "Build a Node API and static web app with exact file deliverables.",
            project_dir=str(tmp_path),
            task_types=["functional", "artifact"],
        )

        llm_response = """{"subtasks": [
            {"id": "structure", "description": "Create the exact project directory structure with web/, api/, cli/, and tests/ subdirectories. Ensure no extra files, package.json, node_modules, or metadata are created.", "agent": "deepseek", "priority": 1, "dependencies": [], "deliverables": [], "acceptance_checks": []},
            {"id": "api", "description": "Create api/server.mjs using Node.js built-in http module.", "agent": "deepseek", "priority": 2, "dependencies": ["structure"], "deliverables": [
                {"artifact_type": "file", "path_hint": "api/server.mjs", "required": true}
            ], "acceptance_checks": [{"check_type": "file_exists", "required": true}]}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(task, context={"task_types": ["functional", "artifact"]})
        dag_subtasks = _dag_subtasks(result.subtasks)

        assert len(dag_subtasks) == 1
        assert "api/server.mjs" in dag_subtasks[0].description
        assert dag_subtasks[0].dependencies == []
        assert not any("directory structure" in st.description.lower() for st in dag_subtasks)

    def test_functional_decomposition_prompt_blocks_unrequested_auth(self, tmp_path):
        state = TaskState()
        task = state.create_task(
            "Build a personal expense FastAPI SQLite web app with native frontend",
            project_dir=str(tmp_path),
            task_types=["functional"],
        )

        llm_response = """{"subtasks": [
            {"id": "backend", "description": "Implement FastAPI backend CRUD APIs", "agent": "deepseek", "priority": 1, "dependencies": []}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        owner.decompose_and_assign(task, context={"task_types": ["functional"]})

        message = llm.calls[0]["message"]
        assert "Do not add authentication" in message
        assert "login" in message

    def test_inspection_only_subtasks_are_filtered_from_functional_decomposition(self, tmp_path):
        state = TaskState()
        task = state.create_task(
            "Build a FastAPI SQLite web app with native frontend and pytest tests",
            project_dir=str(tmp_path),
            task_types=["functional", "artifact"],
        )

        llm_response = """{"subtasks": [
            {"id": "inspect", "description": "Check current state of project directory", "agent": "openclaw", "priority": 1, "dependencies": [], "deliverables": []},
            {"id": "backend", "description": "Create project structure and backend core", "agent": "deepseek", "priority": 2, "dependencies": ["inspect"], "deliverables": [
                {"artifact_type": "api_service_source", "path_hint": "app/main.py", "required": true}
            ]}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(task, context={"task_types": ["functional", "artifact"]})
        dag_subtasks = _dag_subtasks(result.subtasks)

        assert not any("Check current state" in st.description for st in dag_subtasks)
        backend = next(st for st in dag_subtasks if "backend core" in st.description)
        assert backend.dependencies == []

    def test_dependency_mapping_resolves_correct_ids(self):
        state = TaskState()
        task = state.create_task("Build a task management system")

        llm_response = """{"subtasks": [
            {"id": "arch", "description": "Design OpenAPI spec", "agent": "claude", "priority": 1, "dependencies": []},
            {"id": "backend", "description": "Implement backend", "agent": "deepseek", "priority": 2, "dependencies": ["arch"]}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(task)

        dag_subtasks = _dag_subtasks(result.subtasks)

        assert len(dag_subtasks) == 2
        arch_st = dag_subtasks[0]
        backend_st = dag_subtasks[1]

        assert backend_st.dependencies == [arch_st.subtask_id]
        assert arch_st.dependencies == []

    def test_persists_subtasks_in_state(self):
        state = TaskState()
        task = state.create_task("Build a task management system")

        llm_response = """{"subtasks": [
            {"id": "arch", "description": "Design OpenAPI spec", "agent": "claude", "priority": 1, "dependencies": []}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        owner.decompose_and_assign(task)

        persisted_task = state.get_task(task.task_id)
        assert persisted_task is not None
        dag_subtasks = _dag_subtasks(persisted_task.subtasks)

        assert len(dag_subtasks) == 1
        assert dag_subtasks[0].description == "Design OpenAPI spec"

    def test_empty_subtasks_on_llm_failure(self):
        state = TaskState()
        task = state.create_task("Build something")

        llm = MockLLMGateway("not valid json")
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        with pytest.raises(RuntimeError, match="no subtasks generated"):
            owner.decompose_and_assign(task)

    def test_dependency_text_to_id_mapping_by_description(self):
        state = TaskState()
        task = state.create_task("Build a task management system")

        llm_response = """{"subtasks": [
            {"id": "arch", "description": "Design OpenAPI spec", "agent": "claude", "priority": 1, "dependencies": []},
            {"id": "backend", "description": "Implement backend", "agent": "deepseek", "priority": 2, "dependencies": ["Design OpenAPI spec"]}
        ]}"""

        llm = MockLLMGateway(llm_response)
        owner = OwnerAgent(llm, state)
        owner._get_available_agents = _all_agents

        result = owner.decompose_and_assign(task)

        dag_subtasks = _dag_subtasks(result.subtasks)

        assert len(dag_subtasks) == 2
        arch_st = dag_subtasks[0]
        backend_st = dag_subtasks[1]

        # Dependency text "Design OpenAPI spec" should match arch subtask by description
        assert arch_st.subtask_id in backend_st.dependencies


class TestSelectAgent:
    def test_architecture_keywords_select_claude(self):
        state = TaskState()
        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)

        assert owner._select_agent({"description": "Design system architecture"}, AVAILABLE_AGENT_IDS) == "claude"
        assert owner._select_agent({"description": "Create OpenAPI spec"}, AVAILABLE_AGENT_IDS) == "claude"
        assert owner._select_agent({"description": "Define DB schema"}, AVAILABLE_AGENT_IDS) == "claude"
        assert owner._select_agent({"description": "Design the data model"}, AVAILABLE_AGENT_IDS) == "claude"

    def test_backend_keywords_select_deepseek(self):
        state = TaskState()
        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)

        assert owner._select_agent({"description": "Build FastAPI backend"}, AVAILABLE_AGENT_IDS) == "deepseek"
        assert owner._select_agent({"description": "Create Pydantic models"}, AVAILABLE_AGENT_IDS) == "deepseek"
        assert owner._select_agent({"description": "Implement REST API"}, AVAILABLE_AGENT_IDS) == "deepseek"
        assert (
            owner._select_agent(
                {
                    "description": "Implement FastAPI backend with SQLite database.py and serve static/index.html at GET /",
                    "agent": "openclaw",
                },
                AVAILABLE_AGENT_IDS,
                project_dir="/tmp/demo-project",
            )
            == "deepseek"
        )

    def test_frontend_keywords_select_hermes(self):
        state = TaskState()
        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)

        assert owner._select_agent({"description": "Build React frontend"}, AVAILABLE_AGENT_IDS) == "hermes"
        assert owner._select_agent({"description": "Create UI components"}, AVAILABLE_AGENT_IDS) == "hermes"
        assert owner._select_agent({"description": "Write TypeScript code"}, AVAILABLE_AGENT_IDS) == "hermes"
        assert (
            owner._select_agent(
                {
                    "description": "Implement native HTML/CSS/JavaScript frontend using fetch API to call the backend",
                    "agent": "hermes",
                },
                AVAILABLE_AGENT_IDS,
                project_dir="/tmp/demo-project",
            )
            == "hermes"
        )

    def test_devops_keywords_select_minimax(self):
        state = TaskState()
        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)

        assert owner._select_agent({"description": "Setup Docker deployment"}, AVAILABLE_AGENT_IDS) == "minimax"
        assert owner._select_agent({"description": "Configure nginx"}, AVAILABLE_AGENT_IDS) == "minimax"
        assert owner._select_agent({"description": "Setup CI pipeline"}, AVAILABLE_AGENT_IDS) == "minimax"

    def test_default_falls_to_local_agent(self):
        state = TaskState()
        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)

        assert owner._select_agent({"description": "Write documentation"}, ["openclaw"]) == "openclaw"
        assert owner._select_agent({"description": "Do some research"}, ["openclaw"]) == "openclaw"

    def test_trusts_llm_suggested_valid_agent(self):
        state = TaskState()
        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)

        # When no keyword matches but LLM suggested a valid agent
        assert owner._select_agent({"description": "Write documentation", "agent": "claude"}, AVAILABLE_AGENT_IDS) == "claude"
        assert owner._select_agent({"description": "Write documentation", "agent": "openclaw"}, AVAILABLE_AGENT_IDS) == "openclaw"

    def test_project_dir_architecture_keeps_claude_for_design_tasks(self):
        state = TaskState()
        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)

        selected = owner._select_agent(
            {"description": "Design system architecture and API specification"},
            available_agent_ids=["claude", "deepseek", "minimax"],
            project_dir="/tmp/demo-project",
        )

        assert selected == "claude"

    def test_project_dir_implementation_prefers_deepseek_over_claude(self):
        state = TaskState()
        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)

        selected = owner._select_agent(
            {"description": "Create backend service files and implement FastAPI endpoints", "agent": "claude"},
            available_agent_ids=["claude", "deepseek", "minimax"],
            project_dir="/tmp/demo-project",
        )

        assert selected == "deepseek"


class TestParseDecomposition:
    def test_parses_direct_json(self):
        state = TaskState()
        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)

        data = owner._parse_decomposition('{"subtasks": [{"id": "a", "description": "test"}]}')
        assert len(data["subtasks"]) == 1
        assert data["subtasks"][0]["id"] == "a"

    def test_parses_json_in_markdown_block(self):
        state = TaskState()
        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)

        text = '```json\n{"subtasks": [{"id": "b", "description": "test"}]}\n```'
        data = owner._parse_decomposition(text)
        assert len(data["subtasks"]) == 1
        assert data["subtasks"][0]["id"] == "b"

    def test_parses_json_in_plain_markdown_block(self):
        state = TaskState()
        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)

        text = '```\n{"subtasks": [{"id": "c", "description": "test"}]}\n```'
        data = owner._parse_decomposition(text)
        assert len(data["subtasks"]) == 1
        assert data["subtasks"][0]["id"] == "c"

    def test_returns_empty_on_invalid_json(self):
        state = TaskState()
        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)

        data = owner._parse_decomposition("not json at all")
        assert data == {"subtasks": []}

    def test_parses_nested_json_from_text(self):
        state = TaskState()
        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)

        text = 'Here is the plan:\n\n{"subtasks": [{"id": "d", "description": "test"}]}\n\nLet me know if you need changes.'
        data = owner._parse_decomposition(text)
        assert len(data["subtasks"]) == 1
        assert data["subtasks"][0]["id"] == "d"


class TestRunIntegrationTest:
    def test_returns_placeholder_passed(self):
        state = TaskState()
        task = state.create_task("Build something")
        llm = MockLLMGateway('{"subtasks": []}')
        owner = OwnerAgent(llm, state)

        result = owner.run_integration_test(task)

        assert result.passed is True
        assert "no explicit integration deliverables" in result.message.lower()

    def test_resolves_bare_task_deliverable_to_unique_nested_file(self, tmp_path):
        state = TaskState()
        state.set_persistence(InMemoryPersistence())
        task = state.create_task("Build API", project_dir=str(tmp_path))
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
        state._persistence.save_task_contract({
            "contract_id": "contract-main",
            "task_id": task.task_id,
            "level": "task",
            "expected_deliverables": [
                {"artifact_type": "api_service_source", "path_hint": "main.py", "required": True}
            ],
            "project_dir": str(tmp_path),
        })
        owner = OwnerAgent(MockLLMGateway('{"subtasks": []}'), state)

        result = owner.run_integration_test(task)

        assert result.passed is True


class FakePersistence:
    def __init__(self, acceptance_records=None, artifact_records=None, jobs_by_subtask=None):
        self._acceptance_records = acceptance_records or []
        self._artifact_records = artifact_records or []
        self._jobs_by_subtask = jobs_by_subtask or {}

    def get_acceptance_records(self, task_id):
        return list(self._acceptance_records)

    def get_artifact_records(self, task_id):
        return list(self._artifact_records)

    def get_jobs_by_subtask(self, subtask_id):
        return list(self._jobs_by_subtask.get(subtask_id, []))


class TestWaveAcceptanceContext:
    def test_collect_accepted_artifacts_strips_large_tool_metadata(self):
        state = TaskState()
        task = state.create_task("Build app", project_dir="/tmp/demo")
        state.set_persistence(FakePersistence(
            artifact_records=[
                {
                    "artifact_id": "art-1",
                    "task_id": task.task_id,
                    "subtask_id": "st-1",
                    "wave_number": 1,
                    "name": "main.py",
                    "status": "accepted",
                    "content_ref": "/tmp/demo/main.py",
                    "metadata": {
                        "canonical_subtask_id": "st-1",
                        "file_size": "1 KB",
                        "normalized_content_ref": "/tmp/demo/main.py",
                        "tool_calls": [{"huge": "x" * 1000}],
                        "tool_results": [{"huge": "y" * 1000}],
                    },
                }
            ]
        ))
        owner = OwnerAgent(MockLLMGateway("{}"), state)

        artifacts = owner._collect_accepted_artifacts(task)

        assert artifacts[0]["metadata"] == {
            "canonical_subtask_id": "st-1",
            "file_size": "1 KB",
            "normalized_content_ref": "/tmp/demo/main.py",
        }

    def test_includes_prior_approved_wave_outputs_as_available_inputs(self):
        state = TaskState()
        state.set_persistence(FakePersistence(
            acceptance_records=[
                {
                    "level": "wave",
                    "decision": "approve",
                    "judge_passed": True,
                    "wave_number": 1,
                }
            ]
        ))
        task = state.create_task("Build deployable app", project_dir="/tmp/demo")

        st1 = state.add_subtask(task.task_id, "Create backend app", "deepseek")
        st1.wave_number = 1
        st1.status = JobStatus.COMPLETED
        st1.output_file = "/tmp/demo/main.py"

        st2 = state.add_subtask(task.task_id, "Create requirements", "deepseek")
        st2.wave_number = 1
        st2.status = JobStatus.COMPLETED
        st2.output_file = "/tmp/demo/requirements.txt"

        st3 = state.add_subtask(task.task_id, "Create Dockerfile", "minimax", dependencies=[st1.subtask_id, st2.subtask_id])
        st3.wave_number = 2
        st3.status = JobStatus.COMPLETED
        st3.output_file = "/tmp/demo/Dockerfile.fastapi"

        llm = MockLLMGateway('{"passed": true, "feedback": "", "action": "approve"}')
        owner = OwnerAgent(llm, state)

        context = owner._build_wave_acceptance_context(task, 2)

        assert "Prior approved waves:" in context
        assert "1" in context
        assert "/tmp/demo/main.py" in context
        assert "/tmp/demo/requirements.txt" in context
        assert f"{st1.subtask_id}: wave=1, status=completed, output_file=/tmp/demo/main.py" in context
        assert "Do not fail simply because the current wave depends on files that already exist from earlier approved waves." in context

    def test_marks_future_wave_subtasks_as_out_of_scope(self):
        state = TaskState()
        task = state.create_task("Build expense app with CSV import and dashboard", project_dir="/tmp/demo")

        current = state.add_subtask(task.task_id, "Implement Expense CRUD API endpoints", "deepseek")
        current.wave_number = 3
        current.status = JobStatus.COMPLETED
        current.output_file = "/tmp/demo/app/routers/expenses.py"

        future_csv = state.add_subtask(task.task_id, "Implement CSV import with row-level validation", "deepseek")
        future_csv.wave_number = 4
        future_dashboard = state.add_subtask(task.task_id, "Implement dashboard summary endpoints", "deepseek")
        future_dashboard.wave_number = 4

        llm = MockLLMGateway('{"passed": true, "feedback": "", "action": "approve"}')
        owner = OwnerAgent(llm, state)

        context = owner._build_wave_acceptance_context(task, 3)

        assert "Overall task description (context only; do not treat as this wave's acceptance checklist)" in context
        assert "Future wave subtasks (not due in this wave; do not fail the current wave for missing these):" in context
        assert f"Wave 4 {future_csv.subtask_id}: Implement CSV import with row-level validation" in context
        assert f"Wave 4 {future_dashboard.subtask_id}: Implement dashboard summary endpoints" in context
        assert "Do not fail because a feature appears only in the Future wave subtasks section." in context

    def test_includes_current_wave_artifact_records_for_multi_file_delivery(self):
        state = TaskState()
        state.set_persistence(FakePersistence(
            acceptance_records=[
                {
                    "level": "wave",
                    "decision": "approve",
                    "judge_passed": True,
                    "wave_number": 1,
                }
            ],
            artifact_records=[
                {
                    "wave_number": 2,
                    "name": "Dockerfile",
                    "content_ref": "/tmp/demo/Dockerfile",
                },
                {
                    "wave_number": 2,
                    "name": "Dockerfile.nginx",
                    "content_ref": "/tmp/demo/Dockerfile.nginx",
                },
                {
                    "wave_number": 2,
                    "name": "docker-compose.yml",
                    "content_ref": "/tmp/demo/docker-compose.yml",
                },
            ],
        ))
        task = state.create_task("Build deployable app", project_dir="/tmp/demo")

        st1 = state.add_subtask(task.task_id, "Create backend app", "deepseek")
        st1.wave_number = 1
        st1.status = JobStatus.COMPLETED
        st1.output_file = "/tmp/demo/main.py"

        st2 = state.add_subtask(task.task_id, "Create deployment bundle", "minimax", dependencies=[st1.subtask_id])
        st2.wave_number = 2
        st2.status = JobStatus.COMPLETED
        st2.output_file = "/tmp/demo/Dockerfile"

        llm = MockLLMGateway('{"passed": true, "feedback": "", "action": "approve"}')
        owner = OwnerAgent(llm, state)

        context = owner._build_wave_acceptance_context(task, 2)

        assert "Current wave artifact records:" in context
        assert "Dockerfile -> /tmp/demo/Dockerfile" in context
        assert "Dockerfile.nginx -> /tmp/demo/Dockerfile.nginx" in context
        assert "docker-compose.yml -> /tmp/demo/docker-compose.yml" in context

    def test_omits_deleted_current_wave_artifact_records(self, tmp_path):
        app_main = tmp_path / "app" / "main.py"
        app_main.parent.mkdir()
        app_main.write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")

        deleted_run = tmp_path / "run.py"
        state = TaskState()
        state.set_persistence(FakePersistence(
            artifact_records=[
                {
                    "wave_number": 1,
                    "name": "run.py",
                    "content_ref": str(deleted_run),
                    "status": "accepted",
                },
                {
                    "wave_number": 1,
                    "name": "main.py",
                    "content_ref": str(app_main),
                    "status": "accepted",
                },
            ],
        ))
        task = state.create_task("Build app without run.py", project_dir=str(tmp_path))
        subtask = state.add_subtask(task.task_id, "Create app skeleton", "deepseek")
        subtask.wave_number = 1
        subtask.status = JobStatus.COMPLETED
        subtask.output_file = str(app_main)

        owner = OwnerAgent(MockLLMGateway('{"passed": true, "feedback": "", "action": "approve"}'), state)

        context = owner._build_wave_acceptance_context(task, 1)

        assert f"main.py -> {app_main}" in context
        assert "run.py ->" not in context

    def test_includes_project_tree_snapshot_for_scaffolding_waves(self, tmp_path):
        project_dir = tmp_path / "demo"
        (project_dir / "app").mkdir(parents=True)
        (project_dir / "nginx").mkdir()
        (project_dir / "static" / "css").mkdir(parents=True)
        (project_dir / "app" / "requirements.txt").write_text("fastapi\n")
        (project_dir / "nginx" / "nginx.conf").write_text("events {}\n")

        state = TaskState()
        task = state.create_task("Build deployable app", project_dir=str(project_dir))

        st1 = state.add_subtask(task.task_id, "Create project directory structure", "minimax")
        st1.wave_number = 1
        st1.status = JobStatus.COMPLETED
        st1.output_file = str(project_dir / "app" / "requirements.txt")

        llm = MockLLMGateway('{"passed": true, "feedback": "", "action": "approve"}')
        owner = OwnerAgent(llm, state)

        context = owner._build_wave_acceptance_context(task, 1)

        assert "Current project tree snapshot:" in context
        assert "app/" in context
        assert "nginx/" in context
        assert "static/" in context
        assert "static/css/" in context
        assert "Do not fail simply because the current wave created a few coherent scaffolding files in addition to its main deliverable." in context


class TestStructuredOwnerDecision:
    def test_accept_subtask_attaches_owner_session_and_context(self):
        state = TaskState()
        task = state.create_task("Build backend")
        subtask = state.add_subtask(task.task_id, "Implement API", "deepseek")
        job = Job.new(subtask, agent_id="deepseek")
        job.result = "Created `app.py` and implemented endpoint"

        llm = MockLLMGateway('{"passed": true, "feedback": "", "action": "approve"}')
        owner = OwnerAgent(llm, state)

        result = owner.accept_subtask(job)

        assert result.owner_session_id is not None
        assert result.root_cause_scope == "current_subtask"
        assert result.recommended_action == "approve"
        assert "Owner Session ID:" in llm.calls[0]["message"]
        assert "Recent Acceptance Records:" in llm.calls[0]["message"]

    def test_accept_subtask_escalates_ancillary_only_output_to_reassign(self):
        state = TaskState()
        task = state.create_task("Build backend")
        subtask = state.add_subtask(task.task_id, "Implement API", "deepseek")
        job = Job.new(subtask, agent_id="deepseek")
        job.result = "Created README placeholder and extra notes only"

        llm = MockLLMGateway('{"passed": false, "feedback": "Not sufficient", "action": "fix"}')
        owner = OwnerAgent(llm, state)

        result = owner.accept_subtask(job)

        assert result.recommended_action == "reassign"
        assert result.root_cause_scope == "current_wave"
        assert "ancillary_only_output" in result.failed_checks

    def test_accept_subtask_does_not_treat_css_placeholder_states_as_ancillary_only(self):
        state = TaskState()
        task = state.create_task("Build frontend styles")
        subtask = state.add_subtask(task.task_id, "Create styles.css", "claude")
        job = Job.new(subtask, agent_id="claude")
        job.result = "Created `styles.css` with responsive layout, modal styling, skeleton placeholders, and dashboard cards."

        llm = MockLLMGateway('{"passed": true, "feedback": "", "action": "approve"}')
        owner = OwnerAgent(llm, state)

        result = owner.accept_subtask(job)

        assert result.recommended_action == "approve"
        assert "ancillary_only_output" not in result.failed_checks

    def test_accept_subtask_approves_artifact_contract_even_when_output_mentions_readme(self, tmp_path):
        state = TaskState()
        state.set_persistence(InMemoryPersistence())
        task = state.create_task(
            "Create exactly one file named README.md",
            project_dir=str(tmp_path),
            task_types=["artifact"],
        )
        subtask = state.add_subtask(task.task_id, "Create README.md", "hermes")
        (tmp_path / "README.md").write_text("# UI E2E Simple Artifact\n", encoding="utf-8")
        state._persistence.save_task_contract({
            "contract_id": "contract-readme",
            "task_id": task.task_id,
            "subtask_id": subtask.subtask_id,
            "level": "subtask",
            "expected_deliverables": [
                {"artifact_type": "file", "path_hint": "README.md", "required": True}
            ],
            "project_dir": str(tmp_path),
        })
        job = Job.new(subtask, agent_id="hermes")
        job.result = "Created README.md with the requested title."
        job.output_file = str(tmp_path / "README.md")

        llm = MockLLMGateway('{"passed": false, "feedback": "Only docs", "action": "fix"}')
        owner = OwnerAgent(llm, state)

        result = owner.accept_subtask(job)

        assert result.level2_passed is True
        assert result.recommended_action == "approve"

    def test_accept_subtask_resolves_bare_contract_hint_to_unique_nested_file(self, tmp_path):
        state = TaskState()
        state.set_persistence(InMemoryPersistence())
        task = state.create_task("Create CSV import module", project_dir=str(tmp_path))
        subtask = state.add_subtask(task.task_id, "Create csv_import.py", "deepseek")
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "csv_import.py").write_text("def import_csv():\n    return []\n", encoding="utf-8")
        state._persistence.save_task_contract({
            "contract_id": "contract-csv",
            "task_id": task.task_id,
            "subtask_id": subtask.subtask_id,
            "level": "subtask",
            "expected_deliverables": [
                {"artifact_type": "file", "path_hint": "csv_import.py", "required": True}
            ],
            "project_dir": str(tmp_path),
        })
        job = Job.new(subtask, agent_id="deepseek")
        job.status = JobStatus.COMPLETED
        job.result = "Created app/csv_import.py"
        job.output_file = str(tmp_path / "app" / "csv_import.py")
        owner = OwnerAgent(MockLLMGateway('{"passed": false, "feedback": "Missing csv_import.py", "action": "fix"}'), state)

        result = owner.accept_subtask(job)

        assert result.level2_passed is True
        assert result.recommended_action == "approve"
        assert result.failed_checks == []

    def test_accept_subtask_trusts_contract_file_when_judge_only_missing_write_confirmation(self, tmp_path):
        state = TaskState()
        state.set_persistence(InMemoryPersistence())
        task = state.create_task("Create app.js", project_dir=str(tmp_path))
        subtask = state.add_subtask(task.task_id, "Create app.js", "openclaw")
        (tmp_path / "app.js").write_text("const state = { ready: true };\n", encoding="utf-8")
        state._persistence.save_task_contract({
            "contract_id": "contract-app-js",
            "task_id": task.task_id,
            "subtask_id": subtask.subtask_id,
            "level": "subtask",
            "expected_deliverables": [
                {"artifact_type": "file", "path_hint": "app.js", "required": True}
            ],
            "project_dir": str(tmp_path),
        })
        job = Job.new(subtask, agent_id="openclaw")
        job.status = JobStatus.COMPLETED
        job.result = "I read the existing files to understand the structure before writing app.js."
        job.output_file = str(tmp_path / "app.js")
        owner = OwnerAgent(
            MockLLMGateway(
                '{"passed": false, "feedback": "The agent did not report actually writing the app.js file.", "action": "fix"}'
            ),
            state,
        )

        result = owner.accept_subtask(job)

        assert result.level2_passed is True
        assert result.recommended_action == "approve"
        assert result.failed_checks == []

    def test_structured_decision_scopes_repeated_failures_to_same_subtask_family(self):
        state = TaskState()
        state.set_persistence(InMemoryPersistence())
        task = state.create_task("Build CLI")
        state._persistence.acceptance_records_list[task.task_id] = [
            {"subtask_id": "st-docs", "decision": "reassign"},
            {"subtask_id": "st-docs-v2", "decision": "reassign"},
        ]
        subtask = state.add_subtask(task.task_id, "Create notes_cli.py", "deepseek")
        job = Job.new(subtask, agent_id="deepseek")
        job.result = "Created notes_cli.py with argparse commands."

        owner = OwnerAgent(MockLLMGateway('{"passed": true, "feedback": "", "action": "approve"}'), state)

        decision = owner._make_structured_subtask_decision(task, job)

        assert decision.recommended_action == "approve"
        assert "repeated_failures" not in decision.failed_checks

    def test_accept_subtask_identifies_prior_wave_root_cause(self):
        state = TaskState()
        task = state.create_task("Build backend")
        st1 = state.add_subtask(task.task_id, "Create schema", "claude")
        st1.wave_number = 1
        st2 = state.add_subtask(task.task_id, "Implement API", "deepseek", dependencies=[st1.subtask_id])
        st2.wave_number = 2
        job = Job.new(st2, agent_id="deepseek")
        job.error = "Upstream dependency drift: input artifact from dependency no longer matches contract"

        llm = MockLLMGateway('{"passed": false, "feedback": "Dependency issue", "action": "fix"}')
        owner = OwnerAgent(llm, state)

        result = owner.accept_subtask(job)

        assert result.root_cause_scope == "prior_wave"
        assert result.root_cause_wave == 1
        assert result.recommended_action == "prior_wave_fix"


class TestInferSubtaskDeliverables:
    """NEW-1: concrete path hints + additive deliverables inference."""

    def test_infer_subtask_deliverables_adds_file_hints_for_fastapi_description(self):
        """Generic deliverables (api_service_source) are preserved, and explicit
        file names from the description are added as ``file`` deliverables with path_hint."""
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            "Create a FastAPI REST API with main.py, models.py, and requirements.txt",
            "deepseek",
            project_dir="/tmp/project",
        )

        details = {(d.artifact_type, d.path_hint) for d in deliverables}
        assert ("api_service_source", None) in details, (
            f"Generic api_service_source missing from: {details}"
        )
        assert ("file", "main.py") in details, f"main.py missing from: {details}"
        assert ("file", "models.py") in details, f"models.py missing from: {details}"

        check_types = {c.check_type for c in checks}
        assert "api_source_exists" in check_types, f"api_source_exists missing from: {check_types}"
        assert "file_exists" in check_types, f"file_exists missing from: {check_types}"

    def test_infer_subtask_deliverables_treats_dashboard_endpoints_as_backend(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            "Implement dashboard summary endpoints: total spend and monthly trend.",
            "deepseek",
            project_dir="/tmp/project",
        )

        artifact_types = {item.artifact_type for item in deliverables}
        check_types = {item.check_type for item in checks}
        assert "api_service_source" in artifact_types
        assert "frontend_source" not in artifact_types
        assert "api_source_exists" in check_types
        assert "frontend_source_exists" not in check_types

    def test_infer_subtask_deliverables_treats_pytest_dashboard_coverage_as_tests(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            "Write pytest tests covering CRUD operations, filtering, CSV import, dashboard aggregation, and GET / HTML.",
            "hermes",
            project_dir="/tmp/project",
        )

        artifact_types = {item.artifact_type for item in deliverables}
        check_types = {item.check_type for item in checks}
        assert "test_suite" in artifact_types
        assert "frontend_source" not in artifact_types
        assert "test_suite_exists" in check_types
        assert "frontend_source_exists" not in check_types

    def test_extract_path_hints_ignores_common_non_file_words(self):
        """Common non-file words (python3, json, api, etc.) should not be extracted."""
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        hints = owner._extract_path_hints(
            "Use python3 to build a JSON API service in main.py and app/config.yaml",
            project_dir="/tmp/project",
        )

        assert "python3" not in hints, f"python3 should be ignored: {hints}"
        assert "json" not in hints, f"json should be ignored: {hints}"
        assert "api" not in hints, f"api should be ignored: {hints}"
        assert "main.py" in hints, f"main.py should be extracted: {hints}"
        assert "app/config.yaml" in hints, f"app/config.yaml should be extracted: {hints}"

    def test_extract_path_hints_rejects_module_names_and_dedupes_bare_alias(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        hints = owner._extract_path_hints(
            "Create src/server.py using the standard library http.server module.",
            project_dir="/tmp/project",
        )

        assert hints == ["src/server.py"]

    def test_infer_subtask_deliverables_does_not_add_module_name_contract_file(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            "Create src/server.py using the standard library http.server module.",
            "deepseek",
            project_dir="/tmp/project",
        )

        file_hints = [
            d.path_hint
            for d in deliverables
            if d.artifact_type == "file"
        ]
        assert file_hints == ["src/server.py"]
        assert "file_exists" in {c.check_type for c in checks}

    def test_infer_subtask_deliverables_ignores_validation_manifest_references(self):
        from across_agents_assistant.task_manager.orchestration.requirements import extract_forbidden_path_hints

        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)
        description = (
            "Create cli/quality-check.mjs as a standalone validation script. "
            "It must check the exact seven-file manifest (README.md, web/index.html, "
            "web/styles.css, web/app.js, api/server.mjs, cli/quality-check.mjs, "
            "tests/e2e-smoke.mjs), validate security/privacy constraints with no external "
            "packages, and must not require package.json, node_modules, or files outside the manifest."
        )

        deliverables, checks = owner._infer_subtask_deliverables(
            description,
            "deepseek",
            project_dir="/tmp/project",
        )

        file_hints = [
            d.path_hint
            for d in deliverables
            if d.artifact_type == "file"
        ]
        assert file_hints == ["cli/quality-check.mjs"]
        assert "file_exists" in {c.check_type for c in checks}
        forbidden = extract_forbidden_path_hints(description)
        assert "cli/quality-check.mjs" not in forbidden
        assert "web/app.js" not in forbidden
        assert "package.json" in forbidden

    def test_infer_subtask_deliverables_ignores_smoke_test_dependency_references(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, _checks = owner._infer_subtask_deliverables(
            (
                "Implement tests/e2e-smoke.mjs that: starts api/server.mjs on an available "
                "local port, verifies /health, runs cli/quality-check.mjs as a child process, "
                "and exits non-zero on failure."
            ),
            "deepseek",
            project_dir="/tmp/project",
        )

        file_hints = [
            d.path_hint
            for d in deliverables
            if d.artifact_type == "file"
        ]
        assert file_hints == ["tests/e2e-smoke.mjs"]

    def test_infer_subtask_deliverables_ignores_readme_command_references(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, _checks = owner._infer_subtask_deliverables(
            (
                "Create README.md explaining how to: open web/index.html directly, "
                "run API server with node api/server.mjs, run node cli/quality-check.mjs, "
                "and run node tests/e2e-smoke.mjs."
            ),
            "claude",
            project_dir="/tmp/project",
        )

        file_hints = [
            d.path_hint
            for d in deliverables
            if d.artifact_type == "file"
        ]
        assert file_hints == ["README.md"]

    def test_infer_subtask_deliverables_keeps_app_js_with_without_fetch_constraint(self):
        from across_agents_assistant.task_manager.orchestration.requirements import extract_forbidden_path_hints

        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)
        description = (
            "Create web/app.js with animated canvas, localStorage persistence, "
            "offline/static-preview mode for file:// protocol using local fixture data without fetch, "
            "API calls only when protocol is http: or https:, and catch failures without console.error."
        )

        deliverables, _checks = owner._infer_subtask_deliverables(
            description,
            "openclaw",
            project_dir="/tmp/project",
        )

        file_hints = [
            d.path_hint
            for d in deliverables
            if d.artifact_type == "file"
        ]
        assert file_hints == ["web/app.js"]
        assert "web/app.js" not in extract_forbidden_path_hints(description)

    def test_infer_subtask_deliverables_does_not_make_planning_review_a_file(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            (
                "Review requirements and plan cross-agent implementation. Define agent capability "
                "matrix, API contracts, and data flow. Ensure all 7 required files are accounted "
                "for in the architecture."
            ),
            "claude",
            project_dir="/tmp/project",
        )

        assert deliverables == []
        assert {check.check_type for check in checks} == {"planning_review_completed"}

    def test_infer_subtask_deliverables_does_not_require_file_for_directory_structure(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            "Create project directory structure with proper folders for app, static, templates, tests, and docs.", "openclaw",
            project_dir="/tmp/project",
        )

        assert deliverables == []
        assert {c.check_type for c in checks} == {"project_structure_exists"}

    def test_infer_subtask_deliverables_does_not_make_structure_tests_dir_a_test_suite(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            "Create project directory structure: app/__init__.py, app/main.py, static/css/, static/js/, tests/, docs/", "openclaw",
            project_dir="/tmp/project",
        )

        artifact_types = {item.artifact_type for item in deliverables}
        file_hints = {item.path_hint for item in deliverables if item.artifact_type == "file"}
        assert "test_suite" not in artifact_types
        assert "app/__init__.py" in file_hints
        assert "app/main.py" in file_hints
        assert "test_suite_exists" not in {c.check_type for c in checks}

    def test_infer_subtask_deliverables_does_not_make_requirements_pytest_a_test_suite(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            "Create requirements.txt with: fastapi, uvicorn, python-multipart, pytest, pytest-asyncio.", "openclaw",
            project_dir="/tmp/project",
        )

        artifact_types = {item.artifact_type for item in deliverables}
        assert "test_suite" not in artifact_types
        assert "api_service_source" not in artifact_types
        assert ("file", "requirements.txt") in {(d.artifact_type, d.path_hint) for d in deliverables}
        assert "test_suite_exists" not in {c.check_type for c in checks}

    def test_infer_subtask_deliverables_does_not_make_sample_csv_a_test_suite(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            "Create tests/sample.csv with sample expense data for testing CSV import.", "openclaw",
            project_dir="/tmp/project",
        )

        artifact_types = {item.artifact_type for item in deliverables}
        assert "test_suite" not in artifact_types
        assert ("file", "tests/sample.csv") in {(d.artifact_type, d.path_hint) for d in deliverables}
        assert "test_suite_exists" not in {c.check_type for c in checks}

    def test_infer_subtask_deliverables_does_not_make_testing_doc_a_test_suite(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            "Create docs/TESTING.md: how to run tests, expected test output, and manual testing checklist.", "openclaw",
            project_dir="/tmp/project",
        )

        artifact_types = {item.artifact_type for item in deliverables}
        assert "test_suite" not in artifact_types
        assert ("file", "docs/TESTING.md") in {(d.artifact_type, d.path_hint) for d in deliverables}
        assert "test_suite_exists" not in {c.check_type for c in checks}

    def test_infer_subtask_deliverables_does_not_infer_dockerfile_from_minimax_agent_alone(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            "Implement the todo CLI behaviors add, list, and complete in src/todo.py.",
            "minimax",
            project_dir="/tmp/project",
        )

        details = {(d.artifact_type, d.path_hint) for d in deliverables}
        assert ("dockerfile", None) not in details
        assert "container_config_exists" not in {c.check_type for c in checks}
        assert ("file", "src/todo.py") in details

    def test_infer_subtask_deliverables_does_not_treat_ui_container_as_docker(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            (
                "Build index.html with a full semantic DOM skeleton: "
                "canvas animation container, agent capability cards, and route evidence panel."
            ),
            "deepseek",
            project_dir="/tmp/project",
        )

        details = {(d.artifact_type, d.path_hint) for d in deliverables}
        assert ("dockerfile", None) not in details
        assert "container_config_exists" not in {c.check_type for c in checks}
        assert ("file", "index.html") in details

    def test_infer_subtask_deliverables_ignores_backend_label_in_static_app_js_ui_copy(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            (
                "Create app.js implementing dashboard behavior for a static web app. "
                "Show agent cards with DeepSeek: Backend API, Data modeling, Code review."
            ),
            "claude",
            project_dir="/tmp/project",
        )

        details = {(d.artifact_type, d.path_hint) for d in deliverables}
        assert ("api_service_source", None) not in details
        assert ("file", "app.js") in details
        assert "api_source_exists" not in {c.check_type for c in checks}

    def test_infer_subtask_deliverables_ignores_runtime_json_store_paths(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            "Implement todo_cli.py with JSON file persistence (store in ~/.todo.json or local todo.json).",
            "deepseek",
            project_dir="/tmp/project",
        )

        file_hints = {
            d.path_hint
            for d in deliverables
            if d.artifact_type == "file"
        }
        assert "todo_cli.py" in file_hints
        assert "todo.json" not in file_hints
        assert "~/.todo.json" not in file_hints

    def test_requirement_manifest_ignores_default_runtime_json_file(self):
        from across_agents_assistant.task_manager.orchestration.requirements import extract_requirement_manifest

        manifest = extract_requirement_manifest(
            "task-1",
            (
                "Build a Python todo CLI in todo_cli.py with add, list, and complete commands. "
                "Store data in the JSON file path from the TODO_FILE environment variable, "
                "defaulting to todo.json in the project directory. "
                "Add pytest tests in tests/test_todo_cli.py."
            ),
            project_dir="/tmp/project",
        )

        paths = {item.path_hint for item in manifest.deliverables}
        assert "todo_cli.py" in paths
        assert "tests/test_todo_cli.py" in paths
        assert "todo.json" not in paths

    def test_infer_subtask_deliverables_keeps_explicit_json_deliverable(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            "Create config.json with default application settings.",
            "deepseek",
            project_dir="/tmp/project",
        )

        file_hints = {
            d.path_hint
            for d in deliverables
            if d.artifact_type == "file"
        }
        assert "config.json" in file_hints

    def test_sanitize_subtask_contract_specs_removes_runtime_json_from_llm_deliverables(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._sanitize_subtask_contract_specs(
            description="Implement todo_cli.py with JSON file persistence (store in local todo.json).",
            agent_id="deepseek",
            deliverables=[
                DeliverableSpec(artifact_type="file", required=True, path_hint="todo_cli.py", description="source"),
                DeliverableSpec(artifact_type="file", required=True, path_hint="todo.json", description="runtime data"),
            ],
            checks=[AcceptanceCheck(check_type="file_exists", description="files exist", required=True)],
        )

        file_hints = {
            d.path_hint
            for d in deliverables
            if d.artifact_type == "file"
        }
        assert "todo_cli.py" in file_hints
        assert "todo.json" not in file_hints

    def test_sanitize_subtask_contract_specs_removes_auxiliary_init_placeholder(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._sanitize_subtask_contract_specs(
            description="Create the project directory and an empty tests directory. Also create a placeholder __init__.py in tests.",
            agent_id="openclaw",
            deliverables=[
                DeliverableSpec(artifact_type="file", required=True, path_hint="__init__.py", description="placeholder"),
            ],
            checks=[AcceptanceCheck(check_type="file_exists", description="files exist", required=True)],
        )

        assert deliverables == []

    def test_functional_decomposition_message_warns_against_runtime_deliverables(self, fake_task_state):
        owner = OwnerAgent(llm_gateway=MagicMock(), state=fake_task_state)
        task = fake_task_state.create_task(
            "Build a todo CLI using TODO_FILE with default todo.json.",
            task_types=["functional"],
        )

        message = owner._build_decomposition_message(task, context={"task_types": ["functional"]})

        assert "FUNCTIONAL task" in message
        assert "todo.json" in message
        assert "not required deliverables" in message
        assert ".pytest_cache" in message
        assert "duplicate equivalent files" in message

    def test_functional_fallback_decomposition_constrains_static_paths_and_pytest_backend(self, fake_task_state):
        owner = OwnerAgent(llm_gateway=MagicMock(), state=fake_task_state)
        task = fake_task_state.create_task(
            "Build an expense tracking FastAPI web app with frontend and pytest coverage.",
            task_types=["functional"],
        )

        subtasks = owner._build_deterministic_fallback_decomposition(task, AVAILABLE_AGENT_IDS)
        by_id = {item["id"]: item for item in subtasks}

        assert "root static/" in by_id["project_skeleton"]["description"]
        assert "app/static/styles.css" in by_id["frontend_ui"]["description"]
        assert "app/static/css" in by_id["frontend_ui"]["description"]
        assert "FastAPI TestClient" in by_id["pytest_suite"]["description"]
        assert "anyio_backend" in by_id["pytest_suite"]["description"]
        assert "trio" in by_id["pytest_suite"]["description"]
        assert "runnable with declared dependencies" in by_id["pytest_suite"]["acceptance_checks"][0]["description"]

    def test_decomposition_message_enforces_limited_documentation_scope(self, fake_task_state):
        owner = OwnerAgent(llm_gateway=MagicMock(), state=fake_task_state)
        task = fake_task_state.create_task(
            "实现 Web 应用。文档只需要 README.md 和 TESTING.md，不要生成大量额外文档。",
            task_types=["functional", "artifact"],
        )

        message = owner._build_decomposition_message(task, context={"task_types": ["functional", "artifact"]})

        assert "Documentation scope" in message
        assert "README.md, TESTING.md" in message
        assert "Do not create SPEC.md" in message

    def test_exact_file_set_limits_documentation_planning_scope(self, fake_task_state):
        owner = OwnerAgent(llm_gateway=MagicMock(), state=fake_task_state)
        task = fake_task_state.create_task(
            (
                "Deliver exactly these four root files and no package manager output: "
                "index.html, styles.css, app.js, README.md. "
                "No node_modules, no generated assets, no frameworks."
            ),
            task_types=["functional", "artifact"],
        )

        message = owner._build_decomposition_message(task, context={"task_types": ["functional", "artifact"]})
        suffix = owner._build_global_subtask_constraint_suffix(task)

        assert "Documentation scope" in message
        assert "README.md" in message
        assert "Do not create SPEC.md" in message
        assert "Forbidden files: index.html" not in suffix

    def test_sanitize_subtask_contract_specs_removes_container_requirements_without_container_intent(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._sanitize_subtask_contract_specs(
            description="Implement the todo CLI in src/todo.py and tests/test_todo.py.",
            agent_id="minimax",
            deliverables=[
                DeliverableSpec(artifact_type="dockerfile", required=True, description="hallucinated"),
                DeliverableSpec(artifact_type="file", required=True, path_hint="src/todo.py", description="real file"),
            ],
            checks=[
                AcceptanceCheck(check_type="container_config_exists", description="hallucinated", required=True),
                AcceptanceCheck(check_type="file_exists", description="real file", required=True),
            ],
        )

        assert ("dockerfile", None) not in {(d.artifact_type, d.path_hint) for d in deliverables}
        assert "container_config_exists" not in {c.check_type for c in checks}

    def test_sanitize_subtask_contract_specs_removes_api_requirements_for_docs_only_task(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._sanitize_subtask_contract_specs(
            description="Create README.md with Usage and Verification sections. Do not create Dockerfile or setup.py.",
            agent_id="minimax",
            deliverables=[
                DeliverableSpec(artifact_type="api_service_source", required=True, description="hallucinated"),
                DeliverableSpec(artifact_type="file", required=True, path_hint="README.md", description="real file"),
            ],
            checks=[
                AcceptanceCheck(check_type="api_source_exists", description="hallucinated", required=True),
                AcceptanceCheck(check_type="file_exists", description="real file", required=True),
            ],
        )

        assert ("api_service_source", None) not in {(d.artifact_type, d.path_hint) for d in deliverables}
        assert "api_source_exists" not in {c.check_type for c in checks}

    def test_sanitize_subtask_contract_specs_removes_api_requirements_for_cli_task(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._sanitize_subtask_contract_specs(
            description="Implement todo_cli.py: a Python CLI using argparse with add/list/complete commands.",
            agent_id="deepseek",
            deliverables=[
                DeliverableSpec(artifact_type="api_service_source", required=True, description="hallucinated"),
                DeliverableSpec(artifact_type="file", required=True, path_hint="todo_cli.py", description="real file"),
            ],
            checks=[
                AcceptanceCheck(check_type="api_source_exists", description="hallucinated", required=True),
                AcceptanceCheck(check_type="file_exists", description="real file", required=True),
            ],
        )

        assert ("api_service_source", None) not in {(d.artifact_type, d.path_hint) for d in deliverables}
        assert "api_source_exists" not in {c.check_type for c in checks}

    def test_sanitize_ignores_backend_word_inside_project_dir_constraint(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._sanitize_subtask_contract_specs(
            description=(
                "Design the todo CLI architecture and JSON persistence notes.\n\n"
                "[CRITICAL] All files MUST be written to this directory: "
                "/tmp/across-agents-backend-e2e/project\n"
                "Do NOT create files in any other location."
            ),
            agent_id="claude",
            deliverables=[DeliverableSpec(artifact_type="api_service_source", required=True, description="hallucinated")],
            checks=[AcceptanceCheck(check_type="api_source_exists", description="hallucinated", required=True)],
        )

        assert deliverables == []
        assert checks == []

    def test_sanitize_ignores_backend_word_inside_inline_absolute_project_path(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._sanitize_subtask_contract_specs(
            description=(
                "Create README.md in /tmp/across-agents-backend-e2e/project. "
                "Do not create any other files."
            ),
            agent_id="minimax",
            deliverables=[
                DeliverableSpec(artifact_type="api_service_source", required=True, description="hallucinated"),
                DeliverableSpec(artifact_type="file", required=True, path_hint="README.md", description="real file"),
            ],
            checks=[
                AcceptanceCheck(check_type="api_source_exists", description="hallucinated", required=True),
                AcceptanceCheck(check_type="file_exists", description="real file", required=True),
            ],
        )

        assert {(d.artifact_type, d.path_hint) for d in deliverables} == {("file", "README.md")}
        assert {c.check_type for c in checks} == {"file_exists"}

    def test_sanitize_ignores_backend_label_inside_static_app_js_ui_copy(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._sanitize_subtask_contract_specs(
            description=(
                "Create app.js implementing dashboard behavior for a static web app. "
                "Show Local Agent cards and Cloud LLM cards, including DeepSeek: Backend API, "
                "Data modeling, Code review.\n\n"
                "[CRITICAL] All files MUST be written to this directory: /tmp/project\n"
                "Do NOT create files in any other location."
            ),
            agent_id="claude",
            deliverables=[
                DeliverableSpec(artifact_type="api_service_source", required=True, description="hallucinated"),
                DeliverableSpec(artifact_type="file", required=True, path_hint="app.js", description="real file"),
            ],
            checks=[
                AcceptanceCheck(check_type="api_source_exists", description="hallucinated", required=True),
                AcceptanceCheck(check_type="file_exists", description="real file", required=True),
            ],
        )

        assert {(d.artifact_type, d.path_hint) for d in deliverables} == {("file", "app.js")}
        assert {c.check_type for c in checks} == {"file_exists"}

    def test_infer_subtask_deliverables_does_not_require_forbidden_files_or_docker(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        deliverables, checks = owner._infer_subtask_deliverables(
            "Write README.md. Do not create Dockerfile, setup.py, or root __init__.py.",
            "minimax",
            project_dir="/tmp/project",
        )

        details = {(d.artifact_type, d.path_hint) for d in deliverables}
        assert ("file", "README.md") in details
        assert ("dockerfile", None) not in details
        assert ("file", "Dockerfile") not in details
        assert ("file", "setup.py") not in details
        assert ("file", "__init__.py") not in details
        assert "container_config_exists" not in {c.check_type for c in checks}

    def test_decompose_sanitizes_llm_subtask_contract_against_forbidden_request(self, tmp_path):
        state = TaskState()
        state.set_persistence(InMemoryPersistence())
        task = state.create_task(
            "Create only README.md. Do not create Dockerfile, setup.py, or root __init__.py.",
            project_dir=str(tmp_path),
            task_types=["artifact"],
        )
        llm_response = """{"subtasks": [
            {
              "id": "docs",
              "description": "Write README.md. Do not create Dockerfile, setup.py, or root __init__.py.",
              "agent": "minimax",
              "priority": 1,
              "dependencies": [],
              "deliverables": [
                {"artifact_type": "dockerfile", "required": true, "description": "hallucinated"},
                {"artifact_type": "file", "required": true, "path_hint": "README.md"},
                {"artifact_type": "file", "required": true, "path_hint": "setup.py"},
                {"artifact_type": "file", "required": true, "path_hint": "__init__.py"}
              ],
              "acceptance_checks": [
                {"check_type": "container_config_exists", "required": true},
                {"check_type": "file_exists", "required": true}
              ]
            }
        ]}"""
        owner = OwnerAgent(MockLLMGateway(llm_response), state)
        owner._get_available_agents = _all_agents

        owner.decompose_and_assign(task)

        contracts = [
            c for c in state.get_task_contracts(task.task_id)
            if c.get("level") == "subtask"
        ]
        assert len(contracts) == 1
        expected = {
            (item.get("artifact_type"), item.get("path_hint"))
            for item in contracts[0].get("expected_deliverables", [])
        }
        assert expected == {("file", "README.md")}
        assert {c.get("check_type") for c in contracts[0].get("acceptance_checks", [])} == {"file_exists"}


class TestWaveAcceptanceNormalization:
    def test_accept_wave_structured_approve_overrides_conflicting_llm_fix(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        task = Task.new("wave approval conflict")
        task.owner_session_id = "owner-session-1"
        subtask = SubTask(
            subtask_id="st-1",
            task_id=task.task_id,
            description="Produce calculator module",
            agent_id="deepseek",
            dependencies=[],
        )
        subtask.wave_number = 1
        subtask.status = JobStatus.COMPLETED
        subtask.output_file = "/tmp/project/app/calculator.py"
        task.subtasks.append(subtask)

        owner._llm = MagicMock(return_value=MockLLMResponse("""
{"passed": false, "feedback": "Looks incomplete", "action": "fix"}
"""))

        acceptance = owner.accept_wave(task, 1)

        assert acceptance.recommended_action == "approve"
        assert acceptance.action == "approve"
        assert acceptance.level2_passed is True
        assert acceptance.level2_feedback == "Current wave is approved for downstream consumption."
        assert acceptance.failed_checks == []
        assert acceptance.missing_artifacts == []

    def test_accept_wave_parse_failure_does_not_block_when_deterministic_checks_pass(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        task = Task.new("wave parse fallback")
        task.owner_session_id = "owner-session-parse"
        subtask = SubTask(
            subtask_id="st-1",
            task_id=task.task_id,
            description="Produce API routes",
            agent_id="deepseek",
            dependencies=[],
        )
        subtask.wave_number = 1
        subtask.status = JobStatus.COMPLETED
        subtask.output_file = __file__
        task.subtasks.append(subtask)

        owner._llm = MagicMock(return_value=MockLLMResponse("Looks coherent but not JSON."))

        acceptance = owner.accept_wave(task, 1)

        assert acceptance.recommended_action == "approve"
        assert acceptance.action == "approve"
        assert acceptance.level2_passed is True
        assert "deterministic evidence checks passed" in acceptance.level2_feedback

    def test_accept_wave_structured_reject_forces_fix_and_nonpass(self):
        state = TaskState()
        owner = OwnerAgent(llm_gateway=MagicMock(), state=state)

        task = Task.new("wave reject normalization")
        task.owner_session_id = "owner-session-2"
        subtask = SubTask(
            subtask_id="st-1",
            task_id=task.task_id,
            description="Produce required module",
            agent_id="deepseek",
            dependencies=[],
        )
        subtask.wave_number = 1
        subtask.status = JobStatus.COMPLETED
        task.subtasks.append(subtask)

        owner._subtask_has_delivery_evidence = MagicMock(return_value=False)
        owner._llm = MagicMock(return_value=MockLLMResponse("""
{"passed": true, "feedback": "Looks fine", "action": "approve"}
"""))

        acceptance = owner.accept_wave(task, 1)

        assert acceptance.recommended_action in {"wave_fix", "reassign"}
        assert acceptance.action in {"fix", "reassign"}
        assert acceptance.level2_passed is False

    def test_accept_wave_ignores_quality_remediation_subtasks_as_deliverables(self):
        state = TaskState()
        llm = MockLLMGateway("""
{"passed": true, "feedback": "Wave deliverables are coherent.", "action": "approve"}
""")
        owner = OwnerAgent(llm_gateway=llm, state=state)

        task = Task.new("wave with concurrent final quality remediation")
        task.owner_session_id = "owner-session-3"
        normal_subtask = SubTask(
            subtask_id="st-docs",
            task_id=task.task_id,
            description="Create README.md and TESTING.md",
            agent_id="openclaw",
            dependencies=[],
        )
        normal_subtask.wave_number = 5
        normal_subtask.status = JobStatus.COMPLETED
        normal_subtask.output_file = "/tmp/project/README.md"

        quality_probe_subtask = SubTask(
            subtask_id="st-quality-abc123ef",
            task_id=task.task_id,
            description="Quality remediation attempt: fix failing pytest probe",
            agent_id="claude",
            dependencies=[],
        )
        quality_probe_subtask.wave_number = 5
        quality_probe_subtask.status = JobStatus.COMPLETED

        task.subtasks.extend([normal_subtask, quality_probe_subtask])

        acceptance = owner.accept_wave(task, 5)

        assert acceptance.recommended_action == "approve"
        assert acceptance.action == "approve"
        assert acceptance.level2_passed is True
        assert "st-quality-abc123ef" not in llm.calls[0]["message"]

    def test_accept_wave_counts_canonical_fix_artifact_as_original_deliverable(self, tmp_path):
        index_path = tmp_path / "static" / "index.html"
        index_path.parent.mkdir()
        index_path.write_text("<!doctype html><h1>Expense app</h1>", encoding="utf-8")

        state = TaskState()
        state.set_persistence(FakePersistence(
            artifact_records=[
                {
                    "subtask_id": "st-html-fix-1",
                    "wave_number": 6,
                    "status": "accepted",
                    "content_ref": str(index_path),
                    "metadata": {"canonical_subtask_id": "st-html"},
                }
            ]
        ))
        llm = MockLLMGateway("""
{"passed": true, "feedback": "Wave deliverables are coherent.", "action": "approve"}
""")
        owner = OwnerAgent(llm_gateway=llm, state=state)

        task = Task.new("wave with fixed HTML deliverable")
        task.project_dir = str(tmp_path)
        task.owner_session_id = "owner-session-4"
        original_subtask = SubTask(
            subtask_id="st-html",
            task_id=task.task_id,
            description="Create frontend index.html structure",
            agent_id="hermes",
            dependencies=[],
        )
        original_subtask.wave_number = 6
        original_subtask.status = JobStatus.COMPLETED
        task.subtasks.append(original_subtask)

        acceptance = owner.accept_wave(task, 6)

        assert acceptance.recommended_action == "approve"
        assert acceptance.action == "approve"
        assert acceptance.level2_passed is True
        assert acceptance.missing_artifacts == []

    def test_accept_wave_uses_completed_job_summary_file_refs_as_delivery_evidence(self, tmp_path):
        model_path = tmp_path / "app" / "models" / "category.py"
        model_path.parent.mkdir(parents=True)
        model_path.write_text("class Category: pass\n", encoding="utf-8")

        state = TaskState()
        state.set_persistence(FakePersistence(
            jobs_by_subtask={
                "st-models": [
                    {
                        "status": "completed",
                        "result": "Created app/models/category.py and app/schemas/category.py for the data model.",
                    }
                ]
            }
        ))
        owner = OwnerAgent(
            llm_gateway=MockLLMGateway('{"passed": true, "feedback": "", "action": "approve"}'),
            state=state,
        )

        task = Task.new("wave with multi-file model output")
        task.project_dir = str(tmp_path)
        task.owner_session_id = "owner-session-5"
        subtask = SubTask(
            subtask_id="st-models",
            task_id=task.task_id,
            description="Create SQLAlchemy models and Pydantic schemas",
            agent_id="claude",
            dependencies=[],
        )
        subtask.wave_number = 1
        subtask.status = JobStatus.COMPLETED
        task.subtasks.append(subtask)

        acceptance = owner.accept_wave(task, 1)

        assert acceptance.recommended_action == "approve"
        assert acceptance.action == "approve"
        assert acceptance.level2_passed is True
        assert acceptance.missing_artifacts == []


class InMemoryPersistence:
    def __init__(self):
        self.tasks = {}
        self.subtasks = {}
        self.waves = {}
        self.contracts = {}
        self.manifests = {}
        self.delivery_contracts = {}
        self.artifact_records_list = {}
        self.acceptance_records_list = {}

    def save_task(self, task):
        self.tasks[task["task_id"]] = dict(task)

    def save_subtask(self, subtask):
        tid = subtask["task_id"]
        self.subtasks.setdefault(tid, []).append(dict(subtask))

    def save_wave(self, wave):
        tid = wave["task_id"]
        self.waves.setdefault(tid, []).append(dict(wave))

    def save_task_contract(self, contract):
        self.contracts[contract["contract_id"]] = dict(contract)

    def get_task_contracts(self, task_id):
        return [c for c in self.contracts.values() if c["task_id"] == task_id]

    def save_requirement_manifest(self, manifest):
        self.manifests[manifest["task_id"]] = dict(manifest)

    def get_requirement_manifest(self, task_id):
        return self.manifests.get(task_id)

    def save_delivery_contract(self, contract):
        self.delivery_contracts[contract["task_id"]] = dict(contract)

    def get_delivery_contract(self, task_id):
        return self.delivery_contracts.get(task_id)

    def get_acceptance_records(self, task_id):
        return self.acceptance_records_list.get(task_id, [])

    def get_artifact_records(self, task_id):
        return self.artifact_records_list.get(task_id, [])


@pytest.fixture
def fake_task_state():
    from across_agents_assistant.task_manager.state import TaskState
    state = TaskState()
    state.set_persistence(InMemoryPersistence())
    return state


@pytest.fixture
def fake_owner_agent(fake_task_state):
    from across_agents_assistant.task_manager.orchestration.owner_agent import OwnerAgent
    owner = OwnerAgent(lambda **kwargs: type("Resp", (), {"text": '{"subtasks":[]}'})(), fake_task_state)
    owner._get_available_agents = _all_agents
    return owner


class TestSubtaskAcceptanceOverrides:
    def test_satisfied_functional_contract_is_not_rejected_for_missing_source_paste(self, fake_task_state, tmp_path):
        app_dir = tmp_path / "app" / "routers"
        app_dir.mkdir(parents=True)
        (app_dir / "expenses.py").write_text("from fastapi import APIRouter\nrouter = APIRouter()\n", encoding="utf-8")

        task = fake_task_state.create_task(
            "Build FastAPI expense app",
            project_dir=str(tmp_path),
            task_types=["functional", "artifact"],
        )
        subtask = fake_task_state.add_subtask(
            task.task_id,
            "Implement Expense CRUD API endpoints",
            "deepseek",
            subtask_id="st-expenses",
        )
        contract = TaskContract.new(
            task_id=task.task_id,
            level="subtask",
            goal=subtask.description,
            subtask_id=subtask.subtask_id,
            project_dir=str(tmp_path),
        )
        contract.expected_deliverables = [
            DeliverableSpec(
                artifact_type="api_service_source",
                path_hint="app/routers/expenses.py",
                required=True,
            )
        ]
        fake_task_state.save_task_contract(contract)
        job = fake_task_state.create_job(subtask)
        fake_task_state.complete_job(
            job.job_id,
            success=True,
            output=f"Created files: {tmp_path / 'app' / 'routers' / 'expenses.py'}",
        )
        job = fake_task_state.get_job(job.job_id)

        llm = MockLLMGateway("""
        {"passed": false, "feedback": "Unable to verify implementation without file content review. The agent did not show actual CRUD code.", "action": "fix"}
        """)
        owner = OwnerAgent(llm, fake_task_state)

        acceptance = owner.accept_subtask(job)

        assert acceptance.level2_passed is True
        assert acceptance.recommended_action == "approve"
        assert acceptance.failed_checks == []


class TestDeliveryContractInDecomposition:
    def test_decompose_persists_owner_delivery_contract_before_subtasks(self, fake_owner_agent, fake_task_state, tmp_path):
        task = fake_task_state.create_task(
            description="Build a todo tool with add and list. Do not use Docker.",
            project_dir=str(tmp_path),
            task_types=["functional"],
            delivery_mode="functional",
        )

        try:
            fake_owner_agent.decompose_and_assign(task, context={"task_types": ["functional"]})
        except RuntimeError:
            pass

        contract = fake_task_state.get_delivery_contract(task.task_id)
        assert contract is not None
        assert contract["task_types"] == ["functional"]
        assert any(c["value"] == "docker" for c in contract["constraints"])
        assert not any(d["artifact_type"] == "dockerfile" for d in contract["deliverables"])


class TestNoDockerNegativeConstraint:
    def test_task_contract_inference_does_not_treat_no_docker_as_required_docker(self, fake_owner_agent, fake_task_state, tmp_path):
        task = fake_task_state.create_task(
            description="Build a Python tool. Do not use Docker or container tooling.",
            project_dir=str(tmp_path),
            task_types=["functional"],
            delivery_mode="functional",
        )

        deliverables, checks = fake_owner_agent._infer_task_contract_requirements(task)

        assert not any(item.artifact_type == "dockerfile" for item in deliverables)
        assert not any(item.check_type == "container_config_exists" for item in checks)

    def test_task_contract_inference_does_not_treat_chinese_no_docker_as_required_docker(self, fake_owner_agent, fake_task_state, tmp_path):
        task = fake_task_state.create_task(
            description="实现 FastAPI + SQLite Web 应用，不得创建 Dockerfile/docker-compose。",
            project_dir=str(tmp_path),
            task_types=["functional"],
            delivery_mode="functional",
        )

        deliverables, checks = fake_owner_agent._infer_task_contract_requirements(task)

        assert not any(item.artifact_type == "dockerfile" for item in deliverables)
        assert not any(item.check_type == "container_config_exists" for item in checks)

    def test_task_contract_inference_does_not_treat_canvas_container_as_docker(self, fake_owner_agent, fake_task_state, tmp_path):
        task = fake_task_state.create_task(
            description=(
                "Build a static web app with index.html, styles.css, app.js, and README.md. "
                "Create a canvas animation container and responsive card layout. "
                "Open index.html directly without a server."
            ),
            project_dir=str(tmp_path),
            task_types=["functional"],
            delivery_mode="functional",
        )

        deliverables, checks = fake_owner_agent._infer_task_contract_requirements(task)

        assert not any(item.artifact_type == "dockerfile" for item in deliverables)
        assert not any(item.check_type == "container_config_exists" for item in checks)

    def test_task_contract_inference_treats_dashboard_endpoints_as_backend(self, fake_owner_agent, fake_task_state, tmp_path):
        task = fake_task_state.create_task(
            description="Build dashboard summary endpoints for total spend and monthly trends.",
            project_dir=str(tmp_path),
            task_types=["functional"],
            delivery_mode="functional",
        )

        deliverables, checks = fake_owner_agent._infer_task_contract_requirements(task)

        artifact_types = {item.artifact_type for item in deliverables}
        check_types = {item.check_type for item in checks}
        assert "api_service_source" in artifact_types
        assert "frontend_source" not in artifact_types
        assert "api_source_exists" in check_types
        assert "frontend_source_exists" not in check_types
