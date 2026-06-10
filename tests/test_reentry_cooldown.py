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


from tradingagents.strategies.trading.risk_gate import RiskGate
from tradingagents.execution.paper_broker import PaperBroker


def _gate(cooldown_days=7):
    broker = PaperBroker(initial_capital=50_000)
    cfg = RiskGateConfig(total_capital=50_000, reentry_cooldown_days=cooldown_days)
    return RiskGate(cfg, broker)


class TestRiskGateCooldownGate:
    def test_cooling_ticker_rejected(self):
        gate = _gate()
        gate.set_cooling_tickers({"CRWD"})
        passed, reason = gate.check("CRWD", "long", 1000.0, "quantum_readiness")
        assert passed is False
        assert "cooldown" in reason

    def test_non_cooling_ticker_allowed(self):
        gate = _gate()
        gate.set_cooling_tickers({"CRWD"})
        passed, _ = gate.check("MSFT", "long", 1000.0, "congressional_trades")
        assert passed is True

    def test_no_cooling_set_allows_all(self):
        gate = _gate()
        passed, _ = gate.check("CRWD", "long", 1000.0, "quantum_readiness")
        assert passed is True
