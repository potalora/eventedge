"""Exercise both real analysis callers against the OpenAI Responses contract."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.learning.llm_analyzer import LLMAnalyzer
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee


@pytest.fixture
def luna_config():
    return {"autoresearch": {"autoresearch_model": "gpt-6-luna", "llm_effort": "high"}}


def response(text='{"direction":"neutral","conviction":0.0}', status="completed"):
    return SimpleNamespace(status=status, output_text=text, output=[])


@pytest.mark.parametrize("kind", ["analyzer", "committee"])
def test_luna_uses_responses_high_without_anthropic_or_sampling(luna_config, kind):
    obj = (
        LLMAnalyzer(luna_config)
        if kind == "analyzer"
        else PortfolioCommittee(luna_config)
    )
    client = MagicMock()
    client.responses.create.return_value = response()
    obj._client = client
    actual = (
        obj._call_llm("system", "event")
        if kind == "analyzer"
        else obj._call_llm(system="system", prompt="event")
    )
    assert actual == response().output_text
    assert client.responses.create.call_count == 1
    client.messages.create.assert_not_called()
    kwargs = client.responses.create.call_args.kwargs
    assert kwargs["model"] == "gpt-6-luna"
    assert kwargs["reasoning"] == {"effort": "high"}
    assert kwargs["instructions"] == "system"
    assert kwargs["input"] == "event"
    assert kwargs["store"] is False
    assert kwargs["max_output_tokens"] >= 16384
    assert not ({"temperature", "top_p", "top_k", "max_tokens"} & kwargs.keys())


@pytest.mark.parametrize(
    "status,text",
    [("incomplete", '{"conviction":0.8'), ("failed", "{}"), ("completed", "")],
)
def test_incomplete_or_empty_luna_analysis_is_not_parsed(luna_config, status, text):
    client = MagicMock()
    client.responses.create.return_value = response(text, status)
    analyzer = LLMAnalyzer(luna_config)
    analyzer._client = client
    assert analyzer.analyze_filing_change("current", "prior", "IBM") == {}
    committee = PortfolioCommittee(luna_config)
    committee._client = client
    with pytest.raises(RuntimeError):
        committee._call_llm(system="system", prompt="event")


def test_committee_model_override_keeps_claude_route(luna_config):
    config = deepcopy(luna_config)
    config["autoresearch"]["paper_trade"] = {
        "portfolio_committee_model": "claude-sonnet-5"
    }
    committee = PortfolioCommittee(config)
    client = MagicMock()
    client.messages.create.return_value.content = [
        SimpleNamespace(type="text", text="[]")
    ]
    committee._client = client
    assert committee._call_llm(system="system", prompt="event") == "[]"
    client.responses.create.assert_not_called()
    assert client.messages.create.call_args.kwargs["model"] == "claude-sonnet-5"


def test_defaults_separate_bounded_luna_from_frontier_thesis_and_committee():
    assert DEFAULT_CONFIG["autoresearch"]["autoresearch_model"] == "gpt-6-luna"
    assert DEFAULT_CONFIG["autoresearch"]["llm_effort"] == "high"
    assert LLMAnalyzer(DEFAULT_CONFIG)._model_name == "gpt-6-luna"
    assert DEFAULT_CONFIG["autoresearch"]["thesis_model"] == "gpt-6-astra"
    assert DEFAULT_CONFIG["autoresearch"]["thesis_effort"] == "high"
    assert PortfolioCommittee(DEFAULT_CONFIG)._model_name == "gpt-6-astra"


@pytest.mark.parametrize("factory", [LLMAnalyzer, PortfolioCommittee])
def test_luna_initializes_openai_with_bounded_timeout(factory, luna_config):
    with patch("openai.OpenAI") as constructor:
        obj = factory(luna_config)
        assert obj._get_client() is constructor.return_value
        assert obj._get_client() is constructor.return_value
        constructor.assert_called_once()
        assert constructor.call_args.kwargs["max_retries"] == 0
        assert constructor.call_args.kwargs["timeout"].read == 120
        assert constructor.call_args.kwargs["timeout"].connect == 10


def test_invalid_reasoning_effort_makes_no_provider_call(luna_config):
    luna_config["autoresearch"]["llm_effort"] = "typo"
    committee = PortfolioCommittee(luna_config)
    committee._client = MagicMock()
    with pytest.raises(ValueError, match="reasoning effort"):
        committee._call_llm(system="system", prompt="event")
    committee._client.responses.create.assert_not_called()


def test_refusal_is_rejected_before_json_parsing(luna_config):
    committee = PortfolioCommittee(luna_config)
    client = MagicMock()
    refused = response("{}")
    refused.output = [SimpleNamespace(content=[SimpleNamespace(type="refusal")])]
    client.responses.create.return_value = refused
    committee._client = client
    with pytest.raises(RuntimeError, match="refused"):
        committee._call_llm(system="system", prompt="event")
