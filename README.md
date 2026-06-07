# Across Orchestrator

Local-first task orchestration runtime for agent-to-agent delivery work.

> Alpha foundation: `v0.1.0` is not feature-parity with the mature task
> orchestration runtime inside Across Agents Assistant. It proves the independent
> product boundary, protocol surfaces, storage shape, and deterministic E2E
> skeleton. See [Parity Audit](docs/parity-audit.md) before treating it as a
> replacement for the app runtime.

Across Orchestrator is the task runtime companion to Across Context. It is a
standalone product: host apps provide UI, agent credentials, and local
permissions; Across Orchestrator owns task lifecycle, contracts, evidence,
quality checks, and protocol surfaces.

This repository is intentionally small for the first milestone. It proves the
runtime can submit, run, inspect, and verify deterministic tasks without being
embedded inside Across Agents Assistant. It does not yet include owner-agent
decomposition, multi-wave governance, acceptance parsing, remediation loops, or
release-grade quality gates.

## Why It Exists

Across Agents Assistant started as a macOS control panel with chat, local
agents, cloud LLMs, shared memory, and task orchestration in one app. That was
useful for discovering the workflow, but the long-term product shape is an
ecosystem of independent modules:

- Across Agents Assistant: host app and control panel
- Across Context: shared memory plugin
- Across Orchestrator: task orchestration plugin

If the task runtime can stand alone, any host can use the same contract,
evidence, and quality loop.

## What It Does

- Stores task state under `~/.across-orchestrator`
- Accepts tasks with explicit deliverable contracts
- Splits deliverables into deterministic subtasks
- Runs a built-in demo adapter for repeatable local E2E tests
- Supports a command adapter contract for future host-provided agents
- Emits append-only JSONL task events
- Builds evidence bundles with artifacts, hashes, quality, and event history
- Exposes CLI, HTTP/SSE, A2A-style Agent Card, and MCP stdio surfaces

## What It Does Not Yet Do

- Owner-agent decomposition
- Multi-wave DAG governance
- Agent-to-agent contract negotiation
- Local/cloud agent dispatch through real adapters
- Acceptance parsing and repair loops
- Workspace hygiene and release E2E gates
- Agent-mix enforcement
- Full evidence-bundle compatibility with Across Agents Assistant

## Install From Source

```bash
git clone https://github.com/fantasyce/across-orchestrator.git
cd across-orchestrator
python3 -m pip install -e .
```

For local development without installing:

```bash
PYTHONPATH=src python3 -m across_orchestrator.cli --help
```

## Quick Start

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

## CLI

```bash
across-orchestrator init
across-orchestrator submit "Build docs" --project . --deliverable README.md --json
across-orchestrator run <task-id> --json
across-orchestrator status <task-id> --json
across-orchestrator events <task-id> --json
across-orchestrator evidence <task-id> --json
across-orchestrator quality <task-id> --json
across-orchestrator agent-card --json
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
- `POST /tasks`
- `POST /tasks/{task_id}/run`
- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/events`
- `GET /tasks/{task_id}/events/stream`
- `GET /tasks/{task_id}/evidence-bundle`
- `GET /tasks/{task_id}/quality-benchmark`

Submit a task:

```bash
curl -s http://127.0.0.1:8765/tasks \
  -H 'Content-Type: application/json' \
  -d '{"goal":"Build docs","projectRoot":"/tmp/demo","deliverables":["README.md"],"agent":"demo"}'
```

## MCP Server

The MCP server exposes:

- `submit_task`
- `run_task`
- `get_task`
- `get_evidence_bundle`
- `get_agent_card`

Run:

```bash
across-orchestrator mcp
```

## Host Boundary

Across Orchestrator does not manage model keys, local agent installation,
macOS permissions, or chat UI. A host should provide those via adapters and use
Across Orchestrator as the runtime for task lifecycle and evidence.

Across Agents Assistant should eventually prefer an external
`across-orchestrator serve` process and fall back to its current built-in task
runtime only in compatibility mode.

## Development

```bash
bash scripts/check.sh
```

The first release has no runtime dependencies.
