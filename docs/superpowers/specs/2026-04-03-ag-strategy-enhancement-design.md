# Agricultural Strategy Enhancement Design

## Goal

Upgrade the `weather_ag` strategy from a seasonal momentum strategy with basic NOAA weather data into a comprehensive agricultural supply disruption strategy backed by three government data sources (NOAA CDO, USDA NASS, US Drought Monitor), an expanded ticker universe, year-round operation, and LLM-driven signal scoring.

## Architecture

Two new data source modules (`usda_source.py`, `drought_monitor_source.py`) feed alongside the existing `noaa_source.py` into a rewritten `weather_ag` strategy. The strategy uses rule-based filtering to gate candidates, then delegates scoring to the LLM analyzer — matching the pattern used by `filing_analysis`, `supply_chain`, `litigation`, and other strategies.

## Data Sources

### 1. USDA NASS QuickStats (`usda_source.py`)

**API:** `https://quickstats.nass.usda.gov/api/api_GET/`
**Auth:** API key as query parameter (`?key=...`). Key stored in `.env` as `USDA_NASS_API_KEY`, config key `usda_nass_api_key`.
**Rate limits:** 50,000 records per request. No explicit throttle.

**Method: `fetch_crop_progress(commodity, year, states)`**

Queries NASS for weekly crop condition ratings. Returns list of weekly snapshots with:
- `week_ending`: date string
- `commodity`: CORN, SOYBEANS, or WHEAT
- `state`: state alpha code
- `excellent_pct`, `good_pct`, `fair_pct`, `poor_pct`, `very_poor_pct`: condition distribution

**Query parameters:**
```
commodity_desc=CORN
statisticcat_desc=CONDITION
unit_desc=PCT OF AREA PLANTED
freq_desc=WEEKLY
year=2026
state_alpha=IA,IL,KS,NE,MN,IN,OH,SD,ND,MO
format=JSON
```

**Fetch cadence:** Weekly. The strategy checks if the latest Monday has data not yet cached. USDA publishes crop progress on Monday afternoons during the growing season (April-November). Winter wheat condition data is published less frequently (monthly Nov-Mar).

**Caching:** Keyed by `(commodity, year)`. Full year fetched once, then reused — the historical data is immutable.

**Graceful degradation:** Returns empty dict if API key missing or request fails.

### 2. US Drought Monitor (`drought_monitor_source.py`)

**API:** `https://usdmdataservices.unl.edu/api/StateStatistics/GetDroughtSeverityStatisticsByAreaPercent`
**Auth:** None required. Fully public.
**Rate limits:** None documented.

**Method: `fetch_drought_severity(states, start, end)`**

Returns drought category percentages for each state:
- `None`: % not in drought
- `D0`: abnormally dry
- `D1`: moderate drought
- `D2`: severe drought
- `D3`: extreme drought
- `D4`: exceptional drought

**Method: `fetch_composite_score(states, date)`**

Computes a single 0-5 weighted drought score across ag states:
```
score = (D0*0 + D1*1 + D2*2 + D3*3 + D4*4) / 100
```
Averaged across all states. Score of 0 = no drought, 4 = entire region in exceptional drought.

**Fetch cadence:** Daily (single HTTP call, fast). Underlying data updates every Thursday.

**Output format:** JSON via Accept header.

**Graceful degradation:** Returns empty dict on network failure.

### 3. NOAA CDO (existing `noaa_source.py`)

No changes. Already provides `fetch_ag_weather_summary()` with heat stress days, precipitation deficit, frost events, temperature anomaly. Fetched daily.

## Strategy Enhancement (`weather_ag.py`)

### Expanded Ticker Universe

```python
AG_TICKERS_FULL = {
    # ETFs — direct commodity exposure
    "dba": "DBA",    # Invesco DB Agriculture Fund
    "weat": "WEAT",  # Teucrium Wheat Fund
    "corn": "CORN",  # Teucrium Corn Fund
    "moo": "MOO",    # VanEck Agribusiness ETF
    "soyb": "SOYB",  # Teucrium Soybean Fund
    # Stocks — agribusiness companies
    "adm": "ADM",    # Archer-Daniels-Midland
    "bg": "BG",      # Bunge Global
    "ctva": "CTVA",  # Corteva Agriscience
    "de": "DE",      # Deere & Company
    "fmc": "FMC",    # FMC Corporation
}

# Winter subset (Oct-Mar): skip corn/soy-specific instruments
AG_TICKERS_WINTER = {"weat", "dba", "moo", "adm", "bg"}
```

### Year-Round Operation

The hard season gate is removed. Instead, ticker eligibility and data layers vary by season:

- **Apr-Sep (growing season):** All 10 tickers. All four data layers (NOAA, Drought Monitor, USDA crop conditions for corn/soybeans/wheat, momentum).
- **Oct-Mar (off-season):** 5 winter tickers only. Three data layers (Drought Monitor, USDA winter wheat conditions, momentum). NOAA heat/frost signals excluded.

### Signal Flow

```
screen(data, date, params):
    1. Determine season (growing vs off-season)
    2. Select eligible tickers based on season
    3. Gather ag context from all available sources:
       - noaa_data = data.get("noaa", {})
       - drought_data = data.get("drought_monitor", {})
       - usda_data = data.get("usda", {})
       - prices = data.get("yfinance", {}).get("prices", {})
    4. Gate check — is anything interesting happening?
       - Drought: composite score >= 1.0 (moderate drought across region)
       - USDA: week-over-week Good+Excellent decline > 2 percentage points
       - NOAA: heat_stress_days > threshold OR precip_deficit < threshold OR frost > 0
       - Momentum: any ag ticker trailing return > 5%
       If NONE of these, return []
    5. For each eligible ticker with price data:
       - Compute trailing return
       - Bundle ALL raw ag data into metadata
       - Set needs_llm_analysis = True, analysis_type = "ag_weather"
       - Set initial score = 0.5 (LLM will adjust)
       - Emit Candidate
```

### LLM Analysis Prompt

The LLM analyzer receives a structured prompt with all ag data and decides conviction. Added to `LLMAnalyzer` as the `ag_weather` analysis type:

```
You are analyzing agricultural supply disruption risk for {ticker} ({commodity_name}).

WEATHER CONDITIONS (NOAA, last 30 days):
- Heat stress days (TMAX > 95F): {heat_stress_days}
- Precipitation deficit: {precip_deficit_pct}% vs normal
- Late-season frost events: {frost_events}
- Temperature anomaly: {temp_anomaly_f}F above seasonal average

DROUGHT CONDITIONS (US Drought Monitor):
- Composite drought score: {drought_score}/4.0
- States in severe+ drought (D2+): {severe_drought_states}

CROP CONDITIONS (USDA, latest week):
- Corn: {corn_good_excellent}% Good/Excellent (change: {corn_change}pp)
- Soybeans: {soy_good_excellent}% Good/Excellent (change: {soy_change}pp)
- Wheat: {wheat_good_excellent}% Good/Excellent (change: {wheat_change}pp)

PRICE ACTION:
- {ticker} trailing {lookback}-day return: {trailing_return}%

Assess the probability that agricultural supply disruption will drive {ticker} higher
over the next {hold_days} days. Consider:
1. Are weather/drought conditions actually damaging crops, or just concerning?
2. Has the market already priced in the disruption (check momentum)?
3. Is this ticker directly exposed to the affected commodities?

Respond with JSON: {"direction": "long" or "neutral", "score": 0.0-1.0, "reasoning": "..."}
```

### data_sources Update

```python
data_sources = ["yfinance", "noaa", "usda", "drought_monitor", "openbb"]
```

### Parameters

New parameters added to `get_param_space()` and `get_default_params()`:

| Parameter | Range | Default | Description |
|-----------|-------|---------|-------------|
| `drought_min_score` | (0.5, 2.0) | 1.0 | Min composite drought score to trigger gate |
| `crop_decline_threshold` | (1, 5) | 2 | Min week-over-week Good+Excellent decline (pp) |

Existing parameters retained: `lookback_days`, `hold_days`, `min_return`, `heat_stress_threshold`, `precip_deficit_threshold`. Season start/end months removed (year-round now).

## Integration Points

### `multi_strategy_engine.py`

Add to `_fetch_all_data()`:
- `"usda"` fetch via `_fetch_usda_data(trading_date)` — calls `usda_source.fetch_crop_progress()` for corn, soybeans, wheat
- `"drought_monitor"` fetch via `_fetch_drought_data(trading_date)` — calls `drought_monitor_source.fetch_composite_score()` and `fetch_drought_severity()`

Both added to the `api_fetches` dict for parallel I/O.

Add ag stock tickers (ADM, BG, CTVA, DE, FMC, SOYB) to `core_tickers` in `_fetch_yfinance_data()`.

### `llm_analyzer.py`

Add `"ag_weather"` to the analysis type dispatch with the prompt template above.

### `data_sources/registry.py`

Register `USDASource` and `DroughtMonitorSource` in `build_default_registry()`.

### `default_config.py`

Add `usda_nass_api_key` to autoresearch config section.

### `.env.example`

Add `USDA_NASS_API_KEY=`.

## Testing

- Unit tests for `USDASource`: mock HTTP responses, verify crop condition parsing, cache behavior, graceful degradation
- Unit tests for `DroughtMonitorSource`: mock HTTP responses, verify composite score calculation, state-level parsing
- Unit tests for enhanced `WeatherAgStrategy.screen()`: verify gate logic with various data combinations, verify LLM metadata bundling, verify seasonal ticker filtering, verify graceful degradation when sources are missing
- Update `test_multi_strategy.py` strategy count assertion (already at 10)
- Update `test_30day_simulation.py` if any simulation tests reference strategy data sources

## Documentation Updates

- `CLAUDE.md`: Update strategy table entry for `weather_ag`, add `usda_nass_api_key` config entry, update data source count to 12 (yfinance, EDGAR, Finnhub, Regulations.gov, CourtListener, Congress, FRED, USASpending, OpenBB, NOAA, USDA, Drought Monitor)
- `AUTORESEARCH_ARCHITECTURE_MAP.md`: Add USDA and Drought Monitor to data source table
- `.env.example`: Add `USDA_NASS_API_KEY`
