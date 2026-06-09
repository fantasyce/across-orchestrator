# Across Orchestrator As A Hosting Platform Plugin

This note records the current product conclusion for using Across Orchestrator
inside an agent hosting platform. It is intentionally written from the
platform-provider perspective, not from the Across Agents Assistant desktop-app
perspective.

## Scenario

The target platform is an agent hosting platform:

- enterprise users register one or more agent containers on the platform;
- a user's local or upstream agent calls those registered containers through an
  agent-to-agent protocol such as A2A;
- the platform owns registration, routing, authentication, tenant isolation,
  container lifecycle, observability, quotas, and policy;
- each registered container is the externally visible remote agent.

In this architecture, the hosting platform is not one monolithic agent. The
platform is a provider, registry, gateway, and container runtime. Each registered
agent container is the A2A-facing agent.

```text
Upstream/local agent
  -> hosting platform gateway
     -> registered agent container
        -> optional Across Orchestrator runtime
           -> worker/model/tool adapters inside the container
```

## Boundary

The platform should expose stable external A2A surfaces for each registered
agent container:

- agent discovery and Agent Card publication;
- message and task endpoints;
- authentication and tenant routing;
- streaming task events;
- artifact delivery;
- audit logs and policy enforcement.

Across Orchestrator should not be exposed as the platform's raw external API.
It should sit behind an agent container or behind the platform runtime as a
task-orchestration component.

Across Orchestrator owns:

- task lifecycle;
- subtasks and waves;
- delivery contracts;
- acceptance checks;
- quality gates;
- remediation policy;
- events;
- evidence bundles;
- task-quality benchmarks.

The platform or container owns:

- A2A gateway compatibility;
- public endpoint shape;
- auth, tenancy, billing, quotas, and audit;
- container lifecycle;
- model credentials;
- tool permissions;
- workspace and filesystem policy;
- network policy;
- user approvals;
- actual worker/model/tool execution.

## Installation Shapes

Across Orchestrator can become a portable runtime for arbitrary agent hosting
platforms if it supports several installation modes.

### 1. Sidecar Runtime

Run Across Orchestrator next to each agent container.

```text
pod / sandbox / container group
  agent-container
  across-orchestrator-sidecar
  shared workspace volume
  shared orchestrator state volume
```

Example command:

```bash
export ACROSS_ORCHESTRATOR_HOME=/var/lib/across-orchestrator
across-orchestrator serve --host 127.0.0.1 --port 8765
```

The container talks to the sidecar over loopback HTTP or a local Unix socket.
This is the best default for container hosting platforms because it keeps the
orchestrator isolated while making it easy to mount platform-managed volumes and
apply resource limits.

### 2. Embedded SDK Runtime

Install the package into the agent container and embed the engine directly:

```bash
pip install across-orchestrator
```

The container provides host adapters:

- `DispatcherAdapter`
- `ValidatorAdapter`
- `OwnerAgentAdapter`

Then it uses:

```python
from across_orchestrator.engine import MatureOrchestrationEngine
```

This is suitable when the container is Python-based and wants direct in-process
control.

### 3. MCP Runtime

Expose orchestration tools to MCP-capable agent runtimes:

```bash
across-orchestrator mcp
```

This is useful when the host agent already has an MCP client and wants to call
orchestration as tools rather than as a sidecar HTTP API.

### 4. Platform-Managed Shared Runtime

A platform can also run a shared Across Orchestrator service per tenant or per
workspace. This can reduce overhead, but it requires stronger tenant isolation,
explicit task ownership, per-container capability mapping, and careful
workspace scoping. Sidecar mode is safer as the first general-purpose target.

## Current v0.3.1 Readiness

Across Orchestrator v0.3.1 is a good independent-runtime foundation, but it is
not yet a complete "install into any hosting platform" plugin standard.

Already in place:

- Python package with console scripts: `across-orchestrator` and
  `across-tasks`;
- CLI, HTTP/SSE, and MCP surfaces;
- A2A-style Agent Card;
- plugin manifest for CLI, sidecar, MCP, SDK, and filesystem namespaces;
- `~/.across/data/across-orchestrator` as the default state location;
- `ACROSS_ORCHESTRATOR_HOME` for state location override;
- host adapter protocols for dispatcher, validator, and owner-agent behavior;
- task state, contracts, waves, remediation, quality gates, evidence bundles;
- parity tests transplanted from Across Agents Assistant;
- no ownership of platform credentials or user approval state.

Still missing for broad platform portability:

- OCI image and published container tags;
- Helm/Kubernetes sidecar examples;
- plugin manifest that declares protocol versions, ports, volumes, permissions,
  resource needs, and adapter requirements;
- signed release artifacts, checksums, and supply-chain metadata;
- strict A2A server compatibility rather than only A2A-style metadata;
- typed, versioned HTTP schemas;
- cross-language adapter protocol;
- tenant/workspace isolation model;
- platform capability negotiation;
- lifecycle hooks for install, health, upgrade, drain, backup, and uninstall;
- conformance test suite for third-party hosting platforms;
- removal or hiding of the temporary `across_agents_assistant` compatibility
  namespace behind stable `across_orchestrator` APIs.

## General Plugin Design Principles

The research direction is consistent across A2A, MCP, container sidecars, and
large plugin systems:

1. Capability discovery must be explicit.
   Agent Card, MCP tool lists, and plugin manifests all make capabilities
   discoverable before execution.

2. The host controls trust and permissions.
   Plugins should not silently own credentials, broad filesystem access, or user
   approvals. The hosting platform injects scoped permissions.

3. The runtime should be isolated.
   A separate process or sidecar protects the host and the agent container from
   crashes and narrows the blast radius of upgrades.

4. Protocols and schemas must be versioned.
   A platform needs to know which orchestrator version, API schema, A2A profile,
   MCP protocol version, and evidence schema it is using.

5. Installation must be verifiable.
   Production plugin systems normally rely on signed artifacts, checksums,
   pinned versions, and compatibility checks.

6. State and workspace boundaries must be declared.
   A portable plugin must say where durable state lives, which workspace paths it
   may read or write, and how it behaves during backup or migration.

7. Conformance tests are part of the product.
   A generic plugin is not just code. It needs a test kit that a hosting
   platform can run to prove install, submit, stream, artifact, quality, restart,
   and uninstall behavior.

## Recommended Product Direction

Across Orchestrator should evolve toward an "Agent Task Runtime Plugin" that can
be mounted into any agent container.

Near-term milestones:

1. Define `across-orchestrator.plugin.json`.
   Include plugin id, version, protocols, commands, ports, env vars, required
   volumes, permissions, state path, health endpoint, and schema versions.

2. Publish an OCI image.
   Provide sidecar examples for Docker Compose and Kubernetes.

3. Add a strict A2A gateway/profile.
   Keep internal `/tasks` APIs if useful, but also expose A2A-compatible
   message, task, artifact, and streaming mappings.

4. Version public schemas.
   Define JSON schemas for task submission, task state, event stream, evidence
   bundle, and quality benchmark.

5. Formalize host adapter contracts.
   Keep Python SDK adapters and add an HTTP adapter protocol so non-Python
   containers can use the runtime cleanly.

6. Add platform conformance tests.
   A hosting platform should be able to run a test suite that validates install,
   health, submit, run, event streaming, evidence, quality, restart recovery,
   and workspace isolation.

7. Document security and isolation.
   Specify how credentials, workspace paths, network egress, tool execution,
   and approval prompts are delegated to the platform.

## Practical Platform Recommendation

For a real agent hosting platform, start with sidecar mode:

```text
platform registry
  -> install Across Orchestrator sidecar for selected agent containers
  -> mount /workspace according to container policy
  -> mount /var/lib/across-orchestrator for durable task state
  -> expose orchestrator only on loopback or internal network
  -> map external A2A tasks into orchestrator tasks
  -> map orchestrator events and artifacts back into A2A responses
```

This keeps the external protocol stable while allowing Across Orchestrator to
evolve independently.

## Source References

- A2A specification: https://a2a-protocol.org/
- Model Context Protocol specification: https://modelcontextprotocol.io/
- Kubernetes sidecar containers: https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/
- HashiCorp Vault plugin architecture: https://developer.hashicorp.com/vault/docs/plugins/plugin-architecture
- OCI image specification: https://github.com/opencontainers/image-spec
- Figma plugin manifest pattern: https://developers.figma.com/docs/plugins/manifest/
