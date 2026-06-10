import unittest


class AgentCardTests(unittest.TestCase):
    def test_agent_card_describes_protocols_and_boundaries(self):
        from across_orchestrator.agent_card import render_agent_card

        card = render_agent_card()
        self.assertEqual(card["name"], "Across Orchestrator")
        self.assertTrue(card["capabilities"]["taskOrchestration"])
        self.assertTrue(card["capabilities"]["agentLoopRuntime"])
        self.assertTrue(card["capabilities"]["checkpoints"])
        self.assertTrue(card["capabilities"]["humanApproval"])
        self.assertTrue(card["capabilities"]["memoryHooks"])
        self.assertTrue(card["capabilities"]["evidenceBundles"])
        self.assertEqual(card["protocols"]["mcp"]["command"], "across-orchestrator")
        self.assertEqual(card["protocols"]["a2a"]["agentCard"], "/.well-known/agent-card.json")
        self.assertEqual(card["storage"]["overrideEnv"], "ACROSS_ORCHESTRATOR_HOME")
        self.assertEqual(card["storage"]["defaultHome"], "~/.across/data/across-orchestrator")
        self.assertEqual(card["storage"]["acrossHomeEnv"], "ACROSS_HOME")
        self.assertEqual(card["skills"][0]["id"], "agent-loop-runtime")


if __name__ == "__main__":
    unittest.main()
