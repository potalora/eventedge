"""Source-native catalyst identity and cutoff provenance regressions."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from tradingagents.strategies.modules import get_paper_trade_strategies
from tradingagents.strategies.orchestration.event_identity import (
    canonical_event_key,
    canonical_observation_time,
)


FAMILIES = [
    (
        "earnings_call",
        {"year": 2026, "quarter": 2, "signal_tier": "backtestable"},
        ("quarter", 3),
    ),
    (
        "insider_activity",
        {
            "cluster_type": "buy_cluster",
            "filings": [{"accession_number": "0001", "owner_name": "A"}],
        },
        ("filings", [{"accession_number": "0002", "owner_name": "A"}]),
    ),
    (
        "filing_analysis",
        {"form_type": "8-K", "file_url": "https://sec.test/a"},
        ("file_url", "https://sec.test/b"),
    ),
    (
        "regulatory_pipeline",
        {"document_id": "RULE-1"},
        ("document_id", "RULE-2"),
    ),
    (
        "supply_chain",
        {"article_id": "NEWS-1", "headline": "Port closure"},
        ("article_id", "NEWS-2"),
    ),
    (
        "litigation",
        {"docket_id": "DOCKET-1", "case_name": "A v B"},
        ("docket_id", "DOCKET-2"),
    ),
    (
        "congressional_trades",
        {
            "trade_keys": ["house:member:2026-07-30:$15k-$50k"],
            "cluster_direction": "long",
        },
        ("trade_keys", ["house:member:2026-07-31:$15k-$50k"]),
    ),
    (
        "govt_contracts",
        {"source": "usaspending", "award_id": "AWARD-1"},
        ("award_id", "AWARD-2"),
    ),
    (
        "state_economics",
        {
            "source_observation_ids": ["FRED:UNRATE:2026-07-30"],
            "window_end": "2026-07-31",
            "regional_sector": "retail",
        },
        ("source_observation_ids", ["FRED:UNRATE:2026-08-03"]),
    ),
    (
        "weather_ag",
        {
            "source_observation_ids": ["NOAA:2026-07-31", "USDA:2026-W31"],
            "window_end": "2026-07-31",
            "commodity": "corn",
        },
        ("source_observation_ids", ["NOAA:2026-08-03", "USDA:2026-W32"]),
    ),
    (
        "commodity_macro",
        {
            "report_id": "CFTC:gold:2026-W31",
            "window_end": "2026-07-31",
            "commodity": "gold",
        },
        ("report_id", "CFTC:gold:2026-W32"),
    ),
    (
        "quantum_readiness",
        {"source_ids": ["filing:1", "news:1"], "basket": "pqc_vendor"},
        ("source_ids", ["filing:1", "news:2"]),
    ),
]


@pytest.mark.parametrize(("strategy", "metadata", "mutation"), FAMILIES)
def test_source_family_identity_ignores_score_llm_and_screen_session(
    strategy, metadata, mutation
):
    baseline = canonical_event_key(strategy, "AAPL", metadata, date(2026, 7, 31))
    enriched = deepcopy(metadata)
    enriched.update(
        {
            "score": 99.0,
            "llm_analysis": {"conviction": 0.01, "rationale": "changed"},
            "sector": "changed enrichment",
            "signal_tier": "changed derived tier",
            "analysis_type": "changed derived analysis",
            "regime": "changed derived regime",
        }
    )
    assert canonical_event_key(strategy, "AAPL", enriched, date(2026, 8, 3)) == baseline

    distinct = deepcopy(metadata)
    distinct[mutation[0]] = mutation[1]
    assert (
        canonical_event_key(strategy, "AAPL", distinct, date(2026, 7, 31)) != baseline
    )


@pytest.mark.parametrize("strategy", [item[0] for item in FAMILIES])
def test_source_family_identity_fails_closed_without_native_source_key(strategy):
    with pytest.raises(ValueError, match="source identity"):
        canonical_event_key(strategy, "AAPL", {}, date(2026, 7, 31))


def test_supply_chain_fallback_requires_source_headline_and_publication_time():
    with pytest.raises(ValueError, match="published_at"):
        canonical_event_key(
            "supply_chain",
            "AAPL",
            {"source": "Finnhub", "headline": "Port closure"},
            date(2026, 7, 31),
        )


def test_congressional_transaction_date_cannot_prove_source_availability():
    with pytest.raises(ValueError, match="observation time"):
        canonical_observation_time(
            "congressional_trades",
            {
                "trade_keys": ["disclosure-1"],
                "transaction_date": "2026-06-25",
            },
        )
    observed = canonical_observation_time(
        "congressional_trades",
        {
            "trade_keys": ["disclosure-1"],
            "transaction_date": "2026-06-25",
            "publication_date": "2026-07-01",
        },
    )
    assert observed.date() == date(2026, 7, 1)


def test_active_observation_uses_latest_caller_and_native_availability():
    observed = canonical_observation_time(
        "earnings_call",
        {
            "observed_at": "2026-07-31T19:00:00+00:00",
            "published_at": "2026-07-31T20:30:00+00:00",
        },
    )

    assert observed == datetime(2026, 7, 31, 20, 30, tzinfo=timezone.utc)


def test_all_active_strategy_outputs_carry_usable_source_native_identity():
    sessions = pd.bdate_range("2026-05-01", periods=45)

    def prices(*tickers: str) -> dict:
        return {
            ticker: pd.DataFrame(
                {"Close": [100.0 + offset for offset in range(len(sessions))]},
                index=sessions,
            )
            for ticker in tickers
        }

    cases = {
        "earnings_call": {
            "finnhub": {
                "transcripts": [
                    {
                        "symbol": "AAPL",
                        "year": 2026,
                        "quarter": 2,
                        "eps_actual": 2.0,
                        "eps_estimate": 1.5,
                        "transcript_text": "Demand improved.",
                        "published_at": "2026-07-01T12:00:00+00:00",
                    }
                ]
            }
        },
        "insider_activity": {
            "edgar": {
                "form4": {
                    "AAPL": [
                        {
                            "accession_number": f"FORM4-{ordinal}",
                            "transaction_type": "buy",
                            "owner_name": f"Owner {ordinal}",
                            "shares": 10,
                            "price_per_share": 100,
                            "filing_date": "2026-06-30",
                        }
                        for ordinal in range(3)
                    ]
                }
            }
        },
        "filing_analysis": {
            "edgar": {
                "filings": [
                    {
                        "ticker": "AAPL",
                        "form_type": "8-K",
                        "entity_name": "Apple Inc.",
                        "file_date": "2026-06-30",
                        "adsh": "EDGAR-8K-1",
                        "current_text": "Material agreement.",
                    }
                ]
            }
        },
        "regulatory_pipeline": {
            "regulations": {
                "proposed_rules": [
                    {
                        "document_id": "RULE-1",
                        "title": "New standard",
                        "agency_id": "SEC",
                        "posted_date": "2026-06-30",
                    }
                ]
            }
        },
        "supply_chain": {
            "finnhub": {
                "disruption_news": [
                    {
                        "symbol": "AAPL",
                        "headline": "Factory disruption closes port",
                        "summary": "shortage",
                        "url": "https://example.test/news/1",
                        "published_at": "2026-07-01T12:00:00+00:00",
                    }
                ]
            }
        },
        "litigation": {
            "courtlistener": {
                "dockets": [
                    {
                        "docket_id": "DOCKET-1",
                        "case_name": "Securities class action",
                        "nature_of_suit": "securities",
                        "date_filed": "2026-06-30",
                    }
                ]
            }
        },
        "congressional_trades": {
            "congress": {
                "recent_trades": [
                    {
                        "ticker": "AAPL",
                        "transaction_type": "purchase",
                        "amount": "$15,001 - $50,000",
                        "representative": "Member One",
                        "chamber": "house",
                        "transaction_date": "2026-06-25",
                        "source_url": "https://example.test/disclosure/1",
                        "publication_date": "2026-06-30",
                    },
                    {
                        "ticker": "AAPL",
                        "transaction_type": "purchase",
                        "amount": "$15,001 - $50,000",
                        "representative": "Member Two",
                        "chamber": "house",
                        "transaction_date": "2026-06-25",
                        "source_url": "https://example.test/disclosure/2",
                        "publication_date": "2026-06-30",
                    },
                ]
            }
        },
        "govt_contracts": {
            "usaspending": {
                "data": {
                    "contracts": [
                        {
                            "recipient_name": "Lockheed Martin",
                            "amount": 50_000_000,
                            "award_id": "AWARD-1",
                            "last_modified_date": "2026-06-30",
                        }
                    ]
                }
            }
        },
        "state_economics": {
            "yfinance": {"prices": prices("KRE", "IWN")},
            "fred": {
                "UNRATE": pd.Series(
                    [4.1, 4.0], index=pd.to_datetime(["2026-05-01", "2026-06-01"])
                ),
                "ICSA": pd.Series(
                    [220000, 210000], index=pd.to_datetime(["2026-06-20", "2026-06-27"])
                ),
            },
        },
        "weather_ag": {
            "yfinance": {"prices": prices("CORN", "WEAT", "SOYB")},
            "drought_monitor": {
                "composite_score": 2.0,
                "report_date": "2026-06-30",
            },
        },
        "commodity_macro": {
            "cftc": {
                "gold": {
                    "percentile": 0.9,
                    "direction_signal": "short",
                    "net_position": 100000,
                    "report_id": "CFTC:088691:2026-06-26",
                    "window_end": "2026-06-26",
                }
            }
        },
        "quantum_readiness": {
            "finnhub": {
                "pqc_news": [
                    {
                        "symbol": "CRWD",
                        "headline": "Quantum milestone and PQC deadline",
                        "summary": "quantum-safe migration",
                        "url": "https://example.test/pqc/1",
                        "published_at": "2026-07-01T12:00:00+00:00",
                    }
                ]
            }
        },
    }

    strategies = {item.name: item for item in get_paper_trade_strategies()}
    assert set(cases) == set(strategies)
    for name, strategy in strategies.items():
        horizon = "3m" if name == "commodity_macro" else "30d"
        candidates = strategy.screen(
            cases[name], "2026-07-01", strategy.get_default_params(horizon)
        )
        assert candidates, f"representative {name} source event produced no candidate"
        for candidate in candidates:
            assert canonical_event_key(
                name,
                candidate.ticker or "AAPL",
                candidate.metadata,
                date(2026, 7, 1),
            )
            assert canonical_observation_time(name, candidate.metadata)
