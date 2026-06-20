import json


def _valid_contract():
    from across_orchestrator.host_adapters import build_hosting_platform_contract

    return build_hosting_platform_contract(
        "generic-agent-host",
        [
            {
                "id": "planner",
                "name": "Planner",
                "endpoint": "http://127.0.0.1:9910/agents/planner",
                "protocols": ["http", "mcp"],
                "capabilities": ["planning", "contracts"],
                "tenant_id": "tenant-a",
            },
            {
                "id": "builder",
                "name": "Builder",
                "endpoint": "http://127.0.0.1:9910/agents/builder",
                "protocols": ["http"],
                "capabilities": ["implementation", "quality"],
                "tenant_id": "tenant-a",
            },
        ],
        memory_provider="across-context",
        credentials_provider="host-keychain",
        permissions_provider="host-policy",
        project_context={"project_id": "project-a", "workspace_root": "~/.across/workspaces/project-a"},
    ).to_dict()


def test_host_conformance_passes_for_generic_host_contract():
    from across_orchestrator.host_conformance import evaluate_host_conformance
    from across_orchestrator.plugin_manifest import render_plugin_manifest

    report = evaluate_host_conformance(_valid_contract(), manifest=render_plugin_manifest())
    text = json.dumps(report, sort_keys=True)

    assert report["passed"] is True
    assert report["pluginId"] == "across-orchestrator"
    assert report["host"]["platformId"] == "generic-agent-host"
    assert report["host"]["agentCount"] == 2
    assert report["protocols"]["http"] is True
    assert report["protocols"]["mcp"] is True
    assert report["protocols"]["sdk"] is True
    assert report["missingHostProvides"] == []
    assert report["unsupportedPluginProvides"] == []
    assert "autopilot_candidate_execution" in report["pluginProvides"]
    assert "Across Agents Assistant" not in text
    assert "AAA" not in text
    assert "Documents/projects" not in text


def test_host_conformance_fails_when_host_contract_omits_required_obligations():
    from across_orchestrator.host_conformance import evaluate_host_conformance
    from across_orchestrator.plugin_manifest import render_plugin_manifest

    report = evaluate_host_conformance(
        {
            "platform_id": "thin-host",
            "agents": [],
            "approval_mode": "host-mediated",
        },
        manifest=render_plugin_manifest(),
    )

    assert report["passed"] is False
    assert "registered_agent_containers" in report["missingHostProvides"]
    assert "agent_execution" in report["missingHostProvides"]
    assert "credentials" in report["missingHostProvides"]
    assert "user_permissions" in report["missingHostProvides"]
    assert "tenant_and_project_context" in report["missingHostProvides"]
    assert report["errors"]
