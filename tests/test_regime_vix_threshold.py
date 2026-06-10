# tests/test_regime_vix_threshold.py
"""VIX-stressed cutoff is config-driven; default 25 reproduces gen_001."""
import pandas as pd
import pytest

from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
from tradingagents.strategies.state.state import StateManager
from tradingagents.strategies.modules import get_all_strategies


def _engine(tmp_path, stressed=None):
    state = StateManager(str(tmp_path / "state"))
    ar = {"state_dir": str(tmp_path / "state"), "total_capital": 5000}
    if stressed is not None:
        ar["risk_discipline"] = {"regime_vix_stressed": stressed}
    return MultiStrategyEngine(
        config={"autoresearch": ar}, strategies=get_all_strategies(), state_manager=state,
    )


class TestRegimeVixThreshold:
    def test_default_vix_21_is_normal(self, tmp_path):
        eng = _engine(tmp_path)  # default 25
        assert eng._classify_regime(21.5, 272.0, 0.0) == "normal"

    def test_lowered_threshold_makes_vix_21_stressed(self, tmp_path):
        eng = _engine(tmp_path, stressed=20.0)
        assert eng._classify_regime(21.5, 272.0, 0.0) == "stressed"

    def test_build_regime_model_reflects_threshold(self, tmp_path):
        eng = _engine(tmp_path, stressed=20.0)
        data = {"yfinance": {"vix": pd.DataFrame({"Close": [21.5]})}, "fred": {}}
        regime = eng._build_regime_model(data)
        assert regime["overall_regime"] == "stressed"
        assert regime["vix_regime"] == "elevated"
        assert regime["thresholds"]["vix"]["elevated"] == 20.0
