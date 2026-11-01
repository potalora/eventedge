"""Entire-campaign cold snapshots retain caches and coherent committed SQLite WAL."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from tools.live_workflow import baseline
from tools.live_workflow.runner import AcceptanceError


def campaign(tmp_path):
    root=tmp_path/'campaign';root.mkdir(mode=0o700)
    for name in ('repo/.git/objects','repo/.venv/bin','repo/data/source_cache','repo/data/results',
                 'repo/data/generations','hooks','cache','home','logs','tape'):
        (root/name).mkdir(parents=True,exist_ok=True)
    for name,body in {'contract.json':'{"candidate_commit":"abc"}',
            'hook-config.json':'{"phase":"generation_prepare"}', 'profile.json':'{}',
            'repo/.git/config':'git config', 'repo/.git/objects/owned':'object',
            'repo/.venv/pyvenv.cfg':'owned venv', 'repo/.venv/bin/task':'#!/bin/sh\n',
            'repo/data/source_cache/source':'retained cache', 'cache/result':'retained result',
            'repo/data/generations/manifest.json':'{"active":"gen_001"}',
            'hooks/bootstrap.py':'# exact hooks\n'}.items():
        (root/name).write_text(body)
    os.chmod(root/'repo/.venv/bin/task',0o750)
    return root,hashlib.sha256((root/'contract.json').read_bytes()).hexdigest()



def private_python(tmp_path):
    # Hosted toolcache modes are not the runtime safety policy under test.
    # Copy bytes only, then set permissions on this test-owned inode exclusively.
    path=tmp_path/'owned-python'
    path.write_bytes(Path(sys.executable).resolve().read_bytes())
    path.chmod(0o500)
    return path


def test_private_python_fixture_preserves_writable_source_without_reusing_inode(tmp_path,monkeypatch):
    source=tmp_path/'host-python';source.write_bytes(b'controlled interpreter bytes')
    source.chmod(0o775);before=source.stat()
    monkeypatch.setattr(sys,'executable',str(source))
    copied=private_python(tmp_path)
    assert copied.read_bytes()==source.read_bytes()
    assert copied.stat().st_mode & 0o777==0o500
    assert copied.stat().st_ino!=source.stat().st_ino
    assert source.stat().st_mode==before.st_mode
    assert source.stat().st_ino==before.st_ino

def test_complete_private_baseline_and_fresh_restore(tmp_path):
    root,pin=campaign(tmp_path)
    snap=tmp_path/'baseline'
    receipt=baseline.capture_baseline(root,snap,pin)
    assert baseline.verify_baseline(snap,receipt['receipt_sha256']) == receipt
    assert receipt['canonical_root']==str(root)
    assert receipt['contract_sha256']==pin
    assert (snap/'tree/cache/result').read_text()=='retained result'
    assert (snap/'tree/repo/.git/objects/owned').read_text()=='object'
    assert (snap/'tree/repo/.venv/bin/task').stat().st_mode & 0o777==0o750
    assert (snap/'tree/cache/result').stat().st_ino!=(root/'cache/result').stat().st_ino
    assert snap.stat().st_mode & 0o077==0
    assert (snap/'receipt.json').stat().st_mode & 0o077==0
    target=tmp_path/'scenario'
    baseline.copy_baseline(snap,target,receipt['receipt_sha256'])
    assert (target/'cache/result').read_bytes()==(root/'cache/result').read_bytes()
    (target/'cache/result').write_text('changed scenario')
    assert baseline.verify_baseline(snap,receipt['receipt_sha256'])==receipt
    with pytest.raises(AcceptanceError):
        baseline.copy_baseline(snap,root)


def test_committed_wal_rows_and_all_tables_survive(tmp_path):
    root,pin=campaign(tmp_path);db=root/'repo/data/state.db'
    with sqlite3.connect(db) as conn:
        conn.execute('PRAGMA journal_mode=WAL');conn.execute('PRAGMA wal_autocheckpoint=0')
        conn.execute('CREATE TABLE inputs (id INTEGER PRIMARY KEY, body BLOB)')
        conn.execute('CREATE TABLE unrelated (value TEXT)')
        conn.execute('INSERT INTO inputs VALUES (1,?)',(b'payload',))
        conn.execute("INSERT INTO unrelated VALUES ('keep all tables')");conn.commit()
        assert Path(str(db)+'-wal').stat().st_size>0
        snap=tmp_path/'baseline';receipt=baseline.capture_baseline(root,snap,pin)
        copied=snap/'tree/repo/data/state.db'
        with sqlite3.connect(copied) as check:
            assert check.execute('SELECT * FROM inputs').fetchall()==[(1,b'payload')]
            assert check.execute('SELECT * FROM unrelated').fetchall()==[('keep all tables',)]
        assert not Path(str(copied)+'-wal').exists()
        assert receipt['source_inventory']['repo/data/state.db']['sqlite']==receipt['snapshot_inventory']['repo/data/state.db']['sqlite']
        assert baseline.verify_baseline(snap,receipt['receipt_sha256'])==receipt


@pytest.mark.parametrize('change',['bytes','mode','extra','receipt','link'])
def test_snapshot_tampering_rejected(tmp_path,change):
    root,pin=campaign(tmp_path);snap=tmp_path/'baseline'
    receipt=baseline.capture_baseline(root,snap,pin)
    target=snap/'tree/cache/result'
    if change=='bytes':target.write_text('corrupt')
    elif change=='mode':target.chmod(0o777)
    elif change=='extra':(snap/'tree/extra').write_text('unlisted')
    elif change=='receipt':
        value=json.loads((snap/'receipt.json').read_text());value['contract_sha256']='a'*64
        (snap/'receipt.json').write_text(json.dumps(value))
    else:
        target.unlink();target.symlink_to(root/'cache/result')
    with pytest.raises(AcceptanceError):
        baseline.verify_baseline(snap,receipt['receipt_sha256'])


@pytest.mark.parametrize('kind',['external_link','fifo','hardlink','source_parent','size','count','contract','public_root'])
def test_unsafe_or_unbounded_sources_rejected(tmp_path,kind):
    root,pin=campaign(tmp_path);snap=tmp_path/'baseline';kwargs={}
    if kind=='external_link':(root/'cache/escape').symlink_to('/etc/passwd')
    elif kind=='fifo':os.mkfifo(root/'cache/pipe')
    elif kind=='hardlink':os.link(root/'cache/result',root/'cache/alias')
    elif kind=='source_parent':snap=root/'nested'
    elif kind=='size':kwargs['max_bytes']=10
    elif kind=='count':kwargs['max_entries']=3
    elif kind=='contract':pin='0'*64
    else:root.chmod(0o755)
    with pytest.raises(AcceptanceError):baseline.capture_baseline(root,snap,pin,**kwargs)


def test_owned_python_and_internal_venv_links_preserved(tmp_path):
    root,pin=campaign(tmp_path)
    python=private_python(tmp_path)
    runtime={'python_target':str(python),
        'python_sha256':hashlib.sha256(python.read_bytes()).hexdigest()}
    (root/'contract.json').write_text(json.dumps({'owned_runtime':runtime}))
    pin=hashlib.sha256((root/'contract.json').read_bytes()).hexdigest()
    (root/'repo/.venv/bin/python').symlink_to(python)
    (root/'repo/.venv/bin/python3').symlink_to('python')
    (root/'repo/.venv/lib').mkdir()
    (root/'repo/.venv/lib64').symlink_to('lib')
    (root/'cache/internal').symlink_to('result')
    receipt=baseline.capture_baseline(root,tmp_path/'baseline',pin)
    assert receipt['snapshot_inventory']['repo/.venv/bin/python']['target_sha256']==hashlib.sha256(python.read_bytes()).hexdigest()
    assert (tmp_path/'baseline/tree/cache/internal').read_text()=='retained result'
    baseline.verify_baseline(tmp_path/'baseline',receipt['receipt_sha256'])


def test_source_drift_aborts_capture(tmp_path,monkeypatch):
    root,pin=campaign(tmp_path);original=baseline._copy_regular
    def changing_copy(source,target,*args,**kwargs):
        result=original(source,target,*args,**kwargs)
        if source==root/'cache/result':source.write_text('changed during capture')
        return result
    monkeypatch.setattr(baseline,'_copy_regular',changing_copy)
    with pytest.raises(AcceptanceError,match='source_changed'):
        baseline.capture_baseline(root,tmp_path/'baseline',pin)


def test_restored_verification_requires_canonical_mount_and_pristine_tree(tmp_path):
    root,pin=campaign(tmp_path);snap=tmp_path/'baseline'
    receipt=baseline.capture_baseline(root,snap,pin)
    assert baseline.verify_restored_baseline(root,receipt)==receipt
    target=tmp_path/'scenario';baseline.copy_baseline(snap,target)
    with pytest.raises(AcceptanceError,match='canonical_root'):
        baseline.verify_restored_baseline(target,receipt)
    assert baseline.verify_restored_baseline(target,receipt,require_canonical_path=False)==receipt
    (target/'repo/data/source_cache/source').write_text('changed cache')
    with pytest.raises(AcceptanceError,match='restored_changed'):
        baseline.verify_restored_baseline(target,receipt,require_canonical_path=False)


def test_non_database_sidecar_names_are_preserved(tmp_path):
    root,pin=campaign(tmp_path)
    (root/'cache/not-a-db-wal').write_text('ordinary file')
    receipt=baseline.capture_baseline(root,tmp_path/'baseline',pin)
    assert 'cache/not-a-db-wal' in receipt['snapshot_inventory']


def test_unpinned_external_venv_link_rejected(tmp_path):
    root,pin=campaign(tmp_path);python=private_python(tmp_path)
    (root/'repo/.venv/bin/python').symlink_to(python)
    with pytest.raises(AcceptanceError,match='unpinned_python'):
        baseline.capture_baseline(root,tmp_path/'baseline',pin)


def test_snapshot_cannot_hide_sqlite_sidecars(tmp_path):
    root,pin=campaign(tmp_path)
    with sqlite3.connect(root/'repo/data/state.db') as conn:
        conn.execute('CREATE TABLE state (value TEXT)');conn.commit()
    snap=tmp_path/'baseline';receipt=baseline.capture_baseline(root,snap,pin)
    (snap/'tree/repo/data/state.db-shm').write_bytes(b'unlisted mutable sidecar')
    with pytest.raises(AcceptanceError,match='snapshot_sidecar'):
        baseline.verify_baseline(snap,receipt['receipt_sha256'])


def test_parent_traversal_destination_rejected(tmp_path):
    root,pin=campaign(tmp_path);other=tmp_path/'other';other.mkdir()
    with pytest.raises(AcceptanceError,match='canonical_path'):
        baseline.capture_baseline(root,other/'..'/'campaign'/'nested',pin)


def test_absolute_internal_links_use_snapshot_tree_after_original_changes(tmp_path):
    root,pin=campaign(tmp_path)
    (root/'cache/internal').symlink_to(root/'cache/result')
    snap=tmp_path/'baseline';receipt=baseline.capture_baseline(root,snap,pin)
    (root/'cache/result').unlink()
    assert baseline.verify_baseline(snap,receipt['receipt_sha256'])==receipt


def test_sqlite_table_mutation_invalidates_baseline(tmp_path):
    root,pin=campaign(tmp_path)
    with sqlite3.connect(root/'repo/data/state.db') as conn:
        conn.execute('CREATE TABLE state (value BLOB)')
        conn.execute('INSERT INTO state VALUES (?)',(b'original',));conn.commit()
    snap=tmp_path/'baseline';receipt=baseline.capture_baseline(root,snap,pin)
    with sqlite3.connect(snap/'tree/repo/data/state.db') as conn:
        conn.execute('DELETE FROM state');conn.commit()
    with pytest.raises(AcceptanceError,match='snapshot_changed'):
        baseline.verify_baseline(snap,receipt['receipt_sha256'])


def test_source_replacement_with_identical_bytes_still_aborts(tmp_path,monkeypatch):
    root,pin=campaign(tmp_path);original=baseline._copy_regular
    def replacing_copy(source,target,*args,**kwargs):
        result=original(source,target,*args,**kwargs)
        if source==root/'cache/result':
            replacement=root/'cache/replacement'
            replacement.write_bytes(source.read_bytes());replacement.replace(source)
        return result
    monkeypatch.setattr(baseline,'_copy_regular',replacing_copy)
    with pytest.raises(AcceptanceError,match='source_changed'):
        baseline.capture_baseline(root,tmp_path/'baseline',pin)


def test_imported_original_baseline_preserves_canonical_absolute_paths(tmp_path):
    physical,pin=campaign(tmp_path);canonical=tmp_path/'original-campaign'
    contract={'root':str(canonical)}
    (physical/'contract.json').write_text(json.dumps(contract))
    pin=hashlib.sha256((physical/'contract.json').read_bytes()).hexdigest()
    (physical/'cache/internal').symlink_to(canonical/'cache/result')
    snap=tmp_path/'baseline'
    receipt=baseline.capture_baseline(physical,snap,pin,canonical_root=canonical)
    assert receipt['canonical_root']==str(canonical)
    assert baseline.verify_baseline(snap,receipt['receipt_sha256'])==receipt
    restored=tmp_path/'scenario'
    baseline.copy_baseline(snap,restored)
    assert os.readlink(restored/'cache/internal')==str(canonical/'cache/result')
    assert baseline.verify_restored_baseline(restored,receipt,require_canonical_path=False)==receipt


@pytest.mark.parametrize('owner,readonly,valid_pin,writable_mode,accepted',[
    (65534,True,True,False,True),
    (65534,False,True,False,False),
    (65533,True,True,False,False),
    (65534,True,False,False,False),
    (65534,True,True,True,False),
])
def test_unmapped_root_python_requires_exact_pin_overflow_uid_and_readonly_mount(
        tmp_path,monkeypatch,owner,readonly,valid_pin,writable_mode,accepted):
    from types import SimpleNamespace
    root,pin=campaign(tmp_path);python=private_python(tmp_path)
    runtime={'python_target':str(python),
        'python_sha256':hashlib.sha256(python.read_bytes()).hexdigest() if valid_pin else '0'*64}
    (root/'contract.json').write_text(json.dumps({'owned_runtime':runtime}))
    pin=hashlib.sha256((root/'contract.json').read_bytes()).hexdigest()
    (root/'repo/.venv/bin/python').symlink_to(python)
    original_lstat=Path.lstat
    def mapped_lstat(path,*args,**kwargs):
        info=original_lstat(path,*args,**kwargs)
        if path==python:
            fields=list(info);fields[4]=owner
            fields[0]=(fields[0]&~0o022)|(0o022 if writable_mode else 0)
            return os.stat_result(fields)
        return info
    monkeypatch.setattr(Path,'lstat',mapped_lstat)
    monkeypatch.setattr(baseline,'_kernel_overflow_uid',lambda:65534)
    monkeypatch.setattr(baseline.os,'statvfs',lambda path:SimpleNamespace(f_flag=os.ST_RDONLY if readonly else 0))
    if accepted:
        receipt=baseline.capture_baseline(root,tmp_path/'baseline',pin)
        assert baseline.verify_baseline(tmp_path/'baseline',receipt['receipt_sha256'])==receipt
    else:
        with pytest.raises(AcceptanceError,match='baseline_python_'):
            baseline.capture_baseline(root,tmp_path/'baseline',pin)
