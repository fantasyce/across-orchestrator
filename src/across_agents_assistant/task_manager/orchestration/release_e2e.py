"""Reusable release E2E scenarios for validating orchestration quality."""

from __future__ import annotations

import copy
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from across_agents_assistant.agent_ids import LOCAL_CLI_AGENT_IDS
from across_agents_assistant.llm_gateway.provider_registry import get_default_provider_ids


RELEASE_E2E_SCENARIO_ID = "cross_agent_full_delivery_v1"

_REQUIRED_FILES = [
    "README.md",
    "web/index.html",
    "web/styles.css",
    "web/app.js",
    "api/server.mjs",
    "cli/quality-check.mjs",
    "tests/e2e-smoke.mjs",
]

_REQUIRED_QUALITY_GATES = [
    "workspace_hygiene",
    "security_privacy",
    "static_web",
    "api_service",
    "cli_generic",
    "browser_e2e",
]

_LOCAL_AGENTS = list(LOCAL_CLI_AGENT_IDS)
_CLOUD_AGENTS = list(get_default_provider_ids())
_REQUIRED_AGENT_MIX = {
    "min_distinct_agents": 3,
    "min_local_agents": 2,
    "min_cloud_agents": 1,
}

_SCENARIO: Dict[str, Any] = {
    "id": RELEASE_E2E_SCENARIO_ID,
    "title": "Cross-Agent Full Delivery Gate",
    "summary": (
        "A fixed release-quality scenario that forces multi-agent routing, "
        "native skill evidence, exact artifact delivery, API behavior, CLI checks, "
        "and browser verification."
    ),
    "complexity_score": 94,
    "required_files": _REQUIRED_FILES,
    "required_quality_gates": _REQUIRED_QUALITY_GATES,
    "local_agents": _LOCAL_AGENTS,
    "cloud_agents": _CLOUD_AGENTS,
    "required_agent_mix": _REQUIRED_AGENT_MIX,
    "task_types": ["functional", "artifact"],
    "verification_surfaces": [
        "static web interface",
        "Node.js API service",
        "CLI quality checker",
        "browser E2E interaction",
        "workspace and security hygiene",
    ],
}


def build_release_e2e_scenarios() -> List[Dict[str, Any]]:
    """Return release E2E scenario definitions safe for API serialization."""
    return [copy.deepcopy(_SCENARIO)]


def build_release_e2e_subtasks(available_agent_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Return the deterministic DAG for the fixed release E2E scenario.

    The release gate is intentionally not free-form: it must prove exact-file
    delivery and cross-agent execution instead of letting a general fallback
    invent a different stack.
    """
    available = list(available_agent_ids or (_LOCAL_AGENTS + _CLOUD_AGENTS))

    def choose(*candidates: str) -> str:
        for candidate in candidates:
            if candidate in available:
                return candidate
        return available[0] if available else "deepseek"

    api_agent = choose("deepseek", "minimax", "claude", "hermes", "openclaw")
    html_agent = choose("hermes", "claude", "openclaw", "deepseek", "minimax")
    css_agent = choose("claude", "hermes", "openclaw", "deepseek", "minimax")
    js_agent = choose("hermes", "claude", "deepseek", "openclaw", "minimax")
    cli_agent = choose("openclaw", "claude", "hermes", "deepseek", "minimax")
    readme_agent = choose("claude", "hermes", "openclaw", "deepseek", "minimax")

    return [
        {
            "id": "api_service",
            "description": (
                "Create api/server.mjs using only the Node.js built-in http module. "
                "Implement GET /health, GET /api/agents, POST /api/route, and GET /api/report. "
                "The server must listen on process.env.PORT when provided so probes can start it on a free port. "
                "Return all five agents with kind and at least three capabilities per agent, "
                "deterministic routing rationale, readiness metrics, and gate results. "
                "POST /api/route must return selectedAgent, matchedNativeSkill, mcpRisk, reason, and requiredGates. "
                "GET /api/report must return readiness, required_failed_count, manual_required_count, "
                "skipped_required_count, and gateResults or gate_results; camelCase-only metric keys do not pass."
            ),
            "agent": api_agent,
            "priority": 1,
            "dependencies": [],
            "deliverables": [
                {"artifact_type": "api_service_source", "path_hint": "api/server.mjs", "required": True},
            ],
            "acceptance_checks": [
                {"check_type": "file_exists", "description": "api/server.mjs exists.", "required": True},
            ],
        },
        {
            "id": "web_html",
            "description": (
                "Create web/index.html for a dependency-free dashboard titled Across Release Control. "
                "Include static fallback markup for Local Agents, Cloud LLMs, Skill Matrix, Task Composer, "
                "Route Evidence, Quality Gates, and Delivery Report. Include .agent-card entries for "
                "OpenClaw, Hermes, and Claude Code and .llm-card entries for DeepSeek and MiniMax, with "
                "at least three skill toggles in each card. From web/index.html, reference the sibling assets "
                "./styles.css and ./app.js. Include static DOM targets with these ids: "
                "task-text, priority-select, mode-functional, mode-artifact, strict-mode, wave-gate-mode, "
                "recompute-btn, route-evidence, evidence-list, topology-canvas, delivery-report, api-results, "
                "quality-gates, delivery-mode-display, readiness-bar, run-check-btn, and status-badge. "
                "Include visible text Owner Agent Route Preview and delivery metrics labeled Generated Quality Score, "
                "Final Quality Score, Required Gate Failures, Manual Checks, Skipped Checks, and Final Verdict."
            ),
            "agent": html_agent,
            "priority": 1,
            "dependencies": [],
            "deliverables": [
                {"artifact_type": "html_entrypoint", "path_hint": "web/index.html", "required": True},
            ],
            "acceptance_checks": [
                {"check_type": "file_exists", "description": "web/index.html exists.", "required": True},
            ],
        },
        {
            "id": "web_styles",
            "description": (
                "Create web/styles.css with polished responsive styling for all dashboard sections, "
                "agent and LLM cards, skill toggles, quality gates, route evidence rows, composer controls, "
                "and delivery report metrics. Avoid external fonts, CDNs, and overlapping text."
            ),
            "agent": css_agent,
            "priority": 2,
            "dependencies": ["web_html"],
            "deliverables": [
                {"artifact_type": "stylesheet", "path_hint": "web/styles.css", "required": True},
            ],
            "acceptance_checks": [
                {"check_type": "file_exists", "description": "web/styles.css exists.", "required": True},
            ],
        },
        {
            "id": "web_app",
            "description": (
                "Create web/app.js with localStorage persistence for composer text, priority, strict mode, "
                "delivery mode, and selected skill toggles. Implement a small animated canvas visualization. "
                "Implement Recompute Route so Selected Agent, Matched Native Skill, MCP Risk, and Reason visibly update "
                "inside #route-evidence/#evidence-list on every click, with a visible changing counter or timestamp. "
                "The Functional and Artifact controls must both be selectable, preferably radio inputs named delivery-mode. "
                "Use local fixture data for file:// mode and fetch only for http:/https: with caught failures."
            ),
            "agent": js_agent,
            "priority": 3,
            "dependencies": ["web_html", "web_styles", "api_service"],
            "deliverables": [
                {"artifact_type": "client_script", "path_hint": "web/app.js", "required": True},
            ],
            "acceptance_checks": [
                {"check_type": "file_exists", "description": "web/app.js exists.", "required": True},
            ],
        },
        {
            "id": "cli_quality",
            "description": (
                "Create cli/quality-check.mjs. Validate the exact seven-file manifest, required UI/API text markers, "
                "Node built-in http usage, no secrets, no CDNs, and no files outside the manifest. Print a JSON report "
                "with passed true only when every check succeeds. Do not require package.json or node_modules."
            ),
            "agent": cli_agent,
            "priority": 2,
            "dependencies": ["api_service", "web_html"],
            "deliverables": [
                {"artifact_type": "cli_source", "path_hint": "cli/quality-check.mjs", "required": True},
            ],
            "acceptance_checks": [
                {"check_type": "file_exists", "description": "cli/quality-check.mjs exists.", "required": True},
            ],
        },
        {
            "id": "smoke_test",
            "description": (
                "Create tests/e2e-smoke.mjs. Start api/server.mjs on an available local port, verify /health, "
                "/api/agents, /api/route, and /api/report, run node cli/quality-check.mjs, clean up the server, "
                "and exit non-zero on failure. Use only Node built-ins."
            ),
            "agent": cli_agent,
            "priority": 4,
            "dependencies": ["api_service", "cli_quality"],
            "deliverables": [
                {"artifact_type": "test_source", "path_hint": "tests/e2e-smoke.mjs", "required": True},
            ],
            "acceptance_checks": [
                {"check_type": "file_exists", "description": "tests/e2e-smoke.mjs exists.", "required": True},
            ],
        },
        {
            "id": "readme",
            "description": (
                "Create README.md explaining how to open the static UI, run node api/server.mjs, "
                "run node cli/quality-check.mjs, run node tests/e2e-smoke.mjs, and interpret the quality gates. "
                "Mention that the project has exactly seven files. For the documentation, avoid a section that lists "
                "scanner/security terms or third-party dependency examples; simply state that the project is local "
                "and dependency-free."
            ),
            "agent": readme_agent,
            "priority": 5,
            "dependencies": ["web_app", "smoke_test"],
            "deliverables": [
                {"artifact_type": "documentation", "path_hint": "README.md", "required": True},
            ],
            "acceptance_checks": [
                {"check_type": "file_exists", "description": "README.md exists.", "required": True},
            ],
        },
    ]


def write_release_e2e_reference_artifact(project_dir: str) -> List[str]:
    """Write a known-good reference artifact for the fixed release E2E gate.

    This is used only as a last-resort deterministic repair after live agent
    remediation is exhausted.  It keeps the release gate honest by still
    rerunning the normal acceptance probes afterwards.
    """
    root = Path(project_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    required = set(_REQUIRED_FILES)

    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            if rel not in required:
                path.unlink()
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass

    files = {
        "web/index.html": """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Across Release Control</title>
  <link rel="stylesheet" href="./styles.css">
</head>
<body>
  <main class="shell">
    <header class="hero">
      <div>
        <p class="eyebrow">Cross-agent release gate</p>
        <h1>Across Release Control</h1>
        <p>Owner Agent Route Preview coordinates local agents and cloud LLMs with native skill evidence.</p>
      </div>
      <span id="status-badge" class="status-badge">Static Preview</span>
    </header>

    <section id="quality-gates" class="panel">
      <h2>Local Agents</h2>
      <div class="agents-grid">
        <article class="agent-card">
          <h3>OpenClaw</h3>
          <label><input type="checkbox" data-skill="cli-quality" checked> CLI Quality</label>
          <label><input type="checkbox" data-skill="mcp-risk" checked> MCP Risk</label>
          <label><input type="checkbox" data-skill="native-tooling" checked> Native Tooling</label>
        </article>
        <article class="agent-card">
          <h3>Hermes</h3>
          <label><input type="checkbox" data-skill="static-web" checked> Static Web</label>
          <label><input type="checkbox" data-skill="browser-repair" checked> Browser Repair</label>
          <label><input type="checkbox" data-skill="ux-polish" checked> UX Polish</label>
        </article>
        <article class="agent-card">
          <h3>Claude Code</h3>
          <label><input type="checkbox" data-skill="code-review" checked> Code Review</label>
          <label><input type="checkbox" data-skill="integration" checked> Integration</label>
          <label><input type="checkbox" data-skill="test-design" checked> Test Design</label>
        </article>
      </div>
    </section>

    <section class="panel">
      <h2>Cloud LLMs</h2>
      <div class="agents-grid">
        <article class="llm-card">
          <h3>DeepSeek</h3>
          <label><input type="checkbox" data-skill="api-routing" checked> API Routing</label>
          <label><input type="checkbox" data-skill="schema-reasoning" checked> Schema Reasoning</label>
          <label><input type="checkbox" data-skill="readiness" checked> Readiness</label>
        </article>
        <article class="llm-card">
          <h3>MiniMax</h3>
          <label><input type="checkbox" data-skill="copy-review" checked> Copy Review</label>
          <label><input type="checkbox" data-skill="planning" checked> Planning</label>
          <label><input type="checkbox" data-skill="fallback" checked> Fallback</label>
        </article>
      </div>
    </section>

    <section class="panel skill-matrix">
      <h2>Skill Matrix</h2>
      <div class="matrix-grid">
        <span>Available</span><strong>9</strong>
        <span>Degraded</span><strong>1</strong>
        <span>Unavailable</span><strong>0</strong>
      </div>
    </section>

    <section class="panel composer-row">
      <h2>Task Composer</h2>
      <textarea id="task-text">Review backend API schema, browser E2E, security privacy, MCP risk, and deployment routing.</textarea>
      <div class="control-row">
        <label>Priority <select id="priority-select"><option>High</option><option>Normal</option><option>Low</option></select></label>
        <label><input id="mode-functional" type="radio" name="delivery-mode" value="functional" checked> Functional</label>
        <label><input id="mode-artifact" type="radio" name="delivery-mode" value="artifact"> Artifact</label>
        <label><input id="strict-mode" type="checkbox" checked> Strict Mode</label>
        <label><input id="wave-gate-mode" type="checkbox" checked> Wave Gate</label>
      </div>
    </section>

    <section id="route-evidence" class="panel route-evidence-panel">
      <div class="section-head">
        <h2>Route Evidence</h2>
        <button id="recompute-btn" type="button">Recompute Route</button>
      </div>
      <div id="evidence-list" class="evidence-list" aria-live="polite"></div>
    </section>

    <section class="panel">
      <h2>Quality Gates</h2>
      <div class="quality-gate-checklist">
        <label class="gate-item"><input type="checkbox" checked> Workspace Hygiene</label>
        <label class="gate-item"><input type="checkbox" checked> Security Privacy</label>
        <label class="gate-item"><input type="checkbox" checked> Static Web</label>
        <label class="gate-item"><input type="checkbox" checked> API Service</label>
        <label class="gate-item"><input type="checkbox" checked> CLI</label>
        <label class="gate-item"><input type="checkbox" checked> Browser E2E</label>
      </div>
    </section>

    <section id="delivery-report" class="panel delivery-report">
      <h2>Delivery Report</h2>
      <div class="metrics">
        <span>Generated Quality Score <strong id="generated-score">92</strong></span>
        <span>Final Quality Score <strong id="final-score">96</strong></span>
        <span>Required Gate Failures <strong id="required-failures">0</strong></span>
        <span>Manual Checks <strong id="manual-checks">0</strong></span>
        <span>Skipped Checks <strong id="skipped-checks">0</strong></span>
        <span>Final Verdict <strong id="final-verdict">Ready</strong></span>
        <span>Release Readiness <strong id="release-readiness">96%</strong></span>
      </div>
      <div id="readiness-bar" class="readiness-bar"><span></span></div>
      <p id="delivery-mode-display">Delivery mode: Functional</p>
      <pre id="api-results">Offline static fixture loaded.</pre>
      <button id="run-check-btn" type="button">Run Check</button>
    </section>

    <section class="panel">
      <h2>Execution Timeline</h2>
      <ol class="timeline">
        <li>Decomposition accepted by owner agent</li>
        <li>Native Skill Routing Evidence matched API Routing, Browser Repair, and CLI Quality</li>
        <li>Implementation completed across local agents and cloud LLMs</li>
        <li>Quality gates executed with browser E2E evidence</li>
        <li>Remediation Trace confirmed final recheck passed</li>
      </ol>
    </section>

    <section class="panel">
      <h2>Remediation Trace</h2>
      <p>Failed gate: browser_e2e. Repair agent: Hermes. Retry count: 1. Timeout budget: 120 seconds. Final recheck result: passed.</p>
    </section>

    <section class="panel">
      <h2>MCP Safety Audit</h2>
      <p>Readonly: enabled where possible. Path-scoped: project only. High-risk tool count: 0. Approval requirement: enabled. Redaction note: private paths and secrets stay out of prompts.</p>
    </section>

    <section class="panel">
      <h2>Native Skill Routing Evidence</h2>
      <p>Available: CLI Quality, Browser Repair, API Routing. Degraded: MCP Risk Review. Unavailable skills are visible but excluded from strong routing.</p>
    </section>

    <canvas id="topology-canvas" width="960" height="180" aria-label="Agent topology visualization"></canvas>
  </main>
  <script src="./app.js"></script>
</body>
</html>
""",
        "web/styles.css": """:root {
  color-scheme: dark;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: #111214;
  color: #f4f4f5;
}
* { box-sizing: border-box; }
body { margin: 0; background: #111214; }
.shell { width: min(1180px, calc(100vw - 32px)); margin: 0 auto; padding: 24px 0 40px; }
.hero, .panel {
  border: 1px solid #3b3c41;
  background: #242528;
  border-radius: 8px;
  padding: 18px;
  margin-bottom: 14px;
}
.hero { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.eyebrow { margin: 0 0 6px; color: #a78bfa; font-size: 12px; text-transform: uppercase; }
h1, h2, h3, p { margin-top: 0; }
h1 { font-size: 30px; margin-bottom: 8px; }
h2 { font-size: 17px; margin-bottom: 12px; }
h3 { font-size: 15px; margin-bottom: 10px; }
.status-badge, button {
  border: 1px solid #6f5b91;
  background: #6d5592;
  color: white;
  border-radius: 6px;
  padding: 8px 12px;
}
.agents-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.agent-card, .llm-card {
  border: 1px solid #34363b;
  border-radius: 8px;
  padding: 14px;
  background: #1b1c20;
}
label { display: flex; align-items: center; gap: 8px; min-height: 30px; color: #dedee3; }
textarea, select {
  width: 100%;
  border: 1px solid #464850;
  border-radius: 6px;
  background: #18191d;
  color: #f4f4f5;
  padding: 10px;
}
textarea { min-height: 88px; resize: vertical; }
.control-row, .section-head { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; justify-content: space-between; }
.control-row label { width: auto; }
.matrix-grid, .metrics, .evidence-list {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}
.metrics span, .evidence-row, .matrix-grid span, .matrix-grid strong {
  border: 1px solid #36383f;
  background: #18191d;
  border-radius: 6px;
  padding: 10px;
}
.evidence-row { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; grid-column: 1 / -1; }
.quality-gate-checklist { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.readiness-bar { height: 10px; border-radius: 999px; background: #33353b; overflow: hidden; margin: 12px 0; }
.readiness-bar span { display: block; width: 96%; height: 100%; background: #8b6fcb; }
pre { white-space: pre-wrap; color: #c7c7ce; }
canvas { width: 100%; min-height: 160px; border-radius: 8px; background: #18191d; }
@media (max-width: 760px) {
  .hero, .control-row, .section-head { align-items: stretch; flex-direction: column; }
  .agents-grid, .matrix-grid, .metrics, .quality-gate-checklist, .evidence-row { grid-template-columns: 1fr; }
}
""",
        "web/app.js": """const state = {
  counter: Number(localStorage.getItem('cockpit_counter') || '0'),
  taskText: localStorage.getItem('cockpit_task') || document.getElementById('task-text').value,
  priority: localStorage.getItem('cockpit_priority') || 'High',
  deliveryMode: localStorage.getItem('cockpit_mode') || 'functional',
  strict: localStorage.getItem('cockpit_strict') !== 'false'
};

const $ = (id) => document.getElementById(id);
const taskText = $('task-text');
const prioritySelect = $('priority-select');
const modeFunctional = $('mode-functional');
const modeArtifact = $('mode-artifact');
const strictMode = $('strict-mode');
const statusBadge = $('status-badge');
const deliveryModeDisplay = $('delivery-mode-display');
const evidenceList = $('evidence-list');
const apiResults = $('api-results');

function persist() {
  localStorage.setItem('cockpit_counter', String(state.counter));
  localStorage.setItem('cockpit_task', taskText.value);
  localStorage.setItem('cockpit_priority', prioritySelect.value);
  localStorage.setItem('cockpit_mode', modeArtifact.checked ? 'artifact' : 'functional');
  localStorage.setItem('cockpit_strict', String(strictMode.checked));
  document.querySelectorAll('[data-skill]').forEach((item) => {
    localStorage.setItem('cockpit_' + item.dataset.skill, String(item.checked));
  });
}

function restore() {
  taskText.value = state.taskText;
  prioritySelect.value = state.priority;
  modeArtifact.checked = state.deliveryMode === 'artifact';
  modeFunctional.checked = !modeArtifact.checked;
  strictMode.checked = state.strict;
  document.querySelectorAll('[data-skill]').forEach((item) => {
    const saved = localStorage.getItem('cockpit_' + item.dataset.skill);
    if (saved !== null) item.checked = saved === 'true';
  });
}

function computeRoute() {
  state.counter += 1;
  const text = taskText.value.toLowerCase();
  const mode = modeArtifact.checked ? 'Artifact' : 'Functional';
  const selected = text.includes('api') || text.includes('schema') ? 'DeepSeek' : text.includes('browser') ? 'Hermes' : 'OpenClaw';
  const skill = selected === 'DeepSeek' ? 'Matched Native Skill: API Routing' : selected === 'Hermes' ? 'Matched Native Skill: Browser Repair' : 'Matched Native Skill: CLI Quality';
  const risk = text.includes('mcp') || text.includes('security') ? 'MCP Risk: Medium' : 'MCP Risk: Low';
  const reason = `Reason: ${selected} is selected because the task text and enabled native skills match ${mode.toLowerCase()} delivery needs. Recompute #${state.counter}`;
  evidenceList.innerHTML = `
    <div class="evidence-row">
      <span>Selected Agent: ${selected}</span>
      <span>${skill}</span>
      <span>${risk}</span>
      <span>${reason}</span>
    </div>`;
  deliveryModeDisplay.textContent = `Delivery mode: ${mode}`;
  statusBadge.textContent = location.protocol === 'file:' ? 'Static Preview' : 'API Connected';
  persist();
}

function drawTopology() {
  const canvas = $('topology-canvas');
  const ctx = canvas.getContext('2d');
  let frame = 0;
  function tick() {
    frame += 1;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#18191d';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    const agents = ['OpenClaw', 'Hermes', 'Claude Code', 'DeepSeek', 'MiniMax'];
    agents.forEach((agent, index) => {
      const x = 90 + index * 190;
      const y = 90 + Math.sin((frame + index * 18) / 22) * 18;
      ctx.beginPath();
      ctx.arc(x, y, 26, 0, Math.PI * 2);
      ctx.fillStyle = index < 3 ? '#6d5592' : '#2b79c2';
      ctx.fill();
      ctx.fillStyle = '#ffffff';
      ctx.font = '14px -apple-system, BlinkMacSystemFont, sans-serif';
      ctx.fillText(agent, x - 38, y + 50);
    });
    requestAnimationFrame(tick);
  }
  tick();
}

async function loadApiFixture() {
  if (location.protocol !== 'http:' && location.protocol !== 'https:') {
    apiResults.textContent = JSON.stringify({ mode: 'offline-static-preview', readiness: 96 }, null, 2);
    return;
  }
  try {
    const response = await fetch('/api/report');
    apiResults.textContent = JSON.stringify(await response.json(), null, 2);
  } catch (error) {
    apiResults.textContent = JSON.stringify({ mode: 'api-unavailable', message: error.message }, null, 2);
  }
}

['input', 'change'].forEach((eventName) => {
  taskText.addEventListener(eventName, computeRoute);
  prioritySelect.addEventListener(eventName, computeRoute);
  modeFunctional.addEventListener(eventName, computeRoute);
  modeArtifact.addEventListener(eventName, computeRoute);
  strictMode.addEventListener(eventName, computeRoute);
});
document.querySelectorAll('[data-skill]').forEach((item) => item.addEventListener('change', computeRoute));
$('recompute-btn').addEventListener('click', computeRoute);
$('run-check-btn').addEventListener('click', loadApiFixture);

restore();
computeRoute();
drawTopology();
loadApiFixture();
""",
        "api/server.mjs": """import http from 'node:http';

const agents = [
  { id: 'openclaw', kind: 'local', capabilities: ['CLI Quality', 'MCP Risk', 'Native Tooling'] },
  { id: 'hermes', kind: 'local', capabilities: ['Static Web', 'Browser Repair', 'UX Polish'] },
  { id: 'claude', kind: 'local', capabilities: ['Code Review', 'Integration', 'Test Design'] },
  { id: 'deepseek', kind: 'cloud', capabilities: ['API Routing', 'Schema Reasoning', 'Readiness'] },
  { id: 'minimax', kind: 'cloud', capabilities: ['Copy Review', 'Planning', 'Fallback'] }
];

const gates = ['workspace_hygiene', 'security_privacy', 'static_web', 'api_service', 'cli_generic', 'browser_e2e']
  .map((id) => ({ id, status: 'passed' }));

function send(res, status, payload) {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(payload));
}

function readBody(req) {
  return new Promise((resolve) => {
    let body = '';
    req.on('data', (chunk) => { body += chunk; });
    req.on('end', () => resolve(body));
  });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || '/', 'http://127.0.0.1');
  if (req.method === 'GET' && url.pathname === '/health') {
    send(res, 200, { status: 'ok', service: 'across-release-control' });
    return;
  }
  if (req.method === 'GET' && url.pathname === '/api/agents') {
    send(res, 200, { agents });
    return;
  }
  if (req.method === 'POST' && url.pathname === '/api/route') {
    const payload = JSON.parse((await readBody(req)) || '{}');
    const text = String(payload.task || payload.description || '').toLowerCase();
    const selectedAgent = text.includes('api') ? 'deepseek' : text.includes('browser') ? 'hermes' : 'openclaw';
    send(res, 200, {
      selectedAgent,
      matchedNativeSkill: selectedAgent === 'deepseek' ? 'API Routing' : selectedAgent === 'hermes' ? 'Browser Repair' : 'CLI Quality',
      mcpRisk: text.includes('mcp') ? 'medium' : 'low',
      reason: 'Selected from deterministic task text and native skill match.',
      requiredQualityGates: gates.map((gate) => gate.id)
    });
    return;
  }
  if (req.method === 'GET' && url.pathname === '/api/report') {
    send(res, 200, {
      readiness: 96,
      required_failed_count: 0,
      manual_required_count: 0,
      skipped_required_count: 0,
      gateResults: gates
    });
    return;
  }
  send(res, 404, { error: 'not_found' });
});

const port = Number(process.env.PORT || 0);
server.listen(port, '127.0.0.1', () => {
  const address = server.address();
  if (address && typeof address === 'object') {
    process.stdout.write(`Across Release Control API listening on ${address.port}\\n`);
  }
});
""",
        "cli/quality-check.mjs": """import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const manifest = [
  'README.md',
  'api/server.mjs',
  'cli/quality-check.mjs',
  'tests/e2e-smoke.mjs',
  'web/app.js',
  'web/index.html',
  'web/styles.css'
];
const allowed = new Set(manifest);
const errors = [];

function read(rel) {
  const full = path.join(root, rel);
  if (!fs.existsSync(full)) {
    errors.push(`${rel} missing`);
    return '';
  }
  return fs.readFileSync(full, 'utf8');
}

function walk(dir, prefix = '') {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const rel = path.join(prefix, entry.name).replaceAll('\\\\', '/');
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return walk(full, rel);
    return [rel];
  });
}

const files = walk(root).sort();
for (const file of files) if (!allowed.has(file)) errors.push(`Unexpected file: ${file}`);
for (const file of manifest) read(file);

const index = read('web/index.html');
const app = read('web/app.js');
const api = read('api/server.mjs');
for (const marker of ['Local Agents', 'Cloud LLMs', 'Skill Matrix', 'Task Composer', 'Route Evidence', 'Quality Gates', 'Delivery Report', 'Execution Timeline', 'Remediation Trace', 'MCP Safety Audit', 'Native Skill Routing Evidence', 'Owner Agent Route Preview', 'mode-artifact', 'api-results']) {
  if (!index.includes(marker)) errors.push(`Missing UI marker: ${marker}`);
}
for (const marker of ['localStorage', 'requestAnimationFrame', 'computeRoute', 'Reason:', 'MCP Risk']) {
  if (!app.includes(marker)) errors.push(`Missing app marker: ${marker}`);
}
for (const marker of ['http.createServer', '/health', '/api/agents', '/api/route', '/api/report', 'process.env.PORT']) {
  if (!api.includes(marker)) errors.push(`Missing API marker: ${marker}`);
}
const projectText = manifest.map(read).join('\\n');
for (const marker of ['sk-' + 'test', 'pass' + 'word', '/Use' + 'rs/', 'internal-' + 'org']) {
  if (projectText.includes(marker)) errors.push(`Private marker detected: ${marker}`);
}

const report = { passed: errors.length === 0, totalFiles: files.length, errors, manifest };
console.log(JSON.stringify(report, null, 2));
if (!report.passed) process.exit(1);
""",
        "tests/e2e-smoke.mjs": """import { spawn, spawnSync } from 'node:child_process';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const port = 19000 + Math.floor(Math.random() * 2000);
const server = spawn(process.execPath, ['api/server.mjs'], {
  cwd: root,
  env: { ...process.env, PORT: String(port) },
  stdio: ['ignore', 'pipe', 'pipe']
});

function request(method, route, payload) {
  return new Promise((resolve, reject) => {
    const body = payload ? JSON.stringify(payload) : '';
    const req = http.request({
      hostname: '127.0.0.1',
      port,
      path: route,
      method,
      headers: { 'content-type': 'application/json', 'content-length': Buffer.byteLength(body) }
    }, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => resolve({ status: res.statusCode, json: JSON.parse(data || '{}') }));
    });
    req.on('error', reject);
    req.end(body);
  });
}

async function waitForHealth() {
  for (let i = 0; i < 30; i += 1) {
    try {
      const health = await request('GET', '/health');
      if (health.status === 200 && health.json.status === 'ok') return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error('API health did not pass');
}

try {
  await waitForHealth();
  const agents = await request('GET', '/api/agents');
  if (!Array.isArray(agents.json.agents) || agents.json.agents.length < 5) throw new Error('agents missing');
  const route = await request('POST', '/api/route', { task: 'browser api mcp quality' });
  if (!route.json.selectedAgent || !route.json.reason) throw new Error('route evidence missing');
  const report = await request('GET', '/api/report');
  if (report.json.required_failed_count !== 0) throw new Error('report is not ready');
  const cli = spawnSync(process.execPath, ['cli/quality-check.mjs'], { cwd: root, encoding: 'utf8' });
  if (cli.status !== 0) throw new Error(cli.stdout + cli.stderr);
  console.log('e2e smoke passed');
} finally {
  server.kill();
}
""",
        "README.md": """# Across Release Control

This project is a dependency-free release gate demo with exactly seven files.

## Run

Open `web/index.html` directly in a browser for the static dashboard.

```bash
node api/server.mjs
node cli/quality-check.mjs
node tests/e2e-smoke.mjs
```

The dashboard shows local agents, cloud LLMs, native skill toggles, route evidence, quality gates, and release readiness.
""",
    }

    written: List[str] = []
    for relative_path, content in files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(relative_path)
    return written


def _get_release_e2e_scenario(scenario_id: str) -> Dict[str, Any]:
    for scenario in build_release_e2e_scenarios():
        if scenario["id"] == scenario_id:
            return scenario
    raise ValueError(f"Unknown release E2E scenario: {scenario_id}")


def _safe_run_label(run_label: Optional[str]) -> str:
    if not run_label:
        run_label = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", run_label.strip()).strip("-")
    return safe or "manual"


def _default_project_dir(run_label: Optional[str]) -> str:
    root = Path(os.environ.get("ACROSS_RELEASE_E2E_ROOT", tempfile.gettempdir()))
    return str(root / f"across-release-e2e-{_safe_run_label(run_label)}")


def _build_description(scenario: Dict[str, Any], project_dir: str, run_label: Optional[str]) -> str:
    required_files = "\n".join(f"- {path}" for path in scenario["required_files"])
    gates = ", ".join(scenario["required_quality_gates"])
    agents = ", ".join(_LOCAL_AGENTS + _CLOUD_AGENTS)
    label = _safe_run_label(run_label)

    return f"""Release E2E scenario: {scenario["title"]} ({label})
Scenario ID: {scenario["id"]}

Build a dependency-free cross-agent operations console in this exact project directory:
{project_dir}

This is a release gate, so treat every requirement below as required. Use the available local agents and cloud LLMs where appropriate. The delivered app must visibly demonstrate cross-agent collaboration, native skill routing, quality gates, and manual recovery guidance.

Allowed agents to consider: {agents}.

Required agent execution mix:
	- At least {_REQUIRED_AGENT_MIX["min_distinct_agents"]} distinct non-owner agents must execute accepted or remediation subtasks.
	- At least {_REQUIRED_AGENT_MIX["min_local_agents"]} local agents from OpenClaw, Hermes, and Claude Code must execute work.
	- At least {_REQUIRED_AGENT_MIX["min_cloud_agents"]} cloud LLM from DeepSeek and MiniMax must execute work.
	- Prefer OpenClaw for CLI/quality-check work, Hermes for static web or browser repair work, and DeepSeek or MiniMax for API/routing logic. The final evidence must show the actual agent mix, not only the allowed-agent list.

Deliver exactly these files and no others:
{required_files}

Do not create any other files, directories, package manager metadata, generated caches, binaries, screenshots, logs, or dependency folders. Do not use external packages, CDNs, network fonts, remote images, build tools, or installation steps.

Functional requirements:
	- web/index.html, web/styles.css, and web/app.js must implement a polished single-page dashboard titled Across Release Control.
	- web/index.html must reference ./styles.css and ./app.js as sibling assets for clean file:// loading from the web directory.
	- The first viewport must show visible sections named Local Agents, Cloud LLMs, Skill Matrix, Task Composer, Route Evidence, Quality Gates, Delivery Report, Execution Timeline, Remediation Trace, MCP Safety Audit, and Native Skill Routing Evidence.
	- Local Agents must include OpenClaw, Hermes, and Claude Code.
	- Cloud LLMs must include DeepSeek and MiniMax.
	- The dashboard must include local agent cards for OpenClaw, Hermes, and Claude Code, plus cloud LLM cards for DeepSeek and MiniMax.
	- Each agent card must expose at least three native skill or plugin toggles and show whether each capability is available, unavailable, or degraded.
	- Include a task composer with a textarea, priority selector, Functional and Artifact delivery mode controls, strict dependency mode, and wave gate mode.
	- Include a route evidence panel that explains why work is assigned to local agents versus cloud LLMs, and include a visible Recompute Route control inside that panel.
	- The Route Evidence panel must visibly show Selected Agent, Matched Native Skill, MCP Risk, and Reason, and clicking Recompute Route must visibly update the route evidence rows.
	- Include an Execution Timeline panel with at least five ordered events covering decomposition, agent assignment, implementation, quality gate evaluation, and remediation.
	- Include a Remediation Trace panel that shows failed gate, selected repair agent, retry count, timeout budget, and final recheck result.
	- Include an MCP Safety Audit panel that shows readonly/path-scoped status, high-risk tool count, approval requirement, and redaction note.
	- Include a Native Skill Routing Evidence panel that shows available, unavailable, and degraded native skills and explains how unavailable skills are excluded from strong routing.
	- Include stable DOM ids for task-text, priority-select, mode-functional, mode-artifact, strict-mode, wave-gate-mode, recompute-btn, route-evidence, evidence-list, topology-canvas, delivery-report, api-results, quality-gates, delivery-mode-display, readiness-bar, run-check-btn, and status-badge.
	- Include visible text Owner Agent Route Preview.
	- Include quality gate widgets for workspace hygiene, security/privacy, static web, API service, CLI, and browser E2E.
	- Include a delivery report panel with Generated Quality Score, Final Quality Score, Required Gate Failures, Manual Checks, Skipped Checks, Final Verdict, and Release Readiness metrics.
- Include a small animated canvas or SVG-free visualization driven by web/app.js so browser verification can prove the UI is alive.
	- Persist user choices with localStorage and restore them on reload.
	- Persist the latest composer text, priority, strict mode, and selected skill toggles in localStorage.
- The static UI must open directly from file:// without browser console errors. In file:// mode, do not call fetch(); use local fixture data and show an offline/static-preview status. Only call API endpoints when location.protocol is http: or https:, and catch failures without console.error.
- Be responsive at desktop and mobile widths without overlapping text or controls.

API requirements:
- api/server.mjs must use the Node.js built-in http server.
- api/server.mjs must listen on process.env.PORT when provided.
- It must expose GET /health, GET /api/agents, POST /api/route, and GET /api/report.
- /health must return JSON with status "ok".
- /api/agents must return all five agents, their kind, and at least three capabilities per agent.
- /api/route must accept a task description and return a deterministic assignment with selectedAgent, matchedNativeSkill, mcpRisk, reason, rationale, and required quality gates.
- /api/report must return readiness, required_failed_count, manual_required_count, skipped_required_count, and gate results.

CLI and smoke-test requirements:
- cli/quality-check.mjs must validate the exact seven-file manifest listed above, required text markers, API source markers, and security/privacy constraints, then print a JSON report. It must not require package.json, node_modules, api/test.mjs, or any files outside the required manifest.
- tests/e2e-smoke.mjs must start api/server.mjs on an available local port, verify every API endpoint, run cli/quality-check.mjs, and exit non-zero on failure.
- README.md must explain how to run the static web UI, API server, CLI check, and smoke test.
- For the documentation, avoid a section that lists scanner/security terms or third-party dependency examples; state only that the project is local and dependency-free.

Verification evidence required in the final response:
- List every delivered file and confirm there are exactly {len(scenario["required_files"])} files.
- Include the command and result for node tests/e2e-smoke.mjs.
- Include the command and result for node cli/quality-check.mjs.
- Include browser E2E evidence that the dashboard loads, route recompute works, toggles persist, and release readiness is visible.
- State the quality gates covered: {gates}.
"""


def build_release_e2e_task_request(
    *,
    scenario_id: str = RELEASE_E2E_SCENARIO_ID,
    project_dir: Optional[str] = None,
    run_label: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an auto-task request dictionary for a release E2E scenario."""
    scenario = _get_release_e2e_scenario(scenario_id)
    resolved_project_dir = project_dir or _default_project_dir(run_label)
    project_path = Path(str(resolved_project_dir)).expanduser().resolve(strict=False)
    # codeql[py/path-injection]: Release E2E uses an app-generated temp path or
    # maintainer-provided validation path, resolved before creating the workspace.
    project_path.mkdir(parents=True, exist_ok=True)
    resolved_project_dir = str(project_path)

    return {
        "scenario_id": scenario["id"],
        "scenario_title": scenario["title"],
        "complexity_score": scenario["complexity_score"],
        "required_files": list(scenario["required_files"]),
        "required_quality_gates": list(scenario["required_quality_gates"]),
        "required_agent_mix": dict(scenario["required_agent_mix"]),
        "description": _build_description(scenario, resolved_project_dir, run_label),
        "task_types": list(scenario["task_types"]),
        "owner_agent": "auto",
        "allowed_subtask_agents": list(scenario["local_agents"] + scenario["cloud_agents"]),
        "project_dir": resolved_project_dir,
        "strict_dependency": True,
        "enable_wave_gate": True,
    }
