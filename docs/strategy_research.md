# Strategy Research: Stack-Ranked Opportunities

> Generated 2026-03-30. Filtered by: not empirically debunked, passes first-principles smell test, free/cheap data, $5K budget, <60 day horizon.

### Implementation Status

**Active (7 paper-trade strategies):** earnings_call, insider_activity, filing_analysis, regulatory_pipeline, supply_chain, litigation, congressional_trades

**Archived (3 backtest strategies, code exists in `strategies/_archive/`):** govt_contracts (B5), state_economics (B10), weather_ag (B9)

**Research only (not implemented):** B1-B4, B6-B8, P7-P9. Academic references below are preserved for future strategy development.

---

## BACKTEST TRACK

Quantitative signals. Evolution engine optimizes parameters via walk-forward validation. No LLM needed for the signal itself. **Phase 1 infrastructure is dormant — no backtest strategies are currently registered.**

### B1. Factor Momentum (ETF Rotation)

- **Signal:** Rotate among factor ETFs (MTUM/VLUE/QUAL/SIZE) based on trailing 1-12 month factor returns. Winning factors keep winning.
- **Why it should work:** Factors are autocorrelated — institutional rebalancing is slow, creating momentum in factor returns themselves.
- **Academic basis:** Ehsani & Linnainmaa 2022 (*Journal of Finance*). Sharpe 1.0-1.15. Average factor earns 1bp/month after a loss year, 53bp after a positive year.
- **Debunked?** No.
- **Data:** yfinance (factor ETFs). Free.
- **Implementation:** Monthly rebalance, 2-4 ETFs. $5K sufficient with fractional shares.
- **Risk:** Momentum crashes (sudden factor reversals, as in COVID March 2020).

### B2. Cross-Asset Momentum (Bonds Lead Stocks)

- **Signal:** Bond ETF returns predict equity returns. Long SPY when TLT signals risk-on.
- **Why it should work:** Bond market has more sophisticated participants, prices risk before equity. Information diffuses from bonds → equities with a lag.
- **Academic basis:** Pitkäjärvi, Suominen & Vaittinen 2020 (*JFE*). Sharpe improvement of 0.32 over standard momentum. Cross-momentum economic gain of 4.11% per annum.
- **Debunked?** No.
- **Data:** yfinance (TLT/IEF/SPY), FRED (yield curve, credit spreads). Free.
- **Implementation:** 2-3 positions, monthly rebalance. $5K sufficient.

### B3. VIX Mean Reversion (Tactical Cash Deployment)

- **Signal:** Buy SPY when VIX spikes >30, sell when it normalizes. Deploy cash opportunistically during panic.
- **Why it should work:** Fear overshoots. VIX >30 reverts 66% of the time within 10 days. You're being paid for providing liquidity during panic.
- **Academic basis:** Structural feature of volatility markets. Nielsen & Posselt 2024. SPY-based VIX timing: risk-adjusted return 44% (5.78% absolute / 13.1% time in market).
- **Debunked?** No.
- **Data:** yfinance (^VIX, SPY). Free.
- **Implementation:** Only invested ~13% of the time. Best as tactical overlay. $5K sufficient.

### B4. PEAD — Post-Earnings Announcement Drift (Quantitative)

- **Signal:** Buy stocks with positive earnings surprises (high SUE), hold 20-60 days.
- **Why it should work:** Investors anchor on prior expectations, underreact to new earnings information. Numeric PEAD has decayed for large-caps, but ML-enhanced multi-quarter SUE still works. Text-based PEAD (PEAD.txt) is even stronger — see P1.
- **Academic basis:** Brogaard et al 2025: ML-enhanced PEAD Sharpe 0.63 (vs 0.34 single-quarter). Microcap Sharpe ~0.86. Lan et al 2024.
- **Debunked?** Partially — traditional single-quarter SUE on large-caps has largely decayed (UCLA Anderson). ML-enhanced and small-cap versions persist.
- **Data:** yfinance (earnings dates, prices), Finnhub (EPS estimates, free tier). Free.
- **Implementation:** Long-only positive surprises. $5K, 3-5 positions. 20-60 day holds.

### B5. Government Contract Awards

- **Signal:** Small-cap wins material federal contract (>10% of revenue). Buy, hold 30-60 days for market to price in.
- **Why it should work:** USAspending.gov publishes awards with 4-6 day lag. Small-cap government contractors are under-followed. A $50M contract to a $200M company is a 25% revenue event that most investors never see.
- **Academic basis:** "Trading on Government Contracts" 2025 (*Economics Letters*): positive cumulative returns. TenderAlpha/FactSet: 5.4-7.1% annual alpha from "Unexpected Government Receivables" signal.
- **Debunked?** No. Novel — few academics have tested this.
- **Data:** USAspending.gov API (free, no key needed). SAM.gov API (free key).
- **Implementation:** Screen for large awards to small-caps. LLM assists with entity resolution (government legal names → tickers). $5K, 2-3 positions.
- **LLM enhancement:** Entity resolution, contract description interpretation, materiality assessment relative to company size.

### B6. 13D Activist Filings

- **Signal:** Activist investor takes 5%+ stake and files 13D. Buy on filing, hold 30-60 days.
- **Why it should work:** Activists create real value through operational changes, spinoffs, buybacks. Market revalues the governance improvement.
- **Academic basis:** Brav et al 2008 (*JoF*): +7% around filing, +11.4% subsequent year. Bebchuk et al 2015 (NBER): +6% sustained. Polk et al 2023: 41-day BHAR of 7.72%.
- **Debunked?** Partially decayed — SEC shortened filing window from 10 to 5 business days (Feb 2024), compressing pre-disclosure alpha. Announcement-day alpha (4-7%) likely persists.
- **Data:** EDGAR RSS feed (free, real-time). WhaleWisdom (free browse).
- **Implementation:** Monitor EDGAR for new SC 13D filings. $5K, 1-2 positions per event.
- **LLM enhancement:** Parse "Item 4: Purpose of Transaction" to classify activist intent (board seats vs. passive crossing). Identity of activist matters enormously.

### B7. Credit Spread Lead-Lag (Risk-Off Overlay)

- **Signal:** When high-yield bond spreads widen sharply, reduce equity exposure or rotate to defensive sectors.
- **Why it should work:** Corporate bond spreads price distress before equity. Institutional credit analysts are more rigorous than equity analysts.
- **Academic basis:** Gilchrist & Zakrajsek 2012 (*AER*): 50bp excess bond premium increase raises recession probability ~7pp. Lee et al 2018: credit leads equity in stress periods.
- **Debunked?** No. Relationship is regime-dependent (strongest during stress).
- **Data:** FRED — ICE BofA HY spread (BAMLH0A0HYM2), IG spread, excess bond premium. Free.
- **Implementation:** Risk overlay — widening spreads → reduce equity, tighten → add. Complements B2.

### B8. FRED Economic Surprise (Sector Rotation)

- **Signal:** When actual economic data beats/misses consensus, rotate across sectors. CPI dominates Nasdaq; NFP dominates S&P.
- **Why it should work:** Markets don't instantly price macro surprises into all affected sectors. Some sectors are slower to react.
- **Academic basis:** Andersen et al 2003 (*AER*). Macrosynergy Research: Sharpe ~0.7, Sortino 1.0-1.1. BUT ~85% of PnL in top 5% of months.
- **Debunked?** No.
- **Data:** FRED (840K series, free). ALFRED for vintage data (backtest integrity).
- **Implementation:** Sector ETF rotation. $5K, 2-3 positions.
- **Caveat:** Need consensus estimates to calculate "surprise." Citigroup CESI is paywalled. Must build DIY from free sources.

### B9. Weather → Agriculture (Seasonal)

- **Signal:** Extreme weather events → crop yield impact → agricultural commodity ETFs and agribusiness stocks.
- **Why it should work:** Weather explains ~33% of crop yield variability. USDA report surprises move ag markets. Causal, physical link.
- **Academic basis:** "USDA Reports Affect the Stock Market, Too" 2024 (*J. Commodity Markets*). Temperature anomalies significantly affect ag futures returns (Energy Economics 2021).
- **Debunked?** No.
- **Data:** NOAA NWS API (free), USDA NASS QuickStats API (free), USDA WASDE (free).
- **Implementation:** Seasonal (April-September US growing season). Trade DBA, MOO, WEAT, CORN, or stocks (ADM, BG, DE, MOS). $5K sufficient.
- **LLM enhancement:** USDA report parsing on release day, weather-to-crop-impact mapping.

### B10. State Economic Leading Indicators

- **Signal:** Weekly initial claims by state lead national data. State-level divergences from national consensus predict regional stock performance.
- **Why it should work:** State data is published but rarely aggregated across 50 states. Market is slow to incorporate geographically distributed information.
- **Academic basis:** Korniotis & Kumar 2013 (*JoF*): state conditions predict local stock returns. Addoum et al 2017: return predictability strongest for difficult-to-arbitrage firms. Composite leading indicator quintile spread: 1.43%/month.
- **Debunked?** No.
- **Data:** FRED (Philly Fed state coincident indexes, initial claims by state). BLS LAUS. Census building permits. All free.
- **Implementation:** Map state conditions to regional banks, retailers, REITs with geographic concentration. $5K sufficient.

---

## PAPER-TRADE TRACK

LLM is the signal. Multi-agent pipeline processes unstructured text. Cannot be meaningfully backtested — validate via live paper trading.

### P1. Earnings Call Tone/Deception + Revision Prediction

- **Signal:** LLM reads earnings call transcript, detects hedging/deception language, predicts analyst revision direction. Trade BEFORE revision published (1-7 day window). Combined PEAD.txt + revision prediction.
- **Why it should work:** Text-based earnings surprise generates 8% annual drift even though numeric PEAD has decayed to zero. CEOs use hedging language when hiding bad news. LLM processes transcript within minutes; analyst revisions take 1-7 days. You front-run the revision.
- **Academic basis:** Meursault et al 2023 (*JFQA*): PEAD.txt 8% annually. Larcker & Zakolyukina 2012: deceptive language detectable. LLM Sharpe 0.64 vs FinBERT 0.13 on earnings calls (CAIA 2024). Estimate revision momentum: 68bps/month, t=6.10 (Mill Street Research). Cook et al 2023 (KC Fed): LLMs predict analyst behavior from transcripts. arXiv 2025: causal link from transcript language to analyst belief updating.
- **Debunked?** No. One of the strongest recent findings.
- **Data:** Free transcripts — Seeking Alpha (~4,500 companies/quarter, 6hrs after call), Finnhub (free tier), API Ninjas (8,000+ companies from 2005), EDGAR 8-K.
- **LLM value:** Very high. "We remain cautiously optimistic about normalizing headwinds" = negative masked as positive. Dictionaries miss this. Segment-level decomposition reveals differential patterns.
- **Implementation:** Process transcript same evening → predict revision direction → trade next morning → hold 10-30 days through post-revision drift. Focus on small/mid-caps (fewer analysts, slower incorporation). ~$0.05-0.10/transcript with Haiku.
- **Why it can't be backtested:** Running LLM on thousands of historical transcripts is expensive and has look-ahead contamination. The interpretive advantage is forward-looking.

### P2. Earnings Call Cross-Referencing

- **Signal:** When Company A mentions Company B on their earnings call, trade Company B. Information diffuses slowly through economic networks.
- **Why it should work:** Cohen & Frazzini 2008: supplier stocks lag customer moves by ~1 month. Hou 2007: larger firms lead smaller ones. "Frog in Every Pan" (JFE 2022): lead-lag is stronger when information arrives in small continuous amounts (exactly like earnings call mentions).
- **Academic basis:** Cohen & Frazzini 2008 (*JFE*, Smith Breeden Prize): 150+ bps/month alpha. Pinchuk 2023 confirms persistence.
- **Debunked?** No. The underlying economic links anomaly is well-established. Nobody has tested LLM-powered cross-referencing specifically — genuine research gap.
- **Data:** Same transcript sources as P1. Free.
- **LLM value:** Highest of all strategies. Entity disambiguation ("our largest cloud customer" → Amazon), sentiment directionality (positive vs negative mention), materiality assessment. No rule-based system can do this at scale across ~4,500 calls/quarter.
- **Implementation:** LLM reads Company A's call → extracts mentions of Company B, C, D → assesses sentiment → generates trade signals for mentioned companies. Hold 10-30 days.

### P3. Filing Change Detection (Lazy Prices + Bloat Stripping)

- **Signal:** Detect material language changes between consecutive 10-K/10-Q filings. Score the change. Trade on material changes in small-caps before the market prices them in.
- **Why it should work:** Firms that substantially change filing language underperform non-changers. MD&A sections are 75% bloat — LLM summaries strip noise and amplify signal.
- **Academic basis:** Cohen, Malloy & Nguyen 2020 ("Lazy Prices," *JoF*): 188bps/month from Risk Factors changes, 71-72bps in robust subsamples. "Bloated Disclosures": LLM summary sentiment = 161bps abnormal return per SD.
- **Debunked?** No. Note: Kim et al 2024 (LLM beats analysts) was WITHDRAWN (Feb 2025) — do not rely on that paper. Lazy Prices itself used cosine similarity, not LLMs.
- **Data:** EDGAR (free). `edgartools` Python library for parsing.
- **LLM value:** Very high. Cosine similarity catches surface-level word changes; LLM understands whether a new risk factor is boilerplate compliance language vs. genuine material risk. Bloat stripping is pure LLM capability.
- **Implementation:** Monitor EDGAR for new 10-K/10-Q filings on small-caps (<$2B). Compare to prior period. Score changes. Long-only, 2-5 positions, 30-60 day holds.

### P4. Insider Cluster Buy + Filing Context

- **Signal:** When 2+ C-suite insiders buy within 2 days AND concurrent filing shows material changes, buy. Filter out routine purchases.
- **Why it should work:** ~70% of solo insider purchases produce no alpha. Cluster buys (2+ insiders within 2 days) are significantly more informative. Combined with material filing change context, you're identifying the ~30% of insider buys with genuine conviction.
- **Academic basis:** Alldredge & Blank 2019: cluster buys earn 2.1%/month, 0.9% higher than solo. Lakonishok & Lee 2001: insider purchases outperform by 4.8% risk-adjusted. ~50% of alpha accrues within first month.
- **Debunked?** Solo insider signal has decayed (Ozlen 2025: "70-80% of alpha dissipates before Form 4 filing"). Cluster buys + context filtering are untested and may still hold.
- **Data:** EDGAR Form 4 (free, 2-day filing requirement). Cross-reference with 10-K/8-K.
- **LLM value:** High. The filtering is the edge — reading concurrent filings to distinguish routine diversification from conviction buying. LLM assesses whether the insider's buy aligns with the filing's forward-looking statements.
- **Implementation:** Monitor EDGAR for Form 4 cluster buys. LLM reads concurrent filings. $5K, 2-3 positions, 30-60 day holds.

### P5. Regulatory Pipeline → Affected Companies

- **Signal:** Proposed federal regulations → identify most-affected companies → trade before market prices in the impact.
- **Why it should work:** Proposed rules are 50-200+ pages of dense legal prose on regulations.gov. Affected companies are rarely named explicitly. The proposed → final rule timeline is 12-18 months on average, creating a long window.
- **Academic basis:** Investigation announcements: average -6% CAR. Regulation protects incumbents (higher returns, lower vol); deregulation imposes severe losses. JFE 2005: proposed regulatory changes significantly affect systematic risk.
- **Debunked?** No. Novel application.
- **Data:** regulations.gov API (free, requires free API key from api.data.gov, 1K req/hr). Federal Register API (free, no key).
- **LLM value:** Very high. This is pure unstructured text comprehension — mapping regulatory language to specific industries and companies. "EPA proposes PFAS manufacturing limits" → 3M, Chemours face costs; water treatment companies benefit.
- **Implementation:** Opportunistic — major proposed rules happen monthly, not daily. Screen Federal Register for "significant" rules. $5K, 2-3 positions.
- **Caveat:** Low frequency. Best as an opportunistic overlay, not a systematic daily strategy.

### P6. Supply Chain Disruption Detection

- **Signal:** Detect supply chain disruptions from news → identify downstream affected companies → trade before the information diffuses through the economic network.
- **Why it should work:** Multi-hop reasoning: "Taiwan earthquake" → "TSMC production halt" → "Apple/Nvidia supply risk." Markets are slow to trace multi-hop supply chain links.
- **Academic basis:** Cohen & Frazzini 2008: 150bps/month information diffusion lag. Hendricks & Singhal 2003/2005: -10% announcement-day return for disrupted firms, -40% cumulative over 3 years. Firms delay disclosure.
- **Debunked?** No. Core anomaly well-established. LLM-enhanced version untested.
- **Data:** News APIs (Finnhub free tier), EDGAR (customer/supplier disclosures in SFAS 131), BEA Input-Output Tables (free, industry-level).
- **LLM value:** Very high. Multi-hop reasoning is exactly what LLMs excel at and rule-based systems fundamentally cannot do.
- **Implementation:** Event-driven. When disruption detected, map through supply chain graph, identify affected companies. $5K, 1-3 positions. Hold until market fully prices in (typically 1-4 weeks).

### P7. 10b5-1 Plan Red Flags

- **Signal:** Detect suspicious insider trading plan patterns — short-duration plans, pre-earnings adoption, plan cycling, early terminations.
- **Why it should work:** Insiders use 10b5-1 plans to trade around information asymmetry while maintaining legal cover. Short cooling-off + single-trade plans = red flags that "systematically avoid losses and foreshadow considerable stock price declines."
- **Academic basis:** Jagolinzer 2009 (*Management Science*): plan sales before price declines, early terminations before positive shifts. Daniel Taylor (Wharton): 20,000+ plans examined, three red flags identified. Post-2022 amendment: opportunistic behavior continues.
- **Debunked?** No. SEC amended rules specifically because this works.
- **Data:** EDGAR Form 4 (XML with 10b5-1 checkbox post-April 2023), 10-Q/10-K Item 408a (XBRL), 8-K.
- **LLM value:** Medium-high. Form 4 XML is structured — use deterministic parsers. LLM adds value for pre-2023 historical footnote parsing, 8-K narratives, and pattern recognition across plan lifecycle.
- **Implementation:** Monitor Form 4 filings for 10b5-1 checkbox. Flag red-flag patterns. $5K, short-bias (avoid/short flagged stocks) or long-bias (buy after early plan terminations).

### P8. WARN Act Pre-News Signal

- **Signal:** Mass layoff WARN filings appear on state websites before news coverage. Short or avoid the stock.
- **Why it should work:** WARN Act requires 60-day advance notice. Many filings for small/mid-caps never get news coverage. California publishes within 1-5 business days. You get the signal ~45 days before the actual layoff and often before any news article.
- **Academic basis:** Worrell et al 1991: -1% to -3% CAR on layoff announcements. Chen et al 2001: -1.2% CAR. But Farber & Hallock 2009: negative reaction declining over time (markets view layoffs as efficiency-improving for some companies).
- **Debunked?** Signal is weakening — markets increasingly view layoffs as positive restructuring. Context matters.
- **Data:** State websites — California (edd.ca.gov), New York (dol.ny.gov), Texas (twc.texas.gov), Washington (esd.wa.gov). ~60-70% of US filings scrapable. No centralized database.
- **LLM value:** High for entity resolution (subsidiary legal names → parent company tickers). Also: classify as distress signal (reactive) vs. restructuring signal (proactive).
- **Implementation:** Scrape CA + NY WARN pages. Match to tickers. $5K, short-bias or avoidance signal. Combine with other bearish signals for confirmation.

### P9. DEF 14A Executive Compensation Changes

- **Signal:** Detect shifts in executive compensation structure from proxy statements. Golden parachute added = M&A signal. Cash→options shift = management bullish on stock.
- **Why it should work:** Compensation structure reveals management's private expectations about future firm value. Golden parachute provisions signal upcoming acquisition discussions.
- **Academic basis:** Core, Guay & Larcker: equity comp shifts signal private expectations. Srivastava: option exercise patterns predict price movements. CEO option grant timing studies (JFQA 2018): predictable return patterns around comp events.
- **Debunked?** No.
- **Data:** EDGAR DEF 14A filings (free). `edgartools` Python library parses into structured fields. ~6,000-8,000 filings/year.
- **LLM value:** High. Year-over-year comp comparison across heterogeneous filing formats. Golden parachute clause detection in variable legal prose. Connecting comp shifts to strategic context.
- **Implementation:** Annual signal (proxy season March-June). $5K, long-bias (buy when comp structure signals bullish management expectations) or event-driven (golden parachute → potential acquisition target).

### P10. Litigation Pre-Filing Detection

- **Signal:** Monitor for "law firm announces investigation into [Company]" press releases. These precede formal class-action filings by ~9 days and capture 73% of the abnormal return.
- **Why it should work:** Securities class actions produce -12% to -20% CAR. Pre-filing investigation announcements are the leading indicator. Most retail investors don't monitor legal press releases.
- **Academic basis:** Event studies: -12.3% CAR in 20-day window. Pre-filing study (2023, 1,473 lawsuits): 56.4% preceded by investigation announcements. Patent litigation: significant for small firms.
- **Debunked?** No.
- **Data:** CourtListener/RECAP API (free, 5K req/day). PACER ($0.10/page, ~$300/month if monitoring actively). Legal news wire for investigation announcements.
- **LLM value:** High. Read/classify complaints (defendant, allegations, damages, case type). Severity assessment (patent troll vs DOJ antitrust). Entity resolution.
- **Implementation:** Monitor legal news for investigation announcements → short or buy puts on target company → close before formal filing. $5K, requires options or short access. Alternatively: avoidance signal (don't buy stocks under investigation).

---

## EXCLUDED (debunked, decayed, or fails smell test)

| Strategy | Reason |
|----------|--------|
| Technical rule mining (RSI/EMA/MACD parameter search) | Empirically debunked — Bajgrowicz & Scaillet 2012 tested 7,846 rules, zero value after data-snooping correction |
| Congressional trading (follow politicians) | Largely decayed post-STOCK Act 2012. Eggers & Hainmueller 2013 rebuttal. NBER 2020: senators underperform. NANC ETF = tech beta (0.95 Nasdaq correlation) |
| Google Trends | Signal decayed 50-70% for US large-caps (Bijl et al 2016). pytrends is fragile |
| Social media sentiment (Reddit/X/StockTwits) | 1-3 day signal that reverses (Bradley et al 2021). Temporary price pressure, not information |
| App store / web traffic | Free data insufficient granularity. Useful data requires Sensor Tower / data.ai ($$$) |
| VRP via naked option selling | Existential tail risk at $5K — SVXY lost 95% in Feb 2018. SVOL lost 33% in April 2025 |
| Short-term mean reversion (small-caps) | $5K too small for required 20-position long/short weekly rebalance. Works in large-caps only (opposite of useful) |
| Turn-of-month effect | Fading post-2015. No evidence in Nasdaq. Mixed recent studies |
| Municipal bond analysis | $5K = one bond, zero diversification. 50-200+ bps dealer markup |
| Patent analysis | 12+ month holding period required. Doesn't fit <60 days |
| Import/export trade data | ~2 month publication lag kills timeliness |
| Lobbying spend spikes | Quarterly reporting lag limits timeliness |
| Job posting momentum (firm-level) | Free data only at aggregate level (JOLTS). Indeed/LinkedIn APIs no longer free. Ghost jobs add noise (20%+) |
| Domain registrations / website changes | No academic evidence. High noise (defensive registrations, routine changes) |
| LLM strategies long-term (general) | FINSABER 2025 (KDD 2026): LLM trading fails on 20-year diverse stock tests. Prior claims from cherry-picked windows |

---

## KEY CAVEATS

1. **Kim et al 2024 ("LLM beats analysts at 60% vs 53%") was WITHDRAWN** from arXiv in Feb 2025 due to data inconsistencies during replication. Do not cite.
2. **FINSABER (Li et al 2025, accepted KDD 2026)**: Under rigorous 20-year backtesting on 91+ diverse stocks, LLM trading strategies do NOT beat buy-and-hold. Prior claims resulted from selective testing.
3. **Alpha decay is real**: Lopez-Lira's GPT sentiment Sharpe dropped from 6.54 (2021) to ~1.22 (2024) as adoption increased.
4. **Multi-agent debate** has no rigorous evidence of improving returns vs. single well-prompted LLM. It improves reasoning quality and explainability.
5. **LLMs don't beat FinBERT** for pure sentiment classification. They add value for unstructured reasoning — reading between lines, multi-hop inference, bloat stripping.
6. This is a **research project, not a money printer.** Go in with realistic expectations and paper-trade before risking capital.
