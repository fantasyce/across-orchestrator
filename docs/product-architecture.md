# Across Orchestrator Product Architecture

## Product Position

Across Orchestrator is an open, local-first task orchestration runtime for
agent-to-agent delivery work. It owns task state, subtasks, waves, contracts,
events, evidence bundles, quality gates, and remediation policy.

It does not own chat UI, model credentials, macOS permissions, local agent
installation, or user preferences. A host application provides those surfaces
through adapters.

```text
Host app / IDE / CLI
  -> user interaction
  -> model credentials
  -> local/cloud agent execution
  -> permissions and approvals

Across Orchestrator
  -> task lifecycle
  -> owner orchestration state
  -> wave governance
  -> delivery contracts
  -> acceptance and remediation
  -> quality gates
  -> evidence bundle
```

## Runtime Layers

### Product Surface

The public package keeps stable protocol surfaces:

- CLI
- HTTP/SSE
- MCP stdio
- A2A-style Agent Card

These surfaces support both simple deterministic demo tasks and app-grade
Release E2E tasks.

### Mature Engine

The mature engine is transplanted from Across Agents Assistant and remains
covered by the original test suite. It includes:

- `TaskState`
- `TaskOrchestrator`
- `TaskDispatcher`
- `OwnerAgent`
- delivery contracts
- contract acceptance
- project acceptance
- quality gates
- quality benchmark
- release E2E scenario definitions
- release evaluation

The public wrapper is:

```python
from across_orchestrator.engine import MatureOrchestrationEngine
```

Hosts pass dispatcher, validator, and owner-agent adapters into this wrapper.

### Compatibility Namespace

The package currently includes an `across_agents_assistant` compatibility
namespace. This is deliberate: it lets the original app orchestration tests run
unchanged while the public product API stabilizes under `across_orchestrator`.

The compatibility namespace should shrink over time as the mature modules are
renamed behind stable public interfaces.

## Host Adapter Boundary

Across Orchestrator never stores provider secrets or macOS approval state. A
host must provide:

- local/cloud agent execution
- available-agent detection
- LLM gateway calls
- user approval decisions
- scoped tool execution
- persistence integration when needed

The host adapter protocols live in:

```text
src/across_orchestrator/host_adapters.py
```

## State Ownership

The standalone state directory defaults to:

```text
~/.across-orchestrator
```

It can be overridden with:

```text
ACROSS_ORCHESTRATOR_HOME=/path/to/state
```

The project workspace remains wherever the user or host points the task.
Artifacts are written only under `projectRoot`.

## Quality Model

Across Orchestrator exposes two quality paths:

- Demo path: required files, hashes, events, and simple quality.
- App-grade path: transplanted release contract acceptance with artifact
  integrity, workspace hygiene, security/privacy, agent mix, static web, browser
  E2E, API service, and CLI generic probes.

The browser probe requires Node Playwright in the development environment. If it
is unavailable, the mature acceptance report marks the gate as
environment-blocked and the delivery as partial.

## Integration Direction

Across Agents Assistant should consume Across Orchestrator the same way it
consumes Across Context:

1. Prefer an external plugin process.
2. Use app-provided adapters for real local/cloud agent execution.
3. Report implementation mode in UI and diagnostics.
4. Fall back to built-in compatibility mode when external mode is unavailable.
5. Remove duplicated app internals only after external mode passes the same
   Release E2E and restart-recovery gates.
