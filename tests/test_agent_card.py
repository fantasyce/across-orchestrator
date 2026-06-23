import unittest


class AgentCardTests(unittest.TestCase):
    def test_agent_card_describes_protocols_and_boundaries(self):
        from across_orchestrator.agent_card import render_agent_card

        card = render_agent_card()
        self.assertEqual(card["name"], "Across Orchestrator")
        self.assertTrue(card["capabilities"]["taskOrchestration"])
        self.assertTrue(card["capabilities"]["agentLoopRuntime"])
        self.assertTrue(card["capabilities"]["agentLoopV2"])
        self.assertTrue(card["capabilities"]["dynamicLoopPlanning"])
        self.assertTrue(card["capabilities"]["checkpoints"])
        self.assertTrue(card["capabilities"]["humanApproval"])
        self.assertTrue(card["capabilities"]["actionApprovalResume"])
        self.assertTrue(card["capabilities"]["remediationDispatch"])
        self.assertTrue(card["capabilities"]["memoryHooks"])
        self.assertTrue(card["capabilities"]["agentLoopMemoryHooksV2"])
        self.assertTrue(card["capabilities"]["evidenceBundles"])
        self.assertTrue(card["capabilities"]["hostModelDecision"])
        self.assertEqual(card["protocols"]["mcp"]["command"], "across-orchestrator")
        self.assertTrue(card["protocols"]["mcp"]["approveAgentLoopAction"])
        self.assertEqual(card["protocols"]["a2a"]["agentCard"], "/.well-known/agent-card.json")
        self.assertEqual(card["protocols"]["http"]["loopApprove"], "/loops/{loop_id}/actions/{action_id}/approve")
        self.assertEqual(card["protocols"]["http"]["hostModelDecision"], "metadata.model_policy.host_model_command")
        self.assertEqual(card["storage"]["overrideEnv"], "ACROSS_ORCHESTRATOR_HOME")
        self.assertEqual(card["storage"]["defaultHome"], "~/.across/data/across-orchestrator")
        self.assertEqual(card["storage"]["acrossHomeEnv"], "ACROSS_HOME")
        self.assertEqual(card["skills"][0]["id"], "agent-loop-runtime")


if __name__ == "__main__":
    unittest.main()
