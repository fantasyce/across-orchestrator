# Across Orchestrator Ecosystem Plugin Runtime

Date: 2026-06-09

Across Orchestrator is the Across ecosystem task runtime plugin. It must work as
a standalone product and as a plugin mounted by Across Agents Assistant or by a
third-party agent hosting platform.

## Runtime Shape

The runtime core owns task lifecycle, subtasks, waves, contracts, events,
evidence bundles, quality gates, and remediation policy. The protocol adapters
are wrappers over that same core:

- sidecar HTTP/SSE: preferred host integration for long-running tasks;
- MCP stdio: tool-style integration for MCP-capable agents;
- CLI: diagnostics, scripted operation, and conformance checks;
- Python SDK: in-process integration for Python containers.

All adapters use one data home:

```text
~/.across/data/across-orchestrator
```

The `ACROSS_ORCHESTRATOR_HOME` environment variable remains an explicit override
for tests, containers, and managed deployments.

## Sidecar-First Default

Host applications should prefer:

```bash
across-orchestrator serve --host 127.0.0.1 --port <host-selected-port>
```

The sidecar writes runtime metadata under:

```text
~/.across/run/across-orchestrator
```

The host owns process lifecycle and may restart the sidecar after app restart.
The sidecar owns durable task state, so restarting the host must not copy or
recreate task records inside the host app.

## Plugin Installation

The default Across plugin install root is:

```text
~/.across/plugins/across-orchestrator
```

The stable wrapper command is:

```text
~/.across/bin/across-orchestrator
```

The plugin manifest lives at:

```text
~/.across/plugins/across-orchestrator/manifest.json
```

The manifest declares CLI, sidecar, MCP, and SDK entrypoints plus the data,
config, run, logs, and cache paths used by the plugin.

## Host Boundary

Across Orchestrator must not own:

- provider credentials;
- macOS permissions;
- user approval prompts;
- local agent installation;
- billing, tenancy, auth, or quotas in hosting platforms.

The host injects scoped agent, tool, model, and approval behavior through host
adapters or through sidecar request context.

## Compatibility

Legacy `~/.across-orchestrator` state is copied into the new data home when the
new data home is empty. The legacy directory is not deleted automatically.

The temporary `across_agents_assistant` compatibility namespace remains only to
keep parity tests passing while public APIs stabilize. New host integrations
should use `across_orchestrator` APIs, HTTP, MCP, or CLI.
