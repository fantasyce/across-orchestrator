import unittest


class ProtocolGatewayTests(unittest.TestCase):
    def test_protocol_gateway_matrix_is_complete_and_secret_free(self):
        from across_orchestrator.protocol_gateway import render_protocol_gateway

        gateway = render_protocol_gateway()

        self.assertEqual(gateway["schema_version"], "across-orchestrator-protocol-gateway/1.0")
        self.assertEqual(gateway["owner"], "across-orchestrator")
        self.assertEqual(gateway["status"], "passed")
        self.assertEqual(gateway["summary"]["route_count"], 6)
        self.assertEqual(gateway["summary"]["ready_route_count"], 6)
        self.assertFalse(gateway["security"]["secrets_included"])
        self.assertTrue(gateway["security"]["credentials_stay_with_host"])
        self.assertIn("host_model_decision", {route["id"] for route in gateway["routes"]})
        self.assertIn("autopilot_metadata", {route["id"] for route in gateway["routes"]})
        self.assertIn("external_agent_plugins", {route["id"] for route in gateway["routes"]})


if __name__ == "__main__":
    unittest.main()
