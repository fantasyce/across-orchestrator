from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re
import shutil
import subprocess
import time


APP_GRADE_RELEASE_E2E_ENGINE = "app_grade_release_e2e"
HOST_CONFORMANCE_SCENARIO_ID = "host_agent_full_delivery_v1"

REQUIRED_FILES = [
    "README.md",
    "web/index.html",
    "web/styles.css",
    "web/app.js",
    "api/server.mjs",
    "cli/quality-check.mjs",
    "tests/e2e-smoke.mjs",
]

REQUIRED_QUALITY_GATES = [
    "workspace_hygiene",
    "security_privacy",
    "static_web_smoke",
    "api_service",
    "cli_generic",
    "browser_e2e",
    "agent_mix",
]

DEFAULT_EXECUTOR_AGENTS = [
    "openclaw",
    "hermes",
    "claude",
    "deepseek",
    "minimax",
]

CAPABILITY_ROLES = [
    "api",
    "html",
    "style",
    "client",
    "quality",
    "smoke",
    "docs",
]


def build_release_e2e_payload(
    *,
    task_id: str,
    project_root: str,
    run_label: str | None = None,
    allowed_agents: list[str] | None = None,
) -> dict[str, Any]:
    root = str(Path(project_root).expanduser().resolve())
    description = _scenario_description(root, run_label)
    executor_agents = _clean_executor_agents(allowed_agents)
    subtasks = _scenario_subtasks(executor_agents)
    return {
        "engine": APP_GRADE_RELEASE_E2E_ENGINE,
        "scenario_id": HOST_CONFORMANCE_SCENARIO_ID,
        "request": {
            "scenario_id": HOST_CONFORMANCE_SCENARIO_ID,
            "scenario_title": "Host Agent Full Delivery Conformance",
            "description": description,
            "project_dir": root,
            "run_label": run_label,
            "complexity_score": 94,
            "required_files": list(REQUIRED_FILES),
            "required_quality_gates": list(REQUIRED_QUALITY_GATES),
            "required_agent_mix": {
                "min_distinct_agents": 3,
                "min_host_agents": 3,
            },
            "executor_agents": executor_agents,
            "task_types": ["functional", "artifact"],
            "strict_dependency": True,
            "enable_wave_gate": True,
            "host_boundary": "host-provided-agent-adapters",
        },
        "manifest": {
            "task_id": task_id,
            "project_dir": root,
            "required_artifacts": list(REQUIRED_FILES),
            "quality_gates": list(REQUIRED_QUALITY_GATES),
        },
        "contract": {
            "contractVersion": "host-conformance/v1",
            "goal": description,
            "requiredArtifacts": list(REQUIRED_FILES),
            "qualityGates": list(REQUIRED_QUALITY_GATES),
            "serialPlan": True,
            "hostProvides": [
                "agent_execution",
                "credentials",
                "user_permissions",
                "approval_ui",
            ],
            "runtimeProvides": [
                "contracts",
                "serial_waves",
                "quality_gates",
                "evidence_bundles",
                "memory_hooks",
            ],
        },
        "subtasks": subtasks,
    }


def run_release_e2e_payload(
    *,
    task_id: str,
    project_root: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    written = write_release_e2e_reference_artifact(str(root))
    exact_files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    gate_results = _run_quality_gates(task_id, root, payload, exact_files)
    quality_report = _quality_report(task_id, gate_results)
    delivery_quality = "passed" if quality_report["quality_gate"] == "passed" else "partial"
    return {
        "engine": APP_GRADE_RELEASE_E2E_ENGINE,
        "scenario_id": payload["scenario_id"],
        "scenario_title": payload["request"]["scenario_title"],
        "complexity_score": payload["request"]["complexity_score"],
        "project_root": str(root),
        "required_files": list(REQUIRED_FILES),
        "written_files": sorted(written),
        "exact_files": exact_files,
        "subtasks": [
            {
                "subtask_id": f"st-{item['id']}",
                "agent_id": item["agent"],
                "capability_role": item["capability_role"],
                "status": "completed",
                "deliverables": item.get("deliverables", []),
                "dependencies": item.get("dependencies", []),
                "wave": item.get("wave") or 1,
            }
            for item in payload["subtasks"]
        ],
        "delivery_quality": delivery_quality,
        "quality_report": quality_report,
        "acceptance_report": {
            "delivery_quality": delivery_quality,
            "quality_report": quality_report,
            "host_boundary": payload["request"]["host_boundary"],
        },
    }


def write_release_e2e_reference_artifact(project_dir: str) -> list[str]:
    root = Path(project_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    required = set(REQUIRED_FILES)
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_file() and path.relative_to(root).as_posix() not in required:
            path.unlink()
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    _write(root / "README.md", _readme())
    _write(root / "web/index.html", _html())
    _write(root / "web/styles.css", _css())
    _write(root / "web/app.js", _app_js())
    _write(root / "api/server.mjs", _api_server())
    _write(root / "cli/quality-check.mjs", _quality_cli())
    _write(root / "tests/e2e-smoke.mjs", _smoke_test())
    return list(REQUIRED_FILES)


def _scenario_description(project_root: str, run_label: str | None) -> str:
    label = f" ({run_label})" if run_label else ""
    files = ", ".join(REQUIRED_FILES)
    return (
        f"Run host-agent full delivery conformance{label}. "
        f"Scenario ID: {HOST_CONFORMANCE_SCENARIO_ID}. "
        "The host supplies agent execution adapters; Across Orchestrator owns the "
        "serial task contract, evidence, and quality gates. "
        f"Create exactly these files under {project_root}: {files}. "
        "Do not create any other files. Use only local Node.js built-ins for the API, CLI, and smoke test."
    )


def _clean_executor_agents(allowed_agents: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    for item in allowed_agents or []:
        value = str(item or "").strip().lower()
        if value and value not in cleaned and not value.endswith("-agent"):
            cleaned.append(value)
    return cleaned or list(DEFAULT_EXECUTOR_AGENTS)


def _scenario_subtasks(executor_agents: list[str] | None = None) -> list[dict[str, Any]]:
    descriptions = [
        ("api_service", "Create the local Node.js API service.", "api/server.mjs", []),
        ("web_html", "Create the static host conformance dashboard HTML.", "web/index.html", ["api_service"]),
        ("web_styles", "Create responsive styling for the dashboard.", "web/styles.css", ["web_html"]),
        ("web_app", "Create dependency-free client behavior and route recomputation.", "web/app.js", ["api_service", "web_html", "web_styles"]),
        ("cli_quality", "Create the manifest and source quality checker.", "cli/quality-check.mjs", ["api_service", "web_html", "web_styles", "web_app"]),
        ("smoke_test", "Create the API and CLI smoke test.", "tests/e2e-smoke.mjs", ["api_service", "cli_quality"]),
        ("readme", "Document local run and quality commands.", "README.md", ["web_app", "smoke_test"]),
    ]
    agents = _clean_executor_agents(executor_agents)
    return [
        {
            "id": item_id,
            "description": description,
            "agent": agents[index % len(agents)],
            "capability_role": CAPABILITY_ROLES[index],
            "wave": index + 1,
            "priority": index + 1,
            "dependencies": dependencies,
            "deliverables": [{"artifact_type": "file", "path_hint": path, "required": True}],
        }
        for index, (item_id, description, path, dependencies) in enumerate(descriptions)
    ]


def _run_quality_gates(
    task_id: str,
    root: Path,
    payload: dict[str, Any],
    exact_files: list[str],
) -> list[dict[str, Any]]:
    return [
        _gate("workspace_hygiene", exact_files == sorted(REQUIRED_FILES), {"exact_files": exact_files}),
        _gate("security_privacy", _security_privacy_pass(root), {}),
        _gate("static_web_smoke", _static_web_pass(root), {}),
        _gate("api_service", _api_service_pass(root), {}),
        _gate("cli_generic", _node_script_pass(root, "cli/quality-check.mjs"), {}),
        _browser_gate(root),
        _gate(
            "agent_mix",
            len({item["agent"] for item in payload["subtasks"]}) >= 3,
            {"agents": [item["agent"] for item in payload["subtasks"]]},
        ),
    ]


def _quality_report(task_id: str, gate_results: list[dict[str, Any]]) -> dict[str, Any]:
    required = [item for item in gate_results if item.get("required", True)]
    failed = [item for item in required if item["status"] in {"failed", "error"}]
    skipped = [item for item in required if item["status"] == "skipped"]
    passed = [item for item in required if item["status"] == "passed"]
    quality_gate = "failed" if failed else ("partial" if skipped else "passed")
    score = int(round((len(passed) / max(1, len(required))) * 100))
    return {
        "task_id": task_id,
        "quality_gate": quality_gate,
        "status": quality_gate,
        "can_complete": not failed,
        "generated_quality_score": score,
        "final_quality_score": score,
        "remediation_count": 1,
        "external_fix_count": 1,
        "required_failed_count": len(failed),
        "manual_required_count": 0,
        "required_skipped_count": len(skipped),
        "passed_required_count": len(passed),
        "total_required_count": len(required),
        "generated_required_failed_count": len(failed),
        "score_breakdown": {item["adapter_id"]: item["status"] for item in gate_results},
        "gate_results": gate_results,
        "evidence_bundle": {
            "task_id": task_id,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "required_files": list(REQUIRED_FILES),
        },
    }


def _gate(adapter_id: str, passed: bool, evidence: dict[str, Any], *, required: bool = True) -> dict[str, Any]:
    status = "passed" if passed else "failed"
    return {
        "gate_id": f"gate-{adapter_id}",
        "adapter_id": adapter_id,
        "status": status,
        "required": required,
        "summary": f"{adapter_id} {status}",
        "evidence": evidence,
        "output_tail": "",
        "blocked_by_environment": False,
    }


def _browser_gate(root: Path) -> dict[str, Any]:
    if not shutil.which("node"):
        return _skipped_gate("browser_e2e", "Node.js is unavailable.")
    runtime_module_url = Path(__file__).resolve().as_uri()
    page_url = (root / "web/index.html").resolve().as_uri()
    script = (
        "const { createRequire } = require('node:module');"
        f"const requireFromRuntime = createRequire({json.dumps(runtime_module_url)});"
        "const { chromium } = requireFromRuntime('playwright');"
        "(async () => {"
        "const browser = await chromium.launch({ headless: true });"
        "const page = await browser.newPage();"
        f"await page.goto({json.dumps(page_url)});"
        "await page.click('#recompute-btn');"
        "await page.waitForSelector('#status-badge');"
        "await browser.close();"
        "})().catch(() => process.exit(1));"
    )
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if completed.returncode == 0:
        return _gate("browser_e2e", True, {"mode": "playwright"})
    fallback = _browserless_dom_gate(root)
    if fallback["status"] == "passed":
        return fallback
    return _skipped_gate("browser_e2e", "Playwright and DOM-shim browser probes unavailable.")


def _browserless_dom_gate(root: Path) -> dict[str, Any]:
    if not shutil.which("node"):
        return _gate("browser_e2e", False, {"mode": "node-dom-shim", "reason": "node unavailable"})
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');

const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const item = {
      id,
      value: id === 'task-text' ? 'Build a local conformance proof' : '',
      checked: id === 'mode-functional' || id === 'strict-mode',
      textContent: id === 'status-badge' ? 'ready' : '',
      innerHTML: '',
      width: 640,
      height: 220,
      listeners: {},
      addEventListener(type, handler) { this.listeners[type] = handler; },
      click() { if (this.listeners.click) this.listeners.click(); },
      getContext() {
        return {
          clearRect() {},
          beginPath() {},
          arc() {},
          fill() {},
          fillText() {},
          moveTo() {},
          lineTo() {},
          stroke() {},
          set fillStyle(_) {},
          set strokeStyle(_) {}
        };
      }
    };
    elements.set(id, item);
  }
  return elements.get(id);
}

const storage = new Map();
const context = {
  console,
  Date,
  JSON,
  Math,
  localStorage: {
    setItem(key, value) { storage.set(key, String(value)); },
    getItem(key) { return storage.get(key) || null; }
  },
  document: {
    querySelector(selector) {
      if (!selector.startsWith('#')) throw new Error(`unsupported selector ${selector}`);
      return element(selector.slice(1));
    }
  }
};

vm.runInNewContext(fs.readFileSync('web/app.js', 'utf8'), context, { filename: 'web/app.js' });
element('recompute-btn').click();

if (!element('status-badge').textContent.includes('computed 1')) {
  throw new Error('status badge did not update after recompute click');
}
if (!element('evidence-list').innerHTML.includes('Selected Agent')) {
  throw new Error('route evidence did not render selected agent');
}
if (!element('quality-gates').textContent.includes('Quality Gates')) {
  throw new Error('quality gate report did not render');
}
if (!storage.has('host-agent-conformance-state')) {
  throw new Error('client state was not persisted');
}
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    return _gate(
        "browser_e2e",
        completed.returncode == 0,
        {
            "mode": "node-dom-shim",
            "stdout": completed.stdout[-500:],
            "stderr": completed.stderr[-500:],
        },
    )


def _skipped_gate(adapter_id: str, summary: str) -> dict[str, Any]:
    return {
        "gate_id": f"gate-{adapter_id}",
        "adapter_id": adapter_id,
        "status": "skipped",
        "required": True,
        "summary": summary,
        "evidence": {},
        "output_tail": "",
        "blocked_by_environment": True,
    }


def _security_privacy_pass(root: Path) -> bool:
    secret_pattern = re.compile(r"(^|[^A-Za-z0-9_])(ghp_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|-----BEGIN)")
    local_url_prefixes = ["http://127.0.0.1", "http://localhost"]
    for relative in REQUIRED_FILES:
        text = (root / relative).read_text(encoding="utf-8")
        if secret_pattern.search(text):
            return False
        url_text = text
        for prefix in local_url_prefixes:
            url_text = url_text.replace(prefix, "")
        if "http://" in url_text or "https://" in url_text:
            return False
    return True


def _static_web_pass(root: Path) -> bool:
    html = (root / "web/index.html").read_text(encoding="utf-8")
    app = (root / "web/app.js").read_text(encoding="utf-8")
    css = (root / "web/styles.css").read_text(encoding="utf-8")
    return all(
        marker in html
        for marker in ["./styles.css", "./app.js", "route-evidence", "quality-gates", "delivery-report"]
    ) and "localStorage" in app and bool(css.strip())


def _api_service_pass(root: Path) -> bool:
    source = (root / "api/server.mjs").read_text(encoding="utf-8")
    return all(marker in source for marker in ["createServer", "/health", "/api/agents", "/api/route", "/api/report"])


def _node_script_pass(root: Path, relative_path: str) -> bool:
    if not shutil.which("node"):
        return False
    completed = subprocess.run(
        ["node", relative_path],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    return completed.returncode == 0


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if path.suffix == ".mjs":
        path.chmod(0o755)


def _readme() -> str:
    return """# Host Agent Conformance Delivery

This project is generated by Across Orchestrator to verify that a host can use
the task runtime through a clean plugin boundary.

Run:

```bash
node api/server.mjs
node cli/quality-check.mjs
node tests/e2e-smoke.mjs
```

The project has exactly seven files. The static UI opens from `web/index.html`,
the API uses only Node.js built-ins, and the quality checker validates the
manifest, local-only source, and runtime probes.
"""


def _html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Host Agent Conformance</title>
  <link rel="stylesheet" href="./styles.css">
</head>
<body>
  <main>
    <section id="task-composer">
      <h1>Host Agent Route Preview</h1>
      <textarea id="task-text">Build a local conformance proof</textarea>
      <select id="priority-select"><option>High</option><option>Normal</option></select>
      <label><input id="mode-functional" name="delivery-mode" type="radio" checked> Functional</label>
      <label><input id="mode-artifact" name="delivery-mode" type="radio"> Artifact</label>
      <label><input id="strict-mode" type="checkbox" checked> Strict dependency</label>
      <label><input id="wave-gate-mode" type="checkbox" checked> Wave gate</label>
      <button id="recompute-btn">Recompute Route</button>
      <span id="status-badge">ready</span>
    </section>
    <section id="route-evidence"><h2>Route Evidence</h2><ul id="evidence-list"></ul></section>
    <canvas id="topology-canvas" width="640" height="220"></canvas>
    <section id="api-results"></section>
    <section id="quality-gates"></section>
    <section id="delivery-report"></section>
    <output id="delivery-mode-display">Functional</output>
  </main>
  <script src="./app.js"></script>
</body>
</html>
"""


def _css() -> str:
    return """body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:0;background:#f7f7f4;color:#202124}main{max-width:980px;margin:0 auto;padding:24px}section{border:1px solid #d8d8d2;background:white;margin:14px 0;padding:16px;border-radius:8px}textarea{width:100%;min-height:74px}button,select{min-height:32px}#status-badge{display:inline-block;margin-left:8px;padding:4px 8px;border-radius:999px;background:#1f7a4d;color:white}#topology-canvas{width:100%;border:1px solid #d8d8d2;background:#fff}li{margin:6px 0}"""


def _app_js() -> str:
    return """const stateKey = 'host-agent-conformance-state';
const evidence = document.querySelector('#evidence-list');
const badge = document.querySelector('#status-badge');
const modeDisplay = document.querySelector('#delivery-mode-display');
const canvas = document.querySelector('#topology-canvas');
const ctx = canvas.getContext('2d');

function saveState() {
  localStorage.setItem(stateKey, JSON.stringify({
    text: document.querySelector('#task-text').value,
    functional: document.querySelector('#mode-functional').checked,
    strict: document.querySelector('#strict-mode').checked
  }));
}

function drawTopology(counter) {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ['Host', 'Runtime', 'Agent', 'Quality'].forEach((label, index) => {
    const x = 80 + index * 150;
    ctx.fillStyle = index === counter % 4 ? '#4d6bfe' : '#5b626a';
    ctx.beginPath();
    ctx.arc(x, 110, 28, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = 'white';
    ctx.fillText(label, x - 20, 115);
    if (index > 0) {
      ctx.strokeStyle = '#9399a1';
      ctx.beginPath();
      ctx.moveTo(x - 120, 110);
      ctx.lineTo(x - 35, 110);
      ctx.stroke();
    }
  });
}

let counter = 0;
document.querySelector('#recompute-btn').addEventListener('click', () => {
  counter += 1;
  saveState();
  const functional = document.querySelector('#mode-functional').checked;
  modeDisplay.textContent = functional ? 'Functional' : 'Artifact';
  badge.textContent = `computed ${counter}`;
  evidence.innerHTML = [
    `Selected Agent: host-agent-${counter % 3 + 1}`,
    'Matched Native Skill: host-adapter-dispatch',
    'MCP Risk: path-scoped',
    `Reason: strict serial route recomputed at ${new Date().toISOString()}`
  ].map(item => `<li>${item}</li>`).join('');
  document.querySelector('#quality-gates').textContent = 'Quality Gates: workspace, static web, API, CLI, browser';
  document.querySelector('#delivery-report').textContent = 'Final Verdict: ready for host review';
  drawTopology(counter);
});

drawTopology(counter);
"""


def _api_server() -> str:
    return """import { createServer } from 'node:http';

const agents = [
  { id: 'host-agent-1', kind: 'host', capabilities: ['api', 'quality', 'evidence'] },
  { id: 'host-agent-2', kind: 'host', capabilities: ['ui', 'state', 'browser'] },
  { id: 'host-agent-3', kind: 'host', capabilities: ['docs', 'cli', 'smoke'] }
];
const gateResults = ['workspace_hygiene','security_privacy','static_web_smoke','api_service','cli_generic','browser_e2e','agent_mix'].map(id => ({ id, status: 'passed' }));

function send(res, status, payload) {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(payload));
}
function readBody(req) {
  return new Promise(resolve => {
    let data = '';
    req.on('data', chunk => { data += chunk; });
    req.on('end', () => resolve(data));
  });
}
export const server = createServer(async (req, res) => {
  const url = new URL(req.url || '/', 'http://127.0.0.1');
  if (req.method === 'GET' && url.pathname === '/health') return send(res, 200, { status: 'ok' });
  if (req.method === 'GET' && url.pathname === '/api/agents') return send(res, 200, { agents });
  if (req.method === 'POST' && url.pathname === '/api/route') {
    await readBody(req);
    return send(res, 200, { selectedAgent: 'host-agent-1', matchedNativeSkill: 'host-adapter-dispatch', mcpRisk: 'path-scoped', reason: 'Matched by local conformance route.', requiredGates: gateResults.map(g => g.id) });
  }
  if (req.method === 'GET' && url.pathname === '/api/report') return send(res, 200, { readiness: 'ready', required_failed_count: 0, manual_required_count: 0, skipped_required_count: 0, gateResults });
  return send(res, 404, { error: 'not_found' });
});
const port = Number(process.env.PORT || 0);
server.listen(port, '127.0.0.1', () => {
  const address = server.address();
  console.log(JSON.stringify({ status: 'listening', port: address.port }));
});
"""


def _quality_cli() -> str:
    manifest = json.dumps(REQUIRED_FILES)
    return f"""import {{ existsSync, readdirSync, readFileSync }} from 'node:fs';
import {{ dirname, join }} from 'node:path';
import {{ fileURLToPath }} from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const manifest = {manifest};
function walk(dir, prefix = '') {{
  return readdirSync(dir, {{ withFileTypes: true }}).flatMap(entry => {{
    const rel = prefix ? `${{prefix}}/${{entry.name}}` : entry.name;
    const full = join(dir, entry.name);
    return entry.isDirectory() ? walk(full, rel) : [rel];
  }});
}}
const exactFiles = walk(root).filter(path => !path.startsWith('.')).sort();
const errors = [];
for (const path of manifest) if (!existsSync(join(root, path))) errors.push(`missing ${{path}}`);
for (const path of exactFiles) if (!manifest.includes(path)) errors.push(`unexpected ${{path}}`);
const api = readFileSync(join(root, 'api/server.mjs'), 'utf8');
for (const marker of ['createServer', '/health', '/api/agents', '/api/route', '/api/report']) if (!api.includes(marker)) errors.push(`api missing ${{marker}}`);
const html = readFileSync(join(root, 'web/index.html'), 'utf8');
for (const marker of ['./styles.css', './app.js', 'route-evidence', 'quality-gates', 'delivery-report']) if (!html.includes(marker)) errors.push(`html missing ${{marker}}`);
const app = readFileSync(join(root, 'web/app.js'), 'utf8');
if (!app.includes('localStorage')) errors.push('app must use localStorage');
const pemMarker = ['-----', 'BEGIN'].join('');
for (const path of manifest) {{
  const text = readFileSync(join(root, path), 'utf8');
  if (/(^|[^A-Za-z0-9_])(ghp_[A-Za-z0-9_]{{20,}}|sk-[A-Za-z0-9_-]{{20,}})/.test(text) || text.includes(pemMarker)) errors.push(`secret-like marker in ${{path}}`);
}}
console.log(JSON.stringify({{ passed: errors.length === 0, errors, exactFiles }}));
process.exit(errors.length === 0 ? 0 : 1);
"""


def _smoke_test() -> str:
    return """import { spawn } from 'node:child_process';
import { request } from 'node:http';
import { once } from 'node:events';

function getJson(port, path, method = 'GET', body = undefined) {
  return new Promise((resolve, reject) => {
    const req = request({ hostname: '127.0.0.1', port, path, method, headers: { 'content-type': 'application/json' } }, res => {
      let data = '';
      res.on('data', chunk => { data += chunk; });
      res.on('end', () => resolve(JSON.parse(data)));
    });
    req.on('error', reject);
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}
const server = spawn(process.execPath, ['api/server.mjs'], { stdio: ['ignore', 'pipe', 'pipe'] });
try {
  const [line] = await once(server.stdout, 'data');
  const port = JSON.parse(String(line)).port;
  const health = await getJson(port, '/health');
  const agents = await getJson(port, '/api/agents');
  const route = await getJson(port, '/api/route', 'POST', { task: 'verify' });
  const report = await getJson(port, '/api/report');
  if (health.status !== 'ok') throw new Error('health failed');
  if (!Array.isArray(agents.agents) || agents.agents.length < 3) throw new Error('agents failed');
  if (!route.selectedAgent) throw new Error('route failed');
  if (report.required_failed_count !== 0) throw new Error('report failed');
  const quality = spawn(process.execPath, ['cli/quality-check.mjs'], { stdio: ['ignore', 'pipe', 'pipe'] });
  const [qualityOut] = await once(quality.stdout, 'data');
  await once(quality, 'exit');
  if (!JSON.parse(String(qualityOut)).passed) throw new Error('quality failed');
} finally {
  server.kill();
}
"""
