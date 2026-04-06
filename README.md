# TradingAgents

An autonomous trading research system that uses LLMs to analyze stocks and run paper trading strategies. It's a personal project — not a product, not a service, not financial advice.

## What it does

There are two main pieces:

**The core pipeline** simulates a small trading firm. Six AI agents — a fundamentals analyst, sentiment analyst, news analyst, technical analyst, options analyst, and a pair of bull/bear researchers — each look at a stock from their angle. They debate, then a trader agent makes a call. A risk manager and portfolio manager review it before anything happens. You give it a ticker and a date, it gives you a buy/hold/sell decision with reasoning.

**The autoresearch system** is the bigger experiment. It runs 10 event-driven strategies every day across 16 paper portfolios (4 time horizons × 4 portfolio sizes), tracks what works, and learns from the results. Each strategy looks at a specific kind of market signal — not price charts, but things like SEC filings, insider trades, and congressional trading disclosures. A portfolio committee (also LLM-powered) synthesizes the signals and sizes positions.

<p align="center">
  <img src="assets/schema.png" style="width: 100%; height: auto;">
</p>

## The 10 strategies

Each one watches for a different kind of event and generates trade signals:

- **Earnings calls** — clustering around earnings dates and estimate revisions
- **Insider activity** — Form 4 filings (when executives buy or sell their own stock)
- **Filing analysis** — anomalies in 10-K and 10-Q filings
- **Regulatory pipeline** — FDA approvals, FCC licenses, other regulatory signals
- **Supply chain** — stress indicators across supplier/customer networks
- **Litigation** — SEC enforcement actions and major lawsuits
- **Congressional trades** — stock trades disclosed by members of Congress
- **Government contracts** — federal contract awards (USASpending data)
- **State economics** — FRED macroeconomic indicators by region
- **Weather/agriculture** — NOAA weather anomalies, USDA crop conditions, drought severity

Data comes from about a dozen sources: yfinance, Finnhub, SEC EDGAR, OpenBB, FRED, NOAA, USDA, US Drought Monitor, Capitol Trades, CourtListener, Regulations.gov, and USASpending.

## How it runs

Daily cron job on a MacBook Air (16GB M4). The generation management system lets me A/B test different code versions in parallel using git worktrees — each generation gets its own frozen copy of the code and independent state. LLM costs run about $0.03/day per generation using Claude Sonnet and Haiku.

The 16 portfolios vary in size ($5k to $100k) and time horizon (30 days to 1 year). Bigger portfolios unlock more instruments: $10k+ can write covered calls, $50k+ can short stocks with margin and borrow cost gates.

## Setup

```bash
git clone <this repo>
cd TradingAgents
pip install .            # or pip install -e . for development
cp .env.example .env     # add your API keys
```

You'll need API keys for at least one LLM provider (OpenAI, Anthropic, Google, xAI, or OpenRouter) and Alpha Vantage for market data. See `.env.example` for the full list.

```bash
# Interactive CLI — analyze a single ticker
tradingagents

# Daily automation — run all active generations
python scripts/run_generations.py run-daily

# Start a new generation (A/B test a code change)
python scripts/run_generations.py start "description of what changed"

# Dashboard
python -m streamlit run tradingagents/dashboard/app.py
```

Docker works too:
```bash
docker compose run --rm tradingagents
```

## Origin

This started as a fork of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents), an open-source multi-agent trading framework from [this paper](https://arxiv.org/abs/2412.20138). The core pipeline architecture comes from their work. Everything else — the autoresearch system, the strategies, the generation management, the portfolio committee, the paper trading infrastructure — was built on top.

Not financial advice. Not investment advice. Not trading advice.
