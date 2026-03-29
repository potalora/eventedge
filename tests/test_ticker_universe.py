import pytest

from tradingagents.autoresearch.ticker_universe import (
    UNIVERSE_PRESETS,
    SMALL_CAP_WATCHLIST,
    get_universe,
)


class TestGetUniverse:
    def test_default_preset_returns_nonempty_sorted_deduped(self):
        result = get_universe({"autoresearch": {"universe": "sp500_nasdaq100"}})
        assert len(result) > 0
        assert result == sorted(result)
        assert len(result) == len(set(result))

    def test_small_cap_preset(self):
        result = get_universe({"autoresearch": {"universe": "small_cap"}})
        assert result == SMALL_CAP_WATCHLIST

    def test_custom_list_deduplicates_and_sorts(self):
        result = get_universe({"autoresearch": {"universe": ["AAPL", "MSFT", "AAPL"]}})
        assert result == ["AAPL", "MSFT"]

    def test_unknown_preset_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown universe preset"):
            get_universe({"autoresearch": {"universe": "nonexistent_preset"}})

    def test_missing_autoresearch_key_uses_default(self):
        result = get_universe({})
        assert result == UNIVERSE_PRESETS["sp500_nasdaq100"]

    def test_missing_universe_key_uses_default(self):
        result = get_universe({"autoresearch": {}})
        assert result == UNIVERSE_PRESETS["sp500_nasdaq100"]


class TestUniversePresets:
    def test_all_presets_nonempty(self):
        for name, tickers in UNIVERSE_PRESETS.items():
            assert len(tickers) > 0, f"Preset '{name}' is empty"

    def test_sp500_nasdaq100_is_deduplicated(self):
        tickers = UNIVERSE_PRESETS["sp500_nasdaq100"]
        assert len(tickers) == len(set(tickers))

    def test_all_presets_contain_strings(self):
        for name, tickers in UNIVERSE_PRESETS.items():
            for t in tickers:
                assert isinstance(t, str), f"Non-string ticker in preset '{name}': {t}"
