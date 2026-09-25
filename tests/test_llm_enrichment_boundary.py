"""LLM scores must not corrupt the deterministic candidate fallback."""

import socket
from types import SimpleNamespace

import pytest

from tradingagents.strategies.data_sources.registry import DataSourceRegistry
from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def unexpected_network(*args, **kwargs):
        pytest.fail("LLM enrichment boundary tests must not access the network")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_network)
    monkeypatch.setattr(socket.socket, "connect", unexpected_network)
    monkeypatch.setattr(socket.socket, "connect_ex", unexpected_network)


def _enrich(tmp_path, result):
    engine = MultiStrategyEngine(
        config={"autoresearch": {"state_dir": str(tmp_path)}},
        registry=DataSourceRegistry(),
    )
    engine._analyzer = SimpleNamespace(analyze_supply_chain=lambda *args, **kwargs: result)
    candidate = Candidate(
        ticker="DAL", date="2026-09-10", direction="long", score=0.7,
        metadata={"needs_llm_analysis": True, "analysis_type": "supply_chain"},
    )
    enriched = engine._enrich_with_llm([candidate], "supply_chain")
    assert enriched == [candidate]
    return candidate


@pytest.mark.parametrize("field", ["conviction", "score"])
@pytest.mark.parametrize("invalid", [
    "low, normal market regime doesn't support disruption thesis",
    None, True, [], {}, "NaN", "Infinity", -0.1, 1.1,
])
def test_invalid_llm_score_preserves_unmodified_rule_candidate(tmp_path, field, invalid):
    candidate = _enrich(tmp_path, {"direction": "short", field: invalid})
    assert candidate.score == 0.7
    assert candidate.direction == "long"
    assert "llm_analysis" not in candidate.metadata


@pytest.mark.parametrize("invalid", [[{"conviction": 0.9}], "analysis unavailable"])
def test_non_object_analysis_uses_existing_failure_fallback(tmp_path, invalid):
    candidate = _enrich(tmp_path, invalid)
    assert candidate.score == 0.7
    assert candidate.direction == "long"
    assert "llm_analysis" not in candidate.metadata


@pytest.mark.parametrize("field", ["conviction", "score"])
@pytest.mark.parametrize("value", [0, 1, 0.8, "0.8"])
def test_valid_llm_score_is_numeric_in_candidate_and_journal_metadata(tmp_path, field, value):
    result = {"direction": "short", field: value}
    candidate = _enrich(tmp_path, result)
    assert candidate.score == float(value)
    assert candidate.direction == "short"
    assert candidate.metadata["llm_analysis"][field] == float(value)
    assert result[field] == value
