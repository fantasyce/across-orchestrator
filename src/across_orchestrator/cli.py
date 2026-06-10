from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .agent_card import render_agent_card
from .agent_loop import AgentLoopRuntime
from .plugin_manifest import render_plugin_health, render_plugin_manifest, render_plugin_status, uninstall_managed_plugin
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="across-orchestrator")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Create the local orchestrator state directory")

    submit = sub.add_parser("submit", help="Submit a task")
    submit.add_argument("goal")
    submit.add_argument("--project", required=True)
    submit.add_argument("--deliverable", action="append", default=[])
    submit.add_argument("--agent", default="demo")
    submit.add_argument("--json", action="store_true")

    release_e2e = sub.add_parser("submit-release-e2e", help="Submit the app-grade release E2E parity scenario")
    release_e2e.add_argument("--project", required=True)
    release_e2e.add_argument("--run-label")
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
    loop_start.add_argument("--json", action="store_true")

    loop_run = sub.add_parser("loop-run", help="Run or continue an agent loop")
    loop_run.add_argument("loop_id")
    loop_run.add_argument("--json", action="store_true")

    loop_status = sub.add_parser("loop-status", help="Show agent loop status")
    loop_status.add_argument("loop_id")
    loop_status.add_argument("--json", action="store_true")

    loop_events = sub.add_parser("loop-events", help="Show agent loop events")
    loop_events.add_argument("loop_id")
    loop_events.add_argument("--json", action="store_true")

    card = sub.add_parser("agent-card", help="Print the A2A-style Agent Card")
    card.add_argument("--json", action="store_true")

    manifest = sub.add_parser("plugin-manifest", help="Print the Across plugin manifest")
    manifest.add_argument("--json", action="store_true")

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
    loop_runtime = AgentLoopRuntime(runtime.store)

    if args.command == "init":
        store = LocalStore()
        _print({"status": "ready", "home": str(store.home)}, False)
        return 0

    if args.command == "submit":
        task = runtime.submit_task(
            goal=args.goal,
            project_root=args.project,
            deliverables=args.deliverable or ["README.md"],
            agent=args.agent,
        )
        _print(task.to_dict(), args.json)
        return 0

    if args.command == "submit-release-e2e":
        task = runtime.submit_release_e2e_task(
            project_root=args.project,
            run_label=args.run_label,
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
        loop = loop_runtime.start_loop(
            goal=args.goal,
            project_root=args.project,
            agent=args.agent,
            max_turns=args.max_turns,
        )
        _print(loop.to_dict(), args.json)
        return 0

    if args.command == "loop-run":
        _print(loop_runtime.run_loop(args.loop_id).to_dict(), args.json)
        return 0

    if args.command == "loop-status":
        _print(loop_runtime.get_loop(args.loop_id).to_dict(), args.json)
        return 0

    if args.command == "loop-events":
        _print(loop_runtime.list_loop_events(args.loop_id), args.json)
        return 0

    if args.command == "agent-card":
        _print(render_agent_card(), args.json)
        return 0

    if args.command == "plugin-manifest":
        _print(render_plugin_manifest(), args.json)
        return 0

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
