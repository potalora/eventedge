"""Prospective research dependency containment; never an integrity-error fallback."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib

POLICY = 'dependency_isolation_v1'


def configured(config: dict) -> bool:
    """Read the autoresearch mapping without changing historical defaults."""
    value = config.get('research_resilience_policy')
    if value is None:
        return False
    if type(value) is not str or value != POLICY:
        raise ValueError('invalid_research_resilience_policy')
    return True


def digest(value, *, codec_limits=None) -> str:
    """Bind codec-supported evidence without stringifying unsupported objects."""
    from .source_inputs import SourceInputStore
    return hashlib.sha256(SourceInputStore.encode(value, **(codec_limits or {})).encode()).hexdigest()


CAPABILITY_POLICY = 'regime_strategy_capabilities_v1'
ACQUISITION_KEY = '_capability_acquisition'
FRED_GROUPS = {'credit_spreads': ['BAMLH0A0HYM2', 'BAMLC0A4CBBB'],
    'economic_indicators': ['UNRATE', 'CPIAUCSL', 'PAYEMS', 'ICSA', 'FEDFUNDS', 'VIXCLS', 'T10Y2Y']}
YAHOO_TICKERS = ['SPY', 'SHY', 'TLT', 'DBA', 'WEAT', 'CORN', 'MOO', 'SOYB', 'ADM', 'BG', 'CTVA', 'DE', 'FMC',
    'KRE', 'IWN', 'XRT', 'IYR', 'XHB', 'ITB', 'VNQ', 'SOXX', 'XLI', 'XLRE', 'LMT', 'RTX', 'NOC', 'GD', 'BA',
    'LHX', 'LDOS', 'SAIC', 'BAH', 'PLTR', 'KTOS', 'CACI', 'HEI', 'TDG']
_REASONS = {'timeout', 'transport_error', 'http_error', 'invalid_response', 'provider_error', 'batch_failure'}


def capability_configured(config):
    value = config.get('source_capability_policy')
    if value is None:
        return False
    if type(value) is not str or value != CAPABILITY_POLICY or not configured(config):
        raise ValueError('invalid_source_capability_policy')
    return True


def acquisition_group(requested, *, error=None, error_text=None, independent=False):
    """Record exhaustive call outcomes, never infer success from a partial frame."""
    requested = list(requested)
    if error is None and error_text is None:
        return {'requested': requested, 'status': 'complete', 'reason': None, 'http_status': None,
                'error': None, 'outcomes': {name: {'status': 'complete', 'reason': None, 'http_status': None}
                    for name in requested}, 'failed_operations': {}, 'failed_http_statuses': {}}
    reason = getattr(error, 'reason_code', 'provider_error')
    failed = dict(getattr(error, 'failed_operations', {}))
    statuses = dict(getattr(error, 'failed_http_statuses', {}))
    # Only FRED's bounded batch adapter exposes its actual exhaustive outcome
    # list. The error's 16-entry diagnostic map is never a success complement.
    exhaustive = getattr(error, 'complete_operation_outcomes', None) if independent else None
    scoped = (isinstance(exhaustive, dict) and set(exhaustive) == set(requested)
        and set(failed) <= set(requested) and bool(failed)
        and all(exhaustive[name] == ('failed' if name in failed else 'complete') for name in requested))
    return {'requested': requested, 'status': 'failed', 'reason': reason,
        'http_status': getattr(error, 'http_status', None), 'error': error_text or str(error),
        'failed_operations': failed, 'failed_http_statuses': statuses,
        'outcomes': {name: {'status': ('failed' if name in failed else 'complete') if scoped else 'unavailable',
            'reason': failed.get(name) if scoped else reason,
            'http_status': statuses.get(name) if scoped else getattr(error, 'http_status', None)} for name in requested}}


def acquisition_manifest(provider, groups, start, end, started_at, finished_at):
    return {'version': 'source-capability-acquisition-v1', 'provider': provider,
        'observation_start': start, 'observation_end': end, 'vintage_date': end,
        'started_at': started_at, 'finished_at': finished_at, 'groups': groups}


def _capability_manifest(payload, provider):
    from datetime import date, datetime, timezone
    manifest = payload.get(ACQUISITION_KEY)
    if manifest is None:
        return None
    try:
        expected = {'version', 'provider', 'observation_start', 'observation_end', 'vintage_date',
                    'started_at', 'finished_at', 'groups'}
        if (not isinstance(manifest, dict) or set(manifest) != expected
                or manifest['version'] != 'source-capability-acquisition-v1' or manifest['provider'] != provider):
            raise ValueError()
        start, end, vintage = [date.fromisoformat(manifest[key]) for key in
            ('observation_start', 'observation_end', 'vintage_date')]
        if (any(parsed.isoformat() != manifest[key] for parsed, key in zip((start, end, vintage),
                ('observation_start', 'observation_end', 'vintage_date'))) or start > end or end != vintage):
            raise ValueError()
        clocks = [datetime.fromisoformat(manifest[key]) for key in ('started_at', 'finished_at')]
        if any(clock.tzinfo is None or clock.utcoffset() != timezone.utc.utcoffset(clock) for clock in clocks) or clocks[0] > clocks[1]:
            raise ValueError()
        groups = manifest['groups']
        requested = FRED_GROUPS if provider == 'fred' else {'research_prices': YAHOO_TICKERS, 'vix': ['^VIX']}
        if not isinstance(groups, dict) or list(groups) != list(requested):
            raise ValueError()
        errors = []
        for name, operations in requested.items():
            group = groups[name]
            if (not isinstance(group, dict) or set(group) != {'requested', 'status', 'reason', 'http_status', 'error',
                    'outcomes', 'failed_operations', 'failed_http_statuses'} or group['requested'] != operations
                    or group['status'] not in ('complete', 'failed')):
                raise ValueError()
            failures, statuses = group['failed_operations'], group['failed_http_statuses']
            if (not isinstance(failures, dict) or not isinstance(statuses, dict)
                    or any(type(k) is not str or v not in _REASONS for k, v in failures.items())
                    # The diagnostic sanitizer's 'unknown' sentinel proves no
                    # requested identity either; reject it along with all other
                    # undeclared names, even when this whole group is unavailable.
                    or not set(failures) <= set(operations)
                    or not set(statuses) <= set(failures)
                    or any(type(v) is not int or not 100 <= v <= 599 for v in statuses.values())):
                raise ValueError()
            if group['http_status'] is not None and (type(group['http_status']) is not int or not 100 <= group['http_status'] <= 599):
                raise ValueError()
            if group['status'] == 'complete':
                if any(group[k] is not None for k in ('reason', 'http_status', 'error')) or failures or statuses:
                    raise ValueError()
            else:
                if group['reason'] not in _REASONS or type(group['error']) is not str or not group['error']:
                    raise ValueError()
                errors.append(group['error'])
            outcomes = group['outcomes']
            if not isinstance(outcomes, dict) or list(outcomes) != operations:
                raise ValueError()
            scoped = any(row.get('status') != 'unavailable' for row in outcomes.values() if isinstance(row, dict))
            if group['status'] == 'failed' and scoped and (provider != 'fred' or not failures or not set(failures) <= set(operations)):
                raise ValueError()
            for operation, row in outcomes.items():
                if (not isinstance(row, dict) or set(row) != {'status', 'reason', 'http_status'}
                        or row['status'] not in ('complete', 'failed', 'unavailable')):
                    raise ValueError()
                if group['status'] == 'complete' or scoped:
                    expected_row = {'status': 'failed' if operation in failures else 'complete',
                        'reason': failures.get(operation), 'http_status': statuses.get(operation)}
                else:
                    expected_row = {'status': 'unavailable', 'reason': group['reason'], 'http_status': group['http_status']}
                if row != expected_row:
                    raise ValueError()
        if payload.get('error') not in ((None, '') if not errors else ('; '.join(errors),)):
            raise ValueError()
        return manifest
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ValueError('invalid_source_capability_acquisition') from error


def _valid_fred_series(value, manifest, series_id):
    """Validate accepted observations without deleting or replacing their tail."""
    import pandas as pd
    from datetime import date, datetime, timezone
    from tradingagents.strategies.data_sources.fetch_errors import source_number
    if not isinstance(value, pd.Series) or value.empty:
        return False
    if (not isinstance(value.index, pd.DatetimeIndex) or not value.index.is_unique
            or not value.index.is_monotonic_increasing or value.index.hasnans):
        raise ValueError('invalid_source_capability_observations')
    if (not source_number(value.iloc[-1]) or any(not pd.isna(item) and not source_number(item) for item in value)):
        return False
    attrs = value.attrs
    try:
        if any(attrs[key] != manifest[key] for key in ('observation_start', 'observation_end', 'vintage_date')):
            raise ValueError()
        acquired, available = [datetime.fromisoformat(attrs[key]) for key in ('acquired_at', 'available_at')]
        if (acquired.tzinfo is None or available != acquired
                or attrs['available_at'] != attrs['acquired_at']
                or acquired.utcoffset() != timezone.utc.utcoffset(acquired)
                or acquired > datetime.fromisoformat(manifest['finished_at'])
                or attrs['availability_basis'] != 'forward_acquisition_time'):
            raise ValueError()
        records = attrs['observation_records']
        if not isinstance(records, list) or len(records) != len(value):
            raise ValueError()
        monthly = {'UNRATE', 'CPIAUCSL', 'PAYEMS', 'FEDFUNDS'}
        lower = date.fromisoformat(manifest['observation_start'])
        if series_id in monthly:
            lower = lower.replace(day=1)
        elif series_id not in {'BAMLH0A0HYM2', 'BAMLC0A4CBBB', 'T10Y2Y', 'VIXCLS'}:
            lower = None
        for (when, number), record in zip(value.items(), records):
            if (set(record) != {'observation_date', 'value', 'realtime_start', 'realtime_end'}
                    or when.tzinfo is not None or when != when.normalize()
                    or record['observation_date'] != when.date().isoformat()
                    or when.date() > date.fromisoformat(manifest['observation_end'])
                    or (lower is not None and when.date() < lower)
                    or not date.fromisoformat(record['realtime_start']) <= date.fromisoformat(manifest['vintage_date']) <= date.fromisoformat(record['realtime_end'])
                    or (record['value'] is not None and not source_number(record['value']))
                    or (not pd.isna(number) and number != record['value'])
                    or (pd.isna(number) and record['value'] is not None)):
                raise ValueError()
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('invalid_source_capability_observations') from error
    return True


def _valid_yahoo_frame(value, manifest):
    import pandas as pd
    from tradingagents.strategies.data_sources.fetch_errors import source_number
    if not isinstance(value, pd.DataFrame) or value.empty or 'Close' not in value:
        return False
    if (not isinstance(value.index, pd.DatetimeIndex) or not value.index.is_unique
            or not value.index.is_monotonic_increasing or value.index.hasnans
            or not value.columns.is_unique):
        raise ValueError('invalid_source_capability_prices')
    if any(not manifest['observation_start'] <= when.date().isoformat() < manifest['observation_end'] for when in value.index):
        raise ValueError('invalid_source_capability_prices')
    close = value['Close']
    return (source_number(close.iloc[-1], minimum=0) and close.iloc[-1] > 0
        and all(pd.isna(number) or (source_number(number, minimum=0) and number > 0) for number in close))


def _project_capabilities(payload, provider, *, codec_limits=None):
    from tradingagents.strategies.data_sources.fred_source import SERIES_MAP
    manifest = _capability_manifest(payload, provider)
    selected, capabilities = {}, {}
    def outcome(name, usable, evidence, reason='unavailable_capability'):
        capabilities[provider + '.' + name] = {'status': 'usable' if usable else 'excluded',
            'reason': None if usable else reason, 'sha256': digest(evidence, codec_limits=codec_limits)}
    if manifest is None:
        return {}, {}, False
    if provider == 'fred':
        accepted = set()
        for group in manifest['groups'].values():
            for sid, row in group['outcomes'].items():
                value = payload.get(sid)
                # Alias conflicts are integrity failures even for rejected series.
                for alias, canonical in SERIES_MAP.items():
                    if canonical == sid and alias in payload and digest(payload[alias], codec_limits=codec_limits) != digest(value, codec_limits=codec_limits):
                        raise ValueError('invalid_source_capability_alias')
                if row['status'] == 'complete' and _valid_fred_series(value, manifest, sid):
                    accepted.add(sid)
        for key, value in payload.items():
            if key in accepted or SERIES_MAP.get(key) in accepted:
                selected[key] = value
        compartments = {'hy_spread': ['BAMLH0A0HYM2'], 'yield_curve': ['T10Y2Y'], 'vix': ['VIXCLS'],
            'commodity_macro': ['CPIAUCSL', 'FEDFUNDS', 'VIXCLS'],
            'optional_macro': ['BAMLC0A4CBBB', 'UNRATE', 'PAYEMS', 'ICSA']}
        for name, ids in compartments.items():
            outcome(name, set(ids) <= accepted, {sid: payload.get(sid) for sid in ids})
        # Existing state validator is authoritative for the whole compartment;
        # registry/security/clock checks still run in engine state_policy_scope.
        state_keys = ('state_unemployment', 'state_exposure_registry', 'state_policy_settings', 'coverage', 'state_evidence_error')
        state_ok = False
        if payload.get('state_unemployment') is None and any(key in payload for key in state_keys[1:]):
            raise ValueError('invalid_source_capability_state_coverage')
        if payload.get('state_unemployment') is not None:
            from tradingagents.strategies.data_sources.state_economics_policy import validate_snapshot
            coverage = validate_snapshot(payload['state_unemployment'])
            if payload.get('coverage') != {'state_unemployment': coverage}:
                raise ValueError('invalid_source_capability_state_coverage')
            state_ok = coverage['complete'] and not payload.get('state_evidence_error')
            if state_ok:
                selected.update({key: payload[key] for key in state_keys if key in payload})
        outcome('state_unemployment', state_ok, {key: payload.get(key) for key in state_keys},
            reason='state_capability_not_enabled' if payload.get('state_unemployment') is None else 'unavailable_capability')
        complete = len(accepted) == 9 and (payload.get('state_unemployment') is None or state_ok) and not payload.get('error')
        outcome('complete', complete, payload)
        if selected:
            if payload.get('vintage') != {'as_of': manifest['vintage_date'], 'realtime_start': manifest['vintage_date'], 'realtime_end': manifest['vintage_date']}:
                raise ValueError('invalid_source_capability_vintage')
            selected['vintage'] = payload['vintage']
    else:
        groups = manifest['groups']
        prices = payload.get('prices', {})
        prices_ok = (groups['research_prices']['status'] == 'complete' and isinstance(prices, dict)
            and set(prices) == set(YAHOO_TICKERS) and all(_valid_yahoo_frame(frame, manifest) for frame in prices.values()))
        vix_ok = groups['vix']['status'] == 'complete' and _valid_yahoo_frame(payload.get('vix'), manifest)
        if prices_ok:
            selected['prices'] = prices
        if vix_ok:
            selected['vix'] = payload['vix']
        outcome('research_prices', prices_ok, prices)
        outcome('vix', vix_ok, payload.get('vix'))
        complete = prices_ok and vix_ok and not payload.get('error')
    return selected, capabilities, complete


def strategy_capability_holds(scope, strategy_name, data_sources):
    """Unknown FRED consumers retain the complete-provider requirement."""
    if scope.get('capability_policy') != CAPABILITY_POLICY:
        return []
    required = []
    if 'fred' in data_sources:
        required.append('fred.' + {'commodity_macro': 'commodity_macro', 'state_economics': 'state_unemployment'}.get(strategy_name, 'complete'))
    if 'yfinance' in data_sources:
        required.append('yfinance.research_prices')
    return [name for name in required if scope.get('capabilities', {}).get(name, {}).get('status') != 'usable']


def project_sources(data: dict, sources, *, codec_limits=None, capability_policy=None) -> tuple[dict, dict]:
    """Remove failed provider payloads; retain their exact frozen identity.

    This establishes only provider-level selection. Complete-source adapters and
    source-specific validators still determine whether a record can be analyzed.
    The filing adapter may separately prove a narrower independent projection.
    """
    from .source_inputs import successful_source
    if capability_policy is not None and (type(capability_policy) is not str or capability_policy != CAPABILITY_POLICY):
        raise ValueError('invalid_source_capability_policy')
    sources = tuple(sources)
    if not isinstance(data, dict) or any(type(name) is not str or not name for name in sources):
        raise ValueError('invalid_research_source_scope')
    names = sorted(set(sources))
    projected, outcomes, capabilities = {}, {}, {}
    capability_complete = True
    for name in names:
        present = name in data
        payload = data.get(name)
        reason = None
        if not present:
            reason = 'missing_source'
        elif not isinstance(payload, Mapping):
            reason = 'invalid_source_payload'
        elif payload.get('error') not in (None, ''):
            reason = 'recorded_source_failure'
        elif not successful_source(payload):
            reason = 'incomplete_source_coverage'
        if capability_policy and name in ('fred', 'yfinance') and isinstance(payload, Mapping):
            selected, provider_capabilities, complete = _project_capabilities(payload, name, codec_limits=codec_limits)
            capabilities.update(provider_capabilities)
            capability_complete = capability_complete and complete
            if selected:
                projected[name] = selected
                reason = None
            else:
                reason = 'unavailable_capability'
        elif reason is None:
            projected[name] = payload
        outcomes[name] = {'status': 'usable' if reason is None else 'excluded',
                          'reason': reason, 'present': present,
                          'sha256': digest(payload, codec_limits=codec_limits) if present else None}
    # The source codec preserves mapping order. Keep the frozen original order
    # for every projection, including one reconstructed at staging.
    projected = {name: projected[name] for name in data if name in projected}
    receipt = {
        'policy': POLICY, 'version': 'research-source-selection-v1',
        'source_bundle_sha256': digest(data, codec_limits=codec_limits),
        'sources': outcomes, 'usable_sources': sorted(projected),
        'excluded_sources': sorted(set(names) - set(projected)),
        'research_coverage_complete': len(projected) == len(names),
        'order_authority': False,
    }
    if capability_policy:
        receipt.update(version='research-source-selection-v2', capability_policy=capability_policy,
            capabilities=capabilities, projection_sha256=digest(projected, codec_limits=codec_limits),
            research_coverage_complete=receipt['research_coverage_complete'] and capability_complete)
        receipt['strategy_holds'] = {
            'commodity_macro': strategy_capability_holds(receipt, 'commodity_macro', ['fred']),
            'state_economics': strategy_capability_holds(receipt, 'state_economics', ['fred']),
            'fred.other': strategy_capability_holds(receipt, 'other', ['fred']),
            'yfinance.consumers': strategy_capability_holds(receipt, 'other', ['yfinance'])}
    return projected, receipt


def signal_document(signal: dict) -> dict:
    """Exact decision content, excluding only this receipt and late lineage tag."""
    return {**signal, 'metadata': {key: value for key, value in signal.get('metadata', {}).items()
        if key not in ('research_admission', 'retained_from_signal_id')}}


def project_enrichment(original: dict, *, codec_limits=None, capability_config=None) -> tuple[dict, dict]:
    """Select healthy optional facts, retaining every original failure by hash."""
    from .source_inputs import successful_source
    operations = ('profiles', 'short_interest', 'factors', 'commodity_futures_curves')
    if not isinstance(original, dict) or not isinstance(original.get('errors', {}), dict):
        raise ValueError('invalid_research_enrichment')
    from .enrichment_capabilities import declared_scope
    retirement = declared_scope(original, {} if capability_config is None else capability_config)
    errors = original.get('errors', {})
    holds = original.get('research_hold_reasons', [])
    if (not isinstance(holds, list) or any(reason not in
            ('model_deadline_exhausted', 'shared_regime_unavailable', 'required_universe_unavailable')
            for reason in holds)):
        raise ValueError('invalid_research_enrichment_hold')
    projected, excluded = {}, []
    whole_failure = bool(set(errors) - set(operations)) or bool(original.get('error'))
    if whole_failure:
        excluded.append({'operation': 'openbb', 'symbol': None, 'reason': 'recorded_source_failure'})
    for operation in operations:
        payload, failures = original.get(operation), errors.get(operation, {})
        if not isinstance(failures, dict) or (operation in original and not isinstance(payload, dict)):
            raise ValueError('invalid_research_enrichment')
        if whole_failure or holds:
            continue
        if operation == 'factors':
            if failures or (payload is not None and not successful_source(payload)):
                excluded.append({'operation': operation, 'symbol': None, 'reason': 'recorded_source_failure'})
            elif payload is not None:
                projected[operation] = payload
            continue
        selected = {}
        for symbol in sorted(set(payload or {}) | set(failures)):
            value = (payload or {}).get(symbol)
            if symbol in failures or not isinstance(value, dict) or not successful_source(value):
                excluded.append({'operation': operation, 'symbol': symbol, 'reason': 'recorded_source_failure'})
            else:
                selected[symbol] = value
        if payload is not None:
            projected[operation] = {symbol: selected[symbol] for symbol in payload if symbol in selected}
    receipt = {'version': 'research-enrichment-selection-v1', 'policy': POLICY,
        'original_sha256': digest(original, codec_limits=codec_limits),
        'projection_sha256': digest(projected, codec_limits=codec_limits),
        'excluded': excluded, 'hold_reasons': list(holds),
        'research_coverage_complete': not bool(excluded or holds), 'order_authority': False}
    if retirement is not None:
        receipt.update(version='research-enrichment-selection-v2', declared_exclusions=retirement,
            supported_enrichment_complete=not bool(excluded or holds), research_coverage_complete=False)
    return projected, receipt


def bind_signal(signal, regime, strategy_sources, analysis_input_sha256, *, codec_limits=None):
    proof = {
        'version': 'research-candidate-admission-v1', 'policy': POLICY,
        'strategy_sources': sorted(set(strategy_sources)),
        'source_scope_sha256': digest(regime['research_source_scope'], codec_limits=codec_limits),
        'filing_isolation_sha256': digest(regime.get('filing_research_isolation'), codec_limits=codec_limits),
        'projection_sha256': regime['research_projection_sha256'],
        'shared_context_sha256': digest(regime, codec_limits=codec_limits),
        'analysis_input_sha256': analysis_input_sha256,
        'signal_sha256': digest(signal_document(signal), codec_limits=codec_limits),
        'entry_eligible': not bool(signal.get('journal_only') or
            signal.get('metadata', {}).get('non_actionable_reason') or regime['research_hold_reasons']),
    }
    signal.setdefault('metadata', {})['research_admission'] = proof
    return proof


def validate_signal(signal, regime, strategy_sources, *, codec_limits=None):
    """Check a screening result before it can contribute to a new entry.

    The frozen all-horizon screening checkpoint is the authority for this
    receipt. This validation also binds source selection and current context;
    a caller-authored hash alone is not evidence of a successful analysis.
    """
    import re
    try:
        proof = signal['metadata']['research_admission']
        expected = {'version', 'policy', 'strategy_sources', 'source_scope_sha256',
            'filing_isolation_sha256', 'projection_sha256', 'shared_context_sha256',
            'analysis_input_sha256', 'signal_sha256', 'entry_eligible'}
        if not isinstance(proof, dict) or set(proof) != expected:
            raise ValueError()
        if (proof['version'] != 'research-candidate-admission-v1' or proof['policy'] != POLICY
                or proof['strategy_sources'] != sorted(set(strategy_sources))
                or proof['source_scope_sha256'] != digest(regime['research_source_scope'], codec_limits=codec_limits)
                or proof['filing_isolation_sha256'] != digest(regime.get('filing_research_isolation'), codec_limits=codec_limits)
                or proof['projection_sha256'] != regime['research_projection_sha256']
                or proof['shared_context_sha256'] != digest(regime, codec_limits=codec_limits)
                or proof['signal_sha256'] != digest(signal_document(signal), codec_limits=codec_limits)
                or re.fullmatch('[0-9a-f]{64}', proof['analysis_input_sha256']) is None
                or proof['entry_eligible'] is not True):
            raise ValueError()
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('invalid_research_candidate_admission') from error
    return proof


def validate_projection(engine, data, regime):
    """Reconstruct the source selection before committee or exit evaluation."""
    from .source_inputs import source_codec_limits
    from .congress_policy import configured as congress_configured, DECLARATION_KEY
    from tradingagents.strategies.data_sources.filing_material_policy import configured as material_configured
    original = {key: value for key, value in data.items()
                if key not in ('_execution_reference_bars', '_decision_context')}
    names = set(original) | {'fred', 'yfinance'}
    if engine.ar_config.get('equity_universe_policy'):
        names.add('equity_universe')
    names.update(source for strategy in engine.paper_trade_strategies for source in strategy.data_sources)
    limits = source_codec_limits(engine.config)
    projected, scope = project_sources(original, names, codec_limits=limits, capability_policy=engine.ar_config.get('source_capability_policy'))
    if congress_configured(engine.ar_config):
        projected = {key: value for key, value in projected.items() if key not in ('congress', DECLARATION_KEY)}
    from .source_lifecycle_policy import declared_scope, DECLARATION_KEY as LIFECYCLE_KEY
    if declared_scope(original, engine.ar_config, engine.paper_trade_strategies) is not None:
        projected.pop(LIFECYCLE_KEY, None)
    from .filing_research_policy import declared_scope as bulk_scope, DECLARATION_KEY as FILING_KEY
    if bulk_scope(original, config=engine.ar_config) is not None:
        projected.pop(FILING_KEY, None)
    filing_scope = None
    if material_configured(engine.ar_config):
        from .filing_research_isolation import isolate_filing_research
        from .filing_material_validation import validate_filing_material_policy
        from .filing_acquisition_validation import validate_filing_acquisition_policy
        edgar, filing_scope = isolate_filing_research(original.get('edgar', {}), engine.ar_config,
            strict_material_scope=validate_filing_material_policy(original, engine.ar_config),
            acquisition_scope=validate_filing_acquisition_policy(original, engine.ar_config))
        if filing_scope['discovery_scope']['outcome'] == 'complete':
            projected['edgar'] = edgar
        else:
            projected.pop('edgar', None)
    if (digest(scope, codec_limits=limits) != digest(regime.get('research_source_scope'), codec_limits=limits)
            or digest(filing_scope, codec_limits=limits) != digest(regime.get('filing_research_isolation'), codec_limits=limits)
            or digest(projected, codec_limits=limits) != regime.get('research_projection_sha256')):
        raise ValueError('invalid_research_source_projection')
    macro = engine._build_regime_model(projected)
    if any(value != regime.get(key) for key, value in macro.items() if key != 'timestamp'):
        raise ValueError('invalid_research_regime_context')
    holds = ['shared_regime_unavailable'] if any(macro[key] is None for key in
        ('vix_level', 'credit_spread_bps', 'yield_curve_slope')) else []
    if engine.ar_config.get('equity_universe_policy'):
        from tradingagents.strategies.data_sources.equity_universe import EquityUniverse, POLICY as UNIVERSE_POLICY
        if engine.ar_config['equity_universe_policy'] != UNIVERSE_POLICY:
            raise ValueError('invalid_research_universe_policy')
        payload = projected.get('equity_universe', {})
        if payload.get('snapshot') is None:
            holds.append('required_universe_unavailable')
        else:
            EquityUniverse(payload['snapshot'], company_map=projected.get('edgar', {}).get('company_tickers'))
            if payload.get('error'):
                holds.append('required_universe_unavailable')
    if regime.get('research_hold_reasons') != holds:
        raise ValueError('invalid_research_hold')
    return projected


def validate_book_source_scope(status, regime, *, codec_limits=None):
    """Replayed research must match the original durable committee authority."""
    authority = status.get('research_authority', {}) if isinstance(status, dict) else {}
    expected = {'version': 'research-book-authority-v1', 'policy': POLICY,
        'source_bundle_sha256': regime['research_source_scope']['source_bundle_sha256'],
        'source_scope_sha256': digest(regime['research_source_scope'], codec_limits=codec_limits),
        'filing_isolation_sha256': digest(regime.get('filing_research_isolation'), codec_limits=codec_limits),
        'projection_sha256': regime['research_projection_sha256'],
        'shared_context_sha256': digest(regime, codec_limits=codec_limits)}
    if not isinstance(authority, dict) or any(authority.get(key) != value for key, value in expected.items()):
        raise ValueError('research_book_authority_conflict')
    return authority


def hold_signals(signals, reason, *, codec_limits=None):
    """A hold can reduce existing authority, including retained late theses."""
    for signal in signals:
        signal['journal_only'] = True
        metadata = signal.setdefault('metadata', {})
        metadata['non_actionable_reason'] = reason
        proof = metadata.get('research_admission')
        if isinstance(proof, dict):
            proof['entry_eligible'] = False
            proof['signal_sha256'] = digest(signal_document(signal), codec_limits=codec_limits)
    return signals


def validate_retained_signal(signal, ledger, session, epoch_id, policy_id):
    """Late evidence keeps the exact previously accepted ledger observation."""
    import json
    metadata = signal.get('metadata', {})
    origin = metadata.get('retained_from_signal_id')
    observation = ledger.signal_observation(origin) if isinstance(origin, str) else None
    if observation is None:
        raise ValueError('invalid_research_retained_candidate')
    record, context, journal = observation
    original = context.get('signal')
    value = {**signal, 'metadata': {key: item for key, item in metadata.items()
                                   if key != 'retained_from_signal_id'}}
    if (record.epoch_id != epoch_id or record.policy_id != policy_id
            or record.reference_session >= session or journal.get('status') != 'cutoff-late'
            or not isinstance(original, dict)
            or json.dumps(value, sort_keys=True, default=str) != json.dumps(original, sort_keys=True, default=str)
            or original.get('metadata', {}).get('research_admission', {}).get('entry_eligible') is not True):
        raise ValueError('invalid_research_retained_candidate')
    return metadata['research_admission']
