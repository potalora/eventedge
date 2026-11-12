"""Real sixteen-book phases and Git custody with fixture-only external IO."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest

from scheduled_acceptance.harness import create_harness, GENERATION


def _git(harness, *args):
    return subprocess.run(['git','-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false',
        '-c','user.name=Offline fixture','-c','user.email=fixture@example.invalid',*args],
        cwd=harness.repo,env=harness.env,check=True,capture_output=True,text=True).stdout.strip()


@pytest.fixture(scope='module')
def failed_sample(tmp_path_factory):
    from tradingagents.strategies.orchestration.source_retirement_profile import make_retired_profile
    h=create_harness(tmp_path_factory.mktemp('revision-real-books'),profile_variant='stabilization_v3')
    manifest=h.repo/'data/generations/manifest.json'
    document=json.loads(manifest.read_text());row=document['generations'][0]
    previous=h.commit
    original=h.profile.read_text();old_hash=hashlib.sha256(original.encode()).hexdigest()
    retired=make_retired_profile(original,'2026-10-14')
    profile=h.state/'generation-config-congress-retired.json'
    profile.write_text(json.dumps(retired,indent=2)+'\n');profile_hash=hashlib.sha256(profile.read_bytes()).hexdigest()
    _git(h,'commit','--allow-empty','-m','Fixture retirement code')
    current=_git(h,'rev-parse','HEAD')
    archive=h.repo/'.worktrees'/(GENERATION+'-before-congress-retirement')
    _git(h,'worktree','move',row['worktree_path'],str(archive))
    _git(h,'worktree','add','--detach',row['worktree_path'],current)
    row.update(git_commit=current,config_file=str(profile),config_sha256=profile_hash)
    row['source_retirement']={'version':'congress-audit-retirement-v1','effective_session':'2026-10-14',
        'previous_commit':previous,'previous_config_file':str(h.profile),'previous_config_sha256':old_hash,
        'previous_worktree_path':str(archive),'commit':current,'config_file':str(profile),'config_sha256':profile_hash}
    manifest.write_text(json.dumps(document,indent=2)+'\n');h.commit=current
    first=h.run('2026-10-14T22:00:00+00:00')
    assert first.returncode==0,first.stdout+first.stderr
    # This installs a fault only in the disposable venv after its transport
    # fixture bootstrap. The genuine freeze_screening/codec raises afterwards.
    site=next((h.repo/'.venv/lib').glob('python*/site-packages'))
    program="""import os,json
if os.environ.get('EVENTEDGE_ACCEPTANCE_SCENARIO') and json.load(open(os.environ['EVENTEDGE_ACCEPTANCE_SCENARIO'])).get('fault')=='screen_codec_capacity':
 from tradingagents.strategies.orchestration import research_screening
 _freeze=research_screening.freeze_screening
 def _fail_at_publication(*args,**kwargs):
  original=research_screening.screening_codec_limits
  research_screening.screening_codec_limits=lambda:{'max_bytes':512*1024*1024,'max_nodes':256}
  try:return _freeze(*args,**kwargs)
  finally:research_screening.screening_codec_limits=original
 research_screening.freeze_screening=_fail_at_publication
"""
    (site/'zzz_revision_capacity_fault.pth').write_text('import os; exec('+repr(program)+',{})\n')
    second=h.run('2026-10-15T22:00:00+00:00',fault='screen_codec_capacity')
    assert second.returncode==1,second.stdout+second.stderr
    report=h.report('2026-10-15');assert report['outcome']=='failed' and report['accounting_valid']
    journal=json.loads((h.repo/'data/runtime_recovery/journal.json').read_text())
    incident=next(key for key,value in journal['incidents'].items() if value['identity']['session']=='2026-10-15')
    assert journal['incidents'][incident]['reason_code']=='checkpoint_invalid'
    assert sum(len([r for r in rows if r['session']=='2026-10-15']) for rows in h.book_rows('fills').values())>0
    import gc
    gc.collect()  # Reap the historical harness helper's read-only connections.
    _git(h,'commit','--allow-empty','-m','Fixture verified capacity repair')
    fixed=_git(h,'rev-parse','HEAD');_git(h,'checkout','--detach',current)
    return h,dict(repo=h.repo,generation=GENERATION,effective_session='2026-10-16',old_commit=current,
        profile_sha256=profile_hash,commit=fixed,resolve_missed_staging=incident),report


def test_real_p0_assessment_is_stable_and_rejects_missing_session_run(failed_sample):
    from tradingagents.strategies.orchestration.operator_assessment import session_proof
    h,parameters,_=failed_sample
    row=json.loads((h.repo/'data/generations/manifest.json').read_text())['generations'][0]
    record=json.loads((h.repo/'data/runtime_recovery/journal.json').read_text())['incidents'][parameters['resolve_missed_staging']]
    proof=session_proof(h.repo,row,record['identity'],initial=True)
    assert len(proof['books'])==16
    assert session_proof(h.repo,row,record['identity'],initial=False)==proof
    path=h.state/'horizon_1y_size_100k/portfolio.db'
    with closing(sqlite3.connect(path)) as connection:
        original=connection.execute("SELECT * FROM session_runs WHERE session='2026-10-15'").fetchone()
        connection.execute("DELETE FROM session_runs WHERE session='2026-10-15'")
        connection.commit()
    try:
        with pytest.raises(ValueError,match='book_boundary'):
            session_proof(h.repo,row,record['identity'],initial=True)
    finally:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('INSERT INTO session_runs VALUES ('+','.join('?' for _ in original)+')',original)
            connection.commit()


def test_transaction_preview_apply_repeat_preserves_books_and_failed_evidence(failed_sample,monkeypatch):
    import scripts.apply_code_revision as tool
    from scripts.apply_code_revision import apply_code_revision,_protected_files
    from tradingagents.strategies.orchestration.operator_assessment import validate_operator_assessment
    h,parameters,report=failed_sample
    before_row=json.loads((h.repo/'data/generations/manifest.json').read_text())['generations'][0]
    before=_protected_files(before_row)
    original_retirement=deepcopy(before_row['source_retirement'])
    now=datetime(2026,10,16,18,tzinfo=timezone.utc)
    preview=apply_code_revision(**parameters,now=now)
    assert not preview['changed'] and not preview['already_applied']
    assert apply_code_revision(**parameters,now=datetime(2026,10,16,19,tzinfo=timezone.utc))==preview
    runtime=h.repo/'data/runtime_recovery/journal.json'
    original_journal=runtime.read_bytes()
    corrupt=json.loads(original_journal);corrupt['incidents']['bad']={'identity':{},'running':False}
    runtime.write_text(json.dumps(corrupt))
    try:
        with pytest.raises((ValueError,KeyError)):
            apply_code_revision(**parameters,now=now,expected_plan_sha256=preview['plan_sha256'],apply=True,scheduler_quiesced=True)
        assert not (h.state/tool.JOURNAL_NAME).exists()
    finally:runtime.write_bytes(original_journal)
    with pytest.raises(ValueError,match='cutoff'):
        apply_code_revision(**parameters,now=datetime(2026,10,16,12,tzinfo=timezone.utc))
    real_write,real_git=tool._write_atomic,tool._git
    pending=h.state/tool.JOURNAL_NAME
    assessment=h.repo/'data/runtime_recovery/operator_assessments'/(parameters['resolve_missed_staging']+'.json')
    manifest=h.repo/'data/generations/manifest.json'
    status=h.repo/'data/runtime_recovery/status.json'
    for stage in ('pending','assessment','move','add','checkout','manifest','journal','status'):
        def write(path,body,**kwargs):
            real_write(path,body,**kwargs)
            if Path(path)=={'pending':pending,'assessment':assessment,'manifest':manifest,
                            'journal':runtime,'status':status}.get(stage):
                raise RuntimeError('injected after '+stage)
        def git(repo,*args):
            result=real_git(repo,*args)
            if ((stage=='move' and args[:2]==('worktree','move')) or
                (stage=='add' and args[:2]==('worktree','add')) or
                (stage=='checkout' and args[:2]==('checkout','--detach'))):
                raise RuntimeError('injected after '+stage)
            return result
        with monkeypatch.context() as patch:
            patch.setattr(tool,'_write_atomic',write);patch.setattr(tool,'_git',git)
            with pytest.raises(RuntimeError,match='injected after'):
                apply_code_revision(**parameters,now=now,expected_plan_sha256=preview['plan_sha256'],apply=True,scheduler_quiesced=True)
        if stage=='pending':
            original=pending.read_bytes();value=json.loads(original)
            value['journal_after']='{}\n';pending.write_text(json.dumps(value))
            try:
                with pytest.raises(ValueError,match='postimage'):
                    apply_code_revision(**parameters,now=now,expected_plan_sha256=preview['plan_sha256'],apply=True,scheduler_quiesced=True)
                assert not assessment.exists()
            finally:pending.write_bytes(original)
    result=apply_code_revision(**parameters,now=now,expected_plan_sha256=preview['plan_sha256'],apply=True,scheduler_quiesced=True)
    assert result['already_applied'] and not result['changed']
    after_row=json.loads((h.repo/'data/generations/manifest.json').read_text())['generations'][0]
    assert after_row['source_retirement']==original_retirement
    assert _protected_files(after_row)==before
    journal=json.loads((h.repo/'data/runtime_recovery/journal.json').read_text())
    record=journal['incidents'][parameters['resolve_missed_staging']]
    assert record['disposition']=='wait_next_session' and record['reason_code']=='missed_staging'
    assert record['identity']['generation_commit']==parameters['old_commit']
    receipt=validate_operator_assessment(h.repo,record,result['assessment_sha256'])
    assert json.loads(receipt['journal_before'])['incidents'][parameters['resolve_missed_staging']]['disposition']=='requires_user'
    assert h.report('2026-10-15')==report
    repeated=apply_code_revision(**parameters,now=now,expected_plan_sha256=preview['plan_sha256'],
        apply=True,scheduler_quiesced=True)
    assert not repeated['changed'] and repeated['already_applied']
    original_manifest=manifest.read_bytes();changed=json.loads(original_manifest)
    changed['generations'][0]['run_history'].pop(0);manifest.write_text(json.dumps(changed))
    try:
        with pytest.raises(ValueError,match='history|retirement_changed'):
            apply_code_revision(**parameters,now=now,expected_plan_sha256=preview['plan_sha256'])
    finally:manifest.write_bytes(original_manifest)
    # A genuine fresh fixture-only next session evolves balances, positions,
    # intents and the metric epoch. The original immutable proof remains valid.
    third=h.run('2026-10-16T22:00:00+00:00')
    assert third.returncode==0,third.stdout+third.stderr
    assert h.report('2026-10-15')==report
    validate_operator_assessment(h.repo,record,result['assessment_sha256'])
    assert apply_code_revision(**parameters,now=datetime(2026,10,17,18,tzinfo=timezone.utc),
        expected_plan_sha256=preview['plan_sha256'])['already_applied']
