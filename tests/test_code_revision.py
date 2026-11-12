from copy import deepcopy
from pathlib import Path
import subprocess

import pytest

from test_source_retirement_amendment import amended_row, deployed_amendment
from test_source_retirement_pending_intents import pending_retirement


def revised_row(repo):
    row = amended_row(repo)
    row['code_revision'] = {
        'version': 'verified-defect-code-revision-v1',
        'reason': 'research_screening_capacity_restored_v1',
        'effective_session': '2026-11-12', 'previous_commit': row['git_commit'],
        'commit': 'e' * 40,
        'previous_worktree_path': str(repo / '.worktrees/gen_028-before-code-revision'),
        'config_file': row['config_file'], 'config_sha256': row['config_sha256'],
    }
    row['git_commit'] = row['code_revision']['commit']
    return row


def test_revision_selects_three_intervals_and_preserves_retirement(tmp_path):
    from tradingagents.strategies.orchestration.source_retirement import (
        generation_for_session, validate_generation_retirement, validate_code_revision,
    )
    row = revised_row(tmp_path)
    before = deepcopy(row)
    assert validate_generation_retirement(row) == row['source_retirement']
    assert validate_code_revision(row) == row['code_revision']
    selected = [generation_for_session(row, day) for day in ('2026-11-10', '2026-11-11', '2026-11-12')]
    assert [item['git_commit'] for item in selected] == ['a' * 40, 'b' * 40, 'e' * 40]
    assert [Path(item['worktree_path']).name for item in selected] == [
        'gen_028-before-congress-retirement', 'gen_028-before-code-revision', 'gen_028']
    assert [item['config_sha256'] for item in selected] == ['c' * 64, 'd' * 64, 'd' * 64]
    assert all('source_retirement' not in item and 'code_revision' not in item for item in selected)
    assert row == before


@pytest.mark.parametrize('change', [
    lambda row: row['code_revision'].update(extra=True),
    lambda row: row['code_revision'].update(reason='arbitrary_patch'),
    lambda row: row['code_revision'].update(effective_session='2026-11-11'),
    lambda row: row['code_revision'].update(previous_commit='f' * 40),
    lambda row: row['code_revision'].update(commit='f' * 40),
    lambda row: row['code_revision'].update(config_sha256='f' * 64),
    lambda row: row['code_revision'].update(previous_worktree_path='/foreign'),
    lambda row: row.pop('source_retirement'),
])
def test_revision_rejects_unjoined_or_unrelated_changes(tmp_path, change):
    from tradingagents.strategies.orchestration.source_retirement import validate_generation_retirement
    row = revised_row(tmp_path)
    change(row)
    with pytest.raises(ValueError):
        validate_generation_retirement(row, repo=tmp_path)


def test_manager_accepts_joined_code_revision(tmp_path):
    from tradingagents.strategies.orchestration.generation_manager import GenerationManager
    GenerationManager._validate_manifest({'generations': [revised_row(tmp_path)]})


def test_real_git_three_revisions_keep_original_incident_identity(deployed_amendment):
    from tradingagents.strategies.orchestration.runtime_recovery import identity_for, incident_key
    repo,row=deployed_amendment
    def git(*args):
        return subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True,text=True).stdout.strip()
    old_identity=identity_for(repo,row,'2026-11-11')
    git('commit','--allow-empty','-m','Code-only fix')
    fixed=git('rev-parse','HEAD')
    archive=repo/'.worktrees/gen_028-before-code-revision'
    git('worktree','move',row['worktree_path'],str(archive))
    git('worktree','add','--detach',row['worktree_path'],fixed)
    row['code_revision']={**revised_row(repo)['code_revision'],'previous_commit':row['git_commit'],
        'commit':fixed,'config_file':row['config_file'],'config_sha256':row['config_sha256']}
    row['git_commit']=fixed
    middle=identity_for(repo,row,'2026-11-11')
    assert middle==old_identity and incident_key(middle)==incident_key(old_identity)
    assert identity_for(repo,row,'2026-11-10')['generation_commit']==row['source_retirement']['previous_commit']
    assert identity_for(repo,row,'2026-11-12')['generation_commit']==fixed
    Path(archive/'tracked.txt').write_text('dirty historical source\n')
    with pytest.raises(ValueError):identity_for(repo,row,'2026-11-11')


@pytest.mark.parametrize('day',['2026-11-10','2026-11-11'])
def test_latest_boundary_blocks_historical_controller_worker(tmp_path,monkeypatch,day):
    from scripts import recover_runtime as controller
    from tradingagents.strategies.orchestration import runtime_recovery
    row=revised_row(tmp_path)
    monkeypatch.setattr(controller,'identity_for',lambda *args: {})
    monkeypatch.setattr(controller,'bounded_process',lambda *args,**kwargs:pytest.fail('historical worker launched'))
    with pytest.raises(ValueError,match='identity_mismatch'):
        controller.launch(tmp_path,row,day,'recovery')


@pytest.mark.parametrize('day',['20251111','2026-11-12'])
def test_revision_boundary_refuses_noncanonical_or_future_metric_epoch(tmp_path,monkeypatch,day):
    from contextlib import contextmanager, closing
    import json
    import sqlite3
    from scripts import apply_code_revision as tool

    @contextmanager
    def metrics_copy(path):
        with closing(sqlite3.connect(':memory:')) as connection:
            connection.execute('CREATE TABLE metric_epochs(payload_json TEXT)')
            connection.execute('INSERT INTO metric_epochs VALUES (?)',(json.dumps({'start_session':day}),))
            yield connection,path

    monkeypatch.setattr(tool,'ROSTER',())
    monkeypatch.setattr(tool,'database_copy',metrics_copy)
    with pytest.raises(ValueError,match='accepted_epoch_boundary'):
        tool._boundary(tmp_path,{'run_history':[],'state_dir':str(tmp_path),'gen_id':'gen_028'},'2026-11-12')


@pytest.mark.parametrize('fault',[None,'missing_middle','foreign_middle','changed_config','changed_clock','nonadjacent'])
def test_original_pending_proof_requires_the_exact_intermediate_epoch(pending_retirement,fault):
    from dataclasses import asdict, replace
    from datetime import date
    import json
    from test_source_retirement_pending_intents import REFERENCE,EXECUTION
    repo,row,executor,previous,middle,_,_=pending_retirement
    store=executor.metric_store
    store.close_epoch(middle.epoch_id,EXECUTION,'semantic_hash_changed')
    fixed=replace(middle,epoch_id='capacity-fix',generation_commit='e'*40,
        behavior_hash='capacity-fixed-behavior',start_session=date(2026,8,4),
        boundary_reason='semantic_hash_changed')
    store.save_epoch(fixed)
    row['code_revision']={**revised_row(repo)['code_revision'],'effective_session':'2026-08-04',
        'previous_commit':row['git_commit'],'commit':fixed.generation_commit,
        'config_file':row['config_file'],'config_sha256':row['config_sha256']}
    row['git_commit']=fixed.generation_commit
    (repo/'data/generations/manifest.json').write_text(json.dumps({'generations':[row]}))
    if fault:
        changed=asdict(store.load_epoch(middle.epoch_id))
        if fault=='foreign_middle':changed['generation_commit']='f'*40
        elif fault=='changed_config':changed['config_hash']='foreign-configuration'
        elif fault=='changed_clock':changed['execution_clock_version']='foreign-clock'
        elif fault=='nonadjacent':changed['end_session']=REFERENCE
        with store._connect() as connection:
            if fault=='missing_middle':connection.execute('DELETE FROM metric_epochs WHERE epoch_id=?',(middle.epoch_id,))
            else:connection.execute('UPDATE metric_epochs SET payload_json=? WHERE epoch_id=?',
                (json.dumps(changed,default=str),middle.epoch_id))
    binding=executor.ledger.read_policy_session_context(REFERENCE,binding_kind='staging')
    before=executor.ledger.path.read_bytes()
    assert executor._staging_volatility_epoch_matches(binding,REFERENCE,fixed.epoch_id)==(fault is None)
    assert executor.ledger.path.read_bytes()==before
    assert executor.ledger.intent('original-pending-intent').status=='pending'
