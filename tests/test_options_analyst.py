import pytest
from unittest.mock import MagicMock, patch
from langchain_core.messages import AIMessage

from tradingagents.agents.analysts.options_analyst import create_options_analyst
from tradingagents.agents.utils.agent_states import AgentState


class TestOptionsAnalyst:
    def test_create_options_analyst_returns_callable(self):
        mock_llm = MagicMock()
        node = create_options_analyst(mock_llm)
        assert callable(node)

    def test_options_analyst_returns_options_report_key(self):
        mock_llm = MagicMock()
        mock_response = AIMessage(content="Options report here", tool_calls=[])
        mock_chain = MagicMock()
        mock_chain.invoke.return_value = mock_response
        mock_llm.bind_tools.return_value = mock_chain

        with patch("tradingagents.agents.analysts.options_analyst.ChatPromptTemplate") as mock_prompt_cls:
            mock_prompt = MagicMock()
            mock_prompt.partial.return_value = mock_prompt
            mock_prompt.__or__ = MagicMock(return_value=mock_chain)
            mock_prompt_cls.from_messages.return_value = mock_prompt

            node = create_options_analyst(mock_llm)
            state = {
                "trade_date": "2026-03-28",
                "company_of_interest": "SOFI",
                "messages": [],
            }
            result = node(state)

        assert "options_report" in result
        assert "messages" in result

    def test_agent_state_has_options_report_field(self):
        annotations = AgentState.__annotations__
        assert "options_report" in annotations
