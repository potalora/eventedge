"""Proof of completed economics and irretrievably missed prospective staging.

Only disposable SQLite copies are checkpointed. Authoritative databases are
read as stable regular bytes; no constructor can initialize or migrate them.
The durable proof binds immutable session rows, not future account state.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

VERSION = 'missed-staging-operator-assessment-v1'
MAX_FILE = 256 * 1024 * 1024
MAX_RECEIPT = 32 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False, default=str).encode()).hexdigest()


def read_file(path, limit=MAX_FILE):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('operator_assessment_unsafe_path')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 <= before.st_size <= limit:
            raise ValueError('operator_assessment_file_bounds')
        raw = handle.read(limit + 1)
        token = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if len(raw) > limit or token(before) != token(os.fstat(handle.fileno())) or token(before) != token(path.lstat()):
            raise ValueError('operator_assessment_file_changed')
        return raw


@contextmanager
def database_copy(path):
    """Merge a copied WAL privately, then expose an immutable read connection."""
    with tempfile.TemporaryDirectory(prefix='eventedge-code-revision-check-') as directory:
        target = Path(directory) / Path(path).name
        target.write_bytes(read_file(path))
        for suffix in ('-wal', '-shm'):
            adjacent = Path(str(path) + suffix)
            if adjacent.exists() or adjacent.is_symlink():
                Path(str(target) + suffix).write_bytes(read_file(adjacent))
        with closing(sqlite3.connect(target)) as connection:
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        with closing(sqlite3.connect(target.as_uri() + '?mode=ro&immutable=1', uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            yield connection, target


def _envelope(path, generation, session, commit):
    from .source_inputs import SourceInputStore, CONTRACT_VERSION, FULL_FILING_MAX_BYTES, FULL_FILING_MAX_NODES
    raw = read_file(path, FULL_FILING_MAX_BYTES)
    limits = dict(max_bytes=FULL_FILING_MAX_BYTES, max_nodes=FULL_FILING_MAX_NODES)
    envelope = SourceInputStore.decode(raw.decode(), **limits)
    if (type(envelope) is not dict or set(envelope) != {'version', 'identity', 'acquired_at', 'payload', 'digest'}
            or envelope['version'] != CONTRACT_VERSION
            or envelope['identity'].get('generation') != generation
            or envelope['identity'].get('session') != session
            or envelope['identity'].get('commit') != commit
            or envelope['digest'] != hashlib.sha256(SourceInputStore.encode(envelope['payload'], **limits).encode()).hexdigest()):
        raise ValueError('operator_assessment_source_identity')
    return envelope, hashlib.sha256(raw).hexdigest()


def _rows(connection, table, session, *, column='session'):
    rows = connection.execute('SELECT * FROM ' + table + ' WHERE ' + column + '=? ORDER BY 1 LIMIT 10001', (session,)).fetchall()
    if len(rows) > 10000:
        raise ValueError('operator_assessment_row_bounds')
    return [dict(row) for row in rows]


class _MetricView:
    def __init__(self, store, canonical):
        self.store, self.path = store, canonical

    def __getattr__(self, key):
        return getattr(self.store, key)


def session_proof(repo, row, identity, *, initial):
    """Validate and fingerprint one immutable P0-only session, without effects."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.strategies.execution import stable_id
    from tradingagents.strategies.metrics.store import MetricStore
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    from .cohort_orchestrator import build_default_cohorts, SIZE_PROFILES
    from .generation_config import apply_generation_config, read_generation_config
    from .operating_contract import ROSTER, profile_contract
    from .session_executor import SessionExecutor, PHASES
    from .source_retirement import generation_for_session
    from .source_inputs import configuration_fingerprint, public_source_configuration_declaration, PUBLIC_CONFIGURATION_KEY
    from . import shared_execution as shared

    selected = generation_for_session(row, identity['session'])
    state, session = Path(row['state_dir']), date.fromisoformat(identity['session'])
    if (selected['git_commit'] != identity['generation_commit'] or selected['config_sha256'] != identity['config_sha256']
            or identity['state_dir'] != str(state) or identity['worktree_path'] != row['worktree_path']):
        raise ValueError('operator_assessment_identity')
    config = apply_generation_config(DEFAULT_CONFIG, selected['config_file'], selected['config_sha256'])
    config['autoresearch']['state_dir'] = str(state)
    _, raw_profile, _ = read_generation_config(selected['config_file'], selected['config_sha256'])
    profile_contract(raw_profile, profile_sha256=selected['config_sha256'])
    from .source_retirement_profile import profile_binding
    binding = row['source_retirement']
    profile_proof = profile_binding(raw_profile, selected['config_sha256'])
    original_profile,_,_=read_generation_config(binding['previous_config_file'],binding['previous_config_sha256'])
    if (profile_proof is None or profile_proof['effective_session']!=binding['effective_session']
            or profile_proof['previous_config_sha256']!=binding['previous_config_sha256']
            or profile_proof['previous_config_text'].encode()!=original_profile):
        raise ValueError('operator_assessment_retirement_profile_join')
    slot = configuration_fingerprint({'generation': row['gen_id'], 'session': session.isoformat()}) + '.json'
    source, source_hash = _envelope(state/'source_inputs'/slot, row['gen_id'], session.isoformat(), selected['git_commit'])
    seal_envelope, seal_hash = _envelope(state/'source_inputs/shared_execution'/slot, row['gen_id'], session.isoformat(), selected['git_commit'])
    seal = seal_envelope['payload']
    shared._fields(seal, shared.FIELDS)
    bundle = shared.deserialize_execution_bundle(seal['bundle'], seal['accepted_at'])
    if (seal['version'] != shared.VERSION or seal['session'] != session
            or seal['profile_sha256'] != selected['config_sha256']
            or seal['configuration'] != source['identity'].get('configuration')
            or seal_envelope['identity'].get('configuration') != seal['configuration']
            or seal['bundle_sha256'] != shared._digest(seal['bundle']) or set(seal['books']) != set(ROSTER)):
        raise ValueError('operator_assessment_shared_seal')
    if source['payload'].get(PUBLIC_CONFIGURATION_KEY) != public_source_configuration_declaration(config)[PUBLIC_CONFIGURATION_KEY]:
        raise ValueError('operator_assessment_public_source_witness')
    for folder in ('screening', 'decision_inputs', 'staging_volatility'):
        if (state/'source_inputs'/folder/slot).exists() or (state/'source_inputs'/folder/slot).is_symlink():
            raise ValueError('operator_assessment_research_checkpoint_present')
    proof = {'source_sha256': source_hash, 'shared_execution_sha256': seal_hash,
             'profile_sha256': selected['config_sha256'], 'books': {}, 'epoch': {}}
    metric_path = state/'metrics_v2.sqlite3'
    with database_copy(metric_path) as (metrics, metric_copy):
        store = MetricStore.open_existing(metric_copy, immutable=True)
        epoch = store.load_epoch(seal['epoch']['epoch_id'])
        if initial and (epoch.status != 'open' or store.current_epoch().epoch_id != epoch.epoch_id):
            raise ValueError('operator_assessment_later_epoch')
        immutable_epoch = {key: getattr(epoch, key) for key in shared.EPOCH if key not in {'status', 'end_session', 'boundary_reason'}}
        if (epoch.generation_id != row['gen_id'] or epoch.generation_commit != selected['git_commit']
                or not epoch.start_session <= session or epoch.status not in {'open', 'closed'}
                or epoch.end_session is not None and epoch.end_session < session
                or any(seal['epoch'][key] != value for key, value in immutable_epoch.items())):
            raise ValueError('operator_assessment_epoch')
        proof['epoch'] = {key: str(value) for key, value in immutable_epoch.items()}
        for table in ('strategy_health', 'candidate_signal_identity_bindings', 'candidate_input_issues'):
            if metrics.execute('SELECT 1 FROM '+table+' WHERE session=? AND epoch_id=? LIMIT 1', (session.isoformat(), epoch.epoch_id)).fetchone():
                raise ValueError('operator_assessment_research_effects_present')
        if initial and metrics.execute("SELECT 1 FROM critical_gap_markers WHERE status='pending' LIMIT 1").fetchone():
            raise ValueError('operator_assessment_critical_gap')
        union, benchmark_union = set(), set()
        for cohort in build_default_cohorts(config):
            canonical = state/cohort.name/'portfolio.db'
            with database_copy(canonical) as (connection, target):
                contexts = _rows(connection, 'session_execution_contexts', session.isoformat())
                snapshots = _rows(connection, 'account_snapshots', session.isoformat())
                phases = _rows(connection, 'session_phases', session.isoformat())
                runs = _rows(connection, 'session_runs', session.isoformat())
                if (len(contexts) != 1 or len(snapshots) != 1 or len(phases) != len(PHASES) or len(runs) != 1
                        or runs[0]['cohort_id'] != cohort.name or runs[0]['valid'] != 1
                        or runs[0]['invalid_reason'] or not runs[0]['completed_at']
                        or any(_rows(connection, table, session.isoformat()) for table in ('staging_runs', 'committee_decisions', 'session_invalidations'))
                        or _rows(connection, 'signals', session.isoformat(), column='reference_session')):
                    raise ValueError('operator_assessment_book_boundary')
                context, snapshot = contexts[0], snapshots[0]
                if (context['epoch_id'] != epoch.epoch_id or context['cohort_id'] != cohort.name
                        or snapshot['epoch_id'] != epoch.epoch_id or snapshot['cohort_id'] != cohort.name
                        or snapshot['valid'] != 1 or snapshot['invalid_reason']):
                    raise ValueError('operator_assessment_book_identity')
                if initial and connection.execute('SELECT 1 FROM session_execution_contexts WHERE session>? LIMIT 1', (session.isoformat(),)).fetchone():
                    raise ValueError('operator_assessment_later_context')
                by_phase = {value['phase']: value for value in phases}
                expected = context['starting_state_digest']
                for phase in PHASES:
                    item = by_phase.get(phase)
                    if (not item or item['cohort_id'] != cohort.name or not item['completed_at']
                            or item['pre_state_digest'] != expected or not item['post_state_digest']):
                        raise ValueError('operator_assessment_phase_chain')
                    expected = item['post_state_digest']
                book = seal['books'][cohort.name]
                shared._fields(book, shared.BOOK); shared._fields(book['prepared'], shared.PREPARED)
                prepared = book['prepared']
                union.update(book['scope']); benchmark_union.update(book['benchmarks'])
                if (book['starting_state_digest'] != stable_id('session_governed_state', book['starting_state'])
                        or book['precondition_digest'] != shared._digest({'starting_state': book['starting_state'], 'overdue': book['overdue']})
                        or book['post_cancellation_digest'] != shared._digest(shared._cancelled(book['overdue'], session))):
                    raise ValueError('operator_assessment_state_commitment')
                expected_economics = {'market': prepared['market'], 'starting_state': book['starting_state'],
                                     'portfolio_policy_volatility_evidence': prepared['volatility']}
                if (book['identity']['cohort_id'] != cohort.name or book['identity']['ledger_cohort_id'] != cohort.name
                        or context['input_digest'] != stable_id('session_economic_inputs', expected_economics)
                        or json.loads(context['economic_inputs_json']) != expected_economics
                        or context['market_digest'] != stable_id('session_market_inputs', prepared['market'])
                        or context['config_digest'] != stable_id('session_execution_config', prepared['config'])
                        or context['borrow_digest'] != stable_id('session_borrow_inputs', prepared['borrow'])
                        or context['provenance_digest'] != stable_id('session_input_provenance', prepared['provenance'])
                        or context['starting_state_digest'] != book['starting_state_digest']
                        or context['bound_at'] != seal['accepted_at'].isoformat()):
                    raise ValueError('operator_assessment_context_seal')
                if initial:
                    ledger = PortfolioLedger.open_existing(target, immutable=True)
                    try:
                        # SQL remains bound to the immutable disposable copy;
                        # path-sensitive provenance retains the canonical name.
                        ledger.path = canonical
                        settings = deepcopy(config); ar = settings['autoresearch']
                        size = SIZE_PROFILES[cohort.size_profile]
                        ar.update(state_dir=str(canonical.parent), horizon=cohort.horizon, total_capital=size.total_capital)
                        ar['risk_gate'].update(max_positions=size.max_positions, max_position_pct=size.max_position_pct,
                            min_position_value=size.min_position_value, cash_reserve_pct=size.cash_reserve_pct,
                            long_only=not size.short_eligible)
                        executor = SessionExecutor(ledger, settings, size_profile=size, metric_store=_MetricView(store, metric_path))
                        executor.validate_bound_context(session, epoch.epoch_id)
                        from types import SimpleNamespace
                        witness = dict(config=cohort, executor=executor, ledger=ledger,
                                       engine=SimpleNamespace(ar_config=ar))
                        if (book['identity'] != shared._identity(witness)
                                or tuple(book['benchmarks']) != tuple(sorted(executor.benchmark_symbols))
                                or tuple(book['scope']) != tuple(sorted(set(prepared['required_tickers']) | set(executor.benchmark_symbols)))):
                            raise ValueError('operator_assessment_full_book_identity')
                        current, _ = executor.prepare_execution_context(session, epoch.epoch_id,
                            bundle.for_tickers(book['scope']), shared._rates(prepared['borrow']), seal['accepted_at'])
                        actual_overdue = ledger.overdue_cancellation_inputs(session,
                            intent_ids=[value['intent_id'] for value in book['overdue']['intents']])
                        from .session_executor import _canonical_json_value
                        if (current != prepared or shared._digest(_canonical_json_value(actual_overdue)) != book['post_cancellation_digest']
                                or ledger.overdue_cancellation_inputs(session)['intents']):
                            raise ValueError('operator_assessment_scoped_bundle')
                    finally:
                        ledger.close()
                economics = {table: digest(_rows(connection, table, session.isoformat())) for table in (
                    'fills', 'cash_events', 'marks', 'borrow_accruals', 'financing_accruals',
                    'dividend_events', 'fee_events', 'benchmark_observations', 'session_runs')}
                proof['books'][cohort.name] = {'context': digest(context), 'phases': digest(by_phase),
                    'snapshot': digest(snapshot), 'economics': economics}
        if set(bundle.tickers) != union or set(bundle.benchmarks) != {(symbol, session) for symbol in benchmark_union}:
            raise ValueError('operator_assessment_bundle_union')
        from .governed_market_data import _summary
        for value in bundle.governed_recovery_summaries:
            found = store.load_governed_bar_recovery_by_id(value['recovery_id'])
            if found is None or dict(value) != _summary(found):
                raise ValueError('operator_assessment_governed_recovery')
    if set(proof['books']) != set(ROSTER):
        raise ValueError('operator_assessment_roster')
    return proof


def validate_operator_assessment(repo, record, expected_sha256):
    """Revalidate durable session proof after future accounting has evolved."""
    from .source_retirement import validate_code_revision
    from .runtime_recovery import incident_key
    repo = Path(repo)
    key = incident_key(record['identity'])
    raw = read_file(repo/'data/runtime_recovery/operator_assessments'/ (key+'.json'), MAX_RECEIPT)
    receipt_path=repo/'data/runtime_recovery/operator_assessments'/(key+'.json')
    info=receipt_path.lstat()
    if info.st_uid!=Path(record['identity']['state_dir']).stat().st_uid or stat.S_IMODE(info.st_mode)!=0o600:
        raise ValueError('operator_assessment_receipt_custody')
    if not re.fullmatch('[a-f0-9]{64}', expected_sha256) or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('operator_assessment_receipt_hash')
    receipt = json.loads(raw)
    fields = {'version','incident_id','identity','incident_before','manifest_before','journal_before',
              'eligible_from','code_revision','retained_files','proof'}
    if type(receipt) is not dict or set(receipt) != fields:
        raise ValueError('operator_assessment_receipt_fields')
    if (receipt['version'] != VERSION or receipt['incident_id'] != key
            or receipt['identity'] != record['identity']
            or record['disposition'] != 'wait_next_session' or record['reason_code'] != 'missed_staging'
            or record['running'] is not False or record['daily_started'] is not True
            or record['attempt_count'] != receipt['incident_before']['attempt_count']):
        raise ValueError('operator_assessment_receipt_identity')
    expected = deepcopy(receipt['incident_before'])
    expected.update(disposition='wait_next_session', reason_code='missed_staging', next_attempt_at=None,
                    running=False, updated_at=record['updated_at'], operator_assessment_sha256=expected_sha256)
    if record != expected:
        raise ValueError('operator_assessment_incident_transition')
    from .trading_calendar import next_session, session_open
    eligible = session_open(next_session(date.fromisoformat(record['identity']['session'])))
    applied = datetime.fromisoformat(record['updated_at'])
    if (receipt['eligible_from'] != eligible.isoformat() or applied.tzinfo is None or applied < eligible
            or json.loads(receipt['journal_before'])['incidents'].get(key) != receipt['incident_before']):
        raise ValueError('operator_assessment_original_incident')
    rows = json.loads(read_file(repo/'data/generations/manifest.json', 2*1024*1024))['generations']
    row = next(value for value in rows if value['gen_id'] == record['identity']['generation_id'])
    original_rows=json.loads(receipt['manifest_before'])['generations']
    original=next(value for value in original_rows if value['gen_id']==row['gen_id'])
    if (row['source_retirement']!=original['source_retirement']
            or original.get('code_revision') is not None
            or original['git_commit']!=record['identity']['generation_commit']
            or original['config_sha256']!=record['identity']['config_sha256']
            or row['run_history'][:len(original['run_history'])]!=original['run_history']):
        raise ValueError('operator_assessment_retirement_changed')
    revision = validate_code_revision(row, repo=repo)
    if revision is None or revision != receipt['code_revision'] or session_proof(repo, row, record['identity'], initial=False) != receipt['proof']:
        raise ValueError('operator_assessment_proof_changed')
    stem = 'docs/reports/' + record['identity']['session'] + '-' + record['identity']['generation_id'] + '-operational-report'
    retained_files=receipt['retained_files']
    if type(retained_files) is not dict or stem+'.json' not in retained_files or stem+'.md' not in retained_files:
        raise ValueError('operator_assessment_retained_report_missing')
    report=json.loads(retained_files[stem+'.json']['text'])
    from .operational_report import render_operational_report
    if (report['generation_id']!=record['identity']['generation_id'] or report['generation_commit']!=record['identity']['generation_commit']
            or report['session']!=record['identity']['session'] or report['outcome']!='failed'
            or report['profile_sha256']!=record['identity']['config_sha256']
            or render_operational_report(report)!=retained_files[stem+'.md']['text']):
        raise ValueError('operator_assessment_original_report')
    names=[item['attempt_id'] for item in report['attempts']]
    if any(type(name) is not str or not re.fullmatch('[A-Za-z0-9_.-]{1,200}[.]json',name) for name in names):
        raise ValueError('operator_assessment_original_attempt')
    expected_files={stem+'.json',stem+'.md'}|{'data/logs/run_attempts/'+name for name in names}
    if set(retained_files)!=expected_files:
        raise ValueError('operator_assessment_retained_set')
    for relative, retained in retained_files.items():
        if (relative not in {stem+'.json',stem+'.md'} and not re.fullmatch(r'data/logs/run_attempts/[A-Za-z0-9_.-]{1,200}[.]json',relative)):
            raise ValueError('operator_assessment_retained_path')
        if hashlib.sha256(retained['text'].encode()).hexdigest() != retained['sha256']:
            raise ValueError('operator_assessment_retained_bytes')
        if hashlib.sha256(read_file(repo/relative, MAX_RECEIPT)).hexdigest() != retained['sha256']:
            raise ValueError('operator_assessment_retained_evidence_changed')
    return receipt
