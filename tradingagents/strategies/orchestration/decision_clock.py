"""Explicit prospective information clocks; historical session clocks stay fixed."""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import re

from .trading_calendar import (exchange_date, is_session, next_session,
                               previous_session, session_close, session_open)

PROSPECTIVE = 'prospective_next_open_v1'
HISTORICAL = 'session_close_v1'


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def policy(config: dict) -> str:
    value = config.get('autoresearch', {}).get('decision_clock_policy', HISTORICAL)
    if value not in {HISTORICAL, PROSPECTIVE}:
        raise ValueError('unsupported decision clock policy')
    return value


def _aware(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('decision clock requires an aware timestamp')
    return value.astimezone(timezone.utc)


def latest_completed_session(now: datetime) -> date:
    now = _aware(now)
    local = exchange_date(now)
    return local if is_session(local) and session_close(local) <= now else previous_session(local)


def require_reference(session: date, now: datetime) -> None:
    if session != latest_completed_session(now):
        raise ValueError('prospective reference is not the latest completed session')
    if _aware(now) >= session_open(next_session(session)):
        raise ValueError('prospective decision is after the next reference-session open')


def mutable_vintage(config: dict, session: date, *, now: datetime | None = None) -> str:
    if policy(config) == HISTORICAL:
        return session.isoformat()
    now = _aware(now or utc_now())
    require_reference(session, now)
    return exchange_date(now).isoformat()


def create_context(config: dict, session: date, *, acquired_at: datetime,
                   cutoff: datetime, source_digest: str, enrichment_digest: str,
                   eligibility_digest: str = hashlib.sha256(b'null').hexdigest(), document_review_binding=None) -> dict:
    context = {'policy': policy(config), 'reference_session': session.isoformat(),
               'acquisition_started_at': _aware(acquired_at).isoformat(),
               'cutoff': _aware(cutoff).isoformat(),
               'eligible_session': next_session(session).isoformat(),
               'source_digest': source_digest, 'enrichment_digest': enrichment_digest,
               'eligibility_digest': eligibility_digest}
    if document_review_binding is not None:
        context['document_review_binding'] = document_review_binding
    return validate_context(config, session, context)


def validate_context(config: dict, session: date, context: object) -> dict:
    if policy(config) != PROSPECTIVE or not isinstance(context, dict):
        raise ValueError('prospective decision context required')
    required = {'policy', 'reference_session', 'acquisition_started_at', 'cutoff',
                'eligible_session', 'source_digest', 'enrichment_digest', 'eligibility_digest'}
    from ..document_review_clock import POLICY as DOCUMENT_NEXT_OPEN_POLICY
    document_next_open = config.get('autoresearch', {}).get('bounded_document_decision_policy') == DOCUMENT_NEXT_OPEN_POLICY
    if document_next_open:
        required.add('document_review_binding')
    if set(context) != required or context['policy'] != PROSPECTIVE:
        raise ValueError('invalid decision clock schema or policy')
    if context['reference_session'] != session.isoformat():
        raise ValueError('decision reference mismatch')
    if context['eligible_session'] != next_session(session).isoformat():
        raise ValueError('decision eligible session mismatch')
    acquired, cutoff = _aware(context['acquisition_started_at']), _aware(context['cutoff'])
    require_reference(session, acquired)
    require_reference(session, cutoff)
    if acquired > cutoff:
        raise ValueError('decision cutoff precedes acquisition')
    for field in ('source_digest', 'enrichment_digest', 'eligibility_digest'):
        if not isinstance(context[field], str) or not re.fullmatch(r'[0-9a-f]{64}', context[field]):
            raise ValueError('invalid decision evidence digest')
    if document_next_open:
        from ..document_review_clock import validate_binding_context
        validate_binding_context(config, session, context)
    return dict(context)


def resolve_cutoff(config: dict, session: date, context: object) -> datetime:
    if policy(config) == HISTORICAL:
        if context is not None:
            raise ValueError('historical clock cannot accept a prospective context')
        return session_close(session)
    return _aware(validate_context(config, session, context)['cutoff'])


def require_live_staging(config: dict, session: date, context: dict, *, now: datetime | None = None) -> datetime:
    """A fresh publication must still precede its intended execution open."""
    cutoff = resolve_cutoff(config, session, context)
    current = _aware(now or utc_now())
    if current < cutoff:
        raise ValueError('live staging precedes information cutoff')
    if current >= session_open(next_session(session)):
        raise ValueError('live staging is after eligible session open')
    return current


def validate_completed_replay(owner, session: date, epoch: str, completed) -> dict | None:
    """Validate a completed prospective replay without acquisition or publication.

    Completed books can be replayed after their eligible open, but their original
    source, eligibility, enrichment, and clock bindings must still be intact.
    Protected obligations come from the persisted session, never today's holdings.
    """
    config = owner._base_config
    from .research_resilience import configured as research_configured
    research = research_configured(config.get('autoresearch', {}))
    if not completed:
        return None
    if policy(config) == HISTORICAL:
        if research:
            _validate_completed_research_inputs(owner, session, epoch, completed)
        return None
    from .source_inputs import SourceInputStore, daily_source_store
    from ..execution.session_activity import load_new_entry_eligibility

    shared, identity = daily_source_store(owner, session.isoformat())
    source_data = shared.load_frozen(identity)
    if not isinstance(source_data, dict):
        raise ValueError('completed replay lacks immutable shared source inputs')
    source_digest = hashlib.sha256(shared.encode(source_data, **shared.codec_limits).encode()).hexdigest()
    acquisition = source_data.get('_decision_acquisition')
    if (not isinstance(acquisition, dict) or acquisition.get('policy') != PROSPECTIVE
            or acquisition.get('reference_session') != session.isoformat()):
        raise ValueError('prospective source acquisition context missing or mismatched')
    acquired_at = _aware(acquisition.get('started_at'))
    if acquisition.get('vintage_as_of') != mutable_vintage(config, session, now=acquired_at):
        raise ValueError('prospective source vintage mismatch')

    binding = owner._metric_store.read_candidate_signal_identity_binding(epoch, session)
    if binding is None or binding.epoch_id != epoch or binding.session != session:
        raise ValueError('completed replay lacks candidate identity binding')
    protected = set()
    for cohort in owner.cohorts:
        executor = cohort['executor']
        executor.validate_bound_context(session, epoch)
        bundle = executor.persisted_input_bundle(session)
        protected.update(bundle.tickers)
        protected.update(executor.benchmark_symbols)
    candidate_scope = {item['ticker'] for item in binding.identities} - protected
    eligibility = load_new_entry_eligibility(owner, session, candidate_scope, protected,
        listing_snapshot=source_data.get('equity_universe', {}).get('snapshot'))
    eligibility_digest = hashlib.sha256(shared.encode(eligibility, **shared.codec_limits).encode()).hexdigest()

    decision_store = SourceInputStore(shared.cache_dir, accepted_dir=shared.accepted_dir / 'decision_inputs',
                                     ttl_s=shared.ttl_s, **shared.codec_limits)
    accepted = decision_store.load_frozen({**identity, 'purpose': 'prospective-decision-inputs-v1'})
    expected_fields = {'context', 'enrichment', 'research_binding'} if research else {'context', 'enrichment'}
    if not isinstance(accepted, dict) or set(accepted) != expected_fields:
        raise ValueError('completed replay lacks immutable decision inputs')
    if research:
        _validate_research_inputs(owner, session, decision_store, accepted, source_data,
                                  source_digest, eligibility_digest, completed=completed)
    context = validate_context(config, session, accepted['context'])
    if context['source_digest'] != source_digest or _aware(context['acquisition_started_at']) != acquired_at:
        raise ValueError('decision source binding mismatch')
    from .scoped_replay import validate_replay_source_scopes
    validate_replay_source_scopes(source_data, owner, session.isoformat(), epoch,
                                  now=_aware(context['cutoff']))
    if context['eligibility_digest'] != eligibility_digest:
        raise ValueError('decision eligibility binding mismatch')
    if not isinstance(accepted['enrichment'], dict):
        raise ValueError('decision enrichment must be a mapping')
    enrichment_digest = hashlib.sha256(shared.encode(accepted['enrichment'], **shared.codec_limits).encode()).hexdigest()
    if context['enrichment_digest'] != enrichment_digest:
        raise ValueError('decision enrichment binding mismatch')
    for cohort in completed:
        persisted = cohort['ledger'].read_policy_session_context(session)
        if (persisted is None or persisted['epoch_id'] != epoch
                or persisted['context'].get('decision_clock') != context):
            raise ValueError('completed staging decision clock binding mismatch')
    return context


_RESEARCH_HOLDS = frozenset(('shared_regime_unavailable', 'required_universe_unavailable',
                            'model_deadline_exhausted'))


def _decision_digest(store, value):
    return hashlib.sha256(store.encode(value, **store.codec_limits).encode()).hexdigest()


def _research_holds(value):
    if (type(value) is not tuple or any(type(reason) is not str or reason not in _RESEARCH_HOLDS for reason in value)
            or len(value) != len(set(value))):
        raise ValueError('invalid research decision hold')
    return tuple(sorted(value))


def _research_store(owner, session):
    from .source_inputs import SourceInputStore, daily_source_store
    if (type(session) is not date or not is_session(session)
            or type(owner._epoch_id) is not str or not owner._epoch_id):
        raise ValueError('invalid research decision session or epoch')
    shared, identity = daily_source_store(owner, session.isoformat())
    purpose = ('research-historical-decision-inputs-v1' if policy(owner._base_config) == HISTORICAL
               else 'prospective-decision-inputs-v1')
    return SourceInputStore(shared.cache_dir, accepted_dir=shared.accepted_dir / 'decision_inputs',
                           ttl_s=shared.ttl_s, **shared.codec_limits), {**identity, 'purpose': purpose}


def _require_research_source(store, source_data, source_digest):
    if not isinstance(source_data, dict):
        raise ValueError('invalid research decision source')
    original = {key: value for key, value in source_data.items()
                if key not in ('_execution_reference_bars', '_decision_context')}
    if (type(source_digest) is not str or re.fullmatch('[0-9a-f]{64}', source_digest) is None
            or _decision_digest(store, original) != source_digest):
        raise ValueError('research decision source binding mismatch')


def _require_no_decision_effects(owner, session):
    for cohort in getattr(owner, 'cohorts', []):
        ledger = cohort['ledger']
        horizon = cohort['config'].horizon
        policy_id = str(cohort['engine'].ar_config.get('paper_ledger', {}).get('policy_id', 'foundation-' + horizon))
        if (ledger.read_policy_session_context(session) is not None
                or ledger.committee_decision(session, owner._epoch_id, policy_id) is not None
                or ledger.staging_completed(session, owner._epoch_id, policy_id)):
            raise ValueError('persisted research effect lacks immutable decision inputs')


def _verify_first_research_hold(owner, session, source_digest, reasons, deadline):
    """A frozen whole-sample hold or the actual exhausted clock permits local IO."""
    import math
    import time
    from .research_screening import load_screening
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError('invalid research decision deadline')
    horizons = tuple(sorted({cohort['config'].horizon for cohort in owner.cohorts}))
    accepted = load_screening(owner, session, source_digest, horizons)
    if accepted is None:
        raise ValueError('research decision hold lacks immutable screening')
    sample, coverage, _ = accepted
    if 'model_deadline_exhausted' in reasons:
        expired = type(deadline) in (int, float) and math.isfinite(deadline) and time.monotonic() >= deadline
        incomplete = coverage == {'complete': False, 'reason': 'model_deadline_exhausted'}
        if not (expired or incomplete):
            raise ValueError('research decision model deadline hold is unproven')
    contextual = set(reasons) - {'model_deadline_exhausted'}
    if contextual:
        held = [regime.get('research_hold_reasons') for _, regime, _ in sample.values()]
        if (not held or any(type(value) is not list or not value or any(type(reason) is not str or
                reason not in _RESEARCH_HOLDS - {'model_deadline_exhausted'} for reason in value) for value in held)
                or not contextual <= {reason for value in held for reason in value}):
            raise ValueError('research decision shared hold is unproven')


def _validate_research_inputs(owner, session, store, accepted, source_data, source_digest, eligibility_digest,
                              *, completed=()):
    from .research_resilience import POLICY
    if not isinstance(accepted, dict) or set(accepted) != {'context', 'enrichment', 'research_binding'}:
        raise ValueError('invalid accepted research decision inputs')
    binding, enrichment = accepted['research_binding'], accepted['enrichment']
    fields = {'version', 'policy', 'clock_policy', 'epoch_id', 'source_digest', 'eligibility_digest',
              'enrichment_digest', 'hold_reasons'}
    if not isinstance(binding, dict) or set(binding) != fields or not isinstance(enrichment, dict):
        raise ValueError('invalid research decision input schema')
    if type(binding['hold_reasons']) is not list:
        raise ValueError('invalid research decision hold')
    reasons = _research_holds(tuple(binding['hold_reasons']))
    expected = {'version': 'research-decision-inputs-v1', 'policy': POLICY,
        'clock_policy': policy(owner._base_config), 'epoch_id': owner._epoch_id,
        'source_digest': source_digest, 'eligibility_digest': eligibility_digest,
        'enrichment_digest': _decision_digest(store, enrichment), 'hold_reasons': list(reasons)}
    if binding != expected:
        raise ValueError('research decision binding mismatch')
    if ((reasons and enrichment.get('research_hold_reasons') != list(reasons))
            or (not reasons and 'research_hold_reasons' in enrichment)):
        raise ValueError('research decision hold enrichment mismatch')
    from .research_resilience import project_enrichment
    _, enrichment_scope = project_enrichment(enrichment, codec_limits=store.codec_limits)
    completed_names = {cohort['config'].name for cohort in completed}
    for cohort in getattr(owner, 'cohorts', []):
        ledger = cohort['ledger']
        policy_id = str(cohort['engine'].ar_config.get('paper_ledger', {}).get('policy_id',
                        'foundation-' + cohort['config'].horizon))
        decision = ledger.committee_decision(session, owner._epoch_id, policy_id)
        required = (cohort['config'].name in completed_names
                    or ledger.staging_completed(session, owner._epoch_id, policy_id))
        if decision is None and not required:
            continue
        if (not isinstance(decision, dict) or not isinstance(decision.get('status'), dict)
                or decision['status'].get('research_enrichment_scope') != enrichment_scope):
            raise ValueError('research decision enrichment differs from durable committee scope')
    if policy(owner._base_config) == HISTORICAL:
        if accepted['context'] is not None:
            raise ValueError('historical research decision has a prospective context')
        return enrichment, None
    context = validate_context(owner._base_config, session, accepted['context'])
    acquired_at = _research_acquisition(owner._base_config, session, source_data)
    if (context['source_digest'] != source_digest or context['eligibility_digest'] != eligibility_digest
            or context['enrichment_digest'] != expected['enrichment_digest']
            or _aware(context['acquisition_started_at']) != acquired_at):
        raise ValueError('research decision clock binding mismatch')
    from ..document_review_clock import POLICY as DOCUMENT_NEXT_OPEN_POLICY
    if owner._base_config.get('autoresearch', {}).get('bounded_document_decision_policy') == DOCUMENT_NEXT_OPEN_POLICY:
        expected_document_binding = _document_final_binding(owner, session, source_data, source_digest, _aware(context['cutoff']))
        if context['document_review_binding'] != expected_document_binding:
            raise ValueError('invalid_document_decision_final_binding')
    return enrichment, context


def _document_final_binding(owner, session, source_data, source_digest, cutoff):
    from ..document_review_clock import build_final_binding
    from .research_screening import load_screening
    horizons = tuple(sorted({cohort['config'].horizon for cohort in owner.cohorts}))
    accepted = load_screening(owner, session, source_digest, horizons)
    if accepted is None:
        raise ValueError('invalid_document_decision_screening')
    screened, _, receipt = accepted
    return build_final_binding(owner._base_config, session, source_data, screened, source_digest, cutoff,
        receipt['screening_sha256'])


def _research_acquisition(config, session, source_data):
    acquisition = source_data.get('_decision_acquisition')
    if (not isinstance(acquisition, dict) or acquisition.get('policy') != PROSPECTIVE
            or acquisition.get('reference_session') != session.isoformat()):
        raise ValueError('prospective source acquisition context missing or mismatched')
    acquired_at = _aware(acquisition.get('started_at'))
    if acquisition.get('vintage_as_of') != mutable_vintage(config, session, now=acquired_at):
        raise ValueError('prospective source vintage mismatch')
    return acquired_at


def _prepare_research_inputs(owner, session, source_data, source_digest, fetch_enrichment,
                             eligibility, deadline, now, hold_reasons):
    from .source_inputs import _check_deadline
    from .research_resilience import POLICY
    reasons = _research_holds(hold_reasons)
    store, identity = _research_store(owner, session)
    _require_research_source(store, source_data, source_digest)
    acquired_at = (_research_acquisition(owner._base_config, session, source_data)
                   if policy(owner._base_config) == PROSPECTIVE else None)
    eligibility_digest = _decision_digest(store, eligibility)
    accepted = store.load_frozen(identity)
    if accepted is None:
        _require_no_decision_effects(owner, session)
        if reasons:
            _verify_first_research_hold(owner, session, source_digest, reasons, deadline)
            enrichment = {'research_hold_reasons': list(reasons)}
        else:
            _check_deadline(deadline)
            enrichment = fetch_enrichment()
        if not isinstance(enrichment, dict) or (not reasons and 'research_hold_reasons' in enrichment):
            raise ValueError('invalid research decision enrichment')
        if not reasons and deadline is not None:
            import time
            if time.monotonic() >= deadline:
                reasons = ('model_deadline_exhausted',)
                _verify_first_research_hold(owner, session, source_digest, reasons, deadline)
                enrichment = {**enrichment, 'research_hold_reasons': list(reasons)}
        enrichment_digest = _decision_digest(store, enrichment)
        context = None
        if policy(owner._base_config) == PROSPECTIVE:
            cutoff = _aware((now or utc_now)())
            document_binding = None
            from ..document_review_clock import POLICY as DOCUMENT_NEXT_OPEN_POLICY
            if owner._base_config.get('autoresearch', {}).get('bounded_document_decision_policy') == DOCUMENT_NEXT_OPEN_POLICY:
                document_binding = _document_final_binding(owner, session, source_data, source_digest, cutoff)
            context = create_context(owner._base_config, session, acquired_at=acquired_at,
                cutoff=cutoff, source_digest=source_digest,
                enrichment_digest=enrichment_digest, eligibility_digest=eligibility_digest,
                document_review_binding=document_binding)
        binding = {'version': 'research-decision-inputs-v1', 'policy': POLICY,
            'clock_policy': policy(owner._base_config), 'epoch_id': owner._epoch_id,
            'source_digest': source_digest, 'eligibility_digest': eligibility_digest,
            'enrichment_digest': enrichment_digest, 'hold_reasons': list(reasons)}
        # An established hold cannot acquire anything. Only bounded local
        # encoding and atomic publication outlive the exhausted acquisition clock.
        accepted = store.freeze(identity, {'context': context, 'enrichment': enrichment,
            'research_binding': binding}, deadline=None if reasons else deadline)
    return _validate_research_inputs(owner, session, store, accepted, source_data,
                                     source_digest, eligibility_digest)


def _validate_completed_research_inputs(owner, session, epoch, completed):
    from .source_inputs import daily_source_store
    from ..execution.session_activity import POLICY, load_new_entry_eligibility
    shared, identity = daily_source_store(owner, session.isoformat())
    data = shared.load_frozen(identity)
    if not isinstance(data, dict) or owner._epoch_id != epoch:
        raise ValueError('completed research replay lacks immutable source inputs')
    source_digest = _decision_digest(shared, data)
    eligibility = None
    if owner._base_config.get('autoresearch', {}).get('new_entry_eligibility_policy') == POLICY:
        binding = owner._metric_store.read_candidate_signal_identity_binding(epoch, session)
        if binding is None or binding.epoch_id != epoch or binding.session != session:
            raise ValueError('completed research replay lacks candidate identity binding')
        protected = set()
        for cohort in owner.cohorts:
            executor = cohort['executor']
            executor.validate_bound_context(session, epoch)
            protected.update(executor.persisted_input_bundle(session).tickers)
            protected.update(executor.benchmark_symbols)
        eligibility = load_new_entry_eligibility(owner, session,
            {item['ticker'] for item in binding.identities} - protected, protected,
            listing_snapshot=data.get('equity_universe', {}).get('snapshot'))
    store, decision_identity = _research_store(owner, session)
    accepted = store.load_frozen(decision_identity)
    _validate_research_inputs(owner, session, store, accepted, data, source_digest,
                              _decision_digest(store, eligibility), completed=completed)


def prepare_decision_inputs(owner, session: date, source_data: dict, source_digest: str,
                            fetch_enrichment, *, eligibility=None,
                            deadline: float | None = None, now=None,
                            hold_reasons: tuple[str, ...] = ()) -> tuple[dict, dict | None]:
    """Freeze the shared final information cutoff once, before any committee.

    Models may already have analyzed the earlier source bundle. Enrichment is
    acquired later; its actual completion therefore precedes the final cutoff.
    Replay consumes this immutable enrichment and clock without new acquisition.
    """
    config = owner._base_config
    from .research_resilience import configured as research_configured
    if research_configured(config.get('autoresearch', {})):
        return _prepare_research_inputs(owner, session, source_data, source_digest,
            fetch_enrichment, eligibility, deadline, now, hold_reasons)
    if hold_reasons:
        raise ValueError('research decision hold requires opt-in policy')
    if policy(config) == HISTORICAL:
        return fetch_enrichment(), None
    from .source_inputs import SourceInputStore, daily_source_store
    shared, identity = daily_source_store(owner, session.isoformat())
    identity = {**identity, 'purpose': 'prospective-decision-inputs-v1'}
    store = SourceInputStore(shared.cache_dir, accepted_dir=shared.accepted_dir / 'decision_inputs',
                             ttl_s=shared.ttl_s, **shared.codec_limits)
    eligibility_digest = hashlib.sha256(store.encode(eligibility, **shared.codec_limits).encode()).hexdigest()
    acquisition = source_data.get('_decision_acquisition')
    if (not isinstance(acquisition, dict) or acquisition.get('policy') != PROSPECTIVE
            or acquisition.get('reference_session') != session.isoformat()):
        raise ValueError('prospective source acquisition context missing or mismatched')
    acquired_at = _aware(acquisition.get('started_at'))
    if acquisition.get('vintage_as_of') != mutable_vintage(config, session, now=acquired_at):
        raise ValueError('prospective source vintage mismatch')
    accepted = store.load_frozen(identity)
    if accepted is None:
        if any(cohort['ledger'].read_policy_session_context(session) is not None
               for cohort in getattr(owner, 'cohorts', [])):
            raise ValueError('persisted staging context lacks immutable decision inputs')
        enrichment = fetch_enrichment()
        cutoff = _aware((now or utc_now)())
        digest = hashlib.sha256(store.encode(enrichment, **store.codec_limits).encode()).hexdigest()
        context = create_context(config, session, acquired_at=acquired_at, cutoff=cutoff,
                                 source_digest=source_digest, enrichment_digest=digest,
                                 eligibility_digest=eligibility_digest)
        accepted = store.freeze(identity, {'context': context, 'enrichment': enrichment}, deadline=deadline)
    if not isinstance(accepted, dict) or set(accepted) != {'context', 'enrichment'}:
        raise ValueError('invalid accepted decision inputs')
    context = validate_context(config, session, accepted['context'])
    if context['source_digest'] != source_digest or _aware(context['acquisition_started_at']) != acquired_at:
        raise ValueError('decision source binding mismatch')
    if context['eligibility_digest'] != eligibility_digest:
        raise ValueError('decision eligibility binding mismatch')
    if hashlib.sha256(store.encode(accepted['enrichment'], **store.codec_limits).encode()).hexdigest() != context['enrichment_digest']:
        raise ValueError('decision enrichment binding mismatch')
    if not isinstance(accepted['enrichment'], dict):
        raise ValueError('decision enrichment must be a mapping')
    return accepted['enrichment'], context
