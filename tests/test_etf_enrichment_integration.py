"""Exercise the strategy -> role-routed response -> SEC registry boundary offline."""

import json
from copy import deepcopy
import socket
from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.data_sources.edgar_source import EDGARSource
from tradingagents.strategies.data_sources.registry import DataSourceRegistry
from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.modules.weather_ag import WeatherAgStrategy
from tradingagents.strategies.orchestration.multi_strategy_engine import (
    MultiStrategyEngine,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Unexpected network request in integration fixture")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected)
    monkeypatch.setattr(socket.socket, "connect", unexpected)


def engine_with_company_registry(tmp_path, output, expected_model="gpt-6-luna"):
    config = deepcopy(DEFAULT_CONFIG)
    config["autoresearch"]["state_dir"] = str(tmp_path)
    edgar = EDGARSource()
    # Same company-only schema as the real SEC endpoint: an ETF need not occur.
    edgar._session_cache["_company_tickers"] = {
        "0": {"ticker": "AAPL", "title": "Apple Inc.", "cik_str": 320193},
    }
    registry = DataSourceRegistry()
    registry.register(edgar)
    engine = MultiStrategyEngine(config=config, registry=registry, use_llm=True)

    def responses_create(**request):
        assert request["model"] == expected_model
        assert request["reasoning"] == {"effort": "high"}
        return SimpleNamespace(status="completed", output_text=output, output=[])

    engine._analyzer._client = SimpleNamespace(
        responses=SimpleNamespace(create=responses_create),
    )
    return engine


def test_deterministic_weather_etf_survives_luna_and_company_only_registry(tmp_path):
    engine = engine_with_company_registry(
        tmp_path,
        '{"direction":"long","score":0.8,"reasoning":"Fixture drought signal"}',
    )
    engine.paper_trade_strategies = [WeatherAgStrategy()]
    prices = pd.DataFrame(
        {"Close": [50 + i / 10 for i in range(25)]},
        index=pd.bdate_range(end="2026-10-01", periods=25),
    )
    data = {
        "yfinance": {"prices": {"MOO": prices}},
        "noaa": {"heat_stress_days": 0, "precip_deficit_pct": 0, "frost_events": 0, "available_at": "2026-10-01T12:00:00+00:00", "coverage": {"complete": True}},
        "usda": {"crop_progress": {"CORN": [{"week_ending": "2026-09-27", "state": "IA", "good_pct": 50, "excellent_pct": 20}]}, "available_at": "2026-10-01T12:00:00+00:00"},
        "drought_monitor": {"composite_score": 1.2, "states": {"IA": {"D2": 40, "D3": 0, "D4": 0}}, "available_at": "2026-10-01T12:00:00+00:00"},
    }
    signals, _, health = engine.screen_and_enrich(
        "2026-10-01",
        data,
        epoch_id="fixture-epoch",
        policy_id="fixture-policy",
    )
    assert [(s["ticker"], s["direction"], s["score"]) for s in signals] == [
        ("MOO", "long", 0.8)
    ]
    assert len(health) == 1
    assert health[0].status == "signals"
    assert (
        signals[0]["metadata"]["llm_analysis"]["reasoning"] == "Fixture drought signal"
    )


@pytest.mark.parametrize("field", ["affected_tickers", "defendant_ticker"])
@pytest.mark.parametrize("model_ticker,expected", [("AAPL", "AAPL"), ("BOGUS", "")])
def test_new_model_resolved_company_still_requires_sec_identity(
    tmp_path, model_ticker, expected, field
):
    engine = engine_with_company_registry(
        tmp_path,
        json.dumps(
            {
                "direction": "long", "rationale": "Source event affects issuer",
                "score": 0.8,
                field: [model_ticker] if field == "affected_tickers" else model_ticker,
            }
        ),
        expected_model="gpt-6-astra",
    )
    candidate = Candidate(
        ticker="",
        date="2026-10-01",
        direction="long",
        score=0.5,
        metadata={
            "needs_llm_analysis": True, "title": "SEC filing requirement", "case_name": "Company suit",
            "analysis_type": "litigation"
            if field == "defendant_ticker"
            else "regulation",
        },
    )
    enriched = engine._enrich_with_llm([candidate], "regulatory_pipeline")
    assert [c.ticker for c in enriched] == [expected]
