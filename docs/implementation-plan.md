# Across Orchestrator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone Across Orchestrator repository that can submit,
run, inspect, and verify delivery tasks independently from Across Agents
Assistant.

**Architecture:** A dependency-light Python package owns task lifecycle,
contracts, events, evidence, CLI, HTTP, and MCP surfaces. The runtime ships with
a deterministic demo agent for tests and a command-adapter contract for future
host-provided agents.

**Tech Stack:** Python 3.11+, stdlib `argparse`, `http.server`, `json`,
`unittest`, append-only JSONL event storage, manual MCP JSON-RPC stdio.

---

### Task 1: Repository And Test Harness

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `LICENSE`
- Create: `scripts/check.sh`
- Create: `src/across_orchestrator/__init__.py`
- Create: `tests/test_runtime.py`

- [ ] Write the first failing runtime test for submitting a task with two
  deliverables.
- [ ] Run `python -m unittest tests.test_runtime -v` and verify it fails because
  `across_orchestrator.runtime` does not exist.
- [ ] Add package metadata and minimal module files.
- [ ] Re-run the test and continue to Task 2.

### Task 2: Core Runtime

**Files:**
- Create: `src/across_orchestrator/models.py`
- Create: `src/across_orchestrator/store.py`
- Create: `src/across_orchestrator/runtime.py`
- Modify: `tests/test_runtime.py`

- [ ] Implement task, subtask, contract, artifact, and event dataclasses.
- [ ] Implement local JSON store rooted at `ACROSS_ORCHESTRATOR_HOME`.
- [ ] Implement `OrchestratorRuntime.submit_task()`.
- [ ] Verify task submission persists state and emits `task.created`,
  `subtask.created`, and `contract.created` events.

### Task 3: Execution And Evidence

**Files:**
- Create: `src/across_orchestrator/adapters.py`
- Create: `src/across_orchestrator/evidence.py`
- Modify: `src/across_orchestrator/runtime.py`
- Modify: `tests/test_runtime.py`

- [ ] Add a deterministic demo adapter that writes requested deliverables under
  `projectRoot`.
- [ ] Implement `run_task()` for pending subtasks.
- [ ] Implement evidence bundle and quality benchmark generation.
- [ ] Verify an end-to-end run produces files, artifact hashes, passing
  quality, and append-only events.

### Task 4: CLI

**Files:**
- Create: `src/across_orchestrator/cli.py`
- Create: `tests/test_cli.py`

- [ ] Add `init`, `submit`, `run`, `status`, `events`, `evidence`, `quality`,
  and `agent-card` commands.
- [ ] Verify CLI commands work in a temp state directory and project directory.
- [ ] Add `across-orchestrator` console script metadata.

### Task 5: HTTP And A2A Card

**Files:**
- Create: `src/across_orchestrator/agent_card.py`
- Create: `src/across_orchestrator/server.py`
- Create: `tests/test_http.py`
- Create: `tests/test_agent_card.py`

- [ ] Implement an A2A-style Agent Card with protocol metadata.
- [ ] Implement stdlib HTTP endpoints for health, task submit/run/status,
  events, evidence, quality, and event stream.
- [ ] Verify the server can run in a subprocess and complete a task through
  HTTP only.

### Task 6: MCP Server

**Files:**
- Create: `src/across_orchestrator/mcp.py`
- Create: `tests/test_mcp.py`

- [ ] Implement minimal JSON-RPC stdio handling for `initialize`,
  `tools/list`, and `tools/call`.
- [ ] Expose `submit_task`, `run_task`, `get_task`, `get_evidence_bundle`, and
  `get_agent_card`.
- [ ] Verify an MCP client can submit and run a task.

### Task 7: Documentation And Product Checks

**Files:**
- Modify: `README.md`
- Create: `examples/demo-task.json`
- Modify: `scripts/check.sh`

- [ ] Document install, quick start, CLI, HTTP, MCP, Agent Card, host
  integration, and scope boundaries.
- [ ] Add a demo task fixture.
- [ ] Run `bash scripts/check.sh`.
- [ ] Commit the repository when all tests pass.
