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
        metadata={"needs_llm_analysis": True, "analysis_type": "supply_chain", "headline": "Factory closes"},
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
    assert candidate.journal_only


@pytest.mark.parametrize("invalid", [[{"conviction": 0.9}], "analysis unavailable"])
def test_non_object_analysis_uses_existing_failure_fallback(tmp_path, invalid):
    candidate = _enrich(tmp_path, invalid)
    assert candidate.score == 0.7
    assert candidate.direction == "long"
    assert "llm_analysis" not in candidate.metadata
    assert candidate.journal_only


@pytest.mark.parametrize("field", ["conviction", "score"])
@pytest.mark.parametrize("value", [0, 1, 0.8, "0.8"])
def test_valid_llm_score_is_numeric_in_candidate_and_journal_metadata(tmp_path, field, value):
    result = {"direction": "short", field: value, "rationale": "Factory closes"}
    candidate = _enrich(tmp_path, result)
    assert candidate.score == float(value)
    assert candidate.direction == "short"
    assert candidate.metadata["llm_analysis"][field] == float(value)
    assert result[field] == value


@pytest.mark.parametrize("notable,valid", [
    ([{"name": "Fixture Insider", "title": "CEO"}], False),
    (["Fixture Insider, CEO"], True),
])
def test_native_insider_array_shape_keeps_string_schema_enforced(tmp_path, notable, valid):
    engine = MultiStrategyEngine(
        config={"autoresearch": {"state_dir": str(tmp_path)}}, registry=DataSourceRegistry(),
    )
    engine._analyzer = SimpleNamespace(analyze_insider_context=lambda *args, **kwargs: {
        "direction": "long", "conviction": .8, "rationale": "Retained purchase",
        "notable_insiders": notable,
    })
    candidate = Candidate(ticker="AAPL", date="2026-03-30", score=.7, metadata={
        "needs_llm_analysis": True, "analysis_type": "insider_activity", "cluster_type": "buy_cluster",
        "filings": [{"owner_name": "Fixture Insider", "owner_title": "CEO"}],
    })
    result = engine._enrich_with_llm([candidate], "insider_activity")[0]
    if valid:
        assert result.metadata["analysis_status"] == "validated"
        assert result.metadata["llm_analysis"]["notable_insiders"] == notable
    else:
        assert result.metadata["analysis_status"] == "failed"
        assert result.metadata["analysis_failure_reason"] == "invalid_notable_insiders"
        assert result.journal_only and result.score == .7
        assert "llm_analysis" not in result.metadata
