# Across Orchestrator Parity Audit

This document prevents overclaiming and records the boundary between the
transplanted mature engine and host-owned execution concerns.

## Summary

`v0.2.0` changes the product strategy from "clean-room alpha skeleton" to
"mature core extraction." The repository now carries the task orchestration
modules that were already stabilized inside Across Agents Assistant and runs the
original focused app tests unchanged.

Validated:

- App-side orchestration baseline: `395 passed`.
- Plugin-side imported parity tests: `395 passed`.
- Full plugin test suite: `409 passed`.
- App-grade Release E2E product path writes exactly the seven required files and
  runs mature contract acceptance.
- With Node Playwright available, app-grade quality gates pass for artifact
  integrity, workspace hygiene, security/privacy, agent mix, static web, browser
  E2E, API service, and CLI generic.

Still host-owned:

- real local/cloud agent processes
- provider credentials
- macOS approval prompts
- app UI state and task console integration
- long-lived app persistence migration

## Capability Matrix

| Capability | Across Agents Assistant Runtime | Across Orchestrator v0.2.0 | Parity Status |
| --- | --- | --- | --- |
| Standalone repository | No, embedded in app | Yes | Ahead |
| CLI | Internal app APIs only | Demo and app-grade release commands | Ahead |
| HTTP task API | Yes, app backend | Demo and app-grade endpoints | Partial |
| SSE task events | Yes, app task stream | Event replay stream | Partial |
| MCP tools | App exposes many tools | Task tools plus release E2E task | Partial |
| A2A-style Agent Card | App has internal agent cards | Product Agent Card | Partial |
| Persistent task state | SQLite/app persistence | JSON task files plus embedded app-grade evidence | Partial |
| Append-only event log | App audit/task records | JSONL events | Partial |
| Owner-agent decomposition | Mature `OwnerAgent` | Transplanted and parity-tested | Core parity |
| Task DAG and waves | Mature waves/dependencies | Transplanted and parity-tested | Core parity |
| Wave governance | Approval, blocked, revalidating, failed states | Transplanted and parity-tested | Core parity |
| A2A contract negotiation | Task/subtask/wave contracts | Transplanted contract modules | Core parity |
| Subtask writable scope | `allowed_writable_files` | Transplanted dispatcher rules | Core parity |
| Local/cloud agent dispatch | App-owned local CLI + LLM gateway | Host adapter boundary; no credentials | Needs app adapter |
| Tool approval path | App permission UI | Host-owned by design | Needs app adapter |
| Acceptance parsing | Level 1/Level 2, parse retry, owner decisions | Transplanted and parity-tested | Core parity |
| Remediation loops | Subtask, wave, prior-wave, integration, quality | Transplanted and parity-tested | Core parity |
| Remediation budgets | Max fix rounds and quality attempts | Transplanted and parity-tested | Core parity |
| Workspace hygiene | Noise filtering and exact artifact scope | Transplanted and app-grade tested | Parity |
| Quality gates | Static web, browser, API, CLI, security/privacy, agent mix | Transplanted and app-grade tested | Parity when browser deps exist |
| Release E2E | Fixed complex scenario | `submit-release-e2e` / `/release-e2e` / MCP | Parity for deterministic reference scenario |
| Evidence bundle | Rich app-compatible bundle and benchmark | App-grade evidence embedded in product bundle | Partial mapping |
| Release evaluation | Readiness summary and RC verification | Transplanted and parity-tested | Core parity |
| Restart recovery | App task restore/repair paths | Basic JSON reload; app persistence adapter pending | Gap |
| Swift task UI integration | Mature app UI | Documented integration path | Gap |

## Source Relationship

The following mature app modules are now copied into the standalone repository
and covered by imported tests:

- `TaskOrchestrator`
- `OwnerAgent`
- `TaskState`
- `TaskDispatcher`
- `delivery_contract`
- `contract_acceptance`
- `project_acceptance`
- `quality_gates`
- `quality_benchmark`
- `release_e2e`
- `release_evaluation`

The compatibility namespace exists so original tests can run unchanged. Public
host code should prefer `across_orchestrator.engine.MatureOrchestrationEngine`.

## App-Grade Release E2E

The app-grade scenario is available through:

- CLI: `submit-release-e2e`
- HTTP: `POST /release-e2e`
- MCP: `submit_release_e2e_task`

It produces exactly:

- `README.md`
- `web/index.html`
- `web/styles.css`
- `web/app.js`
- `api/server.mjs`
- `cli/quality-check.mjs`
- `tests/e2e-smoke.mjs`

It then runs the mature acceptance stack. When Node Playwright is installed, the
browser E2E gate passes as part of the quality report. Without Playwright, the
report records the browser gate as environment-blocked and delivery quality as
partial.

## Remaining Gates Before The App Defaults To External Plugin

1. Wire Across Agents Assistant to `MatureOrchestrationEngine` with real app
   dispatcher, validator, owner-agent, approval, and persistence adapters.
2. Run the fixed Release E2E through the app UI while the active implementation
   mode is `embedded_plugin` or `external`.
3. Verify restart recovery with app persistence and plugin state reload.
4. Verify user-visible fallback from plugin mode to built-in compatibility mode.
5. Map plugin evidence into the app's Release Evidence Center without losing
   quality gate, remediation, or agent-mix detail.

## Current Quality Claim

`v0.2.0` is no longer just an alpha foundation. It is a tested extraction of the
Across Agents Assistant mature orchestration core, with public plugin surfaces
and an app-grade Release E2E path.

It should still be integrated into Across Agents Assistant behind an
implementation-mode switch until real host adapters and UI-level release E2E
complete.
