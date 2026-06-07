# Across Orchestrator Product Architecture

## Product Position

Across Orchestrator is an open, local-first task orchestration runtime for
agent-to-agent delivery work. It owns task state, waves, subtasks, contracts,
events, evidence bundles, quality checks, and remediation policy.

It does not own chat UI, model credentials, macOS permissions, local agent
installation, or user preferences. A host application provides those surfaces
and talks to Across Orchestrator through stable protocols.

```text
Host app / IDE / CLI
  -> agent configuration
  -> user approval and permissions
  -> project selection
  -> UI console

Across Orchestrator
  -> task lifecycle
  -> agent assignment
  -> delivery contracts
  -> event stream
  -> evidence bundle
  -> quality benchmark
```

## First Release Scope

The first release proves the product can stand alone before Across Agents
Assistant depends on it.

It includes:

- a Python package with no runtime dependencies
- a CLI entrypoint
- a local JSON event store
- deterministic task, subtask, contract, artifact, and evidence models
- a built-in demo agent adapter for repeatable E2E tests
- a command adapter contract for future host-provided agents
- a stdlib HTTP API with task endpoints and an A2A-style Agent Card
- an MCP stdio server exposing orchestration tools
- tests for runtime, CLI, HTTP, MCP, and end-to-end task delivery

It intentionally does not include:

- LLM provider keys
- macOS permissions
- Swift UI
- Across Agents Assistant task page migration
- full owner-agent decomposition copied from the app

Those stay in the host until the independent runtime proves stable.

## Protocol Shape

Across Orchestrator is A2A-first and MCP-compatible.

A2A-style surfaces:

- `/.well-known/agent-card.json`
- task lifecycle state
- messages/events
- artifacts
- streaming status events

MCP-compatible surfaces:

- `submit_task`
- `run_task`
- `get_task`
- `get_evidence_bundle`
- `get_agent_card`

HTTP surfaces:

- `GET /health`
- `GET /.well-known/agent-card.json`
- `POST /tasks`
- `POST /tasks/{task_id}/run`
- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/events`
- `GET /tasks/{task_id}/events/stream`
- `GET /tasks/{task_id}/evidence-bundle`
- `GET /tasks/{task_id}/quality-benchmark`

## Data Ownership

The local state directory defaults to:

```text
~/.across-orchestrator
```

It can be overridden with:

```text
ACROSS_ORCHESTRATOR_HOME=/path/to/state
```

The project workspace remains wherever the user or host points the task.
Artifacts are written only under `projectRoot`.

## Host Integration Strategy

Across Agents Assistant should eventually follow the same plugin-first pattern
used for Across Context:

1. Prefer external `across-orchestrator serve`.
2. Report implementation mode in API/UI: `external` or `builtin_compatibility`.
3. Fall back to the current in-app runtime only in auto mode.
4. Remove duplicated in-app orchestration internals after external mode becomes
   reliable.

The first milestone does not remove existing app code. It creates the product
that the app can later consume.
