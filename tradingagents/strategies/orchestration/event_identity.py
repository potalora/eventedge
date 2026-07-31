"""Source-native event identities for the 12 active paper strategies."""

from __future__ import annotations

from datetime import date
from typing import Any

from tradingagents.strategies.execution import stable_id


def _present(value: object) -> bool:
    return value not in (None, "", [], {})


def _required(metadata: dict[str, Any], key: str, strategy: str) -> object:
    value = metadata.get(key)
    if not _present(value):
        raise ValueError(f"{strategy} candidate lacks source identity field {key}")
    return value


def _insider_filing_key(filing: dict[str, Any]) -> object:
    for key in ("accession_number", "accession_no", "filing_id", "id"):
        if _present(filing.get(key)):
            return (key, filing[key])
    identity = {
        key: filing.get(key)
        for key in (
            "owner_name",
            "filing_date",
            "transaction_date",
            "transaction_code",
            "transaction_type",
            "shares",
            "price_per_share",
        )
        if _present(filing.get(key))
    }
    if not any(key in identity for key in ("filing_date", "transaction_date")):
        raise ValueError("insider_activity filing lacks source identity")
    return identity


def canonical_event_key(
    strategy: str,
    ticker: str,
    metadata: dict[str, Any],
    reference_session: date,
) -> str:
    """Build an immutable source-family key; never use score, LLM, or screen date."""
    if not strategy or not ticker or not isinstance(reference_session, date):
        raise ValueError("candidate source identity context is incomplete")
    family: str
    payload: object

    if strategy == "earnings_call":
        family = "fiscal_period"
        payload = {
            "year": _required(metadata, "year", strategy),
            "quarter": _required(metadata, "quarter", strategy),
        }
    elif strategy == "insider_activity":
        family = "form4_cluster"
        filing_keys = metadata.get("filing_keys")
        if _present(filing_keys):
            if not isinstance(filing_keys, list):
                raise ValueError("insider_activity candidate lacks source identity")
            identities = sorted(str(item) for item in filing_keys)
        else:
            filings = _required(metadata, "filings", strategy)
            if not isinstance(filings, list):
                raise ValueError(
                    "insider_activity candidate lacks source identity filings"
                )
            identities = sorted(
                (_insider_filing_key(filing) for filing in filings), key=str
            )
        payload = {
            "cluster_type": _required(metadata, "cluster_type", strategy),
            "filings": identities,
        }
    elif strategy == "filing_analysis":
        family = "sec_filing"
        locator = metadata.get("accession_number") or metadata.get("file_url")
        if not _present(locator):
            locator = {
                "entity_name": _required(metadata, "entity_name", strategy),
                "form_type": _required(metadata, "form_type", strategy),
                "file_date": _required(metadata, "file_date", strategy),
            }
        payload = {"locator": locator}
    elif strategy == "regulatory_pipeline":
        family = "regulations_document"
        payload = _required(metadata, "document_id", strategy)
    elif strategy == "supply_chain":
        family = "news_article"
        locator = metadata.get("article_id") or metadata.get("url")
        if not _present(locator):
            locator = {
                "source": _required(metadata, "source", strategy),
                "headline": _required(metadata, "headline", strategy),
                "published_at": _required(metadata, "published_at", strategy),
            }
        payload = locator
    elif strategy == "litigation":
        family = "legal_matter"
        locator = metadata.get("docket_id") or metadata.get("url")
        if not _present(locator):
            locator = {
                "release_date": _required(metadata, "release_date", strategy),
                "title": _required(metadata, "title", strategy),
            }
        payload = locator
    elif strategy == "congressional_trades":
        family = "disclosure_cluster"
        trade_keys = _required(metadata, "trade_keys", strategy)
        if not isinstance(trade_keys, list):
            raise ValueError("congressional_trades candidate lacks source identity")
        payload = sorted(str(item) for item in trade_keys)
    elif strategy == "govt_contracts":
        source = _required(metadata, "source", strategy)
        if source == "usaspending":
            family = "federal_award"
            payload = _required(metadata, "award_id", strategy)
        elif source == "momentum_fallback":
            family = "daily_contract_proxy_state"
            payload = {
                "observation_date": _required(metadata, "observation_date", strategy),
                "contractor": _required(metadata, "contractor", strategy),
            }
        else:
            raise ValueError("govt_contracts candidate lacks source identity")
    elif strategy == "state_economics":
        family = "state_economics_observation_window"
        payload = {
            "source_observation_ids": sorted(
                str(item)
                for item in _required(metadata, "source_observation_ids", strategy)
            ),
            "window_end": _required(metadata, "window_end", strategy),
            "regional_sector": _required(metadata, "regional_sector", strategy),
        }
    elif strategy == "weather_ag":
        family = "weather_observation_window"
        payload = {
            "source_observation_ids": sorted(
                str(item)
                for item in _required(metadata, "source_observation_ids", strategy)
            ),
            "window_end": _required(metadata, "window_end", strategy),
            "commodity": _required(metadata, "commodity", strategy),
        }
    elif strategy == "commodity_macro":
        family = "commodity_report_state"
        payload = {
            "report_id": _required(metadata, "report_id", strategy),
            "window_end": _required(metadata, "window_end", strategy),
            "commodity": _required(metadata, "commodity", strategy),
        }
    elif strategy == "quantum_readiness":
        source_ids = metadata.get("source_ids")
        if _present(source_ids):
            if not isinstance(source_ids, list):
                raise ValueError("quantum_readiness candidate lacks source identity")
            family = "quantum_source_set"
            payload = sorted(str(item) for item in source_ids)
        else:
            family = "quantum_observation_window"
            payload = _required(metadata, "source_observation_window_id", strategy)
    else:
        raise ValueError(f"{strategy} candidate lacks a source identity family")

    return stable_id("event_key", strategy, family, ticker, payload)
