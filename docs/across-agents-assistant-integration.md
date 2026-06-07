# Across Agents Assistant Integration Plan

Across Agents Assistant should consume Across Orchestrator the same way it
consumes Across Context: plugin first, compatibility fallback second.

## Boundary

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
- owner orchestration state
- acceptance and remediation policy
- evidence bundle and quality benchmark
- A2A-style task and artifact protocol

## Integration Modes

### External Process Mode

The app starts or connects to:

```bash
across-orchestrator serve --host 127.0.0.1 --port <port>
```

The app then submits tasks and reads status/evidence through HTTP:

- `POST /tasks`
- `POST /release-e2e`
- `POST /tasks/{task_id}/run`
- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/events`
- `GET /tasks/{task_id}/evidence-bundle`
- `GET /tasks/{task_id}/quality-benchmark`

This mode is best for plugin isolation and for non-app hosts.

### Embedded Engine Mode

The app imports:

```python
from across_orchestrator.engine import MatureOrchestrationEngine
```

Then it injects app-owned adapters:

- dispatcher adapter backed by the app's local/cloud agent execution
- validator adapter
- owner-agent adapter backed by the app's LLM gateway and native skill routing
- optional persistence adapter

This mode is best while Across Agents Assistant still needs tight control over
permissions, approvals, and existing task UI behavior.

### Built-In Compatibility Mode

If the plugin is unavailable, the app can keep using its current built-in
runtime. The UI should report this as `builtin_compatibility` so users and
maintainers know which implementation is active.

## Migration Phases

### Phase 1: Probe And Diagnostics

Add an app-side plugin manager that detects:

- external `across-orchestrator` executable
- HTTP health endpoint
- Python package importability
- version and Agent Card metadata

Display implementation mode:

- `external`
- `embedded_plugin`
- `builtin_compatibility`
- `unavailable`

### Phase 2: Read-Only External Task Console

Let the app display external runtime tasks from:

- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/events`
- `GET /tasks/{task_id}/evidence-bundle`
- `GET /tasks/{task_id}/quality-benchmark`

### Phase 3: App-Grade Release E2E Through Plugin

Run the fixed release scenario through:

```bash
across-orchestrator submit-release-e2e --project <project>
```

or:

```http
POST /release-e2e
```

This verifies the app can consume plugin evidence that comes from the same
contract and quality modules as the mature in-app runtime.

### Phase 4: Host-Provided Agent Adapters

Route real task execution through `MatureOrchestrationEngine` with app-provided
adapters. Across Orchestrator receives scoped task/subtask input and returns
task state, events, contracts, quality results, and evidence.

The plugin must not persist provider keys or silently approve tools.

### Phase 5: Default To Plugin

Only after external or embedded plugin mode passes the same release-grade gates
as the built-in runtime should the app default to the plugin.

## Required Compatibility Gates

- original orchestration parity tests pass in the plugin repo
- submit/run/status/evidence/quality work through public surfaces
- event stream updates the Swift task UI
- task state survives app and runtime restart
- app-provided dispatch respects projectRoot and writable-file scope
- evidence bundle maps to the current Release Evidence Center model
- fixed complex Release E2E passes with browser, API, CLI, static web, security,
  workspace hygiene, and agent-mix gates
- fallback to built-in runtime is visible and non-destructive

## Non-Goals

- Do not move chat into Across Orchestrator.
- Do not move model keys into Across Orchestrator.
- Do not move macOS approval prompts into Across Orchestrator.
- Do not remove current app task code until plugin mode passes release-grade
  parity under real app adapters.
