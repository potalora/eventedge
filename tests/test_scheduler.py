import pytest
from unittest.mock import patch, MagicMock
from tradingagents.scheduler.scheduler import TradingScheduler
from tradingagents.scheduler.jobs import daily_scan_job, paper_trading_job, evolution_job


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


class TestPaperTradingJob:
    @patch("tradingagents.strategies._dormant.cached_pipeline.CachedPipelineRunner")
    @patch("tradingagents.storage.db.Database")
    def test_paper_trading_job_runs(self, MockDB, MockPipeline):
        mock_db = MagicMock()
        mock_db.get_strategies_by_status.return_value = [
            {"id": 1, "name": "strat1", "generation": 0, "parent_ids": "[]",
             "hypothesis": "test", "conviction": 75,
             "screener_criteria": '{"market_cap_range": [0, 1e15]}',
             "instrument": "stock_long", "entry_rules": '["RSI > 30"]',
             "exit_rules": '["25% stop loss"]', "position_size_pct": 0.05,
             "max_risk_pct": 0.05, "time_horizon_days": 30,
             "regime_born": "RISK_ON", "status": "paper", "fitness_score": 1.5},
        ]
        MockDB.return_value = mock_db

        mock_pipeline_instance = MagicMock()
        mock_pipeline_instance.run.return_value = {"rating": "BUY"}
        MockPipeline.return_value = mock_pipeline_instance

        config = {
            "results_dir": "/tmp/test_results",
            "autoresearch": {"cache_model": "test", "live_model": "test"},
        }
        mock_alert = MagicMock()

        results = paper_trading_job(config, mock_alert)

        assert len(results) == 1
        assert results[0]["status"] == "processed"


class TestEvolutionJob:
    @patch("tradingagents.strategies._dormant.evolution.EvolutionEngine")
    @patch("tradingagents.storage.db.Database")
    def test_evolution_job_runs(self, MockDB, MockEngine):
        mock_engine = MagicMock()
        mock_engine.run.return_value = {
            "generations_run": 2,
            "leaderboard": [{"name": "best_strat"}],
        }
        MockEngine.return_value = mock_engine
        MockDB.return_value = MagicMock()

        config = {"results_dir": "/tmp/test_results"}
        mock_alert = MagicMock()

        result = evolution_job(config, mock_alert)

        assert result["generations_run"] == 2
        mock_alert.send.assert_called_once()


class TestSchedulerRegistersNewJobs:
    @patch("tradingagents.scheduler.scheduler.BackgroundScheduler")
    def test_registers_paper_and_evolution(self, mock_sched_cls):
        mock_sched = MagicMock()
        mock_sched_cls.return_value = mock_sched

        config = {
            "scheduler": {
                "enabled": True,
                "watchlist": ["AAPL"],
                "scan_time": "07:00",
                "portfolio_check_times": [],
                "timezone": "US/Eastern",
            },
            "alerts": {"enabled": False, "channels": [], "notify_on": []},
        }

        ts = TradingScheduler(config)
        ts.start()

        # Should register at least 3 jobs: daily_scan, paper_trading, weekly_evolution
        assert mock_sched.add_job.call_count >= 3

        # Check that paper_trading and weekly_evolution are registered
        all_kwargs = [call.kwargs for call in mock_sched.add_job.call_args_list]
        registered_ids = [kw.get("id", "") for kw in all_kwargs]
        assert "paper_trading" in registered_ids
        assert "weekly_evolution" in registered_ids
