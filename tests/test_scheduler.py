import pytest
from unittest.mock import patch, MagicMock
from tradingagents.scheduler.scheduler import TradingScheduler
from tradingagents.scheduler.jobs import daily_scan_job


class TestTradingScheduler:
    def _make_config(self):
        return {
            "scheduler": {
                "enabled": True,
                "watchlist": ["SOFI", "PLTR"],
                "scan_time": "07:00",
                "portfolio_check_times": ["10:00", "15:00"],
                "timezone": "US/Eastern",
                "trading_days_only": True,
            },
            "alerts": {"enabled": False, "channels": [], "notify_on": []},
            "llm_provider": "anthropic",
            "deep_think_llm": "claude-sonnet-4-20250514",
            "quick_think_llm": "claude-haiku-4-5-20251001",
        }

    @patch("tradingagents.scheduler.scheduler.BackgroundScheduler")
    def test_scheduler_creates_jobs(self, mock_sched_cls):
        mock_sched = MagicMock()
        mock_sched_cls.return_value = mock_sched

        config = self._make_config()
        ts = TradingScheduler(config)
        ts.start()

        assert mock_sched.add_job.call_count >= 1
        mock_sched.start.assert_called_once()

    @patch("tradingagents.scheduler.scheduler.BackgroundScheduler")
    def test_scheduler_stop(self, mock_sched_cls):
        mock_sched = MagicMock()
        mock_sched_cls.return_value = mock_sched

        config = self._make_config()
        ts = TradingScheduler(config)
        ts.start()
        ts.stop()

        mock_sched.shutdown.assert_called_once()

    @patch("tradingagents.scheduler.scheduler.BackgroundScheduler")
    def test_scheduler_status(self, mock_sched_cls):
        mock_sched = MagicMock()
        mock_sched.get_jobs.return_value = []
        mock_sched_cls.return_value = mock_sched

        config = self._make_config()
        ts = TradingScheduler(config)
        status = ts.status()
        assert "jobs" in status


class TestDailyScanJob:
    @patch("tradingagents.scheduler.jobs.TradingAgentsGraph")
    def test_daily_scan_runs_propagate(self, mock_graph_cls):
        mock_graph = MagicMock()
        mock_graph.propagate.return_value = (
            {"final_trade_decision": "Rating: BUY"},
            "BUY",
        )
        mock_graph_cls.return_value = mock_graph

        config = {
            "scheduler": {"watchlist": ["SOFI"]},
            "alerts": {"enabled": False, "channels": [], "notify_on": []},
        }
        mock_alert = MagicMock()

        results = daily_scan_job(config, mock_alert)
        assert len(results) == 1
        assert results[0]["ticker"] == "SOFI"
        assert results[0]["rating"] == "BUY"
