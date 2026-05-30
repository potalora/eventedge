"""LLM calls must be deterministic (temperature=0) by default.

Determinism is what makes generation comparisons apples-to-apples: the same
signals/data give the same committee decisions and the same enrichment, so a
difference between generations reflects code, not sampling noise.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee


def _fake_client(text="ok"):
    client = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
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
