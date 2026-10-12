"""Validate prospective state inputs and retained health without new calls."""
from __future__ import annotations

from datetime import date

from tradingagents.strategies.data_sources.state_economics_policy import (
    POLICY, REGISTRY_SHA256, aware, configured, digest, validate_assessment,
    validate_snapshot, verified_exposures,
)


def state_policy_scope(data, config, session, *, universe=None):
    enabled = configured(config)
    fred = data.get('fred', {})
    if not isinstance(fred, dict):
        raise ValueError('invalid_state_source_payload')
    snapshot, registry = fred.get('state_unemployment'), fred.get('state_exposure_registry')
    if not enabled:
        if snapshot is not None or registry is not None:
            raise ValueError('undeclared_state_economics_policy')
        return None
    settings = fred.get('state_policy_settings')
    if settings != {'policy': POLICY, 'release_lookback_days': config.get('state_release_lookback_days', 7)}:
        raise ValueError('invalid_state_policy_settings')
    coverage = validate_snapshot(snapshot)
    from .decision_clock import require_reference
    from tradingagents.strategies.data_sources.equity_universe import EquityUniverse
    if universe is None and isinstance(data.get('equity_universe', {}).get('snapshot'), dict):
        universe = EquityUniverse(data['equity_universe']['snapshot'],
            company_map=data.get('edgar', {}).get('company_tickers'))
    clocks = [aware(row['acquired_at']) for row in snapshot['states'].values()]
    if universe is not None:
        clocks.append(aware(universe.evidence['observed_at']))
    company_map_error = False
    edgar = data.get('edgar', {})
    edgar = edgar if isinstance(edgar, dict) else {}
    has_exposures = isinstance(registry, dict) and bool(registry.get('exposures'))
    if has_exposures:
        try:
            clocks.append(aware(edgar.get('company_tickers_available_at')))
            if edgar.get('company_tickers_sha256') != digest(edgar.get('company_tickers')):
                raise ValueError('invalid_state_security_map_binding')
        except (ValueError, TypeError):
            company_map_error = True
    cutoff = max(clocks) if clocks else None
    decision_context = data.get('_decision_context')
    if decision_context is not None:
        decision_cutoff = aware(decision_context['cutoff'])
        if cutoff is not None and decision_cutoff < cutoff:
            raise ValueError('state_source_after_decision_cutoff')
        cutoff = decision_cutoff
    acquisition = data.get('_decision_acquisition')
    if acquisition is not None:
        from .decision_clock import mutable_vintage
        started = aware(acquisition.get('started_at'))
        if (acquisition.get('policy') != 'prospective_next_open_v1' or acquisition.get('reference_session') != session
                or acquisition.get('vintage_as_of') != mutable_vintage({'autoresearch': config}, date.fromisoformat(session), now=started)
                or acquisition['vintage_as_of'] != snapshot['as_of']):
            raise ValueError('invalid_state_acquisition_clock')
    if cutoff is not None:
        require_reference(date.fromisoformat(session), cutoff)
    exposures, registry_error = [], None
    try:
        if company_map_error:
            raise ValueError('invalid_state_security_map_clock')
        if digest(registry) != digest(config['state_exposure_manifest']):
            raise ValueError('invalid_state_exposure_registry_binding')
        if cutoff is None and registry.get('exposures'):
            raise ValueError('state_registry_cutoff_unavailable')
        exposures = verified_exposures(registry, universe=universe,
            company_map=data.get('edgar', {}).get('company_tickers'),
            cutoff=(cutoff.isoformat() if cutoff else snapshot['as_of'] + 'T00:00:00+00:00'))
    except (ValueError, TypeError, KeyError):
        registry_error = 'invalid_state_exposure_registry'
    return {'policy': POLICY, 'series_registry_sha256': REGISTRY_SHA256,
        'source_coverage': coverage, 'exposure_registry_sha256': digest(registry),
        'exposure_registry_error': registry_error,
        'company_tickers_sha256': edgar.get('company_tickers_sha256') if has_exposures else None,
        'company_tickers_available_at': edgar.get('company_tickers_available_at') if has_exposures else None, 'verified_exposure_count': len(exposures),
        'verified_exposures_sha256': digest(exposures),
        'exposure_scope': [{'state': row['state'], 'issuer_cik': row['issuer_cik'],
            'ticker': row['ticker'], 'source_submission_sha256': row['source_submission_sha256']}
            for row in exposures], 'available_at': cutoff.isoformat() if cutoff else None}


def validate_state_health(data, config, session, records, policy_ids):
    """Recompute every retained event, source clock and reviewed exposure binding."""
    if not configured(config):
        state_policy_scope(data, config, session)
        return None
    try:
        scope = state_policy_scope(data, config, session)
    except (ValueError, TypeError, KeyError):
        scope = None
    expected = []
    if scope is not None:
        from tradingagents.strategies.modules.state_economics import StateEconomicsStrategy
        from tradingagents.strategies.data_sources.equity_universe import EquityUniverse
        strategy = StateEconomicsStrategy(); strategy.configure(config)
        universe_data = data.get('equity_universe', {}).get('snapshot')
        universe = EquityUniverse(universe_data, company_map=data.get('edgar', {}).get('company_tickers')) if universe_data else None
        expected = strategy.screen(dict(data, _state_economics_universe=universe), session, {})
    def field(record, key):
        return record.get(key) if isinstance(record, dict) else getattr(record, key)
    selected = [record for record in records if field(record, 'strategy') == 'state_economics']
    if not policy_ids or len(selected) != len(policy_ids) or {field(r, 'policy_id') for r in selected} != policy_ids:
        raise ValueError('invalid_state_health_coverage')
    core = ('state_economics_policy','state_event','state_exposure','state_source_coverage',
            'state_registry_sha256','available_at','source_observation_ids','analysis_type')
    expected_by_key = {(c.metadata['state_event']['source_observation_id'], c.ticker):c for c in expected}
    if len(expected_by_key) != len(expected):
        raise ValueError('duplicate_state_economic_event')
    for record in selected:
        evidence = field(record, 'evidence')
        rows = evidence.get('state_economic_events')
        if (evidence.get('state_source_scope') != scope or not isinstance(rows, list)
                or evidence.get('data_sources') != ['edgar','fred']
                or evidence.get('state_source_coverage') != (scope['source_coverage'] if scope else None)
                or field(record, 'signal_count') != len(rows) or len(rows) != len(expected)):
            raise ValueError('invalid_state_health_binding')
        status = field(record, 'status')
        if (status not in {'signals','legitimate_no_event','data_failure'}
                or type(field(record, 'signal_count')) is not int
                or evidence.get('candidate_count') != len(rows)
                or (status in {'signals','legitimate_no_event'} and
                    (status != ('signals' if rows else 'legitimate_no_event') or evidence.get('provider_errors')))):
            raise ValueError('invalid_state_health_classification')
        failure = (scope is None or scope['exposure_registry_error'] is not None or not scope['source_coverage']['complete'])
        if failure and field(record, 'status') != 'data_failure':
            raise ValueError('invalid_state_failure_health')
        deadline_hold = (evidence.get('model_coverage') == {'complete': False, 'reason': 'model_deadline_exhausted'}
            and status == 'data_failure')
        seen = set()
        for row in rows:
            key = (row.get('state_event', {}).get('source_observation_id'), row.get('ticker'))
            candidate = expected_by_key.get(key)
            if candidate is None or key in seen or any(row.get(k) != candidate.metadata.get(k) for k in core):
                raise ValueError('invalid_state_economic_event')
            seen.add(key)
            if row.get('analysis_status') == 'validated':
                if not candidate.metadata['needs_llm_analysis']:
                    raise ValueError('unexpected_state_analysis')
                assessment = validate_assessment(row.get('llm_analysis'), ticker=candidate.ticker,
                    event=candidate.metadata['state_event'], exposure=candidate.metadata['state_exposure'])
                if row.get('direction') != assessment['direction'] or row.get('score') != assessment['conviction']:
                    raise ValueError('invalid_state_analysis_binding')
            elif candidate.metadata['needs_llm_analysis']:
                if not row.get('analysis_failure_reason') or row.get('journal_only') is not True or field(record, 'status') != 'data_failure':
                    raise ValueError('missing_required_state_analysis')
            elif (row.get('journal_only') is not True or row.get('direction') != 'neutral' or row.get('score') != 0
                    or (row.get('non_actionable_reason') != candidate.metadata['non_actionable_reason'] and not
                        (deadline_hold and row.get('non_actionable_reason') == 'model_sample_incomplete'
                         and row.get('analysis_failure_reason') == 'model_deadline_exhausted'))):
                raise ValueError('invalid_state_journal_event')
    return scope


def declared_state_config(data):
    """Infer only the explicit settings carried by an accepted source bundle."""
    fred = data.get('fred', {})
    if 'state_unemployment' not in fred and 'state_exposure_registry' not in fred:
        return {}
    settings = fred.get('state_policy_settings')
    if not isinstance(settings, dict) or set(settings) != {'policy','release_lookback_days'}:
        raise ValueError('invalid_state_policy_settings')
    result = {'state_economics_policy': settings['policy'],
        'state_release_lookback_days': settings['release_lookback_days'],
        'state_exposure_manifest': fred.get('state_exposure_registry'),
        'equity_universe_policy': 'us_exchange_listed_sip_v1',
        'decision_clock_policy': 'prospective_next_open_v1', 'disabled_strategies': {}}
    configured(result)
    return result
