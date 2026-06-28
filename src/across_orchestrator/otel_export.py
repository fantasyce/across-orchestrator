from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from .evidence_graph import build_evidence_graph_from_payload


OTEL_GENAI_EXPORT_SCHEMA = "across-otel-genai-export/1.0"
EVAL_DATASET_SCHEMA = "across-eval-dataset/1.0"
OTLP_TRACES_SCHEMA = "otlp-traces-json/1.0"


def export_otel_genai_spans(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Convert Across evidence into a compact OTel/GenAI-style span export."""

    graph = build_evidence_graph_from_payload(payload or {})
    run_id = str(graph.get("run_id") or "unknown")
    spec_id = str(graph.get("spec_id") or "unknown")
    trace_id = _hex(run_id, 32)
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    spans = []
    for node in _list(graph.get("nodes")):
        node_id = str(node.get("id") or "")
        node_type = str(node.get("type") or "unknown")
        spans.append(
            {
                "trace_id": trace_id,
                "span_id": _hex(node_id, 16),
                "parent_span_id": _parent_span_id(node, graph),
                "name": f"across.{node_type}",
                "kind": "INTERNAL",
                "start_time": now,
                "end_time": now,
                "attributes": {
                    "gen_ai.operation.name": "agent_workflow",
                    "gen_ai.system": "across",
                    "across.run_id": run_id,
                    "across.spec_id": spec_id,
                    "across.node.id": node_id,
                    "across.node.type": node_type,
                    "across.node.status": str(node.get("status") or "unknown"),
                    "across.node.hash": str(node.get("hash") or ""),
                },
            }
        )
    resource = {
        "service.name": "across-orchestrator",
        "service.namespace": "across",
        "telemetry.sdk.language": "python",
    }
    scope = {
        "name": "across.agent_team",
        "schema_url": "https://opentelemetry.io/schemas/genai",
    }
    eval_dataset = _eval_dataset(graph)
    return {
        "schema_version": OTEL_GENAI_EXPORT_SCHEMA,
        "provider": "across-orchestrator",
        "status": "passed" if spans else "attention",
        "resource": resource,
        "scope": scope,
        "trace_id": trace_id,
        "spans": spans,
        "otlp": _otlp_traces(spans, resource, scope),
        "eval_dataset": eval_dataset,
        "summary": {
            "span_count": len(spans),
            "eval_case_count": len(eval_dataset["cases"]),
            "raw_transcripts_included": False,
            "otlp_resource_span_count": 1 if spans else 0,
        },
    }


def _otlp_traces(spans: list[dict[str, Any]], resource: dict[str, str], scope: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": OTLP_TRACES_SCHEMA,
        "resourceSpans": [
            {
                "resource": {"attributes": _otlp_attributes(resource)},
                "scopeSpans": [
                    {
                        "scope": {
                            "name": scope["name"],
                            "schemaUrl": scope["schema_url"],
                        },
                        "spans": [_otlp_span(span) for span in spans],
                    }
                ],
            }
        ] if spans else [],
    }


def _otlp_span(span: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "traceId": span["trace_id"],
        "spanId": span["span_id"],
        "name": span["name"],
        "kind": span["kind"],
        "startTimeUnixNano": _iso_to_unix_nano(str(span["start_time"])),
        "endTimeUnixNano": _iso_to_unix_nano(str(span["end_time"])),
        "attributes": _otlp_attributes(span.get("attributes") or {}),
    }
    if span.get("parent_span_id"):
        payload["parentSpanId"] = span["parent_span_id"]
    return payload


def _otlp_attributes(values: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": str(key), "value": _otlp_value(value)} for key, value in sorted(values.items())]


def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _iso_to_unix_nano(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return str(int(parsed.timestamp() * 1_000_000_000))


def _eval_dataset(graph: dict[str, Any]) -> dict[str, Any]:
    cases = []
    for node in _list(graph.get("nodes")):
        if str(node.get("type") or "") != "gate":
            continue
        cases.append(
            {
                "id": str(node.get("id") or ""),
                "input": {"run_id": graph.get("run_id"), "spec_id": graph.get("spec_id")},
                "expected": {"status": str(node.get("status") or "unknown")},
                "metadata": {
                    "gate": str(node.get("label") or node.get("id") or ""),
                    "evidence_hash": str(node.get("hash") or ""),
                },
            }
        )
    return {
        "schema_version": EVAL_DATASET_SCHEMA,
        "provider": "across-orchestrator",
        "cases": cases[:50],
    }


def _parent_span_id(node: dict[str, Any], graph: dict[str, Any]) -> str | None:
    node_id = str(node.get("id") or "")
    if node_id.startswith("run:"):
        return None
    for edge in _list(graph.get("edges")):
        if str(edge.get("to") or "") == node_id:
            return _hex(str(edge.get("from") or ""), 16)
    return None


def _hex(value: str, length: int) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _list(value: Any) -> list[dict[str, Any]]:
    return value if isinstance(value, list) else []
