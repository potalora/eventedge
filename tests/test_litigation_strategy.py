from tradingagents.strategies.data_sources.edgar_source import EDGARSource


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
