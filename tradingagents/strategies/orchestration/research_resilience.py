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


def project_sources(data: dict, sources, *, codec_limits=None) -> tuple[dict, dict]:
    """Remove failed provider payloads; retain their exact frozen identity.

    This establishes only provider-level selection. Complete-source adapters and
    source-specific validators still determine whether a record can be analyzed.
    The filing adapter may separately prove a narrower independent projection.
    """
    from .source_inputs import successful_source
    sources = tuple(sources)
    if not isinstance(data, dict) or any(type(name) is not str or not name for name in sources):
        raise ValueError('invalid_research_source_scope')
    names = sorted(set(sources))
    projected, outcomes = {}, {}
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
        if reason is None:
            projected[name] = payload
        outcomes[name] = {'status': 'usable' if reason is None else 'excluded',
                          'reason': reason, 'present': present,
                          'sha256': digest(payload, codec_limits=codec_limits) if present else None}
    # The source codec preserves mapping order. Keep the frozen original order
    # for every projection, including one reconstructed at staging.
    projected = {name: data[name] for name in data if name in projected}
    return projected, {
        'policy': POLICY, 'version': 'research-source-selection-v1',
        'source_bundle_sha256': digest(data, codec_limits=codec_limits),
        'sources': outcomes, 'usable_sources': sorted(projected),
        'excluded_sources': sorted(set(names) - set(projected)),
        'research_coverage_complete': len(projected) == len(names),
        'order_authority': False,
    }


def signal_document(signal: dict) -> dict:
    """Exact decision content, excluding only this receipt and late lineage tag."""
    return {**signal, 'metadata': {key: value for key, value in signal.get('metadata', {}).items()
        if key not in ('research_admission', 'retained_from_signal_id')}}


def project_enrichment(original: dict, *, codec_limits=None) -> tuple[dict, dict]:
    """Select healthy optional facts, retaining every original failure by hash."""
    from .source_inputs import successful_source
    operations = ('profiles', 'short_interest', 'factors', 'commodity_futures_curves')
    if not isinstance(original, dict) or not isinstance(original.get('errors', {}), dict):
        raise ValueError('invalid_research_enrichment')
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
    return projected, {'version': 'research-enrichment-selection-v1', 'policy': POLICY,
        'original_sha256': digest(original, codec_limits=codec_limits),
        'projection_sha256': digest(projected, codec_limits=codec_limits),
        'excluded': excluded, 'hold_reasons': list(holds),
        'research_coverage_complete': not bool(excluded or holds), 'order_authority': False}


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
    projected, scope = project_sources(original, names, codec_limits=limits)
    if congress_configured(engine.ar_config):
        projected = {key: value for key, value in projected.items() if key not in ('congress', DECLARATION_KEY)}
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
