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
    committee = PortfolioCommittee(
        config={
            "autoresearch": {
                "llm_billing_mode": "api",
                "paper_trade": {"portfolio_committee_model": "claude-haiku-4-5-20251001"},
            }
        }
    )
    client = _fake_client("[]")
    with patch.object(PortfolioCommittee, "_get_client", return_value=client):
        committee._call_llm(system="sys", prompt="hi")
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["temperature"] == 0.0


def test_committee_temperature_is_configurable():
    committee = PortfolioCommittee(
        config={
            "autoresearch": {
                "llm_billing_mode": "api",
                "paper_trade": {"portfolio_committee_model": "claude-haiku-4-5-20251001"},
                "llm_temperature": 0.4,
            }
        }
    )
    client = _fake_client("[]")
    with patch.object(PortfolioCommittee, "_get_client", return_value=client):
        committee._call_llm(system="sys", prompt="hi")
    assert client.messages.create.call_args.kwargs["temperature"] == 0.4


def test_analyzer_call_uses_temperature_zero_by_default():
    analyzer = LLMAnalyzer(config={"autoresearch": {"llm_billing_mode": "api", "autoresearch_model": "claude-haiku-4-5-20251001"}})
    client = _fake_client("ok")
    with patch.object(LLMAnalyzer, "_get_client", return_value=client):
        analyzer._call_llm("sys", "user")
    assert client.messages.create.call_args.kwargs["temperature"] == 0.0
