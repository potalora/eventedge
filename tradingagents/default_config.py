import os

DEFAULT_CONFIG = {
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", "./results"),
    "data_cache_dir": os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
        "dataflows/data_cache",
    ),
    # LLM settings
    "llm_provider": "openai",
    "deep_think_llm": "gpt-5.2",
    "quick_think_llm": "gpt-5-mini",
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": None,
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    # Data vendor configuration
    # Category-level configuration (default for all tools in category)
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: alpha_vantage, yfinance
        "technical_indicators": "yfinance",  # Options: alpha_vantage, yfinance
        "fundamental_data": "yfinance",      # Options: alpha_vantage, yfinance
        "news_data": "yfinance",             # Options: alpha_vantage, yfinance
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
    # Options configuration
    "options": {
        "max_risk_per_trade_pct": 0.05,
        "allowed_strategies": [
            "long_call", "long_put", "bull_call_spread",
            "bear_put_spread", "straddle", "strangle",
        ],
        "default_expiry_range_days": 45,
        "covered_call_min_hold_days": 14,
        "covered_call_default_dte": 30,
        "covered_call_strike_offset": 0.05,
    },
    # Backtesting configuration
    "backtest": {
        "initial_capital": 5000,
        "max_position_pct": 0.35,
        "max_options_risk_pct": 0.05,
        "slippage_bps": 10,
        "commission_per_trade": 0,
        "trading_frequency": "weekly",
        "accuracy_windows": [5, 10, 30],
    },
    # Execution configuration
    "execution": {
        "mode": "paper",
        "broker": "alpaca",
        "confirm_before_trade": True,
        "daily_loss_limit_pct": 0.10,
        "execution_enabled": False,
    },
    # Scheduler configuration
    "scheduler": {
        "enabled": False,
        "watchlist": [],
        "scan_time": "07:00",
        "portfolio_check_times": ["10:00", "15:00"],
        "timezone": "US/Eastern",
        "trading_days_only": True,
    },
    # Alerts configuration
    "alerts": {
        "enabled": False,
        "channels": [],
        "notify_on": ["new_signal", "stop_loss", "target_hit", "daily_summary"],
    },
    # Autoresearch configuration
    "autoresearch": {
        "max_generations": 15,
        "strategies_per_generation": 4,
        "tickers_per_strategy": 3,
        "walk_forward_windows": 2,
        "holdout_weeks": 6,
        "fast_backtest": True,
        "fast_backtest_max_workers": 3,
        "min_trades_for_scoring": 5,
        "cache_model": "claude-haiku-4-5-20251001",
        "live_model": "claude-sonnet-4-20250514",
        "strategist_model": "claude-sonnet-4-20250514",
        "cro_model": "claude-haiku-4-5-20251001",
        "fitness_min_sharpe": 1.0,
        "fitness_min_win_rate": 0.50,
        "fitness_min_trades": 10,
        "paper_min_trades": 5,
        "paper_max_divergence_pct": 15,
        "analyst_weight_min": 0.3,
        "analyst_weight_max": 2.5,
        "complexity_penalty_factor": 0.1,
        "stop_unchanged_generations": 3,
        "universe": "sp500_nasdaq100",
        "budget_cap_usd": 150.0,
        # Multi-strategy engine settings
        "state_dir": "data/state",
        "total_capital": 5000,
        "proposals_per_strategy": 3,
        # LLM for autoresearch
        "autoresearch_model": "claude-sonnet-4-6",
        # Tickers to exclude from trading (compliance, conflict of interest)
        "blocked_tickers": [],
        # API keys (user provides when ready)
        "fmp_api_key": "",
        "fred_api_key": "",
        "finnhub_api_key": "",
        "regulations_api_key": "",
        "courtlistener_token": "",
        "noaa_cdo_token": "",
        "usda_nass_api_key": "",
        "edgar_user_agent": "TradingAgents research@example.com",
        # Risk gate (hard portfolio controls)
        "risk_gate": {
            "max_positions": 8,
            "max_position_pct": 0.15,
            "min_position_value": 100.0,
            "daily_loss_limit_pct": 0.03,
            "max_drawdown_pct": 0.15,
            "per_strategy_max": 3,
            "global_stop_loss_pct": 0.08,
            "long_only": True,
        },
        # Paper trade settings
        "paper_trade": {
            "min_trades_for_evaluation": 20,
            "exploration_budget_pct": 0.15,
            "max_vintage_age_days": 540,
            "learning_loop_calendar_days": 30,
            "learning_loop_min_strategies": 5,
            "portfolio_committee_enabled": True,
            "max_sector_concentration_pct": 0.30,
            "max_single_position_pct": 0.10,
        },
        # Short selling settings
        "short_selling": {
            "borrow_cost_tiers": {5: 0.005, 15: 0.02, 30: 0.05},
            "borrow_cost_reject_above": 0.05,
            "hard_to_borrow_si_pct": 30,
        },
    },
}
