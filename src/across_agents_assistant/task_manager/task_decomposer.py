import json
import logging
import re
import uuid
from typing import Dict, Any, Optional

from ..llm_gateway.gateway import LLMGateway
from ..agent_ids import LOCAL_AGENT_ID, LOCAL_CLI_AGENT_IDS, normalize_agent_id
from ..llm_gateway.provider_registry import get_default_provider_ids
from .models import Task, TaskType, SubTask, Wave, JobStatus

logger = logging.getLogger("across_agents_assistant.task_manager")

SYSTEM_PROMPT = """You are a task planning assistant for a macOS assistant app called "Across Agents Assistant".

Your role is to break down user requests into clear, actionable sub-tasks assigned to specialized agents.

**Available Agents:**
- openclaw: General purpose development and automation tasks (CLI-based, file I/O, shell commands)
- hermes: Specific scenario development and conversational tasks (frontend, UI, React, Vue)
- claude: Code/technical deep expertise and code reviews (architecture, design, review, audit)
- codex: Local Codex CLI coding agent for implementation, debugging, and repository-aware changes
- opencode: Local OpenCode CLI coding agent for scripted repository-aware implementation
- cursor: Local Cursor Agent CLI for editor-native implementation and code review tasks
- Cloud providers from the configured provider registry, such as deepseek, minimax, openai, anthropic, bailian, moonshot, zhipu, volcengine, google, xai, mistral, groq, cohere, openrouter, together, and fireworks.

**Task Types:**
- research: Information gathering, web search, knowledge lookup
- code_review: Code analysis, quality assessment, refactoring suggestions
- automation: repetitive tasks, scripting, workflow automation
- simple_qa: Questions the app can answer directly without agent dispatch
- unknown: Cannot determine type

**Output Format:**
You MUST output a JSON object with this exact structure:
{
    "task_type": "research|code_review|automation|simple_qa|unknown",
    "can_handle_directly": true|false,
    "direct_response": "..." (only if can_handle_directly is true),
    "subtasks": [
        {"description": "...", "agent": "openclaw|hermes|claude|codex|opencode|cursor|configured-cloud-provider-id", "priority": 1, "dependencies": []}
    ]
}

**Rules:**
1. If the task is a simple question or can be answered from context, set can_handle_directly=true
2. Complex tasks should be broken into subtasks assigned to appropriate agents
3. Dependencies indicate which subtask must complete before this one starts (use subtask descriptions to match)
4. Priority 1 = highest, run first
5. Keep descriptions concise but actionable
6. If the user asks to build/create/develop a macOS app or any application that needs to be packaged, ALWAYS include a final subtask for packaging/building the app (e.g., "Build and package the app into a distributable .app bundle")
7. If the user asks to build/create/develop a macOS app, ensure the final deliverable includes a working .app bundle, not just source code
"""

class TaskDecomposer:
    """Uses LLM to decompose user requests into subtasks."""

    VALID_AGENTS = [*LOCAL_CLI_AGENT_IDS, *get_default_provider_ids()]
    TASK_TYPES = ["research", "code_review", "automation", "simple_qa", "unknown"]

    def __init__(self, gateway: LLMGateway):
        self._gateway = gateway
        self._default_agents = self.VALID_AGENTS

    async def decompose(self, task: Task, context: Optional[Dict[str, Any]] = None) -> Task:
        """
        Use LLM to decompose a task into subtasks.

        Args:
            task: The task to decompose
            context: Optional context dict (e.g., frontmost_app, window_title)

        Returns:
            The same task object with subtasks populated
        """
        user_message = task.description

        try:
            response = await self._gateway.chat(
                message=user_message,
                system_prompt=SYSTEM_PROMPT,
                context=context,
                temperature=0.3,
                max_tokens=2048
            )

            logger.info(f"LLM decomposition response: {response.text[:200]}...")

            decomposition = self._parse_llm_response(response.text)
            if decomposition:
                self._apply_decomposition(task, decomposition)
                self.assign_waves(task)
                logger.info(f"Task {task.task_id} decomposed into {len(task.subtasks)} subtasks")
            else:
                logger.warning(f"Failed to parse LLM response for task {task.task_id}")
                task.task_type = TaskType.UNKNOWN

        except Exception as e:
            logger.error(f"Task decomposition failed for {task.task_id}: {e}")
            task.task_type = TaskType.UNKNOWN

        return task

    def _parse_llm_response(self, text: str) -> Optional[Dict[str, Any]]:
        """Extract and parse JSON from LLM response text."""
        text = text.strip()

        # Handle thinking model responses (DeepSeek R1, etc.): strip <think>...</think> tags
        text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()

        # Try direct JSON parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to find JSON in markdown code blocks
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # Try to find JSON object pattern
        obj_match = re.search(r"\{[\s\S]*\}", text)
        if obj_match:
            try:
                return json.loads(obj_match.group(0))
            except json.JSONDecodeError:
                pass

        return None

    def _apply_decomposition(self, task: Task, decomposition: Dict[str, Any]) -> None:
        """Apply parsed decomposition to task."""
        task_type_str = decomposition.get("task_type", "unknown")
        if task_type_str in self.TASK_TYPES:
            task.task_type = TaskType(task_type_str)
        else:
            task.task_type = TaskType.UNKNOWN

        task.can_handle_directly = decomposition.get("can_handle_directly", False)
        task.direct_response = decomposition.get("direct_response")

        raw_subtasks = []
        for st_data in decomposition.get("subtasks", []):
            description = st_data.get("description", "")
            if not description:
                continue
            agent = self._validate_agent(st_data.get("agent"), description)
            priority = int(st_data.get("priority", 1))
            raw_deps = st_data.get("dependencies", [])
            raw_subtasks.append({
                "description": description,
                "agent": agent,
                "priority": priority,
                "raw_deps": raw_deps,
            })

        desc_to_id: Dict[str, str] = {}
        for raw in raw_subtasks:
            subtask_id = f"st-{uuid.uuid4().hex[:8]}"
            desc_to_id[raw["description"]] = subtask_id
            subtask = SubTask(
                subtask_id=subtask_id,
                description=raw["description"],
                agent_id=raw["agent"],
                priority=raw["priority"],
                dependencies=[],
            )
            task.subtasks.append(subtask)

        for raw in raw_subtasks:
            resolved_deps = []
            for dep in raw["raw_deps"]:
                if dep in desc_to_id:
                    resolved_deps.append(desc_to_id[dep])
                else:
                    logger.info(f"Resolving dep '{dep}' from candidates: {list(desc_to_id.keys())}")
                    best_match = self._fuzzy_match_dep(dep, desc_to_id)
                    if best_match:
                        resolved_deps.append(best_match)
                    else:
                        logger.warning(f"Cannot resolve dependency '{dep}' to any subtask ID, skipping")
            subtask_id = desc_to_id[raw["description"]]
            for st in task.subtasks:
                if st.subtask_id == subtask_id:
                    st.dependencies = resolved_deps
                    break

    KEYWORD_AGENT_MAP = {
        "claude": ["architecture", "openapi", "schema", "design", "review", "audit"],
        "deepseek": ["backend", "fastapi", "pydantic", "api", "python", "flask", "django"],
        "hermes": ["frontend", "react", "ui", "typescript", "component", "vue", "angular"],
        "minimax": ["devops", "docker", "deploy", "nginx", "ci", "container", "dockerfile"],
    }

    def _validate_agent(self, agent: Optional[str], description: str = "") -> str:
        """Validate and normalize agent ID, with keyword-based routing override."""
        agent = normalize_agent_id(agent) if agent else agent
        text = description.lower()

        keyword_agent = None
        matched_keywords = []
        for agent_id, keywords in self.KEYWORD_AGENT_MAP.items():
            if agent_id not in self.VALID_AGENTS:
                continue
            for kw in keywords:
                if kw in text:
                    keyword_agent = agent_id
                    matched_keywords = [kw]
                    break
            if keyword_agent:
                break

        if keyword_agent and keyword_agent in self.VALID_AGENTS:
            if agent and agent != keyword_agent:
                logger.info(f"_validate_agent: overriding LLM suggested '{agent}' -> '{keyword_agent}' "
                            f"based on keyword match '{matched_keywords}' in description")
            return keyword_agent

        if agent and agent in self.VALID_AGENTS:
            return agent

        logger.warning("Invalid agent '%s', defaulting to '%s'", agent, LOCAL_AGENT_ID)
        return LOCAL_AGENT_ID

    def _fuzzy_match_dep(self, dep: str, desc_to_id: Dict[str, str]) -> Optional[str]:
        """Try to fuzzy-match a dependency description to a subtask description."""
        dep_lower = dep.lower().strip()
        best_id = None
        best_score = 0
        for desc, sid in desc_to_id.items():
            desc_lower = desc.lower().strip()
            if dep_lower == desc_lower:
                return sid
            if dep_lower in desc_lower or desc_lower in dep_lower:
                score = min(len(dep_lower), len(desc_lower)) / max(len(dep_lower), len(desc_lower))
                if score > best_score:
                    best_score = score
                    best_id = sid
        if best_id and best_score >= 0.3:
            logger.info(f"Fuzzy matched dep '{dep}' -> '{best_id}' (score={best_score:.2f})")
            return best_id

        dep_prefix = dep_lower[:min(20, len(dep_lower))]
        for desc, sid in desc_to_id.items():
            desc_lower = desc.lower().strip()
            desc_prefix = desc_lower[:min(20, len(desc_lower))]
            if dep_prefix == desc_prefix:
                logger.info(f"Prefix matched dep '{dep}' -> '{sid}'")
                return sid

        dep_words = set(dep_lower.split())
        for desc, sid in desc_to_id.items():
            desc_words = set(desc.lower().split())
            overlap = len(dep_words & desc_words)
            total = len(dep_words | desc_words)
            if total > 0:
                jaccard = overlap / total
                if jaccard > best_score and jaccard >= 0.5:
                    best_score = jaccard
                    best_id = sid
        if best_id:
            logger.info(f"Jaccard matched dep '{dep}' -> '{best_id}' (score={best_score:.2f})")
            return best_id

        return None

    def assign_waves(self, task: Task) -> Task:
        """Assign wave_number to each SubTask based on DAG topological sort.

        wave_number = max(wave_number of all dependencies) + 1
        SubTasks with no dependencies have wave_number = 1

        Preserves wave 0 (decompose subtask) if it exists.
        """
        if not task.subtasks:
            return task

        subtask_map = {st.subtask_id: st for st in task.subtasks}
        computed: Dict[str, int] = {}

        def compute_wave(subtask_id: str) -> int:
            if subtask_id in computed:
                return computed[subtask_id]

            st = subtask_map.get(subtask_id)
            if not st:
                computed[subtask_id] = 1
                return 1

            # Skip decompose subtask (wave 0)
            if st.subtask_id.endswith("-decompose"):
                computed[subtask_id] = 0
                return 0

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

        for st in task.subtasks:
            compute_wave(st.subtask_id)

        # Preserve wave 0 (decompose subtask) if it exists
        existing_wave0 = None
        for w in task.waves:
            if w.wave_number == 0:
                existing_wave0 = w
                break

        wave_groups: Dict[int, List[SubTask]] = {}
        for st in task.subtasks:
            wn = computed.get(st.subtask_id, 1)
            st.wave_number = wn
            if wn not in wave_groups:
                wave_groups[wn] = []
            wave_groups[wn].append(st)

        # Build waves list, preserving wave 0 if it exists
        task.waves = []
        if existing_wave0:
            task.waves.append(existing_wave0)

        for wn in sorted(wave_groups.keys()):
            if wn == 0:
                continue  # Already handled
            from .models import Wave
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

        logger.info(f"Task {task.task_id} waves assigned: {len(task.waves)} waves")
        return task
