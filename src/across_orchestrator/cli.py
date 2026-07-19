from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .agui_projection import project_events_to_agui
from .agent_card import render_agent_card
from .agent_team import create_agent_team
from .agent_team_readiness import evaluate_agent_team_readiness
from .a2a_delegation import create_a2a_task_delegation
from .agent_loop import CANCEL_CATEGORY_VALUES
from .evidence import build_evidence_receipt
from .evidence_graph import build_evidence_graph_from_payload
from .external_agents import ExternalAgentRegistry
from .host_install import install_agent_host
from .host_conformance import evaluate_host_conformance, load_host_contract
from .otel_export import export_otel_genai_spans
from .plugin_manifest import install_managed_plugin, render_plugin_health, render_plugin_manifest, render_plugin_status, uninstall_managed_plugin
from .protocol_gateway import render_protocol_gateway
from .redaction import redact_sensitive_value
from .remote_mcp import render_remote_mcp_oauth_template
from .run_contracts import build_execution_policy_contract, build_replay_plan, build_run_comparison
from .runtime import OrchestratorRuntime
from .sandbox import evaluate_sandbox_policy, execute_sandbox_command, get_sandbox_provider_registry
from .store import LocalStore


def _emit_public_text(text: str) -> None:
    os.write(sys.stdout.fileno(), text.encode("utf-8", errors="replace"))


def _print(payload: Any, as_json: bool) -> None:
    safe_payload = redact_sensitive_value(payload)
    if as_json:
        _emit_public_text(json.dumps(safe_payload, indent=2, sort_keys=True))
        _emit_public_text("\n")
    else:
        if isinstance(safe_payload, dict):
            for key, value in safe_payload.items():
                _emit_public_text(f"{key}: {value}\n")
        else:
            _emit_public_text(f"{safe_payload}\n")


def _json_object_arg(value: str | None, name: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must be a JSON object")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="across-orchestrator")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Create the local orchestrator state directory")

    submit = sub.add_parser("submit", help="Submit a task")
    submit.add_argument("goal")
    submit.add_argument("--project", required=True)
    submit.add_argument("--deliverable", action="append", default=[])
    submit.add_argument("--agent", default="demo")
    submit.add_argument("--task-type", action="append", default=[])
    submit.add_argument("--strict-dependency", action="store_true")
    submit.add_argument("--subtasks-json")
    submit.add_argument("--agent-adapters-json")
    submit.add_argument("--metadata-json")
    submit.add_argument("--json", action="store_true")

    release_e2e = sub.add_parser("submit-release-e2e", help="Submit the app-grade host conformance scenario")
    release_e2e.add_argument("--project", required=True)
    release_e2e.add_argument("--run-label")
    release_e2e.add_argument("--allowed-agent", action="append", default=[])
    release_e2e.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="Run a task")
    run.add_argument("task_id")
    run.add_argument("--json", action="store_true")

    cancel = sub.add_parser("cancel", help="Cancel a task and its durable execution loop")
    cancel.add_argument("task_id")
    cancel.add_argument("--reason", default="cancelled_by_user")
    cancel.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="Show task status")
    status.add_argument("task_id")
    status.add_argument("--json", action="store_true")

    events = sub.add_parser("events", help="Show task events")
    events.add_argument("task_id")
    events.add_argument("--json", action="store_true")

    evidence = sub.add_parser("evidence", help="Show task evidence bundle")
    evidence.add_argument("task_id")
    evidence.add_argument("--json", action="store_true")

    quality = sub.add_parser("quality", help="Show task quality benchmark")
    quality.add_argument("task_id")
    quality.add_argument("--json", action="store_true")

    loop_start = sub.add_parser("loop-start", help="Start a durable agent loop run")
    loop_start.add_argument("goal")
    loop_start.add_argument("--project", required=True)
    loop_start.add_argument("--agent", default="owner")
    loop_start.add_argument("--max-turns", type=int, default=8)
    loop_start.add_argument("--require-approval-for", action="append", default=[])
    loop_start.add_argument("--memory-policy-json")
    loop_start.add_argument("--approval-policy-json")
    loop_start.add_argument("--metadata-json")
    loop_start.add_argument("--json", action="store_true")

    loop_run = sub.add_parser("loop-run", help="Run or continue an agent loop")
    loop_run.add_argument("loop_id")
    loop_run.add_argument("--json", action="store_true")

    loop_approve = sub.add_parser("loop-approve", help="Approve a pending agent loop action")
    loop_approve.add_argument("loop_id")
    loop_approve.add_argument("action_id")
    loop_approve.add_argument("--json", action="store_true")

    loop_reject = sub.add_parser("loop-reject", help="Reject a pending agent loop action")
    loop_reject.add_argument("loop_id")
    loop_reject.add_argument("action_id")
    loop_reject.add_argument("--reason", default="rejected")
    loop_reject.add_argument("--json", action="store_true")

    loop_cancel = sub.add_parser("loop-cancel", help="Cancel a pending or running agent loop")
    loop_cancel.add_argument("loop_id")
    loop_cancel.add_argument("--reason", default="cancelled")
    loop_cancel.add_argument(
        "--category",
        choices=list(CANCEL_CATEGORY_VALUES),
        default=None,
    )
    loop_cancel.add_argument("--json", action="store_true")

    loop_retry = sub.add_parser("loop-retry", help="Retry an agent loop from a selected step")
    loop_retry.add_argument("loop_id")
    loop_retry.add_argument("step_id")
    loop_retry.add_argument("--json", action="store_true")

    loop_status = sub.add_parser("loop-status", help="Show agent loop status")
    loop_status.add_argument("loop_id")
    loop_status.add_argument("--json", action="store_true")

    loop_health = sub.add_parser("loop-health", help="Show agent loop health summary")
    loop_health.add_argument("loop_id")
    loop_health.add_argument("--json", action="store_true")

    loop_evidence_summary = sub.add_parser("loop-evidence-summary", help="Show compact agent loop evidence summary")
    loop_evidence_summary.add_argument("loop_id")
    loop_evidence_summary.add_argument("--json", action="store_true")

    loop_telemetry = sub.add_parser("loop-telemetry", help="Show bounded agent loop telemetry metrics")
    loop_telemetry.add_argument("loop_id")
    loop_telemetry.add_argument("--json", action="store_true")

    loop_events = sub.add_parser("loop-events", help="Show agent loop events")
    loop_events.add_argument("loop_id")
    loop_events.add_argument("--after-sequence", type=int)
    loop_events.add_argument("--json", action="store_true")

    card = sub.add_parser("agent-card", help="Print the A2A-style Agent Card")
    card.add_argument("--json", action="store_true")

    protocol_gateway = sub.add_parser("protocol-gateway", help="Print the AAA protocol gateway matrix")
    protocol_gateway.add_argument("--json", action="store_true")

    sandbox_probe = sub.add_parser("sandbox-probe", help="Evaluate an Across sandbox policy without executing commands")
    sandbox_probe.add_argument("--policy-json", required=True)
    sandbox_probe.add_argument("--command-json")
    sandbox_probe.add_argument("--cwd")
    sandbox_probe.add_argument("--json", action="store_true")

    sandbox_execute = sub.add_parser("sandbox-execute", help="Execute allowlisted argv through a sandbox provider")
    sandbox_execute.add_argument("--policy-json", required=True)
    sandbox_execute.add_argument("--command-json", required=True)
    sandbox_execute.add_argument("--cwd", required=True)
    sandbox_execute.add_argument("--provider", default="local-workspace")
    sandbox_execute.add_argument("--timeout-seconds", type=float)
    sandbox_execute.add_argument("--max-output-bytes", type=int)
    sandbox_execute.add_argument("--json", action="store_true")

    sandbox_providers = sub.add_parser("sandbox-providers", help="List registered sandbox providers")
    sandbox_providers.add_argument("--json", action="store_true")

    evidence_receipt = sub.add_parser("evidence-receipt", help="Build a unified secret-free evidence receipt")
    evidence_receipt.add_argument("--payload-json", required=True)
    evidence_receipt.add_argument("--json", action="store_true")

    execution_policy = sub.add_parser("execution-policy", help="Render a public role/model/budget and risk-selected sandbox contract")
    execution_policy.add_argument("--payload-json", required=True)
    execution_policy.add_argument("--json", action="store_true")

    run_compare = sub.add_parser("run-compare", help="Compare two evidence-backed run snapshots")
    run_compare.add_argument("--payload-json", required=True)
    run_compare.add_argument("--json", action="store_true")

    replay_plan = sub.add_parser("replay-plan", help="Build a non-executing replay plan with renewed-approval enforcement")
    replay_plan.add_argument("--payload-json", required=True)
    replay_plan.add_argument("--json", action="store_true")

    evidence_graph = sub.add_parser("evidence-graph", help="Build a host-neutral evidence graph from an evidence payload")
    evidence_graph.add_argument("--payload-json", required=True)
    evidence_graph.add_argument("--json", action="store_true")

    agent_team_readiness = sub.add_parser("agent-team-readiness", help="Evaluate a Workflow Pack export for market-ready agent-team use")
    agent_team_readiness.add_argument("--payload-json", required=True)
    agent_team_readiness.add_argument("--json", action="store_true")

    remote_mcp = sub.add_parser("remote-mcp-oauth-template", help="Render a secret-free Streamable HTTP/OAuth template for remote MCP deployment")
    remote_mcp.add_argument("--config-json")
    remote_mcp.add_argument("--json", action="store_true")

    remote_mcp_server = sub.add_parser("remote-mcp-server", help="Start a Streamable HTTP MCP server with OAuth Resource Server enforcement")
    remote_mcp_server_sub = remote_mcp_server.add_subparsers(dest="remote_mcp_server_command")
    remote_mcp_server_start = remote_mcp_server_sub.add_parser("start", help="Bind a local Streamable HTTP endpoint with bearer-token verification")
    remote_mcp_server_start.add_argument("--host", default="127.0.0.1")
    remote_mcp_server_start.add_argument("--port", type=int, default=8765)
    remote_mcp_server_start.add_argument("--config-json", help="OAuth config JSON: {issuer, audience, scopes, jwks_uri, hs256_secret, jwks_url, required_claims}")
    remote_mcp_server_start.add_argument("--allowed-origins", action="append", default=[], help="Allowed Origin header values for DNS-rebinding protection (repeatable)")
    remote_mcp_server_start.add_argument("--json", action="store_true")

    a2a_delegation = sub.add_parser("a2a-delegation", help="Create an A2A-style task/message/artifact delegation envelope")
    a2a_delegation.add_argument("--payload-json")
    a2a_delegation.add_argument("--json", action="store_true")

    agui_projection = sub.add_parser("agui-projection", help="Project Across task or loop events into AG-UI task-card events")
    agui_projection.add_argument("--payload-json", required=True)
    agui_projection.add_argument("--json", action="store_true")

    agent_team = sub.add_parser("agent-team", help="Create a first-class agent-team session/checkpoint/handoff contract")
    agent_team.add_argument("--payload-json")
    agent_team.add_argument("--json", action="store_true")

    otel_export = sub.add_parser("otel-export", help="Export evidence as OTel/GenAI-style spans and eval cases")
    otel_export.add_argument("--payload-json", required=True)
    otel_export.add_argument("--otlp-file", help="Optional path to write OTLP JSON traces for collector-compatible smoke tests")
    otel_export.add_argument("--json", action="store_true")

    external_agents = sub.add_parser("external-agents", help="Manage generic external agent plugin manifests")
    external_agents_sub = external_agents.add_subparsers(dest="external_agents_command")
    external_validate = external_agents_sub.add_parser("validate", help="Validate an across-agent-plugin manifest")
    external_validate.add_argument("--manifest", required=True)
    external_validate.add_argument("--json", action="store_true")
    external_register = external_agents_sub.add_parser("register", help="Register an across-agent-plugin manifest")
    external_register.add_argument("--manifest", required=True)
    external_register.add_argument("--json", action="store_true")
    external_list = external_agents_sub.add_parser("list", help="List registered external agent plugins")
    external_list.add_argument("--manifest", action="append", default=[])
    external_list.add_argument("--probe", action="store_true")
    external_list.add_argument("--json", action="store_true")
    external_health = external_agents_sub.add_parser("health", help="Summarize external agent plugin health")
    external_health.add_argument("--agent-id")
    external_health.add_argument("--probe", action="store_true")
    external_health.add_argument("--json", action="store_true")

    manifest = sub.add_parser("plugin-manifest", help="Print the Across plugin manifest")
    manifest.add_argument("--json", action="store_true")

    host_conformance = sub.add_parser("host-conformance", help="Validate an external host contract against this plugin")
    host_conformance.add_argument("--contract", required=True, help="Path to a host contract JSON file")
    host_conformance.add_argument("--json", action="store_true")

    plugin_status = sub.add_parser("plugin-status", help="Print Across plugin install and runtime status")
    plugin_status.add_argument("--json", action="store_true")

    health = sub.add_parser("health", help="Probe local runtime health")
    health.add_argument("--json", action="store_true")

    install = sub.add_parser("install", help="Prepare generic host MCP registrations")
    install.add_argument("target", choices=["codex", "codex-mcp", "claude", "claude-code", "claude-desktop"])
    install.add_argument("--stdout", action="store_true")
    install.add_argument("--config-file")
    install.add_argument("--json", action="store_true")

    plugin_install = sub.add_parser("plugin-install", help="Install or repair a managed host plugin runtime")
    plugin_install.add_argument("--json", action="store_true")

    plugin_uninstall = sub.add_parser("plugin-uninstall", help="Remove a managed host plugin runtime while preserving data")
    plugin_uninstall.add_argument("--json", action="store_true")

    sub.add_parser("mcp", help="Start MCP stdio server")

    worker_control = sub.add_parser("worker-control", help="Run one host-controlled Worker protocol operation from stdin")
    worker_control.add_argument("--json", action="store_true")

    worker_control_server = sub.add_parser("worker-control-server", help="Serve host-controlled Worker protocol operations on a private Unix socket")
    worker_control_server.add_argument("--socket", required=True)

    worker_listener = sub.add_parser("worker-listener", help="Start the explicit-interface TLS 1.3 Worker session listener")
    worker_listener.add_argument("--host", required=True)
    worker_listener.add_argument("--port", type=int, required=True)
    worker_listener.add_argument("--certificate", required=True)
    worker_listener.add_argument("--private-key", required=True)
    worker_listener.add_argument("--client-ca", required=True)
    worker_listener.add_argument("--artifact-root", required=True)
    worker_listener.add_argument("--transport", choices=("direct", "overlay"), default="direct")
    worker_listener.add_argument("--model-gateway-url")

    worker_relay = sub.add_parser("worker-relay-session", help="Connect the Coordinator to one opaque Relay Worker session")
    worker_relay.add_argument("--endpoint", required=True)
    worker_relay.add_argument("--server-name")
    worker_relay.add_argument("--node-id", required=True, help="Coordinator Relay identity")
    worker_relay.add_argument("--peer-node-id", required=True, help="Approved Worker Node ID")
    worker_relay.add_argument("--session-id", required=True)
    worker_relay.add_argument("--session-key-file", required=True, help="0600 file containing the base64 E2E key")
    worker_relay.add_argument("--certificate", required=True)
    worker_relay.add_argument("--private-key", required=True)
    worker_relay.add_argument("--server-ca", help="Optional private Relay CA; omit for the system trust store")
    worker_relay.add_argument("--artifact-root", required=True)
    worker_relay.add_argument("--model-gateway-unix-socket", help="AAA Unix socket used for encrypted Relay Model Grant calls")
    worker_relay.add_argument("--once", action="store_true")

    serve = sub.add_parser("serve", help="Start HTTP server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--runtime-id")
    serve.add_argument("--runtime-info")
    serve.add_argument(
        "--allow-client-project-roots",
        action="store_true",
        help="Allow local HTTP /tasks clients to select an existing absolute project directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runtime = OrchestratorRuntime()
    loop_runtime = runtime.loop_runtime

    if args.command == "init":
        store = LocalStore()
        _print({"status": "ready", "home": str(store.home)}, False)
        return 0

    if args.command == "submit":
        subtasks = None
        if args.subtasks_json:
            try:
                subtasks = json.loads(args.subtasks_json)
            except json.JSONDecodeError as exc:
                parser.error(f"--subtasks-json must be valid JSON: {exc}")
            if not isinstance(subtasks, list):
                parser.error("--subtasks-json must be a JSON array")
        try:
            agent_adapters = _json_object_arg(args.agent_adapters_json, "--agent-adapters-json")
            metadata = _json_object_arg(args.metadata_json, "--metadata-json")
        except ValueError as exc:
            parser.error(str(exc))
            return 2
        task = runtime.submit_task(
            goal=args.goal,
            project_root=args.project,
            deliverables=args.deliverable or ["README.md"],
            agent=args.agent,
            subtasks=subtasks,
            strict_dependency=bool(args.strict_dependency),
            task_types=args.task_type or None,
            agent_adapters=agent_adapters or None,
            metadata=metadata or None,
        )
        _print(task.to_dict(), args.json)
        return 0

    if args.command == "submit-release-e2e":
        task = runtime.submit_release_e2e_task(
            project_root=args.project,
            run_label=args.run_label,
            allowed_agents=args.allowed_agent or None,
        )
        _print(task.to_dict(), args.json)
        return 0

    if args.command == "run":
        task = runtime.run_task(args.task_id)
        _print(task.to_dict(), args.json)
        return 0

    if args.command == "cancel":
        task = runtime.cancel_task(args.task_id, reason=args.reason)
        _print(task.to_dict(), args.json)
        return 0

    if args.command == "status":
        task = runtime.get_task(args.task_id)
        _print(task.to_dict(), args.json)
        return 0

    if args.command == "events":
        _print(runtime.list_events(args.task_id), args.json)
        return 0

    if args.command == "evidence":
        _print(runtime.evidence_bundle(args.task_id), args.json)
        return 0

    if args.command == "quality":
        _print(runtime.quality_benchmark(args.task_id), args.json)
        return 0

    if args.command == "loop-start":
        try:
            memory_policy = _json_object_arg(args.memory_policy_json, "--memory-policy-json")
            approval_policy = _json_object_arg(args.approval_policy_json, "--approval-policy-json")
            metadata = _json_object_arg(args.metadata_json, "--metadata-json")
        except ValueError as exc:
            parser.error(str(exc))
            return 2
        if args.require_approval_for:
            approval_policy = {
                **approval_policy,
                "requireApprovalFor": args.require_approval_for,
            }
        try:
            loop = loop_runtime.start_loop(
                goal=args.goal,
                project_root=args.project,
                agent=args.agent,
                max_turns=args.max_turns,
                memory_policy=memory_policy or None,
                approval_policy=approval_policy or None,
                metadata=metadata or None,
            )
        except ValueError as exc:
            parser.error(str(exc))
            return 2
        _print(loop.to_dict(), args.json)
        return 0

    if args.command == "loop-run":
        _print(loop_runtime.run_loop(args.loop_id).to_dict(), args.json)
        return 0

    if args.command == "loop-approve":
        _print(loop_runtime.approve_action(args.loop_id, args.action_id).to_dict(), args.json)
        return 0

    if args.command == "loop-reject":
        _print(loop_runtime.reject_action(args.loop_id, args.action_id, reason=args.reason).to_dict(), args.json)
        return 0

    if args.command == "loop-cancel":
        _print(
            loop_runtime.cancel_loop(args.loop_id, reason=args.reason, cancel_category=args.category).to_dict(),
            args.json,
        )
        return 0

    if args.command == "loop-retry":
        _print(loop_runtime.retry_step(args.loop_id, args.step_id).to_dict(), args.json)
        return 0

    if args.command == "loop-status":
        _print(loop_runtime.get_loop(args.loop_id).to_dict(), args.json)
        return 0

    if args.command == "loop-health":
        _print(loop_runtime.get_loop_health(args.loop_id), args.json)
        return 0

    if args.command == "loop-evidence-summary":
        _print(loop_runtime.get_loop_evidence_summary(args.loop_id), args.json)
        return 0

    if args.command == "loop-telemetry":
        _print(loop_runtime.get_loop_telemetry(args.loop_id), args.json)
        return 0

    if args.command == "loop-events":
        _print(loop_runtime.list_loop_events(args.loop_id, after_sequence=args.after_sequence), args.json)
        return 0

    if args.command == "agent-card":
        _print(render_agent_card(), args.json)
        return 0

    if args.command == "protocol-gateway":
        _print(render_protocol_gateway(), args.json)
        return 0

    if args.command == "sandbox-probe":
        try:
            policy = _json_object_arg(args.policy_json, "--policy-json")
            command = None
            if args.command_json:
                command_payload = json.loads(args.command_json)
                if not isinstance(command_payload, list):
                    parser.error("--command-json must be a JSON array")
                command = [str(item) for item in command_payload]
        except (json.JSONDecodeError, ValueError) as exc:
            parser.error(str(exc))
            return 2
        payload = evaluate_sandbox_policy(policy, command=command, cwd=args.cwd)
        _print(payload, args.json)
        return 0 if payload["status"] == "passed" else 1

    if args.command == "sandbox-execute":
        try:
            policy = _json_object_arg(args.policy_json, "--policy-json")
            command_payload = json.loads(args.command_json)
            if not isinstance(command_payload, list):
                parser.error("--command-json must be a JSON array")
            command = [str(item) for item in command_payload]
            payload = execute_sandbox_command(
                policy,
                command=command,
                cwd=args.cwd,
                provider_id=args.provider,
                timeout_seconds=args.timeout_seconds,
                max_output_bytes=args.max_output_bytes,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            parser.error(str(exc))
            return 2
        _print(payload, args.json)
        return 0 if payload["status"] == "completed" else 1

    if args.command == "sandbox-providers":
        _print({"providers": get_sandbox_provider_registry().list()}, args.json)
        return 0

    if args.command == "evidence-receipt":
        try:
            payload = _json_object_arg(args.payload_json, "--payload-json")
            receipt = build_evidence_receipt(payload)
        except ValueError as exc:
            parser.error(str(exc))
            return 2
        _print(receipt, args.json)
        return 0

    if args.command in {"execution-policy", "run-compare", "replay-plan"}:
        try:
            payload = _json_object_arg(args.payload_json, "--payload-json")
        except ValueError as exc:
            parser.error(str(exc))
            return 2
        renderer = {
            "execution-policy": build_execution_policy_contract,
            "run-compare": build_run_comparison,
            "replay-plan": build_replay_plan,
        }[args.command]
        result = renderer(payload)
        _print(result, args.json)
        return 0

    if args.command == "evidence-graph":
        try:
            payload = _json_object_arg(args.payload_json, "--payload-json")
        except ValueError as exc:
            parser.error(str(exc))
            return 2
        _print(build_evidence_graph_from_payload(payload), args.json)
        return 0

    if args.command == "agent-team-readiness":
        try:
            payload = _json_object_arg(args.payload_json, "--payload-json")
        except ValueError as exc:
            parser.error(str(exc))
            return 2
        report = evaluate_agent_team_readiness(payload)
        _print(report, args.json)
        return 0 if report["status"] == "passed" else 1

    if args.command == "remote-mcp-oauth-template":
        try:
            config = _json_object_arg(args.config_json, "--config-json")
        except ValueError as exc:
            parser.error(str(exc))
            return 2
        payload = render_remote_mcp_oauth_template(config)
        _print(payload, args.json)
        return 0 if payload["status"] == "passed" else 1

    if args.command == "remote-mcp-server":
        from .server import build_remote_mcp_oauth_config, serve as serve_runtime

        if not getattr(args, "remote_mcp_server_command", None):
            sys.stderr.write(
                "remote-mcp-server requires a subcommand. Use 'start' to bind a Streamable HTTP endpoint.\n"
            )
            _print(
                {
                    "status": "missing_subcommand",
                    "subcommands": ["start"],
                },
                True,
            )
            return 2
        if args.remote_mcp_server_command != "start":
            parser.error("remote-mcp-server requires 'start'")
        try:
            user_config = _json_object_arg(args.config_json, "--config-json")
        except ValueError as exc:
            parser.error(str(exc))
            return 2
        oauth_config = build_remote_mcp_oauth_config(
            host=args.host,
            port=args.port,
            issuer=user_config.get("issuer"),
            audience=user_config.get("audience"),
            jwks_uri=user_config.get("jwks_uri"),
            hs256_secret=user_config.get("hs256_secret"),
            jwks_url=user_config.get("jwks_url"),
            scopes=user_config.get("scopes"),
            allowed_origins=list(args.allowed_origins) + list(user_config.get("allowed_origins") or []),
            required_claims=user_config.get("required_claims") or user_config.get("requiredClaims"),
        )
        if args.json:
            _print(
                {
                    "status": "starting",
                    "host": args.host,
                    "port": args.port,
                    "endpoint": oauth_config["base_url"],
                    "mcp_endpoint": f"{oauth_config['base_url']}{oauth_config['mcp_endpoint']}",
                    "protected_resource": f"{oauth_config['base_url']}{oauth_config['well_known_protected_resource']}",
                    "authorization_server": f"{oauth_config['base_url']}{oauth_config['well_known_authorization_server']}",
                    "audience": oauth_config["audience"],
                    "issuer": oauth_config["issuer"],
                    "scopes": oauth_config["scopes"],
                },
                True,
            )
        serve_runtime(args.host, args.port, remote_mcp_oauth_config=oauth_config)
        return 0

    if args.command == "a2a-delegation":
        try:
            payload = _json_object_arg(args.payload_json, "--payload-json")
        except ValueError as exc:
            parser.error(str(exc))
            return 2
        _print(create_a2a_task_delegation(payload), args.json)
        return 0

    if args.command == "agui-projection":
        try:
            payload = _json_object_arg(args.payload_json, "--payload-json")
        except ValueError as exc:
            parser.error(str(exc))
            return 2
        _print(project_events_to_agui(payload), args.json)
        return 0

    if args.command == "agent-team":
        try:
            payload = _json_object_arg(args.payload_json, "--payload-json")
        except ValueError as exc:
            parser.error(str(exc))
            return 2
        _print(create_agent_team(payload), args.json)
        return 0

    if args.command == "otel-export":
        try:
            payload = _json_object_arg(args.payload_json, "--payload-json")
        except ValueError as exc:
            parser.error(str(exc))
            return 2
        exported = export_otel_genai_spans(payload)
        if args.otlp_file:
            target = Path(args.otlp_file).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(exported.get("otlp") or {}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            exported = {**exported, "otlp_file": str(target)}
        _print(exported, args.json)
        return 0

    if args.command == "external-agents":
        registry = ExternalAgentRegistry()
        if args.external_agents_command == "validate":
            _print(registry.validate_manifest_file(args.manifest), args.json)
            return 0
        if args.external_agents_command == "register":
            _print(registry.register_manifest_file(args.manifest), args.json)
            return 0
        if args.external_agents_command == "list":
            manifests = registry.list_manifests()
            for manifest_path in args.manifest or []:
                manifests.append(registry.validate_manifest_file(manifest_path))
            _print(registry.registry_payload(manifests, probe=args.probe), args.json)
            return 0
        if args.external_agents_command == "health":
            _print(registry.health_payload(args.agent_id, probe=args.probe), args.json)
            return 0
        parser.error("external-agents requires validate, register, list, or health")

    if args.command == "plugin-manifest":
        _print(render_plugin_manifest(), args.json)
        return 0

    if args.command == "host-conformance":
        report = evaluate_host_conformance(load_host_contract(args.contract))
        _print(report, args.json)
        return 0 if report["passed"] else 1

    if args.command == "plugin-status":
        _print(render_plugin_status(), args.json)
        return 0

    if args.command == "health":
        _print(render_plugin_health(), args.json)
        return 0

    if args.command == "install":
        payload = install_agent_host(args.target, config_file=args.config_file, env=os.environ)
        if args.json:
            _print(payload, True)
        elif args.stdout or payload.get("command"):
            print(payload.get("command") or json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print(payload, args.json)
        return 0

    if args.command == "plugin-install":
        _print(install_managed_plugin(os.environ, force=True), args.json)
        return 0

    if args.command == "plugin-uninstall":
        _print(uninstall_managed_plugin(), args.json)
        return 0

    if args.command == "mcp":
        from .mcp import main as mcp_main

        return mcp_main()

    if args.command == "worker-control":
        from .worker_control_command import handle_worker_control_command

        try:
            request = json.loads(sys.stdin.read())
            if not isinstance(request, dict):
                raise ValueError("worker control request must be a JSON object")
            result = handle_worker_control_command(request)
        except (json.JSONDecodeError, ValueError) as exc:
            _print({"status": "error", "error": str(exc)}, True)
            return 2
        _print(result, True)
        return 0

    if args.command == "worker-control-server":
        import asyncio
        from .worker_control_command import serve_worker_control

        asyncio.run(serve_worker_control(args.socket))
        return 0

    if args.command == "worker-listener":
        import asyncio
        from .coordinator import WorkerCoordinator
        from .worker_transport import CoordinatorSessionServer, tls_server_context

        async def run_worker_listener() -> None:
            server = CoordinatorSessionServer(
                WorkerCoordinator(),
                host=args.host,
                port=args.port,
                ssl_context=tls_server_context(certificate=args.certificate, private_key=args.private_key, client_ca=args.client_ca),
                artifact_root=args.artifact_root,
                transport=args.transport,
                model_gateway_url=args.model_gateway_url,
            )
            await server.start()
            try:
                await asyncio.Event().wait()
            finally:
                await server.close()

        asyncio.run(run_worker_listener())
        return 0

    if args.command == "worker-relay-session":
        import asyncio
        from .coordinator import WorkerCoordinator
        from .relay import RelayEndpoint, create_tls_context
        from .worker_transport import RelayCoordinatorSession, UnixSocketModelGateway

        parsed = urlparse(args.endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            parser.error("Worker Relay endpoint must be a credential-free HTTPS URL")
        key_path = Path(args.session_key_file).expanduser().resolve()
        if key_path.stat().st_mode & 0o077:
            parser.error("Worker Relay session key file must be private (0600)")
        try:
            session_key = base64.urlsafe_b64decode(key_path.read_text(encoding="ascii").strip().encode())
        except Exception:
            parser.error("Worker Relay session key file is invalid")
        context = create_tls_context(
            server=False,
            certificate=args.certificate,
            private_key=args.private_key,
            trust_store=args.server_ca,
        )
        coordinator = WorkerCoordinator()
        model_gateway = UnixSocketModelGateway(args.model_gateway_unix_socket) if args.model_gateway_unix_socket else None

        async def run_worker_relay() -> None:
            backoff = 1.0
            while True:
                endpoint = RelayEndpoint(
                    host=parsed.hostname,
                    port=int(parsed.port or 443),
                    server_hostname=args.server_name or parsed.hostname,
                    ssl_context=context,
                    node_id=args.node_id,
                    peer_node_id=args.peer_node_id,
                    session_id=args.session_id,
                    session_key=session_key,
                )
                try:
                    await endpoint.connect()
                    await endpoint.register_session(ttl_seconds=3600)
                    await RelayCoordinatorSession(
                        coordinator,
                        endpoint,
                        artifact_root=args.artifact_root,
                        model_gateway=model_gateway,
                    ).run_once()
                    backoff = 1.0
                    if args.once:
                        return
                except (OSError, asyncio.TimeoutError, ConnectionError):
                    if args.once:
                        raise
                    await asyncio.sleep(backoff)
                    backoff = min(30.0, backoff * 2)
                finally:
                    await endpoint.close()

        asyncio.run(run_worker_relay())
        return 0

    if args.command == "serve":
        from .server import serve

        serve(
            args.host,
            args.port,
            runtime_id=args.runtime_id,
            runtime_info=args.runtime_info,
            allow_client_project_roots=args.allow_client_project_roots,
        )
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
