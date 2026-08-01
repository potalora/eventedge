from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import defaultdict
from datetime import date as Date
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tradingagents.strategies.orchestration.trading_calendar import (
    is_session,
    next_session,
    session_close,
)

from .base import Candidate

logger = logging.getLogger(__name__)

# Dollar amount buckets from congressional disclosures (ascending)
AMOUNT_BUCKETS = [
    "$1,001 - $15,000",
    "$15,001 - $50,000",
    "$50,001 - $100,000",
    "$100,001 - $250,000",
    "$250,001 - $500,000",
    "$500,001 - $1,000,000",
    "$1,000,001 - $5,000,000",
    "$5,000,001 - $25,000,000",
    "$25,000,001 - $50,000,000",
]

# Map bucket string to a 1-based tier for scoring
BUCKET_TIER = {bucket: index + 1 for index, bucket in enumerate(AMOUNT_BUCKETS)}


def _normalized_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _member_key(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", _normalized_text(value))
    normalized = " ".join(normalized.split())
    return normalized or "unknown"


def _member_tag(member: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", member.casefold()).strip("-") or "unknown"


def _canonical_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return ""
    tracking = {"fbclid", "gclid", "dclid", "msclkid"}
    kept_query = urlencode(
        sorted(
            (
                key,
                value,
            )
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in tracking
            and not key.casefold().startswith(("utm_", "mc_"))
        )
    )
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path.rstrip("/"),
            kept_query,
            "",
        )
    )


def _normalized_date(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return raw
    return timestamp.astimezone(timezone.utc).isoformat()


def _publication_value(trade: dict[str, Any]) -> object:
    for key in ("publication_date", "disclosure_date", "filing_date", "pub_date"):
        if trade.get(key) not in (None, ""):
            return trade[key]
    return ""


def _native_disclosure_id(trade: dict[str, Any]) -> str:
    for key in (
        "native_disclosure_id",
        "disclosure_id",
        "disclosureId",
        "transaction_id",
        "transactionId",
    ):
        value = _normalized_text(trade.get(key))
        if value:
            return value
    return ""


def _stable_facts(trade: dict[str, Any], direction: str) -> dict[str, str]:
    """The narrow cross-vendor bridge; deliberately excludes vendor/source."""
    return {
        "member": _member_key(trade.get("representative") or trade.get("senator")),
        "chamber": _normalized_text(trade.get("chamber")),
        "ticker": _normalized_text(trade.get("ticker")).upper(),
        "direction": direction,
        "transaction_date": _normalized_date(trade.get("transaction_date")),
        "publication_date": _normalized_date(_publication_value(trade)),
        "amount": _normalized_text(trade.get("amount")),
        "owner": _normalized_text(trade.get("owner")),
    }


def _digest(prefix: str, payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{prefix}:{hashlib.sha256(canonical.encode()).hexdigest()[:24]}"


def congressional_event_key(trade: dict[str, Any], direction: str) -> str:
    """Return the source-independent consumable disclosure identity."""
    facts = _stable_facts(trade, direction)
    if not all(
        facts[key]
        for key in ("member", "ticker", "direction", "transaction_date", "publication_date", "amount")
    ):
        return ""
    return _digest("congress-facts", facts)


def _source_identity_alias(trade: dict[str, Any], direction: str) -> str:
    """Retain stronger vendor evidence for audit without affecting consumption."""
    native = _native_disclosure_id(trade)
    if native:
        return _digest("congress-native", {"native_disclosure_id": native})
    facts = _stable_facts(trade, direction)
    url = _canonical_url(
        trade.get("canonical_disclosure_url")
        or trade.get("source_url")
        or trade.get("url")
    )
    if url:
        return _digest("congress-url", {"url": url, "facts": facts})
    if not all(
        facts[key]
        for key in ("member", "ticker", "direction", "transaction_date", "publication_date", "amount")
    ):
        return ""
    return congressional_event_key(trade, direction)


def congressional_cluster_event_key(
    ticker: str, direction: str, component_keys: list[str] | tuple[str, ...]
) -> str:
    return _digest(
        "congress-cluster",
        {
            "ticker": str(ticker).upper(),
            "direction": direction,
            "components": sorted(set(component_keys)),
        },
    )


def _parse_publication(value: object) -> tuple[str, Date | datetime] | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return "timestamp", value.astimezone(timezone.utc)
    if isinstance(value, Date):
        return "date", value
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed_date = Date.fromisoformat(raw)
    except ValueError:
        parsed_date = None
    if parsed_date is not None and raw == parsed_date.isoformat():
        return "date", parsed_date
    for fmt in ("%m/%d/%Y", "%m-%d-%Y"):
        try:
            return "date", datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    try:
        parsed_timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
        return None
    return "timestamp", parsed_timestamp.astimezone(timezone.utc)


def _publication_is_eligible(trade: dict[str, Any], decision_session: Date) -> bool:
    """Enforce no same-day look-ahead for date-only disclosure records."""
    if not is_session(decision_session):
        return False
    parsed = _parse_publication(_publication_value(trade))
    if parsed is None:
        return False
    kind, publication = parsed
    if kind == "date":
        return decision_session >= next_session(publication)  # type: ignore[arg-type]
    return publication <= session_close(decision_session)  # type: ignore[arg-type]


class CongressionalTradesStrategy:
    """Follow timely, independently disclosed congressional clusters."""

    name = "congressional_trades"
    track = "paper_trade"
    data_sources = ["congress", "yfinance"]

    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            HORIZON_PARAMS,
        )

        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_range"],
            "min_amount_bucket": (2, 4),
            "max_positions": (2, 2),
            "min_members": (2, 3),
        }

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import (
            HORIZON_PARAMS,
        )

        hp = HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])
        return {
            "hold_days": hp["hold_days_default"],
            "min_amount_bucket": 2,
            "max_positions": 2,
            "min_members": 2,
            "enable_sale_orders": False,
            "publication_lookback_days": 7,
            "max_journal_only_sales": 2,
        }

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Create deterministic, time-safe long candidates and journal-only sales."""
        congress_data = data.get("congress", {})
        trades = congress_data.get("recent_trades", congress_data.get("trades", []))
        if not trades:
            return []
        try:
            decision_session = Date.fromisoformat(date)
        except ValueError:
            return []
        if not is_session(decision_session):
            return []

        min_bucket = max(2, int(params.get("min_amount_bucket", 2)))
        min_members = max(2, int(params.get("min_members", 2)))
        max_purchases = min(2, max(0, int(params.get("max_positions", 2))))
        max_journal_sales = min(
            2, max(0, int(params.get("max_journal_only_sales", 2)))
        )
        publication_lookback_days = max(
            0, int(params.get("publication_lookback_days", 7))
        )
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

        for trade in trades:
            if not isinstance(trade, dict) or not _publication_is_eligible(
                trade, decision_session
            ):
                continue
            parsed_publication = _parse_publication(_publication_value(trade))
            if parsed_publication is None:
                continue
            publication_day = (
                parsed_publication[1]
                if parsed_publication[0] == "date"
                else parsed_publication[1].date()
            )
            if (decision_session - publication_day).days > publication_lookback_days:
                continue
            transaction_type = _normalized_text(trade.get("transaction_type"))
            if transaction_type in {"buy", "purchase"} or "purchase" in transaction_type:
                direction = "long"
            elif transaction_type in {"sell", "sale"} or "sale" in transaction_type:
                direction = "short"
            else:
                continue
            ticker = str(trade.get("ticker") or "").upper().strip()
            if not ticker or ticker == "--":
                continue
            tier = BUCKET_TIER.get(str(trade.get("amount") or ""), 0)
            if tier < min_bucket:
                continue
            component_key = congressional_event_key(trade, direction)
            source_identity_alias = _source_identity_alias(trade, direction)
            if not component_key or not source_identity_alias:
                continue
            member = _member_key(trade.get("representative") or trade.get("senator"))
            record = {
                "member": member,
                "tier": tier,
                "component_key": component_key,
                "source_identity_alias": source_identity_alias,
                "publication_date": _publication_value(trade),
            }
            grouped[(ticker, direction)].append(record)

        candidates: list[Candidate] = []
        for (ticker, direction), raw_components in sorted(grouped.items()):
            components = self._dedupe_components(raw_components)
            members = sorted({item["member"] for item in components})
            if len(members) < min_members:
                continue
            component_keys = tuple(item["component_key"] for item in components)
            publication_dates = [
                str(item["publication_date"])
                for item in components
                if item["publication_date"] not in (None, "")
            ]
            score = float(sum(int(item["tier"]) for item in components) * len(members))
            risk_tags = {"strategy:congressional_trades"}
            risk_tags.update(f"member:{_member_tag(member)}" for member in members)
            for value in publication_dates:
                parsed = _parse_publication(value)
                if parsed is None:
                    continue
                publication_day = (
                    parsed[1] if parsed[0] == "date" else parsed[1].date()
                )
                iso_year, iso_week, _ = publication_day.isocalendar()
                risk_tags.add(f"disclosure_week:{iso_year}-W{iso_week:02d}")
            candidates.append(
                Candidate(
                    ticker=ticker,
                    date=date,
                    direction=direction,
                    score=score,
                    metadata={
                        "num_members": len(members),
                        "num_trades": len(components),
                        "members": members,
                        "max_tier": max(int(item["tier"]) for item in components),
                        "trade_keys": list(component_keys),
                        "source_identity_aliases": {
                            item["component_key"]: item["source_identity_aliases"]
                            for item in components
                        },
                        "cluster_direction": direction,
                        "publication_date": max(publication_dates),
                        "needs_llm_analysis": False,
                    },
                    event_key=congressional_cluster_event_key(
                        ticker, direction, component_keys
                    ),
                    source_event_keys=component_keys,
                    strategy_tags=("congressional_trades",),
                    risk_tags=tuple(sorted(risk_tags)),
                    journal_only=direction == "short",
                )
            )

        purchases = sorted(
            (candidate for candidate in candidates if candidate.direction == "long"),
            key=lambda candidate: (-candidate.score, candidate.ticker, candidate.event_key),
        )[:max_purchases]
        sales = sorted(
            (candidate for candidate in candidates if candidate.direction == "short"),
            key=lambda candidate: (-candidate.score, candidate.ticker, candidate.event_key),
        )[:max_journal_sales]
        return purchases + sales

    @staticmethod
    def _dedupe_components(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collapse matching stable facts or a matching native audit alias."""
        ordered = sorted(
            records,
            key=lambda item: (
                str(item["component_key"]),
                str(item["source_identity_alias"]),
            ),
        )
        parent = list(range(len(ordered)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        stable_seen: dict[str, int] = {}
        native_seen: dict[str, int] = {}
        for index, item in enumerate(ordered):
            component_key = str(item["component_key"])
            if component_key in stable_seen:
                union(index, stable_seen[component_key])
            else:
                stable_seen[component_key] = index
            alias = str(item["source_identity_alias"])
            if alias.startswith("congress-native:"):
                if alias in native_seen:
                    union(index, native_seen[alias])
                else:
                    native_seen[alias] = index

        groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for index, item in enumerate(ordered):
            groups[find(index)].append(item)
        components: list[dict[str, Any]] = []
        for views in groups.values():
            representative = min(
                views,
                key=lambda item: (
                    str(item["component_key"]),
                    str(item["source_identity_alias"]),
                    str(item["publication_date"]),
                ),
            )
            representative = dict(representative)
            representative["component_key"] = min(
                str(item["component_key"]) for item in views
            )
            representative["source_identity_aliases"] = sorted(
                {
                    str(item["source_identity_alias"])
                    for item in views
                    if item["source_identity_alias"]
                }
            )
            components.append(representative)
        return sorted(components, key=lambda item: str(item["component_key"]))

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
    ) -> tuple[bool, str]:
        """Exit on hold period or stop loss."""
        hold_days = params.get("hold_days", 28)
        if holding_days >= hold_days:
            return True, "hold_period"
        if entry_price > 0 and (current_price - entry_price) / entry_price <= -0.08:
            return True, "stop_loss"
        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        current = context.get("current_params", self.get_default_params())
        return f"""You are optimizing a Congressional Stock Trades strategy.

The fixed controls require at least two distinct members, amount bucket 2 or
higher, at most two long candidates, and sales remain journal-only.

Current parameters: {current}

Suggest 3 parameter combinations. Return JSON array of 3 param dicts."""
