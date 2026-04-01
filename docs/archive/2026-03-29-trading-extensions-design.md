# TradingAgents Extensions: Options, Backtesting, Execution, Dashboard & Scheduling

**Date:** 2026-03-29
**Status:** Approved
**Approach:** Fork & Extend (Approach C)

## Context

Extending TradingAgents v0.2.2 to support a retail investor running a $5k aggressive portfolio with a <6 month time horizon. The user is new to options and wants the system to recommend appropriate strategies, validate decisions through backtesting, execute via Alpaca, and monitor via dashboard + alerts.

## Dependencies (New)

| Library | Version | Purpose |
|---|---|---|
| `py_vollib` | latest | Options Greeks & IV calculation |
| `vectorbt` | latest | Backtest analytics (Sharpe, drawdown, trade stats) |
| `alpaca-py` | latest | Broker execution (paper + live) |
| `streamlit` | latest | Dashboard UI |
| `plotly` | latest | Interactive charts |
| `apprise` | latest | Multi-channel notifications (Slack, email, Telegram, SMS) |
| `apscheduler` | latest | Background job scheduling |

Existing dependencies retained: `yfinance` (options chain data), `typer`/`rich` (CLI), `langgraph`/`langchain` (agent framework).

---

## Section 1: Options Analyst Agent

### New Files

```
tradingagents/dataflows/options_data.py
tradingagents/agents/analysts/options_analyst.py
tradingagents/agents/utils/options_tools.py
```

### Data Tools

Three new tools registered in `dataflows/interface.py`:

**`get_options_chain(symbol, date)`**
- Source: `yfinance` Ticker.options + Ticker.option_chain()
- Returns: available expirations, strikes, bid/ask, volume, open interest, last price
- Cached locally following existing `data_cache_dir` pattern
- Filters to expirations within 6 months of trade date

**`get_options_greeks(symbol, expiration, strike, option_type)`**
- Source: `py_vollib` (Black-Scholes)
- Inputs: underlying price (from yfinance), strike, expiration, risk-free rate (10yr Treasury yield)
- Returns: IV, delta, gamma, theta, vega
- Calculates IV from market price via `py_vollib.black_scholes.implied_volatility`

**`get_put_call_ratio(symbol)`**
- Source: derived from options chain data (total put OI / total call OI)
- Returns: ratio + interpretation (bullish/bearish/neutral threshold)

### Agent: Options Analyst

Pattern matches existing analysts: system prompt + tool bindings + conditional tool execution loop.

**System prompt instructs the agent to:**
1. Analyze the options chain for the ticker
2. Assess IV percentile (is IV high or low relative to recent history?)
3. Read put/call ratio for sentiment signal
4. Recommend 1-3 options strategies appropriate for:
   - A small account ($5k)
   - The user's beginner experience level
   - The current market conditions from other analysts' reports
5. For each recommended strategy, provide:
   - Strategy name and explanation of how it works
   - Specific strikes and expirations
   - Max risk (in dollars)
   - Max reward (in dollars or unlimited)
   - Breakeven price(s)
   - Why this strategy fits the current situation

**Safety constraints baked into prompt:**
- Only recommend defined-risk strategies (no naked calls/puts)
- Max risk per trade: configurable, default 5% of portfolio ($250 on $5k)
- Explain each strategy in beginner-friendly language
- Flag if IV is elevated (strategies may be overpriced)

### Graph Integration

- Options analyst runs in parallel with the other 4 analysts (same graph tier)
- `AgentState` gets new field: `options_report: str`
- Bull/bear researchers receive `options_report` in their prompt context
- Risk managers see options recommendations and debate suitability
- Portfolio manager's final decision can now include options actions

### Config Addition

```python
"options": {
    "enabled": True,
    "max_expiry_months": 6,
    "max_risk_per_trade_pct": 0.05,
    "strategies_allowed": ["long_call", "long_put", "vertical_spread", "straddle", "strangle"],
}
```

---

## Section 2: Backtesting Engine

### New Files

```
tradingagents/backtesting/
├── __init__.py
├── engine.py          # Main backtest loop
├── portfolio.py       # Position tracking, P&L calculation
├── metrics.py         # Performance & accuracy analytics
└── report.py          # Generate backtest summary reports
```

### Engine (`engine.py`)

The `Backtester` class wraps `TradingAgentsGraph.propagate()` in a date loop:

```python
class Backtester:
    def __init__(self, config: dict)
    def run(self, tickers: List[str], start_date: str, end_date: str) -> BacktestResult
```

**Loop logic per trading day:**
1. Call `ta.propagate(ticker, date)` to get the full decision
2. Parse decision into order(s) via the same position manager logic used in live execution
3. Simulate fill at next-day open price (avoids lookahead bias)
4. Update portfolio state: positions, cash, unrealized P&L
5. Check stop-losses and profit targets on existing positions
6. Call `ta.reflect_and_remember(realized_returns)` on closed positions so agents learn
7. Log everything to the backtest result

**Options simulation:**
- Options positions valued using Black-Scholes via `py_vollib` at each date step
- Simulates expiration: ITM options exercised/assigned, OTM expire worthless
- Early exit if stop-loss or profit target hit

**Trading frequency:** Configurable — daily, weekly (default), or custom schedule. Weekly avoids excessive LLM calls during backtesting.

### Portfolio Tracker (`portfolio.py`)

```python
class Portfolio:
    def __init__(self, initial_capital: float)
    def execute_order(self, order: Order, fill_price: float, date: str)
    def update_prices(self, prices: Dict[str, float], date: str)
    def get_equity_curve(self) -> pd.DataFrame
    def get_positions(self) -> List[Position]
    def get_trade_log(self) -> pd.DataFrame
```

Tracks:
- Cash balance
- Open positions (stock + options) with entry price, quantity, date
- Realized + unrealized P&L per position
- Full equity curve (date → total portfolio value)
- Complete trade log (entry, exit, P&L, hold duration)

Applies configurable slippage (default 10 bps) and commissions (default $0 for Alpaca).

### Metrics (`metrics.py`)

Uses `vectorbt` for fast computation on the equity curve and trade log.

**Accuracy metrics:**
- Decision accuracy: % of Buy decisions where price increased within N days (configurable: 5, 10, 30)
- Precision/recall per rating tier (Buy, Overweight, Hold, Underweight, Sell)
- Comparison vs naive buy-and-hold on same tickers and period

**Performance metrics:**
- Total return, annualized return
- Sharpe ratio, Sortino ratio
- Max drawdown (peak-to-trough), average drawdown
- Win rate (% of trades that were profitable)
- Profit factor (gross profit / gross loss)
- Average trade P&L, average winner, average loser
- Options-specific: premium spent vs collected, assignment rate

### Report (`report.py`)

Generates:
- Markdown summary (saved to `results/backtests/`)
- CSV trade log export
- Equity curve data for dashboard charting

### Config Addition

```python
"backtest": {
    "initial_capital": 5000,
    "max_position_pct": 0.35,
    "max_options_risk_pct": 0.05,
    "slippage_bps": 10,
    "commission_per_trade": 0,
    "trading_frequency": "weekly",
    "accuracy_windows": [5, 10, 30],
}
```

---

## Section 3: Execution Layer (Alpaca)

### New Files

```
tradingagents/execution/
├── __init__.py
├── base_broker.py       # Abstract broker interface
├── paper_broker.py      # Local simulation (no API)
├── alpaca_broker.py     # Alpaca paper + live
└── position_manager.py  # Decision parser + risk enforcement
```

### Broker Interface (`base_broker.py`)

```python
class BaseBroker(ABC):
    @abstractmethod
    def submit_stock_order(self, symbol: str, side: str, qty: int, order_type: str = "market") -> OrderResult

    @abstractmethod
    def submit_options_order(self, symbol: str, expiry: str, strike: float, right: str, side: str, qty: int) -> OrderResult

    @abstractmethod
    def get_positions(self) -> List[Position]

    @abstractmethod
    def get_account(self) -> AccountInfo

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool
```

### Three Execution Modes

**`PaperBroker`** — pure local simulation:
- Instant fills at current market price from yfinance
- Tracks positions in-memory
- No API calls, works offline
- Used by backtesting engine

**`AlpacaBroker(paper=True)`** — Alpaca sandbox:
- Real order flow through Alpaca's paper trading API
- Realistic fills with market hours, partial fills, queuing
- Uses `alpaca-py` official SDK
- Good for validation before going live

**`AlpacaBroker(paper=False)`** — live trading:
- Real money, real orders
- Identical code path to paper, just different API endpoint
- Requires explicit `execution_enabled: True` + `mode: "live"` in config

### Position Manager (`position_manager.py`)

Sits between the portfolio manager's natural language decision and the broker:

**Decision parsing:**
- Uses an LLM call via `quick_think_llm` (same pattern as existing `SignalProcessor`) to extract structured order parameters from the portfolio manager's text output
- Parses: action (buy/sell), instrument (stock/option), quantity/dollar amount, option details (strike, expiry, strategy type)
- Returns a structured `Order` object

**Risk enforcement (pre-submission checks):**
- Max position size: rejects if order would put >35% of portfolio in one name
- Max options risk: rejects if premium exceeds 5% of portfolio
- No naked short options: rejects any short option without a covering leg
- Buying power check: rejects if insufficient funds
- Daily loss limit: halts all trading if portfolio down >10% intraday

**If any check fails:** logs the rejection reason, alerts the user, does not submit the order.

### Safety Features

- **Kill switch:** `execution_enabled: false` by default in config. Must be explicitly enabled.
- **Confirmation mode:** when `confirm_before_trade: true` (default), prints order details and prompts "Confirm? (y/n)" before live submission
- **Daily loss limit:** configurable % (default 10%). Once hit, all pending orders cancelled and no new orders accepted until next trading day.
- **Paper-first default:** CLI and config default to paper mode. Live requires `--live` flag AND config change.

### Config Addition

```python
"execution": {
    "mode": "paper",                  # paper, live, dry_run
    "broker": "alpaca",
    "confirm_before_trade": True,
    "daily_loss_limit_pct": 0.10,
    "execution_enabled": False,
}
```

API keys (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`) stored in `.env` alongside existing keys.

---

## Section 4: Dashboard (Streamlit)

### New Files

```
tradingagents/dashboard/
├── __init__.py
├── app.py              # Streamlit entry point
├── pages/
│   ├── portfolio.py    # Positions, P&L, account balance
│   ├── analysis.py     # Agent reports per ticker
│   ├── backtest.py     # Backtest results visualization
│   └── trades.py       # Trade history log
└── components/
    ├── charts.py       # Plotly chart helpers
    └── formatters.py   # Format decisions/reports for display
```

### Pages

**Portfolio (home page):**
- Account balance, buying power, daily/total P&L (numbers + % change)
- Open positions table: ticker, type (stock/option), qty, entry price, current price, unrealized P&L, % change
- Equity curve chart (plotly line chart, interactive zoom)
- Risk exposure: pie chart of allocation by ticker, bar chart of stock vs options exposure

**Analysis:**
- Ticker selector dropdown
- Tabbed view of latest reports: Fundamentals, Technical, News, Sentiment, Options
- Collapsible sections for bull/bear debate and risk debate summaries
- Final decision card with color-coded rating badge
- "Run Analysis" button triggers `propagate()`, streams progress updates

**Backtest:**
- Equity curve with buy-and-hold comparison overlay
- Metrics summary table (Sharpe, drawdown, win rate, etc.)
- Per-trade scatter plot (entry date vs P&L)
- Accuracy breakdown: bar chart by rating tier
- Trade log table with sorting and filtering

**Trades:**
- Full history table: date, ticker, action, instrument, qty, price, P&L, status
- Filters: date range, ticker, strategy type, profit/loss
- CSV export button

### Charts (`charts.py`)

Uses `plotly` for all visualizations:
- Equity curve (line)
- P&L distribution (histogram)
- Position allocation (pie)
- Options Greeks visualization (line charts for delta/gamma/theta over strikes)
- IV surface (3D surface plot of IV across strikes and expirations)

### Launch

```bash
tradingagents dashboard
# Runs: streamlit run tradingagents/dashboard/app.py
```

---

## Section 5: Data Persistence (SQLite)

### New Files

```
tradingagents/storage/
├── __init__.py
├── db.py               # SQLite connection + schema init
├── models.py           # Table definitions
└── queries.py          # Common queries
```

### Schema

**`decisions` table:**
- id, ticker, trade_date, rating, full_decision_text, options_report, created_at

**`trades` table:**
- id, decision_id, ticker, instrument_type (stock/option), action (buy/sell), quantity, price, option_details (JSON: strike, expiry, strategy), status (filled/cancelled/rejected), pnl, created_at

**`reports` table:**
- id, decision_id, ticker, trade_date, report_type (fundamentals/technical/news/sentiment/options/debate), content, created_at

**`backtest_runs` table:**
- id, tickers (JSON array), start_date, end_date, config (JSON), metrics (JSON), created_at

**`equity_snapshots` table:**
- id, backtest_run_id (nullable for live), date, portfolio_value, cash, positions_value

### Usage

All modules write to SQLite:
- `propagate()` → stores decision + reports
- Execution layer → stores trades
- Backtester → stores backtest runs + equity snapshots
- Dashboard reads from all tables

Database file location: `{results_dir}/tradingagents.db` (configurable).

---

## Section 6: Scheduling & Alerts

### New Files

```
tradingagents/scheduler/
├── __init__.py
├── scheduler.py        # APScheduler job management
├── jobs.py             # Job definitions
└── alerts.py           # Apprise notifications
```

### Scheduled Jobs

| Job | Default Schedule | Action |
|---|---|---|
| Daily scan | Weekdays 7:00 AM ET | `propagate()` on watchlist tickers. Store results. Alert on Buy/Sell ratings. |
| Portfolio check | Weekdays 10:00 AM + 3:00 PM ET | Check open positions against stop-losses and profit targets. Alert if triggered. |
| Weekly backtest | Sunday 8:00 PM ET | Re-run backtest on active tickers with latest data. Update metrics in DB. |

All schedules configurable. Skips market holidays.

### Alerts (`alerts.py`)

Uses `apprise` for multi-channel delivery:

**Alert types:**
- `new_signal`: "SOFI rated BUY. Options analyst recommends Nov 10C spread. Max risk: $240."
- `stop_loss`: "PLTR hit stop at $22.50 (down 8%). Recommending exit."
- `target_hit`: "MARA up 25% since entry. Consider partial profit-taking."
- `daily_summary`: "Portfolio: $5,340 (+2.1%). 3 open positions. 1 new signal (SOFI: BUY)."
- `order_filled`: "Order filled: BUY 2x SOFI Nov 10C @ $1.20. Total cost: $240."
- `risk_warning`: "Daily loss limit approaching (down 8.2%, limit 10%)."

**Channels supported:** Slack, email, Telegram, Discord, SMS (via Twilio), push notifications. Configured via apprise URL strings in config.

### Scheduler Management

```bash
tradingagents scheduler start       # launches background daemon
tradingagents scheduler stop        # graceful shutdown
tradingagents scheduler status      # shows jobs + next run times
```

Uses APScheduler `BackgroundScheduler` running in a separate process (daemonized). Stores scheduler state in SQLite so it survives restarts.

### Config Addition

```python
"scheduler": {
    "enabled": False,
    "watchlist": [],
    "scan_time": "07:00",
    "portfolio_check_times": ["10:00", "15:00"],
    "timezone": "US/Eastern",
    "trading_days_only": True,
},
"alerts": {
    "enabled": False,
    "channels": [],
    "notify_on": ["new_signal", "stop_loss", "target_hit", "daily_summary"],
}
```

---

## Section 7: CLI Extensions

New `typer` subcommands added to existing CLI:

```
tradingagents                                    # existing interactive mode (unchanged)
tradingagents scan SOFI PLTR SMCI                # analyze multiple tickers
tradingagents backtest SOFI --start 2025-09-01 --end 2026-03-01
tradingagents portfolio                          # current positions + P&L
tradingagents trades                             # trade history
tradingagents execute SOFI                       # analyze → execute (with confirmation)
tradingagents dashboard                          # launch Streamlit
tradingagents scheduler start|stop|status        # manage scheduler
tradingagents watchlist add|remove|show SOFI     # manage watchlist
tradingagents config set|show                    # manage configuration
```

**Common flags:**
- `--provider anthropic` — override LLM provider
- `--live` / `--paper` — execution mode override
- `--no-options` — skip options analysis
- `--verbose` — show full agent debate output

Existing interactive mode untouched. New commands are additive.

---

## Build Order

Modules should be built in this order due to dependencies:

1. **Storage (SQLite)** — everything else writes to it
2. **Options data tools + Options analyst agent** — extends the core analysis pipeline
3. **Backtesting engine** — depends on storage + options analyst
4. **Execution layer (Alpaca)** — depends on storage
5. **Dashboard (Streamlit)** — reads from storage, depends on all above
6. **Scheduling & alerts** — orchestrates execution + analysis, depends on all above
7. **CLI extensions** — thin wrappers around all modules

---

## Config Summary

All new config merged into `DEFAULT_CONFIG`:

```python
DEFAULT_CONFIG = {
    # ... existing config unchanged ...

    "options": {
        "enabled": True,
        "max_expiry_months": 6,
        "max_risk_per_trade_pct": 0.05,
        "strategies_allowed": ["long_call", "long_put", "vertical_spread", "straddle", "strangle"],
    },
    "backtest": {
        "initial_capital": 5000,
        "max_position_pct": 0.35,
        "max_options_risk_pct": 0.05,
        "slippage_bps": 10,
        "commission_per_trade": 0,
        "trading_frequency": "weekly",
        "accuracy_windows": [5, 10, 30],
    },
    "execution": {
        "mode": "paper",
        "broker": "alpaca",
        "confirm_before_trade": True,
        "daily_loss_limit_pct": 0.10,
        "execution_enabled": False,
    },
    "scheduler": {
        "enabled": False,
        "watchlist": [],
        "scan_time": "07:00",
        "portfolio_check_times": ["10:00", "15:00"],
        "timezone": "US/Eastern",
        "trading_days_only": True,
    },
    "alerts": {
        "enabled": False,
        "channels": [],
        "notify_on": ["new_signal", "stop_loss", "target_hit", "daily_summary"],
    },
}
```
