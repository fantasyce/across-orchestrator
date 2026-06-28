import unittest


class EvidenceGraphTests(unittest.TestCase):
    def test_builds_graph_from_autopilot_evidence_payload(self):
        from across_orchestrator.evidence_graph import build_evidence_graph_from_payload

        graph = build_evidence_graph_from_payload({
            "schema_version": "across-loop-evidence/1.0",
            "run_id": "run-1",
            "spec_id": "plugin-compatibility-lab-v2",
            "status": "completed",
            "sources": [{"id": "plugin-repository", "status": "passed"}],
            "actions": [{"id": "workflow_pack_export", "adapter": "workflow_pack_export", "status": "passed"}],
            "gates": [{"id": "workflow_pack_exports_ready", "status": "passed"}],
            "outputs": [{"id": "json_artifact", "status": "written"}],
            "memory": {"written": [{"memory_id": "mem-1", "status": "pending"}]},
        })

        self.assertEqual(graph["schema_version"], "across-evidence-graph/1.0")
        self.assertEqual(graph["verified_by"], "across-orchestrator")
        self.assertEqual(graph["summary"]["action_count"], 1)
        self.assertTrue(any(node["id"] == "action:workflow_pack_export" for node in graph["nodes"]))
        self.assertTrue(any(edge["relation"] == "validates" for edge in graph["edges"]))

    def test_existing_graph_is_verified_without_rewriting(self):
        from across_orchestrator.evidence_graph import build_evidence_graph_from_payload

        graph = build_evidence_graph_from_payload({
            "evidence_graph": {
                "schema_version": "across-evidence-graph/1.0",
                "run_id": "run-1",
                "spec_id": "spec-1",
                "nodes": [],
                "edges": [],
            }
        })

        self.assertEqual(graph["schema_version"], "across-evidence-graph/1.0")
        self.assertEqual(graph["verified_by"], "across-orchestrator")


if __name__ == "__main__":
    unittest.main()
