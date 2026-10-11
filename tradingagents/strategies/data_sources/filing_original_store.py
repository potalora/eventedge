"""Private, descriptor-bound original views under one inherited replay budget.

Persistent payloads are always charged; a gzip store reserves one complete raw
original before allocating anonymous scratch, and releases it only after close.
Receipts are measured separately (the capture quota owns their charge).
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import zlib

PHYSICAL_LIMIT = 4 * 1024**3
MAX_ORIGINAL_BYTES = 512 * 1024**2
CHUNK_BYTES = 65536
MAX_RECEIPT_BYTES = 65536
_SOURCE_FIELDS = {'identity', 'observed_at', 'size', 'sha256', 'complete'}
_RECEIPT_FIELDS = _SOURCE_FIELDS | {'contract', 'encoding', 'stored_name', 'stored_size', 'stored_sha256'}
_GZIP_HEADER = b'\x1f\x8b\x08\x00\x00\x00\x00\x00\x04\xff'


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _file_identity(info):
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _directory_identity(fd):
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino, info.st_uid, info.st_mode)


def _safe_path(path):
    _require(isinstance(path, (str, Path)), 'invalid_raw_path')
    path = Path(path)
    _require(path.is_absolute() and len(path.parts) > 1 and '..' not in path.parts, 'invalid_raw_path')
    return path


class _DescriptorOwner:
    """Track allocations and uncertain closes without retrying reused numbers."""

    def __init__(self):
        self.live = set()
        self.uncertain = []

    def open(self, *args, **kwargs):
        fd = os.open(*args, **kwargs)
        self.live.add(fd)
        return fd

    def close(self, fd, close=None):
        _require(fd in self.live, 'descriptor_close_already_attempted')
        self.live.remove(fd)
        try:
            (os.close if close is None else close)(fd)
        except BaseException as error:
            self.uncertain.append(fd)
            # A constructor/helper may not return its owner to a caller. The
            # failure itself preserves ownership evidence in that case.
            error.descriptor_owner = self
            raise


class _DescriptorScope:
    def __init__(self, owner):
        self.owner, self.owned = owner, set()

    def __enter__(self):
        return self

    def open(self, *args, **kwargs):
        fd = self.owner.open(*args, **kwargs)
        self.owned.add(fd)
        return fd

    def close(self, fd):
        self.owned.remove(fd)
        self.owner.close(fd)

    def transfer(self, fd):
        self.owned.remove(fd)
        return fd

    def __exit__(self, *args):
        failure = None
        for fd in tuple(self.owned):
            try:
                self.close(fd)
            except BaseException as error:
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure


def _open_original(path, size, check, *, descriptor_owner=None):
    """Walk descriptors; close all directories before transferring the leaf."""
    path = _safe_path(path)
    owner = _DescriptorOwner() if descriptor_owner is None else descriptor_owner
    with _DescriptorScope(owner) as scope:
        directory = scope.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        ancestors = [_directory_identity(directory)]
        for component in path.parts[1:-1]:
            check()
            child = scope.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                               dir_fd=directory)
            scope.close(directory)
            directory = child
            ancestors.append(_directory_identity(directory))
        check()
        before = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        _require(stat.S_ISREG(before.st_mode), 'raw_capture_not_regular')
        fd = scope.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        actual = os.fstat(fd)
        _require((before.st_dev, before.st_ino) == (actual.st_dev, actual.st_ino), 'raw_capture_identity_changed')
        _require(stat.S_ISREG(actual.st_mode) and actual.st_uid == os.getuid()
                 and stat.S_IMODE(actual.st_mode) in {0o400, 0o600}
                 and actual.st_size > 0 and (size is None or actual.st_size == size), 'unsafe_raw_capture')
        scope.close(directory)
        scope.transfer(fd)
        return fd, actual, tuple(ancestors)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'duplicate_original_receipt_key')
        result[key] = value
    return result


def _validate_originals(originals, check):
    _require(isinstance(originals, dict), 'invalid_original_inventory')
    for accession, original in originals.items():
        check()
        _require(isinstance(accession, str) and re.fullmatch(r'[0-9]{10}-[0-9]{2}-[0-9]{6}', accession),
                 'invalid_original_accession')
        _require(isinstance(original, dict) and set(original) == _SOURCE_FIELDS
                 and original['complete'] is True and type(original['size']) is int
                 and 0 < original['size'] <= MAX_ORIGINAL_BYTES
                 and isinstance(original['sha256'], str) and re.fullmatch('[0-9a-f]{64}', original['sha256'])
                 and isinstance(original['observed_at'], str)
                 and datetime.fromisoformat(original['observed_at']).utcoffset() is not None,
                 'invalid_complete_original')
        identity = original['identity']
        _require(isinstance(identity, dict) and set(identity) == {'accession', 'form', 'filing_date', 'source_url'}
                 and identity['accession'] == accession
                 and all(isinstance(value, str) and value for value in identity.values()), 'invalid_original_identity')


def _open_directory(path, check, *, descriptor_owner=None):
    path = _safe_path(path)
    owner = _DescriptorOwner() if descriptor_owner is None else descriptor_owner
    with _DescriptorScope(owner) as scope:
        directory = scope.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        ancestors = [_directory_identity(directory)]
        for component in path.parts[1:]:
            check()
            child = scope.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                               dir_fd=directory)
            scope.close(directory)
            directory = child
            ancestors.append(_directory_identity(directory))
        info = os.fstat(directory)
        _require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, 'unsafe_scratch_directory')
        scope.transfer(directory)
        return directory, tuple(ancestors)


class _AnonymousScratch:
    """Own the pinned directory and every partially allocated scratch resource."""

    def __init__(self, directory, check, descriptor_owner=None):
        self._directory, self._check = directory, check
        self._owner = _DescriptorOwner() if descriptor_owner is None else descriptor_owner
        self._owner.live.add(directory)
        self._fd = self._file = self._name = self._identity = None
        self._anonymous = self._created = self._close_uncertain = False

    def allocate(self):
        self._check()
        self._name = 'eventedge-original-' + secrets.token_hex(16)
        self._fd = self._owner.open(self._name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                           0o600, dir_fd=self._directory)
        self._created = True
        info = os.fstat(self._fd)
        self._identity = (info.st_dev, info.st_ino)
        self._file = os.fdopen(self._fd, 'w+b', buffering=0)
        self._fd = None
        self._unlink_owned()

    def _unlink_owned(self):
        if self._created and not self._anonymous:
            _require(self._identity is not None, 'scratch_identity_unverified')
            info = os.stat(self._name, dir_fd=self._directory, follow_symlinks=False)
            _require((info.st_dev, info.st_ino) == self._identity, 'scratch_inode_changed')
            os.unlink(self._name, dir_fd=self._directory)
            self._anonymous = True

    def fileno(self):
        return self._file.fileno()

    def write(self, value):
        return self._file.write(value)

    def flush(self):
        return self._file.flush()

    @property
    def closed(self):
        return (not self._close_uncertain and self._directory is None and self._fd is None
                and (self._file is None or self._file.closed))

    def close(self):
        if self._fd is not None or self._file is not None:
            self._created = True
        try:
            if self._file is not None and not self._file.closed:
                fd = self._file.fileno()
                self._owner.close(fd, lambda ignored: self._file.close())
                _require(self._file.closed, 'scratch_close_uncertain')
            elif self._fd is not None:
                self._owner.close(self._fd)
                self._fd = None
            self._unlink_owned()
            if self._directory is not None:
                self._owner.close(self._directory)
                self._directory = None
        except BaseException:
            self._close_uncertain = True
            raise


class RawOriginalStore:
    storage_format = 'raw_original_v1'

    def __init__(self, expected_originals, raw_paths, *, check, physical_limit=PHYSICAL_LIMIT):
        self._initialize(expected_originals, raw_paths, check, physical_limit)
        self._sizes = {acc: original['size'] for acc, original in self._expected.items()}
        self._receipts = {}
        self._receipt_paths = {}
        self._register_payloads()

    def _initialize(self, expected_originals, paths, check, physical_limit):
        _require(callable(check), 'missing_raw_audit_deadline')
        self.check_callback = check
        self._check()
        _validate_originals(expected_originals, self._check)
        _require(type(physical_limit) is int and 0 < physical_limit <= PHYSICAL_LIMIT, 'invalid_physical_limit')
        self._expected = deepcopy(expected_originals)
        _require(isinstance(paths, dict) and set(paths) == set(self._expected), 'incomplete_raw_capture_manifest')
        self._paths = {acc: _safe_path(path) for acc, path in paths.items()}
        self._identities = {}
        self._artifact_inodes = set()
        self._physical_limit = physical_limit
        self._retained_bytes = self._receipt_bytes = self._scratch_bytes = self._peak_total_bytes = 0
        self._active = self._cleanup_failures = 0
        self._lock = threading.RLock()
        self._descriptor_owner = _DescriptorOwner()
        self._final_state = 'open'
        self._failed_resources = []

    @property
    def expected_originals(self):
        return deepcopy(self._expected)

    def _check(self):
        remaining = self.check_callback()
        _require(type(remaining) in (int, float) and math.isfinite(remaining) and remaining > 0,
                 'raw_audit_deadline_exhausted')
        return remaining

    def _register(self, kind, accession, path, size):
        fd, info, ancestors = _open_original(path, size, self._check, descriptor_owner=self._descriptor_owner)
        try:
            identity = (_file_identity(info), ancestors)
            inode = identity[0][:2]
            _require(inode not in self._artifact_inodes, 'duplicate_original_artifact')
            self._artifact_inodes.add(inode)
            self._identities[kind, accession] = identity
            return fd, info
        except BaseException:
            self._descriptor_owner.close(fd)
            raise

    def _register_payloads(self):
        for acc in sorted(self._expected):
            fd, info = self._register('payload', acc, self._paths[acc], self._sizes[acc])
            self._descriptor_owner.close(fd)
            self._retained_bytes += info.st_size
            _require(self._retained_bytes <= self._physical_limit, 'original_store_physical_limit')
        self._peak_total_bytes = self._retained_bytes

    def _open_bound(self, kind, accession, path, size):
        fd, info, ancestors = _open_original(path, size, self._check, descriptor_owner=self._descriptor_owner)
        try:
            _require((_file_identity(info), ancestors) == self._identities[kind, accession],
                     'raw_capture_path_changed_during_audit')
            return fd, info
        except BaseException:
            self._descriptor_owner.close(fd)
            raise

    def _begin(self, accession, scratch_bytes=0):
        with self._lock:
            _require(self._final_state == 'open', 'original_store_sealed')
            _require(accession in self._expected, 'unknown_original_accession')
            _require(not self._active and not self._scratch_bytes and not self._cleanup_failures
                     and not self._descriptor_owner.uncertain, 'concurrent_or_unclean_original_open')
            _require(self._retained_bytes + scratch_bytes <= self._physical_limit, 'original_store_physical_limit')
            self._active = 1
            self._scratch_bytes = scratch_bytes
            self._peak_total_bytes = max(self._peak_total_bytes, self._retained_bytes + scratch_bytes)
        # Never invoke caller code while holding admission's owner lock.
        try:
            self._check()
        except BaseException:
            self._end(clean=True)
            raise

    def _end(self, *, clean):
        with self._lock:
            if clean:
                self._active = self._scratch_bytes = 0
            else:
                self._cleanup_failures += 1

    def _finish_original(self, fd, raw, scratch=None):
        failures = []
        if fd is not None:
            try:
                self._descriptor_owner.close(fd)
            except BaseException as error:
                failures.append(error)
        if scratch is not None:
            try:
                scratch.close()
            except BaseException as error:
                failures.append(error)
        clean = not failures and (raw is None or raw.closed) and (scratch is None or scratch.closed)
        if not clean:
            # Keep live mappings/file owners reachable; GC must not silently
            # erase evidence of an unconfirmed close.
            self._failed_resources.extend(resource for resource in (raw, scratch) if resource is not None)
        self._end(clean=clean)
        if failures:
            raise failures[0]

    def _hash_view(self, raw, original):
        digest = hashlib.sha256()
        for start in range(0, len(raw), CHUNK_BYTES):
            self._check()
            digest.update(raw[start:start + CHUNK_BYTES])
        _require(len(raw) == original['size'] and digest.hexdigest() == original['sha256'],
                 'raw_original_hash_mismatch')

    @contextmanager
    def open_original(self, accession):
        self._begin(accession)
        fd = None
        raw = None
        try:
            fd, before = self._open_bound('payload', accession, self._paths[accession], self._sizes[accession])
            with mmap.mmap(fd, 0, access=mmap.ACCESS_READ) as raw:
                self._hash_view(raw, self._expected[accession])
                yield raw
                self._check()
                _require(_file_identity(os.fstat(fd)) == _file_identity(before), 'raw_capture_changed_during_audit')
        finally:
            self._finish_original(fd, raw)

    def _assert_closed_locked(self):
        _require(not self._active and not self._scratch_bytes and not self._cleanup_failures
                 and not self._descriptor_owner.live and not self._descriptor_owner.uncertain,
                 'original_store_scratch_not_closed')

    def assert_closed(self):
        with self._lock:
            self._assert_closed_locked()

    def stats(self):
        with self._lock:
            failures = self._cleanup_failures + len(self._descriptor_owner.uncertain)
            return {'storage_format': self.storage_format, 'retained_bytes': self._retained_bytes,
                    'receipt_bytes': self._receipt_bytes, 'scratch_bytes': self._scratch_bytes,
                    'total_bytes': self._retained_bytes + self._scratch_bytes,
                    'peak_total_bytes': self._peak_total_bytes, 'active_originals': self._active,
                    'scratch_closed': not self._active and not self._scratch_bytes and not failures
                        and not self._descriptor_owner.live,
                    'cleanup_failures': failures, 'sealed': self._final_state == 'sealed'}

    def sealed_stats(self):
        with self._lock:
            _require(self._final_state == 'sealed', 'original_store_not_verified_and_sealed')
            self._assert_closed_locked()
            return self.stats()

    def recheck_persistent_identities(self):
        # Admission closes permanently BEFORE any traversal or external callback.
        # A repeated successful recheck is allowed; a failure is terminal.
        with self._lock:
            _require(self._final_state in {'open', 'sealed'}, 'original_store_final_verification_failed')
            self._final_state = 'sealing'
        try:
            self.assert_closed()
            for (kind, acc), _ in sorted(self._identities.items()):
                paths = self._paths if kind == 'payload' else self._receipt_paths
                size = self._sizes[acc] if kind == 'payload' else None
                fd, _ = self._open_bound(kind, acc, paths[acc], size)
                self._descriptor_owner.close(fd)
            self._check()
            with self._lock:
                self._assert_closed_locked()
                self._final_state = 'sealed'
                return self.stats()
        except BaseException:
            with self._lock:
                self._final_state = 'failed'
            raise


class GzipOriginalStore(RawOriginalStore):
    storage_format = 'gzip_original_v1'

    def __init__(self, expected_originals, stored_paths, receipt_paths, *, check,
                 physical_limit=PHYSICAL_LIMIT, scratch_directory=None):
        self._initialize(expected_originals, stored_paths, check, physical_limit)
        _require(isinstance(receipt_paths, dict) and set(receipt_paths) == set(self._expected),
                 'incomplete_original_receipt_manifest')
        self._receipt_paths = {acc: _safe_path(path) for acc, path in receipt_paths.items()}
        self._sizes, self._receipts = {}, {}
        for acc, original in sorted(self._expected.items()):
            path = self._receipt_paths[acc]
            _require(path.name == f'sec-original-{acc}.json', 'invalid_original_receipt_name')
            fd, info = self._register('receipt', acc, path, None)
            try:
                _require(info.st_size <= MAX_RECEIPT_BYTES, 'oversized_original_receipt')
                self._check()
                content = os.read(fd, MAX_RECEIPT_BYTES + 1)
                _require(len(content) == info.st_size and _file_identity(os.fstat(fd)) == _file_identity(info),
                         'original_receipt_changed')
                receipt = json.loads(content, object_pairs_hook=_unique_object)
            finally:
                self._descriptor_owner.close(fd)
            _require(isinstance(receipt, dict) and set(receipt) == _RECEIPT_FIELDS
                     and receipt['contract'] == 'sec-original-v2' and receipt['encoding'] == self.storage_format
                     and {key: receipt[key] for key in _SOURCE_FIELDS} == original
                     and receipt['complete'] is True and type(receipt['size']) is int
                     and receipt['stored_name'] == f'sec-original-{acc}.bin.gz'
                     and self._paths[acc].name == receipt['stored_name']
                     and type(receipt['stored_size']) is int and 0 < receipt['stored_size'] <= physical_limit
                     and isinstance(receipt['stored_sha256'], str)
                     and re.fullmatch('[0-9a-f]{64}', receipt['stored_sha256']), 'invalid_original_receipt')
            self._receipt_bytes += info.st_size
            self._receipts[acc] = receipt
            self._sizes[acc] = receipt['stored_size']
        self._register_payloads()
        _require(scratch_directory is not None, 'missing_original_scratch_directory')
        scratch = _safe_path(scratch_directory)
        for path in (*self._paths.values(), *self._receipt_paths.values()):
            self._check()
            _require(not scratch.is_relative_to(path.parent) and not path.parent.is_relative_to(scratch),
                     'scratch_inside_retained_evidence')
        directory, ancestors = _open_directory(scratch, self._check, descriptor_owner=self._descriptor_owner)
        try:
            self._scratch_directory_identity = ancestors
        finally:
            self._descriptor_owner.close(directory)
        self._scratch_directory = scratch

    def _open_scratch_directory(self):
        directory, ancestors = _open_directory(self._scratch_directory, self._check, descriptor_owner=self._descriptor_owner)
        try:
            _require(ancestors == self._scratch_directory_identity, 'scratch_directory_changed')
            return directory
        except BaseException:
            self._descriptor_owner.close(directory)
            raise

    def _recheck_scratch_directory(self):
        directory = self._open_scratch_directory()
        self._descriptor_owner.close(directory)

    def _decode(self, fd, scratch, accession):
        original, receipt = self._expected[accession], self._receipts[accession]
        encoded_digest, raw_digest = hashlib.sha256(), hashlib.sha256()
        encoded_size = raw_size = 0
        decoder = zlib.decompressobj(wbits=31)
        while True:
            self._check()
            chunk = os.read(fd, CHUNK_BYTES)
            if not chunk:
                break
            if encoded_size == 0:
                _require(chunk.startswith(_GZIP_HEADER), 'noncanonical_gzip_original_header')
            encoded_size += len(chunk)
            _require(encoded_size <= receipt['stored_size'] and not decoder.eof, 'trailing_gzip_original_bytes')
            encoded_digest.update(chunk)
            pending = chunk
            while pending:
                self._check()
                try:
                    output = decoder.decompress(pending, min(CHUNK_BYTES, original['size'] - raw_size + 1))
                except zlib.error as error:
                    raise ValueError('invalid_gzip_original_stream') from error
                pending = decoder.unconsumed_tail
                raw_size += len(output)
                _require(raw_size <= original['size'], 'gzip_original_expansion_limit')
                if output:
                    self._check()
                    _require(scratch.write(output) == len(output), 'short_original_scratch_write')
                    raw_digest.update(output)
                _require(not decoder.unused_data, 'trailing_gzip_original_bytes')
        self._check()
        _require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail, 'truncated_gzip_original')
        _require(encoded_size == receipt['stored_size'] and encoded_digest.hexdigest() == receipt['stored_sha256'],
                 'stored_original_hash_mismatch')
        _require(raw_size == original['size'] and raw_digest.hexdigest() == original['sha256'],
                 'raw_original_hash_mismatch')
        scratch.flush()
        _require(os.fstat(scratch.fileno()).st_size == original['size'], 'original_scratch_size_mismatch')

    @contextmanager
    def open_original(self, accession):
        _require(accession in self._expected, 'unknown_original_accession')
        self._begin(accession, self._expected[accession]['size'])
        fd = scratch = raw = None
        try:
            fd, before = self._open_bound('payload', accession, self._paths[accession], self._sizes[accession])
            self._check()
            directory = self._open_scratch_directory()
            try:
                scratch = _AnonymousScratch(directory, self._check, self._descriptor_owner)
            except BaseException:
                self._descriptor_owner.close(directory)
                raise
            scratch.allocate()
            info = os.fstat(scratch.fileno())
            _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                     and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 0, 'unsafe_original_scratch')
            self._decode(fd, scratch, accession)
            with mmap.mmap(scratch.fileno(), 0, access=mmap.ACCESS_READ) as raw:
                yield raw
                self._check()
                _require(_file_identity(os.fstat(fd)) == _file_identity(before), 'raw_capture_changed_during_audit')
                self._recheck_scratch_directory()
        finally:
            self._finish_original(fd, raw, scratch)
