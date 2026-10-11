"""Closed source receipts, finite physical ownership, and original deadlines."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import hashlib
import os
import stat
import time

import pytest

try:
    from tradingagents.strategies.data_sources import filing_spool as spool
except ImportError:
    spool = None
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget

IDENTITY = {'accession': '0002065397-26-000001', 'form': '8-K',
            'filing_date': '2026-09-30', 'source_url': 'https://www.sec.gov/fixture.txt'}

class Response:
    def __init__(self, chunks=(b'abc', b'def'), *, on_eof=None, on_close=None):
        self.chunks, self.closed = chunks, False
        self.on_eof, self.on_close = on_eof, on_close
    def iter_content(self, chunk_size):
        assert chunk_size == 65536
        yield from self.chunks
        if self.on_eof: self.on_eof()
    def close(self):
        self.closed = True
        if self.on_close: self.on_close()


def test_completion_requires_eof_and_both_closures_and_private_files(tmp_path):
    assert spool is not None, "closed submission spool API is missing"
    response = Response()
    with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5) as owner:
        record = spool.spool_response(response, identity=IDENTITY)
        assert response.closed and record.complete and record.size == 6
        assert record.sha256 == hashlib.sha256(b'abcdef').hexdigest()
        assert record.path.read_bytes() == b'abcdef'
        assert stat.S_IMODE(record.path.stat().st_mode) == 0o400
        assert stat.S_IMODE(record.path.parent.stat().st_mode) == 0o700
        assert owner.stats()['completed_objects'] == 1
        assert owner.stats()['total_bytes'] == 6
        owner.discard(record)
        assert not record.path.exists() and owner.stats()['total_bytes'] == 0


@pytest.mark.parametrize('late', ['eof', 'close'])
def test_late_eof_or_response_close_cannot_publish_receipt(tmp_path, late):
    now = [10.0]
    expire = lambda: now.__setitem__(0, 20.0)
    response = Response(**{'on_'+late: expire})
    with provider_budget('edgar', 20.0, clock=lambda: now[0], limits=()):
        with spool.submission_spool_scope(tmp_path, original_deadline=20.0) as owner:
            with pytest.raises(SourceFetchError) as caught:
                spool.spool_response(response, identity=IDENTITY)
            assert caught.value.reason_code == 'timeout'
            assert response.closed and owner.stats()['completed_objects'] == 0
            assert owner.stats()['failed_objects'] == 1 and owner.stats()['total_bytes'] == 0
            assert not list(owner.root.iterdir())


@pytest.mark.parametrize('kind', ['decoded_cap', 'chunk_type', 'quota', 'close_error'])
def test_failed_streams_close_and_rollback_all_bytes(tmp_path, kind):
    response = Response()
    maximum, quota = 100, 100
    if kind == 'decoded_cap': maximum = 5
    if kind == 'chunk_type': response.chunks = (b'abc', 'secret provider text')
    if kind == 'quota': quota = 5
    if kind == 'close_error': response.on_close = lambda: (_ for _ in ()).throw(OSError('secret provider text'))
    with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5, physical_limit=quota) as owner:
        with pytest.raises(SourceFetchError) as caught:
            spool.spool_response(response, identity=IDENTITY, max_bytes=maximum)
        assert 'secret' not in str(caught.value)
        assert response.closed and owner.stats()['total_bytes'] == 0
        assert owner.stats()['completed_objects'] == 0 and owner.stats()['failed_objects'] == 1
        assert not list(owner.root.iterdir())


def test_retained_aliases_keep_one_inode_charged_after_parent_unlink(tmp_path):
    capture = tmp_path / 'capture'
    capture.mkdir()
    with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5, physical_limit=12) as owner:
        record = spool.spool_response(Response(), identity=IDENTITY)
        first = owner.retain_hardlink(record, capture/'first')
        second = owner.retain_hardlink(record, capture/'second')
        assert os.stat(first.path).st_ino == os.stat(record.path).st_ino
        owner.discard(record)
        assert owner.stats()['retained_bytes'] == 6 and owner.stats()['transient_bytes'] == 0
        child = owner.reserve_child_copy(record)
        assert owner.stats()['total_bytes'] == 12 and owner.stats()['active_child_copies'] == 1
        with pytest.raises(SourceFetchError): owner.reserve_child_copy(record)
        owner.release_child_copy(child)
        owner.release_retained(first)
        assert owner.stats()['total_bytes'] == 6
        owner.release_retained(second)
        assert owner.stats()['total_bytes'] == 0


def test_partial_write_failure_never_completes_and_releases_reservation(tmp_path, monkeypatch):
    original, calls = os.write, []
    def partial(fd, raw):
        calls.append(1)
        if len(calls) > 1: raise OSError('secret disk error')
        return original(fd, raw[:1])
    with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5) as owner:
        monkeypatch.setattr(spool.os, 'write', partial)
        response = Response()
        with pytest.raises(SourceFetchError): spool.spool_response(response, identity=IDENTITY)
        assert response.closed and owner.stats()['total_bytes'] == 0
        assert owner.stats()['completed_objects'] == 0 and not list(owner.root.iterdir())


def test_concurrent_writers_share_one_finite_ledger(tmp_path):
    with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5, physical_limit=30) as owner:
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(copy_context().run, spool.spool_response, Response(), identity=IDENTITY) for _ in range(16)]
            accepted = []
            for future in futures:
                try: accepted.append(future.result())
                except SourceFetchError: pass
        # Reservations from competing partial streams can reject earlier than
        # the theoretical five complete files; there is deliberately no wait.
        assert 1 <= len(accepted) <= 5
        assert owner.stats()['total_bytes'] == 6*len(accepted)
        assert owner.stats()['peak_total_bytes'] <= 30
        assert owner.stats()['completed_objects'] == len(accepted)
        assert owner.stats()['failed_objects'] == 16-len(accepted)
    assert owner.stats()['total_bytes'] == 0


def test_scope_close_keeps_blocked_writer_charge_until_writer_closes(tmp_path):
    from threading import Event
    entered, resume = Event(), Event()
    class Blocked(Response):
        def iter_content(self, chunk_size):
            yield b'abc'
            entered.set()
            assert resume.wait(3)
            yield b'def'
    response = Blocked()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5) as owner:
            future = pool.submit(copy_context().run, spool.spool_response, response, identity=IDENTITY)
            assert entered.wait(3)
            try:
                with pytest.raises(SourceFetchError): owner.close()
                assert owner.stats()['active_parent_writers'] == 1
                assert owner.stats()['total_bytes'] == 3 and owner.stats()['closed'] is False
            finally:
                resume.set()
            with pytest.raises(SourceFetchError): future.result(timeout=3)
            assert response.closed and owner.stats()['total_bytes'] == 0
            assert owner.stats()['active_parent_writers'] == 0
        assert owner.stats()['closed'] is True


def test_parent_charge_cannot_be_released_while_child_owns_copy(tmp_path):
    with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5) as owner:
        record = spool.spool_response(Response(), identity=IDENTITY)
        lease = owner.reserve_child_copy(record)
        owner.discard(record)
        assert owner.stats()['total_bytes'] == 12 and record.path.exists()
        with pytest.raises(SourceFetchError): owner.close()
        owner.release_child_copy(lease)
        assert owner.stats()['total_bytes'] == 0 and not record.path.exists()


def test_completed_parent_is_readonly_and_forged_receipt_not_owned(tmp_path):
    from dataclasses import replace
    with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5) as owner:
        record = spool.spool_response(Response(), identity=IDENTITY)
        assert stat.S_IMODE(record.path.stat().st_mode) == 0o400
        assert owner.validate_completed(record) is record
        with pytest.raises(SourceFetchError): owner.validate_completed(replace(record, sha256='0'*64))
        with pytest.raises(TypeError): record.identity['form'] = '10-K'


def test_discard_with_open_parent_keeps_physical_charge_until_reader_closes(tmp_path):
    with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5) as owner:
        record = spool.spool_response(Response(), identity=IDENTITY)
        with owner.open_completed(record) as source:
            owner.discard(record)
            assert owner.stats()['parent_bytes'] == 6 and record.path.exists()
            with pytest.raises(SourceFetchError): owner.close()
            assert source.read() == b'abcdef' and owner.stats()['active_parent_readers'] == 1
        assert not record.path.exists() and owner.stats()['total_bytes'] == 0
        assert owner.stats()['closed'] is True


def test_symlink_replacement_cannot_escape_owned_closed_parent(tmp_path):
    with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5) as owner:
        record = spool.spool_response(Response(), identity=IDENTITY)
        unrelated = tmp_path/'unrelated'
        unrelated.write_bytes(b'abcdef')
        record.path.unlink()
        record.path.symlink_to(unrelated)
        with pytest.raises(SourceFetchError): owner.validate_completed(record)
        with pytest.raises(SourceFetchError):
            with owner.open_completed(record): pytest.fail('unowned path opened')
        with pytest.raises(SourceFetchError): owner.retain_hardlink(record, tmp_path/'capture')
        assert not (tmp_path/'capture').exists() and unrelated.read_bytes() == b'abcdef'


def test_failed_hardlink_publication_never_reclassifies_parent_charge(tmp_path):
    with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5) as owner:
        record = spool.spool_response(Response(), identity=IDENTITY)
        occupied = tmp_path/'capture'
        occupied.write_bytes(b'original')
        with pytest.raises(SourceFetchError): owner.retain_hardlink(record, occupied)
        assert occupied.read_bytes() == b'original'
        assert owner.stats()['parent_bytes'] == 6 and owner.stats()['retained_bytes'] == 0


def test_exact_decoded_cap_accepts_last_byte_and_rejects_one_extra(tmp_path):
    with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5) as owner:
        record = spool.spool_response(Response(), identity=IDENTITY, max_bytes=6)
        assert record.size == 6
        with pytest.raises(SourceFetchError): spool.spool_response(Response((b'abcdef', b'g')), identity=IDENTITY, max_bytes=6)
        assert owner.stats()['total_bytes'] == 6


def test_late_file_close_never_publishes_a_receipt(tmp_path, monkeypatch):
    now = [10.0]
    original = os.close
    def late(fd):
        result = original(fd)
        now[0] = 20.0
        return result
    with provider_budget('edgar', 20.0, clock=lambda: now[0], limits=()):
        with spool.submission_spool_scope(tmp_path, original_deadline=20.0) as owner:
            monkeypatch.setattr(spool.os, 'close', late)
            with pytest.raises(SourceFetchError) as error: spool.spool_response(Response(), identity=IDENTITY)
            assert error.value.reason_code == 'timeout'
            assert owner.stats()['completed_objects'] == 0 and owner.stats()['total_bytes'] == 0


def test_missing_completed_parent_is_a_bounded_typed_failure(tmp_path):
    with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5) as owner:
        record = spool.spool_response(Response(), identity=IDENTITY)
        record.path.unlink()
        with pytest.raises(SourceFetchError) as error: owner.validate_completed(record)
        assert str(tmp_path) not in str(error.value)
        owner.discard(record)


def test_completed_metadata_survives_close_without_exposing_owner_or_paths(tmp_path):
    import json
    with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5) as owner:
        first = spool.spool_response(Response(), identity=IDENTITY)
        second = spool.spool_response(Response((b'not valid framing',)), identity=IDENTITY)
        metadata = owner.completed_metadata()
        assert metadata == [
            {'identity': IDENTITY, 'observed_at': first.observed_at, 'size': 6,
             'sha256': first.sha256, 'complete': True},
            {'identity': IDENTITY, 'observed_at': second.observed_at, 'size': 17,
             'sha256': second.sha256, 'complete': True}]
        metadata[0]['identity']['form'] = 'changed'
        metadata.clear()
    closed = owner.completed_metadata()
    assert len(closed) == 2 and closed[0]['identity']['form'] == '8-K'
    assert str(tmp_path) not in json.dumps(closed) and 'owner' not in closed[0]


def test_scope_cleanup_continues_after_first_parent_unlink_error(tmp_path,monkeypatch):
    from pathlib import Path
    owner=spool.SpoolScope(tmp_path,original_deadline=time.monotonic()+5,physical_limit=100)
    first=owner.spool_response(Response(),identity=IDENTITY,max_bytes=100)
    second=owner.spool_response(Response(),identity=IDENTITY,max_bytes=100)
    native=Path.unlink
    def denied(path,*args,**kwargs):
        if path==first.path: raise PermissionError('secret cleanup denial')
        return native(path,*args,**kwargs)
    try:
        monkeypatch.setattr(Path,'unlink',denied)
        with pytest.raises(SourceFetchError):owner.close()
        assert first.path.exists() and not second.path.exists()
        assert owner.stats()['cleanup_failures']>0 and owner.stats()['closed'] is False
        assert owner.stats()['parent_bytes']==6
    finally:
        monkeypatch.setattr(Path,'unlink',native)
        owner.close()


def test_failed_directory_removal_never_marks_scope_closed(tmp_path,monkeypatch):
    from pathlib import Path
    owner=spool.SpoolScope(tmp_path,original_deadline=time.monotonic()+5,physical_limit=100)
    native=Path.rmdir
    def denied(path):
        if path==owner.root:raise PermissionError('secret directory denial')
        return native(path)
    try:
        monkeypatch.setattr(Path,'rmdir',denied)
        with pytest.raises(SourceFetchError):owner.close()
        assert owner.root.exists() and owner.stats()['closed'] is False
        assert owner.stats()['cleanup_failures']>0
    finally:
        monkeypatch.setattr(Path,'rmdir',native)
        owner.close()


def test_later_child_release_cannot_hide_another_failed_parent_unlink(tmp_path,monkeypatch):
    from pathlib import Path
    owner=spool.SpoolScope(tmp_path,original_deadline=time.monotonic()+5,physical_limit=100)
    first=owner.spool_response(Response(),identity=IDENTITY,max_bytes=100)
    second=owner.spool_response(Response(),identity=IDENTITY,max_bytes=100)
    child=owner.reserve_child_copy(second)
    native=Path.unlink
    def denied(path,*args,**kwargs):
        if path==first.path:raise PermissionError('secret unlink denial')
        return native(path,*args,**kwargs)
    try:
        monkeypatch.setattr(Path,'unlink',denied)
        with pytest.raises(SourceFetchError):owner.close()
        owner.release_child_copy(child)
        assert owner.stats()['closed'] is False and owner.stats()['total_bytes']==6
        assert first.path.exists() and not second.path.exists()
    finally:
        monkeypatch.setattr(Path,'unlink',native)
        owner.close()


def test_failed_stream_unlink_error_finishes_writer_but_keeps_parent_charge(tmp_path,monkeypatch):
    from pathlib import Path
    owner=spool.SpoolScope(tmp_path,original_deadline=time.monotonic()+5,physical_limit=100)
    native=Path.unlink
    def denied(path,*args,**kwargs):
        if path.parent==owner.root:raise PermissionError('secret partial unlink')
        return native(path,*args,**kwargs)
    response=Response((b'abc','invalid chunk'))
    try:
        monkeypatch.setattr(Path,'unlink',denied)
        with pytest.raises(SourceFetchError) as error:owner.spool_response(response,identity=IDENTITY,max_bytes=100)
        assert 'secret' not in str(error.value)
        assert response.closed and owner.stats()['active_parent_writers']==0
        assert owner.stats()['failed_objects']==1 and owner.stats()['completed_objects']==0
        assert owner.stats()['total_bytes']==3 and owner.stats()['cleanup_failures']>0
    finally:
        monkeypatch.setattr(Path,'unlink',native)
        owner.close()
    assert owner.stats()['closed'] is True and owner.stats()['total_bytes']==0


def test_failed_stream_uncertain_fd_close_stays_charged_until_explicit_cleanup(tmp_path,monkeypatch):
    owner=spool.SpoolScope(tmp_path,original_deadline=time.monotonic()+5,physical_limit=100)
    native_write,native_close=os.write,os.close
    owned=[]
    def write(fd,data):
        owned.append(fd)
        return native_write(fd,data)
    def denied(fd):
        if fd in owned:raise PermissionError('secret close denial')
        return native_close(fd)
    response=Response((b'abc','invalid chunk'))
    try:
        monkeypatch.setattr(spool.os,'write',write)
        monkeypatch.setattr(spool.os,'close',denied)
        with pytest.raises(SourceFetchError) as error:owner.spool_response(response,identity=IDENTITY,max_bytes=100)
        assert 'secret' not in str(error.value)
        assert response.closed and owner.stats()['failed_objects']==1
        assert owner.stats()['active_parent_writers']==1
        assert owner.stats()['total_bytes']==3 and owner.stats()['cleanup_failures']>0
        assert os.fstat(owned[0]).st_size==3
        with pytest.raises(SourceFetchError):owner.close()
        assert owner.stats()['closed'] is False
    finally:
        monkeypatch.setattr(spool.os,'close',native_close)
        owner.close()
    assert owner.stats()['closed'] is True and owner.stats()['active_parent_writers']==0
    assert owner.stats()['total_bytes']==0


def test_close_error_after_actual_fd_closure_never_publishes_or_leaks(tmp_path,monkeypatch):
    owner=spool.SpoolScope(tmp_path,original_deadline=time.monotonic()+5,physical_limit=100)
    native_write,native_close=os.write,os.close
    owned=[]
    def write(fd,data):
        owned.append(fd)
        return native_write(fd,data)
    def late_error(fd):
        result=native_close(fd)
        if fd in owned:raise OSError('secret post-close error')
        return result
    try:
        monkeypatch.setattr(spool.os,'write',write)
        monkeypatch.setattr(spool.os,'close',late_error)
        with pytest.raises(SourceFetchError) as error:owner.spool_response(Response(),identity=IDENTITY,max_bytes=100)
        assert 'secret' not in str(error.value)
        assert owner.stats()['completed_objects']==0 and owner.stats()['failed_objects']==1
        assert owner.stats()['active_parent_writers']==0 and owner.stats()['total_bytes']==0
        assert not list(owner.root.iterdir())
    finally:
        monkeypatch.setattr(spool.os,'close',native_close)
        owner.close()


def test_uncertain_reader_close_keeps_pin_and_charge_until_explicit_cleanup(tmp_path,monkeypatch):
    owner=spool.SpoolScope(tmp_path,original_deadline=time.monotonic()+5,physical_limit=100)
    record=owner.spool_response(Response(),identity=IDENTITY,max_bytes=100)
    native_fdopen,native_close=os.fdopen,os.close
    owned=[]
    def fdopen(fd,*args,**kwargs):
        owned.append(fd)
        kwargs['closefd']=False
        return native_fdopen(fd,*args,**kwargs)
    def denied(fd):
        if fd in owned:raise PermissionError('secret reader close denial')
        return native_close(fd)
    try:
        monkeypatch.setattr(spool.os,'fdopen',fdopen)
        monkeypatch.setattr(spool.os,'close',denied)
        with pytest.raises(SourceFetchError):
            with owner.open_completed(record) as source:assert source.read()==b'abcdef'
        assert owner.stats()['active_parent_readers']==1
        assert owner.stats()['total_bytes']==6 and owner.stats()['cleanup_failures']>0
        owner.discard(record)
        assert record.path.exists()
        with pytest.raises(SourceFetchError):owner.close()
        assert owner.stats()['closed'] is False
    finally:
        monkeypatch.setattr(spool.os,'close',native_close)
        owner.close()
        for fd in owned:
            try:native_close(fd)
            except OSError:pass
    assert owner.stats()['closed'] is True and owner.stats()['active_parent_readers']==0
    assert owner.stats()['total_bytes']==0
