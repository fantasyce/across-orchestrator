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

`v0.7.3` is the synchronized main-branch release for the AAA plugin ecosystem.
It keeps the generic host compatibility runtime from `v0.7.2` unchanged while
publishing a fresh main-derived tag for hosts that pin the full plugin set
together.

`v0.7.2` is the generic host compatibility release. It makes Across
Orchestrator explicitly reusable outside Across Agents Assistant: Codex,
CloudCode Desktop, CloudCode CLI, Claude Desktop, Claude CLI, OpenClaw, Hermes,
and any CLI, HTTP, MCP, or Python-SDK capable host can install the managed
runtime under `~/.across`, register external agent adapters, drive durable Agent
Loop work, and consume quality-gate, evidence, telemetry, and protocol-gateway
surfaces without importing AAA code or reading a developer checkout.

`v0.7.1` is the generic agent-plugin runtime hardening release. It adds
host-neutral external agent registration and protocol-gateway helpers, preserves
the Agent Loop action-plan quality gate inside the loop budget, and keeps
managed runtime wrappers relocatable under `~/.across/bin` instead of binding
hosts to a source checkout path.

`v0.7.0` is the Loop Engineering runtime release for the Across ecosystem. It
adds the Across Autopilot execution metadata contract: Agent Loop metadata can
carry `metadata.autopilot` with run id, spec id, LoopSpec schema, evidence
schema, action policy, and sandbox summary. The runtime validates that contract
before accepting the metadata and reflects a non-secret Autopilot summary
through loop status and evidence summaries so hosts can verify that an
Autopilot-supervised run actually reached Orchestrator.

`v0.7.0` also adds a host-declared model decision boundary for Agent Loop
dispatch. When loop metadata includes `model_policy.required=true` and a
`host_model_command`, Orchestrator calls that command with JSON loop context,
records non-secret provider/model/decision-hash evidence, and keeps raw model
credentials with the host. This enables model-backed Autopilot loops without
coupling Orchestrator to AAA internals.

The same release completes the current Agent Loop runtime contract with bounded
telemetry, `after_sequence` event resume for HTTP/CLI/MCP consumers,
host-declared budget and concurrency enforcement, structured
`budget_exceeded` cancellation, and routing evidence that includes reasons plus
candidate alternatives.

`v0.6.17` centralizes Agent Loop structured cancel category policy so CLI,
HTTP, MCP schemas, health, and host release evidence share the same category
list and release-blocking classification.

`v0.6.16` promotes compact Agent Loop evidence into host release evidence. CLI,
HTTP, and MCP evidence summaries now include `host_release_evidence` with
readiness, checks, risks, and next actions derived from durable event audit,
routing, recovery, memory-candidate, and cancellation signals.
`healthSummary` remains the loop runtime-state surface for stale leases,
current actions, cancellation state, and executable controls; host release
evidence is the release-readiness surface derived from durable evidence.

`v0.6.15` added compact Agent Loop evidence summaries for hosts that need
release or audit views without parsing full event streams. CLI, HTTP, and MCP
expose routing outcomes, recovery decisions, memory-candidate counts,
cancellation category, and event audit coverage without raw transcripts, memory
text, logs, or stack traces.

`v0.6.14` added true live Agent Loop timeline streaming. The loop events stream
keeps the existing finite SSE snapshot by default, and hosts can opt into
`?follow=true` to receive newly appended durable loop events until the loop
reaches a terminal state, pauses for approval, or idles out.

`v0.6.13` added durable Agent Loop event audit metadata and structured
`cancel_category` values. Loop and task events now expose `event_id`,
monotonic `sequence`, and `correlation_id` fields so hosts can reconstruct
step, heartbeat, task, and cancellation chains without parsing nested payloads.
Cancellation keeps the existing free-form reason text and adds a stable
category for UI, health, and MCP consumers.
`v0.6.12` added opt-in Agent Loop recovery policy, host capability-hint
routing, and structured memory write-candidate summaries. Loop metadata can
declare bounded retry, remediation, or human-handoff behavior for failed steps;
hosts can provide non-secret capability hints so Orchestrator can pick a
compatible adapter; and `memory_write_candidate` emits a compact JSON summary
for Across Context review.
`v0.6.11` added the read-only Agent Loop health surface for hosts that need to
inspect durable loop state without mutating it. CLI, HTTP, MCP, the plugin
manifest, and the public agent card expose loop health summaries with the
current action, pending approval, execution lease, detached dispatch count,
recent `failure_type` counts, cancellation state, and executable controls.
The durable Agent Loop Runtime still persists execution leases before adapter
dispatch, allows long-running adapters to renew leases through heartbeat hooks,
recovers stale leases deterministically, routes cancellation to cooperative
dispatchers, terminates command adapter subprocess groups, preserves root
`failure_type` metadata across failed steps, checkpoints, loop events, task
events, and task metadata, and keeps terminal task execution idempotent.
Metadata-driven `agentRouting` also lets hosts route dispatch by action type or
latest failed quality gate.
The runtime still keeps loop state, step checkpoints, approval gates,
declarative agent adapters, adapter-backed memory hooks, host-supplied action
plans, dynamic remediation dispatch, host-owned loop controls, and final output
evidence inside the plugin so hosts can stay thin.
This release keeps the product-mode path boundary from `v0.6.6` and the generic
agent adapter descriptors introduced for hosts beyond Across Agents Assistant.

Validated in this repository:

- Repository checks cover the standalone task runtime, protocol surfaces,
  plugin manifest, host conformance scenario, and Agent Loop Runtime.
- Sidecar-first host integration writes runtime metadata under
  `~/.across/run/across-orchestrator`.
- Durable task state defaults to `~/.across/data/across-orchestrator`.
- Fresh installs and managed plugin runs use only the unified `~/.across`
  ecosystem root. Old standalone `~/.across-orchestrator` task stores are not
  read or copied automatically unless a host explicitly opts into a custom
  `ACROSS_ORCHESTRATOR_HOME`.
- Product hosts can set `ACROSS_ORCHESTRATOR_PRODUCT_MODE=1`; development
  checkout commands and runtime/data root overrides under protected user
  project locations are reported as `needs_repair`, blocked, or ignored instead
  of being executed or used. A protected `across-orchestrator` found on `PATH`
  is not reported as the available product command. Set
  `ACROSS_ORCHESTRATOR_DEVELOPER_MODE=1` only for intentional source checkout
  development.
- In product mode, an explicit `serve --runtime-info` path under protected user
  project locations is ignored and runtime metadata stays under
  `~/.across/run/across-orchestrator`; developer mode preserves explicit
  runtime-info paths for local debugging.
- The plugin manifest exposes CLI, sidecar, MCP, and Python SDK entrypoints.
- Hosts can inspect `plugin-status`, `health`, and
  `/.well-known/across-plugin.json` before routing work to the runtime.
- Hosts can start, resume, inspect, and audit durable agent loops through CLI,
  HTTP, MCP, or the Python runtime boundary.
- Codex, CloudCode Desktop, CloudCode CLI, Claude Desktop, Claude CLI, AAA, and
  other generic agent hosts can use the same managed plugin contract. The host
  owns UI, model credentials, process launch, and user approval; Orchestrator
  owns task lifecycle, Agent Loop state, quality gates, evidence, and protocol
  surfaces.
- Hosting platforms can pass registered agent-container descriptors through the
  Python SDK boundary without adopting host application internals.
- Hosts can run explicit plugin lifecycle actions, including uninstalling the
  runtime wrapper while preserving durable task data.
- The public `MatureOrchestrationEngine` wraps the standalone runtime for
  host-provided dispatch, validation, and owner-agent adapters.
- CLI, HTTP, and MCP expose the same deterministic demo task path as `v0.1.0`.
- CLI, HTTP, and MCP also expose an app-grade Release E2E scenario that uses the
  mature requirement, delivery contract, acceptance, quality gate, and evidence
  modules.
- Agent loop runs produce explicit `memory_search`, `task_dispatch`,
  `quality_gate`, `remediation_dispatch`, `memory_write_candidate`, and
  `final_output` steps so hosting platforms can attach memory providers, agent
  dispatchers, quality gates, finalizers, and human approval UI without
  adopting host application internals.
- Agent Loop v2 can call Across Context through a subprocess-backed memory
  provider when hosts set `ACROSS_ORCHESTRATOR_MEMORY_PROVIDER=across-context`.

Across Orchestrator still does not own model keys, macOS permissions, or local
agent installation. Those remain host responsibilities by design.

The current Agent Loop runtime contracts are summarized in
[Agent Loop Runtime RFCs](AGENT_LOOP_RFC.md). Further multi-agent product UX or
automation behavior should start from a new product spec rather than ad hoc
runtime changes.

## Why It Exists

The Across ecosystem is organized as independent modules with explicit host
boundaries:

- Across Agents Assistant: host app and control panel
- Across Context: shared memory plugin
- Across Orchestrator: task orchestration plugin
- Across Autopilot: LoopSpec supervision and autonomous iteration plugin

This lets the task runtime evolve independently and lets other hosts reuse the
same contract, quality, and evidence loop.

## Generic Host Compatibility

Across Orchestrator is not an AAA-internal module. Product hosts should install
the pinned release into `~/.across/plugins/across-orchestrator`, expose
`~/.across/bin/across-orchestrator`, and communicate through CLI, HTTP, MCP, or
the Python SDK. The same contract is intended for Codex, CloudCode
Desktop/CLI, Claude Desktop/CLI, AAA, and other local or remote agent hosts
that can provide agent descriptors, dispatch callbacks, model credentials, and
approval UX.

## Install From Source

```bash
git clone https://github.com/fantasyce/across-orchestrator.git
cd across-orchestrator
python3 -m pip install -e .
```

Or install the current release tag directly from GitHub:

```bash
python3 -m pip install "git+https://github.com/fantasyce/across-orchestrator.git@v0.7.3"
```

The GitHub release is source-first. There is no attached wheel asset for
`v0.7.3`; if a packaged host needs a wheel, build it from the pinned tag or
attach the wheel to the release before using a wheel URL.

Packaged hosts should install from the pinned Git tag or an explicitly attached
release wheel into a managed runtime under
`~/.across/plugins/across-orchestrator` and expose the wrapper at
`~/.across/bin/across-orchestrator`.

For development:

```bash
python3 -m pip install -e '.[dev]'
npm install
bash scripts/check.sh
```

`npm install` is used by the strict Playwright browser probe. When Playwright is
not installed, the release E2E path falls back to a self-contained Node DOM-shim
probe. If Node itself is unavailable, the mature quality report records the
browser gate as environment-blocked instead of silently passing it.

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

This path exercises the host-agent full delivery conformance scenario. It
builds a serial dependency chain where planning and data decisions affect later
UI, API, CLI, browser, and documentation artifacts, then records quality gates,
remediation behavior, and final evidence for a host to inspect.

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

## Agent Loop V2 Memory Provider

Agent Loop v2 keeps memory access behind a host-selected provider. To use
Across Context from the runtime, set:

```bash
export ACROSS_ORCHESTRATOR_MEMORY_PROVIDER=across-context
export ACROSS_CONTEXT_COMMAND="$HOME/.across/bin/across-context"
```

In product mode, `ACROSS_CONTEXT_COMMAND` must point at the managed wrapper
under `~/.across/bin`. If it points at a protected source checkout, diagnostics
return `needs_repair` and memory calls return a blocked observation instead of
executing that command. Use `ACROSS_ORCHESTRATOR_DEVELOPER_MODE=1` only for
local runtime development.

The runtime then searches active global/project memory before dispatch and
writes compact post-loop summaries as pending project memory candidates. Missing
or failing memory providers are recorded in loop observations instead of
silently aborting the task runtime.

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
across-orchestrator loop-approve <loop-id> <action-id> --json
across-orchestrator loop-reject <loop-id> <action-id> --reason "Needs a safer plan" --json
across-orchestrator loop-cancel <loop-id> --reason "User stopped the run" --category user_cancelled --json
across-orchestrator loop-retry <loop-id> <step-id> --json
across-orchestrator loop-status <loop-id> --json
across-orchestrator loop-events <loop-id> --json
across-orchestrator loop-events <loop-id> --after-sequence 42 --json
across-orchestrator loop-telemetry <loop-id> --json
across-orchestrator agent-card --json
across-orchestrator plugin-manifest --json
across-orchestrator plugin-status --json
across-orchestrator health --json
across-orchestrator serve --host 127.0.0.1 --port 8765
across-orchestrator mcp
```

### Agent Loop Lease And Routing Contract

Agent Loop actions persist a running checkpoint before dispatch adapters run.
Running action checkpoints include an `execution` block with `lease_id`,
`started_at`, `heartbeat_at`, `lease_seconds`, and `lease_expires_at`. Completed
and failed action checkpoints keep that same lease and add `completed_at` plus
`duration_ms`.

Long-running dispatch adapters receive a heartbeat hook in their dispatch
context and can call it to renew the active lease. Each renewal updates
`heartbeat_at`, moves `lease_expires_at`, increments the renewal count, and emits
`loop.step.heartbeat`. If a later run sees an expired running lease, the runtime
marks the step failed, emits `loop.step.lease_expired`, and fails the loop with
`action_lease_expired`.

Dispatch adapters also receive a `cancellation` token with `is_cancelled()`,
`reason()`, `category()`, and `raise_if_cancelled()`. `loop-cancel` records a
`loop.cancel_requested` marker outside the loop execution lock, so running
adapters can observe it while work is still active. When the token is raised, the
runtime marks the running step `cancelled`, emits `loop.step.cancelled`, clears
the lease, and finishes the loop as `cancelled`. Command adapters terminate their
subprocess group before raising the cancellation error.
Cancellation preserves the free-form reason text and also records a structured
`cancel_category`: `user_cancelled`, `shutdown`, `superseded`,
`timeout_cancelled`, or `budget_exceeded`. If omitted, the category is inferred
from the reason and defaults to `user_cancelled`. CLI, HTTP, MCP schemas,
health, telemetry, and release evidence all use the same runtime cancel category
policy; `shutdown`, `timeout_cancelled`, and `budget_exceeded` are treated as
release-blocking categories, while `user_cancelled` and `superseded` require
host attention.

The dispatch cancellation guard invokes host dispatch adapters behind a managed
runtime wait loop. This lets the Agent Loop finish as `cancelled` even when a
custom adapter ignores the cancellation token and never calls heartbeat. The
guard latches the cancellation token, so the same dispatch context keeps
reporting cancellation even after durable cancel markers are cleared. If a
non-runtime host dispatcher stays blocked, the guard emits `loop.dispatch.detached`
before the Agent Loop records `loop.step.cancelled`. The guard cannot terminate noncooperative in-process Python callbacks; use the command adapter path for
subprocesses that must be killed by the runtime.

Runtime-backed dispatchers that mutate task or subtask state require a cancel ack
before the guard returns. This keeps `subtask.cancelled` and `task.cancelled`
events durable before the Agent Loop publishes `agent_loop.cancelled`.

Failed steps, checkpoints, and failed task/loop events include a stable
`failure_type` for remediation and UI routing. Current values are
`adapter_error`, `timeout`, `quality_failed`, `approval_rejected`,
`lease_expired`, `environment_blocked`, and `max_turns_exceeded`.

Newly appended Agent Loop and task events include durable audit metadata: a
unique `event_id`, a monotonic per-loop or per-task `sequence`, and a
`correlation_id` derived from the event's most specific durable id. Loop events
promote `step_id`, `action_id`, and `task_id` to top-level fields when present;
task events promote `loop_id` from task metadata plus `subtask_id` when present.
Hosts can reconstruct `loop.step.started -> loop.step.heartbeat ->
loop.step.completed/failed/cancelled -> task/subtask event` chains without
parsing nested payloads.
`GET /loops/{loop_id}/events` and
`GET /loops/{loop_id}/events/stream` accept `after_sequence=N` so hosts can
resume from the highest sequence they have already rendered. The stream keeps
the existing finite SSE snapshot shape. Hosts that need live timeline updates
can add `?follow=true`; the sidecar then tails durable loop events until the
loop completes, fails, stops, is cancelled, reaches an approval wait, or the
stream is idle for 30 seconds.

Hosts can tune the lease with loop metadata `actionLeaseSeconds` or
`action_lease_seconds`. Hosts can also set `agentRouting` or `agent_routing` to
select dispatch agents by action type or by the latest failed quality gate, for
example routing `remediation_dispatch.browser_e2e` to a browser specialist.
For host-owned capability routing, `agentCapabilityHints` may include a
declarative `registry.agents[]` snapshot plus `preferred` and
`constraints.requireCapability` hints. The Orchestrator only matches declared
agent ids, aliases, skills, plugins, tools, and capability labels; it never reads
host credentials, model keys, CLI install paths, or agent upgrade state.
`GET /loops/{loop_id}/evidence-summary` exposes a compact read-only summary of
durable loop evidence for hosts that need a release or audit surface without
parsing the full event stream. The summary includes event audit coverage,
recovery decisions, recovered steps, routing outcomes, memory-candidate counts,
structured host release evidence, and cancellation category, while excluding
raw transcripts, memory text, logs, and stack traces.

`GET /loops/{loop_id}/telemetry` exposes bounded runtime metrics for host
diagnostics and release review. The telemetry surface includes compact status,
duration, recovery, routing, memory-candidate, cancellation, and budget signals
without raw observations, memory text, logs, stack traces, provider keys, or
local absolute paths.

Hosts can declare loop budgets through metadata `agentLoopBudget`,
`agent_loop_budget`, or `budget`. Supported fields include
`maxConcurrentLoops`, `maxTurnsPerLoop`, and `maxRuntimeSeconds` with snake-case
aliases. Excess concurrent starts are rejected with a structured `409`; turn or
runtime exhaustion stops the loop with `cancel_category: budget_exceeded`.

### Agent Loop Recovery Policy Contract

Recovery is opt-in. Without `metadata.recoveryPolicy`, adapter failures, quality
failures, and expired action leases keep the existing fail-fast behavior.

Hosts may attach:

```json
{
  "recoveryPolicy": {
    "byFailureType": {
      "lease_expired": {"action": "retry", "maxRetries": 1},
      "quality_failed": {"action": "remediation", "maxRetries": 1},
      "adapter_error": {"action": "require_human", "maxRetries": 1},
      "approval_rejected": {"action": "stop", "maxRetries": 0},
      "environment_blocked": {"action": "stop", "maxRetries": 0},
      "timeout": {"action": "retry", "maxRetries": 1}
    },
    "defaultAction": "stop"
  }
}
```

Supported actions are `stop`, `retry`, `remediation`, and `require_human`.
`retry` rolls the durable loop state back to the failed step and lets the normal
planner select the next action. `remediation` schedules one
`remediation_dispatch` action. `require_human` adds a pending approval step for
the failed action. `maxRetries` is counted per loop, `failure_type`, and recovery
action, using append-only events; it never resets across retries.

Each policy decision emits `loop.step.recovery_decision`. Applied recoveries also
emit `loop.step.recovered` with the failed step id, selected recovery action,
attempt number, and next action or approval id. Recovery never crosses loop
boundaries and never retries indefinitely.

### Agent Loop Memory Candidate Summary

When `memory_policy.writeCandidates` is enabled, the `memory_write_candidate`
action writes a pending Across Context memory whose text is a compact JSON
summary with schema `agent-loop-memory-candidate/1.0`. The summary contains only
durable, whitelisted fields: loop id, goal, outcome, step decisions, artifacts,
commands, failure types, remediation outcomes, and memory references. It avoids
raw transcripts, large logs, stack traces, screenshots, credentials, and
temporary tool errors. Across Context still owns memory storage and review state;
new candidates always start as `pending`.

### Agent Loop Host Release Evidence

The `v0.7.3` Agent Loop runtime covers the release-blocking durability,
cancellation, structured cancel categories, event audit metadata, live timeline
streaming, compact evidence summaries, routing, terminal failure propagation,
terminal task idempotency, read-only loop health inspection, opt-in recovery
policy, capability-hint routing, structured memory candidate semantics, bounded
telemetry, event stream resume, and runtime budget/concurrency guardrails. The
evidence summary promotes those durable signals into `host_release_evidence` so
host apps can display a concise release-readiness surface without re-parsing raw
events. The release evidence includes `readiness` (`ready`, `attention`, or
`blocked`), stable checks for event audit, capability routing, recovery, memory
candidates, cancellation, telemetry, resume readiness, and budget/concurrency,
plus compact risks and next actions.

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
- `POST /loops/{loop_id}/actions/{action_id}/approve`
- `POST /loops/{loop_id}/actions/{action_id}/reject`
- `POST /loops/{loop_id}/cancel`
- `POST /loops/{loop_id}/steps/{step_id}/retry`
- `GET /loops/{loop_id}`
- `GET /loops/{loop_id}/health`
- `GET /loops/{loop_id}/evidence-summary`
- `GET /loops/{loop_id}/telemetry`
- `GET /loops/{loop_id}/events`
- `GET /loops/{loop_id}/events/stream`

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
- `approve_agent_loop_action`
- `reject_agent_loop_action`
- `cancel_agent_loop`
- `retry_agent_loop_step`
- `get_agent_loop`
- `get_agent_loop_health`
- `get_agent_loop_evidence_summary`
- `get_agent_loop_telemetry`
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
standalone runtime. Hosts provide:

- dispatcher adapter for local/cloud agent execution
- validator adapter
- owner-agent adapter
- optional persistence integration
- UI and approval prompts

Across Orchestrator keeps the contracts, waves, task state, acceptance,
remediation, and quality logic in the plugin.

The distribution boundary is enforced in tests and packaging:

- `pyproject.toml` includes only the `across_orchestrator*` namespace.
- Production code must not import `across_agents_assistant`.
- Vendored AAA source trees and parity fixture copies are not allowed.
- Host compatibility is expressed through serializable host descriptors,
  plugin manifests, CLI, HTTP, MCP, and Python SDK contracts.

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
development/test dependencies only; the browser E2E gate can also pass through
the built-in Node DOM-shim fallback when Playwright is unavailable.

GitHub Quality and Security workflows run the same repository checks, CodeQL for
the Python source, and npm audit for the development-only browser probe
dependencies.
