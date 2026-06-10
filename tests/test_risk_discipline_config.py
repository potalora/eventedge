"""Defaults for the gen_002 risk_discipline config block must equal gen_001 behavior."""
from tradingagents.default_config import DEFAULT_CONFIG


def test_risk_discipline_defaults_match_baseline():
    rd = DEFAULT_CONFIG["autoresearch"]["risk_discipline"]
    assert rd["reentry_cooldown_days"] == 0          # disabled = gen_001
    assert rd["short_conviction_threshold"] == 0.60  # gen_001 short gate
    assert rd["regime_vix_stressed"] == 25.0         # gen_001 stressed cutoff
