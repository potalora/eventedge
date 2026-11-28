import pytest
from tradingagents.default_config import DEFAULT_CONFIG


class TestAutoresearchConfig:
    def test_autoresearch_key_exists(self):
        assert "autoresearch" in DEFAULT_CONFIG

    def test_all_expected_keys_present(self):
        ar = DEFAULT_CONFIG["autoresearch"]
        expected_keys = [
            "enabled_strategies",
            "llm_billing_mode",
            "thesis_model",
            "thesis_effort",
            "portfolio_committee_model",
            "portfolio_committee_effort",
            "bounded_model",
            "bounded_effort",
            "blocked_tickers",
            "candidate_analysis_workers",
            "proposals_per_strategy",
            "total_capital",
            "paper_ledger",
            "portfolio_policy",
            "risk_discipline",
            "short_selling",
            "risk_gate",
            "finnhub_reliability",
        ]
        for key in expected_keys:
            assert key in ar, f"Missing key: {key}"

    def test_numeric_types(self):
        ar = DEFAULT_CONFIG["autoresearch"]
        assert isinstance(ar["candidate_analysis_workers"], int)
        assert isinstance(ar["proposals_per_strategy"], int)
        assert isinstance(ar["total_capital"], float)
        assert isinstance(ar["thesis_model"], str)
        assert ar["thesis_model"] == "gemini-4-neon"
        assert ar["portfolio_committee_model"] == "gemini-4-neon"
        assert ar["bounded_model"] == "gemini-4-hydrogen"

    def test_config_is_copyable(self):
        import copy
        config_copy = copy.deepcopy(DEFAULT_CONFIG)
        assert config_copy["autoresearch"] == DEFAULT_CONFIG["autoresearch"]
        config_copy["autoresearch"]["proposals_per_strategy"] = 999
        assert DEFAULT_CONFIG["autoresearch"]["proposals_per_strategy"] == 3

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
