"""Source-native event identities for configured paper research strategies."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any

from tradingagents.strategies.execution import stable_id


ACTIVE_STRATEGY_NAMES = frozenset(
    {
        "earnings_call",
        "insider_activity",
        "filing_analysis",
        "regulatory_pipeline",
        "supply_chain",
        "litigation",
        "congressional_trades",
        "govt_contracts",
        "state_economics",
        "weather_ag",
        "commodity_macro",
        "quantum_readiness",
        "bounded_document_research",
    }
)

_AVAILABILITY_FIELDS: dict[str, tuple[tuple[str, bool], ...]] = {
    "earnings_call": (("published_at", False),),
    "insider_activity": (("filing_date", True),),
    "filing_analysis": (("file_date", True),),
    "regulatory_pipeline": (("posted_date", True),),
    "supply_chain": (("published_at", False),),
    "litigation": (("release_date", True), ("date_filed", True)),
    "congressional_trades": (
        ("publication_date", True),
        ("disclosure_date", True),
        ("filing_date", True),
    ),
    "govt_contracts": (
        ("published_at", False),
        ("last_modified_date", True),
        ("observation_date", True),
    ),
    "state_economics": (("window_end", True),),
    "weather_ag": (("available_at", False), ("window_end", True)),
    "commodity_macro": (("available_at", False), ("window_end", True)),
    "quantum_readiness": (("published_at", False), ("window_end", True)),
}


def _availability_timestamp(value: object, *, date_only: bool, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        if not date_only:
            raise ValueError(f"candidate timestamp {field} requires an aware datetime")
        return datetime.combine(value, time.max, tzinfo=timezone.utc)
    elif isinstance(value, str):
        if date_only:
            try:
                parsed_date = date.fromisoformat(value)
            except ValueError:
                parsed_date = None
            if parsed_date is not None and value == parsed_date.isoformat():
                return datetime.combine(parsed_date, time.max, tzinfo=timezone.utc)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"invalid candidate timestamp {field}") from error
    else:
        raise ValueError(f"invalid candidate timestamp {field}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"candidate timestamp {field} requires timezone awareness")
    return parsed.astimezone(timezone.utc)


def canonical_observation_time(strategy: str, metadata: dict[str, Any]) -> datetime:
    """Resolve when source evidence became available without using economic dates."""
    if strategy == 'regulatory_pipeline' and metadata.get('regulation_publication_policy') == 'linked_publications_v1':
        return _availability_timestamp(_required(metadata, 'available_at', strategy), date_only=False, field='available_at')
    if strategy == 'state_economics' and metadata.get('state_economics_policy') == 'state_unemployment_v1':
        return _availability_timestamp(_required(metadata, 'available_at', strategy), date_only=False, field='available_at')
    values: list[datetime] = []
    if metadata.get("observed_at") not in (None, ""):
        values.append(
            _availability_timestamp(
                metadata["observed_at"], date_only=False, field="observed_at"
            )
        )
    for field, date_only in _AVAILABILITY_FIELDS.get(strategy, ()):
        if metadata.get(field) not in (None, ""):
            values.append(
                _availability_timestamp(
                    metadata[field], date_only=date_only, field=field
                )
            )
    if not values:
        raise ValueError(f"{strategy} candidate lacks recognized observation time")
    return max(values)


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
            return (key, filing[key], filing.get("owner_cik") or filing.get("owner_name"), filing.get("transaction_id") or (filing.get("transaction_date"), filing.get("transaction_code"), filing.get("acquired_disposed"), filing.get("shares"), filing.get("price_per_share")))
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

    if strategy == 'bounded_document_research':
        import re
        source = metadata.get('bounded_document_source')
        if not isinstance(source, dict):
            raise ValueError('bounded document candidate lacks retained source identity')
        document_id = _required(source, 'document_id', strategy)
        original_sha = _required(source, 'original_sha256', strategy)
        pages = _required(source, 'selected_pages', strategy)
        if (not isinstance(document_id, str) or not document_id.strip()
                or not isinstance(original_sha, str) or re.fullmatch('[0-9a-f]{64}', original_sha) is None
                or type(pages) is not list or not 1 <= len(pages) <= 8
                or any(type(page) is not int or page < 1 for page in pages)
                or len(set(pages)) != len(pages)):
            raise ValueError('bounded document candidate source identity invalid')
        available = _availability_timestamp(_required(source, 'as_of', strategy),
            date_only=False, field='as_of')
        family = 'retained_document_pages'
        payload = {'document_id': document_id, 'original_sha256': original_sha,
                   'selected_pages': pages, 'as_of': available.isoformat()}
    elif strategy == "earnings_call":
        family = "fiscal_period"
        payload = {
            "year": _required(metadata, "year", strategy),
            "quarter": _required(metadata, "quarter", strategy),
        }
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
    elif strategy == "govt_contracts":
        source = _required(metadata, "source", strategy)
        if source == "usaspending":
            family = "federal_award"
            payload = metadata.get("award_key") or _required(metadata, "award_id", strategy)
        elif source == "momentum_fallback":
            family = "daily_contract_proxy_state"
            payload = {
                "observation_date": _required(metadata, "observation_date", strategy),
                "contractor": _required(metadata, "contractor", strategy),
            }
        else:
            raise ValueError("govt_contracts candidate lacks source identity")
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
    else:
        raise ValueError(f"{strategy} candidate lacks a source identity family")

    return stable_id("event_key", strategy, family, ticker, payload)
