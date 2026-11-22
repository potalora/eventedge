"""Defaults for the gen_003 risk_discipline config block."""
from tradingagents.default_config import DEFAULT_CONFIG


def test_risk_discipline_defaults_match_baseline():
    rd = DEFAULT_CONFIG["autoresearch"]["risk_discipline"]
    assert rd["reentry_cooldown_days"] == 7          # gen_003 default cooldown
    assert rd["short_conviction_threshold"] == 0.60  # short conviction gate
    assert rd["regime_vix_stressed"] == 25.0         # stressed VIX cutoff
