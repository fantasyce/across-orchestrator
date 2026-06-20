# Agent Loop Runtime Contracts

This document records the accepted Agent Loop runtime contracts implemented by
Across Orchestrator after the Across Agents Assistant `v0.8.28` host-side
closeout. Future product behavior should start from a new RFC instead of
silently extending these contracts.

Across Orchestrator owns loop lifecycle, event audit, recovery, routing
evidence, budget enforcement, bounded telemetry, and protocol surfaces. Hosts own
UI, credentials, local agent processes, explicit user controls, and release
approval. Across Context owns memory policy and pending review.

## Acceptance Decision

Accepted: 2026-06-20.

Decision owner: product owner request in the Agent Loop closeout cycle.

Decision: implement RFC 1 telemetry, RFC 2 event resume, RFC 3 budget and
concurrency policy, and RFC 4 routing evidence as the final engineering scope
for the current Agent Loop release-quality contract.

Acceptance criteria:

- HTTP, CLI, and MCP expose the same telemetry and event-resume contract.
- Telemetry is bounded and excludes prompts, raw observations, raw memory text,
  stack traces, provider keys, local absolute paths, and hidden reasoning.
- Budget and concurrency policy is host-declared, visible to hosts, and enforced
  by Orchestrator rather than by AAA.
- Budget exhaustion uses structured `cancel_category: budget_exceeded` and is
  release-blocking.
- Routing evidence exposes source, reason, selected agent, and alternatives
  while Orchestrator remains the routing owner.
- AAA and Context remain consumers or policy surfaces; neither takes over
  Orchestrator runtime decisions.

## Telemetry Contract

Implemented surfaces:

- HTTP: `GET /loops/{loop_id}/telemetry`
- CLI: `across-orchestrator loop-telemetry <loop-id> --json`
- MCP: `get_agent_loop_telemetry`

Telemetry responses use schema `agent-loop-telemetry/1.0` and include compact
summary data, bounded metrics, latest event sequence, and budget state. Metrics
cover terminal status, cancellation category, duration, recovery, routing,
memory-candidate, and budget outcomes where those signals are available.

Telemetry must not include prompts, raw observations, raw memory text, stack
traces, provider keys, local absolute paths, or hidden reasoning. Hosts should
treat unknown metric names as forward-compatible extensions.

## Event Resume Contract

Implemented surfaces:

- HTTP snapshot: `GET /loops/{loop_id}/events?after_sequence=N`
- HTTP stream: `GET /loops/{loop_id}/events/stream?follow=true&after_sequence=N`
- CLI: `across-orchestrator loop-events <loop-id> --after-sequence N --json`
- MCP: `get_agent_loop_events` accepts `afterSequence`

Rules:

- `sequence` is the primary resume cursor.
- `event_id` remains the audit and deduplication id.
- Hosts should fetch a snapshot first, then follow from the highest observed
  sequence.
- If `after_sequence` is beyond the current tail, the snapshot returns no
  historical events and a follow stream waits for future events.
- Terminal loops close the stream after all available events are delivered.

## Budget And Concurrency Contract

Hosts may attach loop metadata under `agentLoopBudget`, `agent_loop_budget`, or
`budget`.

Supported fields:

- `maxConcurrentLoops` or `max_concurrent_loops`
- `maxTurnsPerLoop`, `max_turns_per_loop`, `maxTurns`, or `max_turns`
- `maxRuntimeSeconds` or `max_runtime_seconds`

Rules:

- Excess active loops are rejected at start with HTTP `409` and a structured
  payload containing active and maximum loop counts.
- Turn or runtime exhaustion stops the loop with `cancel_category:
  budget_exceeded`.
- Budget state is exposed through health, telemetry, evidence summaries, and
  host release evidence.
- Existing loops without budget metadata continue to use runtime defaults.

`budget_exceeded` is a stable structured cancellation category and a
release-blocking runtime condition.

## Routing Evidence Contract

Routing evidence uses schema `agent-loop-routing/1.0`.

```json
{
  "schema_version": "agent-loop-routing/1.0",
  "loop_id": "loop-...",
  "step_id": "step-...",
  "base_agent": "local",
  "selected_agent": "browser-specialist",
  "source": "capability_hint",
  "reason": "requires browser_e2e capability",
  "alternatives": [
    {"agent_id": "local", "selected": false, "reason": "missing browser_e2e"}
  ]
}
```

Rules:

- Orchestrator owns routing decisions and evidence.
- Hosts display routing evidence and explicit user controls.
- Across Context may store safe routing ids in memory candidates, but it does
  not select agents or approve handoffs.
- Unknown routing sources and alternative fields must remain displayable by old
  hosts.

## Completion Boundary

The current Agent Loop runtime contract is complete for release-quality host
integration:

- durable event audit metadata
- structured cancellation policy
- health summaries
- compact evidence summaries and host release evidence
- live event streaming plus resume cursors
- bounded telemetry
- recovery policy
- host capability-hint routing
- structured routing evidence
- structured memory write candidates
- budget and concurrency enforcement

Not included in this contract:

- full multi-agent task-decomposition product UX
- long-horizon analytics dashboards
- cryptographic evidence trust chains
- autonomous ecosystem workflow planning or release automation

Those items require separate product specs because they change user experience,
trust, or operating policy beyond the Agent Loop runtime contract.
