import pytest
from tradingagents.default_config import DEFAULT_CONFIG


class TestAutoresearchConfig:
    def test_autoresearch_key_exists(self):
        assert "autoresearch" in DEFAULT_CONFIG

    def test_all_expected_keys_present(self):
        ar = DEFAULT_CONFIG["autoresearch"]
        expected_keys = [
            "max_generations", "strategies_per_generation", "tickers_per_strategy",
            "walk_forward_windows", "holdout_weeks", "min_trades_for_scoring",
            "cache_model", "live_model", "strategist_model", "cro_model",
            "fitness_min_sharpe", "fitness_min_win_rate", "fitness_min_trades",
            "paper_min_trades", "paper_max_divergence_pct",
            "analyst_weight_min", "analyst_weight_max",
            "complexity_penalty_factor", "stop_unchanged_generations",
            "universe", "budget_cap_usd",
        ]
        for key in expected_keys:
            assert key in ar, f"Missing key: {key}"

    def test_numeric_types(self):
        ar = DEFAULT_CONFIG["autoresearch"]
        assert isinstance(ar["max_generations"], int)
        assert isinstance(ar["fitness_min_sharpe"], float)
        assert isinstance(ar["budget_cap_usd"], float)
        assert isinstance(ar["universe"], str)

    def test_config_is_copyable(self):
        import copy
        config_copy = copy.deepcopy(DEFAULT_CONFIG)
        assert config_copy["autoresearch"] == DEFAULT_CONFIG["autoresearch"]
        config_copy["autoresearch"]["max_generations"] = 999
        assert DEFAULT_CONFIG["autoresearch"]["max_generations"] == 15

    def test_finnhub_reliability_defaults_are_endpoint_scoped_and_bounded(self):
        policy = DEFAULT_CONFIG["autoresearch"]["finnhub_reliability"]

        assert policy["earnings_calendar"]["read_timeout_s"] > (
            policy["company_peers"]["read_timeout_s"]
        )
        assert policy["earnings_calendar"]["max_attempts"] == 3
        assert policy["company_peers"]["max_attempts"] == 2
        assert policy["company_news"]["max_attempts"] == 3
        assert policy["workflow_budget_s"] == 240.0
        assert policy["workflow_budget_s"] <= 300.0 - 30.0
