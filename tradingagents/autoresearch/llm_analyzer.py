"""LLM-based signal analyzer for paper-trade strategies.

Uses Haiku for all analysis calls (~$0.001/call). Each method takes
raw event data and returns a structured signal dict with direction,
conviction, and rationale.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _parse_json_response(text: str) -> dict:
    """Extract JSON from an LLM response, handling markdown fences and truncation."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object within text
    start = text.find("{")
    if start >= 0:
        candidate = text[start:]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        # Handle truncated JSON by closing open braces/brackets
        for suffix in ["}", "]}", "\"}", "\"]}", "\"]}}"]:
            try:
                return json.loads(candidate + suffix)
            except json.JSONDecodeError:
                continue

    logger.warning("Failed to parse LLM JSON response: %s", text[:200])
    return {}


_DEFAULT_PROMPTS: dict[str, str] = {
    "earnings_call": """You are analyzing earnings news coverage and market reaction.
Assess sentiment and identify surprises from news articles about the earnings event.
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
tone_assessment (1 sentence), guidance_changes (max 2 items), red_flags (max 2 items), rationale (1 sentence).
Keep ALL string values under 100 characters.""",
    "insider_activity": """You are analyzing SEC Form 4 insider transaction filings.
Look for: cluster buys (multiple insiders buying within days), C-suite purchases,
large purchases relative to salary, purchases during quiet periods.
Insider SELLS are less informative (diversification, tax planning).
Return JSON with keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
cluster_size (int), notable_insiders (list), rationale (1-2 sentences).""",
    "filing_analysis": """You are a financial analyst comparing two SEC filings for material changes.
Focus on: risk factor changes, revenue guidance shifts, new litigation,
accounting policy changes, going concern language, and segment changes.
Return JSON with keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
changes (list of material changes found), rationale (1-2 sentences).""",
    "regulatory_pipeline": """You are analyzing a proposed regulation for stock market impact.
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
affected_tickers (max 5 ticker symbols), affected_sectors (max 3),
impact_assessment (1 sentence), rationale (1 sentence).
Keep ALL string values under 80 characters.""",
    "supply_chain": """You are analyzing a supply chain disruption for trading signals.
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
primary_impact (1 sentence), secondary_impacts (max 3 items, each {ticker, relationship, estimated_impact}),
duration_estimate (string), rationale (1 sentence).
Keep ALL string values under 80 characters.""",
    "litigation": """You are analyzing a federal court docket for trading signals.
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
defendant_ticker (ticker symbol or ""), case_type (string),
severity ("low"/"medium"/"high"/"critical"), rationale (1 sentence).
Keep ALL string values under 80 characters.""",
    "ag_weather": """You are analyzing agricultural supply disruption risk.
Assess whether weather, drought, and crop condition data support a long position
in agricultural instruments. Consider:
1. Are conditions actually damaging crops, or just concerning?
2. Has the market already priced in the disruption (check momentum)?
3. Is this ticker directly exposed to the affected commodities?
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"neutral"), score (0.0-1.0), reasoning (1-2 sentences).
Keep ALL string values under 100 characters.""",
}


class LLMAnalyzer:
    """Generate trading signals from unstructured events using an LLM.

    All calls use the configured autoresearch model (default: Haiku).
    Prompts can be overridden at runtime for optimization trials.
    """

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self._model_name = self.config.get(
            "autoresearch", {}
        ).get("autoresearch_model", "claude-haiku-4-5-20251001")
        self._client = None
        self._prompt_overrides: dict[str, str] = {}

    def get_prompt(self, strategy_name: str) -> str:
        """Return the active system prompt for a strategy."""
        if strategy_name in self._prompt_overrides and self._prompt_overrides[strategy_name]:
            return self._prompt_overrides[strategy_name]
        return _DEFAULT_PROMPTS.get(strategy_name, "")

    def set_prompt_override(self, strategy_name: str, prompt: str) -> None:
        """Override the system prompt for a strategy (empty string = revert to default)."""
        if prompt:
            self._prompt_overrides[strategy_name] = prompt
        else:
            self._prompt_overrides.pop(strategy_name, None)

    def _get_client(self):
        """Lazy-init the Anthropic client."""
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(
                    timeout=httpx.Timeout(60.0, connect=10.0),
                )
            except ImportError:
                logger.error("anthropic package not installed")
                return None
            except Exception:
                logger.error("Failed to create Anthropic client", exc_info=True)
                return None
        return self._client

    @staticmethod
    def _regime_suffix(regime_context: dict | None) -> str:
        """Build a regime context appendix for LLM prompts."""
        if not regime_context:
            return ""
        overall = regime_context.get("overall_regime", "unknown")
        vix = regime_context.get("vix_level", "?")
        vix_r = regime_context.get("vix_regime", "unknown")
        credit = regime_context.get("credit_spread_bps", "?")
        credit_r = regime_context.get("credit_regime", "unknown")
        return (
            f"\n\nCurrent market regime: {overall}. "
            f"VIX: {vix} ({vix_r}), credit spreads: {credit}bps ({credit_r}). "
            f"Factor regime into your conviction level."
        )

    def _call_llm(self, system: str, user: str, max_tokens: int = 2048) -> str:
        """Make a single LLM call. Returns response text or empty string."""
        client = self._get_client()
        if client is None:
            return ""
        try:
            response = client.messages.create(
                model=self._model_name,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return response.content[0].text
        except Exception:
            logger.error("LLM call failed", exc_info=True)
            return ""

    # ------------------------------------------------------------------
    # Filing analysis (P3: 10-K/10-Q material changes)
    # ------------------------------------------------------------------

    def analyze_filing_change(
        self,
        current_text: str,
        prior_text: str,
        ticker: str,
        regime_context: dict | None = None,
    ) -> dict[str, Any]:
        """P3: Score material changes between consecutive filings.

        Returns dict with: direction, conviction (0-1), changes, rationale.
        """
        system = """You are a financial analyst comparing two SEC filings for material changes.
Focus on: risk factor changes, revenue guidance shifts, new litigation,
accounting policy changes, going concern language, and segment changes.
Return JSON with keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
changes (list of material changes found), rationale (1-2 sentences)."""

        # Truncate to fit context
        current_excerpt = current_text[:3000]
        prior_excerpt = prior_text[:3000]

        user = f"""Ticker: {ticker}

CURRENT FILING (excerpt):
{current_excerpt}

PRIOR FILING (excerpt):
{prior_excerpt}

Analyze material changes and return JSON.""" + self._regime_suffix(regime_context)

        result = self._call_llm(system, user)
        return _parse_json_response(result) if result else {}

    # ------------------------------------------------------------------
    # Insider transaction analysis (P4: cluster buys)
    # ------------------------------------------------------------------

    def analyze_insider_context(
        self,
        form4_filings: list[dict],
        ticker: str,
        regime_context: dict | None = None,
    ) -> dict[str, Any]:
        """P4: Assess insider buy conviction from Form 4 filing context.

        Returns dict with: direction, conviction (0-1), cluster_size,
        notable_insiders, rationale.
        """
        system = """You are analyzing SEC Form 4 insider transaction filings.

Key fields in each filing:
- transaction_type: "buy" or "sell" (derived from XML)
- transaction_code: "P" = open-market purchase (strongest signal), "S" = open-market sale,
  "A" = grant/award (routine compensation, weak signal), "M" = option exercise,
  "G" = gift, "F" = tax withholding (not a real sale)
- shares: number of shares transacted
- price_per_share: price paid/received per share (0.0 for awards)
- owner_name: name of the insider
- owner_title: role (CEO, CFO, Director, etc.)
- is_officer: true if corporate officer
- is_director: true if board member

Prioritize open-market purchases (code "P") by officers — these are the strongest
insider conviction signals. Grants/awards (code "A") are routine compensation and
should NOT be treated as bullish signals. Tax withholding sales (code "F") are
mechanical and should be ignored.

Return JSON with keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
cluster_size (int), notable_insiders (list of names+titles), rationale (1-2 sentences)."""

        filings_text = json.dumps(form4_filings[:10], indent=2, default=str)
        user = f"""Ticker: {ticker}

Recent Form 4 filings:
{filings_text}

Analyze insider trading patterns and return JSON.""" + self._regime_suffix(regime_context)

        result = self._call_llm(system, user)
        return _parse_json_response(result) if result else {}

    # ------------------------------------------------------------------
    # 10b5-1 plan analysis (P7: red flag detection)
    # ------------------------------------------------------------------

    def analyze_10b5_1_plan(
        self,
        form4_filings: list[dict],
        ticker: str,
        regime_context: dict | None = None,
    ) -> dict[str, Any]:
        """P7: Detect 10b5-1 plan red flags from Form 4 data.

        Red flags: plan adoption shortly before bad news, frequent plan
        modifications, suspiciously timed plan terminations.

        Returns dict with: direction, conviction (0-1), red_flags (list),
        rationale.
        """
        system = """You are analyzing SEC Form 4 filings for 10b5-1 trading plan red flags.
Red flags include: plans adopted shortly before material announcements,
frequent plan modifications or terminations, sales clustering at price peaks,
plans with very short cooling-off periods.
Return JSON with keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
red_flags (list of specific concerns), rationale (1-2 sentences)."""

        filings_text = json.dumps(form4_filings[:10], indent=2, default=str)
        user = f"""Ticker: {ticker}

Recent Form 4 filings (check for 10b5-1 plan indicators):
{filings_text}

Analyze for red flags and return JSON.""" + self._regime_suffix(regime_context)

        result = self._call_llm(system, user)
        return _parse_json_response(result) if result else {}

    # ------------------------------------------------------------------
    # Executive compensation analysis (P9: DEF 14A)
    # ------------------------------------------------------------------

    def analyze_exec_comp(
        self,
        proxy_text: str,
        ticker: str,
        regime_context: dict | None = None,
    ) -> dict[str, Any]:
        """P9: Detect meaningful compensation structure shifts from DEF 14A.

        Looks for: shift to stock-based comp (bullish alignment), golden
        parachutes being added (bearish — anticipating takeover/departure),
        large option grants at low strikes, performance metric changes.

        Returns dict with: direction, conviction (0-1), comp_changes (list),
        rationale.
        """
        system = """You are analyzing a DEF 14A proxy statement for executive compensation signals.
Bullish signals: increased stock-based comp, tighter performance hurdles, insider buying.
Bearish signals: golden parachutes, option repricing, lowered performance targets,
excessive perks, management entrenchment provisions.
Return JSON with keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
comp_changes (list of notable changes), rationale (1-2 sentences)."""

        proxy_excerpt = proxy_text[:4000]
        user = f"""Ticker: {ticker}

DEF 14A Proxy Statement (excerpt):
{proxy_excerpt}

Analyze executive compensation signals and return JSON.""" + self._regime_suffix(regime_context)

        result = self._call_llm(system, user)
        return _parse_json_response(result) if result else {}

    # ------------------------------------------------------------------
    # Earnings call analysis (P1+P2: tone + guidance revision)
    # ------------------------------------------------------------------

    def analyze_earnings_call(
        self,
        transcript_text: str,
        ticker: str,
        regime_context: dict | None = None,
        text_source: str = "earnings_news",
    ) -> dict[str, Any]:
        """P1+P2: Detect tone shifts, deception, and guidance revisions.

        Returns dict with: direction, conviction (0-1), tone_assessment,
        guidance_changes, red_flags, rationale.

        Args:
            text_source: "transcript" for real earnings call transcript,
                         "earnings_news" for news-based proxy.
        """
        if text_source == "transcript":
            system = """You are analyzing a real earnings call transcript for trading signals.
Focus on: tone shifts between prepared remarks and Q&A, hedging language,
deceptive or evasive responses, Q&A dynamics (dodged questions, vague answers),
and guidance revisions compared to prior quarters.
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
tone_assessment (1 sentence), guidance_changes (max 2 items), red_flags (max 2 items), rationale (1 sentence).
Keep ALL string values under 100 characters."""
            source_label = "EARNINGS CALL TRANSCRIPT"
        else:
            system = """You are analyzing earnings news coverage and market reaction.
Assess sentiment and identify surprises from news articles about the earnings event.
You do NOT have the actual transcript — be honest about working from news coverage.
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
tone_assessment (1 sentence), guidance_changes (max 2 items), red_flags (max 2 items), rationale (1 sentence).
Keep ALL string values under 100 characters."""
            source_label = "EARNINGS NEWS COVERAGE"

        excerpt = transcript_text[:4000]
        user = f"""Ticker: {ticker}

{source_label} (excerpt):
{excerpt}

Analyze for trading signals and return JSON.""" + self._regime_suffix(regime_context)

        result = self._call_llm(system, user)
        return _parse_json_response(result) if result else {}

    # ------------------------------------------------------------------
    # Regulatory analysis (P5: proposed rule impact)
    # ------------------------------------------------------------------

    def analyze_regulation(
        self,
        rule_title: str,
        rule_summary: str,
        agency: str,
        regime_context: dict | None = None,
    ) -> dict[str, Any]:
        """P5: Map proposed regulation to affected tickers.

        Returns dict with: direction, conviction (0-1), affected_tickers (list),
        affected_sectors (list), impact_assessment, rationale.
        """
        system = """You are analyzing a proposed regulation for stock market impact.
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
affected_tickers (max 5 ticker symbols), affected_sectors (max 3),
impact_assessment (1 sentence), rationale (1 sentence).
Keep ALL string values under 80 characters."""

        user = f"""Agency: {agency}
Rule Title: {rule_title}

Rule Summary:
{rule_summary[:3000]}

Identify affected companies and return JSON.""" + self._regime_suffix(regime_context)

        result = self._call_llm(system, user)
        return _parse_json_response(result) if result else {}

    # ------------------------------------------------------------------
    # Supply chain analysis (P6: disruption multi-hop)
    # ------------------------------------------------------------------

    def analyze_supply_chain(
        self,
        headline: str,
        summary: str,
        source_ticker: str,
        peer_tickers: list[str],
        regime_context: dict | None = None,
    ) -> dict[str, Any]:
        """P6: Multi-hop supply chain impact assessment.

        Returns dict with: direction, conviction (0-1), primary_impact (ticker),
        secondary_impacts (list of {ticker, relationship, impact}), rationale.
        """
        system = """You are analyzing a supply chain disruption for trading signals.
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
primary_impact (1 sentence), secondary_impacts (max 3 items, each {ticker, relationship, estimated_impact}),
duration_estimate (string), rationale (1 sentence).
Keep ALL string values under 80 characters."""

        user = f"""Source company: {source_ticker}
Known peers/supply chain: {', '.join(peer_tickers[:15])}

NEWS:
Headline: {headline}
Summary: {summary[:2000]}

Analyze supply chain impact and return JSON.""" + self._regime_suffix(regime_context)

        result = self._call_llm(system, user)
        return _parse_json_response(result) if result else {}

    # ------------------------------------------------------------------
    # Litigation analysis (P10: court docket assessment)
    # ------------------------------------------------------------------

    def analyze_litigation(
        self,
        case_name: str,
        nature_of_suit: str,
        cause: str,
        court: str,
        regime_context: dict | None = None,
    ) -> dict[str, Any]:
        """P10: Assess litigation risk from federal court docket.

        Returns dict with: direction, conviction (0-1), defendant_ticker,
        case_type, severity, rationale.
        """
        system = """You are analyzing a federal court docket for trading signals.
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
defendant_ticker (ticker symbol or ""), case_type (string),
severity ("low"/"medium"/"high"/"critical"), rationale (1 sentence).
Keep ALL string values under 80 characters."""

        user = f"""Court: {court}
Case Name: {case_name}
Nature of Suit: {nature_of_suit}
Cause: {cause}

Identify the defendant, assess severity, and return JSON.""" + self._regime_suffix(regime_context)

        result = self._call_llm(system, user)
        return _parse_json_response(result) if result else {}

    # ------------------------------------------------------------------
    # Agricultural weather analysis (ag_weather)
    # ------------------------------------------------------------------

    def analyze_ag_weather(
        self,
        ticker: str,
        commodity_name: str,
        ag_context: dict,
        trailing_return: float = 0.0,
        hold_days: int = 21,
        regime_context: dict | None = None,
    ) -> dict[str, Any]:
        """Analyze agricultural supply disruption risk for a ticker.

        Returns dict with: direction ("long"/"neutral"), score (0-1), reasoning.
        """
        system = self.get_prompt("ag_weather")

        noaa = ag_context.get("noaa_data", {})
        drought_score = ag_context.get("drought_score", 0.0)
        drought_states = ag_context.get("drought_states", {})
        usda = ag_context.get("usda_data", {})

        # Format USDA crop progress for prompt
        crop_lines = []
        crop_progress = usda.get("crop_progress", {}) if isinstance(usda, dict) else {}
        for commodity, weeks in crop_progress.items():
            if not isinstance(weeks, list) or not weeks:
                continue
            latest = weeks[-1]
            ge = latest.get("good_pct", 0) + latest.get("excellent_pct", 0)
            change = ""
            if len(weeks) >= 2:
                prior = weeks[-2]
                prior_ge = prior.get("good_pct", 0) + prior.get("excellent_pct", 0)
                change = f" (change: {ge - prior_ge:+d}pp)"
            crop_lines.append(f"- {commodity}: {ge}% Good/Excellent{change}")

        # Count states in severe+ drought
        severe_states = [s for s, d in drought_states.items()
                        if isinstance(d, dict) and d.get("D2", 0) + d.get("D3", 0) + d.get("D4", 0) > 20]

        user = f"""Analyzing {ticker} ({commodity_name}).

WEATHER (NOAA, last 30 days):
- Heat stress days (>95F): {noaa.get('heat_stress_days', 'N/A')}
- Precipitation deficit: {noaa.get('precip_deficit_pct', 'N/A')}%
- Frost events: {noaa.get('frost_events', 'N/A')}
- Temp anomaly: {noaa.get('avg_temp_anomaly_f', 'N/A')}F

DROUGHT (US Drought Monitor):
- Composite score: {drought_score}/4.0
- States in severe+ drought: {', '.join(severe_states) if severe_states else 'none'}

CROP CONDITIONS (USDA):
{chr(10).join(crop_lines) if crop_lines else '- No data available'}

PRICE ACTION:
- Trailing return: {trailing_return:.1%}

Assess probability that ag supply disruption drives {ticker} higher over {hold_days} days.
Return JSON.""" + self._regime_suffix(regime_context)

        result = self._call_llm(system, user)
        return _parse_json_response(result) if result else {}

    # ------------------------------------------------------------------
    # Parameter proposal (for evolution)
    # ------------------------------------------------------------------

    def propose_params(
        self,
        prompt: str,
    ) -> list[dict]:
        """Use LLM to propose new parameter combinations for a strategy.

        Args:
            prompt: The strategy's build_propose_prompt() output.

        Returns:
            List of param dicts (typically 3).
        """
        system = """You are a quantitative strategy optimizer. Given a strategy description,
current parameters, and recent backtest results, suggest 3 new parameter
combinations that explore the parameter space intelligently.
Return ONLY a JSON array of 3 parameter dictionaries. No explanation."""

        result = self._call_llm(system, prompt, max_tokens=512)
        if not result:
            return []

        parsed = _parse_json_response(result)
        if isinstance(parsed, list):
            return parsed
        # Sometimes the LLM wraps in an object
        if isinstance(parsed, dict) and "params" in parsed:
            return parsed["params"]
        return []

    # ------------------------------------------------------------------
    # Reflection
    # ------------------------------------------------------------------

    def reflect_on_generation(
        self,
        generation: int,
        scores: dict[str, float],
        weights: dict[str, float],
        top_results: dict[str, Any],
    ) -> dict:
        """Generate a reflection on generation results.

        Returns dict with: summary, insights, recommendations.
        """
        system = """You are a portfolio strategist reflecting on an autoresearch generation.
Analyze the results and provide actionable insights.
Return JSON with keys: summary (1-2 sentences), insights (list of observations),
recommendations (list of specific next steps)."""

        user = f"""Generation {generation} results:

Strategy scores (Sharpe ratios):
{json.dumps(scores, indent=2)}

Current Darwinian weights:
{json.dumps(weights, indent=2)}

Top strategy details:
{json.dumps(top_results, indent=2, default=str)}

Reflect on performance and suggest improvements."""

        result = self._call_llm(system, user)
        return _parse_json_response(result) if result else {}
