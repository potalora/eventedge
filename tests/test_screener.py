import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np
from tradingagents.autoresearch.screener import MarketScreener, _safe_float, _extract_market_cap
from tradingagents.autoresearch.models import ScreenerCriteria, ScreenerResult, Filter
from tradingagents.storage.db import Database
import tempfile, os


@pytest.fixture
def tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    yield db
    db.close()
    os.unlink(path)


@pytest.fixture
def screener(tmp_db):
    config = {"autoresearch": {"universe": "small_cap"}}
    return MarketScreener(config)


class TestClassifyRegime:
    def test_crisis_high_vix(self, screener):
        assert screener.classify_regime(35, 1.0, 3.0) == "CRISIS"

    def test_crisis_high_spread(self, screener):
        assert screener.classify_regime(15, 1.0, 6.0) == "CRISIS"

    def test_risk_off_moderate_vix(self, screener):
        assert screener.classify_regime(22, 1.0, 3.0) == "RISK_OFF"

    def test_risk_off_inverted_yield(self, screener):
        assert screener.classify_regime(15, -0.5, 3.0) == "RISK_OFF"

    def test_risk_on(self, screener):
        assert screener.classify_regime(12, 1.0, 3.0) == "RISK_ON"

    def test_transition(self, screener):
        assert screener.classify_regime(17, 0.3, 3.0) == "TRANSITION"


class TestComputeTurbulence:
    def test_returns_float(self, screener):
        np.random.seed(42)
        returns = pd.DataFrame(np.random.randn(300, 3), columns=["A", "B", "C"])
        turb = screener.compute_turbulence(returns, lookback=252)
        assert isinstance(turb, float)
        assert turb >= 0

    def test_insufficient_data_returns_zero(self, screener):
        returns = pd.DataFrame(np.random.randn(10, 3), columns=["A", "B", "C"])
        assert screener.compute_turbulence(returns, lookback=252) == 0.0


class TestSurvivorshipFilter:
    def test_passes(self, screener):
        assert screener.survivorship_filter(200, 250, 0.80) is True

    def test_fails(self, screener):
        assert screener.survivorship_filter(150, 250, 0.80) is False

    def test_zero_expected(self, screener):
        assert screener.survivorship_filter(0, 0, 0.80) is False

    def test_exact_threshold(self, screener):
        assert screener.survivorship_filter(200, 250, 0.80) is True  # 0.8 exactly


class TestApplyFilters:
    def _make_result(self, **overrides):
        defaults = dict(
            ticker="AAPL", close=150.0, change_14d=0.05, change_30d=0.10,
            high_52w=180.0, low_52w=120.0, avg_volume_20d=5_000_000,
            volume_ratio=1.2, rsi_14=55.0, ema_10=148.0, ema_50=145.0,
            macd=2.5, boll_position=0.6, iv_rank=None, put_call_ratio=None,
            options_volume=None, market_cap=2_500_000_000_000.0, sector="Technology",
            revenue_growth_yoy=0.15, next_earnings_date=None, regime="RISK_ON",
            trading_day_coverage=0.95,
        )
        defaults.update(overrides)
        return ScreenerResult(**defaults)

    def test_passes_default_criteria(self, screener):
        result = self._make_result()
        criteria = ScreenerCriteria()
        assert screener.apply_filters(result, criteria) is True

    def test_fails_volume(self, screener):
        result = self._make_result(avg_volume_20d=50_000)
        criteria = ScreenerCriteria(min_avg_volume=100_000)
        assert screener.apply_filters(result, criteria) is False

    def test_fails_sector(self, screener):
        result = self._make_result(sector="Healthcare")
        criteria = ScreenerCriteria(sector="Technology")
        assert screener.apply_filters(result, criteria) is False

    def test_custom_filter_passes(self, screener):
        result = self._make_result(rsi_14=25.0)
        criteria = ScreenerCriteria(custom_filters=[Filter("rsi_14", "<", 30)])
        assert screener.apply_filters(result, criteria) is True

    def test_custom_filter_fails(self, screener):
        result = self._make_result(rsi_14=55.0)
        criteria = ScreenerCriteria(custom_filters=[Filter("rsi_14", "<", 30)])
        assert screener.apply_filters(result, criteria) is False


class TestHelpers:
    def test_safe_float_valid(self):
        assert _safe_float("3.14") == 3.14

    def test_safe_float_none(self):
        assert _safe_float(None) is None

    def test_safe_float_invalid(self):
        assert _safe_float("not_a_number") is None
