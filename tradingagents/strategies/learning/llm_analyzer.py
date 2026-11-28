"""LLM-based signal analyzer for active paper-trade strategies.

Thesis analyzers (earnings_call, supply_chain) default to gemini-4-neon (high effort);
bounded analyzers (commodity_macro, weather_ag) default to gemini-4-hydrogen (high effort).
"""
from __future__ import annotations

import json
import logging
from copy import copy, deepcopy
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _parse_json_response(text: str) -> dict:
    """Extract JSON from an LLM response, handling markdown fences and truncation."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start >= 0:
        candidate = text[start:]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
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
tone_assessment (1 sentence), guidance_changes (list of strings, max 2 items), red_flags (list of strings, max 2 items), rationale (1 sentence).
Keep ALL string values under 100 characters.""",
    "supply_chain": """You are analyzing a supply chain disruption for trading signals.
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
primary_impact (1 sentence), secondary_impacts (max 3 items, each {ticker, relationship, estimated_impact}),
duration_estimate (string), rationale (1 sentence).
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
    "commodity_macro": """You are analyzing commodity positioning and macro data
for a non-agricultural commodity trade signal.
Assess whether CFTC COT positioning extremes, macro regime, and catalysts
support the proposed direction. Consider:
1. Is speculative positioning truly extreme, or could it extend further?
2. Does the macro regime (real rates, CPI, VIX) support or contradict?
3. Are there regulatory or supply chain catalysts that could accelerate the move?
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), score (0.0-1.0), reasoning (1-2 sentences).
Keep ALL string values under 100 characters.""",
}


class LLMAnalyzer:
    """Generate trading signals for active strategies using configured Responses/LLM models."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        ar = self.config.get("autoresearch", {})
        self._model_name = ar.get("bounded_model") or ar.get("autoresearch_model", "gemini-4-hydrogen")
        self._effort = ar.get("bounded_effort") or ar.get("llm_effort", "high")
        if ar:
            self._thesis_model = ar.get("thesis_model", ar.get("autoresearch_model", "gemini-4-neon"))
            self._thesis_effort = ar.get("thesis_effort", ar.get("llm_effort", "high"))
        else:
            self._thesis_model = "gemini-4-neon"
            self._thesis_effort = "high"
        self.last_call_provenance: dict[str, Any] = {}
        self.last_call_failure = ""
        self.last_call_transport_failure: dict[str, Any] | None = None
        self.last_research_request: dict[str, Any] | None = None
        self.last_call_response: dict[str, Any] = {"received": False, "text": None}
        self._provider_clients: dict[str, Any] = {}
        self._temperature = ar.get("llm_temperature", 0.0)
        self._client = None

    def fork_for_parallel(self) -> "LLMAnalyzer":
        """Isolate mutable analysis state while retaining exact native settings."""
        def fork_client(client):
            if client is None:
                return None
            from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
            from tradingagents.strategies.llm_utils import GeminiClient
            if isinstance(client, (ChatGPTPlanClient, GeminiClient)):
                return client
            native_bases = {base.__module__.split(".")[0] for base in type(client).__mro__}
            if native_bases & {"openai", "anthropic"}:
                return client
            fork = getattr(client, "fork_for_parallel", None)
            if callable(fork):
                result = fork()
                if result is not None and result is not client:
                    return result
            raise ValueError("parallel_client_unsupported")

        result = copy(self)
        result.config = deepcopy(self.config)
        result.last_call_provenance = {}
        result.last_call_failure = ""
        result.last_call_transport_failure = None
        result.last_research_request = None
        result.last_call_response = {"received": False, "text": None}
        result._client = fork_client(self._client)
        result._provider_clients = {name: fork_client(client) for name, client in self._provider_clients.items()}
        return result

    def get_prompt(self, strategy_name: str) -> str:
        """Return the system prompt for an active strategy."""
        alias = {"weather_ag": "ag_weather", "ag_weather": "weather_ag"}.get(strategy_name)
        return _DEFAULT_PROMPTS.get(strategy_name, _DEFAULT_PROMPTS.get(alias or "", ""))

    def _get_client(self):
        """Lazy-init the selected provider; credentials stay in its environment."""
        if self._client is None:
            from tradingagents.strategies.chatgpt_plan_client import configured_plan_client
            plan_client = configured_plan_client(self.config, self._model_name)
            if plan_client is not None:
                self._client = plan_client
                return self._client
            try:
                from tradingagents.strategies.llm_utils import GeminiClient, model_provider
                provider = model_provider(self._model_name)
                if provider == "gemini":
                    self._client = GeminiClient.from_env()
                    return self._client
                if provider == "openai":
                    from openai import OpenAI
                    self._client = OpenAI(
                        timeout=httpx.Timeout(120.0, connect=10.0), max_retries=0,
                    )
                    return self._client
                import anthropic
                self._client = anthropic.Anthropic(
                    timeout=httpx.Timeout(60.0, connect=10.0), max_retries=0,
                )
            except ImportError:
                logger.error("Selected LLM provider package not installed")
                return None
            except Exception:
                logger.error("Failed to create LLM client", exc_info=True)
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

    def _call_llm(self, system: str, user: str, max_tokens: int = 4096, *, role: str = "thesis") -> str:
        """Make a single LLM call. Returns response text or empty string."""
        from tradingagents.strategies.candidate_response_reuse import (
            retain_candidate_response,
            reused_candidate_response,
        )
        from tradingagents.strategies.chatgpt_plan_client import (
            configured_plan_client,
            require_configured_billing,
        )
        from tradingagents.strategies.llm_utils import GeminiClient, call_analysis_model, model_provider
        from tradingagents.strategies.orchestration.research_model_attempt import request_receipt
        from tradingagents.strategies.orchestration.research_resilience import (
            configured as resilience_configured,
        )
        from tradingagents.strategies.runtime_deadline import (
            ModelDeadlineExceeded,
            ModelTransportError,
            model_timeout,
        )

        model = self._model_name if role == "bounded" else self._thesis_model
        effort = self._effort if role == "bounded" else self._thesis_effort
        self.last_research_request = None
        if resilience_configured(self.config.get("autoresearch", {})):
            self.last_research_request = request_receipt(
                model=model,
                role=role,
                effort=effort,
                max_tokens=max_tokens,
                temperature=self._temperature,
                system=system,
                prompt=user,
            )
        self.last_call_response = {"received": False, "text": None}

        def observed_response(text):
            if type(text) is str:
                self.last_call_response = {"received": True, "text": text}

        self.last_call_failure = ""
        self.last_call_transport_failure = None
        self.last_call_provenance = {
            "configured_model": model,
            "returned_model": None,
            "returned_revision": None,
            "identity_status": "unpinned",
            "response_id": None,
            "reasoning_effort": effort,
            "role": role,
        }
        try:
            model_timeout()
            plan_client = configured_plan_client(self.config, model)
            provider = model_provider(model)
            mixed_provider = provider != model_provider(self._model_name)
            if plan_client is not None and mixed_provider:
                client = self._provider_clients.setdefault("chatgpt_plan", plan_client)
            elif mixed_provider:
                if provider not in self._provider_clients:
                    if provider == "gemini":
                        self._provider_clients[provider] = GeminiClient.from_env()
                    elif provider == "openai":
                        from openai import OpenAI
                        self._provider_clients[provider] = OpenAI(timeout=120, max_retries=0)
                    else:
                        from anthropic import Anthropic
                        self._provider_clients[provider] = Anthropic(timeout=60, max_retries=0)
                client = self._provider_clients[provider]
            else:
                client = self._get_client()
            if client is None:
                self.last_call_failure = "model_unavailable"
                return ""
            require_configured_billing(client, self.config, model)
            reused = reused_candidate_response(
                client,
                model=model,
                system=system,
                prompt=user,
                max_tokens=max_tokens,
                temperature=self._temperature,
                effort=effort,
                role=role,
            )
            if reused is not None:
                text, self.last_call_provenance = reused
                observed_response(text)
                model_timeout()
                return text
            text = call_analysis_model(
                client,
                model=model,
                max_tokens=max_tokens,
                system=system,
                prompt=user,
                temperature=self._temperature,
                effort=effort,
                provenance=self.last_call_provenance,
                response_observer=observed_response,
            )
            self.last_call_provenance["role"] = role
            retain_candidate_response(text, self.last_call_provenance)
            return text
        except ModelDeadlineExceeded:
            self.last_call_failure = "model_deadline_exhausted"
            return ""
        except ModelTransportError as exc:
            from tradingagents.strategies.transport_worker import validate_error_metadata
            status = exc.status_code
            self.last_call_transport_failure = {
                "status_code": status if type(status) is int and 100 <= status <= 599 else None,
                "error_metadata": validate_error_metadata(exc.error_metadata),
            }
            self.last_call_failure = "analysis_unavailable"
            logger.error("LLM transport failed")
            return ""
        except Exception:
            self.last_call_failure = "analysis_unavailable"
            logger.error("LLM call failed")
            return ""

    # ------------------------------------------------------------------
    # Earnings call analysis (thesis: gemini-4-neon high)
    # ------------------------------------------------------------------

    def analyze_earnings_call(
        self,
        transcript_text: str,
        ticker: str,
        regime_context: dict | None = None,
        text_source: str = "earnings_news",
    ) -> dict[str, Any]:
        """Detect tone shifts, deception, and guidance revisions."""
        if text_source == "transcript":
            system = """You are analyzing a real earnings call transcript for trading signals.
Focus on: tone shifts between prepared remarks and Q&A, hedging language,
deceptive or evasive responses, Q&A dynamics (dodged questions, vague answers),
and guidance revisions compared to prior quarters.
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
tone_assessment (1 sentence), guidance_changes (list of strings, max 2 items), red_flags (list of strings, max 2 items), rationale (1 sentence).
Keep ALL string values under 100 characters."""
            source_label = "EARNINGS CALL TRANSCRIPT"
        else:
            system = """You are analyzing earnings news coverage and market reaction.
Assess sentiment and identify surprises from news articles about the earnings event.
You do NOT have the actual transcript — be honest about working from news coverage.
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
tone_assessment (1 sentence), guidance_changes (list of strings, max 2 items), red_flags (list of strings, max 2 items), rationale (1 sentence).
Keep ALL string values under 100 characters."""
            source_label = "EARNINGS NEWS COVERAGE"

        excerpt = transcript_text[:4000]
        user = (
            f"Ticker: {ticker}\n\n"
            f"{source_label} (excerpt):\n"
            f"{excerpt}\n\n"
            f"Analyze for trading signals and return JSON."
            + self._regime_suffix(regime_context)
        )
        system += "\nInclude evidence_claim: one factual source-grounded catalyst assertion with company attribution; exclude return predictions and unsupported inference."
        result = self._call_llm(system, user, role="thesis")
        return _parse_json_response(result) if result else {}

    # ------------------------------------------------------------------
    # Supply chain analysis (thesis: gemini-4-neon high)
    # ------------------------------------------------------------------

    def analyze_supply_chain(
        self,
        headline: str,
        summary: str,
        source_ticker: str,
        peer_tickers: list[str],
        regime_context: dict | None = None,
    ) -> dict[str, Any]:
        """Multi-hop supply chain impact assessment."""
        system = _DEFAULT_PROMPTS["supply_chain"]
        user = (
            f"Source company: {source_ticker}\n"
            f"Known peer companies (not verified supplier/customer edges): {', '.join(peer_tickers[:15])}\n\n"
            f"NEWS:\n"
            f"Headline: {headline}\n"
            f"Summary: {summary[:2000]}\n\n"
            f"Analyze supply chain impact and return JSON."
            + self._regime_suffix(regime_context)
        )
        system += "\nInclude evidence_claim: one factual source-grounded catalyst assertion with company attribution; exclude return predictions and unsupported inference."
        result = self._call_llm(system, user, role="thesis")
        return _parse_json_response(result) if result else {}

    # ------------------------------------------------------------------
    # Commodity macro & ag weather analysis (bounded: gemini-4-hydrogen high)
    # ------------------------------------------------------------------

    def analyze_commodity_macro(
        self,
        ticker: str,
        commodity_name: str,
        cot_context: dict,
        macro_context: dict,
        regime_context: dict | None = None,
    ) -> dict[str, Any]:
        """Optional context only; complete deterministic COT rules remain authority."""
        system = """Analyze retained COT positioning and macro observations.
Do not invent releases or infer commodity evidence from unrelated headlines.
Return JSON: direction (long/short/neutral), score (0-1), reasoning (source-grounded context), evidence_claim (one factual catalyst claim; no future-return prediction)."""
        user = json.dumps(
            {
                "ticker": ticker,
                "commodity": commodity_name,
                "cot": cot_context,
                "macro": macro_context,
            },
            default=str,
        )
        system += "\nInclude evidence_claim: one factual source-grounded catalyst assertion with company attribution; exclude return predictions and unsupported inference."
        result = self._call_llm(system, user + self._regime_suffix(regime_context), role="bounded")
        return _parse_json_response(result) if result else {}

    def analyze_ag_weather(
        self,
        ticker: str,
        commodity_name: str,
        ag_context: dict,
        trailing_return: float = 0.0,
        hold_days: int = 21,
        regime_context: dict | None = None,
    ) -> dict[str, Any]:
        """Analyze agricultural supply disruption risk for a ticker."""
        system = self.get_prompt("ag_weather")

        noaa = ag_context.get("noaa_data", {})
        drought_score = ag_context.get("drought_score", 0.0)
        drought_states = ag_context.get("drought_states", {})
        usda = ag_context.get("usda_data", {})

        from datetime import timedelta
        from tradingagents.strategies.data_sources.usda_source import (
            validated_condition_observations,
        )

        crop_lines = []
        crop_progress = usda.get("crop_progress", {}) if isinstance(usda, dict) else {}
        for commodity, weeks in sorted(crop_progress.items()):
            latest, observations = validated_condition_observations(weeks)
            if latest is None:
                continue
            previous = latest - timedelta(days=7)
            for (week, state, crop_class), ge in sorted(observations.items()):
                if week != latest:
                    continue
                label = f"- {commodity}, {state}, {crop_class}, week ending {week}"
                if ge is None:
                    crop_lines.append(f"{label}: condition data unavailable (invalid or incomplete)")
                    continue
                prior_ge = observations.get((previous, state, crop_class))
                change = (
                    f"change: {ge - prior_ge:+g}pp from {previous}"
                    if prior_ge is not None
                    else "weekly comparison unavailable"
                )
                crop_lines.append(f"{label}: {ge:g}% Good/Excellent ({change})")

        severe_states = [
            s
            for s, d in drought_states.items()
            if isinstance(d, dict) and d.get("D2", 0) + d.get("D3", 0) + d.get("D4", 0) > 20
        ]

        user = f"""Analyzing {ticker} ({commodity_name}).

WEATHER (NOAA, last 30 days):
- Heat stress days (>95F): {noaa.get('heat_stress_days', 'N/A')}
- Precipitation deficit: {noaa.get('precip_deficit_pct', 'N/A')}%
- Frost events: {noaa.get('frost_events', 'N/A')}
- Temp anomaly: {noaa.get('avg_temp_anomaly_f', 'N/A')}F

DROUGHT (US Drought Monitor):
- Composite score: {drought_score}/4.0
- States in severe+ drought: {', '.join(severe_states) if severe_states else 'none in retained observations' if drought_states else 'unavailable'}

CROP CONDITIONS (USDA):
{chr(10).join(crop_lines) if crop_lines else '- No data available'}

PRICE ACTION:
- Trailing return: {trailing_return:.1%}

Assess probability that ag supply disruption drives {ticker} higher over {hold_days} days.
Return JSON.""" + self._regime_suffix(regime_context)

        system += "\nInclude evidence_claim: one factual source-grounded catalyst assertion with company attribution; exclude return predictions and unsupported inference."
        result = self._call_llm(system, user, role="bounded")
        return _parse_json_response(result) if result else {}
