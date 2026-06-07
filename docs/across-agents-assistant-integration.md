# Across Agents Assistant Integration Plan

Across Agents Assistant should consume Across Orchestrator the same way it now
consumes Across Context: plugin first, compatibility fallback second.

## Target Boundary

Across Agents Assistant owns:

- macOS UI and user interaction
- chat panel and project picker
- model/provider credentials
- local agent installation and availability checks
- macOS permissions and approval prompts
- plugin settings and implementation status display

Across Orchestrator owns:

- task, subtask, wave, and event lifecycle
- delivery contracts and required artifacts
- agent assignment plan
- evidence bundle and quality benchmark
- remediation policy
- A2A-style task and artifact protocol

## Migration Phases

### Phase 1: External Runtime Probe

Add an app-side `OrchestratorPluginManager` that probes:

```bash
across-orchestrator serve --host 127.0.0.1 --port 0
```

or a configured HTTP endpoint. Display implementation mode:

- `external`
- `builtin_compatibility`
- `unavailable`

No existing in-app task runtime is removed in this phase.

### Phase 2: Read-Only Task Console

Let the app read external runtime tasks through:

- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/events`
- `GET /tasks/{task_id}/evidence-bundle`
- `GET /tasks/{task_id}/quality-benchmark`

The current task UI can show external tasks in a separate source filter.

### Phase 3: Submit And Run Via External Runtime

Route new task submission to external runtime when implementation mode is
`external`:

- app gathers project, owner/subtask agent choices, and permissions
- app passes task goal, projectRoot, deliverables, and adapter metadata
- Across Orchestrator owns lifecycle after submission

If external runtime is unavailable and mode is auto, the app falls back to the
existing built-in runtime.

### Phase 4: Host-Provided Agent Adapters

Across Orchestrator should not store provider credentials. The app should expose
agent execution to the runtime through one of:

- command adapter with scoped environment
- local loopback callback endpoint
- MCP tool call back into the host

The runtime receives only the task/subtask input and scoped project permissions.

### Phase 5: Remove Duplicated Built-In Runtime

Only after the external runtime passes the same release E2E bar as the current
app runtime should the app remove duplicated orchestration internals. Until
then, the in-app runtime remains a compatibility bridge.

## Required Compatibility Gates

Before the app defaults to external Orchestrator:

- submit/run/status/evidence/quality work through HTTP
- event stream updates the Swift task UI
- task state survives app and runtime restart
- agent execution can be scoped by projectRoot and writable files
- evidence bundle maps to the current Release Evidence Center model
- existing complex Release E2E can be represented as external contracts
- fallback to built-in runtime is visible and non-destructive

## Non-Goals

- Do not move chat into Across Orchestrator.
- Do not move model keys into Across Orchestrator.
- Do not require Across Agents Assistant to install external runtime before the
  fallback path is proven.
- Do not remove current task code until external runtime can pass release-grade
  E2E.
