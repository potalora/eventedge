"""Independent encoded inode ownership under the existing physical spool cap."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
import gzip
import hashlib
import os
from pathlib import Path
import stat
from threading import Event, RLock
import time

import pytest

from tradingagents.strategies.data_sources import filing_spool as spool
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget

IDENTITY = {'accession': '0002065397-26-000001', 'form': '8-K',
            'filing_date': '2026-09-30', 'source_url': 'https://www.sec.gov/fixture.txt'}


class Response:
    def __init__(self, raw):
        self.raw = raw

    def iter_content(self, chunk_size):
        for start in range(0, len(self.raw), chunk_size):
            yield self.raw[start:start+chunk_size]

    def close(self):
        pass


def owner_record(tmp_path, raw=b'abcdef', limit=4*1024**3):
    owner = spool.SpoolScope(tmp_path, original_deadline=time.monotonic()+30,
                             physical_limit=limit)
    return owner, owner.spool_response(Response(raw), identity=IDENTITY, max_bytes=len(raw))


@pytest.mark.parametrize('raw', [b'\x00\xff\r\n</TEXT>\x80', bytes(range(256))*1000, b'a'*200000],
                         ids=['binary', 'all-bytes', 'compressible'])
def test_exact_raw_roundtrip_keeps_independent_encoded_inode_charged(tmp_path, raw):
    owner, record = owner_record(tmp_path, raw)
    destination = tmp_path/'original.gz'
    try:
        lease = owner.retain_encoded(record, destination)
        encoded = destination.read_bytes()
        assert isinstance(lease, spool.EncodedOriginalLease)
        assert lease.record is record and lease.path == destination
        assert lease.encoding == 'gzip_original_v1'
        assert lease.size == len(encoded) and lease.sha256 == hashlib.sha256(encoded).hexdigest()
        assert gzip.decompress(encoded) == raw
        assert encoded[:10] == b'\x1f\x8b\x08\x00\x00\x00\x00\x00\x04\xff'
        assert stat.S_IMODE(destination.stat().st_mode) == 0o400
        assert destination.stat().st_ino != record.path.stat().st_ino
        assert owner.stats()['total_bytes'] == len(raw)+lease.size
        assert owner.stats()['parent_bytes'] == len(raw)
        owner.discard(record)
        assert not record.path.exists()
        assert owner.stats()['retained_bytes'] == lease.size
        assert owner.stats()['transient_bytes'] == 0
        owner.close()
        assert destination.exists() and owner.stats()['retained_bytes'] == lease.size
        owner.release_encoded(lease)
        assert owner.stats()['total_bytes'] == 0 and not destination.exists()
    finally:
        owner.close()


def test_incompressible_expansion_cannot_borrow_unreleased_raw_capacity(tmp_path):
    raw = os.urandom(65536)
    owner, record = owner_record(tmp_path, raw, limit=2*len(raw))
    try:
        with pytest.raises(SourceFetchError): owner.retain_encoded(record, tmp_path/'original.gz')
        assert not (tmp_path/'original.gz').exists()
        assert owner.stats()['parent_bytes'] == len(raw)
        assert owner.stats()['retained_bytes'] == 0
        assert owner.stats()['peak_total_bytes'] <= 2*len(raw)
    finally:
        owner.close()


def test_external_reservation_precedes_each_bounded_write_without_owner_lock(tmp_path, monkeypatch):
    raw = os.urandom(200000)
    owner, record = owner_record(tmp_path, raw)
    capture_lock = RLock()
    native_write = os.write
    reserved, written, staged = [], [], []
    def reserve(size):
        assert capture_lock._is_owned()
        assert not owner._lock._is_owned()
        assert 0 < size <= 65536
        reserved.append(size)
    def write(fd, chunk):
        assert sum(reserved) >= sum(written)+len(chunk)
        assert owner.stats()['retained_bytes'] == 0
        assert owner.stats()['parent_bytes'] >= len(raw)+sum(written)+len(chunk)
        result = native_write(fd, chunk)
        written.append(result)
        staged.append(owner.stats()['parent_bytes'])
        return result
    try:
        monkeypatch.setattr(spool.os, 'write', write)
        with capture_lock:
            lease = owner.retain_encoded(record, tmp_path/'original.gz', reserve_external=reserve)
        assert sum(reserved) == sum(written) == lease.size
        assert max(staged) == len(raw)+lease.size
    finally:
        owner.close()


def test_external_quota_rejection_cleans_staging_and_does_not_publish(tmp_path):
    owner, record = owner_record(tmp_path)
    def deny(size):
        raise RuntimeError('private capture full')
    try:
        with pytest.raises(SourceFetchError) as error:
            owner.retain_encoded(record, tmp_path/'original.gz', reserve_external=deny)
        assert 'private' not in str(error.value)
        assert owner.stats()['total_bytes'] == record.size
        assert not (tmp_path/'original.gz').exists()
    finally:
        owner.close()


def test_concurrent_child_copy_counts_alongside_raw_and_encoded_staging(tmp_path):
    raw = os.urandom(65536)
    owner, record = owner_record(tmp_path, raw, limit=3*len(raw)+1000)
    entered, resume = Event(), Event()
    def reserve(size):
        entered.set()
        assert resume.wait(3)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(copy_context().run, owner.retain_encoded, record,
                                 tmp_path/'original.gz', reserve_external=reserve)
            assert entered.wait(3)
            child = owner.reserve_child_copy(record)
            assert owner.stats()['total_bytes'] >= 2*len(raw)
            resume.set()
            lease = future.result(timeout=3)
        assert owner.stats()['total_bytes'] == 2*len(raw)+lease.size
        owner.discard(record)
        assert record.path.exists()
        owner.release_child_copy(child)
        assert owner.stats()['total_bytes'] == lease.size and not record.path.exists()
    finally:
        resume.set()
        owner.close()


@pytest.mark.parametrize('failure', ['partial_write', 'cancellation', 'deadline'])
def test_interrupted_encoding_cleans_staging_without_publishing(tmp_path, monkeypatch, failure):
    owner, record = owner_record(tmp_path, os.urandom(200000))
    native_write = os.write
    calls = []
    def interrupt(fd, chunk):
        calls.append(1)
        if len(calls) == 1:
            return native_write(fd, chunk[:1])
        if failure == 'cancellation': raise KeyboardInterrupt()
        if failure == 'deadline': owner.original_deadline = time.monotonic()-1
        else: raise OSError('private storage fault')
        return native_write(fd, chunk)
    try:
        monkeypatch.setattr(spool.os, 'write', interrupt)
        with pytest.raises(KeyboardInterrupt if failure == 'cancellation' else SourceFetchError):
            owner.retain_encoded(record, tmp_path/'original.gz')
        assert not (tmp_path/'original.gz').exists()
        assert owner.stats()['total_bytes'] == record.size
        assert owner.stats()['retained_bytes'] == 0
        assert owner.stats()['active_parent_writers'] == 0
    finally:
        owner.close()


def test_raw_hash_mismatch_cannot_become_a_retained_encoded_lease(tmp_path):
    owner, record = owner_record(tmp_path)
    os.chmod(record.path, 0o600)
    record.path.write_bytes(b'fedcba')
    os.chmod(record.path, 0o400)
    try:
        with pytest.raises(SourceFetchError): owner.retain_encoded(record, tmp_path/'original.gz')
        assert owner.stats()['total_bytes'] == record.size
        assert not (tmp_path/'original.gz').exists()
    finally:
        owner.close()


@pytest.mark.parametrize('kind', ['file', 'symlink', 'missing'])
def test_encoded_release_refuses_substituted_or_missing_owned_name(tmp_path, kind):
    owner, record = owner_record(tmp_path)
    destination, saved = tmp_path/'original.gz', tmp_path/'saved.gz'
    lease = owner.retain_encoded(record, destination)
    destination.rename(saved)
    foreign = tmp_path/'foreign'
    foreign.write_bytes(b'foreign')
    if kind == 'file': destination.write_bytes(b'foreign')
    if kind == 'symlink': destination.symlink_to(foreign)
    try:
        with pytest.raises(SourceFetchError): owner.release_encoded(lease)
        assert owner.stats()['retained_bytes'] == lease.size
        assert foreign.read_bytes() == b'foreign'
        if kind == 'file': assert destination.read_bytes() == b'foreign'
        if kind == 'symlink': assert destination.is_symlink()
        assert owner.stats()['cleanup_failures'] > 0
    finally:
        if destination.exists() or destination.is_symlink(): destination.unlink()
        saved.rename(destination)
        owner.release_encoded(lease)
        owner.close()


def test_unknown_or_forged_encoded_lease_cannot_remove_payload(tmp_path):
    owner, record = owner_record(tmp_path)
    lease = owner.retain_encoded(record, tmp_path/'original.gz')
    try:
        with pytest.raises(ValueError): owner.release_encoded(replace(lease, size=1))
        assert lease.path.exists() and owner.stats()['retained_bytes'] == lease.size
        owner.release_encoded(lease)
        with pytest.raises(ValueError): owner.release_encoded(lease)
    finally:
        owner.close()


def test_failed_encoded_release_unlink_remains_charged(tmp_path, monkeypatch):
    owner, record = owner_record(tmp_path)
    lease = owner.retain_encoded(record, tmp_path/'original.gz')
    native_unlink = Path.unlink
    def deny(path, *args, **kwargs):
        if path == lease.path: raise PermissionError('private unlink failure')
        return native_unlink(path, *args, **kwargs)
    try:
        monkeypatch.setattr(Path, 'unlink', deny)
        with pytest.raises(SourceFetchError): owner.release_encoded(lease)
        assert owner.stats()['retained_bytes'] == lease.size and lease.path.exists()
        assert owner.stats()['cleanup_failures'] > 0
    finally:
        monkeypatch.setattr(Path, 'unlink', native_unlink)
        owner.release_encoded(lease)
        owner.close()


def test_uncertain_encoded_close_keeps_staging_charge_until_explicit_cleanup(tmp_path, monkeypatch):
    owner, record = owner_record(tmp_path)
    native_write, native_close = os.write, os.close
    output_fds = set()
    def write(fd, chunk):
        output_fds.add(fd)
        return native_write(fd, chunk)
    def deny(fd):
        if fd in output_fds: raise PermissionError('private close failure')
        return native_close(fd)
    try:
        monkeypatch.setattr(spool.os, 'write', write)
        monkeypatch.setattr(spool.os, 'close', deny)
        with pytest.raises(SourceFetchError): owner.retain_encoded(record, tmp_path/'original.gz')
        staged = (tmp_path/'original.gz').stat().st_size
        assert staged > 0
        assert owner.stats()['total_bytes'] == record.size+staged
        assert owner.stats()['retained_bytes'] == 0
        assert owner.stats()['active_parent_writers'] == 1
        with pytest.raises(SourceFetchError): owner.close()
        assert owner.stats()['closed'] is False
    finally:
        monkeypatch.setattr(spool.os, 'close', native_close)
        owner.close()
    assert owner.stats()['total_bytes'] == 0 and not (tmp_path/'original.gz').exists()


def test_owner_close_aborts_encoding_and_keeps_raw_reader_pinned(tmp_path):
    owner, record = owner_record(tmp_path)
    entered, resume = Event(), Event()
    def reserve(size):
        entered.set()
        assert resume.wait(3)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(copy_context().run, owner.retain_encoded, record,
                                 tmp_path/'original.gz', reserve_external=reserve)
            assert entered.wait(3)
            with pytest.raises(SourceFetchError): owner.close()
            assert owner.stats()['active_parent_readers'] == 1
            assert record.path.exists() and owner.stats()['closed'] is False
            resume.set()
            with pytest.raises(SourceFetchError): future.result(timeout=3)
        assert owner.stats()['closed'] is True and owner.stats()['total_bytes'] == 0
        assert not (tmp_path/'original.gz').exists()
    finally:
        resume.set()
        owner.close()


def test_existing_destination_never_replaced(tmp_path):
    owner, record = owner_record(tmp_path)
    destination = tmp_path/'original.gz'
    destination.write_bytes(b'foreign')
    try:
        with pytest.raises(SourceFetchError): owner.retain_encoded(record, destination)
        assert destination.read_bytes() == b'foreign'
        assert owner.stats()['total_bytes'] == record.size
    finally:
        owner.close()


def test_unresolved_encoded_destination_cannot_overwrite_its_ledger_entry(tmp_path):
    owner, record = owner_record(tmp_path)
    destination, saved = tmp_path/'original.gz', tmp_path/'saved.gz'
    lease = owner.retain_encoded(record, destination)
    destination.rename(saved)
    try:
        with pytest.raises(SourceFetchError): owner.retain_encoded(record, destination)
        assert owner.stats()['total_bytes'] == record.size+lease.size
        assert not destination.exists()
    finally:
        if destination.exists(): destination.unlink()
        saved.rename(destination)
        owner.release_encoded(lease)
        owner.close()


def test_encoded_release_refuses_replaced_destination_directory(tmp_path):
    owner, record = owner_record(tmp_path)
    capture = tmp_path/'capture'
    capture.mkdir()
    lease = owner.retain_encoded(record, capture/'original.gz')
    saved = tmp_path/'saved-capture'
    capture.rename(saved)
    capture.mkdir()
    (capture/'original.gz').write_bytes(b'foreign')
    try:
        with pytest.raises(SourceFetchError): owner.release_encoded(lease)
        assert (capture/'original.gz').read_bytes() == b'foreign'
        assert owner.stats()['retained_bytes'] == lease.size
    finally:
        (capture/'original.gz').unlink()
        capture.rmdir()
        saved.rename(capture)
        owner.release_encoded(lease)
        owner.close()


def test_staging_inode_substitution_stays_charged_and_never_removes_foreign_file(tmp_path):
    owner, record = owner_record(tmp_path)
    destination, saved = tmp_path/'original.gz', tmp_path/'saved.gz'
    replaced = []
    def substitute(size):
        if not replaced:
            destination.rename(saved)
            destination.write_bytes(b'foreign')
            replaced.append(True)
    try:
        with pytest.raises(SourceFetchError):
            owner.retain_encoded(record, destination, reserve_external=substitute)
        assert destination.read_bytes() == b'foreign'
        assert owner.stats()['retained_bytes'] == 0
        assert owner.stats()['parent_bytes'] == record.size+saved.stat().st_size
        assert owner.stats()['cleanup_failures'] > 0
        with pytest.raises(SourceFetchError): owner.close()
        assert destination.read_bytes() == b'foreign'
    finally:
        destination.unlink()
        saved.rename(destination)
        owner.close()


def test_failed_staging_unlink_keeps_reserved_bytes_until_owner_cleanup(tmp_path, monkeypatch):
    owner, record = owner_record(tmp_path)
    destination = tmp_path/'original.gz'
    native_unlink = Path.unlink
    def deny(path, *args, **kwargs):
        if path == destination: raise PermissionError('private staging cleanup')
        return native_unlink(path, *args, **kwargs)
    calls = []
    def reserve(size):
        calls.append(size)
        if len(calls) == 2: raise OSError('capture full')
    try:
        monkeypatch.setattr(Path, 'unlink', deny)
        with pytest.raises(SourceFetchError):
            owner.retain_encoded(record, destination, reserve_external=reserve)
        assert destination.exists()
        assert owner.stats()['parent_bytes'] == record.size+destination.stat().st_size
        assert owner.stats()['retained_bytes'] == 0
        assert owner.stats()['cleanup_failures'] > 0
    finally:
        monkeypatch.setattr(Path, 'unlink', native_unlink)
        owner.close()


def test_output_readback_hash_rejects_same_inode_write_corruption(tmp_path, monkeypatch):
    owner, record = owner_record(tmp_path)
    native_write = os.write
    corrupted = []
    def corrupt(fd, chunk):
        if not corrupted:
            chunk = bytes([chunk[0]^1])+chunk[1:]
            corrupted.append(True)
        return native_write(fd, chunk)
    try:
        monkeypatch.setattr(spool.os, 'write', corrupt)
        with pytest.raises(SourceFetchError): owner.retain_encoded(record, tmp_path/'original.gz')
        assert not (tmp_path/'original.gz').exists()
        assert owner.stats()['total_bytes'] == record.size
    finally:
        owner.close()


def test_encoded_close_error_after_confirmed_closure_cleans_without_publication(tmp_path, monkeypatch):
    owner, record = owner_record(tmp_path)
    native_write, native_close = os.write, os.close
    output_fds = set()
    def write(fd, chunk):
        output_fds.add(fd)
        return native_write(fd, chunk)
    def late_error(fd):
        native_close(fd)
        if fd in output_fds: raise OSError('private post-close error')
    try:
        monkeypatch.setattr(spool.os, 'write', write)
        monkeypatch.setattr(spool.os, 'close', late_error)
        with pytest.raises(SourceFetchError): owner.retain_encoded(record, tmp_path/'original.gz')
        assert owner.stats()['total_bytes'] == record.size
        assert owner.stats()['retained_bytes'] == 0
        assert not (tmp_path/'original.gz').exists()
    finally:
        monkeypatch.setattr(spool.os, 'close', native_close)
        owner.close()


def test_readback_mode_change_prevents_encoded_lease_publication(tmp_path, monkeypatch):
    owner, record = owner_record(tmp_path)
    destination = tmp_path/'original.gz'
    native_read = os.read
    def change_mode(fd, size):
        data = native_read(fd, size)
        if not data: os.chmod(destination, 0o600)
        return data
    try:
        monkeypatch.setattr(spool.os, 'read', change_mode)
        with pytest.raises(SourceFetchError): owner.retain_encoded(record, destination)
        assert not destination.exists()
        assert owner.stats()['total_bytes'] == record.size
    finally:
        owner.close()


def test_encoded_extra_hardlink_cannot_release_still_live_inode_charge(tmp_path):
    owner, record = owner_record(tmp_path)
    lease = owner.retain_encoded(record, tmp_path/'original.gz')
    alias = tmp_path/'alias.gz'
    os.link(lease.path, alias)
    try:
        with pytest.raises(SourceFetchError): owner.release_encoded(lease)
        assert lease.path.exists() and alias.exists()
        assert owner.stats()['retained_bytes'] == lease.size
    finally:
        alias.unlink()
        owner.release_encoded(lease)
        owner.close()


def test_same_size_output_rewrite_after_readback_prevents_publication(tmp_path, monkeypatch):
    owner, record = owner_record(tmp_path)
    destination = tmp_path/'original.gz'
    native_read = os.read
    changed = []
    def corrupt_after_eof(fd, size):
        data = native_read(fd, size)
        if not data and not changed:
            original = destination.read_bytes()
            os.chmod(destination, 0o600)
            destination.write_bytes(bytes([original[0]^1])+original[1:])
            os.chmod(destination, 0o400)
            changed.append(True)
        return data
    try:
        monkeypatch.setattr(spool.os, 'read', corrupt_after_eof)
        with pytest.raises(SourceFetchError): owner.retain_encoded(record, destination)
        assert not destination.exists()
        assert owner.stats()['total_bytes'] == record.size
    finally:
        owner.close()


def test_uncertain_raw_reader_close_prevents_encoded_publication(tmp_path, monkeypatch):
    owner, record = owner_record(tmp_path)
    native_fdopen, native_close = os.fdopen, os.close
    readers = set()
    def fdopen(fd, *args, **kwargs):
        readers.add(fd)
        return native_fdopen(fd, *args, **kwargs)
    def deny_reader(fd):
        if fd in readers: raise PermissionError('private raw-reader close')
        return native_close(fd)
    try:
        monkeypatch.setattr(spool.os, 'fdopen', fdopen)
        monkeypatch.setattr(spool.os, 'close', deny_reader)
        with pytest.raises(SourceFetchError): owner.retain_encoded(record, tmp_path/'original.gz')
        assert owner.stats()['retained_bytes'] == 0
        assert owner.stats()['active_parent_readers'] == 1
        assert owner.stats()['total_bytes'] == record.size
        assert not (tmp_path/'original.gz').exists()
        with pytest.raises(SourceFetchError): owner.close()
        assert owner.stats()['closed'] is False
    finally:
        monkeypatch.setattr(spool.os, 'close', native_close)
        owner.close()


def test_late_encoded_close_cannot_publish_after_inherited_deadline(tmp_path, monkeypatch):
    now = [10.0]
    with provider_budget('edgar', 20.0, clock=lambda: now[0], limits=()):
        owner = spool.SpoolScope(tmp_path, original_deadline=20.0, physical_limit=100)
        record = owner.spool_response(Response(b'abcdef'), identity=IDENTITY, max_bytes=6)
        native_close = os.close
        def expire_after_close(fd):
            native_close(fd)
            now[0] = 20.0
        try:
            monkeypatch.setattr(spool.os, 'close', expire_after_close)
            with pytest.raises(SourceFetchError) as error:
                owner.retain_encoded(record, tmp_path/'original.gz')
            assert error.value.reason_code == 'timeout'
            assert owner.stats()['retained_bytes'] == 0
            assert owner.stats()['total_bytes'] == record.size
            assert not (tmp_path/'original.gz').exists()
        finally:
            monkeypatch.setattr(spool.os, 'close', native_close)
            owner.close()


def test_failed_gzip_constructor_cannot_reserve_again_after_staging_cleanup(tmp_path):
    import gc
    owner, record = owner_record(tmp_path)
    reservations = []
    def deny_first(size):
        reservations.append(size)
        if len(reservations) == 1: raise OSError('capture quota failure')
    try:
        with pytest.raises(SourceFetchError):
            owner.retain_encoded(record, tmp_path/'original.gz', reserve_external=deny_first)
        # A partially initialized GzipFile can run close() in its destructor.
        gc.collect()
        assert reservations == [2]
        assert owner.stats()['total_bytes'] == record.size
        assert owner.stats()['active_parent_writers'] == 0
        assert not (tmp_path/'original.gz').exists()
    finally:
        owner.close()
