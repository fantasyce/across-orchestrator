from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .agent_card import render_agent_card
from .agent_loop import CANCEL_CATEGORY_VALUES
from .external_agents import ExternalAgentRegistry
from .host_conformance import evaluate_host_conformance, load_host_contract
from .plugin_manifest import render_plugin_health, render_plugin_manifest, render_plugin_status, uninstall_managed_plugin
from .protocol_gateway import render_protocol_gateway
from .runtime import OrchestratorRuntime
from .store import LocalStore


def _print(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if isinstance(payload, dict):
            for key, value in payload.items():
                print(f"{key}: {value}")
        else:
            print(payload)


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
    submit.add_argument("--json", action="store_true")

    release_e2e = sub.add_parser("submit-release-e2e", help="Submit the app-grade host conformance scenario")
    release_e2e.add_argument("--project", required=True)
    release_e2e.add_argument("--run-label")
    release_e2e.add_argument("--allowed-agent", action="append", default=[])
    release_e2e.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="Run a task")
    run.add_argument("task_id")
    run.add_argument("--json", action="store_true")

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

    plugin_uninstall = sub.add_parser("plugin-uninstall", help="Remove a managed host plugin runtime while preserving data")
    plugin_uninstall.add_argument("--json", action="store_true")

    sub.add_parser("mcp", help="Start MCP stdio server")

    serve = sub.add_parser("serve", help="Start HTTP server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--runtime-id")
    serve.add_argument("--runtime-info")
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
        except ValueError as exc:
            parser.error(str(exc))
        task = runtime.submit_task(
            goal=args.goal,
            project_root=args.project,
            deliverables=args.deliverable or ["README.md"],
            agent=args.agent,
            subtasks=subtasks,
            strict_dependency=bool(args.strict_dependency),
            task_types=args.task_type or None,
            agent_adapters=agent_adapters or None,
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

    if args.command == "plugin-uninstall":
        _print(uninstall_managed_plugin(), args.json)
        return 0

    if args.command == "mcp":
        from .mcp import main as mcp_main

        return mcp_main()

    if args.command == "serve":
        from .server import serve

        serve(args.host, args.port, runtime_id=args.runtime_id, runtime_info=args.runtime_info)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
