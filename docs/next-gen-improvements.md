# Next Generation Improvements

## 1. Loosen Strategy Gates — Let the LLM Use Its Judgment

Current problem: Most strategies have restrictive rule-based gates that filter out candidates before the LLM ever sees them. This defeats the purpose of having LLM-driven scoring — the LLM is best at weighing ambiguous signals, but it never gets the chance if hard gates reject everything first.

**What to change:**
- Lower or remove hard thresholds on each strategy's gate logic. The gate should be a coarse "is there any data at all?" check, not a conviction filter.
- Example: `weather_ag` currently requires drought composite >= 1.0 OR heat days >= 5 OR precip deficit < -25%. These thresholds are too tight — a drought score of 0.8 with moderate heat and slight precip deficit could still be meaningful in combination, but gets rejected.
- Same pattern across other strategies: `earnings_call` requires specific Finnhub event data, `regulatory_pipeline` gates on keyword matches, `litigation` gates on case type, etc.
- The LLM should be the one deciding "is this interesting enough to trade?" — the gate should only decide "is there enough data to form an opinion?"

## 2. Expand Ticker Universes — Stop Fishing in a Small Pond

Current problem: Each strategy operates on a narrow, hardcoded ticker universe. The system can only generate signals for tickers it already knows about, missing opportunities in the broader market.

**What to change:**
- Expand from hardcoded ticker lists to dynamic screening. Use the existing `sp500_nasdaq100` universe config to cast a wider net.
- For `weather_ag`: The 10-ticker universe (5 ETFs + 5 stocks) is reasonable for direct ag exposure, but consider adding food/beverage companies (PEP, KO, GIS, MDLZ) and fertilizer/chemical names (MOS, NTR, CF) that are also weather-sensitive.
- For other strategies: `supply_chain` should scan all S&P 500 names for disruption news, not just companies with pre-mapped chains. `litigation` should match any defendant ticker, not just known targets. `congressional_trades` already covers broad tickers via the API — good model for others.
- Consider a two-stage approach: broad scan for signals across the full universe, then deep analysis on the hits.
