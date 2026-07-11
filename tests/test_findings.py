import copy
import unittest


class FindingCompatibilityTests(unittest.TestCase):
    def test_normalize_finding_matches_autopilot_aliases_and_required_shape(self):
        from across_orchestrator.findings import FINDING_SCHEMA_VERSION, normalize_finding

        finding = normalize_finding({
            "code": "lint-console",
            "status": "error",
            "message": "Console statement left in production path",
            "level": "warning",
            "path": "src/cli.js",
            "line": "12",
            "refs": ["run:123", "", "gate:lint"],
            "sourceGate": "candidate_quality",
            "owner": "across-autopilot",
            "remediation": "Remove the statement before promotion.",
            "repairRound": "2",
            "evidence": {"command": "npm test", "status": "failed"},
            "metadata": {"command": "npm test", "ignored": None},
        })

        required = {
            "schema_version",
            "id",
            "state",
            "severity",
            "summary",
            "evidence",
            "suggested_action",
            "owner",
            "repair_round",
            "source_gate",
        }
        self.assertTrue(required.issubset(finding))
        self.assertEqual(finding["schema_version"], FINDING_SCHEMA_VERSION)
        self.assertEqual(finding["id"], "lint-console")
        self.assertEqual(finding["state"], "failed")
        self.assertEqual(finding["file"], "src/cli.js")
        self.assertEqual(finding["line"], 12)
        self.assertEqual(finding["repair_round"], 2)
        self.assertEqual(finding["evidence_refs"], ["gate:lint", "run:123"])
        self.assertEqual(finding["evidence"], {"command": "npm test", "status": "failed"})
        self.assertEqual(finding["metadata"], {"command": "npm test"})

    def test_legacy_quality_report_preserves_evidence_and_derives_failed_gates(self):
        from across_orchestrator.findings import normalize_quality_report

        report = {
            "status": "failed",
            "score_breakdown": {"lint": "failed", "browser_e2e": "skipped"},
            "gate_results": [
                {
                    "gate_id": "gate-lint",
                    "adapter_id": "lint",
                    "status": "failed",
                    "required": True,
                    "evidence": {"command": "npm run lint", "exit_code": 1},
                },
                {
                    "gate_id": "gate-browser-e2e",
                    "adapter_id": "browser_e2e",
                    "status": "skipped",
                    "required": True,
                    "evidence": {"reason": "browser unavailable"},
                },
            ],
        }
        original = copy.deepcopy(report)

        normalized = normalize_quality_report(
            report,
            finding_id="legacy_quality",
            source_gate="legacy_quality",
            repair_round=3,
        )

        self.assertEqual(report, original)
        self.assertEqual(normalized["gate_results"], original["gate_results"])
        self.assertEqual(normalized["finding_state"], "failed")
        self.assertEqual(normalized["failed_gates"], ["lint"])
        self.assertEqual(normalized["source_gates"], ["browser_e2e", "lint"])
        self.assertEqual(normalized["findings"], normalized["normalized_findings"])
        lint = next(item for item in normalized["findings"] if item["id"] == "lint")
        browser = next(item for item in normalized["findings"] if item["id"] == "browser_e2e")
        self.assertEqual(lint["repair_round"], 3)
        self.assertEqual(lint["evidence"]["exit_code"], 1)
        self.assertEqual(browser["state"], "no_op")

    def test_finding_state_overrides_contradictory_legacy_passed_boolean(self):
        from across_orchestrator.findings import normalize_quality_report, quality_report_passed

        normalized = normalize_quality_report(
            {
                "quality": "unknown",
                "passed": True,
                "findings": [{
                    "id": "browser_e2e",
                    "state": "blocked",
                    "summary": "Browser E2E cannot run.",
                    "source_gate": "browser_e2e",
                }],
            },
            finding_id="quality_gate",
            source_gate="quality_gate",
        )

        self.assertEqual(normalized["finding_state"], "blocked")
        self.assertEqual(normalized["failed_gates"], ["browser_e2e"])
        self.assertFalse(quality_report_passed(normalized))


if __name__ == "__main__":
    unittest.main()
