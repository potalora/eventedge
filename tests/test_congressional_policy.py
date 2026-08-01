"""Congressional disclosure identity, timing, and order-eligibility policy."""

from __future__ import annotations

from datetime import date

from tradingagents.strategies.data_sources.congress_source import (
    _normalize_fmp_trade,
    _normalize_trade,
)
from tradingagents.strategies.modules.congressional_trades import (
    CongressionalTradesStrategy,
    congressional_event_key,
)
from tradingagents.strategies.orchestration.event_identity import canonical_event_key


AMOUNT = "$15,001 - $50,000"


def _trade(
    member: str,
    *,
    ticker: str = "MSFT",
    transaction_type: str = "purchase",
    amount: str = AMOUNT,
    transaction_date: str = "2026-07-20",
    publication_date: str = "2026-07-30",
    source: str = "fmp",
    native_id: str = "",
    source_url: str = "",
) -> dict[str, str]:
    return {
        "source": source,
        "ticker": ticker,
        "transaction_type": transaction_type,
        "amount": amount,
        "representative": member,
        "chamber": "House",
        "transaction_date": transaction_date,
        "publication_date": publication_date,
        "native_disclosure_id": native_id,
        "source_url": source_url,
    }


def _screen(*trades: dict[str, str], session: str = "2026-07-31"):
    return CongressionalTradesStrategy().screen(
        {"congress": {"recent_trades": list(trades)}},
        session,
        CongressionalTradesStrategy().get_default_params(),
    )


def test_native_disclosure_identity_ignores_audit_source() -> None:
    first = _trade("Rep A", source="fmp", native_id="DISC-42")
    mirror = _trade("Rep A", source="capitoltrades", native_id=" disc-42 ")

    assert congressional_event_key(first, "long") == congressional_event_key(
        mirror, "long"
    )


def test_normalizers_preserve_audit_source_native_id_and_disclosure_url() -> None:
    fmp = _normalize_fmp_trade(
        {
            "symbol": "MSFT",
            "disclosureId": "FMP-42",
            "id": "generic-row-id",
            "link": "HTTPS://disclosures.example/fmp/42/?utm_source=fmp&view=full#page",
        },
        "House",
    )
    capitol = _normalize_trade(
        {
            "issuer": {"issuerTicker": "MSFT:US"},
            "politician": {},
            "txDate": "2026-07-20",
            "id": "generic-row-id",
            "url": "https://disclosures.example/capitol/42",
        }
    )

    assert fmp["source"] == "fmp"
    assert fmp["native_disclosure_id"] == "FMP-42"
    assert fmp["source_url"] == "HTTPS://disclosures.example/fmp/42/?utm_source=fmp&view=full#page"
    assert fmp["canonical_disclosure_url"] == "https://disclosures.example/fmp/42?view=full"
    assert capitol["source"] == "capitoltrades"
    assert capitol["native_disclosure_id"] == ""
    assert capitol["canonical_disclosure_url"] == "https://disclosures.example/capitol/42"


def test_matching_native_alias_dedupes_when_vendor_facts_differ() -> None:
    first = _trade("Rep A", native_id="DISC-42", source="fmp")
    mirror = _trade(
        "Rep A.",
        native_id="disc-42",
        source="capitoltrades",
        source_url="https://other.example/42",
        publication_date="2026-07-30T15:00:00+00:00",
    )
    second_member = _trade("Rep B", native_id="DISC-8")

    candidates = _screen(first, mirror, second_member)

    assert len(candidates) == 1
    assert candidates[0].metadata["num_trades"] == 2


def test_distinct_native_ids_remain_distinct_components_but_share_cluster_identity() -> None:
    first = _trade("Rep A", native_id="DISC-41")
    second = _trade("Rep A", native_id="DISC-42")
    other_member = _trade("Rep B", native_id="DISC-8")

    candidates = _screen(first, second, other_member)

    assert len(candidates) == 1
    assert candidates[0].metadata["num_members"] == 2
    assert candidates[0].metadata["num_trades"] == 3
    assert len(candidates[0].source_event_keys) == 3
    assert len(candidates[0].metadata["trade_keys"]) == 2


def test_url_and_stable_facts_dedupe_across_vendors_before_member_count() -> None:
    first = _trade(
        "Rep A",
        source="fmp",
        source_url="HTTPS://disclosures.example/house/7?view=print&format=pdf&utm_source=fmp#fragment",
    )
    mirror = _trade(
        "Rep A",
        source="capitoltrades",
        source_url="https://disclosures.example/house/7?utm_source=capitol&format=pdf&view=print",
    )
    second_member = _trade("Rep B", native_id="DISC-8")

    assert congressional_event_key(first, "long") == congressional_event_key(
        mirror, "long"
    )
    candidates = _screen(first, mirror, second_member)

    assert len(candidates) == 1
    assert candidates[0].metadata["num_members"] == 2
    assert candidates[0].metadata["num_trades"] == 2
    assert len(candidates[0].source_event_keys) == 2


def test_one_vendor_runs_share_consumable_identity_but_retain_audit_aliases() -> None:
    fmp = _screen(
        _trade("Rep A", source="fmp", source_url="https://fmp.example/disclosure/1"),
        _trade("Rep B", source="fmp", source_url="https://fmp.example/disclosure/2"),
    )
    capitol = _screen(
        _trade(
            "Rep A",
            source="capitoltrades",
            source_url="https://capitol.example/house/101",
        ),
        _trade(
            "Rep B",
            source="capitoltrades",
            source_url="https://capitol.example/house/102",
        ),
    )

    assert len(fmp) == len(capitol) == 1
    assert fmp[0].source_event_keys == capitol[0].source_event_keys
    assert fmp[0].metadata["trade_keys"] == capitol[0].metadata["trade_keys"]
    assert fmp[0].event_key == capitol[0].event_key
    assert fmp[0].metadata["source_identity_aliases"] != capitol[0].metadata[
        "source_identity_aliases"
    ]
    assert canonical_event_key(
        "congressional_trades", fmp[0].ticker, fmp[0].metadata, date(2026, 7, 31)
    ) == fmp[0].event_key


def test_native_and_unkeyed_views_share_cluster_identity() -> None:
    native = _screen(
        _trade("Rep A", native_id="DISC-1"),
        _trade("Rep B", native_id="DISC-2"),
    )
    unkeyed = _screen(
        _trade("Rep A", source_url="https://vendor.example/rep-a"),
        _trade("Rep B", source_url="https://vendor.example/rep-b"),
    )

    assert native[0].source_event_keys != unkeyed[0].source_event_keys
    assert native[0].metadata["trade_keys"] == unkeyed[0].metadata["trade_keys"]
    assert native[0].event_key == unkeyed[0].event_key
    assert canonical_event_key(
        "congressional_trades", native[0].ticker, native[0].metadata, date(2026, 7, 31)
    ) == native[0].event_key
    assert canonical_event_key(
        "congressional_trades", unkeyed[0].ticker, unkeyed[0].metadata, date(2026, 7, 31)
    ) == unkeyed[0].event_key


def test_stable_facts_bridge_does_not_merge_distinct_disclosures() -> None:
    first = _trade("Rep A", source="fmp", source_url="https://fmp.example/1")
    changed = _trade(
        "Rep A",
        source="capitoltrades",
        source_url="https://capitol.example/other",
        amount="$50,001 - $100,000",
    )

    assert congressional_event_key(first, "long") != congressional_event_key(
        changed, "long"
    )
    assert _screen(first, changed) == []


def test_timestamp_publication_is_eligible_only_through_exact_xnys_close() -> None:
    before_close = _trade("Rep A", publication_date="2026-07-31T19:59:59+00:00")
    second_before = _trade("Rep B", publication_date="2026-07-31T19:59:59+00:00")
    after_close = _trade("Rep A", publication_date="2026-07-31T20:00:01+00:00")
    second_after = _trade("Rep B", publication_date="2026-07-31T20:00:01+00:00")

    assert len(_screen(before_close, second_before)) == 1
    assert _screen(after_close, second_after) == []


def test_timestamp_identity_normalizes_equivalent_timezones() -> None:
    utc = _trade("Rep A", publication_date="2026-07-30T20:00:00+00:00")
    eastern = _trade("Rep A", publication_date="2026-07-30T16:00:00-04:00")

    assert congressional_event_key(utc, "long") == congressional_event_key(
        eastern, "long"
    )


def test_naive_or_malformed_publication_fails_closed() -> None:
    naive = _trade("Rep A", publication_date="2026-07-31T15:00:00")
    malformed = _trade("Rep B", publication_date="tomorrow")

    assert _screen(naive, malformed) == []


def test_missing_publication_fails_closed() -> None:
    first = _trade("Rep A", publication_date="")
    second = _trade("Rep B", publication_date="")

    assert _screen(first, second) == []


def test_date_only_friday_publication_is_first_eligible_monday() -> None:
    first = _trade("Rep A", publication_date="2026-07-31")
    second = _trade("Rep B", publication_date="2026-07-31")

    assert _screen(first, second, session="2026-07-31") == []
    assert len(_screen(first, second, session="2026-08-03")) == 1


def test_date_only_holiday_publication_waits_for_next_xnys_session() -> None:
    first = _trade("Rep A", publication_date="2026-07-03")
    second = _trade("Rep B", publication_date="2026-07-03")

    assert _screen(first, second, session="2026-07-03") == []
    assert len(_screen(first, second, session="2026-07-06")) == 1


def test_duplicate_rows_cannot_satisfy_two_member_gate() -> None:
    disclosure = _trade("Rep A", native_id="DISC-1")

    assert _screen(disclosure, dict(disclosure)) == []


def test_default_amount_and_member_gates_are_enforced() -> None:
    low_a = _trade("Rep A", amount="$1,001 - $15,000")
    low_b = _trade("Rep B", amount="$1,001 - $15,000")
    one_member = _trade("Rep A", native_id="DISC-2")

    assert _screen(low_a, low_b) == []
    assert _screen(one_member) == []


def test_publication_lookback_defaults_to_seven_days() -> None:
    first = _trade("Rep A", publication_date="2026-07-23")
    second = _trade("Rep B", publication_date="2026-07-23")

    assert CongressionalTradesStrategy().get_default_params()["publication_lookback_days"] == 7
    assert _screen(first, second) == []


def test_purchase_cap_event_key_and_component_provenance_are_stable() -> None:
    trades = [
        _trade("Rep A", ticker="AAPL", native_id="A-1"),
        _trade("Rep B", ticker="AAPL", native_id="A-2"),
        _trade("Rep C", ticker="MSFT", native_id="M-1"),
        _trade("Rep D", ticker="MSFT", native_id="M-2"),
        _trade("Rep E", ticker="NVDA", native_id="N-1"),
        _trade("Rep F", ticker="NVDA", native_id="N-2"),
    ]

    forward = _screen(*trades)
    reverse = _screen(*reversed(trades))

    assert len([candidate for candidate in forward if not candidate.journal_only]) == 2
    assert [(item.ticker, item.event_key) for item in forward] == [
        (item.ticker, item.event_key) for item in reverse
    ]
    candidate = forward[0]
    assert candidate.source_event_keys == tuple(sorted(candidate.source_event_keys))
    assert candidate.metadata["trade_keys"] == sorted(candidate.metadata["trade_keys"])
    assert candidate.metadata["trade_keys"] != list(candidate.source_event_keys)
    assert canonical_event_key(
        "congressional_trades", candidate.ticker, candidate.metadata, date(2026, 7, 31)
    ) == candidate.event_key
    assert "signal_ids" not in candidate.metadata
    assert not hasattr(candidate, "signal_ids")


def test_qualifying_sales_are_short_and_journal_only() -> None:
    first = _trade("Rep A", ticker="TSLA", transaction_type="sale", native_id="S-1")
    second = _trade("Rep B", ticker="TSLA", transaction_type="sale", native_id="S-2")

    candidates = _screen(first, second)

    assert len(candidates) == 1
    assert candidates[0].direction == "short"
    assert candidates[0].journal_only is True
    assert candidates[0].strategy_tags == ("congressional_trades",)
    assert "disclosure_week:2026-W31" in candidates[0].risk_tags
    assert canonical_event_key(
        "congressional_trades", candidates[0].ticker, candidates[0].metadata, date(2026, 7, 31)
    ) == candidates[0].event_key


def test_journal_only_sales_are_deterministically_bounded() -> None:
    sales = [
        _trade("Rep A", ticker="AAPL", transaction_type="sale", native_id="S-1"),
        _trade("Rep B", ticker="AAPL", transaction_type="sale", native_id="S-2"),
        _trade("Rep C", ticker="MSFT", transaction_type="sale", native_id="S-3"),
        _trade("Rep D", ticker="MSFT", transaction_type="sale", native_id="S-4"),
        _trade("Rep E", ticker="NVDA", transaction_type="sale", native_id="S-5"),
        _trade("Rep F", ticker="NVDA", transaction_type="sale", native_id="S-6"),
    ]

    forward = _screen(*sales)
    reverse = _screen(*reversed(sales))

    assert len(forward) == 2
    assert all(candidate.journal_only and candidate.direction == "short" for candidate in forward)
    assert [(item.ticker, item.event_key) for item in forward] == [
        (item.ticker, item.event_key) for item in reverse
    ]
