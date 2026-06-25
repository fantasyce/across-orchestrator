# AGENTS.md

## Project Overview

Across Orchestrator is the durable execution runtime for Across. It owns task
lifecycle, delivery contracts, dependency waves, Agent Loop checkpoints,
quality gates, event streams, and evidence bundles.

Orchestrator does not own host UI, credentials, global memory policy, or
Autopilot-specific business strategy.

## Setup And Checks

```bash
python3 -m pip install -e '.[dev]'
npm install
bash scripts/check.sh
npm pack --dry-run --json
```

Useful CLI smoke checks:

```bash
PYTHONPATH=src python3 -m across_orchestrator.cli --help
PYTHONPATH=src python3 -m across_orchestrator.cli agent-card --json
PYTHONPATH=src python3 -m across_orchestrator.cli mcp
```

## Product Packaging Rules

- Present Orchestrator as durable execution, not as the whole Across product.
- Explain that Autopilot supervises loops and delegates execution here.
- Explain that Context owns memory and policy.
- Explain that AAA or another host owns UI, credentials, approvals, and model
  decisions.
- Keep product runtime paths under `~/.across`.

## Boundary Rules

- Do not store host credentials.
- Do not own global memory policy.
- Do not grant merge, release, signing, or production authority by default.
- Evidence should be durable and reviewable without raw secrets.

## Important Files

- `src/across_orchestrator/agent_loop.py`: Agent Loop runtime
- `src/across_orchestrator/runtime.py`: task runtime helpers
- `src/across_orchestrator/mcp.py`: MCP server surface
- `src/across_orchestrator/plugin_manifest.py`: plugin contract metadata
- `tests/`: Python runtime tests
