# Across Orchestrator Parity Audit

This document prevents overclaiming. Across Orchestrator `v0.1.0` is an alpha
foundation, not a replacement for the mature task orchestration runtime inside
Across Agents Assistant.

## Summary

The current product is useful because it proves:

- the task orchestration runtime can exist outside the app
- CLI, HTTP/SSE, A2A-style Agent Card, and MCP stdio can all address the same
  local runtime
- task state, events, evidence, and quality can be stored without app-private
  database dependencies

It does not yet prove:

- complex multi-agent delivery quality
- release-grade acceptance and remediation
- parity with the app's owner-agent, wave, contract, and repair loops

## Capability Matrix

| Capability | Across Agents Assistant Runtime | Across Orchestrator v0.1.0 | Parity Status |
| --- | --- | --- | --- |
| Standalone repository | No, embedded in app | Yes | Ahead |
| CLI | Internal app APIs only | `across-orchestrator` CLI | Ahead |
| HTTP task API | Yes, app backend | Yes, stdlib HTTP | Partial |
| SSE task events | Yes, app task stream | Static event replay stream | Partial |
| MCP tools | App exposes many tools; task runtime not standalone MCP product | Basic MCP task tools | Partial |
| A2A-style Agent Card | App has internal agent cards | Product Agent Card | Partial |
| Persistent task state | SQLite/app persistence with recovery | JSON task files | Partial |
| Append-only event log | App audit/task records | JSONL events | Partial |
| Owner-agent decomposition | Mature `OwnerAgent` LLM-driven decomposition | Missing | Gap |
| Task DAG and waves | Mature waves, dependencies, wave gate | Deterministic deliverable subtasks only | Gap |
| Wave governance | Approval, blocked, revalidating, failed states | Missing | Gap |
| A2A contract negotiation | App has task/subtask/wave contracts and acceptance records | Simple required-artifact contract | Gap |
| Subtask writable scope | `allowed_writable_files` and contract-derived scope | Missing | Gap |
| Local/cloud agent dispatch | Local CLI + cloud LLM gateway + tool approval | Demo adapter and command adapter only | Gap |
| Tool approval path | Integrated with app permissions | Missing | Gap |
| Acceptance parsing | Level 1/Level 2, parse retry, owner decisions | Missing | Gap |
| Remediation loops | Subtask, wave, prior-wave, integration, quality remediation | Missing | Gap |
| Remediation budgets | Max fix rounds and quality attempts | Missing | Gap |
| Workspace hygiene | Noise filtering and exact artifact scope | Minimal path normalization | Gap |
| Quality gates | Static web, browser, API, CLI, security/privacy, agent mix | Required files present only | Gap |
| Release E2E | Fixed complex release scenario | Missing | Gap |
| Evidence bundle | Rich app-compatible bundle and benchmark | Simple bundle | Gap |
| Release evaluation | Readiness summary and RC verification | Missing | Gap |
| Restart recovery | App task restore/repair paths | Basic state reload only | Gap |
| Swift task UI integration | Mature app UI | Not integrated | Gap |

## Source Relationship

Across Orchestrator `v0.1.0` was not a direct code transplant from Across
Agents Assistant. It was built as a clean, independent runtime skeleton using
the app's proven concepts:

- task lifecycle
- explicit deliverables
- contracts
- events
- evidence bundle
- quality benchmark
- plugin-first host boundary

The mature logic that took longest to stabilize in the app still needs a
careful migration or reimplementation:

- `TaskOrchestrator`
- `OwnerAgent`
- `TaskState`
- `TaskDispatcher`
- `delivery_contract`
- `contract_acceptance`
- `project_acceptance`
- `quality_gates`
- `release_e2e`
- `release_evaluation`

## Required Parity Gates

Across Orchestrator should not replace the app runtime until these pass:

1. Submit the existing fixed complex Release E2E scenario through external
   Across Orchestrator.
2. Decompose into owner, wave, and subtask contracts with exact artifact scope.
3. Execute at least three non-owner agents, including local and cloud agents,
   through host-provided adapters.
4. Enforce writable-file scope per subtask.
5. Produce all seven release artifacts exactly.
6. Run static web, browser, API service, CLI generic, security/privacy,
   workspace hygiene, and agent-mix gates.
7. Trigger targeted remediation on an injected failure.
8. Pass after remediation and export an app-compatible evidence bundle.
9. Survive runtime restart without losing task/event/evidence state.
10. Let Across Agents Assistant display external task state without using the
    built-in runtime.

## Next Implementation Milestones

### Milestone 1: Model Parity

Port or faithfully reimplement app-grade task models:

- `Task`
- `SubTask`
- `Wave`
- `TaskContract`
- `AcceptanceRecord`
- `Artifact`
- `ValidationReport`

### Milestone 2: Contract Engine

Bring over explicit delivery contracts, requirement manifests, accepted
artifact records, writable scopes, and aggregation rules.

### Milestone 3: Agent Adapter Boundary

Define a host-provided adapter protocol that can execute real local/cloud
agents without moving model keys or macOS permissions into Across Orchestrator.

### Milestone 4: Governance And Remediation

Port wave gate, acceptance parsing, repair planning, remediation budgets, and
dead-end handling.

### Milestone 5: Release E2E Parity

Run the same release-grade task used by Across Agents Assistant and require
matching quality evidence before enabling external mode by default in the app.

## Current Quality Claim

The current quality claim is deliberately narrow:

> Across Orchestrator `v0.1.0` is a tested alpha foundation for an independent
> orchestration product. It is not yet a production-quality replacement for the
> Across Agents Assistant orchestration runtime.
