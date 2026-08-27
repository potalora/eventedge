import logging

import pytest

from tradingagents.strategies.data_sources.edgar_source import EDGARSource
from tradingagents.strategies.modules.litigation import LitigationStrategy


def _edgar_with_name_map() -> EDGARSource:
    source = EDGARSource()
    source._name_to_ticker_cache = {
        "apple": "AAPL",
        "united states lime & minerals": "USLM",
    }
    return source


def test_edgar_exact_name_match_resolves_normalized_company() -> None:
    source = _edgar_with_name_map()

    assert source.name_to_ticker("Apple Inc.", allow_prefix=False) == "AAPL"


def test_edgar_exact_name_match_rejects_ambiguous_prefix() -> None:
    source = _edgar_with_name_map()

    assert source.name_to_ticker("United States", allow_prefix=False) is None
    assert source.name_to_ticker("United States") == "USLM"


@pytest.mark.parametrize("reverse", [False, True])
def test_edgar_duplicate_issuer_prefers_primary_ticker_regardless_of_order(
    reverse: bool,
) -> None:
    entries = [
        {"cik_str": 320335, "ticker": "GL", "title": "GLOBE LIFE INC."},
        {"cik_str": 320335, "ticker": "GL-PD", "title": "GLOBE LIFE INC."},
        {
            "cik_str": 1885408,
            "ticker": "NEXR",
            "title": "Nexera Technologies Ltd",
        },
        {
            "cik_str": 1885408,
            "ticker": "NEXRW",
            "title": "Nexera Technologies Ltd",
        },
    ]
    if reverse:
        entries.reverse()
    source = EDGARSource()
    source._session_cache["_company_tickers"] = {
        str(index): entry for index, entry in enumerate(entries)
    }

    assert source.name_to_ticker("Globe Life Inc.", allow_prefix=False) == "GL"
    assert (
        source.name_to_ticker("Nexera Technologies Ltd", allow_prefix=False)
        == "NEXR"
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_edgar_duplicate_issuer_fails_closed_for_unrelated_tickers(
    reverse: bool,
) -> None:
    entries = [
        {
            "cik_str": 1883984,
            "ticker": "ALCED",
            "title": "Alternus Clean Energy Inc.",
        },
        {
            "cik_str": 1883984,
            "ticker": "ACLEW",
            "title": "Alternus Clean Energy Inc.",
        },
    ]
    if reverse:
        entries.reverse()
    source = EDGARSource()
    source._session_cache["_company_tickers"] = {
        str(index): entry for index, entry in enumerate(entries)
    }

    assert (
        source.name_to_ticker("Alternus Clean Energy Inc.", allow_prefix=False)
        is None
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_edgar_same_cik_mixed_ticker_families_fail_closed(reverse: bool) -> None:
    entries = [
        {
            "cik_str": 1004434,
            "ticker": ticker,
            "title": "Affiliated Managers Group, Inc.",
        }
        for ticker in ("AMG", "MGR", "MGRB", "MGRD", "MGRE")
    ]
    if reverse:
        entries.reverse()
    source = EDGARSource()
    source._session_cache["_company_tickers"] = {
        str(index): entry for index, entry in enumerate(entries)
    }

    assert (
        source.name_to_ticker("Affiliated Managers Group, Inc.", allow_prefix=False)
        is None
    )


def test_edgar_name_map_fails_closed_for_cross_cik_title_collision() -> None:
    source = EDGARSource()
    source._session_cache["_company_tickers"] = {
        "base": {
            "cik_str": 101,
            "ticker": "BASE",
            "title": "Shared Issuer Name Inc.",
        },
        "extension": {
            "cik_str": 202,
            "ticker": "BASEW",
            "title": "Shared Issuer Name Inc.",
        },
    }

    assert source.name_to_ticker("Shared Issuer Name Inc.", allow_prefix=False) is None


def test_edgar_name_map_skips_malformed_title_and_ticker_fields() -> None:
    source = EDGARSource()
    source._session_cache["_company_tickers"] = {
        "null-ticker": {"cik_str": 1, "ticker": None, "title": "Null Ticker Inc."},
        "null-title": {"cik_str": 1, "ticker": "NULLTITLE", "title": None},
        "numeric-ticker": {"cik_str": 1, "ticker": 17, "title": "Numeric Ticker Inc."},
        "numeric-title": {"cik_str": 1, "ticker": "NUMTITLE", "title": 44},
        "blank-ticker": {"cik_str": 1, "ticker": " ", "title": "Blank Ticker Inc."},
        "blank-title": {"cik_str": 1, "ticker": "BLANKTITLE", "title": " "},
        "missing-cik": {"ticker": "MISSINGCIK", "title": "Missing Cik Inc."},
        "zero-cik": {"cik_str": 0, "ticker": "ZEROCIK", "title": "Zero Cik Inc."},
        "negative-cik": {"cik_str": -1, "ticker": "NEGCIK", "title": "Negative Cik Inc."},
        "string-cik": {"cik_str": "2", "ticker": "STRINGCIK", "title": "String Cik Inc."},
        "bool-cik": {"cik_str": True, "ticker": "BOOLCIK", "title": "Boolean Cik Inc."},
        "valid": {"cik_str": 3, "ticker": "real", "title": "Valid Issuer Inc."},
    }

    assert source._ensure_name_map() == {"valid issuer": "REAL"}


@pytest.mark.parametrize(
    ("tickers", "expected"),
    [
        ({"ONLY"}, "ONLY"),
        ({"GOOG", "GOOGL"}, "GOOG"),
        ({"ALCED", "ACLEW"}, None),
        ({"A", "AB", "ABC"}, None),
        ({"AMG", "MGR", "MGRB", "MGRD", "MGRE"}, None),
    ],
)
def test_edgar_company_ticker_selector_only_resolves_unique_base_extensions(
    tickers: set[str], expected: str | None
) -> None:
    assert EDGARSource._select_company_ticker(tickers) == expected


@pytest.fixture
def exact_litigation_tickers(monkeypatch: pytest.MonkeyPatch) -> None:
    matches = {
        "five below": "FIVE",
        "regeneron pharmaceuticals": "REGN",
        "apple": "AAPL",
        "zillow": "Z",
    }

    def fake_name_to_ticker(
        self: EDGARSource,
        company_name: str,
        *,
        allow_prefix: bool = True,
    ) -> str | None:
        assert allow_prefix is False
        return matches.get(self._normalize_name(company_name))

    monkeypatch.setattr(EDGARSource, "name_to_ticker", fake_name_to_ticker)


def _docket(
    docket_id: int,
    case_name: str,
    nature: str = "",
    date_filed: str = "2026-07-16",
) -> dict:
    return {
        "docket_id": docket_id,
        "case_name": case_name,
        "court": "",
        "date_filed": date_filed,
        "nature_of_suit": nature,
        "cause": "",
    }


def test_ordinary_adversarial_case_is_not_a_class_action() -> None:
    strategy = LitigationStrategy()

    assert strategy._is_class_action("Jenell v. Donahoe") is False
    assert strategy._is_class_action("In re Apple Inc. Securities Litigation") is True


def test_coded_high_signal_natures_are_recognized() -> None:
    strategy = LitigationStrategy()

    assert strategy._is_high_signal_nature("850 Securities/Commodities") is True
    assert strategy._is_high_signal_nature("410 Anti-Trust") is True
    assert strategy._is_high_signal_nature("950 Constitutional - State Statute") is False


def test_july_16_noise_cannot_crowd_out_public_company_cases(
    exact_litigation_tickers: None,
) -> None:
    strategy = LitigationStrategy()
    dockets = [
        _docket(1, "ZENG v. SCHEDULE A"),
        _docket(2, "Jenell v. Donahoe"),
        _docket(3, "United States v. STATE OF MARYLAND"),
        _docket(4, "JOHNS v. FIVE BELOW, INC."),
        _docket(
            5,
            "Cheatham v. Regeneron Pharmaceuticals, Inc.",
            "850 Securities/Commodities",
        ),
        _docket(6, "Alvarez v. Apple Inc."),
    ]

    candidates = strategy.screen(
        {"courtlistener": {"dockets": dockets}},
        "2026-07-16",
        {"max_positions": 3},
    )

    assert [candidate.ticker for candidate in candidates] == ["REGN", "FIVE", "AAPL"]


def test_duplicate_dockets_are_selected_once(
    exact_litigation_tickers: None,
) -> None:
    strategy = LitigationStrategy()
    duplicate = _docket(
        5,
        "Cheatham v. Regeneron Pharmaceuticals, Inc.",
        "850 Securities/Commodities",
    )

    candidates = strategy.screen(
        {"courtlistener": {"dockets": [duplicate, dict(duplicate)]}},
        "2026-07-16",
        {"max_positions": 3},
    )

    assert [candidate.ticker for candidate in candidates] == ["REGN"]


def test_sec_enforcement_is_prioritized_and_llm_ready(
    exact_litigation_tickers: None,
) -> None:
    strategy = LitigationStrategy()
    data = {
        "courtlistener": {
            "dockets": [
                _docket(4, "JOHNS v. FIVE BELOW, INC."),
                _docket(5, "Cheatham v. Regeneron Pharmaceuticals, Inc."),
                _docket(6, "Alvarez v. Apple Inc."),
            ]
        },
        "openbb": {
            "sec_litigation": {
                "releases": [
                    {
                        "title": "SEC Charges Example Corp",
                        "url": "https://sec.example/1",
                        "date": "2026-07-16",
                    }
                ]
            }
        },
    }

    candidates = strategy.screen(data, "2026-07-16", {"max_positions": 3})

    assert len(candidates) == 3
    assert candidates[0].metadata["source"] == "sec_enforcement"
    assert candidates[0].metadata["analysis_type"] == "litigation"
    assert candidates[0].metadata["case_name"] == "SEC Charges Example Corp"


def test_litigation_screen_logs_classification_counts(
    exact_litigation_tickers: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = LitigationStrategy()

    with caplog.at_level(
        logging.INFO,
        logger="tradingagents.strategies.modules.litigation",
    ):
        strategy.screen(
            {"courtlistener": {"dockets": [_docket(1, "Alvarez v. Apple Inc.")]}},
            "2026-07-16",
            {"max_positions": 3},
        )

    assert (
        "Litigation screen: fetched=1 unique=1 eligible=1 sec=0 "
        "selected=1 resolved=1 unresolved=0"
    ) in caplog.text


def test_litigation_screen_reuses_edgar_name_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[EDGARSource] = []

    def fake_init(self: EDGARSource) -> None:
        instances.append(self)
        self._name_to_ticker_cache = {
            "apple": "AAPL",
            "five below": "FIVE",
        }

    monkeypatch.setattr(EDGARSource, "__init__", fake_init)
    strategy = LitigationStrategy()
    data = {
        "courtlistener": {
            "dockets": [
                _docket(1, "Alvarez v. Apple Inc."),
                _docket(2, "JOHNS v. FIVE BELOW, INC."),
            ]
        }
    }

    strategy.screen(data, "2026-07-16", {"max_positions": 3})
    strategy.screen(data, "2026-07-16", {"max_positions": 3})

    assert len(instances) == 1
