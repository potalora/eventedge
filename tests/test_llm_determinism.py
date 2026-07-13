"""Model-specific autoresearch request behavior.

Older Claude models retain temperature-zero requests. Sonnet 5 does not accept
non-default sampling controls, so it uses adaptive thinking with an explicit
effort budget instead.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee


def _fake_client(text="ok"):
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(type="text", text=text)]
    client.messages.create.return_value = msg
    return client


def test_committee_call_uses_temperature_zero_by_default():
    committee = PortfolioCommittee(config={})
    client = _fake_client("[]")
    with patch.object(PortfolioCommittee, "_get_client", return_value=client):
        committee._call_llm(system="sys", prompt="hi")
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["temperature"] == 0.0


def test_committee_temperature_is_configurable():
    committee = PortfolioCommittee(config={"autoresearch": {"llm_temperature": 0.4}})
    client = _fake_client("[]")
    with patch.object(PortfolioCommittee, "_get_client", return_value=client):
        committee._call_llm(system="sys", prompt="hi")
    assert client.messages.create.call_args.kwargs["temperature"] == 0.4


def test_analyzer_call_uses_temperature_zero_by_default():
    analyzer = LLMAnalyzer(config={})
    client = _fake_client("ok")
    with patch.object(LLMAnalyzer, "_get_client", return_value=client):
        analyzer._call_llm("sys", "user")
    assert client.messages.create.call_args.kwargs["temperature"] == 0.0


def test_sonnet_5_uses_effort_and_omits_sampling_parameters():
    config = {
        "autoresearch": {
            "autoresearch_model": "claude-sonnet-5",
            "llm_effort": "medium",
            "llm_temperature": 0.0,
        }
    }
    committee = PortfolioCommittee(config=config)
    client = _fake_client("[]")
    with patch.object(PortfolioCommittee, "_get_client", return_value=client):
        committee._call_llm(system="sys", prompt="hi")
    kwargs = client.messages.create.call_args.kwargs
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert "top_k" not in kwargs
    assert kwargs["output_config"] == {"effort": "medium"}


def test_analyzer_extracts_text_after_adaptive_thinking_block():
    config = {
        "autoresearch": {
            "autoresearch_model": "claude-sonnet-5",
            "llm_effort": "medium",
        }
    }
    analyzer = LLMAnalyzer(config=config)
    client = MagicMock()
    client.messages.create.return_value.content = [
        MagicMock(type="thinking", thinking=""),
        MagicMock(type="text", text='{"direction":"neutral"}'),
    ]
    with patch.object(LLMAnalyzer, "_get_client", return_value=client):
        text = analyzer._call_llm("sys", "user")
    assert text == '{"direction":"neutral"}'
    assert client.messages.create.call_args.kwargs["output_config"] == {
        "effort": "medium"
    }
