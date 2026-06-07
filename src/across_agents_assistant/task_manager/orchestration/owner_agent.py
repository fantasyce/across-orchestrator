from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from across_agents_assistant.task_manager.models import (
    AcceptanceResult,
    AcceptanceCheck,
    DeliverableSpec,
    Job,
    SubTask,
    Task,
    JobStatus,
    OwnerDecision,
    RecommendedAction,
    RootCauseScope,
    TaskContract,
)
from across_agents_assistant.task_manager.state import TaskState
from across_agents_assistant.agent_ids import LOCAL_AGENT_ID, LOCAL_CLI_AGENT_IDS, normalize_agent_id
from across_agents_assistant.llm_gateway.config import load_llm_config
from across_agents_assistant.llm_gateway.provider_registry import get_default_provider_definitions, get_default_provider_ids
from across_agents_assistant.native_agent_skills import is_native_skill_available
from .requirements import (
    canonical_requirement_key,
    dedupe_requirement_path_hints,
    expand_path_hint_alternatives,
    extract_forbidden_path_hints,
    has_container_delivery_intent,
    has_negative_container_constraint,
    is_auxiliary_deliverable_path_hint,
    is_probable_deliverable_path,
    is_runtime_data_path_hint,
    normalize_path_hint,
)
from .project_acceptance import first_existing_candidate

logger = logging.getLogger("across_agents_assistant.task_manager")


_CLOUD_AGENT_IDS = get_default_provider_ids()
_AGENT_LABEL_ALIASES = (
    "openclaw",
    "hermes",
    "claude(?:\\s+code)?",
    "codex",
    "opencode",
    "cursor(?:\\s+agent)?",
    *_CLOUD_AGENT_IDS,
)
_AGENT_CAPABILITY_LABEL_RE = re.compile(
    rf"\b(?:{'|'.join(_AGENT_LABEL_ALIASES)})\s*:\s*[^.;)\n]+",
    re.IGNORECASE,
)

_SUBTASK_OUTPUT_VERB_RE = re.compile(
    r"\b(create|write|produce|deliver|output|generate|implement|build|add|update|modify|repair|fix)\b",
    re.IGNORECASE,
)

_SUBTASK_REFERENCE_CONTEXT_RE = re.compile(
    r"\b("
    r"validates?|verif(?:y|ies)|checks?|tests?|starts?|runs?|launch(?:es)?|opens?|loads?|"
    r"references?|imports?|href|src|stylesheet|script|manifest|smoke[-\s]*test|how\s+to|"
    r"explaining|document(?:ing)?"
    r")\b",
    re.IGNORECASE,
)

_PATH_TOKEN_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+"
    r"|\b[A-Za-z0-9_-]+\.[A-Za-z0-9][A-Za-z0-9_.-]*\b"
)


def _strip_agent_capability_label_context(text: str) -> str:
    """Remove UI copy that lists agent skills, such as ``DeepSeek: Backend API``."""
    return _AGENT_CAPABILITY_LABEL_RE.sub(" ", text or "")


def _is_static_frontend_file_scope(description: str, path_hints: List[str]) -> bool:
    """Detect static-web subtasks where backend words are UI labels, not backend scope."""
    basenames = {os.path.basename(path).lower() for path in path_hints if path}
    frontend_exts = (".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte")
    backend_exts = (".py", ".go", ".rs", ".rb", ".php", ".java", ".kt", ".swift")
    has_frontend_asset = any(name.endswith(frontend_exts) for name in basenames)
    has_backend_asset = any(name.endswith(backend_exts) for name in basenames) or any(
        name in {"server.js", "server.ts", "api.js", "api.ts"} for name in basenames
    )
    if not has_frontend_asset or has_backend_asset:
        return False

    text = (description or "").lower()
    return (
        "static web" in text
        or "open directly" in text
        or "no build" in text
        or "index.html" in basenames
        or "styles.css" in basenames
        or "app.js" in basenames
    )


def _path_hint_terms(path_hint: str) -> List[str]:
    normalized = normalize_path_hint(path_hint or "") or ""
    basename = os.path.basename(normalized)
    terms = [term.lower() for term in (normalized, basename) if term]
    unique: List[str] = []
    for term in sorted(terms, key=len, reverse=True):
        if term not in unique:
            unique.append(term)
    return unique


def _path_hint_occurrences(description: str, path_hint: str) -> List[int]:
    lowered = (description or "").lower()
    indexes: List[int] = []
    seen: set[int] = set()
    for term in _path_hint_terms(path_hint):
        start = 0
        while True:
            index = lowered.find(term, start)
            if index < 0:
                break
            if index not in seen:
                seen.add(index)
                indexes.append(index)
            start = index + len(term)
    return sorted(indexes)


def _contains_other_path_token(text: str, path_hint: str) -> bool:
    expected = set(_path_hint_terms(path_hint))
    for match in _PATH_TOKEN_RE.finditer(text or ""):
        token = (normalize_path_hint(match.group(0)) or match.group(0)).lower()
        basename = os.path.basename(token)
        if token not in expected and basename not in expected:
            return True
    return False


def _has_direct_output_context(description: str, path_hint: str) -> bool:
    lowered = (description or "").lower()
    for index in _path_hint_occurrences(description, path_hint):
        window = lowered[max(0, index - 180):index]
        output_matches = list(_SUBTASK_OUTPUT_VERB_RE.finditer(window))
        for match in reversed(output_matches):
            between = window[match.end():]
            if _SUBTASK_REFERENCE_CONTEXT_RE.search(between):
                continue
            if _contains_other_path_token(between, path_hint):
                continue
            return True
    return False


def _is_subtask_reference_only_path_hint(description: str, path_hint: str) -> bool:
    if _has_direct_output_context(description, path_hint):
        return False
    lowered = (description or "").lower()
    for index in _path_hint_occurrences(description, path_hint):
        before = lowered[max(0, index - 220):index]
        after = lowered[index:index + 120]
        if _SUBTASK_REFERENCE_CONTEXT_RE.search(before):
            return True
        if re.search(r"^\s*(?:[,，)]|and\b|or\b)", after, re.IGNORECASE) and re.search(
            r"\bmanifest\b|\bvalidate|\bverify|\bcheck|\brun|\bopen|\bstart",
            before,
        ):
            return True
    return False


class IntegrationResult:
    """Structured result for integration testing."""

    def __init__(self, passed: bool, message: str = "", details: Optional[Dict[str, Any]] = None):
        self.passed = passed
        self.message = message
        self.details = details or {}


class OwnerAgent:
    """
    LLM-based Owner Agent responsible for:
    - Decomposing tasks into SubTasks with DAG dependencies
    - Assigning appropriate agents based on capability keywords
    - Performing Level 2 acceptance review of completed jobs
    - Running integration tests when all subtasks are done
    """

    NATIVE_SKILL_ROUTING_STOPWORDS = {
        "a",
        "an",
        "and",
        "app",
        "audit",
        "behavior",
        "build",
        "code",
        "create",
        "for",
        "implement",
        "in",
        "of",
        "on",
        "review",
        "task",
        "the",
        "to",
        "update",
        "with",
        "work",
        "workflow",
    }

    DECOMPOSITION_SYSTEM_PROMPT = """You are an expert task planner and technical architect.

Your job is to decompose a user request into well-defined subtasks with dependencies.

**Available Agents and their strengths:**
- claude: Deep technical architecture, OpenAPI spec design, database schema design
- hermes: React frontend, UI/UX, TypeScript components
- codex: Local Codex CLI coding agent for implementation, debugging, and repository-aware changes
- opencode: Local OpenCode CLI coding agent for scripted repository-aware implementation
- cursor: Local Cursor Agent CLI for editor-native implementation and code review tasks
- openclaw: General purpose, fallback for anything else
- configured cloud provider ids from the provider registry: backend/API, reasoning, implementation, DevOps, and fallback LLM tasks when their API keys are configured

**Output Format:**
You MUST output a JSON object with this exact structure:
{
    "subtasks": [
        {
            "id": "short-name",
            "description": "Clear, actionable description of what to implement",
            "agent": "openclaw|hermes|claude|codex|opencode|cursor|configured-cloud-provider-id",
            "priority": 1,
            "dependencies": ["id-of-dependency-1", "id-of-dependency-2"],
            "deliverables": [
                {
                    "artifact_type": "api_service_source|frontend_source|dockerfile|macos_app_bundle|file",
                    "required": true,
                    "path_hint": "optional/path/inside/project",
                    "description": "what concrete output must exist after this subtask"
                }
            ],
            "acceptance_checks": [
                {
                    "check_type": "api_source_exists|frontend_source_exists|container_config_exists|packaged_app_exists|file_exists",
                    "required": true,
                    "description": "what deterministic check should pass"
                }
            ]
        }
    ]
}

**Rules:**
1. Each subtask must have a unique short `id` (kebab-case, no spaces)
2. Use `dependencies` to reference other subtask `id`s
3. Priority 1 = highest, run first
4. Keep descriptions concise but actionable
5. Assign the agent whose strengths best match the subtask content
6. REQUIRED: Every subtask MUST include non-empty `deliverables` and `acceptance_checks` arrays.
   Even simple tasks need at least one deliverable (e.g., source file, test file, documentation file, or packaged artifact).
   Do NOT return empty arrays for these fields.
7. For functional tasks, deliverables should be implementation source/tests/docs that prove behavior.
   Do NOT make runtime data files (todo.json, cache.db, local sqlite files), placeholder package markers
   (__init__.py), or setup scaffolding the final deliverable unless the user explicitly requested those files.
8. Do NOT create standalone directory-structure/scaffolding subtasks. Directory creation is implicit in
   the file-producing implementation subtasks.
"""

    ACCEPTANCE_SYSTEM_PROMPT = """You are a senior technical lead performing code and output acceptance review.

Your job is to review a completed subtask and determine if it meets requirements.

**IMPORTANT — How Agents Work:**
The agent executing this subtask is a CLI-based coding assistant (like Claude Code). It:
1. Writes code/files to disk in the project directory
2. Returns a summary of what was done, file paths, and any issues encountered
3. Does NOT typically return full file contents in its response

**Acceptance Rules:**
1. If the agent reports files were created/modified successfully → LIKELY PASSED
2. If the agent describes the implementation approach and confirms completion → LIKELY PASSED
3. If the agent explicitly states it could not complete or encountered errors → FAILED
4. If the output is completely empty or says "I cannot do this" → FAILED
5. If the output mainly asks clarifying questions or requests the user to choose options instead of delivering work → FAILED
6. Only fail if there is a CLEAR, SPECIFIC problem that can be fixed (e.g., "missing error handling", "wrong endpoint path")

**Be PRAGMATIC, not pedantic:**
- Accept summaries and file path references as valid output
- Do NOT require full file contents in the response
- Do NOT fail for minor style issues
- Do NOT fail because the output format doesn't match your expectations

**Output Format:**
You MUST output a JSON object with this exact structure:
{
    "passed": true|false,
    "feedback": "Detailed feedback. If passed=false, explain what needs to be fixed.",
    "action": "approve|fix|downgrade|reassign"
}

**Actions:**
- approve: Output is acceptable, proceed
- fix: Output has a specific, fixable issue (only use if you can clearly describe what to fix)
- downgrade: Functionality is basically usable but has known limitations (after max rounds)
- reassign: Functionality is completely broken, assign to a different agent (after max rounds)
"""

    ACCEPTANCE_REPAIR_SYSTEM_PROMPT = """You normalize acceptance review output into strict JSON.

You will receive an acceptance review response that may contain extra prose or malformed formatting.
Extract the best possible structured decision without changing the meaning.

Output ONLY a JSON object with this exact shape:
{
    "passed": true|false,
    "feedback": "string",
    "action": "approve|fix|downgrade|reassign"
}

Rules:
1. Do not add markdown fences.
2. If the original response clearly indicates approval, set passed=true and action=approve.
3. If the original response clearly indicates rejection, set passed=false and choose the closest action.
4. If the original response is ambiguous, set passed=false and action=fix.
"""

    WAVE_ACCEPTANCE_SYSTEM_PROMPT = """You are a senior technical lead reviewing whether a completed wave is coherent enough to move forward.

Assess the wave as a combined delivery unit rather than reviewing a single subtask in isolation.

Output ONLY a JSON object with this exact shape:
{
    "passed": true|false,
    "feedback": "string",
    "action": "approve|fix"
}

Rules:
1. Evaluate ONLY the current wave's declared subtasks and outputs, not whether the full task is already finished.
2. Treat prior approved waves and their accepted artifacts as valid available inputs for the current wave. Do NOT require the current wave to recreate files that already exist from earlier approved waves.
3. passed=true if the current wave is internally coherent and, together with already approved prior-wave artifacts, provides enough inputs for downstream dependent waves to continue safely.
4. Later waves may intentionally deliver remaining files or features; do not fail solely because future-wave deliverables are not present yet.
5. Extra scaffolding files or support files are acceptable when they are consistent with the current wave's goal. Do NOT require a strict one-artifact-per-subtask mapping.
6. If the current wave's goal includes setting up structure or scaffolding, visible directories and coherent support files in the project tree count as valid evidence even if directories are not stored as artifact records.
7. Future-wave subtasks may be listed as context. They are explicitly out of scope for this wave; missing future-wave functionality is not a blocker unless a current-wave subtask directly depends on it.
8. Global hygiene and user-forbidden constraints still apply to every wave. Fail on forbidden files, wrong technology stack, duplicate/conflicting structures, or other artifacts that would poison downstream work.
9. If there are inconsistencies, missing outputs for this wave, or obvious contract mismatches that are not already satisfied by prior approved artifacts, set passed=false and action=fix.
10. Do not use markdown fences.
"""

    def __init__(self, llm_gateway: Callable, state: TaskState) -> None:
        """
        Args:
            llm_gateway: A callable with signature chat(system_prompt, message, temperature)
                         that returns an object with a `.text` attribute.
            state: TaskState instance for persisting created subtasks.
        """
        self._llm = llm_gateway
        self._state = state
        self._owner_sessions: Dict[str, str] = {}

    def _ensure_owner_session_id(self, task: Optional[Task]) -> Optional[str]:
        if task is None:
            return None
        if task.owner_session_id:
            self._owner_sessions[task.task_id] = task.owner_session_id
            return task.owner_session_id
        session_id = self._owner_sessions.get(task.task_id) or f"owner-{uuid.uuid4().hex[:12]}"
        self._owner_sessions[task.task_id] = session_id
        task.owner_session_id = session_id
        return session_id

    def _get_task_for_job(self, job: Job) -> Optional[Task]:
        return self._state.get_task_by_subtask(job.subtask_id)

    def _get_persistence(self):
        return getattr(self._state, "_persistence", None)

    def _get_available_agents(self) -> List[Dict[str, Any]]:
        all_agents = [
            {"id": "claude", "name": "Claude Code", "characteristics": "Deep technical architecture, OpenAPI spec design, database schema design, system architecture. Best for: designing APIs, data models, and system layouts. Requires: claude CLI installed."},
            {"id": "hermes", "name": "Hermes", "characteristics": "React frontend, UI/UX design, TypeScript components, HTML/CSS. Best for: building user interfaces, React components, and frontend styling. Requires: hermes CLI installed."},
            {"id": "codex", "name": "Codex", "characteristics": "Local Codex CLI coding agent. Best for repository-aware implementation, debugging, tests, and code review tasks. Requires: codex CLI installed and authenticated."},
            {"id": "opencode", "name": "OpenCode", "characteristics": "Local OpenCode CLI coding agent. Best for scripted repository-aware implementation and automation. Requires: opencode CLI installed and authenticated."},
            {"id": "cursor", "name": "Cursor Agent", "characteristics": "Local Cursor Agent CLI. Best for editor-native implementation, refactoring, and code review tasks. Requires: cursor-agent installed and authenticated."},
            {"id": LOCAL_AGENT_ID, "name": "OpenClaw", "characteristics": "General purpose coding, file operations, system commands, fallback for any task. Best for: generic code tasks, file manipulation, and any task other agents cannot handle. Requires: OpenClaw CLI installed."},
        ]
        all_agents.extend(
            {
                "id": provider.provider_id,
                "name": provider.name,
                "characteristics": "Cloud LLM provider from the configured registry. Requires: API key configured.",
            }
            for provider in get_default_provider_definitions()
        )
        available = []
        for agent in all_agents:
            if self._is_agent_available(agent["id"]):
                available.append(agent)
        if not available:
            logger.warning("No agents available for task decomposition")
        return available

    def _resolve_allowed_subtask_agents(
        self,
        context: Optional[Dict[str, Any]],
        available_agents: List[Dict[str, Any]],
    ) -> List[str]:
        available_ids = [agent["id"] for agent in available_agents]
        selected = []
        if context:
            selected = [
                agent_id
                for agent_id in context.get("allowed_subtask_agents", []) or []
                if agent_id in available_ids
            ]
        if selected:
            return selected

        owner_agent = context.get("owner_agent") if context else None
        if owner_agent and owner_agent != "auto" and owner_agent in available_ids:
            return [owner_agent]
        return available_ids

    def _build_system_prompt(
        self,
        available_agents: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        agent_list = "\n".join(
            f"- {a['id']} ({a['name']}): {a['characteristics']}"
            for a in available_agents
        )
        if not agent_list:
            agent_list = "- none (no agents available)"
        agent_ids = ", ".join(a['id'] for a in available_agents) if available_agents else "none"
        capability_block = self._build_capability_system_block(
            context,
            [str(agent["id"]) for agent in available_agents],
        )
        return f"""You are an expert task planner and technical architect.

Your job is to decompose a user request into well-defined subtasks with dependencies.

**Available Agents (ONLY use these):**
{agent_list}
{capability_block}

**Output Format:**
You MUST output a JSON object with this exact structure:
{{
    "subtasks": [
        {{
            "id": "short-name",
            "description": "Clear, actionable description of what to implement",
            "agent": "{agent_ids}",
            "priority": 1,
            "dependencies": ["id-of-dependency-1", "id-of-dependency-2"]
        }}
    ]
}}

**Rules:**
1. Each subtask must have a unique short `id` (kebab-case, no spaces)
2. Use `dependencies` to reference other subtask `id`s
3. Priority 1 = highest, run first
4. Keep descriptions concise but actionable
5. Assign the agent whose strengths best match the subtask content
6. ONLY use agents from the list above. Do NOT invent or suggest agents that aren't listed.
"""

    def _build_capability_system_block(
        self,
        context: Optional[Dict[str, Any]],
        available_agent_ids: List[str],
    ) -> str:
        capabilities = (context or {}).get("agent_capabilities") or {}
        profiles = capabilities.get("profiles") or {}
        prompt = str(capabilities.get("prompt") or "").strip()

        normalized_available = {
            normalize_agent_id(agent_id) or agent_id
            for agent_id in available_agent_ids
        }
        lines: List[str] = []
        if prompt:
            lines.append(prompt)

        skills_by_id = {
            str(item.get("id")): item
            for item in capabilities.get("skills", [])
            if isinstance(item, dict) and item.get("id")
        }
        for agent_id, profile in profiles.items():
            normalized_id = normalize_agent_id(str(agent_id)) or str(agent_id)
            if normalized_available and normalized_id not in normalized_available:
                continue
            if not isinstance(profile, dict):
                continue
            hints = []
            for skill_id in profile.get("enabled_skill_ids", []) or []:
                skill = skills_by_id.get(str(skill_id))
                if skill and skill.get("prompt_hint"):
                    hints.append(f"{skill.get('name', skill_id)}: {skill['prompt_hint']}")
            if hints:
                lines.append(f"- {normalized_id} skill guidance: " + " ".join(hints))

        native_skills = capabilities.get("native_skills") or {}
        if isinstance(native_skills, dict):
            for agent_id, skills in native_skills.items():
                normalized_id = normalize_agent_id(str(agent_id)) or str(agent_id)
                if normalized_available and normalized_id not in normalized_available:
                    continue
                if not isinstance(skills, list):
                    continue
                names = [
                    str(item.get("name") or item.get("id"))
                    for item in skills
                    if isinstance(item, dict)
                    and is_native_skill_available(item)
                    and (item.get("name") or item.get("id"))
                ]
                if names:
                    lines.append(f"- {normalized_id} native skills: " + ", ".join(names))

        if not lines:
            return ""
        return "\n\n**Configured Agent Capabilities:**\n" + "\n".join(lines)

    def _build_agent_capability_suffix(
        self,
        context: Optional[Dict[str, Any]],
        agent_id: str,
    ) -> str:
        capabilities = (context or {}).get("agent_capabilities") or {}
        profiles = capabilities.get("profiles") or {}
        normalized_agent_id = normalize_agent_id(agent_id) or agent_id
        profile = profiles.get(normalized_agent_id) or profiles.get(agent_id)
        if not isinstance(profile, dict):
            profile = {}

        skills_by_id = {
            str(item.get("id")): item
            for item in capabilities.get("skills", [])
            if isinstance(item, dict) and item.get("id")
        }
        skill_names = [
            str(skills_by_id.get(str(skill_id), {}).get("name") or skill_id)
            for skill_id in profile.get("enabled_skill_ids", []) or []
        ]
        skill_hints = [
            str(skills_by_id[str(skill_id)]["prompt_hint"])
            for skill_id in profile.get("enabled_skill_ids", []) or []
            if str(skill_id) in skills_by_id and skills_by_id[str(skill_id)].get("prompt_hint")
        ]
        plugins = [str(item) for item in profile.get("enabled_plugin_ids", []) or []]
        tools = [str(item) for item in profile.get("enabled_tool_names", []) or []]
        native_skills_by_agent = capabilities.get("native_skills") or {}
        native_skill_names = []
        if isinstance(native_skills_by_agent, dict):
            native_values = (
                native_skills_by_agent.get(normalized_agent_id)
                or native_skills_by_agent.get(agent_id)
                or []
            )
            if isinstance(native_values, list):
                native_skill_names = [
                    str(item.get("name") or item.get("id"))
                    for item in native_values
                    if isinstance(item, dict) and (item.get("name") or item.get("id"))
                ]
        custom_instructions = str(profile.get("custom_instructions") or "").strip()
        strict_scope = bool(profile.get("strict_tool_scope", False))

        if not any([skill_names, skill_hints, native_skill_names, plugins, tools, custom_instructions, strict_scope]):
            return ""

        lines = ["[AGENT CAPABILITY PROFILE]"]
        if skill_names:
            lines.append("- Skills: " + ", ".join(skill_names))
        if skill_hints:
            lines.append("- Skill guidance: " + " ".join(skill_hints))
        if native_skill_names:
            lines.append("- Native skills: " + ", ".join(native_skill_names))
        if plugins:
            lines.append("- Enabled plugins: " + ", ".join(plugins))
        if tools:
            lines.append("- Enabled tools: " + ", ".join(tools))
        if custom_instructions:
            lines.append("- Custom instructions: " + custom_instructions)
        if strict_scope:
            lines.append("- Scope: Only use the listed plugins/tools unless the task requires escalation.")
        return "\n\n" + "\n".join(lines)

    def decompose_and_assign(self, task: Task, context: Optional[Dict[str, Any]] = None) -> Task:
        """
        Call LLM to decompose a task, parse the JSON response,
        create SubTasks with agent assignments, and persist them via TaskState.

        Args:
            task: The Task to decompose.
            context: Optional additional context for the LLM.

        Returns:
            The same Task object with subtasks populated.
        """
        from ..models import Wave

        # Reuse the Wave 0 decomposition node created by the orchestrator so
        # task creation can return a DAG immediately without a second API path.
        decompose_subtask_id = f"{task.task_id}-decompose"
        decompose_subtask = next(
            (st for st in task.subtasks if st.subtask_id == decompose_subtask_id),
            None,
        )
        if decompose_subtask is None:
            decompose_subtask = self._state.add_subtask(
                task_id=task.task_id,
                description="Owner Agent 正在分析需求并分解任务...",
                agent_id="owner",
                priority=0,
                dependencies=[],
                subtask_id=decompose_subtask_id,
            )
        if decompose_subtask:
            # Persist the decompose node in wave 0 so restored DAGs keep the
            # dedicated decomposition stage instead of folding it into wave 1.
            decompose_subtask.wave_number = 0
            self._state._persist_subtask(decompose_subtask)
            decompose_subtask.status = JobStatus.RUNNING
            self._state.update_subtask_status(task.task_id, decompose_subtask.subtask_id, JobStatus.RUNNING)

        decompose_wave = next((wave for wave in task.waves if wave.wave_number == 0), None)
        if decompose_wave is None:
            decompose_wave = Wave(
                wave_id=f"wave-decompose-{uuid.uuid4().hex[:8]}",
                wave_number=0,
                task_id=task.task_id,
                subtasks=[decompose_subtask] if decompose_subtask else [],
                status=JobStatus.RUNNING,
                is_blocked=False,
                fix_rounds=[]
            )
            task.waves = [decompose_wave]
        else:
            decompose_wave.status = JobStatus.RUNNING
            decompose_wave.subtasks = [decompose_subtask] if decompose_subtask else []
        self._state._persist_wave(decompose_wave)

        # Phase 1: Create requirement manifest from the original task description
        from .requirements import extract_requirement_manifest
        manifest = extract_requirement_manifest(
            task_id=task.task_id,
            description=task.description,
            project_dir=task.project_dir,
        )
        self._state.save_requirement_manifest(manifest)
        # Task 7: persist owner session early for observability
        self._ensure_owner_session_id(task)
        self._state._persist_task(task)
        logger.info(f"Created requirement manifest for {task.task_id} with {len(manifest.deliverables)} deliverables")

        # Phase 1.5: Generate and persist Owner Delivery Contract before decomposition
        from .delivery_contract import build_owner_delivery_contract

        task_types = list(getattr(task, 'task_types', []) or (context or {}).get("task_types") or [])
        if task_types:
            delivery_contract = build_owner_delivery_contract(
                task_id=task.task_id,
                description=task.description,
                task_types=task_types,
                project_dir=task.project_dir,
                manifest=self._state.get_requirement_manifest(task.task_id),
            )
            self._state.save_delivery_contract(delivery_contract)
            task.last_owner_decision = dict(task.last_owner_decision or {})
            task.last_owner_decision["owner_delivery_contract_id"] = delivery_contract["contract_id"]
            task.last_owner_decision["delivery_mode"] = delivery_contract["delivery_mode"]
            self._state._persist_task(task)

        available_agents = self._get_available_agents()
        allowed_agent_ids = self._resolve_allowed_subtask_agents(context, available_agents)
        if allowed_agent_ids:
            allowed_set = set(allowed_agent_ids)
            available_agents = [agent for agent in available_agents if agent["id"] in allowed_set]
        if not available_agents:
            raise ValueError("No available subtask agents match this task's selected agent pool")
        available_agent_ids = [a["id"] for a in available_agents]
        release_e2e_context = (context or {}).get("release_e2e") or {}
        if release_e2e_context.get("scenario_id") == "cross_agent_full_delivery_v1":
            from .release_e2e import build_release_e2e_subtasks

            decomposition = {"subtasks": build_release_e2e_subtasks(available_agent_ids)}
            logger.info(
                "Using deterministic release E2E decomposition for %s with %d subtasks",
                task.task_id,
                len(decomposition["subtasks"]),
            )
        else:
            system_prompt = self._build_system_prompt(available_agents, context=context)
            user_message = self._build_decomposition_message(task, context)

            logger.info(f"Starting task decomposition for {task.task_id}...")
            try:
                import time
                t0 = time.time()
                response = self._llm(
                    system_prompt=system_prompt,
                    message=user_message,
                    temperature=0.3,
                )
                elapsed = time.time() - t0
                logger.info(f"LLM decomposition response received in {elapsed:.1f}s for task {task.task_id}")
                decomposition = self._parse_decomposition(response.text)
            except Exception as e:
                logger.error(f"LLM decomposition failed for task {task.task_id}: {e}")
                raise RuntimeError(f"LLM decomposition failed: {e}") from e

        if not decomposition.get("subtasks"):
            fallback_subtasks = self._build_deterministic_fallback_decomposition(task, available_agent_ids)
            if fallback_subtasks:
                decomposition["subtasks"] = fallback_subtasks
                logger.warning(
                    "LLM decomposition returned no subtasks for %s; using deterministic fallback with %d subtasks",
                    task.task_id,
                    len(fallback_subtasks),
                )
            else:
                raise RuntimeError("LLM decomposition failed: no subtasks generated")

        # Map LLM subtask IDs to generated subtask_ids
        id_mapping: Dict[str, str] = {}

        # Build project_dir constraint if specified
        project_dir_constraint = ""
        if task.project_dir:
            project_dir_constraint = (
                f"\n\n[CRITICAL] All files MUST be written to this directory: {task.project_dir}\n"
                f"Do NOT create files in any other location. Use this exact path as the base."
            )
        global_constraint_suffix = self._build_global_subtask_constraint_suffix(task)

        allowed_documentation_files = self._allowed_documentation_files(task)
        filtered_subtasks = []
        skipped_llm_ids: set[str] = set()
        skipped_dependencies: Dict[str, List[str]] = {}
        for st_data in decomposition.get("subtasks", []):
            if self._is_validation_only_subtask(st_data):
                llm_id = st_data.get("id", "")
                if llm_id:
                    skipped_llm_ids.add(llm_id)
                    skipped_dependencies[llm_id] = list(st_data.get("dependencies", []) or [])
                logger.info(
                    "Skipping validation-only subtask for %s: %s",
                    task.task_id,
                    st_data.get("description", "")[:120],
                )
                continue
            if self._is_planning_only_subtask(st_data):
                llm_id = st_data.get("id", "")
                if llm_id:
                    skipped_llm_ids.add(llm_id)
                    skipped_dependencies[llm_id] = list(st_data.get("dependencies", []) or [])
                logger.info(
                    "Skipping planning-only subtask for %s: %s",
                    task.task_id,
                    st_data.get("description", "")[:120],
                )
                continue
            if self._is_disallowed_documentation_planning_subtask(st_data, allowed_documentation_files):
                llm_id = st_data.get("id", "")
                if llm_id:
                    skipped_llm_ids.add(llm_id)
                    skipped_dependencies[llm_id] = list(st_data.get("dependencies", []) or [])
                logger.info(
                    "Skipping disallowed documentation/planning subtask for %s: %s",
                    task.task_id,
                    st_data.get("description", "")[:120],
                )
                continue
            if self._is_structure_only_subtask(st_data, task.project_dir):
                llm_id = st_data.get("id", "")
                if llm_id:
                    skipped_llm_ids.add(llm_id)
                    skipped_dependencies[llm_id] = list(st_data.get("dependencies", []) or [])
                logger.info(
                    "Skipping structure-only subtask for %s: %s",
                    task.task_id,
                    st_data.get("description", "")[:120],
                )
                continue
            filtered_subtasks.append(st_data)

        for st_data in filtered_subtasks:
            description = st_data.get("description", "")
            if not description:
                continue

            # Append project_dir constraint to description
            if project_dir_constraint:
                description = description + project_dir_constraint
            if global_constraint_suffix:
                description = description + global_constraint_suffix

            agent_id = self._select_agent(
                st_data,
                available_agent_ids,
                project_dir=task.project_dir,
                context=context,
            )
            routing_suffix = self._build_native_skill_routing_suffix(context, agent_id, st_data)
            if routing_suffix:
                description = description + routing_suffix
            capability_suffix = self._build_agent_capability_suffix(context, agent_id)
            if capability_suffix:
                description = description + capability_suffix
            priority = int(st_data.get("priority", 1))

            # Create via TaskState so it is persisted properly
            subtask = self._state.add_subtask(
                task_id=task.task_id,
                description=description,
                agent_id=agent_id,
                priority=priority,
                dependencies=[],  # populated after mapping
            )
            if subtask is None:
                continue

            # Map the LLM-provided id to the generated subtask_id
            llm_id = st_data.get("id", "")
            if llm_id:
                id_mapping[llm_id] = subtask.subtask_id

            contract = TaskContract.new(
                task_id=task.task_id,
                level="subtask",
                goal=description,
                subtask_id=subtask.subtask_id,
                wave_number=getattr(subtask, "wave_number", 1),
                project_dir=task.project_dir,
            )
            parsed_deliverables = self._parse_deliverable_specs(st_data)
            parsed_checks = self._parse_acceptance_checks(st_data)
            inferred_deliverables, inferred_checks = self._infer_subtask_deliverables(
                subtask.description, subtask.agent_id, task.project_dir
            )
            if not parsed_deliverables:
                parsed_deliverables, parsed_checks = inferred_deliverables, inferred_checks
            else:
                parsed_deliverables, parsed_checks = self._repair_parsed_deliverable_path_hints(
                    parsed_deliverables,
                    parsed_checks,
                    inferred_deliverables,
                    inferred_checks,
                )
            # Sanitize: remove frontend_source from documentation-only subtasks
            parsed_deliverables, parsed_checks = self._sanitize_subtask_contract_specs(
                description=subtask.description,
                agent_id=subtask.agent_id,
                deliverables=parsed_deliverables,
                checks=parsed_checks,
                allowed_documentation_files=allowed_documentation_files,
            )
            contract.expected_deliverables = parsed_deliverables
            contract.acceptance_checks = parsed_checks
            self._state.save_task_contract(contract)

            # Note: state.add_subtask() already appends to task.subtasks,
            # so we do NOT append again here.

        # Second pass: resolve dependency text IDs to actual subtask_ids
        for st_data in filtered_subtasks:
            llm_id = st_data.get("id", "")
            if not llm_id or llm_id not in id_mapping:
                continue

            actual_subtask_id = id_mapping[llm_id]
            dep_ids = st_data.get("dependencies", [])
            resolved_deps = []

            def append_resolved_dep(dep: str, seen: Optional[set[str]] = None) -> None:
                seen = seen or set()
                if dep in seen:
                    return
                seen.add(dep)
                if dep in skipped_llm_ids:
                    for upstream_dep in skipped_dependencies.get(dep, []):
                        append_resolved_dep(upstream_dep, seen)
                    return
                if dep in id_mapping:
                    mapped = id_mapping[dep]
                    if mapped not in resolved_deps:
                        resolved_deps.append(mapped)
                else:
                    # Fallback: try to match by description substring
                    for mapped in self._resolve_dependency_by_text(dep, task):
                        if mapped not in resolved_deps:
                            resolved_deps.append(mapped)

            for dep in dep_ids:
                append_resolved_dep(dep)

            # Update the subtask's dependencies in state
            for st in task.subtasks:
                if st.subtask_id == actual_subtask_id:
                    st.dependencies = resolved_deps
                    self._state._persist_subtask(st)
                    break

        # Mark decompose subtask as completed
        if decompose_subtask:
            self._state.update_subtask_status(task.task_id, decompose_subtask.subtask_id, JobStatus.COMPLETED)

        for w in task.waves:
            if w.wave_number == 0:
                w.status = JobStatus.COMPLETED
                self._state._persist_wave(w)
                break

        task_contract = TaskContract.new(
            task_id=task.task_id,
            level="task",
            goal=task.description,
            project_dir=task.project_dir,
            context_mode="summary",
        )
        (
            task_contract.expected_deliverables,
            task_contract.acceptance_checks,
        ) = self._infer_task_contract_requirements(task)

        # Aggregate subtask-specific deliverables into the task-level contract
        all_contracts = self._state.get_task_contracts(task.task_id)
        subtask_contracts = [c for c in all_contracts if c.get("level") == "subtask"]
        if subtask_contracts:
            self._aggregate_contract_requirements(task_contract, subtask_contracts)

        self._state.save_task_contract(task_contract)

        # Phase 2: Decomposition coverage gate — ensure every required manifest
        # deliverable has an owning subtask contract.
        try:
            self._ensure_decomposition_coverage(task)
        except Exception as exc:
            logger.warning("Decomposition coverage gate failed (non-fatal): %s", exc)

        logger.info(f"Task {task.task_id} decomposed into {len(task.subtasks)} subtasks")
        return task

    def _allowed_documentation_files(self, task: Task) -> List[str]:
        try:
            from .delivery_contract import _extract_allowed_documentation_files
            return _extract_allowed_documentation_files(task.description)
        except Exception:
            return []

    def _build_global_subtask_constraint_suffix(self, task: Task) -> str:
        """Carry task-level negative constraints into every worker prompt."""
        parts: List[str] = []
        task_types = {str(item).lower() for item in (getattr(task, "task_types", None) or [])}
        try:
            from .delivery_contract import (
                _explicitly_requests_auth,
                _extract_allowed_documentation_files,
                _extract_forbidden_runner_script_files,
            )
            allowed_docs = _extract_allowed_documentation_files(task.description)
            runner_forbidden = _extract_forbidden_runner_script_files(task.description)
            auth_requested = _explicitly_requests_auth(task.description)
        except Exception:
            allowed_docs = []
            runner_forbidden = []
            auth_requested = False

        if "functional" in task_types and not auth_requested:
            parts.append(
                "Do NOT implement authentication, users, accounts, login, registration, passwords, JWT, OAuth, roles, or permissions unless explicitly requested."
            )
        if allowed_docs:
            parts.append(
                "Documentation scope: only create "
                + ", ".join(allowed_docs)
                + "; do not create API.md, SCHEMA.md, DESIGN.md, SPEC.md, extra plans, or other documentation files."
            )
        forbidden = dedupe_requirement_path_hints([
            *extract_forbidden_path_hints(task.description),
            *runner_forbidden,
        ])
        if forbidden:
            parts.append("Forbidden files: " + ", ".join(forbidden) + ".")
        text = (task.description or "").lower()
        is_static_web = (
            "static web" in text
            or "file://" in text
            or ("index.html" in text and ("styles.css" in text or "app.js" in text))
        )
        if is_static_web:
            parts.append(
                "Static web acceptance: put the required visible UI skeleton directly in index.html. "
                "Do not rely only on JavaScript-rendered templates for requested evidence. Named Local Agent "
                "cards must be .agent-card elements in the HTML source, named Cloud LLM cards must be .llm-card "
                "elements in the HTML source, and each named card must contain at least three data-skill, checkbox, "
                "or role='switch' controls inside that same card. JavaScript may enhance or hydrate these elements "
                "but must not replace them with alternate class names such as provider-card. If the task requests "
                "a native/local-agent specific skill, include a visible skill control in index.html labeled Native, "
                "Native Agent Skill, Local Native, or Across E2E Quality Gate. If responsive or narrow "
                "screens are requested, add media rules for the actual layout selectors used by the page, such as "
                ".console, .agents-grid, .skill-matrix, .matrix-grid, and .composer-row, so a 390px viewport has no "
                "document-level horizontal overflow."
            )
        if not parts:
            return ""
        return "\n\n[GLOBAL CONSTRAINTS]\n" + "\n".join(f"- {part}" for part in parts)

    def _is_disallowed_documentation_planning_subtask(
        self,
        subtask_data: Dict[str, Any],
        allowed_documentation_files: List[str],
    ) -> bool:
        if not allowed_documentation_files:
            return False
        allowed = {name.lower() for name in allowed_documentation_files}
        description = str(subtask_data.get("description") or "")
        text = description.lower()
        raw_deliverables = subtask_data.get("deliverables") or subtask_data.get("expected_deliverables") or []
        doc_hints = [
            os.path.basename(str(item.get("path_hint") or "")).lower()
            for item in raw_deliverables
            if isinstance(item, dict)
            and str(item.get("path_hint") or "").lower().endswith((".md", ".rst", ".txt"))
        ]
        if doc_hints and all(hint not in allowed for hint in doc_hints):
            return True

        planning_markers = (
            "design",
            "architecture",
            "schema",
            "api structure",
            "spec",
            "plan",
            "分析需求",
            "设计",
            "结构",
            "方案",
            "规划",
        )
        implementation_markers = (
            "implement",
            "create project",
            "create fastapi",
            "crud",
            "pytest",
            "frontend",
            "javascript",
            "html",
            "css",
            "实现",
            "创建项目",
            "编写",
            "测试",
        )
        docs_forbidden_by_text = any(
            marker in text for marker in planning_markers
        ) and not any(marker in text for marker in implementation_markers)
        explicit_allowed_doc = any(name.lower() in text for name in allowed)
        return docs_forbidden_by_text and not explicit_allowed_doc

    def _is_planning_only_subtask(self, subtask_data: Dict[str, Any]) -> bool:
        description = str(subtask_data.get("description") or "")
        text = description.lower()
        if not re.search(r"\bplan(?:ning)?\b", text):
            return False
        raw_deliverables = subtask_data.get("deliverables") or subtask_data.get("expected_deliverables") or []
        for item in raw_deliverables:
            if isinstance(item, dict) and item.get("path_hint"):
                return False
        if self._extract_path_hints(description):
            return False
        if re.search(r"\b(create|implement|build|write|add|update|fix|generate|repair)\b", text):
            return False
        return bool(
            re.search(
                r"\bplan(?:ning)?\b.{0,100}\b(architecture|layout|structure|state\s+model|interaction\s+patterns?|components?)\b",
                text,
            )
        )

    def _is_structure_only_subtask(
        self,
        subtask_data: Dict[str, Any],
        project_dir: Optional[str] = None,
    ) -> bool:
        description = str(subtask_data.get("description") or "")
        text = description.lower()
        if not re.search(
            r"\b(directory structure|project structure|folder structure|subdirectories|folders?|directories|scaffold(?:ing)?|skeleton)\b",
            text,
        ):
            return False

        raw_deliverables = subtask_data.get("deliverables") or subtask_data.get("expected_deliverables") or []
        source_exts = (
            ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".go", ".rs", ".swift", ".md",
        )
        for item in raw_deliverables:
            if not isinstance(item, dict):
                continue
            artifact_type = str(item.get("artifact_type") or "").lower()
            path_hint = str(item.get("path_hint") or "").strip()
            if artifact_type in {"api_service_source", "frontend_source", "test_suite", "documentation"}:
                return False
            if path_hint and os.path.basename(path_hint).lower().endswith(source_exts):
                return False

        path_hints = self._extract_path_hints(description, project_dir)
        forbidden_keys = {
            canonical_requirement_key(path_hint)
            for path_hint in extract_forbidden_path_hints(description)
        }
        concrete_path_hints = [
            path_hint
            for path_hint in path_hints
            if canonical_requirement_key(path_hint) not in forbidden_keys
            and os.path.basename(path_hint).lower().endswith(source_exts)
        ]
        if concrete_path_hints:
            return False

        implementation_terms = (
            "api server",
            "backend api",
            "frontend",
            "fastapi",
            "sqlite",
            "database",
            "endpoint",
            "component",
            "style",
            "stylesheet",
            "test suite",
            "smoke test",
            "readme",
        )
        return not any(term in text for term in implementation_terms)

    def _is_validation_only_subtask(self, subtask_data: Dict[str, Any]) -> bool:
        """Drop final QA/checking tasks from the agent DAG.

        The orchestrator owns deterministic probes and final acceptance. Delegating
        a "run the app and verify everything" node to a coding agent has proven
        brittle because it often cannot execute commands in the same runtime and
        then produces reports or directory paths instead of useful code changes.
        """
        raw_deliverables = subtask_data.get("deliverables") or subtask_data.get("expected_deliverables") or []
        raw_checks = subtask_data.get("acceptance_checks") or []
        text_parts = [
            str(subtask_data.get("id") or ""),
            str(subtask_data.get("description") or ""),
        ]
        for item in list(raw_deliverables) + list(raw_checks):
            if isinstance(item, dict):
                text_parts.extend(str(item.get(key) or "") for key in ("artifact_type", "path_hint", "description", "check_type"))
        text = " ".join(text_parts).lower()

        writes_tests = bool(
            re.search(r"\b(write|create|add|implement)\b.{0,40}\b(pytest|tests?|coverage)\b", text)
            or "test_source" in text
            or re.search(r"\btests?/", text)
        )
        if writes_tests:
            return False

        creates_implementation = bool(
            re.search(r"\b(write|create|add|implement|build)\b.{0,80}\b(api|backend|frontend|crud|model|schema|source|html|css|javascript|js)\b", text)
            or re.search(r"(实现|创建|编写|开发).{0,40}(接口|后端|前端|页面|模型|源码|功能)", text)
        )
        if creates_implementation:
            return False

        concrete_paths: List[str] = []
        reportish_markers = ("report", "validation", "verification", "test-results", "验收", "验证", "报告")
        source_exts = (".py", ".js", ".ts", ".html", ".css", ".json", ".toml", ".yaml", ".yml", ".sql", ".md")
        for item in raw_deliverables:
            if not isinstance(item, dict):
                continue
            hint = str(item.get("path_hint") or "").strip()
            artifact_type = str(item.get("artifact_type") or "").lower()
            if not hint:
                continue
            basename = os.path.basename(hint).lower()
            if any(marker in basename for marker in reportish_markers):
                continue
            if artifact_type in {"api_service_source", "frontend_source", "test_source", "documentation", "config_file", "file"} or basename.endswith(source_exts):
                concrete_paths.append(hint)
        if concrete_paths:
            return False

        validation_patterns = (
            r"\b(run|execute|launch|start)\b.{0,80}\b(pytest|tests?|application|app|server|uvicorn|endpoints?)\b",
            r"\b(verify|validate|validation|smoke|e2e|end-to-end|integration test|acceptance)\b",
            r"\b(check|inspect|audit|review|scan)\b.{0,80}\b(current\s+state|project\s+directory|existing\s+project|workspace|directory\s+state)\b",
            r"\b(current\s+state|project\s+directory|existing\s+project|workspace|directory\s+state)\b.{0,80}\b(check|inspection|audit|review|scan)\b",
            r"(运行|执行|启动).{0,40}(测试|应用|服务|接口)",
            r"(验证|验收|冒烟|端到端).{0,40}(应用|测试|接口|功能)",
            r"(检查|查看|盘点|审查|扫描).{0,40}(当前|项目目录|工作区|已有项目|目录状态)",
        )
        return any(re.search(pattern, text) for pattern in validation_patterns)

    def _build_deterministic_fallback_decomposition(
        self,
        task: Task,
        available_agent_ids: List[str],
    ) -> List[Dict[str, Any]]:
        """Create a conservative implementation plan when the LLM returns an empty DAG."""
        task_types = {str(item).lower() for item in (getattr(task, "task_types", None) or [])}
        text = (task.description or "").lower()
        is_functional = "functional" in task_types or any(
            token in text
            for token in ("web app", "webapp", "application", "api", "fastapi", "frontend", "backend")
        )
        if not is_functional:
            return []

        def choose(*candidates: str) -> str:
            for candidate in candidates:
                if candidate in available_agent_ids:
                    return candidate
            return available_agent_ids[0]

        backend_agent = choose("deepseek", "claude", LOCAL_AGENT_ID)
        frontend_agent = choose("hermes", "deepseek", LOCAL_AGENT_ID)
        general_agent = choose(LOCAL_AGENT_ID, "deepseek", "claude")

        return [
            {
                "id": "project_skeleton",
                "description": (
                    "Create a minimal FastAPI + SQLite project skeleton with app package, "
                    "the exact flat app/static asset directory, dependency manifest, and "
                    "application entrypoint. Do not create an alternate root static/ directory "
                    "or nested static/css and static/js duplicates unless the delivery contract "
                    "explicitly asks for those paths."
                ),
                "agent": general_agent,
                "priority": 1,
                "dependencies": [],
                "deliverables": [
                    {"artifact_type": "file", "path_hint": "pyproject.toml", "required": True},
                    {"artifact_type": "api_service_source", "path_hint": "app/main.py", "required": True},
                ],
                "acceptance_checks": [
                    {"check_type": "file_exists", "description": "Project skeleton files exist.", "required": True},
                ],
            },
            {
                "id": "data_model",
                "description": "Implement SQLite database setup plus Category, Expense, and Receipt data models.",
                "agent": backend_agent,
                "priority": 2,
                "dependencies": ["project_skeleton"],
                "deliverables": [
                    {"artifact_type": "api_service_source", "path_hint": "app/database.py", "required": True},
                    {"artifact_type": "api_service_source", "path_hint": "app/models.py", "required": True},
                    {"artifact_type": "api_service_source", "path_hint": "app/schemas.py", "required": True},
                ],
                "acceptance_checks": [
                    {"check_type": "file_exists", "description": "Database and model files exist.", "required": True},
                ],
            },
            {
                "id": "api_features",
                "description": (
                    "Implement FastAPI endpoints for category CRUD, expense CRUD, filtering, "
                    "CSV import with row-level errors, receipt upload, and dashboard summaries."
                ),
                "agent": backend_agent,
                "priority": 3,
                "dependencies": ["data_model"],
                "deliverables": [
                    {"artifact_type": "api_service_source", "path_hint": "app/routers/categories.py", "required": True},
                    {"artifact_type": "api_service_source", "path_hint": "app/routers/expenses.py", "required": True},
                    {"artifact_type": "api_service_source", "path_hint": "app/routers/imports.py", "required": True},
                    {"artifact_type": "api_service_source", "path_hint": "app/routers/dashboard.py", "required": True},
                ],
                "acceptance_checks": [
                    {"check_type": "file_exists", "description": "Required API router files exist.", "required": True},
                ],
            },
            {
                "id": "frontend_ui",
                "description": (
                    "Implement native HTML/CSS/JavaScript UI for expense list, filters, edit form, "
                    "CSV import, receipt upload, dashboard summary, loading states, and errors. "
                    "Write the required assets exactly at app/static/index.html, "
                    "app/static/styles.css, and app/static/app.js; do not create duplicate "
                    "root static/ assets or nested app/static/css and app/static/js copies."
                ),
                "agent": frontend_agent,
                "priority": 4,
                "dependencies": ["api_features"],
                "deliverables": [
                    {"artifact_type": "frontend_source", "path_hint": "app/static/index.html", "required": True},
                    {"artifact_type": "frontend_source", "path_hint": "app/static/styles.css", "required": True},
                    {"artifact_type": "frontend_source", "path_hint": "app/static/app.js", "required": True},
                ],
                "acceptance_checks": [
                    {"check_type": "file_exists", "description": "Frontend assets exist.", "required": True},
                ],
            },
            {
                "id": "pytest_suite",
                "description": (
                    "Write pytest coverage for CRUD APIs, filtering, dashboard summaries, "
                    "and CSV import validation. Prefer synchronous FastAPI TestClient tests. "
                    "Do not mark tests with pytest.mark.anyio or pytest.mark.asyncio unless "
                    "the dependency manifest and backend fixture make that backend runnable. "
                    "If pytest-anyio is used, include an anyio_backend fixture returning "
                    "'asyncio' so the suite does not require trio unless trio is explicitly "
                    "declared as a dependency."
                ),
                "agent": backend_agent,
                "priority": 5,
                "dependencies": ["api_features"],
                "deliverables": [
                    {"artifact_type": "test_source", "path_hint": "tests/test_api.py", "required": True},
                ],
                "acceptance_checks": [
                    {"check_type": "file_exists", "description": "Pytest source exists and is runnable with declared dependencies.", "required": True},
                ],
            },
            {
                "id": "docs",
                "description": "Create concise README.md and TESTING.md with startup, test commands, implemented features, and known limits.",
                "agent": general_agent,
                "priority": 6,
                "dependencies": ["frontend_ui", "pytest_suite"],
                "deliverables": [
                    {"artifact_type": "documentation", "path_hint": "README.md", "required": True},
                    {"artifact_type": "documentation", "path_hint": "TESTING.md", "required": True},
                ],
                "acceptance_checks": [
                    {"check_type": "file_exists", "description": "README.md and TESTING.md exist.", "required": True},
                ],
            },
        ]

    def _parse_deliverable_specs(self, subtask_data: Dict[str, Any]) -> List[DeliverableSpec]:
        raw_items = subtask_data.get("deliverables") or subtask_data.get("expected_deliverables") or []
        deliverables: List[DeliverableSpec] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            artifact_type = str(item.get("artifact_type") or "").strip()
            if not artifact_type:
                continue
            deliverables.append(
                DeliverableSpec(
                    artifact_type=artifact_type,
                    required=bool(item.get("required", True)),
                    path_hint=item.get("path_hint"),
                    description=str(item.get("description") or "").strip(),
                )
            )
        return deliverables

    def _repair_parsed_deliverable_path_hints(
        self,
        parsed_deliverables: List[DeliverableSpec],
        parsed_checks: List[AcceptanceCheck],
        inferred_deliverables: List[DeliverableSpec],
        inferred_checks: List[AcceptanceCheck],
    ) -> tuple[List[DeliverableSpec], List[AcceptanceCheck]]:
        """Fill missing LLM path hints from deterministic filename inference.

        Owner LLMs sometimes emit a generic ``{"artifact_type": "file"}`` for a
        subtask whose natural-language description clearly says ``Create
        app.js``.  Treating that generic item as authoritative creates false
        coverage gaps, so deterministic path hints are merged back in.
        """
        inferred_with_paths = [item for item in inferred_deliverables if item.path_hint]
        if not inferred_with_paths:
            return parsed_deliverables, parsed_checks

        repaired: List[DeliverableSpec] = []
        used_paths: set[str] = set()
        inferred_iter = iter(inferred_with_paths)
        for item in parsed_deliverables:
            if item.path_hint:
                repaired.append(item)
                used_paths.add(str(item.path_hint))
                continue
            inferred = next(inferred_iter, None)
            if inferred is None:
                repaired.append(item)
                continue
            item.path_hint = inferred.path_hint
            if item.artifact_type == "file" and inferred.artifact_type != "file":
                item.artifact_type = inferred.artifact_type
            if not item.description:
                item.description = inferred.description
            repaired.append(item)
            used_paths.add(str(inferred.path_hint))

        for inferred in inferred_with_paths:
            if str(inferred.path_hint) in used_paths:
                continue
            if any(item.path_hint == inferred.path_hint for item in repaired):
                continue
            repaired.append(inferred)
            used_paths.add(str(inferred.path_hint))

        check_types = {check.check_type for check in parsed_checks}
        repaired_checks = list(parsed_checks)
        for check in inferred_checks:
            if check.check_type in check_types:
                continue
            repaired_checks.append(check)
            check_types.add(check.check_type)
        return repaired, repaired_checks

    def _infer_subtask_deliverables(
        self,
        description: str,
        agent_id: str,
        project_dir: Optional[str] = None,
    ) -> tuple[List[DeliverableSpec], List[AcceptanceCheck]]:
        """Rule-based fallback when LLM returns empty deliverables for a subtask.

        Infers artifact type from agent_id and description keywords.
        Falls back to generic 'file' with path_hint from description if no keyword matches.
        """
        import re

        deliverables: List[DeliverableSpec] = []
        checks: List[AcceptanceCheck] = []
        semantic_description = description.split("[CRITICAL]", 1)[0]
        text = re.sub(r"[/~][^\s,.;)]+", " ", semantic_description).lower()
        try:
            from .delivery_contract import _mentions_api_service, _mentions_packaged_macos_app
        except Exception:
            _mentions_api_service = lambda value: bool(  # type: ignore
                re.search(r"\b(rest\s*api|api\s+service|backend|fastapi|flask|django|endpoint|endpoints|controller|handler|server)\b", value)
            )
            _mentions_packaged_macos_app = lambda value: bool(  # type: ignore
                re.search(r"\.app\b", value)
                or re.search(r"\b(swiftui|appkit)\b", value)
                or re.search(r"\b(macos|ios|desktop)\s+(app|application|bundle)\b(?!\s+(aesthetic|style|look|feel|visual|inspired))", value)
            )

        def add(artifact_type: str, desc: str, path_hint: Optional[str] = None) -> None:
            if any(d.artifact_type == artifact_type and d.path_hint == path_hint for d in deliverables):
                return
            deliverables.append(DeliverableSpec(
                artifact_type=artifact_type,
                required=True,
                path_hint=path_hint,
                description=desc,
            ))

        def add_check(check_type: str, desc: str) -> None:
            if any(c.check_type == check_type for c in checks):
                return
            checks.append(AcceptanceCheck(check_type=check_type, description=desc, required=True))

        is_structure_only_task = bool(
            re.search(
                r"\b(directory structure|project structure|folder structure|folders?|directories|scaffold|skeleton)\b",
                text,
            )
        )
        is_dependency_manifest_task = bool(
            re.search(r'\b(create|write|update)\s+(?:a\s+)?(?:requirements\.txt|package\.json|pyproject\.toml)\b', text)
            or re.search(r'\b(requirements\.txt|package\.json|pyproject\.toml)\b.{0,60}\b(with|containing|dependencies|dependency list|install dependencies)\b', text)
            or re.search(r'\b(dependencies|dependency list|install dependencies)\b.{0,40}\b(requirements\.txt|package\.json|pyproject\.toml)\b', text)
        )
        is_test_data_task = bool(
            re.search(r'\b(sample|fixture|test data|data file)\b.{0,80}\b(csv|json|yaml|yml)\b', text)
            or re.search(r'\btests?/[\w./-]+\.(csv|json|yaml|yml)\b', text)
        )
        is_planning_only_task = bool(
            not re.search(
                r"\b(create|write|produce|deliver|output|generate|implement|build|add|update|modify|repair|fix)\b",
                text,
            )
            and re.search(
                r"\b(review|plan|planning|define|analy[sz]e|analysis|architecture|api contracts?|data flow|"
                r"capability matrix|requirements?)\b",
                text,
            )
        )

        test_suite_intent = (
            not is_structure_only_task
            and not is_dependency_manifest_task
            and not is_test_data_task
            and not re.search(r'\b(readme|testing\.md|docs?/[^\s,.;)]*\.md|documentation|manual testing checklist)\b', text)
            and (
                re.search(
                    r'\b(write|create|implement|add|build)\b.{0,60}\b(pytest|tests?|test suite|unit tests?|integration tests?|e2e tests?)\b',
                    text,
                )
                or re.search(
                    r'\b(pytest|tests?|test suite|unit tests?|integration tests?|e2e tests?)\b.{0,80}\b(cover|coverage|for|against|validate|verify)\b',
                    text,
                )
                or re.search(r'(编写|创建|实现|新增).{0,40}(测试|测试套件|单元测试|集成测试|端到端测试)', text)
            )
        )

        pre_path_hints = self._extract_path_hints(semantic_description, project_dir)
        forbidden_pre_path_keys = {
            canonical_requirement_key(path_hint)
            for path_hint in extract_forbidden_path_hints(semantic_description)
        }
        pre_path_hints = [
            path_hint for path_hint in pre_path_hints
            if canonical_requirement_key(path_hint) not in forbidden_pre_path_keys
            and not is_runtime_data_path_hint(semantic_description, path_hint)
        ]
        backend_detection_text = _strip_agent_capability_label_context(text)
        static_frontend_file_scope = _is_static_frontend_file_scope(semantic_description, pre_path_hints)

        if test_suite_intent:
            add("test_suite", "Automated test suite files must be produced.")
            add_check("test_suite_exists", "Verify that concrete automated tests exist.")
        elif (
            not is_dependency_manifest_task
            and _mentions_api_service(backend_detection_text)
            and not static_frontend_file_scope
        ):
            add("api_service_source", "Backend API service implementation files must be produced.")
            add_check("api_source_exists", "Verify that concrete API service implementation files exist.")
        elif has_container_delivery_intent(text) or re.search(r'\b(deploy|nginx|ci)\b', text):
            add("dockerfile", "Container build configuration file must be produced.")
            add_check("container_config_exists", "Verify that container build configuration exists.")
        elif re.search(r'\b(react|vue|angular|frontend|typescript|ui|component|dashboard)\b', text):
            add("frontend_source", "Frontend source files must be produced.")
            add_check("frontend_source_exists", "Verify that concrete frontend implementation files exist.")
        elif _mentions_packaged_macos_app(text):
            add("macos_app_bundle", "Packaged macOS/iOS application bundle must be produced.")
            add_check("packaged_app_exists", "Verify that the packaged application bundle exists.")

        # Add concrete file path hints from the description alongside generic types.
        # This way, a FastAPI subtask gets both ``api_service_source`` and
        # ``file`` deliverables for ``main.py``, ``models.py``, etc.
        path_hints = self._extract_path_hints(semantic_description, project_dir)
        forbidden_path_keys = {
            canonical_requirement_key(path_hint)
            for path_hint in extract_forbidden_path_hints(semantic_description)
        }
        path_hints = [
            path_hint for path_hint in path_hints
            if canonical_requirement_key(path_hint) not in forbidden_path_keys
            and not is_runtime_data_path_hint(semantic_description, path_hint)
        ]
        for path_hint in path_hints:
            add("file", f"Output file must be produced: {path_hint}", path_hint=path_hint)
        if path_hints:
            add_check("file_exists", "Verify that explicitly requested output files exist.")

        if not deliverables:
            if is_structure_only_task:
                add_check("project_structure_exists", "Verify that requested project directories were created.")
                return deliverables, checks
            if is_planning_only_task:
                add_check("planning_review_completed", "Verify that the planning or review summary was completed.")
                return deliverables, checks
            path_hint = path_hints[0] if path_hints else None
            add("file", f"Output file must be produced: {path_hint or description[:60]}", path_hint=path_hint)
            add_check("file_exists", f"Verify that the output file exists: {path_hint or 'check description'}")

        return deliverables, checks

    def _extract_path_hints(self, description: str, project_dir: Optional[str] = None) -> List[str]:
        """Extract explicit file paths from a subtask description.

        Returns a list of file-path candidates, ordered by extraction confidence
        (backtick-delimited first, then contextual, then bare). Common non-file
        words and module-like dotted names are ignored.
        """
        import re
        try:
            from .delivery_contract import _mentions_api_service, _mentions_packaged_macos_app
        except Exception:
            _mentions_api_service = lambda value: bool(  # type: ignore
                re.search(r"\b(rest\s*api|api\s+service|backend|fastapi|flask|django|endpoint|endpoints|controller|handler|server)\b", value)
            )
            _mentions_packaged_macos_app = lambda value: bool(  # type: ignore
                re.search(r"\.app\b", value)
                or re.search(r"\b(swiftui|appkit)\b", value)
                or re.search(r"\b(macos|ios|desktop)\s+(app|application|bundle)\b(?!\s+(aesthetic|style|look|feel|visual|inspired))", value)
            )

        patterns = [
            r'`([^`\n]+\.\w+)`',
            r'(?:to|into|in)[:\s]+((?:/?[\w.-]+/)*[\w.-]+\.\w{1,10})',
            r'((?:/?[\w.-]+/)+[\w.-]+\.\w{1,10})',
            r'\b([\w.-]+\.\w{1,10})\b',
        ]
        hints: List[str] = []
        seen: set[str] = set()

        for pattern in patterns:
            for match in re.finditer(pattern, description, re.IGNORECASE):
                candidate = normalize_path_hint(match.group(1))
                if not candidate:
                    continue

                if os.path.isabs(candidate) and project_dir:
                    try:
                        real_candidate = os.path.realpath(candidate)
                        real_project = os.path.realpath(project_dir)
                        if real_candidate.startswith(real_project + os.sep):
                            candidate = os.path.relpath(real_candidate, real_project)
                            candidate = candidate.replace("\\", "/")
                    except Exception:
                        pass

                for expanded in expand_path_hint_alternatives(candidate):
                    if not is_probable_deliverable_path(expanded):
                        continue
                    if _is_subtask_reference_only_path_hint(description, expanded):
                        continue
                    if expanded in seen:
                        continue
                    hints.append(expanded)
                    seen.add(expanded)

        return dedupe_requirement_path_hints(hints)

    def _extract_path_hint(self, description: str, project_dir: Optional[str] = None) -> Optional[str]:
        hints = self._extract_path_hints(description, project_dir)
        return hints[0] if hints else None

    def _parse_acceptance_checks(self, subtask_data: Dict[str, Any]) -> List[AcceptanceCheck]:
        raw_items = subtask_data.get("acceptance_checks") or []
        checks: List[AcceptanceCheck] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            check_type = str(item.get("check_type") or "").strip()
            if not check_type:
                continue
            checks.append(
                AcceptanceCheck(
                    check_type=check_type,
                    description=str(item.get("description") or "").strip(),
                    required=bool(item.get("required", True)),
                )
            )
        return checks

    def _infer_task_contract_requirements(self, task: Task) -> tuple[List[DeliverableSpec], List[AcceptanceCheck]]:
        """Infer a conservative deliverable checklist from the original user request.

        Uses word-boundary-aware matching and mutual exclusion to avoid false positives:
        - "ui" alone is too broad (matches "build", "suite", "quick", "quit");
          use \\bui\\b or explicit UI phrases
        - Backend-only indicators (REST API, FastAPI, Flask, Django) suppress frontend_source
          unless frontend keywords are explicitly present
        """
        text = task.description.lower()
        deliverables: List[DeliverableSpec] = []
        checks: List[AcceptanceCheck] = []

        import re
        try:
            from .delivery_contract import _mentions_api_service, _mentions_packaged_macos_app
        except Exception:
            _mentions_api_service = lambda value: bool(  # type: ignore
                re.search(r"\b(rest\s*api|api\s+service|backend|fastapi|flask|django|endpoint|endpoints|controller|handler|server)\b", value)
            )
            _mentions_packaged_macos_app = lambda value: bool(  # type: ignore
                re.search(r"\.app\b", value)
                or re.search(r"\b(swiftui|appkit)\b", value)
                or re.search(r"\b(macos|ios|desktop)\s+(app|application|bundle)\b(?!\s+(aesthetic|style|look|feel|visual|inspired))", value)
            )

        def add_deliverable(artifact_type: str, description: str, path_hint: Optional[str] = None) -> None:
            if any(item.artifact_type == artifact_type and item.path_hint == path_hint for item in deliverables):
                return
            deliverables.append(
                DeliverableSpec(
                    artifact_type=artifact_type,
                    required=True,
                    path_hint=path_hint,
                    description=description,
                )
            )

        def add_check(check_type: str, description: str) -> None:
            if any(item.check_type == check_type for item in checks):
                return
            checks.append(AcceptanceCheck(check_type=check_type, description=description, required=True))

        def has_frontend_keyword(t: str) -> bool:
            dashboard_backend_context = bool(
                re.search(
                    r'\bdashboard\b.{0,80}\b(api|endpoint|endpoints|route|routes|controller|handler|summary|summaries)\b',
                    t,
                )
                or re.search(
                    r'\b(api|endpoint|endpoints|route|routes|controller|handler)\b.{0,80}\bdashboard\b',
                    t,
                )
            )
            patterns = [
                r'\breact\b', r'\bvue\b', r'\bangular\b',
                r'\btypescript\b',
                r'\bfrontend\b', r'\bfront-end\b',
                r'\bweb\s+ui\b', r'\buser\s+interface\b',
                r'\bpage\b', r'\bcomponent\b',
                r'\bmacos\s+ui\b', r'\bnative\s+ui\b',
            ]
            for p in patterns:
                if re.search(p, t):
                    return True
            if not dashboard_backend_context and re.search(r'\bdashboard\b', t):
                return True
            return False

        def has_backend_only_keyword(t: str) -> bool:
            return _mentions_api_service(t)

        has_frontend = has_frontend_keyword(text)
        has_backend = has_backend_only_keyword(text)

        if _mentions_packaged_macos_app(text) or 'macos 应用' in text:
            add_deliverable(
                "macos_app_bundle",
                "A runnable packaged macOS .app bundle must be produced under the project directory.",
            )
            add_check("packaged_app_exists", "Verify that the requested packaged .app deliverable exists.")

        if has_backend:
            add_deliverable(
                "api_service_source",
                "Backend API service source files must be produced, not only notes or documentation.",
            )
            add_check("api_source_exists", "Verify that concrete API service implementation files exist.")

        if has_container_delivery_intent(task.description):
            add_deliverable("dockerfile", "A Dockerfile or container build configuration must be produced.")
            add_check("container_config_exists", "Verify that container build configuration exists.")

        if has_frontend:
            add_deliverable("frontend_source", "Frontend source files must be produced when UI work is requested.")
            add_check("frontend_source_exists", "Verify that concrete frontend implementation files exist.")

        return deliverables, checks

    def _aggregate_contract_requirements(
        self,
        target_contract: TaskContract,
        source_contracts: List[Dict[str, Any]],
    ) -> None:
        """Aggregate deliverables and acceptance checks from source contracts into the target contract.

        Deduplicates deliverables by ``artifact_type + path_hint`` and acceptance checks by ``check_type``.
        """
        manifest = self._state.get_requirement_manifest(target_contract.task_id) if target_contract.level == "task" else None
        manifest_allowed_paths = {
            canonical_requirement_key(item.get("path_hint"))
            for item in (manifest or {}).get("deliverables", []) or []
            if item.get("required", True) and item.get("path_hint")
        }

        existing_deliverable_keys = {
            (d.artifact_type, d.path_hint or "")
            for d in target_contract.expected_deliverables
        }
        for src in source_contracts:
            kept_deliverables: List[DeliverableSpec] = []
            for d in src.get("expected_deliverables", []):
                path_hint = d.get("path_hint")
                if target_contract.level == "task":
                    if not path_hint:
                        continue
                    if canonical_requirement_key(path_hint) not in manifest_allowed_paths:
                        continue
                key = (d.get("artifact_type", ""), d.get("path_hint") or "")
                if key not in existing_deliverable_keys:
                    deliverable = DeliverableSpec(
                        artifact_type=d.get("artifact_type", ""),
                        required=d.get("required", True),
                        path_hint=path_hint,
                        description=d.get("description", ""),
                    )
                    target_contract.expected_deliverables.append(deliverable)
                    kept_deliverables.append(deliverable)
                    existing_deliverable_keys.add(key)
                elif target_contract.level == "task" and path_hint:
                    kept_deliverables.append(
                        DeliverableSpec(
                            artifact_type=d.get("artifact_type", ""),
                            required=d.get("required", True),
                            path_hint=path_hint,
                            description=d.get("description", ""),
                        )
                    )

        existing_check_types = {c.check_type for c in target_contract.acceptance_checks}
        for src in source_contracts:
            if target_contract.level == "task":
                retained_path_hints = {
                    d.get("path_hint")
                    for d in src.get("expected_deliverables", [])
                    if d.get("path_hint") and canonical_requirement_key(d.get("path_hint")) in manifest_allowed_paths
                }
                if not retained_path_hints:
                    continue
            for c in src.get("acceptance_checks", []):
                ct = c.get("check_type", "")
                if target_contract.level == "task" and ct != "file_exists":
                    continue
                if ct and ct not in existing_check_types:
                    target_contract.acceptance_checks.append(
                        AcceptanceCheck(
                            check_type=ct,
                            description=c.get("description", ""),
                            required=c.get("required", True),
                        )
                    )
                    existing_check_types.add(ct)

    def _build_decomposition_message(self, task: Task, context: Optional[Dict[str, Any]]) -> str:
        parts = [f"Task: {task.description}"]
        if context:
            parts.append(f"Context: {json.dumps(context, ensure_ascii=False)}")
        try:
            from .delivery_contract import _extract_allowed_documentation_files
            allowed_docs = _extract_allowed_documentation_files(task.description)
        except Exception:
            allowed_docs = []
        if allowed_docs:
            parts.append(
                "Documentation scope: create only these documentation files: "
                + ", ".join(allowed_docs)
                + ". Do not create SPEC.md, DESIGN.md, extra plans, or other documentation files."
            )
        task_types = [str(t).lower() for t in (getattr(task, "task_types", None) or [])]
        if "functional" in task_types:
            parts.append(
                "Delivery governance: this is a FUNCTIONAL task. Decompose around user-visible capabilities "
                "and runnable evidence. Required deliverables should be source files, tests, and docs that "
                "implement/verify behavior. Runtime state files such as todo.json, cache databases, or default "
                "storage files are implementation details, not required deliverables. Placeholder files such as "
                "__init__.py are auxiliary and must not be acceptance deliverables unless the user explicitly "
                "asked for them as final files. Required path_hints are canonical: do not satisfy a required "
                "file by creating duplicate equivalent files in alternate directories. Do not leave .pytest_cache, "
                "__pycache__, virtualenvs, runtime DBs, or generated cache files in the project. "
                "Do not create a final validation/checking-only subtask such as "
                "'run the app', 'execute pytest', 'verify all endpoints', or 'check current project state'; "
                "deterministic owner acceptance will run probes after implementation. Do not add authentication, login, users, password "
                "hashing, OAuth/JWT, or role systems unless the user explicitly asks for them."
            )
            text = (task.description or "").lower()
            is_static_web = (
                "static web" in text
                or "file://" in text
                or ("index.html" in text and ("styles.css" in text or "app.js" in text))
            )
            if is_static_web:
                parts.append(
                    "Static web decomposition rule: if the user requests named sections, agent cards, controls, "
                    "or exact files, include those requirements in the implementation subtasks verbatim. The "
                    "index.html subtask must create the visible semantic DOM skeleton for requested sections and "
                    "cards directly in HTML, including .agent-card for Local Agents and .llm-card for Cloud LLMs; "
                    "do not plan a JS-only render where the initial HTML contains only empty containers. If the "
                    "task requests responsive or narrow-screen layout, include an explicit CSS requirement to "
                    "collapse the actual multi-column selectors or make matrix sections horizontally contained "
                    "so mobile viewports do not create document-level overflow."
                )
        if "artifact" in task_types:
            parts.append(
                "Delivery governance: this task includes ARTIFACT delivery. Explicit user-requested files are "
                "authoritative deliverables; do not add extra required files outside that contract."
            )
        return "\n".join(parts)

    def _select_agent(
        self,
        subtask_data: Dict[str, Any],
        available_agent_ids: List[str],
        project_dir: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        available_agent_ids = [
            normalized
            for agent_id in available_agent_ids
            if (normalized := normalize_agent_id(agent_id) or agent_id)
        ]
        if subtask_data.get("agent"):
            subtask_data = {
                **subtask_data,
                "agent": normalize_agent_id(subtask_data.get("agent")) or subtask_data.get("agent"),
            }
        text = " ".join([
            subtask_data.get("description", ""),
            subtask_data.get("agent", ""),
        ]).lower()
        workspace_capable_agents = [
            agent_id for agent_id in (*LOCAL_CLI_AGENT_IDS, *_CLOUD_AGENT_IDS)
            if agent_id in available_agent_ids
        ]

        design_keywords = (
            "architecture",
            "design",
            "schema",
            "openapi",
            "specification",
            "topology",
            "plan",
        )

        implementation_keywords = (
            "build",
            "create",
            "implement",
            "write",
            "generate",
            "modify",
            "update",
            "setup",
            "configure",
        )

        def _prefer_workspace_capable(candidate: str) -> str:
            is_design_task = any(keyword in text for keyword in design_keywords)
            is_implementation_task = any(keyword in text for keyword in implementation_keywords)

            if (
                project_dir
                and candidate == "claude"
                and "deepseek" in workspace_capable_agents
                and is_implementation_task
                and not is_design_task
            ):
                logger.info(
                    "_select_agent: overriding claude -> deepseek for project_dir implementation task to prefer stable workspace execution"
                )
                return "deepseek"
            if not project_dir or candidate in workspace_capable_agents or not workspace_capable_agents:
                return candidate
            fallback = LOCAL_AGENT_ID if LOCAL_AGENT_ID in workspace_capable_agents else workspace_capable_agents[0]
            logger.info(
                "_select_agent: overriding %s -> %s because project_dir task requires a workspace-capable agent",
                candidate,
                fallback,
            )
            return fallback

        def _keyword_matches(keyword: str) -> bool:
            if " " in keyword or "-" in keyword or any(ord(ch) > 127 for ch in keyword):
                return keyword in text
            pattern = rf"(?<![a-z0-9_]){re.escape(keyword)}(?![a-z0-9_])"
            return re.search(pattern, text) is not None

        native_skill_match = self._select_native_skill_match(
            subtask_data,
            available_agent_ids,
            context,
        )
        if native_skill_match:
            selected_agent_id = _prefer_workspace_capable(str(native_skill_match["agent_id"]))
            logger.info(
                "_select_agent: selected %s because native skill %s matched terms %s",
                selected_agent_id,
                native_skill_match.get("skill_name"),
                native_skill_match.get("matched_terms"),
            )
            return selected_agent_id

        keyword_map = {
            "claude": ["architecture", "openapi", "schema", "design"],
            "minimax": ["devops", "docker", "deploy", "nginx", "ci"],
            "deepseek": ["backend", "fastapi", "pydantic", "rest api", "endpoint", "route", "sqlite", "sqlalchemy"],
            "hermes": [
                "frontend",
                "front-end",
                "react",
                "ui",
                "html",
                "css",
                "javascript",
                "typescript",
                "component",
                "page",
                "browser",
                "fetch api",
                "前端",
                "页面",
                "界面",
            ],
        }

        suggested = subtask_data.get("agent", "").lower()

        keyword_selected_agent_id = None
        keywords = []

        def _matched_keywords(kws: List[str]) -> List[str]:
            return [kw for kw in kws if _keyword_matches(kw)]

        def _select_keyword_agent(agent_id: str, matched: List[str]) -> bool:
            nonlocal keyword_selected_agent_id, keywords
            if agent_id not in available_agent_ids or not matched:
                return False
            keyword_selected_agent_id = _prefer_workspace_capable(agent_id)
            keywords = matched
            return True

        strong_backend_keywords = [
            "fastapi",
            "pydantic",
            "sqlite",
            "sqlalchemy",
            "database",
            "database.py",
            "models.py",
            "api route",
            "api routes",
            "crud api",
            "endpoint",
            "route",
            "backend service",
            "backend with",
            "build backend",
            "implement backend",
            "后端",
        ]
        backend_matches = _matched_keywords(keyword_map["deepseek"])
        frontend_matches = _matched_keywords(keyword_map["hermes"])
        strong_backend_matches = _matched_keywords(strong_backend_keywords)

        for agent_id in ("claude", "minimax"):
            if _select_keyword_agent(agent_id, _matched_keywords(keyword_map[agent_id])):
                break

        if not keyword_selected_agent_id:
            if strong_backend_matches:
                _select_keyword_agent("deepseek", strong_backend_matches)
            elif frontend_matches:
                _select_keyword_agent("hermes", frontend_matches)
            elif backend_matches:
                _select_keyword_agent("deepseek", backend_matches)

        if keyword_selected_agent_id:
            if suggested and suggested in available_agent_ids and suggested != keyword_selected_agent_id:
                logger.info(
                    "_select_agent: overriding suggested %s -> %s because subtask text matched deterministic keywords %s",
                    suggested,
                    keyword_selected_agent_id,
                    keywords,
                )
            logger.info(f"_select_agent: selected {keyword_selected_agent_id} for subtask (available={available_agent_ids}, keywords={keywords})")
            return keyword_selected_agent_id

        if suggested and suggested in available_agent_ids:
            selected_agent_id = _prefer_workspace_capable(suggested)
            logger.info(f"_select_agent: selected {selected_agent_id} for subtask (available={available_agent_ids}, keywords={keywords})")
            return selected_agent_id

        selected_agent_id = None
        if available_agent_ids:
            selected_agent_id = _prefer_workspace_capable(available_agent_ids[0])
            keywords = []

        if selected_agent_id is None:
            raise ValueError("No available agents to assign subtask")

        logger.info(f"_select_agent: selected {selected_agent_id} for subtask (available={available_agent_ids}, keywords={keywords})")
        return selected_agent_id

    def _build_native_skill_routing_suffix(
        self,
        context: Optional[Dict[str, Any]],
        selected_agent_id: str,
        subtask_data: Dict[str, Any],
    ) -> str:
        match = self._select_native_skill_match(
            subtask_data,
            [selected_agent_id],
            context,
        )
        if not match:
            return ""
        matched_terms = ", ".join(match.get("matched_terms", [])[:6])
        reason = f"- Reason: Native skill match: {match['skill_name']}"
        if matched_terms:
            reason += f" (matched: {matched_terms})"
        return (
            "\n\n[ROUTING DECISION]\n"
            f"- Selected agent: {normalize_agent_id(selected_agent_id) or selected_agent_id}\n"
            f"{reason}"
        )

    def _select_native_skill_match(
        self,
        subtask_data: Dict[str, Any],
        available_agent_ids: List[str],
        context: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        native_skills_by_agent = (
            ((context or {}).get("agent_capabilities") or {}).get("native_skills")
            or {}
        )
        if not isinstance(native_skills_by_agent, dict):
            return None

        normalized_available = [
            normalize_agent_id(agent_id) or agent_id
            for agent_id in available_agent_ids
            if agent_id
        ]
        task_text = " ".join(
            str(subtask_data.get(key, ""))
            for key in ("id", "description")
            if subtask_data.get(key)
        )
        task_tokens = self._native_skill_routing_tokens(task_text)
        if not task_tokens:
            return None

        best_match: Optional[Dict[str, Any]] = None
        for agent_id in normalized_available:
            skills = (
                native_skills_by_agent.get(agent_id)
                or native_skills_by_agent.get(normalize_agent_id(agent_id) or agent_id)
                or []
            )
            if not isinstance(skills, list):
                continue
            for skill in skills:
                if not isinstance(skill, dict):
                    continue
                if not is_native_skill_available(skill):
                    continue
                skill_name = str(skill.get("name") or skill.get("id") or "").strip()
                if not skill_name:
                    continue
                skill_text_parts = [
                    skill_name,
                    str(skill.get("id") or ""),
                    str(skill.get("description") or ""),
                    str(skill.get("source") or ""),
                ]
                tags = skill.get("tags")
                if isinstance(tags, list):
                    skill_text_parts.extend(str(tag) for tag in tags)
                skill_tokens = self._native_skill_routing_tokens(" ".join(skill_text_parts))
                if not skill_tokens:
                    continue
                matched_terms = sorted(task_tokens.intersection(skill_tokens))
                if len(matched_terms) < 2:
                    continue
                score = len(matched_terms)
                skill_name_lower = skill_name.lower()
                if skill_name_lower and skill_name_lower in task_text.lower():
                    score += 3
                if skill.get("description"):
                    score += 1
                candidate = {
                    "agent_id": agent_id,
                    "skill_name": skill_name,
                    "matched_terms": matched_terms,
                    "score": score,
                }
                if not best_match or score > int(best_match.get("score", 0)):
                    best_match = candidate
        return best_match

    def _native_skill_routing_tokens(self, text: str) -> Set[str]:
        tokens = {
            token
            for token in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text.lower())
            if token not in self.NATIVE_SKILL_ROUTING_STOPWORDS
        }
        expanded: Set[str] = set()
        for token in tokens:
            expanded.add(token)
            if "-" in token or "_" in token:
                expanded.update(
                    part
                    for part in re.split(r"[-_]+", token)
                    if len(part) >= 3 and part not in self.NATIVE_SKILL_ROUTING_STOPWORDS
                )
        return expanded

    def _parse_decomposition(self, response_text: str) -> Dict[str, Any]:
        """Parse LLM JSON response into structured decomposition data."""
        data = self._parse_json_response(response_text)
        if "subtasks" not in data:
            logger.warning("Failed to parse decomposition JSON from LLM response")
            return {"subtasks": []}
        return data

    def _parse_json_response(self, response_text: str) -> Dict[str, Any]:
        """Parse LLM JSON response (generic, no assumed structure).

        Tries multiple extraction strategies:
        1. Direct JSON parse
        2. Code block extraction (```json ... ```)
        3. Balanced object extraction (first valid {...})
        4. Key-value pattern extraction (passed: true/false, action: approve/fix)
        """
        text = self._strip_thinking_blocks(response_text.strip())

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        for candidate in self._balanced_json_object_candidates(text):
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue

        # Strategy 4: Extract key fields via regex patterns
        result: Dict[str, Any] = {}
        passed_match = re.search(r'["\']?passed["\']?\s*[:=]\s*(true|false)', text, re.IGNORECASE)
        if passed_match:
            result["passed"] = passed_match.group(1).lower() == "true"

        action_match = re.search(r'["\']?action["\']?\s*[:=]\s*["\']?(approve|fix|downgrade|reassign|retry_acceptance)["\']?', text, re.IGNORECASE)
        if action_match:
            result["action"] = action_match.group(1).lower()

        feedback_match = re.search(r'["\']?feedback["\']?\s*[:=]\s*["\']([^"\']+)["\']', text, re.IGNORECASE)
        if feedback_match:
            result["feedback"] = feedback_match.group(1)
        else:
            # Try extracting feedback after "feedback:" without quotes
            feedback_match2 = re.search(r'feedback[:\s]+(.+?)(?:\n|$)', text, re.IGNORECASE)
            if feedback_match2:
                result["feedback"] = feedback_match2.group(1).strip()

        if result:
            logger.info(f"Extracted fields via regex from non-JSON response: {list(result.keys())}")
            return result

        logger.warning(f"Failed to parse JSON from LLM response, raw text (first 500 chars): {text[:500]}")
        return {}

    @staticmethod
    def _strip_thinking_blocks(text: str) -> str:
        """Remove model reasoning wrappers before parsing strict JSON."""
        return re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()

    @staticmethod
    def _balanced_json_object_candidates(text: str) -> List[str]:
        candidates: List[str] = []
        start: Optional[int] = None
        depth = 0
        in_string = False
        escaped = False

        for index, char in enumerate(text):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue
            if char == "{":
                if depth == 0:
                    start = index
                depth += 1
                continue
            if char == "}" and depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(text[start:index + 1])
                    start = None
        return candidates

    def _resolve_dependency_by_text(self, dep_text: str, task: Task) -> List[str]:
        """Fallback: match a dependency text to existing subtask IDs by description."""
        dep_lower = dep_text.lower()
        matched = []
        for st in task.subtasks:
            if dep_lower in st.description.lower() or st.description.lower() in dep_lower:
                matched.append(st.subtask_id)
        return matched

    def assign_waves(self, task: Task) -> Task:
        """Assign wave_number to each SubTask based on DAG topological sort.

        wave_number = max(wave_number of all dependencies) + 1
        SubTasks with no dependencies have wave_number = 1
        """
        if not task.subtasks:
            return task

        # Keep the dedicated decomposition node in wave 0 instead of letting the
        # normal DAG layering logic fold it into the first execution wave.
        decompose_subtasks = [st for st in task.subtasks if st.subtask_id.endswith("-decompose")]
        dag_subtasks = [st for st in task.subtasks if st.subtask_id not in {dst.subtask_id for dst in decompose_subtasks}]
        if not dag_subtasks:
            return task

        subtask_map = {st.subtask_id: st for st in dag_subtasks}
        computed: Dict[str, int] = {}

        def compute_wave(subtask_id: str) -> int:
            if subtask_id in computed:
                return computed[subtask_id]

            st = subtask_map.get(subtask_id)
            if not st:
                computed[subtask_id] = 1
                return 1

            if not st.dependencies:
                computed[subtask_id] = 1
                return 1

            max_dep_wave = 0
            for dep_id in st.dependencies:
                if dep_id in subtask_map:
                    max_dep_wave = max(max_dep_wave, compute_wave(dep_id))

            wave = max_dep_wave + 1
            computed[subtask_id] = wave
            return wave

        for st in dag_subtasks:
            compute_wave(st.subtask_id)

        wave_groups: Dict[int, List[SubTask]] = {}
        for st in decompose_subtasks:
            st.wave_number = 0
            self._state._persist_subtask(st)

        for st in dag_subtasks:
            wn = computed.get(st.subtask_id, 1)
            st.wave_number = wn
            self._state._persist_subtask(st)
            if wn not in wave_groups:
                wave_groups[wn] = []
            wave_groups[wn].append(st)

        existing_wave0 = None
        for w in task.waves:
            if w.wave_number == 0:
                existing_wave0 = w
                break

        task.waves = []
        if existing_wave0:
            task.waves.append(existing_wave0)

        for wn in sorted(wave_groups.keys()):
            from ..models import Wave, JobStatus
            wave = Wave(
                wave_id=f"wave-{uuid.uuid4().hex[:8]}",
                wave_number=wn,
                task_id=task.task_id,
                subtasks=wave_groups[wn],
                status=JobStatus.PENDING,
                is_blocked=False,
                fix_rounds=[]
            )
            task.waves.append(wave)
            # Persist each wave
            self._state._persist_wave(wave)
            wave_contract = TaskContract.new(
                task_id=task.task_id,
                level="wave",
                goal=f"Wave {wn} combined delivery for task: {task.description}",
                wave_number=wn,
                project_dir=task.project_dir,
                context_mode="summary",
            )
            # Aggregate this wave's subtask deliverables into the wave-level contract
            wave_subtask_ids = {st.subtask_id for st in wave_groups[wn]}
            all_c = self._state.get_task_contracts(task.task_id)
            wave_subtask_contracts = [
                c for c in all_c
                if c.get("level") == "subtask" and c.get("subtask_id") in wave_subtask_ids
            ]
            if wave_subtask_contracts:
                self._aggregate_contract_requirements(wave_contract, wave_subtask_contracts)
            self._state.save_task_contract(wave_contract)

        logger.info(f"assign_waves: assigned {len(task.waves)} waves for task {task.task_id}, wave_summary=[{', '.join(f'W{w.wave_number}({len(w.subtasks)}sts)' for w in task.waves)}]")
        return task

    def accept_subtask(self, job: Job) -> AcceptanceResult:
        """
        Build acceptance context, call LLM for judgment, parse result.

        Args:
            job: The completed Job to review.

        Returns:
            AcceptanceResult with level2_passed and feedback.
        """
        task = self._get_task_for_job(job)
        owner_session_id = self._ensure_owner_session_id(task)
        context = self._build_acceptance_context(job)
        heuristic_decision = self._make_structured_subtask_decision(task, job)

        try:
            response = self._llm(
                system_prompt=self.ACCEPTANCE_SYSTEM_PROMPT,
                message=context,
                temperature=0.2,
            )
            acceptance = self._parse_acceptance(response.text, job.subtask_id)
            if acceptance.parse_failed:
                acceptance = self._repair_acceptance_response(response.text, job.subtask_id)
            acceptance = self._merge_structured_decision(acceptance, heuristic_decision, owner_session_id)
            acceptance = self._apply_artifact_contract_acceptance_override(task, job, acceptance)
            acceptance = self._apply_satisfied_contract_review_override(task, job, acceptance, heuristic_decision)
            logger.info(f"accept_subtask: subtask_id={job.subtask_id}, level2_passed={acceptance.level2_passed}, feedback={acceptance.level2_feedback[:100] if acceptance.level2_feedback else 'None'}")
            return acceptance
        except Exception as e:
            logger.error(f"Acceptance review failed for job {job.job_id}: {e}")
            acceptance = AcceptanceResult(
                subtask_id=job.subtask_id,
                level1_passed=True,
                level2_passed=False,
                level2_feedback=f"Acceptance review error: {e}",
                action="retry_acceptance",
                parse_failed=True,
            )
            acceptance = self._merge_structured_decision(acceptance, heuristic_decision, owner_session_id)
            acceptance = self._apply_artifact_contract_acceptance_override(task, job, acceptance)
            acceptance = self._apply_satisfied_contract_review_override(task, job, acceptance, heuristic_decision)
            logger.info(f"accept_subtask: subtask_id={job.subtask_id}, level2_passed={acceptance.level2_passed}, feedback={acceptance.level2_feedback[:100] if acceptance.level2_feedback else 'None'}")
            return acceptance

    def accept_wave(self, task: Task, wave_number: int) -> AcceptanceResult:
        """Wave acceptance with structured decision output."""
        context = self._build_wave_acceptance_context(task, wave_number)
        owner_session_id = self._ensure_owner_session_id(task)
        heuristic_decision = self._make_structured_wave_decision(task, wave_number)
        try:
            response = self._llm(
                system_prompt=self.WAVE_ACCEPTANCE_SYSTEM_PROMPT,
                message=context,
                temperature=0.2,
            )
            acceptance = self._parse_acceptance(response.text, f"wave-{wave_number}")
            if acceptance.parse_failed:
                acceptance = self._repair_acceptance_response(response.text, f"wave-{wave_number}")
            acceptance = self._merge_structured_decision(acceptance, heuristic_decision, owner_session_id)
            return self._normalize_wave_acceptance(acceptance, heuristic_decision)
        except Exception as e:
            logger.error(f"Wave acceptance review failed for task {task.task_id} wave {wave_number}: {e}")
            acceptance = AcceptanceResult(
                subtask_id=f"wave-{wave_number}",
                level1_passed=True,
                level2_passed=False,
                level2_feedback=f"Wave acceptance review error: {e}",
                action="retry_acceptance",
                parse_failed=True,
            )
            acceptance = self._merge_structured_decision(acceptance, heuristic_decision, owner_session_id)
            return self._normalize_wave_acceptance(acceptance, heuristic_decision)

    def _build_acceptance_context(self, job: Job) -> str:
        """Collect subtask description, output, and contract requirements."""
        task = self._get_task_for_job(job)
        owner_session_id = self._ensure_owner_session_id(task)
        recent_records = self._collect_recent_acceptance_records(task)
        accepted_artifacts = self._collect_accepted_artifacts(task)
        task_contracts = self._collect_task_contracts(task)
        wave_number = self._get_wave_number(task, job.subtask_id)
        parts = [
            f"Owner Session ID: {owner_session_id or 'N/A'}",
            f"Task ID: {task.task_id if task else 'N/A'}",
            f"SubTask ID: {job.subtask_id}",
            f"Agent: {job.agent_id}",
            f"Task Description: {job.task_description}",
            f"Wave Number: {wave_number if wave_number is not None else 'N/A'}",
        ]

        if job.result:
            parts.append(f"Output:\n{job.result}")
        else:
            parts.append("Output: (none)")

        if job.error:
            parts.append(f"Error: {job.error}")

        parts.append(f"Accepted Artifacts: {json.dumps(accepted_artifacts, ensure_ascii=False)}")
        parts.append(f"Task Contracts: {json.dumps(task_contracts, ensure_ascii=False)}")
        parts.append(f"Recent Acceptance Records: {json.dumps(recent_records, ensure_ascii=False)}")

        return "\n\n".join(parts)

    def _collect_recent_acceptance_records(self, task: Optional[Task], limit: int = 5) -> List[Dict[str, Any]]:
        persistence = self._get_persistence()
        if task is None or persistence is None:
            return []
        try:
            return persistence.get_acceptance_records(task.task_id)[-limit:]
        except Exception as exc:
            logger.warning(f"Failed to load recent acceptance records for {task.task_id}: {exc}")
            return []

    def _collect_accepted_artifacts(self, task: Optional[Task]) -> List[Dict[str, Any]]:
        persistence = self._get_persistence()
        if task is None or persistence is None:
            return []
        try:
            records = persistence.get_artifact_records(task.task_id)
        except Exception as exc:
            logger.warning(f"Failed to load accepted artifacts for {task.task_id}: {exc}")
            return []
        artifacts: List[Dict[str, Any]] = []
        for item in records:
            if item.get("status", "accepted") != "accepted":
                continue
            metadata = item.get("metadata") or {}
            safe_metadata = {
                key: metadata.get(key)
                for key in ("canonical_subtask_id", "file_size", "normalized_content_ref")
                if metadata.get(key) is not None
            }
            artifacts.append({
                "artifact_id": item.get("artifact_id"),
                "name": item.get("name"),
                "wave_number": item.get("wave_number"),
                "version": item.get("version", 1),
                "content_ref": item.get("content_ref"),
                "metadata": safe_metadata,
                "canonical_subtask_id": metadata.get("canonical_subtask_id"),
            })
        return artifacts

    def _collect_task_contracts(self, task: Optional[Task]) -> List[Dict[str, Any]]:
        persistence = self._get_persistence()
        if task is None or persistence is None:
            return []
        try:
            return persistence.get_task_contracts(task.task_id)
        except Exception as exc:
            logger.warning(f"Failed to load task contracts for {task.task_id}: {exc}")
            return []

    def _get_wave_number(self, task: Optional[Task], subtask_id: str) -> Optional[int]:
        if task is None:
            return None
        for st in task.subtasks:
            if st.subtask_id == subtask_id:
                return getattr(st, "wave_number", None)
        return None

    def _make_structured_subtask_decision(self, task: Optional[Task], job: Job) -> OwnerDecision:
        wave_number = self._get_wave_number(task, job.subtask_id)
        output_text = (job.result or "").lower()
        error_text = (job.error or "").lower()
        recent_records = self._collect_recent_acceptance_records(task)
        artifact_records = self._collect_accepted_artifacts(task)
        artifact_ids = [item.get("artifact_id") for item in artifact_records if item.get("artifact_id")]

        repeated_failures = (
            sum(
                1
                for record in recent_records
                if record.get("decision") in {"fix", "reassign"}
                and self._same_subtask_family(record.get("subtask_id"), job.subtask_id)
            )
            >= 2
        )
        ancillary_only = self._is_ancillary_only_output(output_text) and not self._allows_document_output(
            task,
            job,
        )
        missing_output = not job.result and not self._has_output_artifact(task, job.subtask_id)

        if "dependency" in error_text or "input artifact" in error_text or "upstream" in error_text:
            return OwnerDecision(
                decision="reject",
                root_cause_scope=RootCauseScope.PRIOR_WAVE.value,
                root_cause_wave=max(1, (wave_number or 1) - 1) if wave_number and wave_number > 1 else 1,
                root_cause_artifact_ids=artifact_ids[:3],
                recommended_action=RecommendedAction.PRIOR_WAVE_FIX.value,
                summary="Detected upstream dependency drift that should be fixed in a prior wave.",
                owner_session_id=self._ensure_owner_session_id(task),
                investigation_level=2,
            )

        if missing_output or ancillary_only or repeated_failures:
            failed_checks = []
            if missing_output:
                failed_checks.append("missing_required_deliverable")
            if ancillary_only:
                failed_checks.append("ancillary_only_output")
            if repeated_failures:
                failed_checks.append("repeated_failures")
            return OwnerDecision(
                decision="reject",
                root_cause_scope=RootCauseScope.CURRENT_WAVE.value,
                root_cause_wave=wave_number,
                root_cause_artifact_ids=artifact_ids[:3],
                recommended_action=RecommendedAction.REASSIGN.value if (ancillary_only or repeated_failures) else RecommendedAction.WAVE_FIX.value,
                failed_checks=failed_checks,
                summary="Wave-level issue detected by Owner lightweight checks.",
                owner_session_id=self._ensure_owner_session_id(task),
                investigation_level=2 if repeated_failures or ancillary_only else 0,
            )

        return OwnerDecision(
            decision="approve",
            root_cause_scope=RootCauseScope.CURRENT_SUBTASK.value,
            root_cause_wave=wave_number,
            recommended_action=RecommendedAction.APPROVE.value,
            summary="No blocking issue detected by Owner checks.",
            owner_session_id=self._ensure_owner_session_id(task),
            investigation_level=0,
        )

    def _looks_like_primary_delivery(self, output_text: str) -> bool:
        primary_markers = (
            ".py", ".ts", ".tsx", ".js", "created", "written", "implemented", "saved", "output file", "endpoint",
            "def ", "class ", "function ", "return ", "import ", "const ", "let ",
        )
        return any(marker in output_text for marker in primary_markers)

    def _is_ancillary_only_output(self, output_text: str) -> bool:
        if not output_text:
            return False
        ancillary_markers = ("readme", "notes", "todo", "placeholder", "edge file", "extra file", "only docs")
        concrete_delivery_markers = (
            ".py", ".ts", ".tsx", ".js", ".html", ".css", "endpoint", "api", "schema", "migration",
            "def ", "class ", "function ", "const ", "return ", "handler", "component",
            "stylesheet", "styles", "css", "layout", "modal", "dashboard", "form styling",
            "table design", "responsive",
        )
        return any(marker in output_text for marker in ancillary_markers) and not any(
            marker in output_text for marker in concrete_delivery_markers
        )

    def _same_subtask_family(self, left: Any, right: Any) -> bool:
        """Return True when two subtask ids are retries of the same logical work item."""
        if not left or not right:
            return False
        return re.sub(r"-v\d+$", "", str(left)) == re.sub(r"-v\d+$", "", str(right))

    def _canonical_subtask_id(self, subtask_id: str) -> str:
        base = subtask_id
        while True:
            new_base = re.sub(r"-(?:fix-\d+|v\d+)$", "", base)
            if new_base == base:
                return base
            base = new_base

    def _allows_document_output(self, task: Optional[Task], job: Job) -> bool:
        """Avoid rejecting explicitly requested documentation deliverables as ancillary."""
        text = (job.task_description or "").lower()
        doc_markers = (".md", "readme", "documentation", "usage examples", "install instructions")
        if any(marker in text for marker in doc_markers):
            return True
        if task is None:
            return False
        try:
            contract = self._state.get_contract_by_subtask(task.task_id, job.subtask_id)
        except Exception:
            contract = None
        deliverables = (contract or {}).get("expected_deliverables") or (contract or {}).get("deliverables") or []
        for item in deliverables:
            path_hint = str(item.get("path_hint") or item.get("name") or "").lower()
            artifact_type = str(item.get("artifact_type") or "").lower()
            if path_hint.endswith((".md", ".txt", ".rst")) or artifact_type in {"documentation", "document", "readme"}:
                return True
        return False

    def _has_output_artifact(self, task: Optional[Task], subtask_id: str) -> bool:
        persistence = self._get_persistence()
        if task is None or persistence is None:
            return False
        canonical_id = self._canonical_subtask_id(subtask_id)
        try:
            return any(
                self._artifact_satisfies_subtask(record, subtask_id, canonical_id)
                for record in persistence.get_artifact_records(task.task_id)
            )
        except Exception:
            return False

    def _artifact_satisfies_subtask(
        self,
        artifact: Dict[str, Any],
        subtask_id: str,
        canonical_id: Optional[str] = None,
    ) -> bool:
        if artifact.get("status", "accepted") not in {"accepted", "provisional"}:
            return False

        content_ref = artifact.get("content_ref")
        if content_ref and not os.path.isfile(os.path.realpath(str(content_ref))):
            return False

        canonical_id = canonical_id or self._canonical_subtask_id(subtask_id)
        artifact_subtask_id = str(artifact.get("subtask_id") or "")
        metadata = artifact.get("metadata") or {}
        metadata_canonical = metadata.get("canonical_subtask_id")

        return (
            artifact_subtask_id == subtask_id
            or self._canonical_subtask_id(artifact_subtask_id) == canonical_id
            or metadata_canonical == canonical_id
        )

    def _merge_structured_decision(
        self,
        acceptance: AcceptanceResult,
        decision: OwnerDecision,
        owner_session_id: Optional[str],
    ) -> AcceptanceResult:
        acceptance.decision = decision.decision
        acceptance.root_cause_scope = decision.root_cause_scope
        acceptance.root_cause_wave = decision.root_cause_wave
        acceptance.root_cause_artifact_ids = list(decision.root_cause_artifact_ids)
        acceptance.recommended_action = decision.recommended_action
        acceptance.preferred_agent = decision.preferred_agent
        acceptance.failed_checks = list(decision.failed_checks)
        acceptance.missing_artifacts = list(decision.missing_artifacts)
        acceptance.owner_session_id = owner_session_id or decision.owner_session_id
        acceptance.investigation_level = decision.investigation_level
        # Task 4: structured reject forces level2_passed=False for consistent records
        if decision.recommended_action != RecommendedAction.APPROVE.value:
            if acceptance.action == "approve":
                acceptance.action = "fix" if decision.recommended_action != RecommendedAction.REASSIGN.value else "reassign"
            acceptance.level2_passed = False
            if decision.summary and not acceptance.level2_feedback:
                acceptance.level2_feedback = decision.summary
        elif decision.summary and not acceptance.level2_passed and not acceptance.level2_feedback:
            acceptance.level2_feedback = decision.summary
        return acceptance

    def _apply_artifact_contract_acceptance_override(
        self,
        task: Optional[Task],
        job: Job,
        acceptance: AcceptanceResult,
    ) -> AcceptanceResult:
        """Let explicit artifact contracts be authoritative for artifact-only work.

        README/setup-style files are ancillary for functional delivery, but they
        can be the primary deliverable for artifact tasks. When the persisted
        subtask contract says the requested artifacts exist, lightweight
        "ancillary only" heuristics and fuzzy LLM judgement must not force a
        reassign loop.
        """
        if not self._is_artifact_only_delivery_task(task):
            return acceptance
        if not self._job_satisfies_subtask_contract(task, job):
            return acceptance

        acceptance.level1_passed = True
        acceptance.level2_passed = True
        acceptance.action = "approve"
        acceptance.decision = "approve"
        acceptance.recommended_action = RecommendedAction.APPROVE.value
        acceptance.failed_checks = []
        acceptance.missing_artifacts = []
        if not acceptance.level2_feedback or "ancillary_only_output" in acceptance.level2_feedback:
            acceptance.level2_feedback = "Explicit artifact delivery contract was satisfied."
        return acceptance

    def _apply_satisfied_contract_review_override(
        self,
        task: Optional[Task],
        job: Job,
        acceptance: AcceptanceResult,
        decision: OwnerDecision,
    ) -> AcceptanceResult:
        """Do not reject satisfied code contracts solely for missing pasted source."""
        if task is None or job.error:
            return acceptance
        if decision.recommended_action != RecommendedAction.APPROVE.value:
            return acceptance
        if not self._job_satisfies_subtask_contract(task, job):
            return acceptance

        feedback = (acceptance.level2_feedback or "").lower()
        source_review_markers = (
            "file content",
            "content review",
            "without file content",
            "did not show",
            "did not report",
            "did not confirm",
            "not show",
            "not report",
            "not confirm",
            "no confirmation",
            "not provide details",
            "actual content",
            "actual crud",
            "display or verify",
            "unable to verify implementation",
            "missing ",
            "not found",
            "file does not exist",
            "required file",
        )
        if not feedback or not any(marker in feedback for marker in source_review_markers):
            return acceptance

        acceptance.level1_passed = True
        acceptance.level2_passed = True
        acceptance.action = "approve"
        acceptance.decision = "approve"
        acceptance.recommended_action = RecommendedAction.APPROVE.value
        acceptance.failed_checks = []
        acceptance.missing_artifacts = []
        acceptance.level2_feedback = (
            "Declared subtask contract files exist; source-content paste is not required for subtask approval."
        )
        return acceptance

    def _is_artifact_only_delivery_task(self, task: Optional[Task]) -> bool:
        if task is None:
            return False
        task_types = {
            getattr(item, "value", item)
            for item in (getattr(task, "task_types", None) or [])
        }
        delivery_mode = getattr(getattr(task, "delivery_mode", None), "value", getattr(task, "delivery_mode", None))
        return delivery_mode == "artifact" or task_types == {"artifact"}

    def _job_satisfies_subtask_contract(self, task: Optional[Task], job: Job) -> bool:
        if task is None:
            return False
        persistence = self._get_persistence()
        contracts: List[Dict[str, Any]] = []
        if persistence is not None:
            try:
                contract_subtask_ids = {job.subtask_id, self._canonical_subtask_id(job.subtask_id)}
                contracts = [
                    c for c in persistence.get_task_contracts(task.task_id)
                    if c.get("level") == "subtask" and c.get("subtask_id") in contract_subtask_ids
                ]
            except Exception as exc:
                logger.warning("Failed to load subtask contract for %s: %s", job.subtask_id, exc)

        if not contracts:
            output_file = getattr(job, "output_file", None)
            return bool(output_file and os.path.isfile(os.path.realpath(output_file)))

        required_hints: List[str] = []
        for contract in contracts:
            for deliverable in contract.get("expected_deliverables", []) or []:
                if not deliverable.get("required", True):
                    continue
                path_hint = deliverable.get("path_hint")
                if path_hint:
                    required_hints.append(path_hint)

        if not required_hints:
            output_file = getattr(job, "output_file", None)
            return bool(output_file and os.path.isfile(os.path.realpath(output_file)))

        for path_hint in required_hints:
            base_dir = task.project_dir or contracts[0].get("project_dir")
            if not base_dir and not os.path.isabs(path_hint):
                return False
            candidate = first_existing_candidate(path_hint, base_dir)
            if not candidate or not os.path.isfile(os.path.realpath(candidate)):
                return False
        return True

    def _normalize_wave_acceptance(
        self,
        acceptance: AcceptanceResult,
        decision: OwnerDecision,
    ) -> AcceptanceResult:
        """Make wave-level governance semantics authoritative and self-consistent."""
        if acceptance.parse_failed:
            if decision.recommended_action == RecommendedAction.APPROVE.value:
                acceptance.action = "approve"
                acceptance.recommended_action = RecommendedAction.APPROVE.value
                acceptance.level2_passed = True
                acceptance.level2_feedback = (
                    "Wave acceptance response could not be parsed; deterministic evidence checks passed."
                )
                return acceptance

        if decision.recommended_action == RecommendedAction.APPROVE.value:
            acceptance.action = "approve"
            acceptance.recommended_action = RecommendedAction.APPROVE.value
            acceptance.level2_passed = True
            # Deterministic wave checks are authoritative here.  If an LLM review
            # rejected the wave for future-wave work, do not carry that blocking
            # feedback into the orchestrator record where it can veto approval.
            acceptance.level2_feedback = (
                decision.summary
                or "Current wave is approved for downstream consumption."
            )
            acceptance.failed_checks = []
            acceptance.missing_artifacts = []
            return acceptance

        if decision.recommended_action == RecommendedAction.REASSIGN.value:
            acceptance.action = "reassign"
        else:
            acceptance.action = "fix"
        acceptance.recommended_action = decision.recommended_action
        acceptance.level2_passed = False
        if not acceptance.level2_feedback and decision.summary:
            acceptance.level2_feedback = decision.summary
        return acceptance

    def _make_structured_wave_decision(self, task: Task, wave_number: int) -> OwnerDecision:
        wave_subtasks = [
            st for st in task.subtasks
            if getattr(st, "wave_number", None) == wave_number
            and not self._is_wave_acceptance_remediation_subtask(st.subtask_id)
            and not st.subtask_id.endswith("-decompose")
        ]
        missing_outputs = [
            st.subtask_id
            for st in wave_subtasks
            if not self._subtask_has_delivery_evidence(task, st)
        ]
        repeated_rejections = 0
        persistence = self._get_persistence()
        if persistence is not None:
            try:
                for record in persistence.get_acceptance_records(task.task_id):
                    if record.get("wave_number") == wave_number and record.get("decision") != "approve":
                        repeated_rejections += 1
            except Exception:
                pass

        if missing_outputs:
            return OwnerDecision(
                decision="reject",
                root_cause_scope=RootCauseScope.CURRENT_WAVE.value,
                root_cause_wave=wave_number,
                failed_checks=["missing_required_deliverable"],
                missing_artifacts=missing_outputs,
                recommended_action=RecommendedAction.WAVE_FIX.value,
                summary="Current wave is missing required deliverables.",
                owner_session_id=self._ensure_owner_session_id(task),
                investigation_level=0,
            )

        if repeated_rejections >= 3:
            return OwnerDecision(
                decision="reject",
                root_cause_scope=RootCauseScope.CURRENT_WAVE.value,
                root_cause_wave=wave_number,
                failed_checks=["repeated_wave_rejection"],
                recommended_action=RecommendedAction.REASSIGN.value,
                summary="Current wave repeatedly failed acceptance and should be reassigned.",
                owner_session_id=self._ensure_owner_session_id(task),
                investigation_level=2,
            )

        return OwnerDecision(
            decision="approve",
            root_cause_scope=RootCauseScope.CURRENT_WAVE.value,
            root_cause_wave=wave_number,
            recommended_action=RecommendedAction.APPROVE.value,
            summary="Current wave is approved for downstream consumption.",
            owner_session_id=self._ensure_owner_session_id(task),
            investigation_level=0,
        )

    def _parse_acceptance(self, response_text: str, subtask_id: str) -> AcceptanceResult:
        """Parse LLM response into AcceptanceResult.

        Conservative policy: if JSON cannot be parsed, default to FAILED (not passed)
        to ensure quality. Only approve when we have explicit confirmation.
        """
        data = self._parse_json_response(response_text)

        if not data:
            logger.warning(f"Could not parse acceptance JSON for {subtask_id}, marking as parse_failed")
            return AcceptanceResult(
                subtask_id=subtask_id,
                level1_passed=True,
                level2_passed=False,
                level2_feedback="Acceptance review response could not be parsed.",
                action="retry_acceptance",
                parse_failed=True,
                raw_response=response_text,
            )

        passed = bool(data.get("passed", False))
        feedback = data.get("feedback", "")
        action = data.get("action", "approve" if passed else "fix")

        return AcceptanceResult(
            subtask_id=subtask_id,
            level1_passed=True,
            level2_passed=passed,
            level2_feedback=feedback if not passed else None,
            action=action,
            raw_response=response_text,
        )

    def _build_wave_acceptance_context(self, task: Task, wave_number: int) -> str:
        """Summarize a wave for shadow-mode wave acceptance."""
        wave_subtasks = [
            st for st in task.subtasks
            if st.wave_number == wave_number
            and not self._is_wave_acceptance_remediation_subtask(st.subtask_id)
            and not st.subtask_id.endswith("-decompose")
        ]
        prior_wave_numbers = self._get_prior_approved_waves(task, wave_number)
        available_inputs = self._collect_prior_wave_artifacts(task, wave_number, prior_wave_numbers)
        current_wave_artifacts = self._collect_wave_artifacts(task, wave_number)
        project_tree = self._collect_project_tree(task.project_dir)
        subtask_by_id = {st.subtask_id: st for st in task.subtasks}
        future_wave_subtasks = [
            st for st in task.subtasks
            if getattr(st, "wave_number", 0) > wave_number
            and not self._is_wave_acceptance_remediation_subtask(st.subtask_id)
            and not st.subtask_id.endswith("-decompose")
        ]
        lines = [
            f"Task ID: {task.task_id}",
            f"Overall task description (context only; do not treat as this wave's acceptance checklist): {task.description}",
            f"Wave number: {wave_number}",
            f"Project dir: {task.project_dir or 'N/A'}",
            "",
            "Wave subtasks:",
        ]
        for st in wave_subtasks:
            lines.extend([
                f"- Subtask ID: {st.subtask_id}",
                f"  Description: {st.description}",
                f"  Agent: {st.agent_id}",
                f"  Status: {st.status.value}",
                f"  Dependencies: {', '.join(st.dependencies) if st.dependencies else 'None'}",
                f"  Output file: {st.output_file or 'N/A'}",
                f"  Error: {st.error_message or 'None'}",
            ])
            if st.dependencies:
                lines.append("  Dependency details:")
                for dep_id in st.dependencies:
                    dep = subtask_by_id.get(dep_id)
                    if dep is None:
                        lines.append(f"    - {dep_id}: missing dependency record")
                        continue
                    lines.append(
                        "    - "
                        f"{dep.subtask_id}: wave={getattr(dep, 'wave_number', 'N/A')}, "
                        f"status={dep.status.value}, output_file={dep.output_file or 'N/A'}"
                    )
        lines.extend([
            "",
            "Prior approved waves:",
            ", ".join(str(num) for num in prior_wave_numbers) if prior_wave_numbers else "None",
            "",
            "Available prior-wave artifacts and outputs:",
        ])
        if available_inputs:
            for item in available_inputs:
                lines.append(f"- {item}")
        else:
            lines.append("- None")
        lines.extend([
            "",
            "Current wave artifact records:",
        ])
        if current_wave_artifacts:
            for item in current_wave_artifacts:
                lines.append(f"- {item}")
        else:
            lines.append("- None")
        lines.extend([
            "",
            "Future wave subtasks (not due in this wave; do not fail the current wave for missing these):",
        ])
        if future_wave_subtasks:
            for st in future_wave_subtasks[:20]:
                description = re.sub(r"\s+", " ", st.description).strip()
                if len(description) > 220:
                    description = description[:220] + "..."
                lines.append(f"- Wave {st.wave_number} {st.subtask_id}: {description}")
            if len(future_wave_subtasks) > 20:
                lines.append(f"- ... {len(future_wave_subtasks) - 20} more future subtasks omitted")
        else:
            lines.append("- None")
        lines.extend([
            "",
            "Current project tree snapshot:",
        ])
        if project_tree:
            for item in project_tree:
                lines.append(f"- {item}")
        else:
            lines.append("- None")
        lines.extend([
            "",
            "Review goal:",
            "Determine whether this wave as a whole is coherent enough for the next wave to consume safely.",
            "Do not require the full task to be finished in this wave.",
            "Do not fail because a feature appears only in the Future wave subtasks section.",
            "Judge whether this wave's own deliverables are coherent and whether downstream dependencies can proceed using both this wave's outputs and the prior approved artifacts listed above.",
            "Do not fail simply because the current wave depends on files that already exist from earlier approved waves.",
            "Do not fail simply because the current wave created a few coherent scaffolding files in addition to its main deliverable.",
            "Do fail if this wave introduced forbidden files, wrong-stack artifacts, duplicate conflicting project structures, or broken current-wave deliverables.",
        ])
        return "\n".join(lines)

    @staticmethod
    def _is_wave_acceptance_remediation_subtask(subtask_id: str) -> bool:
        """Return True for repair/quality subtasks that are not wave deliverables."""
        if not subtask_id:
            return False
        return (
            subtask_id.startswith("st-quality-")
            or subtask_id.startswith("wave-")
            or "-fix-" in subtask_id
            or "-integration-fix" in subtask_id
            or re.search(r"-v\d+$", subtask_id) is not None
        )

    def _get_prior_approved_waves(self, task: Task, wave_number: int) -> List[int]:
        persistence = getattr(self._state, "_persistence", None)
        approved: List[int] = []
        if persistence is not None:
            try:
                acceptance_records = persistence.get_acceptance_records(task.task_id)
                for record in acceptance_records:
                    if (
                        record.get("level") == "wave"
                        and record.get("decision") == "approve"
                        and record.get("judge_passed")
                        and isinstance(record.get("wave_number"), int)
                        and 0 < record["wave_number"] < wave_number
                        and record["wave_number"] not in approved
                    ):
                        approved.append(record["wave_number"])
            except Exception as exc:
                logger.warning(f"Failed to load wave acceptance records for {task.task_id}: {exc}")

        if approved:
            return sorted(approved)

        inferred: List[int] = []
        for candidate in range(1, wave_number):
            subtasks = [
                st for st in task.subtasks
                if getattr(st, "wave_number", None) == candidate and "-fix-" not in st.subtask_id
            ]
            if subtasks and all(st.status == JobStatus.COMPLETED for st in subtasks):
                inferred.append(candidate)
        return inferred

    def _collect_prior_wave_artifacts(
        self,
        task: Task,
        wave_number: int,
        approved_waves: List[int],
    ) -> List[str]:
        inputs: List[str] = []
        approved_set = set(approved_waves)

        for st in task.subtasks:
            st_wave = getattr(st, "wave_number", None)
            if not st.output_file or st_wave is None or st_wave >= wave_number:
                continue
            if approved_set and st_wave not in approved_set:
                continue
            item = (
                f"wave {st_wave} subtask {st.subtask_id} "
                f"({st.agent_id}) output: {st.output_file}"
            )
            if item not in inputs:
                inputs.append(item)

        persistence = getattr(self._state, "_persistence", None)
        if persistence is not None:
            try:
                artifact_records = persistence.get_artifact_records(task.task_id)
                for artifact in artifact_records:
                    artifact_wave = artifact.get("wave_number")
                    content_ref = artifact.get("content_ref")
                    if artifact.get("status") != "accepted" or not content_ref:
                        continue
                    if not self._artifact_content_ref_is_current(task, content_ref):
                        continue
                    if artifact_wave is None or artifact_wave >= wave_number:
                        continue
                    if approved_set and artifact_wave not in approved_set:
                        continue
                    name = artifact.get("name") or os.path.basename(content_ref)
                    item = (
                        f"accepted artifact from wave {artifact_wave}: "
                        f"{name} -> {content_ref}"
                    )
                    if item not in inputs:
                        inputs.append(item)
            except Exception as exc:
                logger.warning(f"Failed to load artifact records for {task.task_id}: {exc}")

        return inputs

    def _collect_wave_artifacts(self, task: Task, wave_number: int) -> List[str]:
        items: List[str] = []
        persistence = getattr(self._state, "_persistence", None)
        if persistence is None:
            return items
        try:
            artifact_records = persistence.get_artifact_records(task.task_id)
            for artifact in artifact_records:
                if artifact.get("wave_number") != wave_number:
                    continue
                content_ref = artifact.get("content_ref")
                if not content_ref:
                    continue
                if not self._artifact_content_ref_is_current(task, content_ref):
                    continue
                name = artifact.get("name") or os.path.basename(content_ref)
                item = (
                    f"{name} -> {content_ref}"
                )
                if item not in items:
                    items.append(item)
        except Exception as exc:
            logger.warning(f"Failed to load current wave artifacts for {task.task_id}: {exc}")
        return items

    @staticmethod
    def _artifact_content_ref_is_current(task: Task, content_ref: str) -> bool:
        """Return False for stale artifact records whose file was later removed."""
        project_dir = os.path.realpath(getattr(task, "project_dir", None) or "")
        if not project_dir or not os.path.isdir(project_dir):
            return True
        resolved = os.path.realpath(content_ref)
        try:
            common = os.path.commonpath([project_dir, resolved])
        except ValueError:
            return True
        if common != project_dir:
            return True
        return os.path.isfile(resolved)

    def _collect_project_tree(self, project_dir: Optional[str], limit: int = 40) -> List[str]:
        if not project_dir:
            return []
        root = Path(project_dir)
        if not root.exists():
            return []
        items: List[str] = []
        try:
            for path in sorted(root.rglob("*")):
                if len(items) >= limit:
                    items.append("... truncated ...")
                    break
                rel = str(path.relative_to(root))
                if path.is_dir():
                    items.append(f"{rel}/")
                else:
                    items.append(rel)
        except Exception as exc:
            logger.warning(f"Failed to collect project tree for {project_dir}: {exc}")
        return items

    def _repair_acceptance_response(self, response_text: str, subtask_id: str) -> AcceptanceResult:
        """Retry acceptance parsing without blaming the execution agent."""
        try:
            response = self._llm(
                system_prompt=self.ACCEPTANCE_REPAIR_SYSTEM_PROMPT,
                message=response_text,
                temperature=0.0,
            )
            repaired = self._parse_acceptance(response.text, subtask_id)
            if repaired.parse_failed:
                repaired.level2_feedback = "Acceptance review response could not be normalized after retry."
                repaired.action = "retry_acceptance"
                repaired.raw_response = response_text
            else:
                repaired.raw_response = response_text
            return repaired
        except Exception as e:
            logger.error(f"Acceptance repair failed for {subtask_id}: {e}")
            return AcceptanceResult(
                subtask_id=subtask_id,
                level1_passed=True,
                level2_passed=False,
                level2_feedback=f"Acceptance normalization failed: {e}",
                action="retry_acceptance",
                parse_failed=True,
                raw_response=response_text,
            )

    def decide_on_failure(self, job: Job, acceptance: AcceptanceResult) -> AcceptanceResult:
        """
        Decide what to do when a subtask has failed max fix rounds.

        Returns an AcceptanceResult with action set to either "downgrade" or "reassign".
        """
        logger.info(f"Deciding on failure for {job.subtask_id} after max rounds")

        # Default to downgrade unless feedback explicitly suggests reassign
        action = "downgrade"
        feedback = acceptance.level2_feedback or ""

        # Heuristic: if feedback mentions "completely broken" or "rewrite",
        # suggest reassigning to a different agent
        reassign_keywords = ["completely broken", "rewrite", "start over", "fundamentally wrong"]
        if any(kw in feedback.lower() for kw in reassign_keywords):
            action = "reassign"

        return AcceptanceResult(
            subtask_id=job.subtask_id,
            level1_passed=acceptance.level1_passed,
            level2_passed=False,
            level2_feedback=feedback,
            action=action,
        )

    def _ensure_decomposition_coverage(self, task: Task) -> None:
        """Phase 2 coverage gate: create gap-fill subtasks for unassigned required deliverables."""
        from .coverage import evaluate_decomposition_coverage
        from ..models import SubTask

        manifest = self._state.get_requirement_manifest(task.task_id)
        if not manifest:
            return
        contracts = self._state.get_task_contracts(task.task_id)
        result = evaluate_decomposition_coverage(manifest, contracts)
        if result.passed:
            self._update_manifest_assignments(task, result)
            return

        for gap in result.gaps:
            self._create_gap_fill_subtask(task, gap)

        # Re-evaluate after gap-fill
        contracts = self._state.get_task_contracts(task.task_id)
        result = evaluate_decomposition_coverage(manifest, contracts)
        self._update_manifest_assignments(task, result)
        if result.passed:
            logger.info("Coverage gate closed all gaps for task %s", task.task_id)
        else:
            logger.warning(
                "Coverage gate: %d gap(s) remain for task %s",
                len(result.gaps), task.task_id,
            )

    def _create_gap_fill_subtask(self, task: Task, gap: "CoverageGap") -> None:
        """Create a deterministic补齐 subtask for a missing required deliverable."""
        from ..models import SubTask

        def choose_agent(*preferred: str) -> str:
            allowed = [
                normalized
                for agent_id in (getattr(task, "allowed_subtask_agents", []) or [])
                if (normalized := normalize_agent_id(agent_id) or agent_id)
            ]
            if allowed:
                for candidate in preferred:
                    normalized = normalize_agent_id(candidate) or candidate
                    if normalized in allowed:
                        return normalized
                return allowed[0]
            for candidate in preferred:
                normalized = normalize_agent_id(candidate) or candidate
                if self._is_agent_available(normalized):
                    return normalized
            return preferred[0] if preferred else "deepseek"

        # Determine agent based on artifact type while respecting the selected pool.
        hint_lower = (gap.path_hint or "").lower()
        if gap.artifact_type in ("dockerfile", "compose_config"):
            agent = choose_agent("minimax", "deepseek", "claude", LOCAL_AGENT_ID)
        elif gap.artifact_type == "test_source" or hint_lower.endswith(".py"):
            agent = choose_agent("deepseek", "claude", LOCAL_AGENT_ID)
        elif gap.artifact_type == "documentation" or "readme" in hint_lower:
            agent = choose_agent("claude", "deepseek", LOCAL_AGENT_ID)
        elif gap.artifact_type in ("frontend_source", "file") or hint_lower.endswith((".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".vue")):
            agent = choose_agent("hermes", "claude", "deepseek", LOCAL_AGENT_ID)
        else:
            agent = choose_agent("deepseek", "claude", LOCAL_AGENT_ID)

        subtask_id = f"st-gap-{uuid.uuid4().hex[:8]}"
        target = gap.path_hint or gap.artifact_type
        description_parts = [f"Create required deliverable: {target}"]
        if task.project_dir:
            description_parts.append(
                f"\n\n[CRITICAL] All files MUST be written to this directory: {task.project_dir}\n"
                "Do NOT create files in any other location. Use this exact path as the base."
            )
        task_text = (getattr(task, "description", "") or "").lower()
        is_static_web_task = (
            "static web" in task_text
            or "file://" in task_text
            or "opening index.html directly" in task_text
            or ("index.html" in task_text and ("styles.css" in task_text or "app.js" in task_text))
        )
        if gap.artifact_type == "documentation" or "readme" in hint_lower:
            documentation_guidance = [
                "Document the actual delivered files and verification steps for the original request.",
                "Do not invent dependencies, servers, generated build output, or unrelated project structure.",
            ]
            if is_static_web_task:
                documentation_guidance.extend([
                    "Open index.html directly in the browser as the run path.",
                    "Do NOT include npm install, npm run, yarn, pnpm, bun, node_modules, package.json, dev server, build, or test-suite commands unless the user explicitly requested a package-managed project.",
                ])
            if "no package manager" in task_text or "no package managers" in task_text:
                documentation_guidance.append("No package managers.")
            description_parts.append("\n\n[DOCUMENTATION REQUIREMENTS]\n- " + "\n- ".join(documentation_guidance))
        global_constraint_suffix = self._build_global_subtask_constraint_suffix(task)
        if global_constraint_suffix:
            description_parts.append(global_constraint_suffix)
        description = "".join(description_parts)
        subtask = SubTask(
            subtask_id=subtask_id,
            task_id=task.task_id,
            description=description,
            agent_id=agent,
            dependencies=[],
        )
        # Assign to the latest wave
        max_wave = max((getattr(st, "wave_number", 1) for st in task.subtasks), default=1)
        subtask.wave_number = max_wave
        task.subtasks.append(subtask)
        self._state._persist_subtask(subtask)

        # Create a matching subtask contract for the gap-fill
        from ..models import AcceptanceCheck, DeliverableSpec, TaskContract
        contract = TaskContract.new(
            task_id=task.task_id,
            level="subtask",
            goal=description,
            subtask_id=subtask_id,
            wave_number=subtask.wave_number,
            project_dir=task.project_dir,
        )
        contract.expected_deliverables = [
            DeliverableSpec(
                artifact_type=gap.artifact_type or "file",
                required=True,
                path_hint=gap.path_hint,
                description=f"Required gap-fill deliverable: {gap.path_hint or gap.artifact_type}",
            )
        ]
        contract.acceptance_checks = [
            AcceptanceCheck(
                check_type="file_exists" if gap.path_hint else f"{gap.artifact_type}_exists",
                description=f"Verify gap-fill deliverable exists: {gap.path_hint or gap.artifact_type}",
                required=True,
            )
        ]
        self._state.save_task_contract(contract)

        logger.info(
            "Coverage gate: created gap-fill subtask %s (agent=%s, wave=%d) for %s",
            subtask_id, agent, subtask.wave_number, gap.path_hint or gap.artifact_type,
        )

    def _update_manifest_assignments(self, task: Task, result: "CoverageResult") -> None:
        """Update persisted manifest deliverables with coverage assignment status."""
        manifest = self._state.get_requirement_manifest(task.task_id)
        if not manifest:
            return

        raw_deliverables = list(manifest.get("deliverables", []) or [])
        for req in raw_deliverables:
            rid = req.get("requirement_id")
            if rid in result.assigned:
                assigned_subtask_id = result.assigned[rid]
                req["assigned_subtask_id"] = assigned_subtask_id
                if req.get("status") in (None, "", "unassigned", "missing"):
                    req["status"] = "assigned"
                evidence = dict(req.get("evidence") or {})
                evidence["coverage_gate"] = {
                    "assigned_subtask_id": assigned_subtask_id,
                    "reason": "matched_subtask_contract",
                }
                req["evidence"] = evidence
            elif req.get("required", True) and req.get("status") in (None, "", "assigned"):
                req["status"] = "unassigned"

        manifest["deliverables"] = raw_deliverables
        manifest["updated_at"] = time.time()
        self._state.save_requirement_manifest(manifest)

    def _sanitize_subtask_contract_specs(
        self,
        description: str,
        agent_id: str,
        deliverables: List[DeliverableSpec],
        checks: List[AcceptanceCheck],
        allowed_documentation_files: Optional[List[str]] = None,
    ) -> tuple[List[DeliverableSpec], List[AcceptanceCheck]]:
        """Remove contract requirements that contradict explicit path-only subtasks."""
        import re

        semantic_description = description.split("[CRITICAL]", 1)[0]
        text = re.sub(r"[/~][^\s,.;)]+", " ", semantic_description).lower()
        explicit_paths = self._extract_path_hints(semantic_description)
        forbidden_path_keys = {
            canonical_requirement_key(path_hint)
            for path_hint in extract_forbidden_path_hints(semantic_description)
        }
        deliverable_explicit_paths = [
            path for path in explicit_paths
            if canonical_requirement_key(path) not in forbidden_path_keys
            and not is_runtime_data_path_hint(semantic_description, path)
        ]
        basenames = {os.path.basename(p).lower() for p in deliverable_explicit_paths}
        docs_only = bool(deliverable_explicit_paths) and all(
            b.startswith("readme") or b.endswith((".md", ".rst", ".txt"))
            for b in basenames
        )
        frontend_intent = any(
            token in text
            for token in ("react", "vue", "angular", "frontend", "front-end", "typescript", "dashboard", "web ui", "user interface")
        )
        backend_detection_text = _strip_agent_capability_label_context(text)
        try:
            from .delivery_contract import _mentions_api_service
        except Exception:
            _mentions_api_service = lambda value: bool(  # type: ignore
                re.search(
                    r"\b(rest\s*api|fastapi|flask|django|backend|api\s+service|"
                    r"service\s+api|endpoint|endpoints|controller|handler|server)\b",
                    value,
                )
            )
        api_path_intent = any(
            (
                path.replace("\\", "/").lstrip("./").startswith("api/")
                and os.path.basename(path).lower()
                in {"server.mjs", "server.js", "index.mjs", "index.js", "main.py"}
            )
            for path in deliverable_explicit_paths
        )
        node_http_intent = bool(
            re.search(
                r"\b(node(?:\.js)?\s+(?:built-in\s+)?http|built-in\s+http\s+module|http\s+module)\b",
                backend_detection_text,
            )
        )
        endpoint_intent = bool(re.search(r"\b(get|post|put|patch|delete)\s+/", backend_detection_text))
        raw_backend_intent = (
            bool(_mentions_api_service(backend_detection_text))
            or api_path_intent
            or node_http_intent
            or endpoint_intent
        )
        backend_intent = raw_backend_intent and not _is_static_frontend_file_scope(
            semantic_description,
            deliverable_explicit_paths,
        )
        negative_container_intent = has_negative_container_constraint(semantic_description)
        container_intent = has_container_delivery_intent(semantic_description) or any(
            token in text
            for token in ("nginx", "ci", "deploy")
        )
        if forbidden_path_keys:
            deliverables = [
                d for d in deliverables
                if not d.path_hint or canonical_requirement_key(d.path_hint) not in forbidden_path_keys
            ]
        if allowed_documentation_files:
            allowed_doc_names = {name.lower() for name in allowed_documentation_files}
            deliverables = [
                d for d in deliverables
                if not d.path_hint
                or not d.path_hint.lower().endswith((".md", ".rst", ".txt"))
                or os.path.basename(d.path_hint).lower() in allowed_doc_names
            ]
        deliverables = [
            d for d in deliverables
            if not d.path_hint or not is_runtime_data_path_hint(semantic_description, d.path_hint)
        ]
        deliverables = [
            d for d in deliverables
            if not d.path_hint or not is_auxiliary_deliverable_path_hint(semantic_description, d.path_hint)
        ]

        if docs_only and not frontend_intent:
            deliverables = [d for d in deliverables if d.artifact_type != "frontend_source"]
            checks = [c for c in checks if c.check_type != "frontend_source_exists"]
            deliverables = [d for d in deliverables if d.artifact_type != "test_suite"]
            checks = [c for c in checks if c.check_type != "test_suite_exists"]
        if not backend_intent:
            deliverables = [d for d in deliverables if d.artifact_type != "api_service_source"]
            checks = [c for c in checks if c.check_type != "api_source_exists"]
        if negative_container_intent or (explicit_paths and not container_intent):
            deliverables = [d for d in deliverables if d.artifact_type != "dockerfile"]
            checks = [c for c in checks if c.check_type != "container_config_exists"]

        return deliverables, checks

    def _subtask_has_delivery_evidence(self, task: Task, subtask: SubTask) -> bool:
        """Check whether a completed subtask has artifact/contract evidence even if output_file is None."""
        if subtask.output_file:
            return True

        persistence = self._get_persistence()
        if persistence is not None:
            try:
                canonical_id = self._canonical_subtask_id(subtask.subtask_id)
                for artifact in persistence.get_artifact_records(task.task_id):
                    if self._artifact_satisfies_subtask(artifact, subtask.subtask_id, canonical_id):
                        return True
            except Exception as exc:
                logger.warning("Failed to check artifact evidence for %s: %s", subtask.subtask_id, exc)

            try:
                contracts = persistence.get_task_contracts(task.task_id)
                for contract in contracts:
                    if contract.get("level") != "subtask":
                        continue
                    if contract.get("subtask_id") != subtask.subtask_id:
                        continue
                    for deliverable in contract.get("expected_deliverables", []) or []:
                        path_hint = deliverable.get("path_hint")
                        if not path_hint:
                            continue
                        resolved = first_existing_candidate(path_hint, task.project_dir)
                        if resolved and os.path.isfile(os.path.realpath(resolved)):
                            return True
            except Exception as exc:
                logger.warning("Failed to check contract path evidence for %s: %s", subtask.subtask_id, exc)

            if self._subtask_result_mentions_existing_file(task, subtask.subtask_id):
                return True

        return False

    def _subtask_result_mentions_existing_file(self, task: Task, subtask_id: str) -> bool:
        """Use completed job summaries as fallback evidence for multi-file subtasks."""
        project_dir = os.path.realpath(getattr(task, "project_dir", None) or "")
        if not project_dir or not os.path.isdir(project_dir):
            return False

        persistence = self._get_persistence()
        if persistence is None or not hasattr(persistence, "get_jobs_by_subtask"):
            return False

        existing_rel: set[str] = set()
        existing_by_basename: Dict[str, List[str]] = {}
        for root, _dirs, files in os.walk(project_dir):
            for filename in files:
                full_path = os.path.realpath(os.path.join(root, filename))
                if not os.path.isfile(full_path):
                    continue
                rel_path = os.path.relpath(full_path, project_dir).replace("\\", "/")
                existing_rel.add(rel_path)
                existing_by_basename.setdefault(filename.lower(), []).append(rel_path)

        try:
            jobs = persistence.get_jobs_by_subtask(subtask_id) or []
        except Exception as exc:
            logger.warning("Failed to load jobs for delivery evidence %s: %s", subtask_id, exc)
            return False

        for job in reversed(jobs):
            if job.get("status") != JobStatus.COMPLETED.value:
                continue
            result = str(job.get("result") or "")
            for hint in self._extract_path_hints(result, project_dir=project_dir):
                normalized = hint.replace("\\", "/").strip("/")
                if not normalized:
                    continue
                if normalized in existing_rel:
                    return True
                basename = os.path.basename(normalized).lower()
                if basename == "__init__.py":
                    continue
                if existing_by_basename.get(basename):
                    return True
        return False

    def refresh_decomposition_coverage(self, task: Task) -> None:
        """Re-run coverage gate assignment after decomposition/wave persistence."""
        self._ensure_decomposition_coverage(task)

    def _is_agent_available(self, agent_id: str) -> bool:
        agent_id = normalize_agent_id(agent_id) or agent_id
        if agent_id in LOCAL_CLI_AGENT_IDS:
            from ...local_agent_health import is_local_agent_available
            return is_local_agent_available(agent_id)

        provider_config = next((p for p in load_llm_config().providers if p.provider_id == agent_id), None)
        if provider_config:
            import os
            return bool(os.environ.get(provider_config.api_key_env, "").strip())
        return False

    def run_integration_test(self, task: Task) -> IntegrationResult:
        """
        Minimal real integration acceptance.

        Deterministic checks only:
        - required task deliverables with explicit path_hint must exist
        - produced outputs must stay within project_dir when project_dir is set
        - project_dir itself must exist when declared

        The policy remains conservative: if no explicit deliverable contract exists,
        the task can still pass as long as no clear deterministic violation is found.
        """
        failed_checks: List[str] = []
        missing_artifacts: List[str] = []

        project_dir = task.project_dir
        normalized_project_dir = None
        if project_dir:
            normalized_project_dir = os.path.realpath(project_dir)
            if not os.path.isdir(normalized_project_dir):
                failed_checks.append(f"project_dir_missing:{normalized_project_dir}")

        persistence = getattr(self._state, "_persistence", None)
        task_contracts: List[Dict[str, Any]] = []
        artifact_records: List[Dict[str, Any]] = []
        if persistence is not None:
            try:
                task_contracts = persistence.get_task_contracts(task.task_id)
                artifact_records = persistence.get_artifact_records(task.task_id)
            except Exception as exc:
                logger.warning(f"Failed to load integration metadata for {task.task_id}: {exc}")

        output_refs = {st.output_file for st in task.subtasks if st.output_file}
        for artifact in artifact_records:
            content_ref = artifact.get("content_ref")
            if artifact.get("status") == "accepted" and content_ref:
                output_refs.add(content_ref)

        if normalized_project_dir:
            for ref in output_refs:
                if not ref:
                    continue
                abs_ref = os.path.realpath(ref)
                if not abs_ref.startswith(normalized_project_dir + os.sep) and abs_ref != normalized_project_dir:
                    failed_checks.append(f"output_outside_project_dir:{abs_ref}")

        for contract in task_contracts:
            if contract.get("level") != "task":
                continue
            for deliverable in contract.get("expected_deliverables", []):
                if not deliverable.get("required", True):
                    continue
                path_hint = deliverable.get("path_hint")
                if not path_hint:
                    continue
                resolved_candidate = first_existing_candidate(path_hint, normalized_project_dir)
                resolved = os.path.realpath(resolved_candidate) if resolved_candidate else None
                if not resolved or not os.path.exists(resolved):
                    missing_artifacts.append(
                        os.path.realpath(os.path.join(normalized_project_dir, path_hint))
                        if normalized_project_dir and not os.path.isabs(path_hint)
                        else os.path.realpath(path_hint)
                    )
                else:
                    output_refs.add(resolved)

        for contract in task_contracts:
            if contract.get("level") != "task":
                continue
            for deliverable in contract.get("expected_deliverables", []):
                if not deliverable.get("required", True):
                    continue
                artifact_type = deliverable.get("artifact_type")
                if not artifact_type or deliverable.get("path_hint"):
                    continue
                if not self._deliverable_type_satisfied(artifact_type, output_refs, normalized_project_dir):
                    missing_artifacts.append(
                        f"{artifact_type}: {deliverable.get('description') or 'required deliverable missing'}"
                    )

        passed = not failed_checks and not missing_artifacts
        if passed:
            if missing_artifacts or failed_checks:
                message = "Integration acceptance passed with no blocking issues"
            elif task_contracts:
                message = "Deterministic integration checks passed"
            else:
                message = "No explicit integration deliverables declared; no deterministic violation found"
        else:
            message = "Integration acceptance failed"

        details = {
            "failed_checks": failed_checks,
            "missing_artifacts": missing_artifacts,
            "output_refs": sorted(output_refs),
        }
        logger.info(
            f"Integration acceptance for {task.task_id}: passed={passed}, "
            f"failed_checks={failed_checks}, missing_artifacts={missing_artifacts}"
        )
        return IntegrationResult(
            passed=passed,
            message=message,
            details=details,
        )

    def _deliverable_type_satisfied(
        self,
        artifact_type: str,
        output_refs: set[str],
        normalized_project_dir: Optional[str],
    ) -> bool:
        refs = set()
        for ref in output_refs:
            if not ref:
                continue
            refs.add(os.path.abspath(ref))
            if normalized_project_dir and not os.path.isabs(ref):
                refs.add(os.path.abspath(os.path.join(normalized_project_dir, ref)))

        def has_ref(predicate: Callable[[str], bool]) -> bool:
            if any(predicate(ref) for ref in refs):
                return True
            if not normalized_project_dir or not os.path.isdir(normalized_project_dir):
                return False
            for root, dirs, files in os.walk(normalized_project_dir):
                candidates = [os.path.join(root, name) for name in dirs + files]
                if any(predicate(path) for path in candidates):
                    return True
            return False

        if artifact_type == "macos_app_bundle":
            return has_ref(lambda path: path.endswith(".app") and os.path.exists(path))
        if artifact_type == "api_service_source":
            return has_ref(
                lambda path: path.endswith((
                    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs", ".rb", ".php", ".java", ".kt",
                ))
                and os.path.exists(path)
            )
        if artifact_type == "dockerfile":
            return has_ref(
                lambda path: os.path.basename(path).lower() in {"dockerfile", "containerfile"}
                and os.path.exists(path)
            )
        if artifact_type == "frontend_source":
            return has_ref(lambda path: path.endswith((".tsx", ".ts", ".jsx", ".js", ".vue")) and os.path.exists(path))
        return False
