"""Lossless replay binds retained bytes and reserves one anonymous decode."""
from copy import deepcopy
import gzip
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat

import pytest

def canonical_gzip(raw):
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode='wb', compresslevel=1, mtime=0, filename='') as stream:
        stream.write(raw)
    return output.getvalue()


MODULE = 'tradingagents.strategies.data_sources.filing_original_store'
ACC = '0000000001-26-000001'
OTHER = '0000000001-26-000002'


def store_class(name='GzipOriginalStore'):
    assert importlib.util.find_spec(MODULE) is not None, 'bounded original stores are missing'
    return getattr(importlib.import_module(MODULE), name)


def fixture(tmp_path, *, raws=None, encoded=None):
    root = tmp_path / 'retained'
    root.mkdir()
    expected, paths, receipts = {}, {}, {}
    for acc, raw in (raws or {ACC: bytes(range(256)) * 1024}).items():
        original = {'identity': {'accession': acc, 'form': '8-K', 'filing_date': '2026-09-30',
            'source_url': 'https://www.sec.gov/Archives/edgar/data/1/' + acc.replace('-', '') + '/' + acc + '.txt'},
            'observed_at': '2026-10-01T00:00:00+00:00', 'size': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(), 'complete': True}
        payload = canonical_gzip(raw) if encoded is None else encoded
        path = root / f'sec-original-{acc}.bin.gz'
        path.write_bytes(payload); path.chmod(0o400)
        receipt = {'contract': 'sec-original-v2', **original, 'encoding': 'gzip_original_v1',
            'stored_name': path.name, 'stored_size': len(payload), 'stored_sha256': hashlib.sha256(payload).hexdigest()}
        receipt_path = root / f'sec-original-{acc}.json'
        receipt_path.write_text(json.dumps(receipt)); receipt_path.chmod(0o400)
        expected[acc], paths[acc], receipts[acc] = original, path, receipt_path
    return expected, paths, receipts


def private_scratch(tmp_path):
    directory = tmp_path / 'scratch'
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def store(tmp_path, *, check=lambda: 10, **kwargs):
    expected, paths, receipts = fixture(tmp_path, **kwargs)
    return store_class()(expected, paths, receipts, check=check, scratch_directory=private_scratch(tmp_path)), expected, paths, receipts


def rewrite(path, content):
    path.chmod(0o600)
    path.write_bytes(content)
    path.chmod(0o400)


def test_arbitrary_original_roundtrip_readonly_and_scratch_closed(tmp_path):
    raw = bytes(range(256)) * 1024
    value, expected, paths, receipts = store(tmp_path, raws={ACC: raw})
    baseline = sum(path.stat().st_size for path in paths.values())
    assert value.stats()['retained_bytes'] == baseline
    with value.open_original(ACC) as decoded:
        assert decoded[:] == raw
        with pytest.raises(TypeError): decoded[0] = 1
        assert value.stats()['scratch_bytes'] == len(raw)
        assert value.stats()['peak_total_bytes'] == baseline + len(raw)
    assert decoded.closed
    assert value.stats()['scratch_closed'] is True
    assert value.stats()['scratch_bytes'] == 0
    value.assert_closed()
    value.recheck_persistent_identities()


@pytest.mark.parametrize('damage', ['stored_hash', 'raw_hash', 'raw_size', 'complete', 'identity',
    'extra_field', 'contract', 'encoding', 'stored_name', 'stored_size', 'boolean_size', 'duplicate_key'])
def test_receipt_binds_exact_complete_source_and_encoding(tmp_path, damage):
    expected, paths, receipts = fixture(tmp_path)
    path = receipts[ACC]; receipt = json.loads(path.read_text())
    if damage == 'stored_hash': receipt['stored_sha256'] = '0' * 64
    elif damage == 'raw_hash': receipt['sha256'] = '0' * 64
    elif damage == 'raw_size': receipt['size'] += 1
    elif damage == 'complete': receipt['complete'] = False
    elif damage == 'identity': receipt['identity']['form'] = '10-K'
    elif damage == 'extra_field': receipt['unknown'] = 1
    elif damage == 'contract': receipt['contract'] = 'sec-original-v1'
    elif damage == 'encoding': receipt['encoding'] = 'raw'
    elif damage == 'stored_name': receipt['stored_name'] = '../other.gz'
    elif damage == 'stored_size': receipt['stored_size'] += 1
    elif damage == 'boolean_size': receipt['stored_size'] = True
    content = json.dumps(receipt)
    if damage == 'duplicate_key': content = content[:-1] + ', "contract":"sec-original-v2"}'
    rewrite(path, content.encode())
    with pytest.raises(ValueError):
        value = store_class()(expected, paths, receipts, check=lambda: 10, scratch_directory=private_scratch(tmp_path))
        with value.open_original(ACC): pass


@pytest.mark.parametrize('damage', ['truncated', 'concatenated', 'trailing', 'checksum', 'expansion', 'hash'])
def test_rejects_invalid_gzip_even_when_stored_receipt_matches(tmp_path, damage):
    raw = b'original bytes' * 10000
    encoded = canonical_gzip(raw)
    if damage == 'truncated': encoded = encoded[:-1]
    elif damage == 'concatenated': encoded += canonical_gzip(b'other')
    elif damage == 'trailing': encoded += b'trailing'
    elif damage == 'checksum': encoded = encoded[:-8] + b'\0' * 8
    elif damage == 'expansion': encoded = canonical_gzip(raw + b'extra' * 10000)
    elif damage == 'hash': encoded = canonical_gzip(raw[:-1] + b'!')
    value, _, _, _ = store(tmp_path, raws={ACC: raw}, encoded=encoded)
    with pytest.raises(ValueError):
        with value.open_original(ACC): pass
    value.assert_closed()
    assert value.stats()['scratch_bytes'] == 0


def test_full_persistent_baseline_plus_decode_must_fit_before_allocating(tmp_path, monkeypatch):
    expected, paths, receipts = fixture(tmp_path, raws={ACC: b'a' * 10000, OTHER: os.urandom(20000)})
    baseline = sum(path.stat().st_size for path in paths.values())
    module = importlib.import_module(MODULE)
    def forbidden(*args, **kwargs): pytest.fail('scratch allocated before physical reservation')
    monkeypatch.setattr(module, '_AnonymousScratch', forbidden)
    value = store_class()(expected, paths, receipts, check=lambda: 10, physical_limit=baseline + 9999, scratch_directory=private_scratch(tmp_path))
    with pytest.raises(ValueError, match='physical'):
        with value.open_original(ACC): pass
    value.assert_closed()


def test_refuses_concurrent_opens_and_limit_increase(tmp_path):
    value, expected, paths, receipts = store(tmp_path)
    with value.open_original(ACC):
        with pytest.raises(ValueError, match='concurrent'):
            with value.open_original(ACC): pass
    with pytest.raises(ValueError):
        store_class()(expected, paths, receipts, check=lambda: 10, physical_limit=4 * 1024**3 + 1, scratch_directory=private_scratch(tmp_path))


def test_inherited_deadline_expires_during_decode_and_closes_scratch(tmp_path):
    expected, paths, receipts = fixture(tmp_path, raws={ACC: os.urandom(2 * 1024**2)})
    deadline = {'remaining': 10, 'calls': 0}
    def check():
        deadline['calls'] += 1
        if deadline['calls'] > deadline.get('expire_at', float('inf')): return 0
        return deadline['remaining']
    value = store_class()(expected, paths, receipts, check=check, scratch_directory=private_scratch(tmp_path))
    deadline['expire_at'] = deadline['calls'] + 20
    with pytest.raises(ValueError, match='deadline'):
        with value.open_original(ACC): pass
    value.assert_closed()
    assert deadline['calls'] > deadline['expire_at']


@pytest.mark.parametrize('kind', ['payload', 'receipt', 'ancestor'])
def test_final_recheck_catches_substitution_of_previously_opened_evidence(tmp_path, kind):
    value, _, paths, receipts = store(tmp_path)
    with value.open_original(ACC): pass
    if kind == 'ancestor':
        root = paths[ACC].parent
        root.rename(tmp_path / 'moved')
        root.mkdir()
        for original in (tmp_path / 'moved').iterdir():
            target = root / original.name; target.write_bytes(original.read_bytes()); target.chmod(0o400)
    else:
        path = paths[ACC] if kind == 'payload' else receipts[ACC]
        data = path.read_bytes(); path.unlink(); path.write_bytes(data); path.chmod(0o400)
    with pytest.raises(ValueError, match='changed'):
        value.recheck_persistent_identities()


@pytest.mark.parametrize('kind', ['payload', 'receipt', 'ancestor'])
def test_no_symlink_components_are_accepted(tmp_path, kind):
    expected, paths, receipts = fixture(tmp_path)
    if kind == 'ancestor':
        alias = tmp_path / 'alias'; alias.symlink_to(paths[ACC].parent, target_is_directory=True)
        paths[ACC] = alias / paths[ACC].name
    else:
        values = paths if kind == 'payload' else receipts
        path = values[ACC]; other = tmp_path / path.name
        path.rename(other); path.symlink_to(other)
    with pytest.raises((ValueError, OSError)):
        store_class()(expected, paths, receipts, check=lambda: 10, scratch_directory=private_scratch(tmp_path))


def test_scratch_close_failure_remains_charged_and_prevents_acceptance(tmp_path, monkeypatch):
    value, expected, _, _ = store(tmp_path)
    module = importlib.import_module(MODULE)
    original_class = module._AnonymousScratch
    leaked = []
    class UncertainClose(original_class):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); leaked.append(self)
        def close(self): raise OSError('uncertain close')
    monkeypatch.setattr(module, '_AnonymousScratch', UncertainClose)
    try:
        with pytest.raises((ValueError, OSError)):
            with value.open_original(ACC): pass
        assert value.stats()['scratch_bytes'] == expected[ACC]['size']
        assert value.stats()['scratch_closed'] is False
        assert value.stats()['cleanup_failures'] == 1
        with pytest.raises(ValueError): value.assert_closed()
    finally:
        for file in leaked: original_class.close(file)


@pytest.mark.parametrize('kind', ['missing', 'extra', 'duplicate_payload', 'duplicate_receipt', 'bad_expected'])
def test_manifest_is_complete_unambiguous_and_detached_from_caller(tmp_path, kind):
    expected, paths, receipts = fixture(tmp_path, raws={ACC: b'a', OTHER: b'b'})
    if kind == 'missing': paths.pop(OTHER)
    elif kind == 'extra': receipts['orphan'] = receipts[ACC]
    elif kind == 'duplicate_payload': paths[OTHER] = paths[ACC]
    elif kind == 'duplicate_receipt': receipts[OTHER] = receipts[ACC]
    elif kind == 'bad_expected': expected[ACC]['complete'] = False
    with pytest.raises(ValueError): store_class()(expected, paths, receipts, check=lambda: 10, scratch_directory=private_scratch(tmp_path))


def test_expected_inventory_is_snapshotted(tmp_path):
    value, expected, paths, _ = store(tmp_path)
    expected[ACC]['size'] = 1
    paths.clear()
    with value.open_original(ACC) as decoded: assert len(decoded) == 256 * 1024


@pytest.mark.parametrize('index', [0, 2, 3, 4, 8, 9])
def test_rejects_noncanonical_gzip_header(tmp_path, index):
    raw = b'original' * 100
    encoded = bytearray(canonical_gzip(raw)); encoded[index] ^= 1
    value, _, _, _ = store(tmp_path, raws={ACC: raw}, encoded=bytes(encoded))
    with pytest.raises(ValueError):
        with value.open_original(ACC): pass
    value.assert_closed()


def test_scratch_must_be_private_and_outside_retained_evidence(tmp_path):
    expected, paths, receipts = fixture(tmp_path)
    for directory in (paths[ACC].parent, tmp_path):
        directory.chmod(0o755)
        with pytest.raises(ValueError):
            store_class()(expected, paths, receipts, check=lambda: 10, scratch_directory=directory)
    paths[ACC].parent.chmod(0o700)
    with pytest.raises(ValueError):
        store_class()(expected, paths, receipts, check=lambda: 10, scratch_directory=paths[ACC].parent)


def test_all_payload_bytes_are_charged_during_first_decode(tmp_path):
    raws = {ACC: os.urandom(4096), OTHER: b'b' * 10000}
    value, _, paths, _ = store(tmp_path, raws=raws)
    baseline = sum(path.stat().st_size for path in paths.values())
    assert paths[ACC].stat().st_size > len(raws[ACC])
    with value.open_original(ACC):
        assert value.stats()['total_bytes'] == baseline + 4096
    with value.open_original(OTHER):
        assert value.stats()['total_bytes'] == baseline + 10000
    assert value.stats()['peak_total_bytes'] == baseline + 10000


def test_post_constructor_payload_corruption_is_rejected(tmp_path):
    value, _, paths, _ = store(tmp_path)
    path = paths[ACC]
    content = path.read_bytes()
    rewrite(path, content[:-1] + bytes([content[-1] ^ 1]))
    with pytest.raises(ValueError):
        with value.open_original(ACC): pass
    value.assert_closed()


def test_caller_failure_closes_view_and_scratch_before_next_original(tmp_path):
    value, _, _, _ = store(tmp_path)
    with pytest.raises(RuntimeError, match='proof failure'):
        with value.open_original(ACC) as view:
            raise RuntimeError('proof failure')
    assert view.closed and value.stats()['scratch_closed']
    with value.open_original(ACC) as view: assert len(view) == 256 * 1024
    value.assert_closed()


def test_deadline_failure_after_allocation_is_inside_decode_and_closes(tmp_path, monkeypatch):
    expected, paths, receipts = fixture(tmp_path, raws={ACC: os.urandom(1024**2)})
    module = importlib.import_module(MODULE)
    allocated = []
    original_class = module._AnonymousScratch
    class Observed(original_class):
        def allocate(self):
            super().allocate(); allocated.append(self)
    monkeypatch.setattr(module, '_AnonymousScratch', Observed)
    calls = {'after_allocation': 0}
    def check():
        if allocated:
            calls['after_allocation'] += 1
            if calls['after_allocation'] > 8: return 0
        return 10
    value = store_class()(expected, paths, receipts, check=check, scratch_directory=private_scratch(tmp_path))
    with pytest.raises(ValueError, match='deadline'):
        with value.open_original(ACC): pass
    assert allocated and all(file.closed for file in allocated)
    assert calls['after_allocation'] == 9
    value.assert_closed()


def test_scratch_allocation_failure_releases_only_unallocated_reservation(tmp_path, monkeypatch):
    value, _, _, _ = store(tmp_path)
    module = importlib.import_module(MODULE)
    def failed(*args, **kwargs): raise OSError('allocation failed')
    monkeypatch.setattr(module, '_AnonymousScratch', failed)
    with pytest.raises(OSError, match='allocation failed'):
        with value.open_original(ACC): pass
    value.assert_closed()
    assert value.stats()['scratch_bytes'] == 0


def test_owned_scratch_directory_substitution_is_rejected(tmp_path):
    expected, paths, receipts = fixture(tmp_path)
    scratch = tmp_path / 'scratch'; scratch.mkdir(mode=0o700)
    value = store_class()(expected, paths, receipts, check=lambda: 10, scratch_directory=scratch)
    scratch.rename(tmp_path / 'old-scratch'); scratch.mkdir(mode=0o700)
    with pytest.raises(ValueError, match='scratch_directory_changed'):
        with value.open_original(ACC): pass
    value.assert_closed()


def test_gzip_requires_explicit_owned_scratch_directory(tmp_path):
    expected, paths, receipts = fixture(tmp_path)
    with pytest.raises(ValueError, match='missing_original_scratch_directory'):
        store_class()(expected, paths, receipts, check=lambda: 10)


def test_scratch_partial_write_fails_and_closes_all_owned_resources(tmp_path, monkeypatch):
    value, _, _, _ = store(tmp_path)
    module = importlib.import_module(MODULE)
    original_class = module._AnonymousScratch
    allocated = []
    class ShortWrite(original_class):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); allocated.append(self)
        def write(self, content): return super().write(content[:len(content) // 2])
    monkeypatch.setattr(module, '_AnonymousScratch', ShortWrite)
    with pytest.raises(ValueError, match='short_original_scratch_write'):
        with value.open_original(ACC): pass
    assert allocated and all(file.closed for file in allocated)
    value.assert_closed()
    assert not list((tmp_path / 'scratch').iterdir())


def test_exclusive_scratch_allocation_cannot_overwrite_existing_file(tmp_path, monkeypatch):
    value, _, _, _ = store(tmp_path)
    module = importlib.import_module(MODULE)
    monkeypatch.setattr(module.secrets, 'token_hex', lambda size: '0' * 32)
    path = tmp_path / 'scratch' / ('eventedge-original-' + '0' * 32)
    path.write_bytes(b'preserve me')
    with pytest.raises(FileExistsError):
        with value.open_original(ACC): pass
    assert path.read_bytes() == b'preserve me'
    value.assert_closed()


def test_failed_scratch_unlink_stays_charged_until_cleanup_is_confirmed(tmp_path, monkeypatch):
    value, expected, _, _ = store(tmp_path)
    module = importlib.import_module(MODULE)
    original_class, original_unlink = module._AnonymousScratch, module.os.unlink
    allocated = []
    class Observed(original_class):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); allocated.append(self)
    def failed(path, *args, **kwargs):
        if str(path).startswith('eventedge-original-'): raise OSError('unlink failed')
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(module, '_AnonymousScratch', Observed)
    monkeypatch.setattr(module.os, 'unlink', failed)
    try:
        with pytest.raises(OSError, match='unlink failed'):
            with value.open_original(ACC): pass
        assert value.stats()['scratch_bytes'] == expected[ACC]['size']
        assert value.stats()['cleanup_failures'] == 1
        assert not value.stats()['scratch_closed']
        assert list((tmp_path / 'scratch').iterdir())
        with pytest.raises(ValueError): value.assert_closed()
    finally:
        monkeypatch.setattr(module.os, 'unlink', original_unlink)
        for scratch in allocated: scratch.close()


def test_raw_store_hashes_failed_parse_bytes_and_binds_inventory(tmp_path):
    raw = b'unparsed original'
    expected, _, _ = fixture(tmp_path, raws={ACC: raw})
    path = tmp_path / 'raw.bin'; path.write_bytes(raw); path.chmod(0o400)
    cls = store_class('RawOriginalStore')
    value = cls(expected, {ACC: path}, check=lambda: 10)
    with value.open_original(ACC) as view: assert view[:] == raw
    assert value.stats()['retained_bytes'] == len(raw) and value.stats()['scratch_bytes'] == 0
    rewrite(path, b'X' * len(raw))
    with pytest.raises(ValueError):
        with value.open_original(ACC): pass
    value.assert_closed()


def test_scratch_inode_is_anonymous_before_first_write(tmp_path, monkeypatch):
    value, _, _, _ = store(tmp_path)
    module = importlib.import_module(MODULE)
    original_class = module._AnonymousScratch
    observed = []
    class Observed(original_class):
        def write(self, content):
            observed.append(os.fstat(self.fileno()).st_nlink)
            assert not list((tmp_path / 'scratch').iterdir())
            return super().write(content)
    monkeypatch.setattr(module, '_AnonymousScratch', Observed)
    with value.open_original(ACC): pass
    assert observed and set(observed) == {0}


def test_failed_scratch_identity_read_cannot_release_or_delete_unverified_inode(tmp_path, monkeypatch):
    value, expected, _, _ = store(tmp_path)
    module = importlib.import_module(MODULE)
    real_open, real_fstat, original_class = module.os.open, module.os.fstat, module._AnonymousScratch
    allocated, scratch_fds = [], set()
    class Observed(original_class):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs); allocated.append(self)
    def opened(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if str(path).startswith('eventedge-original-'): scratch_fds.add(fd)
        return fd
    def failed(fd):
        if fd in scratch_fds: raise OSError('identity unavailable')
        return real_fstat(fd)
    monkeypatch.setattr(module, '_AnonymousScratch', Observed)
    monkeypatch.setattr(module.os, 'open', opened)
    monkeypatch.setattr(module.os, 'fstat', failed)
    try:
        with pytest.raises((ValueError, OSError)):
            with value.open_original(ACC): pass
        assert value.stats()['scratch_bytes'] == expected[ACC]['size']
        assert value.stats()['cleanup_failures'] == 1
        assert not value.stats()['scratch_closed']
        assert list((tmp_path / 'scratch').iterdir())
    finally:
        monkeypatch.setattr(module.os, 'fstat', real_fstat)
        for scratch in allocated:
            if scratch._directory is not None: os.close(scratch._directory)
        for path in (tmp_path / 'scratch').iterdir(): path.unlink()


def descriptor_live(fd):
    try: os.fstat(fd)
    except OSError: return False
    return True


@pytest.mark.parametrize('helper', ['original', 'directory'])
def test_initial_traversal_identity_failure_closes_root(tmp_path, monkeypatch, helper):
    module = importlib.import_module(MODULE)
    real_open, real_fstat = module.os.open, module.os.fstat
    opened = []
    def observed(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs); opened.append(fd); return fd
    def failed(fd): raise OSError('initial identity failed')
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'open', observed); patch.setattr(module.os, 'fstat', failed)
        with pytest.raises(OSError, match='initial identity failed'):
            if helper == 'original': module._open_original(tmp_path / 'payload', 1, lambda: 10)
            else: module._open_directory(tmp_path, lambda: 10)
    leaked = [fd for fd in opened if descriptor_live(fd)]
    for fd in leaked: os.close(fd)
    assert not leaked


def test_final_traversal_directory_close_fault_does_not_lose_leaf(tmp_path, monkeypatch):
    module = importlib.import_module(MODULE)
    path = tmp_path / 'payload'; path.write_bytes(b'x'); path.chmod(0o400)
    real_open, real_close = module.os.open, module.os.close
    leaves = []
    def observed(name, *args, **kwargs):
        fd = real_open(name, *args, **kwargs)
        if str(name) == path.name: leaves.append(fd)
        return fd
    def failed(fd):
        if leaves and fd not in leaves:
            real_close(fd); raise OSError('final directory close failed')
        return real_close(fd)
    with monkeypatch.context() as patch:
        patch.setattr(module.os, 'open', observed); patch.setattr(module.os, 'close', failed)
        with pytest.raises(OSError, match='final directory close failed'):
            module._open_original(path, 1, lambda: 10)
    leaked = [fd for fd in leaves if descriptor_live(fd)]
    for fd in leaked: os.close(fd)
    assert not leaked


def test_final_recheck_seals_admission_before_callbacks_without_deadlock(tmp_path, monkeypatch):
    import threading
    expected, paths, receipts = fixture(tmp_path)
    start, finished = threading.Event(), threading.Event()
    refusal, unexpected = [], []
    state = {'armed': False}
    def check():
        if state['armed']:
            state['armed'] = False
            start.set()
            assert finished.wait(1), 'final callback held admission lock while waiting'
        return 10
    value = store_class()(expected, paths, receipts, check=check, scratch_directory=private_scratch(tmp_path))
    def worker():
        try:
            assert start.wait(1)
            with value.open_original(ACC): unexpected.append('decode admitted during seal')
        except ValueError as error: refusal.append(str(error))
        except BaseException as error: unexpected.append(error)
        finally: finished.set()
    thread = threading.Thread(target=worker); thread.start()
    state['armed'] = True
    try: value.recheck_persistent_identities()
    finally: start.set(); thread.join(2)
    assert refusal == ['original_store_sealed'] and not unexpected
    assert value.stats()['sealed'] is True and value.stats()['scratch_closed']
    with pytest.raises(ValueError, match='original_store_sealed'):
        with value.open_original(ACC): pass
    value.recheck_persistent_identities()
    assert value.stats()['sealed'] is True


def test_failed_final_verification_permanently_refuses_future_originals(tmp_path):
    value, _, paths, _ = store(tmp_path)
    with value.open_original(ACC): pass
    path = paths[ACC]; content = path.read_bytes(); path.unlink(); path.write_bytes(content); path.chmod(0o400)
    with pytest.raises(ValueError): value.recheck_persistent_identities()
    with pytest.raises(ValueError, match='original_store_sealed'):
        with value.open_original(ACC): pass
    with pytest.raises(ValueError): value.recheck_persistent_identities()


def test_traversal_close_uncertainty_is_owned_and_never_retried(tmp_path, monkeypatch):
    module = importlib.import_module(MODULE)
    real_close = module.os.close
    owner = module._DescriptorOwner()
    attempts, unclosed = [], []
    def failed(fd):
        attempts.append(fd)
        if not unclosed:
            unclosed.append(fd)
            raise OSError('close result uncertain')
        return real_close(fd)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module.os, 'close', failed)
            with pytest.raises(OSError, match='close result uncertain') as failure:
                module._open_directory(tmp_path, lambda: 10, descriptor_owner=owner)
        assert owner.uncertain == unclosed
        assert failure.value.descriptor_owner is owner
        assert not owner.live
        assert attempts.count(unclosed[0]) == 1
    finally:
        for fd in unclosed:
            if descriptor_live(fd): real_close(fd)


def test_payload_close_uncertainty_still_closes_scratch_without_retrying_reused_fd(tmp_path, monkeypatch):
    value, _, _, _ = store(tmp_path)
    module = importlib.import_module(MODULE)
    real_close, real_open = module.os.close, module.os.open
    replacement = []
    attempts = []
    try:
        with value.open_original(ACC):
            payload_fd = next(fd for fd in value._descriptor_owner.live
                              if os.fstat(fd).st_nlink and stat.S_ISREG(os.fstat(fd).st_mode))
            # The payload is the only regular descriptor with a persistent link;
            # the scratch directory is a directory, and scratch is unlinked.
            def failed(fd):
                attempts.append(fd)
                real_close(fd)
                if fd == payload_fd:
                    replacement.append(real_open(tmp_path / 'replacement', os.O_RDWR | os.O_CREAT, 0o600))
                    raise OSError('payload close uncertain')
            monkeypatch.setattr(module.os, 'close', failed)
    except OSError as error:
        assert str(error) == 'payload close uncertain'
    else:
        pytest.fail('payload close fault was not enforced')
    finally:
        monkeypatch.setattr(module.os, 'close', real_close)
        for fd in replacement:
            assert descriptor_live(fd)
            real_close(fd)
    assert attempts.count(payload_fd) == 1
    assert not value._descriptor_owner.live and value._descriptor_owner.uncertain == [payload_fd]
    assert value._failed_resources and all(getattr(resource, 'closed', False) for resource in value._failed_resources)
    assert value.stats()['cleanup_failures'] and not value.stats()['scratch_closed']
    with pytest.raises(ValueError): value.assert_closed()
