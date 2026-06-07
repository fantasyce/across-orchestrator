# App Parity Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade Across Orchestrator from an alpha runtime into a standalone plugin that carries the mature task orchestration capability already proven inside Across Agents Assistant.

**Architecture:** Treat Across Agents Assistant as the reference implementation. First migrate its task-manager tests as parity gates, then transplant the mature task-manager core and replace app-private services with narrow host adapters. Keep the existing CLI, HTTP, MCP, and Agent Card surfaces as product wrappers around the parity engine.

**Tech Stack:** Python 3.11+, pytest/unittest, the existing Across Agents Assistant task-manager modules, local JSON state for standalone mode, host adapter interfaces for local/cloud agents, and stdlib CLI/HTTP/MCP wrappers.

---

### Task 1: Reference Baseline

**Files:**
- Read: `<across-agents-assistant>/backend/tests/test_task_orchestrator.py`
- Read: `<across-agents-assistant>/backend/tests/test_owner_agent.py`
- Read: `<across-agents-assistant>/backend/tests/test_release_e2e.py`
- Modify: `<across-orchestrator>/docs/parity-audit.md`

- [x] Run the app-side orchestration baseline:

```bash
cd <across-agents-assistant>
PYTHONPATH=backend/src backend/.venv/bin/pytest \
  backend/tests/test_task_orchestrator.py \
  backend/tests/test_dispatcher.py \
  backend/tests/test_owner_agent.py \
  backend/tests/test_delivery_contract.py \
  backend/tests/test_contract_acceptance.py \
  backend/tests/test_contract_aggregation.py \
  backend/tests/test_contract_sanitization.py \
  backend/tests/test_contract_validator.py \
  backend/tests/test_delivery_report.py \
  backend/tests/test_quality_benchmark.py \
  backend/tests/test_quality_gates.py \
  backend/tests/test_release_e2e.py \
  backend/tests/test_release_evaluation.py \
  -q
```

Expected: `395 passed`, proving the app runtime is the migration source.

- [x] Update the parity audit so the reference baseline and migration rule are explicit.

### Task 2: Parity Test Import Gate

**Files:**
- Create: `<across-orchestrator>/tests/parity/`
- Copy: app task-manager tests into `<across-orchestrator>/tests/parity/`
- Modify: `<across-orchestrator>/pyproject.toml`

- [x] Copy the focused app orchestration tests into `tests/parity/`.
- [x] Run:

```bash
cd <across-orchestrator>
PYTHONPATH=src python3 -m pytest tests/parity/test_delivery_contract.py -q
```

Expected red: import failure for `across_agents_assistant`, showing the standalone repo does not yet expose the mature core.

### Task 3: Mature Core Transplant

**Files:**
- Create: `<across-orchestrator>/src/across_agents_assistant/task_manager/`
- Create: `<across-orchestrator>/src/across_agents_assistant/agent_ids.py`
- Create: `<across-orchestrator>/src/across_agents_assistant/workspace_hygiene.py`
- Create: `<across-orchestrator>/src/across_agents_assistant/llm_gateway/`
- Create: `<across-orchestrator>/src/across_agents_assistant/native_agent_skills.py`

- [x] Copy the mature app `task_manager` package and low-risk support modules.
- [x] Keep imports app-compatible for parity tests, then wrap them through `across_orchestrator`.
- [x] Run focused contract, owner, release, quality, and state tests until all imported core behavior passes.

### Task 4: Host Adapter Boundary

**Files:**
- Create: `<across-orchestrator>/src/across_orchestrator/host_adapters.py`
- Modify: `<across-orchestrator>/src/across_orchestrator/runtime.py`
- Modify: `<across-orchestrator>/src/across_orchestrator/cli.py`
- Test: `<across-orchestrator>/tests/test_runtime.py`

- [x] Add a host adapter protocol for local CLI agents, cloud LLM providers, approvals, and tool execution.
- [x] Provide deterministic standalone adapters for tests.
- [x] Preserve external host injection so Across Agents Assistant can use the plugin without moving credentials or app UI concerns into it.

### Task 5: Product Surface Parity

**Files:**
- Modify: `<across-orchestrator>/src/across_orchestrator/runtime.py`
- Modify: `<across-orchestrator>/src/across_orchestrator/server.py`
- Modify: `<across-orchestrator>/src/across_orchestrator/mcp.py`
- Modify: `<across-orchestrator>/tests/test_http.py`
- Modify: `<across-orchestrator>/tests/test_mcp.py`

- [x] Route CLI, HTTP, and MCP task execution through the parity engine when task context requests app-grade delivery.
- [x] Keep simple demo tasks working for quick-start users.
- [x] Expose evidence bundles that include release quality gates and remediation records.

### Task 6: Release-Grade E2E

**Files:**
- Create: `<across-orchestrator>/tests/e2e/test_release_delivery_parity.py`
- Modify: `<across-orchestrator>/scripts/check.sh`
- Modify: `<across-orchestrator>/docs/parity-audit.md`

- [x] Run a standalone complex release task with the exact seven-file manifest from Across Agents Assistant.
- [x] Verify static web, API, CLI, browser E2E, security/privacy, workspace hygiene, agent mix, and artifact integrity gates.
- [x] Inject a dirty workspace failure and verify app-grade repair returns to the exact manifest.
- [x] Update docs only after the E2E evidence passes.

### Task 7: Release Decision

**Files:**
- Modify: `<across-orchestrator>/README.md`
- Modify: `<across-orchestrator>/docs/product-architecture.md`
- Modify: `<across-orchestrator>/docs/across-agents-assistant-integration.md`

- [x] Run `bash scripts/check.sh`.
- [x] Run imported parity tests.
- [x] Run standalone release E2E.
- [x] If all pass, mark the release as plugin-parity capable and create the next release. If any gate fails, keep the release prerelease and document the blocker.
