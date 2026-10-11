"""Two scoped, pure SEC parser subprocesses with private bounded pipes.

HTTP acquisition remains in its existing threads. No process survives its owning
hydration scope, and no failed dispatcher falls back or replaces a child. The
parent's original provider clock governs queueing, IPC, and result acceptance;
children also inherit one real monotonic deadline for their entire lifetime.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import hashlib
import importlib.util
import json
import math
import mmap
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time

POLICY = 'two_processes_v1'
_RAW_LIMIT = 64 * 1024 * 1024
_SPOOL_LIMIT = 512 * 1024 * 1024
_DOCUMENT_LIMIT = 16 * 1024 * 1024
_RESULT_LIMIT = 512 * 1024 * 1024
# Accommodates all 2000 document filenames even with JSON Unicode escaping.
_META_LIMIT = 4 * 1024 * 1024
_CURRENT = ContextVar('sec_parser_dispatch', default=None)


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate parser field')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite parser JSON')))


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def _transfer(fd, size_or_bytes, *, write=False, check, io_lock=None):
    """Nonblocking exact transfer: check the inherited clock every <=50ms."""
    data = memoryview(size_or_bytes) if write else bytearray()
    size, offset = (len(data) if write else size_or_bytes), 0
    while offset < size:
        check()
        readable, writable, _ = select.select([] if write else [fd], [fd] if write else [], [], .05)
        if not (writable if write else readable):
            continue
        try:
            # Cancellation may close and the OS may reuse an FD after select.
            # Check and nonblocking syscall share the exact lock used by close;
            # never hold it during select, queueing, parsing, or process waits.
            with io_lock if io_lock is not None else nullcontext():
                check()
                chunk = os.write(fd, data[offset:offset + 65536]) if write else os.read(fd, min(65536, size - offset))
        except BlockingIOError:
            continue
        if not chunk:
            raise ValueError('incomplete parser pipe')
        if write:
            offset += chunk
        else:
            data.extend(chunk)
            offset += len(chunk)
    check()
    return None if write else bytes(data)


def _frame(fd, value, *, check, limit, io_lock=None):
    data = _encoded(value)
    if len(data) > limit:
        raise ValueError('parser frame limit')
    _transfer(fd, struct.pack('!I', len(data)), write=True, check=check, io_lock=io_lock)
    _transfer(fd, data, write=True, check=check, io_lock=io_lock)


def _read_frame(fd, *, check, limit, io_lock=None):
    size = struct.unpack('!I', _transfer(fd, 4, check=check, io_lock=io_lock))[0]
    if not 0 < size <= limit:
        raise ValueError('parser frame limit')
    return _json(_transfer(fd, size, check=check, io_lock=io_lock))


def _failure(reason='provider_error'):
    from .fetch_errors import SourceFetchError
    return SourceFetchError('SEC parser dispatch failed', reason_code=reason)


def _remaining():
    from .request_policy import current_provider_deadline, provider_clock_time, provider_timeout
    provider_timeout('edgar')
    deadline = current_provider_deadline('edgar')
    if deadline is None:
        raise ValueError('An inherited EDGAR deadline is required')
    return deadline - provider_clock_time('edgar')


class _Dispatcher:
    def __init__(self):
        self.children = []
        self._condition = threading.Condition()
        self._cleanup_done = threading.Event()
        self._available = []
        self._io_locks = {}
        self._closed = False
        self._failed = None
        self._sequence = 0
        self._child_leases = {}
        deadline = time.monotonic() + _remaining()
        try:
            for _ in range(2):
                _remaining()
                child = subprocess.Popen(
                    [sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--worker', repr(deadline)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env={'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8'},
                    close_fds=True, bufsize=0)
                self.children.append(child)
                self._io_locks[child] = threading.Lock()
                os.set_blocking(child.stdin.fileno(), False)
                os.set_blocking(child.stdout.fileno(), False)
                self._available.append(child)
            _remaining()
        except BaseException:
            self.close(failed='provider_error')
            raise

    def _check(self):
        _remaining()
        if self._closed:
            raise _failure(self._failed or 'provider_error')
        if any(child.poll() is not None for child in self.children):
            raise _failure()

    def parse(self, raw, arguments):
        child = None
        started, ordinary_rejection = False, False
        try:
            from .filing_spool import CompletedSubmission, current_submission_spool
            receipt = isinstance(raw, CompletedSubmission)
            if receipt:
                if raw.owner is not current_submission_spool():
                    raise _failure('invalid_response')
                raw.owner.validate_completed(raw)
                from .filing_evidence import _form
                if (raw.identity['accession'] != arguments['expected_accession']
                        or _form(raw.identity['form']) != _form(arguments['expected_form'])
                        or raw.identity['filing_date'] != arguments['expected_date']
                        or raw.observed_at != arguments['observed_at']):
                    raise _failure('invalid_response')
                size, digest = raw.size, raw.sha256
            elif isinstance(raw, bytes) and 0 < len(raw) <= _RAW_LIMIT:
                size, digest = len(raw), hashlib.sha256(raw).hexdigest()
            else:
                raise _failure('invalid_response')
            with self._condition:
                while not self._available:
                    self._check()
                    self._condition.wait(.05)
                self._check()
                child = self._available.pop(0)
                self._sequence += 1
                job = self._sequence
                if receipt:
                    self._child_leases[child] = raw.owner.reserve_child_copy(raw)
            metadata = dict(arguments, job=job, raw_size=size, raw_sha256=digest)
            if receipt:
                metadata['spooled'] = True
                metadata['max_document_bytes'] = _DOCUMENT_LIMIT
            started = True
            _frame(child.stdin.fileno(), metadata, check=self._check, limit=_META_LIMIT,
                   io_lock=self._io_locks[child])
            if receipt:
                with raw.owner.open_completed(raw) as source:
                    remaining = size
                    while remaining:
                        self._check()
                        chunk = source.read(min(65536, remaining))
                        if not chunk:
                            raise ValueError('incomplete parser source')
                        _transfer(child.stdin.fileno(), chunk, write=True, check=self._check,
                                  io_lock=self._io_locks[child])
                        remaining -= len(chunk)
                    if source.read(1):
                        raise ValueError('parser source size changed')
            else:
                _transfer(child.stdin.fileno(), raw, write=True, check=self._check, io_lock=self._io_locks[child])
            response = _read_frame(child.stdout.fileno(), check=self._check, limit=_RESULT_LIMIT,
                                   io_lock=self._io_locks[child])
            self._check()
            if (not isinstance(response, dict) or type(response.get('job')) is not int
                    or response.get('job') != job):
                raise ValueError('parser response identity')
            if receipt:
                if response.pop('closed', None) is not True:
                    raise ValueError('parser copy closure missing')
                self._release_child(child)
            if set(response) == {'job', 'error'} and response['error'] == 'invalid_evidence':
                ordinary_rejection = True
                raise _failure('invalid_response')
            if set(response) != {'job', 'result'} or not isinstance(response['result'], dict):
                raise ValueError('parser response schema')
            result = response['result']
            from .filing_evidence import _form
            identities = {'accession': arguments['expected_accession'], 'form': _form(arguments['expected_form']),
                'filing_date': arguments['expected_date'], 'observed_at': arguments['observed_at'],
                'submission_sha256': digest}
            if any(result.get(key) != value for key, value in identities.items()):
                raise ValueError('parser evidence identity')
            self._check()
            return result
        except BaseException as exc:
            from .fetch_errors import SourceFetchError
            if (isinstance(exc, SourceFetchError) and exc.reason_code == 'invalid_response'
                    and (not started or ordinary_rejection)):
                raise  # A rejected filing does not poison otherwise healthy children.
            reason = self._failed or (exc.reason_code if isinstance(exc, SourceFetchError) else 'provider_error')
            self.close(failed=reason)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise _failure(reason) from None
        finally:
            if child is not None:
                with self._condition:
                    if not self._closed:
                        self._available.append(child)
                    self._condition.notify_all()

    def _release_child(self, child):
        with self._condition:
            lease = self._child_leases.pop(child, None)
            if lease is not None:
                lease.record.owner.release_child_copy(lease)

    def close(self, *, failed=None):
        with self._condition:
            already_closed = self._closed
            if not already_closed:
                self._closed = True
                self._failed = failed
                self._condition.notify_all()
        if already_closed:
            # A hydration owner may arrive while a failed worker is reaping.
            if not self._cleanup_done.wait(3):
                raise _failure()
            return
        # No descendant enumeration, IPC namespace, or cleanup of unowned PIDs.
        unreaped = False
        try:
            for child in self.children:
                if child.poll() is None:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
            for child in self.children:
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    unreaped = True
                with self._io_locks[child]:
                    for pipe in (child.stdin, child.stdout):
                        if pipe is not None:
                            pipe.close()
                if child.poll() is not None:
                    self._release_child(child)
        finally:
            self._cleanup_done.set()
        if unreaped:
            raise _failure()


@contextmanager
def parser_scope():
    """Own two children for one hydration call inside its original EDGAR budget.

    Exit only cleans up: partial hydration graphs remain inspectable after
    deadline exhaustion. Each dispatch and acquisition still reject late output;
    the caller retains responsibility for final graph acceptance under its budget.
    """
    if _CURRENT.get() is not None:
        raise ValueError('Nested SEC parser scope is not supported')
    owner = _Dispatcher()
    token = _CURRENT.set(owner)
    try:
        yield owner
    finally:
        _CURRENT.reset(token)
        owner.close()


def current_dispatcher():
    return _CURRENT.get()


def dispatch_evidence(raw, **arguments):
    owner = _CURRENT.get()
    if owner is None:
        raise ValueError('SEC parser scope is not active')
    return owner.parse(raw, arguments)


def _worker(deadline):
    """Pure child entry; direct file import avoids eager provider package imports."""
    if not math.isfinite(deadline) or deadline <= time.monotonic():
        return 124
    signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
    signal.setitimer(signal.ITIMER_REAL, deadline - time.monotonic())
    violations = []

    def deny(event, args):
        if event.startswith(('socket.', 'subprocess.')) or event in ('os.system', 'os.fork', 'os.posix_spawn'):
            violations.append(event)
            raise RuntimeError('SEC parser acquisition forbidden')
    sys.addaudithook(deny)
    def forbidden(*a, **k):
        violations.append('thread')
        raise RuntimeError('SEC parser thread forbidden')
    threading.Thread.start = forbidden
    import builtins
    native_import = builtins.__import__
    def pure_import(name, *a, **k):
        if name.split('.')[0] in {'requests', 'httpx', 'openai', 'anthropic', 'langchain', 'openbb', 'yfinance', 'curl_cffi'}:
            violations.append('provider_import')
            raise RuntimeError('SEC parser provider import forbidden')
        return native_import(name, *a, **k)
    builtins.__import__ = pure_import
    spec = importlib.util.spec_from_file_location('_sec_pure_evidence', Path(__file__).with_name('filing_evidence.py'))
    evidence = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evidence)
    def check():
        if time.monotonic() >= deadline or violations:
            raise RuntimeError('SEC parser boundary failed')
    for fd in (0, 1):
        os.set_blocking(fd, False)
    while True:
        # Clean EOF is only used when the owner closes; no partial request accepted.
        readable, _, _ = select.select([0], [], [], .05)
        check()
        if not readable:
            continue
        meta = _read_frame(0, check=check, limit=_META_LIMIT)
        required = {'job', 'raw_size', 'raw_sha256', 'expected_accession', 'expected_form',
            'expected_date', 'observed_at', 'max_submission_bytes', 'required_exhibits'}
        spooled = isinstance(meta, dict) and meta.get('spooled') is True
        allowed = required | {'spooled', 'max_document_bytes'} if spooled else required
        raw_limit = _SPOOL_LIMIT if spooled else _RAW_LIMIT
        if (not isinstance(meta, dict) or set(meta) != allowed or type(meta['job']) is not int
                or meta['job'] < 1 or type(meta['raw_size']) is not int
                or not 0 < meta['raw_size'] <= raw_limit
                or type(meta['max_submission_bytes']) is not int
                or not 0 < meta['max_submission_bytes'] <= raw_limit
                or (spooled and (type(meta['max_document_bytes']) is not int
                                 or meta['max_document_bytes'] != _DOCUMENT_LIMIT))
                or not isinstance(meta['required_exhibits'], list)):
            raise ValueError('SEC parser request invalid')
        size, digest = meta.pop('raw_size'), meta.pop('raw_sha256')
        job = meta.pop('job')
        exhibits = meta.pop('required_exhibits')
        if spooled:
            meta.pop('spooled')
            document_limit = meta.pop('max_document_bytes')
            # No parent pathname crosses IPC. The child owns one anonymous file
            # whose full exact size has already been reserved by the parent.
            # Native launch checks this exact filesystem. Never silently move
            # child storage to tempfile's /var/tmp or cwd fallback candidates.
            with tempfile.TemporaryFile(mode='w+b', buffering=0, dir='/tmp') as source:
                remaining, actual = size, hashlib.sha256()
                while remaining:
                    chunk = _transfer(0, min(65536, remaining), check=check)
                    offset = 0
                    while offset < len(chunk):
                        check()
                        written = source.write(chunk[offset:])
                        if not written:
                            raise ValueError('incomplete parser temporary write')
                        offset += written
                    actual.update(chunk)
                    remaining -= len(chunk)
                    check()
                if actual.hexdigest() != digest or source.tell() != size:
                    raise ValueError('SEC parser request hash invalid')
                check()
                with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                    try:
                        framed = evidence.frame_submission(mapped, **meta, check=check)
                        selected = evidence.select_primary(framed, mapped, check=check)
                        result = evidence.build_evidence_from_buffer(selected, mapped,
                            required_exhibits=exhibits, max_document_bytes=document_limit, check=check)
                        check()
                        response = {'job': job, 'result': result}
                    except evidence.EvidenceError:
                        response = {'job': job, 'error': 'invalid_evidence'}
            # The acknowledgement is constructed only after mmap and file exit.
            response['closed'] = True
            check()
        else:
            raw = _transfer(0, size, check=check)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('SEC parser request hash invalid')
            try:
                parsed = evidence.parse_submission(raw, **meta)
                check()
                result = evidence.build_evidence(parsed, required_exhibits=exhibits)
                check()
                response = {'job': job, 'result': result}
            except evidence.EvidenceError:
                response = {'job': job, 'error': 'invalid_evidence'}
            check()
        check()
        _frame(1, response, check=check, limit=_RESULT_LIMIT)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--worker':
        raise SystemExit(2)
    try:
        raise SystemExit(_worker(float(sys.argv[2])))
    except Exception:
        raise SystemExit(1) from None
