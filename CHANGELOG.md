# Changelog

## 0.12.2 - 2026-08-30

- Preserve ordinary Task identifiers such as `task-...` across generic CLI and public-value redaction while continuing to remove standalone credential-shaped values.

## 0.12.1 - 2026-08-30

- Preserve ordinary `task-...` protocol identities during public secret redaction so receipt-bound Worker evidence remains hash-verifiable while real token-shaped values are still removed.

## 0.12.0 - 2026-08-30

- Added explicit, deterministic Goal revalidation `plan`, durable and
  idempotent `start`, and receipt-bound `complete` phases.
- Added host-validation and Worker-reexecution modes with criterion, Goal,
  Task, revision, input, and evidence authority preserved end to end.
- Added fail-closed provider contracts that reject authority drift without
  leaving partial revalidation Attempts or Jobs.
