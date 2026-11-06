"""One immutable accepted economic boundary for the supported sixteen books.

The seal accepts evidence, not prospective decisions. All-book validation is
read-only; existing execution contexts remain the authority for started books.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import re
from types import MappingProxyType

from tradingagents.strategies.execution import MarketBar, CorporateAction, stable_id
from tradingagents.strategies.execution.price_source import AdjustedClose
from tradingagents.strategies.metrics.epochs import _matches_context
from .governed_market_data import GovernedRecoveryBinding
from .session_executor import SessionInputBundle, _PersistedSessionInputBundle, _canonical_json_value, _SealedPreconditionConflictError
from .source_inputs import SourceInputStore, SourceInputError, daily_source_store, source_configuration_fingerprint, configuration_fingerprint
from .trading_calendar import session_open

VERSION = 'shared-execution-v1'
POLICY = 'supported_sixteen_books_v1'
# Named, finite codecs only. No arbitrary dataclass or class-name decoding.
BAR = ('ticker', 'session', 'open', 'high', 'low', 'close', 'source', 'fetched_at', 'adjusted')
ACTION = ('action_id', 'ticker', 'session', 'action_type', 'ratio', 'cash_per_share', 'source',
          'fetched_at', 'verified', 'payment_date', 'payment_source', 'payment_reference', 'payment_observed_at')
CLOSE = ('symbol', 'session', 'close', 'source', 'fetched_at', 'previous_session', 'previous_close')
RECOVERY = ('ticker', 'recovery_id', 'contract_version', 'evidence_digest')
SUMMARY = {'ticker', 'session', 'recovery_id', 'contract_version', 'evidence_digest', 'affected_cohort_ids'}
BUNDLE = {'session', 'tickers', 'bars', 'actions', 'benchmarks', 'governed_recoveries',
          'governed_failure_map', 'governed_recovery_summaries', 'continuity_actions', 'action_coverage', 'payment_actions'}
EPOCH = ('epoch_id', 'generation_id', 'generation_commit', 'behavior_hash', 'config_hash',
         'metric_schema_version', 'execution_clock_version', 'pricing_version', 'cost_model_version',
         'start_session', 'end_session', 'status', 'boundary_reason')
FIELDS = {'version', 'session', 'epoch', 'configuration', 'effective_configuration', 'profile_sha256', 'accepted_at',
          'bundle', 'bundle_sha256', 'books'}
BOOK = {'identity', 'scope', 'benchmarks', 'prepared', 'starting_state', 'starting_state_digest',
        'overdue', 'precondition_digest', 'post_cancellation_digest'}
PREPARED = {'required_tickers', 'market', 'config', 'borrow', 'provenance', 'volatility'}


def configured(owner):
    return owner._base_config.get('autoresearch', {}).get('stabilization_profile_policy') == POLICY


def _fields(document, allowed):
    if not isinstance(document, Mapping) or set(document) != set(allowed):
        raise SourceInputError('shared execution fields invalid')


def _ordered(value):
    if isinstance(value, Mapping):
        return {key: _ordered(value[key]) for key in sorted(value, key=SourceInputStore.encode)}
    if isinstance(value, tuple):
        return tuple(_ordered(item) for item in value)
    if isinstance(value, list):
        return [_ordered(item) for item in value]
    return value


def _digest(value):
    return hashlib.sha256(SourceInputStore.encode(_ordered(value)).encode()).hexdigest()


def _aware(value, cutoff):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None or value > cutoff:
        raise SourceInputError('shared execution observation clock invalid')


def _named(value, cls, names):
    if not isinstance(value, cls):
        raise SourceInputError('shared execution named value invalid')
    return {name: getattr(value, name) for name in names}


def serialize_execution_bundle(bundle):
    if not isinstance(bundle, SessionInputBundle):
        raise SourceInputError('shared execution bundle invalid')
    return {'session': bundle.session, 'tickers': bundle.tickers,
        'bars': {key: _named(value, MarketBar, BAR) for key, value in bundle.bars.items()},
        'actions': tuple(_named(value, CorporateAction, ACTION) for value in bundle.actions),
        'benchmarks': {key: _named(value, AdjustedClose, CLOSE) for key, value in bundle.benchmarks.items()},
        'governed_recoveries': {key: _named(value, GovernedRecoveryBinding, RECOVERY) for key, value in bundle.governed_recoveries.items()},
        'governed_failure_map': dict(bundle.governed_failure_map),
        'governed_recovery_summaries': tuple(dict(row) for row in bundle.governed_recovery_summaries),
        'continuity_actions': tuple(_named(value, CorporateAction, ACTION) for value in bundle.continuity_actions),
        'action_coverage': dict(bundle.action_coverage),
        'payment_actions': tuple(_named(value, CorporateAction, ACTION) for value in bundle.payment_actions)}


def _restore(document, cls, names, validated_at):
    _fields(document, names)
    optional = {'payment_observed_at', 'previous_session', 'payment_date', 'previous_close', 'ratio', 'cash_per_share'}
    for name, value in document.items():
        if value is None:
            if name not in optional:
                raise SourceInputError('shared execution required named value missing')
            continue
        if name in {'fetched_at', 'payment_observed_at'}:
            _aware(value, validated_at)
        elif name in {'session', 'previous_session', 'payment_date'}:
            if not isinstance(value, date) or isinstance(value, datetime):
                raise SourceInputError('shared execution date invalid')
        elif name in {'open', 'high', 'low', 'close', 'previous_close', 'ratio', 'cash_per_share'}:
            if not isinstance(value, Decimal) or not value.is_finite():
                raise SourceInputError('shared execution decimal invalid')
        elif name in {'verified', 'adjusted'}:
            if type(value) is not bool:
                raise SourceInputError('shared execution boolean invalid')
        elif not isinstance(value, str) or (not value and name not in {'payment_source', 'payment_reference'}):
            raise SourceInputError('shared execution text invalid')
    return cls(**document)


def _tickers(values):
    if (not isinstance(values, tuple) or len(values) > 10000 or tuple(sorted(set(values))) != values
            or any(not isinstance(t, str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,31}', t) for t in values)):
        raise SourceInputError('shared execution scope invalid')
    return values


def deserialize_execution_bundle(document, validated_at):
    _fields(document, BUNDLE)
    _aware(validated_at, datetime.now(timezone.utc))
    session, tickers = document['session'], _tickers(document['tickers'])
    if not isinstance(session, date) or isinstance(session, datetime):
        raise SourceInputError('shared execution session invalid')
    if document['governed_failure_map']:
        raise SourceInputError('shared execution cannot accept failed economics')
    bars, benchmarks, recoveries = {}, {}, {}
    for key, row in document['bars'].items():
        bar = _restore(row, MarketBar, BAR, validated_at)
        if key != (bar.ticker, bar.session) or bar.ticker not in tickers or bar.session != session:
            raise SourceInputError('shared execution bar identity invalid')
        bars[key] = bar
    if set(bars) != {(ticker, session) for ticker in tickers}:
        raise SourceInputError('shared execution bars incomplete')
    for key, row in document['benchmarks'].items():
        close = _restore(row, AdjustedClose, CLOSE, validated_at)
        if key != (close.symbol, close.session) or close.session != session:
            raise SourceInputError('shared execution benchmark identity invalid')
        benchmarks[key] = close
    for key, row in document['governed_recoveries'].items():
        binding = _restore(row, GovernedRecoveryBinding, RECOVERY, validated_at)
        if key != binding.ticker or key not in tickers:
            raise SourceInputError('shared execution recovery identity invalid')
        recoveries[key] = binding
    summaries = document['governed_recovery_summaries']
    if not isinstance(summaries, tuple) or len(summaries) != len(recoveries):
        raise SourceInputError('shared execution recovery summaries incomplete')
    seen = set()
    for row in summaries:
        _fields(row, SUMMARY)
        binding = recoveries.get(row['ticker'])
        if (binding is None or row['ticker'] in seen or row['session'] != session.isoformat()
                or any(row[key] != getattr(binding, key) for key in RECOVERY)
                or not isinstance(row['affected_cohort_ids'], tuple)
                or not row['affected_cohort_ids'] or tuple(sorted(set(row['affected_cohort_ids']))) != row['affected_cohort_ids']):
            raise SourceInputError('shared execution recovery summary identity invalid')
        seen.add(row['ticker'])
    groups = {}
    ids = set()
    for name in ('actions', 'continuity_actions', 'payment_actions'):
        groups[name] = tuple(_restore(row, CorporateAction, ACTION, validated_at) for row in document[name])
        for action in groups[name]:
            if action.action_id in ids or (name != 'payment_actions' and action.ticker not in tickers):
                raise SourceInputError('shared execution action identity invalid')
            ids.add(action.action_id)
    for day, scope in document['action_coverage'].items():
        if not isinstance(day, date) or isinstance(day, datetime) or day >= session or not set(_tickers(scope)) <= set(tickers):
            raise SourceInputError('shared execution action coverage invalid')
    return _PersistedSessionInputBundle(session, tickers, bars, groups['actions'], benchmarks,
        recoveries, {}, document['governed_recovery_summaries'], groups['continuity_actions'],
        document['action_coverage'], groups['payment_actions'], validated_at=validated_at)


def _store(owner, session):
    shared, identity = daily_source_store(owner, session.isoformat())
    return SourceInputStore(shared.cache_dir, accepted_dir=shared.accepted_dir / 'shared_execution'), {
        **identity, 'purpose': VERSION}


def load_shared_execution(owner, session):
    store, identity = _store(owner, session)
    return store.load_frozen(identity)


def _epoch(owner, session, epoch_id):
    epoch = owner._metric_store.current_epoch()
    if (epoch is None or epoch.status != 'open' or epoch.epoch_id != epoch_id
            or epoch.start_session > session or not _matches_context(epoch, owner._metric_epoch_context)
            or owner._metric_store.pending_critical_gap() is not None):
        raise SourceInputError('shared execution epoch identity invalid')
    return {key: getattr(epoch, key) for key in EPOCH}


def _roster(owner):
    from .operational_report import EXPECTED_COHORTS
    names = [c['config'].name for c in owner.cohorts]
    if len(names) != len(set(names)) or set(names) != set(EXPECTED_COHORTS):
        raise SourceInputError('shared execution exact roster invalid')
    return sorted(owner.cohorts, key=lambda c: c['config'].name)


def _identity(cohort):
    config, executor = cohort['config'], cohort['executor']
    return {'cohort_id': config.name, 'horizon': config.horizon, 'size_profile': config.size_profile,
        'ledger_cohort_id': cohort['ledger'].cohort_id,
        'policy_id': str(cohort['engine'].ar_config.get('paper_ledger', {}).get('policy_id', 'foundation-'+config.horizon)),
        'semantic_config_digest': stable_id('session_execution_config', executor.semantic_policy_document())}


def _profile_sha():
    value = os.environ.get('EVENTEDGE_GENERATION_CONFIG_SHA256')
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise SourceInputError('shared execution requires frozen profile SHA')
    return value


def _rates(document):
    if not isinstance(document, Mapping):
        raise SourceInputError('shared execution borrow document invalid')
    rates = {}
    for ticker, value in document.items():
        if value is None:
            rates[ticker] = None
            continue
        if not isinstance(value, str):
            raise SourceInputError('shared execution borrow value invalid')
        try:
            rate = Decimal(value)
        except ArithmeticError as error:
            raise SourceInputError('shared execution borrow value invalid') from error
        if not rate.is_finite() or rate < 0 or format(rate, 'f') != value:
            raise SourceInputError('shared execution borrow value invalid')
        rates[ticker] = rate
    return rates


def _cancelled(overdue, session):
    # Overdue intents are deliberately absent from execution_starting_state's
    # due-intent set. Commit their deterministic cancellation separately rather
    # than treating the pre-effect and post-cancellation documents as one hash.
    expected = {'intents': [{**row, 'status': 'cancelled'} for row in overdue['intents']],
                'transitions': list(overdue['transitions']), 'signals': overdue['signals'],
                'associations': overdue['associations'], 'exit_allocations': overdue['exit_allocations']}
    reason = 'missed exact eligible session'
    for row in overdue['intents']:
        expected['transitions'].append({'transition_id': stable_id('order_status', row['intent_id'], 'cancelled', reason),
            'intent_id': row['intent_id'], 'status': 'cancelled', 'occurred_at': session_open(session).isoformat(), 'reason': reason})
    expected['transitions'].sort(key=lambda row: (row['intent_id'], row['transition_id']))
    return expected


def _precondition(ledger, session):
    starting = _canonical_json_value(ledger.execution_starting_state(session))
    overdue = _canonical_json_value(ledger.overdue_cancellation_inputs(session))
    return starting, overdue


def validate_unbound_precondition(ledger, session, book):
    starting, overdue = _precondition(ledger, session)
    if (starting != book['starting_state'] or overdue != book['overdue']
            or _digest({'starting_state': starting, 'overdue': overdue}) != book['precondition_digest']):
        raise _SealedPreconditionConflictError('shared execution unbound starting state changed')


def prepare_shared_execution(owner, session, epoch_id, bundle, processed_at, borrow_by_book):
    if not configured(owner):
        raise SourceInputError('shared execution profile policy missing')
    _aware(processed_at, datetime.now(timezone.utc))
    document = serialize_execution_bundle(bundle)
    # Round trip strictly before accepting internal persisted freshness semantics.
    deserialize_execution_bundle(document, processed_at)
    books = {}
    for cohort in _roster(owner):
        executor, ledger, name = cohort['executor'], cohort['ledger'], cohort['config'].name
        if ledger.session_execution_context(session) is not None:
            raise SourceInputError('shared execution cannot replace historical contexts')
        required = executor.required_tickers(session, epoch_id)
        scope = tuple(sorted(set(required) | set(executor.benchmark_symbols)))
        prepared, _ = executor.prepare_execution_context(session, epoch_id, bundle.for_tickers(scope),
                                                        borrow_by_book.get(name, {}), processed_at)
        _rates(prepared['borrow'])
        starting, overdue = _precondition(ledger, session)
        books[name] = {'identity': _identity(cohort), 'scope': scope,
            'benchmarks': tuple(sorted(executor.benchmark_symbols)), 'prepared': prepared,
            'starting_state': starting, 'starting_state_digest': ledger.execution_governed_state_digest(session),
            'overdue': overdue, 'precondition_digest': _digest({'starting_state': starting, 'overdue': overdue}),
            'post_cancellation_digest': _digest(_cancelled(overdue, session))}
    return MappingProxyType({'version': VERSION, 'session': session, 'epoch': _epoch(owner, session, epoch_id),
        'configuration': source_configuration_fingerprint(owner._base_config),
        'effective_configuration': configuration_fingerprint(owner._base_config), 'profile_sha256': _profile_sha(),
        'accepted_at': processed_at, 'bundle': document, 'bundle_sha256': _digest(document), 'books': books})


def freeze_shared_execution(owner, payload, deadline=None):
    store, identity = _store(owner, payload['session'])
    accepted = store.freeze(identity, payload, acquired_at=payload['accepted_at'], deadline=deadline)
    if _digest(accepted) != _digest(payload):
        raise SourceInputError('shared execution accepted winner differs')
    validate_shared_execution(owner, accepted, payload['session'], payload['epoch']['epoch_id'])
    return accepted


@dataclass(frozen=True)
class SharedExecutionPlan:
    cohort: object
    bundle: SessionInputBundle
    borrow_rates: Mapping
    precondition: Mapping | None


def validate_shared_execution(owner, payload, session, epoch_id):
    """Validate the ENTIRE cohort before any caller may apply the first effect."""
    _fields(payload, FIELDS)
    if (not configured(owner) or payload['version'] != VERSION or payload['session'] != session
            or payload['epoch'] != _epoch(owner, session, epoch_id)
            or payload['profile_sha256'] != _profile_sha()
            or payload['configuration'] != source_configuration_fingerprint(owner._base_config)
            or payload['effective_configuration'] != configuration_fingerprint(owner._base_config)
            or payload['bundle_sha256'] != _digest(payload['bundle'])):
        raise SourceInputError('shared execution identity invalid')
    bundle = deserialize_execution_bundle(payload['bundle'], payload['accepted_at'])
    cohorts = _roster(owner)
    if set(payload['books']) != {c['config'].name for c in cohorts}:
        raise SourceInputError('shared execution sealed roster invalid')
    plans = []
    all_scopes = set()
    for cohort in cohorts:
        executor, ledger = cohort['executor'], cohort['ledger']
        book = payload['books'][cohort['config'].name]
        _fields(book, BOOK)
        prepared = book['prepared']
        _fields(prepared, PREPARED)
        _tickers(book['scope'])
        _tickers(prepared['required_tickers'])
        if (book['identity'] != _identity(cohort) or ledger.cohort_id != cohort['config'].name
                or book['benchmarks'] != tuple(sorted(executor.benchmark_symbols))
                or book['scope'] != tuple(sorted(set(prepared['required_tickers']) | set(executor.benchmark_symbols)))
                or ledger.session_invalid_reason(session)
                or ledger.connection.execute('SELECT 1 FROM session_execution_contexts WHERE session > ? LIMIT 1', (session.isoformat(),)).fetchone()):
            raise SourceInputError('shared execution book identity invalid')
        if (book['starting_state_digest'] != stable_id('session_governed_state', book['starting_state'])
                or book['precondition_digest'] != _digest({'starting_state': book['starting_state'], 'overdue': book['overdue']})
                or book['post_cancellation_digest'] != _digest(_cancelled(book['overdue'], session))):
            raise SourceInputError('shared execution state commitment invalid')
        all_scopes.update(book['scope'])
        rates = _rates(prepared['borrow'])
        view = bundle.for_tickers(book['scope'])
        scoped = _PersistedSessionInputBundle(**{key: getattr(view, key) for key in BUNDLE},
                                              validated_at=payload['accepted_at'])
        context = ledger.session_execution_context(session)
        if context is None:
            # No phase, staging, snapshot or accounting may precede the context.
            for table in ('session_phases', 'staging_runs', 'account_snapshots', 'session_runs', 'fills', 'cash_events',
                          'marks', 'borrow_accruals', 'financing_accruals', 'dividend_events', 'fee_events', 'benchmark_observations'):
                if ledger.connection.execute('SELECT 1 FROM '+table+' WHERE session=? LIMIT 1', (session.isoformat(),)).fetchone():
                    raise SourceInputError('shared execution unbound effects present')
            validate_unbound_precondition(ledger, session, book)
            current, _ = executor.prepare_execution_context(session, epoch_id, scoped, rates, payload['accepted_at'])
            if current != prepared:
                raise SourceInputError('shared execution prepared inputs changed')
            plans.append(SharedExecutionPlan(cohort, scoped, MappingProxyType(rates), book))
        else:
            executor.validate_bound_context(session, epoch_id)
            current, _ = executor.prepare_execution_context(session, epoch_id, scoped, rates, payload['accepted_at'])
            if current != prepared:
                raise SourceInputError('shared execution bundle differs from bound inputs')
            economic = json.loads(str(context['economic_inputs_json']))
            expected_economic = {'market': prepared['market'], 'starting_state': book['starting_state']}
            if executor.portfolio_policy_config is not None:
                expected_economic['portfolio_policy_volatility_evidence'] = prepared['volatility']
            actual_overdue = _canonical_json_value(ledger.overdue_cancellation_inputs(session,
                intent_ids=[row['intent_id'] for row in book['overdue']['intents']]))
            if (economic != expected_economic or context['bound_at'] != payload['accepted_at']
                    or tuple(context['required_tickers']) != prepared['required_tickers']
                    or context['input_digest'] != stable_id('session_economic_inputs', expected_economic)
                    or context['market_digest'] != stable_id('session_market_inputs', prepared['market'])
                    or context['config_digest'] != stable_id('session_execution_config', prepared['config'])
                    or context['borrow_digest'] != stable_id('session_borrow_inputs', prepared['borrow'])
                    or json.loads(str(context['provenance_json'])) != prepared['provenance']
                    or context['provenance_digest'] != stable_id('session_input_provenance', prepared['provenance'])
                    or context['starting_state_digest'] != book['starting_state_digest']
                    or _digest(actual_overdue) != book['post_cancellation_digest']
                    or ledger.overdue_cancellation_inputs(session)['intents']):
                raise SourceInputError('shared execution bound context differs from seal')
            plans.append(SharedExecutionPlan(cohort, executor.persisted_input_bundle(session),
                                            MappingProxyType(executor.persisted_borrow_rates(session)), None))
    expected_benchmarks = {symbol for c in cohorts for symbol in c['executor'].benchmark_symbols}
    if set(bundle.benchmarks) != {(symbol, session) for symbol in expected_benchmarks}:
        raise SourceInputError('shared execution exact benchmark scope invalid')
    from .governed_market_data import _summary
    for row in bundle.governed_recovery_summaries:
        record = owner._metric_store.load_governed_bar_recovery_by_id(row['recovery_id'])
        if record is None or dict(row) != _summary(record):
            raise SourceInputError('shared execution recovery summary evidence invalid')
    if set(bundle.tickers) != all_scopes:
        raise SourceInputError('shared execution union scope invalid')
    return tuple(plans)
