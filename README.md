# EventEdge

An autonomous event-driven trading research system that runs 12 strategies across 16 paper portfolio scenarios (4 time horizons × 4 portfolio sizes). This is a personal research project.

## What it does

Strategies examine events such as SEC filings, insider trades, and congressional disclosures. An LLM portfolio committee combines their signals and sizes positions for each scenario book.

<p align="center">
  <img src="assets/autoresearch.svg" style="width: 100%; height: auto;">
</p>

## The 12 strategies

Each one watches for a different kind of event and generates trade signals:

- Earnings calls: clustering around earnings dates and estimate revisions
- Insider activity: Form 4 filings when executives buy or sell their own stock
- Filing analysis: anomalies in 10-K and 10-Q filings
- Regulatory pipeline: FDA approvals, FCC licenses, other regulatory signals
- Supply chain: stress indicators across supplier/customer networks
- Litigation: SEC enforcement actions and major lawsuits
- Congressional trades: stock trades disclosed by members of Congress
- Government contracts: federal contract awards from USASpending
- State economics: FRED macroeconomic indicators by region
- Weather/agriculture: NOAA weather anomalies, USDA crop conditions, drought severity
- Commodity macro: CFTC COT positioning extremes, futures curves, macro regime alignment
- Quantum readiness: post-quantum cryptography migration signals from SEC filings and news, with regime-based selection among PQC vendor, crypto-exposed and quantum hardware baskets

Event and research inputs come from Finnhub, SEC EDGAR, FMP, FRED, NOAA, USDA, US Drought Monitor, CourtListener, Regulations.gov, USASpending, CFTC and Yahoo/yfinance. OpenBB supplies optional enrichment. Congressional disclosures use the authenticated FMP stable House and Senate feeds; missing access is an explicit coverage failure. The pipeline does not scrape CapitolTrades as a fallback.

## How it runs

Production should be scheduled for 18:00 ET; the repository does not install or change that schedule. Python validates the exact requested XNYS session, including holidays and early closes. A signal observed at one session's close can stage an intent for the next exact XNYS session open.

Each cohort has its own authoritative SQLite `portfolio.db`. It records signals, next-open intents, fills, lots, marks, benchmark observations and account snapshots, with explicit slippage, commission, other fees, borrow costs and financing. JSON files are deterministic projections from SQLite.

Alpaca SIP is the primary source for critical raw daily execution, mark, candidate and reference bars. The adapter requires the exact session's close plus 15 minutes, requests `feed=sip` and `adjustment=raw`, and validates the symbol, session timestamp, pagination and OHLC. Invalid or missing bars fail closed; there is no Yahoo fallback for raw prices. Yahoo remains a declared dependency for corporate actions, dividend-adjusted SPY/BIL benchmarks, research price history, volatility and VIX.

An unresolved candidate-only reference bar or volatility history excludes that candidate from staging and persists a typed input issue. Repeated cohort references become one run-level issue. Existing-position accounting can remain valid while the run is degraded; any ticker needed by an open lot or pending entry remains governed and fail-closed.

Shared acquisition has one bounded deadline, including provider pacing, retries and fanout queue time. Transient transport failures, timeouts, HTTP 429 and eligible 5xx responses receive bounded retries with backoff, jitter and `Retry-After`; malformed data and authentication failures are terminal. SDK calls obey the cooperative deadline, but an in-flight SDK call that cannot be interrupted may continue after the caller stops waiting. Valid partial results remain available with their failure status. Missing required inputs cannot become a healthy empty result.

Managed workers reuse successful operational source inputs from `data/source_cache`, with a maximum five-minute TTL and session/window/configuration identity checks. Failed or partial results never enter the success cache. Daily screening freezes its shared source bundle before analysis under the generation/session identity. A separate `source_inputs/staging_volatility` bundle freezes accepted volatility histories before committee decisions. An allowed resume reuses both bundles. Accepted execution inputs and source observations remain immutable. Preflight can populate the operational cache without writing accepted generation inputs.

Generations freeze code through git worktrees. Model, reasoning, strategy or other behavior changes require a fresh generation; historical evidence is preserved. Current event analysis and the portfolio committee use OpenAI Responses with `gpt-6-luna` and high reasoning effort. Incomplete or refused responses follow the failure path.

The 16 scenario books share signals and source observations. Performance views show four separate $100k horizon books and an equal-weighted scenario panel; the panel is not investable fund AUM. Smaller books test concentration constraints. Metrics use XNYS sessions, next-session-open outcomes, persisted SPY/BIL benchmarks, explicit costs and immutable schema-v2 epochs. Policy audits count attributed accept, trim and reject decisions, ingress blocks and committee non-selection. These counts are governance evidence, not alpha validation. Production learning is disabled. Promotion output requires Pedro's review against precommitted 30/60/90-session gates and complete benchmark, cost and provenance evidence.

<p align="center">
  <img src="assets/daily-cycle.svg" style="width: 100%; height: auto;">
</p>

The 16 portfolios vary in size ($5k to $100k) and time horizon (30 days to 1 year). Eligible $50k+ scenarios can short stocks with margin and borrow-cost gates. Covered-call execution remains inactive until authoritative premium, assignment, expiry, and contract-mark accounting exists.

## Setup

```bash
git clone <this repo>
cd <repo>
pip install .            # or pip install -e . for development
cp .env.example .env     # add your API keys
```

Set `OPENAI_API_KEY`, `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` in `.env`. Alpaca credentials must permit the historical SIP queries used by the price adapter. Configure each event provider's required key or token, including FMP access to both congressional feeds and an identifying EDGAR User-Agent. See `.env.example` for the full list. Missing required access appears in source coverage and can block governed pricing or degrade strategy inputs.

Daily and preflight worker attempts retain separate JSON evidence under
`data/logs/run_attempts/`. The generation CLI prints the artifact path, and failed
governed checks also print their available ticker/reason details. Each artifact
contains the session, generation commit, process status, captured output, and
structured result; later runs do not overwrite it. Managed timeouts retain partial
output. The latest daily log remains available for existing readers.

Preflight leaves accepted generation inputs and ledgers unchanged. Daily outcomes
carry required-source and strategy-health failures through each affected cohort
and the generation summary. Accounting validity, source coverage, staging
completion and staging validity are separate checks. Optional enrichment can warn
without becoming a required-input failure.

The deterministic operational report verifies the exact session's manifest,
all attempt artifacts, shared metrics, frozen source bundle and all 16 SQLite
books. It counts distinct fill IDs, preserves preflight incidents even when the
daily run is clean, and writes reports for completed degraded runs. Missing or
conflicting evidence produces explicit diagnostics and withholds performance
claims. JSON is the canonical monitor input; Markdown presents the same facts.
`scripts/daily_trading.sh` generates both files under `docs/reports` in its exit
trap, including after a governed gate or daily failure. Reporting preserves an
existing failure status; an incomplete report makes an otherwise successful
script exit nonzero.

Clef runs an optional evidence check after all books finish staging. It samples
up to five distinct events with retained source evidence and asks whether that
evidence supports the candidate claim and company attribution. Its answers do
not feed trade selection, sizing, accounting or source-health checks. The default
budget is 20 seconds per session, with at most three seconds per request. Events
without usable source evidence are marked insufficient without an API call.

Set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` in the production environment
to activate hosted `@cf/cloudflare/clef`. The token needs Workers AI Read and Edit
permissions for that account ([Cloudflare setup](https://developers.cloudflare.com/workers-ai/get-started/rest-api/)).
Only compact public source fields and the candidate claim go to Cloudflare;
holdings, balances and credentials are excluded. The hosted model name is an
alias, so the recorded name does not establish an immutable weights revision.

Results and pending inputs live in each generation's
`decision_shadow/YYYY-MM-DD.json`. The operational report shows assessed events,
answer counts, sampling limits and errors separately from financial validity.
An attempted request is never repeated automatically, including after a timeout.
Missing credentials and exhausted budgets leave unattempted inputs that can be
processed later with `scripts/run_decision_shadow.py --help`, without running
trading or fetching new evidence. A resume without every horizon records
`incomplete_sampling` when no complete sample was saved. These observations are
for evaluation; support probabilities do not estimate a trade's chance of profit.

Evidence files use restrictive permissions and redact known credential environment
values (including common JSON/repr escaping) and common authentication fields.
Truncated quoted credentials and complete authorization-header lines are redacted;
this does not detect arbitrary unknown secrets. Keep files private: provider
payloads and research data can remain sensitive. Archives accumulate without automatic deletion;
include them in log storage/retention planning. A host loss or hard kill before the
manager finishes can still leave no finalized artifact or a private temporary file.
Temporary-file cleanup failures are logged without masking a completed archive.
Publication is atomic for readers; directory entries are not explicitly synced,
so power-loss durability is not guaranteed. Lock rejection, command
validation failure, and separately invoked report commands are outside this worker
attempt archive. If writing evidence fails, the CLI reports that separately without
changing the worker's outcome or rerunning economic work.

```bash
# Daily run for all active generations
python scripts/run_generations.py run-daily --date 2026-07-31

# Read-only operational report for all active generations
python scripts/generate_operational_report.py --repo-root /path/to/repo \
  --all-active --date 2026-07-31 --output-dir /path/to/repo/docs/reports

# Optional direct checks. The scheduled daily script runs the screen first,
# then requires the governed check to be ready before it starts trading.
python scripts/run_generations.py preflight --date 2026-07-31 --preflight-mode screen
python scripts/run_generations.py preflight --date 2026-07-31 --preflight-mode governed

# Start a new generation (A/B test a code change)
python scripts/run_generations.py start "description of what changed"

# List active generations
python scripts/run_generations.py list

# Compare generations side-by-side
python scripts/run_generations.py compare \
  --pair gen_005:horizon_30d_size_100k:candidate_epoch_id,gen_004:horizon_30d_size_100k:baseline_epoch_id

# Check five consecutive XNYS sessions in a generation. This reads state only.
python scripts/check_generation_readiness.py --repo /path/to/production/repo \
  --generation gen_NNN --expected-commit FULL_40_CHARACTER_SHA \
  --through 2026-09-21

# Streamlit dashboard (interactive, in a browser)
python -m streamlit run tradingagents/dashboard/app.py

# Email-able HTML snapshot (forward to yourself in Gmail)
python scripts/email_dashboard.py
```

The readiness command exits 0 only when all five sessions have one clean daily
result, valid completed accounting and SPY/BIL benchmarks in all 16 cohorts,
completed staging in all 16 cohorts, no quarantined candidate bars or input
issues, and healthy evidence from all 12 strategies across four horizons. It
fails closed on missing or inconsistent records. This is a continuity check
for a generation. It does not establish a performance result.
Review incident-specific replay tests before launching a candidate and apply
the separate 30/60/90-session performance gates before any strategy promotion.

Run the checker while the runtime is idle: it requires the existing canonical
runtime lock and refuses a busy lock without creating or changing one. SQLite
reads use temporary copies, including committed WAL data; source fingerprints
must remain unchanged. The frozen generation worktree must match the full SHA
and have no tracked modifications. Evidence must match the generation epoch,
cohort, and exact strategy/horizon policies. If runtime configuration overrides
`autoresearch.paper_ledger.policy_id`, pass that same value with `--policy-id`.

Docker works too:
```bash
docker compose run --rm tradingagents
```

## Origin

This started as a fork of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents), an open-source multi-agent trading framework from [this paper](https://arxiv.org/abs/2412.20138). The original 6-agent debate pipeline was the seed; the autoresearch system, the strategies, the generation management, the portfolio committee, and the paper trading infrastructure were all built on top. The original pipeline code has since been removed since the project's focus narrowed to the autoresearch experiment.

## License

Code attributable to TauricResearch is Apache 2.0 (see `LICENSE-APACHE`). All other code is proprietary (see `LICENSE` and `NOTICE`).

Not financial advice. Not investment advice. Not trading advice.
