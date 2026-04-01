# Autoresearch Strategy Discovery — Design Spec

## Goal

Build an autonomous strategy discovery system that screens the market, proposes trading strategies via LLM, backtests them using the full TradingAgents 6-agent pipeline, reflects on results, and evolves better strategies over generations. Top strategies graduate to paper trading for live validation, then to real trading.

## Constraints

- Portfolio: <$5,000
- Holding period: <60 days (options and short-term stock trades)
- Risk: defined-risk only (no naked short options)
- Max risk per trade: $250 (5% of portfolio)
- Single-ticker strategies only (no pairs, no baskets)
- Must use the full TradingAgents agent pipeline — that's the product's edge
- API cost budget: ~$130-200 to reach first live trade

## 3-Phase Flow

```
PHASE 1: AUTORESEARCH (batch, ~$130 total)
  10-15 generations of strategy evolution
  Full pipeline backtesting with cache-first architecture
  Output: leaderboard of top strategies
  Duration: a few hours

PHASE 2: PAPER TRADING (automated, ~$60 over 2-3 weeks)
  Top 3-5 strategies run daily via scheduler
  Full pipeline with Sonnet confirms each signal
  Results feed back to autoresearch for future runs
  Duration: 2-3 weeks, need 5+ completed trades per strategy

PHASE 3: LIVE TRADING (user-initiated, ~$3-5/day)
  Graduated strategies show on dashboard as "READY"
  User approves, execution layer handles trades
  Ongoing monitoring, degraded strategies get retired
```

---

## Strategy Schema

A strategy is a ticker-agnostic set of rules that gets applied to whatever tickers pass its screener criteria.

```python
@dataclass
class Strategy:
    id: int                           # auto-increment
    generation: int                   # which evolution cycle produced this
    parent_ids: list[int]             # strategies it was derived from (empty for gen 0)

    # What to screen for
    screener: ScreenerCriteria
    #   market_cap_range: [float, float]
    #   min_avg_volume: int
    #   sector: str | None              # None = any
    #   min_options_volume: int | None   # None = stock-only strategy
    #   custom_filters: list[Filter]     # e.g., {"field": "rsi_14", "op": "<", "value": 35}

    # What instrument to use
    instrument: str                   # stock_long, long_call, long_put,
                                      # bull_call_spread, bear_put_spread,
                                      # straddle, strangle

    # When to enter
    entry_rules: list[str]            # e.g., ["RSI_14 crosses above 30", "price > EMA_10"]

    # When to exit
    exit_rules: list[str]             # e.g., ["50% profit target", "25% stop loss", "14 DTE"]

    # Risk sizing
    position_size_pct: float          # % of portfolio per trade
    max_risk_pct: float               # max $ at risk per trade as % of portfolio
    time_horizon_days: int            # max holding period

    # LLM reasoning
    hypothesis: str                   # why this strategy should work
    conviction: int                   # 1-100, from strategist

    # Backtest results (filled after backtesting)
    backtest_results: BacktestResults | None
    #   sharpe: float
    #   total_return: float
    #   max_drawdown: float
    #   win_rate: float
    #   profit_factor: float
    #   num_trades: int
    #   tickers_tested: list[str]
    #   backtest_period: str
    #   walk_forward_scores: list[float]  # sharpe per walk-forward window

    # Status
    status: str                       # proposed, backtested, active, paper,
                                      # ready, live, failed, degraded, retired
    regime_born: str                  # RISK_ON, RISK_OFF, CRISIS, TRANSITION
    created_at: datetime
```

---

## Screener

Pure data, no LLM calls. Fetches market data and applies quantitative filters.

### Universe

Start from a configurable static list: S&P 500 + NASDAQ 100 + a curated small-cap watchlist (~600 tickers). User can override in config.

### Data Fetched Per Ticker (yfinance, free)

- **Price:** close, 14-day change, 30-day change, 52-week high/low
- **Volume:** avg 20-day volume, today vs avg ratio
- **Technicals:** RSI-14, EMA-10, EMA-50, MACD, Bollinger band position
- **Options:** IV rank (if options chain exists), put/call ratio, total options volume
- **Fundamentals:** market cap, sector, revenue growth YoY, next earnings date
- **Regime:** VIX level, turbulence index

### Regime Classification (adapted from ATLAS)

```python
def classify_regime(vix: float, yield_curve_slope: float, hy_spread: float) -> str:
    if vix > 30 or hy_spread > 5:
        return "CRISIS"
    if vix > 20 or yield_curve_slope < 0:
        return "RISK_OFF"
    if vix < 15 and yield_curve_slope > 0.5:
        return "RISK_ON"
    return "TRANSITION"
```

Regime tagged on every screener run. Strategies inherit the regime they were discovered in.

### Turbulence Index (from FinRL)

Mahalanobis distance of today's cross-asset returns from the 252-day rolling covariance matrix. When turbulence exceeds threshold, strategies can use it as a "don't enter" filter.

### Survivorship Bias Filter (from FinRL)

Drop tickers with < 80% of expected trading days in the lookback window. Prevents backtesting on delisted or illiquid stocks.

### Caching

Screener results cached in SQLite keyed by `(date, ticker)`. Same-day re-runs are free. Historical screener data accumulates, making future backtests faster.

### Output

`ScreenerResult`: structured data per ticker with all metrics attached, plus current regime label. This is what the strategist LLM receives — it never fetches data itself.

---

## Strategist Agent

Single LLM agent that proposes and evolves strategies. Not the full 6-agent pipeline — this is the "scientist" that designs experiments.

### Input Per Generation

- Screener results (40-50 tickers with all data attached)
- Current market regime label
- Top 5 strategies from previous generations (with full backtest results)
- Reflection notes from all previous generations
- Analyst weight history (which agents have been most accurate)
- Constraints reminder (<60 day, defined-risk, max $250/trade, <$5k)

### Output

3-5 strategy templates as structured JSON matching the Strategy schema. Each includes a hypothesis (why it should work) and conviction score (1-100).

### Adversarial CRO Review (from ATLAS)

Before backtesting, each proposed strategy gets a second LLM call — a "Chief Risk Officer" prompted to find every reason the strategy will fail:

- Concentration risk
- Liquidity issues
- Overfitting to recent data
- Regime dependency
- Unrealistic entry/exit assumptions
- Transaction costs eating the edge
- Survivorship bias

Strategies that survive proceed to backtest. Objections are stored and shown to the strategist next generation so it can address them.

### Model Choice

- **Sonnet** for strategist hypothesis generation (needs reasoning quality)
- **Haiku** for CRO adversarial review (just needs to poke holes)

### Cost Per Generation

~$0.15 (one Sonnet call + one Haiku call per strategy)

---

## Backtesting with Full Pipeline

Each strategy is backtested using the actual TradingAgents 6-agent pipeline. A cache-first architecture makes this affordable.

### How a Strategy Gets Backtested

1. Strategy's screener criteria are applied to historical data to find matching tickers
2. For each ticker, for each weekly date in the backtest window:
   - **Cache check:** Is there a full pipeline result for `(ticker, date)` in SQLite?
   - **Cache hit:** Use the cached rating, reports, and decision
   - **Cache miss:** Run the full pipeline with Haiku, cache the result
3. Evaluate: does the cached rating + market data match the strategy's entry rules?
4. If entry signal fires, track the position through subsequent dates until exit rules trigger
5. Record trade result (entry price, exit price, P&L, holding period)

### Walk-Forward Validation (from FinRL)

Every backtest uses rolling windows. A strategy must perform across ALL test windows.

```
Window 1: Train Sep 2025 - Dec 2025 → Test Jan 2026
Window 2: Train Oct 2025 - Jan 2026 → Test Feb 2026
Window 3: Train Nov 2025 - Feb 2026 → Test Mar 2026

Reported metrics come from test windows only.
```

### Out-of-Sample Holdout

The most recent 6 weeks of data are completely reserved. The system never touches them during autoresearch. After all generations complete, top 10 strategies are tested against the holdout. Any strategy that degrades >30% vs walk-forward score is flagged as likely overfit.

### Cross-Ticker Validation

A strategy discovered on 5 tickers must also be tested on tickers it was NOT designed for. If it only works on the original 5, it's curve-fit. If it works across 15+ different names, the pattern is real.

### Regime Diversity Requirement

A strategy needs trades across at least 2 regime types to graduate. Strategies that only fire during one regime are flagged as regime-fit.

### Cache Economics

```
Generation 0 (cold cache):
  4 strategies × 5 tickers × 24 weekly dates = 480 pipeline runs
  480 × $0.10 (Haiku) = $48

Generation 1: ~60% cache hit → ~190 new runs = $19
Generation 3: ~80% cache hit → ~95 new runs = $9.50
Generation 5+: ~90% cache hit → ~50 new runs = $5
Generation 10+: ~95% cache hit → ~25 new runs = $2.50

Cumulative 15 generations: ~$130
Cache asset: ~1,300 permanently stored pipeline results
```

### Darwinian Analyst Weights (from ATLAS)

Each cached pipeline result includes per-analyst reports. After backtesting, the system scores which analysts correlated with profitable trades:

- For each completed trade, score each analyst's contribution (+1 if their signal aligned with the profitable outcome, -1 if not)
- Top-quartile analysts: weight × 1.05
- Bottom-quartile: weight × 0.95
- Weight range: 0.3 to 2.5

These weights get injected into the portfolio manager's prompt during paper and live trading:
```
"Weight the options analyst's input at 1.8x (historically most accurate)
 and the sentiment analyst at 0.6x (historically least reliable for this strategy type)"
```

### Scoring Formula

```python
# Base fitness
fitness = sharpe * min(profit_factor, 3.0) * (1 - abs(max_drawdown))

# Complexity penalty — simpler strategies generalize better
num_filters = len(strategy.screener.custom_filters)
num_rules = len(strategy.entry_rules)
complexity = num_filters + num_rules
complexity_penalty = 1.0 / (1.0 + 0.1 * complexity)

fitness *= complexity_penalty

# Tiebreakers:
# - Higher win rate preferred
# - More trades preferred (statistical significance)
# - Strategies with < 5 trades in test windows: "insufficient data"
```

---

## Reflection & Evolution

After each generation's backtests complete, the system learns.

### Reflection Call

One Sonnet LLM call receives all strategies from this generation (with backtest results), top 5 all-time strategies, previous reflection notes, and current analyst weights.

### Reflection Output (stored in SQLite)

```json
{
  "generation": 8,
  "patterns_that_work": ["..."],
  "patterns_that_fail": ["..."],
  "next_generation_guidance": ["..."],
  "regime_notes": "..."
}
```

This accumulates — every future strategist call receives the full reflection history, giving the system a growing understanding of what works.

### Strategy Lifecycle

```
PROPOSED → BACKTESTED → ACTIVE → PAPER → READY → LIVE
                ↓                   ↓               ↓
             RETIRED             FAILED          DEGRADED
                                                    ↓
                                                 RETIRED
```

- **ACTIVE:** Passed walk-forward validation, on the leaderboard
- **PAPER:** Graduated to paper trading
- **FAILED:** Paper results diverged significantly from backtest expectations
- **READY:** Paper validated, waiting for user approval
- **LIVE:** Actively trading with real money
- **DEGRADED:** Was LIVE but recent performance dropped below threshold
- **RETIRED:** Removed from active use, kept for historical reference

### Stop Criterion

Don't run a fixed number of generations. Stop when:
- Top strategy hasn't changed for 3 consecutive generations, OR
- Average strategy fitness is declining generation-over-generation, OR
- User-set budget cap reached

The system declares "I've found what I can find" and recommends moving to paper.

---

## Paper Trading & Graduation

### Entry Criteria for Paper

Top 3-5 strategies from the leaderboard with:
- Sharpe > 1.0 across walk-forward test windows
- At least 10 trades in backtest
- Win rate > 50%
- Survived adversarial CRO review
- Passed out-of-sample holdout (< 30% degradation)

### Daily Paper Loop (runs via existing scheduler)

1. Screener runs, finds today's candidates
2. For each PAPER strategy, filter candidates through its screener criteria
3. For matching tickers, run full pipeline with **Sonnet** (real-money decisions deserve the good model)
4. Apply Darwinian analyst weights to portfolio manager prompt
5. If pipeline rating matches strategy's entry rules → execute on paper broker
6. Check open positions for exit signals, close those that trigger
7. Log everything to SQLite linked to strategy_id

### Tracking

Per strategy during paper:
- paper_trades, paper_wins, paper_win_rate
- paper_avg_return, paper_sharpe
- Comparison vs backtest baselines
- Divergence metric (how far paper is from backtest expectations)

### Graduation Criteria (PAPER → READY)

- Minimum 5 completed paper trades (entered AND exited)
- Paper win rate within 15 percentage points of backtest win rate
- Paper Sharpe > 0.5 (absolute floor)
- No single trade lost more than 2x the strategy's stated max risk

### Failure Criteria (PAPER → FAILED)

- Win rate more than 20 points below backtest
- 3 consecutive losses
- Sharpe < 0 after 5+ trades

### Feedback to Autoresearch

Paper results stored alongside backtest results. When autoresearch runs again, the strategist sees how strategies performed in live market conditions vs backtest expectations. This is the most valuable feedback — real market behavior correcting backtest assumptions.

### Cost

~$3-5/day during paper (1-3 Sonnet pipeline runs on signal days, zero on quiet days). Over 3 weeks ≈ $45-75.

---

## Storage (SQLite Extensions)

New tables added to existing `Database` class:

### `strategies` table
```sql
CREATE TABLE strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation INTEGER NOT NULL,
    parent_ids TEXT,                    -- JSON array of parent strategy IDs
    name TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    conviction INTEGER,                -- 1-100
    screener_criteria TEXT NOT NULL,    -- JSON
    instrument TEXT NOT NULL,
    entry_rules TEXT NOT NULL,          -- JSON array
    exit_rules TEXT NOT NULL,           -- JSON array
    position_size_pct REAL,
    max_risk_pct REAL,
    time_horizon_days INTEGER,
    regime_born TEXT,                   -- RISK_ON, RISK_OFF, CRISIS, TRANSITION
    status TEXT DEFAULT 'proposed',     -- proposed/backtested/active/paper/ready/live/failed/degraded/retired
    fitness_score REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### `strategy_backtest_results` table
```sql
CREATE TABLE strategy_backtest_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER REFERENCES strategies(id),
    sharpe REAL,
    total_return REAL,
    max_drawdown REAL,
    win_rate REAL,
    profit_factor REAL,
    num_trades INTEGER,
    tickers_tested TEXT,               -- JSON array
    backtest_period TEXT,              -- "YYYY-MM-DD to YYYY-MM-DD"
    walk_forward_scores TEXT,          -- JSON array of per-window sharpes
    holdout_sharpe REAL,               -- out-of-sample result
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### `strategy_trades` table
```sql
CREATE TABLE strategy_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER REFERENCES strategies(id),
    ticker TEXT NOT NULL,
    trade_type TEXT NOT NULL,          -- backtest, paper, live
    entry_date TEXT,
    exit_date TEXT,
    instrument TEXT,
    entry_price REAL,
    exit_price REAL,
    quantity REAL,
    pnl REAL,
    pnl_pct REAL,
    holding_days INTEGER,
    regime TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### `pipeline_cache` table
```sql
CREATE TABLE pipeline_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    model_tier TEXT NOT NULL,           -- haiku, sonnet
    rating TEXT,
    market_report TEXT,
    sentiment_report TEXT,
    news_report TEXT,
    fundamentals_report TEXT,
    options_report TEXT,
    full_decision TEXT,
    debate_summary TEXT,
    analyst_scores TEXT,               -- JSON: {"market": 1, "news": -1, ...} per-analyst hit/miss
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticker, trade_date, model_tier)
);
```

### `reflections` table
```sql
CREATE TABLE reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation INTEGER NOT NULL,
    patterns_that_work TEXT,            -- JSON array
    patterns_that_fail TEXT,            -- JSON array
    next_generation_guidance TEXT,      -- JSON array
    regime_notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### `analyst_weights` table
```sql
CREATE TABLE analyst_weights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analyst_name TEXT NOT NULL,         -- market, news, sentiment, fundamentals, options
    weight REAL NOT NULL DEFAULT 1.0,   -- range 0.3 to 2.5
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## New Module Structure

```
tradingagents/autoresearch/
    __init__.py
    screener.py             # Market screener with quantitative filters
    strategist.py           # LLM strategy proposal + adversarial review
    evolution.py            # Evolution engine orchestrating the loop
    fitness.py              # Strategy scoring and ranking
    walk_forward.py         # Walk-forward validation splitter
    ticker_universe.py      # Static universe lists + config
```

### Integration Points with Existing Code

- **Screener** uses `tradingagents/dataflows/` (yfinance tools already built)
- **Backtester** reuses `tradingagents/backtesting/engine.py` but adds a `run_with_cache()` method that checks the pipeline cache before calling `propagate()`
- **Paper trading** uses existing `tradingagents/execution/paper_broker.py` and `position_manager.py`
- **Storage** extends existing `tradingagents/storage/db.py` with new tables
- **Scheduling** extends existing `tradingagents/scheduler/jobs.py` with paper trading and evolution jobs
- **Dashboard** gets new pages: Strategy Leaderboard, Evolution History, Paper Trading Monitor
- **CLI** gets new commands: `autoresearch`, `leaderboard`, `paper-status`

---

## Config Additions

```python
DEFAULT_CONFIG["autoresearch"] = {
    "max_generations": 15,
    "strategies_per_generation": 4,
    "tickers_per_strategy": 5,
    "walk_forward_windows": 3,
    "holdout_weeks": 6,
    "min_trades_for_scoring": 5,
    "cache_model": "haiku",                  # model for cached pipeline runs
    "live_model": "sonnet",                  # model for paper/live signals
    "strategist_model": "sonnet",
    "cro_model": "haiku",
    "fitness_min_sharpe": 1.0,               # min sharpe to graduate to paper
    "fitness_min_win_rate": 0.50,
    "fitness_min_trades": 10,
    "paper_min_trades": 5,                   # min completed trades for graduation
    "paper_max_divergence_pct": 15,          # max win rate divergence from backtest
    "analyst_weight_min": 0.3,
    "analyst_weight_max": 2.5,
    "complexity_penalty_factor": 0.1,
    "stop_unchanged_generations": 3,         # stop if top strategy unchanged for N gens
    "universe": "sp500_nasdaq100",           # or custom list
    "budget_cap_usd": 150.0,                # max API spend per autoresearch run
}
```

---

## Cost Summary

| Phase | Duration | Cost |
|-------|----------|------|
| Autoresearch (15 gen) | Few hours | ~$130 |
| Paper trading | 2-3 weeks | ~$45-75 |
| Live trading | Ongoing | ~$3-5/day |
| **Total to first live trade** | **~3-4 weeks** | **~$190** |

Cache is a permanent asset. Subsequent autoresearch runs on the same ticker universe are significantly cheaper (~$30-50 for 15 generations).
