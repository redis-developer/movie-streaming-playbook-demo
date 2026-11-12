"""Checks for the evidence gate; live tests separately exercise OpenAI and Playbook."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from backend.support_agent import SupportAgent, Trace


class CustomerConfirmationTests(unittest.TestCase):
    def agent(self, confirmed=True):
        agent = SupportAgent.__new__(SupportAgent)
        agent.redis = Mock()
        agent.redis.get.return_value = b"ses_previous"
        agent.redis.eval.return_value = 1
        agent.sessions = Mock()
        agent.sessions.get.return_value = {
            "session_id": "ses_previous", "question": "How do I enable parental controls?",
            "answer": "Select the child profile and set viewing restrictions.",
            "answered_by": "rag", "proposal_id": "",
        }
        agent.openai = Mock()
        agent.openai.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content='{"confirmed": ' + str(confirmed).lower() + '}'))])
        agent.resolve_conversation = Mock(return_value={
            "session": {"session_id": "ses_previous"},
            "worker": {"status": "waiting", "proposed": False, "clusterSize": 1, "needed": 3},
        })
        return agent

    def test_explicit_confirmation_captures_original_answer_and_customer_signal(self):
        agent = self.agent()
        result = agent._confirmed_resolution("That worked", "alice", "chat-a", Trace())
        self.assertEqual(result["worker"]["clusterSize"], 1)
        agent.resolve_conversation.assert_called_once_with(
            "ses_previous", "Select the child profile and set viewing restrictions.",
            source="customer-confirmation", confirmation="That worked")

    def test_negative_classifier_result_does_not_create_evidence(self):
        agent = self.agent(confirmed=False)
        self.assertIsNone(agent._confirmed_resolution("It still fails", "alice", "chat-a", Trace()))
        agent.resolve_conversation.assert_not_called()
        agent.redis.eval.assert_not_called()

    def test_missing_conversation_context_does_not_call_classifier(self):
        agent = self.agent()
        agent.redis.get.return_value = None
        self.assertIsNone(agent._confirmed_resolution("That worked", "bob", "chat-b", Trace()))
        agent.openai.chat.completions.create.assert_not_called()

    def test_classifier_failure_does_not_fabricate_confirmation(self):
        agent = self.agent()
        agent.openai.chat.completions.create.side_effect = RuntimeError("model unavailable")
        self.assertIsNone(agent._confirmed_resolution("That worked", "alice", "chat-a", Trace()))
        agent.resolve_conversation.assert_not_called()

    def test_concurrent_duplicate_cannot_claim_same_answer_twice(self):
        agent = self.agent()
        agent.redis.eval.return_value = 0
        self.assertIsNone(agent._confirmed_resolution("That worked", "alice", "chat-a", Trace()))
        agent.resolve_conversation.assert_not_called()

    def test_owner_and_memory_session_both_scope_pending_answer(self):
        self.assertNotEqual(SupportAgent._conversation_key("alice", "chat-a"),
                            SupportAgent._conversation_key("bob", "chat-a"))
        self.assertNotEqual(SupportAgent._conversation_key("alice", "chat-a"),
                            SupportAgent._conversation_key("alice", "chat-b"))


if __name__ == "__main__":
    unittest.main()
