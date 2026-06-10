# tests/test_short_gate_config.py
"""The short-conviction threshold is config-driven; default stays 0.60 (gen_001)."""
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee


def _short(ticker, strategy, conviction):
    return {
        "ticker": ticker, "strategy": strategy, "direction": "short", "score": conviction,
        "metadata": {"llm_analysis": {"conviction": conviction}},
    }


class TestConfigurableShortThreshold:
    def test_default_threshold_is_baseline(self):
        c = PortfolioCommittee(config={})
        assert c._short_conviction_threshold == 0.60

    def test_config_lowers_threshold(self):
        c = PortfolioCommittee(
            config={"autoresearch": {"risk_discipline": {"short_conviction_threshold": 0.45}}}
        )
        assert c._short_conviction_threshold == 0.45

    def test_045_lets_a_045_short_clear(self):
        c = PortfolioCommittee(
            config={"autoresearch": {"risk_discipline": {"short_conviction_threshold": 0.45}}}
        )
        assert c._short_passes_gate([_short("BTGO", "litigation", 0.45)], c._short_conviction_threshold) is True

    def test_045_still_blocks_a_044_short(self):
        c = PortfolioCommittee(
            config={"autoresearch": {"risk_discipline": {"short_conviction_threshold": 0.45}}}
        )
        assert c._short_passes_gate([_short("BTGO", "litigation", 0.44)], c._short_conviction_threshold) is False

    def test_rule_based_short_still_blocked(self):
        # conviction 0.0 (no llm_analysis) must not clear even at 0.45
        c = PortfolioCommittee(
            config={"autoresearch": {"risk_discipline": {"short_conviction_threshold": 0.45}}}
        )
        raw = {"ticker": "COPX", "strategy": "commodity_macro", "direction": "short", "score": 0.50}
        assert c._short_passes_gate([raw], c._short_conviction_threshold) is False
