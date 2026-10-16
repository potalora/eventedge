"""LLM-based signal analyzer for paper-trade strategies.

Each method takes raw event data and returns a structured signal dict with
direction, conviction, and rationale. New generations default to GPT-6 Luna
with high reasoning effort.
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
tone_assessment (1 sentence), guidance_changes (list of strings, max 2 items), red_flags (list of strings, max 2 items), rationale (1 sentence).
Keep ALL string values under 100 characters.""",
    "insider_activity": """You are analyzing SEC Form 4 insider transaction filings.
Look for: cluster buys (multiple insiders buying within days), C-suite purchases,
large purchases relative to salary, purchases during quiet periods.
Insider SELLS are less informative (diversification, tax planning).
Return JSON with keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
cluster_size (int), notable_insiders (list of strings, each combining name and title; not objects), rationale (1-2 sentences).""",
    "filing_analysis": """You are a financial analyst comparing two SEC filings for material changes.
Focus on: risk factor changes, revenue guidance shifts, new litigation,
accounting policy changes, going concern language, and segment changes.
Return JSON with keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
changes (list of strings describing material changes found), rationale (1-2 sentences).""",
    "regulatory_pipeline": """You are analyzing a proposed regulation for stock market impact.
Return ONLY compact JSON. No explanation outside JSON.
Keys: direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
affected_tickers (max 5 ticker symbols), affected_sectors (list of strings, max 3),
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
    "quantum_readiness": """You are analyzing signals related to post-quantum cryptography migration.

Context: PQC migration is a research theme. Infer no CRQC arrival date or probability without retained source evidence.
Three regimes exist: (1) CRQC accelerating -- timeline compression, (2) CRQC stalling -- physics
bottleneck, (3) migration manageable -- priced in. Assess which regime a signal supports AND
whether the specific company is a winner or loser in that regime.

The key asymmetry: centralized systems push updates quickly; decentralized systems (crypto) need
consensus upgrades, hard forks, and individual wallet migrations -- potentially years.

Return ONLY compact JSON. No explanation outside JSON.
Keys: regime_signal ("bull"/"bear"/"neutral"), regime_confidence (0.0-1.0),
direction ("long"/"short"/"neutral"), conviction (0.0-1.0),
pqc_readiness ("proactive"/"aware"/"silent"/"n/a"),
crypto_dependency ("high"/"medium"/"low"/"unknown"),
rationale (1 sentence).
Keep ALL string values under 100 characters.""",
}


class LLMAnalyzer:
    """Generate trading signals from unstructured events using an LLM.

    All calls use the configured autoresearch model (default: Haiku).
    Prompts can be overridden at runtime for optimization trials.
    """

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        from tradingagents.strategies.data_sources.regulation_graphics_grid import configured as graphics_execution_configured
        graphics_execution_configured(self.config)
        from tradingagents.strategies.data_sources.regulation_prompt_budget import configured as budget_configured
        budget_configured(self.config.get('autoresearch', {}))
        self._model_name = self.config.get(
            "autoresearch", {}
        ).get("autoresearch_model", "claude-haiku-4-5-20251001")
        self._effort = self.config.get("autoresearch", {}).get("llm_effort", "medium")
        self._thesis_model = self.config.get("autoresearch", {}).get("thesis_model", self._model_name)
        self._thesis_effort = self.config.get("autoresearch", {}).get("thesis_effort", self._effort)
        self.last_call_provenance: dict[str, Any] = {}
        self.last_call_failure = ""
        self.last_call_transport_failure: dict[str, Any] | None = None
        self.last_research_request: dict[str, Any] | None = None
        self.last_bounded_document_attempt: dict[str, Any] | None = None
        self._provider_clients: dict[str, Any] = {}
        # Older models use temperature zero by default. Sonnet 5 omits sampling
        # controls because adaptive thinking rejects non-default values.
        self._temperature = self.config.get("autoresearch", {}).get("llm_temperature", 0.0)
        self._client = None
        self._prompt_overrides: dict[str, str] = {}

    def fork_for_parallel(self) -> "LLMAnalyzer":
        """Isolate mutable analysis state while retaining exact native settings.

        Native clients are read only at the supported constructor-property
        boundary in call_analysis_model; actual calls use disposable processes.
        Their lifecycle remains with the original owner. Lazy clients initialize
        in the fork as before. Injected direct-call clients must explicitly fork.
        """
        def fork_client(client):
            if client is None:
                return None
            from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
            if isinstance(client, ChatGPTPlanClient):
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
        result._prompt_overrides = dict(self._prompt_overrides)
        result.last_call_provenance = {}
        result.last_call_failure = ""
        result.last_call_transport_failure = None
        result.last_research_request = None
        result.last_bounded_document_attempt = None
        result._client = fork_client(self._client)
        result._provider_clients = {name: fork_client(client) for name, client in self._provider_clients.items()}
        return result

    def get_prompt(self, strategy_name: str) -> str:
        """Return the active system prompt for a strategy."""
        if strategy_name in self._prompt_overrides and self._prompt_overrides[strategy_name]:
            return self._prompt_overrides[strategy_name]
        alias = {"weather_ag": "ag_weather", "ag_weather": "weather_ag"}.get(strategy_name)
        if alias in self._prompt_overrides:
            return self._prompt_overrides[alias]
        return _DEFAULT_PROMPTS.get(strategy_name, _DEFAULT_PROMPTS.get(alias, ""))

    def set_prompt_override(self, strategy_name: str, prompt: str) -> None:
        """Override the system prompt for a strategy (empty string = revert to default)."""
        if prompt:
            self._prompt_overrides[strategy_name] = prompt
        else:
            self._prompt_overrides.pop(strategy_name, None)

    def _get_client(self):
        """Lazy-init the selected provider; credentials stay in its environment."""
        if self._client is None:
            from tradingagents.strategies.chatgpt_plan_client import configured_plan_client
            plan_client = configured_plan_client(self.config, self._model_name)
            if plan_client is not None:
                self._client = plan_client
                return self._client
            try:
                from tradingagents.strategies.llm_utils import uses_responses
                if uses_responses(self._model_name):
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

    def _call_llm(self, system: str, user: str, max_tokens: int = 4096, *, role: str = "thesis", document_inputs=None, max_call_seconds=None) -> str:
        """Make a single LLM call. Returns response text or empty string."""
        from tradingagents.strategies.candidate_response_reuse import reused_candidate_response, retain_candidate_response
        from tradingagents.strategies.llm_utils import call_analysis_model, uses_responses
        from tradingagents.strategies.chatgpt_plan_client import configured_plan_client, require_configured_billing
        from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded, ModelTransportError, model_timeout
        model = self._model_name if role == "bounded" else self._thesis_model
        effort = self._effort if role == "bounded" else self._thesis_effort
        from tradingagents.strategies.orchestration.research_resilience import configured as resilience_configured
        from tradingagents.strategies.orchestration.research_model_attempt import request_receipt
        self.last_research_request = None
        if resilience_configured(self.config.get('autoresearch', {})):
            self.last_research_request = request_receipt(model=model, role=role, effort=effort,
                max_tokens=max_tokens, temperature=self._temperature, system=system, prompt=user)
        self.last_call_response = {'received': False, 'text': None}
        def observed_response(text):
            if type(text) is str:
                self.last_call_response = {'received': True, 'text': text}
        self.last_call_failure = ""
        self.last_call_transport_failure = None
        self.last_call_provenance = {"configured_model": model, "returned_model": None,
                                     "returned_revision": None, "identity_status": "unpinned",
                                     "response_id": None, "reasoning_effort": effort, "role": role}
        # Attachment identity is not part of the legacy candidate memo key.
        # Clear pending state as well: a prior plain request must not be committed
        # accidentally after a document call (including a failed call).
        if document_inputs is not None:
            from tradingagents.strategies.candidate_response_reuse import _CANDIDATE
            candidate_request = _CANDIDATE.get()
            if candidate_request is not None:
                candidate_request.key = candidate_request.digest = candidate_request.pending = None
        try:
            from tradingagents.strategies.runtime_deadline import graphics_call_seconds
            configured_call_seconds = graphics_call_seconds(self.config)
            if max_call_seconds is not None:
                if (type(max_call_seconds) not in (int, float)
                        or max_call_seconds != configured_call_seconds
                        or role != 'bounded' or document_inputs is None):
                    raise ValueError('invalid_model_call_cap')
            model_timeout()
            if document_inputs is not None:
                from tradingagents.strategies.plan_document_inputs import encode_document_inputs
                encode_document_inputs(document_inputs)
                if role != 'bounded':
                    raise ValueError('invalid_bounded_document')
            plan_client = configured_plan_client(self.config, model)
            mixed_provider = uses_responses(model) != uses_responses(self._model_name)
            if plan_client is not None and mixed_provider:
                client = self._provider_clients.setdefault("chatgpt_plan", plan_client)
            elif mixed_provider:
                # Mixed-provider configurations do not mutate the shared role model.
                provider = "openai" if uses_responses(model) else "anthropic"
                if provider not in self._provider_clients:
                    if provider == "openai":
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
            reused = None if document_inputs is not None else reused_candidate_response(client, model=model, system=system, prompt=user,
                                                max_tokens=max_tokens, temperature=self._temperature,
                                                effort=effort, role=role)
            if reused is not None:
                text, self.last_call_provenance = reused
                observed_response(text)
                model_timeout()
                return text
            text = call_analysis_model(
                client, model=model, max_tokens=max_tokens,
                system=system, prompt=user, temperature=self._temperature,
                effort=effort, provenance=self.last_call_provenance,
                response_observer=observed_response,
                **({"document_inputs": document_inputs} if document_inputs is not None else {}),
                **({"max_call_seconds": max_call_seconds} if max_call_seconds is not None else {}),
            )
            self.last_call_provenance["role"] = role
            if document_inputs is None:
                retain_candidate_response(text, self.last_call_provenance)
            return text
        except ModelDeadlineExceeded:
            self.last_call_failure = "model_deadline_exhausted"
            return ""
        except ModelTransportError as exc:
            from tradingagents.strategies.transport_worker import validate_error_metadata
            status = exc.status_code
            self.last_call_transport_failure = {
                'status_code': status if type(status) is int and 100 <= status <= 599 else None,
                'error_metadata': validate_error_metadata(exc.error_metadata),
            }
            self.last_call_failure = "analysis_unavailable"
            logger.error("LLM transport failed")
            return ""
        except Exception:
            self.last_call_failure = "analysis_unavailable"
            logger.error("LLM call failed")
            return ""

    def analyze_bounded_document_evidence(
        self, evidence: dict, *, session_as_of=None, regime_context: dict | None = None,
        decision_scope=None, ticker=None, verification_as_of=None, verification_clock=None,
    ) -> dict[str, Any]:
        """Recognize pages, then obtain a scoped thesis with optional eligibility.

        Structural validation is not independent extraction truth. Complete
        source residual failures survive every successful selected-page result.
        """
        import hashlib
        from tradingagents.strategies.bounded_document_research import (
            POLICY, REQUEST_VERSION, MAX_RESPONSE_BYTES, SAFE_FAILURE_CODES, prepare_recognition, validate_recognition,
            prepare_thesis, validate_thesis,
        )
        from tradingagents.strategies.orchestration.research_model_attempt import request_receipt
        from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded, model_timeout
        self.last_call_failure = ''
        self.last_call_transport_failure = None
        self.last_call_provenance = {}
        self.last_call_response = None
        self.last_research_request = None
        attempt = {'version': 'bounded-document-research-attempt-v1', 'validation_status': 'not_started',
                   'source_identity': None, 'recognition': None, 'recognition_attempt': None,
                   'thesis_attempt': None, 'failure_reason': None}
        self.last_bounded_document_attempt = attempt

        def seal_attempt():
            compact = {key: value for key, value in attempt.items() if key != 'receipt_sha256'}
            attempt['receipt_sha256'] = hashlib.sha256(json.dumps(compact, sort_keys=True,
                separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('utf-8')).hexdigest()

        def call(system, user, *, role, document_inputs=None, stage_name=None):
            model = self._model_name if role == 'bounded' else self._thesis_model
            effort = self._effort if role == 'bounded' else self._thesis_effort
            stage = {'model_request': request_receipt(model=model, role=role, effort=effort,
                max_tokens=4096, temperature=self._temperature, system=system, prompt=user),
                'model_provenance': {}, 'model_call_started': True, 'response_received': False,
                'response_text': None, 'response_sha256': None, 'response_bytes': None,
                'response_encoding': None, 'response_over_limit': False, 'validation_status': 'failed'}
            attempt[stage_name or ('recognition_attempt' if role == 'bounded' else 'thesis_attempt')] = stage
            text = self._call_llm(system, user, role=role,
                                 **({'document_inputs': document_inputs} if document_inputs is not None else {}))
            if isinstance(self.last_call_transport_failure, dict):
                stage['transport_failure'] = deepcopy(self.last_call_transport_failure)
            stage['model_provenance'] = deepcopy(self.last_call_provenance)
            observed = self.last_call_response
            raw_text = observed.get('text') if type(observed) is dict and observed.get('received') is True else None
            if type(raw_text) is str:
                encoding = 'utf-8'
                try:
                    raw = raw_text.encode('utf-8')
                except UnicodeEncodeError:
                    raw = raw_text.encode('utf-8', errors='surrogatepass')
                    encoding = 'utf-8-surrogatepass'
                stage.update(response_received=True, response_sha256=hashlib.sha256(raw).hexdigest(),
                    response_bytes=len(raw), response_encoding=encoding,
                    response_over_limit=len(raw) > MAX_RESPONSE_BYTES,
                    response_text=raw_text if len(raw) <= MAX_RESPONSE_BYTES else None)
            model_timeout()
            if self.last_call_failure == 'model_deadline_exhausted':
                raise ModelDeadlineExceeded('model_deadline_exhausted')
            if self.last_call_failure or not text or stage['response_over_limit']:
                raise ValueError('invalid_bounded_document')
            return text, stage

        try:
            ar = self.config.get('autoresearch', {})
            from tradingagents.strategies.document_review_clock import POLICY as NEXT_OPEN_POLICY, review_clock
            clock_options = {}
            if ar.get('bounded_document_decision_policy') == NEXT_OPEN_POLICY:
                clock_options = {'review_clock':review_clock(self.config, session_as_of)}
                if type(decision_scope) is not dict or 'independent_review' in decision_scope:
                    raise ValueError('invalid_document_decision_clock_policy')
            if ar.get('bounded_document_decision_policy') is not None:
                execution = self.config.get('execution', {})
                if execution.get('mode') != 'paper' or execution.get('execution_enabled') is not False:
                    raise ValueError('invalid_document_decision_paper_only')
            if (ar.get('bounded_document_research_policy') != POLICY
                    or ar.get('llm_billing_mode') != 'chatgpt_plan'
                    or self._model_name != 'gpt-6-luna' or self._effort != 'high'
                    or self._thesis_model != 'gpt-6.1-sol' or self._thesis_effort != 'high'):
                raise ValueError('invalid_bounded_document')
            model_timeout()
            request = prepare_recognition(evidence, session_as_of=session_as_of,
                request_version=ar.get('bounded_document_recognition_request_version', REQUEST_VERSION))
            attempt['source_identity'] = json.loads(request.context_json)
            text, stage = call(request.system, request.user, role='bounded', document_inputs=request.document_inputs)
            recognition = validate_recognition(text, request)
            model_timeout()
            stage['validation_status'] = 'validated'
            attempt['recognition'] = recognition
            decision_eligibility = None
            decision_policy = ar.get('bounded_document_decision_policy')
            if decision_policy is not None:
                from tradingagents.strategies.document_decision_eligibility import (
                    POLICY as DECISION_POLICY, assess_decision_eligibility,
                    prepare_automatic_verification, build_automatic_review,
                )
                if decision_policy not in (DECISION_POLICY, NEXT_OPEN_POLICY):
                    raise ValueError('invalid_document_decision_policy')
                if type(decision_scope) is dict and 'independent_review' not in decision_scope:
                    system,user = prepare_automatic_verification(request,decision_scope,**clock_options)
                    checked,check_stage = call(system,user,role='bounded',document_inputs=request.document_inputs,
                        stage_name='verification_attempt')
                    if verification_clock is not None:
                        verification_as_of = verification_clock()
                    elif verification_as_of is None:
                        from datetime import datetime,timezone
                        verification_as_of = datetime.now(timezone.utc).isoformat()
                    automatic_review = build_automatic_review(recognition,checked,request,decision_scope,
                        ticker=ticker,session_as_of=session_as_of,verified_at=verification_as_of,
                        model_provenance=check_stage['model_provenance'],**clock_options)
                    check_stage['validation_status'] = 'validated'
                    attempt['automatic_review'] = deepcopy(automatic_review)
                    decision_scope = {**decision_scope,'independent_review':automatic_review}
                decision_eligibility = assess_decision_eligibility(recognition, decision_scope,
                    ticker=ticker, session_as_of=session_as_of, **clock_options)
                attempt['decision_eligibility'] = deepcopy(decision_eligibility)
            system, user = prepare_thesis(recognition, regime_suffix=self._regime_suffix(regime_context),
                decision_eligibility=decision_eligibility)
            text, stage = call(system, user, role='thesis')
            result = validate_thesis(text, decision_eligibility=decision_eligibility)
            model_timeout()
            stage['validation_status'] = 'validated'
            attempt['validation_status'] = 'validated'
            seal_attempt()
            return result | {'bounded_document_research': deepcopy(attempt)}
        except ModelDeadlineExceeded:
            self.last_call_failure = 'model_deadline_exhausted'
            raise
        except ValueError as exc:
            if not self.last_call_failure:
                reason = str(exc)
                self.last_call_failure = reason if reason in SAFE_FAILURE_CODES else 'invalid_bounded_document'
            raise
        finally:
            if attempt['validation_status'] != 'validated':
                attempt['validation_status'] = 'failed'
                attempt['failure_reason'] = self.last_call_failure or 'analysis_unavailable'
            seal_attempt()

    # ------------------------------------------------------------------
    # Filing analysis (P3: 10-K/10-Q material changes)
    # ------------------------------------------------------------------

    def analyze_filing_evidence(
        self, analysis_type: str, current_evidence, *, prior_evidence=None,
        issuer_binding: dict, target_binding: dict | None = None,
        regime_context: dict | None = None, news_evidence=None,
        required_material_dependencies=None, comparison_binding=None, ownership_bundles=None,
    ) -> dict[str, Any]:
        """Opt-in complete selected evidence with strict attribution/citations.

        Existing thesis transport, settings, output cap and inherited aggregate
        deadline remain authoritative. This does not resolve trading securities.
        """
        from tradingagents.strategies.data_sources.filing_assessment import (
            prepare_request, validate_assessment,
        )
        from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded, model_timeout
        self.last_call_provenance = {}
        self.last_call_failure = ""
        try:
            model_timeout()
            override = self._prompt_overrides.get(analysis_type)
            if override is None:
                override = (self.get_prompt("quantum_readiness") if analysis_type == "quantum_readiness"
                            else self._prompt_overrides.get("filing_analysis"))
            request = prepare_request(
                analysis_type, current_evidence, prior_evidence=prior_evidence,
                issuer_binding=issuer_binding, target_binding=target_binding,
                regime_context=regime_context, news_evidence=news_evidence,
                required_material_dependencies=required_material_dependencies,
                comparison_binding=comparison_binding, system_override=override,
                ownership_bundles=ownership_bundles,
            )
            model_timeout()
            text = self._call_llm(request.system, request.user, max_tokens=4096, role="thesis")
            model_timeout()
            if not text and self.last_call_failure == "model_deadline_exhausted":
                raise ModelDeadlineExceeded("model_deadline_exhausted")
            result = validate_assessment(text, request.context)
            model_timeout()
            return result
        except ModelDeadlineExceeded:
            self.last_call_failure = "model_deadline_exhausted"
            raise
        except ValueError as exc:
            if not self.last_call_failure:
                code = str(exc)
                self.last_call_failure = code if code.startswith("invalid_filing_") else "analysis_unavailable"
            raise

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
changes (list of strings describing material changes found), rationale (1-2 sentences)."""

        # Truncate to fit context
        current_excerpt = current_text[:3000]
        prior_excerpt = prior_text[:3000]

        user = f"""Ticker: {ticker}

CURRENT FILING (excerpt):
{current_excerpt}

PRIOR FILING (excerpt):
{prior_excerpt}

Analyze material changes and return JSON.""" + self._regime_suffix(regime_context)

        system += "\nInclude evidence_claim: one factual source-grounded catalyst assertion with company attribution; exclude return predictions and unsupported inference."
        result = self._call_llm(self._prompt_overrides.get("filing_change", self._prompt_overrides.get("filing_analysis", system)), user)
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
cluster_size (int), notable_insiders (list of strings, each combining name and title; not objects), rationale (1-2 sentences)."""

        filings_text = json.dumps(form4_filings[:10], indent=2, default=str)
        user = f"""Ticker: {ticker}

Recent Form 4 filings:
{filings_text}

Analyze insider trading patterns and return JSON.""" + self._regime_suffix(regime_context)

        system += "\nInclude evidence_claim: one factual source-grounded catalyst assertion with company attribution; exclude return predictions and unsupported inference."
        result = self._call_llm(self._prompt_overrides.get("insider_activity", system), user, role="bounded")
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
red_flags (list of strings describing specific concerns), rationale (1-2 sentences)."""

        filings_text = json.dumps(form4_filings[:10], indent=2, default=str)
        user = f"""Ticker: {ticker}

Recent Form 4 filings (check for 10b5-1 plan indicators):
{filings_text}

Analyze for red flags and return JSON.""" + self._regime_suffix(regime_context)

        system += "\nInclude evidence_claim: one factual source-grounded catalyst assertion with company attribution; exclude return predictions and unsupported inference."
        result = self._call_llm(self._prompt_overrides.get("insider_activity", system), user)
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
comp_changes (list of strings describing notable changes), rationale (1-2 sentences)."""

        proxy_excerpt = proxy_text[:4000]
        user = f"""Ticker: {ticker}

DEF 14A Proxy Statement (excerpt):
{proxy_excerpt}

Analyze executive compensation signals and return JSON.""" + self._regime_suffix(regime_context)

        system += "\nInclude evidence_claim: one factual source-grounded catalyst assertion with company attribution; exclude return predictions and unsupported inference."
        result = self._call_llm(self._prompt_overrides.get("exec_comp", self._prompt_overrides.get("filing_analysis", system)), user)
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
        user = f"""Ticker: {ticker}

{source_label} (excerpt):
{excerpt}

Analyze for trading signals and return JSON.""" + self._regime_suffix(regime_context)

        system += "\nInclude evidence_claim: one factual source-grounded catalyst assertion with company attribution; exclude return predictions and unsupported inference."
        result = self._call_llm(self._prompt_overrides.get("earnings_call", system), user)
        return _parse_json_response(result) if result else {}

    # ------------------------------------------------------------------
    # Regulatory analysis (P5: proposed rule impact)
    # ------------------------------------------------------------------

    def analyze_regulation_evidence(
        self, evidence: dict, *, target_bindings=(), regime_context: dict | None = None, assessment_policy=None,
    ) -> dict[str, Any]:
        """Assess all retained text and preserve the original attempt on failure."""
        import hashlib
        from tradingagents.strategies.data_sources.regulation_assessment import (
            MAX_RESPONSE_BYTES, prepare_request, validate_assessment,
        )
        from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded, model_timeout
        self.last_call_provenance = {}
        self.last_call_failure = ''
        self.last_call_response = None
        self.last_research_request = None
        self.last_call_transport_failure = None
        attempt = {'version': 'regulation-model-attempt-v1', 'source_provenance': None,
            'model_provenance': {}, 'model_call_started': False, 'response_received': False,
            'response_text': None, 'response_sha256': None, 'response_bytes': None,
            'response_encoding': None,
            'response_over_limit': False, 'validation_status': 'not_started', 'failure_reason': None}
        self.last_regulation_attempt = attempt
        try:
            from tradingagents.strategies.data_sources.regulation_graphics_grid import configured as graphics_execution_configured
            graphics_policy = graphics_execution_configured(self.config)
            from tradingagents.strategies.data_sources.regulation_prompt_budget import configured as budget_configured, require_dispatchable
            budget_policy = budget_configured(self.config.get('autoresearch', {}))
            if graphics_policy is not None:
                attempt.update(version='regulation-model-attempt-v5', graphics_plan=None,
                    graphics_batches=[], graphics_failures=[], prompt_budget_proof=None)
            model_timeout()
            from tradingagents.strategies.data_sources.regulation_graphics_inputs import graphics_assets
            assets = graphics_assets(evidence)
            if assets:
                from tradingagents.strategies.learning.regulation_graphics_runtime import recognize_graphics
                if graphics_policy is None:
                    attempt.update(version='regulation-model-attempt-v2', graphics_batches=[])
                try:
                    recognize_graphics(self, assets, attempt)
                finally:
                    self.last_call_response = None
                    self.last_call_provenance = {}
                    self.last_research_request = None
                    self.last_call_transport_failure = None
                request = prepare_request(evidence, target_bindings=target_bindings,
                    regime_context=regime_context, graphics_batches=attempt['graphics_batches'], assessment_policy=assessment_policy,
                    prompt_budget_policy=budget_policy,
                    **({'graphics_plan': attempt['graphics_plan']}
                       if attempt['version'] == 'regulation-model-attempt-v5' else {}))
            else:
                request = prepare_request(evidence, target_bindings=target_bindings, regime_context=regime_context,
                    assessment_policy=assessment_policy, prompt_budget_policy=budget_policy)
            attempt['source_provenance'] = json.loads(request.context.provenance_json)
            if attempt['version'] == 'regulation-model-attempt-v5':
                attempt['prompt_budget_proof'] = deepcopy(attempt['source_provenance']['prompt_budget_proof'])
                require_dispatchable(attempt['prompt_budget_proof'])
            model_timeout()
            attempt['model_call_started'] = True
            text = self._call_llm(request.system, request.user, max_tokens=4096, role='thesis')
            observed = self.last_call_response
            response_text = (observed['text'] if isinstance(observed, dict)
                and observed.get('received') is True else None) if observed is not None else text
            # Retain before checking the elapsed budget or validating JSON. A
            # returned but invalid/late original must not vanish from the audit.
            if type(response_text) is str:
                encoding = 'utf-8'
                try:
                    raw = response_text.encode('utf-8')
                except UnicodeEncodeError:
                    # Preserve invalid Unicode scalars without replacement or
                    # silently claiming valid UTF-8. JSON storage escapes them.
                    raw = response_text.encode('utf-8', errors='surrogatepass')
                    encoding = 'utf-8-surrogatepass'
                attempt.update(response_received=True, response_sha256=hashlib.sha256(raw).hexdigest(),
                    response_bytes=len(raw), response_over_limit=len(raw) > MAX_RESPONSE_BYTES,
                    response_encoding=encoding,
                    response_text=response_text if len(raw) <= MAX_RESPONSE_BYTES else None)
                if attempt['response_over_limit']:
                    raise ValueError('invalid_regulation_response_limit')
                if encoding != 'utf-8':
                    raise ValueError('invalid_regulation_response_encoding')
            model_timeout()
            if not text and self.last_call_failure == 'model_deadline_exhausted':
                raise ModelDeadlineExceeded('model_deadline_exhausted')
            result = validate_assessment(text, request.context)
            model_timeout()
            attempt['validation_status'] = 'validated'
            return result
        except ModelDeadlineExceeded:
            self.last_call_failure = 'model_deadline_exhausted'
            raise
        except ValueError as error:
            if not self.last_call_failure:
                code = str(error)
                self.last_call_failure = code if code.startswith('invalid_regulation_') else 'analysis_unavailable'
            raise
        except Exception:
            if not self.last_call_failure:
                self.last_call_failure = 'analysis_unavailable'
            raise
        finally:
            attempt['model_provenance'] = deepcopy(self.last_call_provenance)
            if attempt['validation_status'] != 'validated':
                attempt['validation_status'] = 'failed'
                attempt['failure_reason'] = self.last_call_failure or 'analysis_unavailable'

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
affected_tickers (max 5 ticker symbols), affected_sectors (list of strings, max 3),
impact_assessment (1 sentence), rationale (1 sentence).
Keep ALL string values under 80 characters."""

        user = f"""Agency: {agency}
Rule Title: {rule_title}

Rule Summary:
{rule_summary[:3000]}

Identify affected companies and return JSON.""" + self._regime_suffix(regime_context)

        system += "\nInclude evidence_claim: one factual source-grounded catalyst assertion with company attribution; exclude return predictions and unsupported inference."
        result = self._call_llm(self._prompt_overrides.get("regulatory_pipeline", system), user)
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
Known peer companies (not verified supplier/customer edges): {', '.join(peer_tickers[:15])}

NEWS:
Headline: {headline}
Summary: {summary[:2000]}

Analyze supply chain impact and return JSON.""" + self._regime_suffix(regime_context)

        system += "\nInclude evidence_claim: one factual source-grounded catalyst assertion with company attribution; exclude return predictions and unsupported inference."
        result = self._call_llm(self._prompt_overrides.get("supply_chain", system), user)
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

        system += "\nInclude evidence_claim: one factual source-grounded catalyst assertion with company attribution; exclude return predictions and unsupported inference."
        result = self._call_llm(self._prompt_overrides.get("litigation", system), user)
        return _parse_json_response(result) if result else {}

    # ------------------------------------------------------------------
    # Quantum readiness analysis (PQC migration regime)
    # ------------------------------------------------------------------

    def analyze_quantum_readiness(
        self,
        text: str,
        ticker: str,
        text_source: str = "filing",
        regime_context: dict | None = None,
    ) -> dict[str, Any]:
        """Assess PQC readiness and regime signal from filing or news text.

        Args:
            text: Filing excerpt or news article text.
            ticker: Company ticker (may be empty for mandate/industry news).
            text_source: "filing", "news", or "milestone".
            regime_context: Optional market regime data.

        Returns:
            Dict with: regime_signal, regime_confidence, direction,
            conviction, pqc_readiness, crypto_dependency, rationale.
        """
        system = self.get_prompt("quantum_readiness")

        source_labels = {
            "filing": "SEC FILING",
            "news": "NEWS ARTICLE",
            "milestone": "QUANTUM MILESTONE REPORT",
        }
        label = source_labels.get(text_source, "TEXT")
        excerpt = text[:4000]

        user = f"""Ticker: {ticker or "(industry-level signal)"}
Source type: {text_source}

{label} (excerpt):
{excerpt}

Analyze for PQC regime signal and trading direction. Return JSON.""" + self._regime_suffix(regime_context)

        system += "\nInclude evidence_claim: one factual source-grounded catalyst assertion with company attribution; exclude return predictions and unsupported inference."
        result = self._call_llm(system, user)
        return _parse_json_response(result) if result else {}

    # ------------------------------------------------------------------
    # Agricultural weather analysis (ag_weather)
    # ------------------------------------------------------------------

    def analyze_state_economics(self, *, ticker: str, state_event: dict, state_exposure: dict,
                                regime_context: dict | None = None) -> dict[str, Any]:
        """Required assessment of a retained release and reviewed issuer exposure."""
        system = '''Assess only the retained state unemployment event and reviewed source registry exposure.
The registry asserts operating geography; it does not establish causal returns.
Distinguish observation period, original/revised vintage, and acquisition availability.
Do not call a rate change a consensus surprise without expectations evidence.
Do not infer exposure from headquarters, incorporation, sector ETFs, or momentum.
Do not invent tickers, exposure weights, release times, or performance evidence.
Return neutral when the evidence does not support a directional thesis.
Return ONLY JSON with exactly: contract_version ("state-assessment-v1"),
direction ("long"/"short"/"neutral"), conviction (number 0-1), rationale,
evidence_claim (one factual source-grounded attribution assertion), event_id
(copy state_event.source_observation_id), target_ticker (copy supplied ticker),
exposure_quote (copy the exact supplied geography_quote).'''
        user = json.dumps({'ticker': ticker, 'state_event': state_event,
                           'state_exposure': state_exposure, 'regime': regime_context}, allow_nan=False)
        result = self._call_llm(system, user, role='bounded')
        return _parse_json_response(result) if result else {}

    def analyze_commodity_macro(self, ticker: str, commodity_name: str,
                                cot_context: dict, macro_context: dict,
                                regime_context: dict | None = None) -> dict[str, Any]:
        """Optional context only; complete deterministic COT rules remain authority."""
        system = self._prompt_overrides.get("commodity_macro", """Analyze retained COT positioning and macro observations.
Do not invent releases or infer commodity evidence from unrelated headlines.
Return JSON: direction (long/short/neutral), score (0-1), reasoning (source-grounded context), evidence_claim (one factual catalyst claim; no future-return prediction).""")
        user = json.dumps({"ticker": ticker, "commodity": commodity_name,
                           "cot": cot_context, "macro": macro_context}, default=str)
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
        """Analyze agricultural supply disruption risk for a ticker.

        Returns dict with: direction ("long"/"neutral"), score (0-1), reasoning.
        """
        system = self.get_prompt("ag_weather")

        noaa = ag_context.get("noaa_data", {})
        drought_score = ag_context.get("drought_score", 0.0)
        drought_states = ag_context.get("drought_states", {})
        usda = ag_context.get("usda_data", {})

        # Use the same dated region/class observations as the screening gate.
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
                    if prior_ge is not None else "weekly comparison unavailable"
                )
                crop_lines.append(f"{label}: {ge:g}% Good/Excellent ({change})")

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
