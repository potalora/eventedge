from __future__ import annotations

from typing import Any

from .base import Candidate


class StateEconomicsStrategy:
    """Opt-in source-vintage state releases; legacy proxy stays retired."""

    retirement_reason = "unsupported_state_event_proxy"

    name = "state_economics"
    track = "paper_trade"
    data_sources = ["yfinance", "fred", "openbb"]

    def __init__(self):
        self._state_config = None

    def configure(self, config):
        from ..data_sources.state_economics_policy import configured
        enabled = config is not None and configured(config)
        self._state_config = dict(config) if enabled else None
        self.retirement_reason = None if enabled else 'unsupported_state_event_proxy'
        self.data_sources = ['fred', 'edgar'] if enabled else ['yfinance', 'fred', 'openbb']

    def get_param_space(self, horizon: str = "30d") -> dict[str, tuple]:
        return {}

    def get_default_params(self, horizon: str = "30d") -> dict[str, Any]:
        from tradingagents.strategies.orchestration.cohort_orchestrator import HORIZON_PARAMS
        return {"rebalance_days": HORIZON_PARAMS.get(horizon, HORIZON_PARAMS["30d"])["hold_days_default"]}

    def screen(self, data: dict, date: str, params: dict) -> list[Candidate]:
        """Retain genuine state releases; only reviewed exposures request a model."""
        if self._state_config is None:
            return []
        from ..data_sources.state_economics_policy import (
            POLICY, aware, digest, release_events, verified_exposures,
        )
        source = data.get('fred', {})
        snapshot = source.get('state_unemployment')
        registry = source.get('state_exposure_registry')
        universe = data.get('_state_economics_universe')
        from ..orchestration.state_inputs import state_policy_scope
        scope = state_policy_scope(data, self._state_config, date, universe=universe)
        coverage = scope['source_coverage']
        events = release_events(snapshot, lookback_days=self._state_config.get('state_release_lookback_days', 7))
        if scope['available_at'] is None:
            return []
        cutoff = aware(scope['available_at'])
        exposure_error = scope['exposure_registry_error'] is not None
        exposures = ([] if exposure_error else verified_exposures(registry, universe=universe,
            company_map=data.get('edgar', {}).get('company_tickers'), cutoff=cutoff.isoformat()))
        candidates = []
        for event in events:
            matches = [row for row in exposures if row['state'] == event['state']]
            for exposure in matches or [None]:
                reason = ('incomplete_state_unemployment_coverage' if not coverage['complete'] else
                          'invalid_state_exposure_registry' if exposure_error else
                          'no_verified_state_exposure' if exposure is None else
                          'missing_state_baseline' if event['previous_month_value'] is None else None)
                metadata = {'state_economics_policy': POLICY, 'state_event': event,
                    'state_exposure': exposure, 'state_source_coverage': coverage,
                    'state_registry_sha256': digest(registry),
                    'available_at': max(aware(event['available_at']), cutoff).isoformat(),
                    'source_observation_ids': [event['source_observation_id']],
                    'needs_llm_analysis': reason is None, 'analysis_type': 'state_economics'}
                if reason:
                    metadata['non_actionable_reason'] = reason
                candidates.append(Candidate(ticker=exposure['ticker'] if exposure else '', date=date,
                    direction='neutral', score=0.0, confidence=0.0, metadata=metadata,
                    journal_only=reason is not None))
        return candidates

    def check_exit(
        self,
        ticker: str,
        entry_price: float,
        current_price: float,
        holding_days: int,
        params: dict,
        data: dict,
        direction: str = "long",
    ) -> tuple[bool, str]:
        """Exit on rebalance schedule."""
        rebalance_days = params.get("rebalance_days", 30)
        if holding_days >= rebalance_days:
            return True, "rebalance"
        return False, ""

    def build_propose_prompt(self, context: dict) -> str:
        if self._state_config is None:
            raise RuntimeError("state_economics is retired: unsupported_state_event_proxy")
        return 'State release discovery and reviewed exposure scope are fixed; do not optimize source clocks or invent directional rules.'
