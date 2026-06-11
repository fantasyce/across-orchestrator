# Across Orchestrator

![Quality](https://github.com/fantasyce/across-orchestrator/actions/workflows/quality.yml/badge.svg)
![Security](https://github.com/fantasyce/across-orchestrator/actions/workflows/security.yml/badge.svg)
![License](https://img.shields.io/badge/license-MIT-blue.svg)

Local-first task orchestration runtime for agent-to-agent delivery work.

Across Orchestrator is the task-runtime companion to Across Context. It is a
standalone product: host apps provide UI, credentials, local agent processes,
and user permissions; Across Orchestrator owns task lifecycle, contracts,
quality gates, evidence, and protocol surfaces.

## Current Status

`v0.5.1` refines the durable Agent Loop Runtime on top of the mature task
orchestration core that was split out from Across Agents Assistant. The
runtime keeps loop state, step checkpoints, approval gates, memory hooks, and
final output evidence in the external plugin so hosts can stay thin.

Validated in this repository:

- 429 repository tests pass, including the transplanted Across Agents Assistant
  orchestration suite and the new Agent Loop Runtime protocol tests.
- Sidecar-first host integration writes runtime metadata under
  `~/.across/run/across-orchestrator`.
- Durable task state defaults to `~/.across/data/across-orchestrator`.
- Existing legacy `~/.across-orchestrator` task/event files are backfilled into
  `~/.across/data/across-orchestrator` without overwriting newer files.
- The plugin manifest exposes CLI, sidecar, MCP, and Python SDK entrypoints.
- Hosts can inspect `plugin-status`, `health`, and
  `/.well-known/across-plugin.json` before routing work to the runtime.
- Hosts can start, resume, inspect, and audit durable agent loops through CLI,
  HTTP, MCP, or the Python runtime boundary.
- Hosting platforms can pass registered agent-container descriptors through the
  Python SDK boundary without adopting Across Agents Assistant internals.
- Hosts can run explicit plugin lifecycle actions, including uninstalling the
  runtime wrapper while preserving durable task data.
- The public `MatureOrchestrationEngine` wraps the transplanted `TaskState` and
  `TaskOrchestrator` for host-provided dispatch, validation, and owner-agent
  adapters.
- CLI, HTTP, and MCP expose the same deterministic demo task path as `v0.1.0`.
- CLI, HTTP, and MCP also expose an app-grade Release E2E scenario that uses the
  mature requirement, delivery contract, acceptance, quality gate, and evidence
  modules.
- Agent loop runs produce explicit `memory_search`, `task_dispatch`,
  `quality_gate`, `memory_write_candidate`, and `final_output` steps so hosting
  platforms can attach memory providers, agent dispatchers, and human approval
  UI without adopting Across Agents Assistant internals.

Across Orchestrator still does not own model keys, macOS permissions, or local
agent installation. Those remain host responsibilities by design.

## Why It Exists

Across Agents Assistant started as a macOS control panel with chat, local
agents, cloud LLMs, shared memory, and task orchestration in one app. The
long-term product shape is an ecosystem of independent modules:

- Across Agents Assistant: host app and control panel
- Across Context: shared memory plugin
- Across Orchestrator: task orchestration plugin

This lets the task runtime evolve independently and lets other hosts reuse the
same contract, quality, and evidence loop.

## Install From Source

```bash
git clone https://github.com/fantasyce/across-orchestrator.git
cd across-orchestrator
python3 -m pip install -e .
```

For development:

```bash
python3 -m pip install -e '.[dev]'
npm install
bash scripts/check.sh
```

`npm install` is only needed for the strict browser E2E probe. Without the Node
Playwright dev dependency, the mature quality report records the browser gate as
environment-blocked instead of silently passing it.

## Quick Demo Task

```bash
export ACROSS_ORCHESTRATOR_HOME="$(mktemp -d)"
mkdir -p /tmp/across-orchestrator-demo

TASK_ID="$(
  PYTHONPATH=src python3 -m across_orchestrator.cli submit \
    "Build a tiny product page" \
    --project /tmp/across-orchestrator-demo \
    --deliverable README.md \
    --deliverable web/index.html \
    --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["task_id"])'
)"

PYTHONPATH=src python3 -m across_orchestrator.cli run "$TASK_ID" --json
PYTHONPATH=src python3 -m across_orchestrator.cli evidence "$TASK_ID" --json
PYTHONPATH=src python3 -m across_orchestrator.cli quality "$TASK_ID" --json
```

## App-Grade Release E2E

This path exercises the transplanted Across Agents Assistant release-quality
contract and acceptance stack.

```bash
export ACROSS_ORCHESTRATOR_HOME="$(mktemp -d)"
mkdir -p /tmp/across-release-e2e

TASK_ID="$(
  PYTHONPATH=src python3 -m across_orchestrator.cli submit-release-e2e \
    --project /tmp/across-release-e2e \
    --run-label local-check \
    --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["task_id"])'
)"

PYTHONPATH=src python3 -m across_orchestrator.cli run "$TASK_ID" --json
PYTHONPATH=src python3 -m across_orchestrator.cli evidence "$TASK_ID" --json
```

The app-grade scenario delivers exactly:

- `README.md`
- `web/index.html`
- `web/styles.css`
- `web/app.js`
- `api/server.mjs`
- `cli/quality-check.mjs`
- `tests/e2e-smoke.mjs`

It then runs mature quality gates for artifact integrity, workspace hygiene,
security/privacy, agent mix, static web, browser E2E, API service, and generic
CLI.

## CLI

```bash
across-orchestrator init
across-orchestrator submit "Build docs" --project . --deliverable README.md --json
across-orchestrator submit-release-e2e --project /tmp/release-e2e --json
across-orchestrator run <task-id> --json
across-orchestrator status <task-id> --json
across-orchestrator events <task-id> --json
across-orchestrator evidence <task-id> --json
across-orchestrator quality <task-id> --json
across-orchestrator loop-start "Refactor checkout flow" --project . --json
across-orchestrator loop-run <loop-id> --json
across-orchestrator loop-status <loop-id> --json
across-orchestrator loop-events <loop-id> --json
across-orchestrator agent-card --json
across-orchestrator plugin-manifest --json
across-orchestrator plugin-status --json
across-orchestrator health --json
across-orchestrator serve --host 127.0.0.1 --port 8765
across-orchestrator mcp
```

## HTTP And A2A Card

Start the server:

```bash
across-orchestrator serve --host 127.0.0.1 --port 8765
```

Endpoints:

- `GET /health`
- `GET /.well-known/agent-card.json`
- `GET /.well-known/across-plugin.json`
- `POST /tasks`
- `POST /release-e2e`
- `POST /tasks/{task_id}/run`
- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/events`
- `GET /tasks/{task_id}/events/stream`
- `GET /tasks/{task_id}/evidence-bundle`
- `GET /tasks/{task_id}/quality-benchmark`
- `POST /loops`
- `POST /loops/{loop_id}/run`
- `GET /loops/{loop_id}`
- `GET /loops/{loop_id}/events`

## MCP Server

The MCP server exposes:

- `submit_task`
- `submit_release_e2e_task`
- `run_task`
- `get_task`
- `get_evidence_bundle`
- `get_agent_card`
- `start_agent_loop`
- `run_agent_loop`
- `get_agent_loop`
- `get_agent_loop_events`

It also exposes resources:

- `across-orchestrator://agent-card`
- `across-orchestrator://plugin-manifest`
- `across-orchestrator://plugin-status`
- `across-orchestrator://agent-loop-schema`

Run:

```bash
across-orchestrator mcp
```

## Host Boundary And Hosting Platforms

The public `across_orchestrator.engine.MatureOrchestrationEngine` wraps the
transplanted mature engine. Hosts provide:

- dispatcher adapter for local/cloud agent execution
- validator adapter
- owner-agent adapter
- optional persistence integration
- UI and approval prompts

Across Orchestrator keeps the contracts, waves, task state, acceptance,
remediation, and quality logic in the plugin.

For a hosting platform that exposes many user-owned agent containers, the
platform remains the A2A-facing host. Across Orchestrator mounts inside the
platform as a task-runtime plugin: it receives agent descriptors, creates the
delivery contract and execution waves, then asks the host to dispatch actual
agent work through the platform's own SDK, HTTP, MCP, or A2A adapters.

The lightweight SDK helper keeps that boundary serializable:

```python
from across_orchestrator.host_adapters import build_hosting_platform_contract

contract = build_hosting_platform_contract(
    "example-host",
    [
        {
            "agent_id": "frontend-agent",
            "display_name": "Frontend Agent",
            "endpoint": "https://host.example/agents/frontend-agent",
            "protocols": ["a2a", "sdk"],
            "capabilities": ["web-ui", "tests"],
        }
    ],
    memory_provider="across-context",
)
```

## Development Checks

```bash
python3 -m pip install -e '.[dev]'
npm install
bash scripts/check.sh
```

The Python package has no runtime dependencies. `pytest` and Node Playwright are
development/test dependencies only.

GitHub Quality and Security workflows run the same repository checks, CodeQL for
the Python source, and npm audit for the development-only browser probe
dependencies.
