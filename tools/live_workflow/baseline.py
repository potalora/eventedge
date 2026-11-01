"""Bounded, whole-campaign cold baselines; no candidate code is executed.

Call before acquisition with the campaign idle and its runtime lease held by the
trusted parent. Each scenario gets a new copy mounted at ``canonical_root`` by
that parent: absolute config paths and venv links are deliberately not rewritten.
The receipt hash must be retained outside the untrusted snapshot directory.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .runner import AcceptanceError, _kernel_overflow_uid, digest, no_symlinks, private_json, require, sha

MAX_BYTES = 2 * 1024**3
MAX_ENTRIES = 100000
MAX_TABLE_ROWS = 1000000
MAGIC = b'SQLite format 3\x00'


def _path(path):
    path=no_symlinks(path)
    require('..' not in path.parts,'baseline_canonical_path_required')
    return path


def _regular(path):
    info=path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid()
        and info.st_nlink==1,'baseline_regular_file_identity')
    return info


def _private_dir(path):
    path=_path(path)
    info=path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid()
        and not info.st_mode & 0o077,'baseline_private_directory_required')
    return path


def _connect(path):
    # immutable=1 would ignore committed WAL and is intentionally forbidden.
    conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)
    conn.execute('PRAGMA trusted_schema=OFF')
    conn.execute('PRAGMA query_only=ON')
    deadline=time.monotonic()+30
    conn.set_progress_handler(lambda: int(time.monotonic()>deadline),10000)
    conn.execute('BEGIN')
    return conn


def _cell(value):
    if value is None:return ['null']
    if isinstance(value,bytes):return ['blob',value.hex()]
    if isinstance(value,float):return ['float',value.hex()]
    if isinstance(value,int):return ['integer',str(value)]
    return ['text',value]


def _logical(conn):
    require(conn.execute('PRAGMA quick_check').fetchall()==[('ok',)],'baseline_sqlite_integrity')
    schema=conn.execute('SELECT type,name,tbl_name,rootpage,sql FROM sqlite_schema ORDER BY type,name').fetchall()
    tables={}
    for kind,name,*_ in schema:
        if kind!='table':continue
        quoted='"'+name.replace('"','""')+'"'
        rows=[]
        for row in conn.execute('SELECT * FROM '+quoted):
            require(len(rows)<MAX_TABLE_ROWS,'baseline_sqlite_row_limit')
            rows.append(digest([_cell(value) for value in row]))
        rows.sort()
        tables[name]={'rows':len(rows),'sha256':digest(rows)}
    # Root-page locations are physical layout, not schema identity.
    return {'schema_sha256':digest([[row[0],row[1],row[2],row[4]] for row in schema]),
        'tables':tables,'pragmas':{name:conn.execute('PRAGMA '+name).fetchone()[0]
            for name in ('user_version','application_id','encoding')}}


def _link(path, root, canonical_root, runtime, seen=()):
    relative=str(path.relative_to(root))
    require(relative not in seen,'baseline_symlink_cycle')
    raw=os.readlink(path)
    require(raw and not any(ch in raw for ch in '\x00\r\n'),'baseline_invalid_symlink')
    virtual=Path(os.path.normpath(raw if os.path.isabs(raw) else str(canonical_root/relative/ '..'/raw)))
    # Resolve against the original logical root, mapping internal absolute links
    # to the copied tree rather than ever reading the original campaign.
    if virtual.is_relative_to(canonical_root):
        target=root/virtual.relative_to(canonical_root)
        # Reject traversing intermediate directory symlinks; ordinary venv
        # python aliases and lib64 each point directly at their owned target.
        require(not any(parent.is_symlink() for parent in target.parents if parent.is_relative_to(root)),
            'baseline_symlink_parent')
        require(target.exists() or target.is_symlink(),'baseline_dangling_symlink')
        if target.is_symlink():
            identity=_link(target,root,canonical_root,runtime,(*seen,relative))
            return {'target':raw,**{k:v for k,v in identity.items() if k!='target'}}
        require(target.is_dir() or target.is_file(),'baseline_symlink_special_target')
        return {'target':raw}
    require(re.fullmatch(r'repo/\.venv/bin/python(?:[0-9]+(?:\.[0-9]+)?)?',relative) is not None,
        'baseline_external_symlink')
    require(str(virtual)==runtime.get('python_target') and re.fullmatch('[a-f0-9]{64}',runtime.get('python_sha256','')),
        'baseline_unpinned_python_symlink')
    actual=_path(virtual);info=actual.lstat()
    owner_valid=info.st_uid in (0,os.getuid())
    if not owner_valid:
        # The trusted supervisor verifies host-root ownership before the
        # single-UID user namespace maps that inode to the kernel overflow UID.
        # Its exact contract hash and a read-only mount remain mandatory.
        owner_valid=(info.st_uid==_kernel_overflow_uid()
            and bool(os.statvfs(actual).f_flag & os.ST_RDONLY))
    require(stat.S_ISREG(info.st_mode) and owner_valid and not info.st_mode & 0o022,
        'baseline_python_identity')
    require(sha(actual)==runtime['python_sha256'],'baseline_python_changed')
    return {'target':raw,'target_sha256':runtime['python_sha256']}


def _inventory(root, canonical_root, runtime, max_bytes, max_entries, *, allow_sqlite_sidecars=False, source_stats=False):
    entries={};files=[];total=0
    def walk(folder):
        nonlocal total
        for path in sorted(folder.iterdir()):
            rel=str(path.relative_to(root));info=path.lstat()
            require(info.st_uid==os.getuid(),'baseline_foreign_owner')
            require(not any(ch in rel for ch in '\x00\r\n'),'baseline_invalid_path')
            require(len(entries)<max_entries,'baseline_entry_limit')
            common={'mode':stat.S_IMODE(info.st_mode)}
            if source_stats and not stat.S_ISDIR(info.st_mode):
                common['source_stat']={'device':info.st_dev,'inode':info.st_ino,
                    'mtime_ns':info.st_mtime_ns,'ctime_ns':info.st_ctime_ns}
            if stat.S_ISDIR(info.st_mode):
                entries[rel]={'kind':'directory',**common};walk(path)
            elif stat.S_ISLNK(info.st_mode):
                entries[rel]={'kind':'symlink',**common,**_link(path,root,canonical_root,runtime)}
            elif stat.S_ISREG(info.st_mode):
                _regular(path);total+=info.st_size
                require(total<=max_bytes,'baseline_byte_limit')
                entries[rel]={'kind':'file',**common,'size':info.st_size,'sha256':sha(path)}
                files.append(path)
            else:raise AcceptanceError('baseline_special_file')
    walk(root)
    databases=[]
    for path in files:
        with path.open('rb') as handle:header=handle.read(16)
        if header==MAGIC:databases.append(path)
    for path in databases:
        rel=str(path.relative_to(root))
        require(allow_sqlite_sidecars or not any((root/(rel+suffix)).exists() for suffix in ('-wal','-shm')),
            'baseline_snapshot_sidecar')
        with _connect(path) as conn:entries[rel]['sqlite']=_logical(conn)
        for suffix in ('-wal','-shm'):
            entries.pop(rel+suffix,None)
    return entries


def _copy_regular(source,target):
    # O_NOFOLLOW + exclusive destination prevent accidental symlink following
    # or source hardlink propagation. No copytree hardlink optimization is used.
    info=_regular(source)
    descriptor=os.open(source,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        opened=os.fstat(descriptor)
        require((opened.st_dev,opened.st_ino)==(info.st_dev,info.st_ino),'baseline_source_changed')
        with os.fdopen(descriptor,'rb',closefd=False) as src, target.open('xb') as dst:
            written=0
            while block:=src.read(1024**2):
                written+=len(block)
                require(written<=info.st_size,'baseline_source_changed')
                dst.write(block)
            require(written==info.st_size,'baseline_source_changed')
            dst.flush();os.fsync(dst.fileno())
    finally:os.close(descriptor)
    target.chmod(stat.S_IMODE(info.st_mode))


def _copy_database(source,target,expected):
    deadline=time.monotonic()+30
    def progress(status,remaining,total):
        require(time.monotonic()<deadline,'baseline_sqlite_backup_timeout')
    with _connect(source) as src:
        require(_logical(src)==expected,'baseline_source_changed')
        with sqlite3.connect(target) as dst:
            src.backup(dst,pages=256,progress=progress,sleep=.01)
            # A standalone snapshot has all committed pages and needs no WAL.
            dst.execute('PRAGMA journal_mode=DELETE')
    target.chmod(stat.S_IMODE(source.lstat().st_mode))
    with _connect(target) as conn:require(_logical(conn)==expected,'baseline_sqlite_copy_changed')


def _copy_tree(source,target,inventory):
    target.mkdir(mode=0o700)
    directories=[]
    for rel,row in inventory.items():
        src=source/rel;dst=target/rel
        if row['kind']=='directory':dst.mkdir(mode=0o700);directories.append((dst,row['mode']))
        elif row['kind']=='symlink':dst.symlink_to(row['target'])
        elif 'sqlite' in row:_copy_database(src,dst,row['sqlite'])
        else:_copy_regular(src,dst)
    for path,mode in reversed(directories):path.chmod(mode)
    target.chmod(stat.S_IMODE(source.stat().st_mode))


def _equivalent(source,snapshot):
    if set(source)!=set(snapshot):return False
    for rel,row in source.items():
        row={k:v for k,v in row.items() if k!='source_stat'}
        copied={k:v for k,v in snapshot[rel].items() if k!='source_stat'}
        if 'sqlite' in row:
            if {k:v for k,v in row.items() if k not in ('sha256','size')}!={k:v for k,v in copied.items() if k not in ('sha256','size')}:return False
        elif row!=copied:return False
    return True


def capture_baseline(root, destination, contract_sha256, *, max_bytes=MAX_BYTES, max_entries=MAX_ENTRIES, canonical_root=None):
    """Capture a fresh external baseline; caller must hold the campaign lease."""
    root=_private_dir(root);destination=_path(destination)
    canonical=root if canonical_root is None else _path(canonical_root)
    require(not destination.exists() and not destination.is_relative_to(root)
        and not root.is_relative_to(destination),'baseline_destination_must_be_fresh_external')
    require(type(max_bytes) is int and 0<max_bytes<=MAX_BYTES and type(max_entries) is int
        and 0<max_entries<=MAX_ENTRIES,'baseline_invalid_limits')
    _regular(root/'contract.json')
    require(re.fullmatch('[a-f0-9]{64}',contract_sha256 or '') is not None
        and sha(root/'contract.json')==contract_sha256,'baseline_contract_changed')
    contract=json.loads((root/'contract.json').read_bytes());runtime=contract.get('owned_runtime',{})
    require(contract.get('root',str(canonical))==str(canonical),'baseline_contract_canonical_root')
    before=_inventory(root,canonical,runtime,max_bytes,max_entries,allow_sqlite_sidecars=True,source_stats=True)
    destination.mkdir(mode=0o700)
    _copy_tree(root,destination/'tree',before)
    after=_inventory(root,canonical,runtime,max_bytes,max_entries,allow_sqlite_sidecars=True,source_stats=True)
    require(before==after,'baseline_source_changed')
    copied=_inventory(destination/'tree',canonical,runtime,max_bytes,max_entries)
    require(_equivalent(before,copied),'baseline_copy_changed')
    receipt={'schema_version':1,'kind':'whole_campaign_pre_acquisition_baseline',
        'canonical_root':str(canonical),'contract_sha256':contract_sha256,
        'captured_at':datetime.now(timezone.utc).isoformat(),'root_mode':stat.S_IMODE(root.stat().st_mode),
        'max_bytes':max_bytes,'max_entries':max_entries,'owned_runtime':runtime,
        'source_inventory':before,'snapshot_inventory':copied,
        'sqlite_policy':'coherent_backup_all_tables_committed_wal_no_selective_deletion',
        'restore_policy':'fresh_copy_mounted_at_canonical_root_by_external_supervisor'}
    receipt['receipt_sha256']=digest(receipt)
    private_json(destination/'receipt.json',receipt)
    verify_baseline(destination,receipt['receipt_sha256'])
    return receipt


def verify_baseline(destination, expected_receipt_sha256=None):
    """Verify all snapshot bytes and modes; supply the parent's external hash pin."""
    destination=_private_dir(destination)
    require({p.name for p in destination.iterdir()}=={'tree','receipt.json'},'baseline_unexpected_top_level')
    receipt_path=destination/'receipt.json';info=_regular(receipt_path)
    require(not info.st_mode & 0o077 and info.st_size<=64*1024**2,'baseline_receipt_permissions_or_size')
    receipt=json.loads(receipt_path.read_bytes())
    body={k:v for k,v in receipt.items() if k!='receipt_sha256'}
    require(receipt.get('schema_version')==1 and receipt.get('kind')=='whole_campaign_pre_acquisition_baseline'
        and digest(body)==receipt.get('receipt_sha256'),'baseline_receipt_changed')
    require(expected_receipt_sha256 is None or receipt['receipt_sha256']==expected_receipt_sha256,
        'baseline_receipt_pin_changed')
    require(type(receipt['max_bytes']) is int and 0<receipt['max_bytes']<=MAX_BYTES
        and type(receipt['max_entries']) is int and 0<receipt['max_entries']<=MAX_ENTRIES,'baseline_invalid_limits')
    tree=_private_dir(destination/'tree');canonical=_path(receipt['canonical_root'])
    inventory=_inventory(tree,canonical,receipt['owned_runtime'],receipt['max_bytes'],receipt['max_entries'])
    require(inventory==receipt['snapshot_inventory'] and _equivalent(receipt['source_inventory'],inventory),
        'baseline_snapshot_changed')
    require(sha(tree/'contract.json')==receipt['contract_sha256'],'baseline_contract_changed')
    return receipt


def copy_baseline(destination, target, expected_receipt_sha256=None):
    """Restore a new physical tree; supervisor later binds it to canonical_root."""
    destination=_path(destination);receipt=verify_baseline(destination,expected_receipt_sha256)
    target=_path(target);canonical=Path(receipt['canonical_root'])
    require(not target.exists() and not target.is_relative_to(canonical)
        and not canonical.is_relative_to(target) and not target.is_relative_to(destination)
        and not destination.is_relative_to(target),'baseline_restore_must_be_fresh_external')
    _copy_tree(destination/'tree',target,receipt['snapshot_inventory'])
    copied=_inventory(target,canonical,receipt['owned_runtime'],receipt['max_bytes'],receipt['max_entries'])
    require(_equivalent(receipt['snapshot_inventory'],copied),'baseline_restore_changed')
    verify_baseline(destination,receipt['receipt_sha256'])
    return receipt


def verify_restored_baseline(root, receipt, *, require_canonical_path=True):
    """Verify a restored tree before hooks or application imports execute.

    The caller supplies a receipt pinned by its trusted parent. The default
    requires the supervisor's mount at the original absolute campaign path; the
    parent can verify an unmounted fresh copy with require_canonical_path=False.
    SQLite physical layout may differ after coherent backup; all schemas and
    table rows must match, while every other byte, mode and path matches exactly.
    """
    root=_private_dir(root)
    body={k:v for k,v in receipt.items() if k!='receipt_sha256'}
    require(digest(body)==receipt.get('receipt_sha256')
        and receipt.get('schema_version')==1
        and receipt.get('kind')=='whole_campaign_pre_acquisition_baseline','baseline_receipt_changed')
    canonical=_path(receipt['canonical_root'])
    require(not require_canonical_path or root==canonical,'baseline_restored_canonical_root')
    require(stat.S_IMODE(root.stat().st_mode)==receipt['root_mode'],'baseline_restored_root_mode')
    require(type(receipt['max_bytes']) is int and 0<receipt['max_bytes']<=MAX_BYTES
        and type(receipt['max_entries']) is int and 0<receipt['max_entries']<=MAX_ENTRIES,'baseline_invalid_limits')
    inventory=_inventory(root,canonical,receipt['owned_runtime'],receipt['max_bytes'],receipt['max_entries'])
    require(_equivalent(receipt['snapshot_inventory'],inventory),'baseline_restored_changed')
    require(sha(root/'contract.json')==receipt['contract_sha256'],'baseline_contract_changed')
    return receipt
