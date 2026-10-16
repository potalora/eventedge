"""Bounded unattended recovery; accepted economics and decisions remain immutable.

The scheduler owns a durable launch journal. Recovery workers accept only bound
paper execution contexts and cannot acquire inputs or invoke a model. The daily
cycle remains the sole path that may initiate a new sample.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile

VERSION = 'eventedge-runtime-recovery-v1'
PROSPECTIVE_LAUNCH_MODE = 'prospective_once_v1'
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (300, 1800, 7200)
MAX_INCIDENTS = 64
RECOVERY_TIMEOUT_SECONDS = 900
DAILY_TIMEOUT_SECONDS = 3 * 3600 + 300
_LOCAL_ONLY = ContextVar('eventedge_recovery_local_only', default=False)
DISPOSITIONS = frozenset({'recovered', 'retrying', 'wait_next_session', 'requires_user'})
REASONS = frozenset({'complete', 'interrupted', 'transient_io', 'attempts_exhausted',
    'identity_mismatch', 'state_corrupt', 'accepted_session_invalid', 'missed_staging',
    'checkpoint_missing', 'checkpoint_invalid', 'accepted_outcome', 'auth_required',
    'worker_timeout', 'worker_failed', 'daily_failed', 'missed_session'})


class RecoveryUnavailable(ValueError):
    """Accepted local evidence cannot complete this session without fresh work."""


def local_recovery() -> bool:
    return _LOCAL_ONLY.get()


def require_acquisition_allowed(boundary: str) -> None:
    if local_recovery():
        raise RecoveryUnavailable('recovery_checkpoint_missing:' + boundary)


@contextmanager
def persisted_only():
    token = _LOCAL_ONLY.set(True)
    try:
        yield
    finally:
        _LOCAL_ONLY.reset(token)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('aware recovery clock required')
    return value.astimezone(timezone.utc)


def _result(disposition: str, reason: str) -> dict:
    if disposition not in DISPOSITIONS or reason not in REASONS:
        raise ValueError('invalid recovery result')
    return {'version': VERSION, 'disposition': disposition, 'reason_code': reason}


def recover_owner(owner, session: date, *, now: datetime | None = None) -> dict:
    """Resume actual committed P0 methods, then strictly local prospective staging.

    Caller holds the canonical exclusive runtime lock and has verified the frozen
    generation. This never creates an epoch, reopens invalid accounting, replays
    outcomes, or synthesizes a missing committee decision.
    """
    from .session_executor import PHASES
    from .trading_calendar import next_session, session_open
    from .decision_clock import PROSPECTIVE, policy
    from .daily_pipeline import (DailyRunState, partition_daily_replay,
        run_horizon_screening, run_candidate_reference_validation,
        run_candidate_volatility_validation, stage_daily_results)
    from .source_inputs import daily_source_store
    from .research_screening import load_screening

    now = _utc(now or datetime.now(timezone.utc))
    if policy(owner._base_config) != PROSPECTIVE or not owner.cohorts:
        return _result('requires_user', 'identity_mismatch')
    contexts = [cohort['ledger'].session_execution_context(session) for cohort in owner.cohorts]
    if any(cohort['ledger'].session_invalid_reason(session) for cohort in owner.cohorts):
        return _result('requires_user', 'accepted_session_invalid')
    if owner._metric_store.pending_critical_gap() is not None:
        return _result('requires_user', 'accepted_session_invalid')
    if not any(context is not None for context in contexts):
        return _result('wait_next_session', 'missed_session')
    if not all(context is not None for context in contexts):
        return _result('requires_user', 'checkpoint_invalid')
    epoch = owner._metric_store.current_epoch()
    if epoch is None or epoch.status != 'open':
        return _result('requires_user', 'accepted_session_invalid')
    generation = owner._metric_epoch_context
    if epoch.generation_id != generation.generation_id or epoch.generation_commit != generation.generation_commit:
        return _result('requires_user', 'identity_mismatch')
    owner._epoch_id = epoch.epoch_id
    missing_stage = []
    # Validate every book BEFORE any resumed effect. No mixed identity/fresh book
    # is accepted merely because another book already committed execution.
    for cohort in owner.cohorts:
        ledger, executor = cohort['ledger'], cohort['executor']
        if ledger.session_invalid_reason(session):
            return _result('requires_user', 'accepted_session_invalid')
        context = ledger.session_execution_context(session)
        # A later accepted session prevents rewinding the current accounting.
        if ledger.connection.execute('SELECT 1 FROM session_execution_contexts WHERE session > ? LIMIT 1',
                                     (session.isoformat(),)).fetchone():
            return _result('requires_user', 'identity_mismatch')
        executor.validate_bound_context(session, epoch.epoch_id)
        # Health partitions include a horizon suffix; authoritative committee
        # and staging records use the engine's frozen ledger policy identity.
        policy_id = str(cohort['engine'].ar_config.get('paper_ledger', {}).get(
            'policy_id', 'foundation-' + cohort['config'].horizon))
        if not ledger.staging_completed(session, epoch.epoch_id, policy_id):
            missing_stage.append((cohort, policy_id))
    with persisted_only():
        for cohort in owner.cohorts:
            ledger, executor = cohort['ledger'], cohort['executor']
            if not all(ledger.phase_completed(session, phase) for phase in PHASES):
                outcome = executor.execute_open_and_mark(session, epoch.epoch_id,
                    executor.persisted_input_bundle(session), executor.persisted_borrow_rates(session), now)
                if not outcome.valid or outcome.snapshot is None:
                    return _result('requires_user', 'accepted_session_invalid')
        if not missing_stage:
            # Completed replay/reporting must not run outcome or shadow phases.
            return _result('recovered', 'complete') if _completed_owner_valid(owner, session, now) else _result('requires_user', 'checkpoint_invalid')
        if now >= session_open(next_session(session)):
            return _result('wait_next_session', 'missed_staging')
        # Missing model work is a missed sample, never a request to re-infer.
        if any(cohort['ledger'].committee_decision(session, epoch.epoch_id, policy_id) is None
               for cohort, policy_id in missing_stage):
            return _result('wait_next_session', 'checkpoint_missing')
        store, identity = daily_source_store(owner, session.isoformat())
        source = store.load_frozen(identity)
        if not isinstance(source, dict):
            return _result('requires_user', 'checkpoint_invalid')
        digest = hashlib.sha256(store.encode(source, **store.codec_limits).encode()).hexdigest()
        horizons = tuple(sorted({cohort['config'].horizon for cohort in owner.cohorts}))
        if load_screening(owner, session, digest, horizons) is None:
            return _result('requires_user', 'checkpoint_invalid')
        # Deliberately bypass run_governed_execution: it captures/replays due
        # outcomes and may diagnose absent prices. P0 was completed above using
        # only its accepted bundle. Remaining phases consume frozen checkpoints.
        state = DailyRunState(owner=owner, trading_date=session.isoformat(), session=session,
                             processed_at=now, epoch_id=epoch.epoch_id)
        for phase in (partition_daily_replay, run_horizon_screening,
                      run_candidate_reference_validation, run_candidate_volatility_validation):
            early = phase(state)
            if early is not None:
                return _result('requires_user', 'checkpoint_invalid')
        results = stage_daily_results(state)
        if any(row.get('error') for row in results.values()):
            return _result('requires_user', 'checkpoint_invalid')
        if not all(cohort['ledger'].staging_completed(session, epoch.epoch_id, policy_id)
                   for cohort, policy_id in missing_stage):
            return _result('requires_user', 'checkpoint_invalid')
        return _result('recovered', 'complete') if _completed_owner_valid(owner, session, now) else _result('requires_user', 'checkpoint_invalid')


def atomic_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.recovery-')
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(document, handle, sort_keys=True, separators=(',', ':'), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def identity_for(repo: Path, generation: dict, session: str) -> dict:
    """Bind identity to actual checkout/profile, never to environment assertions."""
    from .generation_config import read_generation_config
    date.fromisoformat(session)
    commit = generation['git_commit']
    if not re.fullmatch('[0-9a-f]{40}', commit) or not re.fullmatch('gen_[0-9]+', generation['gen_id']):
        raise ValueError('identity_mismatch')
    from .generation_config import FROZEN_CONFIG_NAME
    worktree = Path(generation['worktree_path'])
    state = Path(generation['state_dir'])
    expected_worktree = repo / '.worktrees' / generation['gen_id']
    expected_state = repo / 'data/generations' / generation['gen_id']
    if worktree != expected_worktree or state != expected_state:
        raise ValueError('identity_mismatch')
    for target in (worktree, state):
        if target.resolve() != target or any(parent.is_symlink() for parent in (target, *target.parents)):
            raise ValueError('identity_mismatch')
    if generation.get('config_file') != str(state / FROZEN_CONFIG_NAME):
        raise ValueError('identity_mismatch')
    actual = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=worktree,
                            check=True, capture_output=True, text=True).stdout.strip()
    changed = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=worktree,
                             check=True, capture_output=True, text=True).stdout.strip()
    common = subprocess.run(['git', 'rev-parse', '--git-common-dir'], cwd=worktree,
                            check=True, capture_output=True, text=True).stdout.strip()
    repo_common = subprocess.run(['git', 'rev-parse', '--git-common-dir'], cwd=repo,
                                 check=True, capture_output=True, text=True).stdout.strip()
    if (actual != commit or changed or (worktree/ common).resolve() != (repo / repo_common).resolve()):
        raise ValueError('identity_mismatch')
    pointer = worktree / '.git'
    admin = Path(subprocess.run(['git','rev-parse','--absolute-git-dir'], cwd=worktree,
        check=True,capture_output=True,text=True).stdout.strip())
    backlink = admin / 'gitdir'
    if (pointer.is_symlink() or not pointer.is_file() or admin.resolve()!=admin
            or admin.parent != (repo/repo_common).resolve()/'worktrees'
            or backlink.is_symlink() or not backlink.is_file()
            or backlink.read_text().strip()!=str(pointer)):
        raise ValueError('identity_mismatch')
    config_sha = generation.get('config_sha256')
    config_file = generation.get('config_file')
    # Frozen profiles are required for unattended recovery; legacy implicit
    # environment/default configuration needs an explicit operator migration.
    if not config_sha or not config_file:
        raise ValueError('identity_mismatch')
    read_generation_config(config_file, config_sha)
    return {'generation_id': generation['gen_id'], 'generation_commit': commit,
            'config_sha256': config_sha, 'session': session,
            'state_dir': str(Path(generation['state_dir']).resolve()),
            'worktree_path': str(worktree)}


def incident_key(identity: dict) -> str:
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def load_journal(path: Path) -> dict:
    if not path.exists():
        return {'version': VERSION, 'schema_version': 1, 'incidents': {}}
    if path.stat().st_size > 262144:
        raise ValueError('state_corrupt')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('state_corrupt')
            value[key] = item
        return value
    document = json.loads(path.read_text(), object_pairs_hook=unique)
    if (set(document) != {'version', 'schema_version', 'incidents'} or document['version'] != VERSION
            or document['schema_version'] != 1 or type(document['incidents']) is not dict
            or len(document['incidents']) > MAX_INCIDENTS):
        raise ValueError('state_corrupt')
    for key, row in document['incidents'].items():
        if (not isinstance(row, dict) or key != incident_key(row['identity'])
                or row['disposition'] not in DISPOSITIONS or row['reason_code'] not in REASONS
                or type(row['attempt_count']) is not int or not 0 <= row['attempt_count'] <= MAX_ATTEMPTS
                or type(row['running']) is not bool):
            raise ValueError('state_corrupt')
        if 'launch_mode' in row or 'launch_intent' in row:
            validate_prospective_intent(row)
    return document


def validate_prospective_intent(row):
    """Finite additive one-shot marker; legacy records keep their old contract."""
    fields = {'identity', 'disposition', 'reason_code', 'attempt_count', 'next_attempt_at',
              'running', 'daily_started', 'updated_at', 'launch_mode', 'launch_intent'}
    try:
        intent, identity = row['launch_intent'], row['identity']
        if (set(row) != fields or row['launch_mode'] != PROSPECTIVE_LAUNCH_MODE
                or type(intent) is not dict or set(intent) != {'version', 'generation_created_at', 'started_at'}
                or intent['version'] != 'prospective-native-launch-v1'
                or type(identity) is not dict or set(identity) != {'generation_id', 'generation_commit',
                    'config_sha256', 'session', 'state_dir', 'worktree_path'}
                or type(row['daily_started']) is not bool or row['daily_started'] is not True
                or type(row['running']) is not bool or type(row['attempt_count']) is not int
                or row['attempt_count'] != 0):
            raise ValueError()
        created = intent['generation_created_at']
        if type(created) is not str or len(created) > 64 or 'T' not in created:
            raise ValueError()
        datetime.fromisoformat(created)  # Preserve native naive creation identity without inventing a zone.
        started = _utc(datetime.fromisoformat(intent['started_at']))
        updated = _utc(datetime.fromisoformat(row['updated_at']))
        if (updated < started or (row['running'] and
                (row['disposition'] != 'retrying' or row['reason_code'] != 'interrupted'
                 or row['next_attempt_at'] != intent['started_at'] or updated != started))
                or (not row['running'] and
                    (row['disposition'] == 'retrying' or row['next_attempt_at'] is not None))):
            raise ValueError()
        if (not re.fullmatch(r'gen_[0-9]{3,}', identity['generation_id'])
                or not re.fullmatch('[0-9a-f]{40}', identity['generation_commit'])
                or not re.fullmatch('[0-9a-f]{64}', identity['config_sha256'])
                or date.fromisoformat(identity['session']).isoformat() != identity['session']):
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('state_corrupt') from None


def new_incident(identity: dict, now: datetime) -> dict:
    return {'identity': identity, 'disposition': 'retrying', 'reason_code': 'interrupted',
            'attempt_count': 0, 'next_attempt_at': now.isoformat(), 'running': False,
            'daily_started': False, 'updated_at': now.isoformat()}


def finish_attempt(row: dict, result: dict, now: datetime) -> None:
    disposition, reason = result['disposition'], result['reason_code']
    if disposition not in DISPOSITIONS or reason not in REASONS:
        raise ValueError('invalid recovery worker result')
    if disposition == 'retrying' and row['attempt_count'] >= MAX_ATTEMPTS:
        disposition, reason = 'requires_user', 'attempts_exhausted'
    row.update(disposition=disposition, reason_code=reason, running=False, updated_at=now.isoformat(),
        next_attempt_at=(now + timedelta(seconds=BACKOFF_SECONDS[max(0, min(row['attempt_count'] - 1, 2))])).isoformat()
                        if disposition == 'retrying' else None)


def status_projection(journal: dict, now: datetime, *, active_ids=None) -> dict:
    incidents = []
    for key, row in sorted(journal['incidents'].items()):
        identity = row['identity']
        if active_ids is not None and identity['generation_id'] not in active_ids:
            continue
        incidents.append({**{field: identity[field] for field in
            ('generation_id', 'generation_commit', 'config_sha256', 'session')},
            'incident_id': key, 'disposition': row['disposition'], 'reason_code': row['reason_code'],
            'attempt_count': row['attempt_count'], 'next_attempt_at': row['next_attempt_at']})
    latest = {}
    for row in incidents:
        latest[row['generation_id']] = max(latest.get(row['generation_id'], ''), row['session'])
    dispositions = {row['disposition'] for row in incidents if row['disposition'] in {'requires_user', 'retrying'}
                    or row['session'] == latest[row['generation_id']]}
    status = ('operator_required' if 'requires_user' in dispositions else
              'recovering' if 'retrying' in dispositions else
              'waiting_next_session' if 'wait_next_session' in dispositions else 'healthy')
    return {'version': VERSION, 'schema_version': 1, 'updated_at': _utc(now).isoformat(),
            'status': status, 'controller_reason_code': None, 'incidents': incidents[-MAX_INCIDENTS:]}


def bounded_process(command: list[str], *, cwd: Path, env: dict, timeout: int, pass_fds=(), heartbeat=None) -> tuple[int, str]:
    """Drain a bounded output tail; kill/reap the entire group on every failure."""
    import selectors
    import time
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=True, pass_fds=pass_fds)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline, next_heartbeat = time.monotonic() + timeout, time.monotonic() + 60
    tail = bytearray()
    code, finished = None, False
    try:
        while selector.get_map() or process.poll() is None:
            current = time.monotonic()
            if current >= deadline:
                code = 124
                break
            for key, _ in selector.select(timeout=min(1, deadline-current)):
                chunk = os.read(key.fileobj.fileno(), 16384)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    tail.extend(chunk)
                    if len(tail) > 65536:
                        del tail[:-65536]
            if heartbeat is not None and time.monotonic() >= next_heartbeat:
                heartbeat()
                next_heartbeat = time.monotonic() + 60
        if code != 124:
            code = process.wait(timeout=max(.001, deadline-time.monotonic()))
            finished = True
    finally:
        selector.close()
        if not finished:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # A shell leader can exit before a child forwards termination into
            # its separately owned preflight group. Give the entire outer
            # group the existing grace, reaping the leader while we wait.
            grace_deadline = time.monotonic() + 10
            while True:
                process.poll()
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    # macOS can report EPERM transiently for a dying group.
                    # Unknown liveness must retain, not shorten, its grace.
                    pass
                remaining = grace_deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(.05, remaining))
            # The group may still contain a child even if its leader exited.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
        process.stdout.close()
    return code, tail.decode('utf-8', errors='replace')


def _hash_document(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        allow_nan=False).encode()).hexdigest()


def _completed_owner_valid(owner, session, now) -> bool:
    from .daily_pipeline import DailyRunState, partition_daily_replay
    state = DailyRunState(owner=owner, trading_date=session.isoformat(), session=session,
                         processed_at=now, epoch_id=owner._epoch_id)
    with persisted_only():
        early = partition_daily_replay(state)
    return early is None and len(state.completed) == len(owner.cohorts) and not state.stage_only and not state.execution_needed


def session_recovery_proof(state, generation, session, epoch_id):
    """Read-only canonical proof; constructors never initialize/migrate state."""
    import sqlite3
    from .session_executor import PHASES
    from .source_inputs import configuration_fingerprint
    from .operational_report import EXPECTED_COHORTS
    state = Path(state)
    proof = {'books': {}, 'accepted_inputs': {}, 'metrics': {}}
    for name in EXPECTED_COHORTS:
        path = state/name/'portfolio.db'
        if path.is_symlink() or not path.is_file():
            raise ValueError('recovery book missing')
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            context = connection.execute('SELECT * FROM session_execution_contexts WHERE session=?', (session,)).fetchall()
            phases = connection.execute('SELECT * FROM session_phases WHERE session=? ORDER BY phase', (session,)).fetchall()
            staging = connection.execute('SELECT * FROM staging_runs WHERE session=?', (session,)).fetchall()
            snapshot = connection.execute('SELECT * FROM account_snapshots WHERE session=?', (session,)).fetchall()
            committee = connection.execute('SELECT * FROM committee_decisions WHERE session=?', (session,)).fetchall()
            if not (len(context)==len(staging)==len(snapshot)==len(committee)==1 and len(phases)==len(PHASES)):
                raise ValueError('recovery book incomplete')
            context, staging, snapshot, committee = (dict(rows[0]) for rows in (context,staging,snapshot,committee))
            if (context['cohort_id']!=name or context['epoch_id']!=epoch_id or staging['cohort_id']!=name
                    or staging['epoch_id']!=epoch_id or snapshot['epoch_id']!=epoch_id
                    or not snapshot['valid'] or snapshot['invalid_reason'] or not staging['completed_at']):
                raise ValueError('recovery book identity invalid')
            by_phase = {row['phase']:dict(row) for row in phases}
            expected = context['starting_state_digest']
            for phase in PHASES:
                row = by_phase.get(phase)
                if (not row or row['cohort_id']!=name or not row['completed_at']
                        or row['pre_state_digest']!=expected or not row['post_state_digest']):
                    raise ValueError('recovery phase proof invalid')
                expected = row['post_state_digest']
            economics = {}
            for table in ('fills','cash_events','marks','borrow_accruals','financing_accruals',
                          'dividend_events','fee_events','benchmark_observations','session_runs',
                          'session_invalidations'):
                rows = connection.execute('SELECT * FROM '+table+' WHERE session=? ORDER BY 1 LIMIT 10001',
                                          (session,)).fetchall()
                if len(rows)>10000:
                    raise ValueError('recovery economic proof exceeds bounds')
                economics[table] = _hash_document([dict(row) for row in rows])
            opening = connection.execute("SELECT * FROM cash_events WHERE session IS NULL ORDER BY cash_event_id LIMIT 10001").fetchall()
            if len(opening)>10000:
                raise ValueError('recovery opening proof exceeds bounds')
            economics['opening_cash_events'] = _hash_document([dict(row) for row in opening])
            proof['books'][name] = {
                'economics':economics,
                'execution_input_digest':context['input_digest'], 'config_digest':context['config_digest'],
                'borrow_digest':context['borrow_digest'], 'provenance_digest':context['provenance_digest'],
                'context_digest':_hash_document(context), 'phase_chain_digest':_hash_document(by_phase),
                'staging_run_id':staging['staging_run_id'], 'staging_post_state_digest':staging['post_state_digest'],
                'staging_digest':_hash_document(staging), 'snapshot_digest':_hash_document(snapshot),
                'committee_digest':_hash_document(committee)}
    with sqlite3.connect(f'file:{state / "metrics_v2.sqlite3"}?mode=ro',uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        for table in ('strategy_health','candidate_bar_recoveries','governed_bar_recoveries',
                      'candidate_signal_identity_bindings','candidate_input_issues'):
            rows = connection.execute('SELECT * FROM '+table+' WHERE epoch_id=? AND session=? ORDER BY 1 LIMIT 10001',
                                      (epoch_id,session)).fetchall()
            if len(rows)>10000:
                raise ValueError('recovery metric proof exceeds bounds')
            proof['metrics'][table] = _hash_document([dict(row) for row in rows])
    slot = configuration_fingerprint({'generation':generation, 'session':session})+'.json'
    source_root = state/'source_inputs'
    paths = [source_root/slot, *sorted(source_root.glob('*/'+slot))]
    if len(paths)>16 or not (source_root/'screening'/slot).is_file() or not (source_root/'decision_inputs'/slot).is_file():
        raise ValueError('recovery accepted checkpoints missing')
    for path in paths:
        if not path.is_file() or path.is_symlink() or path.resolve() != path or path.stat().st_size>64*1024*1024:
            raise ValueError('recovery accepted input invalid')
        proof['accepted_inputs'][str(path.relative_to(state))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return proof


def write_recovery_receipt(owner, session: date, *, now=None, configuration_sha256=None):
    """Distinct completion proof; preserves original attempts/history unchanged."""
    now = _utc(now or datetime.now(timezone.utc))
    configuration_sha256 = configuration_sha256 or os.environ.get('EVENTEDGE_GENERATION_CONFIG_SHA256')
    if not isinstance(configuration_sha256,str) or not re.fullmatch('[0-9a-f]{64}',configuration_sha256):
        raise ValueError('recovery requires frozen configuration')
    if not _completed_owner_valid(owner,session,now):
        raise ValueError('recovery completed prospective proof invalid')
    generation = owner._metric_epoch_context
    state = Path(owner._base_config['autoresearch']['state_dir'])
    proof = session_recovery_proof(state,generation.generation_id,session.isoformat(),owner._epoch_id)
    document = {'version':VERSION,'generation_id':generation.generation_id,
        'generation_commit':generation.generation_commit,'session':session.isoformat(),
        'configuration_sha256':configuration_sha256,'epoch_id':owner._epoch_id,
        'completed_at':now.isoformat(),'original_interruption_preserved':True,
        'proof':proof,'proof_sha256':_hash_document(proof)}
    path = state/'runtime_recovery'/f'{session.isoformat()}.json'
    # First successful receipt owns the original recovery time/proof. A later
    # replay validates it rather than overwriting its evidence.
    if path.exists() or path.is_symlink():
        validate_recovery_receipt(state,generation.generation_id,session.isoformat(),generation.generation_commit,
                                  expected_configuration=configuration_sha256)
    else:
        atomic_json(path,document)
    return path


def validate_recovery_receipt(state, generation, session, commit, expected_configuration=None):
    """Validate distinct completion proof against current immutable evidence.

    None means no recovery receipt; all malformed or changed proof raises. The
    caller retains ordinary source/health/epoch validation and original attempts.
    """
    state = Path(state)
    path = state/'runtime_recovery'/f'{session}.json'
    if path.is_symlink() or path.resolve() != path:
        raise ValueError('recovery receipt invalid')
    if not path.exists():
        return None
    if path.stat().st_size>262144:
        raise ValueError('recovery receipt invalid')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('recovery receipt duplicate field')
            value[key] = item
        return value
    try:
        document = json.loads(path.read_text(), object_pairs_hook=unique)
    except (RecursionError, UnicodeError) as error:
        raise ValueError('recovery receipt invalid') from error
    fields = {'version','generation_id','generation_commit','session','configuration_sha256','epoch_id',
              'completed_at','original_interruption_preserved','proof','proof_sha256'}
    if (set(document)!=fields or document['version']!=VERSION or document['generation_id']!=generation
            or document['generation_commit']!=commit or document['session']!=session
            or document['original_interruption_preserved'] is not True
            or not re.fullmatch('[0-9a-f]{64}', document['configuration_sha256'])
            or expected_configuration is None or document['configuration_sha256']!=expected_configuration
            or _utc(datetime.fromisoformat(document['completed_at']))>datetime.now(timezone.utc)):
        raise ValueError('recovery receipt identity invalid')
    from .generation_config import FROZEN_CONFIG_NAME, read_generation_config
    read_generation_config(state/FROZEN_CONFIG_NAME, document['configuration_sha256'])
    proof = session_recovery_proof(state,generation,session,document['epoch_id'])
    if document['proof']!=proof or document['proof_sha256']!=_hash_document(proof):
        raise ValueError('recovery receipt proof conflict')
    return {key:document[key] for key in ('version','epoch_id','completed_at','proof_sha256',
            'original_interruption_preserved','configuration_sha256')} | {'books_count':len(proof['books'])}
