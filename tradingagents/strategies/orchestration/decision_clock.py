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
                   eligibility_digest: str = hashlib.sha256(b'null').hexdigest()) -> dict:
    context = {'policy': policy(config), 'reference_session': session.isoformat(),
               'acquisition_started_at': _aware(acquired_at).isoformat(),
               'cutoff': _aware(cutoff).isoformat(),
               'eligible_session': next_session(session).isoformat(),
               'source_digest': source_digest, 'enrichment_digest': enrichment_digest,
               'eligibility_digest': eligibility_digest}
    return validate_context(config, session, context)


def validate_context(config: dict, session: date, context: object) -> dict:
    if policy(config) != PROSPECTIVE or not isinstance(context, dict):
        raise ValueError('prospective decision context required')
    required = {'policy', 'reference_session', 'acquisition_started_at', 'cutoff',
                'eligible_session', 'source_digest', 'enrichment_digest', 'eligibility_digest'}
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
    if policy(config) == HISTORICAL or not completed:
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
    if not isinstance(accepted, dict) or set(accepted) != {'context', 'enrichment'}:
        raise ValueError('completed replay lacks immutable decision inputs')
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


def prepare_decision_inputs(owner, session: date, source_data: dict, source_digest: str,
                            fetch_enrichment, *, eligibility=None,
                            deadline: float | None = None, now=None) -> tuple[dict, dict | None]:
    """Freeze the shared final information cutoff once, before any committee.

    Models may already have analyzed the earlier source bundle. Enrichment is
    acquired later; its actual completion therefore precedes the final cutoff.
    Replay consumes this immutable enrichment and clock without new acquisition.
    """
    config = owner._base_config
    if policy(config) == HISTORICAL:
        return fetch_enrichment(), None
    from .source_inputs import SourceInputStore, daily_source_store
    shared, identity = daily_source_store(owner, session.isoformat())
    identity = {**identity, 'purpose': 'prospective-decision-inputs-v1'}
    store = SourceInputStore(shared.cache_dir, accepted_dir=shared.accepted_dir / 'decision_inputs',
                             ttl_s=shared.ttl_s)
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
        digest = hashlib.sha256(store.encode(enrichment).encode()).hexdigest()
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
    if hashlib.sha256(store.encode(accepted['enrichment']).encode()).hexdigest() != context['enrichment_digest']:
        raise ValueError('decision enrichment binding mismatch')
    if not isinstance(accepted['enrichment'], dict):
        raise ValueError('decision enrichment must be a mapping')
    return accepted['enrichment'], context
