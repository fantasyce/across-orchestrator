# Agent Loop Runtime RFCs

This document records the runtime-owned Agent Loop work that remains after the
Across Agents Assistant `v0.8.28` host-side closeout.

These are specification gates. Do not implement runtime behavior from this file
until the corresponding RFC section is accepted and tests are planned.

## RFC 1: Telemetry Schema

Owner: Across Orchestrator.

Consumers: Across Agents Assistant, release verification, future ecosystem
automation.

### Goals

- Measure loop duration distributions by task class.
- Count terminal outcomes by status and `cancel_category`.
- Count stream fallback and reconnect behavior without raw transcripts.
- Count capability mismatch, recovery decisions, and recovery outcomes.
- Count memory candidate produced/accepted/rejected rates without storing memory
  text in telemetry.

### Proposed Event Envelope

```json
{
  "schema_version": "agent-loop-telemetry/1.0",
  "loop_id": "loop-...",
  "event_id": "evt-...",
  "sequence": 12,
  "correlation_id": "corr-...",
  "metric": "loop.duration_ms",
  "value": 1234,
  "unit": "ms",
  "dimensions": {
    "task_class": "release_e2e",
    "terminal_status": "completed",
    "cancel_category": null,
    "recovery_action": null,
    "selected_agent": "local"
  },
  "observed_at": "2026-06-20T00:00:00Z"
}
```

### Privacy Rules

- Never include prompts, raw memory text, stack traces, local absolute paths, or
  provider keys.
- Use counts, categories, and stable ids only.
- Redact agent names if a host marks them private.

### Acceptance Tests

- Unit test that telemetry excludes raw observation payloads.
- Unit test that failed, cancelled, recovered, and completed loops emit bounded
  metrics.
- MCP/HTTP schema test that unknown metrics remain forward compatible.

## RFC 2: Stream Resume Protocol

Owner: Across Orchestrator.

Consumers: hosts that render `events/stream?follow=true`.

### Goals

- Let hosts recover after app sleep, network interruption, or process restart.
- Preserve the existing finite snapshot default.
- Keep resume semantics deterministic across HTTP, CLI, and MCP surfaces.

### Proposed API

```text
GET /loops/{loop_id}/events/stream?follow=true&after_sequence=42
GET /loops/{loop_id}/events?after_sequence=42
```

Rules:

- `sequence` is the primary resume cursor.
- `event_id` is retained for audit and deduplication.
- Hosts should fetch a snapshot first, then follow from the highest observed
  sequence.
- If `after_sequence` is older than retained events, return a snapshot with a
  `resume_reset` marker.
- If `after_sequence` is beyond the current tail, return no historical events
  and continue following new ones.

### Acceptance Tests

- Snapshot then follow does not duplicate events.
- Reconnect after a known sequence returns only later events.
- Reconnect with a stale sequence emits a reset marker.
- Terminal loops close the stream after all events are delivered.

## RFC 3: Cost And Concurrency Policy

Owner: Across Orchestrator policy, with product input from host apps.

### Goals

- Prevent runaway loops.
- Make budgets visible before work starts.
- Keep cancellation categories compatible with release-blocking health.

### Proposed Policy Fields

```json
{
  "schema_version": "agent-loop-budget/1.0",
  "max_concurrent_loops": 2,
  "max_turns_per_loop": 12,
  "max_runtime_seconds": 1800,
  "max_recovery_attempts": 2,
  "timeout_cancel_category": "timeout_cancelled",
  "budget_exceeded_category": "budget_exceeded"
}
```

Rules:

- Defaults must be conservative and host-overridable.
- Budget exhaustion is a structured cancellation category, not a free-form
  failure string.
- Hosts can display budgets but should not enforce hidden local limits unless
  Orchestrator exposes the policy.

### Acceptance Tests

- Concurrent loop limit rejects excess starts with a stable status.
- Max turns transitions the loop to a terminal category.
- Timeout emits structured cancellation and release evidence.
- Existing loops without budget metadata continue using defaults.

## RFC 4: Multi-Agent Routing And UX Contract

Owner: Across Orchestrator for routing; host products for UX.

### Goals

- Define whether multi-agent behavior is automatic routing, explicit handoff,
  or task decomposition.
- Keep host UI thin: display routing evidence and expose approved controls.
- Avoid Context taking ownership of task scheduling.

### Proposed Routing Evidence

```json
{
  "schema_version": "agent-loop-routing/1.0",
  "loop_id": "loop-...",
  "step_id": "step-...",
  "base_agent": "hermes",
  "selected_agent": "openclaw",
  "source": "capability_hint",
  "reason": "requires browser_e2e capability",
  "alternatives": [
    {"agent": "hermes", "reason": "missing browser_e2e"}
  ]
}
```

### Scope Boundaries

- Orchestrator owns routing decisions and evidence.
- AAA owns display and explicit user controls.
- Context owns memory policy and pending review only.
- Plugin manifests declare capabilities, not runtime decisions.

### Acceptance Tests

- Routing evidence is present when a non-base agent is selected.
- Unknown routing sources remain displayable by old hosts.
- Rejected or cancelled handoffs preserve terminal state and evidence.
- Memory candidate summaries record routing ids, not raw handoff transcripts.

## Automation Integration

The AAA ecosystem review workflow may create issues referencing these RFCs.
It must not auto-merge runtime changes for any RFC above. Runtime changes require
an accepted RFC, tests, and a release plan.
