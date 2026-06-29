from across_orchestrator.agent_team_readiness import evaluate_agent_team_readiness


def workflow_export_payload():
    return {
        "schema_version": "across-workflow-pack-host-exports/1.0",
        "pack_id": "plugin-compatibility-lab-v2",
        "host_targets": ["codex", "claude_code", "mcp", "a2a", "across"],
        "runtime_policy": {
            "promotion": {
                "human_approval_required": True,
                "merge_release_signing_blocked": True,
            }
        },
        "trust_boundary": {"secrets": "not_allowed"},
        "product_card": {
            "schema_version": "across-workflow-pack-product-card/1.0",
            "pack_id": "plugin-compatibility-lab-v2",
            "headline": "Test an agent plugin before adoption.",
            "user_problem": "I need to know whether this plugin is safe.",
            "job_to_be_done": "Evaluate compatibility before team adoption.",
            "quickstart": {"cli": "across-autopilot loop run --spec plugin-compatibility-lab-v2 --json"},
            "market_readiness": {
                "status": "passed",
                "first_value_artifact": "run://plugin-compatibility-lab/report.md",
            },
        },
        "protocol_readiness": {
            "schema_version": "across-workflow-pack-protocol-readiness/1.0",
            "summary": {"score": 75, "honest_protocol_claims": True},
            "checks": [
                {"id": "remote_mcp_http_oauth", "status": "passed"},
                {"id": "mcp_stdio", "status": "passed"},
            ],
        },
        "trust_receipt": {
            "schema_version": "across-agent-team-trust-receipt/1.0",
            "receipt_id": "receipt-template:plugin-compatibility-lab-v2",
            "status": "passed",
            "evidence_contract": {
                "required": [
                    "runtime_policy",
                    "trust_boundary",
                    "host_exports",
                    "evidence_graph",
                    "validation_gates",
                ]
            },
        },
        "frontier_interop": {
            "schema_version": "across-workflow-pack-frontier-interop/1.0",
            "remote_mcp": {
                "schema_version": "across-remote-mcp-oauth-template/1.0",
                "oauth_required": True,
            },
            "a2a": {
                "schema_version": "across-a2a-task-delegation/2.0",
            },
            "projections": {
                "mcp_tasks": {"status": "projection_only"},
                "a2a": {"status": "passed"},
                "ag_ui": {"status": "passed"},
                "remote_mcp_oauth": {"status": "passed"},
                "otel": {"status": "passed"},
            },
            "observability": {
                "otel_schema": "across-otel-genai-export/1.0",
                "otlp_trace_schema": "otlp-traces-json/1.0",
                "raw_transcripts_included": False,
            },
        },
    }


def test_evaluate_agent_team_readiness_passes_complete_workflow_export():
    report = evaluate_agent_team_readiness(workflow_export_payload())

    assert report["schema_version"] == "across-agent-team-readiness/1.0"
    assert report["status"] == "passed"
    assert report["summary"]["market_ready"] is True
    assert report["score"] >= 80


def test_evaluate_agent_team_readiness_rejects_incomplete_projection_status():
    payload = workflow_export_payload()
    payload["frontier_interop"]["projections"].pop("ag_ui")

    report = evaluate_agent_team_readiness(payload)

    assert report["status"] == "failed"
    assert any(item["id"] == "projection_status_ready" and item["status"] == "failed" for item in report["checks"])


def test_evaluate_agent_team_readiness_accepts_projection_dimensions_shape():
    payload = workflow_export_payload()
    projections = payload["frontier_interop"]["projections"]
    payload["frontier_interop"]["projections"] = {
        "schema_version": "across-external-projection/1.0",
        "dimensions": projections,
    }

    report = evaluate_agent_team_readiness(payload)

    assert report["status"] == "passed"
    assert any(item["id"] == "projection_status_ready" and item["status"] == "passed" for item in report["checks"])
