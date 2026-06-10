"""Re-entry cooldown: block names stopped out within the cooldown window."""
from tradingagents.strategies.trading.risk_gate import compute_cooling_tickers


def _stop(ticker, exit_date, reason="stop_loss"):
    return {"ticker": ticker, "exit_date": exit_date, "exit_reason": reason, "status": "closed"}


class TestComputeCoolingTickers:
    def test_recent_stop_is_cooling(self):
        closed = [_stop("CRWD", "2026-06-09")]
        assert compute_cooling_tickers(closed, "2026-06-11", 7) == {"CRWD"}

    def test_same_day_stop_is_cooling(self):
        closed = [_stop("T", "2026-06-09")]
        assert compute_cooling_tickers(closed, "2026-06-09", 7) == {"T"}

    def test_stop_outside_window_not_cooling(self):
        closed = [_stop("BA", "2026-06-01")]
        assert compute_cooling_tickers(closed, "2026-06-09", 7) == set()  # 8 days >= 7

    def test_take_profit_does_not_cool(self):
        closed = [_stop("IBM", "2026-06-09", reason="take_profit")]
        assert compute_cooling_tickers(closed, "2026-06-10", 7) == set()

    def test_zero_cooldown_disables(self):
        closed = [_stop("CRWD", "2026-06-09")]
        assert compute_cooling_tickers(closed, "2026-06-09", 0) == set()

    def test_malformed_dates_ignored(self):
        closed = [_stop("X", ""), _stop("Y", None), {"ticker": "Z", "exit_reason": "stop_loss"}]
        assert compute_cooling_tickers(closed, "2026-06-09", 7) == set()


from tradingagents.strategies.trading.risk_gate import RiskGateConfig


class TestRiskGateConfigCooldown:
    def test_default_cooldown_is_zero(self):
        assert RiskGateConfig().reentry_cooldown_days == 0

    def test_from_dict_reads_risk_discipline(self):
        cfg = RiskGateConfig.from_dict(
            {"autoresearch": {"risk_discipline": {"reentry_cooldown_days": 7}}}
        )
        assert cfg.reentry_cooldown_days == 7

    def test_from_dict_defaults_to_zero(self):
        assert RiskGateConfig.from_dict({"autoresearch": {}}).reentry_cooldown_days == 0
