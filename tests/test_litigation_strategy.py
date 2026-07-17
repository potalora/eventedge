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
