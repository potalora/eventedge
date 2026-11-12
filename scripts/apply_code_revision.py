#!/usr/bin/env python3
"""Apply one reviewed commit-only repair and assess an explicitly missed sample.

Default mode is a deterministic proof-bound preview. No providers, models,
historical workers, ledger initializers or service commands are invoked.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.retire_congress import _git, _verify_worktree, _protected, _write_atomic, _sync, _json
from tradingagents.strategies.orchestration.generation_manager import GenerationManager
from tradingagents.strategies.orchestration.operator_assessment import (
    VERSION as ASSESSMENT_VERSION, MAX_RECEIPT, read_file, digest, session_proof,
    validate_operator_assessment, database_copy,
)
from tradingagents.strategies.orchestration.operating_contract import ROSTER
from tradingagents.strategies.orchestration.runtime_lock import canonical_runtime_lock_path, runtime_lock
from tradingagents.strategies.orchestration.runtime_recovery import (
    load_journal, incident_key, identity_for, status_projection,
)
from tradingagents.strategies.orchestration.source_retirement import (
    CODE_REVISION_VERSION, CODE_REVISION_REASON, validate_generation_retirement, validate_code_revision,
)
from tradingagents.strategies.orchestration.trading_calendar import is_session, next_session, session_open

JOURNAL_NAME = '.pending-code-revision.json'
MAX_JOURNAL = 64 * 1024 * 1024


def _invalid(reason):
    raise ValueError('code_revision_' + reason)


def _bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()


def _transition(before, key, assessment_sha, prepared_at):
    after = deepcopy(before)
    after['incidents'][key].update(disposition='wait_next_session',reason_code='missed_staging',
        next_attempt_at=None,running=False,updated_at=prepared_at,operator_assessment_sha256=assessment_sha)
    return after


def _status(repo, journal, now, rows):
    active_ids={value['gen_id'] for value in rows if value['status']=='active'}
    return {**status_projection(journal,now,active_ids=active_ids),'controller_reason_code':None}


def _proposal(repo, original, parameters):
    document = _json(original)
    GenerationManager._validate_manifest(document)
    matches = [row for row in document['generations'] if row['gen_id'] == parameters['generation']]
    if len(matches) != 1:
        _invalid('generation_identity')
    old = matches[0]
    binding = validate_generation_retirement(old, repo=repo)
    if (binding is None or old.get('code_revision') is not None or old['status'] != 'active'
            or old['git_commit'] != parameters['old_commit']
            or old['config_sha256'] != parameters['profile_sha256']
            or parameters['effective_session'] <= binding['effective_session']):
        _invalid('old_identity')
    new = deepcopy(old)
    new['git_commit'] = parameters['commit']
    new['code_revision'] = {
        'version': CODE_REVISION_VERSION, 'reason': CODE_REVISION_REASON,
        'effective_session': parameters['effective_session'], 'previous_commit': old['git_commit'],
        'commit': parameters['commit'],
        'previous_worktree_path': str(repo/'.worktrees'/(old['gen_id']+'-before-code-revision')),
        'config_file': old['config_file'], 'config_sha256': old['config_sha256'],
    }
    validate_code_revision(new, repo=repo)
    document['generations'][document['generations'].index(old)] = new
    return old, new, _bytes(document)


def _retained(repo, identity):
    day, generation = identity['session'], identity['generation_id']
    stem = f'docs/reports/{day}-{generation}-operational-report'
    raw = read_file(repo/(stem+'.json'), MAX_RECEIPT)
    report = _json(raw)
    from tradingagents.strategies.orchestration.operational_report import render_operational_report
    markdown = read_file(repo/(stem+'.md'), MAX_RECEIPT)
    if (report['generation_id'] != generation or report['generation_commit'] != identity['generation_commit']
            or report['session'] != day or report.get('profile_sha256') != identity['config_sha256']
            or report.get('outcome') != 'failed' or report.get('accounting_valid') is not True
            or report.get('staging_complete') is not False
            or markdown != render_operational_report(report).encode()):
        _invalid('failed_report_identity')
    files = {stem+'.json': raw, stem+'.md': markdown}
    failures = 0
    for item in report.get('attempts', []):
        name = item.get('attempt_id')
        if type(name) is not str or not re.fullmatch('[A-Za-z0-9_.-]{1,200}[.]json', name):
            _invalid('attempt_identity')
        relative = 'data/logs/run_attempts/' + name
        body = read_file(repo/relative, MAX_RECEIPT)
        attempt = _json(body)
        if (attempt.get('generation_id') != generation or attempt.get('generation_commit') != identity['generation_commit']
                or attempt.get('requested_session') != day):
            _invalid('attempt_identity')
        if attempt.get('action') == 'daily':
            if (attempt.get('process_return_code') != 1 or attempt.get('process_status') != 'completed'
                    or 'source document exceeds structural bounds' not in attempt.get('stderr', '')
                    or 'freeze_screening' not in attempt.get('stderr', '')):
                _invalid('different_daily_failure')
            failures += 1
        files[relative] = body
    if failures != 1:
        _invalid('failed_daily_missing_or_ambiguous')
    return {path: {'sha256': hashlib.sha256(body).hexdigest(), 'text': body.decode('utf-8')} for path, body in files.items()}


def _protected_files(row):
    result = _protected(row)
    paths = [Path(row['source_retirement']['previous_config_file']), Path(row['state_dir'])/'metrics_v2.sqlite3']
    previous_journal = Path(row['state_dir'])/'.pending-congress-retirement.json'
    if previous_journal.exists() or previous_journal.is_symlink():
        paths.append(previous_journal)
    for path in paths:
        result[str(path)] = hashlib.sha256(read_file(path)).hexdigest()
        if path.suffix in {'.db', '.sqlite3'}:
            for suffix in ('-wal', '-shm'):
                adjacent = Path(str(path)+suffix)
                if adjacent.exists() or adjacent.is_symlink():
                    result[str(adjacent)] = hashlib.sha256(read_file(adjacent)).hexdigest()
    return result


def _boundary(repo, row, effective):
    for item in row['run_history']:
        day = item.get('date', item.get('session'))
        if type(day) is not str or date.fromisoformat(day).isoformat() != day or day >= effective:
            _invalid('accepted_boundary')
    for name in ROSTER:
        with database_copy(Path(row['state_dir'])/name/'portfolio.db') as (connection, _):
            for table in ('session_execution_contexts', 'session_phases', 'staging_runs', 'committee_decisions', 'fills'):
                if connection.execute('SELECT 1 FROM '+table+' WHERE session>=? LIMIT 1', (effective,)).fetchone():
                    _invalid('accepted_boundary')
    with database_copy(Path(row['state_dir'])/'metrics_v2.sqlite3') as (connection,_):
        for (raw,) in connection.execute('SELECT payload_json FROM metric_epochs'):
            day=_json(raw)['start_session']
            if type(day) is not str or date.fromisoformat(day).isoformat()!=day or day >= effective:
                _invalid('accepted_epoch_boundary')
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore, FULL_FILING_MAX_BYTES, FULL_FILING_MAX_NODES
    count, total = 0, 0
    def walk_error(error):
        raise error
    root=Path(row['state_dir'])/'source_inputs'
    for ancestor in (root,*root.parents):
        if not stat.S_ISDIR(ancestor.lstat().st_mode): _invalid('unsafe_accepted_root')
    for directory, directories, files in os.walk(root,followlinks=False,onerror=walk_error):
        for name in directories:
            if not stat.S_ISDIR((Path(directory)/name).lstat().st_mode): _invalid('unsafe_accepted_directory')
        for name in files:
            path=Path(directory)/name
            count += 1
            if count > 4096 or not stat.S_ISREG(path.lstat().st_mode): _invalid('accepted_input_bounds')
            if path.suffix != '.json':
                _invalid('unexpected_accepted_input')
            body = read_file(path, FULL_FILING_MAX_BYTES); total += len(body)
            if total > 1024*1024*1024:
                _invalid('accepted_input_bounds')
            envelope = SourceInputStore.decode(body.decode(), max_bytes=FULL_FILING_MAX_BYTES, max_nodes=FULL_FILING_MAX_NODES)
            day=envelope.get('identity',{}).get('session')
            if (type(day) is not str or date.fromisoformat(day).isoformat()!=day
                    or envelope.get('identity', {}).get('generation') != row['gen_id'] or day >= effective):
                _invalid('accepted_boundary')


def apply_code_revision(*, repo, generation, effective_session, old_commit, profile_sha256,
                        commit, resolve_missed_staging, expected_plan_sha256=None,
                        apply=False, scheduler_quiesced=False, now=None):
    repo = Path(repo).absolute()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        _invalid('aware_clock_required')
    if repo.resolve() != repo or not (repo/'.git').is_dir() or _git(repo, 'rev-parse', '--show-toplevel') != str(repo):
        _invalid('canonical_repository')
    for value, width in ((old_commit,40), (commit,40), (profile_sha256,64), (resolve_missed_staging,64)):
        if type(value) is not str or not re.fullmatch('[a-f0-9]{%d}' % width, value):
            _invalid('invalid_pin')
    if (not re.fullmatch('gen_[0-9]{3,}', generation) or date.fromisoformat(effective_session).isoformat() != effective_session
            or not is_session(date.fromisoformat(effective_session)) or commit == old_commit):
        _invalid('invalid_boundary')
    if apply and (not scheduler_quiesced or expected_plan_sha256 is None):
        _invalid('approval_pins_required')
    parameters = dict(generation=generation, effective_session=effective_session, old_commit=old_commit,
                      profile_sha256=profile_sha256, commit=commit, resolve_missed_staging=resolve_missed_staging)
    controller = repo/'data/runtime_recovery'
    lock, controller_lock = canonical_runtime_lock_path(repo), controller/'controller.lock'
    if not lock.is_file() or not controller_lock.is_file():
        _invalid('existing_locks_required')
    with runtime_lock(controller_lock, exclusive=True), runtime_lock(lock, exclusive=True):
        manifest_path, runtime_path = repo/'data/generations/manifest.json', controller/'journal.json'
        pending_path = repo/'data/generations'/generation/JOURNAL_NAME
        pending = None
        manifest_raw = read_file(manifest_path, 2*1024*1024)
        runtime_raw = read_file(runtime_path, 262144)
        load_journal(runtime_path)  # Validate every incident before any mutation.
        if pending_path.exists() or pending_path.is_symlink():
            raw = read_file(pending_path, MAX_JOURNAL); pending = _json(raw)
            info = pending_path.lstat()
            if (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid()
                    or set(pending) != {'version','parameters','plan','plan_sha256','manifest_before','manifest_after',
                        'journal_before','journal_after','assessment','prepared_at'}
                    or pending['version'] != CODE_REVISION_VERSION or pending['parameters'] != parameters
                    or pending['plan_sha256'] != digest(pending['plan'])):
                _invalid('foreign_journal')
        original_manifest = pending['manifest_before'].encode() if pending else manifest_raw
        old, new, proposed = _proposal(repo, original_manifest.decode(), parameters)
        original_journal = pending['journal_before'].encode() if pending else runtime_raw
        journal_before = _json(original_journal)
        record = journal_before['incidents'].get(resolve_missed_staging)
        if (not isinstance(record,dict) or incident_key(record['identity']) != resolve_missed_staging
                or record['identity']['generation_id'] != generation
                or record['identity']['generation_commit'] != old_commit or record['identity']['config_sha256'] != profile_sha256
                or record['disposition'] != 'requires_user' or record['reason_code'] != 'checkpoint_invalid'
                or record['running'] is not False or record['daily_started'] is not True):
            _invalid('incident_identity')
        eligible = session_open(next_session(date.fromisoformat(record['identity']['session'])))
        if now < eligible or effective_session <= record['identity']['session']:
            _invalid('prospective_cutoff_not_passed')
        active, archive = Path(old['worktree_path']), Path(new['code_revision']['previous_worktree_path'])
        _verify_worktree(repo,Path(old['source_retirement']['previous_worktree_path']),old['source_retirement']['previous_commit'])
        receipt_path = controller/'operator_assessments'/(resolve_missed_staging+'.json')
        _git(repo,'merge-base','--is-ancestor',old_commit,commit)
        if (_git(repo,'diff','--name-only','-z','HEAD','--')
                or _git(repo,'diff','--cached','--name-only','-z','HEAD','--')
                or _git(repo,'ls-files','--others','--exclude-standard','-z','--','.',':(top,exclude)data/**')
                or _git(repo,'rev-parse','HEAD') not in {old_commit,commit}):
            _invalid('host_checkout_changed')
        current = _json(manifest_raw)
        GenerationManager._validate_manifest(current)
        normalized = deepcopy(current)
        for value in normalized['generations']:
            if value['gen_id']==generation:
                if value['run_history'][:len(new['run_history'])] != new['run_history']:
                    _invalid('original_history_changed')
                value['run_history']=deepcopy(new['run_history'])
        published = normalized == _json(proposed)
        if manifest_raw != original_manifest and not published:
            _invalid('manifest_changed')
        if pending is not None and (pending['manifest_after'].encode()!=proposed or pending['manifest_before'].encode()!=original_manifest):
            _invalid('foreign_journal')
        if pending is not None:
            prepared=datetime.fromisoformat(pending['prepared_at'])
            if prepared.tzinfo is None or not eligible<=prepared<=now:
                _invalid('owned_transaction_clock')
            expected_after=_bytes(_transition(journal_before,resolve_missed_staging,pending['plan']['assessment_sha256'],pending['prepared_at']))
            if pending['journal_after'].encode()!=expected_after:
                _invalid('runtime_postimage_changed')
        if published and pending is not None:
            live = load_journal(runtime_path)
            transitioned = live['incidents'].get(resolve_missed_staging,{})
            if transitioned.get('operator_assessment_sha256') == pending['plan']['assessment_sha256']:
                _verify_worktree(repo,archive,old_commit); _verify_worktree(repo,active,commit)
                validate_operator_assessment(repo,transitioned,pending['plan']['assessment_sha256'])
                if _git(repo,'rev-parse','HEAD') != commit: _invalid('host_commit_changed')
                if expected_plan_sha256 is not None and expected_plan_sha256 != pending['plan_sha256']: _invalid('plan_pin')
                wanted=_status(repo,live,now,current['generations'])
                status_path=controller/'status.json'
                existing=_json(read_file(status_path,262144)) if status_path.exists() else {}
                same={key:value for key,value in existing.items() if key!='updated_at'}=={key:value for key,value in wanted.items() if key!='updated_at'}
                if apply and not same: _write_atomic(status_path,_bytes(wanted))
                return dict(changed=bool(apply and not same),already_applied=bool(same or apply),plan_sha256=pending['plan_sha256'],
                            assessment_sha256=pending['plan']['assessment_sha256'],commit=commit)
        if runtime_raw != original_journal:
            _invalid('runtime_journal_changed')
        if any(value['running'] or (value['identity']['generation_id']==generation
                and key!=resolve_missed_staging and value['disposition'] not in {'recovered','wait_next_session'})
                for key,value in journal_before['incidents'].items()):
            _invalid('unfinished_runtime')
        before = _protected_files(old)
        _boundary(repo,old,effective_session)
        if archive.exists():
            if pending is None: _invalid('foreign_archive')
            _verify_worktree(repo,archive,old_commit)
            if active.exists(): _verify_worktree(repo,active,commit)
        else:
            _verify_worktree(repo,active,old_commit)
        selected_for_proof = new if published else old
        proof = session_proof(repo,selected_for_proof,record['identity'],initial=True)
        retained = _retained(repo,record['identity'])
        assessment = {'version':ASSESSMENT_VERSION,'incident_id':resolve_missed_staging,
            'identity':record['identity'],'incident_before':record,'manifest_before':original_manifest.decode(),
            'journal_before':original_journal.decode(),'eligible_from':eligible.isoformat(),
            'code_revision':new['code_revision'],'retained_files':retained,'proof':proof}
        assessment_body = _bytes(assessment)
        if len(assessment_body)>MAX_RECEIPT: _invalid('assessment_bounds')
        assessment_sha = hashlib.sha256(assessment_body).hexdigest()
        plan = {'parameters':parameters,'manifest_sha256':hashlib.sha256(original_manifest).hexdigest(),
            'runtime_journal_sha256':hashlib.sha256(original_journal).hexdigest(),'protected':before,
            'assessment_sha256':assessment_sha,'manifest_after_sha256':hashlib.sha256(proposed).hexdigest()}
        plan_sha = digest(plan)
        if expected_plan_sha256 is not None and expected_plan_sha256 != plan_sha: _invalid('plan_pin')
        if pending is not None and (pending['plan']!=plan or pending['assessment'].encode()!=assessment_body):
            _invalid('owned_transaction_changed')
        if _protected_files(old)!=before: _invalid('protected_state_changed')
        result = dict(changed=False,already_applied=False,plan_sha256=plan_sha,assessment_sha256=assessment_sha,commit=commit)
        if not apply: return result
        if pending is None:
            if receipt_path.exists() or receipt_path.is_symlink(): _invalid('foreign_assessment')
            after = _transition(journal_before,resolve_missed_staging,assessment_sha,now.isoformat())
            pending = {'version':CODE_REVISION_VERSION,'parameters':parameters,'plan':plan,'plan_sha256':plan_sha,
                'manifest_before':original_manifest.decode(),'manifest_after':proposed.decode(),
                'journal_before':original_journal.decode(),'journal_after':_bytes(after).decode(),
                'assessment':assessment_body.decode(),'prepared_at':now.isoformat()}
            pending_body=_bytes(pending)
            if len(pending_body)>MAX_JOURNAL: _invalid('journal_bounds')
            _write_atomic(pending_path,pending_body,exclusive=True)
        receipt_path.parent.mkdir(mode=0o700,exist_ok=True)
        if receipt_path.exists():
            if read_file(receipt_path,MAX_RECEIPT)!=assessment_body: _invalid('foreign_assessment')
        else: _write_atomic(receipt_path,assessment_body,exclusive=True)
        if not archive.exists():
            _git(repo,'worktree','move',str(active),str(archive));_sync(archive.parent)
        if not active.exists():
            _git(repo,'worktree','add','--detach',str(active),commit);_sync(active.parent)
        if _git(repo,'rev-parse','HEAD')!=commit: _git(repo,'checkout','--detach',commit)
        _verify_worktree(repo,archive,old_commit);_verify_worktree(repo,active,commit)
        if _protected_files(old)!=before or read_file(runtime_path,262144)!=original_journal: _invalid('protected_state_changed')
        if not published:
            if read_file(manifest_path,2*1024*1024)!=original_manifest: _invalid('manifest_changed')
            _write_atomic(manifest_path,proposed)
        _write_atomic(runtime_path,pending['journal_after'].encode())
        live=load_journal(runtime_path)
        validate_operator_assessment(repo,live['incidents'][resolve_missed_staging],assessment_sha)
        status=_status(repo,live,now,current['generations'])
        _write_atomic(controller/'status.json',_bytes(status))
        return {**result,'changed':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--generation',required=True)
    parser.add_argument('--effective-session',required=True)
    parser.add_argument('--expected-old-commit',dest='old_commit',required=True)
    parser.add_argument('--expected-profile-sha256',dest='profile_sha256',required=True)
    parser.add_argument('--commit',required=True)
    parser.add_argument('--resolve-missed-staging',required=True)
    parser.add_argument('--expected-plan-sha256')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--scheduler-quiesced',action='store_true')
    try: result=apply_code_revision(**vars(parser.parse_args()))
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
        print(type(error).__name__+': '+str(error),file=sys.stderr);return 1
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':
    raise SystemExit(main())
